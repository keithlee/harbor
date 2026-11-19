"""First Mate monitor-and-recover, including a replay of the HAN-86 deadlock."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from harbor import first_mate_recovery as recovery
from harbor.config.projects import ProjectConfig
from harbor.config.workflow import load_workflow
from harbor.db import HarborDB, apply_phase9_migration
from harbor.poll.linear_poller import PollResult
from harbor.runner.base import apply_phase4_migration
from harbor.supervisor import Supervisor, WorktreeDispatcher
from harbor.wake import apply_phase3_migration
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


def test_blocked_run_becomes_a_case_after_recheck_and_backs_off(tmp_path):
    db = ledger(tmp_path, finished=15)
    cases = recovery.find_cases(db.connection, "Demo", SETTINGS)
    assert [(c.ticket, c.trigger) for c in cases] == [("HAN-86", "blocked")]
    recovery.record(db.connection, cases[0], {"action": "resume", "instruction": "x", "reason": "r"})
    assert recovery.find_cases(db.connection, "Demo", SETTINGS) == []  # backoff
    later = time.time() + 31 * 60
    assert len(recovery.find_cases(db.connection, "Demo", SETTINGS, now=later)) == 1
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


def supervisor_for(tmp_path, db, answer):
    workflow = load_workflow(config(tmp_path))
    dispatcher = WorktreeDispatcher(SimpleNamespace(), lambda *a: None, db=db, project="Demo")
    dispatcher.workflow = workflow
    mate, linear = Mate(answer), Linear()
    ladder = SimpleNamespace(first_mate=mate, linear=linear, workflow=workflow)
    supervisor = Supervisor(Poller(), db=db, dispatcher=dispatcher, unblock_ladder=ladder)
    return supervisor, mate, linear


def test_han86_replay_first_mate_resumes_with_close_and_respawn(tmp_path):
    db = ledger(tmp_path)
    answer = {"action": "resume", "reason": "stale interrupted planner holds the name",
              "instruction": "List agents, close the interrupted /root/planner, spawn planner-2 and continue."}
    supervisor, mate, linear = supervisor_for(tmp_path, db, answer)
    resumed = []

    async def resume_session(run_id, prompt):
        resumed.append((run_id, prompt))
        return True

    supervisor.resume_session = resume_session
    asyncio.run(supervisor._monitor_blocked())
    assert resumed == [("run-HAN-86", answer["instruction"])]
    assert "/root/planner already exists" in mate.prompts[0]
    assert linear.comments and linear.comments[0][1].startswith("[First Mate recovery] resume (blocked)")
    rows = db.connection.execute("SELECT action FROM first_mate_recoveries").fetchall()
    assert [r[0] for r in rows] == ["resume"]
    asyncio.run(supervisor._monitor_blocked())  # backoff: no second attempt this poll
    assert len(resumed) == 1
    db.close()


def test_han86_replay_fresh_session_keeps_worktree_and_drops_old_session(tmp_path):
    db = ledger(tmp_path)
    answer = {"action": "fresh_session", "reason": "agent state is wedged", "instruction": "Respawn all roles."}
    supervisor, _, _ = supervisor_for(tmp_path, db, answer)
    started = []

    class Runner:
        async def start(self, instruction, *, run_id=None):
            started.append((instruction, run_id))
            return {"status": "completed", "outcome": "no_pr", "summary": "continuing", "run_id": run_id}

    supervisor.dispatcher.runner_factory = lambda worktree, wake: Runner()
    asyncio.run(supervisor._monitor_blocked())
    assert len(started) == 1 and started[0][1] == "run-HAN-86"
    assert "fresh builder session" in started[0][0] and "Respawn all roles." in started[0][0]
    row = db.connection.execute("SELECT worktree, session_id FROM runs WHERE id='run-HAN-86'").fetchone()
    assert Path(row[0]).is_dir()
    old = db.connection.execute("SELECT old_session_id FROM first_mate_recoveries").fetchone()[0]
    assert old == "native-1"
    db.close()


def test_product_question_is_never_recovered(tmp_path):
    db = ledger(tmp_path, final={**HAN86, "blocker": {"kind": "product_decision", "question": "Which spacing?"}})
    supervisor, mate, _ = supervisor_for(tmp_path, db, {"action": "resume", "instruction": "pick 8px", "reason": "guess"})
    calls = []

    async def resume_session(*args):
        calls.append(args)
        return True

    supervisor.resume_session = resume_session
    asyncio.run(supervisor._monitor_blocked())
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
