"""Registry formats accepted by ``projects.md``: table, heading sections, YAML list."""

from __future__ import annotations

from pathlib import Path

import pytest

from harbor.config.pipeline import parse_projects_markdown
from harbor.config.projects import get_project
from workflow_support import materialize


YAML_LIST_REGISTRY = """# Harbor projects

```yaml
- name: Alpha
  repo_path: /repos/alpha
  pipeline_path: /pipelines/alpha.md
  linear_project_slug: alpha-a1
  linear_team: ALPHA
- name: Beta
  repo_path: /repos/beta
  linear_team: BETA
  base_branch: develop
```
"""


def _by_name(text: str) -> dict[str, object]:
    return {definition.name: definition for definition in parse_projects_markdown(text, source="/registry/projects.md")}


def test_yaml_list_registry_reads_every_entry_with_its_fields() -> None:
    projects = _by_name(YAML_LIST_REGISTRY)

    assert sorted(projects) == ["Alpha", "Beta"]
    alpha, beta = projects["Alpha"], projects["Beta"]
    assert alpha.repo_path == Path("/repos/alpha")
    assert alpha.pipeline_path == Path("/pipelines/alpha.md")
    assert dict(alpha.values) == {"linear_project_slug": "alpha-a1", "linear_team": "ALPHA"}
    assert beta.repo_path == Path("/repos/beta")
    assert beta.pipeline_path is None
    assert dict(beta.values) == {"linear_team": "BETA", "base_branch": "develop"}


def test_yaml_list_under_a_custom_heading_is_not_read_as_one_heading_section() -> None:
    text = YAML_LIST_REGISTRY.replace("# Harbor projects", "# My registry", 1)

    projects = _by_name(text)

    assert sorted(projects) == ["Alpha", "Beta"]
    assert projects["Alpha"].repo_path == Path("/repos/alpha")
    assert projects["Beta"].repo_path == Path("/repos/beta")


def test_yaml_list_accepts_project_key_and_indented_list() -> None:
    text = "  - project: Gamma\n    repo: /repos/gamma\n    linear_team: GAMMA\n"

    projects = _by_name(text)

    assert list(projects) == ["Gamma"]
    assert projects["Gamma"].repo_path == Path("/repos/gamma")
    assert dict(projects["Gamma"].values) == {"linear_team": "GAMMA"}


def test_get_project_resolves_a_yaml_list_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "alpha-repo"
    repo.mkdir()
    materialize(repo)
    pipeline = tmp_path / "alpha-pipeline.md"
    pipeline.write_text(
        "# Alpha pipeline\npoll_states: [Todo, In Progress]\ndispatch_states: [Todo]\n",
        encoding="utf-8",
    )
    registry = tmp_path / "projects.md"
    registry.write_text(
        "\n".join(
            [
                "# Harbor projects",
                "",
                "```yaml",
                "- name: Alpha",
                f"  repo_path: {repo}",
                f"  pipeline_path: {pipeline}",
                "  linear_project_slug: alpha-a1",
                "  linear_team: ALPHA",
                "  github_repository: example/alpha",
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))

    config = get_project("Alpha")

    assert config.name == "Alpha"
    assert config.repo_path == repo.resolve()
    assert config.linear_project_slug == "alpha-a1"
    assert config.linear_team == "ALPHA"
    assert config.github_repository == "example/alpha"


def test_table_registry_is_unchanged() -> None:
    text = "\n".join(
        [
            "# Harbor projects",
            "",
            "| name | repo_path | pipeline_path | linear_team |",
            "| --- | --- | --- | --- |",
            "| Alpha | /repos/alpha | /pipelines/alpha.md | ALPHA |",
            "| Beta | /repos/beta |  | BETA |",
            "",
        ]
    )

    projects = _by_name(text)

    assert sorted(projects) == ["Alpha", "Beta"]
    assert projects["Alpha"].pipeline_path == Path("/pipelines/alpha.md")
    assert projects["Beta"].pipeline_path is None
    assert dict(projects["Beta"].values) == {"linear_team": "BETA"}


def test_heading_registry_is_unchanged_including_bulleted_fields() -> None:
    text = "\n".join(
        [
            "# Harbor projects",
            "",
            "## Alpha",
            "repo_path: /repos/alpha",
            "linear_team: ALPHA",
            "",
            "## Beta",
            "- repo_path: /repos/beta",
            "- linear_team: BETA",
            "",
        ]
    )

    projects = _by_name(text)

    assert sorted(projects) == ["Alpha", "Beta"]
    assert projects["Alpha"].repo_path == Path("/repos/alpha")
    assert dict(projects["Alpha"].values) == {"linear_team": "ALPHA"}
    assert projects["Beta"].repo_path == Path("/repos/beta")
    assert dict(projects["Beta"].values) == {"linear_team": "BETA"}


def test_checked_in_example_registry_stays_empty() -> None:
    example = Path(__file__).resolve().parents[1] / "projects.md"

    assert parse_projects_markdown(example.read_text(encoding="utf-8"), source=example) == ()
