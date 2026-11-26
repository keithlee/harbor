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

import pytest

from harbor.cli import _pipeline_text
from harbor.config.projects import ProjectConfig
from workflow_support import config

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "workflows" / "work-pipeline" / "skills" / "delivery" / "SKILL.md"
CODEX_ONLY_TOOLS = "native collaboration tools (spawn, list, wait, resume, close)"
# Any mention of the parameter other than an explicit `false`.
LOOSE_BACKGROUND = re.compile(r"run_in_background(?!`?: false\b)")


def assert_explicit_foreground(text: str) -> None:
    """Claude Code backgrounds an `Agent` child when `run_in_background` is omitted.

    So "never set it" or "use it" is wrong; the rule must ask for an explicit `false`.
    """
    assert "always pass `run_in_background: false`" in text, text
    loose = [text[max(0, m.start() - 40):m.end() + 20] for m in LOOSE_BACKGROUND.finditer(text)]
    assert not loose, f"run_in_background mentioned without an explicit false: {loose}"


def assert_round_count_rule(text: str) -> None:
    """The three-round limit cannot be reset, stalled or spoofed by PR comments.

    - The session's own count is a floor (a reviewer that writes prose leaves the PR count at 0).
    - Every reject counts, even of the same head (a fix that failed to push).
    - Only reviewer JSON verdicts on a commit of the PR count; a human "FAIL" does not.
    """
    text = " ".join(text.split())
    assert "larger of the rounds you counted in this session and the rounds on the PR" in text, text
    assert '`"verdict": "reject"`' in text and "JSON verdict comment" in text
    assert "even when it rejects the same head again" in text
    assert "`head_sha` is a commit on the PR" in text
    assert 'such as a human "FAIL"' in text
    assert "distinct `head_sha`" not in text  # a repeat reject of one head is still a round


@pytest.mark.parametrize("wrong", [
    # PR comments alone: a prose-only reviewer leaves the count at 0 forever.
    'Count failed rounds from the PR: each reviewer JSON verdict comment with `"verdict": "reject"` is one '
    "round, even when it rejects the same head again; count only comments whose `head_sha` is a commit on "
    'the PR, ignoring others such as a human "FAIL".',
    # Distinct heads: a fix that never pushed keeps the count at 1.
    "The count is the larger of the rounds you counted in this session and the rounds on the PR. A failed "
    'round is a distinct `head_sha` with `"verdict": "reject"` in a JSON verdict comment, even when it '
    'rejects the same head again; `head_sha` is a commit on the PR; ignore others such as a human "FAIL".',
])
def test_round_count_rule_rejects_counts_that_can_stall(wrong):
    with pytest.raises(AssertionError):
        assert_round_count_rule(wrong)


@pytest.mark.parametrize("wrong", [
    "spawn each child with the `Agent` tool and use `run_in_background` for long reviews",
    "spawn each child with the `Agent` tool in the foreground; never set `run_in_background`",
    "spawn each child with the `Agent` tool in the foreground",
])
def test_foreground_rule_rejects_wording_that_yields_a_background_child(wrong):
    with pytest.raises(AssertionError):
        assert_explicit_foreground(wrong)


def _assert_provider_correct(text: str) -> None:
    # The Codex-only tool list is scoped to Codex, never an unconditional order.
    for match in re.finditer(re.escape(CODEX_ONLY_TOOLS), text):
        preceding = text[max(0, match.start() - 40):match.start()]
        assert "Codex:" in preceding, f"Codex tool list is not scoped to Codex: ...{preceding}{CODEX_ONLY_TOOLS}"
    claude = text[text.index("Claude Code:"):]
    assert "`Agent` tool" in claude[:200]
    assert "no list, resume, message or close" in claude
    assert "spawn a fresh child for that role" in claude
    # A background child is still running when the builder's turn ends, so it hands off without the result.
    # (Claude Code does deliver the result in a later turn, so "lost" would be the wrong reason.)
    assert_explicit_foreground(claude)
    flat = " ".join(claude.split())
    assert "turn can end before it returns" in flat and "result is lost" not in flat
    # A fresh session has no earlier child report to hand over.
    assert "no earlier child report" in claude and "review comments" in claude
    # A respawned child replaces the old one; it does not break "one per role".
    assert "replaces the earlier one" in claude
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
    launch = prompt[prompt.index("Harbor commands: "):]
    _assert_provider_correct(launch)


def test_shipped_delivery_skill_gives_each_provider_followable_child_agent_rules():
    text = SKILL.read_text(encoding="utf-8")
    section = text[text.index("## Child agents"):text.index("## Flow")]
    _assert_provider_correct(section)
    # Codex keeps its list/resume/close recovery steps.
    assert "list the existing agents" in section


def test_review_fixes_and_planner_continuation_have_a_claude_path():
    """"Return fixes to the same implementor" and "resume the existing planner" assume a retained child.

    Claude Code cannot message or resume a child (Harbor does not grant ``SendMessage``),
    so each step must also say what a Claude builder does instead.
    """
    delivery = SKILL.read_text(encoding="utf-8")
    flow = delivery[delivery.index("## Flow"):]
    review_step = flow[flow.index("3. Review"):flow.index("4. Risk")]
    assert "same implementor" in review_step
    assert "Claude Code: a fresh implementor" in review_step
    planning = (SKILL.parent.parent / "planning" / "SKILL.md").read_text(encoding="utf-8")
    assert "resume the existing planner" in planning
    assert "Claude Code cannot" in planning and "spawns a fresh planner" in planning
    # The three-round limit is counted from durable PR state, not session memory.
    assert "three failed rounds" in review_step
    assert_round_count_rule(review_step)
    review = (SKILL.parent.parent / "review" / "SKILL.md").read_text(encoding="utf-8")
    assert "retained implementor (Codex)" in review and "fresh implementor" in review and "(Claude Code)" in review
    # The PR count only works if the reviewer posts the machine-readable verdict, not prose.
    assert "PR comment whose body is exactly that JSON verdict object" in review


def test_launch_rules_override_older_installed_skill_copies(tmp_path, monkeypatch):
    """An existing install keeps old skill copies; the prompt must say which child-agent rules win."""
    launch = _prompt(tmp_path, monkeypatch)
    launch = launch[launch.index("Harbor commands: "):]
    assert "conflict with child-agent steps in the workflow's skills, these rules win" in launch
    # Codex still follows the skill's own recovery steps.
    assert "Codex builder still follows the skills' list, resume and close steps" in launch
    assert "three-round limit" in launch
    assert_round_count_rule(launch)
