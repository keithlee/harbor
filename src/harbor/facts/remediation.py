"""Deterministic, zero-model remediation facts for blocked builder runs.

This module deliberately knows only a tiny set of safe environment repairs.
It returns a command plan first, and executes through an injected callable so
behavioral tests never touch a developer's package manager, emulator, or
worktree.  Unknown blockers fail closed and are handed to the next ladder
rung.
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


MODULE_NOT_FOUND_MARKERS = ("cannot find module", "module not found")
ADB_NOT_RUNNING_MARKERS = (
    "adb server",
    "cannot connect to adb",
    "unable to connect to adb",
    "no devices/emulators found",
)


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


def blocker_text(blocker: Any) -> str:
    """Collect only observed blocker prose, preserving it as data."""

    if blocker is None:
        return ""
    if isinstance(blocker, str):
        return blocker
    values: list[str] = []
    for name in ("kind", "question", "evidence", "summary", "error", "message", "detail"):
        value = _value(blocker, name, default=None)
        if value is not None:
            if isinstance(value, (list, tuple, set)):
                values.extend(str(item) for item in value)
            else:
                values.append(str(value))
    return "\n".join(values)


@dataclass(frozen=True)
class RemediationPlan:
    """A deterministic repair and the reason it is allowed."""

    kind: str
    commands: tuple[tuple[str, ...], ...]
    reason: str
    retry_same_session: bool = True

    @property
    def command(self) -> tuple[str, ...] | None:
        return self.commands[0] if self.commands else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "commands": [list(command) for command in self.commands],
            "reason": self.reason,
            "retry_same_session": self.retry_same_session,
        }


@dataclass(frozen=True)
class RemediationResult:
    plan: RemediationPlan | None
    attempted: bool
    fixed: bool
    outputs: tuple[Any, ...] = ()
    error: str | None = None

    @property
    def handled(self) -> bool:
        return self.fixed

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.as_dict() if self.plan else None,
            "attempted": self.attempted,
            "fixed": self.fixed,
            "outputs": list(self.outputs),
            "error": self.error,
        }


def remediation_plan(blocker: Any) -> RemediationPlan | None:
    """Return the one safe playbook matching ``blocker`` or ``None``.

    ``Cannot find module`` is intentionally matched case-insensitively and
    maps to one package installation.  ADB startup is similarly deterministic
    and never broadens into device selection, APK installation, or retries.
    """

    text = blocker_text(blocker).casefold()
    if any(marker in text for marker in MODULE_NOT_FOUND_MARKERS):
        return RemediationPlan(
            kind="missing_module",
            commands=(("pnpm", "install"),),
            reason="Cannot find module is repaired by installing the locked workspace dependencies.",
        )
    if any(marker in text for marker in ADB_NOT_RUNNING_MARKERS):
        return RemediationPlan(
            kind="adb_server",
            commands=(("adb", "start-server"),),
            reason="The Android bridge server is not running and can be started deterministically.",
        )
    return None


plan_remediation = remediation_plan
deterministic_plan = remediation_plan


def _successful(value: Any) -> bool:
    if value is None:
        # A command executor that returns None conventionally means success.
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        for key in ("fixed", "success", "ok", "passed"):
            if key in value:
                return bool(value[key])
        if "returncode" in value:
            return int(value["returncode"] or 0) == 0
    returncode = getattr(value, "returncode", None)
    if returncode is not None:
        try:
            return int(returncode) == 0
        except (TypeError, ValueError):
            return False
    return True


async def _invoke_executor(executor: Any, command: Sequence[str], cwd: Path | None) -> Any:
    target = executor
    if target is None:
        # Shell-free invocation is important: blocker text never becomes part
        # of a shell command.  ``check=True`` turns a failed repair into a
        # rung-1 escalation rather than claiming it was fixed.
        return await asyncio.to_thread(
            subprocess.run,
            list(command),
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            text=True,
        )
    for name in ("run", "execute", "invoke"):
        candidate = getattr(target, name, None)
        if callable(candidate):
            target = candidate
            break
    if not callable(target):
        raise TypeError("remediation executor must be callable or expose run(command)")
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        parameters = list(signature.parameters.values())
        names = {parameter.name for parameter in parameters}
        if "cwd" in names:
            result = target(list(command), cwd=cwd)
        elif len(
            [
                parameter
                for parameter in parameters
                if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
        ) <= 1:
            result = target(list(command))
        else:
            result = target(list(command), cwd)
    else:
        result = target(list(command))
    return await result if inspect.isawaitable(result) else result


async def execute_remediation(
    blocker: Any,
    *,
    executor: Callable[..., Any] | Any | None = None,
    cwd: str | Path | None = None,
) -> RemediationResult:
    """Execute a known plan and report whether every command succeeded."""

    plan = remediation_plan(blocker)
    if plan is None:
        return RemediationResult(None, attempted=False, fixed=False, error="no deterministic remediation")
    workdir = Path(cwd) if cwd is not None else None
    outputs: list[Any] = []
    try:
        for command in plan.commands:
            value = await _invoke_executor(executor, command, workdir)
            outputs.append(value)
            if not _successful(value):
                return RemediationResult(plan, attempted=True, fixed=False, outputs=tuple(outputs), error="remediation command failed")
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        return RemediationResult(plan, attempted=True, fixed=False, outputs=tuple(outputs), error=str(exc))
    return RemediationResult(plan, attempted=True, fixed=True, outputs=tuple(outputs))


remediate = execute_remediation
run_remediation = execute_remediation


__all__ = [
    "ADB_NOT_RUNNING_MARKERS",
    "MODULE_NOT_FOUND_MARKERS",
    "RemediationPlan",
    "RemediationResult",
    "blocker_text",
    "deterministic_plan",
    "execute_remediation",
    "plan_remediation",
    "remediate",
    "remediation_plan",
    "run_remediation",
]
