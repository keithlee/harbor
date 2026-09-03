"""Deterministic polling and optional Phase 6 re-engagement supervisor.

Polling remains model-free.  A GitHub poller, wake decider, and runner are
injectable so tests can use fakes and a project can swap its delivery skill
without Harbor learning the skill's name or internals.  Harbor only emits a
``Wake`` value; the runner/skill owns all GitHub and Linear mutations.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable

from harbor.db import HarborDB
from harbor.digest import render_digest
from harbor.heartbeat import Heartbeat, HeartbeatEvent
from harbor.poll.linear_poller import LinearPoller, PollResult
from harbor.runner.base import apply_phase4_migration, reap_orphan_runs
from harbor.wake import (
    Wake,
    WakeDecider,
    apply_phase3_migration,
    apply_phase6_migration,
    observe_pr_facts,
    record_push,
)
from harbor.pr_events import PREventError

try:  # PR feedback was added after the initial supervisor phases.
    from harbor.pr_events import apply_phase8_migration
except ImportError:  # pragma: no cover - older embedding
    apply_phase8_migration = None  # type: ignore[assignment]

try:  # Phase 7 may be supplied by a sibling worktree while Harbor is booting.
    from harbor.db import apply_phase7_migration
except ImportError:  # pragma: no cover - older embedding
    apply_phase7_migration = None  # type: ignore[assignment]


class Lifecycle(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"


@dataclass(frozen=True)
class SupervisorStatus:
    project: str
    lifecycle: str
    linear_healthy: bool
    tickets: tuple[dict[str, Any], ...]
    last_error: str | None = None
    last_snapshot_hash: str | None = None
    github_healthy: bool | None = None
    github_error: str | None = None
    wakes: tuple[dict[str, Any], ...] = ()
    last_activity: str | None = None
    heartbeat: dict[str, Any] | None = None
    escalations: tuple[dict[str, Any], ...] = ()
    runs: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "lifecycle": self.lifecycle,
            "linear_healthy": self.linear_healthy,
            "tickets": list(self.tickets),
            "last_error": self.last_error,
            "last_snapshot_hash": self.last_snapshot_hash,
            "github_healthy": self.github_healthy,
            "github_error": self.github_error,
            "wakes": list(self.wakes),
            "last_activity": self.last_activity,
            "heartbeat": self.heartbeat,
            "escalations": list(self.escalations),
            "runs": list(self.runs),
        }


class WorktreeDispatcher:
    """Small adapter from deterministic ``Wake`` values to delivery seams.

    Worktree creation and builder execution are deliberately supplied by
    injection.  The adapter never performs Linear/GitHub mutations itself and
    retains one runner per ticket so review/CI wakes can resume that same
    runner/session in a live process.
    """

    def __init__(
        self,
        worktree_manager: Any,
        runner_factory: Callable[..., Any],
        *,
        db: Any | None = None,
        project: str = "",
        builder_prompt: str = "",
        push_recorder: Callable[..., Any] | None = None,
    ) -> None:
        self.worktree_manager = worktree_manager
        self.runner_factory = runner_factory
        self.db = db
        self.project = str(project)
        self.builder_prompt = str(builder_prompt or "").strip()
        self.push_recorder = push_recorder
        self.runners: dict[str, Any] = {}
        self.worktrees: dict[str, Any] = {}

    @staticmethod
    def _identifier(wake: Wake) -> str:
        return str(wake.ticket.get("identifier") or wake.ticket.get("id") or "")

    def _stable_run_id(self, wake: Wake) -> str:
        """Return the durable logical id for a ticket's first builder run.

        Build wakes intentionally do not contain a run id because the decider
        is model-free.  The dispatcher is the ownership boundary where the
        run is created, so derive one deterministically before constructing a
        runner.  A later review/CI/comment wake can therefore never create a
        second logical session for the same ticket.
        """

        existing = wake.run_id or wake.ticket.get("builder_run_id") or wake.ticket.get("run_id")
        if existing:
            return str(existing)
        identifier = self._identifier(wake)
        project = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.project).strip("-") or "project"
        ticket = re.sub(r"[^A-Za-z0-9_.-]+", "-", identifier).strip("-") or "ticket"
        return f"harbor-{project}-{ticket}-builder"

    @staticmethod
    def _with_run_id(wake: Wake, run_id: str) -> Wake:
        if wake.run_id == run_id:
            return wake
        return Wake(
            ticket={**wake.ticket, "builder_run_id": run_id, "run_id": run_id},
            action=wake.action,
            resume=run_id,
            instruction=wake.instruction,
            idem_key=wake.idem_key,
            facts=wake.facts,
            notify=wake.notify,
            reason=wake.reason,
        )

    def _remember_run_id(self, identifier: str, run_id: str) -> None:
        """Attach the logical id to the queued build receipt when available."""

        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None or not identifier:
            return
        lock = getattr(self.db, "_lock", None)
        context = lock if lock is not None else nullcontext()
        try:
            with context:
                connection.execute(
                    "UPDATE work_queue SET run_id = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE ticket = ? AND action = 'build' AND status IN ('queued', 'dispatched', 'running', 'waiting')",
                    (run_id, identifier),
                )
                connection.commit()
        except Exception:
            # Queue metadata is an audit aid.  The runs row written by the
            # runner remains authoritative for same-session recovery.
            return

    def _initial_wake(self, wake: Wake) -> Wake:
        if not self.builder_prompt or wake.action != "build":
            return wake
        instruction = f"{self.builder_prompt}\n\n{wake.instruction}" if wake.instruction else self.builder_prompt
        return Wake(
            ticket=wake.ticket,
            action=wake.action,
            resume=wake.resume,
            instruction=instruction,
            idem_key=wake.idem_key,
            facts=wake.facts,
            notify=wake.notify,
            reason=wake.reason,
        )

    def _record_observed_push(self, wake: Wake, result: Any) -> Any:
        """Record an explicit push receipt without granting push authority."""

        if self.push_recorder is None or not isinstance(result, Mapping):
            return
        pushed = result.get("pushed") or result.get("push") or result.get("pushed_head_sha") or result.get("push_head_sha")
        if isinstance(pushed, Mapping):
            pushed = pushed.get("head_sha") or pushed.get("sha") or pushed.get("headRefOid")
        head = result.get("head_sha") or result.get("headSha") or result.get("sha")
        if isinstance(pushed, str) and pushed not in {"true", "True", "1"}:
            head = head or pushed
        if not pushed or not head:
            return
        number = result.get("pr_number") or result.get("number") or wake.facts.get("pr_number") or wake.facts.get("number")
        try:
            value = self.push_recorder(wake.ticket, str(head), pr_number=int(number) if number else None)
        except (TypeError, ValueError, RuntimeError):
            return
        # A recorder may be asynchronous in an embedding.  Dispatch callers
        # await this helper's result through ``_record_push_result`` below.
        return value

    async def _record_push_result(self, wake: Wake, result: Any) -> None:
        value = self._record_observed_push(wake, result)
        if inspect.isawaitable(value):
            await value

    async def _factory(self, worktree: Any, wake: Wake) -> Any:
        target = self.runner_factory
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            signature = None
        if signature is None:
            value = target(worktree, wake)
        else:
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            if len(positional) <= 1:
                value = target(worktree)
            else:
                value = target(worktree, wake)
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    async def _runner_call(runner: Any, wake: Wake, *, resume: bool) -> Any:
        names = ("resume", "reengage", "continue_run", "continue") if resume else ("start", "run", "dispatch", "build")
        method = next((getattr(runner, name, None) for name in names if callable(getattr(runner, name, None))), None)
        if method is None and callable(runner):
            method = runner
        if method is None:
            return None
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            signature = None
        if signature is None:
            value = method(wake)
        else:
            parameters = signature.parameters
            positional = [
                parameter
                for parameter in parameters.values()
                if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            if resume and len(positional) >= 2:
                value = method(wake.run_id, wake.instruction)
            elif "run_id" in parameters and not resume:
                value = method(wake.instruction, run_id=wake.run_id)
            elif len(positional) <= 1:
                parameter_name = positional[0].name.casefold() if positional else "wake"
                value = method(wake.instruction if parameter_name in {"instruction", "prompt", "text"} else wake)
            else:
                value = method(wake.instruction, run_id=wake.run_id)
        return await value if inspect.isawaitable(value) else value

    async def dispatch(self, wake: Wake) -> Any:
        identifier = self._identifier(wake)
        if not identifier:
            return None
        wake = self._with_run_id(wake, self._stable_run_id(wake))
        wake = self._initial_wake(wake)
        self._remember_run_id(identifier, wake.run_id or "")
        create = getattr(self.worktree_manager, "create", None) or getattr(self.worktree_manager, "ensure", None)
        if not callable(create):
            raise TypeError("worktree manager must expose create or ensure")
        worktree = create(identifier)
        if inspect.isawaitable(worktree):
            worktree = await worktree
        runner = await self._factory(worktree, wake)
        self.worktrees[identifier] = worktree
        self.runners[identifier] = runner
        result = await self._runner_call(runner, wake, resume=False)
        await self._record_push_result(wake, result)
        return result

    def _persisted_run(self, identifier: str) -> dict[str, Any] | None:
        """Read the ticket's most recent run row from the durable ledger."""

        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None or not identifier:
            return None
        try:
            row = connection.execute(
                "SELECT id, session_id, status, worktree FROM runs "
                "WHERE ticket = ? ORDER BY COALESCE(finished_at, started_at, id) DESC, id DESC LIMIT 1",
                (identifier,),
            ).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "session_id": str(row[1]) if row[1] else None,
            "status": str(row[2]).casefold() if row[2] else None,
            "worktree": str(row[3]) if row[3] else None,
        }

    async def hydrate(self, identifier: str) -> Any:
        """Rebuild the retained runner for a ticket after a Harbor restart.

        ``self.runners`` is process-local, but the run row, the session id, and
        the worktree all survive a restart -- so a parked or waiting builder is
        recoverable.  This never creates a worktree and never starts an agent:
        it reconstructs the handle to an existing session, or returns ``None``.

        Only non-live rows are hydrated.  A ``running`` row belongs to the
        startup orphan reap, which must not be raced by a second handle.
        """

        if not identifier or identifier in self.runners:
            return self.runners.get(identifier)
        row = self._persisted_run(identifier)
        if row is None or row.get("status") not in {"waiting", "parked", "blocked"}:
            return None
        worktree_path = row.get("worktree")
        if not worktree_path:
            return None
        path = Path(worktree_path).expanduser()
        if not path.is_dir():
            # The worktree was removed (merged, or cleaned up by hand).  There
            # is nothing to resume into; fail closed rather than rebuild it.
            return None
        wake = Wake(
            ticket={"identifier": identifier, "builder_run_id": row["id"], "run_id": row["id"]},
            action="reengage",
            resume=row["id"],
        )
        worktree = self.worktrees.get(identifier)
        if worktree is None:
            build = getattr(self.worktree_manager, "attach", None)
            if callable(build):
                worktree = build(identifier)
                if inspect.isawaitable(worktree):
                    worktree = await worktree
            else:
                worktree = path
        try:
            runner = await self._factory(worktree, wake)
        except Exception:
            # A factory that cannot rebuild a handle is not a supervision
            # failure; the resume simply stays unavailable for this poll.
            return None
        self.worktrees[identifier] = worktree
        self.runners[identifier] = runner
        return runner

    async def resume(self, wake: Wake) -> Any:
        identifier = self._identifier(wake)
        runner = self.runners.get(identifier)
        if runner is None:
            # After a restart the in-memory runner is gone but the session is
            # not.  Try to rebuild the handle from the ledger before giving up.
            runner = await self.hydrate(identifier)
        if runner is None:
            # Never create a fresh worktree or agent for a resume wake: same
            # session recovery is fail-closed when the retained runner is gone.
            return None
        if not wake.run_id:
            # A wake decided from Linear/GitHub facts alone carries no run id.
            # The retained runner knows which session this ticket owns, and
            # resuming without it would start a second conversation.
            recovered = str(getattr(runner, "default_run_id", "") or "")
            if recovered:
                wake = self._with_run_id(wake, recovered)
        result = await self._runner_call(runner, wake, resume=True)
        await self._record_push_result(wake, result)
        return result

    async def handle_pr_event(self, event: Any) -> Any:
        """Offer one PR event to the retained ticket runner.

        ``PREventProcessor`` deliberately knows nothing about worktrees.  This
        adapter is the narrow bridge from its immutable event to the same
        runner/session that handled the original build.  Missing retention is
        an error rather than a successful no-op so the event remains pending.
        """

        if isinstance(event, Mapping):
            ticket = str(event.get("ticket") or "")
            instruction = str(event.get("instruction") or "")
            run_id = event.get("run_id")
            facts = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        else:
            ticket = str(getattr(event, "ticket", "") or "")
            instruction = str(getattr(event, "instruction", "") or "")
            run_id = getattr(event, "run_id", None)
            facts = getattr(event, "payload", {})
        runner = self.runners.get(ticket) or await self.hydrate(ticket)
        if runner is None:
            raise PREventError(f"no retained builder for PR event ticket {ticket or '(unknown)'}")
        run_id = str(run_id or getattr(runner, "default_run_id", "") or self._stable_run_id(
            Wake(ticket={"identifier": ticket}, action="reengage")
        ))
        wake = Wake(
            ticket={"identifier": ticket, "builder_run_id": run_id, "run_id": run_id},
            action="reengage",
            resume=run_id,
            instruction=instruction,
            facts=facts if isinstance(facts, Mapping) else {},
        )
        result = await self._runner_call(runner, wake, resume=True)
        await self._record_push_result(wake, result)
        return result

    def _ticket_for_run(self, run_id: str) -> str:
        """Find which ticket owns a run id, using the durable ledger."""

        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None or not run_id:
            return ""
        try:
            row = connection.execute(
                "SELECT ticket FROM runs WHERE id = ?", (str(run_id),)
            ).fetchone()
        except Exception:
            return ""
        return str(row[0]) if row and row[0] else ""

    async def resume_session(self, run_id: str, prompt: str) -> bool:
        """Resume a retained runner for the unblock ladder's command seam.

        An operator answer routinely arrives in a NEW process -- ``harbor
        answer`` is its own command -- so the in-memory runner map is empty
        even though the run row, session id and worktree all survive.  Hydrate
        from the ledger before reporting the session unavailable.
        """

        if not any(
            str(run_id) in getattr(runner, "contexts", {})
            or str(getattr(runner, "default_run_id", "")) == str(run_id)
            for runner in self.runners.values()
        ):
            identifier = self._ticket_for_run(run_id)
            if identifier:
                await self.hydrate(identifier)

        for runner in tuple(self.runners.values()):
            contexts = getattr(runner, "contexts", {})
            if str(run_id) not in contexts and str(getattr(runner, "default_run_id", "")) != str(run_id):
                continue
            method = getattr(runner, "resume", None) or getattr(runner, "resume_run", None)
            if not callable(method):
                continue
            value = method(run_id, prompt)
            if inspect.isawaitable(value):
                value = await value
            return value is not False
        return False

    async def pause(self, reason: str | None = None) -> None:
        for runner in tuple(self.runners.values()):
            method = getattr(runner, "pause", None) or getattr(runner, "checkpoint", None)
            if not callable(method):
                continue
            try:
                parameters = list(inspect.signature(method).parameters.values())
            except (TypeError, ValueError):
                parameters = []
            if parameters and parameters[0].name.casefold() in {"run_id", "runid", "id"}:
                value = method()
            else:
                try:
                    value = method(reason)
                except TypeError:
                    value = method()
            if inspect.isawaitable(value):
                await value

    async def resume_deadline(self) -> None:
        for runner in tuple(self.runners.values()):
            method = getattr(runner, "resume_deadline", None) or getattr(runner, "resume_pause", None)
            if callable(method):
                value = method()
                if inspect.isawaitable(value):
                    await value


