"""Common lifecycle, result, deadline, and process-group machinery."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable

from harbor.runner.stream import StreamEvent, extract_final, iter_stream
from harbor.db import apply_phase9_migration, sql_statements
from harbor.resources import runtime_asset


STREAM_BUFFER_LIMIT = 8 * 1024 * 1024

RUN_STATUSES = frozenset({"running", "waiting", "completed", "blocked", "failed", "terminated", "orphaned"})
WAITING_ON = frozenset({"ci", "review", "human", "none"})
OUTCOMES = frozenset({"merged", "pr_open", "parked", "no_pr"})
ARTIFACT_KEYS = frozenset(
    {
        "pr_url",
        "pr_urls",
        "pull_request_url",
        "commit_sha",
        "commit_url",
        "commit",
        "commits",
        "commit_evidence",
    }
)
BLOCKER_KINDS = frozenset(
    {
        "product_decision",
        "env_blocker",
        "github_transport",
        "ambiguous_ac",
        "missing_proof_capability",
        "conflicting_instruction",
    }
)


class RunnerError(RuntimeError):
    """A runner could not be started or did not produce a valid result."""


class ResultValidationError(RunnerError):
    """The builder's final output did not satisfy run-result.json."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("invalid builder result: " + "; ".join(self.errors))


class RunState(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    TERMINATED = "terminated"
    ORPHANED = "orphaned"


def validate_result(payload: Any) -> list[str]:
    """Return schema violations for the small, dependency-free result schema."""

    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return ["result must be an object"]
    if "status" not in payload:
        errors.append("status is required")
    elif payload["status"] not in {"completed", "waiting", "blocked", "failed"}:
        errors.append("status must be completed, waiting, blocked, or failed")
    if "summary" not in payload:
        errors.append("summary is required")
    elif not isinstance(payload["summary"], str):
        errors.append("summary must be a string")
    # Structured outputs require every property to appear in `required`, so
    # the builder expresses "unset" as an explicit null rather than omission.
    # Treat null and absent identically here.
    if payload.get("outcome") is not None and payload["outcome"] not in OUTCOMES:
        errors.append("outcome is not recognized")
    if payload.get("waiting_on") is not None and payload["waiting_on"] not in WAITING_ON:
        errors.append("waiting_on is not recognized")
    if payload.get("pr_url") is not None and not isinstance(payload["pr_url"], str):
        errors.append("pr_url must be a string")
    if payload.get("next_action") is not None and not isinstance(payload["next_action"], str):
        errors.append("next_action must be a string")
    blocker = payload.get("blocker")
    if blocker is not None and not isinstance(blocker, Mapping):
        errors.append("blocker must be an object or null")
    elif isinstance(blocker, Mapping):
        if blocker.get("kind") is not None and blocker["kind"] not in BLOCKER_KINDS:
            errors.append("blocker.kind is not recognized")
        for key in ("kind", "question", "recommendation", "evidence"):
            if blocker.get(key) is not None and not isinstance(blocker[key], str):
                errors.append(f"blocker.{key} must be a string")
        if blocker.get("options") is not None and (
            not isinstance(blocker["options"], list) or not all(isinstance(item, str) for item in blocker["options"])
        ):
            errors.append("blocker.options must be an array of strings")
    return errors


def _present(value: Any) -> bool:
    """Whether a PR/commit evidence value is materially populated."""

    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_present(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_present(item) for item in value)
    return True


def has_pr_evidence(payload: Mapping[str, Any]) -> bool:
    """Return whether a final result names a pull request artifact."""

    return any(_present(payload.get(key)) for key in ("pr_url", "pr_urls", "pull_request_url"))


def has_commit_evidence(payload: Mapping[str, Any]) -> bool:
    """Return whether a final result names a commit artifact."""

    return any(
        _present(payload.get(key))
        for key in ("commit_sha", "commit_url", "commit", "commits", "commit_evidence")
    )


def merge_is_verified(payload: Mapping[str, Any], observed: Mapping[str, Any] | None = None) -> bool:
    """Whether a merge is corroborated by something other than self-report.

    ``FINAL_SCHEMA`` deliberately has no merge-evidence field, so a builder
    literally cannot certify its own merge.  The authority is what Harbor
    observed on the PR itself -- ``PRFacts.merged`` from the GitHub poller --
    which is why ``observed`` is the primary source here.
    """

    if not isinstance(observed, Mapping):
        return False
    # GitHubPoller emits a boolean ``merged`` flag; only that literal ``True``
    # counts.  Never inspect the builder payload here: ``merged_at``,
    # ``merge_commit_sha`` and ``merge_verified`` in final JSON are all
    # self-reported and cannot authorize cleanup.
    return observed.get("merged") is True


def _missing_artifact_blocker(*, reason: str) -> dict[str, str]:
    return {
        "kind": "missing_proof_capability",
        "question": "What PR URL or commit evidence should certify this completed run?",
        "recommendation": "Provide a pull request URL or commit SHA/URL, then resume the same run.",
        "evidence": reason,
    }


def normalize_final_result(
    payload: Mapping[str, Any],
    *,
    observed: Mapping[str, Any] | None = None,
    known_pr_url: str | None = None,
) -> dict[str, Any]:
    """Fail closed around self-reported completion artifacts.

    The builder's JSON schema validates shape, but shape alone is not delivery
    evidence.  A completed result naming neither a PR nor a commit is therefore
    represented as a blocked ``no_pr`` outcome.

    A claimed merge is never destroyed here -- doing so would corrupt the run
    record for a merge that genuinely happened.  Instead it carries
    ``merge_verified``, set only from ``observed`` GitHub state.  The supervisor
    gates every irreversible consequence of a merge (worktree removal) on that
    flag, so an optimistic builder cannot cause one.

    ``known_pr_url`` is the PR this run opened on an earlier turn.  A resumed
    session that merges its own PR routinely omits ``pr_url`` from the final
    message; the run's own ledger, not the builder, supplies it.
    """

    value = dict(payload)
    if value.get("status") != "completed":
        return value
    if not has_pr_evidence(value) and _present(known_pr_url):
        value["pr_url"] = known_pr_url
    pr = has_pr_evidence(value)
    commits = has_commit_evidence(value)
    outcome = str(value.get("outcome") or "").casefold()
    if outcome == "merged":
        if not pr and not commits:
            value.update(
                {
                    "status": "blocked",
                    "outcome": "no_pr",
                    "waiting_on": "human",
                    "merge_verified": False,
                    "blocker": value.get("blocker")
                    or _missing_artifact_blocker(reason="completed outcome=merged named no PR or commit"),
                }
            )
            return value
        verified = merge_is_verified(value, observed)
        value["merge_verified"] = verified
        if not verified:
            # Keep the builder's own account of the run intact; the supervisor
            # confirms the merge on its next poll (plan section 7, AC-10).
            value.setdefault("next_action", "Awaiting Harbor's own observation of the merge.")
        if observed:
            for key in ("merged_at", "merge_commit_sha"):
                if _present(observed.get(key)) and not _present(value.get(key)):
                    value[key] = observed[key]
        return value
    if pr:
        # A PR artifact is necessarily still open unless Harbor observed a merge.
        if merge_is_verified(value, observed):
            value.update({"outcome": "merged", "waiting_on": "none", "merge_verified": True})
            return value
        if outcome == "parked" and str(value.get("waiting_on") or "").casefold() == "human":
            # A merge-ready park is waiting on the operator's merge, not on
            # review.  A park waiting on CI or review keeps its own wake path.  Keeping ``waiting_on=human`` is what lets an operator
            # merge re-engage this same builder and later clean its worktree.
            value.update(
                {
                    "status": "waiting",
                    "outcome": "parked",
                    "waiting_on": "human",
                    "merge_verified": False,
                    "next_action": value.get("next_action") or "Wait for the operator to merge the PR.",
                }
            )
            return value
        value.update(
            {
                "status": "waiting",
                "outcome": "pr_open",
                "waiting_on": "review",
                "merge_verified": False,
                "next_action": value.get("next_action") or "Wait for PR review/merge.",
            }
        )
        return value
    if not commits:
        value.update(
            {
                "status": "blocked",
                "outcome": "no_pr",
                "waiting_on": "human",
                "blocker": value.get("blocker")
                or _missing_artifact_blocker(reason="completed result contained neither PR nor commit evidence"),
            }
        )
    return value


def validate_final_result(
    payload: Any,
    *,
    observed: Mapping[str, Any] | None = None,
    known_pr_url: str | None = None,
) -> dict[str, Any]:
    errors = validate_result(payload)
    if errors:
        raise ResultValidationError(errors)
    return normalize_final_result(dict(payload), observed=observed, known_pr_url=known_pr_url)


@dataclass(frozen=True)
class RunResult:
    status: str
    summary: str
    outcome: str | None = None
    waiting_on: str | None = None
    pr_url: str | None = None
    commit_sha: str | None = None
    commit_url: str | None = None
    commits: Any | None = None
    merge_verified: bool | None = None
    merged_at: str | None = None
    merge_commit_sha: str | None = None
    next_action: str | None = None
    blocker: Mapping[str, Any] | None = None
    cost_usd: float | None = None
    exit_code: int | None = None
    run_id: str | None = None
    session_id: str | None = None
    error: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], **metadata: Any) -> "RunResult":
        value = validate_final_result(
            payload,
            observed=metadata.pop("observed", None),
            known_pr_url=metadata.pop("known_pr_url", None),
        )
        return cls(
            status=str(value["status"]),
            summary=str(value["summary"]),
            outcome=value.get("outcome"),
            waiting_on=value.get("waiting_on"),
            pr_url=value.get("pr_url"),
            commit_sha=value.get("commit_sha"),
            commit_url=value.get("commit_url"),
            commits=value.get("commits"),
            merge_verified=value.get("merge_verified"),
            merged_at=value.get("merged_at"),
            merge_commit_sha=value.get("merge_commit_sha"),
            next_action=value.get("next_action"),
            blocker=value.get("blocker"),
            cost_usd=metadata.get("cost_usd"),
            exit_code=metadata.get("exit_code"),
            run_id=metadata.get("run_id"),
            session_id=metadata.get("session_id"),
            error=metadata.get("error"),
        )

    def as_dict(self, *, include_metadata: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status, "summary": self.summary}
        for key in (
            "outcome",
            "waiting_on",
            "pr_url",
            "commit_sha",
            "commit_url",
            "commits",
            "merge_verified",
            "merged_at",
            "merge_commit_sha",
            "next_action",
            "blocker",
        ):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        if include_metadata:
            for key in ("cost_usd", "exit_code", "run_id", "session_id", "error"):
                value = getattr(self, key)
                if value is not None:
                    result[key] = value
        return result

    @property
    def final_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


