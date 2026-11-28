"""Public workflow operations using fresh scoped external evidence."""
import json
import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path

import httpx

from harbor.config.workflow import load_workflow
from harbor.hooks import execute_event
from harbor.workflow_store import bind, record

WORKFLOW_CHECK_TOKEN_HELP = (
    "workflow check cannot read GitHub: {problem}. It uses the project's GitHub token: inside a "
    "builder, the variable harbor start named; from a terminal, export the variable named by the "
    "project's github_token_env (or GITHUB_TOKEN or GH_TOKEN when github_token_env is not set), "
    "then run it again."
)


def _http_status(exc):
    """The HTTP status of an httpx status error, directly or as a chained cause; else None."""
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code
        exc, seen = exc.__cause__, seen + 1
    return None


def _one_line(exc):
    """An exception's message on one line (httpx status errors span several)."""
    return " ".join(str(exc).split()) or type(exc).__name__


_LINEAR_NOT_FOUND = ("not found", "cannot verify")
_LINEAR_DENIED = ("authenticat", "unauthorized", "forbidden", "permission", "access denied")


def _linear_failure(ticket, exc):
    """One accurate line for a LinearClientError, by what actually went wrong.

    Only a missing ticket or a refused key earns the identifier/API-key hint;
    a timeout, 5xx or malformed/partial page is a Linear-side failure to retry,
    and saying otherwise sends the operator (and the supervisor's escalation
    summary, via workflow_lifecycle.handoff) after the wrong fix.
    """
    detail = _one_line(exc)
    lowered = detail.casefold()
    cause = exc.__cause__
    status = cause.response.status_code if isinstance(cause, httpx.HTTPStatusError) else None
    if status in {401, 403} or any(word in lowered for word in _LINEAR_DENIED):
        hint = "check that the Linear API key (LINEAR_API_KEY or HARBOR_LINEAR_API_KEY) is valid and can read it"
    elif status == 404 or (status is None and any(word in lowered for word in _LINEAR_NOT_FOUND)):
        hint = "check the ticket identifier and that the Linear API key can read it"
    elif isinstance(cause, httpx.HTTPError):
        hint = "Linear is unreachable or returned an error; retry once Linear responds"
    else:
        hint = "Linear returned incomplete or malformed ticket context; retry, and report it if it persists"
    return f"Linear could not provide ticket {ticket} ({detail}); {hint}"


def _attach_retained_worktree(config, db, ticket):
    """Attach the ticket's retained worktree and read its HEAD, or fail closed."""
    from harbor.cli import _worktree_root
    from harbor.worktree import WorktreeError, WorktreeManager
    try:
        manager = WorktreeManager(config.repo_path,root=_worktree_root(config),repo_name=config.name,custody=db.connection,install_command="none")
        worktree = manager.attach(ticket)
    except WorktreeError as exc:
        # Covers WorktreeConfinementError: a run Harbor never gave a worktree,
        # or one outside its root, must not be checked in some other checkout.
        raise ValueError(
            f"{ticket} has no retained Harbor worktree to check ({_one_line(exc)}); "
            f"run `harbor start {config.name}` so Harbor creates and records the ticket's worktree, "
            "then run workflow checks from it"
        ) from exc
    try:
        head = subprocess.run(["git","-C",str(worktree.path),"rev-parse","--verify","HEAD"],check=True,capture_output=True,text=True).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"the retained worktree for {ticket} has no readable HEAD commit "
            f"({_one_line(exc.stderr or exc)}); commit the work in {worktree.path} before running workflow checks"
        ) from exc
    return worktree, head


def add_commands(commands):
    workflow = commands.add_parser("workflow", help="inspect and validate configured workflows")
    subs = workflow.add_subparsers(dest="workflow_command", required=True)
    for name in ("inspect", "check", "checks"):
        cmd = subs.add_parser(name)
        cmd.add_argument("project")
        if name != "inspect":
            cmd.add_argument("--ticket", required=True)
        if name == "check":
            cmd.add_argument("--event", choices=["pre_handoff", "pre_merge"], required=True)
        if name == "checks":
            cmd.add_argument("--parallel", action="store_true")


