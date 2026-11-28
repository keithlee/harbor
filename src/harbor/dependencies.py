"""Reinstall a worktree's dependencies outside the builder sandbox, before each turn.

A builder cannot reliably install dependencies itself. Claude Code's sandbox
refuses writes to, and deletion of, files it protects (``.gitmodules``,
``.idea/``, ``.vscode/`` and so on) even deep inside ``node_modules``, so a
``pnpm install`` run in the sandbox can delete ``node_modules`` and then fail
to write it back (HAN-94: ``ERR_PNPM_EPERM: reflink to
node_modules/eslint-plugin-react/node_modules/resolve_tmp_13868_2/.gitmodules,
Operation not permitted``). The worktree is then unrunnable until a person
reinstalls outside the sandbox.

So before every builder turn, once the launch is authorized, Harbor compares
the worktree's dependency inputs (``scan_inputs``) with what it recorded at its
last install, and checks that the install output is present. If either
changed, it re-runs the project's configured ``install_command``.

That install runs code the worktree chooses, and the builder can edit the
worktree. Harbor does not try to tell reviewed code from builder code. Every
install it runs goes through Harbor's own install sandbox
(``harbor.install_sandbox``), where that code cannot read credentials, write
outside the worktree and Harbor's private caches, touch git metadata, or reach
the keychain, LaunchServices or launchd. Harbor's credentials and the SSH
agent are also removed from its environment, the package manager is resolved
by absolute path from Harbor's own ``PATH``, and pnpm may not switch to the
package-manager version the worktree names. Where there is no install sandbox
(Linux), the pre-turn reinstall is refused with a blocker instead.

What Harbor recorded lives in its SQLite ledger, keyed by worktree path, never
in the worktree, where the builder could edit it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

# The blocker kind of a turn Harbor refused to start because its install failed
# or could not be contained. Rung-0 remediation never acts on it
# (harbor.facts.remediation): its evidence is install output the worktree controls.
BLOCKER_KIND = "dependency_install"

# Inputs, matched by file name (case-insensitively, as macOS resolves them) at
# any depth outside PRUNED directories. They decide when to reinstall; the
# install sandbox, not this list, is what contains the install's code.
INPUT_NAMES = frozenset(name.casefold() for name in (
    "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "package-lock.json", "npm-shrinkwrap.json",
    "yarn.lock", "bun.lock", "bun.lockb", "bunfig.toml",
    ".npmrc", ".pnpmfile.cjs", ".pnpmfile.mjs", ".yarnrc", ".yarnrc.yml",
    "uv.lock", "uv.toml", "pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "Pipfile", "Pipfile.lock",
    "Gemfile", "Gemfile.lock",
))
# Also inputs: every file under top-level patches/, and Yarn's patches, plugins and releases.
YARN_CODE_DIRS = frozenset({"patches", "plugins", "releases"})
# Install output and git metadata are never inputs, and are not walked.
PRUNED = frozenset({"node_modules", ".git", ".venv", ".pnpm-store"})

DEFAULT_TIMEOUT_SECONDS = 15 * 60.0
_KILL_GRACE_SECONDS = 10.0
_ERROR_CHARS = 2000
_COMMAND_KEY = "(install_command)"
SCRIPTS_SETTINGS = ("on", "off")

# Credentials Harbor itself holds. An install does not need them. A package
# registry token (NPM_TOKEN, NODE_AUTH_TOKEN) is the project's, and is kept.
HARBOR_SECRET_NAMES = frozenset({
    "GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GITHUB_PAT",
    "LINEAR_API_KEY", "HARBOR_LINEAR_API_KEY",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "CODEX_API_KEY",
})
_SECRET_PREFIXES = ("HARBOR_", "LINEAR_", "ANTHROPIC_", "OPENAI_", "CLAUDE_", "CODEX_", "GITHUB_", "GH_")
_SECRET_WORDS = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL")
# HARBOR_GITHUB_TOKEN_ENV names the variable that holds the GitHub token.
_TOKEN_POINTER = "HARBOR_GITHUB_TOKEN_ENV"
# Pinned for every install: no package-manager version switching (pnpm 10
# downloads and runs the version package.json names, from the registry the
# worktree's .npmrc names), no interactive purge prompt, no git config.
PINNED_ENV = {
    "npm_config_manage_package_manager_versions": "false",
    "npm_config_confirm_modules_purge": "false",
    "npm_config_update_notifier": "false",
    "npm_config_fund": "false",
    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    "COREPACK_ENABLE_STRICT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}


def install_environment(environ: Mapping[str, str] | None = None, *, extra_secrets: Iterable[str] = (),
                        worktree: str | Path | None = None, scripts: bool = True) -> dict[str, str]:
    """``environ`` (default: Harbor's own) prepared for an install.

    Removed: the names in ``HARBOR_SECRET_NAMES``; any Harbor, Linear, GitHub
    or model-provider variable whose name contains TOKEN, KEY, SECRET,
    PASSWORD or CREDENTIAL; the variable ``HARBOR_GITHUB_TOKEN_ENV`` names;
    ``extra_secrets``; every ``GIT_*`` variable; and ``SSH_AUTH_SOCK``. ``PATH``
    loses empty, relative and worktree entries (``harbor.safe_git.safe_path``).
    ``PINNED_ENV`` is set. With ``scripts=False`` npm, pnpm and Yarn are also
    told not to run scripts.
    """

    from harbor.safe_git import safe_path

    source = dict(os.environ if environ is None else environ)
    drop = set(HARBOR_SECRET_NAMES) | {str(name) for name in extra_secrets if name} | {"SSH_AUTH_SOCK"}
    pointer = source.get(_TOKEN_POINTER)
    if pointer:
        drop.add(pointer)
    for name in source:
        upper = name.upper()
        if (upper in drop or upper.startswith("GIT_")
                or (upper.startswith(_SECRET_PREFIXES) and _SECRET_WORDS.search(upper))):
            drop.add(name)
    env = {name: value for name, value in source.items() if name not in drop}
    outside = Path(worktree).expanduser().resolve() if worktree is not None else None
    env["PATH"] = safe_path(source.get("PATH", ""), outside=outside)
    env.update(PINNED_ENV)
    if not scripts:
        env.update(npm_config_ignore_scripts="true", YARN_ENABLE_SCRIPTS="0")
    return env


def normalize_command(value: Sequence[str] | str | None) -> tuple[str, ...]:
    """The configured install command as an argument list; empty means none."""

    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text or text.casefold() in {"none", "false", "off", "skip"}:
            return ()
        return tuple(shlex.split(text))
    return tuple(str(item) for item in value)


def _program(command: Sequence[str]) -> tuple[str, int]:
    """The package manager a command runs, and its index (``corepack pnpm install`` -> pnpm, 1)."""

    if not command:
        return "", 0
    index = 1 if Path(command[0]).name == "corepack" and len(command) > 1 else 0
    return Path(command[index]).name, index


def resolve_command(command: Sequence[str], worktree: str | Path, environ: Mapping[str, str] | None = None
                    ) -> tuple[tuple[str, ...] | None, str]:
    """``command`` with its executable as an absolute path from Harbor's own ``PATH``, or ``(None, why)``.

    Empty, relative and worktree ``PATH`` entries are ignored, so a ``pnpm``
    the builder planted in ``node_modules/.bin`` or the worktree root is never
    the one that runs. A configured absolute path inside the worktree is refused.
    """

    from harbor.safe_git import safe_path

    if not command:
        return None, "the install command is empty"
    root = Path(worktree).expanduser().resolve()
    name = command[0]
    if os.path.isabs(name):
        found = name
    else:
        path = safe_path((os.environ if environ is None else environ).get("PATH", ""), outside=root)
        found = shutil.which(name, path=path) or ""
    if not found:
        return None, f"`{name}` is not on Harbor's PATH (outside the worktree)"
    real = Path(found).resolve()
    if real == root or root in real.parents:
        return None, f"`{found}` is inside the worktree"
    return (found, *command[1:]), ""


def _is_input(path: str, extra: frozenset[str] = frozenset()) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or any(part in PRUNED for part in parts):
        return False
    if path in extra:
        return True
    name = parts[-1].casefold()
    if name in INPUT_NAMES or (name.startswith("requirements") and name.endswith(".txt")):
        return True
    first = parts[0].casefold()
    if first == "patches":
        return len(parts) > 1
    return first == ".yarn" and len(parts) > 2 and parts[1].casefold() in YARN_CODE_DIRS


def _blob(data: bytes) -> str:
    """git's SHA-1 object id for a blob holding ``data``."""

    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _patched_dependencies(root: Path) -> frozenset[str]:
    """Patch files that package.json's ``pnpm.patchedDependencies`` points at, wherever they are."""

    try:
        manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
        patches = (manifest.get("pnpm") or {}).get("patchedDependencies") or {}
    except (OSError, ValueError, AttributeError):
        return frozenset()
    found = set()
    for value in patches.values() if isinstance(patches, dict) else ():
        if isinstance(value, str):
            candidate = PurePosixPath(os.path.normpath(value))
            if not candidate.is_absolute() and ".." not in candidate.parts:
                found.add(candidate.as_posix())
    return frozenset(found)


def scan_inputs(worktree: str | Path) -> dict[str, str]:
    """Every dependency input in ``worktree``, tracked or not: ``path -> "file:<blob id>"`` or ``"link:<blob id>"``."""

    root = Path(worktree)
    extra = _patched_dependencies(root)
    found: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in PRUNED)
        for name in filenames + [name for name in dirnames if (Path(directory) / name).is_symlink()]:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if not _is_input(relative, extra):
                continue
            try:
                if path.is_symlink():
                    found[relative] = "link:" + _blob(os.fsencode(os.readlink(path)))
                elif path.is_file():
                    found[relative] = "file:" + _blob(path.read_bytes())
            except OSError:
                found[relative] = "unreadable:"
    return found


