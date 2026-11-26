"""Worktree root resolution: the `worktree_root` config key is the only override."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor.cli import _worktree_root
from harbor.config.projects import get_project
from workflow_support import materialize


def registry(tmp_path: Path, pipeline_line: str) -> Path:
    repo = tmp_path / "SampleProject"
    repo.mkdir()
    materialize(repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text(
        "# Pipeline\nlinear_project_slug: sample-project\nlinear_team: SAMPLE\ninstall_command: none\n"
        f"{pipeline_line}\n",
        encoding="utf-8",
    )
    projects = tmp_path / "projects.md"
    projects.write_text(
        "| name | repo_path | pipeline_path | linear_project_slug | linear_team |\n"
        "| --- | --- | --- | --- | --- |\n"
        f"| SampleProject | {repo} | {pipeline} | sample-project | SAMPLE |\n",
        encoding="utf-8",
    )
    return projects


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HARBOR_WORKTREE_ROOT", str(tmp_path / "from-env"))
    return home


def test_unset_worktree_root_uses_the_default_and_ignores_the_env_var(tmp_path: Path, monkeypatch, fake_home: Path) -> None:
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry(tmp_path, "")))
    config = get_project("SampleProject")
    assert config.worktree_root == ""
    assert _worktree_root(config) == (fake_home / ".harbor" / "wt" / "SampleProject").resolve()


def test_configured_worktree_root_wins_and_ignores_the_env_var(tmp_path: Path, monkeypatch, fake_home: Path) -> None:
    configured = tmp_path / "configured-wt"
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry(tmp_path, f"worktree_root: {configured}")))
    assert _worktree_root(get_project("SampleProject")) == configured.resolve()


def test_configured_worktree_root_expands_home(fake_home: Path) -> None:
    config = SimpleNamespace(name="SampleProject", worktree_root="~/elsewhere/wt")
    assert _worktree_root(config) == (fake_home / "elsewhere" / "wt").resolve()


def test_a_config_without_the_attribute_does_not_fall_back_to_the_env_var(tmp_path: Path, fake_home: Path) -> None:
    # Before the fallback was removed, a None/missing worktree_root read HARBOR_WORKTREE_ROOT.
    config = SimpleNamespace(name="SampleProject")
    assert _worktree_root(config) == (fake_home / ".harbor" / "wt" / "SampleProject").resolve()
    assert _worktree_root(config) != (tmp_path / "from-env").resolve()
