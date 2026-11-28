"""Reinstall a worktree's dependencies outside the builder sandbox before each turn.

A builder cannot reliably install dependencies itself. Claude Code's sandbox
refuses writes to, and deletion of, files it protects (``.gitmodules``,
``.idea/``, ``.vscode/`` and so on) even deep inside ``node_modules``, so a
``pnpm install`` run in the sandbox can delete ``node_modules`` and then fail
to write it back (HAN-94: ``ERR_PNPM_EPERM: reflink to
node_modules/eslint-plugin-react/node_modules/resolve_tmp_13868_2/.gitmodules,
Operation not permitted``). The worktree is then unrunnable until a person
reinstalls outside the sandbox.

So before every builder turn, after the launch is authorized, Harbor compares
the worktree's dependency inputs (``scan_inputs``) with what it recorded at its
last install, and checks that the install output is present. If either
changed, it re-runs the project's configured ``install_command`` outside the
sandbox, as an argument list, never a shell.

The install runs code the inputs control (lifecycle scripts, ``.pnpmfile.cjs``,
``.npmrc``), and the builder can edit them. So install scripts run only when
every input is byte-identical to the base branch tip (read by Harbor from its
own remote into a private repository, never from refs in the worktree) or to
the inputs of Harbor's last trusted install. Otherwise Harbor installs with
scripts and hooks switched off (``without_scripts``), or refuses when it cannot
switch them off for that package manager, unless the project opted in with
``[builder] dependency_scripts = "always"``. Either way Harbor's own
credentials are removed from the environment (``install_environment``).

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
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

# The blocker kind of a turn Harbor refused to start because its install failed.
# Rung-0 remediation never acts on it (harbor.facts.remediation), so the raw
# install output it carries can never trigger another unsandboxed command.
BLOCKER_KIND = "dependency_install"

# Inputs, matched by file name at any depth (outside PRUNED directories).
INPUT_NAMES = frozenset({
    "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "package-lock.json", "npm-shrinkwrap.json",
    "yarn.lock", "bun.lock", "bun.lockb", "bunfig.toml",
    ".npmrc", ".pnpmfile.cjs", ".pnpmfile.mjs", ".yarnrc", ".yarnrc.yml",
    "uv.lock", "uv.toml", "pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "Pipfile", "Pipfile.lock",
    "Gemfile", "Gemfile.lock",
})
# Also inputs: every file under top-level patches/, and Yarn's patches, plugins and releases (code Yarn runs).
YARN_CODE_DIRS = frozenset({"patches", "plugins", "releases"})
# Install output and git metadata are never inputs, and are not walked.
PRUNED = frozenset({"node_modules", ".git", ".venv", ".pnpm-store"})

DEFAULT_TIMEOUT_SECONDS = 15 * 60.0
_ERROR_CHARS = 2000
_COMMAND_KEY = "(install_command)"
_SAFE_BRANCH = re.compile(r"[A-Za-z0-9._/-]+")

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


def install_environment(environ: Mapping[str, str] | None = None, *, extra_secrets: Iterable[str] = (),
                        scripts: bool = True) -> dict[str, str]:
    """``environ`` (default: Harbor's own) without Harbor's credentials.

    Removed: the names in ``HARBOR_SECRET_NAMES``; any Harbor, Linear, GitHub
    or model-provider variable whose name contains TOKEN, KEY, SECRET,
    PASSWORD or CREDENTIAL; the variable ``HARBOR_GITHUB_TOKEN_ENV`` names; and
    ``extra_secrets``. With ``scripts=False`` (inputs the builder changed) the
    SSH agent socket is removed too, and npm, pnpm and Yarn are told through
    their environment not to run scripts, in addition to the command's flags.

    ``HOME`` is kept on purpose: version-manager shims (volta, asdf, mise,
    nvm, corepack), the pnpm store, the npm cache and the user's registry
    ``.npmrc`` all resolve from it, so a private ``HOME`` breaks or silently
    changes ordinary installs. It would not confine anything either: an
    install can read ``~/.ssh`` by absolute path. The protection for
    builder-changed inputs is that their scripts do not run.
    """

    source = dict(os.environ if environ is None else environ)
    drop = set(HARBOR_SECRET_NAMES) | {str(name) for name in extra_secrets if name}
    pointer = source.get(_TOKEN_POINTER)
    if pointer:
        drop.add(pointer)
    for name in source:
        upper = name.upper()
        if upper in drop or (upper.startswith(_SECRET_PREFIXES) and _SECRET_WORDS.search(upper)):
            drop.add(name)
    env = {name: value for name, value in source.items() if name not in drop}
    if not scripts:
        env.pop("SSH_AUTH_SOCK", None)
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


def _is_input(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or any(part in PRUNED for part in parts):
        return False
    name = parts[-1]
    if name in INPUT_NAMES or (name.startswith("requirements") and name.endswith(".txt")):
        return True
    if parts[0] == "patches":
        return len(parts) > 1
    # Yarn runs .yarn/releases (yarnPath) and .yarn/plugins; .yarn/cache is package data, not hashed.
    return parts[0] == ".yarn" and len(parts) > 2 and parts[1] in YARN_CODE_DIRS


def _blob(data: bytes) -> str:
    """git's SHA-1 object id for a blob holding ``data``."""

    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def scan_inputs(worktree: str | Path) -> dict[str, str]:
    """Every dependency input in ``worktree``, tracked or not: ``path -> "file:<blob id>"`` or ``"link:<blob id>"``.

    The walk does not follow directory symlinks. A symlinked input is recorded
    by its link text, as git records it, so it can only match a symlink.
    """

    root = Path(worktree)
    found: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in PRUNED)
        for name in filenames + [name for name in dirnames if (Path(directory) / name).is_symlink()]:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if not _is_input(relative):
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
    ``node_modules/.package-lock.json`` for npm. A sandboxed install that failed
    half way (HAN-94) leaves ``node_modules`` without it.
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
    """``command`` with install scripts and hooks switched off, or ``(None, why it cannot be)``."""

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
        rc = sorted(path for path in inputs if PurePosixPath(path).name in {".yarnrc", ".yarnrc.yml"}
                    or path.startswith(".yarn/"))
        if rc:
            return None, (f"Yarn loads {', '.join(rc[:3])} (yarnPath, plugins), which run code "
                          "that no Yarn flag switches off")
        return command + ("--ignore-scripts",), ""
    if program == "uv" and sub == "sync":
        # Never build the project, workspace members, local paths or source distributions:
        # each runs a build backend that the inputs choose.
        return command + ("--no-install-project", "--no-install-workspace", "--no-install-local", "--no-build"), ""
    return None, f"Harbor knows no way to switch off install scripts for `{shlex.join(command)}`"


