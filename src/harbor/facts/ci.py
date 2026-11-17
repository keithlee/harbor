"""Deterministic classification of GitHub status-check rollups."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


_SUCCESS = {"SUCCESS", "SUCCEEDED", "PASS", "PASSED", "GREEN"}
_FAILURE = {
    "FAILURE",
    "FAILED",
    "ERROR",
    "FAIL",
    "CANCELLED",
    "CANCELED",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
}
_PENDING = {
    "EXPECTED",
    "PENDING",
    "QUEUED",
    "IN_PROGRESS",
    "INPROGRESS",
    "REQUESTED",
    "WAITING",
    "BLOCKED",
}


def _value(item: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] is not None:
            return item[name]
    return None


def _contexts(rollup: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    if isinstance(rollup, Mapping):
        contexts = rollup.get("contexts", rollup.get("nodes", ()))
        if isinstance(contexts, Mapping):
            contexts = contexts.get("nodes", ())
    else:
        contexts = rollup
    if not contexts:
        return []
    return [item for item in contexts if isinstance(item, Mapping)]


def _name(context: Mapping[str, Any]) -> str:
    name = _value(context, "name", "context", "label", "workflowName")
    if name is not None:
        return str(name)
    # A few GraphQL fakes put the CheckRun fields below a ``checkRun`` key.
    nested = context.get("checkRun")
    if isinstance(nested, Mapping):
        return _name(nested)
    return ""


def _conclusion(context: Mapping[str, Any]) -> str:
    value = _value(context, "conclusion", "state", "status", "result")
    if value is None:
        nested = context.get("checkRun")
        if isinstance(nested, Mapping):
            return _conclusion(nested)
        return ""
    return str(value).upper().replace("-", "_").replace(" ", "_")


def _normalise_required(required_checks: Iterable[str] | None) -> tuple[str, ...]:
    if required_checks is None:
        return ()
    if isinstance(required_checks, str):
        required_checks = (required_checks,)
    return tuple(str(check).strip() for check in required_checks if str(check).strip())


def classify_ci(
    rollup: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
    required_checks: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return ``{"state", "required_seen"}`` for a GraphQL rollup.

    A null rollup is deliberately ``absent``.  A rollup can report overall
    success while omitting a required named check; that is ``pending`` (and
    never green).  This fail-closed behavior matters for repositories where
    Actions billing can leave the rollup null or incomplete.
    """

    if rollup is None:
        return {"state": "absent", "required_seen": []}

    required = _normalise_required(required_checks)
    contexts = _contexts(rollup)
    by_name: dict[str, list[str]] = {}
    for context in contexts:
        name = _name(context)
        if name:
            by_name.setdefault(name, []).append(_conclusion(context))

    # Match required check names exactly.  GitHub's check names are case
    # sensitive in the UI, but lower-casing the comparison makes profile edits
    # less surprising without changing the names returned in required_seen.
    lower = {name.casefold(): name for name in by_name}
    seen: list[str] = []
    missing = False
    pending = False
    failed = False
    for required_name in required:
        actual = lower.get(required_name.casefold())
        if actual is None:
            missing = True
            continue
        seen.append(actual)
        conclusions = by_name[actual]
        if not conclusions or any(item in _FAILURE for item in conclusions):
            failed = True
        elif any(item in _PENDING or not item for item in conclusions):
            pending = True
        elif not all(item in _SUCCESS for item in conclusions):
            # Unknown conclusions are not proof of success.
            pending = True

    if failed:
        state = "red"
    elif missing or pending:
        state = "pending"
    elif not required:
        # No named checks means Harbor cannot assert the condition required by
        # the contract, even if GitHub's aggregate rollup says SUCCESS.
        state = "absent"
    else:
        state = "green"
    return {"state": state, "required_seen": seen}


def classify_ci_state(
    rollup: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
    required_checks: Iterable[str] | None = None,
) -> str:
    """Small public helper used by callers that only need the state."""

    return str(classify_ci(rollup, required_checks)["state"])


# Names used by earlier callers and useful in a tiny public facts API.
ci_facts = classify_ci
classify = classify_ci


__all__ = ["classify", "classify_ci", "classify_ci_state", "ci_facts"]
