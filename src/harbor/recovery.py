"""Bounded recovery receipts. External ticket state is always checked by the caller."""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


@dataclass(frozen=True)
class BlockerPolicy:
    retry_delays: tuple[float, ...] = (30, 120, 300)
    decision_mode: str = "bounded"
    unblock_attempts: int = 2
    authorized_authors: tuple[str, ...] = ()

    @classmethod
    def parse(cls, values: Mapping[str, Any]) -> "BlockerPolicy":
        delays = values.get("recovery_delays", "30,120,300")
        try:
            delays = tuple(float(x.strip()) for x in str(delays).strip("[]").split(",") if x.strip())
            attempts = int(values.get("unblock_attempts", 2))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid blocker recovery policy") from exc
        mode = str(values.get("firstmate_decisions", "bounded"))
        if (len(delays) > 10 or any(not math.isfinite(x) or x < 0 for x in delays)
                or attempts < 0 or attempts > 10 or mode not in {"bounded", "cite_only", "human"}):
            raise ValueError("invalid blocker recovery policy")
        authors = tuple(x.strip() for x in str(values.get("decision_authors", "")).strip("[]").split(",") if x.strip())
        return cls(delays, mode, attempts, authors)


def recovery_schema(connection: Any) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS recovery_receipts (
        run_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0,
        next_retry REAL NOT NULL, last_reason TEXT NOT NULL,
        notified INTEGER NOT NULL DEFAULT 0)""")
    connection.commit()


def recoverable(row: Mapping[str, Any]) -> bool:
    # Explicit operator stops and unknown legacy failures fail closed. Only
    # positive evidence of an accidental exit permits automatic recovery.
    return (row.get("status") in {"failed", "orphaned"}
            and row.get("terminal_reason") in {
                "unexpected_exit", "Harbor restarted while run was active"}
            and bool(row.get("session_id")) and bool(row.get("worktree"))
            and not row.get("paused_at"))


def claim_retry(connection: Any, row: Mapping[str, Any], policy: BlockerPolicy, now: float) -> bool:
    """Atomic budget reservation before dispatch, retained through restarts."""
    if not recoverable(row) or not policy.retry_delays:
        return False
    recovery_schema(connection)
    run_id = row["id"]
    with connection:
        connection.execute(
            "INSERT OR IGNORE INTO recovery_receipts(run_id,next_retry,last_reason) VALUES (?,?,?)",
            (run_id, now + policy.retry_delays[0], row["terminal_reason"]),
        )
        receipt = connection.execute(
            "SELECT attempts,next_retry FROM recovery_receipts WHERE run_id=?", (run_id,)
        ).fetchone()
        attempts, due = receipt
        if attempts >= len(policy.retry_delays) or now < due:
            return False
        next_delay = policy.retry_delays[min(attempts + 1, len(policy.retry_delays) - 1)]
        cursor = connection.execute(
            "UPDATE recovery_receipts SET attempts=attempts+1,next_retry=?,last_reason=? "
            "WHERE run_id=? AND attempts=? AND next_retry=?",
            (now + max(next_delay, 0.001), row["terminal_reason"], run_id, attempts, due),
        )
        return cursor.rowcount == 1


def retained_budget(row: Mapping[str, Any], default: float) -> float:
    """Terminal rows carry remaining active seconds; orphans use wall deadline.

    Downtime for an unclean shutdown counts against the budget conservatively.
    Waiting/review time does not consume an execution budget.
    """
    remaining = float(row.get("deadline_seconds") if row.get("deadline_seconds") is not None else default)
    if row.get("status") in {"running", "orphaned"} and row.get("deadline_at"):
        remaining = min(remaining, datetime.fromisoformat(row["deadline_at"]).timestamp() - datetime.now().timestamp())
    return max(0.0, remaining)


def recovery_instruction(ticket: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    try:
        final = json.loads(row.get("final_json") or "{}")
    except (ValueError, TypeError):
        final = {}
    next_action = final.get("next_action") or "Inspect git status and the last failed command, then continue the ticket acceptance criteria."
    return (f"Recover ticket {ticket['identifier']} in retained session {row['session_id']} "
            f"and worktree {row['worktree']}. Stop reason: {row['terminal_reason']}.\n"
            f"Next step: {next_action}\n"
            "Re-read current Linear and GitHub facts; reconcile partial edits and existing PRs before acting. "
            "Do not create another session, worktree or PR. Report progress or a structured blocker; "
            "resuming alone is not proof the blocker is resolved.")


def interrupted_retry_instruction(connection: Any, ticket: str, row: Mapping[str, Any]) -> str | None:
    """Read the exact operator retry that quota interrupted, from its queue receipt."""
    try:
        receipt = connection.execute(
            """SELECT payload_json,status,error,updated_at FROM work_queue WHERE ticket=? AND idem_key=?
            ORDER BY id DESC LIMIT 1""",
            (ticket, f"{ticket}@retry@{row['id']}"),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    from harbor.runner.quota import USAGE_LIMIT_ERROR
    if not receipt or receipt[1:3] != ("failed", USAGE_LIMIT_ERROR):
        return None
    try:
        receipt_at = datetime.fromisoformat(receipt[3]).replace(tzinfo=timezone.utc)
        finished_at = datetime.fromisoformat(row["finished_at"])
        if abs((receipt_at - finished_at).total_seconds()) > 30:
            return None
        payload = json.loads(receipt[0])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("run_id") != row["id"] or payload.get("session_id") != row.get("session_id"):
        return None
    instruction = payload.get("instruction")
    return instruction if isinstance(instruction, str) and instruction.strip() else None


def quota_schema(connection: Any) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS quota_recovery (
        run_id TEXT PRIMARY KEY, provider TEXT NOT NULL, next_retry REAL NOT NULL,
        confirmed INTEGER NOT NULL, failures INTEGER NOT NULL)""")


