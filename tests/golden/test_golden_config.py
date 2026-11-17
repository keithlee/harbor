"""Golden fail-closed matrix for provider and browser-runtime configuration."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harbor.config.projects import get_project

PIPELINE = """# Pipeline
linear_project_slug: sample-project
linear_team: SAMPLE
install_command: none
{extra}
"""

DIGEST = "sha256:" + "c" * 64

CASES = {
    "default_is_codex": "",
    "claude_builder_with_model": "builder_provider: claude\nbuilder_model: claude-opus-5-5\nbuilder_effort: high",
    "claude_builder_without_model": "builder_provider: claude",
    "claude_builder_with_gpt_model": "builder_provider: claude\nbuilder_model: gpt-5.6-luna",
    "claude_first_mate_with_model": "firstmate_provider: claude\nfirstmate_model: claude-sonnet-5-5",
    "claude_first_mate_without_model": "firstmate_provider: claude",
    "blank_provider": "builder_provider:",
    "capitalized_provider": "builder_provider: Claude\nbuilder_model: claude-opus-5-5",
    "unknown_provider": "builder_provider: gemini\nbuilder_model: x",
    "playwright_digest_pinned": f"playwright_browser_image: mcr.microsoft.com/playwright@{DIGEST}",
    "playwright_tag_and_digest": f"playwright_browser_image: mcr.microsoft.com/playwright:v1.50.0@{DIGEST}",
    "playwright_tag_only": "playwright_browser_image: mcr.microsoft.com/playwright:v1.50.0",
    "playwright_short_digest": "playwright_browser_image: img@sha256:abc",
    "playwright_shell_metachar": f"playwright_browser_image: img;rm -rf /@{DIGEST}",
}


def registry(tmp_path: Path, extra: str) -> Path:
    repo = tmp_path / "SampleProject"
    repo.mkdir()
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


def resolve(tmp_path: Path, monkeypatch, extra: str) -> dict:
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry(tmp_path, extra)))
    try:
        config = get_project("SampleProject")
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "builder": [config.builder_provider, config.builder_model, config.builder_effort],
        "firstmate": [config.firstmate_provider, config.firstmate_model, config.firstmate_effort],
        "playwright_browser_image": config.playwright_browser_image,
    }


def test_provider_and_browser_configuration_matrix(tmp_path_factory, monkeypatch, golden) -> None:
    table = {name: resolve(tmp_path_factory.mktemp(name), monkeypatch, extra) for name, extra in CASES.items()}
    golden.json("config_provider_matrix.json", table)


@pytest.mark.parametrize("name", [name for name in CASES if "without_model" in name or "gpt_model" in name
                                  or name in {"blank_provider", "unknown_provider", "capitalized_provider",
                                              "playwright_tag_only", "playwright_short_digest",
                                              "playwright_shell_metachar"}])
def test_invalid_configuration_fails_closed(tmp_path: Path, monkeypatch, name: str) -> None:
    assert "error" in resolve(tmp_path, monkeypatch, CASES[name])
