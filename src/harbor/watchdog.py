"""Progress watchdog: a separate, read-mostly process for one project.

``harbor watch PROJECT`` reads the supervisor heartbeat file and the SQLite
ledger and flags:

* ``no_progress``: an active run with no new commit and no new receipt for N minutes;
* ``repeated_answer``: the same question answered, or the same comment re-delivered, again and again;
* ``cleanup_stuck``: a merged/completed run whose worktree still exists after N minutes;
* ``supervisor_stale``: the supervisor heartbeat stopped.

Flags are written as ``events`` rows (``event_type = 'watchdog.flag'``) — no
schema migration — and First Mate handles them on the supervisor's next poll.
The watchdog never touches worktrees, Linear or GitHub.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harbor.safe_git import git_argv

ACTIVE = ("running", "waiting", "starting")
REPEAT_LIMIT = 3


def initialize(connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS watchdog_state (key TEXT PRIMARY KEY, value TEXT, changed_at REAL NOT NULL)")


def _head(worktree: str | None) -> str:
    if not worktree or not Path(worktree).is_dir():
        return ""
    result = subprocess.run(git_argv("-C", worktree, "rev-parse", "HEAD"), capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def _ts(value: Any) -> float | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()


def _state(connection, key: str) -> tuple[str | None, float | None]:
    row = connection.execute("SELECT value, changed_at FROM watchdog_state WHERE key=?", (key,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _set(connection, key: str, value: str, at: float) -> None:
    connection.execute("INSERT INTO watchdog_state(key,value,changed_at) VALUES (?,?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value, changed_at=excluded.changed_at", (key, value, at))


def _flag_once(connection, project: str, ticket: str | None, kind: str, key: str, detail: str, run_id: str | None, now: float) -> dict | None:
    marker = f"flagged:{kind}:{key}"
    if _state(connection, marker)[0] is not None:
        return None
    _set(connection, marker, "1", now)
    flag = {"kind": kind, "detail": detail, "run_id": run_id}
    connection.execute(
        "INSERT INTO events(project, identifier, event_type, payload_json, snapshot_hash, at) VALUES (?,?,?,?,?,?)",
        (project, ticket, "watchdog.flag", json.dumps(flag, sort_keys=True), None,
         datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")),
    )
    return {**flag, "ticket": ticket}


def _receipts(connection, ticket: str, run_id: str) -> int:
    total = 0
    for sql, args in (("SELECT COUNT(*) FROM workflow_receipts WHERE run_id=?", (run_id,)),
                      ("SELECT COUNT(*) FROM events WHERE identifier=? AND event_type != 'watchdog.flag'", (ticket,)),
                      ("SELECT COUNT(*) FROM reengagement_events WHERE ticket=?", (ticket,))):
        try:
            total += int(connection.execute(sql, args).fetchone()[0])
        except Exception:
            continue
    return total


def tick(connection, project: str, *, minutes: float = 30, heartbeat_path: Path | None = None,
         heartbeat_seconds: float = 60, now: float | None = None) -> list[dict]:
    """One watchdog pass. Returns the new flags it wrote."""
    initialize(connection)
    now = time.time() if now is None else now
    limit = minutes * 60
    flags: list[dict] = []
    with connection:
        try:
            runs = connection.execute(
                """SELECT r.id, r.ticket, r.status, r.worktree, r.finished_at, r.merge_verified, r.cleanup_at, r.final_json
                   FROM runs r JOIN tickets t ON t.identifier = r.ticket WHERE t.project = ?""", (project,)
            ).fetchall()
        except Exception:  # a ledger the supervisor has not migrated yet
            runs = []
        for run_id, ticket, status, worktree, finished, merged, cleanup_at, final_json in runs:
            status = str(status or "").casefold()
            if status in ACTIVE:
                signature = f"{_head(worktree)}|{_receipts(connection, ticket, run_id)}"
                value, changed = _state(connection, f"progress:{run_id}")
                if value != signature:
                    _set(connection, f"progress:{run_id}", signature, now)
                elif changed is not None and now - changed >= limit:
                    flag = _flag_once(connection, project, ticket, "no_progress", f"{run_id}:{signature}",
                                      f"no new commit or receipt for {int((now - changed) // 60)} minutes", run_id, now)
                    flags += [flag] if flag else []
            try:
                outcome = json.loads(final_json or "{}").get("outcome")
            except (TypeError, ValueError, AttributeError):
                outcome = None
            done = status == "completed" and (merged == 1 or outcome == "merged")
            ended = _ts(finished)
            if done and not cleanup_at and worktree and Path(worktree).is_dir() and ended and now - ended >= limit:
                # Re-flag hourly while it stays stuck, so a failed cleanup is retried.
                flag = _flag_once(connection, project, ticket, "cleanup_stuck", f"{run_id}:{int(now // 3600)}",
                                  f"merged run's worktree {worktree} still present after {int((now - ended) // 60)} minutes", run_id, now)
                flags += [flag] if flag else []
        try:
            repeated = connection.execute(
                """SELECT e.ticket, e.question, COUNT(*) FROM escalations e JOIN tickets t ON t.identifier = e.ticket
                   WHERE t.project = ? AND e.status IN ('answered','resolved','superseded') GROUP BY e.ticket, e.question
                   HAVING COUNT(*) >= ?""", (project, REPEAT_LIMIT)
            ).fetchall()
        except Exception:
            repeated = []
        for ticket, question, count in repeated:
            flag = _flag_once(connection, project, ticket, "repeated_answer", f"{ticket}:{hashlib.sha256(str(question).encode()).hexdigest()[:16]}:{count // REPEAT_LIMIT}",
                              f"the same question was answered {count} times: {str(question)[:300]}", None, now)
            flags += [flag] if flag else []
        try:
            rows = connection.execute(
                """SELECT r.ticket, r.payload_json FROM reengagement_events r JOIN tickets t ON t.identifier = r.ticket
                   WHERE t.project = ?""", (project,)
            ).fetchall()
        except Exception:
            rows = []
        seen: dict[tuple[str, str], int] = {}
        for ticket, payload in rows:
            try:
                data = json.loads(payload or "{}")
            except (TypeError, ValueError):
                continue
            comment = str(data.get("comment_id") or data.get("comment", {}).get("id", "") if isinstance(data, dict) else "")
            if comment:
                seen[(ticket, comment)] = seen.get((ticket, comment), 0) + 1
        for (ticket, comment), count in seen.items():
            if count >= REPEAT_LIMIT:
                flag = _flag_once(connection, project, ticket, "repeated_answer", f"{ticket}:comment:{comment}:{count // REPEAT_LIMIT}",
                                  f"comment {comment} was delivered to the builder {count} times", None, now)
                flags += [flag] if flag else []
        if heartbeat_path is not None:
            beat = None
            try:
                beat = json.loads(Path(heartbeat_path).read_text()).get("recorded_at")
            except (OSError, ValueError, AttributeError):
                pass
            stale = beat is None or now - float(beat) > max(3 * heartbeat_seconds, 180)
            if stale:
                flag = _flag_once(connection, project, None, "supervisor_stale", str(int(beat or 0)),
                                  "supervisor heartbeat is missing or stale; start `harbor start " + project + "`", None, now)
                flags += [flag] if flag else []
    return flags


async def watch(project: str, *, minutes: float, interval: float, once: bool = False) -> int:
    from harbor.cli import _db_path
    from harbor.config.projects import get_project
    from harbor.db import HarborDB
    config = get_project(project)
    db = HarborDB(_db_path())
    beat = db.heartbeat_path(config.name)
    try:
        while True:
            for flag in tick(db.connection, config.name, minutes=minutes, heartbeat_path=beat,
                             heartbeat_seconds=float(getattr(config, "heartbeat_seconds", 60))):
                print(f"Harbor watch: {flag['kind']} {flag.get('ticket') or config.name}: {flag['detail']}", flush=True)
            if once:
                return 0
            await asyncio.sleep(interval)
    finally:
        db.close()


def add_command(commands) -> None:
    cmd = commands.add_parser("watch", help="run the progress watchdog for a project (separate process)")
    cmd.add_argument("project")
    cmd.add_argument("--minutes", type=float, default=30, help="minutes without progress before flagging")
    cmd.add_argument("--interval", type=float, default=60, help="seconds between passes")
    cmd.add_argument("--once", action="store_true", help="run a single pass and exit")
