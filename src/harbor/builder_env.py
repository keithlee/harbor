"""The builder environment ``harbor start`` settles once per project.

Every builder process (and every child agent it starts) inherits these
variables, so a builder runs plain ``harbor workflow ...`` instead of a
per-ticket shell prefix:

- ``HARBOR_PROJECT``: the project, the default for ``harbor workflow``.
- ``HARBOR_PROJECTS_FILE`` / ``HARBOR_DB_PATH``: the absolute registry and
  ledger this start uses.
- ``PATH``: a Harbor-owned ``bin`` directory first, holding a ``harbor`` shim
  that runs this exact Harbor install with that registry and ledger.
- ``HARBOR_BIN``: that shim's absolute path. A login or interactive shell's
  startup files (``path_helper``, ``.zprofile``, ``.zshrc``) can reorder or
  replace ``PATH`` after Harbor sets it, so PATH order alone does not
  guarantee which ``harbor`` runs. ``harbor doctor`` checks plain, login and
  interactive shells, and the builder prompt tells builders to compare
  ``command -v harbor`` with ``$HARBOR_BIN`` and use ``"$HARBOR_BIN"`` when
  they differ.
- The GitHub token variables (``github_token_env``, then ``GITHUB_TOKEN`` /
  ``GH_TOKEN``): pinned to their values at start, and removed when unset
  then, so a token exported into Harbor's shell later never reaches builders.
  Values are held only in memory and handed to the builder process; they are
  never written to the shim, config, ledger, prompt or logs.

The shim lives under ``$XDG_STATE_HOME/harbor/builder-bin`` (default
``~/.local/state``), and resolution fails closed if that is inside a
directory builders can write (the ledger directory, worktree root or the
repository's git directory), so a builder can neither edit the shim nor plant
another executable in front of it.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SHIM_NAME = "harbor"
_RESOLVED_MARKER = "HARBOR-RESOLVED:"


def harbor_source_root() -> Path:
    """The directory whose ``harbor`` package is this running Harbor."""

    return Path(__file__).resolve().parent.parent


def shim_root(environ: Mapping[str, str] | None = None) -> Path:
    """Where Harbor keeps builder shims: ``$XDG_STATE_HOME/harbor/builder-bin``."""

    env = os.environ if environ is None else environ
    state = env.get("XDG_STATE_HOME") or str(Path(env.get("HOME") or Path.home()) / ".local" / "state")
    return Path(state).expanduser().resolve() / "harbor" / "builder-bin"


def shim_text(registry: Path, db_path: Path, *, python: str | None = None, source: Path | None = None) -> str:
    """A POSIX ``sh`` script that runs this Harbor install with this registry and ledger.

    ``-P`` keeps a ``harbor`` package in the builder's working directory from
    shadowing the real one, exactly as the old per-ticket prefix did.
    """

    python = python or sys.executable
    source = source or harbor_source_root()
    lines = [
        "#!/bin/sh",
        "# Written by harbor start: the Harbor install, registry and ledger this project's builders use.",
        f"HARBOR_PROJECTS_FILE={shlex.quote(str(registry))}; export HARBOR_PROJECTS_FILE",
        f"HARBOR_DB_PATH={shlex.quote(str(db_path))}; export HARBOR_DB_PATH",
        f"PYTHONPATH={shlex.quote(str(source))}; export PYTHONPATH",
        f'exec {shlex.quote(python)} -P -m harbor.cli "$@"',
        "",
    ]
    return "\n".join(lines)


def write_shim(bin_dir: Path, text: str) -> Path:
    """Write the shim (idempotently) and return its path."""

    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / SHIM_NAME
    try:
        current = shim.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != text:
        temporary = bin_dir / f".{SHIM_NAME}.tmp"
        temporary.write_text(text, encoding="utf-8")
        temporary.chmod(0o755)
        temporary.replace(shim)
    elif not os.access(shim, os.X_OK):
        shim.chmod(0o755)
    return shim


@dataclass(frozen=True)
class BuilderEnvironment:
    """The settings resolved once at ``harbor start``; see the module docstring."""

    project: str
    registry: Path
    db_path: Path
    bin_dir: Path
    shim_text: str
    base_path: str
    token_env: str | None = None
    # Each token variable's value at start (None: unset then). Never shown:
    # not in repr, reports or the shim.
    tokens: tuple[tuple[str, str | None], ...] = field(default=(), repr=False, compare=False)

    @property
    def shim(self) -> Path:
        return self.bin_dir / SHIM_NAME

    @property
    def path(self) -> str:
        rest = [part for part in self.base_path.split(os.pathsep) if part and part != str(self.bin_dir)]
        return os.pathsep.join((str(self.bin_dir), *rest))

    def ensure_shim(self) -> Path:
        try:
            return write_shim(self.bin_dir, self.shim_text)
        except OSError as exc:
            # A configuration error for the operator, not a traceback.
            raise ValueError(f"could not write the builders' harbor shim {self.shim} ({exc})") from exc

    def variables(self) -> dict[str, str | None]:
        """The variables every builder process gets; ``None`` removes one."""

        values: dict[str, str | None] = {
            "HARBOR_PROJECT": self.project,
            "HARBOR_PROJECTS_FILE": str(self.registry),
            "HARBOR_DB_PATH": str(self.db_path),
            "HARBOR_BIN": str(self.shim),
            "PATH": self.path,
        }
        values.update(dict(self.tokens))
        return values

    def report(self) -> list[str]:
        """``harbor doctor`` lines: names and paths only, never a secret value."""

        token = (f"{self.token_env} (value not shown)" if self.token_env else "none set")
        return [
            f"Builder environment: HARBOR_PROJECT={self.project} · HARBOR_PROJECTS_FILE={self.registry} · "
            f"HARBOR_DB_PATH={self.db_path}",
            f"Builder PATH: {self.bin_dir} first · HARBOR_BIN={self.shim} · GitHub token variable: {token}",
        ]


def resolve_builder_environment(
    config: Any,
    *,
    db_path: Path,
    environ: Mapping[str, str] | None = None,
    token_names: Sequence[str] = (),
    writable_roots: Sequence[str | Path] = (),
) -> BuilderEnvironment:
    """Resolve the builder environment for ``config`` once.

    Fails closed without a registry, or when the shim directory would be
    inside one of ``writable_roots`` (directories a builder can write).
    ``token_names`` are the token variable names in preference order; each is
    pinned to its current value (or to unset), and the first one set is named
    in reports.
    """

    env = os.environ if environ is None else environ
    registry = getattr(config, "registry_path", None) or env.get("HARBOR_PROJECTS_FILE")
    if not registry or not Path(registry).expanduser().is_file():
        raise ValueError("workflow delivery requires its authoritative project registry")
    registry_path = Path(registry).expanduser().resolve()
    ledger = Path(db_path).expanduser().resolve()
    text = shim_text(registry_path, ledger)
    # One directory per distinct install/registry/ledger, so two projects (or
    # two Harbor installs) never overwrite each other's shim.
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    bin_dir = shim_root(env) / digest
    for root in (ledger.parent, *writable_roots):
        resolved_root = Path(root).expanduser().resolve()
        if bin_dir == resolved_root or bin_dir.is_relative_to(resolved_root):
            raise ValueError(
                f"the builders' harbor shim directory {bin_dir} is inside {resolved_root}, which builders can write; "
                "set XDG_STATE_HOME to a directory outside the ledger directory, worktree root and repository"
            )
    names = tuple(dict.fromkeys(name for name in token_names if name))
    tokens = tuple((name, env.get(name) or None) for name in names)
    token_env = next((name for name, value in tokens if value), None)
    return BuilderEnvironment(
        project=str(config.name),
        registry=registry_path,
        db_path=ledger,
        bin_dir=bin_dir,
        shim_text=text,
        base_path=str(env.get("PATH") or os.defpath),
        token_env=token_env,
        tokens=tokens,
    )


def shell_modes(user_shell: str | None = None) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The shells a builder's commands may run in: plain ``sh``, and ``$SHELL`` plain, login and interactive.

    Codex runs commands through a login shell; Claude Code runs Bash from a
    snapshot of a login, interactive shell. Each mode reads different startup
    files, any of which can change PATH.
    """

    modes: list[tuple[str, tuple[str, ...]]] = [("/bin/sh", ("-c",))]
    shell = user_shell if user_shell is not None else os.environ.get("SHELL", "")
    if shell and Path(shell).is_file():
        modes += [(shell, ("-c",)), (shell, ("-l", "-c")), (shell, ("-i", "-c"))]
    return tuple(dict.fromkeys(modes))


