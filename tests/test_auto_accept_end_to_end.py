"""Auto-accept end to end: a real Supervisor, WorktreeDispatcher, CodexRunner,
UnblockLadder and First Mate monitor pass (review round 10 repros)."""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from harbor import auto_accept, first_mate_recovery as recovery
from harbor.auto_accept import AutoAcceptPolicy
from harbor.db import HarborDB
from harbor.runner.codex import CodexRunner
from harbor.unblock import UnblockLadder, touches_protected_paths
from harbor.wake import Wake
from test_first_mate_recovery import ledger, monitor_observed, supervisor_for

QUESTION = "The empty state has no copy yet. Which wording should it use?"
OPTIONS = ["Use the short wording from the design file", "Write a longer explanatory paragraph"]
BLOCKER = {"kind": "product_decision", "question": QUESTION, "options": OPTIONS,
           "recommendation": "Option 1", "recommended_option_index": 0, "evidence": "design file"}
FINAL = {"status": "blocked", "outcome": "no_pr", "waiting_on": "builder", "summary": "need copy", "blocker": BLOCKER}
STARTS = "import sys\nsys.stdin.read()\n"


def _fake_codex(tmp_path, body):
    script = tmp_path / "fake-codex"
    script.write_text(f"#!{sys.executable}\n" + body)
    script.chmod(0o755)
    return str(script)


def _factory(db, tmp_path, scenario):
    async def factory(worktree, wake):
        kwargs = dict(db=db, ticket="HAN-86", run_id=wake.run_id)
        if scenario == "missing_executable":
            return CodexRunner(worktree, executable=str(tmp_path / "nope"), **kwargs)
        return CodexRunner(worktree, executable=_fake_codex(tmp_path, STARTS), deadline_seconds=30, **kwargs)
    return factory


def _build(tmp_path):
    db = ledger(tmp_path, final=FINAL)
    supervisor, mate, linear = supervisor_for(tmp_path, db, {"action": "escalate", "reason": "product decision"})

    async def notify(reason):
        pass

    ladder = UnblockLadder(db, linear=linear, resumer=supervisor.resume_session, notifier=notify,
                           remediation_executor=lambda command: False)
    ladder.workflow = supervisor.dispatcher.workflow
    supervisor.unblock_ladder = ladder
    notices: list[str] = []

    async def operator(key, message):
        notices.append(message)

    supervisor._notify_operator = operator
    return db, supervisor, mate, linear, ladder, notices


def _ladder_pass(supervisor):
    wake = Wake(ticket={"identifier": "HAN-86", "builder_run_id": "run-HAN-86", "run_id": "run-HAN-86", "id": "lin-86"},
                action="reengage", resume="run-HAN-86")
    asyncio.run(supervisor._handle_runner_result(wake, {**FINAL, "run_id": "run-HAN-86"}))
    return supervisor.last_unblock


def _first_mate_pass(supervisor, db, ladder, mate, factory, *, final=None):
    db.connection.execute("UPDATE runs SET status='blocked', finished_at=?, final_json=? WHERE id='run-HAN-86'",
                          ("2000-01-01T00:00:00+00:00", json.dumps(final or FINAL)))
    db.connection.commit()
    supervisor.dispatcher.runners.clear()
    supervisor.dispatcher.runner_factory = factory
    ladder.first_mate = mate

    async def go():
        await monitor_observed(supervisor)
        await supervisor._drain_recoveries()
    asyncio.run(go())


def _opt_out(workflow_file, edit):
    text = workflow_file.read_text()
    if edit == "opt_out":
        text = text.replace('mode = "cite_only"', 'mode = "cite_only"\nauto_accept_first_option = false')
    else:
        text = text.replace('mode = "cite_only"', 'mode = "human"')
    workflow_file.write_text(text)


@pytest.mark.parametrize("edit", ["opt_out", "mode_human"])
def test_first_mate_honours_an_opt_out_made_while_the_runner_is_prepared(tmp_path, edit):
    """Review round 10, B1 (First Mate): the policy is re-read at launch."""

    db, supervisor, mate, linear, ladder, notices = _build(tmp_path)
    supervisor.dispatcher.runner_factory = _factory(db, tmp_path, "missing_executable")
    _ladder_pass(supervisor)  # parks: the ladder's builder cannot start
    workflow_file = supervisor.dispatcher.workflow.package / "workflow.toml"
    inner = _factory(db, tmp_path, "starts")

    async def factory(worktree, wake):
        _opt_out(workflow_file, edit)  # the operator opts out during preparation
        return await inner(worktree, wake)

    try:
        _first_mate_pass(supervisor, db, ladder, mate, factory)
        escalation = db.escalations()[-1]
        assert escalation["status"] == "open" and escalation["answer"] is None
        assert [row["source"] for row in auto_accept.history(db, "HAN-86")] == ["ladder"]  # nothing new recorded
        assert not any("auto-accepted option 1" in body for _, body in linear.comments)
        assert not any(row[0] == "resume" for row in db.connection.execute("SELECT action FROM first_mate_recoveries"))
        assert any("was not delivered: auto-accept was turned off before the builder started" in n for n in notices)
        assert not any("auto-accepted the recommended" in n for n in notices)
    finally:
        db.close()


