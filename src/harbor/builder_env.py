"""The builder environment ``harbor start`` settles once per project.

Every builder process (and every child agent it starts) inherits these
variables, so a builder runs plain ``harbor workflow ...`` instead of a
per-ticket shell prefix:

- ``HARBOR_PROJECT``: the project, the default for ``harbor workflow``.
- ``HARBOR_PROJECTS_FILE`` / ``HARBOR_DB_PATH``: the absolute registry and
  ledger this start uses.
- ``PATH``: a Harbor-owned ``bin`` directory first, holding a ``harbor`` shim
  that runs this exact Harbor install with that registry and ledger, so shell
  initialisation cannot select another ``harbor``.
- The GitHub token variable (``github_token_env``, then ``GITHUB_TOKEN`` /
  ``GH_TOKEN``), copied from Harbor's own environment. Its value is held only
  in memory and handed to the builder process; it is never written to the
  shim, config, ledger, prompt or logs.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SHIM_NAME = "harbor"


def harbor_source_root() -> Path:
    """The directory whose ``harbor`` package is this running Harbor."""

    return Path(__file__).resolve().parent.parent


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
    """Write the shim (idempotently) and return its path.

    Rewritten when its content differs, so a shim a builder edited is restored
    before the next builder process starts.
    """

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
    # The token value is never shown: not in repr, reports, or the shim.
    token_value: str | None = field(default=None, repr=False, compare=False)

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

    def variables(self) -> dict[str, str]:
        """The variables every builder process gets, overriding the spawn-time environment."""

        values = {
            "HARBOR_PROJECT": self.project,
            "HARBOR_PROJECTS_FILE": str(self.registry),
            "HARBOR_DB_PATH": str(self.db_path),
            "PATH": self.path,
        }
        if self.token_env and self.token_value:
            values[self.token_env] = self.token_value
        return values

    def report(self) -> list[str]:
        """``harbor doctor`` lines: names and paths only, never a secret value."""

        token = (f"{self.token_env} (value not shown)" if self.token_env else "none set")
        return [
            f"Builder environment: HARBOR_PROJECT={self.project} · HARBOR_PROJECTS_FILE={self.registry} · "
            f"HARBOR_DB_PATH={self.db_path}",
            f"Builder PATH: {self.bin_dir} first · GitHub token variable: {token}",
        ]


def resolve_builder_environment(
    config: Any,
    *,
    db_path: Path,
    environ: Mapping[str, str] | None = None,
    token_names: tuple[str, ...] = (),
) -> BuilderEnvironment:
    """Resolve the builder environment for ``config`` once, failing closed without a registry.

    ``token_names`` are the token variable names in preference order; the
    first one set is copied.
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
    bin_dir = ledger.parent / "bin" / digest
    token_env = next((name for name in dict.fromkeys(token_names) if name and env.get(name)), None)
    return BuilderEnvironment(
        project=str(config.name),
        registry=registry_path,
        db_path=ledger,
        bin_dir=bin_dir,
        shim_text=text,
        base_path=str(env.get("PATH") or os.defpath),
        token_env=token_env,
        token_value=env.get(token_env) if token_env else None,
    )


def check_resolution(builder: BuilderEnvironment, shells: tuple[str, ...] | None = None) -> list[tuple[str, str | None]]:
    """Which ``harbor`` each shell finds in the builder environment.

    Returns ``(shell, problem)`` pairs; ``problem`` is ``None`` when the shell
    resolves ``harbor`` to the shim and the shim runs this Harbor.
    """

    builder.ensure_shim()
    env = os.environ.copy()
    env.update(builder.variables())
    if shells is None:
        user_shell = os.environ.get("SHELL") or ""
        shells = tuple(dict.fromkeys(s for s in ("/bin/sh", user_shell) if s and Path(s).is_file()))
    results: list[tuple[str, str | None]] = []
    for shell in shells:
        try:
            found = subprocess.run(
                [shell, "-c", "command -v harbor && harbor --help"],
                env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            results.append((shell, f"could not run ({exc})"))
            continue
        first = (found.stdout.splitlines() or [""])[0].strip()
        if first != str(builder.shim):
            results.append((shell, f"resolves harbor to {first or 'nothing'}, not {builder.shim}"))
        elif found.returncode != 0 or "A calm project supervisor" not in found.stdout:
            results.append((shell, f"{builder.shim} did not run Harbor (exit {found.returncode})"))
        else:
            results.append((shell, None))
    return results
