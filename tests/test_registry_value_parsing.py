"""Registry cells are raw strings; ``get_project`` must type them like the
pipeline parser does, ignore empty cells, and never release hold on a value
it cannot read."""

from __future__ import annotations

from pathlib import Path

import pytest

from harbor.config.projects import ProjectConfig, get_project
from workflow_support import materialize


def _project(tmp_path: Path, monkeypatch, registry: dict[str, str], pipeline: str = "") -> ProjectConfig:
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


# -- hold: an empty cell never releases it ------------------------------------


@pytest.mark.parametrize("column", ["hold", "hold_all"])
def test_an_empty_registry_hold_cell_keeps_the_pipeline_hold_on(tmp_path, monkeypatch, column) -> None:
    config = _project(tmp_path, monkeypatch, {column: ""}, pipeline="hold: true")
    assert config.hold is True
    assert config.hold_all is True


def test_an_empty_registry_hold_cell_keeps_the_default_hold_on(tmp_path, monkeypatch) -> None:
    config = _project(tmp_path, monkeypatch, {"hold": "", "hold_all": ""})
    assert config.hold is True


def test_an_empty_registry_hold_cell_keeps_a_released_pipeline_hold(tmp_path, monkeypatch) -> None:
    config = _project(tmp_path, monkeypatch, {"hold": ""}, pipeline="hold: false")
    assert config.hold is False


# -- hold: booleans parsed like the pipeline parser ---------------------------


@pytest.mark.parametrize("column", ["hold", "hold_all"])
@pytest.mark.parametrize("text", ["false", "False", "NO", "off", "0"])
def test_a_registry_false_releases_a_pipeline_hold(tmp_path, monkeypatch, column, text) -> None:
    config = _project(tmp_path, monkeypatch, {column: text}, pipeline="hold: true")
    assert config.hold is False
    assert config.hold_all is False


@pytest.mark.parametrize("column", ["hold", "hold_all"])
@pytest.mark.parametrize("text", ["true", "YES", "On", "1"])
def test_a_registry_true_holds_over_a_released_pipeline(tmp_path, monkeypatch, column, text) -> None:
    config = _project(tmp_path, monkeypatch, {column: text}, pipeline="hold: false")
    assert config.hold is True
    assert config.hold_all is True


def test_registry_hold_wins_over_registry_hold_all(tmp_path, monkeypatch) -> None:
    # Same precedence as the pipeline parser: ``hold`` before ``hold_all``.
    assert _project(tmp_path, monkeypatch, {"hold": "false", "hold_all": "true"}).hold is False


def test_registry_hold_all_applies_when_hold_cell_is_empty(tmp_path, monkeypatch) -> None:
    config = _project(tmp_path, monkeypatch, {"hold": "", "hold_all": "false"}, pipeline="hold: true")
    assert config.hold is False


@pytest.mark.parametrize("column", ["hold", "hold_all"])
@pytest.mark.parametrize("text", ["maybe", "ture", "2"])
def test_an_unrecognized_registry_hold_fails_closed(tmp_path, monkeypatch, column, text) -> None:
    with pytest.raises(ValueError, match=rf"SampleProject: registry {column} must be true or false .*, got '{text}'"):
        _project(tmp_path, monkeypatch, {column: text}, pipeline="hold: false")


# -- lists --------------------------------------------------------------------


def test_registry_state_lists_are_parsed_like_the_pipeline(tmp_path, monkeypatch) -> None:
    config = _project(
        tmp_path,
        monkeypatch,
        {"poll_states": "[Ready, In Progress, 'Needs, Review']", "dispatch_states": "[Ready]"},
    )
    assert config.poll_states == ("Ready", "In Progress", "Needs, Review")
    assert config.dispatch_states == ("Ready",)


def test_registry_linear_projects_list_is_parsed(tmp_path, monkeypatch) -> None:
    assert _project(tmp_path, monkeypatch, {"linear_projects": "[extra-a, extra-b]"}).linear_projects == (
        "extra-a",
        "extra-b",
    )


