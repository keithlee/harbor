from __future__ import annotations

from pathlib import Path

import pytest

from harbor.config.profile import PipelineProfile
from harbor.config.projects import get_project
from workflow_support import materialize


def _pipeline(
    path: Path,
    *,
    slug: str = "sample-project-abc123",
    team: str = "SAMPLE",
    extra: tuple[str, ...] = (),
) -> Path:
    path.write_text(
        "\n".join(
            [
                "# Sample pipeline",
                f"linear_project_slug: {slug}" if slug else "",
                f"linear_team: {team}" if team else "",
                "poll_states: [Todo, In Progress]",
                "dispatch_states: [Todo]",
                *extra,
            ]
        ),
        encoding="utf-8",
    )
    return path


def _registry(path: Path, rows: list[str]) -> Path:
    path.write_text(
        "\n".join(
            [
                "# Harbor projects",
                "",
                "| name | repo_path | pipeline_path | linear_project_slug | linear_team | github_repository | worktree_root |",
                "| --- | --- | --- | --- | --- | --- | --- |",
                *rows,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_empty_registry_never_resolves_an_unregistered_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path / "projects.md", [])
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))
    legacy_name = "Book" + "gen"
    monkeypatch.setenv("HARBOR_" + legacy_name.upper() + "_PATH", str(tmp_path / "sentinel"))

    with pytest.raises(ValueError, match=f"unknown Harbor project: {legacy_name}"):
        get_project(legacy_name)


def test_explicit_missing_registry_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_registry = _registry(tmp_path / "projects.md", [])
    assert local_registry.is_file()
    selected = tmp_path / "selected-but-missing.md"
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(selected))

    with pytest.raises(ValueError, match=str(selected)):
        get_project("SampleProject", config_root=tmp_path)


def test_two_registered_projects_keep_identity_and_pipeline_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha_repo = tmp_path / "alpha"
    beta_repo = tmp_path / "beta"
    alpha_repo.mkdir()
    materialize(alpha_repo)
    beta_repo.mkdir()
    materialize(beta_repo)
    alpha_pipeline = _pipeline(tmp_path / "alpha-pipeline.md", slug="alpha-a1", team="ALPHA")
    beta_pipeline = _pipeline(tmp_path / "beta-pipeline.md", slug="beta-b2", team="BETA")
    registry = _registry(
        tmp_path / "projects.md",
        [
            f"| Alpha | {alpha_repo} | {alpha_pipeline} | alpha-a1 | ALPHA | acme/alpha | {tmp_path / 'wt-alpha'} |",
            f"| Beta | {beta_repo} | {beta_pipeline} | beta-b2 | BETA | acme/beta | {tmp_path / 'wt-beta'} |",
        ],
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))

    alpha = get_project("Alpha")
    beta = get_project("Beta")

    assert (alpha.repo_path, alpha.pipeline_path, alpha.linear_project_slug) == (
        alpha_repo,
        alpha_pipeline,
        "alpha-a1",
    )
    assert (beta.repo_path, beta.pipeline_path, beta.linear_project_slug) == (
        beta_repo,
        beta_pipeline,
        "beta-b2",
    )
    assert (alpha.linear_team, alpha.github_repository, alpha.worktree_root) == (
        "ALPHA",
        "acme/alpha",
        str(tmp_path / "wt-alpha"),
    )
    assert (beta.linear_team, beta.github_repository, beta.worktree_root) == (
        "BETA",
        "acme/beta",
        str(tmp_path / "wt-beta"),
    )


def test_registry_identity_overrides_conflicting_pipeline_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    materialize(repo)
    pipeline = _pipeline(tmp_path / "pipeline.md", slug="pipeline-shared", team="PIPE")
    registry = _registry(
        tmp_path / "projects.md",
        [f"| Alpha | {repo} | {pipeline} | registry-alpha | ALPHA | | |"],
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))

    config = get_project("Alpha")

    assert config.linear_project_slug == "registry-alpha"
    assert config.linear_team == "ALPHA"


def test_registry_preserves_additional_linear_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    materialize(repo)
    pipeline = _pipeline(
        tmp_path / "pipeline.md",
        slug="alpha-primary",
        team="ALPHA",
        extra=("linear_projects: [alpha-secondary, alpha-third, alpha-secondary]",),
    )
    registry = _registry(
        tmp_path / "projects.md",
        [
            f"| Alpha | {repo} | {pipeline} | alpha-primary | ALPHA | | |",
        ],
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))

    assert get_project("Alpha").linear_project_scope == (
        "alpha-primary",
        "alpha-secondary",
        "alpha-third",
    )


@pytest.mark.parametrize(
    ("slug", "team", "message"),
    [
        ("", "SAMPLE", "SampleProject is missing required linear_project_slug"),
        ("sample-project-abc123", "", "SampleProject is missing required linear_team"),
    ],
)
def test_required_linear_identity_never_inherits_product_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slug: str,
    team: str,
    message: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    materialize(repo)
    pipeline = _pipeline(tmp_path / "pipeline.md", slug=slug, team=team)
    registry = _registry(
        tmp_path / "projects.md",
        [f"| SampleProject | {repo} | {pipeline} | {slug} | {team} | | |"],
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))

    with pytest.raises(ValueError, match=message):
        get_project("SampleProject")


def test_missing_pipeline_fails_with_the_registered_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    materialize(repo)
    missing = tmp_path / "missing-pipeline.md"
    registry = _registry(
        tmp_path / "projects.md",
        [f"| SampleProject | {repo} | {missing} | sample-project-abc123 | SAMPLE | | |"],
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))

    with pytest.raises(ValueError, match=str(missing)):
        get_project("SampleProject")


def test_github_repository_is_optional_for_linear_only_supervision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    materialize(repo)
    pipeline = _pipeline(tmp_path / "pipeline.md")
    registry = _registry(
        tmp_path / "projects.md",
        [f"| SampleProject | {repo} | {pipeline} | sample-project-abc123 | SAMPLE | | |"],
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))

    assert get_project("SampleProject").github_repository == ""


def test_linear_only_project_ignores_ambient_github_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    materialize(repo)
    pipeline = _pipeline(tmp_path / "pipeline.md")
    registry = _registry(
        tmp_path / "projects.md",
        [f"| SampleProject | {repo} | {pipeline} | sample-project-abc123 | SAMPLE | | |"],
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("GITHUB_REPOSITORY", "foreign/repository")
    monkeypatch.setenv("GITHUB_OWNER", "foreign")

    profile = PipelineProfile.from_project(get_project("SampleProject"))

    assert profile.repository_owner == ""
    assert profile.repository_name == ""
