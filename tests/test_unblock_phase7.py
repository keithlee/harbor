from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import httpx

from harbor.db import HarborDB
from harbor.clients.linear import LinearClient
from harbor.unblock import (
    UnblockLadder,
    model_merge_outcome,
    observe_manual_merge,
    triage,
)


def run(awaitable):
    return asyncio.run(awaitable)


def ticket(**extra):
    value = {"identifier": "HAN-42", "id": "linear-42", "title": "Blocked", "state": "In Progress"}
    value.update(extra)
    return value


class LinearFake:
    def __init__(self):
        self.comments = []
        self.states = []

    async def create_comment(self, issue_id, body):
        self.comments.append((issue_id, body))
        return {"comment": {"id": f"comment-{len(self.comments)}"}}

    async def update_issue_state(self, issue_id, state):
        self.states.append((issue_id, state))
        return {"success": True}


def test_rung_zero_repairs_missing_module_and_resumes_same_session(tmp_path: Path):
    db = HarborDB(tmp_path / "harbor.db")
    commands = []
    resumed = []
    linear = LinearFake()

    async def execute(command):
        commands.append(command)
        return True

    async def resume(run_id, prompt):
        resumed.append((run_id, prompt))
        return True

    try:
        result = run(
            UnblockLadder(
                db,
                linear=linear,
                resumer=resume,
                remediation_executor=execute,
            ).handle(
                ticket(),
                {"id": "builder-session"},
                {"blocker": {"kind": "env_blocker", "question": "Cannot find module 'x'"}},
            )
        )
        assert result.rung == 0
        assert result.resumed and result.same_session
        assert commands == [["pnpm", "install"]]
        assert resumed[0][0] == "builder-session"
        assert linear.comments == [] and linear.states == []
        assert db.escalations()[0]["rung_resolved"] == 0
    finally:
        db.close()


def test_rung_one_cites_discoveries_without_paging(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "DISCOVERIES.md").write_text(
        "Use the existing reader cache when the cache is unavailable.\n", encoding="utf-8"
    )
    db = HarborDB(tmp_path / "harbor.db")
    linear = LinearFake()
    calls = []

    async def terra(prompt, blocker):
        calls.append(prompt)
        return {
            "disposition": "resolve",
            "answer": "Use the existing reader cache.",
            "citation": "docs/DISCOVERIES.md:1",
        }

    async def resume(run_id, prompt):
        return True

    try:
        result = run(
            UnblockLadder(
                db,
                linear=linear,
                first_mate=terra,
                resumer=resume,
                repo_path=tmp_path,
                remediation_executor=lambda command: False,
            ).handle(
                ticket(),
                {"id": "builder-session"},
                {"blocker": {"kind": "ambiguous_ac", "question": "How should cache be unavailable?"}},
            )
        )
        assert result.rung == 1 and result.resumed and not result.notified
        assert result.citation.startswith("docs/DISCOVERIES.md")
        assert len(calls) == 1
        assert len(linear.comments) == 1 and linear.states == []
    finally:
        db.close()


def test_novel_product_question_escalates_and_lists_sources(tmp_path: Path):
    db = HarborDB(tmp_path / "harbor.db")
    linear = LinearFake()
    terra_calls = []
    notices = []

    async def terra(*args):
        terra_calls.append(args)
        return {"disposition": "resolve", "answer": "invented", "citation": "made-up"}

    async def notify(reason):
        notices.append(reason)

    try:
        result = run(
            UnblockLadder(
                db,
                linear=linear,
                first_mate=terra,
                notifier=notify,
                repo_path=tmp_path,
                remediation_executor=lambda command: False,
            ).handle(
                ticket(),
                {"id": "builder-session"},
                {
                    "blocker": {
                        "kind": "product_decision",
                        "question": "Choose a new product behavior for empty state",
                    }
                },
            )
        )
        assert result.rung == 2 and result.action == "park" and result.notified
        assert terra_calls == []
        assert notices and "tried rung 0 and rung 1" in notices[0]
        assert linear.states == [("linear-42", "Human Review")]
        assert "Searched: none" in linear.comments[0][1]
        assert db.escalations()[0]["rung_resolved"] == 2
    finally:
        db.close()


def test_empty_resolution_citation_is_rejected(tmp_path: Path):
    db = HarborDB(tmp_path / "harbor.db")
    try:
        with pytest.raises(ValueError, match="citation"):
            db.record_resolution("env_blocker", "question", "answer", "  ")
    finally:
        db.close()


