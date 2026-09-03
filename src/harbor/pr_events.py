"""Read-only GitHub PR events and builder handoff contracts.

GitHub is an observation source in Harbor.  This module turns a normalized PR
fact into deterministic ``PR_CREATED`` and ``NEW_COMMENT_VERSION`` events and
offers those events to an already-running builder.  The builder/skill owns all
GitHub mutations; this module never imports a GitHub mutation operation.

The local SQLite tables are receipts only.  They make an observation safe to
replay after a restart while leaving GitHub, Linear, and the builder as the
authoritative systems for their respective state.
"""

from __future__ import annotations

import hashlib
import html
import inspect
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from harbor.facts.threads import is_harbor_marked


PR_CREATED = "PR_CREATED"
NEW_COMMENT_VERSION = "NEW_COMMENT_VERSION"
RISK_INVALIDATED = "RISK_INVALIDATED"
COMMENT_VERSION = NEW_COMMENT_VERSION  # descriptive compatibility alias

_HARBOR_REPLY_MARKER = re.compile(
    r"(?:<!--\s*harbor(?:\b|[-_:])|\[harbor\]|(?:^|\s)harbor\s*:)",
    re.IGNORECASE,
)
_DEFAULT_RISK_SKILL = "risk-analysis"
_DEFAULT_MAX_INSTRUCTION_BYTES = 4096


class PREventError(RuntimeError):
    """Base error for local PR event processing."""


class CommentContractError(PREventError):
    """A builder did not return the required comment disposition contract."""

    def __init__(self, errors: Iterable[str], *, response: Any = None) -> None:
        self.errors = tuple(str(error) for error in errors)
        self.response = response
        super().__init__("invalid comment response: " + "; ".join(self.errors))


class RiskContractError(PREventError):
    """A risk result does not prove it was written at the current head."""


def _connection(db: Any) -> sqlite3.Connection | None:
    if isinstance(db, sqlite3.Connection):
        return db
    value = getattr(db, "connection", None)
    return value if isinstance(value, sqlite3.Connection) else None


