"""First Mate monitor-and-recover, including a replay of the HAN-86 deadlock."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from harbor import first_mate_recovery as recovery
from harbor.clients.linear import LinearClient
from harbor.config.projects import ProjectConfig
from harbor.config.workflow import load_workflow
from harbor.db import HarborDB, apply_phase9_migration
from harbor.poll.linear_poller import LinearPoller, PollResult
from harbor.runner.base import apply_phase4_migration
from harbor.supervisor import Supervisor, WorktreeDispatcher
from harbor.wake import Wake, apply_phase3_migration
from workflow_support import config

SETTINGS = {"max_recoveries": 3, "allow_fresh_session": True, "recheck_minutes": 10, "stall_minutes": 30}
HAN86 = {
    "status": "blocked", "outcome": "no_pr", "waiting_on": "human",
    "summary": "spawn_agent returned `agent path /root/planner already exists`; registration failed.",
    "blocker": {"kind": "missing_proof_capability",
                "question": "Can Harbor clear or resume the existing /root/planner path?"},
}


def iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def ledger(tmp_path, *, status="blocked", final=HAN86, finished=15, activity=None, ticket="HAN-86"):
    db = HarborDB(tmp_path / "state.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    db.replace_tickets("Demo", [{"identifier": ticket, "state": "Blocked", "title": "Spacing strip", "owned": True,
                                 "hold": False, "id": "lin-86"}], emit_events=False)
    worktree = tmp_path / "wt" / ticket
    worktree.mkdir(parents=True, exist_ok=True)
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,session_id,worktree,final_json,started_at,finished_at,last_activity_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (f"run-{ticket}", ticket, status, "native-1", str(worktree), json.dumps(final), iso(60),
         iso(finished) if finished is not None else None, iso(activity) if activity is not None else None),
    )
    db.connection.commit()
    return db


def test_blocked_run_is_rechecked_on_a_flat_cadence(tmp_path):
    db = ledger(tmp_path, finished=15)
    cases = recovery.find_cases(db.connection, "Demo", SETTINGS)
    assert [(c.ticket, c.trigger) for c in cases] == [("HAN-86", "blocked")]
    recovery.record(db.connection, cases[0], {"action": "resume", "instruction": "x", "reason": "r"})
    recovery.record(db.connection, cases[0], {"action": "escalate", "instruction": "", "reason": "r"})
    assert recovery.find_cases(db.connection, "Demo", SETTINGS) == []  # recheck delay
    # Ordinary recoveries never back off: the next check is one recheck later.
    assert len(recovery.find_cases(db.connection, "Demo", SETTINGS, now=time.time() + 11 * 60)) == 1
    db.close()


def test_recent_blocks_and_held_tickets_are_left_alone(tmp_path):
    db = ledger(tmp_path, finished=2)
    assert recovery.find_cases(db.connection, "Demo", SETTINGS) == []
    db.connection.execute("UPDATE runs SET finished_at=?", (iso(30),))
    db.connection.execute("UPDATE tickets SET hold=1")
    db.connection.commit()
    assert recovery.find_cases(db.connection, "Demo", SETTINGS) == []
    db.close()


def test_stalled_running_run_is_detected(tmp_path):
    db = ledger(tmp_path, status="running", final={}, finished=None, activity=45)
    assert [c.trigger for c in recovery.find_cases(db.connection, "Demo", SETTINGS)] == ["stalled"]
    db.close()


def case(**overrides):
    base = dict(project="Demo", ticket="HAN-86", run_id="run", trigger="blocked", status="blocked",
                blocker=dict(HAN86["blocker"]), history=[])
    base.update(overrides)
    return recovery.Case(**base)


def test_validator_guards():
    resume = {"action": "resume", "instruction": "close /root/planner and spawn planner-2", "reason": "stale child"}
    assert recovery.validate(resume, case(), SETTINGS)["action"] == "resume"
    assert recovery.validate({**resume, "action": "retry"}, case(), SETTINGS)["action"] == "resume"
    assert recovery.validate({"action": "resume"}, case(), SETTINGS)["action"] == "escalate"  # no instruction
    assert recovery.validate({"action": "nonsense"}, case(), SETTINGS)["action"] == "escalate"
    product = case(blocker={"kind": "product_decision", "question": "Which copy?"})
    assert recovery.validate(resume, product, SETTINGS)["action"] == "escalate"
    sensitive = case(blocker={"kind": "env_blocker", "question": "Run the schema migration?"})
    assert recovery.validate(resume, sensitive, SETTINGS)["action"] == "escalate"
    spent = case(history=[{"action": "resume"}] * 3)
    assert recovery.validate(resume, spent, SETTINGS)["action"] == "escalate"
    escalated = case(history=[{"action": "resume"}] * 3 + [{"action": "escalate"}])
    assert recovery.validate(resume, escalated, SETTINGS)["action"] == "wait"  # escalate once, never loop
    fresh = {"action": "fresh_session", "instruction": "start over", "reason": "wedged"}
    assert recovery.validate(fresh, case(), SETTINGS)["action"] == "fresh_session"
    assert recovery.validate(fresh, case(history=[{"action": "fresh_session"}]), SETTINGS)["action"] == "escalate"
    assert recovery.validate(fresh, case(), {**SETTINGS, "allow_fresh_session": False})["action"] == "escalate"
    assert recovery.validate(resume, case(status="running", trigger="stalled"), SETTINGS)["action"] == "escalate"


class Poller:
    project_name = "Demo"
    config = ProjectConfig(name="Demo", linear_project_slug="demo", heartbeat_seconds=1)

    async def poll(self):
        return PollResult((), "snapshot", False, ())


class Mate:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    async def triage(self, prompt):
        self.prompts.append(prompt)
        return json.dumps(self.answer)


class Linear:
    def __init__(self):
        self.comments = []

    async def create_comment(self, issue_id, body):
        self.comments.append((issue_id, body))
        return {"id": f"c{len(self.comments)}"}


def supervisor_for(tmp_path, db, answer, *, ticket=None):
    workflow = load_workflow(config(tmp_path))
    dispatcher = WorktreeDispatcher(SimpleNamespace(), lambda *a: None, db=db, project="Demo", ticket_scope=ticket)
    dispatcher.workflow = workflow
    mate, linear = Mate(answer), Linear()
    ladder = SimpleNamespace(first_mate=mate, linear=linear, workflow=workflow)
    supervisor = Supervisor(Poller(), db=db, dispatcher=dispatcher, unblock_ladder=ladder, ticket=ticket)
    return supervisor, mate, linear


async def monitor_observed(supervisor):
    observed = {ticket["identifier"] for ticket in supervisor.db.tickets(supervisor.project)}
    await supervisor._monitor_blocked(observed)


def monitor(supervisor):
    """One monitor pass, then wait for the recoveries it scheduled."""
    async def run():
        await monitor_observed(supervisor)
        await supervisor._drain_recoveries()
    asyncio.run(run())


def add_ticket(db, tmp_path, ticket, *, status="blocked", final=HAN86, finished=15, hold=False, state_type=None):
    """Add a second ticket and its run without replacing the first."""
    rows = [dict(r) for r in db.connection.execute("SELECT identifier, title, state, linear_id AS id, hold FROM tickets")]
    tickets = [{**r, "owned": True} for r in rows] + [{"identifier": ticket, "state": "Blocked", "title": ticket,
                                                        "owned": True, "hold": hold, "id": f"lin-{ticket}"}]
    db.replace_tickets("Demo", tickets, emit_events=False)
    if state_type:
        db.connection.execute("UPDATE tickets SET state_type=? WHERE identifier=?", (state_type, ticket))
    worktree = tmp_path / "wt" / ticket
    worktree.mkdir(parents=True, exist_ok=True)
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,session_id,worktree,final_json,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?)",
        (f"run-{ticket}", ticket, status, "native-2", str(worktree), json.dumps(final), iso(60), iso(finished)),
    )
    db.connection.commit()


def flag(db, ticket, kind="no_progress"):
    db.connection.execute(
        "INSERT INTO events(project, identifier, event_type, payload_json, snapshot_hash, at) VALUES (?,?,?,?,?,?)",
        ("Demo", ticket, "watchdog.flag", json.dumps({"kind": kind, "detail": kind}), None, iso(0)))
    db.connection.commit()


def test_han86_replay_first_mate_resumes_with_close_and_respawn(tmp_path):
    db = ledger(tmp_path, final={**HAN86, "summary": "planner child stopped", "blocker": {"kind": "env_blocker", "question": "planner child exited"}})
    answer = {"action": "resume", "reason": "stale interrupted planner holds the name",
              "instruction": "List agents, close the interrupted /root/planner, spawn planner-2 and continue."}
    supervisor, mate, linear = supervisor_for(tmp_path, db, answer)
    resumed = []

    async def resume_session(run_id, prompt):
        resumed.append((run_id, prompt))
        return True

    supervisor.resume_session = resume_session
    monitor(supervisor)
    assert resumed == [("run-HAN-86", answer["instruction"])]
    assert "planner child exited" in mate.prompts[0]
    assert linear.comments and linear.comments[0][1].startswith("[First Mate recovery] resume (blocked)")
    rows = db.connection.execute("SELECT action FROM first_mate_recoveries").fetchall()
    assert [r[0] for r in rows] == ["resume"]
    monitor(supervisor)  # backoff: no second attempt this poll
    assert len(resumed) == 1
    db.close()


def test_han86_replay_fresh_session_keeps_worktree_and_drops_old_session(tmp_path):
    db = ledger(tmp_path, final={**HAN86, "summary": "agent state wedged", "blocker": {"kind": "env_blocker", "question": "builder loops"}})
    answer = {"action": "fresh_session", "reason": "agent state is wedged", "instruction": "Respawn all roles."}
    supervisor, _, _ = supervisor_for(tmp_path, db, answer)
    started = []

    class Runner:
        async def start(self, instruction, *, run_id=None):
            started.append((instruction, run_id))
            return {"status": "completed", "outcome": "no_pr", "summary": "continuing", "run_id": run_id}

    supervisor.dispatcher.runner_factory = lambda worktree, wake: Runner()
    monitor(supervisor)
    assert len(started) == 1 and started[0][1] == "run-HAN-86"
    assert "fresh builder session" in started[0][0] and "Respawn all roles." in started[0][0]
    row = db.connection.execute("SELECT worktree, session_id FROM runs WHERE id='run-HAN-86'").fetchone()
    assert Path(row[0]).is_dir()
    old = db.connection.execute("SELECT old_session_id FROM first_mate_recoveries").fetchone()[0]
    assert old == "native-1"
    db.close()


def test_product_question_is_never_recovered(tmp_path):
    # Even with a summary that mentions the retired registration gate, a
    # product question is never "recovered".
    db = ledger(tmp_path, final={**HAN86, "blocker": {"kind": "product_decision", "question": "Which spacing?"}})
    supervisor, mate, _ = supervisor_for(tmp_path, db, {"action": "resume", "instruction": "pick 8px", "reason": "guess"})
    calls = []

    async def resume_session(*args):
        calls.append(args)
        return True

    supervisor.resume_session = resume_session
    monitor(supervisor)
    assert calls == []
    assert [r[0] for r in db.connection.execute("SELECT action FROM first_mate_recoveries")] == ["escalate"]
    db.close()


def test_watchdog_flag_reaches_first_mate(tmp_path):
    from harbor.watchdog import tick
    db = ledger(tmp_path, status="running", final={}, finished=None, activity=1)
    assert tick(db.connection, "Demo", minutes=30) == []
    db.connection.execute("UPDATE watchdog_state SET changed_at = changed_at - 3600")
    db.connection.commit()
    flags = tick(db.connection, "Demo", minutes=30)
    assert [f["kind"] for f in flags] == ["no_progress"]
    assert tick(db.connection, "Demo", minutes=30) == []  # flagged once
    cases = recovery.find_cases(db.connection, "Demo", SETTINGS)
    assert [c.trigger for c in cases] == ["watchdog:no_progress"]
    recovery.record(db.connection, cases[0], {"action": "wait", "instruction": "", "reason": "progressing"})
    assert recovery.find_cases(db.connection, "Demo", SETTINGS) == []  # flag consumed
    db.close()


def test_obsolete_bookkeeping_blocker_starts_a_fresh_session_then_resumes():
    proposal = {"action": "escalate", "reason": "Harbor cannot verify the planner's provenance"}
    decision = recovery.validate(proposal, case(summary=HAN86["summary"]), SETTINGS)
    assert decision["action"] == "fresh_session"
    assert "no longer registers" in decision["instruction"] and "planner-2" in decision["instruction"]
    used = case(summary=HAN86["summary"], history=[{"action": "fresh_session"}])
    assert recovery.validate(proposal, used, SETTINGS)["action"] == "resume"
    # Live HAN-86 follow-up: the retained session reported a conflict with its old contract.
    conflict = case(blocker={"kind": "conflicting_instruction",
                             "question": "Should the effective Harbor policy be updated to permit delivery without registered roles?",
                             "evidence": "`workflow roles` returned invalid choice"}, history=[{"action": "resume"}])
    assert recovery.validate({"action": "escalate"}, conflict, SETTINGS)["action"] == "fresh_session"
    # Real product questions still escalate; the budget still applies.
    product = case(blocker={"kind": "product_decision", "question": "Which spacing?"})
    assert recovery.validate(proposal, product, SETTINGS)["action"] == "escalate"
    spent = case(summary=HAN86["summary"], history=[{"action": "resume"}] * 3)
    assert recovery.validate(proposal, spent, SETTINGS)["action"] == "escalate"


def test_only_usage_limits_back_off_exponentially():
    plain = [{"action": "resume", "reason": "x"}] * 3
    assert recovery.backoff_seconds(SETTINGS, plain) == 600
    quota = [{"action": "wait", "reason": "usage limit: resets 10:00"}]
    assert [recovery.backoff_seconds(SETTINGS, quota * n) for n in (1, 2, 3, 4)] == [600, 1800, 3600, 3600]
    # A non-quota result ends the streak.
    assert recovery.backoff_seconds(SETTINGS, quota * 3 + plain[:1]) == 600



# ---- Review finding #3: watchdog flags respect hold and closed tickets ----

def _flag_cases(db):
    return [c for c in recovery.find_cases(db.connection, "Demo", SETTINGS) if c.flag_id is not None]


def test_flag_for_held_ticket_is_dropped(tmp_path):
    db = ledger(tmp_path, status="running", final={}, finished=None, activity=1)
    flag(db, "HAN-86")
    db.connection.execute("UPDATE tickets SET hold=1"); db.connection.commit()
    assert _flag_cases(db) == []
    assert db.connection.execute("SELECT COUNT(*) FROM first_mate_flags_seen").fetchone()[0] == 1  # handled, not retried
    db.close()


def test_flag_for_canceled_ticket_is_dropped(tmp_path):
    db = ledger(tmp_path, status="running", final={}, finished=None, activity=1)
    flag(db, "HAN-86")
    db.connection.execute("UPDATE tickets SET state_type='canceled'"); db.connection.commit()
    assert _flag_cases(db) == []
    db.close()


def test_repeated_answer_flag_respects_hold(tmp_path):
    db = ledger(tmp_path, finished=1)
    flag(db, "HAN-86", "repeated_answer")
    db.connection.execute("UPDATE tickets SET hold=1"); db.connection.commit()
    assert _flag_cases(db) == []
    db.close()


def test_cleanup_flag_for_done_ticket_still_cleans(tmp_path):
    db = ledger(tmp_path, status="completed", final={"outcome": "merged"}, finished=45)
    flag(db, "HAN-86", "cleanup_stuck")
    db.connection.execute("UPDATE tickets SET state_type='completed'"); db.connection.commit()
    assert [c.trigger for c in _flag_cases(db)] == ["watchdog:cleanup_stuck"]
    db.close()


# ---- Review finding #7: First Mate respects the --ticket scope ----

def test_scoped_supervisor_ignores_other_tickets(tmp_path):
    db = ledger(tmp_path)                       # recoverable HAN-86
    add_ticket(db, tmp_path, "HAN-99", status="completed", final={"status": "completed"})
    flag(db, "HAN-86")
    supervisor, mate, linear = supervisor_for(tmp_path, db, {"action": "resume", "instruction": "x", "reason": "r"}, ticket="HAN-99")
    monitor(supervisor)
    assert mate.prompts == [] and linear.comments == []
    assert db.connection.execute("SELECT COUNT(*) FROM first_mate_recoveries").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM first_mate_flags_seen").fetchone()[0] == 0  # left for its owner
    db.close()


def test_unscoped_supervisor_then_handles_the_flag(tmp_path):
    db = ledger(tmp_path)
    flag(db, "HAN-86")
    supervisor, mate, _ = supervisor_for(tmp_path, db, {"action": "wait", "reason": "later"})
    monitor(supervisor)
    assert len(mate.prompts) == 1
    assert db.connection.execute("SELECT COUNT(*) FROM first_mate_flags_seen").fetchone()[0] == 1
    db.close()



# ---- Review findings #2 and #4: recovery runs in the background ----

NEUTRAL = {"status": "blocked", "outcome": "no_pr", "waiting_on": "human", "summary": "builder stopped",
           "blocker": {"kind": "env_blocker", "question": "builder process exited"}}


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)

def test_recovery_does_not_block_polling(tmp_path):
    db = ledger(tmp_path, final=NEUTRAL)
    supervisor, mate, _ = supervisor_for(tmp_path, db, {"action": "resume", "instruction": "continue", "reason": "r"})
    never = asyncio.Event()
    calls = []

    async def resume_session(run_id, prompt):
        calls.append(run_id)
        await never.wait()  # a builder that never returns
        return True

    supervisor.resume_session = resume_session

    async def scenario():
        await asyncio.wait_for(monitor_observed(supervisor), timeout=1)
        await asyncio.wait_for(monitor_observed(supervisor), timeout=1)  # a second poll is not stuck either
        await settle()
        names = [t.get_name() for t in supervisor._dispatch_tasks.values()]
        assert names == ["harbor-recovery-HAN-86"]
        assert calls == ["run-HAN-86"] and len(mate.prompts) == 1  # one recovery per ticket
        for task in list(supervisor._dispatch_tasks.values()):
            task.cancel()
        await asyncio.gather(*supervisor._dispatch_tasks.values(), return_exceptions=True)

    asyncio.run(scenario())
    db.close()


def test_recovery_task_blocks_duplicate_wake(tmp_path):
    db = ledger(tmp_path, final=NEUTRAL)
    supervisor, _, _ = supervisor_for(tmp_path, db, {"action": "resume", "instruction": "continue", "reason": "r"})
    never = asyncio.Event()
    delivered = []

    async def resume_session(run_id, prompt):
        await never.wait()
        return True

    async def deliver(wake):
        delivered.append(wake)

    supervisor.resume_session = resume_session
    supervisor._deliver_scheduled_wake = deliver

    async def scenario():
        await monitor_observed(supervisor)
        supervisor._schedule_wake(Wake(ticket={"identifier": "HAN-86"}, action="reengage", resume="run-HAN-86"))
        await asyncio.sleep(0)
        assert delivered == []  # never two builders on one ticket
        for task in list(supervisor._dispatch_tasks.values()):
            task.cancel()
        await asyncio.gather(*supervisor._dispatch_tasks.values(), return_exceptions=True)

    asyncio.run(scenario())
    db.close()


def test_stalled_live_delivery_is_cancelled_and_replaced(tmp_path):
    db = ledger(tmp_path, status="running", final={}, finished=None, activity=45)
    flag(db, "HAN-86", "no_progress")
    supervisor, mate, _ = supervisor_for(tmp_path, db, {"action": "fresh_session", "instruction": "start over", "reason": "hung"})
    replaced = []

    async def fresh_session(identifier, instruction):
        replaced.append(identifier)
        return {"status": "completed", "outcome": "no_pr", "summary": "ok", "run_id": "run-HAN-86"}

    supervisor.dispatcher.fresh_session = fresh_session

    async def scenario():
        hung = asyncio.create_task(asyncio.Event().wait(), name="harbor-dispatch-HAN-86")
        supervisor._dispatch_tasks["HAN-86"] = hung
        await monitor_observed(supervisor)
        await supervisor._drain_recoveries()
        assert hung.cancelled()
        assert replaced == ["HAN-86"] and len(mate.prompts) == 1

    asyncio.run(scenario())
    db.close()


def test_live_delivery_that_is_not_stalled_is_left_alone(tmp_path):
    db = ledger(tmp_path)  # blocked-looking case, but a delivery is active
    supervisor, mate, _ = supervisor_for(tmp_path, db, {"action": "resume", "instruction": "x", "reason": "r"})

    async def scenario():
        active = asyncio.create_task(asyncio.Event().wait(), name="harbor-dispatch-HAN-86")
        supervisor._dispatch_tasks["HAN-86"] = active
        await monitor_observed(supervisor)
        assert mate.prompts == [] and not active.cancelled()
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)

    asyncio.run(scenario())
    db.close()


def test_recovery_failure_escalates_and_frees_the_ticket(tmp_path):
    db = ledger(tmp_path, final=NEUTRAL)
    supervisor, _, _ = supervisor_for(tmp_path, db, {"action": "resume", "instruction": "x", "reason": "r"})
    notices = []

    async def resume_session(run_id, prompt):
        raise RuntimeError("session gone")

    async def notify(key, message):
        notices.append(message)
        return True

    supervisor.resume_session = resume_session
    supervisor._notify_operator = notify
    monitor(supervisor)
    actions = [r[0] for r in db.connection.execute("SELECT action FROM first_mate_recoveries ORDER BY id")]
    assert actions == ["resume", "escalate"]
    assert any("could not resume" in n for n in notices)
    assert supervisor._recovery_tickets == set() and "HAN-86" not in supervisor._dispatch_tasks
    db.close()


def test_successful_recovery_notifies_after_completion(tmp_path):
    db = ledger(tmp_path, final=NEUTRAL)
    supervisor, _, _ = supervisor_for(tmp_path, db, {"action": "resume", "instruction": "x", "reason": "r"})
    events = []
    gate = asyncio.Event()

    async def resume_session(run_id, prompt):
        events.append("builder-start")
        await gate.wait()
        events.append("builder-done")
        return True

    async def notify(key, message):
        events.append("notify")
        return True

    supervisor.resume_session = resume_session
    supervisor._notify_operator = notify

    async def scenario():
        await monitor_observed(supervisor)
        await settle()
        assert events == ["builder-start"]  # not announced at scheduling time  # not announced at scheduling time
        gate.set()
        await supervisor._drain_recoveries()

    asyncio.run(scenario())
    assert events == ["builder-start", "builder-done", "notify"]
    db.close()


def test_stalled_real_codex_process_is_killed_before_replacement(tmp_path):
    """Live-shape check for finding #4: a hung `codex exec` subprocess."""
    import sys
    from harbor.runner.base import process_alive
    from harbor.runner.codex import CodexRunner

    hung = tmp_path / "hung-codex.py"
    hung.write_text("#!/usr/bin/env python3\nimport json, time\n"
                    "print(json.dumps({'type': 'heartbeat'}), flush=True)\ntime.sleep(120)\n")
    hung.chmod(0o755)
    db = ledger(tmp_path, status="running", final={}, finished=None, activity=1, ticket="DEM-2")
    db.connection.execute("DELETE FROM runs")  # the real runner owns its run row
    db.connection.commit()
    supervisor, mate, _ = supervisor_for(tmp_path, db, {"action": "fresh_session", "instruction": "start over", "reason": "hung"})
    replaced = []

    async def fresh_session(identifier, instruction):
        replaced.append(identifier)
        return {"status": "completed", "outcome": "no_pr", "summary": "ok", "run_id": "retained"}

    supervisor.dispatcher.fresh_session = fresh_session

    async def scenario():
        runner = CodexRunner(tmp_path / "wt" / "DEM-2", db=db, ticket="DEM-2", executable=str(hung), run_id="retained")
        delivery = asyncio.create_task(runner.start("continue"), name="harbor-dispatch-DEM-2")
        supervisor._dispatch_tasks["DEM-2"] = delivery
        for _ in range(200):
            if runner.active:
                break
            await asyncio.sleep(0.01)
        pid = runner.active.pid
        db.connection.execute("UPDATE runs SET last_activity_at=?, started_at=? WHERE id='retained'", (iso(45), iso(60)))
        db.connection.commit()
        await monitor_observed(supervisor)
        await supervisor._drain_recoveries()
        assert delivery.done() and not process_alive(pid)
        row = db.connection.execute("SELECT status, terminal_reason FROM runs WHERE id='retained'").fetchone()
        assert row[0] == "terminated"
        assert replaced == ["DEM-2"] and len(mate.prompts) == 1

    asyncio.run(scenario())
    db.close()


