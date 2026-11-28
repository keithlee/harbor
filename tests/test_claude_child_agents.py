"""A Claude builder can spawn the child agents the shipped delivery workflow requires.

The bundled delivery skill tells the builder to spawn planner, implementor and
reviewer children. Claude Code exposes that only through its ``Agent`` tool, so
the builder argv must name it. Children run in the same Claude Code process, so
they share the parent's ``--tools`` set, permission rules, mode and sandbox settings;
adding ``Agent`` must not widen any of those.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harbor.runner.claude import ClaudeRunner

ROOT = Path(__file__).resolve().parents[1]
SESSION = "01a1e206-64a0-70e2-ae18-183a99c4cb79"


def _tools(argv: list[str]) -> list[str]:
    return argv[argv.index("--tools") + 1].split(",")


@pytest.mark.parametrize("resume", [False, True])
def test_claude_builder_can_spawn_child_agents_within_the_same_sandbox(tmp_path: Path, resume: bool) -> None:
    runner = ClaudeRunner(tmp_path, model="SampleModel", reasoning_effort="medium", executable="claude")
    argv = runner.command("ignored", run_id=SESSION, resume=resume)

    assert "Agent" in _tools(argv)
    # Children inherit this exact set: no web or MCP tool is added with Agent, and the
    # file-writing tools are approved for the worktree only (test_claude_builder_file_tools.py).
    # TaskStop only stops this session's own background Bash tasks (test_claude_builder_shell.py).
    assert sorted(_tools(argv)) == ["Agent", "Bash", "Edit", "Glob", "Grep", "Read", "Skill", "TaskStop", "Write"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    sandbox = json.loads(argv[argv.index("--settings") + 1])["sandbox"]
    assert sandbox["enabled"] is True and sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False


def test_shipped_delivery_workflow_still_requires_child_agents() -> None:
    """If the skill stops spawning children, revisit whether the builder still needs Agent."""
    skill = (ROOT / "workflows" / "work-pipeline" / "skills" / "delivery" / "SKILL.md").read_text(encoding="utf-8")
    assert "Spawn `planner`, `implementor` and `reviewer` children" in skill