def _lock(db: Any) -> Any:
    return getattr(db, "_lock", None) or nullcontext()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _value(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _text(value: Any, default: str = "") -> str:
    return str(value) if value is not None else default


def _pr_number(facts: Mapping[str, Any]) -> int:
    value = _value(facts, "number", "pr_number", "pull_request_number")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("PR facts require a positive number") from exc
    if number <= 0:
        raise ValueError("PR facts require a positive number")
    return number


def _ticket_id(ticket: Any) -> str:
    return _text(_value(ticket, "identifier", "ticket", "id", default=""))


def _head(facts: Mapping[str, Any]) -> str | None:
    value = _value(facts, "head_sha", "headSha", "headRefOid", "current_head", "sha")
    return _text(value) if value else None


def _surface_comments(facts: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = _value(facts, "comments", "comment_versions", "external_comments", default=())
    if isinstance(value, Mapping):
        value = value.get("nodes", ())
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def pr_key(ticket: Any, facts: Mapping[str, Any]) -> str:
    """Stable local identity for a linked PR (ticket + number)."""

    return f"{_ticket_id(ticket)}:pr:{_pr_number(facts)}"


def comment_version_key(comment: Mapping[str, Any]) -> str:
    """Return a deterministic version identity for one external comment.

    GitHub's ``updatedAt`` is useful but not sufficient in fakes/webhooks that
    omit it.  Including body content guarantees an edited body is a new
    version, while keeping repeated identical observations idempotent.
    """

    comment_id = _text(_value(comment, "id", "comment_id", "database_id", default=""))
    body = _text(_value(comment, "body", "text", default=""))
    updated = _text(_value(comment, "updated_at", "updatedAt", default=""))
    created = _text(_value(comment, "created_at", "createdAt", default=""))
    digest = hashlib.sha256(
        _json(
            {
                "body": body,
                "updated_at": updated,
                "created_at": created,
                "surface": _value(comment, "surface", "source", default=""),
                "path": _value(comment, "path", default=""),
                "thread_id": _value(comment, "thread_id", default=""),
            }
        ).encode("utf-8")
    ).hexdigest()
    # Comments without an API id still need a stable identity.  The digest is
    # retained as the id in that narrow adapter case.
    return f"{comment_id or 'comment'}:{digest}"


def _event_key(event_type: str, key: str) -> str:
    return f"{event_type}:{key}"


def _risk_skill(profile: Any | None, skill: str | None = None) -> str:
    if skill and str(skill).strip():
        return str(skill).strip()
    for name in ("risk_analysis_skill", "risk_skill", "risk_analysis"):
        value = _value(profile, name, default=None)
        if value and str(value).strip():
            return str(value).strip()
    return _DEFAULT_RISK_SKILL


def _run_id(ticket: Any, facts: Mapping[str, Any], run: Any | None = None) -> str | None:
    value = _value(run, "id", "run_id", "session_id", default=None) if run is not None else None
    value = value or _value(
        ticket,
        "builder_run_id",
        "run_id",
        "builder_session_id",
        "session_id",
        default=None,
    )
    value = value or _value(facts, "builder_run_id", "run_id", "builder_session_id", default=None)
    return _text(value) if value else None


def _bounded(text: str, max_bytes: int = _DEFAULT_MAX_INSTRUCTION_BYTES) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[: max(0, max_bytes - 1)].decode("utf-8", errors="ignore")


def build_risk_instruction(
    ticket: Any,
    facts: Mapping[str, Any],
    *,
    skill: str | None = None,
    profile: Any | None = None,
    run: Any | None = None,
    max_bytes: int = 1200,
) -> str:
    """Build a compact same-builder instruction for a newly-created PR."""

    identifier = _ticket_id(ticket) or "(unknown ticket)"
    number = _pr_number(facts)
    head = _head(facts) or "unknown"
    run_id = _run_id(ticket, facts, run) or "the existing builder session"
    selected = _risk_skill(profile, skill)
    return _bounded(
        "Continue ticket "
        f"{identifier} in the existing builder session {run_id}. "
        f"PR #{number} was just created at head SHA {head}. "
        f"Run the configured risk skill `{selected}` against that exact current SHA, "
        "then write or update `## Risk Analysis` in the PR body at this SHA. "
        "If the head changes, stop and rerun risk analysis for the new SHA; report the result.",
        max_bytes,
    )


def build_comment_instruction(
    ticket: Any,
    facts: Mapping[str, Any],
    comment: Mapping[str, Any],
    *,
    run: Any | None = None,
    max_bytes: int = _DEFAULT_MAX_INSTRUCTION_BYTES,
) -> str:
    """Build bounded, explicitly untrusted input for the same builder."""

    identifier = _ticket_id(ticket) or "(unknown ticket)"
    head = _head(facts) or "unknown"
    run_id = _run_id(ticket, facts, run) or "the existing builder session"
    body = html.escape(_text(_value(comment, "body", "text", default="")), quote=False)
    surface = _text(_value(comment, "surface", "source", default="comment"))
    location = ""
    path = _value(comment, "path", default=None)
    line = _value(comment, "line", default=None)
    if path:
        location = f" path={path}"
        if line is not None:
            location += f" line={line}"
    text = (
        f"Continue ticket {identifier} in the existing builder session {run_id}. "
        f"Current PR head SHA: {head}. Review the external {surface} comment{location}.\n"
        "<external-comment>\n"
        f"{body}\n"
        "</external-comment>\n"
        "Return a structured disposition: valid requires the fix and tests plus a reply; "
        "invalid or out-of-scope requires evidence plus a reply; ambiguous requires a "
        "clarifying question or an explicit park decision plus a reply."
    )
    return _bounded(text, max_bytes)


def normalize_comment_response(response: Any) -> dict[str, Any]:
    """Validate and normalize the mandatory builder comment-response contract."""

    if not isinstance(response, Mapping):
        raise CommentContractError(("response must be an object",), response=response)
    classification = _text(
        _value(
            response,
            "classification",
            "disposition",
            "status",
            "kind",
            "action",
            "decision",
            default="",
        )
    ).strip().casefold().replace("-", "_").replace(" ", "_")
    if classification in {"fix", "addressed", "change", "valid_comment"}:
        classification = "valid"
    elif classification in {"outofscope", "out_of_scope", "not_applicable", "wontfix", "wont_fix"}:
        classification = "out_of_scope"
    elif classification in {"question", "needs_clarification", "clarification"}:
        classification = "ambiguous"
    errors: list[str] = []
    def present(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, Mapping)):
            return bool(value)
        return bool(str(value).strip())

    reply = _value(response, "reply", "reply_body", "response", "message", default=None)
    if not isinstance(reply, str) or not reply.strip():
        errors.append("reply is required for every external comment")
    if classification not in {"valid", "invalid", "out_of_scope", "ambiguous"}:
        errors.append("classification must be valid, invalid, out_of_scope, or ambiguous")
    if classification == "valid":
        fix = _value(response, "fix", "fix_applied", "change", "implementation", default=None)
        tests = _value(response, "tests", "tests_run", "test", "verification", default=None)
        if not present(fix):
            errors.append("valid response requires fix")
        if not present(tests):
            errors.append("valid response requires tests")
    elif classification in {"invalid", "out_of_scope"}:
        evidence = _value(response, "evidence", "reason", "rationale", "why", default=None)
        if not present(evidence):
            errors.append(f"{classification} response requires evidence")
    elif classification == "ambiguous":
        clarification = _value(
            response,
            "clarify",
            "clarification",
            "clarifying_question",
            "question",
            default=None,
        )
        parked = _value(response, "park", "parked", "park_reason", default=None)
        if not (str(clarification or "").strip() or str(parked or "").strip()):
            errors.append("ambiguous response requires clarify or park")
    if errors:
        raise CommentContractError(errors, response=response)
    normalized = dict(response)
    normalized["classification"] = classification
    normalized["reply"] = str(reply).strip()
    return normalized


validate_comment_response = normalize_comment_response


def validate_risk_result(
    result: Any,
    *,
    current_head: str | None,
) -> dict[str, Any]:
    """Require a builder risk result to name the current head and heading."""

    if not isinstance(result, Mapping):
        raise RiskContractError("risk result must be an object")
    result_head = _text(_value(result, "head_sha", "headSha", "sha", default=""))
    body = _text(_value(result, "body", "pr_body", "description", default=""))
    if current_head and result_head != current_head:
        raise RiskContractError(
            f"risk result head {result_head or '(missing)'} does not match current head {current_head}"
        )
    if "## Risk Analysis" not in body:
        raise RiskContractError("risk result must include ## Risk Analysis")
    return dict(result)


@dataclass(frozen=True)
class PREvent:
    """Immutable event handed to a builder or an operator integration."""

    event_type: str
    key: str
    ticket: str
    pr_number: int
    head_sha: str | None
    payload: Mapping[str, Any]
    instruction: str
    run_id: str | None = None

    @property
    def type(self) -> str:
        return self.event_type

    @property
    def event_key(self) -> str:
        return _event_key(self.event_type, self.key)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.event_type,
            "event_type": self.event_type,
            "kind": self.event_type.lower().replace("_", "-"),
            "key": self.key,
            "event_key": self.event_key,
            "ticket": self.ticket,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "payload": dict(self.payload),
            "instruction": self.instruction,
            "run_id": self.run_id,
        }


