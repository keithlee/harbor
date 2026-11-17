"""Injectable macOS Notification Center delivery.

Notification delivery is intentionally a tiny boundary.  Harbor decides when
to park; this module only turns the literal reason into one ``osascript``
invocation.  Tests can inject an executor and therefore never send a real
notification.
"""

from __future__ import annotations

import inspect
import subprocess
from collections.abc import Callable, Sequence
from typing import Any


DEFAULT_TITLE = "Harbor"
Executor = Callable[[Sequence[str]], Any]


def _applescript_string(value: str) -> str:
    """Escape a value for a double-quoted AppleScript string literal."""

    # AppleScript accepts backslash escapes for quote, slash, and line breaks.
    # Replace slash first so the escapes we add below are not re-escaped.
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def build_notification_command(reason: str, *, title: str = DEFAULT_TITLE) -> list[str]:
    """Build a shell-free Notification Center command."""

    literal_reason = str(reason)
    literal_title = str(title)
    script = (
        f'display notification "{_applescript_string(literal_reason)}" '
        f'with title "{_applescript_string(literal_title)}"'
    )
    return ["osascript", "-e", script]


notification_command = build_notification_command


def _default_executor(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


class Notifier:
    """Send one notification through an injectable executor."""

    def __init__(self, executor: Executor | None = None, *, title: str = DEFAULT_TITLE) -> None:
        self.executor = executor if executor is not None else _default_executor
        self.title = title

    async def notify(self, reason: str) -> Any:
        """Execute a Notification Center command with ``reason`` unchanged."""

        command = build_notification_command(str(reason), title=self.title)
        result = self.executor(command)
        if inspect.isawaitable(result):
            return await result
        return result

    async def send(self, reason: str) -> Any:
        return await self.notify(reason)

    __call__ = notify


MacOSNotifier = Notifier
NotificationCenter = Notifier


async def send_notification(
    reason: str,
    *,
    executor: Executor | None = None,
    title: str = DEFAULT_TITLE,
) -> Any:
    """One-shot async helper used by supervisor park paths."""

    return await Notifier(executor, title=title).notify(reason)


notify = send_notification
notify_park = send_notification


__all__ = [
    "DEFAULT_TITLE",
    "MacOSNotifier",
    "NotificationCenter",
    "Notifier",
    "build_notification_command",
    "notification_command",
    "notify",
    "notify_park",
    "send_notification",
]