# ---- First Mate never recovers a shelved (backlog-type or paused) ticket ----

def _shelve(db, *, state, state_type):
    db.connection.execute("UPDATE tickets SET state=?, state_type=?", (state, state_type)); db.connection.commit()


def _recording_supervisor(tmp_path, db):
    answer = {"action": "resume", "instruction": "close the stale child and continue", "reason": "stuck"}
    supervisor, mate, linear = supervisor_for(tmp_path, db, answer)
    resumed = []

    async def resume_session(run_id, prompt):
        resumed.append(run_id)
        return True

    supervisor.resume_session = resume_session
    return supervisor, mate, linear, resumed


ENV_BLOCKED = {**HAN86, "summary": "builder stopped", "blocker": {"kind": "env_blocker", "question": "builder exited"}}


def test_blocked_run_on_custom_backlog_state_is_not_recovered(tmp_path):
    # The operator moved the ticket to a custom backlog-type state ("Icebox").
    db = ledger(tmp_path, final=ENV_BLOCKED)
    _shelve(db, state="Icebox", state_type="backlog")
    assert recovery.find_cases(db.connection, "Demo", SETTINGS) == []
    supervisor, mate, linear, resumed = _recording_supervisor(tmp_path, db)
    monitor(supervisor)
    assert mate.prompts == [] and linear.comments == [] and resumed == []
    assert db.connection.execute("SELECT COUNT(*) FROM first_mate_recoveries").fetchone()[0] == 0
    db.close()


