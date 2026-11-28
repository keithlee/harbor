"""Native headless Claude Code invocations."""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path
from typing import Any
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


# Opt-in device and browser proof (``[builder] device_proof``). Each platform
# adds only the sandbox keys its tools were seen to need on a live builder.
# None of them is harmless: each gives Bash a path out of the sandbox (see
# DEVICE_PROOF_GRANTS and docs/configurable-workflows.md). Keys are merged, so
# "android" plus "web" still asks for local binding once.
def ios_mach_services(machine: str | None = None) -> tuple[str, ...]:
    """The exact Mach services simctl list, install, launch and io screenshot need on a booted simulator.

    Found live in a sandboxed Claude builder: with CoreSimulatorService alone a
    booted device shows "liblaunch_sim.dylib could not be opened" and simctl
    says "No devices are booted"; adding the host-architecture SimLaunchHost made
    install, launch and screenshot work. simdiskimaged, SimulatorTrampoline,
    SimRenderingServices and SimAudioProcessorServices were not needed. Booting
    is not granted: Harbor boots the simulator outside the sandbox
    (harbor.device_boot). Only the arm64 name was verified live.
    """

    machine = platform.machine() if machine is None else machine
    host = "x86" if machine in {"x86_64", "i386"} else "arm64"
    return ("com.apple.CoreSimulator.CoreSimulatorService", f"com.apple.CoreSimulator.SimLaunchHost-{host}")


IOS_MACH_SERVICES = ios_mach_services()
DEVICE_PROOF_RULES: dict[str, dict[str, object]] = {
    # The adb client connects to the adb server on 127.0.0.1:5037, or starts it there.
    "android": {"allowLocalBinding": True},
    # simctl asks CoreSimulatorService, which runs outside the sandbox, to act on a booted simulator.
    "ios": {"allowMachLookup": list(IOS_MACH_SERVICES)},
    # Metro, Expo, Next and http.server bind a local port, and a client such as curl connects to it.
    "web": {"allowLocalBinding": True},
}
DEVICE_PROOF_GRANTS = {
    "allowLocalBinding": (
        "network.allowLocalBinding: Bash commands may listen on any port and connect to every local service, "
        "including an adb server (adb shell reaches any host; adb push/pull moves files), Chrome debugging "
        "ports and a Docker TCP socket; this is a path out of the sandbox"
    ),
    "allowMachLookup": (
        f"network.allowMachLookup {', '.join(IOS_MACH_SERVICES)}: Bash commands may drive CoreSimulatorService, "
        "which runs outside the sandbox (simctl openurl reaches any host; simctl spawn runs a binary with host "
        "file access); this is a sandbox escape"
    ),
}


def device_proof_network(platforms: tuple[str, ...] | list[str]) -> dict[str, object]:
    """The sandbox ``network`` keys the enabled device-proof platforms add, or ``{}``.

    An unknown platform raises, so a typo never silently grants nothing or
    something else.
    """

    network: dict[str, object] = {}
    for platform in platforms:
        if platform not in DEVICE_PROOF_RULES:
            raise ValueError(f"unknown device_proof platform {platform!r}; use any of {', '.join(DEVICE_PROOF_RULES)}")
        for key, value in DEVICE_PROOF_RULES[platform].items():
            if isinstance(value, list):
                existing = list(network.get(key) or [])
                network[key] = existing + [item for item in value if item not in existing]
            else:
                network[key] = value
    return network


def device_proof_report(platforms: tuple[str, ...] | list[str], system: str | None = None) -> list[str]:
    """Human-readable lines for ``harbor doctor``: what each enabled platform grants, and where it applies."""

    if not platforms:
        return ["Device proof: off (builder sandbox has no local ports and no simulator access; set [builder] device_proof to opt in)"]
    network = device_proof_network(platforms)
    lines = [f"Device proof: {', '.join(platforms)}"]
    lines.extend(f"  grants {DEVICE_PROOF_GRANTS[key]}" for key in network)
    system = sys.platform if system is None else system
    if system != "darwin":
        lines.append(
            "  not effective on this host: Claude Code documents network.allowLocalBinding and "
            "network.allowMachLookup as macOS-only, so on "
            f"{'Linux' if system.startswith('linux') else system} the builder cannot rely on them"
            + (", and there is no iOS Simulator" if "ios" in platforms else "")
            + "; Harbor has verified device proof on macOS only"
        )
    return lines


# Wildcards would widen a rule past the one path; a line break would end it.
_RULE_UNSAFE = "*?[]\n\r"


