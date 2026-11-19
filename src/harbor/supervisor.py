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
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
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
    mark_threads_seen,
    observe_pr_facts,
    record_push,
    ticket_scope as normalize_ticket_scope,
)
from harbor.pr_events import PREventDeliveryExhausted, PREventError, handoff_gate_text, has_handoff_gaps

# Terminal runner states that stop a builder until an operator ``/retry``.
_STOPPED_RUN_STATUSES = frozenset({"failed", "terminated", "orphaned"})
_MAX_NOTICE_CHARS = 300
# A handoff whose PR Linear has not attached yet is re-checked on later polls
# and escalated as unverified after this many polls.
_HANDOFF_OBSERVATION_POLLS = 3


# A Linear reply is stored as an escalation answer only when it starts with
# ``Answer:``; the colon is required so "Answer pending, ..." stays chatter.
_LINEAR_ANSWER_RE = re.compile(r"^\s*answer\s*:\s*(?P<answer>\S.*)$", re.IGNORECASE | re.DOTALL)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

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
        # Set by Supervisor: the open handoff gate appended to every resume.
        self.handoff_gate: Callable[[str], str] | None = None
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

    def _with_handoff_gate(self, identifier: str, instruction: str) -> str:
        """Append unreplied PR comments and stale risk to a resume prompt."""

        gate = self.handoff_gate
        if gate is None:
            return instruction
        try:
            text = str(gate(identifier) or "").strip()
        except Exception:
            # The gate is a reminder built from local receipts; a failure to
            # render it must not block delivering the builder's actual wake.
            return instruction
        return f"{instruction}\n\n{text}" if text and instruction else text or instruction

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
            workflow = getattr(self, "workflow", None)
            if workflow:
                from harbor.workflow_store import bind
                workflow = self.workflow = workflow.reload()
                bind(self.db.connection, self.project, identifier, wake.run_id, workflow)
            create = getattr(self.worktree_manager, "create", None) or getattr(self.worktree_manager, "ensure", None)
            if not callable(create):
                raise TypeError("worktree manager must expose create or ensure")
            from harbor.worktree import WorktreeManager
            worktree = create(identifier,retry_setup=True) if workflow and isinstance(self.worktree_manager,WorktreeManager) else create(identifier)
            if inspect.isawaitable(worktree):
                worktree = await worktree
            if workflow:
                from harbor.hooks import execute_event
                from harbor.workflow_store import record
                receipts = await execute_event(workflow,"setup",{"project":self.project,"ticket":identifier,"run_id":wake.run_id},cwd=getattr(worktree,"path",worktree))
                for receipt in receipts:
                    record(self.db.connection,receipt)
                if any(r["blocking"] for r in receipts):
                    raise ValueError("required setup hook failed; worktree retained")
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

    async def hydrate(self, identifier: str, *, recover_terminal: bool = False) -> Any:
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
        recoverable = {"waiting", "parked", "blocked"}
        if recover_terminal:
            recoverable.update({"terminated", "orphaned", "failed"})
        if row is None or row.get("status") not in recoverable:
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
        attached_path = getattr(worktree, "path", worktree)
        if Path(attached_path).expanduser().resolve() != path.resolve():
            return None
        try:
            runner = await self._factory(worktree, wake)
        except Exception:
            # A factory that cannot rebuild a handle is not a supervision
            # failure; the resume simply stays unavailable for this poll.
            return None
        self.worktrees[identifier] = worktree
        self.runners[identifier] = runner
        return runner

    async def fresh_session(self, identifier: str, instruction: str) -> Any:
        """Start a NEW builder session for a stuck ticket in its retained worktree.

        Only First Mate recovery calls this. The logical run id, worktree and
        branch are kept; the old native session id is dropped so the runner
        starts a clean conversation with a hand-off instruction.
        """

        identifier = str(identifier or "").strip()
        if not identifier or not self._in_scope(identifier):
            raise ValueError(f"ticket is outside this supervisor's scope: {identifier or '(empty)'}")
        row = self._persisted_run(identifier)
        if row is None or not row.get("worktree"):
            raise RuntimeError(f"no retained worktree exists for {identifier}")
        live = self.runners.get(identifier)
        terminate = getattr(live, "terminate", None)
        if row.get("status") == "running":
            if not callable(terminate):
                raise RuntimeError(f"{identifier} is running in another process; cannot replace its session")
            value = terminate(reason="first_mate_fresh_session")
            if inspect.isawaitable(value):
                await value
        connection = getattr(self.db, "connection", None)
        if connection is not None:
            with connection:
                connection.execute(
                    "UPDATE runs SET session_id=NULL, status='terminated', terminal_reason='first_mate_fresh_session' WHERE id=?",
                    (row["id"],),
                )
        self.runners.pop(identifier, None)
        runner = await self.hydrate(identifier, recover_terminal=True)
        if runner is None:
            raise RuntimeError(f"retained worktree for {identifier} cannot be attached")
        wake = Wake(
            ticket={"identifier": identifier, "builder_run_id": row["id"], "run_id": row["id"]},
            action="reengage",
            resume=row["id"],
            instruction=instruction,
            reason="first-mate-fresh-session",
        )
        result = await self._runner_call(runner, wake, resume=False)
        await self._record_push_result(wake, result)
        return result

    async def retry(self, identifier: str, instruction: str | None = None) -> Any:
        """Explicitly resume a terminated/orphaned run in its retained session.

        Explicit operators and the bounded recovery scheduler share this
        custody path. The scheduler excludes deliberate stops. This method
        accepts terminated, orphaned, or failed runs, verifies the durable session and
        worktree, replaces any stale build queue receipt with a re-engagement
        receipt, and resumes the same logical run id.
        """

        identifier = str(identifier or "").strip()
        if not identifier or not self._in_scope(identifier):
            raise ValueError(f"ticket is outside this supervisor's scope: {identifier or '(empty)'}")
        row = self._persisted_run(identifier)
        if row is None:
            raise RuntimeError(f"no retained builder exists for {identifier}")
        status = str(row.get("status") or "").casefold()
        if status not in {"terminated", "orphaned", "failed"}:
            raise RuntimeError(
                f"retained builder for {identifier} is {status or 'unknown'}, not recoverable"
            )
        run_id = str(row.get("id") or "").strip()
        session_id = str(row.get("session_id") or "").strip()
        if not run_id or not session_id:
            raise RuntimeError(f"retained builder for {identifier} has no resumable session id")

        # A terminal in-memory runner may retain `terminated=True` on its old
        # context. Rebuild its lightweight handle from the durable session so
        # the retry cannot inherit the prior process's terminal flags.
        self.runners.pop(identifier, None)
        runner = await self.hydrate(identifier, recover_terminal=True)
        if runner is None:
            raise RuntimeError(
                f"retained builder for {identifier} cannot attach its existing worktree"
            )

        retry_key = f"{identifier}@retry@{run_id}"
        retry_instruction = instruction or (
            f"Operator requested recovery of {identifier} after its builder process stopped. "
            "Continue in this same retained session and worktree. Re-read current Linear and "
            "GitHub facts, reconcile any interrupted work, and complete the configured gate."
        )
        has_idem = getattr(self.db, "queue_has_idem", None) if self.db is not None else None
        if callable(has_idem) and has_idem(retry_key, live_only=True):
            raise RuntimeError(f"a retry is already active for {identifier}")

        transition = getattr(self.db, "transition_ticket_queue", None) if self.db is not None else None
        if callable(transition):
            transition(
                identifier,
                "stopped",
                error="superseded by explicit same-session retry",
            )
        enqueue = getattr(self.db, "enqueue", None) if self.db is not None else None
        if callable(enqueue):
            queued = enqueue(
                identifier,
                "reengage",
                retry_key,
                {
                    "instruction": retry_instruction,
                    "ticket": {"identifier": identifier},
                    "run_id": run_id,
                    "session_id": session_id,
                    "reason": "operator-retry",
                },
            )
            if not queued:
                raise RuntimeError(f"a retry is already active for {identifier}")

        wake = Wake(
            ticket={"identifier": identifier, "builder_run_id": run_id, "run_id": run_id},
            action="reengage",
            resume=run_id,
            instruction=retry_instruction,
            idem_key=retry_key,
            reason="operator-retry",
        )
        transition_queue = getattr(self.db, "transition_queue", None) if self.db is not None else None
        try:
            result = await self.resume(wake)
        except Exception:
            if callable(transition_queue):
                transition_queue(
                    "failed",
                    idem_key=retry_key,
                    error="same-session retry raised before starting",
                )
            raise
        if result is None or result is False:
            if callable(transition_queue):
                transition_queue(
                    "failed",
                    idem_key=retry_key,
                    error="same-session retry could not start",
                )
            raise RuntimeError(f"same-session retry could not start for {identifier}")
        return result

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
            workflow = getattr(self, "workflow", None)
            if workflow is not None:
                from harbor.workflow_store import bind
                if not wake.run_id:
                    raise ValueError("workflow resume requires retained run identity")
                # Adopt edits to the project's workflow instead of failing the run.
                workflow = self.workflow = workflow.reload()
                bind(self.db.connection, self.project, identifier, wake.run_id, workflow)
            wake = replace(wake, instruction=self._with_handoff_gate(identifier, wake.instruction))
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
                instruction=self._with_handoff_gate(ticket, instruction),
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


