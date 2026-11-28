"""Harbor's own macOS sandbox for the dependency installs it runs outside the builder sandbox.

An install runs code the worktree chooses: lifecycle scripts and whatever they
call, ``.pnpmfile.cjs``, ``.npmrc`` settings such as ``git=`` and
``node-options=``, packages fetched from a registry the worktree names. The
builder can edit all of it, and hashing inputs cannot establish that the code
which runs was reviewed. So Harbor does not try to decide whether the code is
safe. It runs every install it starts (at worktree creation and before each
builder turn) under a ``sandbox-exec`` profile generated per run, in which the
install's code cannot reach anything valuable, whatever it runs:

* **writes** only to the worktree (never its ``.git`` file or the repository's
  git directory), Harbor's private package caches, a private ``TMPDIR`` and the
  caches of a few known build tools;
* **reads** of credential locations (``~/.ssh``, ``~/.config/gh``, ``~/.harbor``,
  ``~/.claude*``, ``~/.codex``, keychains, cloud CLI config, ``~/.netrc``,
  ``~/.git-credentials``, ``~/.npmrc``), the operator's main checkout and
  Harbor's own code, ledger and virtualenv are denied;
* **IPC**: the profile starts from ``(deny default)``, so only a short list of
  system services can be looked up: no keychain (``securityd``), no
  LaunchServices (``open``), no Apple Events (``osascript``), no launchd job
  submission, no unix-domain sockets except DNS (no ssh agent, no Docker);
* **network**: TCP/UDP to any host, because registries need it. With
  credentials unreadable and Harbor's credentials removed from the
  environment, the worktree (which the builder already has) is all there is to
  send.

Package caches are Harbor's own (``~/.harbor/install-cache``), never the
operator's ``~/.npm`` or pnpm store: an install may write its cache, and a
cache the operator's own tools read later outside any sandbox would be a way
out.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Relative to $HOME. Read and write are both denied.
CREDENTIAL_PATHS = (
    ".ssh", ".gnupg", ".config/gh", ".config/git/credentials", ".git-credentials", ".netrc", ".npmrc", ".yarnrc",
    ".yarnrc.yml", ".pypirc", ".docker", ".kube", ".aws", ".azure", ".config/gcloud", ".config/op", ".harbor",
    ".codex", ".claude", ".claude.json", ".config/claude", ".anthropic", ".config/hub", ".gitconfig",
    ".config/git", "Library/Keychains", "Library/Application Support/Claude",
    "Library/Application Support/com.apple.TCC", "Library/Cookies",
)
# Prefix matches under $HOME (".claude*" covers .claude, .claude.json, .claude-mem, ...).
CREDENTIAL_PREFIXES = (".claude", ".codex", ".harbor")
# Build tools that cache downloads outside the package manager (node-gyp
# headers, browser binaries); writable so native builds and browser downloads work.
TOOL_CACHES = (
    "Library/Caches/node-gyp", "Library/Caches/ms-playwright", "Library/Caches/Cypress", "Library/Caches/electron",
    "Library/Caches/electron-builder", ".cache/node-gyp", ".cache/puppeteer", ".cache/ms-playwright",
)
# The only Mach services an install may look up: logging, notifications, user
# and DNS lookups, network configuration, and TLS trust evaluation.
MACH_SERVICES = (
    "com.apple.system.notification_center", "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership", "com.apple.system.logger", "com.apple.logd",
    "com.apple.diagnosticd", "com.apple.dnssd.service", "com.apple.SystemConfiguration.configd",
    "com.apple.SystemConfiguration.DNSConfiguration", "com.apple.trustd", "com.apple.trustd.agent",
    "com.apple.cfprefsd.daemon", "com.apple.cfprefsd.agent", "com.apple.FSEvents",
)
# Denied even if an imported system profile allows them: Apple Events, LaunchServices, the keychain.
DENIED_MACH_SERVICES = (
    "com.apple.coreservices.appleevents", "com.apple.CoreServices.coreservicesd",
    "com.apple.coreservices.launchservicesd", "com.apple.lsd.mapdb", "com.apple.lsd.modifydb",
    "com.apple.SecurityServer", "com.apple.securityd", "com.apple.security.agent",
    "com.apple.security.authhost", "com.apple.windowserver.active",
)
# Unix-domain sockets an install may connect to: DNS only.
UNIX_SOCKETS = ("/private/var/run/mDNSResponder",)


def available() -> bool:
    """Whether this host can run Harbor's install sandbox (macOS with ``sandbox-exec``)."""

    return sys.platform == "darwin" and os.access(SANDBOX_EXEC, os.X_OK)