@dataclass(frozen=True)
class BaseSource:
    """Where Harbor reads the base branch: its own remote, never the worktree's refs.

    ``token_env`` names the variable holding Harbor's GitHub token; it is read
    when needed and only ever reaches Harbor's own git fetch.
    """

    fetch_url: str
    branch: str
    token_env: str = ""


def _git_env(home: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1",
               GIT_CEILING_DIRECTORIES=home)
    env.update(extra or {})
    return env


def base_inputs(source: BaseSource, *, environ: Mapping[str, str] | None = None,
                timeout: float = 120) -> tuple[dict[str, str] | None, str]:
    """The base branch tip's dependency inputs, ``path -> "file:<blob id>"``, and a description.

    git runs in a fresh private bare repository with no alternates, so nothing
    the builder can write (the project's refs, objects or config) is read. The
    tip is fetched from ``source.fetch_url`` with only trees (``--depth=1
    --filter=blob:none``); git verifies every fetched object against its id, so
    the blob ids in its trees are the base branch's. On failure the dict is
    None and the description says why.
    """

    from harbor.device_proof import HARDENING, _auth_env, redact

    branch = source.branch
    label = f"origin/{branch}"
    if not _SAFE_BRANCH.fullmatch(branch or "") or branch.startswith("-") or ".." in branch:
        return None, f"the base branch name {branch!r} is not a plain branch name"
    environ = os.environ if environ is None else environ
    token = environ.get(source.token_env) if source.token_env else None
    with tempfile.TemporaryDirectory(prefix="harbor-deps-") as home:
        repo = str(Path(home) / "base.git")

        def git(*args: str, extra: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "--no-replace-objects", *HARDENING, "--git-dir", repo, *args],
                                  capture_output=True, text=True, timeout=timeout, cwd=home,
                                  env=_git_env(home, extra))

        try:
            init = subprocess.run(["git", *HARDENING, "init", "--bare", "--quiet", repo], capture_output=True,
                                  text=True, timeout=timeout, cwd=home, env=_git_env(home))
            if init.returncode != 0:
                return None, f"could not create a private repository: {init.stderr.strip()}"
            fetched = git("fetch", "--no-tags", "--no-write-fetch-head", "--quiet", "--depth=1", "--filter=blob:none",
                          "--upload-pack=git-upload-pack", "--end-of-options", source.fetch_url,
                          f"+refs/heads/{branch}:refs/harbor/base", extra=_auth_env(source.fetch_url, token))
            if fetched.returncode != 0:
                detail = redact((fetched.stderr or fetched.stdout or "").strip(), token).splitlines()
                return None, f"could not fetch {label} from {source.fetch_url}: {detail[-1] if detail else 'unknown error'}"
            sha = git("rev-parse", "--verify", "--end-of-options", "refs/harbor/base^{commit}").stdout.strip()
            listed = git("ls-tree", "-r", "-z", "--full-tree", "--end-of-options", "refs/harbor/base")
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"could not read {label}: {exc}"
        if listed.returncode != 0 or not sha:
            return None, f"could not list {label}: {listed.stderr.strip()}"
    inputs: dict[str, str] = {}
    for record in listed.stdout.split("\0"):
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) != 3 or not _is_input(path):
            continue
        mode, _, object_id = fields
        if mode == "120000":
            inputs[path] = "link:" + object_id
        elif mode in {"100644", "100755"}:
            inputs[path] = "file:" + object_id
    return inputs, f"{label} at {sha[:12]}"