def workflow_permissions(invocation: str, package: str | Path | None, helper: str = "") -> list[str]:
    """Pre-approvals a Claude builder needs to follow the delivery workflow under ``dontAsk``.

    Sandboxed Bash is auto-allowed, except a command that sets ``PYTHONPATH``
    or assigns a variable in front of an absolute executable path (directly or
    through ``/usr/bin/env``), which is exactly what Harbor's runtime
    invocation and its GitHub helper invocation
    (harbor.github_rest.builder_helper_invocation) do, so those two prefixes
    are approved literally. Read, Glob and
    Grep are limited to the working directory, and the workflow package lives
    outside the ticket worktree, so it is approved read-only. Both still run
    inside the OS sandbox: nothing becomes writable or reachable that was not
    already. A value a rule cannot express raises instead of being widened.
    """

    allow: list[str] = []
    invocation = invocation.strip()
    if invocation:
        if any(char in invocation for char in _RULE_UNSAFE):
            raise ValueError("the Harbor runtime invocation contains a character a permission rule cannot express")
        allow.append(f"Bash({invocation}:*)")
    helper = helper.strip()
    if helper:
        if any(char in helper for char in _RULE_UNSAFE):
            raise ValueError("the GitHub helper invocation contains a character a permission rule cannot express")
        allow.append(f"Bash({helper}:*)")
    if package is not None:
        resolved = Path(package).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"the workflow package {resolved} is not a directory")
        if any(char in str(resolved) for char in _RULE_UNSAFE):
            raise ValueError(f"the workflow package path {resolved} contains a character a permission rule cannot express")
        # A leading ``//`` anchors the rule at the filesystem root, not the settings file.
        allow.append(f"Read(/{resolved}/**)")
    return allow


def builder_shell_note(helper: str = "") -> str:
    """What a dontAsk Claude builder's Bash may and may not run, appended to every turn.

    Found live with ``ClaudeRunner.command`` (Claude Code 2.1.332): sandboxed
    Bash is auto-allowed, but every command that sets an environment variable
    is refused with "Claude Code is running in don't ask mode", and an allow
    rule does not help ``export``. Refused: ``FOO=1 cmd``, ``env FOO=1 cmd``,
    ``/usr/bin/env FOO=1 cmd``, ``cd sub && FOO=1 npm test``, ``export FOO=1``
    alone or chained, ``set -a; . ./.env.local``, ``sh -c 'FOO=1 cmd'``, and
    even ``node -e "console.log('FOO=' + x)"``. ``Bash(export:*)`` does not
    make ``export`` pass; ``Bash(env:*)`` would make ``env FOO=1 cmd`` pass but
    approves every command behind ``env``, so Harbor does not add it.
    Allowed: a variable read from a file in the worktree (``node
    --env-file=.env.local``, or a framework's own ``.env.local``), which the
    Write tool may create there. A resumed session never sees the launch
    prompt again, so the exact GitHub helper command is repeated here.
    """

    lines = [
        "Harbor shell note (Claude Code in dontAsk mode): sandboxed Bash runs without asking, but Claude Code "
        "refuses every command that sets an environment variable, and a refused form never works on retry. Refused: "
        "`FOO=1 cmd`, `env FOO=1 cmd`, `/usr/bin/env FOO=1 cmd`, `cd dir && FOO=1 cmd`, `export FOO=1` (alone or "
        "chained), `set -a; . ./.env.local` and `sh -c 'FOO=1 cmd'`; the only exceptions are Harbor's runtime "
        "invocation and GitHub helper command, run exactly as given. To give your own build, test or dev-server command a variable, write it to an "
        "env file in the ticket worktree with the Write tool and let the tool read it: `.env.local` (read by Next.js, "
        "Vite and dotenv; Next.js also reads `.env.development.local`, `.env.production.local` and `.env.test.local`), "
        "or `node --env-file=.env.local script.js` for plain Node. Text that looks like an assignment inside inline "
        "code is refused as well (`node -e \"console.log('FOO=' + x)\"`), so put such code in a script file. "
        "Use placeholder or scratch values only (for example a scratch database path inside the worktree), never "
        "a real secret, and never commit the file: "
        "check `git status` before each commit. If a command needs a variable and reads no env file, report a "
        "blocker that names the variable instead of working around the refusal.",
    ]
    if helper:
        lines.append(
            f"GitHub helper: run it exactly as `{helper} <op>`; that exact prefix is pre-approved, and its "
            "configuration is already in it. Do not add `/usr/bin/env`, `PYTHONPATH`, `python -m` or any variable in "
            "front of it, and do not change its interpreter or path, or Claude Code refuses it. The token is already "
            "in your environment under the variable that HARBOR_GITHUB_TOKEN_ENV names; never print or echo it."
        )
    return "\n".join(lines)


# The builder's file tools. Edit and Write run inside Claude Code, outside the
# OS sandbox, so the ``Edit(...)`` rule below (which covers every file-editing
# tool) is their only boundary; dontAsk refuses an edit anywhere else.
# TaskStop stops a Bash command the builder started with run_in_background (a dev
# server for web proof). Without it nothing can: pkill and lsof cannot see that
# process from a later sandboxed command, and a ``claude -p`` session with a
# background command still running does not end until it exits (live: about ten
# minutes). It acts only on this session's own background tasks.
BUILDER_TOOLS = "Bash,Read,Glob,Grep,Edit,Write,Skill,Agent,TaskStop"


