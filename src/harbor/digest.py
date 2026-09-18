"""Deterministic, read-only status facts for the console and First Mate.

The digest is deliberately a formatting boundary.  It does not poll Linear or
GitHub and it never invokes a model.  Both the ``/status`` command and a First
Mate question consume the same rendered value, which prevents the answer from
quietly being based on a different snapshot than the one shown to the
operator.

The functions in this module accept either mappings or the small dataclasses
used by Harbor.  That keeps this layer independent of the supervisor and also
makes it useful to callers that already have a serialized status payload.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


def _get(value: Any, *names: str, default: Any = None) -> Any:
    """Read a field from a mapping or a light-weight object."""

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
    if isinstance(value, Mapping):
        # State objects returned by Linear are represented as ``{"name": ...}``.
        value = value.get("name", value.get("state", value))
    return str(value)


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on", "healthy", "green"}:
            return True
        if normalized in {"0", "false", "no", "off", "unavailable", "red"}:
            return False
    return bool(value)


def _items(value: Any) -> list[Any]:
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return []
    if isinstance(value, Iterable):
        return list(value)
    return []


_PR_NUMBER = re.compile(r"(?:/pull/|#)(\d+)(?:\b|$)", re.IGNORECASE)


def _pr_label(value: Any) -> str:
    """Return a stable short PR label from a URL or PR fact."""

    if isinstance(value, Mapping):
        number = _get(value, "number", "pr_number", default=None)
        if number is not None and str(number).isdigit():
            return f"PR #{number}"
        value = _get(value, "url", "html_url", default=value)
    raw = _text(value)
    match = _PR_NUMBER.search(raw)
    return f"PR #{match.group(1)}" if match else raw


def _ticket_line(ticket: Any) -> str:
    identifier = _text(_get(ticket, "identifier", "id", default="?"), "?")
    observed = " (observed)" if not _bool(_get(ticket, "owned", default=False)) else ""
    state = _text(_get(ticket, "state", default="unknown"), "unknown")
    title = " ".join(_text(_get(ticket, "title", default="")).split())

    # Poller tickets carry URLs; richer supervisor facts may carry PR objects.
    prs: list[Any] = []
    prs.extend(_items(_get(ticket, "prs", "pull_requests", default=())))
    prs.extend(_items(_get(ticket, "pr_urls", default=())))
    labels: list[str] = []
    for pr in prs:
        label = _pr_label(pr)
        if label and label not in labels:
            labels.append(label)
    labels.sort()

    suffix: list[str] = []
    if labels:
        suffix.append(", ".join(labels))

    ci = _get(ticket, "ci", "ci_state", default=None)
    if ci is not None:
        ci_state = _text(_get(ci, "state", default=ci), "unknown").lower()
        suffix.append(f"CI {ci_state}")

    threads = _items(_get(ticket, "threads", "review_threads", default=()))
    if threads:
        unresolved = [thread for thread in threads if not _bool(_get(thread, "resolved", "isResolved", default=False))]
        severities = sorted(
            {
                _text(_get(thread, "severity", default="")).upper()
                for thread in unresolved
                if _get(thread, "severity", default=None) is not None
            }
        )
        detail = f"{len(unresolved)} thread" + ("s" if len(unresolved) != 1 else "")
        if severities:
            detail += f" ({','.join(severities)})"
        suffix.append(detail)

    for gap in _items(_get(ticket, "handoff", default=())):
        summary = " ".join(_text(_get(gap, "summary", default="")).split())
        if summary:
            suffix.append(f"handoff gate: {summary}")

    line = f"{identifier}{observed}  {state}"
    if title:
        line += f"  {title}"
    if suffix:
        line += "  " + "  ".join(suffix)
    return line


def _folded(value: Any, limit: int) -> str:
    text = " ".join(_text(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _escalation_line(row: Any) -> str:
    """One open escalation: what to answer, why, and what was already tried."""

    escalation_id = _text(_get(row, "id", default="?"), "?")
    ticket = _text(_get(row, "ticket", default=None), "(no ticket)")
    kind = _text(_get(row, "kind", default="unknown"), "unknown")
    line = f"Escalation #{escalation_id}  {ticket}  {kind}  {_folded(_get(row, 'question', default=''), 200)}"
    details: list[str] = []
    if kind != "handoff_gate":
        try:
            rung = int(_get(row, "rung_resolved", default=2))
        except (TypeError, ValueError):
            rung = 2
        details.append("tried rung 0 and rung 1" if rung >= 2 else f"stopped at rung {rung}")
    reason = _folded(_get(row, "reason", default=""), 160)
    if reason:
        details.append(reason)
    searched = [_text(item) for item in _items(_get(row, "searched", default=()))]
    if searched:
        details.append(f"searched {', '.join(searched)}")
    if details:
        line += f"  ({'; '.join(details)})"
    return f"{line}  → /answer {escalation_id} <answer>"


def _status_value(source: Any) -> Any:
    """Resolve a Supervisor-like source without doing any I/O."""

    if callable(source) and not isinstance(source, type):
        return source()
    status = getattr(source, "status", None)
    if callable(status):
        return status()
    return source


def render_digest(source: Any) -> str:
    """Render a canonical, model-free status digest.

    ``source`` may be a :class:`Supervisor`, a ``SupervisorStatus``, or a
    mapping containing a status payload.  Ticket order is always by
    identifier and whitespace in externally supplied titles/errors is folded,
    so repeated calls over the same observation are byte-for-byte identical.
    """

    # A caller may already have a canonical digest (for example, when it is
    # passing the exact value shown by /status into First Mate).
    if isinstance(source, str):
        return source
    source = _status_value(source)
    if isinstance(source, str):
        return source
    project = _text(_get(source, "project", "project_name", default="Harbor"), "Harbor")
    lifecycle = _text(_get(source, "lifecycle", default="UNKNOWN"), "UNKNOWN")
    healthy = _bool(_get(source, "linear_healthy", "linearHealthy", default=False))
    lines = [
        f"Harbor: {lifecycle} · {project} · Linear {'healthy' if healthy else 'unavailable'}"
    ]

    error = _get(source, "last_error", "error", default=None)
    if error:
        lines.append(f"Linear error: {' '.join(str(error).split())}")

    tickets = _items(_get(source, "tickets", default=()))
    tickets.sort(key=lambda item: _text(_get(item, "identifier", "id", default="")))
    lines.extend(_ticket_line(ticket) for ticket in tickets)
    if not tickets:
        lines.append("No tickets in the configured poll states.")

    # A fleet digest may include retained builder sessions from the local
    # operational ledger.  These are observations only; rendering them never
    # polls or invokes a model.  Stable sorting keeps one snapshot byte-stable.
    runs = _items(_get(source, "runs", default=()))
    runs.sort(key=lambda item: _text(_get(item, "id", "run_id", default="")))
    for run in runs:
        run_id = _text(_get(run, "id", "run_id", default="?"), "?")
        role = _text(_get(run, "role", default="builder"), "builder")
        state = _text(_get(run, "status", default="unknown"), "unknown")
        session = _get(run, "session_id", "sessionId", default=None)
        waiting = _get(run, "waiting_on", "waitingOn", default=None)
        line = f"Run {run_id}  {role}  {state}"
        if session:
            line += f"  session {session}"
        if waiting:
            line += f"  waiting on {waiting}"
        outcome = _get(run, "outcome", default=None)
        if outcome:
            line += f"  outcome={_text(outcome).casefold()}"
        pr_url = _get(run, "pr_url", "pull_request_url", default=None)
        if pr_url:
            line += f"  pr_url={_text(pr_url)}"
        lines.append(line)

    # These fields are optional and are emitted only when a richer Phase 3+
    # status provider supplies them.  This keeps the Phase 1 shape compatible.
    escalations = _get(source, "escalations", default=None)
    if escalations is not None:
        values = _items(escalations)
        values.sort(key=lambda item: (len(_text(_get(item, "id", default=""))), _text(_get(item, "id", default=""))))
        lines.append(f"Escalations: {len(values)} open")
        lines.extend(_escalation_line(item) for item in values)
    spend = _get(source, "spend_today", "cost_today", "cost_usd", default=None)
    if spend is not None:
        try:
            lines.append(f"Spend today: ${float(spend):.2f}")
        except (TypeError, ValueError):
            # A malformed optional cost value must not make /status fail.
            pass
    return "\n".join(lines)


def status_digest(source: Any) -> str:
    """Named alias used by console integrations."""

    return render_digest(source)


def digest_for(source: Any) -> str:
    """Compatibility alias for callers that describe the operation as lookup."""

    return render_digest(source)


render_status = render_digest
build_digest = render_digest
digest = render_digest


__all__ = [
    "build_digest",
    "digest",
    "digest_for",
    "render_digest",
    "render_status",
    "status_digest",
]
