"""Native headless Claude Code invocations."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from uuid import UUID

from harbor.runner.base import RunnerError
from harbor.runner.codex import CodexRunner


def validate_claude(executable: str, model: str, effort: str | None) -> None:
    if shutil.which(executable) is None:
        raise ValueError("Claude Code executable unavailable")
    if not model.strip() or model.startswith("gpt-"):
        raise ValueError("configure a Claude Code model explicitly")
    if effort not in {None, "low", "medium", "high", "xhigh", "max"}:
        raise ValueError("unsupported Claude Code reasoning effort")


def claude_read_only(executable: str, model: str, effort: str | None) -> list[str]:
    command = [
        executable, "--print", "--verbose", "--output-format", "stream-json", "--model", model,
        "--no-session-persistence", "--tools", "", "--strict-mcp-config",
        "--setting-sources", "", "--disable-slash-commands",
    ]
    if effort:
        command.extend(("--effort", effort))
    return command


SANDBOX_ENV = "HARBOR_CLAUDE_SANDBOX"
ALLOW_BASH_ENV = "HARBOR_CLAUDE_ALLOW_BASH"
ATTRIBUTION_ENV = "HARBOR_CLAUDE_ATTRIBUTION"


def attribution_off() -> bool:
    """True when the operator opted out of Claude's commit and PR attribution (``HARBOR_CLAUDE_ATTRIBUTION=off``)."""
    value = os.environ.get(ATTRIBUTION_ENV, "").strip().lower()
    if value == "off":
        return True
    if value in ("", "on"):
        return False
    raise ValueError(f"{ATTRIBUTION_ENV} must be 'off' or unset; got {value!r}")


def sandbox_off() -> bool:
    """True only when the operator explicitly opted out of Claude's OS sandbox (``HARBOR_CLAUDE_SANDBOX=off``)."""
    value = os.environ.get(SANDBOX_ENV, "").strip().lower()
    if value in ("", "on"):
        return False
    if value == "off":
        return True
    raise ValueError(f"{SANDBOX_ENV} must be 'off' or unset; got {value!r}")


def bash_allowlist() -> list[str]:
    """Bash command prefixes allowed when the sandbox is off (``HARBOR_CLAUDE_ALLOW_BASH``, comma-separated).

    Without the OS sandbox the only guard left is this list, so it is mandatory and strict: each
    entry is a literal command prefix, with no shell syntax or rule wildcards.
    """
    prefixes = [item.strip() for item in os.environ.get(ALLOW_BASH_ENV, "").split(",") if item.strip()]
    for prefix in prefixes:
        if not re.fullmatch(r"[A-Za-z0-9._/= +-]+", prefix):
            raise ValueError(f"{ALLOW_BASH_ENV} entries must be plain command prefixes; got {prefix!r}")
    if not prefixes:
        raise ValueError(f"{ALLOW_BASH_ENV} must list at least one command prefix when {SANDBOX_ENV}=off")
    return prefixes


class ClaudeRunner(CodexRunner):
    """Reuse Harbor's retained lifecycle with Claude's native CLI protocol."""

    provider = "claude"

    def command(self, instruction: str, *, run_id: str, resume: bool = False) -> list[str]:
        del instruction
        settings = {
            "sandbox": {
                "enabled": True,
                "failIfUnavailable": True,
                "allowUnsandboxedCommands": False,
                "autoAllowBashIfSandboxed": True,
                "filesystem": {"allowWrite": list(self.writable_roots)},
                "network": {"allowedDomains": ["github.com", "api.github.com", "api.linear.app", "registry.npmjs.org"]},
            },
        }
        allowed = bash_allowlist() if sandbox_off() else None
        if allowed:
            # No OS sandbox: Bash is limited to the listed command prefixes; everything else is denied.
            # File edits are limited to the working directory (the retained worktree) by the Edit rule.
            settings = {"sandbox": {"enabled": False},
                        "permissions": {"allow": [*(f"Bash({item}:*)" for item in allowed), "Edit(./**)"]}}
        if attribution_off():
            settings["attribution"] = {"commit": "", "pr": ""}
            settings["includeCoAuthoredBy"] = False
        schema = json.loads(Path(self.schema_path).read_text(encoding="utf-8"))
        # Claude's validator supplies its own draft. Harbor's result schema
        # uses the shared subset of keywords from both drafts.
        schema.pop("$schema", None)
        schema.pop("$id", None)
        command = [
            self.executable, "--print", "--verbose", "--output-format", "stream-json",
            "--json-schema", json.dumps(schema),
            "--model", self.model, "--effort", self.reasoning_effort,
            "--permission-mode", "dontAsk", "--tools", "Bash,Read,Glob,Grep,Skill" + (",Edit,Write" if allowed else ""),
            "--setting-sources", "", "--strict-mcp-config", "--settings", json.dumps(settings),
        ]
        if allowed:
            command.extend(("--append-system-prompt", (
                "There is no OS sandbox. Bash commands must start with one of these prefixes, and every part of a "
                f"compound command must match one: {'; '.join(allowed)}. Anything else is denied, so run each tool "
                "as the first word of its own Bash call.")))
        if resume:
            try:
                session_id = str(UUID(run_id))
            except ValueError as exc:
                raise RunnerError("Claude Code resume requires a retained native session ID") from exc
            command.extend(("--resume", session_id))
        return command