def _event_from_dict(value: Mapping[str, Any]) -> PREvent:
    event_type = _text(value.get("event_type") or value.get("type"))
    payload = value.get("payload")
    payload = dict(payload) if isinstance(payload, Mapping) else dict(value)
    return PREvent(
        event_type=event_type,
        key=_text(value.get("key") or value.get("event_key")),
        ticket=_text(value.get("ticket")),
        pr_number=int(value.get("pr_number") or payload.get("pr_number") or payload.get("number")),
        head_sha=value.get("head_sha") or payload.get("head_sha"),
        payload=payload,
        instruction=_text(value.get("instruction") or payload.get("instruction")),
        run_id=value.get("run_id") or payload.get("run_id"),
    )


def _migration_path() -> Path:
    return Path(__file__).resolve().parents[2] / "migrations" / "007_phase8_pr_feedback.sql"


def apply_phase8_migration(db: Any, path: str | Path | None = None) -> None:
    """Apply additive PR-event receipt tables to a Harbor database."""

    connection = _connection(db)
    if connection is None:
        raise TypeError("PR-event migration requires a sqlite3.Connection or HarborDB")
    migration = Path(path) if path is not None else _migration_path()
    script = migration.read_text(encoding="utf-8")
    with _lock(db):
        connection.executescript(script)
        connection.commit()