def test_third_same_kind_block_parks_after_two_rung_one_answers(tmp_path: Path):
    db = HarborDB(tmp_path / "harbor.db")
    linear = LinearFake()
    db.record_escalation("HAN-42", "one", "env_blocker", "q1", 1, citation="AGENTS.md:1", answer="a")
    db.record_escalation("HAN-42", "two", "env_blocker", "q2", 1, citation="AGENTS.md:2", answer="a")
    try:
        result = run(
            UnblockLadder(db, linear=linear, remediation_executor=lambda command: False).handle(
                ticket(), {"id": "three"}, {"blocker": {"kind": "env_blocker", "question": "q3"}}
            )
        )
        assert result.rung == 2
        assert db.escalations()[-1]["rung_resolved"] == 2
    finally:
        db.close()


def test_resolution_is_reused_at_rung_one_without_terra(tmp_path: Path):
    db = HarborDB(tmp_path / "harbor.db")
    db.record_resolution(
        "ambiguous_ac",
        "Which cache?",
        "Use local cache.",
        "resolutions#7",
        source_ticket="HAN-41",
    )
    calls = []

    async def terra(*args):
        calls.append(args)
        return None

    try:
        result = run(
            triage(
                ticket(),
                {"kind": "ambiguous_ac", "question": "Which cache?"},
                db=db,
                first_mate=terra,
                repo_path=tmp_path,
            )
        )
        assert result.resolved and result.from_resolution
        assert result.answer == "Use local cache."
        assert calls == []
    finally:
        db.close()


def test_merge_model_is_a_builder_command_not_a_harbor_write():
    held = model_merge_outcome("low", True, ticket="HAN-42")
    free = model_merge_outcome("low", False, ticket="HAN-42")
    high = model_merge_outcome("high", False, ticket="HAN-42")
    assert held.outcome == "parked" and held.waiting_on == "human" and held.command.action == "park"
    assert free.outcome == "merged" and free.command.action == "merge"
    assert high.outcome == "parked" and high.command.action == "park"
    assert held.harbor_writes == free.harbor_writes == high.harbor_writes == ()


def test_manual_merge_reengages_retained_builder_only():
    command = observe_manual_merge(
        ticket(), {"id": "builder-session", "waiting_on": "human"}, {"merged": True}
    )
    assert command is not None
    assert command.action == "reengage" and command.run_id == "builder-session"


def test_linear_progress_primitives_write_comment_and_nonterminal_state_only():
    requests = []

    def handler(request):
        requests.append(request)
        body = request.content.decode()
        if "commentCreate" in body:
            return httpx.Response(200, json={"data": {"commentCreate": {"success": True, "comment": {"id": "c"}}}})
        if "HarborStates" in body:
            # issueUpdate takes a workflow state UUID; a name is resolved first.
            return httpx.Response(200, json={"data": {"issue": {"id": "linear-42", "team": {
                "id": "t", "name": "HAN", "states": {"nodes": [
                    {"id": "6876f9e0-76aa-4dac-ac4f-fabeceb1d54f", "name": "Blocked", "type": "started"},
                ]}}}}})
        return httpx.Response(200, json={"data": {"issueUpdate": {"success": True, "issue": {"state": {"name": "Blocked"}}}}})

    async def exercise():
        client = LinearClient(token="test", transport=httpx.MockTransport(handler))
        try:
            await client.create_comment("linear-42", "[harbor] progress")
            await client.update_issue_state("linear-42", "Blocked")
            with pytest.raises(ValueError):
                await client.update_issue_state("linear-42", "Done")
        finally:
            await client.close()

    run(exercise())
    # comment, state-name lookup, issueUpdate -- and nothing for "Done",
    # which is refused before any network call.
    assert len(requests) == 3
    assert all("Done" not in request.content.decode() for request in requests)


def test_a_parked_merge_ready_run_goes_to_human_review_not_blocked():
    """AC-7: outcome=parked + waiting_on=human means the work is DONE.

    Only the merge click is the operator's, so the ticket belongs in Human
    Review even when the run also reported an incidental env blocker --
    routing it to Blocked misreports a finished ticket as a broken one.
    """

    from harbor.unblock import _is_parked_for_human

    assert _is_parked_for_human({"outcome": "parked", "waiting_on": "human"}) is True
    assert _is_parked_for_human({"outcome": "parked", "waiting_on": "review"}) is False
    assert _is_parked_for_human({"outcome": "no_pr", "waiting_on": "human"}) is False
    assert _is_parked_for_human({}) is False
    assert _is_parked_for_human(None) is False
