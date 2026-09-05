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

import asyncio
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

from harbor.db import sql_statements
from harbor.facts.threads import is_harbor_marked
from harbor.resources import runtime_asset


PR_CREATED = "PR_CREATED"
NEW_COMMENT_VERSION = "NEW_COMMENT_VERSION"
RISK_INVALIDATED = "RISK_INVALIDATED"
COMMENT_VERSION = NEW_COMMENT_VERSION  # descriptive compatibility alias

_HARBOR_REPLY_MARKER = re.compile(
    r"(?:<!--\s*harbor(?:\b|[-_:])|\[harbor\])",
    re.IGNORECASE,
)
_LINEAR_LINKBACK_MARKER = re.compile(r"<!--\s*linear-linkback\b", re.IGNORECASE)
_REPLY_MARKER_PREFIX = "<!-- harbor:reply:"
_REPLY_MARKER_SUFFIX = " -->"
_RISK_MARKER_PREFIX = "<!-- harbor:risk-sha:"
_RISK_MARKER_SUFFIX = " -->"
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


def _pr_url(facts: Mapping[str, Any]) -> str:
    value = _value(facts, "url", "html_url", "href")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("PR facts require an exact URL")
    return value.strip().rstrip("/")


def _ticket_pr_urls(ticket: Any) -> set[str]:
    if not isinstance(ticket, Mapping):
        return set()
    values = ticket.get("pr_urls", ticket.get("attachments", ()))
    if isinstance(values, Mapping):
        values = values.get("nodes", ())
    if isinstance(values, (str, bytes, Mapping)):
        values = (values,)
    result: set[str] = set()
    for value in values or ():
        if isinstance(value, Mapping):
            value = value.get("url") or value.get("href")
        if isinstance(value, str) and value.strip():
            result.add(value.strip().rstrip("/").casefold())
    return result


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
    """Stable local identity for one exact linked PR URL."""

    url_digest = hashlib.sha256(_pr_url(facts).casefold().encode("utf-8")).hexdigest()[:24]
    return f"{_ticket_id(ticket)}:pr-url:{url_digest}"


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


def comment_reply_marker(comment_or_version: Mapping[str, Any] | str) -> str:
    """Return the deterministic receipt marker for one comment version.

    The marker is deliberately derived from the *source* comment version, not
    from the builder's reply text.  A builder includes it in the reply body;
    the next read-only GitHub poll can then prove that this particular
    external comment was answered without Harbor ever posting or editing the
    reply itself.
    """

    version = (
        comment_version_key(comment_or_version)
        if isinstance(comment_or_version, Mapping)
        else str(comment_or_version)
    )
    digest = hashlib.sha256(version.encode("utf-8")).hexdigest()[:24]
    return f"{_REPLY_MARKER_PREFIX}{digest}{_REPLY_MARKER_SUFFIX}"


# A descriptive alias makes the protocol easier for poller/runner adapters to
# discover without coupling them to the implementation name.
build_comment_reply_marker = comment_reply_marker


def risk_analysis_marker(head_sha: str) -> str:
    """Return the exact PR-body marker binding risk analysis to one SHA."""

    return f"{_RISK_MARKER_PREFIX}{str(head_sha).strip()}{_RISK_MARKER_SUFFIX}"


def _event_key(event_type: str, key: str) -> str:
    return f"{event_type}:{key}"


