"""First Mate's child-agent recovery steps must be followable by the builder that runs them.

A Codex builder can list, close and resume its children. A Claude Code
builder's only child-agent tool is ``Agent`` (no list, resume, message, wait
or close), so telling it to "list agents and close /root/planner" makes the
recovery unfollowable. Harbor knows each run's provider from
``runner_identity`` and must word the recovery for it.
"""
from __future__ import annotations

import re

from harbor import first_mate_recovery as recovery
from harbor.runner.quota import bind_runner_identity
from test_child_agent_instructions import LOOSE_BACKGROUND, assert_explicit_foreground
from test_first_mate_recovery import HAN86, SETTINGS, case, ledger

# The pre-#48 hand-off, which a Codex builder must still receive word for word.
CODEX_OBSOLETE = (
    "Harbor no longer registers or verifies child agents, and policy edits no longer need reconcile; "
    "that blocker is obsolete. List agents, close any interrupted or unknown children (for example "
    "/root/planner), spawn fresh planner/implementor/reviewer children under new names (planner-2, ...) "
    "with the models in config.toml, and continue delivering the ticket from where it stopped."
)

# Steps only a Codex builder can perform, as an order (not as a "do not" warning).
CODEX_ORDER = re.compile(r"\bList agents\b|\bclose (?:any|the) (?:interrupted|stale)|/root/planner|planner-2")


def _assert_claude_followable(text: str) -> None:
    assert not CODEX_ORDER.search(text), text
    assert "`Agent`" in text
    assert_explicit_foreground(text)
    assert "review comments" in text


def _assert_codex_unchanged(text: str) -> None:
    assert "List agents" in text and "/root/planner" in text and "planner-2" in text
    assert "`Agent`" not in text


def _obsolete_decision(provider: str) -> dict:
    proposal = {"action": "escalate", "reason": "Harbor cannot verify the planner's provenance"}
    return recovery.validate(proposal, case(summary=HAN86["summary"], provider=provider), SETTINGS)


def test_obsolete_blocker_instruction_is_followable_by_a_claude_builder():
    decision = _obsolete_decision("claude")
    assert decision["action"] == "fresh_session"
    assert "no longer registers" in decision["instruction"]
    _assert_claude_followable(decision["instruction"])
    # The resume path (fresh session already used) carries the same wording.
    used = case(summary=HAN86["summary"], provider="claude", history=[{"action": "fresh_session"}])
    resumed = recovery.validate({"action": "escalate"}, used, SETTINGS)
    assert resumed["action"] == "resume"
    _assert_claude_followable(resumed["instruction"])


def test_obsolete_blocker_instruction_keeps_codex_steps():
    instruction = _obsolete_decision("codex")["instruction"]
    _assert_codex_unchanged(instruction)
    assert instruction == CODEX_OBSOLETE


def test_claude_recovery_hands_over_the_earlier_childs_report_when_there_is_one():
    text = _obsolete_decision("claude")["instruction"]
    assert "earlier child's report when you have it" in text
    assert "the ticket, the PR and its review comments" in text


def test_unknown_provider_gets_both_steps_each_scoped_to_its_runtime():
    text = _obsolete_decision("")["instruction"]
    codex = text.index("If you are a Codex builder:")
    claude = text.index("If you are a Claude Code builder:")
    assert codex < text.index("List agents") < claude < text.index("`Agent`")


def test_first_mate_prompt_never_tells_a_claude_builder_to_list_or_close_children():
    claude = recovery.build_prompt(case(provider="claude"), SETTINGS)
    guidance = claude[:claude.index("Case:\n")]
    _assert_claude_followable(guidance)
    # The resume example obeys the same rule as the steps it illustrates.
    example = guidance[guidance.index("(e.g. '"):guidance.index("').")]
    assert "`run_in_background: false`" in example and not LOOSE_BACKGROUND.search(example)
    assert "review comments" in example
    assert '"builder_provider": "claude"' in claude
    codex = recovery.build_prompt(case(provider="codex"), SETTINGS)
    _assert_codex_unchanged(codex[:codex.index("Case:\n")])
    assert '"builder_provider": "codex"' in codex


def test_find_cases_reads_the_runs_builder_provider(tmp_path):
    db = ledger(tmp_path, finished=15)
    assert [c.provider for c in recovery.find_cases(db.connection, "Demo", SETTINGS)] == [""]
    bind_runner_identity(db.connection, "run-HAN-86", "claude", "sample-model", "high")
    assert [c.provider for c in recovery.find_cases(db.connection, "Demo", SETTINGS)] == ["claude"]
    db.close()


def test_unrecognised_provider_is_kept_for_first_mate_and_gets_both_steps(tmp_path):
    db = ledger(tmp_path, finished=15)
    bind_runner_identity(db.connection, "run-HAN-86", "SampleRuntime", "sample-model", "high")
    [found] = recovery.find_cases(db.connection, "Demo", SETTINGS)
    assert found.provider == "sampleruntime"
    assert '"builder_provider": "sampleruntime"' in recovery.build_prompt(found, SETTINGS)
    text = recovery.obsolete_instruction(found.provider)
    assert "If you are a Codex builder:" in text and "If you are a Claude Code builder:" in text
    db.close()