@dataclass
class Deadline:
    """A monotonic deadline whose clock stops while Harbor is paused."""

    duration_seconds: float
    started_at: float = field(default_factory=time.monotonic)
    paused_seconds: float = 0.0
    paused_at: float | None = None
    clock: Callable[[], float] = time.monotonic

    @property
    def is_paused(self) -> bool:
        return self.paused_at is not None

    def pause(self, now: float | None = None) -> None:
        if self.paused_at is None:
            self.paused_at = self.clock() if now is None else float(now)

    def resume(self, now: float | None = None) -> None:
        if self.paused_at is not None:
            current = self.clock() if now is None else float(now)
            self.paused_seconds += max(0.0, current - self.paused_at)
            self.paused_at = None

    def elapsed(self, now: float | None = None) -> float:
        current = self.clock() if now is None else float(now)
        end = self.paused_at if self.paused_at is not None else current
        return max(0.0, end - self.started_at - self.paused_seconds)

    def remaining(self, now: float | None = None) -> float:
        return max(0.0, float(self.duration_seconds) - self.elapsed(now))

    @property
    def remaining_seconds(self) -> float:
        return self.remaining()

    def expired(self, now: float | None = None) -> bool:
        return not self.is_paused and self.remaining(now) <= 0

    @property
    def deadline_at(self) -> float:
        return self.started_at + float(self.duration_seconds) + self.paused_seconds


