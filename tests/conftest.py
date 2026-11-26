from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path_factory, monkeypatch):
    """Never read the developer's own project registry.

    The production runner factory needs a registry to build a Claude builder's
    permission rules. Without this, tests silently used whatever
    ``HARBOR_PROJECTS_FILE`` the shell exported. Tests that need a real
    registry, or none, set or delete the variable themselves.
    """

    registry = tmp_path_factory.mktemp("registry") / "projects.md"
    registry.write_text("", encoding="utf-8")
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))
