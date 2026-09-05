"""Small SQLite ledger for observations and operator state.

Linear remains the source of truth for tickets.  The database only remembers
what Harbor observed and what the console needs to resume after a pause or
restart.  All public writes are short transactions so polling never holds a
database lock while doing network I/O.
"""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from harbor.resources import runtime_asset


SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    project TEXT PRIMARY KEY,
    snapshot_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    identifier TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    linear_id TEXT,
    title TEXT NOT NULL,
    url TEXT,
    state TEXT NOT NULL,
    state_type TEXT,
    branch_name TEXT,
    pr_urls_json TEXT NOT NULL DEFAULT '[]',
    comment_ids_json TEXT NOT NULL DEFAULT '[]',
    blocked_by_json TEXT NOT NULL DEFAULT '[]',
    owned INTEGER NOT NULL DEFAULT 0,
    hold INTEGER NOT NULL DEFAULT 1,
    unblock_attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tickets_project_idx ON tickets(project);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    identifier TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    snapshot_hash TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_project_time_idx ON events(project, at);
CREATE TABLE IF NOT EXISTS runtime (
    project TEXT PRIMARY KEY,
    lifecycle TEXT NOT NULL DEFAULT 'STOPPED',
    pause_reason TEXT,
    updated_at TEXT NOT NULL
);
-- Phase 7 is intentionally an additive operational ledger.  Linear remains
-- authoritative for ticket state and the builder remains authoritative for
-- implementation/merge; these rows only preserve the unblock conversation.
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT,
    run_id TEXT,
    kind TEXT NOT NULL,
    question TEXT NOT NULL,
    rung_resolved INTEGER NOT NULL,
    citation TEXT,
    answer TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS escalations_ticket_kind_idx
    ON escalations(ticket, kind, created_at);
CREATE TABLE IF NOT EXISTS resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    citation TEXT NOT NULL,
    source_ticket TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kind, question_hash)
);
CREATE INDEX IF NOT EXISTS resolutions_kind_hash_idx
    ON resolutions(kind, question_hash);
