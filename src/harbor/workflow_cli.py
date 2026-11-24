"""Public workflow operations using fresh scoped external evidence."""
import json
import hashlib
from pathlib import Path

from harbor.config.workflow import load_workflow
from harbor.hooks import execute_event
from harbor.workflow_store import bind, record


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
    from harbor.clients.linear import LinearClient
    from harbor.clients.github import GitHubClient
    from harbor.poll.linear_poller import normalize_issue
    from harbor.poll.github_poller import GitHubPoller
    from harbor.pr_events import PREventProcessor
    from harbor.project_identity import require_scope
    linear, github = LinearClient(endpoint=config.linear_api_url), GitHubClient()
    try:
        raw = await linear.fetch_blocker_context(ticket_id)
        if raw.get("identifier") != ticket_id:
            raise ValueError("Linear ticket identity mismatch")
        await require_scope(linear, config.linear_project_scope, raw.get("project", {}))
        retained = next((t for t in db.tickets(config.name) if t["identifier"] == ticket_id), None)
        if retained is None or not retained.get("owned"):
            raise ValueError("ticket has no custody in this project")
        ticket = normalize_issue(raw, hold_all=retained.get("hold", True))
        if not ticket:
            raise ValueError("ticket state unavailable")
        processor = PREventProcessor(db, project=config.name, profile=config)
        poller = GitHubPoller(github, profile=config, db=db, project=config.name, event_processor=processor)
        facts = await poller.poll([ticket])
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
        from harbor.risk_evidence import observed_floor, retain_floor
        feedback_digest = hashlib.sha256(json.dumps([raw.get("comments"),fact.get("comments")],sort_keys=True).encode()).hexdigest()
        risk = retain_floor(db.connection,config.name,ticket_id,run["id"],observed_floor(raw,{}))
        return {"project":config.name,"ticket":ticket_id,"run_id":run["id"],"head_sha":fact.get("head_sha"),
                "event":event,"fresh":True,"terminal":ticket["state_type"] == "completed",
                "hold":bool(inactive_merge or ticket["hold"] or ticket["blocked_by"] or ticket["state_type"] == "canceled"),
                "facts":fact,"gaps":processor.handoff_gaps(ticket,fact["url"],head_sha=fact.get("head_sha")),
                "linear_feedback":linear_feedback,"feedback_digest":feedback_digest,"risk":risk}
    finally:
        await linear.close()
        await github.close()


async def execute(args, config, db):
    workflow = load_workflow(Path(config.workflow_path))
    if args.workflow_command == "inspect":
        print(json.dumps({"effective":workflow.config,"package":str(workflow.package),"models":str(workflow.models_path),"digest":workflow.digest,"enforcement":"workflow_checked"},indent=2))
        return 0
    row = db.connection.execute("SELECT * FROM runs WHERE ticket=? ORDER BY started_at DESC LIMIT 1", (args.ticket,)).fetchone()
    ticket = next((t for t in db.tickets(config.name) if t["identifier"] == args.ticket), None)
    if row is None or ticket is None or not ticket.get("owned"):
        raise ValueError("no retained run for ticket in this project")
    run = dict(row)
    bind(db.connection,config.name,args.ticket,run["id"],workflow)
    if args.workflow_command == "checks":
        from harbor.checks import run_checks
        from harbor.worktree import WorktreeManager
        from harbor.cli import _worktree_root
        import subprocess
        manager = WorktreeManager(config.repo_path,root=_worktree_root(config),repo_name=config.name,custody=db.connection,install_command="none")
        worktree = manager.attach(args.ticket)
        head = subprocess.run(["git","-C",str(worktree.path),"rev-parse","HEAD"],check=True,capture_output=True,text=True).stdout.strip()
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
    print(json.dumps({"ready":not blocked,"event":args.event,"head_sha":context["head_sha"],"linear_feedback":context.get("linear_feedback",[]),"feedback_digest":context.get("feedback_digest"),"results":results,"enforcement":"workflow_checked"},indent=2))
    return int(blocked)
