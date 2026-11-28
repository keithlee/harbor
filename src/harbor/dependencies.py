"""Reinstall a worktree's dependencies outside the builder sandbox before each turn.

A builder cannot reliably install dependencies itself. Claude Code's sandbox
refuses writes to, and deletion of, files it protects (``.gitmodules``,
``.idea/``, ``.vscode/`` and so on) even deep inside ``node_modules``, so a
``pnpm install`` run in the sandbox can delete ``node_modules`` and then fail
to write it back (HAN-94: ``ERR_PNPM_EPERM: reflink to
node_modules/eslint-plugin-react/node_modules/resolve_tmp_13868_2/.gitmodules,
Operation not permitted``). The worktree is then unrunnable until a person
reinstalls outside the sandbox.

So before every builder turn Harbor compares the worktree's dependency inputs
(lockfiles and ``package.json``) with the fingerprint it recorded at its last
install, and checks that the install output (``node_modules``) is present. If
either changed, it re-runs the project's configured ``install_command`` the way
``WorktreeManager.install`` does: as an argument list, never a shell, in the
worktree, outside the sandbox. Unlike ``install``, this reinstall runs with
Harbor's own credentials removed from its environment (``install_environment``).

The fingerprint lives in Harbor's SQLite ledger, keyed by worktree path, never
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
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

# Files whose change means the installed packages may no longer match. Every
# file present at the worktree root is hashed, whichever package manager the
# install command uses: an extra reinstall is cheap, a missed one is HAN-94.
DEPENDENCY_INPUTS: tuple[str, ...] = (
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.txt",
    "Gemfile.lock",
)

DEFAULT_TIMEOUT_SECONDS = 15 * 60.0
_ERROR_CHARS = 2000

# Credentials Harbor itself holds. The install runs code the worktree controls
# (lifecycle scripts, .pnpmfile.cjs), and the builder can edit the worktree, so
# none of these reach it. A package registry token (NPM_TOKEN, NODE_AUTH_TOKEN)
# is the project's, not Harbor's, and is kept.
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


def install_environment(environ: Mapping[str, str] | None = None, *, extra_secrets: Iterable[str] = ()) -> dict[str, str]:
    """``environ`` (default: Harbor's own) without Harbor's credentials.

    Removed: the names in ``HARBOR_SECRET_NAMES``; any Harbor, Linear, GitHub
    or model-provider variable whose name contains TOKEN, KEY, SECRET,
    PASSWORD or CREDENTIAL; the variable ``HARBOR_GITHUB_TOKEN_ENV`` names; and
    ``extra_secrets``. Everything else (PATH, HOME, registry settings) is kept,
    because the install needs it.
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
    return {name: value for name, value in source.items() if name not in drop}


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


def dependency_inputs(worktree: str | Path) -> list[Path]:
    root = Path(worktree)
    return [root / name for name in DEPENDENCY_INPUTS if (root / name).is_file()]


_COMMAND_KEY = "(install_command)"


def input_hashes(worktree: str | Path, command: Sequence[str]) -> dict[str, str]:
    """sha256 of every dependency input present, and of the install command itself."""

    hashes = {_COMMAND_KEY: hashlib.sha256(json.dumps(list(command)).encode("utf-8")).hexdigest()}
    for path in dependency_inputs(worktree):
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
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

    pnpm writes ``node_modules/.modules.yaml`` and npm writes
    ``node_modules/.package-lock.json`` when an install finishes; a sandboxed
    install that failed half way (HAN-94) leaves ``node_modules`` without them.
    """

    root = Path(worktree)
    if (root / "package.json").is_file() and _declares_packages(root):
        modules = root / "node_modules"
        yarn_pnp = (root / ".yarnrc.yml").is_file() and (root / ".pnp.cjs").is_file()
        if not modules.is_dir() and not yarn_pnp:
            return "node_modules is missing"
        if (root / "pnpm-lock.yaml").is_file() and not (modules / ".modules.yaml").is_file():
            return "node_modules is incomplete (no node_modules/.modules.yaml)"
        if (root / "package-lock.json").is_file() and not (modules / ".package-lock.json").is_file():
            return "node_modules is incomplete (no node_modules/.package-lock.json)"
    program = Path(command[0]).name if command else ""
    if program == "uv" and (root / "uv.lock").is_file() and not (root / ".venv").is_dir():
        return ".venv is missing"
    return None


def _schema(connection: Any) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS dependency_installs ("
        "worktree TEXT PRIMARY KEY, project TEXT, fingerprint TEXT, inputs TEXT, status TEXT, "
        "command TEXT, error TEXT, updated_at TEXT)"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _key(worktree: str | Path) -> str:
    return str(Path(worktree).expanduser().resolve())


@dataclass(frozen=True)
class InstallRecord:
    fingerprint: str
    inputs: dict[str, str]
    status: str


def recorded(connection: Any, worktree: str | Path) -> InstallRecord | None:
    """What Harbor last recorded for ``worktree``'s dependency install."""

    _schema(connection)
    row = connection.execute(
        "SELECT fingerprint, inputs, status FROM dependency_installs WHERE worktree=?", (_key(worktree),)
    ).fetchone()
    if row is None:
        return None
    try:
        inputs = json.loads(row[1] or "{}")
    except ValueError:
        inputs = {}
    return InstallRecord(str(row[0] or ""), inputs if isinstance(inputs, dict) else {}, str(row[2] or ""))


def record(connection: Any, worktree: str | Path, command: Sequence[str], *, status: str,
           inputs: Mapping[str, str] | None = None, error: str = "", project: str = "") -> None:
    """Record an install of ``worktree`` (``inputs`` defaults to its current input hashes)."""

    _schema(connection)
    hashes = dict(inputs) if inputs is not None else input_hashes(worktree, command)
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO dependency_installs VALUES (?,?,?,?,?,?,?,?)",
            (_key(worktree), project, _digest(hashes), json.dumps(hashes, sort_keys=True), status,
             json.dumps(list(command)), error, _now()),
        )