def input_hashes(worktree: str | Path, command: Sequence[str]) -> dict[str, str]:
    """``scan_inputs`` plus the install command itself."""

    hashes = scan_inputs(worktree)
    hashes[_COMMAND_KEY] = hashlib.sha256(json.dumps(list(command)).encode("utf-8")).hexdigest()
    return hashes


def _digest(hashes: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(dict(hashes), sort_keys=True).encode("utf-8")).hexdigest()


def fingerprint(worktree: str | Path, command: Sequence[str]) -> str:
    """One sha256 over ``input_hashes``: the value Harbor records per worktree."""

    return _digest(input_hashes(worktree, command))


def _declares_packages(worktree: Path) -> bool:
    if (worktree / "pnpm-workspace.yaml").is_file():
        return True
    try:
        manifest = json.loads((worktree / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict):
        return False
    keys = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "workspaces")
    return any(manifest.get(key) for key in keys)


def missing_output(worktree: str | Path, command: Sequence[str]) -> str | None:
    """Why the install output looks absent or incomplete, or ``None``.

    The marker checked is the one the configured package manager writes when
    an install finishes: ``node_modules/.modules.yaml`` for pnpm,
    ``node_modules/.package-lock.json`` for npm.
    """

    root = Path(worktree)
    program, _ = _program(command)
    if (root / "package.json").is_file() and _declares_packages(root):
        modules = root / "node_modules"
        yarn_pnp = (root / ".yarnrc.yml").is_file() and (root / ".pnp.cjs").is_file()
        if not modules.is_dir() and not yarn_pnp:
            return "node_modules is missing"
        if program == "pnpm" and not (modules / ".modules.yaml").is_file():
            return "node_modules is incomplete (no node_modules/.modules.yaml)"
        if program == "npm" and not (modules / ".package-lock.json").is_file():
            return "node_modules is incomplete (no node_modules/.package-lock.json)"
    if program == "uv" and (root / "uv.lock").is_file() and not (root / ".venv").is_dir():
        return ".venv is missing"
    return None


