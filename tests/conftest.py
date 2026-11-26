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
