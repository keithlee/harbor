"""Normalize native CLI quota errors without retaining provider messages."""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

USAGE_LIMIT = "usage_limit_exceeded"
# The run-result error Harbor records when a provider usage limit ends a run.
USAGE_LIMIT_ERROR = "Provider usage limit reached"


def _reset(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed.timestamp() if parsed.tzinfo else None
        except (ValueError, OverflowError, OSError):
            return None
    return timestamp if math.isfinite(timestamp) and 0 < timestamp < 253402300799 else None


def _message_reset(message: str, observed: datetime) -> float | None:
    match = re.search(
        r"try again at (?:(?P<date>[A-Za-z]{3} \d{1,2})(?:st|nd|rd|th)?, (?P<year>\d{4}) )?"
        r"(?P<clock>\d{1,2}:\d{2} [AP]M)(?:\.|$)", message, re.I,
    )
    if not match:
        return None
    try:
        clock = datetime.strptime(match["clock"].upper(), "%I:%M %p").time()
        local = datetime.fromtimestamp(observed.timestamp())
        day = datetime.strptime(f"{match['date']} {match['year']}", "%b %d %Y").date() if match["date"] else local.date()
        target = datetime.combine(day, clock)
        if not match["date"] and target < local:
            target += timedelta(days=1)
        # A clock during a DST transition can represent zero or two instants.
        early, late = target.replace(fold=0).timestamp(), target.replace(fold=1).timestamp()
        if early != late or datetime.fromtimestamp(early) != target:
            return None
        return early
    except (ValueError, OverflowError, OSError):
        return None


def quota_error(event: Mapping[str, Any], observed: datetime, *, nested: bool = False) -> tuple[bool, float | None]:
    kind = event.get("type")
    if kind not in {"error", "turn.failed", "task_complete", "rate_limit_event", "result"}:
        return False, None
    error = event.get("error")
    fields = error if isinstance(error, Mapping) else event
    info = event.get("rate_limit_info")
    if kind == "rate_limit_event" and isinstance(info, Mapping) and info.get("status") == "rejected":
        return True, _reset(info.get("resetsAt"))
    message = fields.get("message")
    message = message if isinstance(message, str) else ""
    if not nested and kind in {"error", "turn.failed"} and message.lstrip().startswith("{"):
        try:
            parsed = json.loads(message)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, Mapping):
            limited, reset = quota_error({"type": "error", **parsed}, observed, nested=True)
            if limited:
                return limited, reset
    confirmed = any(fields.get(key) == USAGE_LIMIT for key in ("code", "type", "codex_error_info"))
    # Codex exec sometimes flattens the provider error into its error message.
    confirmed |= kind in {"error", "turn.failed"} and message.startswith(("You’ve hit your usage limit.", "You've hit your usage limit."))
    if not confirmed:
        return False, None
    for key in ("resets_at", "reset_at", "resetsAt"):
        timestamp = _reset(fields.get(key))
        if timestamp is not None:
            return True, timestamp
    return True, _message_reset(message, observed)


def legacy_codex_quota(row: Mapping[str, Any]) -> tuple[bool, float | None]:
    """Match the last completed native turn to the recorded failed run."""
    try:
        session = str(UUID(str(row["session_id"])))
        finished = datetime.fromisoformat(row["finished_at"])
    except (KeyError, ValueError, TypeError):
        return False, None
    directory = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    matches = [path for path in directory.glob(f"*/*/*/rollout-*-{session}.jsonl") if path.is_file()]
    if len(matches) != 1:
        return False, None
    latest: Mapping[str, Any] = {}
    try:
        with matches[0].open(encoding="utf-8") as stream:
            for line in stream:
                if '"task_complete"' not in line:
                    continue
                event = json.loads(line)
                payload = event.get("payload")
                if event.get("type") == "event_msg" and isinstance(payload, Mapping) and payload.get("type") == "task_complete":
                    latest = payload
        completed = _reset(latest.get("completed_at"))
        if completed is None or abs(finished.timestamp() - completed) > 30:
            return False, None
        return quota_error(latest, datetime.fromtimestamp(completed).astimezone())
    except (OSError, ValueError, AttributeError):
        return False, None


def runner_identity(connection: Any, run_id: str) -> Any:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runner_identity'"
    ).fetchone():
        return None
    return connection.execute(
        "SELECT provider, model, effort FROM runner_identity WHERE run_id=?", (run_id,)
    ).fetchone()


def bind_runner_identity(connection: Any, run_id: str, provider: str, model: str, effort: str) -> None:
    with connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS runner_identity (
            run_id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL)""")
        connection.execute("INSERT OR IGNORE INTO runner_identity VALUES (?, ?, ?, ?)", (run_id, provider, model, effort))
        identity = runner_identity(connection, run_id)
        if identity[0] != provider:
            raise ValueError("configured runner does not match the retained session provider")