@pytest.mark.parametrize("edit", ["opt_out", "mode_human"])
def test_ladder_honours_an_opt_out_made_while_the_runner_is_prepared(tmp_path, edit):
    """Review round 10, B1 (ladder): the runner's launch gate re-reads the policy."""

    db, supervisor, mate, linear, ladder, notices = _build(tmp_path)
    workflow_file = supervisor.dispatcher.workflow.package / "workflow.toml"
    inner = _factory(db, tmp_path, "starts")

    async def factory(worktree, wake):
        _opt_out(workflow_file, edit)
        return await inner(worktree, wake)

    supervisor.dispatcher.runner_factory = factory
    try:
        result = _ladder_pass(supervisor)
        assert result.action == "park"
        escalation = db.escalations()[-1]
        assert escalation["status"] == "open" and escalation["answer"] is None
        assert auto_accept.history(db, "HAN-86") == []  # the receipt is withdrawn, not left undelivered
        [(_, body)] = linear.comments
        assert "auto-accept was turned off before the builder started" in body
        assert "auto-accepted option 1" not in body
    finally:
        db.close()


def test_digest_covers_every_path_bearing_field():
    """Review round 10, B2(a)."""

    base = auto_accept.blocker_digest(BLOCKER)
    for field in ("path", "file", "paths", "files", "touched_paths", "touchedFiles", "protected_paths", "protectedPaths"):
        assert auto_accept.blocker_digest({**BLOCKER, field: ["infra/deploy.yml"]}) != base, field


def test_first_mate_rechecks_reported_paths_itself(tmp_path):
    """Review round 10, B2(b): even with a matching assessment that says
    unprotected, First Mate re-runs the protected-path checks on its blocker."""

    db = HarborDB(tmp_path / "harbor.db")
    try:
        value = {**BLOCKER, "touched_paths": ["infra/deploy.yml"]}
        row = db.record_escalation("HAN-86", "run-HAN-86", value["kind"], value["question"], 2, status="open")
        auto_accept.record_eligibility(db, row["id"], value, protected=False, human_decisions=False)
        case = recovery.Case("Demo", "HAN-86", "run-HAN-86", "blocked", "blocked", value)
        decision = recovery.apply_auto_accept(db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy(),
                                              protected_paths=("infra/**",))
        assert decision["action"] == "escalate" and "protected path" in decision["reason"]
        mentioned = {**BLOCKER, "options": ["Use the short wording from infra/deploy.yml", OPTIONS[1]]}
        row = db.record_escalation("HAN-86", "run-HAN-86", mentioned["kind"], mentioned["question"], 2, status="open")
        auto_accept.record_eligibility(db, row["id"], mentioned, protected=False, human_decisions=False)
        case = recovery.Case("Demo", "HAN-86", "run-HAN-86", "blocked", "blocked", mentioned)
        decision = recovery.apply_auto_accept(db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy(),
                                              protected_paths=("infra/**",))
        assert decision["action"] == "escalate"
    finally:
        db.close()


def test_first_mate_does_not_auto_accept_a_blocker_that_later_reports_protected_paths(tmp_path):
    """Review round 10, B2 (the reviewer's repro, end to end)."""

    db, supervisor, mate, linear, ladder, notices = _build(tmp_path)
    ladder.protected_paths = ("infra/**",)
    supervisor.dispatcher.runner_factory = _factory(db, tmp_path, "missing_executable")
    _ladder_pass(supervisor)  # escalation #1, recorded unprotected
    assert auto_accept.eligibility(db, 1)["protected"] is False
    touched = {**BLOCKER, "touched_paths": ["infra/deploy.yml"]}
    assert touches_protected_paths(touched, protected_paths=["infra/**"])
    try:
        _first_mate_pass(supervisor, db, ladder, mate, _factory(db, tmp_path, "starts"),
                         final={**FINAL, "blocker": touched})
        assert [row["source"] for row in auto_accept.history(db, "HAN-86")] == ["ladder"]
        assert db.escalations()[-1]["status"] == "open"
        assert not any("First Mate auto-accepted option 1" in body for _, body in linear.comments)
    finally:
        db.close()