def worktree_permissions(worktree: str | Path) -> list[str]:
    """Pre-approve the Edit and Write tools for the ticket worktree, and nowhere else.

    Under ``dontAsk`` an edit is refused unless a rule allows it, and so is a
    Bash command whose output redirection or ``mkdir`` target is a path no rule
    allows (live: ``mkdir -p tests && printf ... > tests/x.py`` was refused).
    The worktree is the builder's cwd and is already writable to sandboxed
    Bash, so this rule makes nothing writable that was not before.
    """

    resolved = Path(worktree).expanduser().resolve()
    if any(char in str(resolved) for char in _RULE_UNSAFE):
        raise ValueError(f"the worktree path {resolved} contains a character a permission rule cannot express")
    # A leading ``//`` anchors the rule at the filesystem root, not the settings file.
    return [f"Edit(/{resolved}/**)"]


def tmp_write_paths(system: str | None = None) -> list[str]:
    """``[builder] allow_tmp``: the shared temp directory, under both names on macOS (``/tmp`` -> ``/private/tmp``)."""

    system = sys.platform if system is None else system
    return ["/tmp", "/private/tmp"] if system == "darwin" else ["/tmp"]


class ClaudeRunner(CodexRunner):
    """Reuse Harbor's retained lifecycle with Claude's native CLI protocol."""

    provider = "claude"
    # Extra hosts derived from the project's configured endpoints (see sandbox_hosts).
    sandbox_hosts: tuple[str, ...] = ()
    # The exact ``harbor`` runtime prefix the builder prompt tells it to use.
    harbor_invocation: str = ""
    # The exact GitHub REST helper invocation (harbor.github_rest.builder_helper_invocation).
    github_helper_invocation: str = ""
    # The project's workflow package, which lives outside the ticket worktree.
    workflow_package: Path | None = None
    # Opt-in device/browser proof platforms from the project's workflow ([builder] device_proof).
    device_proof: tuple[str, ...] = ()
    # Boots the mobile devices outside the sandbox before each launch (harbor.device_boot.prepare_devices).
    device_preparer: Any = None
    # Opt-in extra network hosts for the Bash sandbox ([builder] allowed_domains).
    allowed_domains: tuple[str, ...] = ()
    # Opt-in: let Bash write the host's shared /tmp ([builder] allow_tmp).
    allow_tmp: bool = False

    async def _execute(self, context, instruction, *, resume, before_spawn=None):
        if self.device_proof:
            from harbor.device_boot import device_note
            statuses = []
            if self.device_preparer is not None and {"android", "ios"} & set(self.device_proof):
                # A boot can take minutes; keep the supervisor's event loop free meanwhile.
                statuses = await asyncio.to_thread(self.device_preparer, self.device_proof)
            for status in statuses:
                print(f"Harbor: device proof for {self.ticket or context.run_id}: {status.line()[2:]}", file=sys.stderr)
            note = device_note(statuses, web="web" in self.device_proof)
            if note:
                instruction = f"{instruction}\n\n{note}" if instruction else note
        # Every turn, fresh or resumed: a resumed session never sees the launch prompt again.
        shell = builder_shell_note(self.github_helper_invocation)
        instruction = f"{instruction}\n\n{shell}" if instruction else shell
        return await super()._execute(context, instruction, resume=resume, before_spawn=before_spawn)

    def _workflow_permissions(self) -> list[str]:
        try:
            return [*worktree_permissions(self.worktree),
                    *workflow_permissions(self.harbor_invocation, self.workflow_package,
                                          self.github_helper_invocation)]
        except ValueError as exc:
            raise RunnerError(f"refusing a builder permission: {exc}") from exc

    def command(self, instruction: str, *, run_id: str, resume: bool = False) -> list[str]:
        del instruction
        try:
            domains = sandbox_domains(*self.sandbox_hosts, *self.allowed_domains)
        except ValueError as exc:
            raise RunnerError(f"refusing a builder sandbox host: {exc}") from exc
        writable = list(self.writable_roots)
        if self.allow_tmp:
            writable.extend(path for path in tmp_write_paths() if path not in writable)
        try:
            device_network = device_proof_network(self.device_proof)
        except ValueError as exc:
            raise RunnerError(f"refusing builder device proof: {exc}") from exc
        settings = {
            "sandbox": {
                "enabled": True,
                "failIfUnavailable": True,
                "allowUnsandboxedCommands": False,
                "autoAllowBashIfSandboxed": True,
                "filesystem": {"allowWrite": writable},
                "network": {"allowedDomains": domains, **device_network},
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
        # run in this process, so they get the same tools, permission rules, dontAsk mode and
        # sandbox as the builder.
        command = [
            self.executable, "--print", "--verbose", "--output-format", "stream-json",
            "--json-schema", json.dumps(schema),
            "--model", self.model, "--effort", self.reasoning_effort,
            "--permission-mode", "dontAsk", "--tools", BUILDER_TOOLS,
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
