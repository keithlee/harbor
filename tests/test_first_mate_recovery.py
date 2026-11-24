"""First Mate monitor-and-recover, including a replay of the HAN-86 deadlock."""
import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import replace
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


async def monitor_observed(supervisor, tickets=None, *, flags_before=None):
    if tickets is None:
        tickets = supervisor.db.tickets(supervisor.project)
    observation = PollResult(tuple(tickets), "snapshot", True, ())
    supervisor.last_result = observation
    supervisor.linear_healthy = True
    await supervisor._monitor_blocked(observation, flags_before=flags_before)


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

    async def resume_session(run_id, prompt, *, before_launch=None):
        if before_launch is not None:
            before_launch()
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

    async def resume_session(run_id, prompt, *, before_launch=None):
        if before_launch is not None:
            before_launch()
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

    async def resume_session(run_id, prompt, *, before_launch=None):
        if before_launch is not None:
            before_launch()
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

    async def fresh_session(identifier, instruction, *, before_launch=None):
        if before_launch is not None:
            before_launch()
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

    async def resume_session(run_id, prompt, *, before_launch=None):
        if before_launch is not None:
            before_launch()
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

    async def resume_session(run_id, prompt, *, before_launch=None):
        if before_launch is not None:
            before_launch()
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

    async def fresh_session(identifier, instruction, *, before_launch=None):
        if before_launch is not None:
            before_launch()
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

    async def resume_session(run_id, prompt, *, before_launch=None):
        if before_launch is not None:
            before_launch()
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


