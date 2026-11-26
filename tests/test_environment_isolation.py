"""Regression guard: the suite never sees the developer's Harbor environment.

Before the autouse isolation fixture in ``conftest.py``, exported variables
such as ``HARBOR_PROJECTS_FILE``, ``HARBOR_DB_PATH`` or
``HARBOR_CLAUDE_ATTRIBUTION`` changed test results (the Claude golden command
tests failed with ``HARBOR_CLAUDE_ATTRIBUTION`` set), and ``~`` resolved to the
developer's real home, so defaults pointed at the live ``~/.harbor``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from env_isolation import REAL_HOME, is_isolated_env_var


def test_no_harbor_or_credential_variable_reaches_a_test() -> None:
    leaked = sorted(name for name in os.environ if is_isolated_env_var(name))
    assert leaked == [], f"developer environment leaked into the suite: {leaked}"


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
    probe = ("import os, pathlib; "
             "print(sorted(k for k in os.environ if k.startswith('HARBOR_') "
             "or k in ('LINEAR_API_KEY', 'GITHUB_TOKEN', 'GH_TOKEN'))); "
             "print(pathlib.Path.home().resolve())")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout.splitlines()
    assert out[0] == "[]"
    assert not Path(out[1]).is_relative_to(REAL_HOME)


def test_suite_result_does_not_depend_on_exported_harbor_variables(tmp_path: Path) -> None:
    """Run a variable-sensitive test file with dummy developer variables exported."""

    dummy = tmp_path / "dummy"
    env = {**os.environ,
           "HARBOR_PROJECTS_FILE": str(dummy / "projects.md"), "HARBOR_DB_PATH": str(dummy / "harbor.db"),
           "HARBOR_WORKTREE_ROOT": str(dummy / "wt"), "HARBOR_CLAUDE_ATTRIBUTION": "off",
           "HARBOR_CLAUDE_EXECUTABLE": "/nonexistent/claude", "HARBOR_GITHUB_TOKEN_ENV": "GH_TOKEN",
           "HARBOR_GITHUB_API_URL": "https://ghe.dummy.invalid/api/v3", "LINEAR_API_KEY": "dummy-linear",
           "GITHUB_TOKEN": "dummy-github", "GH_TOKEN": "dummy-gh"}
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/golden/test_golden_runner_commands.py", "tests/test_environment_isolation.py::"
         "test_no_harbor_or_credential_variable_reaches_a_test"],
        cwd=root, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not dummy.exists()