def test_failed_and_stalled_runs_on_backlog_state_are_not_recovered(tmp_path):
    db = ledger(tmp_path, status="failed", final={"status": "failed"})
    _shelve(db, state="Icebox", state_type="backlog")
    assert recovery.find_cases(db.connection, "Demo", SETTINGS) == []
    db.close()
    (tmp_path / "stalled").mkdir()
    stalled = ledger(tmp_path / "stalled", status="running", final={}, finished=None, activity=60)
    _shelve(stalled, state="Icebox", state_type="backlog")
    assert recovery.find_cases(stalled.connection, "Demo", SETTINGS) == []
    stalled.close()


def test_watchdog_flag_for_backlog_ticket_is_dropped(tmp_path):
    db = ledger(tmp_path, status="running", final={}, finished=None, activity=1)
    flag(db, "HAN-86")
    _shelve(db, state="Icebox", state_type="backlog")
    assert _flag_cases(db) == []
    assert db.connection.execute("SELECT COUNT(*) FROM first_mate_flags_seen").fetchone()[0] == 1  # handled, not retried
    db.close()


def _monitor_in_state(tmp_path, name):
    root = tmp_path / name.replace(" ", "-")
    root.mkdir()
    db = ledger(root, final=ENV_BLOCKED)
    _shelve(db, state=name, state_type="started")
    supervisor, mate, _, resumed = _recording_supervisor(root, db)
    supervisor.dispatcher.workflow.config["states"] = {"paused": ["On Hold"], "terminal": ["Shipped"]}
    monitor(supervisor)
    db.close()
    return mate.prompts, resumed