def process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def process_group_alive(pgid: int | None) -> bool:
    if not pgid or pgid <= 1:
        return False
    try:
        os.killpg(int(pgid), 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def _safe_pgid(pid: int, pgid: int | None = None) -> int:
    group = int(pgid) if pgid else os.getpgid(pid)
    if group <= 1 or group == os.getpgrp():
        raise RunnerError(f"refusing to signal unsafe process group {group}")
    return group


def signal_process_group(pid: int, pgid: int | None = None, *, sig: signal.Signals = signal.SIGTERM) -> bool:
    """Signal a child process group, never Harbor's own process group."""

    try:
        group = _safe_pgid(int(pid), pgid)
    except (ProcessLookupError, PermissionError, RunnerError):
        return False
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        return False
    return True


kill_process_group = signal_process_group
terminate_process_group = signal_process_group
killpg = signal_process_group


def _db_connection(db: Any) -> sqlite3.Connection | None:
    if isinstance(db, sqlite3.Connection):
        return db
    connection = getattr(db, "connection", None)
    return connection if isinstance(connection, sqlite3.Connection) else None


def _db_lock(db: Any) -> Any:
    return getattr(db, "_lock", contextlib.nullcontext())


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def apply_phase4_migration(db: Any, path: str | Path | None = None) -> None:
    """Apply 004's additive columns without changing the older DB module."""

    connection = _db_connection(db)
    if connection is None:
        raise TypeError("Phase 4 migration requires HarborDB or sqlite3.Connection")
    if not _table_exists(connection, "runs"):
        # Phase 3 owns creation of runs.  Keeping this a no-op lets a Phase 4
        # runner be imported with a fresh Phase 1 database; the supervisor's
        # startup migration must still apply 003 before dispatching.
        return
    migration = Path(path) if path else runtime_asset("migrations", "004_phase4_runner.sql")
    script = migration.read_text(encoding="utf-8")
    with _db_lock(db):
        for statement in sql_statements(script):
            upper = statement.upper()
            if upper.startswith("ALTER TABLE RUNS ADD COLUMN"):
                column = statement.split()[5]
                columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
                if column in columns:
                    continue
            connection.execute(statement)
        connection.commit()


def _call_store(db: Any, names: tuple[str, ...], *args: Any, **kwargs: Any) -> Any:
    if db is None:
        return None
    for name in names:
        method = getattr(db, name, None)
        if callable(method):
            try:
                return method(*args, **kwargs)
            except TypeError:
                if kwargs:
                    return method(*args)
                raise
    return None


def _update_run_sql(
    db: Any,
    run_id: str,
    values: Mapping[str, Any],
    *,
    expected_status: str | None = None,
) -> int | None:
    """Update one run row and optionally compare its current status.

    Returning the affected-row count lets lifecycle recovery use a small
    compare-and-set instead of overwriting a result that another supervisor
    persisted while the reaper was waiting for a process group to exit.
    ``None`` means this database exposes no SQLite connection and callers may
    use their legacy adapter fallback.
    """

    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "runs"):
        return None
    columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    updates = [(key, value) for key, value in values.items() if key in columns]
    if not updates:
        return 0
    where = "id = ?"
    parameters: list[Any] = [value for _, value in updates]
    parameters.append(run_id)
    if expected_status is not None and "status" in columns:
        where += " AND status = ?"
        parameters.append(expected_status)
    lock = _db_lock(db)
    with lock:
        cursor = connection.execute(
            f"UPDATE runs SET {', '.join(f'{key} = ?' for key, _ in updates)} WHERE {where}",
            tuple(parameters),
        )
        connection.commit()
    return int(cursor.rowcount)


