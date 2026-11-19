"""Golden fail-closed matrix for provider and browser-runtime configuration."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harbor.config.projects import get_project
from workflow_support import materialize

PIPELINE = """# Pipeline
linear_project_slug: sample-project
linear_team: SAMPLE
install_command: none
{extra}
"""

DIGEST = "sha256:" + "c" * 64

# name -> (pipeline.md extra, config.toml models). Models live in the
# project's .harbor/workflow/config.toml; pipeline.md only picks providers.
CLAUDE = {"controller": {"model": "claude-opus-5-5", "reasoning_effort": "high"}}
GPT = {"controller": {"model": "gpt-5.6-luna"}}
CASES = {
    "default_is_codex": ("", None),
    "claude_builder_with_model": ("builder_provider: claude", CLAUDE),
    "claude_builder_with_gpt_model": ("builder_provider: claude", GPT),
    "claude_first_mate_with_model": ("firstmate_provider: claude", {"first_mate": {"model": "claude-sonnet-5-5"}}),
    "claude_first_mate_with_gpt_model": ("firstmate_provider: claude", {"first_mate": {"model": "gpt-5.6-luna"}}),
    "blank_provider": ("builder_provider:", None),
    "capitalized_provider": ("builder_provider: Claude", CLAUDE),
    "unknown_provider": ("builder_provider: gemini", None),
    "retired_model_key": ("builder_model: claude-opus-5-5", None),
    "playwright_digest_pinned": (f"playwright_browser_image: mcr.microsoft.com/playwright@{DIGEST}", None),
    "playwright_tag_and_digest": (f"playwright_browser_image: mcr.microsoft.com/playwright:v1.50.0@{DIGEST}", None),
    "playwright_tag_only": ("playwright_browser_image: mcr.microsoft.com/playwright:v1.50.0", None),
    "playwright_short_digest": ("playwright_browser_image: img@sha256:abc", None),
    "playwright_shell_metachar": (f"playwright_browser_image: img;rm -rf /@{DIGEST}", None),
}


def registry(tmp_path: Path, extra: str, models=None) -> Path:
    repo = tmp_path / "SampleProject"
    repo.mkdir()
    materialize(repo, models=models)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text(PIPELINE.format(extra=extra), encoding="utf-8")
    projects = tmp_path / "projects.md"
    projects.write_text(
        "| name | repo_path | pipeline_path | linear_project_slug | linear_team |\n"
        "| --- | --- | --- | --- | --- |\n"
        f"| SampleProject | {repo} | {pipeline} | sample-project | SAMPLE |\n",
        encoding="utf-8",
    )
    return projects


def resolve(tmp_path: Path, monkeypatch, case) -> dict:
    extra, models = case
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry(tmp_path, extra, models)))
    try:
        config = get_project("SampleProject")
    except ValueError as exc:
        return {"error": str(exc).replace(str(tmp_path), "<tmp>")}
    return {
        "builder": [config.builder_provider, config.builder_model, config.builder_effort],
        "firstmate": [config.firstmate_provider, config.firstmate_model, config.firstmate_effort],
        "playwright_browser_image": config.playwright_browser_image,
    }


def test_provider_and_browser_configuration_matrix(tmp_path_factory, monkeypatch, golden) -> None:
    table = {name: resolve(tmp_path_factory.mktemp(name), monkeypatch, case) for name, case in CASES.items()}
    golden.json("config_provider_matrix.json", table)


@pytest.mark.parametrize("name", [name for name in CASES if "gpt_model" in name
                                  or name in {"blank_provider", "unknown_provider", "capitalized_provider", "retired_model_key",
                                              "playwright_tag_only", "playwright_short_digest",
                                              "playwright_shell_metachar"}])
def test_invalid_configuration_fails_closed(tmp_path: Path, monkeypatch, name: str) -> None:
    assert "error" in resolve(tmp_path, monkeypatch, CASES[name])