def _risk_observation(facts: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract only current-head PR-body evidence for a risk receipt."""

    head = _head(facts)
    body = _value(facts, "body", "pr_body", "description", default=None)
    if isinstance(body, str) and head and "## Risk Analysis" in body and risk_analysis_marker(head) in body:
        return {"head_sha": head, "body": body}
    risk = _value(facts, "risk", "risk_result", default=None)
    if isinstance(risk, Mapping):
        risk_body = _value(risk, "body", "pr_body", "description", default="")
        risk_head = _value(risk, "head_sha", "headSha", "sha", default=head)
        if (
            isinstance(risk_body, str)
            and risk_head
            and "## Risk Analysis" in risk_body
            and risk_analysis_marker(str(risk_head)) in risk_body
        ):
            return {"head_sha": risk_head, "body": risk_body}
    return None


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
    safe_head = head if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", head) else "unknown"
    external_facts = (
        f"pr_number={number}\nhead_sha="
        + _bounded(html.escape(head, quote=False), 256)
    )
    return _bounded(
        "Continue ticket "
        f"{identifier} in the existing builder session {run_id}. "
        "A PR was just created with these untrusted GitHub facts:\n"
        f"<external-pr-facts>\n{external_facts}\n</external-pr-facts>\n"
        f"Run the configured risk skill `{selected}` against that exact current SHA, "
        f"then write or update `## Risk Analysis` in the PR body at this SHA and include "
        f"the exact marker `{risk_analysis_marker(safe_head)}`. "
        "If the marker head is `unknown`, stop because the observed head was not a safe SHA token. "
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
    version = comment_version_key(comment)
    reply_marker = comment_reply_marker(version)
    surface = _text(_value(comment, "surface", "source", default="comment"))
    metadata_parts = [
        "head_sha=" + _bounded(html.escape(head, quote=False), 256),
        "surface=" + _bounded(html.escape(surface, quote=False), 128),
    ]
    path = _value(comment, "path", default=None)
    line = _value(comment, "line", default=None)
    if path:
        metadata_parts.append(
            "path=" + _bounded(html.escape(_text(path), quote=False), 256)
        )
        if line is not None:
            metadata_parts.append(
                "line=" + _bounded(html.escape(_text(line), quote=False), 32)
            )
    metadata = " ".join(metadata_parts)
    prefix = (
        f"Continue ticket {identifier} in the existing builder session {run_id}. "
        "Review the external GitHub facts and comment below.\n"
        f"<external-comment>\n{metadata}\n"
    )
    footer = (
        "\n"
        "</external-comment>\n"
        "Return a structured disposition: valid requires the fix and tests plus a reply; "
        "invalid or out-of-scope requires evidence plus a reply; ambiguous requires a "
        "clarifying question or an explicit park decision plus a reply. "
        f"Post the reply on GitHub yourself and include this exact receipt marker "
        f"verbatim in its body: {reply_marker}"
    )
    fixed_bytes = len((prefix + footer).encode("utf-8"))
    if fixed_bytes > max_bytes:
        raise PREventError("comment instruction budget is too small for its required fence and footer")
    body_budget = max_bytes - fixed_bytes
    return prefix + _bounded(body, body_budget) + footer


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
    pr_url: str
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
            "pr_url": self.pr_url,
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
        pr_url=_text(value.get("pr_url") or payload.get("pr_url") or payload.get("url")),
        head_sha=value.get("head_sha") or payload.get("head_sha"),
        payload=payload,
        instruction=_text(value.get("instruction") or payload.get("instruction")),
        run_id=value.get("run_id") or payload.get("run_id"),
    )


def _migration_path() -> Path:
    return runtime_asset("migrations", "007_phase8_pr_feedback.sql")


def apply_phase8_migration(db: Any, path: str | Path | None = None) -> None:
    """Apply additive PR-event receipt tables to a Harbor database."""

    connection = _connection(db)
    if connection is None:
        raise TypeError("PR-event migration requires a sqlite3.Connection or HarborDB")
    migration = Path(path) if path is not None else _migration_path()
    script = migration.read_text(encoding="utf-8")
    with _lock(db):
        for statement in sql_statements(script):
            if statement.upper().startswith("CREATE INDEX"):
                continue
            connection.execute(statement)
        for table in ("github_pr_states", "github_pr_events"):
            columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if "pr_url" not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN pr_url TEXT NOT NULL DEFAULT ''"
                )

        # Older phase-8 rows stored the exact URL only inside the serialized
        # PR-created payload. Recover that durable authority, propagate it to
        # sibling comment receipts, and rewrite the old ticket+number keys
        # before an observation can create duplicate events. Ambiguous or
        # URL-less groups remain empty and therefore fail closed.
        legacy_events = connection.execute(
            "SELECT id, event_key, project, ticket, pr_number, event_type, "
            "payload_json, offered, replied FROM github_pr_events WHERE pr_url=''"
        ).fetchall()
        decoded: list[tuple[Any, dict[str, Any], str]] = []
        urls_by_group: dict[tuple[str, str, int], set[str]] = {}
        for row in legacy_events:
            try:
                document = json.loads(row[6])
            except (TypeError, ValueError, json.JSONDecodeError):
                document = {}
            if not isinstance(document, dict):
                document = {}
            payload = document.get("payload")
            payload = payload if isinstance(payload, Mapping) else {}
            candidate = _text(
                document.get("pr_url")
                or document.get("url")
                or payload.get("pr_url")
                or payload.get("url")
            ).strip().rstrip("/")
            group = (_text(row[2]), _text(row[3]), int(row[4]))
            # Only the original PR-created observation is an authority source
            # for upgrading a number-keyed group. Comment payloads may contain
            # copied or foreign URLs and cannot select a repository.
            if candidate and _text(row[5]) == PR_CREATED:
                urls_by_group.setdefault(group, set()).add(candidate)
            decoded.append((row, document, candidate))

        resolved_urls = {
            group: next(iter(urls))
            for group, urls in urls_by_group.items()
            if len(urls) == 1
        }
        for row, document, candidate in decoded:
            group = (_text(row[2]), _text(row[3]), int(row[4]))
            pr_url = resolved_urls.get(group, "")
            if not pr_url:
                continue
            new_state_key = pr_key({"identifier": row[3]}, {"url": pr_url})
            payload = document.get("payload")
            if not isinstance(payload, dict):
                payload = {}
                document["payload"] = payload
            event_type = _text(row[5])
            if event_type == PR_CREATED:
                new_key = new_state_key
            elif event_type == NEW_COMMENT_VERSION:
                version = _text(payload.get("comment_version"))
                comment = payload.get("comment")
                if not version and isinstance(comment, Mapping):
                    version = comment_version_key(comment)
                if not version:
                    continue
                new_key = f"{new_state_key}:{version}"
            else:
                continue
            new_event_key = _event_key(event_type, new_key)
            document.update(
                {"key": new_key, "event_key": new_event_key, "pr_url": pr_url}
            )
            payload["pr_url"] = pr_url
            connection.execute(
                "UPDATE github_pr_events SET event_key=?, pr_url=?, payload_json=? WHERE id=?",
                (new_event_key, pr_url, _json(document), row[0]),
            )

        legacy_states = connection.execute(
            "SELECT pr_key, project, ticket, pr_number FROM github_pr_states WHERE pr_url=''"
        ).fetchall()
        for row in legacy_states:
            group = (_text(row[1]), _text(row[2]), int(row[3]))
            pr_url = resolved_urls.get(group, "")
            if not pr_url:
                continue
            new_key = pr_key({"identifier": row[2]}, {"url": pr_url})
            connection.execute(
                "UPDATE github_pr_states SET pr_key=?, pr_url=? WHERE pr_key=?",
                (new_key, pr_url, row[0]),
            )
        connection.execute("DROP INDEX IF EXISTS github_pr_states_ticket_idx")
        connection.execute(
            "CREATE INDEX github_pr_states_ticket_idx ON "
            "github_pr_states(project, ticket, pr_url)"
        )
        connection.execute("DROP INDEX IF EXISTS github_pr_events_ticket_type_idx")
        connection.execute(
            "CREATE INDEX github_pr_events_ticket_type_idx ON "
            "github_pr_events(project, ticket, pr_url, event_type, id)"
        )
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
        # PR polling and a slow builder run in separate supervisor tasks.  A
        # per-processor in-flight guard prevents two polls from resuming the
        # same retained session before the first handoff has returned.
        self._delivery_lock = asyncio.Lock()
        self._in_flight: set[str] = set()
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
                    "INSERT INTO github_pr_events(event_key, project, ticket, pr_number, pr_url, event_type, "
                    "head_sha, payload_json, offered, replied, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)",
                    (
                        key,
                        self.project,
                        event.ticket,
                        event.pr_number,
                        event.pr_url,
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
        pr_url: str,
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
            "pr_url": pr_url,
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
                "INSERT INTO github_pr_states(pr_key, project, ticket, pr_number, pr_url, head_sha, created_at, "
                "risk_status, risk_head_sha, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(pr_key) DO UPDATE SET project=excluded.project, ticket=excluded.ticket, "
                "pr_number=excluded.pr_number, pr_url=excluded.pr_url, head_sha=excluded.head_sha, created_at=excluded.created_at, "
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
        pr_url = _pr_url(facts)
        identifier = _ticket_id(ticket)
        if pr_url.casefold() not in _ticket_pr_urls(ticket):
            return []
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
            pr_url,
            head_sha,
            _value(facts, "created_at", "createdAt", default=None),
            risk_status=risk_status,
            risk_head_sha=risk_head_sha,
        )

        # GitHub PR body facts are read-only evidence for a risk analysis
        # written by the builder.  Bind the receipt to this exact head so a
        # push invalidates it on the next observation.
        risk_evidence = _risk_observation(facts)
        if risk_evidence is not None:
            try:
                validate_risk_result(risk_evidence, current_head=head_sha)
            except RiskContractError:
                pass
            else:
                self._save_state(
                    state_key,
                    identifier,
                    number,
                    pr_url,
                    head_sha,
                    _value(facts, "created_at", "createdAt", default=None),
                    risk_status="valid",
                    risk_head_sha=head_sha,
                )

        if previous and previous.get("head_sha") != head_sha and risk_status != "valid":
            # A new PR head requires the risk event to be offered again.  The
            # event identity remains ticket+PR for idempotence, while this
            # local flag reopens delivery after invalidation.
            self._reset_pr_event_offer(state_key)

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
                pr_url,
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
            surface = _text(_value(source, "surface", "source", default="comment"))
            is_bot = bool(_value(source, "is_bot", "isBot", default=False))
            # Suppress Harbor receipts and explicitly marked Linear metadata.
            # Bot authorship alone is not a safe suppression boundary: review
            # tools post actionable top-level issue comments and review bodies.
            # Bot review-thread comments are the one exception because
            # WakeDecider handles those with severity and rework-cap context.
            if (
                marked
                or bool(_LINEAR_LINKBACK_MARKER.search(body))
                or (is_bot and surface == "thread")
            ):
                continue
            version = comment_version_key(source)
            key = f"{state_key}:{version}"
            event = PREvent(
                NEW_COMMENT_VERSION,
                key,
                identifier,
                number,
                pr_url,
                head_sha,
                {
                    "ticket": identifier,
                    "pr_number": number,
                    "pr_url": pr_url,
                    "head_sha": head_sha,
                    "comment": dict(source),
                    "comment_version": version,
                    "reply_marker": comment_reply_marker(version),
                    "surface": surface,
                },
                build_comment_instruction(ticket, facts, source, run=run),
                _run_id(ticket, facts, run),
            )
            if self._insert_event(event):
                events.append(event)

        # A builder reply is an external side effect owned by the configured
        # skill.  Harbor only records the local receipt after a later poll
        # observes the exact marker in a *different* GitHub comment.  This is
        # intentionally done after inserting this poll's events so a reply
        # that races the first observation can still acknowledge its source.
        self._acknowledge_reply_markers(facts, pr_url=pr_url)

        events.sort(key=lambda item: (0 if item.event_type == PR_CREATED else 1, item.event_key))
        return [event.as_dict() for event in events]

    def events(
        self,
        *,
        pending_only: bool = False,
        project: str | None = None,
        ticket: str | None = None,
        pr_number: int | None = None,
        pr_url: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return observed PR events, optionally scoped to one PR.

        Durable pending recovery is invoked while processing one ticket.  A
        Harbor database can contain receipts for other tickets (and, in a
        shared database, other projects), so callers must be able to constrain
        the recovery query before dispatching any builder work.
        """

        connection = _connection(self.db)
        if connection is None:
            rows = list(self._events.values())
            if project is not None and str(project) != self.project:
                rows = []
            if ticket is not None:
                rows = [row for row in rows if _text(row.get("ticket")) == str(ticket)]
            if pr_number is not None:
                rows = [row for row in rows if row.get("pr_number") == pr_number]
            if pr_url is not None:
                rows = [
                    row
                    for row in rows
                    if _text(row.get("pr_url") or _value(row.get("payload", {}), "url"))
                    .rstrip("/")
                    .casefold()
                    == pr_url.rstrip("/").casefold()
                ]
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
                        and self.risk_status(
                            {"identifier": row.get("ticket")},
                            {
                                "number": row.get("pr_number"),
                                "url": row.get("pr_url")
                                or _value(row.get("payload", {}), "url"),
                            },
                        )
                        != "valid"
                    )
                ]
            return rows
        query = "SELECT payload_json FROM github_pr_events"
        clauses: list[str] = []
        parameters: list[Any] = []
        if pending_only:
            # PR-created rows are filtered below against the risk state because
            # an offered event can remain explicitly pending until the current
            # PR body proves its risk heading/head pair.
            clauses.append("((event_type = 'NEW_COMMENT_VERSION' AND replied = 0) OR event_type = 'PR_CREATED')")
        if project is not None:
            clauses.append("project = ?")
            parameters.append(str(project))
        if ticket is not None:
            clauses.append("ticket = ?")
            parameters.append(str(ticket))
        if pr_number is not None:
            clauses.append("pr_number = ?")
            parameters.append(int(pr_number))
        if pr_url is not None:
            clauses.append("pr_url = ?")
            parameters.append(pr_url.rstrip("/"))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        rows = connection.execute(query, parameters).fetchall()
        values = [json.loads(row[0]) for row in rows]
        if not pending_only:
            return values
        filtered: list[dict[str, Any]] = []
        for value in values:
            event_type = _text(value.get("event_type") or value.get("type"))
            if event_type != PR_CREATED:
                filtered.append(value)
                continue
            state = self._state_row(
                pr_key(
                    {"identifier": value.get("ticket")},
                    {
                        "number": value.get("pr_number"),
                        "url": value.get("pr_url")
                        or _value(value.get("payload", {}), "url"),
                    },
                )
            )
            if not state or _text(state.get("risk_status"), "unknown") != "valid":
                filtered.append(value)
        return filtered

    def pending_comments(self) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for row in self.events(pending_only=True):
            if _text(row.get("event_type") or row.get("type")) != NEW_COMMENT_VERSION:
                continue
            event_key = _text(row.get("event_key") or row.get("key"))
            offered, replied = self._event_flags(event_key)
            if replied:
                continue
            value = dict(row)
            # This makes the no-marker case an explicit, queryable pending
            # state rather than a silent terminal condition.  An offered event
            # is not retried on every poll; the next observed marker closes it.
            value["delivery_state"] = (
                "awaiting_reply_observation" if offered else "awaiting_builder_delivery"
            )
            pending.append(value)
        return pending

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

    def _reset_pr_event_offer(self, state_key: str) -> None:
        """Reopen the ticket's PR-created event after a head invalidation."""

        event_key = _event_key(PR_CREATED, state_key)
        connection = _connection(self.db)
        if connection is None:
            self._offered.discard(event_key)
            return
        with _lock(self.db):
            connection.execute(
                "UPDATE github_pr_events SET offered=0 WHERE event_key=?",
                (event_key,),
            )
            connection.commit()

    def _event_flags(self, event_key: str) -> tuple[bool, bool]:
        """Return ``(offered, replied)`` for one durable/local event."""

        connection = _connection(self.db)
        if connection is None:
            row = self._events.get(event_key)
            if row is None:
                return False, event_key in self._replied
            return event_key in self._offered, event_key in self._replied
        row = connection.execute(
            "SELECT offered, replied FROM github_pr_events WHERE event_key=? LIMIT 1",
            (event_key,),
        ).fetchone()
        return (bool(row[0]), bool(row[1])) if row is not None else (False, False)

    def _mark_replied(self, event_key: str) -> None:
        """Record a reply receipt only after a read-only marker observation."""

        connection = _connection(self.db)
        if connection is None:
            self._offered.add(event_key)
            self._replied.add(event_key)
            return
        with _lock(self.db):
            connection.execute(
                "UPDATE github_pr_events SET offered=1, replied=1 WHERE event_key=?",
                (event_key,),
            )
            connection.commit()

    def _run_is_running(self, ticket: Any, run: Any | None) -> bool:
        """Return whether delivery would race a live builder process.

        ``run`` is normally the supervisor's latest durable run row.  The
        ledger fallback covers direct integrations that omit it, and makes a
        second poll fail closed during the small window before a dispatcher
        has installed its in-memory runner.
        """

        status = _value(run, "status", "run_status", default=None)
        if status is not None:
            return _text(status).casefold() == "running"
        connection = _connection(self.db)
        identifier = _ticket_id(ticket)
        if connection is None or not identifier or not _table_exists(connection, "runs"):
            return False
        row = connection.execute(
            "SELECT status FROM runs WHERE ticket=? ORDER BY COALESCE(finished_at, started_at, id) DESC, id DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        return bool(row and _text(row[0]).casefold() == "running")

    async def _begin_delivery(self, event: PREvent) -> bool:
        """Reserve an event for one handoff, without holding a DB lock."""

        async with self._delivery_lock:
            if event.event_key in self._in_flight:
                return False
            offered, replied = self._event_flags(event.event_key)
            if replied or offered:
                return False
            self._in_flight.add(event.event_key)
            return True

    async def _end_delivery(self, event_key: str) -> None:
        async with self._delivery_lock:
            self._in_flight.discard(event_key)

    @staticmethod
    def _looks_like_run_result(value: Any) -> bool:
        """Recognize the normal Codex runner result, not a reply contract."""

        status = _value(value, "status", default=None)
        summary = _value(value, "summary", default=None)
        return isinstance(status, str) and status in {"completed", "waiting", "blocked", "failed"} and isinstance(summary, str)

    @staticmethod
    def _response_dict(value: Any) -> Any:
        if isinstance(value, Mapping):
            return dict(value)
        as_dict = getattr(value, "as_dict", None)
        if callable(as_dict):
            try:
                converted = as_dict()
            except TypeError:
                converted = as_dict(include_metadata=False)
            return dict(converted) if isinstance(converted, Mapping) else converted
        return value

    def _acknowledge_reply_markers(
        self,
        facts: Mapping[str, Any],
        *,
        pr_url: str,
    ) -> None:
        """Acknowledge comment events from observed GitHub reply markers.

        The source comment itself is excluded by id.  That prevents an edited
        source comment containing a copied marker from becoming its own reply
        receipt.  We do not require a particular author here because GitHub
        installations expose bot identity differently; the unguessable,
        source-version-bound marker is the portable evidence contract.
        """

        comments = _surface_comments(facts)
        if not comments:
            return
        observed: list[tuple[str, str]] = []
        for comment in comments:
            body = _text(_value(comment, "body", "text", default=""))
            comment_id = _text(_value(comment, "id", "comment_id", default=""))
            # A receipt must be tied to a distinct GitHub comment object.  A
            # body-only adapter cannot prove that a copied marker was posted
            # separately from the source comment, so it is fail-closed.
            if body and comment_id:
                observed.append((comment_id, body))
        if not observed:
            return
        # Markers are source-version-bound but are not globally unique to a
        # repository.  Only events belonging to the exact PR currently being
        # observed may be acknowledged by comments on that PR.
        for raw in self.events(pr_url=pr_url):
            if _text(raw.get("event_type") or raw.get("type")) != NEW_COMMENT_VERSION:
                continue
            event_key = _text(raw.get("event_key") or raw.get("key"))
            _, replied = self._event_flags(event_key)
            if replied:
                continue
            payload = raw.get("payload")
            payload = payload if isinstance(payload, Mapping) else raw
            version = _text(payload.get("comment_version"), "")
            if not version:
                comment = payload.get("comment")
                if isinstance(comment, Mapping):
                    version = comment_version_key(comment)
            if not version:
                continue
            marker = _text(payload.get("reply_marker"), "") or comment_reply_marker(version)
            source = payload.get("comment")
            source_id = _text(_value(source, "id", "comment_id", default="")) if isinstance(source, Mapping) else ""
            for candidate_id, body in observed:
                if source_id and candidate_id and source_id == candidate_id:
                    continue
                if marker in body:
                    self._mark_replied(event_key)
                    break

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
            _pr_url(facts),
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
            if not await self._begin_delivery(event):
                # ``offered`` means the builder already received this event
                # and Harbor is waiting for the next GitHub observation to
                # prove its reply.  It must not resume the same session on
                # every one-minute poll.
                continue
            try:
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
                        event_ticket = {
                            "identifier": event.ticket,
                            "pr_urls": [event.pr_url],
                        }
                        event_facts = {
                            "number": event.pr_number,
                            "url": event.pr_url,
                        }
                        current = self._state_row(pr_key(event_ticket, event_facts))
                        current_head = _text(current.get("head_sha"), event.head_sha) if current else event.head_sha
                        self.mark_risk_valid(
                            event_ticket,
                            {**event_facts, "head_sha": current_head},
                            risk,
                        )
                    self._mark_offered(event.event_key, replied=False)
                    delivered.append(event.as_dict())
                elif event.event_type == NEW_COMMENT_VERSION:
                    if target_comments is None:
                        continue
                    result = await self._invoke(target_comments, event)
                    if self._looks_like_run_result(result):
                        # WorktreeDispatcher/CodexRunner returns the ordinary
                        # run-result schema.  That proves only that the
                        # instruction was handed to the builder; the reply is
                        # receipted later from an observed marker on GitHub.
                        self._mark_offered(event.event_key, replied=False)
                        delivered.append(
                            {
                                **event.as_dict(),
                                "response": self._response_dict(result),
                                "reply_observed": False,
                            }
                        )
                    else:
                        normalized = normalize_comment_response(result)
                        # A structured disposition proves only local builder
                        # delivery. Every adapter, including compatibility
                        # handlers, requires a later read-only observation of
                        # the source-version-bound GitHub reply marker.
                        self._mark_offered(event.event_key, replied=False)
                        delivered.append(
                            {
                                **event.as_dict(),
                                "response": normalized,
                                "reply_observed": False,
                            }
                        )
                else:
                    # Unknown local events are not silently marked as delivered;
                    # this keeps future event types fail-closed.
                    raise PREventError(f"unsupported PR event type: {event.event_type}")
            finally:
                await self._end_delivery(event.event_key)
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
        if self._run_is_running(ticket, run):
            # The poll above intentionally remains durable/observable; only
            # the resume is deferred until the current process has returned a
            # waiting/completed/parked/blocked run result.
            return []
        # A handler failure must not lose the receipt: observe() is
        # intentionally idempotent and therefore returns no duplicate event
        # on the next poll.  Include durable pending receipts so the same
        # builder gets another chance to produce the mandatory reply.
        pending = self.events(
            pending_only=True,
            project=self.project,
            ticket=_ticket_id(ticket),
            pr_number=_pr_number(facts),
            pr_url=_pr_url(facts),
        )
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
    "build_comment_reply_marker",
    "build_risk_instruction",
    "comment_reply_marker",
    "comment_version_key",
    "detect_pr_events",
    "is_harbor_marked",
    "normalize_comment_response",
    "pr_key",
    "risk_analysis_marker",
    "validate_comment_response",
    "validate_risk_result",
]