def harbor_home() -> Path:
    return Path.home() / ".harbor"


def cache_root() -> Path:
    """Harbor's private package caches, shared by its sandboxed installs and nothing else."""

    return harbor_home() / "install-cache"


def cache_environment(root: Path | None = None) -> dict[str, str]:
    """Environment pointing every package manager at Harbor's private caches."""

    root = root or cache_root()
    return {
        "npm_config_cache": str(root / "npm"),
        "npm_config_store_dir": str(root / "pnpm-store"),
        "npm_config_devdir": str(root / "node-gyp"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "UV_CACHE_DIR": str(root / "uv"),
        "YARN_CACHE_FOLDER": str(root / "yarn"),
        "BUN_INSTALL_CACHE_DIR": str(root / "bun"),
        "PIP_CACHE_DIR": str(root / "pip"),
        "COREPACK_HOME": str(root / "corepack"),
    }


def _real(path: str | Path) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def _quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _subpaths(paths: Iterable[str | Path]) -> str:
    return " ".join(f"(subpath {_quote(_real(path))})" for path in dict.fromkeys(str(p) for p in paths if p))


@dataclass
class Profile:
    """One install's sandbox: the paths it may write and the ones it may not read."""

    worktree: Path
    tmpdir: Path
    home: Path
    git_dirs: tuple[Path, ...] = ()
    denied: tuple[Path, ...] = ()
    caches: tuple[Path, ...] = ()
    extra_writable: tuple[Path, ...] = field(default=())

    def render(self) -> str:
        home = _real(self.home)
        worktree = _real(self.worktree)
        writable = [worktree, self.tmpdir, *self.caches, *self.extra_writable,
                    *(Path(home) / tool for tool in TOOL_CACHES)]
        credentials = [Path(home) / name for name in CREDENTIAL_PATHS] + list(self.denied)
        prefixes = " ".join(f"(regex #\"^{_regex(home + '/' + prefix)}\")" for prefix in CREDENTIAL_PREFIXES)
        git = [Path(worktree) / ".git", *self.git_dirs]
        services = " ".join(f"(global-name {_quote(name)})" for name in MACH_SERVICES)
        sockets = " ".join(f"(remote unix-socket (path-literal {_quote(path)}))" for path in UNIX_SOCKETS)
        return "\n".join((
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process-fork process-exec*)",
            "(allow signal (target same-sandbox))",
            "(allow process-info* (target same-sandbox))",
            "(allow sysctl-read)",
            "(allow ipc-posix-shm* ipc-posix-sem*)",
            "(allow pseudo-tty)",
            f"(allow mach-lookup {services})",
            # TCP and UDP anywhere; unix-domain sockets only for DNS.
            "(allow network-outbound (remote ip))",
            "(allow network-bind network-inbound (local ip))",
            f"(allow network-outbound {sockets})",
            "(allow system-socket)",
            # Read everything except credentials, the operator's checkout and Harbor itself.
            "(allow file-read*)",
            f"(deny file-read* file-write* {_subpaths(credentials)} {prefixes})",
            # Writes only here. The worktree may sit inside ~/.harbor, so it is re-allowed after the deny.
            "(allow file-write-data (literal \"/dev/null\") (literal \"/dev/zero\") (literal \"/dev/dtracehelper\")"
            " (regex #\"^/dev/tty\") (regex #\"^/dev/fd/\"))",
            f"(allow file-read* file-write* {_subpaths(writable)})",
            # Never git metadata: no hooks, config or refs, from anywhere.
            f"(deny file-read* file-write* {_subpaths(git)})",
            # No Apple Events: `tell application "Terminal" to do script` would run outside.
            "(deny appleevent-send)",
            f"(deny mach-lookup {' '.join(f'(global-name {_quote(name)})' for name in DENIED_MACH_SERVICES)})",
        )) + "\n"