@dataclass(frozen=True)
class DependencyCheck:
    """What the pre-turn check found and did."""

    action: str  # "current", "installed" or "failed"
    command: tuple[str, ...]
    reasons: tuple[str, ...] = ()
    error: str = ""
    returncode: int | None = None
    ran: bool = True

    @property
    def failed(self) -> bool:
        return self.action == "failed"

    @property
    def command_text(self) -> str:
        return shlex.join(self.command)

    def line(self) -> str:
        """One sentence, starting with a verb, for the log and the builder's prompt."""

        why = "; ".join(self.reasons)
        if self.action == "current":
            return f"found the dependencies up to date, so no install was needed (`{self.command_text}` last ran on these inputs)"
        if self.action == "installed":
            return f"ran `{self.command_text}` outside the sandbox because {why}; it succeeded"
        if not self.ran:
            return f"refused to run `{self.command_text}`: {self.error}"
        return (f"ran `{self.command_text}` outside the sandbox because {why}; "
                f"it FAILED with exit code {self.returncode}: {self.error}")


def _changed_reasons(previous: InstallRecord | None, current: Mapping[str, str]) -> list[str]:
    if previous is None:
        return ["Harbor has no record of installing this worktree"]
    if previous.status != "installed":
        return ["the previous install did not succeed"]
    changed = sorted(name for name in set(previous.inputs) | set(current)
                     if previous.inputs.get(name) != current.get(name))
    if not changed:
        return []
    names = ", ".join("the install command" if name == _COMMAND_KEY else name for name in changed)
    return [f"{names} changed since Harbor's last install"]