def _insert_run(db: Any, values: Mapping[str, Any]) -> None:
    if db is None:
        return
    for name in ("insert_run", "create_run"):
        method = getattr(db, name, None)
        if callable(method):
            try:
                method(**dict(values))
            except TypeError:
                # A store may expose a compact positional adapter.  SQLite is
                # still the fallback when a method explicitly declines the
                # extended Phase 4 metadata.
                try:
                    method(values)
                except TypeError:
                    pass
            return
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "runs"):
        return
    columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    selected = [(key, value) for key, value in values.items() if key in columns]
    if not selected:
        return
    with _db_lock(db):
        names = ", ".join(key for key, _ in selected)
        placeholders = ", ".join("?" for _ in selected)
        # ``INSERT OR REPLACE`` deletes the old row before inserting its
        # replacement.  A resumed run would therefore lose final/artifact
        # fields that are not present in this startup record.  An UPSERT
        # updates only the process-lifecycle fields supplied here and keeps
        # durable evidence on the existing logical run.
        updates = [
            key
            for key, _ in selected
            if key not in {"id", "started_at", "finished_at", "final_json", "cost_usd"}
        ]
        assignments = [f"{key} = excluded.{key}" for key in updates]
        if "session_id" in columns and "session_id" in {key for key, _ in selected}:
            assignments = [
                item for item in assignments if not item.startswith("session_id = ")
            ]
            assignments.append("session_id = COALESCE(excluded.session_id, runs.session_id)")
        if "finished_at" in columns:
            # A new process execution is active again; retain the original
            # start timestamp but clear the prior attempt's finish marker.
            assignments.append("finished_at = NULL")
        if assignments:
            conflict = f" ON CONFLICT(id) DO UPDATE SET {', '.join(assignments)}"
        else:
            conflict = " ON CONFLICT(id) DO NOTHING"
        connection.execute(
            f"INSERT INTO runs ({names}) VALUES ({placeholders}){conflict}",
            tuple(value for _, value in selected),
        )
        connection.commit()


@dataclass
class RunContext:
    run_id: str
    ticket: str
    worktree: Path
    deadline: Deadline
    process: asyncio.subprocess.Process | None = None
    pid: int | None = None
    pgid: int | None = None
    session_id: str | None = None
    expected_session_id: str | None = None
    session_mismatch: bool = False
    paused: bool = False
    deadline_expired: bool = False
    terminated: bool = False
    events: list[StreamEvent] = field(default_factory=list)
    last_activity: float = field(default_factory=time.monotonic)
    cost_usd: float = 0.0
    pause_changed: asyncio.Event = field(default_factory=asyncio.Event)


def _persisted_waiting_runs(db: Any, ticket: str | None = None) -> list[dict[str, Any]]:
    """Read retained waiting runs without requiring a concrete HarborDB class."""

    if db is None:
        return []
    for name in ("waiting_runs", "retained_runs"):
        method = getattr(db, name, None)
        if callable(method):
            try:
                value = method(ticket=ticket) if ticket is not None else method()
            except TypeError:
                value = method(ticket) if ticket is not None else method()
            return [dict(item) for item in value or () if isinstance(item, Mapping)]
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "runs"):
        return []
    if ticket is None:
        rows = connection.execute("SELECT * FROM runs WHERE status = 'waiting' ORDER BY id").fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM runs WHERE status = 'waiting' AND ticket = ? ORDER BY id", (str(ticket),)
        ).fetchall()
    return [dict(row) for row in rows]