class Supervisor:
    """Poll sources and emit deterministic build/resume wakes.

    ``github_poller`` is optional for backwards compatibility with Phase 1.
    ``dispatcher``/``runner`` are deliberately duck-typed seams: Harbor can
    hand a ``Wake`` to a real persistent runner or a test fake without knowing
    which opaque delivery skill the runner loads.  If neither is supplied the
    supervisor still performs read-only observation and exposes wakes in
    ``status()``.
    """

    def __init__(
        self,
        poller: LinearPoller,
        *,
        db: HarborDB | None = None,
        poll_interval: float | None = None,
        on_update: Callable[[PollResult], Awaitable[None] | None] | None = None,
        github_poller: Any | None = None,
        github: Any | None = None,
        decider: WakeDecider | None = None,
        wake_decider: WakeDecider | None = None,
        dispatcher: Any | None = None,
        runner: Any | None = None,
        notifier: Any | None = None,
        shadow: bool = False,
        on_wake: Callable[[Wake], Awaitable[None] | None] | None = None,
        heartbeat: Any | None = None,
        heartbeat_interval: float | None = None,
        heartbeat_interval_seconds: float | None = None,
        on_heartbeat: Callable[[HeartbeatEvent], Awaitable[None] | None] | None = None,
        migrations: Iterable[Callable[[Any], Any]] | None = None,
        worktree_manager: Any | None = None,
        runner_factory: Callable[..., Any] | None = None,
        pr_event_processor: Any | None = None,
        unblock_ladder: Any | None = None,
    ) -> None:
        self.poller = poller
        self.db = db if db is not None else getattr(poller, "db", None)
        self.project = poller.project_name
        poller_config = getattr(poller, "config", None)
        self.poll_interval = poll_interval if poll_interval is not None else getattr(poller_config, "poll_interval_seconds", 60.0)
        self.on_update = on_update
        self.lifecycle = Lifecycle.STOPPED
        self.linear_healthy = False
        self.last_error: str | None = None
        self.last_result: PollResult | None = None
        self.github_poller = github_poller or github
        self.github_healthy: bool | None = None
        self.github_error: str | None = None
        self.decider = decider or wake_decider
        if self.decider is None:
            config = getattr(self.poller, "config", None)
            self.decider = WakeDecider(
                self.db,
                dispatch_states=getattr(config, "dispatch_states", ("Todo",)),
                bot_rework_cap=getattr(config, "bot_rework_cap", 1),
                # WakeDecider already implements shadow as "decide without a
                # ledger write", which is what makes two shadow cycles diff
                # empty.  Pass it through rather than reimplementing it.
                shadow=bool(shadow),
            )
        self.worktree_manager = worktree_manager
        self.runner_factory = runner_factory
        self.dispatcher = dispatcher or (
            WorktreeDispatcher(
                worktree_manager,
                runner_factory,
                db=self.db,
                project=self.project,
            )
            if worktree_manager is not None and runner_factory is not None and runner is None
            else None
        )
        self.runner = runner
        self.pr_event_processor = pr_event_processor or getattr(self.github_poller, "event_processor", None)
        self.notifier = notifier
        # Shadow mode decides and reports but never dispatches, resumes, or
        # enqueues.  It is the plan's Phase 3 proof that a first start against
        # a live board wakes nothing.
        self.shadow = bool(shadow)
        self.shadow_wakes: list[Wake] = []
        self.last_pr_facts: tuple[Mapping[str, Any], ...] = ()
        self.unblock_ladder = unblock_ladder
        self.last_unblock: Any | None = None
        self.on_wake = on_wake
        self.on_heartbeat = on_heartbeat
        self.last_wakes: list[Wake] = []
        self._fleet_tickets: tuple[dict[str, Any], ...] = ()
        self._last_activity_at: str | None = None
        self._migrations = tuple(migrations) if migrations is not None else None
        self._migrations_applied = False
        self._dispatch_suspended = False
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.heartbeat = heartbeat or Heartbeat(
            self.project,
            interval_seconds=(
                heartbeat_interval
                if heartbeat_interval is not None
                else heartbeat_interval_seconds
                if heartbeat_interval_seconds is not None
                else getattr(
                    getattr(self.poller, "config", None),
                    "heartbeat_seconds",
                    getattr(getattr(self.poller, "config", None), "heartbeat_interval_seconds", 60.0),
                )
            ),
            status_provider=self.status,
            last_activity=lambda: self._last_activity_at,
            publisher=self._publish_heartbeat,
        )
        if self.db:
            self.db.set_runtime(self.project, self.lifecycle.value)

    @property
    def paused(self) -> bool:
        return self.lifecycle == Lifecycle.PAUSED

    @staticmethod
    def _ticket_key(ticket: Mapping[str, Any]) -> str:
        return str(ticket.get("identifier") or ticket.get("id") or "")

    def _github_facts_by_ticket(
        self,
        tickets: Iterable[Mapping[str, Any]],
        facts: Iterable[Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Group PR poll results into the aggregate shape consumed by wake.py."""

        ticket_rows = list(tickets)
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        # Normal GitHubPoller results carry ``ticket``.  Minimal test fakes
        # may omit it, in which case match the PR number against attachments.
        for item in facts:
            if not isinstance(item, Mapping):
                continue
            identifier = str(item.get("ticket") or item.get("ticket_id") or "")
            if not identifier:
                number = item.get("number", item.get("pr_number"))
                for ticket in ticket_rows:
                    attachments = ticket.get("pr_urls", ticket.get("attachments", ()))
                    if isinstance(attachments, Mapping):
                        attachments = attachments.get("nodes", ())
                    if isinstance(attachments, (str, bytes)):
                        attachments = (attachments,)
                    def attachment_number(attachment: Any) -> str:
                        if isinstance(attachment, Mapping):
                            attachment = attachment.get(
                                "number", attachment.get("url", attachment.get("href", ""))
                            )
                        return str(attachment).rstrip("/").split("/")[-1]

                    if any(str(number) == attachment_number(attachment) for attachment in attachments or ()):
                        identifier = self._ticket_key(ticket)
                        break
            if identifier:
                grouped.setdefault(identifier, []).append(item)

        result: dict[str, dict[str, Any]] = {}
        for identifier, rows in grouped.items():
            aggregate: dict[str, Any] = {"ticket": identifier, "prs": list(rows)}
            if len(rows) == 1:
                # Keep direct keys for callers/fakes that provide one PR.
                aggregate.update(rows[0])
            else:
                # A ticket with multiple PRs is actionable only when the
                # helper can identify one unambiguous head/CI state.  The
                # re-engagement decision remains fail-closed otherwise.
                heads = [row.get("head_sha") for row in rows if row.get("head_sha")]
                if heads and all(head == heads[0] for head in heads):
                    aggregate["head_sha"] = heads[0]
            result[identifier] = aggregate
        return result

    async def _call_github(self, tickets: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        poller = self.github_poller
        if poller is None:
            return []
        method = getattr(poller, "poll", None)
        if method is None and callable(poller):
            method = poller
        if method is None:
            raise TypeError("GitHub poller must expose poll")
        try:
            signature = inspect.signature(method)
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
        except (TypeError, ValueError):
            positional = [object()]
        value = method(tickets) if positional else method()
        value = await value if inspect.isawaitable(value) else value
        if isinstance(value, Mapping):
            values = value.get("facts", value.get("prs", value.values()))
            if isinstance(values, Mapping):
                values = values.values()
            value = values
        return [item for item in (value or ()) if isinstance(item, Mapping)]

    async def _invoke(self, target: Any, names: tuple[str, ...], *args: Any) -> Any:
        """Invoke the first available method on a runner/dispatcher fake."""

        if target is None:
            return None
        method = None
        for name in names:
            candidate = getattr(target, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None and callable(target):
            method = target
        if method is None:
            return None
        # Fakes commonly model build as ``dispatch(wake)`` and resume as
        # ``resume(run_id, instruction)``.  Try the rich Wake form first, then
        # the compact two-argument form only for a TypeError from invocation.
        try:
            value = method(*args)
        except TypeError:
            if len(args) == 1 and isinstance(args[0], Wake):
                value = method(args[0].run_id, args[0].instruction)
            else:
                raise
        return await value if inspect.isawaitable(value) else value

    async def _invoke_lifecycle(self, target: Any, names: tuple[str, ...], *args: Any) -> Any:
        """Call a lifecycle seam once, accepting no-arg test doubles."""

        if target is None:
            return None
        method = None
        for name in names:
            candidate = getattr(target, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None:
            return None
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            required = [parameter for parameter in positional if parameter.default is inspect.Parameter.empty]
            keyword_only = {
                parameter.name
                for parameter in signature.parameters.values()
                if parameter.kind == inspect.Parameter.KEYWORD_ONLY
            }
            if not positional and args and "reason" in keyword_only:
                value = method(reason=args[0])
                return await value if inspect.isawaitable(value) else value
            if not positional and args:
                args = ()
            elif positional and positional[0].name.casefold() in {"run_id", "runid", "id"}:
                # BaseRunner.pause(run_id=None) uses its argument as a run
                # identifier, not as an operator reason.  Keep that reason
                # out of the runner seam so pause remains graceful.
                args = ()
            elif not required and len(positional) < len(args):
                args = args[: len(positional)]
        value = method(*args)
        return await value if inspect.isawaitable(value) else value

    async def _publish_heartbeat(self, event: HeartbeatEvent) -> None:
        """Publish a heartbeat without entering the First Mate/wake paths."""

        # Newer ledgers may expose an optional receipt method.  Keep this
        # duck-typed and best-effort so heartbeat publication remains useful
        # with the Phase 1 database and never becomes a model/API call.
        if self.db is not None:
            for name in ("record_heartbeat", "save_heartbeat", "publish_heartbeat"):
                method = getattr(self.db, name, None)
                if callable(method):
                    try:
                        value = method(self.project, event.as_dict())
                    except TypeError:
                        try:
                            value = method(event.as_dict())
                        except Exception:
                            value = None
                    except Exception:
                        value = None
                    if inspect.isawaitable(value):
                        await value
                    break
        if self.on_heartbeat is None:
            return
        value = self.on_heartbeat(event)
        if inspect.isawaitable(value):
            await value

    async def heartbeat_once(self) -> HeartbeatEvent:
        """Publish one operational heartbeat; this never invokes an LLM."""

        return await self.heartbeat.tick(self.status())

    async def _apply_startup_migrations(self) -> None:
        if self._migrations_applied or self.db is None:
            return
        hooks = self._migrations
        if hooks is None:
            hooks = (
                apply_phase3_migration,
                apply_phase6_migration,
                apply_phase4_migration,
            )
            if apply_phase7_migration is not None:
                hooks = (*hooks, apply_phase7_migration)
            if apply_phase8_migration is not None:
                hooks = (*hooks, apply_phase8_migration)
        for migration in hooks:
            try:
                parameters = list(inspect.signature(migration).parameters.values())
            except (TypeError, ValueError):
                parameters = [object()]
            value = migration() if not parameters else migration(self.db)
            if inspect.isawaitable(value):
                await value
        # A restart cannot safely reuse an orphaned active process.  This is a
        # local-ledger cleanup only; no Linear/GitHub mutation is performed.
        await reap_orphan_runs(self.db)
        self._migrations_applied = True

    async def _emit_wake(self, wake: Wake) -> None:
        if self._dispatch_suspended or self.lifecycle in {Lifecycle.PAUSING, Lifecycle.PAUSED}:
            return
        if self.shadow:
            # Record the decision and stop.  No dispatch, no resume, no
            # notification, and no queue row -- so two consecutive shadow
            # cycles are byte-identical.
            self.shadow_wakes.append(wake)
            self.last_wakes.append(wake)
            if len(self.last_wakes) > 20:
                del self.last_wakes[:-20]
            return
        self.last_wakes.append(wake)
        if len(self.last_wakes) > 20:
            del self.last_wakes[:-20]
        if self.on_wake is not None:
            value = self.on_wake(wake)
            if inspect.isawaitable(value):
                await value
        target = self.dispatcher or self.runner
        if wake.action == "build":
            result = await self._invoke(target, ("dispatch", "start", "build", "run"), wake)
            await self._handle_runner_result(wake, result)
        elif wake.action == "reengage":
            result = await self._invoke(target, ("resume", "reengage", "continue_run", "continue"), wake)
            await self._handle_runner_result(wake, result)
        elif wake.action == "stop" and wake.notify:
            await self._invoke(self.notifier, ("notify", "send", "publish"), wake)

    async def _handle_runner_result(self, wake: Wake, result: Any) -> None:
        """Route a blocked builder final to the injected Phase 7 ladder."""

        if hasattr(result, "as_dict") and callable(result.as_dict):
            result = result.as_dict(include_metadata=True)
        if self.unblock_ladder is None or result is None:
            # A delivery runner may expose an explicit push receipt even when
            # no unblock result is configured.  Pushes themselves remain the
            # builder's responsibility; Harbor only records the observation.
            if not isinstance(self.dispatcher, WorktreeDispatcher):
                self._record_observed_push(wake, result)
            if isinstance(result, Mapping):
                await self._cleanup_merged_worktree(wake, result)
            return
        if not isinstance(result, Mapping):
            return
        if not isinstance(self.dispatcher, WorktreeDispatcher):
            self._record_observed_push(wake, result)
        await self._cleanup_merged_worktree(wake, result)
        status = str(result.get("status", "")).casefold()
        if status != "blocked" and not isinstance(result.get("blocker"), Mapping):
            return
        run_id = str(result.get("run_id") or wake.run_id or "")
        run = {"id": run_id}
        if isinstance(result.get("worktree"), str):
            run["worktree"] = result["worktree"]
        try:
            value = self.unblock_ladder.handle(
                wake.ticket,
                run,
                result,
                blocker=result.get("blocker") if isinstance(result.get("blocker"), Mapping) else None,
            )
            self.last_unblock = await value if inspect.isawaitable(value) else value
        except Exception as exc:
            # A malformed blocked final must not stop source polling.  The
            # literal error remains visible in /status for operator action.
            self.last_error = str(exc)

    async def _cleanup_merged_worktree(self, wake: Wake, result: Mapping[str, Any]) -> None:
        """Remove a ticket's worktree once Harbor has verified its merge.

        Gated on ``merge_verified``, which only ``normalize_final_result`` sets
        and only from observed GitHub state.  A builder that merely claims
        ``outcome=merged`` therefore cannot cause an irreversible deletion.

        Harbor still never merges, never writes ``Done``, and performs no
        Linear or GitHub mutation here -- this is local disk reclamation.
        """

        if str(result.get("status", "")).casefold() != "completed":
            return
        if str(result.get("outcome", "")).casefold() != "merged":
            return
        if not result.get("merge_verified"):
            # The merge is claimed but unobserved.  Keep the worktree; the
            # next poll confirms it (plan section 7, AC-10) or it never does.
            return
        identifier = self._ticket_key(wake.ticket)
        if not identifier:
            return
        manager = self.worktree_manager or getattr(self.dispatcher, "worktree_manager", None)
        if manager is None:
            return
        target = None
        dispatcher = self.dispatcher
        if isinstance(dispatcher, WorktreeDispatcher):
            target = dispatcher.worktrees.get(identifier)
        if target is None:
            path_for = getattr(manager, "path_for", None)
            if callable(path_for):
                target = path_for(identifier)
        if target is None:
            return
        remove = getattr(manager, "remove", None)
        if not callable(remove):
            return
        try:
            value = remove(target)
            if inspect.isawaitable(value):
                await value
        except Exception as exc:
            # A retained worktree is untidy, never unsafe.  Surface it in
            # /status rather than failing a successfully merged run.
            self.last_error = f"worktree cleanup for {identifier} failed: {exc}"
            return
        if isinstance(dispatcher, WorktreeDispatcher):
            dispatcher.worktrees.pop(identifier, None)
            dispatcher.runners.pop(identifier, None)

    def _record_observed_push(self, wake: Wake, result: Any) -> None:
        """Record an explicit builder push receipt, never perform the push."""

        if self.db is None or not isinstance(result, Mapping):
            return
        pushed = result.get("pushed") or result.get("push") or result.get("pushed_head_sha") or result.get("push_head_sha")
        if isinstance(pushed, Mapping):
            head = pushed.get("head_sha") or pushed.get("sha") or pushed.get("headRefOid")
            pushed = head if head else None
        if not pushed:
            return
        head = result.get("head_sha") or result.get("headSha") or result.get("sha")
        if isinstance(pushed, str) and pushed not in {"true", "True", "1"}:
            head = head or pushed
        if not head:
            return
        number = result.get("pr_number") or result.get("number")
        if number is None and isinstance(wake.facts, Mapping):
            number = wake.facts.get("pr_number") or wake.facts.get("number")
        try:
            self.record_builder_push(wake.ticket, str(head), pr_number=int(number) if number else None)
        except (TypeError, ValueError, RuntimeError):
            # A push receipt is advisory; do not turn a completed builder run
            # into a supervisor failure when the local ledger lacks its PR.
            return

    def _record_observed_rework(self, ticket: Mapping[str, Any], facts: Mapping[str, Any]) -> None:
        """Count a builder push that Harbor observed, not one it was told about.

        ``FINAL_SCHEMA`` has no push field -- the builder literally cannot
        report one -- so the rework cap has to come from Harbor's own polling.
        The plan's fixed point is *code changed*: when an owned ticket's PR
        head moves while its builder session is retained, that is one rework
        round, and the cap then stops the agent-pushes/bot-posts loop.
        """

        if self.db is None or not self._owns(ticket):
            return
        identifier = self._ticket_key(ticket)
        if not identifier or self._latest_run(identifier) is None:
            return
        connection = getattr(self.db, "connection", None)
        if connection is None:
            return
        for row in self._pr_rows(facts):
            number = row.get("number") or row.get("pr_number")
            head = str(row.get("head_sha") or row.get("headSha") or row.get("headRefOid") or "").strip()
            if number is None or not head:
                continue
            try:
                stored = connection.execute(
                    "SELECT head_sha FROM prs WHERE number = ?", (int(number),)
                ).fetchone()
            except Exception:
                continue
            previous = str(stored[0]).strip() if stored and stored[0] else ""
            if not previous or previous == head:
                # Either the first observation of this PR, or nothing moved.
                continue
            try:
                self.record_builder_push(identifier, head, pr_number=int(number))
            except (RuntimeError, TypeError, ValueError) as exc:
                self.last_error = f"could not record rework for {identifier}: {exc}"

    def record_builder_push(
        self,
        ticket: str | Mapping[str, Any],
        head_sha: str,
        *,
        pr_number: int | None = None,
    ) -> int:
        """Record a builder-owned push without performing the push.

        Runner implementations call this after their actual push succeeds.
        Keeping the callback explicit ensures Harbor never mistakes a remote
        observation for permission to write GitHub.
        """

        if self.db is None:
            raise RuntimeError("record_builder_push requires a HarborDB")
        return record_push(self.db, ticket, head_sha, pr_number=pr_number)

    def _enrich_ticket(self, ticket: Mapping[str, Any]) -> dict[str, Any]:
        """Preserve local ownership/rework state across Linear snapshots.

        Older Phase 1 normalizers include ``owned=False`` on every snapshot,
        which can otherwise hide a ticket Harbor already owns.  This read-only
        overlay does not change Linear or the DB and is harmless for fakes.
        """

        value = dict(ticket)
        identifier = self._ticket_key(value)
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if identifier and connection is not None:
            try:
                row = connection.execute(
                    "SELECT owned, hold, bot_rework_rounds FROM tickets WHERE identifier = ?",
                    (identifier,),
                ).fetchone()
            except Exception:
                row = None
            if row is not None:
                value["owned"] = bool(row[0])
                value["hold"] = bool(row[1])
                value["bot_rework_rounds"] = int(row[2] or 0)
            try:
                run = connection.execute(
                    "SELECT id, session_id, status, waiting_on, worktree FROM runs "
                    "WHERE ticket = ? ORDER BY COALESCE(finished_at, started_at, id) DESC, id DESC LIMIT 1",
                    (identifier,),
                ).fetchone()
            except Exception:
                run = None
            if run is not None:
                value.setdefault("builder_run_id", str(run[0]))
                value.setdefault("run_id", str(run[0]))
                if run[1]:
                    value.setdefault("builder_session_id", str(run[1]))
                    value.setdefault("session_id", str(run[1]))
                value.setdefault(
                    "builder_run",
                    {
                        "id": str(run[0]),
                        "session_id": str(run[1]) if run[1] else None,
                        "status": str(run[2]) if run[2] else None,
                        "waiting_on": str(run[3]) if run[3] else None,
                        "worktree": str(run[4]) if run[4] else None,
                    },
                )
        return value

    @staticmethod
    def _pr_rows(facts: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        """Return one normalized PR fact per event processor invocation."""

        values = facts.get("prs")
        if isinstance(values, Mapping):
            values = values.values()
        if isinstance(values, Iterable) and not isinstance(values, (str, bytes, Mapping)):
            rows = tuple(item for item in values if isinstance(item, Mapping))
            if rows:
                return rows
        return (facts,) if facts.get("number") or facts.get("pr_number") else ()

    def _latest_run(self, identifier: str) -> Mapping[str, Any] | None:
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None or not identifier:
            return None
        try:
            row = connection.execute(
                "SELECT id, session_id, status, waiting_on, worktree FROM runs "
                "WHERE ticket = ? ORDER BY COALESCE(finished_at, started_at, id) DESC, id DESC LIMIT 1",
                (identifier,),
            ).fetchone()
        except Exception:
            return None
        return (
            {
                "id": str(row[0]),
                "session_id": str(row[1]) if row[1] else None,
                "status": str(row[2]) if row[2] else None,
                "waiting_on": str(row[3]) if row[3] else None,
                "worktree": str(row[4]) if row[4] else None,
            }
            if row is not None
            else None
        )

    def _owns(self, ticket: Mapping[str, Any]) -> bool:
        """Whether Harbor started this ticket, rather than merely observing it."""

        if bool(ticket.get("owned")):
            return True
        identifier = self._ticket_key(ticket)
        if not identifier or self.db is None:
            return False
        connection = getattr(self.db, "connection", None)
        if connection is None:
            return False
        try:
            row = connection.execute(
                "SELECT owned FROM tickets WHERE identifier = ?", (identifier,)
            ).fetchone()
        except Exception:
            return False
        return bool(row and row[0])

    async def _process_pr_events(self, ticket: Mapping[str, Any], facts: Mapping[str, Any]) -> None:
        """Offer newly observed and durable PR events to the retained builder."""

        processor = self.pr_event_processor
        if processor is None or not facts or self.shadow:
            return
        if not self._owns(ticket):
            # Pre-existing tickets are displayed as (observed) and never
            # woken.  Offering their review threads to a builder would both
            # cross ticket boundaries and log a spurious "no retained builder"
            # error for work Harbor never started.
            return
        process = getattr(processor, "process", None) or getattr(processor, "handle", None)
        if not callable(process):
            return
        identifier = self._ticket_key(ticket)
        run = self._latest_run(identifier)
        for row in self._pr_rows(facts):
            # ``GitHubPoller`` already performs a read-only observe while
            # constructing facts.  ``process`` is intentionally called again
            # so pending receipts survive a transient builder/contract error.
            try:
                signature = inspect.signature(process)
            except (TypeError, ValueError):
                signature = None
            if signature is None:
                value = process(ticket, row, run=run, builder=self.dispatcher or self.runner)
            else:
                parameters = signature.parameters
                kwargs: dict[str, Any] = {}
                if "run" in parameters:
                    kwargs["run"] = run
                if "builder" in parameters:
                    kwargs["builder"] = self.dispatcher or self.runner
                if "comment_handler" in parameters and (self.dispatcher or self.runner) is not None:
                    kwargs["comment_handler"] = self.dispatcher or self.runner
                value = process(ticket, row, **kwargs)
            if inspect.isawaitable(value):
                await value

    async def poll_once(self) -> PollResult | None:
        if self.lifecycle in {Lifecycle.PAUSING, Lifecycle.PAUSED}:
            return self.last_result
        try:
            result = await self.poller.poll()
        except Exception as exc:  # keep the console alive through a transient API error
            self.linear_healthy = False
            self.last_error = str(exc)
            return None
        self.linear_healthy = True
        self.last_error = None
        self.last_result = result
        self._last_activity_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # GitHub is optional in Phase 1.  When present it is strictly
        # query-only: the poller is injected and Harbor calls no mutation API.
        github_facts: list[Mapping[str, Any]] = []
        if self.github_poller is not None:
            try:
                github_facts = await self._call_github(result.tickets)
            except Exception as exc:
                self.github_healthy = False
                self.github_error = str(exc)
            else:
                self.github_healthy = True
                self.github_error = None

        self.last_pr_facts = tuple(github_facts)
        grouped = self._github_facts_by_ticket(result.tickets, github_facts)
        self.last_wakes = []
        fleet_tickets: list[dict[str, Any]] = []
        for raw_ticket in result.tickets:
            ticket = self._enrich_ticket(raw_ticket)
            identifier = self._ticket_key(ticket)
            facts = grouped.get(identifier, {})
            if facts:
                # Keep the latest read-only PR/CI/review facts next to the
                # Linear row so /status and First Mate see one fleet digest.
                ticket["prs"] = list(facts.get("prs", ()))
                if facts.get("ci") is not None:
                    ticket["ci"] = facts["ci"]
                if facts.get("threads") is not None:
                    ticket["threads"] = list(facts.get("threads", ()))
            fleet_tickets.append(ticket)
            # Persist observations before deciding so a restart sees the same
            # review/CI facts.  This helper only writes the local ledger.
            if facts and self.db is not None:
                # Must run BEFORE observe_pr_facts overwrites the stored head.
                self._record_observed_rework(ticket, facts)
                try:
                    observe_pr_facts(self.db, ticket, facts)
                except (TypeError, ValueError):
                    # A Phase 1-only fixture may not have the additive tables;
                    # the decider still has enough in-memory facts to proceed.
                    pass
            if facts:
                try:
                    await self._process_pr_events(ticket, facts)
                except Exception as exc:
                    # Event receipts are written before delivery.  A missing
                    # retained builder or malformed response therefore stays
                    # pending for a later poll, while a transient review
                    # problem cannot take down Linear/GitHub supervision.
                    self.last_error = str(exc)
            wake = self.decider.decide(ticket, facts) if self.decider is not None else None
            if wake is not None:
                await self._emit_wake(wake)
        self._fleet_tickets = tuple(fleet_tickets)
        if self.on_update:
            update = self.on_update(result)
            if asyncio.iscoroutine(update):
                await update
        return result

    async def tick(self) -> PollResult | None:
        return await self.poll_once()

    async def start(self, *, initial_poll: bool = True) -> None:
        if self.lifecycle in {Lifecycle.RUNNING, Lifecycle.STARTING}:
            return
        self.lifecycle = Lifecycle.STARTING
        self._stop.clear()
        self._dispatch_suspended = False
        if self.db:
            self.db.set_runtime(self.project, self.lifecycle.value)
        await self._apply_startup_migrations()
        await self._hydrate_retained_builders()
        if initial_poll:
            await self.poll_once()
        self.lifecycle = Lifecycle.RUNNING
        if self.db:
            self.db.set_runtime(self.project, self.lifecycle.value)
        start_heartbeat = getattr(self.heartbeat, "start", None)
        if callable(start_heartbeat):
            value = start_heartbeat()
            if inspect.isawaitable(value):
                await value
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name=f"harbor-poll-{self.project}")

    async def _hydrate_retained_builders(self) -> list[str]:
        """Rebuild handles to builders that were waiting when Harbor stopped.

        Distinct from the orphan reap, which only touches ``running`` rows
        whose process is gone.  A ``waiting`` or ``parked`` row has no process
        by definition -- its session and worktree are exactly what a later
        re-engagement needs, so recovering the handle is what makes a restart
        resume the same agent instead of stranding the ticket.
        """

        dispatcher = self.dispatcher
        if not isinstance(dispatcher, WorktreeDispatcher):
            return []
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None:
            return []
        try:
            rows = connection.execute(
                "SELECT DISTINCT ticket FROM runs WHERE status IN ('waiting', 'parked', 'blocked')"
            ).fetchall()
        except Exception:
            return []
        restored: list[str] = []
        for row in rows:
            identifier = str(row[0] or "").strip()
            if not identifier:
                continue
            try:
                runner = await dispatcher.hydrate(identifier)
            except Exception as exc:
                self.last_error = f"could not hydrate builder for {identifier}: {exc}"
                continue
            if runner is not None:
                restored.append(identifier)
        return restored

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.01, self.poll_interval))
            except asyncio.TimeoutError:
                if self.lifecycle == Lifecycle.RUNNING:
                    await self.poll_once()

    async def pause(self, reason: str | None = None) -> None:
        if self.lifecycle in {Lifecycle.PAUSED, Lifecycle.STOPPED}:
            return
        self.lifecycle = Lifecycle.PAUSING
        self._dispatch_suspended = True
        if self.db:
            self.db.set_runtime(self.project, self.lifecycle.value, reason)
        # BaseRunner.pause() pauses only its deadline; the process remains
        # alive and may finish.  A dispatcher can additionally checkpoint its
        # own persistent session, but no new dispatch/resume is allowed.
        try:
            await self._invoke_lifecycle(self.runner, ("pause", "checkpoint"), reason)
            if self.dispatcher is not None and self.dispatcher is not self.runner:
                await self._invoke_lifecycle(self.dispatcher, ("pause", "checkpoint"), reason)
        except Exception as exc:
            self.last_error = str(exc)
        self.lifecycle = Lifecycle.PAUSED
        if self.db:
            self.db.set_runtime(self.project, self.lifecycle.value, reason)

    async def resume(self) -> None:
        if self.lifecycle != Lifecycle.PAUSED:
            return
        # Re-open the deadline for a still-running process before reconciling
        # the external sources.  ``poll_once`` below is the required fresh
        # Linear/GitHub fetch and any resulting re-engagement uses the same
        # persisted run/session id from the wake ledger.
        try:
            has_active = hasattr(self.runner, "active")
            active = getattr(self.runner, "active", None)
            if self.runner is not None and (active is not None or not has_active):
                await self._invoke_lifecycle(self.runner, ("resume_deadline", "resume_pause"))
            if self.dispatcher is not None and self.dispatcher is not self.runner:
                await self._invoke_lifecycle(self.dispatcher, ("resume_deadline", "resume_pause"))
        except Exception as exc:
            self.last_error = str(exc)
        self._dispatch_suspended = False
        self.lifecycle = Lifecycle.RUNNING
        if self.db:
            self.db.set_runtime(self.project, self.lifecycle.value)
        await self.poll_once()

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task and task is not asyncio.current_task() and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                # A host embedding may close its event loop while Harbor is
                # idle.  Cancellation is already the requested stop outcome.
                pass
        stop_heartbeat = getattr(self.heartbeat, "stop", None)
        if callable(stop_heartbeat):
            value = stop_heartbeat()
            if inspect.isawaitable(value):
                await value
        self.lifecycle = Lifecycle.STOPPED
        if self.db:
            self.db.set_runtime(self.project, self.lifecycle.value)

    async def exit(self) -> None:
        await self.stop()

    def status(self) -> SupervisorStatus:
        tickets = self._fleet_tickets or (self.last_result.tickets if self.last_result else ())
        escalation_rows: tuple[dict[str, Any], ...] = ()
        run_rows: tuple[dict[str, Any], ...] = ()
        if self.db is not None:
            method = getattr(self.db, "escalations", None) or getattr(self.db, "list_escalations", None)
            if callable(method):
                try:
                    values = method()
                    escalation_rows = tuple(dict(item) for item in values if isinstance(item, Mapping))
                except Exception:
                    escalation_rows = ()
            connection = getattr(self.db, "connection", None)
            if connection is not None:
                try:
                    table = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
                    ).fetchone()
                    if table:
                        rows = connection.execute(
                            "SELECT id, ticket, role, status, waiting_on, worktree, session_id, "
                            "last_activity_at, paused_at, paused_seconds, deadline_at "
                            "FROM runs ORDER BY COALESCE(started_at, id), id"
                        ).fetchall()
                        run_rows = tuple(dict(row) for row in rows)
                except Exception:
                    run_rows = ()
        return SupervisorStatus(
            project=self.project,
            lifecycle=self.lifecycle.value,
            linear_healthy=self.linear_healthy,
            tickets=tickets,
            last_error=self.last_error,
            last_snapshot_hash=self.last_result.snapshot_hash if self.last_result else None,
            github_healthy=self.github_healthy,
            github_error=self.github_error,
            wakes=tuple(
                wake.as_dict() if hasattr(wake, "as_dict") else dict(wake) if isinstance(wake, Mapping) else {"action": getattr(wake, "action", "")}
                for wake in self.last_wakes
            ),
            last_activity=self._last_activity_at,
            heartbeat=(self.heartbeat.last_event.as_dict() if getattr(self.heartbeat, "last_event", None) else None),
            escalations=escalation_rows,
            runs=run_rows,
        )

    async def refresh(self) -> PollResult | None:
        """Fetch a fresh Linear/GitHub observation for one console question."""

        return await self.poll_once()

    async def fresh_digest(self) -> str:
        """Refresh sources once and render the deterministic fleet digest."""

        await self.refresh()
        return render_digest(self.status())

    def fleet_digest(self) -> str:
        """Render the current deterministic digest without any I/O."""

        return render_digest(self.status())

    def touch_activity(self, value: str | None = None) -> str:
        """Record runner/adapter activity used by the next heartbeat."""

        self._last_activity_at = value or datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self._last_activity_at

    async def resume_session(self, run_id: str, prompt: str) -> bool:
        """Resume one persistent builder session, without creating a replacement."""

        target = self.dispatcher or self.runner
        method = getattr(target, "resume_session", None) if target is not None else None
        if callable(method):
            value = method(run_id, prompt)
            value = await value if inspect.isawaitable(value) else value
            return value is not False
        if target is None:
            return False
        method = getattr(target, "resume", None) or getattr(target, "resume_run", None)
        if not callable(method):
            return False
        try:
            value = method(run_id, prompt)
        except TypeError:
            return False
        value = await value if inspect.isawaitable(value) else value
        return value is not False

    async def answer_escalation(self, escalation_id: str, response: str) -> dict[str, Any]:
        """Record a cited operator answer and resume its retained builder.

        Phase 7 owns escalation storage and Linear mutations.  This console
        convenience only calls its DB primitives and the injected same-session
        runner seam; it does not duplicate Linear state logic.
        """

        if self.db is None:
            raise RuntimeError("no Harbor ledger is configured")
        list_rows = getattr(self.db, "escalations", None) or getattr(self.db, "list_escalations", None)
        if not callable(list_rows):
            raise RuntimeError("the Harbor ledger has no escalation records")
        wanted = str(escalation_id).strip()
        rows = list_rows()
        row = next((item for item in rows if str(item.get("id")) == wanted), None)
        if row is None:
            raise ValueError(f"unknown escalation: {wanted}")
        text = str(response).strip()
        if not text:
            raise ValueError("an answer is required")
        citation_match = re.search(r"(?:^|\s)(?:citation|source)\s*[:=]\s*(.+?)\s*$", text, re.IGNORECASE | re.MULTILINE)
        if citation_match is None:
            citation_match = re.search(r"\[(?:citation|source)\s*[:=]\s*([^\]]+)\]", text, re.IGNORECASE)
        if citation_match is None:
            raise ValueError("a citation is required (use 'Citation: path:line')")
        citation = citation_match.group(1).strip()
        answer = (text[: citation_match.start()] + text[citation_match.end() :]).strip(" \t\r\n-|")
        if not answer:
            raise ValueError("an answer is required")
        record = getattr(self.db, "record_resolution", None) or getattr(self.db, "add_resolution", None)
        if not callable(record):
            raise RuntimeError("the Harbor ledger cannot record resolutions")
        try:
            resolution = record(
                str(row.get("kind", "env_blocker")),
                str(row.get("question", "")),
                answer,
                citation,
                source_ticket=row.get("ticket"),
            )
        except TypeError:
            resolution = record(str(row.get("kind", "env_blocker")), str(row.get("question", "")), answer, citation)
        run_id = str(row.get("run_id") or "")
        resumed = False
        if run_id:
            resumed = await self.resume_session(
                run_id,
                f"Operator answer for escalation {wanted}: {answer}\nAuthority citation: {citation}",
            )
        return {"escalation": dict(row), "resolution": resolution, "resumed": resumed, "answer": answer, "citation": citation}


__all__ = ["Lifecycle", "Supervisor", "SupervisorStatus", "WorktreeDispatcher"]