def _regex(text: str) -> str:
    out = []
    for char in text:
        out.append("\\" + char if char in ".^$*+?()[]{}|\\/" else char)
    return "".join(out).replace('"', '\\"')


def denied_paths(repo: str | Path | None, extra: Iterable[str | Path] = ()) -> tuple[Path, ...]:
    """The operator's checkout, Harbor's own code, virtualenv and ledger, and ``extra``."""

    import harbor

    paths: list[Path] = [Path(harbor.__file__).resolve().parent, Path(sys.prefix)]
    if repo:
        paths.append(Path(repo))
    ledger = os.environ.get("HARBOR_DB_PATH")
    if ledger:
        paths.append(Path(ledger).expanduser().parent)
    paths.extend(Path(item) for item in extra)
    return tuple(paths)


@dataclass
class MacSandbox:
    """Runs a command under a per-run ``sandbox-exec`` profile (see the module docstring)."""

    repo: Path | None = None
    caches: Path | None = None
    name: str = "macOS sandbox-exec"

    def prepare(self, command: Sequence[str], worktree: Path, env: Mapping[str, str],
                git_dirs: Sequence[Path] = ()) -> tuple[list[str], dict[str, str], "Cleanup"]:
        cache = self.caches or cache_root()
        environment = dict(env)
        environment.update(cache_environment(cache))
        for key in ("npm_config_cache", "npm_config_store_dir", "npm_config_devdir", "XDG_CACHE_HOME", "UV_CACHE_DIR",
                    "YARN_CACHE_FOLDER", "BUN_INSTALL_CACHE_DIR", "PIP_CACHE_DIR", "COREPACK_HOME"):
            Path(environment[key]).mkdir(parents=True, exist_ok=True)
        # ~/.npmrc is unreadable in the sandbox (it may hold registry tokens): point
        # npm and pnpm at an empty user config instead of letting them fail on it.
        userconfig = cache / "empty-npmrc"
        userconfig.touch(exist_ok=True)
        environment["npm_config_userconfig"] = str(userconfig)
        tmp = Path(tempfile.mkdtemp(prefix="harbor-install-"))
        environment["TMPDIR"] = str(tmp) + "/"
        home = Path(env.get("HOME") or Path.home())
        profile = Profile(worktree=worktree, tmpdir=tmp, home=home, git_dirs=tuple(git_dirs),
                          denied=denied_paths(self.repo), caches=(cache,))
        path = tmp.parent / (tmp.name + ".sb")
        path.write_text(profile.render(), encoding="utf-8")
        return [SANDBOX_EXEC, "-f", str(path), *command], environment, Cleanup((tmp, path))


@dataclass
class Cleanup:
    paths: tuple[Path, ...]

    def __call__(self) -> None:
        for path in self.paths:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)


def default_isolation(repo: str | Path | None = None) -> MacSandbox | None:
    """Harbor's install sandbox on this host, or None where there is none (Linux)."""

    return MacSandbox(repo=Path(repo) if repo else None) if available() else None


__all__ = ["CREDENTIAL_PATHS", "MacSandbox", "Profile", "available", "cache_environment", "cache_root",
           "default_isolation", "denied_paths"]
