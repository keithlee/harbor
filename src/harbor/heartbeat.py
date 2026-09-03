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

    @property
    def last_activity_at(self) -> str | None:
        return self.last_activity

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
        healthy = bool(_get(status, "linear_healthy", "healthy", default=False))
        lifecycle = str(_get(status, "lifecycle", default="UNKNOWN"))
        event = HeartbeatEvent(
            project=str(_get(status, "project", "project_name", default=self.project)),
            lifecycle=lifecycle,
            healthy=healthy,
            last_activity=last_activity,
            idle_seconds=idle,
            published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            snapshot_hash=_get(status, "last_snapshot_hash", "snapshot_hash", default=None),
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
    "HeartbeatService",
    "SupervisorHeartbeat",
]