def _run_row(db: Any, run_id: str) -> dict[str, Any] | None:
    """Read one run row, tolerating the small fakes used in tests."""

    if db is None or not run_id:
        return None
    for name in ("run", "get_run", "last_run"):
        method = getattr(db, name, None)
        if callable(method):
            try:
                value = method(run_id)
            except Exception:
                continue
            if isinstance(value, Mapping):
                return dict(value)
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "runs"):
        return None
    try:
        row = connection.execute("SELECT * FROM runs WHERE id = ?", (str(run_id),)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return dict(row)
    except (TypeError, ValueError):
        # A connection without ``row_factory`` yields plain tuples; fall back
        # to the cursor description so this stays usable in either shape.
        return None


class BaseRunner:
    """Base implementation used by the Codex runner and deterministic tests."""

    role = "builder"

    def __init__(
        self,
        worktree: str | Path,
        *,
        db: Any | None = None,
        ticket: str = "",
        deadline_seconds: float = 90 * 60,
        run_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        cleanup_hook: Callable[..., Any] | None = None,
    ) -> None:
        self.worktree = Path(worktree).expanduser().resolve()
        if not self.worktree.is_dir():
            raise RunnerError(f"builder worktree does not exist: {self.worktree}")
        self.db = db
        self.ticket = ticket
        self.deadline_seconds = float(deadline_seconds)
        self.clock = clock
        self.default_run_id = run_id
        self.cleanup_hook = cleanup_hook
        self.contexts: dict[str, RunContext] = {}
        self.active: RunContext | None = None
        # The PR a run opened on an earlier turn.  A resumed builder that
        # merges its own PR routinely omits ``pr_url`` from the final message.
        self.run_pr_urls: dict[str, str] = {}
        # Supplied by the supervisor: ``PRFacts`` for a ticket as Harbor
        # observed them on GitHub.  This is the only authority for a merge;
        # the builder cannot certify its own.
        self.observed_pr_facts: Callable[[str], Any] | None = None
        if db is not None:
            with contextlib.suppress(Exception):
                apply_phase4_migration(db)
            with contextlib.suppress(Exception):
                apply_phase9_migration(db)
            # A process restart loses in-memory RunContext objects.  Waiting
            # rows are safe to hydrate because they have no live process; the
            # retained session id lets a later re-engagement use the same
            # builder conversation instead of creating a duplicate agent.
            self.hydrate_waiting_contexts()

    def _observed_facts(self, context: RunContext) -> Mapping[str, Any] | None:
        """Ask the supervisor what it observed on this ticket's PR, if anything."""

        if self.observed_pr_facts is None:
            return None
        try:
            facts = self.observed_pr_facts(context.ticket or self.ticket)
        except Exception:
            # An observation gap must never turn a finished builder run into a
            # runner failure; an unverified merge is simply not yet verified.
            return None
        return facts if isinstance(facts, Mapping) else None

    def _known_pr_url(self, context: RunContext) -> str | None:
        """Recover the PR this run already opened, from memory or the ledger."""

        remembered = self.run_pr_urls.get(context.run_id)
        if remembered:
            return remembered
        row = _run_row(self.db, context.run_id)
        if not row:
            return None
        raw = row.get("final_json")
        if not raw:
            return None
        try:
            previous = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(previous, Mapping):
            return None
        for key in ("pr_url", "pull_request_url"):
            value = previous.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def command(self, instruction: str, *, run_id: str, resume: bool = False) -> list[str]:
        raise NotImplementedError

    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "HARBOR_WORKTREE": str(self.worktree),
                "GIT_CEILING_DIRECTORIES": str(self.worktree.parent),
                # PWD is advisory but useful to fake executables and tools that
                # report their effective workspace in event output.
                "PWD": str(self.worktree),
            }
        )
        # GitHub transport settings for harbor.github_rest (names only; the
        # token stays in whichever variable the operator already set).
        env.update(dict(getattr(self, "github_env", None) or {}))
        return env

    def _new_context(self, run_id: str | None = None) -> RunContext:
        logical_id = run_id or self.default_run_id or str(uuid.uuid4())
        context = RunContext(
            run_id=logical_id,
            ticket=self.ticket,
            worktree=self.worktree,
            deadline=Deadline(self.deadline_seconds, clock=self.clock),
        )
        self.contexts[logical_id] = context
        return context

    def hydrate_waiting_contexts(self) -> list[RunContext]:
        """Restore persisted waiting contexts after a Harbor restart.

        This method is public so a dispatcher that creates runners before its
        database migration can call it explicitly once migrations finish.  It
        never resumes a process; it only restores identity, worktree, and
        pause/deadline bookkeeping for a future same-session resume.
        """

        restored: list[RunContext] = []
        for row in _persisted_waiting_runs(self.db, self.ticket or None):
            run_id = str(row.get("id") or row.get("run_id") or "").strip()
            if not run_id or run_id in self.contexts:
                continue
            persisted_worktree = row.get("worktree")
            worktree = self.worktree
            if persisted_worktree:
                candidate = Path(str(persisted_worktree)).expanduser().resolve()
                if candidate.is_dir():
                    worktree = candidate
            try:
                duration = float(row.get("deadline_seconds") or self.deadline_seconds)
            except (TypeError, ValueError):
                duration = self.deadline_seconds
            context = RunContext(
                run_id=run_id,
                ticket=str(row.get("ticket") or self.ticket),
                worktree=worktree,
                deadline=Deadline(duration, started_at=self.clock(), clock=self.clock),
                session_id=str(row.get("session_id")) if row.get("session_id") else None,
                paused=bool(row.get("paused_at")),
                last_activity=self.clock(),
            )
            # Waiting time belongs to the external review/CI system, not the
            # implementation deadline.  The persisted pause accounting is
            # retained for diagnostics and for callers that inspect it.
            try:
                context.deadline.paused_seconds = float(row.get("paused_seconds") or 0.0)
            except (TypeError, ValueError):
                context.deadline.paused_seconds = 0.0
            self.contexts[run_id] = context
            restored.append(context)
        return restored

    restore_waiting_contexts = hydrate_waiting_contexts

    def _record_insert(self, context: RunContext) -> None:
        started = utc_now()
        _insert_run(
            self.db,
            {
                "id": context.run_id,
                "ticket": context.ticket,
                "role": self.role,
                "worktree": str(context.worktree),
                "pid": context.pid,
                "pgid": context.pgid,
                "status": RunState.RUNNING.value,
                "waiting_on": None,
                "deadline_at": (started + timedelta(seconds=context.deadline.duration_seconds)).isoformat(timespec="seconds"),
                "deadline_seconds": context.deadline.duration_seconds,
                "paused_seconds": 0.0,
                "paused_at": None,
                "last_activity_at": iso_now(),
                "session_id": context.session_id,
                "started_at": started.isoformat(timespec="seconds"),
                "terminal_reason": None,
            },
        )

    def _queue_transition(self, context: RunContext, status: str, *, error: str | None = None) -> None:
        """Persist the queue receipt associated with this ticket/run.

        Queue ownership lives in the database, while runner implementations
        own the actual process.  Keeping this adapter here means every runner
        (including a direct ``BaseRunner`` test seam) advances the same row at
        process start, result, and spawn failure.
        """

        if self.db is None or not self.ticket:
            return
        method = getattr(self.db, "transition_ticket_queue", None)
        if callable(method):
            with contextlib.suppress(Exception):
                method(self.ticket, status, run_id=context.run_id, error=error)
            return
        connection = _db_connection(self.db)
        if connection is None or not _table_exists(connection, "work_queue"):
            return
        columns = {row[1] for row in connection.execute("PRAGMA table_info(work_queue)")}
        updates = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
        values: list[Any] = [status]
        if "run_id" in columns:
            updates.append("run_id = ?")
            values.append(context.run_id)
        if error is not None and "error" in columns:
            updates.append("error = ?")
            values.append(error)
        values.append(self.ticket)
        with _db_lock(self.db):
            connection.execute(
                f"UPDATE work_queue SET {', '.join(updates)} WHERE id = ("
                "SELECT id FROM work_queue WHERE ticket = ? AND status IN "
                "('queued', 'dispatched', 'running', 'waiting') ORDER BY id DESC LIMIT 1)",
                values,
            )
            connection.commit()

    def _touch(self, context: RunContext, event: StreamEvent) -> None:
        context.last_activity = self.clock()
        if event.session_id and context.expected_session_id and event.session_id != context.expected_session_id:
            context.session_mismatch = True
            return
        if event.session_id:
            context.session_id = event.session_id
            _update_run_sql(self.db, context.run_id, {"session_id": context.session_id})
        if event.cost_usd is not None:
            context.cost_usd = max(context.cost_usd, event.cost_usd)
        _call_store(self.db, ("touch_activity", "update_run_activity"), context.run_id, event)
        _update_run_sql(
            self.db,
            context.run_id,
            {"last_activity_at": iso_now(), "cost_usd": context.cost_usd, "session_id": context.session_id},
        )

    async def _drain(self, context: RunContext) -> None:
        process = context.process
        if process is None or process.stdout is None:
            return
        async for event in iter_stream(process.stdout, on_event=lambda item: self._touch(context, item)):
            context.events.append(event)
            if event.session_id and not context.session_mismatch:
                context.session_id = event.session_id
            if event.cost_usd is not None:
                context.cost_usd = max(context.cost_usd, event.cost_usd)

    async def _deadline_watch(self, context: RunContext) -> None:
        while context.process is not None and context.process.returncode is None:
            if context.deadline.is_paused:
                context.pause_changed.clear()
                await context.pause_changed.wait()
                continue
            remaining = context.deadline.remaining()
            if remaining <= 0:
                context.deadline_expired = True
                await self.terminate(context, reason="deadline exceeded")
                return
            context.pause_changed.clear()
            try:
                await asyncio.wait_for(context.pause_changed.wait(), timeout=min(remaining, 0.25))
            except asyncio.TimeoutError:
                continue

    async def _execute(self, context: RunContext, instruction: str, *, resume: bool) -> RunResult:
        from harbor.recovery import retained_budget
        connection = _db_connection(self.db)
        row = None
        if resume and connection is not None:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (context.run_id,)).fetchone()
        remaining = retained_budget(dict(row), self.deadline_seconds) if row is not None else context.deadline.remaining()
        context.deadline = Deadline(remaining, started_at=self.clock(), clock=self.clock)
        if remaining <= 0:
            _update_run_sql(self.db, context.run_id, {"status": "failed", "terminal_reason": "deadline exceeded"})
            return self._failure(context, "deadline exceeded", status="failed")
        # Each invocation must prove its own result; never reuse a prior final.
        context.events.clear()
        context.expected_session_id = context.session_id if resume else None
        context.session_mismatch = False
        if connection is not None and hasattr(self, "model"):
            from harbor.runner.quota import bind_runner_identity
            bind_runner_identity(
                connection, context.run_id, getattr(self, "provider", "codex"),
                getattr(self, "primary_model", self.model), self.reasoning_effort,
            )
        self._queue_transition(context, "dispatched")
        try:
            command = self.command(instruction, run_id=context.session_id or context.run_id, resume=resume)
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(context.worktree),
                env=self.environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                # A single Codex event carrying a large diff or reasoning
                # payload easily exceeds asyncio's 64 KiB default.  iter_stream
                # also survives an over-long line, but raising the buffer keeps
                # the common case a plain readline.
                limit=STREAM_BUFFER_LIMIT,
            )
        except OSError as exc:
            context.terminated = True
            self._queue_transition(context, "failed", error=f"could not start builder: {exc}")
            return self._failure(context, f"could not start builder: {exc}", status=RunState.FAILED.value)
        except Exception as exc:
            context.terminated = True
            self._queue_transition(context, "failed", error=f"could not prepare builder: {exc}")
            return self._failure(context, f"could not prepare builder: {exc}", status=RunState.FAILED.value)
        context.process = process
        context.pid = process.pid
        with contextlib.suppress(ProcessLookupError):
            context.pgid = os.getpgid(process.pid)
        self.active = context
        try:
            self._record_insert(context)
        except Exception:
            await self.terminate(context, reason="ledger write failed")
            await process.wait()
            context.process = None
            self.active = None
            raise
        self._queue_transition(context, "running")
        _update_run_sql(self.db, context.run_id, {"pid": context.pid, "pgid": context.pgid, "session_id": context.session_id})
        if process.stdin is not None:
            try:
                process.stdin.write(instruction.encode("utf-8"))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # The builder exited, or closed its stdin, before taking the
                # prompt.  That is the builder's failure, not Harbor's: its
                # output and exit status are still read below and recorded
                # as this run's result.
                pass
            finally:
                process.stdin.close()

        drain_task = asyncio.create_task(self._drain(context), name=f"harbor-stream-{context.run_id}")
        deadline_task = asyncio.create_task(self._deadline_watch(context), name=f"harbor-deadline-{context.run_id}")
        try:
            await drain_task
            exit_code = await process.wait()
        except asyncio.CancelledError:
            await self.terminate(context, reason="operator exit")
            self._queue_transition(context, "failed", error="operator exit")
            context.process = None
            if self.active is context:
                self.active = None
            raise
        finally:
            deadline_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await deadline_task
            if not drain_task.done():
                drain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain_task
        context.process = None
        if self.active is context:
            self.active = None

        final = extract_final(context.events)
        from harbor.model_policy import rejected_events
        model_unavailable = final is None and rejected_events(context.events)
        if not context.terminated and not context.deadline_expired and (final is None or exit_code != 0):
            _update_run_sql(self.db, context.run_id, {"terminal_reason": "model_unavailable" if model_unavailable else "unexpected_exit"})
        quota = False
        reset = None
        if not (context.session_mismatch or context.terminated or context.deadline_expired) and (final is None or exit_code != 0):
            from harbor.runner.quota import quota_error
            for event in reversed(context.events):
                confirmed, timestamp = quota_error(event.data or {}, utc_now())
                quota |= confirmed
                if confirmed and timestamp is not None:
                    reset = timestamp
                    break
        if quota and connection is not None:
            from harbor.recovery import defer_quota, record_provider_quota
            now = utc_now().timestamp()
            if reset is not None and reset <= now:
                reset = None
            defer_quota(connection, context.run_id, getattr(self, "provider", "codex"), reset, now)
            record_provider_quota(connection, getattr(self, "provider", "codex"), reset, now)
            _update_run_sql(self.db, context.run_id, {"terminal_reason": "usage_limit_exceeded"})
        if context.session_mismatch:
            _update_run_sql(self.db, context.run_id, {"terminal_reason": "session_mismatch"})
            result = self._failure(context, "resume returned a different native session", status=RunState.FAILED.value, exit_code=exit_code)
        elif context.deadline_expired:
            result = self._failure(context, "deadline exceeded", status=RunState.FAILED.value, exit_code=exit_code)
        elif context.terminated:
            result = self._failure(context, "builder process terminated", status=RunState.TERMINATED.value, exit_code=exit_code)
        elif quota:
            from harbor.runner.quota import USAGE_LIMIT_ERROR
            result = self._failure(context, USAGE_LIMIT_ERROR, status=RunState.FAILED.value, exit_code=exit_code)
        elif model_unavailable:
            result = self._failure(context, "configured model unavailable; configure an authorized fallback", status=RunState.FAILED.value, exit_code=exit_code)
        elif final is None or exit_code != 0:
            result = self._failure(context, "builder exited without a schema-valid final result", status=RunState.FAILED.value, exit_code=exit_code)
        else:
            try:
                result = RunResult.from_payload(
                    final,
                    cost_usd=context.cost_usd or None,
                    exit_code=exit_code,
                    run_id=context.run_id,
                    session_id=context.session_id,
                    observed=self._observed_facts(context),
                    known_pr_url=self._known_pr_url(context),
                )
            except ResultValidationError as exc:
                result = self._failure(context, str(exc), status=RunState.FAILED.value, exit_code=exit_code)
            else:
                if result.pr_url:
                    self.run_pr_urls[context.run_id] = str(result.pr_url)
        status = result.status if result.status in RUN_STATUSES else RunState.FAILED.value
        waiting_on = result.waiting_on or "none"
        _call_store(self.db, ("finish_run", "complete_run"), context.run_id, status=status, waiting_on=waiting_on, final_json=result.final_json, cost_usd=context.cost_usd)
        _update_run_sql(
            self.db,
            context.run_id,
            {
                "status": status,
                "waiting_on": waiting_on,
                "final_json": result.final_json,
                "cost_usd": context.cost_usd,
                "finished_at": iso_now(),
                "paused_seconds": context.deadline.paused_seconds,
                "deadline_seconds": context.deadline.remaining(),
                "session_id": context.session_id,
            },
        )
        if not quota and connection is not None and _table_exists(connection, "quota_recovery"):
            with connection:
                connection.execute("DELETE FROM quota_recovery WHERE run_id=?", (context.run_id,))
        if not quota and final is not None and exit_code == 0 and connection is not None:
            # A schema-valid final result proves this provider is usable again.
            from harbor.recovery import clear_provider_quota
            clear_provider_quota(connection, getattr(self, "provider", "codex"))
        queue_status = "waiting" if status == RunState.WAITING.value else "completed" if status == RunState.COMPLETED.value else "failed"
        self._queue_transition(context, queue_status, error=result.error)
        if (
            status == RunState.COMPLETED.value
            and result.outcome == "merged"
            and result.merge_verified is True
        ):
            await self.cleanup_after_merge(context, result)
        return result

    def _failure(self, context: RunContext, summary: str, *, status: str, exit_code: int | None = None) -> RunResult:
        return RunResult(
            status=status,
            summary=summary,
            waiting_on="none",
            cost_usd=context.cost_usd or None,
            exit_code=exit_code,
            run_id=context.run_id,
            session_id=context.session_id,
            error=summary,
        )

    async def start(self, instruction: str, *, run_id: str | None = None) -> RunResult:
        context = self._new_context(run_id)
        return await self._execute(context, instruction, resume=False)

    run = start

    async def cleanup_after_merge(self, context: RunContext, result: RunResult) -> None:
        """Invoke the optional post-merge cleanup seam exactly once per result."""

        hook = self.cleanup_hook
        if hook is None:
            return
        try:
            value = hook(context, result)
        except TypeError:
            # Small embedders often need only the worktree or result.  Keep
            # those adapters explicit while avoiding broad ``*args`` calls.
            try:
                value = hook(context.worktree, result)
            except TypeError:
                value = hook(context.worktree)
        if inspect.isawaitable(value):
            await value

    post_merge_cleanup = cleanup_after_merge

    async def pause(self, run_id: str | None = None) -> None:
        context = self._context(run_id)
        context.paused = True
        context.deadline.pause()
        context.pause_changed.set()
        _update_run_sql(self.db, context.run_id, {"paused_at": iso_now(), "paused_seconds": context.deadline.paused_seconds})

    async def resume_pause(self, run_id: str | None = None) -> None:
        context = self._context(run_id)
        context.paused = False
        context.deadline.resume()
        context.pause_changed.set()
        _update_run_sql(self.db, context.run_id, {"paused_at": None, "paused_seconds": context.deadline.paused_seconds})

    resume_deadline = resume_pause

    def _context(self, run_id: str | None = None) -> RunContext:
        if run_id and run_id in self.contexts:
            return self.contexts[run_id]
        if self.active is not None:
            return self.active
        if run_id:
            raise RunnerError(f"unknown run: {run_id}")
        raise RunnerError("no active run")

    async def terminate(self, context: RunContext | None = None, *, reason: str = "terminated", grace_seconds: float = 1.0) -> None:
        context = context or self.active
        if context is None:
            return
        context.terminated = True
        if context.pid:
            signal_process_group(context.pid, context.pgid, sig=signal.SIGTERM)
        process = context.process
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=max(0.05, grace_seconds))
            except asyncio.TimeoutError:
                if context.pid:
                    signal_process_group(context.pid, context.pgid, sig=signal.SIGKILL)
                with contextlib.suppress(ProcessLookupError):
                    await process.wait()
        _update_run_sql(self.db, context.run_id, {"terminal_reason": reason, "status": "terminated", "finished_at": iso_now(), "deadline_seconds": context.deadline.remaining()})

    kill = terminate
    stop = terminate