# -- numbers ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "attribute"),
    [
        ("poll_interval", "poll_interval_seconds"),
        ("poll_interval_seconds", "poll_interval_seconds"),
        ("heartbeat_seconds", "heartbeat_seconds"),
        ("heartbeat_interval", "heartbeat_seconds"),
        ("deadline_minutes", "deadline_minutes"),
    ],
)
def test_registry_numbers_are_floats(tmp_path, monkeypatch, column, attribute) -> None:
    value = getattr(_project(tmp_path, monkeypatch, {column: "30"}), attribute)
    assert value == 30.0
    assert isinstance(value, float)


@pytest.mark.parametrize(
    ("column", "setting"),
    [
        ("poll_interval", "poll_interval_seconds"),
        ("heartbeat_seconds", "heartbeat_seconds"),
        ("deadline_minutes", "deadline_minutes"),
    ],
)
@pytest.mark.parametrize("text", ["thirty", "0", "-5", "nan", "inf"])
def test_an_unparseable_registry_number_fails_closed(tmp_path, monkeypatch, column, setting, text) -> None:
    with pytest.raises(ValueError, match=rf"registry {setting} must be a positive number, got '{text}'"):
        _project(tmp_path, monkeypatch, {column: text})


# -- reasoning effort -----------------------------------------------------------


def test_an_unknown_registry_effort_fails_closed(tmp_path, monkeypatch) -> None:
    with pytest.raises(ValueError, match=r"registry firstmate_effort must be one of .*, got 'huge'"):
        _project(tmp_path, monkeypatch, {"firstmate_effort": "huge"})


# -- empty cells and pipeline-only setups ---------------------------------------


PIPELINE = "\n".join(
    [
        "hold: false",
        "poll_interval: 45",
        "heartbeat_seconds: 15",
        "poll_states: [Todo, In Progress]",
        "dispatch_states: [Todo]",
        "linear_projects: [extra-a]",
        "base_branch: develop",
        "worktree_root: /tmp/harbor-wt",
        "install_command: make setup",
    ]
)
TYPED_COLUMNS = (
    "hold",
    "hold_all",
    "poll_interval",
    "heartbeat_seconds",
    "deadline_minutes",
    "poll_states",
    "dispatch_states",
    "linear_projects",
    "base_branch",
    "worktree_root",
    "install_command",
    "skill",
    "builder_effort",
    "firstmate_effort",
    "builder_provider",
    "firstmate_provider",
    "playwright_browser_image",
)


def _settings(config: ProjectConfig) -> tuple[object, ...]:
    return (
        config.hold,
        config.hold_all,
        config.poll_interval_seconds,
        config.heartbeat_seconds,
        config.deadline_minutes,
        config.poll_states,
        config.dispatch_states,
        config.linear_projects,
        config.base_branch,
        config.worktree_root,
        config.install_command,
        config.skill,
        config.builder_effort,
        config.firstmate_effort,
        config.builder_provider,
        config.firstmate_provider,
        config.playwright_browser_image,
    )


def test_a_pipeline_only_setup_is_unchanged(tmp_path, monkeypatch) -> None:
    config = _project(tmp_path, monkeypatch, {}, pipeline=PIPELINE)
    direct = ProjectConfig.from_pipeline("SampleProject", tmp_path / "pipeline.md", repo_path=tmp_path / "repo")
    assert _settings(config) == _settings(direct)
    assert (config.hold, config.poll_interval_seconds, config.poll_states, config.base_branch) == (
        False,
        45.0,
        ("Todo", "In Progress"),
        "develop",
    )


def test_empty_registry_cells_leave_every_pipeline_value_alone(tmp_path, monkeypatch) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    baseline = _settings(_project(tmp_path / "a", monkeypatch, {}, pipeline=PIPELINE))
    blank = _settings(_project(tmp_path / "b", monkeypatch, {column: "" for column in TYPED_COLUMNS}, pipeline=PIPELINE))
    assert blank == baseline
