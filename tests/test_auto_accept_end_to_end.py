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


def test_an_opt_out_after_the_hand_off_does_not_block_later_ordinary_resumes(tmp_path):
    """Review round 11, B1 (the reviewer's repro, real runners).

    The ladder auto-accepts #1 and the builder starts.  During that turn the
    operator opts out, and the builder re-blocks with an env blocker the
    ladder resolves deterministically.  That ordinary resume must still run
    (two builder invocations), #1 stays answered and delivered, and no false
    "not applied" park comment is posted for #1."""

    db, supervisor, mate, linear, ladder, notices = _build(tmp_path)
    workflow_file = supervisor.dispatcher.workflow.package / "workflow.toml"
    counter = tmp_path / "invocations"
    reblock = {"status": "blocked", "outcome": "no_pr", "waiting_on": "none", "summary": "connector",
               "next_action": "retry", "blocker": {
                   "kind": "env_blocker", "question": "The Linear connector requires approval; how should I proceed?",
                   "options": [], "recommendation": None, "recommended_option_index": None, "evidence": None}}
    done = {"status": "waiting", "outcome": "pr_open", "waiting_on": "review", "summary": "done",
            "pr_url": "https://example.test/pr/1", "next_action": None, "blocker": None}
    body = (
        "import json, pathlib, sys\n"
        "sys.stdin.read()\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "n = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(n))\n"
        "if n == 1:\n"
        f"    wf = pathlib.Path({str(workflow_file)!r})\n"
        "    wf.write_text(wf.read_text().replace('mode = \"cite_only\"', "
        "'mode = \"cite_only\"\\nauto_accept_first_option = false'))\n"
        f"    print(json.dumps({{'type': 'result', 'result': {reblock!r}}}), flush=True)\n"
        "else:\n"
        f"    print(json.dumps({{'type': 'result', 'result': {done!r}}}), flush=True)\n"
    )
    executable = _fake_codex(tmp_path, body)

    async def factory(worktree, wake):
        return CodexRunner(worktree, db=db, ticket="HAN-86", run_id=wake.run_id, executable=executable,
                           deadline_seconds=30)

    supervisor.dispatcher.runner_factory = factory
    try:
        _ladder_pass(supervisor)
        assert int(counter.read_text()) == 2, supervisor.last_error  # the ordinary resume ran
        first = db.escalations()[0]
        assert first["status"] == "answered" and first["answer"] == OPTIONS[0]
        assert [row["delivered"] for row in auto_accept.history(db, "HAN-86")] == [1]
        bodies = [body for _, body in linear.comments]
        assert any("Harbor auto-accepted option 1 for HAN-86 (escalation #1)" in b for b in bodies)
        assert not any("Auto-accept of option 1 not applied" in b for b in bodies), bodies
    finally:
        db.close()


def test_the_launch_gate_never_fires_after_the_builder_started(tmp_path):
    """Review round 11, B1: a later invocation in the same context (a model
    fallback re-execute, a nested resume) cannot trip the gate or flip the
    outcome once the observed launch started."""

    from harbor.runner.base import SPAWN_OBSERVER, SpawnObserver

    calls = []

    def gate():
        calls.append("gate")
        if len(calls) > 1:
            raise AssertionError("gate re-run after the hand-off")

    observer = SpawnObserver(on_start=lambda: calls.append("start"), on_attempt=gate)
    executable = _fake_codex(tmp_path, STARTS)
    worktree = tmp_path / "wt-gate"
    worktree.mkdir()

    async def go():
        token = SPAWN_OBSERVER.set(observer)
        try:
            runner = CodexRunner(worktree, ticket="HAN-86", executable=executable, deadline_seconds=30)
            await runner.start("first launch", run_id="run-gate")
            assert observer.started and SPAWN_OBSERVER.get() is None  # scoped to that launch
            SPAWN_OBSERVER.set(observer)  # even if a layer re-installs it...
            await runner.resume("run-gate", "fallback re-execute")
        finally:
            SPAWN_OBSERVER.reset(token)

    asyncio.run(go())
    assert calls == ["gate", "start"]  # ...the second invocation is a no-op for the observer


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


def _git_worktree_with_untracked_protected_file(path):
    import subprocess

    def git(*args):
        subprocess.run(["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.test", *args],
                       check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "initial")
    (path / "infra").mkdir(exist_ok=True)
    (path / "infra" / "deploy.yml").write_text("replicas: 3\n")  # untracked, observed by the ladder