def _schema(connection: Any) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS dependency_installs ("
        "worktree TEXT PRIMARY KEY, project TEXT, fingerprint TEXT, inputs TEXT, trusted_inputs TEXT, "
        "status TEXT, scripts INTEGER, output_missing TEXT, command TEXT, error TEXT, updated_at TEXT)"
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
    trusted_inputs: dict[str, str] | None = None
    output_missing: str = ""


def recorded(connection: Any, worktree: str | Path) -> InstallRecord | None:
    """What Harbor last recorded for ``worktree``'s dependency install."""

    _schema(connection)
    row = connection.execute(
        "SELECT fingerprint, inputs, status, trusted_inputs, output_missing FROM dependency_installs WHERE worktree=?",
        (_key(worktree),),
    ).fetchone()
    if row is None:
        return None
    return InstallRecord(str(row[0] or ""), _json_map(row[1]) or {}, str(row[2] or ""), _json_map(row[3]),
                         str(row[4] or ""))


def record(connection: Any, worktree: str | Path, command: Sequence[str], *, status: str,
           inputs: Mapping[str, str] | None = None, trusted: bool = False,
           trusted_inputs: Mapping[str, str] | None = None, scripts: bool = True,
           error: str = "", project: str = "") -> None:
    """Record an install of ``worktree``.

    ``inputs`` defaults to its current input hashes. ``trusted`` marks those
    inputs as safe to run scripts on later (a worktree made from the base
    branch, or inputs that matched it); otherwise ``trusted_inputs`` carries the
    previous trusted set forward.
    """

    _schema(connection)
    hashes = dict(inputs) if inputs is not None else input_hashes(worktree, command)
    trust = hashes if trusted else (dict(trusted_inputs) if trusted_inputs is not None else None)
    missing = missing_output(worktree, command) or "" if status == "installed" else ""
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO dependency_installs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_key(worktree), project, _digest(hashes), json.dumps(hashes, sort_keys=True),
             json.dumps(trust, sort_keys=True) if trust is not None else None, status, int(bool(scripts)),
             missing, json.dumps(list(command)), error, _now()),
        )


STALE = "stale"


def schedule_reinstall(connection: Any, worktree: str | Path) -> None:
    """Make the next pre-turn check reinstall ``worktree``; nothing runs now.

    Rung-0 remediation calls this for a builder-reported ``Cannot find module``
    instead of running a package manager itself, so that reinstall goes through
    ``prepare_dependencies``: the project's install_command, its environment
    and process group, and install scripts only on base-matching or trusted
    inputs. The trusted inputs are kept. A worktree with no record already
    reinstalls on its next turn.
    """

    _schema(connection)
    with connection:
        connection.execute("UPDATE dependency_installs SET status=?, updated_at=? WHERE worktree=?",
                           (STALE, _now(), _key(worktree)))


@dataclass(frozen=True)
class DependencyCheck:
    """What the pre-turn check found and did."""

    action: str  # "current", "installed" or "failed"
    command: tuple[str, ...]
    reasons: tuple[str, ...] = ()
    error: str = ""
    returncode: int | None = None
    ran: bool = True
    scripts: bool | None = None  # whether install scripts were allowed; None when nothing ran
    trust: str = ""  # why scripts were or were not allowed
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

    def _scripts_text(self) -> str:
        if self.scripts is None:
            return ""
        state = "on" if self.scripts else "OFF"
        return f" with install scripts {state} ({self.trust})"

    def line(self) -> str:
        """One clause, starting with a verb, for the log and the builder's prompt."""

        why = "; ".join(self.reasons)
        if self.action == "current":
            return f"found the dependencies up to date, so no install was needed (`{self.command_text}` last ran on these inputs)"
        if not self.ran:
            return f"refused to run `{self.command_text}` because {why}: {self.error}"
        ran = f"ran `{self.ran_text}` outside the sandbox{self._scripts_text()} because {why}"
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


