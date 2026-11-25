"""Registry formats in ``projects.md``.

Table rows and heading sections are the supported formats. A YAML-style list
is not: Harbor must name the offending line instead of silently dropping
projects (which surfaced as "unknown Harbor project") or merging several
entries into one project.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harbor.config.pipeline import parse_projects_markdown
from harbor.config.projects import get_project
from workflow_support import materialize


def _parse(text: str) -> list[tuple[str, str, str | None, dict[str, object]]]:
    return [
        (d.name, str(d.repo_path), str(d.pipeline_path) if d.pipeline_path else None, dict(d.values))
        for d in parse_projects_markdown(text, source="/registry/projects.md")
    ]


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
```
"""


def _assert_unsupported_yaml(text: str, line: int) -> None:
    with pytest.raises(ValueError) as error:
        parse_projects_markdown(text, source="/registry/projects.md")
    message = str(error.value)
    assert "unsupported Harbor project registry format" in message
    assert f"line {line}" in message
    assert "YAML" in message
    assert "README.md" in message


# --- YAML-style lists fail loudly ------------------------------------------


def test_yaml_list_registry_names_the_unsupported_format() -> None:
    _assert_unsupported_yaml(YAML_LIST_REGISTRY, line=4)


def test_yaml_list_under_a_custom_heading_is_not_merged_into_one_project() -> None:
    # On main this produced one project "Beta" carrying Alpha's pipeline and slug.
    text = YAML_LIST_REGISTRY.replace("# Harbor projects", "# My registry", 1)

    _assert_unsupported_yaml(text, line=4)


@pytest.mark.parametrize(
    ("entry_body", "line"),
    [
        pytest.param("  # owner: platform\n  repo_path: /repos/alpha\n", 1, id="comment"),
        pytest.param("  repo_path: /repos/alpha\n  required_checks:\n    - tests\n  linear_team: ALPHA\n", 1, id="nested-list"),
        pytest.param("  repo_path: /repos/alpha\n  description: >\n    long text\n  linear_team: ALPHA\n", 1, id="block-scalar"),
        pytest.param("repo_path: /repos/alpha\nlinear_team: ALPHA\n", 1, id="unindented-field"),
        pytest.param("\trepo_path: /repos/alpha\n  linear_team: ALPHA\n", 1, id="tab-space-mix"),
    ],
)
def test_yaml_entry_shapes_never_drop_fields_silently(entry_body: str, line: int) -> None:
    _assert_unsupported_yaml("- name: Alpha\n" + entry_body, line=line)


def test_yaml_entry_with_empty_name_is_reported() -> None:
    _assert_unsupported_yaml("- name:\n  repo_path: /repos/alpha\n", line=1)


def test_duplicate_yaml_entries_are_reported() -> None:
    text = "- name: Alpha\n  repo_path: /repos/a\n- name: Alpha\n  repo_path: /repos/b\n"

    _assert_unsupported_yaml(text, line=1)


def test_get_project_reports_yaml_list_instead_of_unknown_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "alpha-repo"
    repo.mkdir()
    materialize(repo)
    registry = tmp_path / "projects.md"
    registry.write_text(
        f"# Harbor projects\n\n```yaml\n- name: Alpha\n  repo_path: {repo}\n  linear_team: ALPHA\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))

    with pytest.raises(ValueError, match=r"unsupported Harbor project registry format: line 4") as error:
        get_project("Alpha")
    assert "unknown Harbor project" not in str(error.value)


# --- Table and heading registries parse exactly as on main ------------------


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

    assert _parse(text) == [
        ("Alpha", "/repos/alpha", "/pipelines/alpha.md", {"linear_team": "ALPHA"}),
        ("Beta", "/repos/beta", None, {"linear_team": "BETA"}),
    ]


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

    assert _parse(text) == [
        ("Alpha", "/repos/alpha", None, {"linear_team": "ALPHA"}),
        ("Beta", "/repos/beta", None, {"linear_team": "BETA"}),
    ]


def test_heading_registry_with_a_fenced_yaml_example_in_notes_matches_main() -> None:
    text = """# Harbor projects

## Alpha
repo_path: /repos/alpha
linear_team: ALPHA

## Beta
repo_path: /repos/beta

## Notes
Example entry for another tool:

```yaml
- name: Example
  repo_path: /repos/example
```
"""

    assert _parse(text) == [
        ("Alpha", "/repos/alpha", None, {"linear_team": "ALPHA"}),
        ("Beta", "/repos/beta", None, {}),
        ("Example", "/repos/example", None, {"example_entry_for_another_tool": ""}),
    ]


def test_heading_registry_after_yaml_looking_prose_matches_main() -> None:
    text = """# Harbor projects

Each entry has:
- name: the display name
  used in the CLI: yes

## Alpha
repo_path: /repos/alpha
"""

    assert _parse(text) == [("Alpha", "/repos/alpha", None, {})]


def test_heading_section_with_project_key_and_sub_bullets_matches_main() -> None:
    text = """# Harbor projects

## Alpha
- project: alpha-linear
  - repo_path: /repos/alpha
  - linear_team: ALPHA

## Beta
repo_path: /repos/beta
"""

    assert _parse(text) == [
        ("Alpha", "/repos/alpha", None, {"linear_team": "ALPHA"}),
        ("Beta", "/repos/beta", None, {}),
    ]


def test_checked_in_example_registry_stays_empty() -> None:
    example = Path(__file__).resolve().parents[1] / "projects.md"

    assert parse_projects_markdown(example.read_text(encoding="utf-8"), source=example) == ()
