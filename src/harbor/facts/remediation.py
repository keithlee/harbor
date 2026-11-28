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
import os
import subprocess
import tempfile
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
GITHUB_TRANSPORT_MARKERS = (
    "github mutation transport",
    "github mutation approval",
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
    # Set when nothing can be repaired here and only the operator can fix it
    # (for example a GitHub project whose token variable is not set). The
    # text is the exact fix; it never contains a secret.
    operator_fix: str | None = None
    # Shown to the operator when a command fails (the check ran and failed).
    failure_fix: str | None = None
    # Run in a fresh empty directory instead of the builder's worktree.
    neutral_cwd: bool = False

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


def github_transport_plan(
    github_profile: Any = None,
    *,
    project: str = "",
    environ: Mapping[str, str] | None = None,
) -> RemediationPlan:
    """Check the project's own GitHub token; never the GitHub CLI.

    Harbor never reads the operator's ``gh`` login, and ``gh`` hangs inside
    Claude Code's macOS sandbox, so the only repair is the project's token
    (``github_token_env``, else ``GITHUB_TOKEN``/``GH_TOKEN``). Missing: the
    operator must set it. Present: one read-only REST request through Harbor's
    helper proves GitHub accepts it, and the builder resumes with the helper.
    Only variable *names* ever appear in the command; the value stays in the
    environment the helper inherits.
    """

    from harbor.github_rest import helper_command, helper_invocation, rest_base_url
    from harbor.github_token import missing_token_help, project_token_env

    env = os.environ if environ is None else environ
    label = f" for project {project}" if project else ""
    fix = f"no GitHub token{label}: {missing_token_help(github_profile)}"
    name = project_token_env(github_profile, env)
    if name is None:
        return RemediationPlan(kind="github_transport", commands=(), reason=fix, operator_fix=fix)
    repository = str(getattr(github_profile, "repository", "") or "")
    if repository.count("/") != 1 or not all(repository.split("/")):
        # The helper acts only on the configured repository, so there is
        # nothing to check, and nothing a builder could do on GitHub either.
        fix = f"no github_repository is configured{label}, so builders have no GitHub API access; set github_repository"
        return RemediationPlan(kind="github_transport", commands=(), reason=fix, operator_fix=fix)
    path = f"/repos/{repository}"
    try:
        api = rest_base_url(getattr(github_profile, "github_api_url", None))
    except ValueError as exc:
        fix = f"the GitHub API URL{label} is not usable ({exc}); fix github_api_url"
        return RemediationPlan(kind="github_transport", commands=(), reason=fix, operator_fix=fix)
    # The helper's api command acts only on HARBOR_GITHUB_REPOSITORY (#61).
    command = ("env", f"HARBOR_GITHUB_TOKEN_ENV={name}", f"HARBOR_GITHUB_API_URL={api}",
               f"HARBOR_GITHUB_REPOSITORY={repository}", *helper_command(), "api", "GET", path)
    return RemediationPlan(
        kind="github_transport",
        commands=(command,),
        # Runs on the host with the token in its env: never in the worktree.
        neutral_cwd=True,
        reason=(
            f"The project's GitHub token ({name}) is set and GitHub accepts it. Use Harbor's REST helper "
            f"(`{helper_invocation()} <op>`) for every GitHub read and write, as your "
            "launch prompt describes; never the gh CLI, which hangs inside Claude Code's sandbox on macOS."
        ),
        failure_fix=(
            f"GitHub rejected the project's token in {name}{label}, or it cannot read "
            f"{repository}:{missing_token_help(github_profile)}"
        ),
    )


def remediation_plan(
    blocker: Any,
    *,
    github_profile: Any = None,
    project: str = "",
    environ: Mapping[str, str] | None = None,
) -> RemediationPlan | None:
    """Return the one safe playbook matching ``blocker`` or ``None``.

    ``Cannot find module`` is intentionally matched case-insensitively and
    maps to one package installation.  ADB startup is similarly deterministic
    and never broadens into device selection, APK installation, or retries.
    """

    text = blocker_text(blocker).casefold()
    kind = str(_value(blocker, "kind", "type", "category", default="")).casefold().replace("-", "_")
    if kind == "github_transport" or any(marker in text for marker in GITHUB_TRANSPORT_MARKERS):
        return github_transport_plan(github_profile, project=project, environ=environ)
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
    github_profile: Any = None,
    project: str = "",
) -> RemediationResult:
    """Execute a known plan and report whether every command succeeded."""

    plan = remediation_plan(blocker, github_profile=github_profile, project=project)
    if plan is None:
        return RemediationResult(None, attempted=False, fixed=False, error="no deterministic remediation")
    if plan.operator_fix is not None:
        # Nothing to run: only the operator can fix this.
        return RemediationResult(plan, attempted=True, fixed=False, error=plan.operator_fix)
    workdir = Path(cwd) if cwd is not None else None
    outputs: list[Any] = []
    neutral = tempfile.TemporaryDirectory(prefix="harbor-remediation-") if plan.neutral_cwd else None
    if neutral is not None:
        workdir = Path(neutral.name)
    try:
        for command in plan.commands:
            value = await _invoke_executor(executor, command, workdir)
            outputs.append(value)
            if not _successful(value):
                return RemediationResult(plan, attempted=True, fixed=False, outputs=tuple(outputs), error="remediation command failed")
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        return RemediationResult(plan, attempted=True, fixed=False, outputs=tuple(outputs), error=str(exc))
    finally:
        if neutral is not None:
            neutral.cleanup()
    return RemediationResult(plan, attempted=True, fixed=True, outputs=tuple(outputs))


remediate = execute_remediation
run_remediation = execute_remediation


__all__ = [
    "ADB_NOT_RUNNING_MARKERS",
    "GITHUB_TRANSPORT_MARKERS",
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
