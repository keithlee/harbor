"""Small, deterministic helpers for resuming a waiting builder.

Re-engagement is deliberately a formatting and facts boundary.  The delivery
skill remains opaque to Harbor: this module never imports, reloads, or tries to
interpret a skill.  Review text comes from GitHub and is therefore untrusted;
it is escaped, bounded, and surrounded by explicit delimiters before it is
handed to a runner.

The functions accept mappings and light-weight test doubles.  Keeping this
module independent of the clients makes Phase 6 tests entirely offline and
also lets a different skill use the same resume protocol.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping
from typing import Any


MAX_RESUME_BYTES = 2 * 1024
UNTRUSTED_OPEN = "<untrusted-review-comment>"
UNTRUSTED_CLOSE = "</untrusted-review-comment>"


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
    if value is None:
        return default
    return str(value)


def _thread_id(thread: Mapping[str, Any]) -> str:
    return _text(_value(thread, "id", "thread_id", "threadId", default=""))


def _thread_body(thread: Mapping[str, Any]) -> str:
    body = _value(thread, "body", default=None)
    comments = _value(thread, "comments", default=None)
    if not isinstance(body, str):
        if isinstance(comments, Mapping):
            comments = comments.get("nodes", ())
        if isinstance(comments, Iterable) and not isinstance(comments, (str, bytes, Mapping)):
            comment = next((item for item in comments if isinstance(item, Mapping)), None)
            if comment is not None:
                body = comment.get("body")
    return _text(body)


def _is_outdated(thread: Mapping[str, Any]) -> bool:
    value = _value(thread, "outdated", "isOutdated", default=False)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _line(thread: Mapping[str, Any], outdated: bool) -> int | None:
    # Never carry a line from an outdated thread.  That line is no longer a
    # stable location at the current head and can make a skill edit the wrong
    # file.  Accept camelCase for raw GraphQL fakes.
    if outdated:
        return None
    value = _value(thread, "line", default=None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_bot(thread: Mapping[str, Any]) -> bool:
    value = _value(thread, "is_bot", "isBot", default=None)
    if value is not None:
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on"}
        return bool(value)
    author = _value(thread, "author", default=None)
    if isinstance(author, Mapping):
        value = author.get("isBot")
        if value is not None:
            return bool(value)
        login = author.get("login") or author.get("name")
        return isinstance(login, str) and (
            login.endswith("[bot]") or login.casefold() == "chatgpt-codex-connector"
        )
    return False


def _severity(thread: Mapping[str, Any]) -> int | None:
    value = _value(thread, "severity", default=None)
    if isinstance(value, int) and value >= 0:
        return value
    if value is not None:
        text = str(value).upper()
        if text.startswith("P"):
            try:
                return int(text[1:])
            except ValueError:
                pass
    # Parsed facts normally carry a severity.  The fallback is intentionally
    # narrow: prose mentioning P2 is not an authorization to wake a builder.
    body = _thread_body(thread)
    marker = "/badge/P"
    lower = body.casefold()
    at = lower.find(marker.casefold())
    if at >= 0:
        digits = []
        for char in body[at + len(marker) :]:
            if char.isdigit():
                digits.append(char)
            else:
                break
        if digits:
            return int("".join(digits))
    return None


def eligible_review_threads(
    threads: Iterable[Mapping[str, Any]] | None,
    *,
    head_sha: str | None = None,
    seen_at: Mapping[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    """Return unresolved bot P1/P2 facts not already offered at this head.

    ``seen_at`` is optional for pure callers; the wake ledger supplies it when
    available.  Unknown authors and unknown severities fail closed.  The
    normalized output keeps both ``path`` and ``outdated`` so downstream
    skills can adjudicate an old location safely.
    """

    result: list[dict[str, Any]] = []
    for source in threads or ():
        if not isinstance(source, Mapping) or not _thread_id(source):
            continue
        if not _is_bot(source):
            continue
        resolved = _value(source, "resolved", "isResolved", default=False)
        if isinstance(resolved, str):
            resolved = resolved.strip().casefold() in {"1", "true", "yes", "on"}
        if resolved:
            continue
        severity = _severity(source)
        if severity is None or severity > 2:
            continue
        thread_id = _thread_id(source)
        if head_sha and seen_at and seen_at.get(thread_id) == head_sha:
            continue
        outdated = _is_outdated(source)
        fact = dict(source)
        fact.update(
            {
                "id": thread_id,
                "is_bot": True,
                "severity": f"P{severity}",
                "resolved": False,
                "outdated": outdated,
                "path": _value(source, "path", default=None),
                "line": _line(source, outdated),
            }
        )
        result.append(fact)
    return result


def ci_state(facts: Mapping[str, Any]) -> str | None:
    """Extract a normalized CI state from a PR fact or aggregate."""

    value: Any = _value(facts, "ci_state", "ciState", default=None)
    if value is None:
        value = _value(facts, "ci", default=None)
    if isinstance(value, Mapping):
        value = _value(value, "state", "status", "conclusion", default=None)
    if value is None:
        prs = _value(facts, "prs", default=())
        if isinstance(prs, Mapping):
            prs = prs.values()
        if isinstance(prs, Iterable) and not isinstance(prs, (str, bytes, Mapping)):
            states = [ci_state(pr) for pr in prs if isinstance(pr, Mapping)]
            states = [state for state in states if state]
            # Fail closed for multiple PRs unless all are at the same state.
            if states and all(state == states[0] for state in states):
                value = states[0]
    if value is None:
        return None
    return str(value).strip().casefold() or None


def ci_finished(facts: Mapping[str, Any]) -> bool:
    """Whether CI has reached a terminal, actionable state."""

    return ci_state(facts) in {
        "green",
        "red",
        "success",
        "succeeded",
        "passed",
        "pass",
        "failure",
        "failed",
        "error",
        "cancelled",
        "canceled",
    }


def merged(facts: Mapping[str, Any]) -> bool:
    """Return whether an observed PR (or every observed PR) is merged."""

    if "merged" in facts:
        return facts.get("merged") is True
    prs = _value(facts, "prs", default=())
    if isinstance(prs, Mapping):
        prs = prs.values()
    if isinstance(prs, Iterable) and not isinstance(prs, (str, bytes, Mapping)):
        rows = [item for item in prs if isinstance(item, Mapping)]
        return bool(rows) and all(merged(item) for item in rows)
    return False


def _utf8_prefix(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _safe_body(body: str) -> str:
    # Escaping angle brackets prevents a review body from manufacturing a
    # closing fence or looking like a system instruction.  Preserve all prose
    # otherwise; the skill can decide what it means.
    return html.escape(body, quote=False)


def build_resume_instruction(
    ticket: Mapping[str, Any],
    run: Mapping[str, Any],
    facts: Mapping[str, Any] | None = None,
    threads: Iterable[Mapping[str, Any]] | None = None,
    *,
    max_bytes: int = MAX_RESUME_BYTES,
) -> str:
    """Build a bounded same-session resume instruction.

    The byte limit is strict, not an approximation based on Python character
    count.  Every included body has a matching open/close fence, even when a
    hostile connector response forces the body to be truncated to empty.
    """

    facts = facts or {}
    identifier = _text(_value(ticket, "identifier", "id", default=""))
    run_id = _text(_value(run, "id", "run_id", "session_id", default=""))
    head = _text(_value(facts, "head_sha", "headSha", "current_head", "sha", default="unknown"), "unknown")
    if head == "unknown":
        prs = _value(facts, "prs", default=())
        if isinstance(prs, Mapping):
            prs = prs.values()
        if isinstance(prs, Iterable) and not isinstance(prs, (str, bytes, Mapping)):
            first = next((item for item in prs if isinstance(item, Mapping)), None)
            if first is not None:
                head = _text(_value(first, "head_sha", "headSha", "headRefOid", "sha", default="unknown"), "unknown")
    lines = [f"Continue ticket {identifier} in the existing builder session {run_id}."]
    state = ci_state(facts)
    pr_metadata = "\n".join(
        (
            "head_sha=" + _utf8_prefix(_safe_body(head), 256),
            "ci_state=" + _utf8_prefix(_safe_body(state or ""), 128),
            f"merged={'true' if merged(facts) else 'false'}",
        )
    )
    lines.extend(
        [
            "Observed untrusted PR facts:",
            "<untrusted-pr-facts>",
            pr_metadata,
            "</untrusted-pr-facts>",
        ]
    )

    # Build metadata first.  Body budgets are reduced if needed so every
    # supplied thread remains represented without crossing the hard bound.
    blocks: list[tuple[str, str]] = []
    for source in threads or ():
        if not isinstance(source, Mapping):
            continue
        thread_id = _utf8_prefix(_safe_body(_thread_id(source)), 48)
        outdated = _is_outdated(source)
        path = _utf8_prefix(
            _safe_body(_text(_value(source, "path", default="(no path)"), "(no path)")),
            96,
        )
        line = _line(source, outdated)
        if outdated:
            metadata = f"Review thread {thread_id}; path={path}; outdated: true."
        elif line is None:
            metadata = f"Review thread {thread_id}; path={path}; outdated: false."
        else:
            metadata = f"Review thread {thread_id}; path={path}; line={line}; outdated: false."
        body = _safe_body(_thread_body(source))
        blocks.append((metadata, body))

    footer = "Continue your workflow using these facts; do not load the skill again."
    # Reserve enough space for the footer when it fits.  If a caller passes a
    # very small test limit, the same function still honors the requested cap.
    prefix = "\n".join(lines)
    # The contract says "< 2 KB", so leave one byte of headroom when the
    # default 2048-byte ceiling is used.  This also makes callers' strict
    # ``len(...) < max_bytes`` assertions deterministic.
    available = max(0, int(max_bytes) - 1)
    result = prefix
    if blocks:
        footer_cost = len(("\n" + footer).encode("utf-8"))
        # Allocate the body budget across ALL threads before writing any of
        # them.  A greedy first-come pass would spend the whole envelope on
        # the first one or two bodies and silently drop the rest -- and with
        # bot_rework_cap at 1, a dropped P2 finding is never offered again.
        overhead = sum(
            len(f"\n{UNTRUSTED_OPEN}\n{metadata}\n\n{UNTRUSTED_CLOSE}".encode("utf-8"))
            for metadata, _ in blocks
        )
        spare = available - len(prefix.encode("utf-8")) - footer_cost - overhead
        if spare >= 0:
            # Every thread fits at least its metadata and an empty fence, so
            # share what is left equally and keep the footer.
            body_budget = min(900, spare // len(blocks))
            remainder = spare - body_budget * len(blocks) if body_budget < 900 else 0
            for metadata, body in blocks:
                budget = body_budget
                if remainder > 0:
                    # Hand the rounding slack to the earliest bodies rather
                    # than wasting it.
                    extra = min(remainder, 900 - budget)
                    budget += extra
                    remainder -= extra
                fitted = _utf8_prefix(body, max(0, budget))
                result = f"{result}\n{UNTRUSTED_OPEN}\n{metadata}\n{fitted}\n{UNTRUSTED_CLOSE}"
            result += "\n" + footer
        else:
            # Never acknowledge a selected thread that did not fit into the
            # handoff. The caller will leave every thread unseen and retry or
            # escalate instead of silently losing actionable feedback.
            raise ValueError("resume instruction cannot represent every review thread")
    elif len(result.encode("utf-8")) + len(("\n" + footer).encode("utf-8")) <= available:
        result += "\n" + footer

    # Defensive final clipping is only reachable with an unusually tiny
    # max_bytes or enormous ticket/run identifiers.  Keep UTF-8 valid.
    return _utf8_prefix(result, available)


resume_instruction = build_resume_instruction


__all__ = [
    "MAX_RESUME_BYTES",
    "UNTRUSTED_CLOSE",
    "UNTRUSTED_OPEN",
    "build_resume_instruction",
    "ci_finished",
    "ci_state",
    "eligible_review_threads",
    "merged",
    "resume_instruction",
]
