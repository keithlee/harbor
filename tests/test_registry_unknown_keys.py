"""Unknown project-registry columns are reported, never silently dropped.

Regression: ``get_project`` keeps only the registry keys it knows and drops
the rest without a word. A pipeline-only setting such as ``disk_floor_gb`` or
a typo such as ``github_api_ur1`` left the operator believing a setting was in
effect while Harbor used the default. This is how ``github_api_url`` was
ignored before it was added to the registry (#33).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import harbor.cli as cli
from harbor.config.projects import get_project, registry_key_warnings

GHES_GRAPHQL = "https://ghe.example.test/api/graphql"

UNKNOWN = {"disk_floor_gb": "5", "github_api_ur1": GHES_GRAPHQL}

# Every documented column and alias a registry may use. None of these may be
# reported as unknown.
DOCUMENTED = {
    "linear_project_slug": "sample",
    "linear_team": "SAMPLE",
    "github_repository": "",
    "base_branch": "main",
    "linear_api_url": "",
    "api_url": "",
    "github_api_url": "",
    "github_graphql_url": "",
    "github_endpoint": "",
    "project_slug": "sample",
    "team": "SAMPLE",
    "poll_interval": "30",
    "heartbeat_interval": "30",
    "hold": "true",
    "hold_all": "true",
    "slug": "sample",
    "linear_projects": "[other-project]",
    "poll_interval_seconds": "30",
    "heartbeat_seconds": "30",
}


def write_project(tmp_path: Path, registry: dict[str, str], pipeline_extra: str = "") -> Path:
    from workflow_support import materialize

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    materialize(repo)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text("# Pipeline\nlinear_project_slug: sample\nlinear_team: SAMPLE\ninstall_command: none\n"
                        "linear_api_url: https://linear.invalid/graphql\n" + pipeline_extra)
    projects = tmp_path / "projects.md"
    projects.write_text(
        "| name | repo_path | pipeline_path |" + "".join(f" {key} |" for key in registry) + "\n"
        + "| --- | --- | --- |" + " --- |" * len(registry) + "\n"
        + f"| SampleProject | {repo} | {pipeline} |" + "".join(f" {value} |" for value in registry.values()) + "\n"
    )
    return projects


def run_doctor(tmp_path: Path, registry: dict[str, str]) -> subprocess.CompletedProcess[str]:
    projects = write_project(tmp_path, registry)
    env = {k: v for k, v in os.environ.items() if k not in ("LINEAR_API_KEY", "HARBOR_LINEAR_API_KEY")}
    env.update(HARBOR_PROJECTS_FILE=str(projects), HARBOR_DB_PATH=str(tmp_path / "harbor.db"),
               GITHUB_TOKEN="dummy-token-value", HARBOR_CODEX_EXECUTABLE="/bin/echo",
               HARBOR_WORKTREE_ROOT=str(tmp_path / "wt"))
    return subprocess.run([sys.executable, "-c", "import sys; from harbor.cli import main; sys.exit(main())",
                           "doctor", "SampleProject"], env=env, capture_output=True, text=True, timeout=60)


class EmptyLinearClient:
    async def fetch_issues(self, projects: tuple[str, ...], states: tuple[str, ...]) -> list[dict[str, Any]]:
        return []

    async def close(self) -> None:
        return None


class IdleDispatcher:
    async def dispatch(self, wake: Any) -> None:
        raise AssertionError("shadow start must not dispatch")

    async def resume(self, wake: Any) -> None:
        raise AssertionError("shadow start must not resume")


def run_start(tmp_path: Path, monkeypatch, capsys, registry: dict[str, str]) -> tuple[int, str]:
    projects = write_project(tmp_path, registry)
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(projects))
    monkeypatch.setattr(cli, "_db_path", lambda: tmp_path / "harbor.db")
    result = asyncio.run(cli._start(
        "SampleProject", shadow=True, linear_client=EmptyLinearClient(), dispatcher=IdleDispatcher(),
        first_mate=object(), notifier=object(), unblock_ladder=object(),
    ))
    return result, capsys.readouterr().err


# ------------------------------------------------------------------ doctor


def test_doctor_reports_a_pipeline_only_setting_in_the_registry(tmp_path: Path) -> None:
    result = run_doctor(tmp_path, UNKNOWN)
    out = result.stdout
    assert (f"Registry: SampleProject sets unknown column `disk_floor_gb` in {tmp_path / 'projects.md'}; "
            f"Harbor ignores it. `disk_floor_gb` is a pipeline setting: set it in {tmp_path / 'pipeline.md'}.\n"
            ) in out, out + result.stderr


def test_doctor_reports_a_typo_with_the_closest_registry_column(tmp_path: Path) -> None:
    result = run_doctor(tmp_path, UNKNOWN)
    assert (f"Registry: SampleProject sets unknown column `github_api_ur1` in {tmp_path / 'projects.md'}; "
            "Harbor ignores it. Did you mean `github_api_url`?\n") in result.stdout, result.stdout + result.stderr


def test_doctor_reports_an_unknown_column_with_no_close_match(tmp_path: Path) -> None:
    result = run_doctor(tmp_path, {"owner_notes": "anything"})
    line = next(line for line in result.stdout.splitlines() if "owner_notes" in line)
    assert line == (f"Registry: SampleProject sets unknown column `owner_notes` in {tmp_path / 'projects.md'}; "
                    "Harbor ignores it.")


def test_doctor_does_not_report_documented_columns_or_aliases(tmp_path: Path) -> None:
    result = run_doctor(tmp_path, DOCUMENTED)
    assert "Project: SampleProject" in result.stdout, result.stdout + result.stderr
    assert "Registry:" not in result.stdout + result.stderr


def test_an_unknown_registry_column_warns_and_does_not_change_the_doctor_verdict(tmp_path: Path) -> None:
    clean = run_doctor(tmp_path / "clean", {})
    flagged = run_doctor(tmp_path / "flagged", UNKNOWN)
    assert "unknown column" in flagged.stdout
    assert flagged.returncode == clean.returncode


# ------------------------------------------------------------------- start


def test_start_warns_once_naming_the_key_project_and_hint(tmp_path: Path, monkeypatch, capsys) -> None:
    code, err = run_start(tmp_path, monkeypatch, capsys, UNKNOWN)
    assert code == 0, err
    typo = (f"Harbor: warning: SampleProject sets unknown column `github_api_ur1` in {tmp_path / 'projects.md'}; "
            "Harbor ignores it. Did you mean `github_api_url`?\n")
    assert err.count(typo) == 1, err
    assert err.count("unknown column `disk_floor_gb`") == 1, err
    assert f"`disk_floor_gb` is a pipeline setting: set it in {tmp_path / 'pipeline.md'}." in err


def test_start_does_not_warn_for_documented_columns_or_aliases(tmp_path: Path, monkeypatch, capsys) -> None:
    code, err = run_start(tmp_path, monkeypatch, capsys, DOCUMENTED)
    assert code == 0, err
    assert "warning" not in err


# ----------------------------------------------------------------- loader


def test_get_project_records_only_the_unknown_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(write_project(tmp_path, {**DOCUMENTED, **UNKNOWN})))
    config = get_project("SampleProject")
    assert config.registry_unknown_keys == ("disk_floor_gb", "github_api_ur1")
    assert len(registry_key_warnings(config)) == 2


def test_a_heading_format_registry_reports_unknown_keys(tmp_path: Path, monkeypatch) -> None:
    projects = write_project(tmp_path, {})
    projects.write_text(f"# Projects\n\n## SampleProject\nrepo_path: {tmp_path / 'repo'}\n"
                        f"pipeline_path: {tmp_path / 'pipeline.md'}\ngithub_api_ur1: x\n")
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(projects))
    assert get_project("SampleProject").registry_unknown_keys == ("github_api_ur1",)


# ------------------------------------------ allowlisted but not used (round 2)
#
# Regression: allowlisted columns that .harbor/workflow always or usually
# replaces (role models, fallbacks, workflow_path, efforts) were dropped as
# silently as unknown ones.

WORKFLOW_OWNED = {
    "builder_model": "REG-MODEL",
    "firstmate_model": "REG-FM",
    "workflow_path": "/nope",
    "builder_fallback_models": "[x]",
    "firstmate_fallback_models": "[y]",
}


def warnings_for(tmp_path: Path, monkeypatch, registry: dict[str, str]) -> tuple[str, ...]:
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(write_project(tmp_path, registry)))
    return tuple(w.replace(f" in {tmp_path / 'projects.md'}", "") for w in registry_key_warnings(get_project("SampleProject")))


def test_columns_owned_by_the_workflow_are_reported_with_the_migration_hint(tmp_path: Path, monkeypatch) -> None:
    from harbor.config.workflow import MIGRATION_HINT

    warnings = warnings_for(tmp_path, monkeypatch, WORKFLOW_OWNED)
    assert warnings == tuple(
        f"SampleProject sets `{key}` to `{value}`, but Harbor ignores it: `{key}` comes from .harbor/workflow; "
        f"{MIGRATION_HINT}." for key, value in WORKFLOW_OWNED.items()
    )


def test_an_effort_the_workflow_overrides_is_reported(tmp_path: Path, monkeypatch) -> None:
    # The bundled work-pipeline sets reasoning_effort for both roles.
    warnings = warnings_for(tmp_path, monkeypatch, {"builder_effort": "low", "firstmate_effort": "high"})
    assert warnings == (
        "SampleProject sets `builder_effort` to `low`, but Harbor uses `medium`, set in .harbor/workflow; change it there.",
        "SampleProject sets `firstmate_effort` to `high`, but Harbor uses `medium`, set in .harbor/workflow; change it there.",
    )


def test_an_effort_that_matches_the_workflow_is_not_reported(tmp_path: Path, monkeypatch) -> None:
    assert warnings_for(tmp_path, monkeypatch, {"builder_effort": "medium"}) == ()


def test_doctor_and_start_report_a_workflow_owned_column(tmp_path: Path, monkeypatch, capsys) -> None:
    doctor = run_doctor(tmp_path / "doctor", {"builder_model": "REG-MODEL"})
    assert "Registry: SampleProject sets `builder_model` to `REG-MODEL` in " in doctor.stdout, doctor.stdout
    assert "but Harbor ignores it: `builder_model` comes from .harbor/workflow" in doctor.stdout
    code, err = run_start(tmp_path / "start", monkeypatch, capsys, {"builder_model": "REG-MODEL"})
    assert code == 0, err
    assert err.count("Harbor: warning: SampleProject sets `builder_model` to `REG-MODEL` in ") == 1, err


# --------------------------------------------------------------- hints


def test_a_near_miss_of_a_workflow_owned_column_suggests_nothing_that_would_be_ignored(tmp_path: Path, monkeypatch) -> None:
    warnings = warnings_for(tmp_path, monkeypatch, {"builder_mode": "x", "workflow_pat": "y", "firstmate_mode": "z"})
    assert warnings[:2] == tuple(f"SampleProject sets unknown column `{key}`; Harbor ignores it."
                                 for key in ("builder_mode", "workflow_pat"))
    for ignored in ("`builder_model`", "`firstmate_model`", "`workflow_path`"):
        assert not any(ignored in warning for warning in warnings), warnings


def test_a_pipeline_spelling_of_a_registry_setting_names_the_registry_column(tmp_path: Path, monkeypatch) -> None:
    warnings = warnings_for(tmp_path, monkeypatch, {
        "deadline": "5", "branch": "dev", "heartbeat_interval_seconds": "9", "install": "none",
    })
    assert warnings == (
        "SampleProject sets unknown column `deadline`; Harbor ignores it. The registry column is `deadline_minutes`.",
        "SampleProject sets unknown column `branch`; Harbor ignores it. The registry column is `base_branch`.",
        "SampleProject sets unknown column `heartbeat_interval_seconds`; Harbor ignores it. "
        "The registry column is `heartbeat_seconds`.",
        "SampleProject sets unknown column `install`; Harbor ignores it. The registry column is `install_command`.",
    )


# ----------------------------------------------- one source of truth for aliases


def test_no_column_the_loader_reads_is_reported_as_unknown() -> None:
    from harbor.config.projects import (
        REGISTRY_ALIAS_KEYS, REGISTRY_ALIAS_TABLES, REGISTRY_SETTING_KEYS, unknown_registry_keys,
    )

    aliases = {alias for table in REGISTRY_ALIAS_TABLES for names in table.values() for alias in names}
    assert aliases == REGISTRY_ALIAS_KEYS
    assert {"slug", "project_slug", "team", "poll_interval", "heartbeat_interval", "api_url",
            "github_graphql_url", "github_endpoint", "github_repo", "ci_required_checks",
            "bot_rework_rounds"} <= aliases
    read = REGISTRY_SETTING_KEYS | REGISTRY_ALIAS_KEYS
    assert {"linear_projects", "poll_interval_seconds", "poll_states", "hold_all", "dispatch_states"} <= read
    assert unknown_registry_keys({key: "x" for key in read}) == ()


def test_every_alias_resolves_to_its_setting(tmp_path: Path, monkeypatch) -> None:
    from harbor.config.projects import REGISTRY_ALIAS_TABLES

    cells = {"linear_team": "OTHER", "poll_interval_seconds": "45", "heartbeat_seconds": "45",
             "linear_project_slug": "other", "linear_api_url": "https://l.example.test/graphql",
             "github_api_url": "https://g.example.test/api/graphql", "github_repository": "owner/repo",
             "required_checks": '[CI, "Lint, typecheck"]', "bot_rework_cap": "2"}
    resolved = {"required_checks": ("CI", "Lint, typecheck"), "bot_rework_cap": 2}
    for index, (setting, names) in enumerate(item for table in REGISTRY_ALIAS_TABLES for item in table.items()):
        for alias in names:
            root = tmp_path / f"{index}-{alias}"
            monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(write_project(root, {alias: cells[setting]})))
            config = get_project("SampleProject")
            assert getattr(config, setting) == resolved.get(setting, cells[setting]), alias
            assert config.registry_unknown_keys == () and config.registry_unused_values == (), alias


# ----------------------------------------------------- doctor verdict (AC6)


class HealthyLinearClient:
    def __init__(self, *, endpoint: str) -> None:
        self.endpoint = endpoint

    async def health(self) -> bool:
        return True

    async def list_projects(self) -> list[dict[str, str]]:
        return [{"id": "p1", "slugId": "sample", "name": "Sample"}]

    async def close(self) -> None:
        return None


def doctor_in_process(tmp_path: Path, monkeypatch, capsys, registry: dict[str, str]) -> tuple[int, str]:
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(write_project(tmp_path, registry)))
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/bin/echo")
    monkeypatch.setattr(cli, "LinearClient", HealthyLinearClient)
    code = asyncio.run(cli._doctor("SampleProject"))
    return code, capsys.readouterr().out


def test_a_registry_warning_leaves_a_passing_doctor_at_exit_0(tmp_path: Path, monkeypatch, capsys) -> None:
    clean_code, clean = doctor_in_process(tmp_path / "clean", monkeypatch, capsys, {})
    assert clean_code == 0, clean
    assert "Registry:" not in clean
    code, out = doctor_in_process(tmp_path / "flagged", monkeypatch, capsys, {**UNKNOWN, "builder_model": "REG-MODEL"})
    assert "Registry: SampleProject sets unknown column `github_api_ur1`" in out
    assert "Registry: SampleProject sets `builder_model` to `REG-MODEL`" in out
    assert code == 0, out


# ------------------------------------------- GitHub registry columns (#38)


@pytest.mark.parametrize("registry, pipeline_extra", [
    ({"github_repository": "owner/repo"}, ""),
    ({"github_repo": "owner/repo"}, ""),
    # A bare name keeps the pipeline's owner; the registry cell is still in effect.
    ({"github_repository": "repo"}, "github_owner: owner\n"),
    ({"github_repo": "repo"}, "github_owner: owner\n"),
    ({"required_checks": '[CI, "Lint, typecheck"]'}, ""),
    ({"ci_required_checks": '[CI, "Lint, typecheck"]'}, "required_checks: [Other]\n"),
    ({"required_checks": "CI"}, ""),
    ({"bot_rework_cap": "0"}, ""),
    ({"bot_rework_rounds": "3"}, "bot_rework_cap: 1\n"),
])
def test_github_registry_columns_and_aliases_are_not_reported(
    tmp_path: Path, monkeypatch, registry: dict[str, str], pipeline_extra: str
) -> None:
    from harbor.config.profile import PipelineProfile

    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(write_project(tmp_path, registry, pipeline_extra)))
    config = get_project("SampleProject")
    assert registry_key_warnings(config) == ()
    profile = PipelineProfile.from_project(config)
    if "repo" in "".join(registry.values()):
        assert profile.repository == "owner/repo"


# ------------------------------------------------ empty cells (round 3)
#
# Today an empty registry cell can win over the pipeline (an empty
# base_branch resolves to "", an empty hold to false). Report it while that
# is so; once resolution ignores empty cells the check must stop firing.


def plain(tmp_path: Path, monkeypatch, registry: dict[str, str], pipeline_extra: str = "") -> tuple[str, ...]:
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(write_project(tmp_path, registry, pipeline_extra)))
    return tuple(w.replace(f" in {tmp_path / 'projects.md'}", "") for w in registry_key_warnings(get_project("SampleProject")))


@pytest.mark.parametrize("column, pipeline_extra, fallback", [
    ("base_branch", "base_branch: dev\n", "dev"),
    ("poll_interval_seconds", "poll_interval_seconds: 45\n", "45.0"),
    ("hold", "", "true"),
])
def test_an_empty_cell_that_wins_over_the_pipeline_is_reported(
    tmp_path: Path, monkeypatch, column: str, pipeline_extra: str, fallback: str
) -> None:
    warnings = plain(tmp_path, monkeypatch, {column: ""}, pipeline_extra)
    assert len(warnings) == 1, warnings
    assert warnings[0].startswith(f"SampleProject leaves `{column}` empty, but Harbor uses the empty value")
    assert warnings[0].endswith(f"instead of `{fallback}` from the pipeline or its default; "
                                "fill in the cell or remove the column.")


@pytest.mark.parametrize("registry, pipeline_extra", [
    ({"github_repository": ""}, "github_repository: owner/repo\n"),   # #38 ignores an empty GitHub cell
    ({"required_checks": "", "bot_rework_cap": ""}, "required_checks: [CI]\n"),
    ({"linear_api_url": "", "api_url": ""}, ""),                     # endpoint cells are ignored when empty
    ({"linear_project_slug": "", "linear_team": ""}, ""),            # from_pipeline ignores an empty slug/team
    ({"skill": ""}, ""),                                              # empty in the pipeline too
])
def test_an_empty_cell_that_resolution_ignores_is_not_reported(
    tmp_path: Path, monkeypatch, registry: dict[str, str], pipeline_extra: str
) -> None:
    assert plain(tmp_path, monkeypatch, registry, pipeline_extra) == ()


def test_the_empty_cell_report_stops_once_resolution_ignores_empty_cells(tmp_path: Path, monkeypatch) -> None:
    from harbor.config.projects import registry_values_not_used

    # The config an empty-cell-ignoring loader would produce: the pipeline's value.
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(write_project(tmp_path, {}, "base_branch: dev\n")))
    config = get_project("SampleProject")
    assert config.base_branch == "dev"
    assert registry_values_not_used(config, {"base_branch": ""}, {"base_branch": ""}) == ()


# -------------------------------- spellings that lose to another (round 3)


def test_an_alias_behind_an_empty_scope_column_is_reported(tmp_path: Path, monkeypatch) -> None:
    assert plain(tmp_path, monkeypatch, {"linear_project_slug": "", "slug": "reg-slug"}) == (
        "SampleProject sets `slug` to `reg-slug`, but Harbor ignores it: it reads `linear_project_slug`, "
        "which is empty, and uses `sample`.",
    )


@pytest.mark.parametrize("registry, expected", [
    ({"heartbeat_seconds": "30", "heartbeat_interval": "99"},
     "SampleProject sets `heartbeat_interval` to `99`, but Harbor ignores it: it reads `heartbeat_seconds`, and uses `30`."),
    ({"linear_api_url": "https://a.example.test/graphql", "api_url": "https://b.example.test/graphql"},
     "SampleProject sets `api_url` to `https://b.example.test/graphql`, but Harbor ignores it: it reads "
     "`linear_api_url`, and uses `https://a.example.test/graphql`."),
    ({"github_repository": "owner/repo", "github_repo": "owner/other"},
     "SampleProject sets `github_repo` to `owner/other`, but Harbor ignores it: it reads `github_repository`, "
     "and uses `owner/repo`."),
])
def test_a_second_spelling_of_a_set_column_is_reported(tmp_path: Path, monkeypatch, registry, expected) -> None:
    assert plain(tmp_path, monkeypatch, registry) == (expected,)


def test_matching_spellings_are_not_reported(tmp_path: Path, monkeypatch) -> None:
    assert plain(tmp_path, monkeypatch, {"heartbeat_seconds": "30", "heartbeat_interval": "30"}) == ()


def test_identity_columns_behind_repo_path_and_pipeline_path_are_reported(tmp_path: Path, monkeypatch) -> None:
    warnings = plain(tmp_path, monkeypatch, {"repository": "owner/repo", "repo": "/elsewhere", "pipeline": "/x.md"})
    assert warnings == (
        f"SampleProject sets `repo` to `/elsewhere`, but Harbor ignores it: it reads `repo_path` (`{tmp_path / 'repo'}`).",
        f"SampleProject sets `repository` to `owner/repo`, but Harbor ignores it: it reads `repo_path` "
        f"(`{tmp_path / 'repo'}`); for the GitHub repository, use `github_repository`.",
        f"SampleProject sets `pipeline` to `/x.md`, but Harbor ignores it: it reads `pipeline_path` "
        f"(`{tmp_path / 'pipeline.md'}`).",
    )


# ------------------------------------------------------- hints (round 3)


def test_hints_name_ci_required_checks_but_not_the_state_lists(tmp_path: Path, monkeypatch) -> None:
    warnings = plain(tmp_path, monkeypatch, {"ci_required_check": "x", "poll_state": "y", "dispatch_state": "z"})
    assert warnings[0] == ("SampleProject sets unknown column `ci_required_check`; Harbor ignores it. "
                           "Did you mean `ci_required_checks`?")
    assert not any("Did you mean `poll_states`?" in w or "Did you mean `dispatch_states`?" in w for w in warnings)


# ----------------------------------------------------------- harbor facts


class FactsLinearClient:
    def __init__(self, *, endpoint: str) -> None:
        self.endpoint = endpoint

    async def fetch_issues(self, projects: tuple[str, ...], states: tuple[str, ...]) -> list[dict[str, Any]]:
        return []

    async def close(self) -> None:
        return None


def run_facts(tmp_path: Path, monkeypatch, capsys, registry: dict[str, str]) -> tuple[int, str, str]:
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(write_project(tmp_path, registry)))
    monkeypatch.setattr(cli, "_db_path", lambda: tmp_path / "harbor.db")
    monkeypatch.setattr(cli, "LinearClient", FactsLinearClient)
    code = asyncio.run(cli._facts("SampleProject", as_json=True))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_facts_warns_on_stderr_and_leaves_stdout_and_exit_code_alone(tmp_path: Path, monkeypatch, capsys) -> None:
    clean_code, clean_out, clean_err = run_facts(tmp_path / "clean", monkeypatch, capsys, {})
    code, out, err = run_facts(tmp_path / "flagged", monkeypatch, capsys, UNKNOWN)
    assert "warning" not in clean_err
    assert err.count("Harbor: warning: SampleProject sets unknown column `github_api_ur1`") == 1, err
    assert out == clean_out
    assert json.loads(out) == []
    assert code == clean_code