def tracked_runs(db: Any) -> list[dict[str, Any]]:
    result = _call_store(db, ("running_runs", "active_runs"))
    if result is not None:
        return [dict(item) for item in result]
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "runs"):
        return []
    rows = connection.execute("SELECT * FROM runs WHERE status = 'running'").fetchall()
    return [dict(row) for row in rows]


def _run_in_ticket_scope(ticket: Any, scope: Any | None) -> bool:
    """Match one persisted run without importing Harbor's wake layer."""

    if scope is None:
        return True
    matcher = getattr(scope, "matches", None)
    if callable(matcher):
        return bool(matcher(str(ticket or "")))
    selector = str(scope or "").strip().casefold()
    return bool(selector and str(ticket or "").strip().casefold() == selector)


async def reap_orphan_runs(
    db: Any,
    *,
    terminate_alive: bool = True,
    grace_seconds: float = 1.0,
    ticket_scope: Any | None = None,
) -> list[str]:
    """Mark persisted running runs as orphaned after a Harbor restart.

    A restart cannot safely assume a process belongs to the newly-created
    supervisor, so an extant process group is terminated before its row is
    closed.  This closes the duplicate-dispatch window.  When a supervisor
    supplies ``ticket_scope``, only that exact invocation-local ticket is
    touched; omitting it preserves the historical all-runs behavior.
    """

    orphaned: list[str] = []
    for row in tracked_runs(db):
        run_id = str(row.get("id") or row.get("run_id") or "")
        if not _run_in_ticket_scope(row.get("ticket"), ticket_scope):
            continue
        pid = int(row["pid"]) if row.get("pid") else None
        pgid = int(row["pgid"]) if row.get("pgid") else None
        if pid and process_alive(pid) and terminate_alive:
            signal_process_group(pid, pgid, sig=signal.SIGTERM)
            deadline = time.monotonic() + max(0.05, grace_seconds)
            while process_group_alive(pgid) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            if process_group_alive(pgid):
                signal_process_group(pid, pgid, sig=signal.SIGKILL)
        if run_id:
            changed = _update_run_sql(
                db,
                run_id,
                {
                    "status": RunState.ORPHANED.value,
                    "waiting_on": "none",
                    "finished_at": iso_now(),
                    "terminal_reason": "Harbor restarted while run was active",
                },
                expected_status=RunState.RUNNING.value,
            )
            if changed is None:
                # Stores without a SQLite connection cannot offer an atomic
                # compare-and-set.  Preserve their adapter compatibility, but
                # still refuse to overwrite an already-finished row.
                current = _run_row(db, run_id)
                if current is None or str(current.get("status") or "").casefold() != RunState.RUNNING.value:
                    continue
                _call_store(
                    db,
                    ("finish_run", "mark_run_status"),
                    run_id,
                    status=RunState.ORPHANED.value,
                    waiting_on="none",
                    final_json=None,
                )
            elif changed != 1:
                # The runner completed (or another reaper won) while this
                # process was waiting for termination.  Never overwrite that
                # newer lifecycle result with a stale orphan marker.
                continue
            else:
                _call_store(
                    db,
                    ("finish_run", "mark_run_status"),
                    run_id,
                    status=RunState.ORPHANED.value,
                    waiting_on="none",
                    final_json=None,
                )
            _call_store(
                db, ("transition_queue",), "failed",
                idem_key=f"{row['ticket']}@retry@{run_id}",
                error="Harbor restarted while retry was active",
            )
            orphaned.append(run_id)
    return orphaned


detect_orphans = tracked_runs
reap_orphans = reap_orphan_runs


__all__ = [
    "BaseRunner",
    "BLOCKER_KINDS",
    "Deadline",
    "OUTCOMES",
    "ResultValidationError",
    "RunContext",
    "RunResult",
    "RunState",
    "RunnerError",
    "apply_phase4_migration",
    "detect_orphans",
    "iso_now",
    "kill_process_group",
    "killpg",
    "process_alive",
    "process_group_alive",
    "reap_orphan_runs",
    "reap_orphans",
    "signal_process_group",
    "terminate_process_group",
    "tracked_runs",
    "validate_final_result",
    "validate_result",
]
