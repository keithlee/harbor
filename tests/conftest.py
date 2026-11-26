from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import env_isolation  # noqa: E402


@pytest.fixture(scope="session")
def _isolated_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return env_isolation.make_home(tmp_path_factory.mktemp("isolated"))


@pytest.fixture(autouse=True)
def _isolate_developer_environment(monkeypatch: pytest.MonkeyPatch, _isolated_home: Path) -> Path:
    """Seal every test off from the developer's environment (see tests/env_isolation.py)."""

    env_isolation.isolate(monkeypatch, _isolated_home)
    return _isolated_home


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path_factory, monkeypatch, _isolate_developer_environment):
    """Never read the developer's own project registry.

    The production runner factory needs a registry to build a Claude builder's
    permission rules. Without this, tests silently used whatever
    ``HARBOR_PROJECTS_FILE`` the shell exported. Tests that need a real
    registry, or none, set or delete the variable themselves.  It depends on
    the isolation fixture so the variable is set after that fixture clears
    every ``HARBOR_*`` variable.
    """

    registry = tmp_path_factory.mktemp("registry") / "projects.md"
    registry.write_text("", encoding="utf-8")
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))