def without_scripts(command: Sequence[str], inputs: Mapping[str, str]) -> tuple[tuple[str, ...] | None, str]:
    """``command`` with install scripts and hooks switched off, or ``(None, why it cannot be)``.

    Used only for ``[builder] dependency_scripts = "off"``. It is a project
    preference, not a security boundary: the install sandbox contains the
    install either way.
    """

    command = tuple(command)
    program, index = _program(command)
    rest = command[index + 1:]
    sub = rest[0] if rest else ""
    if program == "pnpm" and sub in {"", "install", "i"}:
        return command + ("--ignore-scripts", "--ignore-pnpmfile"), ""
    if program == "npm" and sub in {"install", "i", "ci", "clean-install"}:
        return command + ("--ignore-scripts",), ""
    if program == "bun" and sub in {"install", "i"}:
        return command + ("--ignore-scripts",), ""
    if program == "yarn" and sub in {"", "install"}:
        rc = sorted(path for path in inputs if PurePosixPath(path).name.casefold() in {".yarnrc", ".yarnrc.yml"}
                    or path.casefold().startswith(".yarn/"))
        if rc:
            return None, (f"Yarn loads {', '.join(rc[:3])} (yarnPath, plugins), which run code "
                          "that no Yarn flag switches off")
        return command + ("--ignore-scripts",), ""
    if program == "uv" and sub == "sync":
        return command + ("--no-install-project", "--no-install-workspace", "--no-install-local", "--no-build"), ""
    return None, f"Harbor knows no way to switch off install scripts for `{shlex.join(command)}`"