def check_resolution(
    builder: BuilderEnvironment,
    modes: Sequence[tuple[str, Sequence[str]]] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[tuple[str, str | None]]:
    """Which ``harbor`` each shell mode finds in the builder environment.

    Returns ``(label, problem)`` pairs, such as ``("/bin/zsh -lc", None)``;
    ``problem`` is ``None`` when the shell resolves ``harbor`` to the shim and
    the shim runs this Harbor.
    """

    builder.ensure_shim()
    env = dict(os.environ if environ is None else environ)
    for name, value in builder.variables().items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    script = f"printf '{_RESOLVED_MARKER}%s\\n' \"$(command -v harbor)\"; harbor --help"
    results: list[tuple[str, str | None]] = []
    for shell, flags in (shell_modes() if modes is None else modes):
        label = f"{shell} -{''.join(flag.lstrip('-') for flag in flags)}"  # e.g. "/bin/zsh -lc"
        try:
            found = subprocess.run(
                [shell, *flags, script],
                env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            results.append((label, f"could not run ({exc})"))
            continue
        resolved = next((line[len(_RESOLVED_MARKER):].strip() for line in found.stdout.splitlines()
                         if line.startswith(_RESOLVED_MARKER)), "")
        if resolved != str(builder.shim):
            results.append((label, f"resolves harbor to {resolved or 'nothing'}, not {builder.shim}"))
        elif "A calm project supervisor" not in found.stdout:
            results.append((label, f"{builder.shim} did not run Harbor (exit {found.returncode})"))
        else:
            results.append((label, None))
    return results