def test_workflow_paused_and_terminal_states_are_not_recovered(tmp_path):
    for name in ("On Hold", "Shipped", "Paused"):
        prompts, resumed = _monitor_in_state(tmp_path, name)
        assert prompts == [] and resumed == [], name


def test_in_progress_and_human_review_are_still_recovered(tmp_path):
    for name in ("In Progress", "Human Review"):
        prompts, resumed = _monitor_in_state(tmp_path, name)
        assert len(prompts) == 1 and resumed == ["run-HAN-86"], name


@pytest.mark.parametrize(
    ("state", "state_type", "recover"),
    [
        ("Icebox", "backlog", False),
        ("Backlog", "backlog", False),
        ("Shipped", "completed", False),
        ("In Progress", "started", True),
    ],
)
def test_first_mate_requires_a_fresh_polled_ticket(tmp_path, state, state_type, recover):
    db = ledger(tmp_path, final=ENV_BLOCKED)
    supervisor, mate, linear, resumed = _recording_supervisor(tmp_path, db)
    supervisor.dispatcher.workflow.config["states"] = {"terminal": ["Shipped"]}
    issue = {
        "identifier": "HAN-86",
        "id": "lin-86",
        "title": "Sample issue",
        "state": {"name": "In Progress", "type": "started"},
    }

    def linear_issues(request):
        variables = json.loads(request.content)["variables"]
        assert variables["slugs"] == ["demo"]
        nodes = [issue] if issue["state"]["name"] in variables["states"] else []
        return httpx.Response(200, json={"data": {"issues": {"nodes": nodes}}})

    client = LinearClient(token="test-key", transport=httpx.MockTransport(linear_issues))
    supervisor.poller = LinearPoller(client, Poller.config, db=db)

    async def scenario():
        observed = await supervisor.poller.poll()
        assert [ticket["identifier"] for ticket in observed.tickets] == ["HAN-86"]
        issue["state"] = {"name": state, "type": state_type}
        observed = await supervisor.poll_once()
        await supervisor._drain_recoveries()
        assert observed is not None
        assert [ticket["identifier"] for ticket in observed.tickets] == (["HAN-86"] if recover else [])
        # An unpolled issue still has its previous observation in SQLite.
        assert db.tickets("Demo")[0]["state"] == "In Progress"

    try:
        asyncio.run(scenario())
        assert resumed == (["run-HAN-86"] if recover else [])
        assert len(mate.prompts) == len(linear.comments) == int(recover)
        receipts = db.connection.execute("SELECT COUNT(*) FROM first_mate_recoveries").fetchone()[0]
        assert receipts == int(recover)
    finally:
        asyncio.run(client.close())
        db.close()