class _ResultRoutingTarget:
    """Deliver a PR event to the retained builder, then route its run result.

    ``PREventProcessor`` records only delivery receipts.  Without this bridge
    a builder that blocked while handling a PR event never reached the ladder.
    """

    def __init__(self, supervisor: "Supervisor", processor: Any, target: Any) -> None:
        self.supervisor = supervisor
        self.processor = processor
        self.target = target

    async def handle_pr_event(self, event: Mapping[str, Any]) -> Any:
        result = await self.processor.deliver(self.target, event)
        await self.supervisor.route_pr_event_result(event, result)
        return result


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
        read_only: bool = False,
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
                shadow=bool(shadow or read_only),
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
            self.dispatcher.handoff_gate = self.handoff_gate_text
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
        # Read-only is a complete delivery boundary, including for embedders
        # that construct Supervisor directly instead of going through the CLI.
        self.shadow = bool(shadow or read_only)
        # A duplicate project start is an observer of the owner, not another
        # runtime.  It may refresh local observations, but it must not replace
        # the owner's durable lifecycle row while it starts or exits.
        self.read_only = bool(read_only)
        self.shadow_wakes: list[Wake] = []
        self.last_pr_facts: tuple[Mapping[str, Any], ...] = ()
        self.unblock_ladder = unblock_ladder
        self.last_unblock: Any | None = None
        from harbor.recovery import BlockerPolicy
        self.blocker_policy = getattr(getattr(self.poller, "config", None), "blocker_policy", None) or BlockerPolicy()
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
        # Process-local keys of operator notifications already sent, so a
        # per-poll retry of the same failure pages the operator once.
        self._operator_notices: set[str] = set()
        # Handoffs reported before Linear attached their PR, keyed by ticket.
        self._pending_handoffs: dict[str, dict[str, Any]] = {}
        # Ledger handoffs (run id + finish time) already checked by this
        # process; a restart re-derives parked handoffs from the runs table.
        self._handoffs_seen: set[str] = set()
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
        self._set_runtime()

    def _set_runtime(self, reason: str | None = None) -> None:
        """Persist lifecycle only when this process owns the project lease."""

        if self.db is not None and not self.read_only:
            self.db.set_runtime(self.project, self.lifecycle.value, reason)

    @property
    def paused(self) -> bool:
        return self.lifecycle == Lifecycle.PAUSED

    @staticmethod
    def _ticket_key(ticket: Mapping[str, Any]) -> str:
        return str(ticket.get("identifier") or ticket.get("id") or "")

    @staticmethod
    def _normalized_pr_url(value: Any) -> str | None:
        if isinstance(value, Mapping):
            value = value.get("url") or value.get("href")
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip().rstrip("/").casefold()

    @classmethod
    def _ticket_pr_urls(cls, ticket: Mapping[str, Any]) -> set[str]:
        values = ticket.get("pr_urls", ticket.get("attachments", ()))
        if isinstance(values, Mapping):
            values = values.get("nodes", ())
        if isinstance(values, (str, bytes, Mapping)):
            values = (values,)
        return {
            normalized
            for value in values or ()
            if (normalized := cls._normalized_pr_url(value)) is not None
        }

    def _authoritative_ticket(self, ticket: Mapping[str, Any]) -> dict[str, Any]:
        """Prefer the latest Linear-observed attachments retained in Harbor."""

        candidate = dict(ticket)
        identifier = self._ticket_key(candidate)
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if not identifier or connection is None:
            return candidate
        try:
            row = connection.execute(
                "SELECT pr_urls_json FROM tickets WHERE identifier = ?", (identifier,)
            ).fetchone()
        except Exception:
            return candidate
        if row is None:
            return candidate
        try:
            urls = json.loads(row[0] or "[]")
        except (TypeError, ValueError):
            urls = []
        candidate["pr_urls"] = urls if isinstance(urls, list) else []
        return candidate

    def _ticket_has_pr_url(self, ticket: Mapping[str, Any], pr_url: Any) -> bool:
        target = self._normalized_pr_url(pr_url)
        if not target:
            return False
        return target in self._ticket_pr_urls(self._authoritative_ticket(ticket))

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
        if target_number and not pr_url:
            for row in rows:
                number = row.get("number", row.get("pr_number"))
                if cls._pr_number(number) == target_number:
                    return row
        if pr_url:
            # A named artifact is an identity boundary.  Do not bind it to a
            # sole row for another PR (or another repository with the same
            # number) merely because no second observation exists.
            return None
        return rows[0] if len(rows) == 1 else None

    @staticmethod
    def _fact_confirms_merge(facts: Mapping[str, Any] | None) -> bool:
        """Accept only a positive read-only PR observation as merge proof."""

        if not isinstance(facts, Mapping):
            return False
        # GitHubPoller emits a bool.  Do not let a string such as ``"false"``
        # become truthy evidence merely because it came from a JSON adapter.
        return facts.get("merged") is True

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
        # may omit it, in which case require their exact URL to match an
        # independently observed Linear attachment.  PR numbers alone are
        # repository-local and cannot establish identity.
        for item in facts:
            if not isinstance(item, Mapping):
                continue
            identifier = str(item.get("ticket") or item.get("ticket_id") or "")
            if not identifier:
                item_url = self._normalized_pr_url(item)
                for ticket in ticket_rows:
                    if item_url and item_url in self._ticket_pr_urls(ticket):
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
                orphaned = await reap_orphan_runs(self.db, ticket_scope=self.ticket_scope)
            else:
                project_tickets = self._project_ticket_ids()
                if project_tickets is None:
                    orphaned = await reap_orphan_runs(self.db)
                else:
                    # A shared ledger may contain another project's live agent.
                    class ProjectScope:
                        def matches(scope, identifier):
                            return identifier in project_tickets
                    orphaned = await reap_orphan_runs(self.db, ticket_scope=ProjectScope())
            await self._notify_orphaned_runs(orphaned)
        self._migrations_applied = True

    def _project_ticket_ids(self) -> set[str] | None:
        """Identifiers this project has observed, or ``None`` when unknown."""

        method = getattr(self.db, "tickets", None) if self.db is not None else None
        if not callable(method):
            return None
        try:
            return {str(item.get("identifier")) for item in method(self.project) if item.get("identifier")}
        except Exception:
            return None

    def _ticket_project(self, identifier: str) -> str | None:
        """The project that last observed a ticket, or ``None`` when unknown."""

        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None or not identifier:
            return None
        try:
            row = connection.execute("SELECT project FROM tickets WHERE identifier = ?", (identifier,)).fetchone()
        except Exception:
            return None
        return str(row[0]) if row and row[0] else None

    async def _notify_operator(self, key: str, message: str) -> bool:
        """Send one operator notification per distinct key in this process."""

        if self.notifier is None or self.shadow or self.read_only or key in self._operator_notices:
            return False
        target = getattr(self.notifier, "notify", None) or getattr(self.notifier, "send", None) or self.notifier
        if not callable(target):
            return False
        text = " ".join(str(message).split())
        if len(text) > _MAX_NOTICE_CHARS:
            text = text[: _MAX_NOTICE_CHARS - 1] + "…"
        try:
            value = target(text)
            if inspect.isawaitable(value):
                await value
        except Exception as exc:
            # Only a delivered notice is deduplicated; a flaky notifier is
            # retried the next time the same event is reported.
            self.last_error = f"operator notification failed: {exc}"
            return False
        self._operator_notices.add(key)
        return True

    async def _notify_orphaned_runs(self, run_ids: Iterable[str] | None) -> None:
        """Tell the operator which of this project's builders a restart stopped."""

        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None or not run_ids:
            return
        project_tickets = self._project_ticket_ids()
        for run_id in run_ids:
            try:
                row = connection.execute("SELECT ticket FROM runs WHERE id = ?", (str(run_id),)).fetchone()
            except Exception:
                continue
            identifier = str(row[0]) if row and row[0] else ""
            if not identifier or not self._in_scope(identifier):
                continue
            if project_tickets is not None and identifier not in project_tickets:
                continue
            from harbor.recovery import recoverable
            if self.blocker_policy.retry_delays and recoverable(self._latest_run(identifier) or {}):
                continue
            await self._notify_operator(
                f"orphaned:{run_id}",
                f"{identifier} builder orphaned: Harbor restarted while it was running. "
                f"Run /retry {identifier} to resume the same session.",
            )

    async def _refresh_ticket_pr_links(self, identifier: str) -> bool:
        """Re-read one ticket's PR attachments and state from Linear.

        A ticket that went Done before Harbor observed its PR attachment
        leaves the poll scope with ``pr_urls = []``; its merged worktree would
        then never be cleaned up. Linear stays the authority for which PR
        belongs to the ticket, so refresh from it instead of trusting the
        builder's claim.
        """

        fetch = None
        for linear in (getattr(self.unblock_ladder, "linear", None), getattr(self.poller, "client", None),
                       getattr(self, "linear", None)):
            fetch = getattr(linear, "fetch_issue_status", None)
            if callable(fetch):
                break
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if not callable(fetch) or connection is None or not identifier:
            return False
        try:
            issue = await fetch(identifier)
        except Exception as exc:
            self.last_error = f"PR link refresh for {identifier}: {exc}"
            return False
        nodes = (issue.get("attachments") or {}).get("nodes") or []
        urls = [str(n["url"]) for n in nodes if n.get("url") and str(n.get("sourceType", "")).lower() == "github"]
        state = issue.get("state") or {}
        with connection:
            connection.execute(
                "UPDATE tickets SET pr_urls_json=?, state=COALESCE(?, state), state_type=COALESCE(?, state_type) WHERE identifier=?",
                (json.dumps(urls), state.get("name"), state.get("type"), identifier),
            )
        return True

    async def _reconcile_completed_merges(self, only: str | None = None, *, refresh: bool = False) -> None:
        """Recover merge receipts that arrived just after a builder exited.

        A successful skill can merge a PR and return before Harbor's next
        normal poll.  If Linear has already moved to ``Done``, that ticket is
        outside the configured poll states and would otherwise strand the
        completed run and its worktree forever.  Reconciliation is scoped to
        persisted ``completed/outcome=merged`` rows and queries only the PR
        URL already present in the final artifact and independently retained
        from the Linear ticket attachment.
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
            if not self._in_scope(identifier) or (only is not None and identifier != only):
                continue
            try:
                payload = json.loads(row[3]) if row[3] else {}
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, Mapping):
                continue
            pr_url = payload.get("pr_url") or payload.get("pull_request_url")
            if not isinstance(pr_url, str) or not pr_url.strip():
                continue
            ticket = self._authoritative_ticket({"identifier": identifier})
            if not self._ticket_has_pr_url(ticket, pr_url) and refresh:
                if await self._refresh_ticket_pr_links(identifier):
                    ticket = self._authoritative_ticket({"identifier": identifier})
            if not self._ticket_has_pr_url(ticket, pr_url):
                continue
            # A merge receipt can be durable even when cleanup failed (for
            # example, a transient filesystem error).  Retry cleanup from
            # that Harbor receipt alone, but keep it bound to the exact PR URL
            # stored when the positive observation was recorded.
            if row[4] == 1 and row[5] is None:
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
                    "pr_url": pr_url,
                }
                await self._cleanup_merged_worktree(wake, result)
                continue
            if str(payload.get("outcome", "")).casefold() != "merged":
                continue
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
            if wake.reason == "linear-comment-version":
                comment = wake.facts.get("linear_comment", {})
                if self.blocker_policy.authorized_authors and _LINEAR_ANSWER_RE.match(str(comment.get("body") or "").strip()):
                    author = comment.get("author") or {}
                    if not isinstance(author, Mapping) or str(author.get("id")) not in self.blocker_policy.authorized_authors:
                        self.last_error = "decision reply refused: unauthorized or missing Linear author"
                        transition = getattr(self.db, "transition_queue", None)
                        if callable(transition) and wake.idem_key:
                            transition("failed", idem_key=wake.idem_key, error=self.last_error)
                        return
                self._record_linear_reply(wake)
            result = await self._invoke(target, ("resume", "reengage", "continue_run", "continue"), wake)
            if result is None or result is False:
                raise RuntimeError("retained builder refused re-engagement delivery")
            threads = wake.facts.get("threads") if isinstance(wake.facts, Mapping) else None
            head_sha = wake.facts.get("head_sha") if isinstance(wake.facts, Mapping) else None
            if (
                isinstance(threads, Iterable)
                and not isinstance(threads, (str, bytes, Mapping))
            ):
                mark_threads_seen(self.db, threads, str(head_sha) if head_sha else None)
            await self._handle_runner_result(wake, result)
        elif wake.action == "stop" and wake.notify:
            try:
                await self._invoke(self.notifier, ("notify", "send", "publish"), wake)
            except Exception as exc:
                self.last_error = f"operator notification failed: {exc}"

    def _record_linear_reply(self, wake: Wake) -> None:
        """Save an explicit Linear ``Answer:`` reply to the ticket's open escalations.

        Every human comment resumes the builder as an instruction.  Only a
        comment that starts with ``Answer:`` and was created after the
        escalation opened marks it answered and, for ladder questions, stores
        its reusable resolution, cited to that comment.  It may replace an
        earlier Linear answer but never an operator's console answer.
        Teammate chatter such as "looking into it" never becomes authority.
        """

        comment = wake.facts.get("linear_comment") if isinstance(wake.facts, Mapping) else None
        body = str(comment.get("body") or "").strip()[:4000] if isinstance(comment, Mapping) else ""
        if self.blocker_policy.authorized_authors:
            author = comment.get("author", {}) if isinstance(comment, Mapping) else {}
            if not isinstance(author, Mapping) or str(author.get("id")) not in self.blocker_policy.authorized_authors:
                return
        match = _LINEAR_ANSWER_RE.match(body)
        identifier = self._wake_ticket_identifier(wake)
        finder = getattr(self.db, "open_escalations", None) if self.db is not None else None
        if match is None or not identifier or not callable(finder):
            return
        # Creation time, not edit time: an old comment edited later still
        # predates the question it would otherwise claim to answer.
        created = _parse_time(comment.get("created_at"))
        if created is None:
            return
        answer = match.group("answer").strip()
        record = getattr(self.db, "record_resolution", None)
        update = getattr(self.db, "update_escalation", None)
        comment_id = str(comment.get("id") or "").strip()
        citation = f"linear:{identifier}" + (f"#comment-{comment_id}" if comment_id else "")
        answerable = [
            row
            for row in finder([identifier])
            if (opened := _parse_time(row.get("created_at"))) is not None and created > opened
        ]
        # Legacy rows can leave several inferred-open questions on one ticket;
        # a reply answers only the newest one it could have been written for.
        for row in sorted(answerable, key=lambda item: int(item.get("id") or 0))[-1:]:
            resolution: Mapping[str, Any] | None = None
            # A handoff gate is PR state, not a reusable policy question.
            if row.get("kind") != "handoff_gate" and callable(record):
                finder_resolution = getattr(self.db, "find_resolution", None)
                existing = finder_resolution(str(row.get("kind")), str(row.get("question")), project=self.project) if callable(finder_resolution) and hasattr(self.db, "connection") else None
                replace = not (
                    isinstance(existing, Mapping)
                    and not str(existing.get("citation") or "").startswith("linear:")
                )
                try:
                    resolution = record(
                        str(row.get("kind")), str(row.get("question")), answer, citation,
                        source_ticket=identifier, replace=replace,
                    )
                except (TypeError, ValueError):
                    resolution = None
            if callable(update):
                resolution_id = resolution.get("id") if isinstance(resolution, Mapping) else None
                update(row["id"], status="answered", resolution_id=resolution_id)

    async def _deliver_scheduled_wake(self, wake: Wake) -> None:
        """Background delivery that tells the operator when a builder cannot run."""

        try:
            await self._deliver_wake(wake)
        except Exception as exc:
            await self._notify_dispatch_failure(wake, str(exc))
            raise

    async def _notify_dispatch_failure(self, wake: Wake, error: str) -> None:
        identifier = self._wake_ticket_identifier(wake)
        if not identifier or self._stop.is_set():
            return
        if wake.action != "reengage":
            await self._notify_operator(
                f"dispatch:{identifier}:{wake.action}",
                f"Harbor could not start the builder for {identifier}: {error}",
            )
            return
        # A refused resume is usually a benign race with a live handoff and is
        # retried on the next poll.  Only a stopped builder needs the operator.
        run = self._latest_run(identifier) or {}
        status = str(run.get("status") or "").casefold()
        if status in _STOPPED_RUN_STATUSES:
            from harbor.recovery import recoverable
            if self.blocker_policy.retry_delays and recoverable(run):
                return
            await self._notify_operator(
                f"stopped-reengage:{identifier}:{run.get('id')}",
                f"{identifier} has new work but its builder is {status}. "
                f"Run /retry {identifier} to resume the same session.",
            )

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
        task = asyncio.create_task(self._deliver_scheduled_wake(wake), name=f"harbor-dispatch-{key}")
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
        claim names a PR that Linear independently attached to the ticket,
        query that exact attachment once and use the returned PR facts as the
        merge receipt. Cached facts remain useful when a restart occurs while
        GitHub is temporarily unavailable.
        """

        if not self._in_scope(wake.ticket):
            return None
        if str(result.get("status", "")).casefold() != "completed":
            return None
        pr_url = result.get("pr_url") or result.get("pull_request_url")
        candidate = self._authoritative_ticket(wake.ticket)
        if not self._ticket_has_pr_url(candidate, pr_url):
            return None

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

        trusted_run_id = str(wake.run_id or "").strip()
        claimed_run_id = str(result.get("run_id") or "").strip()
        if trusted_run_id:
            if claimed_run_id and claimed_run_id != trusted_run_id:
                return ""
            return trusted_run_id
        identifier = self._ticket_key(wake.ticket)
        dispatcher = self.dispatcher
        if claimed_run_id and isinstance(dispatcher, WorktreeDispatcher):
            expected = dispatcher._stable_run_id(wake)
            if claimed_run_id == expected and identifier in dispatcher.runners:
                return claimed_run_id
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None or not identifier:
            return ""
        if claimed_run_id:
            try:
                claimed = connection.execute(
                    "SELECT ticket, role FROM runs WHERE id = ? LIMIT 1", (claimed_run_id,)
                ).fetchone()
            except Exception:
                return ""
            if (
                claimed
                and str(claimed[0]) == identifier
                and str(claimed[1]).casefold() == "builder"
            ):
                return claimed_run_id
            return ""
        try:
            rows = connection.execute(
                "SELECT id FROM runs WHERE ticket = ? AND role = 'builder' "
                "ORDER BY COALESCE(started_at, id), id",
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
        pr_url = result.get("pr_url") or result.get("pull_request_url")
        target = str(pr_url).rstrip("/") if pr_url else ""
        return (
            f"{identifier}\x00{run_id}\x00{target}"
            if identifier and run_id and target
            else ""
        )

    def _run_merge_receipt_is_authorized(self, wake: Wake, result: Mapping[str, Any]) -> bool:
        """Require an observed receipt, including for direct cleanup calls."""

        if wake.run_id and result.get("run_id") and not self._run_id_for_result(wake, result):
            return False
        # A deterministic wake may carry the exact PR observation that caused
        # this delivery.  This is the only in-memory authority available to
        # DB-less embedders/tests; builder result fields are intentionally not
        # consulted.
        pr_url = result.get("pr_url") or result.get("pull_request_url")
        if not self._ticket_has_pr_url(wake.ticket, pr_url):
            return False
        observed = self._matching_pr_fact(
            wake.facts if isinstance(wake.facts, Mapping) else None,
            str(pr_url) if pr_url else None,
        )
        if pr_url and self._fact_confirms_merge(observed):
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
                "SELECT ticket, merge_verified, final_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        except Exception:
            return False
        if not row or str(row[0]) != identifier or row[1] != 1 or not pr_url:
            return False
        try:
            stored = json.loads(row[2] or "{}")
        except (TypeError, ValueError):
            return False
        stored_url = (
            stored.get("pr_url") or stored.get("pull_request_url")
            if isinstance(stored, Mapping)
            else None
        )
        return bool(stored_url and str(stored_url).rstrip("/") == str(pr_url).rstrip("/"))

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
        identifier = self._ticket_key(wake.ticket)
        if connection is None or not run_id or not identifier:
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
                    f"UPDATE runs SET {', '.join(f'{key} = ?' for key in updates)} "
                    "WHERE id = ? AND ticket = ?",
                    tuple(updates.values()) + (run_id, identifier),
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

    async def _report_result(self, wake: Wake, result: Mapping[str, Any]) -> None:
        """Enforce the handoff gate, close finished escalations, and page stops.

        Blocked finals belong to the unblock ladder, which pages on its own.  An
        ordinary waiting or completed turn never closes an open escalation;
        only an answer, a newer ladder result, a clean handoff, or a verified
        merge does.
        """

        if self.shadow or self.read_only:
            return
        identifier = self._ticket_key(wake.ticket)
        if not identifier:
            return
        try:
            if wake.run_id and result.get("run_id") and not self._run_id_for_result(wake, result):
                # A result claiming another run must not change this ticket.
                return
            # A newer result supersedes any handoff still waiting to be checked.
            self._pending_handoffs.pop(identifier, None)
            status = str(result.get("status") or "").casefold()
            outcome = str(result.get("outcome") or "").casefold()
            waiting_on = str(result.get("waiting_on") or "").casefold()
            pr_url = str(result.get("pr_url") or "").strip()
            if status == "blocked" or isinstance(result.get("blocker"), Mapping):
                return
            if status in {"waiting", "completed"} and result.get("summary"):
                connection = getattr(self.db, "connection", None)
                if connection is not None:
                    with connection:
                        connection.execute("UPDATE escalations SET status='resolved' WHERE ticket=? AND run_id=? AND status='resume_pending'", (identifier, self._run_id_for_result(wake, result)))
            handoff = outcome == "merged" or (outcome == "parked" and waiting_on == "human")
            if handoff and pr_url and getattr(self.dispatcher,"workflow",None):
                from harbor.workflow_lifecycle import handoff as check_handoff
                try:
                    await check_handoff(self,wake,result)
                except Exception as exc:
                    await self._escalate_handoff(wake,result,outcome,{"pr_url":pr_url,"risk":"unverified","unreplied":[],"summary":str(exc)})
                    return
                await self._complete_handoff(wake,result,{"risk":"valid","unreplied":[]})
            elif handoff and pr_url and self._handoff_gate_enabled():
                ledger_key = self._ledger_handoff(identifier)
                if ledger_key is not None:
                    self._handoffs_seen.add(ledger_key[0])
                gaps = await self._handoff_gaps(wake.ticket, pr_url, refresh=True)
                if gaps is None:
                    # Linear has not attached this PR yet, so nothing about it
                    # is verified.  Re-check on later polls instead of paging.
                    self._pending_handoffs[identifier] = {"wake": wake, "result": dict(result), "polls": 0}
                    return
                await self._complete_handoff(wake, result, gaps)
            elif outcome == "parked" and waiting_on == "human":
                await self._notify_park(identifier, result)
            elif status in _STOPPED_RUN_STATUSES and not self._stop.is_set():
                from harbor.recovery import recoverable
                if self.blocker_policy.retry_delays and recoverable(self._latest_run(identifier) or {}):
                    return  # The next healthy poll schedules bounded recovery.
                run_id = self._run_id_for_result(wake, result) or str(result.get("run_id") or "")
                summary = str(result.get("error") or result.get("summary") or status)
                await self._notify_operator(
                    f"{status}:{identifier}:{run_id}:{summary}",
                    f"{identifier} builder {status}: {summary}. Run /retry {identifier} to resume the same session.",
                )
        except Exception as exc:
            # Reporting is operator convenience; it must never take down the
            # result router that owns cleanup and the unblock ladder.
            self.last_error = f"result report for {identifier} failed: {exc}"

    def _handoff_gate_enabled(self) -> bool:
        return callable(getattr(self.pr_event_processor, "handoff_gaps", None))

    async def _complete_handoff(self, wake: Wake, result: Mapping[str, Any], gaps: Mapping[str, Any]) -> None:
        """Escalate a handoff with gaps, or close its gate and page a clean park."""

        identifier = self._ticket_key(wake.ticket)
        outcome = str(result.get("outcome") or "").casefold()
        if has_handoff_gaps(gaps):
            await self._escalate_handoff(wake, result, outcome, gaps)
            return
        close = getattr(self.db, "close_open_escalations", None) if self.db is not None else None
        if callable(close):
            if outcome == "merged" and result.get("merge_verified") is True:
                close(identifier)
            else:
                close(identifier, kinds=("handoff_gate",))
        if outcome == "parked" and str(result.get("waiting_on") or "").casefold() == "human":
            await self._notify_park(identifier, result)

    async def _notify_park(self, identifier: str, result: Mapping[str, Any]) -> None:
        pr_url = str(result.get("pr_url") or "").strip()
        summary = str(result.get("summary") or "")
        detail = (
            f"is merge-ready and waiting on you: {pr_url}"
            if pr_url
            else f"is parked and waiting on you: {summary or 'no PR reported'}"
        )
        await self._notify_operator(f"parked:{identifier}:{pr_url}:{summary}", f"{identifier} {detail}")

    def _ledger_handoff(self, identifier: str) -> tuple[str, dict[str, Any], str] | None:
        """The ticket's latest run if it is a handoff: ``(key, final, run id)``."""

        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if connection is None or not identifier:
            return None
        try:
            row = connection.execute(
                "SELECT id, status, final_json, finished_at FROM runs WHERE ticket = ? "
                "ORDER BY COALESCE(finished_at, started_at, id) DESC, id DESC LIMIT 1",
                (identifier,),
            ).fetchone()
            final = json.loads(row[2]) if row is not None and row[2] else None
        except Exception:
            return None
        if row is None or not isinstance(final, dict):
            return None
        status = str(row[1] or "").casefold()
        outcome = str(final.get("outcome") or "").casefold()
        parked = outcome == "parked" and str(final.get("waiting_on") or "").casefold() == "human"
        handoff = (status == "waiting" and parked) or (status == "completed" and outcome == "merged")
        if not handoff or not str(final.get("pr_url") or "").strip():
            return None
        return f"{row[0]}@{row[3] or ''}", final, str(row[0])

    async def _retry_pending_handoffs(self) -> None:
        """Check handoffs whose PR was not attached when reported, or parked before a restart."""

        for ticket in self._fleet_tickets:
            identifier = self._ticket_key(ticket)
            if identifier in self._pending_handoffs or not self._in_scope(identifier) or not self._owns(ticket):
                continue
            ledger = self._ledger_handoff(identifier)
            if ledger is None or ledger[0] in self._handoffs_seen or ledger[1].get("outcome") != "parked":
                continue
            # A park recorded before this process started (or one whose report
            # was lost) still needs its handoff gate and merge-ready page.
            self._handoffs_seen.add(ledger[0])
            self._pending_handoffs[identifier] = {
                "wake": Wake(ticket={"identifier": identifier}, action="reengage", resume=ledger[2]),
                "result": ledger[1],
                "polls": 0,
            }
        for identifier, pending in list(self._pending_handoffs.items()):
            if not self._in_scope(identifier):
                continue
            wake, result = pending["wake"], pending["result"]
            pr_url = str(result.get("pr_url") or "")
            ledger = self._ledger_handoff(identifier)
            if self._latest_run(identifier) is not None and (
                ledger is None or str(ledger[1].get("pr_url") or "") != pr_url
            ):
                # The builder has moved on (blocked, resumed, or a new PR);
                # a stale handoff must not supersede its newer escalation.
                self._pending_handoffs.pop(identifier, None)
                continue
            try:
                if getattr(self.dispatcher,"workflow",None):
                    await self._report_result(wake,result)
                    self._pending_handoffs.pop(identifier,None)
                    continue
                gaps = await self._handoff_gaps(wake.ticket, pr_url)
                if gaps is None:
                    pending["polls"] += 1
                    if pending["polls"] < _HANDOFF_OBSERVATION_POLLS:
                        continue
                    gaps = {
                        "pr_url": pr_url,
                        "risk": "unverified",
                        "unreplied": [],
                        "summary": (
                            f"no Harbor observation of the PR after {pending['polls']} polls "
                            "(is it attached to the Linear ticket?)"
                        ),
                    }
                self._pending_handoffs.pop(identifier, None)
                await self._complete_handoff(wake, result, gaps)
            except Exception as exc:
                self.last_error = f"handoff check for {identifier} failed: {exc}"

    def _linear_ticket(self, identifier: str) -> dict[str, Any]:
        """The ticket fields Linear writes need, from Harbor's last observation."""

        ticket: dict[str, Any] = {"identifier": identifier}
        method = getattr(self.db, "tickets", None) if self.db is not None else None
        if callable(method):
            try:
                row = next((item for item in method(self.project) if item.get("identifier") == identifier), None)
            except Exception:
                row = None
            if row and row.get("linear_id"):
                ticket["linear_id"] = row["linear_id"]
        return self._authoritative_ticket(ticket)

    async def _handoff_gaps(
        self, ticket: Mapping[str, Any], pr_url: str, *, refresh: bool = False
    ) -> Mapping[str, Any] | None:
        """Observe one handed-off PR and report its unreplied comments and risk."""

        processor = self.pr_event_processor
        finder = getattr(processor, "handoff_gaps", None)
        identifier = self._ticket_key(ticket)
        ticket = self._authoritative_ticket(ticket)
        if not callable(finder) or not self._ticket_has_pr_url(ticket, pr_url):
            return None
        head: str | None = None
        comment_ids: set[str] | None = None
        if refresh and self.github_poller is not None:
            # A builder can reply and merge inside one run.  Read the PR once
            # more so a reply marker posted seconds ago is not reported open.
            try:
                rows = await self._call_github((ticket,))
            except Exception:
                rows = []
            grouped = self._github_facts_by_ticket((ticket,), rows).get(identifier, {})
            match = self._matching_pr_fact(grouped, pr_url)
            if match is not None:
                observe = getattr(processor, "observe", None)
                if callable(observe):
                    observe(ticket, match)
                head = str(match.get("head_sha") or "") or None
                comment_ids = self._comment_ids(match)
        return finder(identifier, pr_url, head_sha=head, comment_ids=comment_ids)

    async def _escalate_handoff(
        self, wake: Wake, result: Mapping[str, Any], outcome: str, gaps: Mapping[str, Any]
    ) -> None:
        identifier = self._ticket_key(wake.ticket)
        verb = "merged" if outcome == "merged" else "parked for human review"
        question = f"{identifier} {verb} {gaps.get('pr_url')} with {gaps.get('summary')}."
        reason = "every external PR comment needs an observed reply and a risk analysis at the current head before handoff"
        method = getattr(self.unblock_ladder, "escalate_handoff", None) if self.unblock_ladder is not None else None
        if not callable(method):
            await self._notify_operator(f"handoff:{identifier}:{question}", question)
            return
        run = {"id": self._run_id_for_result(wake, result)}
        value = method(self._linear_ticket(identifier), run, question=question, reason=reason)
        self.last_unblock = (await value if inspect.isawaitable(value) else value) or self.last_unblock

    def handoff_gate_text(self, identifier: str) -> str:
        """Render open handoff gaps for every unmerged PR attached to a ticket."""

        finder = getattr(self.pr_event_processor, "handoff_gaps", None)
        if not callable(finder) or not identifier:
            return ""
        ticket = self._authoritative_ticket({"identifier": identifier})
        cached = self.observed_pr_facts(identifier)
        gaps: list[Mapping[str, Any]] = []
        urls = {
            str(value).strip().rstrip("/")
            for value in ticket.get("pr_urls") or ()
            if isinstance(value, str) and value.strip()
        }
        for url in sorted(urls):
            row = self._matching_pr_fact(cached, url)
            if row is not None and self._fact_confirms_merge(row):
                continue
            gap = finder(identifier, url, head_sha=(row or {}).get("head_sha"), comment_ids=self._comment_ids(row))
            if has_handoff_gaps(gap):
                gaps.append(gap)
        return handoff_gate_text(gaps)

    def _ticket_handoff(self, ticket: Mapping[str, Any], facts: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """Open handoff gaps for this poll's unmerged PRs, for /status."""

        finder = getattr(self.pr_event_processor, "handoff_gaps", None)
        if not callable(finder):
            return []
        identifier = self._ticket_key(ticket)
        attached = self._ticket_pr_urls(self._authoritative_ticket(ticket))
        gaps: list[Mapping[str, Any]] = []
        try:
            for row in self._pr_rows(facts):
                url = row.get("url")
                if not url or self._normalized_pr_url(row) not in attached or self._fact_confirms_merge(row):
                    continue
                gap = finder(identifier, str(url), head_sha=row.get("head_sha"), comment_ids=self._comment_ids(row))
                if has_handoff_gaps(gap):
                    gaps.append(gap)
        except Exception as exc:
            self.last_error = f"handoff status for {identifier} failed: {exc}"
        return gaps

    @staticmethod
    def _comment_ids(row: Mapping[str, Any] | None) -> set[str] | None:
        comments = row.get("comments") if isinstance(row, Mapping) else None
        if not isinstance(comments, list):
            return None
        return {str(item.get("id")) for item in comments if isinstance(item, Mapping)}

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
                await self._report_result(wake, result)
                await self._cleanup_merged_worktree(wake, result)
            return
        if not isinstance(result, Mapping):
            return
        if not isinstance(self.dispatcher, WorktreeDispatcher):
            self._record_observed_push(wake, result)
        await self._report_result(wake, result)
        await self._cleanup_merged_worktree(wake, result)
        status = str(result.get("status", "")).casefold()
        if status != "blocked" and not isinstance(result.get("blocker"), Mapping):
            return
        run_id = self._run_id_for_result(wake, result)
        if wake.run_id and result.get("run_id") and not run_id:
            self.last_error = "runner result run_id does not match retained wake run_id"
            return
        run = {"id": run_id}
        connection = getattr(self.db, "connection", None)
        if connection is not None and run_id:
            try:
                row = connection.execute(
                    "SELECT * FROM runs WHERE id=? AND ticket=?", (run_id, self._ticket_key(wake.ticket)),
                ).fetchone()
            except Exception:
                row = None  # Legacy embedded ledgers may not have run custody.
            if row is not None:
                observed_run = dict(row)
                run.update({key: observed_run.get(key) for key in ("session_id", "worktree", "terminal_reason", "deadline_seconds")})
        if isinstance(result.get("worktree"), str):
            run["worktree"] = result["worktree"]
        elif run_id and self.db is not None:
            # Runner finals intentionally contain only result metadata.  The
            # retained worktree is durable run state, so recover it by the
            # exact run id before resolving repository-local authority.
            connection = getattr(self.db, "connection", None)
            try:
                row = connection.execute(
                    "SELECT worktree FROM runs WHERE id = ?", (run_id,)
                ).fetchone() if connection is not None else None
            except Exception:
                row = None
            if row is not None and row[0]:
                run["worktree"] = str(row[0])
        try:
            value = self.unblock_ladder.handle(
                {**self._authoritative_ticket(wake.ticket), "project": self.project},
                run,
                result,
                blocker=result.get("blocker") if isinstance(result.get("blocker"), Mapping) else None,
            )
            self.last_unblock = await value if inspect.isawaitable(value) else value
            connection = getattr(self.db, "connection", None)
            if connection is not None and run_id:
                from harbor.recovery import quota_schema
                with connection:
                    quota_schema(connection)
                    connection.execute("DELETE FROM quota_recovery WHERE run_id=?", ("triage:" + run_id,))
        except Exception as exc:
            from harbor.first_mate import FirstMateError, FirstMateQuotaError
            from harbor.recovery import defer_quota, recovery_schema
            import time
            # Only a failed model invocation gets an automatic triage retry;
            # explicit task cancellation is a BaseException and bypasses this.
            connection = getattr(self.db, "connection", None)
            if isinstance(exc, FirstMateQuotaError) and connection is not None and run_id:
                defer_quota(connection, "triage:" + run_id, exc.provider, exc.reset, time.time())
            elif isinstance(exc, FirstMateError) and connection is not None and run_id and self.blocker_policy.retry_delays:
                recovery_schema(connection)
                with connection:
                    connection.execute("INSERT OR IGNORE INTO recovery_receipts(run_id,next_retry,last_reason) VALUES (?,?,?)", ("triage:" + run_id, time.time() + self.blocker_policy.retry_delays[0], "First Mate invocation failed"))
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
            from harbor.workflow_lifecycle import cleanup_allowed
            if not await cleanup_allowed(self,wake,result,target_path,run_id):
                return
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
        pr_url = result.get("pr_url") or result.get("pull_request_url")
        if number is None and isinstance(wake.facts, Mapping):
            number = wake.facts.get("pr_number") or wake.facts.get("number")
        if pr_url is None and isinstance(wake.facts, Mapping):
            pr_url = wake.facts.get("url") or wake.facts.get("pr_url")
        try:
            self.record_builder_push(
                wake.ticket,
                str(head),
                pr_number=int(number) if number else None,
                pr_url=str(pr_url) if pr_url else None,
            )
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
            pr_url = row.get("url") or row.get("pr_url")
            head = str(row.get("head_sha") or row.get("headSha") or row.get("headRefOid") or "").strip()
            if number is None or not pr_url or not head:
                continue
            try:
                stored = connection.execute(
                    "SELECT head_sha FROM prs WHERE ticket = ? AND url = ?",
                    (identifier, str(pr_url)),
                ).fetchone()
            except Exception:
                continue
            previous = str(stored[0]).strip() if stored and stored[0] else ""
            if not previous or previous == head:
                # Either the first observation of this PR, or nothing moved.
                continue
            try:
                self.record_builder_push(
                    identifier,
                    head,
                    pr_number=int(number),
                    pr_url=str(pr_url),
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                self.last_error = f"could not record rework for {identifier}: {exc}"

    def record_builder_push(
        self,
        ticket: str | Mapping[str, Any],
        head_sha: str,
        *,
        pr_number: int | None = None,
        pr_url: str | None = None,
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
        return record_push(
            self.db,
            ticket,
            head_sha,
            pr_number=pr_number,
            pr_url=pr_url,
        )

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
            cursor = connection.execute(
                "SELECT * FROM runs WHERE ticket = ? ORDER BY COALESCE(finished_at, started_at, id) DESC, id DESC LIMIT 1",
                (identifier,),
            )
            row = cursor.fetchone()
            return dict(zip((column[0] for column in cursor.description), row)) if row is not None else None
        except Exception:
            return None

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
        if hasattr(processor, "retry_delays"):
            # Failed PR-event handoffs share the project's recovery budget.
            processor.retry_delays = tuple(self.blocker_policy.retry_delays)
        identifier = self._ticket_key(ticket)
        run = self._latest_run(identifier)
        authoritative_ticket = self._authoritative_ticket(ticket)
        attached_urls = self._ticket_pr_urls(authoritative_ticket)
        target = self.dispatcher or self.runner
        if target is not None and callable(getattr(processor, "deliver", None)):
            # The builder's reply to a PR event is a full run result.  Route it
            # like any other wake so a blocker raised while answering a PR
            # comment or running the risk turn reaches the unblock ladder.
            target = _ResultRoutingTarget(self, processor, target)
        for row in self._pr_rows(facts):
            row_url = self._normalized_pr_url(row)
            if not row_url or row_url not in attached_urls:
                continue
            # ``GitHubPoller`` already performs a read-only observe while
            # constructing facts.  ``process`` is intentionally called again
            # so pending receipts survive a transient builder/contract error.
            try:
                signature = inspect.signature(process)
            except (TypeError, ValueError):
                signature = None
            if signature is None:
                value = process(authoritative_ticket, row, run=run, builder=target)
            else:
                parameters = signature.parameters
                kwargs: dict[str, Any] = {}
                if "run" in parameters:
                    kwargs["run"] = run
                if "builder" in parameters:
                    kwargs["builder"] = target
                if "comment_handler" in parameters and target is not None:
                    kwargs["comment_handler"] = target
                value = process(authoritative_ticket, row, **kwargs)
            if inspect.isawaitable(value):
                await value

    async def route_pr_event_result(self, event: Mapping[str, Any], result: Any) -> None:
        """Send one PR-event builder result through the normal result router."""

        if result is None or result is False:
            return
        identifier = str(event.get("ticket") or "")
        run_id = str(event.get("run_id") or "")
        wake = Wake(
            ticket={"identifier": identifier, "builder_run_id": run_id, "run_id": run_id},
            action="reengage",
            resume=run_id or None,
            reason=f"pr-event:{event.get('event_type') or event.get('type') or ''}",
        )
        await self._handle_runner_result(wake, result)

    async def poll_once(self) -> PollResult | None:
        if self.lifecycle in {Lifecycle.PAUSING, Lifecycle.PAUSED}:
            return self.last_result
        # A builder may merge and move Linear to Done before GitHub's read
        # model reports the merge to Harbor.  Done tickets are intentionally
        # absent from the normal Linear poll, so retry exact-PR reconciliation
        # on every live cycle instead of requiring a supervisor restart.
        if not self.shadow:
            await self._reconcile_completed_merges()
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
                except PREventDeliveryExhausted as exc:
                    # The event stays parked until an explicit /retry re-arms it.
                    self.last_error = str(exc)
                    ticket_id = exc.ticket or self._ticket_key(ticket)
                    await self._notify_operator(
                        f"pr-event-exhausted:{exc.event_key}",
                        f"{ticket_id}: builder failed {exc.attempts} times handling a PR event; "
                        f"inspect the retained session, then /retry {ticket_id}.",
                    )
                except Exception as exc:
                    # Event receipts are written before delivery.  A missing
                    # retained builder or malformed response therefore stays
                    # pending for a later poll, while a transient review
                    # problem cannot take down Linear/GitHub supervision.
                    self.last_error = str(exc)
                if self._owns(ticket):
                    handoff = self._ticket_handoff(ticket, facts)
                    if handoff:
                        ticket["handoff"] = handoff
            pending_run = self._latest_run(identifier) or {}
            quota_wait = pending_run.get("terminal_reason") == "usage_limit_exceeded"
            wake = self.decider.decide(ticket, facts) if self.decider is not None and not quota_wait else None
            if wake is not None:
                await self._emit_wake(wake)
        self._fleet_tickets = tuple(fleet_tickets)
        if not self.shadow:
            await self._retry_pending_handoffs()
            from harbor.answer_queue import drain
            drain(self)
            await self._retry_stopped_builders()
            await self._monitor_blocked()
        if self.on_update:
            update = self.on_update(result)
            if asyncio.iscoroutine(update):
                await update
        return result

    @staticmethod
    def _quota_wait_text(connection: Any, run_id: str) -> str:
        """Describe a stored usage-limit wait, e.g. ``codex usage limit; resets ... (confirmed)``."""

        try:
            row = connection.execute(
                "SELECT provider,next_retry,confirmed FROM quota_recovery WHERE run_id=?", (run_id,)
            ).fetchone()
        except sqlite3.Error:
            row = None
        if not row:
            return "provider usage limit reached; reset time unknown"
        when = datetime.fromtimestamp(float(row[1]), timezone.utc).isoformat(timespec="minutes")
        return f"{row[0]} usage limit; resets {when} ({'confirmed' if row[2] else 'estimated'})"

    async def _retry_stopped_builders(self) -> None:
        from harbor.recovery import (
            claim_quota_retry, claim_retry, defer_quota, interrupted_retry_instruction, quota_schema, recoverable,
            recovery_instruction, retained_budget, recovery_schema,
        )
        from harbor.runner.quota import USAGE_LIMIT, legacy_codex_quota
        from harbor.runner.base import process_alive, process_group_alive
        import time
        if (self.shadow or self.read_only or self._dispatch_suspended or self._stop.is_set()
                or self.lifecycle not in {Lifecycle.RUNNING, Lifecycle.STARTING}
                or not self.linear_healthy or self.github_healthy is False):
            return
        connection = getattr(self.db, "connection", None)
        if connection is None or not callable(getattr(self.dispatcher, "retry", None)):
            return
        recovery_schema(connection)
        quota_schema(connection)
        for ticket in self._fleet_tickets:
            identifier = self._ticket_key(ticket)
            if (self._ticket_project(identifier) != self.project or not self._in_scope(ticket) or not self._owns(ticket) or ticket.get("hold", True)
                    or ticket.get("state_type") in {"completed", "canceled"}
                    or str(ticket.get("state", "")).strip().casefold() in {"backlog", "paused", "done", "completed", "canceled", "cancelled"} or ticket.get("blocked_by")):
                continue
            workflow = getattr(self.dispatcher, "workflow", None)
            if workflow:
                stopped = workflow.config.get("states", {})
                if str(ticket.get("state", "")).strip().casefold() in {str(s).casefold() for s in (*stopped.get("paused", []), *stopped.get("terminal", []))}:
                    continue
            task = self._dispatch_tasks.get(identifier)
            if task is not None and not task.done():
                continue
            row = self._latest_run(identifier) or {}
            if row.get("status") == "failed" and row.get("terminal_reason") == "unexpected_exit":
                from harbor.runner.quota import runner_identity
                identity = runner_identity(connection, row["id"])
                confirmed, reset = legacy_codex_quota(row) if not identity or identity[0] == "codex" else (False, None)
                if confirmed:
                    defer_quota(connection, row["id"], "codex", reset, time.time())
                    with connection:
                        connection.execute("UPDATE runs SET terminal_reason=? WHERE id=? AND status='failed'", (USAGE_LIMIT, row["id"]))
                    row = {**row, "terminal_reason": USAGE_LIMIT}
            quota = row.get("terminal_reason") == USAGE_LIMIT
            human_review = str(ticket.get("state", "")).strip().casefold() == "human review"
            if human_review and not quota:
                # Review work interrupted by a supervisor restart can still
                # have a durable, undelivered event on the attached open PR.
                pending = getattr(self.pr_event_processor, "events", None)
                urls = self._ticket_pr_urls(self._authoritative_ticket(ticket))
                has_feedback = callable(pending) and any(
                    item.get("event_type") == "NEW_COMMENT_VERSION"
                    and item.get("pr_url") in urls
                    for item in pending(pending_only=True, project=self.project, ticket=identifier)
                )
                if row.get("status") != "orphaned" or not has_feedback:
                    continue
            if quota:
                blocked = ("the run is paused" if row.get("paused_at")
                           else f"the run is {row.get('status') or 'unknown'}, not failed" if row.get("status") != "failed"
                           else "its retained session is missing" if not row.get("session_id")
                           else "its retained worktree is missing" if not row.get("worktree")
                           else None)
                if blocked:
                    await self._notify_operator(
                        f"quota-stalled:{row.get('id')}",
                        f"{identifier}: {self._quota_wait_text(connection, str(row.get('id')))}; "
                        f"automatic resume is blocked because {blocked}; run /retry {identifier} after then.",
                    )
                    continue
            triage_retry = (row.get("status") == "blocked" and connection.execute("SELECT 1 FROM recovery_receipts WHERE run_id=?", ("triage:" + str(row.get("id")),)).fetchone() is not None)
            triage_quota = row.get("status") == "blocked" and connection.execute(
                "SELECT 1 FROM quota_recovery WHERE run_id=?", ("triage:" + str(row.get("id")),)
            ).fetchone() is not None
            triage_retry = triage_retry or triage_quota
            if triage_retry and any(item.get("run_id") == row.get("id") for item in self.db.open_escalations([identifier])):
                continue
            if not quota and not triage_retry and not recoverable(row):
                continue
            if process_alive(row.get("pid")) or process_group_alive(row.get("pgid")):
                continue
            if not Path(str(row["worktree"])).is_dir() or retained_budget(row, 0) <= 0:
                await self._notify_operator(f"recovery-unavailable:{row['id']}", f"{identifier}: retained worktree missing or execution budget exhausted; human action required.")
                continue
            retry_row = {**row, "id": "triage:" + row["id"], "status": "failed", "terminal_reason": "unexpected_exit"} if triage_retry else row
            if quota or triage_quota:
                # Waiting out a provider usage limit is not crash recovery: it
                # resumes at the provider reset even when recovery is disabled.
                if not claim_quota_retry(connection, row["id"], time.time(), triage=triage_quota):
                    continue
            elif not claim_retry(connection, retry_row, self.blocker_policy, time.time()):
                receipt = connection.execute("SELECT attempts,notified FROM recovery_receipts WHERE run_id=?", (retry_row["id"],)).fetchone()
                if receipt and receipt[0] >= len(self.blocker_policy.retry_delays) and not receipt[1]:
                    next_step = "First Mate could not finish triage; inspect its failure and answer the blocker." if triage_retry else f"Inspect the retained session; /retry {identifier} is an explicit override."
                    notified = await self._notify_operator(f"recovery-exhausted:{retry_row['id']}", f"{identifier}: automatic recovery exhausted after {receipt[0]} attempts. {next_step}")
                    if triage_retry:
                        self.db.record_escalation(identifier, row["id"], "env_blocker", "First Mate retries exhausted; how should this blocker be resolved?", 2, status="open", reason=next_step)
                    if notified:
                        with connection:
                            connection.execute("UPDATE recovery_receipts SET notified=1 WHERE run_id=?", (retry_row["id"],))
                continue
            wake = Wake(ticket=ticket, action="reengage", resume=row["id"], reason="automatic-recovery")
            async def recover(wake=wake, row=row, ticket=ticket, triage_retry=triage_retry, human_review=human_review,
                              quota=quota):
                if triage_retry:
                    await self._handle_runner_result(wake, json.loads(row.get("final_json") or "{}"))
                    return
                instruction = interrupted_retry_instruction(connection, self._ticket_key(ticket), row) if quota else None
                if instruction is None:
                    instruction = recovery_instruction(ticket, row)
                    if human_review:
                        instruction += (" The ticket is in Human Review. Keep the existing PR open and its review gate in place. "
                                        "Check pending Linear and GitHub feedback through the delivery workflow.")
                result = await self.dispatcher.retry(self._ticket_key(ticket), instruction)
                await self._handle_runner_result(wake, result)
            task = asyncio.create_task(recover(), name=f"harbor-recovery-{identifier}")
            self._dispatch_tasks[identifier] = task
            self._dispatch_wakes[identifier] = wake
            task.add_done_callback(lambda done, key=identifier: self._dispatch_finished(key, done))

    async def tick(self) -> PollResult | None:
        return await self.poll_once()

    async def start(self, *, initial_poll: bool = True) -> None:
        if self.lifecycle in {Lifecycle.RUNNING, Lifecycle.STARTING}:
            return
        self.lifecycle = Lifecycle.STARTING
        self._stop.clear()
        self._dispatch_suspended = False
        self._background_dispatch = True
        self._set_runtime()
        await self._apply_startup_migrations()
        await self._hydrate_retained_builders()
        await self._retry_deterministic_blockers()
        if initial_poll:
            await self.poll_once()
        await self._reconcile_completed_merges()
        self.lifecycle = Lifecycle.RUNNING
        self._set_runtime()
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

    async def _retry_deterministic_blockers(self) -> list[str]:
        """Resume only capability blockers that have a known safe adapter.

        This intentionally excludes product decisions and protected-path
        review gates.  It lets a deployed Harbor fix recover an already parked
        builder without requiring a user to repeat the instruction.
        """

        ladder = self.unblock_ladder
        checker = getattr(ladder, "can_retry_deterministically", None) if ladder is not None else None
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if not callable(checker) or connection is None or self.shadow:
            return []
        tickets = {
            str(item.get("identifier")): item
            for item in self.db.tickets(self.project)
            if item.get("identifier") and self._in_scope(item)
        }
        try:
            rows = connection.execute(
                "SELECT * FROM runs WHERE status = 'blocked' AND final_json IS NOT NULL "
                "ORDER BY COALESCE(finished_at, started_at, id) DESC, id DESC"
            ).fetchall()
        except Exception:
            return []
        retried: list[str] = []
        seen: set[str] = set()
        for row in rows:
            run = dict(row)
            identifier = str(run.get("ticket") or "")
            if not identifier or identifier in seen or identifier not in tickets:
                continue
            seen.add(identifier)
            try:
                final = json.loads(str(run.get("final_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(final, Mapping) or not checker(tickets[identifier], run, final):
                continue
            value = ladder.handle(tickets[identifier], run, final)
            self.last_unblock = await value if inspect.isawaitable(value) else value
            retried.append(identifier)
        return retried

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
        self._set_runtime(reason)
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
        self._set_runtime(reason)

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
        self._set_runtime()
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
        self._set_runtime()

    async def exit(self) -> None:
        await self.stop()

    def status(self) -> SupervisorStatus:
        tickets = self._fleet_tickets or (self.last_result.tickets if self.last_result else ())
        escalation_rows: tuple[dict[str, Any], ...] = ()
        run_rows: tuple[dict[str, Any], ...] = ()
        if self.db is not None:
            method = (
                getattr(self.db, "open_escalations", None)
                or getattr(self.db, "escalations", None)
                or getattr(self.db, "list_escalations", None)
            )
            if callable(method):
                try:
                    values = method()
                    # Show only escalations for tickets in this project's
                    # current poll: a Done ticket's stale escalation is noise,
                    # and another project's id is not answerable here.
                    polled = {self._ticket_key(ticket) for ticket in tickets}
                    project_tickets = self._project_ticket_ids()

                    def visible(item: Mapping[str, Any]) -> bool:
                        ticket_id = str(item.get("ticket") or "")
                        if not ticket_id:
                            return True
                        if not self._in_scope(ticket_id):
                            return False
                        if ticket_id in polled:
                            return True
                        # A pre-migration row has no recorded state and is
                        # usually stale once its ticket leaves the poll; an
                        # explicitly open row stays visible for its project.
                        if item.get("status_inferred"):
                            return False
                        return project_tickets is None or ticket_id in project_tickets

                    escalation_rows = tuple(
                        dict(item) for item in values if isinstance(item, Mapping) and visible(item)
                    )
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
                            "merge_verified, cleanup_at, terminal_reason, deadline_seconds "
                            "FROM runs ORDER BY COALESCE(started_at, id), id"
                        ).fetchall()
                        parsed_runs: list[dict[str, Any]] = []
                        for row in rows:
                            item = dict(row)
                            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_receipts'").fetchone():
                                receipt = connection.execute("SELECT attempts,next_retry,last_reason FROM recovery_receipts WHERE run_id IN (?,?) ORDER BY run_id DESC LIMIT 1", (item["id"], "triage:" + item["id"])).fetchone()
                                if receipt:
                                    item["recovery"] = dict(receipt)
                            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='quota_recovery'").fetchone():
                                receipt = connection.execute(
                                    "SELECT provider,next_retry,confirmed FROM quota_recovery WHERE run_id=?",
                                    ("triage:" + item["id"] if item["status"] == "blocked" else item["id"],),
                                ).fetchone()
                                if receipt:
                                    item["quota_recovery"] = dict(receipt)
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
                                        "next_action",
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

    async def ticket_status_context(self, identifier: str) -> str:
        """Return fresh Linear status plus clearly labeled Harbor history.

        Ticket-specific questions may refer to completed issues that are no
        longer in the configured poll states. Resolve those directly by
        identifier, verify the issue belongs to this supervisor's configured
        Linear project scope, and then add any retained run/PR observations.
        """
        import re

        ticket_id = str(identifier or "").strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}-[0-9]+", ticket_id):
            return f"Ticket lookup skipped for invalid identifier {ticket_id!r}."
        if not self._in_scope(ticket_id):
            return f"Ticket lookup skipped for {ticket_id}: outside the requested ticket scope."

        client = getattr(self.poller, "client", None)
        fetch = getattr(client, "fetch_issue_status", None)
        config = getattr(self.poller, "config", None)
        configured_scope = tuple(
            getattr(config, "linear_project_scope", ())
            or ((getattr(config, "linear_project_slug", ""),) if config else ())
        )
        observed: Mapping[str, Any] | None = None
        fresh_note = "Fresh Linear status unavailable; showing any retained Harbor observation."
        if callable(fetch) and configured_scope and all(str(value).strip() for value in configured_scope):
            try:
                issue = fetch(ticket_id)
                if inspect.isawaitable(issue):
                    issue = await issue
                if not isinstance(issue, Mapping) or str(issue.get("identifier", "")).upper() != ticket_id:
                    raise ValueError("Linear returned a different ticket")
                from harbor.project_identity import require_scope

                await require_scope(client, configured_scope, issue.get("project", {}))
                observed = issue
                state = issue.get("state")
                state_name = state.get("name") if isinstance(state, Mapping) else state
                state_type = state.get("type") if isinstance(state, Mapping) else None
                fresh_note = (
                    f"Fresh Linear status: {ticket_id} — {state_name or 'unknown'}"
                    f" ({state_type or 'type unknown'})."
                )
                title = " ".join(str(issue.get("title") or "").split())
                if title:
                    fresh_note += f" Title: {title}."
                if issue.get("updatedAt"):
                    fresh_note += f" Linear updated: {issue['updatedAt']}."
                if issue.get("url"):
                    fresh_note += f" URL: {issue['url']}."
            except Exception:
                # Keep a failed or unverifiable lookup from being presented as
                # fresh truth. The local ledger below is explicitly labeled.
                fresh_note = f"Fresh Linear status could not be verified for {ticket_id}."
        elif not configured_scope:
            fresh_note = f"Fresh Linear status could not be verified for {ticket_id}: project scope is not configured."

        lines = [fresh_note]
        db = self.db
        if db is None:
            return "\n".join(lines)
        try:
            stored = next(
                (item for item in db.tickets(self.project)
                 if str(item.get("identifier", "")).upper() == ticket_id),
                None,
            )
        except Exception:
            stored = None
        if stored is not None:
            lines.append(
                "Last Harbor ticket observation: "
                f"{stored.get('state', 'unknown')} at {stored.get('observed_at', 'unknown time')}"
                + (f"; Linear updated {stored['updated_at']}" if stored.get("updated_at") else "")
                + "."
            )

        connection = getattr(db, "connection", None)
        # Never read run/PR rows from a shared DB unless this ticket's
        # project identity was verified by Linear or by this project's own
        # stored ticket row.
        if connection is not None and (observed is not None or stored is not None):
            try:
                run = connection.execute(
                    "SELECT id,status,waiting_on,started_at,finished_at,terminal_reason,final_json "
                    "FROM runs WHERE ticket=? ORDER BY COALESCE(finished_at,started_at,id) DESC,id DESC LIMIT 1",
                    (ticket_id,),
                ).fetchone()
                if run:
                    item = dict(run)
                    line = f"Latest Harbor builder run: {item.get('status', 'unknown')}"
                    if item.get("finished_at"):
                        line += f", finished {item['finished_at']}"
                    if item.get("terminal_reason"):
                        line += f", reason {item['terminal_reason']}"
                    try:
                        final = json.loads(item.get("final_json") or "{}")
                    except (TypeError, ValueError):
                        final = {}
                    if isinstance(final, Mapping):
                        details = [f"{key}={final[key]}" for key in
                                   ("outcome", "pr_url", "merge_commit_sha") if final.get(key)]
                        if details:
                            line += "; " + ", ".join(details)
                    lines.append(line + ".")
                prs = connection.execute(
                    "SELECT number,url,head_sha,ci_state,merged,observed_at FROM prs "
                    "WHERE ticket=? ORDER BY number", (ticket_id,),
                ).fetchall()
                for row in prs:
                    pr = dict(row)
                    lines.append(
                        "Last Harbor PR observation: "
                        f"PR #{pr.get('number')} CI {pr.get('ci_state') or 'unknown'}, "
                        f"merged={'yes' if pr.get('merged') else 'no'}, "
                        f"head {pr.get('head_sha') or 'unknown'}, observed {pr.get('observed_at') or 'unknown'}"
                        + (f"; {pr['url']}" if pr.get("url") else "") + "."
                    )
            except Exception:
                # Older/minimal DBs may not have the additive run/PR tables.
                pass
        return "\n".join(lines)

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

    async def _monitor_blocked(self) -> None:
        """First Mate monitor-and-recover pass over stuck runs and watchdog flags."""

        workflow = getattr(self.dispatcher, "workflow", None)
        ladder = self.unblock_ladder
        mate = getattr(ladder, "first_mate", None) if ladder is not None else None
        connection = getattr(self.db, "connection", None) if self.db is not None else None
        if workflow is None or mate is None or connection is None or self._dispatch_suspended:
            return
        from harbor import first_mate_recovery as recovery
        from harbor.first_mate import FirstMateError, FirstMateQuotaError

        settings = workflow.config.get("first_mate", {})
        try:
            cases = recovery.find_cases(connection, self.project, settings)
        except Exception as exc:
            self.last_error = f"First Mate monitor: {exc}"
            return
        for case in cases:
            if case.ticket in self._dispatch_tasks and not self._dispatch_tasks[case.ticket].done():
                continue
            if case.trigger == "watchdog:cleanup_stuck":
                # Deterministic: re-run the verified merge cleanup; no model call.
                recovery.record(connection, case, {"action": "cleanup", "instruction": "", "reason": case.detail})
                try:
                    await self._reconcile_completed_merges(case.ticket, refresh=True)
                except Exception as exc:  # never abort the rest of the monitor pass
                    self.last_error = f"First Mate cleanup for {case.ticket}: {exc}"
                    await self._notify_operator(f"cleanup-failed:{case.run_id}:{len(case.history)}",
                                                f"First Mate could not clean up {case.ticket}: {exc}")
                continue
            try:
                proposal = recovery.parse(await mate.triage(recovery.build_prompt(case, settings)))
            except FirstMateQuotaError as exc:
                # The only case that backs off exponentially (see backoff_seconds).
                proposal = {"action": "wait", "reason": f"{recovery.USAGE_LIMIT} {exc}"}
            except FirstMateError as exc:
                proposal = {"action": "wait", "reason": f"First Mate unavailable: {exc}"}
            decision = recovery.validate(proposal, case, settings)
            recovery.record(connection, case, decision)
            if decision["action"] == "wait":
                continue
            await self._post_recovery_comment(case, decision)
            try:
                await self._apply_recovery(case, decision)
            except Exception as exc:
                failed = {"action": "escalate", "instruction": "", "reason": f"{decision['action']} failed: {exc}"}
                recovery.record(connection, case, failed)
                await self._notify_operator(f"recovery-failed:{case.run_id}:{len(case.history)}",
                                            f"First Mate could not {decision['action']} {case.ticket}: {exc}")
                continue
            await self._notify_operator(f"recovery:{case.run_id}:{len(case.history)}",
                                        f"First Mate {decision['action']} {case.ticket} ({case.trigger}): {decision['reason']}")

    async def _post_recovery_comment(self, case: Any, decision: Mapping[str, Any]) -> None:
        from harbor import first_mate_recovery as recovery
        from harbor.unblock import _linear_comment
        linear = getattr(self.unblock_ladder, "linear", None) or getattr(self, "linear", None)
        if linear is None or not case.linear_id:
            return
        try:
            await _linear_comment(linear, self.db, case.linear_id, recovery.comment(case, decision))
        except Exception as exc:  # a comment failure never stops recovery
            self.last_error = f"First Mate comment: {exc}"

    async def _apply_recovery(self, case: Any, decision: Mapping[str, Any]) -> None:
        from harbor import first_mate_recovery as recovery
        action, instruction = decision["action"], decision.get("instruction") or ""
        if action == "resume":
            if not await self.resume_session(case.run_id, instruction):
                raise RuntimeError("retained builder session is not available to resume")
        elif action == "retry":
            result = await self.dispatcher.retry(case.ticket, instruction)
            run_id = str((result.get("run_id") if isinstance(result, Mapping) else getattr(result, "run_id", "")) or case.run_id)
            wake = Wake(ticket={"identifier": case.ticket, "builder_run_id": run_id, "run_id": run_id},
                        action="reengage", resume=run_id, reason="first-mate-retry")
            await self._handle_runner_result(wake, result)
        elif action == "fresh_session":
            result = await self.dispatcher.fresh_session(case.ticket, recovery.fresh_session_instruction(case, instruction))
            wake = Wake(ticket={"identifier": case.ticket, "builder_run_id": case.run_id, "run_id": case.run_id},
                        action="reengage", resume=case.run_id, reason="first-mate-fresh-session")
            await self._handle_runner_result(wake, result)
        elif action == "escalate":
            await self._notify_operator(f"recovery-escalate:{case.run_id}",
                                        f"First Mate needs a human for {case.ticket} ({case.trigger}): {decision['reason']}")

    async def resume_session(self, run_id: str, prompt: str) -> bool:
        """Resume one persistent builder session, without creating a replacement."""

        identifier = self._ticket_for_run(run_id)
        if self.ticket_scope and (not identifier or not self._in_scope(identifier)):
            return False
        target = self.dispatcher or self.runner
        if isinstance(target, WorktreeDispatcher):
            if not identifier:
                return False
            wake = Wake(
                ticket={"identifier": identifier, "builder_run_id": str(run_id), "run_id": str(run_id)},
                action="reengage",
                resume=str(run_id),
                instruction=prompt,
            )
            value = await target.resume(wake)
            if value is None or value is False:
                return False
            # WorktreeDispatcher returns the builder's terminal payload.  Feed
            # it back through the same result router used for poll-driven
            # wakes so a second blocker is bounded, recorded, and either
            # adapted again or escalated instead of disappearing.
            await self._handle_runner_result(wake, value)
            return True
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

    async def retry_builder(self, identifier: str) -> Any:
        """Run an explicit terminal recovery through the normal result router."""

        if self.shadow or self.read_only or self._dispatch_suspended:
            raise RuntimeError("retry is unavailable while supervision is read-only or paused")
        target = self.dispatcher
        retry = getattr(target, "retry", None) if target is not None else None
        if not callable(retry):
            raise RuntimeError("retained builder retry is unavailable")
        result = retry(str(identifier), None)
        result = await result if inspect.isawaitable(result) else result
        rearm = getattr(self.pr_event_processor, "rearm_failed_events", None)
        if callable(rearm):
            # The operator has inspected the session; offer parked PR events again.
            rearm(ticket=str(identifier), project=self.project)
        if isinstance(result, Mapping):
            run_id = str(result.get("run_id") or "")
        else:
            run_id = str(getattr(result, "run_id", "") or "")
        wake = Wake(
            ticket={"identifier": str(identifier), "builder_run_id": run_id, "run_id": run_id},
            action="reengage",
            resume=run_id or None,
            reason="operator-retry",
        )
        await self._handle_runner_result(wake, result)
        return result

    async def answer_escalation(self, escalation_id: str, response: str, *, background: bool = False, enqueue_only: bool = False) -> dict[str, Any]:
        """Record an operator answer, mark the escalation answered, and resume.

        ``Citation: path:line`` is optional.  Without one the operator is the
        cited authority (``operator:escalation-<id>``), so a product decision
        can be answered directly and still reused at rung 1.

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
        active = self._dispatch_tasks.get(str(row.get("ticket") or ""))
        if active is not None and not active.done():
            raise ValueError("builder already has active work; wait before answering again")
        if self.ticket_scope:
            identifier = str(row.get("ticket") or "")
            if not identifier or not self._in_scope(identifier):
                raise ValueError(
                    f"escalation {wanted} is outside invocation scope "
                    f"{self.ticket_scope.selector}"
                )
        if background and getattr(self.db,"connection",None) is not None:
            from harbor.answer_queue import initialize
            initialize(self.db.connection)
            queued = self.db.connection.execute("SELECT status FROM answer_queue WHERE escalation=?",(wanted,)).fetchone()
            if queued and queued[0] not in {"failed","stopped"}:
                raise ValueError("answer already queued or delivered; no duplicate work launched")
        row_status = str(row.get("status") or "open").casefold()
        if row_status not in {"open", "answered"}:
            raise ValueError(f"escalation {wanted} is {row_status}; answer the ticket's current escalation instead")
        row_ticket = str(row.get("ticket") or "")
        if getattr(self.db, "connection", None) is not None:
            # A durable ledger can place every ticket it observed; an answer
            # for a ticket it cannot place is refused rather than trusted.
            owner = self._ticket_project(row_ticket) if row_ticket else None
            if owner != self.project:
                raise ValueError(
                    f"escalation {wanted} is for {row_ticket or 'no ticket'}, not a ticket observed in project {self.project}"
                )
        text = str(response).strip()
        if not text:
            raise ValueError("an answer is required")
        citation_match = re.search(r"(?:^|\s)(?:citation|source)\s*[:=]\s*(.+?)\s*$", text, re.IGNORECASE | re.MULTILINE)
        if citation_match is None:
            citation_match = re.search(r"\[(?:citation|source)\s*[:=]\s*([^\]]+)\]", text, re.IGNORECASE)
        if citation_match is None:
            # A product call has no document to cite: the operator is the
            # authority, and the resolution records that attribution.
            citation = f"operator:escalation-{wanted}"
            answer = text
        else:
            citation = citation_match.group(1).strip()
            answer = (text[: citation_match.start()] + text[citation_match.end() :]).strip(" \t\r\n-|")
        if not answer:
            raise ValueError("an answer is required")
        record = getattr(self.db, "record_resolution", None) or getattr(self.db, "add_resolution", None)
        if not callable(record):
            raise RuntimeError("the Harbor ledger cannot record resolutions")
        try:
            # An explicit operator answer corrects any earlier stored answer.
            resolution = record(
                str(row.get("kind", "env_blocker")),
                str(row.get("question", "")),
                answer,
                citation,
                source_ticket=row.get("ticket"),
                replace=True,
            )
        except TypeError:
            resolution = record(str(row.get("kind", "env_blocker")), str(row.get("question", "")), answer, citation)
        update = getattr(self.db, "update_escalation", None)
        if callable(update):
            resolution_id = resolution.get("id") if isinstance(resolution, Mapping) else None
            update(wanted, status="answered", resolution_id=resolution_id)
        run_id = str(row.get("run_id") or "")
        resumed = False
        if run_id:
            prompt = f"Operator answer for escalation {wanted}: {answer}\nAuthority citation: {citation}"
            if background:
                from harbor.answer_queue import enqueue, schedule
                state = enqueue(self.db.connection,wanted,self.project,str(row["ticket"]),run_id,prompt)
                if not enqueue_only and state == "queued":
                    schedule(self,(wanted,self.project,str(row["ticket"]),run_id,prompt,state))
            else:
                resumed = await self.resume_session(run_id, prompt)
        return {"escalation": dict(row), "resolution": resolution, "resumed": resumed, "queued": bool(run_id and background), "answer": answer, "citation": citation}


__all__ = ["Lifecycle", "Supervisor", "SupervisorStatus", "WorktreeDispatcher"]
