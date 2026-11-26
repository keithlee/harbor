"""One precedence for the GitHub settings a registry row and a pipeline share.

Regression: ``github_repository``, ``required_checks`` and ``bot_rework_cap``
may be set in the project registry and in ``.harbor/pipeline.md``.
``ProjectConfig`` let the registry win, but ``PipelineProfile.from_project``
re-read the pipeline file and let it win, so the builder's
``HARBOR_GITHUB_REPOSITORY`` and the GitHub poller could name different
repositories.  Registry cells also reached ``ProjectConfig`` unparsed:
``[CI, tests]`` became a tuple of characters, ``3`` stayed a string, and an
empty cell blanked the pipeline's value.  ``harbor workflow check`` handed the
raw ``ProjectConfig`` to its poller instead of the resolved profile.

The documented rule, as for ``github_api_url``: registry -> pipeline ->
default, and an empty registry cell is ignored.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor.cli import builder_github
from harbor.config.profile import PipelineProfile
from harbor.config.projects import get_project

PIPELINE_GITHUB = (
    "github_repository: pipe/repo\n"
    "required_checks: [pipe-ci]\n"
    "bot_rework_cap: 2\n"
)


def registry_project(tmp_path: Path, monkeypatch, registry: dict[str, str], pipeline: str = ""):
    from workflow_support import materialize

    repo = tmp_path / "repo"
    repo.mkdir()
    materialize(repo)
    pipeline_path = tmp_path / "pipeline.md"
    pipeline_path.write_text(f"# Pipeline\nlinear_project_slug: sample\nlinear_team: SAMPLE\n{pipeline}\n")
    projects = tmp_path / "projects.md"
    projects.write_text(
        "| name | repo_path | pipeline_path |" + "".join(f" {key} |" for key in registry) + "\n"
        + "| --- | --- | --- |" + " --- |" * len(registry) + "\n"
        + f"| SampleProject | {repo} | {pipeline_path} |" + "".join(f" {value} |" for value in registry.values()) + "\n"
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(projects))
    return get_project("SampleProject")


def builder_repository(config) -> str | None:
    github = builder_github(config, SimpleNamespace(config={}), "codex", env={})
    return github["env"].get("HARBOR_GITHUB_REPOSITORY")


def test_the_registry_repository_wins_everywhere(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch, {"github_repository": "reg/repo"}, PIPELINE_GITHUB)
    profile = PipelineProfile.from_project(config)
    assert config.github_repository == "reg/repo"
    assert (profile.repository_owner, profile.repository_name) == ("reg", "repo")
    assert builder_repository(config) == "reg/repo"


def test_a_pipeline_owner_key_does_not_split_the_registry_repository(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch, {"github_repository": "reg/repo"},
                              "github_owner: pipe\ngithub_repository: pipe/repo\n")
    assert PipelineProfile.from_project(config).repository == "reg/repo"


def test_the_github_repo_alias_is_read_from_the_registry(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch, {"github_repo": "reg/repo"}, PIPELINE_GITHUB)
    assert config.github_repository == "reg/repo"
    assert PipelineProfile.from_project(config).repository == "reg/repo"


def test_registry_required_checks_are_parsed_and_win(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch,
                              {"required_checks": '[reg-ci, "Lint, typecheck and test (root)"]'}, PIPELINE_GITHUB)
    expected = ("reg-ci", "Lint, typecheck and test (root)")
    assert config.required_checks == expected
    assert config.pipeline_config.required_checks == expected
    assert PipelineProfile.from_project(config).required_checks == expected


def test_a_registry_bot_rework_cap_is_an_int_and_wins(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch, {"bot_rework_cap": "3"}, PIPELINE_GITHUB)
    assert config.bot_rework_cap == 3
    assert PipelineProfile.from_project(config).bot_rework_cap == 3


def test_empty_registry_cells_keep_the_pipeline_values(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(
        tmp_path, monkeypatch,
        {"github_repository": "", "github_repo": "", "required_checks": "", "bot_rework_cap": ""},
        PIPELINE_GITHUB,
    )
    profile = PipelineProfile.from_project(config)
    assert (config.github_repository, config.required_checks, config.bot_rework_cap) == ("pipe/repo", ("pipe-ci",), 2)
    assert (profile.repository, profile.required_checks, profile.bot_rework_cap) == ("pipe/repo", ("pipe-ci",), 2)
    assert builder_repository(config) == "pipe/repo"


def test_without_registry_values_the_pipeline_applies(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch, {}, PIPELINE_GITHUB)
    profile = PipelineProfile.from_project(config)
    assert (profile.repository, profile.required_checks, profile.bot_rework_cap) == ("pipe/repo", ("pipe-ci",), 2)
    assert builder_repository(config) == "pipe/repo"


def test_the_builder_targets_the_repository_the_poller_reads(tmp_path: Path, monkeypatch) -> None:
    # ``repo:`` is a profile spelling (AGENTS.md's Pipeline Profile) that the
    # ProjectConfig parser does not read; the builder must still get it.
    config = registry_project(tmp_path, monkeypatch, {}, "repo: pipe/repo\n")
    assert PipelineProfile.from_project(config).repository == "pipe/repo"
    assert builder_repository(config) == "pipe/repo"


# ------------------------------------------- review follow-ups
#
# Regression: the first fix applied every ProjectConfig value over the
# profile, so a bare repository name dropped the owner the pipeline resolved,
# and ProjectConfig defaults replaced profile-only spellings.


def test_a_pipeline_owner_and_bare_repository_keep_the_owner(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch, {}, "github_owner: sample\ngithub_repository: repo\n")
    profile = PipelineProfile.from_project(config)
    assert (profile.repository_owner, profile.repository_name) == ("sample", "repo")
    assert builder_repository(config) == "sample/repo"


def test_a_bare_registry_name_keeps_the_pipeline_owner(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch, {"github_repository": "repo"},
                              "github_owner: sample\ngithub_repository: other\n")
    profile = PipelineProfile.from_project(config)
    assert (profile.repository_owner, profile.repository_name) == ("sample", "repo")
    assert builder_repository(config) == "sample/repo"


@pytest.mark.parametrize("pipeline", [
    PIPELINE_GITHUB,
    "github_owner: sample\ngithub_repository: repo\n",
    "github_repository: p/r\nrepository_name: other\n",
    "repo: pipe/repo\nbot_rework_rounds: 3\nrequired_check: [only-ci]\n",
    "github_repository: pipe/repo\nbot_rework_cap: 0\n",
])
def test_a_pipeline_only_setup_matches_the_pipeline_profile(tmp_path: Path, monkeypatch, pipeline: str) -> None:
    config = registry_project(tmp_path, monkeypatch, {}, pipeline)
    expected = PipelineProfile.from_file(config.pipeline_path)
    resolved = PipelineProfile.from_project(config)
    assert (resolved.repository, resolved.required_checks, resolved.bot_rework_cap) == (
        expected.repository, expected.required_checks, expected.bot_rework_cap)


def test_the_wake_decider_cap_matches_the_profile_cap(tmp_path: Path, monkeypatch) -> None:
    # The supervisor's WakeDecider reads ProjectConfig.bot_rework_cap.
    config = registry_project(tmp_path, monkeypatch, {}, "github_repository: pipe/repo\nbot_rework_rounds: 3\n")
    assert config.bot_rework_cap == PipelineProfile.from_project(config).bot_rework_cap == 3


@pytest.mark.parametrize("value", ["abc", "-1", "2.5"])
def test_an_unparseable_registry_cap_fails_closed(tmp_path: Path, monkeypatch, value: str) -> None:
    with pytest.raises(ValueError, match="bot_rework_cap must be a non-negative integer"):
        registry_project(tmp_path, monkeypatch, {"bot_rework_cap": value}, PIPELINE_GITHUB)


def test_workflow_check_polls_the_resolved_repository(tmp_path: Path, monkeypatch) -> None:
    from harbor.db import HarborDB
    from harbor.workflow_cli import fresh_context

    config = registry_project(tmp_path, monkeypatch, {"required_checks": "[tests]"}, "repo: pipe/repo\n")
    pr_url = "https://github.com/pipe/repo/pull/1"
    seen: list[tuple[str, str]] = []

    class Linear:
        def __init__(self, **kwargs):
            pass

        async def fetch_blocker_context(self, ticket):
            return {
                "identifier": ticket, "project": {"slugId": "sample"}, "description": "Risk: low",
                "state": {"name": "In Progress", "type": "started"},
                "attachments": {"nodes": [{"sourceType": "github", "url": pr_url}]},
            }

        async def close(self):
            pass

    class GitHub:
        def __init__(self, **kwargs):
            pass

        @classmethod
        def from_profile(cls, profile, **kwargs):
            return cls()

        async def fetch_pull_requests(self, owner, repo, *args, **kwargs):
            seen.append((owner, repo))
            return {"pr_1": {
                "number": 1, "headRefOid": "abc", "url": pr_url, "merged": False, "mergeable": "MERGEABLE",
                "statusCheckRollup": {"contexts": {"nodes": [{"name": "tests", "conclusion": "SUCCESS"}]}},
            }}

        async def close(self):
            pass

    async def in_scope(*args, **kwargs):
        return None

    monkeypatch.setattr("harbor.clients.linear.LinearClient", Linear)
    monkeypatch.setattr("harbor.clients.github.GitHubClient", GitHub)
    monkeypatch.setattr("harbor.project_identity.require_scope", in_scope)
    db = HarborDB(":memory:")
    try:
        db.replace_tickets(config.name, [{"identifier": "A-1", "state": "In Progress", "owned": True, "hold": False}])
        context = asyncio.run(fresh_context(config, db, "A-1", {"id": "run", "pr_url": pr_url}, "pre_handoff"))
    finally:
        db.close()
    assert seen == [("pipe", "repo")]
    assert context["facts"]["ci"]["state"] == "green"


@pytest.mark.parametrize("document", ["projects.md", "docs/configurable-workflows.md"])
def test_the_precedence_is_documented(document: str) -> None:
    text = (Path(__file__).resolve().parents[1] / document).read_text(encoding="utf-8")
    for key in ("github_repository", "required_checks", "bot_rework_cap"):
        assert key in text, (document, key)