def _linear_observation(supervisor, db, *, poll_states=Poller.config.poll_states):
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
        return httpx.Response(200, json={"data": {"issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}})

    client = LinearClient(token="test-key", transport=httpx.MockTransport(linear_issues))
    supervisor.poller = LinearPoller(client, replace(Poller.config, poll_states=poll_states), db=db)
    return client, issue


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
    client, issue = _linear_observation(supervisor, db)

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


@pytest.mark.parametrize("blocked_on", ["github", "triage"])
def test_first_mate_cannot_recover_from_an_older_overlapping_poll(tmp_path, blocked_on):
    db = ledger(tmp_path, final=ENV_BLOCKED)
    supervisor, mate, linear, resumed = _recording_supervisor(tmp_path, db)
    client, issue = _linear_observation(supervisor, db)

    async def scenario():
        older_waiting, release_older = asyncio.Event(), asyncio.Event()

        async def github(tickets):
            if tickets:
                older_waiting.set()
                await release_older.wait()
            return []

        original_triage = mate.triage

        async def triage(prompt):
            proposal = await original_triage(prompt)
            older_waiting.set()
            await release_older.wait()
            return proposal

        if blocked_on == "github":
            supervisor.github_poller = github
        else:
            mate.triage = triage
        older = asyncio.create_task(supervisor.poll_once())
        try:
            await asyncio.wait_for(older_waiting.wait(), timeout=2)
            issue["state"] = {"name": "Icebox", "type": "backlog"}
            latest = await asyncio.wait_for(supervisor.refresh(), timeout=2)
            assert latest.tickets == ()
            assert db.tickets("Demo")[0]["state"] == "In Progress"
        finally:
            release_older.set()
            previous = await asyncio.wait_for(older, timeout=2)
        assert [ticket["identifier"] for ticket in previous.tickets] == ["HAN-86"]
        await supervisor._drain_recoveries()

    try:
        asyncio.run(scenario())
        assert len(mate.prompts) == int(blocked_on == "triage")
        assert linear.comments == resumed == []
        assert db.connection.execute("SELECT COUNT(*) FROM first_mate_recoveries").fetchone()[0] == 0
    finally:
        asyncio.run(client.close())
        db.close()


def test_first_mate_rechecks_state_after_a_failed_snapshot_write(tmp_path):
    db = ledger(tmp_path, final=ENV_BLOCKED)
    supervisor, mate, linear, resumed = _recording_supervisor(tmp_path, db)
    client, issue = _linear_observation(supervisor, db, poll_states=(*Poller.config.poll_states, "Icebox"))

    async def scenario():
        previous = await supervisor.poller.poll()
        db.connection.execute("""
            CREATE TEMP TRIGGER fail_icebox_write BEFORE UPDATE OF state ON tickets
            WHEN NEW.state = 'Icebox' BEGIN SELECT RAISE(ABORT, 'temporary failure'); END
        """)
        issue["state"] = {"name": "Icebox", "type": "backlog"}
        assert await supervisor.poll_once() is None
        assert supervisor.linear_healthy is False
        digest_after_failure = db.snapshot_hash("Demo")
        db.connection.execute("DROP TRIGGER fail_icebox_write")
        latest = await supervisor.refresh()
        await supervisor._drain_recoveries()
        assert latest.tickets[0]["state"] == "Icebox"
        assert mate.prompts == linear.comments == resumed == []
        assert db.connection.execute("SELECT COUNT(*) FROM first_mate_recoveries").fetchone()[0] == 0
        assert digest_after_failure == previous.snapshot_hash
        assert latest.changed and db.snapshot_hash("Demo") == latest.snapshot_hash
        assert db.tickets("Demo")[0]["state_type"] == "backlog"

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(client.close())
        db.close()


def test_linear_observations_publish_in_request_order(tmp_path):
    db = ledger(tmp_path, final=ENV_BLOCKED)
    supervisor, mate, linear, resumed = _recording_supervisor(tmp_path, db)
    client, issue = _linear_observation(supervisor, db)

    async def scenario():
        first_response, release_response, release_github = (asyncio.Event() for _ in range(3))
        refresh_started = asyncio.Event()
        fetch_issues = client.fetch_issues

        async def fetch(*args):
            nodes = await fetch_issues(*args)
            if nodes:
                first_response.set()
                await release_response.wait()
            return nodes

        async def github(tickets):
            if tickets:
                await release_github.wait()
            return []

        async def refresh():
            refresh_started.set()
            return await supervisor.refresh()

        client.fetch_issues = fetch
        supervisor.github_poller = github
        older = asyncio.create_task(supervisor.poll_once())
        await asyncio.wait_for(first_response.wait(), timeout=2)
        issue["state"] = {"name": "Icebox", "type": "backlog"}
        newer = asyncio.create_task(refresh())
        await asyncio.wait_for(refresh_started.wait(), timeout=2)
        release_response.set()
        try:
            latest = await asyncio.wait_for(newer, timeout=2)
            assert latest.tickets == ()
        finally:
            release_github.set()
            await asyncio.wait_for(older, timeout=2)
        await supervisor._drain_recoveries()
        assert supervisor.last_result is latest
        assert db.snapshot_hash("Demo") == latest.snapshot_hash
        assert mate.prompts == linear.comments == resumed == []

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(client.close())
        db.close()


@pytest.mark.parametrize("action", ["resume", "retry", "fresh_session"])
def test_first_mate_rechecks_eligibility_after_runner_hydration(tmp_path, action):
    status = "failed" if action == "retry" else "blocked"
    db = ledger(tmp_path, status=status, final=ENV_BLOCKED)
    decision = {"action": action, "instruction": "continue", "reason": "stuck"}
    supervisor, mate, linear = supervisor_for(tmp_path, db, decision)
    client, issue = _linear_observation(supervisor, db)
    launches = []

    class Runner:
        async def resume(self, run_id, instruction):
            launches.append(("resume", run_id))
            return {"status": "completed", "outcome": "no_pr", "run_id": run_id}

        async def start(self, instruction, *, run_id=None):
            launches.append(("start", run_id))
            return {"status": "completed", "outcome": "no_pr", "run_id": run_id}

    async def scenario():
        hydrating, release_hydration = asyncio.Event(), asyncio.Event()

        async def factory(worktree, wake):
            hydrating.set()
            await release_hydration.wait()
            return Runner()

        supervisor.dispatcher.runner_factory = factory
        await supervisor.poll_once()
        try:
            await asyncio.wait_for(hydrating.wait(), timeout=2)
            issue["state"] = {"name": "Icebox", "type": "backlog"}
            latest = await asyncio.wait_for(supervisor.refresh(), timeout=2)
            assert latest.tickets == ()
        finally:
            release_hydration.set()
            await supervisor._drain_recoveries()
        assert not launches
        assert len(mate.prompts) == len(linear.comments) == 1
        assert recovery.history(db.connection, "run-HAN-86") == []
        row = supervisor.dispatcher._persisted_run("HAN-86")
        assert (row["status"], row["session_id"]) == (status, "native-1")
        assert not db.queue_has_idem("HAN-86@retry@run-HAN-86", live_only=False)

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(client.close())
        db.close()


def test_first_mate_preserves_fresh_session_attempt_when_title_changes(tmp_path):
    db = ledger(tmp_path, final=ENV_BLOCKED)
    decision = {"action": "fresh_session", "instruction": "start over", "reason": "stuck"}
    supervisor, mate, linear = supervisor_for(tmp_path, db, decision)
    client, issue = _linear_observation(supervisor, db)
    launches = []

    class Runner:
        async def start(self, instruction, *, run_id=None):
            launches.append(run_id)
            return {"status": "completed", "outcome": "no_pr", "run_id": run_id}

    async def scenario():
        posting, release_comment = asyncio.Event(), asyncio.Event()
        create_comment = linear.create_comment

        async def comment(issue_id, body):
            posting.set()
            await release_comment.wait()
            return await create_comment(issue_id, body)

        supervisor.dispatcher.runner_factory = lambda worktree, wake: Runner()
        linear.create_comment = comment
        older = asyncio.create_task(supervisor.poll_once())
        try:
            await asyncio.wait_for(posting.wait(), timeout=2)
            issue["title"] = "Renamed while the comment was pending"
            latest = await asyncio.wait_for(supervisor.refresh(), timeout=2)
            assert latest.tickets[0]["state"] == "In Progress"
            queued_actions = recovery.history(db.connection, "run-HAN-86")
        finally:
            release_comment.set()
            await asyncio.wait_for(older, timeout=2)
        await supervisor._drain_recoveries()
        assert queued_actions == []
        assert launches == ["run-HAN-86"]
        assert len(mate.prompts) == len(linear.comments) == 1
        assert [item["action"] for item in recovery.history(db.connection, "run-HAN-86")] == ["fresh_session"]

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(client.close())
        db.close()


@pytest.mark.parametrize("change", ["unrelated_ticket", "recovering_ticket_shelved"])
def test_first_mate_does_not_charge_a_recovery_abandoned_during_the_comment(tmp_path, change):
    """A newer poll while the recovery comment is posting must not consume budget.

    An unrelated ticket changing leaves the recovering ticket eligible, so the
    fresh session still runs and is recorded once.  When the recovering ticket
    itself is shelved, nothing runs, and nothing is recorded: the run keeps its
    whole recovery budget, including its single fresh-session allowance.
    """
    db = ledger(tmp_path, final=ENV_BLOCKED)
    decision = {"action": "fresh_session", "instruction": "start over", "reason": "stuck"}
    supervisor, mate, linear = supervisor_for(tmp_path, db, decision)
    recovering = {"identifier": "HAN-86", "id": "lin-86", "title": "Sample issue",
                  "state": {"name": "In Progress", "type": "started"}}
    unrelated = {"identifier": "HAN-99", "id": "lin-99", "title": "Unrelated issue",
                 "state": {"name": "Human Review", "type": "started"}}

    def linear_issues(request):
        states = json.loads(request.content)["variables"]["states"]
        nodes = [issue for issue in (recovering, unrelated) if issue["state"]["name"] in states]
        return httpx.Response(200, json={"data": {"issues": {"nodes": nodes}}})

    client = LinearClient(token="test-key", transport=httpx.MockTransport(linear_issues))
    supervisor.poller = LinearPoller(client, Poller.config, db=db)
    launches = []

    class Runner:
        async def start(self, instruction, *, run_id=None):
            launches.append(run_id)
            return {"status": "completed", "outcome": "no_pr", "run_id": run_id}

    async def scenario():
        posting, release_comment = asyncio.Event(), asyncio.Event()
        create_comment = linear.create_comment

        async def comment(issue_id, body):
            posting.set()
            await release_comment.wait()
            return await create_comment(issue_id, body)

        supervisor.dispatcher.runner_factory = lambda worktree, wake: Runner()
        linear.create_comment = comment
        older = asyncio.create_task(supervisor.poll_once())
        try:
            await asyncio.wait_for(posting.wait(), timeout=2)
            if change == "unrelated_ticket":
                unrelated["title"] = "Renamed while the comment was pending"
            else:
                recovering["state"] = {"name": "Icebox", "type": "backlog"}
            previous_hash = supervisor.last_result.snapshot_hash
            latest = await asyncio.wait_for(supervisor.refresh(), timeout=2)
            assert latest.snapshot_hash != previous_hash
        finally:
            release_comment.set()
            await asyncio.wait_for(older, timeout=2)
        await supervisor._drain_recoveries()

    try:
        asyncio.run(scenario())
        assert len(mate.prompts) == len(linear.comments) == 1
        charged = [item["action"] for item in recovery.history(db.connection, "run-HAN-86")]
        if change == "unrelated_ticket":
            assert launches == ["run-HAN-86"]
            assert charged == ["fresh_session"]
        else:
            assert launches == []
            # No receipt: the unexecuted fresh session spent neither the
            # recovery budget nor the run's single fresh-session allowance.
            assert charged == []
    finally:
        asyncio.run(client.close())
        db.close()


@pytest.mark.parametrize(
    ("action", "change"),
    [("resume", "icebox"), ("retry", "icebox"), ("fresh_session", "icebox"),
     ("resume", "paused_policy"), ("retry", "paused_policy"), ("fresh_session", "paused_policy"),
     ("resume", "paused_browser_policy"), ("retry", "paused_browser_policy"),
     ("fresh_session", "paused_browser_policy"),
     ("fresh_session", "title")],
)
def test_first_mate_authorizes_actual_codex_spawn_after_async_preparation(tmp_path, monkeypatch, action, change):
    from harbor.runner.codex import CodexRunner

    status = "failed" if action == "retry" else "blocked"
    db = ledger(tmp_path, status=status, final=ENV_BLOCKED)
    decision = {"action": action, "instruction": "continue", "reason": "stuck"}
    supervisor, mate, linear = supervisor_for(tmp_path, db, decision)
    client, issue = _linear_observation(supervisor, db)
    launches = tmp_path / "launched"
    executable = tmp_path / "record-codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(launches)!r}).write_text('launched')\n"
        "sys.stdin.read()\n"
    )
    executable.chmod(0o755)

    async def scenario():
        preparing, release_preparation = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def runtime(image, *, lifetime_seconds):
            if change != "paused_policy":
                preparing.set()
                await release_preparation.wait()
            yield "ws://127.0.0.1:3000/test", ""

        async def factory(worktree, wake):
            if change == "paused_policy":
                preparing.set()
                await release_preparation.wait()
            return CodexRunner(
                worktree, db=db, ticket="HAN-86", run_id=wake.run_id,
                executable=str(executable), playwright_browser_image=f"example/browser@sha256:{'a' * 64}",
            )

        monkeypatch.setattr("harbor.runner.codex.browser_endpoint", runtime)
        supervisor.dispatcher.runner_factory = factory
        await supervisor.poll_once()
        try:
            await asyncio.wait_for(preparing.wait(), timeout=2)
            if change == "icebox":
                issue["state"] = {"name": "Icebox", "type": "backlog"}
            elif change in {"paused_policy", "paused_browser_policy"}:
                workflow = supervisor.dispatcher.workflow.package / "workflow.toml"
                workflow.write_text(workflow.read_text() + '\n[states]\npaused = ["In Progress"]\n')
            else:
                issue["title"] = "Renamed during browser preparation"
            latest = await asyncio.wait_for(supervisor.refresh(), timeout=2)
            assert len(latest.tickets) == int(change != "icebox")
            early_attempts = recovery.history(db.connection, "run-HAN-86")
        finally:
            release_preparation.set()
            await supervisor._drain_recoveries()

        assert launches.exists() == (change == "title")
        assert early_attempts == []
        assert len(mate.prompts) == len(linear.comments) == 1
        actions = [item["action"] for item in recovery.history(db.connection, "run-HAN-86")]
        assert actions == (["fresh_session"] if change == "title" else [])
        if change != "title":
            row = supervisor.dispatcher._persisted_run("HAN-86")
            assert (row["status"], row["session_id"]) == (status, "native-1")
            assert not db.queue_has_idem("HAN-86@retry@run-HAN-86", live_only=False)

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(client.close())
        db.close()


@pytest.mark.parametrize("shelved", [True, False])
def test_first_mate_rechecks_authorization_before_model_fallback(tmp_path, shelved):
    from harbor.runner.codex import CodexRunner
    from test_model_fallback import provider_error

    db = ledger(tmp_path, final=ENV_BLOCKED)
    decision = {"action": "resume", "instruction": "continue", "reason": "stuck"}
    supervisor, _, _ = supervisor_for(tmp_path, db, decision)
    client, issue = _linear_observation(supervisor, db)
    release = tmp_path / "release"
    launches = tmp_path / "models"
    executable = tmp_path / "record-codex"
    rejection = {"type": "error", "message": json.dumps(provider_error())}
    final = {"type": "result", "result": {"status": "waiting", "outcome": "no_pr", "waiting_on": "human",
                                         "summary": "fallback worked", "next_action": "wait", "pr_url": None, "blocker": None}}
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "model = sys.argv[sys.argv.index('-m') + 1]\n"
        "sys.stdin.read()\n"
        f"with Path({str(launches)!r}).open('a') as stream:\n"
        "    stream.write(model + '\\n')\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'native-1'}), flush=True)\n"
        "if model == 'primary':\n"
        f"    while not Path({str(release)!r}).exists():\n"
        "        time.sleep(0.01)\n"
        f"    print(json.dumps({rejection!r}), flush=True)\n"
        "    raise SystemExit(1)\n"
        f"print(json.dumps({final!r}), flush=True)\n"
    )
    executable.chmod(0o755)

    def factory(worktree, wake):
        return CodexRunner(worktree, db=db, ticket="HAN-86", model="primary", fallback_models=["fallback"],
                           executable=str(executable), run_id=wake.run_id)

    async def scenario():
        supervisor.dispatcher.runner_factory = factory
        await supervisor.poll_once()
        try:
            async with asyncio.timeout(3):
                while not launches.exists():
                    await asyncio.sleep(0.01)
            if shelved:
                issue["state"] = {"name": "Icebox", "type": "backlog"}
            else:
                issue["title"] = "Renamed during primary builder process"
            latest = await supervisor.refresh()
            assert len(latest.tickets) == int(not shelved)
        finally:
            release.touch()
            await supervisor._drain_recoveries()
        assert launches.read_text().splitlines() == (["primary"] if shelved else ["primary", "fallback"])
        attempts = recovery.history(db.connection, "run-HAN-86")
        assert [item["action"] for item in attempts] == ["resume"]
        fallback_receipts = db.connection.execute("SELECT COUNT(*) FROM workflow_receipts WHERE kind='model_fallback'").fetchone()[0]
        assert fallback_receipts == int(not shelved)

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(client.close())
        db.close()


@pytest.mark.parametrize("state_type", [" Backlog", "BACKLOG", "Completed ", " canceled"])
def test_launch_recheck_and_find_cases_agree_on_an_unnormalized_stopped_state_type(tmp_path, state_type):
    """The launch-time recheck and ``find_cases`` share ``ticket_states.is_stopped``.

    A separate, unnormalized stopped check would call " Backlog" eligible at
    launch while ``find_cases`` (which normalizes) calls it stopped.
    """
    db = ledger(tmp_path, final=ENV_BLOCKED)
    supervisor, mate, linear, resumed = _recording_supervisor(tmp_path, db)

    def observe():
        supervisor.last_result = PollResult(tuple(db.tickets("Demo")), "snapshot", True, ())
        supervisor.linear_healthy = True

    _shelve(db, state="In Progress", state_type="started")
    [case] = recovery.find_cases(db.connection, "Demo", SETTINGS)
    observe()
    assert supervisor._first_mate_can_recover(case) is True

    _shelve(db, state="Icebox", state_type=state_type)
    observe()
    assert recovery.find_cases(db.connection, "Demo", SETTINGS) == []
    assert supervisor._first_mate_can_recover(case) is False
    assert supervisor._first_mate_can_recover(case, reload_policy=True) is False
    assert supervisor._first_mate_can_recover(case, reload_policy=True, attempted=True) is False
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
        asyncio.run(monitor_observed(supervisor, ()))
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
        cutoff = recovery.latest_flag_id(db.connection)
        await monitor_observed(supervisor, (), flags_before=cutoff)  # unpolled pass
        await supervisor._drain_recoveries()
        seen = db.connection.execute("SELECT COUNT(*) FROM first_mate_flags_seen").fetchone()[0]
        assert seen == 1  # the stale flag is consumed, not left pending
        # Days later the ticket returns to In Progress and is polled again.
        await monitor_observed(supervisor)
        await supervisor._drain_recoveries()

    try:
        asyncio.run(scenario())
        assert mate.prompts == [] and linear.comments == [] and resumed == []
        assert db.connection.execute("SELECT COUNT(*) FROM first_mate_recoveries").fetchone()[0] == 0
    finally:
        db.close()


def test_flag_cutoff_is_the_newest_event_of_any_kind(tmp_path):
    # The cutoff must not scan the ledger for the newest watchdog flag: a poll
    # reads it every cycle and the events table only grows. The newest id of
    # any event is an equally valid cutoff, read straight from the rowid.
    db = ledger(tmp_path, status="running", final={}, finished=None, activity=1)
    try:
        flag(db, "HAN-86")
        db.connection.execute(
            "INSERT INTO events(project, identifier, event_type, payload_json, at) "
            "VALUES ('SampleProject', 'HAN-86', 'ticket.observed', '{}', '2026-01-01T00:00:00+00:00')"
        )
        newest = db.connection.execute("SELECT MAX(id) FROM events").fetchone()[0]
        assert recovery.latest_flag_id(db.connection) == newest
        plan = " ".join(str(row) for row in db.connection.execute(
            "EXPLAIN QUERY PLAN SELECT MAX(id) FROM events").fetchall())
        assert "SCAN" not in plan
    finally:
        db.close()


# ---- #23 relies on a complete poll: absence must mean "left the polled states" ----

def _filler(number):
    return {"id": f"lin-f{number}", "identifier": f"HAN-{1000 + number}", "title": f"Filler {number}",
            "state": {"name": "In Progress", "type": "started"}}


HAN86_NODE = {"id": "lin-86", "identifier": "HAN-86", "title": "Sample issue",
              "state": {"name": "In Progress", "type": "started"}}


def _paged_linear(pages_for):
    """Serve Linear's cursor pagination; ``pages_for()`` gives the current pages."""

    def handler(request):
        pages = pages_for()
        after = json.loads(request.content)["variables"].get("after")
        index = 0 if after is None else int(after.removeprefix("cursor-")) + 1
        page = pages[min(index, len(pages) - 1)]
        if page == "endless":  # a misbehaving server that never ends pagination
            nodes, has_next = [], True
        else:
            nodes, has_next = page, index + 1 < len(pages)
        return httpx.Response(200, json={"data": {"issues": {
            "nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": f"cursor-{index}"}}}})

    return httpx.MockTransport(handler)