def _pinned(command: tuple[str, ...]) -> tuple[str, ...]:
    """pnpm never switches to the package-manager version the worktree names (also pinned by environment)."""

    program, _ = _program(command)
    return command + ("--config.manage-package-manager-versions=false",) if program == "pnpm" else command


def _schema(connection: Any) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS dependency_installs ("
        "worktree TEXT PRIMARY KEY, project TEXT, fingerprint TEXT, inputs TEXT, status TEXT, "
        "output_missing TEXT, command TEXT, error TEXT, updated_at TEXT)"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _key(worktree: str | Path) -> str:
    return str(Path(worktree).expanduser().resolve())


def _json_map(text: Any) -> dict[str, str] | None:
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class InstallRecord:
    fingerprint: str
    inputs: dict[str, str]
    status: str
    output_missing: str = ""


def recorded(connection: Any, worktree: str | Path) -> InstallRecord | None:
    """What Harbor last recorded for ``worktree``'s dependency install."""

    _schema(connection)
    row = connection.execute(
        "SELECT fingerprint, inputs, status, output_missing FROM dependency_installs WHERE worktree=?",
        (_key(worktree),),
    ).fetchone()
    if row is None:
        return None
    return InstallRecord(str(row[0] or ""), _json_map(row[1]) or {}, str(row[2] or ""), str(row[3] or ""))


def record(connection: Any, worktree: str | Path, command: Sequence[str], *, status: str,
           inputs: Mapping[str, str] | None = None, error: str = "", project: str = "") -> None:
    """Record an install of ``worktree`` (``inputs`` defaults to its current input hashes)."""

    _schema(connection)
    hashes = dict(inputs) if inputs is not None else input_hashes(worktree, command)
    missing = (missing_output(worktree, command) or "") if status == "installed" else ""
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO dependency_installs VALUES (?,?,?,?,?,?,?,?,?)",
            (_key(worktree), project, _digest(hashes), json.dumps(hashes, sort_keys=True), status, missing,
             json.dumps(list(command)), error, _now()),
        )


STALE = "stale"


def schedule_reinstall(connection: Any, worktree: str | Path, *, lock: Any = None) -> None:
    """Make the next pre-turn check reinstall ``worktree``; nothing runs now.

    Rung-0 remediation calls this for a builder-reported ``Cannot find module``
    instead of running a package manager itself, so the reinstall goes through
    ``prepare_dependencies`` and its sandbox. ``lock`` is the ledger's lock.
    """

    with lock if lock is not None else contextlib.nullcontext():
        _schema(connection)
        with connection:
            connection.execute("UPDATE dependency_installs SET status=?, updated_at=? WHERE worktree=?",
                               (STALE, _now(), _key(worktree)))


@dataclass(frozen=True)
class DependencyCheck:
    """What the pre-turn check found and did."""

    action: str  # "current", "installed", "failed" or "skipped"
    command: tuple[str, ...]
    reasons: tuple[str, ...] = ()
    error: str = ""
    returncode: int | None = None
    ran: bool = True
    scripts: bool | None = None  # whether install scripts were allowed; None when nothing ran
    isolation: str = ""
    ran_command: tuple[str, ...] = field(default=())

    @property
    def failed(self) -> bool:
        return self.action == "failed"

    @property
    def command_text(self) -> str:
        return shlex.join(self.command)

    @property
    def ran_text(self) -> str:
        return shlex.join(self.ran_command or self.command)

    def line(self) -> str:
        """One clause, starting with a verb, for the log and the builder's prompt."""

        why = "; ".join(self.reasons)
        if self.action == "current":
            return f"found the dependencies up to date, so no install was needed (`{self.command_text}` last ran on these inputs)"
        if self.action == "skipped":
            return f"did not check the dependencies: {self.error}"
        if not self.ran:
            return f"refused to run `{self.command_text}` because {why}: {self.error}"
        where = f" in {self.isolation}" if self.isolation else ""
        scripts = "" if self.scripts is None else f" with install scripts {'on' if self.scripts else 'OFF'}"
        ran = f"ran `{self.ran_text}`{where}{scripts} because {why}"
        if self.action == "installed":
            return f"{ran}; it succeeded"
        return f"{ran}; it FAILED with exit code {self.returncode}: {self.error}"


