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
    attribution_off()  # fail at startup, not on the first dispatch, when HARBOR_CLAUDE_ATTRIBUTION is invalid


def claude_read_only(executable: str, model: str, effort: str | None) -> list[str]:
    command = [
        executable, "--print", "--verbose", "--output-format", "stream-json", "--model", model,
        "--no-session-persistence", "--tools", "", "--strict-mcp-config",
        "--setting-sources", "", "--disable-slash-commands",
    ]
    if effort:
        command.extend(("--effort", effort))
    return command


def github_mcp_config(github_mcp: dict | None) -> dict | None:
    """The opt-in GitHub MCP server, or None for the default REST transport.

    The header holds a ``${VAR}`` reference that Claude Code expands from its
    own environment, so the token itself is never written into the command.
    """

    if not github_mcp:
        return None
    url, token_env = str(github_mcp.get("url") or ""), str(github_mcp.get("token_env") or "")
    if not url.startswith("https://") or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise RunnerError("GitHub MCP needs an https URL and a token environment variable name")
    return {"mcpServers": {"github": {
        "type": "http", "url": url, "headers": {"Authorization": f"Bearer ${{{token_env}}}"},
    }}}


ATTRIBUTION_ENV = "HARBOR_CLAUDE_ATTRIBUTION"


def attribution_off() -> bool:
    """True when the operator opted out of Claude's commit and PR attribution (``HARBOR_CLAUDE_ATTRIBUTION=off``)."""
    value = os.environ.get(ATTRIBUTION_ENV, "").strip().lower()
    if value == "off":
        return True
    if value in ("", "on"):
        return False
    raise ValueError(f"{ATTRIBUTION_ENV} must be 'off', 'on', or unset; got {value!r}")


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
        mcp = github_mcp_config(getattr(self, "github_mcp", None))
        if mcp:
            # Pre-approve only the GitHub server's tools; dontAsk refuses everything else.
            settings["permissions"] = {"allow": ["mcp__github"]}
        if attribution_off():
            # Empty strings hide the commit trailer and the PR text; they supersede the deprecated includeCoAuthoredBy.
            settings["attribution"] = {"commit": "", "pr": ""}
        schema = json.loads(Path(self.schema_path).read_text(encoding="utf-8"))
        # Claude's validator supplies its own draft. Harbor's result schema
        # uses the shared subset of keywords from both drafts.
        schema.pop("$schema", None)
        schema.pop("$id", None)
        command = [
            self.executable, "--print", "--verbose", "--output-format", "stream-json",
            "--json-schema", json.dumps(schema),
            "--model", self.model, "--effort", self.reasoning_effort,
            "--permission-mode", "dontAsk", "--tools", "Bash,Read,Glob,Grep,Skill",
            "--setting-sources", "", "--strict-mcp-config", "--settings", json.dumps(settings),
        ]
        if mcp:
            command.extend(("--mcp-config", json.dumps(mcp)))
        if resume:
            try:
                session_id = str(UUID(run_id))
            except ValueError as exc:
                raise RunnerError("Claude Code resume requires a retained native session ID") from exc
            command.extend(("--resume", session_id))
        return command
