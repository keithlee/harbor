"""Command-line entry point for Harbor Phase 1."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from harbor.clients.github import GitHubClient
from harbor.clients.linear import LinearClient, LinearClientError
from harbor.config.profile import PipelineProfile
from harbor.config.projects import get_project
from harbor.console import Console
from harbor.db import HarborDB
from harbor.first_mate import FirstMate
from harbor.lease import ProjectLease, ProjectLeaseError
from harbor.notify import Notifier
from harbor.poll.github_poller import GitHubPoller
from harbor.poll.linear_poller import LinearPoller
from harbor.pr_events import PREventProcessor
from harbor.runner.codex import CodexRunner
from harbor.supervisor import Supervisor, WorktreeDispatcher
from harbor.wake import TicketScope
from harbor.worktree import WorktreeManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harbor", description="A calm project supervisor")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "start", "status", "facts"):
        command = commands.add_parser(name)
        command.add_argument("project")
    commands.choices["start"].add_argument("--once", action="store_true", help="poll once and print status")
    commands.choices["start"].add_argument(
        "--ticket",
        metavar="IDENTIFIER",
        help="limit builder custody to this exact ticket identifier (observations remain fleet-wide)",
    )
    commands.choices["start"].add_argument(
        "--shadow",
        action="store_true",
        help="decide and print WOULD WAKE lines without dispatching or mutating anything",
    )
    commands.choices["status"].add_argument("--json", action="store_true", help="print machine-readable status")
    commands.choices["facts"].add_argument("--json", action="store_true", help="print machine-readable PR facts")
    answer = commands.add_parser("answer")
    answer.add_argument("escalation")
    answer.add_argument("text", nargs="+", help="the answer; include Citation: path:line when you have one")
    answer.add_argument("--project", default=None, help="project whose ledger holds the escalation")
    linear = commands.add_parser(
        "linear",
        help="make a narrow, non-interactive-safe Linear progress update",
    )
    linear_commands = linear.add_subparsers(dest="linear_command", required=True)
    comment = linear_commands.add_parser("comment", help="post a comment on one Linear issue")
    comment.add_argument("issue", metavar="IDENTIFIER", help="Linear issue id or identifier, e.g. HAN-46")
    comment.add_argument("--body", required=True, help="comment body")
    comment.add_argument("--project", help="optional Harbor project, used for its configured Linear endpoint")
    state = linear_commands.add_parser("state", help="move one Linear issue to a non-terminal state")
    state.add_argument("issue", metavar="IDENTIFIER", help="Linear issue id or identifier, e.g. HAN-46")
    state.add_argument("--state", required=True, help="non-terminal Linear workflow state name or id")
    state.add_argument("--project", help="optional Harbor project, used for its configured Linear endpoint")
    return parser


def _db_path() -> Path:
    return Path(os.environ.get("HARBOR_DB_PATH", Path.home() / ".harbor" / "harbor.db")).expanduser()


def _pipeline_text(config: Any) -> str:
    """Read the configured pipeline document without interpreting its prose."""

    for name in ("pipeline_prompt", "raw_prompt", "builder_prompt"):
        value = getattr(config, name, None)
        if value:
            return str(value)
    path = getattr(config, "pipeline_path", None)
    if path:
        candidate = Path(path).expanduser()
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return ""


def _pipeline_setting(config: Any, name: str, default: str) -> str:
    value = getattr(config, name, None)
    if value is not None and str(value).strip():
        return str(value).strip()
    return os.environ.get(f"HARBOR_{name.upper()}", default).strip() or default


def _deadline_seconds(config: Any, default_minutes: float = 90.0) -> float:
    """Resolve the configured builder deadline in seconds."""

    value = getattr(config, "deadline_minutes", None)
    try:
        minutes = float(value) if value is not None else default_minutes
    except (TypeError, ValueError):
        minutes = default_minutes
    return max(60.0, minutes * 60.0)


def _worktree_root(config: Any) -> Path:
    configured = getattr(config, "worktree_root", None)
    if configured is None:
        configured = os.environ.get("HARBOR_WORKTREE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".harbor" / "wt" / str(getattr(config, "name", "project"))).resolve()


def _is_terminal_linear_state(value: str | None) -> bool:
    """Keep the direct CLI fail-closed even when a client is substituted.

    ``LinearClient`` rejects terminal state names too, but this command is the
    builder-facing authority boundary.  Checking here means an injected client
    or future transport cannot accidentally turn a verified-merge transition
    into ordinary builder custody.
    """

    state = str(value or "").strip().casefold()
    return state in {"done", "completed", "canceled", "cancelled"} or any(
        marker in state for marker in ("done", "completed", "canceled", "cancelled")
    )


def build_production_dispatcher(
    config: Any,
    db: Any,
    *,
    worktree_manager: Any | None = None,
    runner_factory: Any | None = None,
    builder_prompt: str = "",
    ticket_scope: TicketScope | str | None = None,
) -> tuple[Any, Any, Any]:
    """Construct the real worktree manager, runner factory and dispatcher.

    Shared by ``start`` and ``answer`` so an operator answer can hydrate and
    resume the SAME retained builder session.  Without this, ``answer`` built
    a dispatcher-less supervisor and could only ever report "builder session
    not available" -- the run row and worktree were right there in the ledger.
    """

    manager = worktree_manager
    if manager is None:
        manager = WorktreeManager(
            config.repo_path,
            root=_worktree_root(config),
            repo_name=config.name,
            base_branch=_pipeline_setting(config, "base_branch", "main"),
            remote=_pipeline_setting(config, "git_remote", "origin"),
            install_command=getattr(config, "install_command", None),
        )
    factory = runner_factory
    if factory is None:
        def factory(worktree: Any, wake: Any) -> CodexRunner:
            ticket = str(wake.ticket.get("identifier") or wake.ticket.get("id") or "")
            path = getattr(worktree, "path", worktree)
            return CodexRunner(
                path,
                db=db,
                ticket=ticket,
                model=str(getattr(config, "builder_model", "gpt-5.6-luna")),
                reasoning_effort=str(getattr(config, "builder_effort", "xhigh")),
                executable=os.environ.get("HARBOR_CODEX_EXECUTABLE", "codex"),
                run_id=wake.run_id,
                # Without this the runner silently takes its own 90-minute
                # default and ignores the project's deadline_minutes.
                deadline_seconds=_deadline_seconds(config),
            )
    dispatcher = WorktreeDispatcher(
        manager,
        factory,
        db=db,
        project=config.name,
        builder_prompt=builder_prompt,
        ticket_scope=ticket_scope,
    )
    return dispatcher, manager, factory


async def _doctor(project_name: str) -> int:
    try:
        config = get_project(project_name)
    except ValueError as exc:
        print(f"Harbor: {exc}", file=sys.stderr)
        return 2
    client = LinearClient(endpoint=config.linear_api_url)
    try:
        healthy = await client.health()
    except LinearClientError as exc:
        healthy = False
        print(f"Project: {config.name} · Linear: unavailable ({exc})")
    else:
        print(f"Project: {config.name} · Linear: healthy")
    scope_ok = True
    if healthy:
        scope_ok = await _check_project_scope(client, config)
    print(f"Poll states: {', '.join(config.poll_states)}")
    print(f"Dispatch states: {', '.join(config.dispatch_states)}")
    print("Backlog: ignored")
    await client.close()
    return 0 if healthy and scope_ok else 1


async def _check_project_scope(client: Any, config: Any) -> bool:
    """Verify every configured project slug actually exists in Linear.

    A slug that matches nothing returns zero issues rather than an error, so a
    typo makes Harbor poll forever and see an empty board.  Naming the real
    slugs here turns a silent no-op into an actionable failure.
    """

    scope = tuple(getattr(config, "linear_project_scope", ()) or (config.linear_project_slug,))
    lister = getattr(client, "list_projects", None)
    if not callable(lister):
        return True
    try:
        projects = await lister()
    except LinearClientError as exc:
        print(f"Projects: could not verify scope ({exc})")
        return True
    known: dict[str, str] = {}
    for project in projects:
        slug = str(project.get("slugId") or "").strip()
        name = str(project.get("name") or "").strip()
        if slug:
            known[slug] = name
            # Linear also accepts the URL form "<name>-<slugId>".
            if name:
                known[f"{name.casefold()}-{slug}"] = name
    ok = True
    for slug in scope:
        match = known.get(slug) or known.get(slug.casefold())
        if match:
            print(f"Project scope: {slug} -> {match}")
        else:
            ok = False
            available = ", ".join(f"{name} ({slug})" for slug, name in sorted(known.items(), key=lambda i: i[1]) if "-" not in slug)
            print(f"Project scope: {slug} matches NO Linear project; Harbor would poll an empty board")
            print(f"  available: {available}")
    excluded = sorted({name for slug, name in known.items() if name and not any(
        slug == item or slug.casefold() == item.casefold() for item in scope
    )} - {known.get(item) or known.get(item.casefold()) for item in scope})
    if excluded:
        print(f"Project scope: excluded {', '.join(excluded)}")
    return ok


async def _start(
    project_name: str,
    *,
    once: bool = False,
    shadow: bool = False,
    read_only: bool = False,
    ticket: str | None = None,
    # Dependency seams keep CLI assembly testable and let a delivery skill
    # provide its own runner/worktree dispatcher without Harbor importing it.
    linear_client: Any | None = None,
    github_poller: Any | None = None,
    dispatcher: Any | None = None,
    runner: Any | None = None,
    decider: Any | None = None,
    first_mate: FirstMate | None = None,
    migrations: Any | None = None,
    heartbeat: Any | None = None,
    unblock_ladder: Any | None = None,
    worktree_manager: Any | None = None,
    runner_factory: Any | None = None,
    pr_event_processor: Any | None = None,
    notifier: Any | None = None,
) -> int:
    try:
        invocation_scope = TicketScope(ticket)
    except ValueError as exc:
        print(f"Harbor: {exc}", file=sys.stderr)
        return 2
    try:
        config = get_project(project_name)
    except ValueError as exc:
        print(f"Harbor: {exc}", file=sys.stderr)
        return 2
    db = HarborDB(_db_path())
    client = linear_client or LinearClient(endpoint=config.linear_api_url)
    poller = LinearPoller(client, config, db=db)
    profile = PipelineProfile.from_project(config)
    event_processor = pr_event_processor or PREventProcessor(
        db,
        project=config.name,
        profile=profile,
    )
    github_client: GitHubClient | None = None
    if github_poller is None and profile.repository_owner and profile.repository_name:
        github_client = GitHubClient.from_profile(profile)
        github_poller = GitHubPoller(
            github_client,
            profile,
            event_processor=event_processor,
            project=config.name,
        )

    # The CLI is the production composition root.  Tests and embedders may
    # inject any of these seams, but an ordinary ``harbor start SampleProject`` must
    # still create an isolated worktree and a persistent Codex builder.
    builder_prompt = _pipeline_text(config)
    production_dispatcher = dispatcher
    if production_dispatcher is None and runner is None and not read_only:
        manager = worktree_manager
        if manager is None:
            manager = WorktreeManager(
                config.repo_path,
                root=_worktree_root(config),
                repo_name=config.name,
                base_branch=_pipeline_setting(config, "base_branch", "main"),
                remote=_pipeline_setting(config, "git_remote", "origin"),
                install_command=getattr(config, "install_command", None),
            )
        production_dispatcher, manager, factory = build_production_dispatcher(
            config, db,
            worktree_manager=manager,
            runner_factory=runner_factory,
            builder_prompt=builder_prompt,
            ticket_scope=invocation_scope,
        )
        # Retain the concrete seams for status and tests without changing the
        # Supervisor API's existing duck-typed injection behavior.
        worktree_manager = manager
        runner_factory = factory

    notifier = notifier if notifier is not None else Notifier()
    supervisor = Supervisor(
        poller,
        db=db,
        notifier=notifier,
        shadow=shadow or read_only,
        read_only=read_only,
        github_poller=github_poller,
        dispatcher=production_dispatcher,
        runner=runner,
        decider=decider,
        heartbeat=heartbeat,
        migrations=migrations,
        unblock_ladder=unblock_ladder,
        worktree_manager=worktree_manager,
        runner_factory=runner_factory,
        pr_event_processor=event_processor,
        ticket_scope=invocation_scope,
    )
    if isinstance(production_dispatcher, WorktreeDispatcher):
        production_dispatcher.push_recorder = supervisor.record_builder_push
    if getattr(event_processor, "builder", None) is None and production_dispatcher is not None:
        # This is useful for callers that invoke the processor directly.  The
        # supervisor still passes the retained target explicitly on each poll.
        try:
            event_processor.builder = production_dispatcher
        except (AttributeError, TypeError):
            pass
    mate = first_mate or FirstMate(
        supervisor,
        model=config.firstmate_model,
        reasoning_effort=config.firstmate_effort,
        notifier=notifier,
    )
    if supervisor.unblock_ladder is None:
        # Phase 7 owns Linear comment/state semantics. Harbor only supplies
        # the same-session resume seam and observed repository path.
        #
        # ``first_mate`` and ``notifier`` are what make rung 1 (cite or
        # escalate, via the configured Terra) and rung 2 (park and page the
        # operator) reachable from a real ``harbor start``.  Without them the
        # ladder silently degrades to rung 0 only.
        from harbor.unblock import UnblockLadder

        supervisor.unblock_ladder = UnblockLadder(
            db,
            linear=client,
            first_mate=mate,
            notifier=notifier,
            resumer=supervisor.resume_session,
            repo_path=config.repo_path,
            protected_paths=getattr(config, "protected_paths", ()) or (),
        )
    # Install the operator callbacks before the first poll can wake a builder.
    # ``Supervisor.start`` delivers started-mode wakes in the background, so
    # heartbeat and observation output are visible from the first long run.
    console = Console(supervisor, first_mate=mate)
    try:
        await supervisor.start()
        if shadow:
            # Phase 3 proof surface: one decided cycle, printed, nothing done.
            print(console.banner())
            print("Shadow: decisions only · no dispatch, no resume, no queue row, no Linear/GitHub write")
            # Keep the observed Linear ticket state beside the decisions so a
            # one-shot shadow run can be compared directly with the source
            # board.  ``status_text`` is a pure formatter over this poll's
            # in-memory observation and performs no additional I/O.
            print(console.status_text())
            print(_render_shadow(supervisor))
            return 0 if supervisor.linear_healthy else 1
        if once:
            print(console.banner())
            print(console.status_text())
            return 0 if supervisor.linear_healthy else 1
        await console.run()
        return 0
    finally:
        await supervisor.exit()
        close = getattr(client, "close", None)
        if callable(close):
            value = close()
            if asyncio.iscoroutine(value):
                await value
        if github_client is not None and github_client is not client:
            await github_client.close()
        db.close()


def _render_shadow(supervisor: Any) -> str:
    """Render the decided-but-not-dispatched wakes for one cycle."""

    wakes = list(getattr(supervisor, "shadow_wakes", ()) or ())
    if not wakes:
        return "WOULD WAKE: none"
    lines: list[str] = []
    for wake in wakes:
        ticket = wake.ticket.get("identifier") or wake.ticket.get("id") or "(unknown)"
        detail = f" · {wake.reason}" if getattr(wake, "reason", None) else ""
        lines.append(f"WOULD WAKE {wake.action} {ticket} · idem_key={wake.idem_key}{detail}")
    return "\n".join(sorted(lines))


async def _facts(project_name: str, *, as_json: bool = True) -> int:
    """Print the read-only PR facts for the tickets Harbor polls.

    This performs exactly one Linear query and one aliased GitHub query, and
    writes nothing anywhere.  It is the Phase 2 proof surface.
    """

    try:
        config = get_project(project_name)
    except ValueError as exc:
        print(f"Harbor: {exc}", file=sys.stderr)
        return 2
    db = HarborDB(_db_path())
    client = LinearClient(endpoint=config.linear_api_url)
    profile = PipelineProfile.from_project(config)
    github_client: GitHubClient | None = None
    github_poller = None
    if profile.repository_owner and profile.repository_name:
        github_client = GitHubClient.from_profile(profile)
        github_poller = GitHubPoller(github_client, profile, project=config.name)
    supervisor = Supervisor(
        LinearPoller(client, config, db=db),
        db=db,
        github_poller=github_poller,
        # Deciding is harmless, but shadow keeps this command strictly
        # read-only: no queue row, no dispatch, no PR-event delivery.
        shadow=True,
    )
    try:
        await supervisor.poll_once()
        facts = [dict(item) for item in supervisor.last_pr_facts]
        if as_json:
            print(json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            for item in facts:
                ci = item.get("ci") or {}
                threads = item.get("threads") or []
                print(
                    f"PR #{item.get('number')} {item.get('head_sha', '')[:8]} "
                    f"CI {ci.get('state', 'absent')} · {len(threads)} threads"
                )
        if supervisor.github_error:
            print(f"Harbor: GitHub: {supervisor.github_error}", file=sys.stderr)
        return 0 if supervisor.linear_healthy else 1
    finally:
        await supervisor.exit()
        await client.close()
        if github_client is not None:
            await github_client.close()
        db.close()


async def _answer(escalation_id: str, text: str, *, project_name: str | None = None) -> int:
    """Record a cited operator answer and resume the same builder session.

    This is the non-interactive twin of the console's ``/answer``: both call
    ``Supervisor.answer_escalation``, so both write the same ``resolutions``
    row and a later identical question resolves at rung 1 without paging you.
    """

    if not text.strip():
        print("Harbor: an answer is required", file=sys.stderr)
        return 2
    db = HarborDB(_db_path())
    client: Any = None
    supervisor: Any = None
    try:
        config = None
        if project_name:
            try:
                config = get_project(project_name)
            except ValueError as exc:
                print(f"Harbor: {exc}", file=sys.stderr)
                return 2
        if config is not None:
            client = LinearClient(endpoint=config.linear_api_url)
            poller = LinearPoller(client, config, db=db)
        else:
            poller = None
        if poller is None:
            print("Harbor: --project is required to answer an escalation", file=sys.stderr)
            return 2
        # Shadow keeps this command from DISPATCHING anything.  The dispatcher
        # is still required so the explicit resume inside answer_escalation can
        # hydrate the retained builder from the ledger and continue the SAME
        # session, which is the whole point of answering an escalation.
        dispatcher, _, _ = build_production_dispatcher(
            config, db, builder_prompt=_pipeline_text(config)
        )
        supervisor = Supervisor(poller, db=db, dispatcher=dispatcher, shadow=True)
        try:
            result = await supervisor.answer_escalation(escalation_id, text)
        except ValueError as exc:
            print(f"Harbor: answer not recorded: {exc}", file=sys.stderr)
            return 1
        except RuntimeError as exc:
            print(f"Harbor: {exc}", file=sys.stderr)
            return 1
        suffix = " · same builder resumed" if result.get("resumed") else " · builder session not available"
        print(f"Harbor: answer recorded for escalation {escalation_id}{suffix}")
        print(f"Citation: {result.get('citation', '')}")
        return 0
    finally:
        if supervisor is not None:
            await supervisor.exit()
        if client is not None:
            await client.close()
        db.close()


async def _linear_write(
    command: str,
    issue: str,
    *,
    body: str | None = None,
    state: str | None = None,
    project_name: str | None = None,
) -> int:
    """Apply one scoped Linear progress mutation without using Linear MCP.

    Builders may invoke this command in a non-interactive sandbox, where MCP
    mutation approval is unavailable.  The transport deliberately exposes only
    comments and non-terminal state updates; ``Done`` remains merge-reconciled
    Harbor custody.
    """

    endpoint: str | None = None
    if project_name:
        try:
            endpoint = get_project(project_name).linear_api_url
        except ValueError as exc:
            print(f"Harbor: {exc}", file=sys.stderr)
            return 2
    client = LinearClient(endpoint=endpoint or "https://api.linear.app/graphql")
    try:
        if not client.token:
            print(
                "Harbor: Linear writes require LINEAR_API_KEY or HARBOR_LINEAR_API_KEY",
                file=sys.stderr,
            )
            return 2
        if command == "comment":
            await client.create_comment(issue, body or "")
            print(f"Harbor: commented on {issue}")
            return 0
        if command == "state":
            if _is_terminal_linear_state(state):
                print(
                    "Harbor: terminal Linear states are reconciled only after a verified merge",
                    file=sys.stderr,
                )
                return 1
            result = await client.update_issue_state(issue, state or "")
            updated = ((result.get("issue") or {}).get("state") or {}).get("name") or state
            print(f"Harbor: set {issue} to {updated}")
            return 0
        print(f"Harbor: unknown Linear command {command}", file=sys.stderr)
        return 2
    except (LinearClientError, ValueError) as exc:
        print(f"Harbor: Linear {command} failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.close()


async def _status(project_name: str, *, as_json: bool = False) -> int:
    try:
        config = get_project(project_name)
    except ValueError as exc:
        print(f"Harbor: {exc}", file=sys.stderr)
        return 2
    db = HarborDB(_db_path())
    try:
        tickets = db.tickets(config.name)
        if not tickets:
            if as_json:
                print(json.dumps({"project": config.name, "tickets": []}, sort_keys=True))
            else:
                print(f"Harbor: no observations for {config.name}; run harbor start {project_name}")
            return 0
        if as_json:
            print(json.dumps({"project": config.name, "tickets": tickets}, ensure_ascii=False, sort_keys=True))
            return 0
        for ticket in tickets:
            observed = " (observed)" if not ticket.get("owned") else ""
            print(f"{ticket['identifier']}{observed}  {ticket['state']}  {ticket['title']}")
        return 0
    finally:
        db.close()


async def _async_main(args: argparse.Namespace) -> int:
    if args.command == "doctor":
        return await _doctor(args.project)
    if args.command == "start":
        # Explicit shadow starts are already read-only and do not need to own
        # the project's live supervisor lease.  A normal duplicate start is
        # still useful as a status/question console, so keep it alive as a
        # read-only observer instead of allowing a second dispatcher.
        if args.shadow:
            return await _start(
                args.project,
                once=args.once,
                shadow=True,
                read_only=True,
                ticket=args.ticket,
            )
        try:
            config = get_project(args.project)
        except ValueError as exc:
            print(f"Harbor: {exc}", file=sys.stderr)
            return 2
        lease = ProjectLease(_db_path(), config.name)
        try:
            lease.acquire()
        except ProjectLeaseError as exc:
            reason = (
                f"Another Harbor instance is already running for {config.name}; "
                "this instance is read-only. "
                f"Owner: {exc.owner}"
            )
            print(f"Harbor: {reason}", file=sys.stderr)
            try:
                notification = Notifier().notify(reason)
                if asyncio.iscoroutine(notification):
                    await notification
            except Exception as notify_exc:
                # Notification Center is an operator convenience; losing it
                # must not turn a safe read-only observer into a failed start.
                print(f"Harbor: notification unavailable: {notify_exc}", file=sys.stderr)
            return await _start(
                args.project,
                once=args.once,
                shadow=False,
                read_only=True,
                ticket=args.ticket,
            )
        try:
            return await _start(
                args.project,
                once=args.once,
                shadow=False,
                read_only=False,
                ticket=args.ticket,
            )
        finally:
            lease.release()
    if args.command == "facts":
        return await _facts(args.project, as_json=getattr(args, "json", True))
    if args.command == "answer":
        return await _answer(args.escalation, " ".join(args.text), project_name=args.project)
    if args.command == "linear":
        return await _linear_write(
            args.linear_command,
            args.issue,
            body=getattr(args, "body", None),
            state=getattr(args, "state", None),
            project_name=getattr(args, "project", None),
        )
    return await _status(args.project, as_json=getattr(args, "json", False))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
