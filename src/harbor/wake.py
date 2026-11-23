"""Pure, deterministic decisions for Harbor's Phase 3 wake loop.

The delivery skill owns all work and all Linear/GitHub mutations.  This module
only turns an observed ticket plus already-fetched facts into a small
``Wake`` value.  In particular, :class:`WakeDecider` never calls a client.
``shadow=True`` makes the decision path completely read-only, including the
local ledger: useful for the first live run and for repeatable acceptance
tests.

The module intentionally accepts mappings and small duck-typed database
objects.  Phase 1's ``HarborDB`` predates the Phase 3 tables, so
``apply_phase3_migration`` is provided as an explicit, additive bridge rather
than requiring a change to that module.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harbor.db import sql_statements
from harbor.facts.threads import parse_severity
from harbor.resources import runtime_asset
from harbor.reengagement import (
    MAX_RESUME_BYTES,
    build_resume_instruction,
    ci_finished,
    ci_state,
    eligible_review_threads,
    merged,
)


DISPATCH_STATES = ("Todo",)
LIVE_QUEUE_STATUSES = ("queued", "dispatched", "running", "waiting")
# ``waiting`` is a completed runner invocation whose session is retained for
# a later re-engagement.  It must not suppress that very re-engagement.
ACTIVE_RUN_STATUSES = ("running",)
_SEVERITY_RE = re.compile(r"\bP([0-9]+)\b", re.IGNORECASE)
# GitHub integration comments carry explicit HTML markers; Linear comments do
# not trust body text because humans can naturally write ``Harbor: ...``.
_HARBOR_PR_MARKER_RE = re.compile(r"(?:<!--\s*harbor\b|\[harbor\])", re.IGNORECASE)
_MIGRATION_NAME = "003_phase3_wake.sql"
_PHASE6_MIGRATION_NAME = "005_phase6_reengagement.sql"
_PR_IDENTITY_MIGRATION_NAME = "009_pr_url_identity.sql"


@dataclass(frozen=True)
class TicketScope:
    """Invocation-local exact ticket selector.

    A scope is deliberately not part of the Harbor ledger.  It is a custody
    boundary for one supervisor invocation: observations may still include
    the complete project fleet, but only the selected Linear identifier may
    enter a wake decision or any downstream builder path.  Matching trims
    surrounding whitespace and folds case; it never performs substring or
    prefix matching.
    """

    selector: str | None = None

    def __post_init__(self) -> None:
        if self.selector is not None and not str(self.selector).strip():
            raise ValueError("ticket selector must not be blank")

    @property
    def normalized(self) -> str | None:
        if self.selector is None:
            return None
        return str(self.selector).strip().casefold()

    def matches(self, value: Any) -> bool:
        """Return whether *value* is exactly the selected identifier."""

        wanted = self.normalized
        if wanted is None:
            return True
        identifier = value if isinstance(value, str) else _identifier(value)
        return bool(identifier and identifier.strip().casefold() == wanted)

    def __bool__(self) -> bool:
        return self.normalized is not None


def ticket_scope(selector: str | TicketScope | None) -> TicketScope:
    """Normalize a selector while keeping ``None`` as an unrestricted scope."""

    if isinstance(selector, TicketScope):
        return selector
    return TicketScope(selector)


def _value(value: Any, *names: str, default: Any = None) -> Any:
    """Read a field from either a mapping or a small object."""

    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _identifier(ticket: Any) -> str:
    return str(_value(ticket, "identifier", "id", default="") or "")


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _db_connection(db: Any) -> sqlite3.Connection | None:
    if isinstance(db, sqlite3.Connection):
        return db
    connection = getattr(db, "connection", None)
    return connection if isinstance(connection, sqlite3.Connection) else None


def _db_lock(db: Any) -> Any:
    lock = getattr(db, "_lock", None)
    return lock if lock is not None else nullcontext()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))


def _migration_path() -> Path:
    return runtime_asset("migrations", _MIGRATION_NAME)


def apply_phase3_migration(db: Any, path: str | Path | None = None) -> None:
    """Apply the additive Phase 3 migration to a Harbor SQLite database.

    SQLite does not support ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``.
    The checked column test makes this helper safe to call on a restarted
    process while leaving the checked-in SQL migration easy to audit.
    """

    connection = _db_connection(db)
    if connection is None:
        raise TypeError("Phase 3 migration requires a sqlite3.Connection or HarborDB")
    migration = Path(path) if path is not None else _migration_path()
    script = migration.read_text(encoding="utf-8")
    # The migration contains only straightforward DDL.  Removing full-line
    # comments lets us execute one statement at a time and skip the one
    # non-idempotent ALTER safely.
    statements = sql_statements(script)
    with _db_lock(db):
        for statement in statements:
            if statement.upper().startswith("ALTER TABLE TICKETS ADD COLUMN BOT_REWORK_ROUNDS"):
                if not _table_exists(connection, "tickets") or _column_exists(
                    connection, "tickets", "bot_rework_rounds"
                ):
                    continue
            if (
                "REVIEW_THREADS_PR_IDX" in statement.upper()
                and not _column_exists(connection, "review_threads", "pr_url")
            ):
                continue
            connection.execute(statement)
        connection.commit()
        pr_columns = {
            str(row[1]): int(row[5])
            for row in connection.execute("PRAGMA table_info(prs)").fetchall()
        }
        if pr_columns.get("number") == 1 and pr_columns.get("url") != 1:
            migration = runtime_asset("migrations", _PR_IDENTITY_MIGRATION_NAME)
            try:
                connection.executescript(migration.read_text(encoding="utf-8"))
            except Exception:
                connection.rollback()
                connection.execute("PRAGMA foreign_keys=ON")
                raise
            connection.commit()


def apply_phase6_migration(db: Any, path: str | Path | None = None) -> None:
    """Apply the additive Phase 6 re-engagement ledger migration.

    Phase 6 can be imported into a process that only ran the Phase 3 helper,
    so the two migrations are intentionally independent and idempotent.  The
    table is local audit/idempotency state only; no Linear or GitHub operation
    is represented by this migration.
    """

    connection = _db_connection(db)
    if connection is None:
        raise TypeError("Phase 6 migration requires a sqlite3.Connection or HarborDB")
    migration = Path(path) if path is not None else runtime_asset("migrations", _PHASE6_MIGRATION_NAME)
    script = migration.read_text(encoding="utf-8")
    with _db_lock(db):
        # This is a checked-in, DDL-only migration.  ``executescript`` keeps
        # semicolons in comments from being mistaken for statement boundaries
        # (the hand-written splitter used by Phase 3 cannot safely do that).
        connection.executescript(script)
        connection.commit()


def _ticket_row(db: Any, identifier: str) -> dict[str, Any] | None:
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "tickets"):
        return None
    columns = {row[1] for row in connection.execute("PRAGMA table_info(tickets)")}
    selected = [name for name in ("identifier", "owned", "hold", "bot_rework_rounds") if name in columns]
    if not selected:
        return None
    row = connection.execute(
        f"SELECT {', '.join(selected)} FROM tickets WHERE identifier = ?", (identifier,)
    ).fetchone()
    return dict(zip(selected, row)) if row else None


def _active_run(db: Any, identifier: str) -> dict[str, Any] | None:
    method = getattr(db, "active_run", None)
    if callable(method):
        result = method(identifier)
        return dict(result) if isinstance(result, Mapping) else result
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "runs"):
        return None
    marks = ", ".join("?" for _ in ACTIVE_RUN_STATUSES)
    row = connection.execute(
        f"SELECT * FROM runs WHERE ticket = ? AND status IN ({marks}) "
        "ORDER BY COALESCE(started_at, id) DESC LIMIT 1",
        (identifier, *ACTIVE_RUN_STATUSES),
    ).fetchone()
    return dict(row) if row else None


def _last_run(db: Any, identifier: str) -> dict[str, Any] | None:
    method = getattr(db, "last_run", None)
    if callable(method):
        result = method(identifier)
        return dict(result) if isinstance(result, Mapping) else result
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "runs"):
        return None
    row = connection.execute(
        "SELECT * FROM runs WHERE ticket = ? ORDER BY COALESCE(finished_at, started_at, id) DESC, id DESC LIMIT 1",
        (identifier,),
    ).fetchone()
    return dict(row) if row else None


def _queue_has_idem(db: Any, idem_key: str, *, live_only: bool = True) -> bool:
    method = getattr(db, "queue_has_idem", None)
    if callable(method):
        return bool(method(idem_key, live_only=live_only))
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "work_queue"):
        return False
    query = "SELECT 1 FROM work_queue WHERE idem_key = ?"
    args: tuple[Any, ...] = (idem_key,)
    if live_only:
        marks = ", ".join("?" for _ in LIVE_QUEUE_STATUSES)
        query += f" AND status IN ({marks})"
        args += LIVE_QUEUE_STATUSES
    return connection.execute(query + " LIMIT 1", args).fetchone() is not None


def _reengagement_has_idem(db: Any, idem_key: str) -> bool:
    """Check the retained re-engagement audit, including completed rows."""

    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "reengagement_events"):
        return False
    return connection.execute(
        "SELECT 1 FROM reengagement_events WHERE idem_key = ? LIMIT 1", (idem_key,)
    ).fetchone() is not None


def _record_reengagement_event(
    db: Any,
    *,
    ticket: str,
    run_id: str,
    kind: str,
    idem_key: str,
    head_sha: str | None,
    payload: Mapping[str, Any],
) -> None:
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "reengagement_events"):
        return
    with _db_lock(db):
        connection.execute(
            "INSERT INTO reengagement_events(ticket, run_id, kind, idem_key, head_sha, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(idem_key) DO NOTHING",
            (ticket, run_id, kind, idem_key, head_sha, _json(payload)),
        )
        connection.commit()


def _row_bot_rounds(db: Any, identifier: str) -> int:
    row = _ticket_row(db, identifier)
    return _as_int(row.get("bot_rework_rounds"), 0) if row else 0


def _mark_owned(db: Any, identifier: str) -> None:
    method = getattr(db, "mark_owned", None)
    if callable(method):
        method(identifier)
        return
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "tickets") or not _column_exists(connection, "tickets", "owned"):
        return
    connection.execute("UPDATE tickets SET owned = 1 WHERE identifier = ?", (identifier,))


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps({"unserializable": repr(value)}, sort_keys=True)


def _enqueue(
    db: Any,
    *,
    identifier: str,
    action: str,
    idem_key: str,
    payload: Mapping[str, Any],
) -> bool:
    """Insert one queue row and mark the ticket owned, atomically where possible."""

    method = getattr(db, "enqueue", None)
    if callable(method):
        return bool(method(identifier, action, idem_key, payload))
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "work_queue"):
        # A caller can still use a pure decider before wiring a migration.  A
        # missing queue is not an excuse to issue duplicate external work, so
        # report failure and let the in-memory shadow gate handle that mode.
        return False
    with _db_lock(db):
        try:
            connection.execute(
                "INSERT INTO work_queue(ticket, action, idem_key, status, payload_json) VALUES (?, ?, ?, 'queued', ?)",
                (identifier, action, idem_key, _json(payload)),
            )
        except sqlite3.IntegrityError:
            connection.rollback()
            return False
        _mark_owned(db, identifier)
        connection.commit()
    return True


def _receipt_exists(db: Any, issue_id: str, *, comment_id: str | None = None, body: str | None = None) -> bool:
    method = getattr(db, "comment_receipt_exists", None)
    if callable(method):
        return bool(method(issue_id, comment_id=comment_id, body=body))
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "comment_receipts"):
        return False
    if comment_id is not None:
        row = connection.execute(
            "SELECT 1 FROM comment_receipts WHERE issue_id = ? AND comment_id = ? LIMIT 1",
            (issue_id, comment_id),
        ).fetchone()
        return row is not None
    if body is not None:
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return connection.execute(
            "SELECT 1 FROM comment_receipts WHERE issue_id = ? AND body_hash = ? LIMIT 1",
            (issue_id, digest),
        ).fetchone() is not None
    return False


def record_comment_receipt(
    db: Any,
    issue_id: str,
    comment_id: str | None = None,
    body: str | None = None,
) -> bool:
    """Remember a Harbor-authored Linear comment without posting anything.

    Either ``comment_id`` or ``body`` is sufficient.  Body hashes preserve the
    duplicate-comment guard even when the API response omits an id.
    """

    if comment_id is None and body is None:
        raise ValueError("comment_id or body is required")
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "comment_receipts"):
        raise RuntimeError("Phase 3 migration has not been applied")
    digest = hashlib.sha256((body or comment_id or "").encode("utf-8")).hexdigest()
    before = connection.total_changes
    with _db_lock(db):
        try:
            connection.execute(
                "INSERT INTO comment_receipts(issue_id, body_hash, comment_id) VALUES (?, ?, ?) "
                "ON CONFLICT(issue_id, body_hash) DO NOTHING",
                (str(issue_id), digest, str(comment_id) if comment_id is not None else None),
            )
            changed = connection.total_changes > before
        except sqlite3.IntegrityError:
            changed = False
        connection.commit()
    return changed


def comment_is_harbor_authored(
    comment: Mapping[str, Any],
    *,
    db: Any | None = None,
    issue_id: str | None = None,
) -> bool:
    """Return whether a Linear comment is known to be Harbor-authored.

    Receipt ids and the explicit trusted adapter flag are authoritative.  A
    body marker is deliberately insufficient because a human may naturally
    begin an instruction with ``Harbor:`` or quote Harbor output.
    """

    if _as_bool(_value(comment, "harbor_authored", "is_harbor_authored", default=False)):
        return True
    comment_id = _value(comment, "id", "comment_id")
    if db is not None and issue_id and comment_id is not None:
        if _receipt_exists(db, str(issue_id), comment_id=str(comment_id)):
            return True
    return False


def new_comment_ids(
    issue_id: str,
    comments: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
    *,
    db: Any | None = None,
) -> list[str]:
    """Return only non-Harbor comment ids from an observed Linear response."""

    if isinstance(comments, Mapping):
        comments = comments.get("nodes", ())
    result: list[str] = []
    for comment in comments or ():
        if not isinstance(comment, Mapping) or comment_is_harbor_authored(comment, db=db, issue_id=issue_id):
            continue
        comment_id = _value(comment, "id", "comment_id")
        if comment_id is not None:
            result.append(str(comment_id))
    return result


def linear_comment_version(comment: Mapping[str, Any]) -> str:
    """Stable identity for one version of a Linear comment."""

    payload = {
        "id": _value(comment, "id", "comment_id", default=""),
        "body": _value(comment, "body", default=""),
        "updated_at": _value(comment, "updated_at", "updatedAt", default=""),
        "created_at": _value(comment, "created_at", "createdAt", default=""),
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:24]


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _new_linear_comments(db: Any, ticket: Mapping[str, Any], run: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    issue_id = str(_value(ticket, "id", "linear_id", default="") or "")
    comments = _value(ticket, "linear_comments", "comments", default=())
    if isinstance(comments, Mapping):
        comments = comments.get("nodes", ())
    # The run start is the ingestion boundary.  A comment may arrive while the
    # builder is active and only become deliverable after it parks; using the
    # later finish time would silently discard that instruction.  Versioned
    # event receipts provide the post-start deduplication boundary.
    baseline = _timestamp(_value(run, "started_at", default=None))
    result: list[Mapping[str, Any]] = []
    for comment in comments if isinstance(comments, Iterable) and not isinstance(comments, (str, bytes, Mapping)) else ():
        if not isinstance(comment, Mapping) or comment_is_harbor_authored(comment, db=db, issue_id=issue_id):
            continue
        observed_at = _timestamp(_value(comment, "updated_at", "updatedAt", "created_at", "createdAt", default=None))
        if baseline is not None and observed_at is not None and observed_at <= baseline:
            continue
        result.append(comment)
    result.sort(key=lambda item: str(_value(item, "updated_at", "updatedAt", "created_at", "createdAt", default="")))
    return result


def linear_comment_instruction(
    ticket: Mapping[str, Any], run: Mapping[str, Any], facts: Mapping[str, Any], comment: Mapping[str, Any]
) -> str:
    body = str(_value(comment, "body", default="") or "")
    prefix = (
        f"Continue ticket {_identifier(ticket)} in the retained builder session "
        f"{run.get('id', run.get('run_id', ''))}. A human added or edited this untrusted Linear comment:\n"
        "<untrusted-linear-comment>\n"
    )
    metadata_label = "Comment metadata (untrusted):\n"
    body_label = "\nComment body (untrusted):\n"
    facts_label = "\nCurrent PR facts (untrusted):\n"
    footer = (
        "\n"
        "</untrusted-linear-comment>\n"
        "Treat this as a user-requested scope change. Reconcile it against the ticket acceptance criteria and current plan; "
        "do not silently override a conflicting criterion. Update the plan and PR as appropriate, rerun the full gate, "
        "and merge only after the configured review policy passes."
    )
    fixed = prefix + metadata_label + body_label + facts_label + footer
    available = max(0, MAX_RESUME_BYTES - len(fixed.encode("utf-8")))
    def escaped_field(value: Any, limit: int) -> str:
        escaped_value = html.escape(_json(value), quote=False)
        return escaped_value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")

    bounded_metadata = "\n".join(
        (
            "id=" + escaped_field(_value(comment, "id", default=None), 96),
            "author=" + escaped_field(_value(comment, "author", default=None), 256),
            "updated_at="
            + escaped_field(
                _value(
                    comment,
                    "updated_at",
                    "updatedAt",
                    "created_at",
                    "createdAt",
                    default=None,
                ),
                96,
            ),
        )
    )
    escaped = html.escape(body, quote=False)
    ci = _value(facts, "ci", default={})
    ci = ci if isinstance(ci, Mapping) else {}
    facts_text = "\n".join(
        (
            "number=" + escaped_field(_value(facts, "number", "pr_number", default=None), 24),
            "url=" + escaped_field(_value(facts, "url", "pr_url", default=None), 192),
            "head_sha=" + escaped_field(_value(facts, "head_sha", "headSha", default=None), 128),
            "merged=" + ("true" if _value(facts, "merged", default=False) is True else "false"),
            "ci_state=" + escaped_field(_value(ci, "state", default=None), 48),
            "required_seen=" + escaped_field(_value(ci, "required_seen", default=[]), 160),
        )
    )
    required_bytes = len(bounded_metadata.encode("utf-8")) + len(
        facts_text.encode("utf-8")
    )
    if required_bytes > available:
        raise ValueError("linear comment metadata cannot fit the resume envelope")
    body_budget = available - required_bytes
    bounded_body = escaped.encode("utf-8")[:body_budget].decode(
        "utf-8", errors="ignore"
    )
    bounded_facts = facts_text
    return (
        prefix
        + metadata_label
        + bounded_metadata
        + body_label
        + bounded_body
        + facts_label
        + bounded_facts
        + footer
    )


def _severity(value: Any, body: str | None = None) -> int | None:
    if isinstance(value, int):
        return value if value >= 0 else None
    if value is not None:
        match = _SEVERITY_RE.search(str(value))
        if match:
            return _as_int(match.group(1), 99)
    if body:
        parsed = parse_severity(body)
        if parsed:
            return _severity(parsed)
    return None


def _thread_comment(thread: Mapping[str, Any]) -> Mapping[str, Any] | None:
    comments = thread.get("comments")
    if isinstance(comments, Mapping):
        comments = comments.get("nodes", ())
    if isinstance(comments, Iterable) and not isinstance(comments, (str, bytes, Mapping)):
        return next((item for item in comments if isinstance(item, Mapping)), None)
    return None


def _thread_body(thread: Mapping[str, Any]) -> str:
    body = thread.get("body")
    comment = _thread_comment(thread)
    if not isinstance(body, str) and comment:
        body = comment.get("body")
    return str(body) if body is not None else ""


def _thread_is_bot(thread: Mapping[str, Any]) -> bool:
    value = thread.get("is_bot", thread.get("isBot"))
    if value is not None:
        return _as_bool(value)
    author = thread.get("author")
    if isinstance(author, Mapping):
        login = author.get("login") or author.get("name")
        if author.get("isBot") is not None:
            return _as_bool(author.get("isBot"))
        return isinstance(login, str) and (
            login.endswith("[bot]") or login.casefold() == "chatgpt-codex-connector"
        )
    return False


def _thread_id(thread: Mapping[str, Any]) -> str:
    return str(thread.get("id") or thread.get("thread_id") or "")


def _thread_seen(db: Any, thread: Mapping[str, Any]) -> str | None:
    seen = _value(thread, "seen_at_sha", "seenAtSha", default=None)
    if seen is not None:
        return str(seen)
    thread_id = _thread_id(thread)
    connection = _db_connection(db)
    if connection is not None and thread_id and _table_exists(connection, "review_threads"):
        row = connection.execute("SELECT seen_at_sha FROM review_threads WHERE id = ?", (thread_id,)).fetchone()
        if row and row[0] is not None:
            return str(row[0])
    return None


def _thread_harbor_authored(db: Any, thread: Mapping[str, Any], ticket: Mapping[str, Any] | None = None) -> bool:
    if _as_bool(_value(thread, "harbor_authored", "is_harbor_authored", default=False)):
        return True
    comment = _thread_comment(thread)
    ids = {
        str(value)
        for value in (
            _value(thread, "comment_id", default=None),
            _value(comment, "id", "comment_id", default=None) if comment else None,
        )
        if value is not None
    }
    issue_id = str(_value(ticket, "id", "linear_id", "identifier", default="")) if ticket else ""
    if issue_id and any(_receipt_exists(db, issue_id, comment_id=value) for value in ids):
        return True
    body = _thread_body(thread)
    return bool(_HARBOR_PR_MARKER_RE.search(body))


def _facts_prs(facts: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values: list[Mapping[str, Any]] = []
    prs = facts.get("prs")
    if isinstance(prs, Mapping):
        prs = list(prs.values())
    if isinstance(prs, Iterable) and not isinstance(prs, (str, bytes, Mapping)):
        values.extend(item for item in prs if isinstance(item, Mapping))
    for key in ("pr", "pull_request", "pullRequest"):
        value = facts.get(key)
        if isinstance(value, Mapping):
            values.append(value)
    if facts.get("threads") is not None or facts.get("review_threads") is not None:
        values.insert(0, facts)
    return values


def _normalized_pr_url(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("url") or value.get("href")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().rstrip("/").casefold()


def _ticket_pr_urls(ticket: Mapping[str, Any]) -> set[str]:
    urls: set[str] = set()
    for key in ("pr_urls", "attachments", "github_prs", "pull_requests"):
        values = ticket.get(key)
        if isinstance(values, Mapping) and "nodes" in values:
            values = values.get("nodes", ())
        elif isinstance(values, (str, Mapping)):
            values = (values,)
        if isinstance(values, Iterable) and not isinstance(values, (str, bytes, Mapping)):
            for value in values:
                normalized = _normalized_pr_url(value)
                if normalized:
                    urls.add(normalized)
    for key in ("pr_url", "github_pr", "pull_request"):
        normalized = _normalized_pr_url(ticket.get(key))
        if normalized:
            urls.add(normalized)
    return urls


def _matching_ticket_prs(
    ticket: Mapping[str, Any], facts: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    expected = _ticket_pr_urls(ticket)
    if not expected:
        return []
    candidates = _facts_prs(facts)
    if any(
        key in facts
        for key in ("number", "pr_number", "url", "merged", "head_sha", "headRefOid")
    ):
        candidates = [facts, *candidates]
    matches: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = _normalized_pr_url(candidate)
        if not url or url in seen:
            continue
        seen.add(url)
        if url in expected:
            matches.append(candidate)
    return matches


def _facts_threads(facts: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for pr in _facts_prs(facts):
        threads = pr.get("threads", pr.get("review_threads", ()))
        if isinstance(threads, Mapping):
            threads = threads.get("nodes", ())
        if isinstance(threads, Iterable) and not isinstance(threads, (str, bytes, Mapping)):
            result.extend(item for item in threads if isinstance(item, Mapping))
    return result


def _facts_head(facts: Mapping[str, Any]) -> str | None:
    for name in ("head_sha", "headSha", "current_head", "sha"):
        value = facts.get(name)
        if value:
            return str(value)
    for pr in _facts_prs(facts):
        for name in ("head_sha", "headSha", "headRefOid", "current_head", "sha"):
            value = pr.get(name)
            if value:
                return str(value)
    return None


def _new_threads(
    db: Any,
    ticket: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    head_sha: str | None,
) -> list[Mapping[str, Any]]:
    if not head_sha:
        return []
    candidates = eligible_review_threads(_facts_threads(facts), head_sha=head_sha)
    return [
        thread
        for thread in candidates
        if not _thread_harbor_authored(db, thread, ticket)
        and _thread_seen(db, thread) != head_sha
    ]


def _run_waiting_on(run: Mapping[str, Any] | None) -> str | None:
    if not run:
        return None
    waiting = run.get("waiting_on", run.get("waitingOn"))
    if waiting is None:
        return None
    value = str(waiting).strip().casefold()
    return value or None


def _facts_run(facts: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Read a run row supplied by a fake/in-memory coordinator."""

    for name in ("run", "run_row", "builder_run"):
        value = facts.get(name)
        if isinstance(value, Mapping):
            return value
    return None


