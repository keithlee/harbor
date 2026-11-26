from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from env_isolation import is_isolated_env_var  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_developer_environment(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Seal every test off from the developer's environment.

    Without this, a developer who exports ``HARBOR_PROJECTS_FILE``,
    ``HARBOR_DB_PATH``, a token, etc. gets different results from a fresh
    clone or CI, and tests could resolve the live registry, ledger or
    ``~/.harbor`` worktree root.  Tests that need a variable set it explicitly
    with ``monkeypatch.setenv`` or a subprocess ``env``.
    """

    for name in list(os.environ):
        if is_isolated_env_var(name):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    # Git still needs an identity for test commits once the real ~/.gitconfig is out of reach.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in (("GIT_AUTHOR_NAME", "Harbor Test"), ("GIT_AUTHOR_EMAIL", "harbor-test@example.test"),
                       ("GIT_COMMITTER_NAME", "Harbor Test"), ("GIT_COMMITTER_EMAIL", "harbor-test@example.test")):
        monkeypatch.setenv(key, value)
    return home