def _trust(previous: InstallRecord | None, current: Mapping[str, str], base: BaseSource | None,
           environ: Mapping[str, str] | None) -> tuple[bool, str]:
    """Whether every input is one Harbor may run install scripts on, and why (not)."""

    if previous is not None and previous.trusted_inputs == dict(current):
        return True, "the inputs match Harbor's last trusted install"
    if base is None:
        return False, "the inputs changed and Harbor has no base branch to compare them with"
    tip, description = base_inputs(base, environ=environ)
    if tip is None:
        return False, f"the inputs changed and Harbor could not read the base branch: {description}"
    files = {path: value for path, value in current.items() if path != _COMMAND_KEY}
    if files == tip:
        return True, f"every input matches {description}"
    differ = sorted(path for path in set(files) | set(tip) if files.get(path) != tip.get(path))
    shown = ", ".join(differ[:6]) + (f" and {len(differ) - 6} more" if len(differ) > 6 else "")
    return False, f"the builder's inputs differ from {description}: {shown}"


def _kill_group(process: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def _communicate(process: subprocess.Popen, timeout: float) -> tuple[int, str]:
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # The install may have started children (postinstall, node-gyp): stop them all.
        _kill_group(process)
        process.communicate()
        return 124, f"timed out after {timeout:.0f}s"
    return int(process.returncode), (err or out or "").strip()


def _spawn(command: Sequence[str], cwd: str | Path, env: Mapping[str, str]) -> subprocess.Popen:
    return subprocess.Popen(list(command), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)


def run_hardened(command: Sequence[str], cwd: str | Path, env: Mapping[str, str],
                 timeout: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[int, str]:
    """Run ``command`` in its own process group; on timeout kill the whole group. ``(returncode, output)``."""

    try:
        process = _spawn(command, cwd, env)
    except FileNotFoundError as exc:
        return 127, str(exc)
    except OSError as exc:
        return 126, str(exc)
    try:
        return _communicate(process, timeout)
    except BaseException:
        _kill_group(process)
        raise


async def _run_async(command: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float) -> tuple[int, str]:
    try:
        process = _spawn(command, cwd, env)
    except FileNotFoundError as exc:
        return 127, str(exc)
    except OSError as exc:
        return 126, str(exc)
    try:
        return await asyncio.to_thread(_communicate, process, timeout)
    except BaseException:
        # Operator exit or supervisor shutdown: never leave an unsandboxed install running.
        _kill_group(process)
        raise


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
    base: BaseSource | None = None,
    scripts_policy: str = "base",
) -> DependencyCheck:
    """Reinstall ``worktree`` when its dependency inputs changed or its output is missing.

    Ledger access stays on the caller's thread (Harbor's SQLite connection
    belongs to the supervisor's event loop); the install and the base branch
    read run in worker threads, so the loop is never stalled.
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
    # A marker the package manager never writes (npm without dependencies, a pnpm
    # project that ships package-lock.json) was already absent after Harbor's own
    # successful install; it is not a reason to reinstall every turn.
    if missing and not (previous is not None and previous.status == "installed" and previous.output_missing == missing):
        reasons.append(missing)
    if not reasons:
        return DependencyCheck("current", command)
    if scripts_policy == "always":
        trusted, why = False, '[builder] dependency_scripts = "always" allows scripts on any inputs'
        scripts = True
    else:
        trusted, why = await asyncio.to_thread(_trust, previous, current, base, environ)
        scripts = trusted
    carried = previous.trusted_inputs if previous is not None else None
    effective: tuple[str, ...] | None = command
    if not scripts:
        effective, refusal = without_scripts(command, current)
        if effective is None:
            error = (f"{why}, and {refusal}. Harbor will not run builder-changed install inputs outside the sandbox. "
                     "Make the inputs match the base branch, install by hand, or set "
                     '[builder] dependency_scripts = "always" in .harbor/workflow/workflow.toml '
                     "(that runs builder-written code outside the sandbox)")
            with lock:
                record(connection, target, command, status="refused", inputs=current, trusted_inputs=carried,
                       scripts=False, error=error, project=project)
            return DependencyCheck("failed", command, tuple(reasons), error, 126, ran=False, scripts=False, trust=why)
    env = install_environment(environ, extra_secrets=extra_secrets, scripts=scripts)
    returncode, output = await _run_async(effective, target, env, timeout_seconds)
    check = dict(command=command, reasons=tuple(reasons), scripts=scripts, trust=why, ran_command=effective)
    if returncode == 0:
        with lock:
            # Hash again: the install may rewrite its own lockfile (pnpm, uv).
            record(connection, target, command, status="installed", trusted=trusted, trusted_inputs=carried,
                   scripts=scripts, project=project)
        return DependencyCheck("installed", returncode=0, **check)
    from harbor.url_credentials import redact_userinfo
    error = redact_userinfo(output[-_ERROR_CHARS:]) or f"exit code {returncode} with no output"
    with lock:
        record(connection, target, command, status="failed", inputs=current, trusted_inputs=carried,
               scripts=scripts, error=error, project=project)
    return DependencyCheck("failed", error=error, returncode=returncode, **check)


def builder_note(check: DependencyCheck) -> str:
    """The text Harbor appends to every builder prompt when it manages installs."""

    lines = [
        "Harbor dependencies: before each turn Harbor checks whether the dependency inputs (lockfiles, "
        "package.json, .npmrc, .pnpmfile.cjs, patches) changed since its last install, or the install output "
        "(node_modules) is missing, and if so runs the project's install command "
        f"(`{check.command_text}`) outside your sandbox. Before this turn Harbor {check.line()}.",
        "Do not run install or reinstall commands yourself (pnpm install, npm ci, npm install, yarn install, "
        "or anything else that deletes or rewrites node_modules): the sandbox cannot write some files inside "
        "node_modules, so an install from inside it can leave this worktree unrunnable. To change dependencies, "
        "edit package.json and update only the lockfile (for example `pnpm install --lockfile-only` or "
        "`npm install --package-lock-only`). If the lockfile changes during this turn, including through a "
        "merge, finish the turn: Harbor installs before the next one.",
    ]
    if check.scripts is False:
        lines.append(
            "Install scripts were switched off for that install (postinstall, prepare and similar did not run, "
            "and neither did .pnpmfile.cjs), because the inputs differ from the base branch. Harbor runs them "
            "again once the inputs match the base branch. If the work needs them, report that as a blocker; "
            "do not run them yourself."
        )
    return "\n".join(lines)


def blocker(check: DependencyCheck, worktree: str | Path, ticket: str) -> dict[str, str]:
    """The structured blocker for a turn Harbor refused to start.

    Its kind is ``BLOCKER_KIND``, which rung-0 remediation never acts on: the
    evidence is the install's raw output, which the inputs (and so the builder)
    control.
    """

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
                       base: BaseSource | None = None,
                       scripts_policy: str = "base") -> Callable[[Path], Awaitable[DependencyCheck]] | None:
    """A per-turn installer bound to ``manager``'s install command, or ``None`` when it has none.

    ``manager.install_command`` is already normalized: an unset
    ``install_command`` means ``pnpm install`` there, so only an explicit
    ``none`` (or empty) command turns this off. ``db`` is Harbor's ledger
    (``harbor.db.HarborDB``) or a bare connection; ledger access takes the
    database's own lock when it has one.
    """

    command = normalize_command(getattr(manager, "install_command", None))
    connection = getattr(db, "connection", db)
    if not command or connection is None:
        return None
    root = getattr(manager, "root", None)
    timeout = float(getattr(manager, "install_timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    secrets = tuple(name for name in extra_secrets if name)
    lock = getattr(db, "_lock", None)

    async def prepare(worktree: Path) -> DependencyCheck:
        return await prepare_dependencies(worktree, command=command, connection=connection, project=project,
                                          root=root, timeout_seconds=timeout, extra_secrets=secrets, lock=lock,
                                          base=base, scripts_policy=scripts_policy)

    return prepare


def log(ticket: str, check: DependencyCheck) -> None:
    print(f"Harbor: dependencies for {ticket}: {check.line()}", file=sys.stderr)


__all__ = [
    "BLOCKER_KIND", "BaseSource", "DependencyCheck", "HARBOR_SECRET_NAMES", "INPUT_NAMES", "InstallRecord",
    "base_inputs", "blocker", "builder_note", "fingerprint", "input_hashes", "install_environment",
    "missing_output", "normalize_command", "pre_turn_installer", "prepare_dependencies", "record", "recorded",
    "run_hardened", "scan_inputs", "schedule_reinstall", "without_scripts",
]