def _changed_reasons(previous: InstallRecord | None, current: Mapping[str, str]) -> list[str]:
    if previous is None:
        return ["Harbor has no record of installing this worktree"]
    if previous.status == STALE:
        return ["the builder reported a missing module, so rung-0 remediation asked for a reinstall"]
    if previous.status != "installed":
        return ["the previous install did not succeed"]
    changed = sorted(name for name in set(previous.inputs) | set(current)
                     if previous.inputs.get(name) != current.get(name))
    if not changed:
        return []
    names = [("the install command" if name == _COMMAND_KEY else name) for name in changed]
    shown = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
    return [f"{shown} changed since Harbor's last install"]


def _kill_group(process: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def _spawn(command: Sequence[str], cwd: str | Path, env: Mapping[str, str], output: Any) -> subprocess.Popen:
    # Output goes to a file, never a pipe: a descendant that keeps a pipe open
    # (a daemon a postinstall started) could otherwise hold Harbor past its deadline.
    return subprocess.Popen(list(command), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
                            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)


def _wait(process: subprocess.Popen, timeout: float) -> int:
    """The exit code, or 124 after killing the whole group at ``timeout``; never waits past the grace period."""

    try:
        code = int(process.wait(timeout=timeout))
    except subprocess.TimeoutExpired:
        _kill_group(process)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_KILL_GRACE_SECONDS)
        return 124
    # Leftover children in the install's group (a watcher a script started) are stopped too.
    _kill_group(process)
    return code


def _read_output(handle: Any) -> str:
    handle.flush()
    handle.seek(0)
    return handle.read().decode("utf-8", errors="replace").strip()