def defer_quota(connection: Any, run_id: str, provider: str, reset: float | None, now: float) -> None:
    quota_schema(connection)
    previous = connection.execute("SELECT failures FROM quota_recovery WHERE run_id=?", (run_id,)).fetchone()
    failures = previous[0] + 1 if previous else 1
    delay = (300, 900, 1800)[min(failures - 1, 2)]
    confirmed_reset = reset is not None and reset > now
    next_retry = reset if confirmed_reset else now + delay
    with connection:
        connection.execute(
            """INSERT INTO quota_recovery VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET provider=excluded.provider,
            next_retry=excluded.next_retry, confirmed=excluded.confirmed, failures=excluded.failures""",
            (run_id, provider, next_retry, int(confirmed_reset), failures),
        )


def claim_quota_retry(connection: Any, run_id: str, now: float, *, triage: bool = False) -> bool:
    with connection:
        # Reserve a short retry window in case the supervisor exits before the
        # retained dispatcher can start. A new quota result replaces this time.
        result = connection.execute(
            """UPDATE quota_recovery SET next_retry=?, confirmed=0
            WHERE run_id=? AND next_retry<=?
            AND EXISTS (SELECT 1 FROM runs WHERE id=? AND status=?
                        AND (? OR terminal_reason='usage_limit_exceeded'))""",
            (now + 300, "triage:" + run_id if triage else run_id, now,
             run_id, "blocked" if triage else "failed", triage),
        )
        return result.rowcount == 1


def pr_event_schema(connection: Any) -> None:
    """Bounded delivery receipts for PR events whose builder handoff failed."""
    connection.execute("""CREATE TABLE IF NOT EXISTS pr_event_attempts (
        event_key TEXT PRIMARY KEY, attempts INTEGER NOT NULL,
        next_attempt REAL NOT NULL, exhausted INTEGER NOT NULL DEFAULT 0)""")


def pr_event_due(connection: Any, event_key: str, now: float) -> bool:
    pr_event_schema(connection)
    row = connection.execute(
        "SELECT next_attempt, exhausted FROM pr_event_attempts WHERE event_key=?", (event_key,)
    ).fetchone()
    return row is None or (not row[1] and now >= row[0])


def record_pr_event_failure(connection: Any, event_key: str, delays: tuple[float, ...], now: float) -> tuple[int, bool]:
    """Count one failed handoff; return ``(attempts, exhausted)``.

    ``delays`` is the wait before each retry, so a failing event is offered
    ``len(delays) + 1`` times in all. An empty policy stops after the first.
    """
    pr_event_schema(connection)
    with connection:
        row = connection.execute("SELECT attempts FROM pr_event_attempts WHERE event_key=?", (event_key,)).fetchone()
        attempts = (row[0] if row else 0) + 1
        exhausted = attempts > len(delays)
        delay = 0.0 if exhausted else max(float(delays[attempts - 1]), 0.0)
        connection.execute(
            """INSERT INTO pr_event_attempts VALUES (?, ?, ?, ?)
            ON CONFLICT(event_key) DO UPDATE SET attempts=excluded.attempts,
            next_attempt=excluded.next_attempt, exhausted=excluded.exhausted""",
            (event_key, attempts, now + delay, int(exhausted)),
        )
    return attempts, exhausted


def clear_pr_event(connection: Any, *event_keys: str) -> None:
    if not event_keys:
        return
    pr_event_schema(connection)
    with connection:
        connection.executemany("DELETE FROM pr_event_attempts WHERE event_key=?", [(key,) for key in event_keys])