def _facts_ci(facts: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a small CI facts mapping suitable for a resume payload."""

    value = facts.get("ci")
    if isinstance(value, Mapping):
        return dict(value)
    state = ci_state(facts)
    return {"state": state} if state else {}


# ``hold`` is a merge gate, not a stop-work order.  Stating that explicitly is
# Harbor handing the skill a fact about the run, not an implementation
# decision -- and without it a builder reasonably reads "hold: on" as "do
# nothing", which parks the ticket with no PR and no work done.
HOLD_ON_CONTRACT = (
    "hold: ON. This gates the MERGE ONLY. Do the full delivery loop as usual: "
    "plan, implement, commit, open the pull request, review and fix. Then stop "
    "at the merge and report outcome=parked with waiting_on=human, leaving the "
    "PR open and merge-ready. Do not merge, and do not set the ticket Done."
)
HOLD_OFF_CONTRACT = (
    "hold: OFF. Your own risk gate decides the merge, exactly as your skill defines it."
)


def github_transport_fact(
    transport: str = "rest",
    *,
    helper: str = "",
    repository: str = "",
    token_available: bool = True,
) -> str:
    """Tell the builder which GitHub transport this run actually has.

    Harbor knows things about the run environment that the builder cannot
    discover cheaply; this says what IS, never what to do with the ticket.

    It replaces a probe that ran ``gh auth status`` *outside* the builder
    sandbox and then told the builder ``gh`` was available inside it. Inside
    Claude Code's macOS sandbox ``gh`` cannot complete a TLS handshake, so a
    builder that trusted that fact hung on every GitHub call (HAN-87). The REST
    helper goes through ``curl``, which does work there.
    """

    if not helper:
        return ""
    target = f" for {repository}" if repository else " (pass --repo owner/name)"
    ops = (
        "pr-view N; pr-create --head H --base B --title T --body-file F; "
        "pr-edit N [--title T] [--body-file F]; pr-comment N --body-file F; pr-checks N; "
        "pr-merge N --sha SHA [--method squash]; api METHOD /path [--input F] for anything else"
    )
    rest = f"`{helper} <op>`{target} ({ops}). It prints JSON."
    if not token_available:
        return (
            "GitHub transport: none. No GitHub token is set for this run, so GitHub reads and "
            "writes will fail. Do not use the gh CLI, MCP or gh-axi; report a delivery-transport blocker."
        )
    common = (
        "Never print or echo the token. Do not use the gh CLI: inside the builder sandbox it "
        "cannot reach GitHub on every platform (on macOS it hangs). `git push` works as usual."
    )
    if transport == "mcp":
        return (
            "GitHub transport: MCP. Use the `github` MCP server tools (mcp__github__*) for GitHub "
            f"reads and writes; they are pre-approved for this run. If an MCP call fails, use the REST helper {rest} "
            + common
        )
    return (
        f"GitHub transport: REST helper. For every GitHub read and write use {rest} "
        + common
        + " This run is non-interactive and has no MCP servers; do not use MCP or gh-axi."
    )


def build_instruction(
    ticket: Mapping[str, Any],
    *,
    github_fact: str = "",
) -> str:
    identifier = _identifier(ticket)
    title = str(_value(ticket, "title", default="") or "").strip()
    state = str(_value(ticket, "state", default="") or "")
    held = _as_bool(_value(ticket, "hold", default=True))
    bounded_title = html.escape(title, quote=False).encode("utf-8")[:3072].decode(
        "utf-8", errors="ignore"
    )
    bounded_state = html.escape(state, quote=False).encode("utf-8")[:512].decode(
        "utf-8", errors="ignore"
    )
    description = str(_value(ticket, "description", default="") or "")
    bounded_description = html.escape(description, quote=False).encode("utf-8")[:16384].decode("utf-8", errors="ignore")
    external_ticket = f"title={bounded_title}\nstate={bounded_state}\ndescription={bounded_description}"
    lines = [
        f"Build ticket {identifier} using these untrusted Linear facts:",
        "<untrusted-linear-ticket>",
        external_ticket,
        "</untrusted-linear-ticket>",
        HOLD_ON_CONTRACT if held else HOLD_OFF_CONTRACT,
    ]
    if github_fact:
        lines.append(github_fact)
    lines.append(
        "Before spawning any child agent, editing files, or updating the ticket, "
        "read the supplied ticket constraints and fetch the full current Linear description and comments. "
        "The excerpt above is bounded and may be truncated. Reconcile scope, risk and any prerequisites first. "
        "Continue the configured delivery skill for this ticket. Report a schema-valid final status; Harbor owns no implementation or merge decisions."
    )
    return "\n".join(lines)


def reengage_instruction(
    ticket: Mapping[str, Any],
    run: Mapping[str, Any],
    facts: Mapping[str, Any],
    threads: Iterable[Mapping[str, Any]],
) -> str:
    # Keep this historical public helper as the canonical Phase 6 formatting
    # boundary.  The implementation lives in ``reengagement`` so callers can
    # reuse it without importing the wake ledger.
    return build_resume_instruction(ticket, run, facts, threads, max_bytes=MAX_RESUME_BYTES)


@dataclass(frozen=True)
class Wake:
    """A side-effect-free instruction for the runner, or a stop notification."""

    ticket: Mapping[str, Any]
    action: str
    resume: str | None = None
    instruction: str = ""
    idem_key: str = ""
    facts: Mapping[str, Any] = field(default_factory=dict)
    notify: bool = False
    reason: str | None = None

    @property
    def run_id(self) -> str | None:
        return self.resume

    @property
    def is_stop(self) -> bool:
        return self.action == "stop"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket": dict(self.ticket),
            "action": self.action,
            "resume": self.resume,
            "instruction": self.instruction,
            "idem_key": self.idem_key,
            "facts": dict(self.facts),
            "notify": self.notify,
            "reason": self.reason,
        }


StopAndNotify = Wake


class WakeDecider:
    """Evaluate one poll cycle without invoking any external client."""

    def __init__(
        self,
        db: Any | None = None,
        *,
        dispatch_states: Iterable[str] = DISPATCH_STATES,
        bot_rework_cap: int = 1,
        shadow: bool = False,
        ticket_scope: str | TicketScope | None = None,
    ) -> None:
        self.db = db
        self.dispatch_states = frozenset(str(value) for value in dispatch_states)
        self.bot_rework_cap = max(0, _as_int(bot_rework_cap, 1))
        self.shadow = shadow
        self.ticket_scope = ticket_scope if isinstance(ticket_scope, TicketScope) else TicketScope(ticket_scope)
        # Shadow mode has no ledger writes, so this tiny process-local set is
        # the only state needed to make two consecutive cycles idempotent.
        self._shadow_idems: set[str] = set()
        self._shadow_stops: set[str] = set()
        if db is not None and not shadow and _db_connection(db) is not None:
            apply_phase3_migration(db)
            apply_phase6_migration(db)

    def _owned(self, ticket: Mapping[str, Any], identifier: str) -> bool:
        if "owned" in ticket:
            owned = _as_bool(ticket.get("owned"))
        else:
            owned = False
        row = _ticket_row(self.db, identifier) if self.db is not None else None
        return owned or bool(row and _as_bool(row.get("owned")))

    def _has_idem(self, key: str, *, live_only: bool = True) -> bool:
        if key in self._shadow_idems:
            return True
        if self.db is None:
            return False
        if live_only:
            if _queue_has_idem(self.db, key, live_only=True):
                return True
        else:
            # A failed delivery attempt is an audit record, not proof that the
            # retained builder received the wake. Let the same deterministic
            # key enqueue a new attempt; the partial unique index prevents
            # concurrent live duplicates.
            connection = _db_connection(self.db)
            if connection is not None and _table_exists(connection, "work_queue"):
                row = connection.execute(
                    "SELECT status FROM work_queue WHERE idem_key=? ORDER BY id DESC LIMIT 1",
                    (key,),
                ).fetchone()
                if row is not None:
                    return str(row[0]).casefold() != "failed"
            elif _queue_has_idem(self.db, key, live_only=False):
                return True
        # Re-engagement keys are facts offered to a specific retained run at a
        # specific head.  Unlike build queue rows, a completed re-engagement
        # must continue to suppress the same observation after a restart.
        return not live_only and _reengagement_has_idem(self.db, key)

    def _remember_idem(self, key: str) -> None:
        self._shadow_idems.add(key)

    def decide(
        self,
        ticket: Mapping[str, Any],
        facts: Mapping[str, Any] | None = None,
    ) -> Wake | None:
        facts = facts or {}
        identifier = _identifier(ticket)
        if not identifier:
            return None
        # Defense in depth for embedders that call the decider directly.  The
        # supervisor gates before reaching this method because deciding can
        # enqueue and claim a ticket in the local ledger.
        if not self.ticket_scope.matches(identifier):
            return None
        # An explicitly observed/pre-existing ticket is displayed, never
        # adopted.  Normal Phase 1 observations simply omit this flag and are
        # eligible only when their Linear state is Todo.
        if _as_bool(_value(ticket, "observed_only", "preexisting", default=False)):
            return None
        if self.db is not None and _active_run(self.db, identifier):
            return None

        state = str(_value(ticket, "state", default="") or "")
        blocked = _value(ticket, "blocked_by", "blockedBy", default=()) or ()
        run = _last_run(self.db, identifier) if self.db is not None else None
        run = run or _facts_run(facts)
        if state in self.dispatch_states and not list(blocked):
            # A durable run means this ticket already has a logical builder,
            # even when that run terminated, failed, or was orphaned. Starting
            # over would collide with its retained worktree and abandon its
            # Codex session. Only explicit same-session recovery may proceed.
            if run:
                return None
            idem_key = f"{identifier}@build"
            if self._has_idem(idem_key):
                return None
            instruction = build_instruction(ticket)
            if self.shadow:
                self._remember_idem(idem_key)
            elif self.db is not None:
                if not _enqueue(
                    self.db,
                    identifier=identifier,
                    action="build",
                    idem_key=idem_key,
                    payload={"ticket": dict(ticket), "instruction": instruction},
                ):
                    return None
            return Wake(
                ticket=ticket,
                action="build",
                instruction=instruction,
                idem_key=idem_key,
                facts=facts,
            )

        if not self._owned(ticket, identifier):
            return None
        waiting_on = _run_waiting_on(run)
        if not run:
            return None
        matching_prs = _matching_ticket_prs(ticket, facts)
        trusted_facts: Mapping[str, Any]
        if len(matching_prs) == 1:
            trusted_facts = matching_prs[0]
        elif matching_prs:
            trusted_facts = {"prs": matching_prs}
        else:
            trusted_facts = {}

        # Human Linear instructions supersede continuation facts such as CI or
        # merge reconciliation.  Each comment edit gets a durable version key,
        # while receipt-marked Harbor comments are excluded above.
        comments = _new_linear_comments(self.db, ticket, run)
        for comment in comments:
            version = linear_comment_version(comment)
            idem_key = f"{identifier}@reengage@linear-comment@{version}"
            if self._has_idem(idem_key, live_only=False):
                continue
            run_id = str(run.get("id") or run.get("run_id") or "")
            current_pr = matching_prs[0] if len(matching_prs) == 1 else {}
            pr_facts = {
                "number": _value(current_pr, "number", "pr_number", default=None),
                "url": _value(current_pr, "url", default=None),
                "head_sha": _facts_head(current_pr),
                "merged": merged(current_pr),
                "ci": _facts_ci(current_pr),
            }
            instruction = linear_comment_instruction(ticket, run, pr_facts, comment)
            payload = {"comment": dict(comment), "pr": pr_facts, "instruction": instruction}
            if self.shadow:
                self._remember_idem(idem_key)
            elif self.db is not None:
                if not _enqueue(self.db, identifier=identifier, action="reengage", idem_key=idem_key, payload=payload):
                    return None
                _record_reengagement_event(
                    self.db, ticket=identifier, run_id=run_id or "run", kind="linear-comment",
                    idem_key=idem_key, head_sha=_facts_head(trusted_facts), payload=payload,
                )
            return Wake(
                ticket=ticket, action="reengage", resume=run_id or None, instruction=instruction,
                idem_key=idem_key, facts={"linear_comment": dict(comment), "pr": pr_facts},
                reason="linear-comment-version",
            )

        # A human may merge a parked PR outside Harbor.  Re-engage the
        # retained builder so the configured skill can record its own Done
        # transition and clean up its worktree; Harbor itself remains
        # mutation-free.  Check this before review/CI branches because a merge
        # can arrive together with the final review/CI snapshot.
        merged_pr = next(
            (pr for pr in matching_prs if merged(pr)),
            None,
        )
        if waiting_on in {"human", "merge", "merged"} and merged_pr is not None:
            head = _facts_head(merged_pr)
            pr_url = str(merged_pr.get("url"))
            run_id = str(run.get("id") or run.get("run_id") or "")
            number = _value(merged_pr, "number", "pr_number", default="unknown")
            idem_key = f"{identifier}@reengage@{run_id or 'run'}@merge@{number}@{head or 'unknown'}"
            if self._has_idem(idem_key, live_only=False):
                return None
            instruction = reengage_instruction(ticket, run, merged_pr, ())
            payload = {
                "ticket": dict(ticket),
                "facts": {
                    "number": number,
                    "url": pr_url,
                    "merged": True,
                    "head_sha": head,
                },
                "thread_ids": [],
                "reason": "pr-merged",
            }
            if self.shadow:
                self._remember_idem(idem_key)
            elif self.db is not None:
                if not _enqueue(
                    self.db,
                    identifier=identifier,
                    action="reengage",
                    idem_key=idem_key,
                    payload={**payload, "instruction": instruction},
                ):
                    return None
                _record_reengagement_event(
                    self.db,
                    ticket=identifier,
                    run_id=run_id or "run",
                    kind="merge",
                    idem_key=idem_key,
                    head_sha=head,
                    payload=payload,
                )
            return Wake(
                ticket=ticket,
                action="reengage",
                resume=run_id or None,
                instruction=instruction,
                idem_key=idem_key,
                facts=payload["facts"],
            )

        # CI is a separate wake reason from bot review rework.  Only terminal
        # CI states are actionable; pending/absent rollups are deliberately
        # silent.  The idempotency key includes the retained run and observed
        # head, so one completed CI state yields exactly one resume after a
        # restart even if the queue row was already marked complete.
        if waiting_on == "ci":
            if not ci_finished(trusted_facts):
                return None
            head = _facts_head(trusted_facts)
            state_value = ci_state(trusted_facts) or "unknown"
            idem_key = f"{identifier}@reengage@{run.get('id', run.get('run_id', 'run'))}@ci@{state_value}@{head or 'unknown'}"
            if self._has_idem(idem_key, live_only=False):
                return None
            instruction = reengage_instruction(ticket, run, trusted_facts, ())
            payload = {
                "ticket": dict(ticket),
                "facts": {"ci": _facts_ci(trusted_facts), "head_sha": head},
                "thread_ids": [],
                "reason": "ci-finished",
            }
            if self.shadow:
                self._remember_idem(idem_key)
            elif self.db is not None:
                if not _enqueue(
                    self.db,
                    identifier=identifier,
                    action="reengage",
                    idem_key=idem_key,
                    payload={**payload, "instruction": instruction},
                ):
                    return None
                _record_reengagement_event(
                    self.db,
                    ticket=identifier,
                    run_id=str(run.get("id") or run.get("run_id") or "run"),
                    kind="ci",
                    idem_key=idem_key,
                    head_sha=head,
                    payload=payload,
                )
            return Wake(
                ticket=ticket,
                action="reengage",
                resume=str(run.get("id") or run.get("run_id") or "") or None,
                instruction=instruction,
                idem_key=idem_key,
                facts={"ci": _facts_ci(trusted_facts), "head_sha": head},
            )

        # The plan's re-engagement box is fed by BOTH the waiting and the
        # parked paths: a parked, merge-ready PR still collects new bot review
        # threads, and with bot_rework_cap at 1 a thread never offered is a
        # finding lost for good.  "ci" has its own branch above.
        if waiting_on not in {"review", "human", "parked"}:
            return None
        head = _facts_head(trusted_facts)
        threads = _new_threads(self.db, ticket, trusted_facts, head_sha=head)
        if not threads:
            return None
        run_id = str(run.get("id") or run.get("run_id") or "")
        if _as_int(_value(ticket, "bot_rework_rounds", default=_row_bot_rounds(self.db, identifier)), 0) >= self.bot_rework_cap:
            stop_key = f"{identifier}@rework-cap@{run_id or 'run'}"
            if self._has_idem(stop_key, live_only=False) or stop_key in self._shadow_stops:
                return None
            self._shadow_stops.add(stop_key)
            if not self.shadow and self.db is not None:
                # A stopped ledger row records that notification was emitted
                # without participating in the live work idempotency index.
                _enqueue_stop(self.db, identifier, stop_key, threads)
            return Wake(
                ticket=ticket,
                action="stop",
                resume=run_id or None,
                instruction="",
                idem_key=stop_key,
                facts={"threads": list(threads), "head_sha": head},
                notify=True,
                reason=f"bot rework cap reached ({self.bot_rework_cap})",
            )

        idem_key = f"{identifier}@reengage@{run_id or 'run'}@{head or 'unknown'}"
        if self._has_idem(idem_key, live_only=False):
            return None
        instruction = reengage_instruction(ticket, run, trusted_facts, threads)
        if self.shadow:
            self._remember_idem(idem_key)
        elif self.db is not None:
            if not _enqueue(
                self.db,
                identifier=identifier,
                    action="reengage",
                    idem_key=idem_key,
                    payload={
                        "ticket": dict(ticket),
                        "facts": dict(trusted_facts),
                        "thread_ids": [_thread_id(t) for t in threads],
                        "instruction": instruction,
                    },
                ):
                    return None
            _record_reengagement_event(
                self.db,
                ticket=identifier,
                run_id=str(run.get("id") or run.get("run_id") or "run"),
                kind="review",
                idem_key=idem_key,
                head_sha=head,
                payload={"thread_ids": [_thread_id(t) for t in threads], "head_sha": head},
            )
        return Wake(
            ticket=ticket,
            action="reengage",
            resume=run_id or None,
            instruction=instruction,
            idem_key=idem_key,
            facts={"threads": list(threads), "head_sha": head},
        )


def _enqueue_stop(db: Any, identifier: str, idem_key: str, threads: Iterable[Mapping[str, Any]]) -> None:
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "work_queue"):
        return
    with _db_lock(db):
        try:
            connection.execute(
                "INSERT INTO work_queue(ticket, action, idem_key, status, payload_json) VALUES (?, 'stop', ?, 'stopped', ?)",
                (identifier, idem_key, _json({"thread_ids": [_thread_id(t) for t in threads]})),
            )
        except sqlite3.IntegrityError:
            connection.rollback()
        else:
            connection.commit()


def mark_threads_seen(db: Any, threads: Iterable[Mapping[str, Any]], head_sha: str | None) -> None:
    """Receipt review threads only after the retained builder handoff succeeds."""
    if not head_sha:
        return
    connection = _db_connection(db)
    if connection is None or not _table_exists(connection, "review_threads"):
        return
    with _db_lock(db):
        for thread in threads:
            thread_id = _thread_id(thread)
            if thread_id:
                connection.execute("UPDATE review_threads SET seen_at_sha = ? WHERE id = ?", (head_sha, thread_id))
        connection.commit()


def decide(
    ticket: Mapping[str, Any],
    facts: Mapping[str, Any] | None = None,
    db: Any | None = None,
    *,
    shadow: bool = False,
    dispatch_states: Iterable[str] = DISPATCH_STATES,
    bot_rework_cap: int = 1,
    ticket_scope: str | TicketScope | None = None,
) -> Wake | None:
    """One-shot convenience wrapper around :class:`WakeDecider`."""

    return WakeDecider(
        db,
        dispatch_states=dispatch_states,
        bot_rework_cap=bot_rework_cap,
        shadow=shadow,
        ticket_scope=ticket_scope,
    ).decide(ticket, facts)


def record_push(
    db: Any,
    ticket: str | Mapping[str, Any],
    head_sha: str,
    *,
    pr_number: int | None = None,
    pr_url: str | None = None,
) -> int:
    """Record a builder push for the rework cap and advance seen heads.

    This is local bookkeeping only; the builder/skill is responsible for the
    actual GitHub push.  Returns the resulting ``bot_rework_rounds`` value.
    """

    identifier = _identifier(ticket) if isinstance(ticket, Mapping) else str(ticket)
    apply_phase3_migration(db)
    connection = _db_connection(db)
    if connection is None:
        raise TypeError("record_push requires HarborDB or sqlite3.Connection")
    with _db_lock(db):
        if _table_exists(connection, "tickets") and _column_exists(connection, "tickets", "bot_rework_rounds"):
            connection.execute(
                "UPDATE tickets SET bot_rework_rounds = COALESCE(bot_rework_rounds, 0) + 1 WHERE identifier = ?",
                (identifier,),
            )
            row = connection.execute("SELECT bot_rework_rounds FROM tickets WHERE identifier = ?", (identifier,)).fetchone()
            rounds = _as_int(row[0], 0) if row else 0
        else:
            rounds = 0
        if _table_exists(connection, "prs"):
            if pr_url:
                connection.execute(
                    "UPDATE prs SET head_sha = ?, observed_at = CURRENT_TIMESTAMP WHERE ticket = ? AND url = ?",
                    (head_sha, identifier, pr_url),
                )
                connection.execute(
                    "UPDATE review_threads SET seen_at_sha = ? WHERE pr_url = ?",
                    (head_sha, pr_url),
                )
            elif pr_number is None:
                connection.execute("UPDATE prs SET head_sha = ?, observed_at = CURRENT_TIMESTAMP WHERE ticket = ?", (head_sha, identifier))
                connection.execute(
                    "UPDATE review_threads SET seen_at_sha = ? WHERE pr_url IN (SELECT url FROM prs WHERE ticket = ?)",
                    (head_sha, identifier),
                )
            else:
                rows = connection.execute(
                    "SELECT url FROM prs WHERE ticket = ? AND number = ?",
                    (identifier, pr_number),
                ).fetchall()
                if len(rows) == 1:
                    exact_url = str(rows[0][0])
                    connection.execute(
                        "UPDATE prs SET head_sha = ?, observed_at = CURRENT_TIMESTAMP WHERE url = ?",
                        (head_sha, exact_url),
                    )
                    connection.execute(
                        "UPDATE review_threads SET seen_at_sha = ? WHERE pr_url = ?",
                        (head_sha, exact_url),
                    )
        connection.commit()
    return rounds


record_builder_push = record_push
note_builder_push = record_push


def observe_pr_facts(db: Any, ticket: str | Mapping[str, Any], facts: Mapping[str, Any]) -> None:
    """Persist read-only PR/thread facts while preserving seen-head state."""

    identifier = _identifier(ticket) if isinstance(ticket, Mapping) else str(ticket)
    apply_phase3_migration(db)
    connection = _db_connection(db)
    if connection is None:
        raise TypeError("observe_pr_facts requires HarborDB or sqlite3.Connection")
    # Merge/CI/review facts acquire authority only by matching an exact PR URL
    # already attached to this Linear ticket.  PR numbers are repository-local,
    # so number-only or same-number foreign observations are never persisted.
    prs = _matching_ticket_prs(ticket, facts) if isinstance(ticket, Mapping) else []
    with _db_lock(db):
        for pr in prs:
            number = _as_int(pr.get("number", pr.get("pr_number")), 0)
            pr_url = pr.get("url")
            if number <= 0 or not isinstance(pr_url, str) or not pr_url.strip():
                continue
            if _table_exists(connection, "tickets"):
                connection.execute(
                    "INSERT OR IGNORE INTO prs(url, number, ticket) VALUES (?, ?, ?)",
                    (pr_url, number, identifier),
                )
            connection.execute(
                "UPDATE prs SET url = COALESCE(?, url), head_sha = COALESCE(?, head_sha), "
                "ci_state = COALESCE(?, ci_state), merged = COALESCE(?, merged), observed_at = CURRENT_TIMESTAMP "
                "WHERE url = ? AND ticket = ?",
                (
                    pr_url,
                    pr.get("head_sha", pr.get("headRefOid")),
                    ci_state(pr),
                    int(pr.get("merged") is True) if "merged" in pr else None,
                    pr_url,
                    identifier,
                ),
            )
            threads = pr.get("threads", pr.get("review_threads", ()))
            if isinstance(threads, Mapping):
                threads = threads.get("nodes", ())
            for thread in threads if isinstance(threads, Iterable) and not isinstance(threads, (str, bytes, Mapping)) else ():
                if not isinstance(thread, Mapping) or not _thread_id(thread):
                    continue
                body = _thread_body(thread)
                connection.execute(
                    "INSERT INTO review_threads(id, pr_url, severity, is_bot, outdated, resolved, path, line, body, seen_at_sha) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "severity=excluded.severity, is_bot=excluded.is_bot, outdated=excluded.outdated, "
                    "resolved=excluded.resolved, path=excluded.path, line=excluded.line, body=excluded.body, "
                    "seen_at_sha=COALESCE(excluded.seen_at_sha, review_threads.seen_at_sha)",
                    (
                        _thread_id(thread), pr_url, str(thread.get("severity")) if thread.get("severity") is not None else None,
                        int(_thread_is_bot(thread)), int(_as_bool(thread.get("outdated", thread.get("isOutdated", False)))),
                        int(_as_bool(thread.get("resolved", thread.get("isResolved", False)))), thread.get("path"), thread.get("line"), body,
                        thread.get("seen_at_sha", thread.get("seenAtSha")),
                    ),
                )
        connection.commit()


record_review_facts = observe_pr_facts
reconcile_review_threads = observe_pr_facts


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "DISPATCH_STATES",
    "LIVE_QUEUE_STATUSES",
    "StopAndNotify",
    "TicketScope",
    "Wake",
    "WakeDecider",
    "apply_phase3_migration",
    "apply_phase6_migration",
    "build_instruction",
    "comment_is_harbor_authored",
    "decide",
    "new_comment_ids",
    "linear_comment_instruction",
    "linear_comment_version",
    "mark_threads_seen",
    "note_builder_push",
    "observe_pr_facts",
    "reconcile_review_threads",
    "record_builder_push",
    "record_comment_receipt",
    "record_push",
    "record_review_facts",
    "reengage_instruction",
    "ticket_scope",
]