def test_an_empty_reported_paths_key_cannot_hide_observed_protected_paths(tmp_path):
    """Review round 12, B1 (the reviewer's repro): the builder reports
    "paths": [] while the worktree has an untracked infra/deploy.yml under
    the protected infra/**.  The ladder's observed paths must still count."""

    db, supervisor, mate, linear, ladder, notices = _build(tmp_path)
    ladder.protected_paths = ("infra/**",)
    _git_worktree_with_untracked_protected_file(tmp_path / "wt" / "HAN-86")
    invoked = []
    inner = _factory(db, tmp_path, "starts")

    async def factory(worktree, wake):
        invoked.append(wake)
        return await inner(worktree, wake)

    supervisor.dispatcher.runner_factory = factory
    final = {**FINAL, "blocker": {**BLOCKER, "paths": []}}
    try:
        wake = Wake(ticket={"identifier": "HAN-86", "builder_run_id": "run-HAN-86", "run_id": "run-HAN-86",
                            "id": "lin-86"}, action="reengage", resume="run-HAN-86")
        asyncio.run(supervisor._handle_runner_result(wake, {**final, "run_id": "run-HAN-86"}))
        result = supervisor.last_unblock
        assert result is not None and result.action == "park", supervisor.last_error
        assert invoked == [] and auto_accept.history(db, "HAN-86") == []
        assert auto_accept.eligibility(db, db.escalations()[-1]["id"])["protected"] is True
        assert not any("auto-accepted option 1" in body for _, body in linear.comments)
    finally:
        db.close()


def test_touches_protected_paths_takes_the_union_of_path_fields():
    for shadow in ("path", "file", "paths", "files"):
        value = {shadow: [], "touched_paths": ["infra/deploy.yml"]}
        assert touches_protected_paths(value, protected_paths=["infra/**"]), shadow
    assert touches_protected_paths({"paths": ["docs/a.md"], "touchedFiles": ["infra/x.yml"]},
                                   protected_paths=["infra/**"])


def test_first_mate_recheck_is_not_shadowed_by_an_empty_paths_key(tmp_path):
    db = HarborDB(tmp_path / "harbor.db")
    try:
        value = {**BLOCKER, "paths": [], "touched_paths": ["infra/deploy.yml"]}
        row = db.record_escalation("HAN-86", "run-HAN-86", value["kind"], value["question"], 2, status="open")
        auto_accept.record_eligibility(db, row["id"], value, protected=False, human_decisions=False)
        case = recovery.Case("Demo", "HAN-86", "run-HAN-86", "blocked", "blocked", value)
        decision = recovery.apply_auto_accept(db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy(),
                                              protected_paths=("infra/**",))
        assert decision["action"] == "escalate" and "protected path" in decision["reason"]
    finally:
        db.close()


def test_builder_results_with_unknown_blocker_keys_are_invalid():
    from harbor.runner.base import validate_result

    payload = {"status": "blocked", "summary": "s", "blocker": {**BLOCKER, "paths": []}}
    assert validate_result(payload) == ["blocker has unknown field(s): paths"]
    assert validate_result({**payload, "blocker": BLOCKER}) == []


def test_first_mate_with_no_retained_session_records_only_the_truthful_failure(tmp_path):
    """Review round 12, B2: with the real WorktreeDispatcher, a session that is
    not available returns before before_launch, so no resume row is written
    or amended; only the "resume failed" escalate row and its notice."""

    db, supervisor, mate, linear, ladder, notices = _build(tmp_path)
    supervisor.dispatcher.runner_factory = _factory(db, tmp_path, "missing_executable")
    _ladder_pass(supervisor)

    async def unavailable(worktree, wake):
        raise RuntimeError("factory refused: session not available")

    try:
        _first_mate_pass(supervisor, db, ladder, mate, unavailable)
        rows = [tuple(r) for r in db.connection.execute("SELECT action, reason FROM first_mate_recoveries ORDER BY id")]
        assert [action for action, _ in rows] == ["escalate"], rows
        assert "resume failed" in rows[0][1]
        assert any("could not resume" in n for n in notices), notices
        assert not any("auto-accepted" in n for n in notices)
        assert [row["source"] for row in auto_accept.history(db, "HAN-86")] == ["ladder"]
        assert db.escalations()[-1]["status"] == "open"
    finally:
        db.close()


def test_a_failing_hand_off_callback_leaves_the_queue_row_as_a_failed_ledger_insert(tmp_path):
    """Review round 12, N1: the queue row never stays "running"."""

    import sqlite3

    from harbor.runner.base import SPAWN_OBSERVER, SpawnObserver
    from harbor.wake import apply_phase3_migration

    db = ledger(tmp_path, final=FINAL)
    apply_phase3_migration(db)
    assert db.enqueue("HAN-86", "reengage", "idem-1")
    sleeper = _fake_codex(tmp_path, "import sys, time\nsys.stdin.read()\ntime.sleep(30)\n")
    runner = CodexRunner(tmp_path / "wt" / "HAN-86", db=db, ticket="HAN-86", run_id="run-HAN-86",
                         executable=sleeper, deadline_seconds=60)

    def locked():
        raise sqlite3.OperationalError("database is locked")

    async def go():
        token = SPAWN_OBSERVER.set(SpawnObserver(on_start=locked))
        try:
            with pytest.raises(sqlite3.OperationalError):
                await runner.resume("run-HAN-86", "Answer")
        finally:
            SPAWN_OBSERVER.reset(token)

    try:
        asyncio.run(go())
        [status] = [row[0] for row in db.connection.execute("SELECT status FROM work_queue WHERE ticket='HAN-86'")]
        assert status == "dispatched"  # as after a failed ledger insert; never "running"
        assert runner.active is None
    finally:
        db.close()