def _flagged_supervisor(tmp_path, pages_for):
    db = ledger(tmp_path, status="running", final={}, finished=None, activity=1)
    _shelve(db, state="In Progress", state_type="started")
    flag(db, "HAN-86", "repeated_answer")
    supervisor, mate, linear, resumed = _recording_supervisor(tmp_path, db)
    client = LinearClient(token="test-key", transport=_paged_linear(pages_for))
    supervisor.poller = LinearPoller(client, Poller.config, db=db)
    return db, client, supervisor, mate, linear, resumed


def _seen(db):
    recovery.initialize(db.connection)  # the monitor may never have run
    return db.connection.execute("SELECT COUNT(*) FROM first_mate_flags_seen").fetchone()[0]


def test_failed_poll_keeps_a_pending_flag_for_the_next_complete_poll(tmp_path):
    # Page 1 does not contain HAN-86 and the server never ends pagination, so
    # the client refuses the partial poll. A truncated snapshot must not reach
    # _monitor_blocked, or #23 would consume HAN-86's flag (never re-emitted).
    fillers = [_filler(n) for n in range(50)]
    current = {"pages": [fillers, "endless"]}
    db, client, supervisor, mate, linear, resumed = _flagged_supervisor(tmp_path, lambda: current["pages"])

    async def scenario():
        assert await supervisor.poll_once() is None
        assert supervisor.linear_healthy is False
        assert "pagination limit" in (supervisor.last_error or "")
        await supervisor._drain_recoveries()
        assert _seen(db) == 0  # still pending
        assert mate.prompts == [] and resumed == []
        # The next poll completes, with HAN-86 on its second page.
        current["pages"] = [fillers, [HAN86_NODE]]
        observed = await supervisor.poll_once()
        await supervisor._drain_recoveries()
        assert observed is not None and "HAN-86" in {t["identifier"] for t in observed.tickets}

    try:
        asyncio.run(scenario())
        assert len(mate.prompts) == 1 and "repeated_answer" in mate.prompts[0]
        triggers = db.connection.execute("SELECT ticket, trigger FROM first_mate_recoveries").fetchall()
        assert [tuple(row) for row in triggers] == [("HAN-86", "watchdog:repeated_answer")]
    finally:
        asyncio.run(client.close())
        db.close()