async def fresh_context(config, db, ticket_id, run, event):
    from harbor.clients.linear import LinearClient, LinearClientError
    from harbor.clients.github import GitHubClient, GitHubError
    from harbor.poll.linear_poller import normalize_issue
    from harbor.poll.github_poller import GitHubPoller
    from harbor.pr_events import PREventProcessor
    from harbor.project_identity import require_scope
    from harbor.config.profile import PipelineProfile
    from harbor.github_rest import require_https_api_url
    # Build the GitHub client from the project's profile, as every other
    # consumer does, so the token only goes to the configured GitHub host,
    # and refuse a non-https endpoint before any request is made.
    from harbor.github_token import preferred_token_env, token_env_candidates
    profile = PipelineProfile.from_project(config)
    require_https_api_url(profile.github_api_url)
    # The project's token: inside a builder, the variable Harbor named in
    # HARBOR_GITHUB_TOKEN_ENV; from a terminal, github_token_env (strict) or
    # GITHUB_TOKEN/GH_TOKEN. Never gh, which hangs in the builder sandbox.
    github = GitHubClient.from_profile(profile, token_env=preferred_token_env(profile))
    linear = LinearClient(endpoint=config.linear_api_url)
    try:
        try:
            raw = await linear.fetch_blocker_context(ticket_id)
            if raw.get("identifier") != ticket_id:
                raise ValueError("Linear ticket identity mismatch")
            await require_scope(linear, config.linear_project_scope, raw.get("project", {}))
        except LinearClientError as exc:
            # Builders run this non-interactively: fail closed with one
            # actionable line instead of a traceback.
            raise ValueError(_linear_failure(ticket_id, exc)) from exc
        retained = next((t for t in db.tickets(config.name) if t["identifier"] == ticket_id), None)
        if retained is None or not retained.get("owned"):
            raise ValueError("ticket has no custody in this project")
        ticket = normalize_issue(raw, hold_all=retained.get("hold", True))
        if not ticket:
            raise ValueError("ticket state unavailable")
        # The same resolved profile the GitHub client uses (and `harbor poll`
        # and `harbor start` use), so the workflow check reads the same
        # repository and required checks.
        processor = PREventProcessor(db, project=config.name, profile=profile)
        poller = GitHubPoller(github, profile=profile, db=db, project=config.name, event_processor=processor)
        try:
            facts = await poller.poll([ticket])
        except (GitHubError, httpx.HTTPError) as exc:
            if _http_status(exc) == 401:
                raise ValueError(WORKFLOW_CHECK_TOKEN_HELP.format(
                    problem="GitHub answered 401 Unauthorized (the token is missing, expired or revoked)")) from exc
            # The default client lets transport failures through as httpx
            # errors, so both are a GitHub outage, never a "blocked" verdict.
            token_names = " or ".join(token_env_candidates(preferred_token_env(profile)))
            raise ValueError(
                f"GitHub could not provide the linked PR for {ticket_id} ({_one_line(exc)}); "
                f"check github_api_url, network access, and that the token in {token_names} "
                f"can read {profile.repository_owner}/{profile.repository_name}"
            ) from exc
        matches = [f for f in facts if f.get("url") == run.get("pr_url")] if run.get("pr_url") else facts
        if len(matches) != 1:
            raise ValueError("expected one unambiguous linked PR")
        fact = matches[0]
        states = load_workflow(Path(config.workflow_path)).config.get("states",{})
        inactive = {"paused","human review","backlog",*(str(s).casefold() for s in states.get("paused",[])),*(str(s).casefold() for s in states.get("terminal",[]))}
        inactive_merge = event == "pre_merge" and (
            ticket["state_type"] in {"backlog", "completed"}
            or str(ticket.get("state", "")).casefold() in inactive
        )
        from harbor.linear_feedback import pending
        linear_feedback = pending(raw)
        from harbor.risk_evidence import merge_risk
        feedback_digest = hashlib.sha256(json.dumps([raw.get("comments"),fact.get("comments")],sort_keys=True).encode()).hexdigest()
        risk = merge_risk(db.connection,config.name,ticket_id,run["id"],raw,fact)
        sandbox_change, sandbox_why = None, []
        if event == "pre_merge":
            # GitHub's own head and base SHAs, never local refs a builder could forge.
            # The git dir, fetch URL and token come from Harbor's project config and
            # environment, never from the worktree, whose .git file and config the builder can rewrite.
            from harbor.device_proof import builder_sandbox_change, git_fetch_url, main_git_dir
            from harbor.github_rest import token_env_name
            token_env = token_env_name(os.environ, profile.github_token_env)
            sandbox_change = builder_sandbox_change(
                main_git_dir(getattr(config, "repo_path", None)), fact.get("head_sha"), fact.get("base_sha"),
                fetch_url=git_fetch_url(profile.github_api_url, profile.repository if profile.repository_owner else None),
                token=os.environ[token_env].strip() if token_env else None, reasons=sandbox_why,
            )
        return {"project":config.name,"ticket":ticket_id,"run_id":run["id"],"head_sha":fact.get("head_sha"),
                "event":event,"fresh":True,"terminal":ticket["state_type"] == "completed",
                "hold":bool(inactive_merge or ticket["hold"] or ticket["blocked_by"] or ticket["state_type"] == "canceled"),
                "facts":fact,"gaps":processor.handoff_gaps(ticket,fact["url"],head_sha=fact.get("head_sha")),
                "linear_feedback":linear_feedback,"feedback_digest":feedback_digest,"risk":risk,
                "builder_sandbox_change":sandbox_change,"builder_sandbox_detail":"; ".join(sandbox_why)}
    finally:
        await linear.close()
        await github.close()


