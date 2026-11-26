"""Regression guard: the suite never sees the developer's Harbor environment.

Before the autouse isolation fixture in ``conftest.py``, exported variables
such as ``HARBOR_PROJECTS_FILE``, ``HARBOR_DB_PATH`` or
``HARBOR_CLAUDE_ATTRIBUTION`` changed test results (the Claude golden command
tests failed with ``HARBOR_CLAUDE_ATTRIBUTION`` set), and ``~`` resolved to the
developer's real home, so defaults pointed at the live ``~/.harbor``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from env_isolation import ISOLATED_ENV_VARS, REAL_HOME, UV_DIRS, is_isolated_env_var

ROOT = Path(__file__).resolve().parents[1]


def assert_isolated_registry(value: str | None) -> None:
    # conftest's _isolated_registry points HARBOR_PROJECTS_FILE at an empty temp
    # registry after the isolation fixture runs; anything else is a leak.
    assert value, "HARBOR_PROJECTS_FILE must point at the isolated empty registry"
    registry = Path(value)
    assert registry.is_file() and registry.read_text(encoding="utf-8") == ""
    assert not registry.resolve().is_relative_to(REAL_HOME)


def test_no_harbor_or_credential_variable_reaches_a_test() -> None:
    leaked = sorted(name for name in os.environ if is_isolated_env_var(name) and name != "HARBOR_PROJECTS_FILE")
    assert leaked == [], f"developer environment leaked into the suite: {leaked}"
    assert_isolated_registry(os.environ.get("HARBOR_PROJECTS_FILE"))


def test_home_and_harbor_defaults_resolve_outside_the_real_home() -> None:
    from harbor import cli

    home = Path.home().resolve()
    assert home != REAL_HOME
    assert not cli._db_path().resolve().is_relative_to(REAL_HOME)

    class Config:
        name = "SampleProject"
        worktree_root = None

    assert cli._worktree_root(Config()).is_relative_to(home)


def test_a_subprocess_inherits_the_sealed_environment() -> None:
    probe = ("import json, os, pathlib, sys; "
             "names = set(json.loads(sys.argv[1])); "
             "print(json.dumps(sorted(k for k in os.environ if (k.startswith('HARBOR_') "
             "and k not in ('HARBOR_UPDATE_GOLDEN', 'HARBOR_PROJECTS_FILE')) "
             "or k in names or k.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))))); "
             "print(pathlib.Path.home().resolve()); "
             "print(os.environ.get('HARBOR_PROJECTS_FILE', ''))")
    out = subprocess.run([sys.executable, "-c", probe, json.dumps(list(ISOLATED_ENV_VARS))],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    assert json.loads(out[0]) == []
    assert not Path(out[1]).is_relative_to(REAL_HOME)
    assert_isolated_registry(out[2] if len(out) > 2 else None)


def test_git_uses_a_test_identity_that_a_command_can_still_override(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()

    assert git("config", "--global", "user.name") == "Harbor Test"
    assert git("-c", "user.name=Override", "config", "user.name") == "Override"
    assert not Path(os.environ["GIT_CONFIG_GLOBAL"]).is_relative_to(REAL_HOME)


def test_uv_keeps_the_developer_cache_and_managed_pythons() -> None:
    # Moving HOME must not make ``uv build`` re-download build backends (or fail offline).
    if shutil.which("uv"):
        assert {"UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR"} <= UV_DIRS.keys()
    for name, value in UV_DIRS.items():
        assert os.environ[name] == value


def test_test_control_variables_are_not_isolated() -> None:
    assert not is_isolated_env_var("HARBOR_UPDATE_GOLDEN")
    assert is_isolated_env_var("HARBOR_PROJECTS_FILE")
    assert is_isolated_env_var("GIT_DIR") and is_isolated_env_var("GIT_CONFIG_KEY_0")


def test_harbor_update_golden_still_regenerates_snapshots(tmp_path: Path) -> None:
    """``HARBOR_UPDATE_GOLDEN=1 uv run pytest tests/golden`` must survive the isolation fixture."""

    copy = tmp_path / "repo"
    (copy / "tests" / "golden" / "snapshots").mkdir(parents=True)
    (copy / "src").symlink_to(ROOT / "src")
    for name in ("tests/conftest.py", "tests/env_isolation.py", "tests/golden/conftest.py",
                 "tests/golden/test_golden_runner_commands.py"):
        shutil.copy(ROOT / name, copy / name)
    snapshot = "tests/golden/snapshots/argv_claude_builder_fresh.json"
    (copy / snapshot).write_text("corrupt\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/golden/test_golden_runner_commands.py::test_claude_builder_fresh_command"],
        cwd=copy, env={**os.environ, "HARBOR_UPDATE_GOLDEN": "1"}, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (copy / snapshot).read_text(encoding="utf-8") == (ROOT / snapshot).read_text(encoding="utf-8")


def test_suite_result_does_not_depend_on_exported_harbor_variables(tmp_path: Path) -> None:
    """Run a variable-sensitive test file with dummy developer variables exported."""

    dummy = tmp_path / "dummy"
    env = {**os.environ,
           "HARBOR_PROJECTS_FILE": str(dummy / "projects.md"), "HARBOR_DB_PATH": str(dummy / "harbor.db"),
           "HARBOR_CLAUDE_ATTRIBUTION": "off",
           "HARBOR_CLAUDE_EXECUTABLE": "/nonexistent/claude", "HARBOR_GITHUB_TOKEN_ENV": "GH_TOKEN",
           "HARBOR_GITHUB_API_URL": "https://ghe.dummy.invalid/api/v3", "LINEAR_API_KEY": "dummy-linear",
           "GITHUB_TOKEN": "dummy-github", "GH_TOKEN": "dummy-gh"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
         "tests/golden/test_golden_runner_commands.py", "tests/test_environment_isolation.py::"
         "test_no_harbor_or_credential_variable_reaches_a_test"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    # Every selected test ran (none skipped or deselected), including the leak check.
    summary = result.stdout.strip().splitlines()[-1]
    assert re.fullmatch(r"[1-9][0-9]* passed in [0-9.]+s", summary), result.stdout
