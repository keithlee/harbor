"""Codex JSONL event parsing and liveness plumbing.

Codex's ``exec --json`` stream is intentionally treated as an untrusted event
source.  One malformed line does not hide later events or make the supervisor
think a process is idle.  A caller can attach an activity callback; it is
called for every non-empty line, including malformed output.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping


ActivityCallback = Callable[["StreamEvent"], Awaitable[None] | None]


@dataclass(frozen=True)
class StreamEvent:
    """One line from the Codex event stream."""

    data: Mapping[str, Any] | None
    raw: str
    received_at: float = field(default_factory=time.monotonic)
    parse_error: str | None = None

    @property
    def payload(self) -> Mapping[str, Any] | None:
        return self.data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default) if self.data is not None else default

    def __getitem__(self, key: str) -> Any:
        if self.data is None:
            raise KeyError(key)
        return self.data[key]

    @property
    def type(self) -> str:
        if self.data is None:
            return "raw"
        value = self.data.get("type") or self.data.get("event") or self.data.get("kind")
        return str(value) if value is not None else "unknown"

    @property
    def text(self) -> str | None:
        if self.data is None:
            return self.raw
        return _text_value(self.data)

    @property
    def session_id(self) -> str | None:
        if self.data is None:
            return None
        for key in ("session_id", "sessionId", "thread_id", "threadId", "conversation_id"):
            value = self.data.get(key)
            if value:
                return str(value)
        return None

    @property
    def cost_usd(self) -> float | None:
        value = _find_first(self.data, ("cost_usd", "costUsd", "total_cost_usd", "totalCostUsd"))
        if value is None:
            usage = self.data.get("usage") if isinstance(self.data, Mapping) else None
            value = _find_first(usage, ("cost_usd", "costUsd", "total_cost_usd", "totalCostUsd"))
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None


def _find_first(value: Any, names: Iterable[str]) -> Any:
    if not isinstance(value, Mapping):
        return None
    for name in names:
        if name in value:
            return value[name]
    return None


def _text_value(value: Any) -> str | None:
    """Find text in both old flat events and current content-block events."""

    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "message", "content", "output_text"):
            if key in value:
                found = _text_value(value[key])
                if found is not None:
                    return found
        for key in ("item", "output", "result", "data", "payload", "response", "last_agent_message"):
            if key in value:
                found = _text_value(value[key])
                if found is not None:
                    return found
    if isinstance(value, list):
        for item in value:
            found = _text_value(item)
            if found is not None:
                return found
    return None


def parse_line(line: bytes | str) -> StreamEvent | None:
    """Parse one JSONL line, retaining malformed output as a liveness event."""

    if isinstance(line, bytes):
        text = line.decode("utf-8", errors="replace")
    else:
        text = str(line)
    text = text.rstrip("\r\n")
    if not text.strip():
        return None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        return StreamEvent(data=None, raw=text, parse_error=str(exc))
    if not isinstance(decoded, Mapping):
        return StreamEvent(data={"type": "value", "value": decoded}, raw=text)
    return StreamEvent(data=dict(decoded), raw=text)


parse_event = parse_line


def parse_events(lines: Iterable[bytes | str]) -> Iterator[StreamEvent]:
    for line in lines:
        event = parse_line(line)
        if event is not None:
            yield event


parse_stream = parse_events


def _json_from_text(text: str | None) -> Any:
    if not text:
        return None
    candidate = text.strip()
    # Codex agent messages occasionally fence the structured result.
    if candidate.startswith("```"):
        pieces = candidate.splitlines()
        if pieces and pieces[0].startswith("```"):
            pieces = pieces[1:]
        if pieces and pieces[-1].strip() == "```":
            pieces = pieces[:-1]
        candidate = "\n".join(pieces).strip()
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None


_FINAL_TYPES = frozenset(
    {
        "result",
        "final",
        "final_result",
        "task_complete",
        "task.completed",
        "turn.completed",
        "turn_complete",
        "session.completed",
        "response.completed",
    }
)


def _event_types(event: StreamEvent) -> frozenset[str]:
    """Return envelope and nested event kinds carried by one stream line."""

    if event.data is None:
        return frozenset()
    values: set[str] = {event.type.casefold()}
    payload = event.data.get("payload")
    if isinstance(payload, Mapping):
        for key in ("type", "event", "kind"):
            value = payload.get(key)
            if value is not None:
                values.add(str(value).casefold())
    return frozenset(values)


def _is_terminal_event(event: StreamEvent) -> bool:
    """Whether *event* is an explicit Codex turn/task result envelope."""

    if event.data is None:
        return False
    event_types = _event_types(event)
    # A bare schema object is the compact result form used by a few runner
    # adapters.  Real Codex progress messages carry an ``event_msg`` or
    # ``response_item`` envelope, so an untyped object with the required result
    # keys is safe to treat as terminal.
    if event.type.casefold() == "unknown" and "status" in event.data and "summary" in event.data:
        return True
    return any(
        value in _FINAL_TYPES
        or (
            value.endswith(".completed")
            and value not in {"item.completed", "message.completed", "command.completed"}
        )
        for value in event_types
    )


def _text_payload(event: StreamEvent) -> Mapping[str, Any] | None:
    """Parse a JSON result carried in an agent-message text block."""

    candidate = _json_from_text(event.text)
    if not isinstance(candidate, Mapping):
        return None
    nested = candidate.get("result")
    if isinstance(nested, Mapping) and "status" in nested:
        candidate = nested
    if "status" in candidate and "summary" in candidate:
        return dict(candidate)
    return None


def final_payload(event: StreamEvent) -> Mapping[str, Any] | None:
    """Return a structured final result from an explicit terminal event.

    Progress messages frequently contain schema-shaped JSON for operator
    visibility.  They are held by :func:`extract_final` until a terminal
    envelope arrives and must never be returned as the final result by
    themselves.
    """

    if event.data is None:
        return None
    terminal = _is_terminal_event(event)
    if not terminal:
        return None
    if "status" in event.data and "summary" in event.data:
        return dict(event.data)
    candidates: list[Any] = []
    for key in ("structured_output", "result", "output", "final", "data", "payload"):
        if key in event.data:
            candidates.append(event.data[key])
    # Some Codex versions put the final JSON in the terminal event's agent
    # message text.  ``item.completed`` itself is not terminal; extract_final
    # carries that text forward until ``turn.completed``/``task_complete``.
    candidates.append(_text_payload(event))
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            nested = candidate.get("result")
            if isinstance(nested, Mapping) and "status" in nested:
                candidate = nested
            if "status" in candidate and "summary" in candidate:
                return dict(candidate)
    return None


def extract_final(events: Iterable[StreamEvent]) -> Mapping[str, Any] | None:
    result: Mapping[str, Any] | None = None
    pending: Mapping[str, Any] | None = None
    for event in events:
        if _is_terminal_event(event):
            candidate = final_payload(event) or pending
            if candidate is not None:
                result = candidate
            pending = None
            continue
        # Keep schema-shaped text from nonterminal agent messages provisional.
        # If the process is killed before Codex emits its terminal envelope,
        # extract_final must return None instead of promoting progress JSON.
        candidate = _text_payload(event)
        if candidate is not None:
            pending = candidate
    return result


async def iter_stream(
    reader: asyncio.StreamReader,
    *,
    on_event: ActivityCallback | None = None,
) -> AsyncIterator[StreamEvent]:
    """Yield events until EOF and invoke ``on_event`` for every event."""

    while True:
        try:
            line = await reader.readline()
        except ValueError as exc:
            # A line longer than the reader's buffer limit.  ``readline``
            # raises rather than returning it, and that exception would
            # otherwise propagate out of the poll loop and kill the whole
            # supervisor, orphaning a live builder.  The over-long line is
            # already dropped from the buffer and the stream stays aligned on
            # the next line, so surface it as a parse error and keep reading:
            # a lost event still counts as liveness.
            event = StreamEvent(data=None, raw="", parse_error=f"oversized stream line: {exc}")
            if on_event is not None:
                callback_result = on_event(event)
                if inspect.isawaitable(callback_result):
                    await callback_result
            yield event
            continue
        if not line:
            return
        event = parse_line(line)
        if event is None:
            continue
        if on_event is not None:
            callback_result = on_event(event)
            if inspect.isawaitable(callback_result):
                await callback_result
        yield event


read_stream = iter_stream
stream_events = iter_stream


__all__ = [
    "ActivityCallback",
    "StreamEvent",
    "extract_final",
    "final_payload",
    "iter_stream",
    "parse_event",
    "parse_events",
    "parse_line",
    "parse_stream",
    "read_stream",
    "stream_events",
]