async def execute(args, config, db):
    workflow = load_workflow(Path(config.workflow_path))
    if args.workflow_command == "inspect":
        print(json.dumps({"effective":workflow.config,"package":str(workflow.package),"models":str(workflow.models_path),"digest":workflow.digest,"enforcement":"workflow_checked"},indent=2))
        return 0
    try:
        row = db.connection.execute("SELECT * FROM runs WHERE ticket=? ORDER BY started_at DESC LIMIT 1", (args.ticket,)).fetchone()
    except sqlite3.OperationalError as exc:
        if not str(exc).startswith("no such table"):
            raise
        # `harbor start` creates the runs table when it first supervises the
        # project, so an empty or unmigrated ledger has no run to check.
        raise ValueError(
            f"the Harbor ledger has no run records ({exc}); "
            f"run `harbor start {config.name}` so Harbor takes custody of {args.ticket} before workflow checks"
        ) from exc
    ticket = next((t for t in db.tickets(config.name) if t["identifier"] == args.ticket), None)
    if row is None or ticket is None or not ticket.get("owned"):
        raise ValueError("no retained run for ticket in this project")
    run = dict(row)
    bind(db.connection,config.name,args.ticket,run["id"],workflow)
    if args.workflow_command == "checks":
        from harbor.checks import run_checks
        worktree, head = _attach_retained_worktree(config, db, args.ticket)
        result = await run_checks(workflow,{"project":config.name,"ticket":args.ticket,"run_id":run["id"],"head_sha":head},cwd=worktree.path,parallel=args.parallel)
        for receipt in result["checks"].values():
            if "project" in receipt:
                record(db.connection,receipt)
        print(json.dumps(result,indent=2))
        return int(result["status"] == "fail")
    context = await fresh_context(config,db,args.ticket,run,args.event)
    results = await execute_event(workflow,args.event,context,cwd=run["worktree"])
    for result in results:
        record(db.connection,result)
    blocked = any(r["blocking"] for r in results)
    if args.event == "pre_merge" and workflow.config["delivery"]["merge_mode"] == "human":
        blocked = True
    if args.event == "pre_merge":
        # Enforced here as well as in harbor.readiness, so a project that replaces
        # its readiness hook still cannot auto-merge a [builder] sandbox change.
        from harbor.readiness import builder_sandbox_failures
        reasons = builder_sandbox_failures(context)
        if reasons:
            blocked = True
            detail = context.get("builder_sandbox_detail")
            results = [*results, {"hook": "builder_sandbox", "mode": "required", "head_sha": context.get("head_sha"),
                                  "status": "fail", "blocking": True,
                                  "reason": f"{reasons[0]} ({detail})" if detail else reasons[0]}]
    print(json.dumps({"ready":not blocked,"event":args.event,"head_sha":context["head_sha"],"linear_feedback":context.get("linear_feedback",[]),"feedback_digest":context.get("feedback_digest"),"results":results,"enforcement":"workflow_checked"},indent=2))
    return int(blocked)
