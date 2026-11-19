"""The free-disk floor: one default for every project, overridable per project."""

from __future__ import annotations

from contextlib import closing
import subprocess
from pathlib import Path

import pytest

from harbor.cli import build_production_dispatcher
from harbor.config.pipeline import PipelineConfig
from harbor.config.projects import ProjectConfig, get_project
from harbor.db import HarborDB
from harbor.worktree import DEFAULT_DISK_FLOOR_GB, DiskFloor, WorktreeManager
from workflow_support import materialize


PIPELINE = """# Pipeline
linear_project_slug: sample-project
linear_team: SAMPLE
install_command: none
{floor}
"""


def registry(tmp_path: Path, floor_line: str) -> Path:
    repo = tmp_path / "SampleProject"
    repo.mkdir()
    materialize(repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text(PIPELINE.format(floor=floor_line), encoding="utf-8")
    projects = tmp_path / "projects.md"
    projects.write_text(
        "| name | repo_path | pipeline_path | linear_project_slug | linear_team |\n"
        "| --- | --- | --- | --- | --- |\n"
        f"| SampleProject | {repo} | {pipeline} | sample-project | SAMPLE |\n",
        encoding="utf-8",
    )
    return projects


def test_every_project_defaults_to_the_five_gigabyte_floor(tmp_path: Path, monkeypatch) -> None:
    assert DEFAULT_DISK_FLOOR_GB == 5.0
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry(tmp_path, "")))
    assert get_project("SampleProject").disk_floor_gb == 5.0
    assert PipelineConfig.from_markdown("skill: /work-pipeline").disk_floor_gb == 5.0


@pytest.mark.parametrize("line", ["disk_floor_gb: 12", "disk_floor: 12", "| disk_floor_gb | 12 |", "disk_floor_gb: `12` # large repo"])
def test_a_project_pipeline_overrides_the_floor(tmp_path: Path, monkeypatch, line: str) -> None:
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry(tmp_path, line)))
    assert get_project("SampleProject").disk_floor_gb == 12.0


@pytest.mark.parametrize("value", ["abc", "0", "-3", "nan", "inf"])
def test_an_invalid_floor_fails_closed_instead_of_using_the_default(tmp_path: Path, monkeypatch, value: str) -> None:
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry(tmp_path, f"disk_floor_gb: {value}")))
    with pytest.raises(ValueError, match="disk_floor_gb must be a positive number of GB"):
        get_project("SampleProject")


def test_the_cli_worktree_manager_enforces_the_project_floor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry(tmp_path, "disk_floor_gb: 12")))
    config = get_project("SampleProject")
    with closing(HarborDB(tmp_path / "ledger.sqlite")) as db:
        _, manager, _ = build_production_dispatcher(config, db=db)
        assert isinstance(manager, WorktreeManager) and manager.disk_floor_gb == 12.0

        manager._disk_usage = lambda _: 11.5
        with pytest.raises(DiskFloor, match="free disk floor is 12.00 GB; only 11.50 GB is available"):
            manager.create("SAMPLE-1")


@pytest.mark.parametrize("line", [
    "disk_floor_gb:", "disk_floor:", "disk_floor_gb: ``", "disk_floor_gb: \"\"",
    "| disk_floor_gb | |", "| disk_floor | |", "disk_floor_gb: # required value",
])
def test_explicitly_blank_floor_is_rejected(tmp_path, monkeypatch, line):
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry(tmp_path, line)))
    with pytest.raises(ValueError, match="disk_floor_gb must be a positive number of GB"):
        get_project("SampleProject")


def test_pipeline_conversion_preserves_floor(tmp_path, monkeypatch):
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry(tmp_path, "disk_floor_gb: 12")))
    config = get_project("SampleProject")
    converted = config.pipeline_config
    assert converted.disk_floor_gb == 12
    assert ProjectConfig(name="Demo", linear_project_slug="demo", disk_floor_gb=12).pipeline_config.disk_floor_gb == 12