def run_hardened(command: Sequence[str], cwd: str | Path, env: Mapping[str, str],
                 timeout: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[int, str]:
    """Run ``command`` in its own process group with a hard deadline. ``(returncode, output)``."""

    with tempfile.TemporaryFile() as output:
        try:
            process = _spawn(command, cwd, env, output)
        except FileNotFoundError as exc:
            return 127, str(exc)
        except OSError as exc:
            return 126, str(exc)
        try:
            code = _wait(process, timeout)
        except BaseException:
            _kill_group(process)
            raise
        text = _read_output(output)
    return code, (f"timed out after {timeout:.0f}s" + (f": {text[-500:]}" if text else "")) if code == 124 else text


async def _run_async(command: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float) -> tuple[int, str]:
    with tempfile.TemporaryFile() as output:
        try:
            process = _spawn(command, cwd, env, output)
        except FileNotFoundError as exc:
            return 127, str(exc)
        except OSError as exc:
            return 126, str(exc)
        try:
            code = await asyncio.to_thread(_wait, process, timeout)
        except BaseException:
            # Operator exit or supervisor shutdown: never leave an install running.
            _kill_group(process)
            raise
        text = _read_output(output)
    return code, (f"timed out after {timeout:.0f}s" + (f": {text[-500:]}" if text else "")) if code == 124 else text


def _git_dirs(worktree: Path) -> list[Path]:
    from harbor.safe_git import main_git_dir

    dirs = []
    with contextlib.suppress(OSError, ValueError):
        common = main_git_dir(worktree)
        if common is not None:
            dirs.append(common)
    return dirs


def isolated_command(isolation: Any, command: Sequence[str], worktree: Path, env: Mapping[str, str],
                     repo_git: Sequence[Path] = ()) -> tuple[list[str], dict[str, str], Callable[[], None]]:
    """``command`` and ``env`` wrapped by ``isolation`` (``harbor.install_sandbox.MacSandbox``)."""

    return isolation.prepare(command, worktree, env, git_dirs=[*_git_dirs(worktree), *repo_git])


async def prepare_dependencies(
    worktree: str | Path,
    *,
    command: Sequence[str],
    connection: Any,
    project: str = "",
    root: str | Path | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    extra_secrets: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
    lock: Any = None,
    isolation: Any = None,
    scripts: str = "on",
) -> DependencyCheck:
    """Reinstall ``worktree``, in ``isolation``, when its inputs changed or its output is missing.

    ``isolation`` is Harbor's install sandbox; ``None`` means this host has
    none, and a needed reinstall is refused rather than run uncontained.
    Ledger access stays on the caller's thread; the install runs in a worker
    thread, so the supervisor's loop is never stalled.
    """

    command = tuple(command)
    lock = lock if lock is not None else contextlib.nullcontext()
    target = Path(worktree).expanduser().resolve()
    if root is not None:
        boundary = Path(root).expanduser().resolve()
        try:
            target.relative_to(boundary)
        except ValueError:
            return DependencyCheck("failed", command, ("the worktree is outside Harbor's worktree root",),
                                   f"refusing to install outside {boundary}: {target}", 126, ran=False)
    current = await asyncio.to_thread(input_hashes, target, command)
    with lock:
        previous = recorded(connection, target)
    reasons = _changed_reasons(previous, current)
    missing = missing_output(target, command)
    # A marker the package manager never writes was already absent after Harbor's
    # own successful install: not a reason to reinstall every turn.
    if missing and not (previous is not None and previous.status == "installed" and previous.output_missing == missing):
        reasons.append(missing)
    if not reasons:
        return DependencyCheck("current", command)

    def refuse(error: str) -> DependencyCheck:
        with lock:
            record(connection, target, command, status="refused", inputs=current, error=error, project=project)
        return DependencyCheck("failed", command, tuple(reasons), error, 126, ran=False)

    if isolation is None:
        return refuse("this host has no install sandbox (Harbor's is macOS sandbox-exec), and Harbor does not "
                      "run an install of builder-editable files uncontained. Run it by hand outside the sandbox, "
                      "then answer this blocker, or set install_command: none")
    resolved, why = resolve_command(command, target, environ)
    if resolved is None:
        return refuse(why)
    effective: tuple[str, ...] | None = resolved
    if scripts == "off":
        effective, why = without_scripts(resolved, current)
        if effective is None:
            return refuse(f'[builder] dependency_scripts = "off", but {why}')
    effective = _pinned(effective)
    env = install_environment(environ, extra_secrets=extra_secrets, worktree=target, scripts=scripts != "off")
    argv, env, cleanup = isolated_command(isolation, effective, target, env)
    try:
        returncode, output = await _run_async(argv, target, env, timeout_seconds)
    finally:
        cleanup()
    check = dict(command=command, reasons=tuple(reasons), scripts=scripts != "off",
                 isolation=getattr(isolation, "name", "an install sandbox"), ran_command=effective)
    if returncode == 0:
        with lock:
            # Hash again: the install may rewrite its own lockfile (pnpm, uv).
            record(connection, target, command, status="installed", project=project)
        return DependencyCheck("installed", returncode=0, **check)
    from harbor.url_credentials import redact_userinfo
    error = redact_userinfo(output[-_ERROR_CHARS:]) or f"exit code {returncode} with no output"
    with lock:
        record(connection, target, command, status="failed", inputs=current, error=error, project=project)
    return DependencyCheck("failed", error=error, returncode=returncode, **check)


def creation_install(command: Sequence[str], worktree: Path, *, repo: Path, timeout: float,
                     isolation: Any = None) -> tuple[int, str]:
    """The install ``WorktreeManager`` runs in a new worktree: sandboxed where Harbor can, credentials removed.

    Without an install sandbox (Linux) it runs as before, unsandboxed on the
    base branch's files, but still without Harbor's credentials.
    """

    target = Path(worktree).expanduser().resolve()
    resolved, why = resolve_command(command, target)
    if resolved is None:
        return 127, why
    env = install_environment(worktree=target)
    argv: Sequence[str] = _pinned(resolved)
    cleanup: Callable[[], None] = lambda: None
    if isolation is not None:
        argv, env, cleanup = isolated_command(isolation, argv, target, env, repo_git=[repo / ".git"])
    try:
        return run_hardened(argv, target, env, timeout)
    finally:
        cleanup()


def builder_note(check: DependencyCheck) -> str:
    """The text Harbor appends to every builder prompt when it manages installs."""

    lines = [
        "Harbor dependencies: before each turn Harbor checks whether the dependency inputs (lockfiles, "
        "package.json, .npmrc, .pnpmfile.cjs, patches) changed since its last install, or the install output "
        "(node_modules) is missing, and if so runs the project's install command "
        f"(`{check.command_text}`) outside your sandbox, in its own install sandbox. "
        f"Before this turn Harbor {check.line()}.",
        "Do not run install or reinstall commands yourself (pnpm install, npm ci, npm install, yarn install, "
        "or anything else that deletes or rewrites node_modules): the sandbox cannot write some files inside "
        "node_modules, so an install from inside it can leave this worktree unrunnable. To change dependencies, "
        "edit package.json and update only the lockfile (for example `pnpm install --lockfile-only` or "
        "`npm install --package-lock-only`). If the lockfile changes during this turn, including through a "
        "merge, finish the turn: Harbor installs before the next one. Install scripts run without network "
        "credentials, without git metadata and with writes limited to this worktree, so a script that needs "
        "more fails; report that as a blocker.",
    ]
    if check.scripts is False:
        lines.append("Install scripts were switched off for that install ([builder] dependency_scripts = \"off\").")
    return "\n".join(lines)


def blocker(check: DependencyCheck, worktree: str | Path, ticket: str) -> dict[str, str]:
    """The structured blocker for a turn Harbor refused to start (kind ``BLOCKER_KIND``)."""

    command = check.ran_text if check.ran else check.command_text
    did = "failed" if check.ran else "was refused"
    return {
        "kind": BLOCKER_KIND,
        "question": (f"Harbor's pre-turn dependency install `{command}` {did} in {worktree}, so the builder turn "
                     "was not started. How should the install be fixed?"),
        "recommendation": (f"Run `{check.command_text}` in {worktree} outside the sandbox, fix what it reports "
                           "(the lockfile, the registry, or the install_command), then answer this blocker "
                           f"(/answer, or a comment on {ticket or 'the ticket'}). "
                           "Harbor re-runs the install before the resumed turn."),
        "evidence": check.error,
    }


def pre_turn_installer(manager: Any, db: Any, *, project: str = "", extra_secrets: Iterable[str] = (),
                       isolation: Any = "default", scripts: str = "on"
                       ) -> Callable[[Path], Awaitable[DependencyCheck]] | None:
    """A per-turn installer bound to ``manager``'s install command, or ``None`` when it has none.

    ``manager.install_command`` is already normalized: an unset
    ``install_command`` means ``pnpm install`` there, so only an explicit
    ``none`` (or empty) command turns this off. ``isolation`` defaults to
    Harbor's install sandbox for this host (``None`` on Linux). ``db`` is
    Harbor's ledger or a bare connection; ledger access takes its lock.
    """

    command = normalize_command(getattr(manager, "install_command", None))
    connection = getattr(db, "connection", db)
    if not command or connection is None:
        return None
    if isolation == "default":
        from harbor.install_sandbox import default_isolation
        isolation = default_isolation(getattr(manager, "repo", None))
    root = getattr(manager, "root", None)
    timeout = float(getattr(manager, "install_timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    secrets = tuple(name for name in extra_secrets if name)
    lock = getattr(db, "_lock", None)

    async def prepare(worktree: Path) -> DependencyCheck:
        return await prepare_dependencies(worktree, command=command, connection=connection, project=project,
                                          root=root, timeout_seconds=timeout, extra_secrets=secrets, lock=lock,
                                          isolation=isolation, scripts=scripts)

    return prepare


def log(ticket: str, check: DependencyCheck) -> None:
    print(f"Harbor: dependencies for {ticket}: {check.line()}", file=sys.stderr)


__all__ = [
    "BLOCKER_KIND", "DependencyCheck", "HARBOR_SECRET_NAMES", "INPUT_NAMES", "InstallRecord", "PINNED_ENV",
    "blocker", "builder_note", "creation_install", "fingerprint", "input_hashes", "install_environment",
    "missing_output", "normalize_command", "pre_turn_installer", "prepare_dependencies", "record", "recorded",
    "resolve_command", "run_hardened", "scan_inputs", "schedule_reinstall", "without_scripts",
]
