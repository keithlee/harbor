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
from test_first_mate_recovery import HAN86, SETTINGS, case, ledger

# Steps only a Codex builder can perform, as an order (not as a "do not" warning).
CODEX_ORDER = re.compile(r"\bList agents\b|\bclose (?:any|the) (?:interrupted|stale)|/root/planner|planner-2")


def _assert_claude_followable(text: str) -> None:
    assert not CODEX_ORDER.search(text), text
    assert "`Agent`" in text and "foreground" in text and "run_in_background" in text
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
    _assert_codex_unchanged(_obsolete_decision("codex")["instruction"])


def test_unknown_provider_gets_both_steps_each_scoped_to_its_runtime():
    text = _obsolete_decision("")["instruction"]
    codex = text.index("If you are a Codex builder:")
    claude = text.index("If you are a Claude Code builder:")
    assert codex < text.index("List agents") < claude < text.index("`Agent`")


def test_first_mate_prompt_never_tells_a_claude_builder_to_list_or_close_children():
    claude = recovery.build_prompt(case(provider="claude"), SETTINGS)
    guidance = claude[:claude.index("Case:\n")]
    _assert_claude_followable(guidance)
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
