from pathlib import Path

from harbor.config.profile import PipelineProfile
from workflow_support import materialize


def test_profile_accepts_pipeline_table_and_scalar_forms() -> None:
    profile = PipelineProfile.from_markdown(
        """
        | github_repository | example/SampleProject |
        required_checks: [CI, typecheck]
        bot_rework_cap: 1
        """
    )
    assert profile.repository == "example/SampleProject"
    assert profile.required_checks == ("CI", "typecheck")
    assert profile.bot_rework_cap == 1


def test_required_check_names_may_contain_commas() -> None:
    """SampleProject's real check is literally "Lint, typecheck and test (root)".

    A naive split turns one required check into two that can never both be
    SUCCESS, so CI would never be reported green.
    """

    from harbor.config.projects import _split_list

    assert _split_list('["Lint, typecheck and test (root)", "Test (apps/mobile)"]') == (
        "Lint, typecheck and test (root)",
        "Test (apps/mobile)",
    )
    assert _split_list("[Todo, In Progress]") == ("Todo", "In Progress")
    assert _split_list("[]") == ()
    assert _split_list("[Todo]") == ("Todo",)


def test_registered_pipeline_parses_without_a_maintainer_checkout(
    tmp_path: Path, monkeypatch,
) -> None:
    from harbor.config.projects import get_project

    repo = tmp_path / "repo"
    repo.mkdir()
    materialize(repo)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text(
        """linear_project_slug: sample-project-abc123
linear_team: SAMPLE
dispatch_states: [Todo]
poll_states: [Todo, In Progress]
hold: false
required_checks: ["Lint, typecheck and test (root)", "Test (apps/mobile)"]
""",
        encoding="utf-8",
    )
    registry = tmp_path / "projects.md"
    registry.write_text(
        f"""| name | repo_path | pipeline_path |
| --- | --- | --- |
| SampleProject | {repo} | {pipeline} |
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))

    config = get_project("SampleProject")
    assert config.dispatch_states == ("Todo",)
    assert "Backlog" not in config.poll_states
    # hold is an operational toggle the operator flips; only assert it parses
    # as a bool, never a particular value.
    assert isinstance(config.hold_all, bool)
    assert config.hold_all == config.hold
    assert config.required_checks == (
        "Lint, typecheck and test (root)",
        "Test (apps/mobile)",
    )
