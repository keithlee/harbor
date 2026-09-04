"""Deterministic polling and optional Phase 6 re-engagement supervisor.

Polling remains model-free.  A GitHub poller, wake decider, and runner are
injectable so tests can use fakes and a project can swap its delivery skill
without Harbor learning the skill's name or internals.  Harbor only emits a
``Wake`` value; the runner/skill owns all GitHub and Linear mutations.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable

from harbor.db import HarborDB, apply_phase9_migration
from harbor.digest import render_digest
from harbor.wake import delivery_tools_fact, detect_delivery_tools
from harbor.heartbeat import Heartbeat, HeartbeatEvent
from harbor.poll.linear_poller import LinearPoller, PollResult
from harbor.runner.base import (
    apply_phase4_migration,
    normalize_final_result,
    reap_orphan_runs,
)
from harbor.wake import (
    Wake,
    WakeDecider,
    TicketScope,
    apply_phase3_migration,
    apply_phase6_migration,
    observe_pr_facts,
    record_push,
    ticket_scope as normalize_ticket_scope,
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
        ticket_scope: str | TicketScope | None = None,
    ) -> None:
        self.worktree_manager = worktree_manager
        self.runner_factory = runner_factory
        self.db = db
        self.project = str(project)
        self.builder_prompt = str(builder_prompt or "").strip()
        self.push_recorder = push_recorder
        self.ticket_scope = normalize_ticket_scope(ticket_scope)
        # Set by Supervisor so a BaseRunner can normalize a final result
        # against Harbor's last read-only PR observation before persisting it.
        self.observed_pr_facts: Callable[[str], Any] | None = None
        self.runners: dict[str, Any] = {}
        self.worktrees: dict[str, Any] = {}
        # Serializes the whole lifecycle of one ticket's retained builder.
        # This closes the pre-runs-row window between storing ``runner`` and
        # BaseRunner inserting its ``running`` row in SQLite.
        self._execution_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _identifier(wake: Wake) -> str:
        return str(wake.ticket.get("identifier") or wake.ticket.get("id") or "")

    def _in_scope(self, value: Any) -> bool:
        """Check one ticket identifier against this invocation's custody scope."""

        return self.ticket_scope.matches(value)

    def _retained_runners(self) -> tuple[Any, ...]:
        """Return only builders owned by this invocation's ticket scope."""

        return tuple(
            runner
            for identifier, runner in self.runners.items()
            if self._in_scope(identifier)
        )

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

    def _run_is_running(self, identifier: str, run_id: str | None = None) -> bool:
        """Fail closed when a direct resume races a retained process."""

        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None or not identifier:
            return False
        try:
            if run_id:
                row = connection.execute(
                    "SELECT status FROM runs WHERE id = ? LIMIT 1", (str(run_id),)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT status FROM runs WHERE ticket = ? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (identifier,),
                ).fetchone()
        except Exception:
            return False
        return bool(row and str(row[0] or "").casefold() == "running")

    def _execution_lock(self, identifier: str) -> asyncio.Lock:
        """Return the per-ticket lock used by every builder handoff."""

        lock = self._execution_locks.get(identifier)
        if lock is None:
            lock = asyncio.Lock()
            self._execution_locks[identifier] = lock
        return lock

    def _initial_wake(self, wake: Wake) -> Wake:
        # Report which delivery tools actually work in the builder's sandbox.
        # A builder that reaches only for an MCP tool strands an approved
        # merge, because a non-interactive run refuses any MCP call that wants
        # approval -- even while plain `gh` is authenticated right there.
        environment = delivery_tools_fact(detect_delivery_tools())
        base = self.builder_prompt
        if environment and wake.action == "build":
            base = f"{base}\n\n{environment}" if base else environment
        if not base or wake.action != "build":
            return wake
        instruction = f"{base}\n\n{wake.instruction}" if wake.instruction else base
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
        value = await value if inspect.isawaitable(value) else value
        observer = self.observed_pr_facts
        if observer is not None and value is not None:
            # This is intentionally duck-typed: production BaseRunner exposes
            # the callback, while tiny test runners may simply ignore it.
            try:
                setattr(value, "observed_pr_facts", observer)
            except (AttributeError, TypeError):
                pass
        return value

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
        if not identifier or not self._in_scope(identifier):
            return None
        lock = self._execution_lock(identifier)
        if lock.locked():
            # A second wake during the first runner's pre-ledger setup or
            # process execution must not create/resume another agent.
            return None
        async with lock:
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
                    "SELECT id, session_id, status, worktree, ticket FROM runs "
                    "WHERE ticket = ? COLLATE NOCASE "
                    "ORDER BY COALESCE(finished_at, started_at, id) DESC, id DESC LIMIT 1",
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
            "ticket": str(row[4]) if row[4] else identifier,
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

        identifier = str(identifier or "").strip()
        if not identifier or not self._in_scope(identifier):
            return None
        existing_key = next(
            (key for key in self.runners if str(key).casefold() == identifier.casefold()),
            None,
        )
        if existing_key is not None:
            return self.runners.get(existing_key)
        row = self._persisted_run(identifier)
        if row is None or row.get("status") not in {"waiting", "parked", "blocked"}:
            return None
        identifier = str(row.get("ticket") or identifier)
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
        if not identifier or not self._in_scope(identifier):
            return None
        lock = self._execution_lock(identifier)
        if lock.locked():
            # Do not wait behind another handoff: that would turn a duplicate
            # wake into a second sequential resume after the first completes.
            return None
        async with lock:
            if self._run_is_running(identifier, wake.run_id):
                # A PR poll can race a process that has not emitted its final
                # result yet.  Its event remains durable for a later poll.
                return None
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
        if not ticket:
            raise PREventError("no retained builder for PR event ticket (unknown)")
        if not self._in_scope(ticket):
            raise PREventError(
                f"ticket {ticket or '(unknown)'} is outside invocation scope "
                f"{self.ticket_scope.selector}"
            )
        lock = self._execution_lock(ticket)
        if lock.locked():
            raise PREventError(
                f"builder handoff for {ticket or '(unknown)'} is already active; defer PR event"
            )
        async with lock:
            if self._run_is_running(ticket, str(run_id) if run_id else None):
                raise PREventError(
                    f"builder run for {ticket or '(unknown)'} is still running; defer PR event"
                )
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
        from the ledger before reporting the session unavailable.  Route the
        handoff through ``resume`` so its per-ticket lock and running-row guard
        apply to operator answers as well as poll-driven re-engagements.
        """

        run_ticket = self._ticket_for_run(run_id)
        if self.ticket_scope and (not run_ticket or not self._in_scope(run_ticket)):
            return False
        if not run_ticket:
            # A retained runner can be useful with a lightweight in-memory
            # adapter that has no runs table.  Recover its ticket only when it
            # already owns this exact logical session.
            for identifier, runner in self.runners.items():
                contexts = getattr(runner, "contexts", {})
                if str(run_id) in contexts or str(getattr(runner, "default_run_id", "")) == str(run_id):
                    run_ticket = str(identifier)
                    break
        if not run_ticket:
            return False
        wake = Wake(
            ticket={"identifier": run_ticket, "builder_run_id": str(run_id), "run_id": str(run_id)},
            action="reengage",
            resume=str(run_id),
            instruction=prompt,
        )
        value = await self.resume(wake)
        # Production runners return a RunResult/dict.  ``None`` is reserved by
        # ``resume`` for a guard miss or unavailable retained session.
        return value is not None and value is not False

    async def pause(self, reason: str | None = None) -> None:
        for runner in self._retained_runners():
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
        for runner in self._retained_runners():
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
        ticket_scope: str | TicketScope | None = None,
        ticket: str | None = None,
    ) -> None:
        self.poller = poller
        self.db = db if db is not None else getattr(poller, "db", None)
        self.project = poller.project_name
        if ticket is not None:
            if ticket_scope is not None and normalize_ticket_scope(ticket_scope).normalized != normalize_ticket_scope(ticket).normalized:
                raise ValueError("ticket and ticket_scope selectors must match")
            ticket_scope = ticket
        self.ticket_scope = normalize_ticket_scope(ticket_scope)
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
                ticket_scope=self.ticket_scope,
            )
        self.worktree_manager = worktree_manager
        self.runner_factory = runner_factory
        self.dispatcher = dispatcher or (
            WorktreeDispatcher(
                worktree_manager,
                runner_factory,
                db=self.db,
                project=self.project,
                ticket_scope=self.ticket_scope,
            )
            if worktree_manager is not None and runner_factory is not None and runner is None
            else None
        )
        self.runner = runner
        # Wire the read-only observation seam into both the persistent
        # dispatcher and a directly injected runner.  The callback is bound
        # below on this Supervisor and never performs external writes.
        if isinstance(self.dispatcher, WorktreeDispatcher):
            self.dispatcher.ticket_scope = self.ticket_scope
            self.dispatcher.observed_pr_facts = self.observed_pr_facts
        for target in (self.runner,):
            if target is not None:
                try:
                    setattr(target, "observed_pr_facts", self.observed_pr_facts)
                except (AttributeError, TypeError):
                    pass
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
        # ``poll_once`` remains awaitable and deterministic for embedders and
        # tests that call it directly.  Once the supervisor is started, wake
        # delivery is handed to a task so a slow builder cannot block the
        # operator console (or the next poll/heartbeat).
        self._background_dispatch = False
        self._dispatch_tasks: dict[str, asyncio.Task[Any]] = {}
        self._dispatch_wakes: dict[str, Wake] = {}
        self._started_build_tickets: set[str] = set()
        # A process-local marker for a merge receipt obtained from an
        # observed PR fact.  Builder JSON alone never enters this set.
        self._verified_merge_runs: set[str] = set()
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

    def _in_scope(self, value: Any) -> bool:
        """Return whether one ticket may mutate or reach a builder."""

        return self.ticket_scope.matches(value)

    @staticmethod
    def _pr_number(value: Any) -> str | None:
        """Extract a PR number for matching a final artifact to one PR."""

        if isinstance(value, bool):
            return None
        text = str(value or "").strip()
        if text.isdigit():
            return text
        match = re.search(r"/pulls?/([0-9]+)(?:$|[/?#])", text, re.IGNORECASE)
        return match.group(1) if match else None

    @classmethod
    def _pr_fact_rows(cls, facts: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        """Return the individual PR rows inside either aggregate or direct facts."""

        values = facts.get("prs")
        if isinstance(values, Mapping):
            values = values.values()
        rows = tuple(item for item in values or () if isinstance(item, Mapping)) if not isinstance(values, (str, bytes, Mapping)) else ()
        if rows:
            return rows
        return (facts,) if facts.get("number") or facts.get("pr_number") else ()

    @classmethod
    def _matching_pr_fact(
        cls,
        facts: Mapping[str, Any] | None,
        pr_url: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Select the observed row corresponding to a builder's PR artifact."""

        if not isinstance(facts, Mapping):
            return None
        rows = cls._pr_fact_rows(facts)
        if not rows:
            return None
        target_number = cls._pr_number(pr_url)
        if pr_url:
            target_url = str(pr_url).rstrip("/")
            for row in rows:
                candidate = row.get("url") or row.get("html_url") or row.get("href")
                if candidate and str(candidate).rstrip("/") == target_url:
                    return row
        if target_number:
            for row in rows:
                number = row.get("number", row.get("pr_number"))
                if cls._pr_number(number) == target_number:
                    return row
        return rows[0] if len(rows) == 1 else None

    @staticmethod
    def _fact_confirms_merge(facts: Mapping[str, Any] | None) -> bool:
        """Accept only a positive read-only PR observation as merge proof."""

        if not isinstance(facts, Mapping):
            return False
        # GitHubPoller emits a bool.  Do not let a string such as ``"false"``
        # become truthy evidence merely because it came from a JSON adapter.
        merged = facts.get("merged")
        if isinstance(merged, bool) and merged:
            return True
        state = facts.get("state")
        if isinstance(state, str) and state.casefold() == "merged":
            return True
        return bool(
            isinstance(facts.get("merge_commit_sha"), str)
            and facts.get("merge_commit_sha", "").strip()
            or isinstance(facts.get("merged_at"), str)
            and facts.get("merged_at", "").strip()
        )

    def observed_pr_facts(self, ticket: str | Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Return the latest local read-only PR observation for one ticket.

        This callback is injected into :class:`BaseRunner` before a process
        starts.  It reads only the in-memory poll result and local observation
        ledger; it never calls Linear/GitHub and never changes state.
        """

        identifier = self._ticket_key(ticket) if isinstance(ticket, Mapping) else str(ticket or "")
        if not identifier or not self._in_scope(identifier):
            return None
        rows = [
            item
            for item in self.last_pr_facts
            if str(item.get("ticket") or item.get("ticket_id") or "") == identifier
        ]
        if rows:
            return rows[0] if len(rows) == 1 else {"ticket": identifier, "prs": rows}
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None:
            return None
        try:
            stored = connection.execute(
                "SELECT number, url, head_sha, ci_state, merged, observed_at "
                "FROM prs WHERE ticket = ? ORDER BY number",
                (identifier,),
            ).fetchall()
        except Exception:
            return None
        values = [
            {
                "number": int(row[0]),
                "url": row[1],
                "head_sha": row[2],
                "ci_state": row[3],
                "merged": bool(row[4]),
                "observed_at": row[5],
            }
            for row in stored
        ]
        if not values:
            return None
        return values[0] if len(values) == 1 else {"ticket": identifier, "prs": values}

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

    async def _invoke_termination(self, target: Any, *, reason: str) -> Any:
        """Invoke a runner termination seam without guessing its context.

        ``BaseRunner.terminate`` accepts an optional positional context and
        keyword-only ``reason``/``grace_seconds``.  Small dispatch fakes often
        expose ``stop()`` or ``terminate(reason)`` instead.  Inspecting the
        signature keeps shutdown one-shot and avoids passing the operator
        reason as a runner context by accident.
        """

        if target is None:
            return None
        method = next(
            (
                getattr(target, name, None)
                for name in ("terminate", "kill", "stop")
                if callable(getattr(target, name, None))
            ),
            None,
        )
        if method is None:
            return None
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            signature = None
        if signature is None:
            value = method()
        else:
            parameters = signature.parameters
            kwargs: dict[str, Any] = {}
            if "reason" in parameters:
                kwargs["reason"] = reason
            if "grace_seconds" in parameters:
                kwargs["grace_seconds"] = 1.0
            if kwargs:
                value = method(**kwargs)
            else:
                positional = [
                    parameter
                    for parameter in parameters.values()
                    if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ]
                required = [parameter for parameter in positional if parameter.default is inspect.Parameter.empty]
                if required and positional[0].name.casefold() not in {"context", "run", "run_id", "runid", "id"}:
                    value = method(reason)
                else:
                    value = method()
        return await value if inspect.isawaitable(value) else value

    async def _stop_active_dispatches(self, *, reason: str = "operator exit") -> None:
        """Terminate active builders, then drain all delivery wrapper tasks.

        The CLI closes its database and API clients immediately after
        ``Supervisor.exit`` returns.  A background delivery task must
        therefore be finished (or explicitly cancelled after termination)
        before this method returns.  Termination is best-effort and bounded;
        a malformed injected adapter cannot make `/exit` hang forever.
        """

        dispatch_entries = tuple(
            (key, task)
            for key, task in self._dispatch_tasks.items()
            if not task.done() and self._dispatch_in_scope(key, self._dispatch_wakes.get(key))
        )
        tasks = tuple(task for _, task in dispatch_entries)

        targets: list[Any] = []
        retained = getattr(self.dispatcher, "runners", None)
        for target in (self.dispatcher, self.runner):
            # A per-ticket dispatcher must be terminated through its filtered
            # retained-runner map below.  Calling an aggregate termination
            # hook would let a scoped invocation kill foreign builders.
            if self.ticket_scope and isinstance(getattr(target, "runners", None), Mapping):
                continue
            if target is not None and all(target is not existing for existing in targets):
                targets.append(target)
        # The production dispatcher retains one runner per ticket.  It has no
        # single process to terminate, so visit only in-scope runners.
        if isinstance(retained, Mapping):
            for identifier, target in retained.items():
                if not self._in_scope(identifier):
                    continue
                if target is not None and all(target is not existing for existing in targets):
                    targets.append(target)
        for target in targets:
            try:
                await asyncio.wait_for(
                    self._invoke_termination(target, reason=reason),
                    timeout=2.0,
                )
            except asyncio.TimeoutError as exc:
                # A termination hook is an integration seam.  Keep the
                # literal failure visible, but still cancel its wrapper below
                # so closed clients can never be reached by a late callback.
                self.last_error = str(exc)
            except Exception as exc:
                # Keep the literal failure visible, but still cancel its
                # wrapper below so closed clients can never be reached by a
                # late callback.
                self.last_error = str(exc)
        # A wrapper cancelled before its runner starts otherwise leaves its
        # durable queue row in ``queued``.  That live idempotency receipt would
        # suppress the ticket forever on restart.  ``stopped`` is terminal for
        # this attempt but deliberately outside the live-idem set, so a later
        # Harbor start can dispatch the still-Todo ticket exactly once.
        if self.db is not None:
            transition = getattr(self.db, "transition_queue", None)
            transition_ticket = getattr(self.db, "transition_ticket_queue", None)
            for key, task in dispatch_entries:
                wake = self._dispatch_wakes.get(key)
                try:
                    if wake is not None and wake.idem_key and callable(transition):
                        transition("stopped", idem_key=wake.idem_key, error=reason)
                    elif wake is not None and callable(transition_ticket):
                        transition_ticket(
                            self._wake_ticket_identifier(wake),
                            "stopped",
                            action=wake.action,
                            error=reason,
                        )
                except Exception as exc:
                    self.last_error = str(exc)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for key, _ in dispatch_entries:
            self._dispatch_tasks.pop(key, None)
            self._dispatch_wakes.pop(key, None)

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
            hooks = (*hooks, apply_phase9_migration)
        for migration in hooks:
            try:
                parameters = list(inspect.signature(migration).parameters.values())
            except (TypeError, ValueError):
                parameters = [object()]
            value = migration() if not parameters else migration(self.db)
            if inspect.isawaitable(value):
                await value
        # A restart cannot safely reuse an orphaned active process.  Shadow is
        # an observation-only invocation, however: it may share the ledger
        # with the live console and must never kill or rewrite that console's
        # running rows.
        if not self.shadow:
            if self.ticket_scope:
                await reap_orphan_runs(self.db, ticket_scope=self.ticket_scope)
            else:
                # Preserve the original unscoped call shape for embedders that
                # provide a narrow reap adapter of their own.
                await reap_orphan_runs(self.db)
        self._migrations_applied = True

    async def _reconcile_completed_merges(self) -> None:
        """Recover merge receipts that arrived just after a builder exited.

        A successful skill can merge a PR and return before Harbor's next
        normal poll.  If Linear has already moved to ``Done``, that ticket is
        outside the configured poll states and would otherwise strand the
        completed run and its worktree forever.  Reconciliation is scoped to
        persisted ``completed/outcome=merged`` rows and queries only the PR
        URL already present in the final artifact.
        """

        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None:
            return
        try:
            rows = connection.execute(
                "SELECT id, ticket, worktree, final_json, merge_verified, cleanup_at "
                "FROM runs WHERE status = 'completed' AND "
                "(COALESCE(merge_verified, 0) = 0 OR cleanup_at IS NULL)"
            ).fetchall()
        except Exception:
            return
        for row in rows:
            run_id = str(row[0])
            identifier = str(row[1])
            if not self._in_scope(identifier):
                continue
            # A merge receipt can be durable even when cleanup failed (for
            # example, a transient filesystem error).  Retry cleanup from
            # that Harbor receipt alone; do not parse or trust builder JSON.
            if row[4] == 1 and row[5] is None:
                ticket = {"identifier": identifier}
                wake = Wake(
                    ticket=ticket,
                    action="reengage",
                    resume=run_id,
                    facts={},
                    reason="retry-verified-merge-cleanup",
                )
                result = {
                    "status": "completed",
                    "outcome": "merged",
                    "waiting_on": "none",
                    "merge_verified": True,
                    "run_id": run_id,
                }
                await self._cleanup_merged_worktree(wake, result)
                continue
            try:
                payload = json.loads(row[3]) if row[3] else {}
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, Mapping):
                continue
            if str(payload.get("outcome", "")).casefold() != "merged":
                continue
            pr_url = payload.get("pr_url") or payload.get("pull_request_url")
            if not isinstance(pr_url, str) or not pr_url.strip():
                continue
            ticket = {"identifier": identifier, "pr_urls": [pr_url]}
            wake = Wake(
                ticket=ticket,
                action="reengage",
                resume=run_id,
                facts={},
                reason="reconcile-completed-merge",
            )
            # GitHub is optional for a retry of an already durable Harbor
            # receipt.  Unverified rows still need the read-only poller below;
            # never promote their builder JSON merely because reconciliation
            # ran during startup.
            if self.github_poller is None:
                continue
            normalized = await self._reconcile_runner_result(wake, dict(payload))
            if normalized.get("merge_verified") is True:
                await self._cleanup_merged_worktree(wake, normalized)

    @staticmethod
    def _wake_ticket_identifier(wake: Wake) -> str:
        return str(wake.ticket.get("identifier") or wake.ticket.get("id") or "")

    def _dispatch_in_scope(self, key: str, wake: Wake | None = None) -> bool:
        """Filter one delivery wrapper by its exact ticket."""

        if wake is not None:
            return self._in_scope(wake.ticket)
        # Production tasks always retain their wake alongside the task.  The
        # fallback keeps manually injected task maps fail-closed for scoped
        # invocations while preserving the unscoped behavior.
        return self._in_scope(str(key).split("@", 1)[0])

    async def _deliver_wake(self, wake: Wake) -> None:
        """Deliver one wake, keeping the slow runner off the poll loop."""

        # ``_emit_wake`` is the normal gate, but this second check protects
        # embedders that call the delivery seam directly.
        if not self._in_scope(wake.ticket):
            return
        target = self.dispatcher or self.runner
        if wake.action == "build":
            result = await self._invoke(target, ("dispatch", "start", "build", "run"), wake)
            await self._handle_runner_result(wake, result)
        elif wake.action == "reengage":
            result = await self._invoke(target, ("resume", "reengage", "continue_run", "continue"), wake)
            await self._handle_runner_result(wake, result)
        elif wake.action == "stop" and wake.notify:
            await self._invoke(self.notifier, ("notify", "send", "publish"), wake)

    def _dispatch_finished(self, key: str, task: asyncio.Task[Any]) -> None:
        """Consume a background delivery result so it cannot be lost/warned."""

        wake = self._dispatch_wakes.get(key)
        if self._dispatch_tasks.get(key) is task:
            self._dispatch_tasks.pop(key, None)
            self._dispatch_wakes.pop(key, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            # A failed builder must not kill polling.  Keep the literal error
            # available to /status and the next First Mate query.  The
            # delivery task can fail before BaseRunner has inserted a run row
            # (for example, WorktreeManager.create raises DiskFloor).  Close
            # that queue attempt explicitly so its stable build idem key is
            # no longer a live receipt that suppresses a restart forever.
            self.last_error = str(exc)
            self._record_dispatch_failure(wake, str(exc))

    def _record_dispatch_failure(self, wake: Wake | None, error: str) -> None:
        """Terminalize a failed background delivery in the local queue.

        Queue rows are retained as audit receipts.  A ``failed`` row is outside
        the live idempotency index, allowing the unchanged ``<ticket>@build``
        key to be enqueued on a later poll.  Ownership intentionally remains
        set: enqueue is the existing Harbor custody boundary, and there is no
        run/session to release or hand to a different owner when preparation
        fails.

        The in-process build guard is released only when this failure happened
        before any run row was created.  If a runner did start and then raised,
        retaining the guard prevents a later Todo observation from creating a
        second active builder.
        """

        if wake is None:
            return
        identifier = self._wake_ticket_identifier(wake)
        pre_run = self._dispatch_has_no_run(identifier)
        if wake.action == "build" and pre_run:
            self._started_build_tickets.discard(identifier)

        if self.db is None:
            return
        transition = getattr(self.db, "transition_queue", None)
        transitioned = False
        try:
            if callable(transition) and wake.idem_key:
                transitioned = bool(transition("failed", idem_key=wake.idem_key, error=error))
            if not transitioned:
                transition_ticket = getattr(self.db, "transition_ticket_queue", None)
                if callable(transition_ticket) and identifier:
                    transition_ticket(
                        identifier,
                        "failed",
                        action=wake.action,
                        error=error,
                    )
        except Exception as exc:
            # Queue persistence is an audit seam, but a failure to write that
            # receipt must remain visible alongside the original dispatch
            # error.  Do not replace the literal delivery exception.
            self.last_error = f"{error} (queue transition failed: {exc})"

    def _dispatch_has_no_run(self, identifier: str) -> bool:
        """Return whether a failed delivery created no durable run row."""

        if not identifier:
            return True
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None:
            # DB-less embedders have no durable run to protect; let their
            # process-local build gate retry after a failed handoff.
            return True
        try:
            row = connection.execute(
                "SELECT 1 FROM runs WHERE ticket = ? COLLATE NOCASE LIMIT 1", (identifier,)
            ).fetchone()
        except Exception:
            # Fail closed around duplicate prevention when the ledger cannot
            # answer whether a runner started.
            return False
        return row is None

    def _schedule_wake(self, wake: Wake) -> None:
        """Schedule one active delivery per ticket and action.

        The durable WakeDecider queue is the cross-restart idempotency gate.
        This in-process gate closes the smaller race where a fast subsequent
        poll observes the same ticket before the queue/run row is updated.
        """

        if not self._in_scope(wake.ticket):
            return
        identifier = self._wake_ticket_identifier(wake)
        key = identifier or wake.idem_key or wake.action
        existing = self._dispatch_tasks.get(key)
        if existing is not None and not existing.done():
            return
        if wake.action == "build":
            # A completed build can briefly leave Linear in Todo while the
            # skill writes its status.  Do not start a second builder for that
            # same in-process ticket during that window.
            if identifier in self._started_build_tickets:
                return
            self._started_build_tickets.add(identifier)
        task = asyncio.create_task(self._deliver_wake(wake), name=f"harbor-dispatch-{key}")
        self._dispatch_tasks[key] = task
        self._dispatch_wakes[key] = wake
        task.add_done_callback(lambda finished, key=key: self._dispatch_finished(key, finished))

    async def _emit_wake(self, wake: Wake) -> None:
        # Observation is fleet-wide, but no out-of-scope wake may reach an
        # on_wake callback, queue-backed dispatcher, or notifier.
        if not self._in_scope(wake.ticket):
            return
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
        if self._background_dispatch and wake.action in {"build", "reengage"}:
            self._schedule_wake(wake)
            return
        await self._deliver_wake(wake)

    async def _observe_merge_for_result(
        self,
        wake: Wake,
        result: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Refresh one completed PR result using read-only GitHub facts.

        A builder can finish immediately after merging, before the next normal
        poll.  Its final JSON is still only a claim at that point.  If the
        claim names a PR, query that exact attachment once and use the returned
        PR facts as the merge receipt.  Cached facts remain useful when a
        restart occurs while GitHub is temporarily unavailable.
        """

        if not self._in_scope(wake.ticket):
            return None
        if str(result.get("status", "")).casefold() != "completed":
            return None
        pr_url = result.get("pr_url") or result.get("pull_request_url")
        candidate = dict(wake.ticket)
        if pr_url:
            urls = candidate.get("pr_urls")
            if isinstance(urls, Mapping):
                urls = urls.get("nodes", ())
            if isinstance(urls, (str, bytes)) or urls is None:
                urls = [urls] if urls else []
            urls = list(urls)
            if str(pr_url) not in {str(item) for item in urls}:
                urls.append(str(pr_url))
            candidate["pr_urls"] = urls

        # A Wake can carry facts from the deterministic GitHub poll that
        # caused the resume.  Treat only a positive PR fact as authority; the
        # builder result remains an untrusted claim.
        wake_facts = wake.facts if isinstance(wake.facts, Mapping) else None
        match = self._matching_pr_fact(wake_facts, str(pr_url) if pr_url else None)
        if self._fact_confirms_merge(match):
            return match

        cached = self.observed_pr_facts(candidate)
        match = self._matching_pr_fact(cached, str(pr_url) if pr_url else None)
        if self._fact_confirms_merge(match):
            return match
        # A completed run with no PR cannot be verified by GitHub.  Likewise,
        # avoid a broad query when there is no named artifact to bind to.
        if self.github_poller is None or not pr_url:
            return match
        try:
            fresh_rows = await self._call_github((candidate,))
        except Exception as exc:
            self.github_healthy = False
            self.github_error = str(exc)
            return match
        self.github_healthy = True
        self.github_error = None
        grouped = self._github_facts_by_ticket((candidate,), fresh_rows).get(
            self._ticket_key(candidate), {}
        )
        match = self._matching_pr_fact(grouped, str(pr_url))
        if match is not None and self.db is not None:
            # Keep the local observation ledger in sync for restart recovery;
            # this helper has no Linear/GitHub mutation capability.
            try:
                observe_pr_facts(self.db, candidate, match)
            except (TypeError, ValueError):
                pass
        return match

    def _run_id_for_result(self, wake: Wake, result: Mapping[str, Any]) -> str:
        """Resolve an exact run identity without guessing across runs."""

        run_id = result.get("run_id") or wake.run_id
        if run_id:
            return str(run_id)
        identifier = self._ticket_key(wake.ticket)
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None or not identifier:
            return ""
        try:
            rows = connection.execute(
                "SELECT id FROM runs WHERE ticket = ? ORDER BY COALESCE(started_at, id), id",
                (identifier,),
            ).fetchall()
        except Exception:
            return ""
        # With more than one run for a ticket, selecting "latest" could bind
        # an older result to a newer worktree.  Require the caller to carry
        # the run id in that case.
        return str(rows[0][0]) if len(rows) == 1 else ""

    def _merge_receipt_key(self, wake: Wake, result: Mapping[str, Any]) -> str:
        run_id = self._run_id_for_result(wake, result)
        identifier = self._ticket_key(wake.ticket)
        return f"{identifier}\x00{run_id}" if identifier and run_id else ""

    def _run_merge_receipt_is_authorized(self, wake: Wake, result: Mapping[str, Any]) -> bool:
        """Require an observed receipt, including for direct cleanup calls."""

        # A deterministic wake may carry the exact PR observation that caused
        # this delivery.  This is the only in-memory authority available to
        # DB-less embedders/tests; builder result fields are intentionally not
        # consulted.
        if self._fact_confirms_merge(
            wake.facts if isinstance(wake.facts, Mapping) else None
        ):
            return True
        key = self._merge_receipt_key(wake, result)
        if key and key in self._verified_merge_runs:
            return True
        # A persisted ``merge_verified=1`` is a Harbor receipt created from a
        # prior positive PR observation.  It is safe for a cleanup retry, but
        # only when the result identifies that exact run.
        run_id = self._run_id_for_result(wake, result)
        identifier = self._ticket_key(wake.ticket)
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if not run_id or connection is None or not identifier:
            return False
        try:
            row = connection.execute(
                "SELECT ticket, merge_verified FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        except Exception:
            return False
        return bool(row and str(row[0]) == identifier and row[1] == 1)

    def _update_run_receipt(
        self,
        wake: Wake,
        result: Mapping[str, Any],
        *,
        cleanup_at: str | None = None,
        persist_final: bool = True,
    ) -> None:
        """Persist the verified merge and cleanup receipts when supported."""

        if not self._in_scope(wake.ticket):
            return
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        run_id = self._run_id_for_result(wake, result)
        if connection is None or not run_id:
            return
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        except Exception:
            return
        updates: dict[str, Any] = {}
        if persist_final and "final_json" in columns:
            updates["final_json"] = json.dumps(dict(result), ensure_ascii=False, sort_keys=True)
        if "merge_verified" in columns:
            updates["merge_verified"] = int(result.get("merge_verified") is True)
        if "merge_commit_sha" in columns and result.get("merge_commit_sha"):
            updates["merge_commit_sha"] = result.get("merge_commit_sha")
        if cleanup_at is not None and "cleanup_at" in columns:
            updates["cleanup_at"] = cleanup_at
        if not updates:
            return
        lock = getattr(self.db, "_lock", None)
        if lock is None:
            from contextlib import nullcontext

            lock = nullcontext()
        try:
            with lock:
                connection.execute(
                    f"UPDATE runs SET {', '.join(f'{key} = ?' for key in updates)} WHERE id = ?",
                    tuple(updates.values()) + (run_id,),
                )
                connection.commit()
        except Exception:
            # Receipt persistence is audit-only; never turn a successfully
            # verified merge into a supervisor outage because an old ledger
            # lacks one of the additive columns.
            return

    async def _reconcile_runner_result(self, wake: Wake, result: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize a builder result and attach a read-only merge receipt."""

        if not self._in_scope(wake.ticket):
            return dict(result)
        observed = await self._observe_merge_for_result(wake, result)
        try:
            normalized = normalize_final_result(
                result,
                observed=observed if self._fact_confirms_merge(observed) else None,
                known_pr_url=(
                    str(result.get("pr_url"))
                    if result.get("pr_url")
                    else None
                ),
            )
        except Exception:
            # The runner already validated its result.  A duck-typed delivery
            # adapter may return richer metadata; retain it if normalization
            # cannot interpret that adapter-specific shape.
            normalized = dict(result)
        if self._fact_confirms_merge(observed) and normalized.get("merge_verified") is True:
            receipt_key = self._merge_receipt_key(wake, normalized)
            if receipt_key:
                self._verified_merge_runs.add(receipt_key)
            self._update_run_receipt(wake, normalized)
        return normalized

    async def _handle_runner_result(self, wake: Wake, result: Any) -> None:
        """Route a blocked builder final to the injected Phase 7 ladder."""

        if not self._in_scope(wake.ticket):
            return
        if hasattr(result, "as_dict") and callable(result.as_dict):
            result = result.as_dict(include_metadata=True)
        if isinstance(result, Mapping):
            result = await self._reconcile_runner_result(wake, result)
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

        if not self._in_scope(wake.ticket):
            return
        if str(result.get("status", "")).casefold() != "completed":
            return
        if str(result.get("outcome", "")).casefold() != "merged":
            return
        if result.get("merge_verified") is not True:
            # The merge is claimed but unobserved.  Keep the worktree; the
            # next poll confirms it (plan section 7, AC-10) or it never does.
            return
        identifier = self._ticket_key(wake.ticket)
        if not identifier:
            return
        # ``merge_verified`` in a delivered result is not itself authority:
        # direct callers can pass builder-shaped JSON straight to this seam.
        # Only a receipt created from an observed PR fact (or its durable
        # exact-run equivalent during restart) can authorize cleanup.
        if not self._run_merge_receipt_is_authorized(wake, result):
            return
        manager = self.worktree_manager or getattr(self.dispatcher, "worktree_manager", None)
        if manager is None:
            return
        dispatcher = self.dispatcher

        # When a ledger is present, bind both identity and path to the exact
        # persisted run.  Looking up ``path_for(ticket)`` or the current
        # dispatcher map can remove a newer same-ticket worktree after an
        # older completed result is delivered.
        run_id = self._run_id_for_result(wake, result)
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        target = None
        persisted_target = False
        if connection is not None:
            if not run_id:
                return
            try:
                row = connection.execute(
                    "SELECT ticket, worktree, merge_verified FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
            except Exception:
                return
            if row is not None:
                if str(row[0] or "") != identifier or row[2] != 1:
                    return
                persisted_target = True
                if row[1]:
                    target = Path(str(row[1]))
            elif wake.reason in {"reconcile-completed-merge", "retry-verified-merge-cleanup"}:
                # Startup reconciliation is only allowed to act on the exact
                # durable run row it selected.  Never fall back to a
                # ticket-level path lookup in this mode.
                return
        if target is None and not persisted_target:
            if isinstance(dispatcher, WorktreeDispatcher):
                target = dispatcher.worktrees.get(identifier)
            if target is None:
                path_for = getattr(manager, "path_for", None)
                if callable(path_for):
                    target = path_for(identifier)
        # A durable receipt with no path has no local resource to remove.  It
        # is safe to close the idempotent cleanup receipt in that case.
        if target is None:
            if persisted_target:
                self._update_run_receipt(
                    wake,
                    result,
                    cleanup_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    persist_final=False,
                )
            return
        target_path = Path(getattr(target, "path", target))

        # Missing paths are an already-completed idempotent cleanup.  Record
        # the receipt, but only release an in-memory mapping if it points at
        # this exact persisted path.
        if not target_path.exists():
            if isinstance(dispatcher, WorktreeDispatcher):
                mapped = dispatcher.worktrees.get(identifier)
                if mapped is not None and Path(getattr(mapped, "path", mapped)).resolve() == target_path.resolve():
                    dispatcher.worktrees.pop(identifier, None)
                    dispatcher.runners.pop(identifier, None)
            self._update_run_receipt(
                wake,
                result,
                cleanup_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                persist_final=False,
            )
            return
        remove = getattr(manager, "remove", None)
        if not callable(remove):
            return
        try:
            value = remove(target_path)
            if inspect.isawaitable(value):
                await value
        except Exception as exc:
            # A retained worktree is untidy, never unsafe.  Surface it in
            # /status rather than failing a successfully merged run.
            self.last_error = f"worktree cleanup for {identifier} failed: {exc}"
            return
        if isinstance(dispatcher, WorktreeDispatcher):
            mapped = dispatcher.worktrees.get(identifier)
            if mapped is not None and Path(getattr(mapped, "path", mapped)).resolve() == target_path.resolve():
                dispatcher.worktrees.pop(identifier, None)
                dispatcher.runners.pop(identifier, None)
        self._update_run_receipt(
            wake,
            result,
            cleanup_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            # The normalized result was already persisted before cleanup.
            # Keep this receipt-only update from replacing the real final
            # artifact during a restart retry's synthetic result.
            persist_final=False,
        )

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

        if not self._in_scope(ticket) or self.db is None or not self._owns(ticket):
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
        if not self._in_scope(ticket):
            return 0
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

        if not self._in_scope(ticket):
            return False
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
        if not self._in_scope(ticket) or processor is None or not facts or self.shadow:
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
            # Keep all observed tickets in the fleet digest, but stop the
            # custody path here.  WakeDecider can enqueue/claim locally, so a
            # foreign ticket must never even enter ``decide``.
            if not self._in_scope(ticket):
                continue
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
        self._background_dispatch = True
        if self.db:
            self.db.set_runtime(self.project, self.lifecycle.value)
        await self._apply_startup_migrations()
        await self._hydrate_retained_builders()
        if initial_poll:
            await self.poll_once()
        await self._reconcile_completed_merges()
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
            if not identifier or not self._in_scope(identifier):
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
        if (
            self.lifecycle == Lifecycle.STOPPED
            and self._task is None
            and not any(not item.done() for item in self._dispatch_tasks.values())
        ):
            return
        self._stop.set()
        self._dispatch_suspended = True
        task, self._task = self._task, None
        stop_heartbeat = getattr(self.heartbeat, "stop", None)
        if callable(stop_heartbeat):
            value = stop_heartbeat()
            if inspect.isawaitable(value):
                await value
        # Terminate retained runner processes before cancelling the poll task:
        # that task may itself be awaiting a long PR-event resume.  Waiting on
        # it first makes `/exit` hang until the model finishes.
        await self._stop_active_dispatches()
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
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
                            "last_activity_at, paused_at, paused_seconds, deadline_at, final_json, "
                            "merge_verified, cleanup_at "
                            "FROM runs ORDER BY COALESCE(started_at, id), id"
                        ).fetchall()
                        parsed_runs: list[dict[str, Any]] = []
                        for row in rows:
                            item = dict(row)
                            raw_final = item.pop("final_json", None)
                            if raw_final:
                                try:
                                    final = json.loads(raw_final)
                                except (TypeError, ValueError):
                                    final = None
                                if isinstance(final, Mapping):
                                    # The final artifact is the durable source
                                    # for the PR/outcome shown in /status and
                                    # First Mate's shared digest.
                                    for key in (
                                        "pr_url",
                                        "outcome",
                                        "merge_commit_sha",
                                        "merge_verified",
                                    ):
                                        if key in final:
                                            item[key] = final[key]
                            parsed_runs.append(item)
                        run_rows = tuple(parsed_runs)
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

    def set_ticket_hold(self, identifier: str, hold: bool) -> bool:
        """Persist the merge hold for one observed ticket.

        A hold is an operational, per-ticket flag.  It must not be implemented
        as ``pause``: pausing suspends the whole supervisor and prevents
        polling, while changing a ticket hold leaves the fleet running.  The
        database is the durable owner of this flag; the in-memory observation
        is updated as well so ``/status`` reflects the command immediately.

        Identifiers are matched case-insensitively for operator convenience,
        but the stored Linear identifier is used for the actual update.
        ``False`` means either that the ticket is not in the current project
        observation or that no durable ledger is available.
        """

        requested = str(identifier or "").strip()
        if not requested or not self._in_scope(requested) or self.db is None:
            return False

        try:
            observed = self.db.tickets(self.project)
        except Exception:
            observed = []
        match = next(
            (
                item
                for item in observed
                if str(item.get("identifier") or "").strip().casefold() == requested.casefold()
            ),
            None,
        )
        if not isinstance(match, Mapping):
            return False
        canonical = str(match.get("identifier") or "").strip()
        if not canonical or not self.db.set_hold(canonical, bool(hold)):
            return False

        # Keep the latest in-memory digest in sync until the next poll.  These
        # are the same mutable ticket dictionaries that status() exposes, and
        # mutating only the local custody field cannot alter Linear data.
        for tickets in (self._fleet_tickets, self.last_result.tickets if self.last_result else ()):
            for ticket in tickets:
                if self._ticket_key(ticket).casefold() == canonical.casefold():
                    ticket["hold"] = bool(hold)
        return True

    # Short alias for embedders that already use the database method name.
    set_hold = set_ticket_hold

    def _ticket_for_run(self, run_id: str) -> str:
        """Resolve a run's ticket for scoped resume authorization."""

        value = str(run_id or "").strip()
        if not value:
            return ""
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is not None:
            try:
                row = connection.execute("SELECT ticket FROM runs WHERE id = ?", (value,)).fetchone()
            except Exception:
                row = None
            if row is not None and row[0]:
                return str(row[0])
        dispatcher = self.dispatcher
        retained = getattr(dispatcher, "runners", None)
        if isinstance(retained, Mapping):
            for identifier, runner in retained.items():
                contexts = getattr(runner, "contexts", {})
                if value in contexts or str(getattr(runner, "default_run_id", "")) == value:
                    return str(identifier)
        direct = self.runner
        if direct is not None:
            contexts = getattr(direct, "contexts", {})
            candidate = getattr(direct, "ticket", None) or getattr(direct, "identifier", None)
            if candidate and (value in contexts or str(getattr(direct, "default_run_id", "")) == value):
                return str(candidate)
        return ""

    async def resume_session(self, run_id: str, prompt: str) -> bool:
        """Resume one persistent builder session, without creating a replacement."""

        if self.ticket_scope:
            identifier = self._ticket_for_run(run_id)
            if not identifier or not self._in_scope(identifier):
                return False
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
        if self.ticket_scope:
            identifier = str(row.get("ticket") or "")
            if not identifier or not self._in_scope(identifier):
                raise ValueError(
                    f"escalation {wanted} is outside invocation scope "
                    f"{self.ticket_scope.selector}"
                )
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
