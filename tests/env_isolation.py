"""Seal the test suite off from the developer's environment.

``conftest.py`` calls :func:`isolate` from an autouse fixture.  Without it, a
developer who exports ``HARBOR_PROJECTS_FILE``, ``HARBOR_DB_PATH``, a token,
etc. gets different results from a fresh clone or CI, and tests could resolve
the live registry, ledger or ``~/.harbor`` worktree root.  Tests that need a
variable set it explicitly with ``monkeypatch.setenv`` or a subprocess ``env``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# The developer's real home, captured before any test runs, so the isolation
# guard can prove that no test resolves ``~`` (and so ``~/.harbor``) to it.
REAL_HOME = Path.home().resolve()

# Variables Harbor reads (or hands to builders) that would otherwise leak from
# the developer's shell into the suite: registry/ledger/worktree locations,
# executables, GitHub/Linear endpoints and credentials.  Every ``HARBOR_*``
# variable is cleared as well, whatever its name, except TEST_CONTROL_ENV_VARS.
ISOLATED_ENV_VARS = (
    "LINEAR_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GH_HOST",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_OWNER",
    "GITHUB_REPOSITORY",
    "CODEX_HOME",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    # Home-derived config.  XDG_* then fall back to the temporary HOME.
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    # Git: a pre-commit hook that runs pytest exports GIT_DIR/GIT_INDEX_FILE,
    # which would send the tests' ``git init``/``commit`` into the real repo.
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)
_GIT_CONFIG_ENTRY = re.compile(r"GIT_CONFIG_(KEY|VALUE)_\d+")

# HARBOR_* variables that steer the test suite itself rather than Harbor, and
# so must reach the tests.  ``HARBOR_UPDATE_GOLDEN=1`` regenerates snapshots
# (see tests/golden/conftest.py).
TEST_CONTROL_ENV_VARS = frozenset({"HARBOR_UPDATE_GOLDEN"})

TEST_GIT_IDENTITY = "[user]\n\tname = Harbor Test\n\temail = harbor-test@example.test\n"


def is_isolated_env_var(name: str) -> bool:
    if name in TEST_CONTROL_ENV_VARS:
        return False
    return name.startswith("HARBOR_") or name in ISOLATED_ENV_VARS or bool(_GIT_CONFIG_ENTRY.fullmatch(name))


def _uv_dir(env_name: str, *args: str) -> str | None:
    """Where uv keeps a directory now, so moving HOME does not lose it."""

    if os.environ.get(env_name):
        return os.environ[env_name]
    uv = shutil.which("uv")
    if not uv:
        return None
    try:
        result = subprocess.run([uv, *args], capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


# Tests that build or install with uv need the developer's existing uv cache and
# managed Pythons; a fresh HOME would re-download them (and fail offline).
UV_DIRS = {
    name: value
    for name, value in (
        ("UV_CACHE_DIR", _uv_dir("UV_CACHE_DIR", "cache", "dir")),
        ("UV_PYTHON_INSTALL_DIR", _uv_dir("UV_PYTHON_INSTALL_DIR", "python", "dir")),
    )
    if value
}


def make_home(root: Path) -> Path:
    """A throwaway HOME with a git identity, created once per session.

    One shared directory rather than one per test: on macOS ``/usr/bin/git``
    is an ``xcrun`` shim whose lookup cache misses for every new HOME, adding
    about 45 ms to each git call and roughly 10 s to the suite.
    """

    home = root / "home"
    home.mkdir()
    # The real ~/.gitconfig is out of reach, so give git a test identity.  It is
    # a global config file, so a command's own ``-c user.name=...`` still wins.
    (home / ".gitconfig").write_text(TEST_GIT_IDENTITY, encoding="utf-8")
    return home


def isolate(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    for name in list(os.environ):
        if is_isolated_env_var(name):
            monkeypatch.delenv(name, raising=False)
    for name, value in UV_DIRS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