def test_first_mate_keeps_cleanup_for_an_unpolled_done_ticket(tmp_path):
    db = ledger(tmp_path, status="completed", final={"status": "completed", "outcome": "merged"})
    _shelve(db, state="Done", state_type="completed")
    flag(db, "HAN-86", "cleanup_stuck")
    supervisor, mate, linear, resumed = _recording_supervisor(tmp_path, db)
    reconciled = []

    async def reconcile(identifier, *, refresh):
        reconciled.append((identifier, refresh))

    supervisor._reconcile_completed_merges = reconcile
    try:
        asyncio.run(supervisor._monitor_blocked(set()))
        assert reconciled == [("HAN-86", True)]
        assert mate.prompts == linear.comments == resumed == []
    finally:
        db.close()


def test_watchdog_flag_for_an_unpolled_ticket_does_not_recover_it_later(tmp_path):
    # The watchdog flags HAN-86, then the operator moves it to a backlog-type
    # state before the next poll. SQLite still holds its old active state, so
    # find_cases keeps the flag; the poll no longer observes the ticket.
    db = ledger(tmp_path, status="running", final={}, finished=None, activity=1)
    _shelve(db, state="In Progress", state_type="started")
    flag(db, "HAN-86")
    supervisor, mate, linear, resumed = _recording_supervisor(tmp_path, db)

    async def scenario():
        await supervisor._monitor_blocked(set())  # unpolled pass
        await supervisor._drain_recoveries()
        seen = db.connection.execute("SELECT COUNT(*) FROM first_mate_flags_seen").fetchone()[0]
        assert seen == 1  # the stale flag is consumed, not left pending
        # Days later the ticket returns to In Progress and is polled again.
        await supervisor._monitor_blocked({"HAN-86"})
        await supervisor._drain_recoveries()

    try:
        asyncio.run(scenario())
        assert mate.prompts == [] and linear.comments == [] and resumed == []
        assert db.connection.execute("SELECT COUNT(*) FROM first_mate_recoveries").fetchone()[0] == 0
    finally:
        db.close()