def test_ticket_on_a_later_page_is_observed_and_its_flag_drives_recovery(tmp_path):
    fillers = [_filler(n) for n in range(50)]
    db, client, supervisor, mate, linear, resumed = _flagged_supervisor(
        tmp_path, lambda: [fillers, [HAN86_NODE]])

    async def scenario():
        observed = await supervisor.poll_once()
        await supervisor._drain_recoveries()
        assert observed is not None and len(observed.tickets) == 51
        assert "HAN-86" in {t["identifier"] for t in observed.tickets}

    try:
        asyncio.run(scenario())
        # The flag reached First Mate instead of being consumed as "unpolled".
        assert len(mate.prompts) == 1 and "repeated_answer" in mate.prompts[0]
        triggers = db.connection.execute("SELECT ticket, trigger FROM first_mate_recoveries").fetchall()
        assert [tuple(row) for row in triggers] == [("HAN-86", "watchdog:repeated_answer")]
    finally:
        asyncio.run(client.close())
        db.close()


# ---- a flag the poll could not have seen is never consumed against it ----

def _switchable_linear(state, on_request=None):
    """HAN-86 is returned only while ``state['polled']`` is true."""

    def handler(request):
        nodes = [HAN86_NODE] if state["polled"] else []
        response = httpx.Response(200, json={"data": {"issues": {
            "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}})
        if on_request is not None:
            on_request()
        return response

    return httpx.MockTransport(handler)


def _poll_driven_supervisor(tmp_path, *, status, transport):
    db = ledger(tmp_path, status=status, final={}, finished=None, activity=1)
    _shelve(db, state="In Progress", state_type="started")  # last observed state, now stale
    supervisor, mate, linear, resumed = _recording_supervisor(tmp_path, db)
    client = LinearClient(token="test-key", transport=transport)
    supervisor.poller = LinearPoller(client, Poller.config, db=db)
    return db, client, supervisor, mate


def _receipts(db):
    rows = db.connection.execute("SELECT ticket, trigger FROM first_mate_recoveries").fetchall()
    return [tuple(row) for row in rows]


def test_flag_written_after_the_linear_fetch_is_not_consumed_by_that_poll(tmp_path):
    # Poll A fetches while HAN-86 is parked in Icebox (absent). While A is
    # still in flight the operator moves HAN-86 back to In Progress and the
    # watchdog flags it. A's monitor must not consume a flag A could not see:
    # _flag_once never re-emits it, so poll B would otherwise never triage it.
    state = {"polled": False, "db": None}

    def watchdog_flags_during_poll_a():
        if not state["polled"] and state["db"] is not None:
            flag(state["db"], "HAN-86")

    db, client, supervisor, mate = _poll_driven_supervisor(
        tmp_path, status="waiting", transport=_switchable_linear(state, watchdog_flags_during_poll_a))
    state["db"] = db

    async def scenario():
        first = await supervisor.poll_once()  # poll A
        await supervisor._drain_recoveries()
        assert first is not None and first.tickets == ()
        assert _seen(db) == 0  # the new flag stays pending
        state["polled"] = True
        await supervisor.poll_once()  # poll B observes HAN-86 In Progress
        await supervisor._drain_recoveries()

    try:
        asyncio.run(scenario())
        assert len(mate.prompts) == 1 and "no_progress" in mate.prompts[0]
        assert _receipts(db) == [("HAN-86", "watchdog:no_progress")]
    finally:
        asyncio.run(client.close())
        db.close()


def test_every_stale_flag_of_an_unpolled_ticket_is_consumed(tmp_path):
    # Two flags were pending before the fetch; the ticket is unpolled. Both
    # are stale, so neither may trigger triage when the ticket returns.
    state = {"polled": False}
    db, client, supervisor, mate = _poll_driven_supervisor(
        tmp_path, status="running", transport=_switchable_linear(state))
    flag(db, "HAN-86", "repeated_answer")
    flag(db, "HAN-86", "no_progress")

    async def scenario():
        await supervisor.poll_once()
        await supervisor._drain_recoveries()
        assert _seen(db) == 2
        state["polled"] = True
        await supervisor.poll_once()
        await supervisor._drain_recoveries()

    try:
        asyncio.run(scenario())
        assert mate.prompts == [] and _receipts(db) == []
    finally:
        asyncio.run(client.close())
        db.close()


def test_superseded_poll_does_not_run_the_monitor(tmp_path):
    # Poll A is overtaken by poll B during A's post-fetch work. A's snapshot
    # is older than B's, so A must not run the First Mate monitor at all.
    state = {"polled": False}
    db, client, supervisor, mate = _poll_driven_supervisor(
        tmp_path, status="running", transport=_switchable_linear(state))
    monitored = []
    original_monitor = supervisor._monitor_blocked
    original_retry = supervisor._retry_stopped_builders

    async def monitor(observed, *args, **kwargs):
        monitored.append(sorted(supervisor._ticket_key(ticket) for ticket in observed.tickets))
        return await original_monitor(observed, *args, **kwargs)

    async def retry_then_overtake():
        await original_retry()
        if not monitored and not state["polled"]:
            state["polled"] = True
            await supervisor.poll_once()  # poll B publishes while A is in flight

    supervisor._monitor_blocked = monitor
    supervisor._retry_stopped_builders = retry_then_overtake

    async def scenario():
        await supervisor.poll_once()  # poll A
        await supervisor._drain_recoveries()

    try:
        asyncio.run(scenario())
        assert monitored == [["HAN-86"]]  # only B's monitor ran
    finally:
        asyncio.run(client.close())
        db.close()


def test_one_triage_consumes_every_flag_it_was_shown(tmp_path):
    # Both flags are folded into one case and shown to First Mate together,
    # so recording that decision consumes both, not just the last one.
    state = {"polled": True}
    db, client, supervisor, mate = _poll_driven_supervisor(
        tmp_path, status="waiting", transport=_switchable_linear(state))
    flag(db, "HAN-86", "repeated_answer")
    flag(db, "HAN-86", "no_progress")

    async def scenario():
        await supervisor.poll_once()
        await supervisor._drain_recoveries()

    try:
        asyncio.run(scenario())
        assert len(mate.prompts) == 1
        assert "repeated_answer" in mate.prompts[0] and "no_progress" in mate.prompts[0]
        assert _seen(db) == 2
    finally:
        asyncio.run(client.close())
        db.close()