CREATE TABLE IF NOT EXISTS comment_receipts (
    issue_id TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    comment_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (issue_id, body_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS comment_receipts_issue_comment_idx
    ON comment_receipts(issue_id, comment_id)
    WHERE comment_id IS NOT NULL;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _question_hash(question: str) -> str:
    """Stable lookup key for an operator/blocker question."""

    normalized = " ".join(str(question).split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


question_hash = _question_hash


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))


def sql_statements(script: str) -> list[str]:
    """Split a checked-in DDL migration into individually executable statements.

    Comments are stripped *before* splitting on ``;``.  Splitting first is the
    subtly wrong order: a ``--`` comment containing a semicolon is cut in half,
    and its tail no longer starts with ``--``, so the leftover prose survives a
    line-based comment filter and reaches SQLite as a bogus statement.

    ``executescript`` handles comments correctly but cannot skip a single
    non-idempotent ALTER, which is why every phase migration needs this.
    """

    out: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    index = 0
    length = len(script)
    while index < length:
        char = script[index]
        if quote is not None:
            buffer.append(char)
            if char == quote:
                # A doubled quote is an escaped literal, not a terminator.
                if index + 1 < length and script[index + 1] == quote:
                    buffer.append(script[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == "-" and script.startswith("--", index):
            end = script.find("\n", index)
            index = length if end == -1 else end
            continue
        if char == "/" and script.startswith("/*", index):
            end = script.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                out.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    trailing = "".join(buffer).strip()
    if trailing:
        out.append(trailing)
    return out


_sql_statements = sql_statements


def apply_phase7_migration(db: Any, path: str | Path | None = None) -> None:
    """Apply the Phase 7 ledger migration to a new or existing database.

    The migration is deliberately safe to call at every startup.  SQLite has
    no portable ``ADD COLUMN IF NOT EXISTS``; the one ALTER is guarded here,
    while the checked-in migration remains easy to audit and replay.
    """

    connection = db if isinstance(db, sqlite3.Connection) else getattr(db, "connection", None)
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("Phase 7 migration requires a sqlite3.Connection or HarborDB")
    migration = Path(path) if path is not None else runtime_asset("migrations", "006_phase7_unblock.sql")
    if not migration.is_file():
        # ``SCHEMA`` contains the same additive objects for brand-new DBs.  A
        # missing migration is still an error for an existing deployment so a
        # caller cannot silently believe its ledger is durable.
        if not _table_exists(connection, "resolutions"):
            raise FileNotFoundError(migration)
        return
    script = migration.read_text(encoding="utf-8")
    statements = sql_statements(script)
    lock = getattr(db, "_lock", None)
    lock_context = lock if lock is not None else threading.RLock()
    with lock_context:
        for statement in statements:
            if statement.upper().startswith("ALTER TABLE TICKETS ADD COLUMN UNBLOCK_ATTEMPTS"):
                if not _table_exists(connection, "tickets") or _column_exists(connection, "tickets", "unblock_attempts"):
                    continue
            connection.execute(statement)
        connection.commit()


def apply_phase9_migration(db: Any, path: str | Path | None = None) -> None:
    """Apply the additive queue/artifact columns used by the correctness gate.

    Phase 9 deliberately does not create the Phase 3 tables.  This allows a
    plain Phase 1 database to start normally; once Phase 3/4 has created the
    relevant tables, this helper adds the durable metadata on the next
    startup.  Every statement is guarded because SQLite lacks portable
    ``ADD COLUMN IF NOT EXISTS`` support.
    """

    connection = db if isinstance(db, sqlite3.Connection) else getattr(db, "connection", None)
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("Phase 9 migration requires a sqlite3.Connection or HarborDB")
    migration = Path(path) if path is not None else runtime_asset("migrations", "008_phase9_correctness.sql")
    if not migration.is_file():
        raise FileNotFoundError(migration)
    statements = sql_statements(migration.read_text(encoding="utf-8"))
    lock = getattr(db, "_lock", None)
    lock_context = lock if lock is not None else threading.RLock()
    with lock_context:
        for statement in statements:
            match = re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", statement, re.IGNORECASE)
            if match:
                table, column = match.groups()
                if not _table_exists(connection, table) or _column_exists(connection, table, column):
                    continue
            if statement.upper().startswith("CREATE INDEX") and not _table_exists(connection, "work_queue"):
                continue
            connection.execute(statement)
        connection.commit()


class HarborDB:
    """Thread-safe wrapper around one Harbor SQLite database."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path and str(path) != ":memory:" else path or Path.home() / ".harbor" / "harbor.db"
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(SCHEMA)
            self._connection.commit()
            # Existing Phase 1--6 databases need the guarded ALTER from the
            # additive migration; fresh databases simply no-op here.
            apply_phase7_migration(self)
            apply_phase9_migration(self)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def snapshot_hash(self, project: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT snapshot_hash FROM snapshots WHERE project = ?", (project,)
            ).fetchone()
            return str(row[0]) if row else None

    def save_snapshot(self, project: str, snapshot_hash: str, payload: Mapping[str, Any]) -> bool:
        """Persist a changed snapshot and return whether it was new/changed."""

        timestamp = _now()
        encoded = _json(payload)
        with self._lock:
            current = self._connection.execute(
                "SELECT snapshot_hash FROM snapshots WHERE project = ?", (project,)
            ).fetchone()
            if current and current[0] == snapshot_hash:
                return False
            self._connection.execute(
                "INSERT INTO snapshots(project, snapshot_hash, payload_json, observed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project) DO UPDATE SET snapshot_hash=excluded.snapshot_hash, "
                "payload_json=excluded.payload_json, observed_at=excluded.observed_at",
                (project, snapshot_hash, encoded, timestamp),
            )
            self._connection.commit()
            return True

    def replace_tickets(
        self,
        project: str,
        tickets: Iterable[Mapping[str, Any]],
        *,
        snapshot_hash: str | None = None,
        emit_events: bool = True,
    ) -> int:
        """Upsert observed tickets, optionally emitting one event per ticket.

        This method is called only after :meth:`save_snapshot` returns true.
        Calling it directly is supported for fixtures and intentionally emits
        events on every call when ``emit_events`` is true.
        """

        timestamp = _now()
        rows = [dict(ticket) for ticket in tickets]
        with self._lock:
            for ticket in rows:
                identifier = str(ticket.get("identifier", ""))
                if not identifier:
                    continue
                previous = self._connection.execute(
                    "SELECT owned, hold FROM tickets WHERE identifier = ?", (identifier,)
                ).fetchone()
                # ``owned`` and ``hold`` are Harbor's local custody flags, not
                # Linear fields.  Normalized Linear snapshots historically
                # included ``owned=False`` and ``hold=True`` on every poll;
                # accepting those defaults here would silently release a
                # ticket (or put it back on hold) whenever any remote field
                # changed.  A first observation may seed the flags, but once
                # a row exists the local value always wins.  Explicit local
                # changes use ``mark_owned``/``set_hold`` below.
                owned = int(previous[0]) if previous else int(ticket.get("owned", 0))
                hold = int(previous[1]) if previous else int(ticket.get("hold", 1))
                values = (
                    identifier,
                    project,
                    ticket.get("id"),
                    str(ticket.get("title", "")),
                    ticket.get("url"),
                    str(ticket.get("state", "")),
                    ticket.get("state_type"),
                    ticket.get("branch_name"),
                    _json(ticket.get("pr_urls", [])),
                    _json(ticket.get("comment_ids", [])),
                    _json(ticket.get("blocked_by", [])),
                    owned,
                    hold,
                    ticket.get("updated_at"),
                    timestamp,
                )
                self._connection.execute(
                    "INSERT INTO tickets(identifier, project, linear_id, title, url, state, state_type, "
                    "branch_name, pr_urls_json, comment_ids_json, blocked_by_json, owned, hold, updated_at, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(identifier) DO UPDATE SET project=excluded.project, linear_id=excluded.linear_id, "
                    "title=excluded.title, url=excluded.url, state=excluded.state, state_type=excluded.state_type, "
                    "branch_name=excluded.branch_name, pr_urls_json=excluded.pr_urls_json, "
                    "comment_ids_json=excluded.comment_ids_json, blocked_by_json=excluded.blocked_by_json, "
                    "owned=excluded.owned, hold=excluded.hold, updated_at=excluded.updated_at, observed_at=excluded.observed_at",
                    values,
                )
                if emit_events:
                    self._connection.execute(
                        "INSERT INTO events(project, identifier, event_type, payload_json, snapshot_hash, at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (project, identifier, "ticket.observed", _json(ticket), snapshot_hash, timestamp),
                    )
            self._connection.commit()
        return len(rows)

    def record_observation(
        self, project: str, snapshot_hash: str, payload: Mapping[str, Any], tickets: Iterable[Mapping[str, Any]]
    ) -> bool:
        """Atomically persist a changed observation and its ticket events."""

        timestamp = _now()
        encoded = _json(payload)
        rows = [dict(ticket) for ticket in tickets]
        with self._lock:
            current = self._connection.execute(
                "SELECT snapshot_hash FROM snapshots WHERE project = ?", (project,)
            ).fetchone()
            if current and current[0] == snapshot_hash:
                return False
            self._connection.execute(
                "INSERT INTO snapshots(project, snapshot_hash, payload_json, observed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project) DO UPDATE SET snapshot_hash=excluded.snapshot_hash, "
                "payload_json=excluded.payload_json, observed_at=excluded.observed_at",
                (project, snapshot_hash, encoded, timestamp),
            )
            for ticket in rows:
                identifier = str(ticket.get("identifier", ""))
                if not identifier:
                    continue
                previous = self._connection.execute(
                    "SELECT owned, hold FROM tickets WHERE identifier = ?", (identifier,)
                ).fetchone()
                values = (
                    identifier,
                    project,
                    ticket.get("id"),
                    str(ticket.get("title", "")),
                    ticket.get("url"),
                    str(ticket.get("state", "")),
                    ticket.get("state_type"),
                    ticket.get("branch_name"),
                    _json(ticket.get("pr_urls", [])),
                    _json(ticket.get("comment_ids", [])),
                    _json(ticket.get("blocked_by", [])),
                    int(previous[0]) if previous else int(ticket.get("owned", 0)),
                    int(previous[1]) if previous else int(ticket.get("hold", 1)),
                    ticket.get("updated_at"),
                    timestamp,
                )
                self._connection.execute(
                    "INSERT INTO tickets(identifier, project, linear_id, title, url, state, state_type, "
                    "branch_name, pr_urls_json, comment_ids_json, blocked_by_json, owned, hold, updated_at, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(identifier) DO UPDATE SET project=excluded.project, linear_id=excluded.linear_id, "
                    "title=excluded.title, url=excluded.url, state=excluded.state, state_type=excluded.state_type, "
                    "branch_name=excluded.branch_name, pr_urls_json=excluded.pr_urls_json, "
                    "comment_ids_json=excluded.comment_ids_json, blocked_by_json=excluded.blocked_by_json, "
                    "owned=excluded.owned, hold=excluded.hold, updated_at=excluded.updated_at, observed_at=excluded.observed_at",
                    values,
                )
                self._connection.execute(
                    "INSERT INTO events(project, identifier, event_type, payload_json, snapshot_hash, at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (project, identifier, "ticket.observed", _json(ticket), snapshot_hash, timestamp),
                )
            self._connection.commit()
            return True

    def tickets(self, project: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM tickets WHERE project = ? ORDER BY identifier", (project,)
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key, output in (
                ("pr_urls_json", "pr_urls"),
                ("comment_ids_json", "comment_ids"),
                ("blocked_by_json", "blocked_by"),
            ):
                item[output] = json.loads(item.pop(key))
            item["owned"] = bool(item["owned"])
            item["hold"] = bool(item["hold"])
            result.append(item)
        return result

    def events(self, project: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM events"
        args: tuple[Any, ...] = ()
        if project is not None:
            query += " WHERE project = ?"
            args = (project,)
        query += " ORDER BY id"
        with self._lock:
            rows = self._connection.execute(query, args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def event_count(self, project: str | None = None) -> int:
        query = "SELECT count(*) FROM events"
        args: tuple[Any, ...] = ()
        if project is not None:
            query += " WHERE project = ?"
            args = (project,)
        with self._lock:
            return int(self._connection.execute(query, args).fetchone()[0])

    count_events = event_count

    def set_runtime(self, project: str, lifecycle: str, reason: str | None = None) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO runtime(project, lifecycle, pause_reason, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project) DO UPDATE SET lifecycle=excluded.lifecycle, "
                "pause_reason=excluded.pause_reason, updated_at=excluded.updated_at",
                (project, lifecycle, reason, _now()),
            )
            self._connection.commit()

    def runtime(self, project: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM runtime WHERE project = ?", (project,)).fetchone()
        return dict(row) if row else {"project": project, "lifecycle": "STOPPED", "pause_reason": None}

    # ------------------------------------------------------------------
    # Local custody and dispatch queue
    # ------------------------------------------------------------------
    def mark_owned(self, identifier: str) -> bool:
        """Claim a ticket for Harbor without allowing Linear snapshots to undo it."""

        with self._lock:
            cursor = self._connection.execute(
                "UPDATE tickets SET owned = 1 WHERE identifier = ?", (str(identifier),)
            )
            self._connection.commit()
        return cursor.rowcount > 0

    def set_hold(self, identifier: str, hold: bool) -> bool:
        """Set Harbor's local hold flag.

        This is deliberately separate from :meth:`replace_tickets`: Linear
        does not own this operational flag and an observation must never
        overwrite it.
        """

        with self._lock:
            cursor = self._connection.execute(
                "UPDATE tickets SET hold = ? WHERE identifier = ?",
                (int(bool(hold)), str(identifier)),
            )
            self._connection.commit()
        return cursor.rowcount > 0

    def queue_has_idem(self, idem_key: str, *, live_only: bool = True) -> bool:
        """Return whether a queue item with *idem_key* still participates in the gate."""

        if not _table_exists(self._connection, "work_queue"):
            return False
        query = "SELECT 1 FROM work_queue WHERE idem_key = ?"
        args: list[Any] = [str(idem_key)]
        if live_only:
            query += " AND status IN ('queued', 'dispatched', 'running', 'waiting')"
        query += " LIMIT 1"
        with self._lock:
            return self._connection.execute(query, args).fetchone() is not None

    def queue_item(self, *, queue_id: int | None = None, idem_key: str | None = None) -> dict[str, Any] | None:
        """Read one queue item by idempotency key or row id."""

        if not _table_exists(self._connection, "work_queue"):
            return None
        if queue_id is None and idem_key is None:
            raise ValueError("queue_id or idem_key is required")
        if queue_id is not None:
            query, args = "SELECT * FROM work_queue WHERE id = ?", (int(queue_id),)
        else:
            query, args = (
                "SELECT * FROM work_queue WHERE idem_key = ? ORDER BY id DESC LIMIT 1",
                (str(idem_key),),
            )
        with self._lock:
            row = self._connection.execute(query, args).fetchone()
        if row is None:
            return None
        item = dict(row)
        with_context = item.get("payload_json")
        if with_context is not None:
            try:
                item["payload"] = json.loads(with_context)
            except (TypeError, ValueError):
                item["payload"] = {}
        return item

    queue = queue_item

    def enqueue(
        self,
        ticket: str,
        action: str,
        idem_key: str,
        payload: Mapping[str, Any] | None = None,
    ) -> bool:
        """Insert a queued action and claim its ticket atomically.

        The Phase 3 migration owns the table.  Returning ``False`` when it is
        absent is intentional: a caller must not dispatch work it cannot make
        durable and deduplicable.
        """

        if not _table_exists(self._connection, "work_queue"):
            return False
        encoded = _json(payload or {})
        with self._lock:
            try:
                self._connection.execute(
                    "INSERT INTO work_queue(ticket, action, idem_key, status, payload_json) "
                    "VALUES (?, ?, ?, 'queued', ?)",
                    (str(ticket), str(action), str(idem_key), encoded),
                )
            except sqlite3.IntegrityError:
                self._connection.rollback()
                return False
            self._connection.execute("UPDATE tickets SET owned = 1 WHERE identifier = ?", (str(ticket),))
            self._connection.commit()
        return True

    def transition_queue(
        self,
        status: str,
        *,
        queue_id: int | None = None,
        idem_key: str | None = None,
        run_id: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Move one queued action through its durable lifecycle.

        The method is intentionally idempotent.  A caller may retry the
        dispatch/running update after a crash; the latest row is selected by
        ``idem_key`` and completed/failed rows can never be moved backwards by
        an accidental duplicate callback.
        """

        target = str(status).strip().casefold()
        allowed = {"queued", "dispatched", "running", "waiting", "completed", "failed", "stopped"}
        if target not in allowed:
            raise ValueError(f"unknown queue status: {status}")
        if queue_id is None and idem_key is None:
            raise ValueError("queue_id or idem_key is required")
        if not _table_exists(self._connection, "work_queue"):
            return False
        with self._lock:
            if queue_id is not None:
                row = self._connection.execute(
                    "SELECT id, status FROM work_queue WHERE id = ?", (int(queue_id),)
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT id, status FROM work_queue WHERE idem_key = ? ORDER BY id DESC LIMIT 1",
                    (str(idem_key),),
                ).fetchone()
            if row is None:
                return False
            current = str(row[1]).casefold()
            # Terminal queue rows are retained for audit and must not be
            # resurrected by a late completion callback.
            if current in {"completed", "failed", "stopped"} and target not in {current}:
                return False
            columns = {item[1] for item in self._connection.execute("PRAGMA table_info(work_queue)")}
            updates: list[str] = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
            values: list[Any] = [target]
            if run_id is not None and "run_id" in columns:
                updates.append("run_id = ?")
                values.append(str(run_id))
            if error is not None and "error" in columns:
                updates.append("error = ?")
                values.append(str(error))
            values.append(int(row[0]))
            self._connection.execute(
                f"UPDATE work_queue SET {', '.join(updates)} WHERE id = ?", values
            )
            self._connection.commit()
        return True

    # Names used by embedders and older dispatcher adapters.
    update_queue = transition_queue
    set_queue_status = transition_queue

    def transition_ticket_queue(
        self,
        ticket: str,
        status: str,
        *,
        action: str | None = None,
        run_id: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Transition the most recent live queue row for a ticket."""

        if not _table_exists(self._connection, "work_queue"):
            return False
        query = (
            "SELECT id FROM work_queue WHERE ticket = ? "
            "AND status IN ('queued', 'dispatched', 'running', 'waiting')"
        )
        args: list[Any] = [str(ticket)]
        if action is not None:
            query += " AND action = ?"
            args.append(str(action))
        query += " ORDER BY id DESC LIMIT 1"
        with self._lock:
            row = self._connection.execute(query, args).fetchone()
        if row is None:
            return False
        return self.transition_queue(status, queue_id=int(row[0]), run_id=run_id, error=error)

    mark_queue_status = transition_ticket_queue

    # ------------------------------------------------------------------
    # Phase 7 unblock ledger
    # ------------------------------------------------------------------
    def record_escalation(
        self,
        ticket: str | None,
        run_id: str | None,
        kind: str,
        question: str,
        rung_resolved: int,
        *,
        citation: str | None = None,
        answer: str | None = None,
        notified: bool = False,
    ) -> dict[str, Any]:
        """Record one ladder result, including successful lower rungs.

        The name is historical: a rung-0/1 result is still an escalation
        audit, but it does not imply that the operator was paged.  This makes
        the acceptance evidence unambiguous and preserves the exact question
        that led to the decision.
        """

        kind_text = " ".join(str(kind).split())
        question_text = str(question).strip()
        if not kind_text:
            raise ValueError("escalation kind is required")
        if not question_text:
            raise ValueError("escalation question is required")
        try:
            rung = int(rung_resolved)
        except (TypeError, ValueError) as exc:
            raise ValueError("rung_resolved must be an integer") from exc
        if rung not in (0, 1, 2):
            raise ValueError("rung_resolved must be 0, 1, or 2")
        timestamp = _now()
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO escalations(ticket, run_id, kind, question, rung_resolved, citation, answer, notified, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(ticket) if ticket is not None else None,
                    str(run_id) if run_id is not None else None,
                    kind_text,
                    question_text,
                    rung,
                    str(citation).strip() if citation is not None and str(citation).strip() else None,
                    str(answer) if answer is not None else None,
                    int(bool(notified)),
                    timestamp,
                ),
            )
            if ticket and _table_exists(self._connection, "tickets") and _column_exists(
                self._connection, "tickets", "unblock_attempts"
            ):
                self._connection.execute(
                    "UPDATE tickets SET unblock_attempts = COALESCE(unblock_attempts, 0) + 1 WHERE identifier = ?",
                    (str(ticket),),
                )
            self._connection.commit()
            row = self._connection.execute(
                "SELECT * FROM escalations WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row) if row else {"id": cursor.lastrowid, "ticket": ticket, "run_id": run_id, "kind": kind_text, "question": question_text, "rung_resolved": rung}

    add_escalation = record_escalation

    def escalations(
        self,
        ticket: str | None = None,
        *,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM escalations"
        clauses: list[str] = []
        args: list[Any] = []
        if ticket is not None:
            clauses.append("ticket = ?")
            args.append(str(ticket))
        if kind is not None:
            clauses.append("kind = ?")
            args.append(str(kind))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        with self._lock:
            rows = self._connection.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    list_escalations = escalations

    def count_escalations(
        self,
        ticket: str | None = None,
        *,
        kind: str | None = None,
        rung_resolved: int | None = None,
    ) -> int:
        clauses: list[str] = []
        args: list[Any] = []
        if ticket is not None:
            clauses.append("ticket = ?")
            args.append(str(ticket))
        if kind is not None:
            clauses.append("kind = ?")
            args.append(str(kind))
        if rung_resolved is not None:
            clauses.append("rung_resolved = ?")
            args.append(int(rung_resolved))
        query = "SELECT count(*) FROM escalations" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        with self._lock:
            return int(self._connection.execute(query, args).fetchone()[0])

    def unblock_attempts(self, ticket: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT unblock_attempts FROM tickets WHERE identifier = ?", (str(ticket),)
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def record_resolution(
        self,
        kind: str,
        question: str,
        answer: str,
        citation: str,
        *,
        source_ticket: str | None = None,
    ) -> dict[str, Any]:
        """Persist an operator answer that is reusable by rung 1.

        A citation is the safety boundary: an answer without one is never a
        resolution, even when it sounds plausible.  The unique kind/hash key
        makes repeated ``harbor answer`` commands idempotent.
        """

        kind_text = " ".join(str(kind).split())
        question_text = " ".join(str(question).split())
        answer_text = str(answer).strip()
        citation_text = str(citation).strip()
        if not kind_text:
            raise ValueError("resolution kind is required")
        if not question_text:
            raise ValueError("resolution question is required")
        if not answer_text:
            raise ValueError("resolution answer is required")
        if not citation_text:
            raise ValueError("resolution citation is required")
        digest = _question_hash(question_text)
        timestamp = _now()
        with self._lock:
            self._connection.execute(
                "INSERT INTO resolutions(kind, question_hash, question, answer, citation, source_ticket, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(kind, question_hash) DO NOTHING",
                (
                    kind_text,
                    digest,
                    question_text,
                    answer_text,
                    citation_text,
                    str(source_ticket) if source_ticket is not None else None,
                    timestamp,
                ),
            )
            self._connection.commit()
            row = self._connection.execute(
                "SELECT * FROM resolutions WHERE kind = ? AND question_hash = ?", (kind_text, digest)
            ).fetchone()
        return dict(row) if row else {}

    add_resolution = record_resolution

    def find_resolution(self, kind: str, question: str) -> dict[str, Any] | None:
        digest = _question_hash(question)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM resolutions WHERE kind = ? AND question_hash = ? LIMIT 1",
                (" ".join(str(kind).split()), digest),
            ).fetchone()
        return dict(row) if row else None

    resolution_for = find_resolution

    def resolutions(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM resolutions"
        args: list[Any] = []
        if kind is not None:
            query += " WHERE kind = ?"
            args.append(str(kind))
        query += " ORDER BY id"
        with self._lock:
            rows = self._connection.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    list_resolutions = resolutions

    def comment_receipt_exists(
        self,
        issue_id: str,
        *,
        comment_id: str | None = None,
        body: str | None = None,
    ) -> bool:
        """Check a receipt by exact id, or by body only when no id exists."""

        with self._lock:
            if comment_id is not None:
                row = self._connection.execute(
                    "SELECT 1 FROM comment_receipts WHERE issue_id = ? AND comment_id = ? LIMIT 1",
                    (str(issue_id), str(comment_id)),
                ).fetchone()
                return row is not None
            if body is None:
                return False
            digest = hashlib.sha256(str(body).encode("utf-8")).hexdigest()
            return self._connection.execute(
                "SELECT 1 FROM comment_receipts WHERE issue_id = ? AND body_hash = ? LIMIT 1",
                (str(issue_id), digest),
            ).fetchone() is not None

    has_comment_receipt = comment_receipt_exists

    def record_comment_receipt(
        self,
        issue_id: str,
        comment_id: str | None = None,
        body: str | None = None,
    ) -> bool:
        """Persist a Linear comment receipt keyed by issue and body hash."""

        if comment_id is None and body is None:
            raise ValueError("comment_id or body is required")
        digest = hashlib.sha256(str(body if body is not None else comment_id).encode("utf-8")).hexdigest()
        with self._lock:
            before = self._connection.total_changes
            self._connection.execute(
                "INSERT INTO comment_receipts(issue_id, body_hash, comment_id) VALUES (?, ?, ?) "
                "ON CONFLICT(issue_id, body_hash) DO NOTHING",
                (str(issue_id), digest, str(comment_id) if comment_id is not None else None),
            )
            changed = self._connection.total_changes > before
            self._connection.commit()
        return changed


def open_db(path: str | Path | None = None) -> HarborDB:
    return HarborDB(path)


init_db = open_db


__all__ = [
    "HarborDB",
    "SCHEMA",
    "apply_phase7_migration",
    "apply_phase9_migration",
    "open_db",
    "question_hash",
]
