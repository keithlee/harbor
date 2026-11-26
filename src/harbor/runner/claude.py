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


DEFAULT_SANDBOX_DOMAINS = ("github.com", "api.github.com", "api.linear.app", "registry.npmjs.org")
_HOST_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
# At least two labels, and a top-level label with a letter, so IP literals are refused.
_HOST = re.compile(rf"(?:{_HOST_LABEL}\.)+(?=[0-9-]*[a-z]){_HOST_LABEL}")
# A last label that is a number makes the whole host an IPv4 address to URL
# parsers and inet_aton: decimal, or hex such as 0x7f.0x0.0x0.0x1 (127.0.0.1).
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")


def _domain(host: str) -> str:
    if not _HOST.fullmatch(host) or _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
        raise ValueError(f"not a plain host name: {host!r}")
    return host


def sandbox_hosts(url: str, label: str = "GitHub API", *, git_host: bool = True) -> tuple[str, ...]:
    """Hosts the builder's Bash sandbox must reach for one configured endpoint.

    Only the host of an ``https`` URL is taken: never a path, wildcard, user
    info or IP literal. With ``git_host``, ``api.<host>`` also yields ``<host>``, where GitHub
    serves git (``api.github.com`` -> ``github.com``, GHE.com likewise); GitHub
    Enterprise Server serves its API and git from one host. Anything malformed
    raises, so a bad value fails closed instead of widening the allowlist.
    """

    text = str(url or "")
    match = re.fullmatch(r"https://([^/?#\s@\[\]]+?)(?::[0-9]{1,5})?(?:[/?#][^\s]*)?", text)
    if not match:
        raise ValueError(f"the {label} URL must be an https URL with a plain host name")
    try:
        host = _domain(match.group(1).lower())
    except ValueError as exc:
        raise ValueError(f"the {label} URL must be an https URL with a plain host name") from exc
    hosts = [host]
    if git_host and host.startswith("api.") and host.count(".") >= 2:
        hosts.append(host[len("api."):])
    return tuple(hosts)


def sandbox_domains(*extra: str) -> list[str]:
    """Harbor's default allowlist plus the project's configured hosts, deduplicated."""

    return list(dict.fromkeys((*DEFAULT_SANDBOX_DOMAINS, *(_domain(host) for host in extra))))


# Wildcards would widen a rule past the one path; a line break would end it.
_RULE_UNSAFE = "*?[]\n\r"

# Builders run plain ``harbor``: the shim ``harbor start`` puts first on PATH
# (see ``harbor.builder_env``). One fixed rule, the same for every project.
HARBOR_BASH_RULE = "Bash(harbor:*)"


def workflow_permissions(package: str | Path | None, *, harbor: bool = True) -> list[str]:
    """Pre-approvals a Claude builder needs to follow the delivery workflow under ``dontAsk``.

    ``harbor`` approves the fixed ``Bash(harbor:*)`` rule. Read, Glob and Grep
    are limited to the working directory, and the workflow package lives
    outside the ticket worktree, so it is approved read-only. Both still run
    inside the OS sandbox: nothing becomes writable or reachable that was not
    already. A path a rule cannot express raises instead of being widened.
    """

    allow: list[str] = [HARBOR_BASH_RULE] if harbor else []
    if package is not None:
        resolved = Path(package).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"the workflow package {resolved} is not a directory")
        if any(char in str(resolved) for char in _RULE_UNSAFE):
            raise ValueError(f"the workflow package path {resolved} contains a character a permission rule cannot express")
        # A leading ``//`` anchors the rule at the filesystem root, not the settings file.
        allow.append(f"Read(/{resolved}/**)")
    return allow


class ClaudeRunner(CodexRunner):
    """Reuse Harbor's retained lifecycle with Claude's native CLI protocol."""

    provider = "claude"
    # Extra hosts derived from the project's configured endpoints (see sandbox_hosts).
    sandbox_hosts: tuple[str, ...] = ()
    # Approve plain ``harbor`` (Harbor wires this for every production builder).
    allow_harbor: bool = False
    # The project's workflow package, which lives outside the ticket worktree.
    workflow_package: Path | None = None

    def _workflow_permissions(self) -> list[str]:
        try:
            return workflow_permissions(self.workflow_package, harbor=self.allow_harbor)
        except ValueError as exc:
            raise RunnerError(f"refusing a builder permission: {exc}") from exc

    def command(self, instruction: str, *, run_id: str, resume: bool = False) -> list[str]:
        del instruction
        try:
            domains = sandbox_domains(*self.sandbox_hosts)
        except ValueError as exc:
            raise RunnerError(f"refusing a builder sandbox host: {exc}") from exc
        settings = {
            "sandbox": {
                "enabled": True,
                "failIfUnavailable": True,
                "allowUnsandboxedCommands": False,
                "autoAllowBashIfSandboxed": True,
                "filesystem": {"allowWrite": list(self.writable_roots)},
                "network": {"allowedDomains": domains},
            },
        }
        allow = self._workflow_permissions()
        mcp = github_mcp_config(getattr(self, "github_mcp", None))
        if mcp:
            # Pre-approve only the GitHub server's tools; dontAsk refuses everything else.
            allow.append("mcp__github")
        if allow:
            settings["permissions"] = {"allow": allow}
        if attribution_off():
            # Empty strings hide the commit trailer and the PR text; they supersede the deprecated includeCoAuthoredBy.
            settings["attribution"] = {"commit": "", "pr": ""}
        schema = json.loads(Path(self.schema_path).read_text(encoding="utf-8"))
        # Claude's validator supplies its own draft. Harbor's result schema
        # uses the shared subset of keywords from both drafts.
        schema.pop("$schema", None)
        schema.pop("$id", None)
        # Agent lets the delivery skill spawn its planner/implementor/reviewer children. They
        # run in this process, so they get the same tools, dontAsk mode and sandbox as the builder.
        command = [
            self.executable, "--print", "--verbose", "--output-format", "stream-json",
            "--json-schema", json.dumps(schema),
            "--model", self.model, "--effort", self.reasoning_effort,
            "--permission-mode", "dontAsk", "--tools", "Bash,Read,Glob,Grep,Skill,Agent",
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