async def prepare_dependencies(
    worktree: str | Path,
    *,
    command: Sequence[str],
    connection: Any,
    project: str = "",
    root: str | Path | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    extra_secrets: Iterable[str] = (),
    run: Callable[..., Any] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    lock: Any = None,
) -> DependencyCheck:
    """Reinstall ``worktree`` when its dependency inputs changed or its output is missing.

    Ledger access stays on the caller's thread (Harbor's SQLite connection
    belongs to the supervisor's event loop); only the install itself runs in a
    worker thread, so a minutes-long install never stalls the loop.
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
    current = input_hashes(target, command)
    with lock:
        previous = recorded(connection, target)
    reasons = _changed_reasons(previous, current)
    missing = missing_output(target, command)
    if missing:
        reasons.append(missing)
    if not reasons:
        return DependencyCheck("current", command)
    try:
        completed = await asyncio.to_thread(
            run, list(command), cwd=str(target), env=install_environment(environ, extra_secrets=extra_secrets),
            capture_output=True, text=True, timeout=timeout_seconds, check=False,
        )
        returncode = int(completed.returncode)
        output = (completed.stderr or completed.stdout or "").strip()
    except FileNotFoundError as exc:
        returncode, output = 127, str(exc)
    except subprocess.TimeoutExpired:
        returncode, output = 124, f"timed out after {timeout_seconds:.0f}s"
    except OSError as exc:
        returncode, output = 126, str(exc)
    if returncode == 0:
        # Hash again: the install may rewrite its own lockfile (pnpm, uv).
        with lock:
            record(connection, target, command, status="installed", project=project)
        return DependencyCheck("installed", command, tuple(reasons), returncode=0)
    from harbor.url_credentials import redact_userinfo
    error = redact_userinfo(output[-_ERROR_CHARS:]) or f"exit code {returncode} with no output"
    with lock:
        record(connection, target, command, status="failed", inputs=current, error=error, project=project)
    return DependencyCheck("failed", command, tuple(reasons), error, returncode)


def builder_note(check: DependencyCheck) -> str:
    """The text Harbor appends to every builder prompt when it manages installs."""

    return "\n".join((
        "Harbor dependencies: before each turn Harbor checks whether the lockfile or package.json changed "
        "since its last install, or the install output (node_modules) is missing, and if so runs the "
        f"project's install command (`{check.command_text}`) outside your sandbox. "
        f"Before this turn Harbor {check.line()}.",
        "Do not run install or reinstall commands yourself (pnpm install, npm ci, npm install, yarn install, "
        "or anything else that deletes or rewrites node_modules): the sandbox cannot write some files inside "
        "node_modules, so an install from inside it can leave this worktree unrunnable. To change dependencies, "
        "edit package.json and update only the lockfile (for example `pnpm install --lockfile-only` or "
        "`npm install --package-lock-only`). If the lockfile changes during this turn, including through a "
        "merge, finish the turn: Harbor installs before the next one.",
    ))


def blocker(check: DependencyCheck, worktree: str | Path, ticket: str) -> dict[str, str]:
    """The structured blocker for a turn Harbor refused to start."""

    return {
        "kind": "env_blocker",
        "question": (f"Harbor's pre-turn dependency install `{check.command_text}` failed in {worktree} "
                     f"(exit code {check.returncode}), so the builder turn was not started. "
                     "How should the install be fixed?"),
        "recommendation": (f"Run `{check.command_text}` in {worktree} outside the sandbox, fix what it reports "
                           "(the lockfile, the registry, or the install_command), then answer this blocker "
                           f"(/answer, or a comment on {ticket or 'the ticket'}). "
                           "Harbor re-runs the install before the resumed turn."),
        "evidence": check.error,
    }


def pre_turn_installer(manager: Any, db: Any, *, project: str = "",
                       extra_secrets: Iterable[str] = ()) -> Callable[[Path], Awaitable[DependencyCheck]] | None:
    """A per-turn installer bound to ``manager``'s install command, or ``None`` when it has none.

    ``db`` is Harbor's ledger (``harbor.db.HarborDB``) or a bare connection;
    ledger access takes the database's own lock when it has one.
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
                                    root=root, timeout_seconds=timeout, extra_secrets=secrets, lock=lock)

    return prepare


def log(ticket: str, check: DependencyCheck) -> None:
    print(f"Harbor: dependencies for {ticket}: {check.line()}", file=sys.stderr)


__all__ = [
    "DEPENDENCY_INPUTS", "DependencyCheck", "InstallRecord", "input_hashes", "HARBOR_SECRET_NAMES", "blocker", "builder_note",
    "dependency_inputs", "fingerprint", "install_environment", "missing_output", "pre_turn_installer",
    "prepare_dependencies", "record", "recorded",
]
