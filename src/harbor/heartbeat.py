"""A model-free, pause-aware supervisor heartbeat.

Heartbeats are operational publications, not conversational turns.  They read
the latest status/last-activity value and hand one immutable event to an
injected publisher.  No First Mate, wake decider, Linear mutation, or GitHub
mutation is reachable from this module.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping


DEFAULT_HEARTBEAT_SECONDS = 60.0
_ACTIVE_RUN_STATUSES = frozenset({"running", "waiting", "blocked", "parked"})
_ACTIVE_RUN_ORDER = {
    status: index
    for index, status in enumerate(("running", "waiting", "blocked", "parked"))
}


def _get(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _safe_text(value: Any, default: str = "") -> str:
    """Fold untrusted status values into one bounded, terminal-safe line."""

    if value is None:
        return default
    try:
        raw = str(value)
    except Exception:
        return default
    # Newlines, tabs, and control characters must not let an externally
    # supplied ticket/status value create a second terminal line or escape
    # the heartbeat's deterministic shape.
    clean = "".join(
        character if ord(character) >= 32 and ord(character) != 127 else " "
        for character in raw
    )
    text = " ".join(clean.split())
    return text[:120] if text else default


def _items(value: Any) -> tuple[Any, ...]:
    if value is None or isinstance(value, (str, bytes)):
        return ()
    if isinstance(value, Mapping):
        return (value,)
    try:
        return tuple(value)
    except TypeError:
        return ()


def _healthy(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"0", "false", "no", "off", "unavailable", "red"}:
            return False
        if normalized in {"1", "true", "yes", "on", "healthy", "green"}:
            return True
    return bool(value)


@dataclass(frozen=True)
class HeartbeatRun:
    """The small run fact needed to explain what the fleet is doing."""

    ticket: str
    status: str
    waiting_on: str

    def as_dict(self) -> dict[str, str]:
        return {
            "ticket": self.ticket,
            "status": self.status,
            "waiting_on": self.waiting_on,
        }


def _active_runs(status: Any) -> tuple[HeartbeatRun, ...]:
    """Extract only active run states in a stable operator-facing order."""

    runs = _items(_get(status, "runs", "active_runs", default=()))
    active: list[HeartbeatRun] = []
    for run in runs:
        state = _safe_text(_get(run, "status", "state", default="")).casefold()
        outcome = _safe_text(_get(run, "outcome", default="")).casefold()
        # Parked work is commonly persisted as a completed run with
        # ``outcome=parked`` and ``waiting_on=human``.  Surface it as parked
        # while keeping all other terminal runs out of a liveness heartbeat.
        if state not in _ACTIVE_RUN_STATUSES:
            if outcome != "parked":
                continue
            state = "parked"
        ticket = _safe_text(
            _get(run, "ticket", "identifier", "ticket_id", "ticketId", default="?"),
            "?",
        )
        waiting_on = _safe_text(
            _get(run, "waiting_on", "waitingOn", default="none"),
            "none",
        )
        active.append(HeartbeatRun(ticket=ticket, status=state, waiting_on=waiting_on))
    active.sort(
        key=lambda run: (
            _ACTIVE_RUN_ORDER.get(run.status, len(_ACTIVE_RUN_ORDER)),
            run.ticket.casefold(),
            run.waiting_on.casefold(),
        )
    )
    return tuple(active)


def _timestamp(value: Any, *, now: float) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.isoformat(timespec="seconds")
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    return text or None


def _idle_seconds(value: Any, *, monotonic_now: float) -> float:
    if isinstance(value, (int, float)):
        # Lightweight fakes generally use the same monotonic clock that they
        # pass into Heartbeat.
        return max(0.0, monotonic_now - float(value))
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return 0.0
    else:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


@dataclass(frozen=True)
class HeartbeatEvent:
    project: str
    lifecycle: str
    healthy: bool
    last_activity: str | None
    idle_seconds: float
    published_at: str
    snapshot_hash: str | None = None
    active_runs: tuple[HeartbeatRun, ...] = ()

    @property
    def last_activity_at(self) -> str | None:
        return self.last_activity

    @property
    def fleet_status(self) -> str:
        """Return the concise, deterministic fleet activity description."""

        if not self.active_runs:
            return "idle"
        return "; ".join(
            f"{run.ticket} {run.status} waiting_on={run.waiting_on}" for run in self.active_runs
        )

    @property
    def fleet(self) -> str:
        """Compatibility alias for integrations that call this fleet output."""

        return self.fleet_status

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "lifecycle": self.lifecycle,
            "healthy": self.healthy,
            "last_activity": self.last_activity,
            "last_activity_at": self.last_activity,
            "idle_seconds": self.idle_seconds,
            "published_at": self.published_at,
            "snapshot_hash": self.snapshot_hash,
            "active_runs": [run.as_dict() for run in self.active_runs],
            "fleet": self.fleet_status,
        }


Publisher = Callable[[HeartbeatEvent], Awaitable[None] | None]


class Heartbeat:
    """Publish one status event on a configurable interval.

    ``status_provider`` and ``last_activity`` are called only when a tick is
    due.  Both are duck-typed so a supervisor, test fake, or an embedding can
    supply them without importing each other.  Calling :meth:`tick` directly
    is useful for deterministic tests and does not create a background task.
    """

    def __init__(
        self,
        project: str = "Harbor",
        *,
        interval_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        heartbeat_interval_seconds: float | None = None,
        interval: float | None = None,
        status_provider: Any | None = None,
        last_activity: Any | None = None,
        publisher: Publisher | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if heartbeat_interval_seconds is not None:
            interval_seconds = heartbeat_interval_seconds
        if interval is not None:
            interval_seconds = interval
        if float(interval_seconds) <= 0:
            raise ValueError("heartbeat interval must be greater than zero")
        self.project = str(project)
        self.interval_seconds = float(interval_seconds)
        self.status_provider = status_provider
        self.last_activity = last_activity
        self.publisher = publisher
        self.clock = clock
        self.last_event: HeartbeatEvent | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def _status(self) -> Any:
        source = self.status_provider
        if callable(source) and not isinstance(source, type):
            return source()
        return source or {}

    def _activity(self, status: Any) -> Any:
        value = self.last_activity
        if callable(value) and not isinstance(value, type):
            value = value()
        if value is None:
            value = _get(status, "last_activity", "last_activity_at", default=None)
        return value

    async def tick(self, status: Any | None = None) -> HeartbeatEvent:
        """Publish exactly one event and return it; this never invokes a model."""

        status = self._status() if status is None else status
        now = self.clock()
        activity = self._activity(status)
        last_activity = _timestamp(activity, now=now)
        # A monotonic activity timestamp may be supplied by lightweight fakes.
        idle = _idle_seconds(activity, monotonic_now=now)
        healthy = _healthy(_get(status, "linear_healthy", "healthy", default=False))
        lifecycle = str(_get(status, "lifecycle", default="UNKNOWN"))
        event = HeartbeatEvent(
            project=str(_get(status, "project", "project_name", default=self.project)),
            lifecycle=lifecycle,
            healthy=healthy,
            last_activity=last_activity,
            idle_seconds=idle,
            published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            snapshot_hash=_get(status, "last_snapshot_hash", "snapshot_hash", default=None),
            active_runs=_active_runs(status),
        )
        self.last_event = event
        if self.publisher is not None:
            result = self.publisher(event)
            if inspect.isawaitable(result):
                await result
        return event

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                await self.tick()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name=f"harbor-heartbeat-{self.project}")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            await task

    close = stop


SupervisorHeartbeat = Heartbeat
HeartbeatService = Heartbeat


__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "Heartbeat",
    "HeartbeatEvent",
    "HeartbeatRun",
    "HeartbeatService",
    "SupervisorHeartbeat",
]
