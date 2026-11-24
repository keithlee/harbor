"""The one definition of a "stopped" Linear ticket.

A stopped ticket is shelved (backlog/paused) or closed (done/canceled). No
gate may resume, retry or recover its builder: First Mate recovery, the answer
queue, the unblock ladder and the supervisor's stopped-builder retry all ask
:func:`is_stopped` so they cannot disagree.

Human Review is deliberately *not* stopped: Harbor parks escalations there and
resumes the builder from it.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

STOPPED_STATE_TYPES = frozenset({"backlog", "completed", "canceled"})
STOPPED_STATE_NAMES = frozenset({"backlog", "paused", "done", "completed", "canceled", "cancelled"})


def _fold(value: Any) -> str:
    return str(value or "").strip().casefold()


def configured_stopped_names(states: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The workflow's extra paused and terminal state names (``[states]`` in workflow.toml)."""
    states = states or {}
    return (*states.get("paused", ()), *states.get("terminal", ()))


def stopped_names(extra: Iterable[Any] = ()) -> frozenset[str]:
    """Built-in stopped names plus ``extra`` (already-listed workflow names), case-folded."""
    return STOPPED_STATE_NAMES | {_fold(name) for name in extra}


def is_stopped(state: Any, state_type: Any, states: Mapping[str, Any] | None = None) -> bool:
    """Whether a ticket in ``state`` (name) of ``state_type`` is shelved or closed.

    ``states`` is the workflow's ``[states]`` table; its ``paused`` and
    ``terminal`` names count as stopped too.
    """
    return (_fold(state_type) in STOPPED_STATE_TYPES
            or _fold(state) in stopped_names(configured_stopped_names(states)))


def workflow_states(owner: Any) -> Mapping[str, Any]:
    """The ``[states]`` table of ``owner.workflow`` (empty when there is no workflow)."""
    workflow = getattr(owner, "workflow", None)
    return workflow.config.get("states", {}) if workflow is not None else {}
