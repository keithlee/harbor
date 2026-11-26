"""The builder's child-agent rules must be followable by every builder provider.

Harbor renders one launch prompt before it picks the builder's provider (and
quota fallback can switch providers), so the prompt and the shipped delivery
skill must tell a Codex builder and a Claude builder each how to run children
with the tools they actually have. Claude Code's only child-agent tool is
``Agent``; it has no list, resume, message (``SendMessage``) or close.
"""

from __future__ import annotations

import re
from pathlib import Path

from harbor.cli import _pipeline_text
from harbor.config.projects import ProjectConfig
from workflow_support import config

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "workflows" / "work-pipeline" / "skills" / "delivery" / "SKILL.md"
CODEX_ONLY_TOOLS = "native collaboration tools (spawn, list, wait, resume, close)"


def _assert_provider_correct(text: str) -> None:
    # The Codex-only tool list is scoped to Codex, never an unconditional order.
    for match in re.finditer(re.escape(CODEX_ONLY_TOOLS), text):
        preceding = text[max(0, match.start() - 40):match.start()]
        assert "Codex:" in preceding, f"Codex tool list is not scoped to Codex: ...{preceding}{CODEX_ONLY_TOOLS}"
    claude = text[text.index("Claude Code:"):]
    assert "`Agent` tool" in claude[:200]
    assert "no list, resume, message or close" in claude
    assert "spawn a fresh child for that role" in claude
    assert "codex exec for children" not in text


def _prompt(tmp_path, monkeypatch) -> str:
    registry = tmp_path / "registry.md"
    registry.write_text("# registry")
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "ledger.db"))
    workflow = config(tmp_path)  # a copy of the shipped work-pipeline, as `harbor init` makes
    cfg = ProjectConfig(name="SampleProject", linear_project_slug="sample", workflow_path=str(workflow))
    return _pipeline_text(cfg)


def test_launch_prompt_gives_each_provider_followable_child_agent_rules(tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch)
    # The whole launch prompt: the copied delivery skill and Harbor's own appended rules.
    _assert_provider_correct(prompt)
    launch = prompt[prompt.index("Authoritative runtime invocation: "):]
    _assert_provider_correct(launch)


def test_shipped_delivery_skill_gives_each_provider_followable_child_agent_rules():
    text = SKILL.read_text(encoding="utf-8")
    section = text[text.index("## Child agents"):text.index("## Flow")]
    _assert_provider_correct(section)
    # Codex keeps its list/resume/close recovery steps.
    assert "list the existing agents" in section


def test_review_fixes_and_planner_continuation_have_a_claude_path():
    """"Return fixes to the same implementor" and "resume the existing planner" need SendMessage on Claude."""
    delivery = SKILL.read_text(encoding="utf-8")
    flow = delivery[delivery.index("## Flow"):]
    review_step = flow[flow.index("3. Review"):flow.index("4. Risk")]
    assert "same implementor" in review_step
    assert "Claude Code: a fresh implementor" in review_step
    planning = (SKILL.parent.parent / "planning" / "SKILL.md").read_text(encoding="utf-8")
    assert "resume the existing planner" in planning
    assert "Claude Code cannot" in planning and "spawns a fresh planner" in planning