apply_pr_events_migration = apply_phase8_migration


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


class PREventProcessor:
    """Persist and deliver PR events without granting Harbor write access."""

    def __init__(
        self,
        db: Any | None = None,
        *,
        project: str = "",
        profile: Any | None = None,
        risk_skill: str | None = None,
        builder: Any | None = None,
        comment_handler: Any | None = None,
    ) -> None:
        self.db = db
        self.project = str(project)
        self.profile = profile
        self.risk_skill = _risk_skill(profile, risk_skill)
        self.builder = builder
        self.comment_handler = comment_handler
        self._state: dict[str, dict[str, Any]] = {}
        self._events: dict[str, dict[str, Any]] = {}
        self._offered: set[str] = set()
        self._replied: set[str] = set()
        if db is not None:
            apply_phase8_migration(db)

    def _state_row(self, key: str) -> dict[str, Any] | None:
        connection = _connection(self.db)
        if connection is None or not _table_exists(connection, "github_pr_states"):
            return self._state.get(key)
        row = connection.execute("SELECT * FROM github_pr_states WHERE pr_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def risk_status(self, ticket: Any, facts: Mapping[str, Any]) -> str:
        row = self._state_row(pr_key(ticket, facts))
        return _text(row.get("risk_status"), "unknown") if row else "unknown"

    def risk_is_valid(self, ticket: Any, facts: Mapping[str, Any]) -> bool:
        key = pr_key(ticket, facts)
        row = self._state_row(key)
        return bool(row and row.get("risk_status") == "valid" and row.get("risk_head_sha") == _head(facts))

    def _insert_event(self, event: PREvent) -> bool:
        event_dict = event.as_dict()
        key = event.event_key
        connection = _connection(self.db)
        if connection is None:
            if key in self._events:
                return False
            self._events[key] = event_dict
            return True
        with _lock(self.db):
            try:
                connection.execute(
                    "INSERT INTO github_pr_events(event_key, project, ticket, pr_number, event_type, "
                    "head_sha, payload_json, offered, replied, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?)",
                    (
                        key,
                        self.project,
                        event.ticket,
                        event.pr_number,
                        event.event_type,
                        event.head_sha,
                        _json(event_dict),
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
            if _table_exists(connection, "events"):
                connection.execute(
                    "INSERT INTO events(project, identifier, event_type, payload_json, snapshot_hash, at) "
                    "VALUES (?, ?, ?, ?, NULL, ?)",
                    (self.project, event.ticket, event.event_type, _json(event_dict), _now()),
                )
            connection.commit()
        return True

    def _save_state(
        self,
        key: str,
        ticket: str,
        number: int,
        head_sha: str | None,
        created_at: Any,
        *,
        risk_status: str,
        risk_head_sha: str | None,
    ) -> None:
        state = {
            "pr_key": key,
            "project": self.project,
            "ticket": ticket,
            "pr_number": number,
            "head_sha": head_sha,
            "created_at": created_at,
            "risk_status": risk_status,
            "risk_head_sha": risk_head_sha,
            "observed_at": _now(),
        }
        self._state[key] = state
        connection = _connection(self.db)
        if connection is None:
            return
        with _lock(self.db):
            connection.execute(
                "INSERT INTO github_pr_states(pr_key, project, ticket, pr_number, head_sha, created_at, "
                "risk_status, risk_head_sha, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(pr_key) DO UPDATE SET project=excluded.project, ticket=excluded.ticket, "
                "pr_number=excluded.pr_number, head_sha=excluded.head_sha, created_at=excluded.created_at, "
                "risk_status=excluded.risk_status, risk_head_sha=excluded.risk_head_sha, observed_at=excluded.observed_at",
                tuple(state.values()),
            )
            connection.commit()

    def _comment_seen(self, event_key: str) -> bool:
        connection = _connection(self.db)
        if connection is None:
            return event_key in self._events
        row = connection.execute(
            "SELECT 1 FROM github_pr_events WHERE event_key=? LIMIT 1", (event_key,)
        ).fetchone()
        return row is not None

    def observe(
        self,
        ticket: Any,
        facts: Mapping[str, Any],
        *,
        run: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Observe one PR and return only newly-created local event receipts."""

        number = _pr_number(facts)
        identifier = _ticket_id(ticket)
        state_key = pr_key(ticket, facts)
        previous = self._state_row(state_key)
        head_sha = _head(facts)
        risk_status = "unknown"
        risk_head_sha = None
        if previous:
            risk_status = _text(previous.get("risk_status"), "unknown")
            risk_head_sha = previous.get("risk_head_sha")
            if previous.get("head_sha") != head_sha:
                # A push invalidates a prior risk result.  This is local state;
                # the builder must run risk again before any merge decision.
                risk_status = "invalidated"
                risk_head_sha = None
        self._save_state(
            state_key,
            identifier,
            number,
            head_sha,
            _value(facts, "created_at", "createdAt", default=None),
            risk_status=risk_status,
            risk_head_sha=risk_head_sha,
        )

        events: list[PREvent] = []
        if previous is None or not self._comment_seen(_event_key(PR_CREATED, state_key)):
            payload = {
                "ticket": identifier,
                "pr_number": number,
                "number": number,
                "head_sha": head_sha,
                "created_at": _value(facts, "created_at", "createdAt", default=None),
                "url": _value(facts, "url", default=None),
                "title": _value(facts, "title", default=None),
                "ci": _value(facts, "ci", default=None),
            }
            event = PREvent(
                PR_CREATED,
                state_key,
                identifier,
                number,
                head_sha,
                payload,
                build_risk_instruction(ticket, facts, skill=self.risk_skill, run=run),
                _run_id(ticket, facts, run),
            )
            if self._insert_event(event):
                events.append(event)

        for source in _surface_comments(facts):
            body = _text(_value(source, "body", "text", default=""))
            marked = bool(_value(source, "harbor_marked", "is_harbor_marked", default=False)) or is_harbor_marked(body) or bool(_HARBOR_REPLY_MARKER.search(body))
            if marked:
                continue
            version = comment_version_key(source)
            key = f"{state_key}:{version}"
            event = PREvent(
                NEW_COMMENT_VERSION,
                key,
                identifier,
                number,
                head_sha,
                {
                    "ticket": identifier,
                    "pr_number": number,
                    "head_sha": head_sha,
                    "comment": dict(source),
                    "comment_version": version,
                    "surface": _value(source, "surface", "source", default="comment"),
                },
                build_comment_instruction(ticket, facts, source, run=run),
                _run_id(ticket, facts, run),
            )
            if self._insert_event(event):
                events.append(event)

        events.sort(key=lambda item: (0 if item.event_type == PR_CREATED else 1, item.event_key))
        return [event.as_dict() for event in events]

    def events(self, *, pending_only: bool = False) -> list[dict[str, Any]]:
        connection = _connection(self.db)
        if connection is None:
            rows = list(self._events.values())
            if pending_only:
                rows = [
                    row
                    for row in rows
                    if (
                        _text(row.get("event_type") or row.get("type")) == NEW_COMMENT_VERSION
                        and _text(row.get("event_key")) not in self._replied
                    )
                    or (
                        _text(row.get("event_type") or row.get("type")) == PR_CREATED
                        and _text(row.get("event_key")) not in self._offered
                    )
                ]
            return rows
        query = "SELECT payload_json FROM github_pr_events"
        if pending_only:
            query += " WHERE (event_type = 'NEW_COMMENT_VERSION' AND replied = 0) OR (event_type = 'PR_CREATED' AND offered = 0)"
        query += " ORDER BY id"
        rows = connection.execute(query).fetchall()
        return [json.loads(row[0]) for row in rows]

    def pending_comments(self) -> list[dict[str, Any]]:
        return [
            row
            for row in self.events(pending_only=True)
            if _text(row.get("event_type") or row.get("type")) == NEW_COMMENT_VERSION
        ]

    def _mark_offered(self, event_key: str, *, replied: bool = False) -> None:
        connection = _connection(self.db)
        if connection is None:
            self._offered.add(event_key)
            if replied:
                self._replied.add(event_key)
            return
        with _lock(self.db):
            connection.execute(
                "UPDATE github_pr_events SET offered=1, replied=CASE WHEN ? THEN 1 ELSE replied END WHERE event_key=?",
                (int(replied), event_key),
            )
            connection.commit()

    def mark_risk_valid(self, ticket: Any, facts: Mapping[str, Any], result: Any) -> dict[str, Any]:
        normalized = validate_risk_result(result, current_head=_head(facts))
        key = pr_key(ticket, facts)
        row = self._state_row(key)
        if row is None:
            raise RiskContractError("cannot record risk result for an unobserved PR")
        self._save_state(
            key,
            _ticket_id(ticket),
            _pr_number(facts),
            _head(facts),
            row.get("created_at"),
            risk_status="valid",
            risk_head_sha=_head(facts),
        )
        return normalized

    async def _invoke(self, target: Any, event: PREvent) -> Any:
        if target is None:
            return None
        method = None
        # A retained runner commonly exposes resume(run_id, instruction).  A
        # dedicated handler generally exposes handle(event) or is callable.
        for name in (
            "handle_pr_event",
            "handle_comment",
            "handle",
            "resume",
            "run",
            "continue_run",
            "dispatch",
            "offer",
        ):
            candidate = getattr(target, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None and callable(target):
            method = target
        if method is None:
            raise TypeError("builder must be callable or expose handle/resume")
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
        else:
            positional = []
        if event.run_id and getattr(method, "__name__", "") in {"resume", "continue_run"} and len(positional) >= 2:
            result = method(event.run_id, event.instruction)
        elif len(positional) <= 1:
            parameter_name = positional[0].name.casefold() if positional else "event"
            argument = (
                event.instruction
                if parameter_name in {"instruction", "prompt", "message", "text", "command"}
                else event.as_dict()
            )
            result = method(argument)
        else:
            result = method(event.run_id, event.instruction)
        return await result if inspect.isawaitable(result) else result

    async def dispatch(
        self,
        events: Iterable[Mapping[str, Any]] | None = None,
        *,
        builder: Any | None = None,
        comment_handler: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Offer events to the configured same builder and validate replies."""

        target_builder = builder or self.builder
        target_comments = comment_handler or self.comment_handler or target_builder
        source = list(events) if events is not None else self.events(pending_only=True)
        delivered: list[dict[str, Any]] = []
        for raw in source:
            event = raw if isinstance(raw, PREvent) else _event_from_dict(raw)
            if event.event_type == PR_CREATED:
                if target_builder is None:
                    # Observation is still durable; delivery remains pending
                    # until the supervisor wires the retained builder.
                    continue
                result = await self._invoke(target_builder, event)
                # A builder can return a structured risk result.  If it does,
                # record it only after proving current SHA + heading.  A
                # runner may instead record it later with mark_risk_valid.
                if isinstance(result, Mapping) and (
                    _value(result, "risk", "risk_result", default=None) is not None
                    or _value(result, "head_sha", "headSha", "sha", default=None) is not None
                ):
                    risk = _value(result, "risk", "risk_result", default=result)
                    # The event receipt may have been observed on an older
                    # poll.  Validate against the latest observed state, not
                    # the stale event payload, before accepting risk.
                    current = self._state_row(pr_key({"identifier": event.ticket}, {"number": event.pr_number}))
                    current_head = _text(current.get("head_sha"), event.head_sha) if current else event.head_sha
                    self.mark_risk_valid(
                        {"identifier": event.ticket},
                        {"number": event.pr_number, "head_sha": current_head},
                        risk,
                    )
                self._mark_offered(event.event_key, replied=False)
                delivered.append(event.as_dict())
            elif event.event_type == NEW_COMMENT_VERSION:
                if target_comments is None:
                    continue
                result = await self._invoke(target_comments, event)
                normalized = normalize_comment_response(result)
                self._mark_offered(event.event_key, replied=True)
                delivered.append({**event.as_dict(), "response": normalized})
            else:
                # Unknown local events are not silently marked as delivered;
                # this keeps future event types fail-closed.
                raise PREventError(f"unsupported PR event type: {event.event_type}")
        return delivered

    async def process(
        self,
        ticket: Any,
        facts: Mapping[str, Any],
        *,
        run: Any | None = None,
        builder: Any | None = None,
        comment_handler: Any | None = None,
    ) -> list[dict[str, Any]]:
        new = self.observe(ticket, facts, run=run)
        # A handler failure must not lose the receipt: observe() is
        # intentionally idempotent and therefore returns no duplicate event
        # on the next poll.  Include durable pending receipts so the same
        # builder gets another chance to produce the mandatory reply.
        pending = self.events(pending_only=True)
        by_key = {_text(item.get("event_key") or item.get("key")): item for item in new}
        for item in pending:
            key = _text(item.get("event_key") or item.get("key"))
            if key not in by_key:
                by_key[key] = item
        return await self.dispatch(by_key.values(), builder=builder, comment_handler=comment_handler)

    handle = process
    offer = dispatch


PRFeedbackProcessor = PREventProcessor
GitHubPREventProcessor = PREventProcessor
PrEventProcessor = PREventProcessor


def detect_pr_events(
    ticket: Any,
    facts: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
    profile: Any | None = None,
    run: Any | None = None,
) -> list[dict[str, Any]]:
    """Pure convenience wrapper for tests and poller integrations."""

    processor = PREventProcessor(profile=profile)
    if previous is not None:
        processor._state[pr_key(ticket, facts)] = dict(previous)
    return processor.observe(ticket, facts, run=run)


__all__ = [
    "COMMENT_VERSION",
    "CommentContractError",
    "GitHubPREventProcessor",
    "NEW_COMMENT_VERSION",
    "PREvent",
    "PREventError",
    "PREventProcessor",
    "PrEventProcessor",
    "PRFeedbackProcessor",
    "PR_CREATED",
    "RISK_INVALIDATED",
    "RiskContractError",
    "apply_phase8_migration",
    "apply_pr_events_migration",
    "build_comment_instruction",
    "build_risk_instruction",
    "comment_version_key",
    "detect_pr_events",
    "is_harbor_marked",
    "normalize_comment_response",
    "pr_key",
    "validate_comment_response",
    "validate_risk_result",
]
