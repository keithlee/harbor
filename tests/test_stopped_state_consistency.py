"""Every gate that resumes or retries a builder agrees on which tickets are stopped.

First Mate recovery, the answer queue, the unblock ladder and the supervisor's
stopped-builder retry each used to carry their own copy of the stopped-state
set, and the copies disagreed (the answer queue ignored Done/Completed/
Canceled names, the ladder ignored "Completed"). A ticket in such a state was
then treated as stopped by one gate and resumed by another.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from harbor import first_mate_recovery as recovery
from harbor.db import HarborDB, apply_phase9_migration
from harbor.recovery import BlockerPolicy
from harbor.runner.base import apply_phase4_migration
from harbor.supervisor import Lifecycle, Supervisor
from harbor.unblock import UnblockLadder
from harbor.wake import apply_phase3_migration

WORKFLOW_STATES = {"paused": ["On Hold"], "terminal": ["Shipped"]}

# (state name, state type, stopped?). Names are deliberately paired with a
# ``started`` type so only the name decides; the type-only rows use a custom name.
CASES = [
    ("Completed", "started", True),
    ("Done", "started", True),
    ("Canceled", "started", True),
    ("Cancelled", "started", True),
    ("Backlog", "started", True),
    (" Paused ", "started", True),
    ("On Hold", "started", True),     # workflow [states] paused
    ("shipped", "started", True),     # workflow [states] terminal
    ("Icebox", "backlog", True),
    ("Merged", "completed", True),
    ("Won't Do", "canceled", True),
    ("In Progress", "started", False),
]
IDS = [f"{name.strip()}/{kind}" for name, kind, _ in CASES]


def _first_mate_stops(tmp_path, state, state_type):
    db = HarborDB(tmp_path / "fm.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    db.replace_tickets("Demo", [{"identifier": "HAN-1", "state": state, "state_type": state_type, "title": "t",
                                 "owned": True, "hold": False, "id": "lin-1"}], emit_events=False)
    final = {"status": "blocked", "summary": "stopped", "blocker": {"kind": "env_blocker", "question": "exited"}}
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,session_id,worktree,final_json,started_at,finished_at) "
        "VALUES ('run-1','HAN-1','blocked','s',?,?,'2000-01-01T00:00:00+00:00','2000-01-01T00:00:00+00:00')",
        (str(tmp_path), json.dumps(final)))
    db.connection.commit()
    cases = recovery.find_cases(db.connection, "Demo", {"recheck_minutes": 10, "stall_minutes": 30},
                                stopped_states=(*WORKFLOW_STATES["paused"], *WORKFLOW_STATES["terminal"]))
    db.close()
    return cases == []


def _answer_queue_stops(tmp_path, state, state_type):
    from harbor.answer_queue import drain, enqueue

    async def scenario():
        db = HarborDB(tmp_path / "queue.db")
        enqueue(db.connection, "nine", "A", "A-1", "run", "Answer")
        calls = []

        async def resume(run, instruction):
            calls.append(run)
            return True
        run = {"id": "run", "terminal_reason": None}
        supervisor = SimpleNamespace(
            db=db, project="A", read_only=False, _dispatch_suspended=False,
            _fleet_tickets=[{"identifier": "A-1", "hold": False, "state": state, "state_type": state_type}],
            _latest_run=lambda _: run, _in_scope=lambda _: True, _dispatch_tasks={}, _dispatch_wakes={},
            resume_session=resume, _dispatch_finished=lambda *args: None,
            dispatcher=SimpleNamespace(workflow=SimpleNamespace(config={"states": WORKFLOW_STATES})))
        drain(supervisor)
        await asyncio.gather(*supervisor._dispatch_tasks.values())
        db.close()
        return calls == []
    return asyncio.run(scenario())


def _unblock_ladder_stops(tmp_path, state, state_type):
    class Linear:
        async def fetch_blocker_context(self, identifier):
            return {"identifier": identifier, "description": "Use blue.", "state": {"name": state, "type": state_type}}

    reached = []

    async def record(*args, **kwargs):
        reached.append(args)
        raise RuntimeError("reached")
    ladder = UnblockLadder(linear=Linear(), first_mate=record, resumer=record, remediation_executor=record,
                           repo_path=tmp_path)
    ladder.workflow = SimpleNamespace(config={"states": WORKFLOW_STATES})
    try:
        asyncio.run(ladder.handle({"identifier": "MOCK-1"}, {"id": "retained"},
                                  {"blocker": {"kind": "env_blocker", "question": "Cannot find module"}}))
    except ValueError as exc:
        return "no longer active" in str(exc)
    except RuntimeError:
        return False
    return not reached


def _stopped_builder_retry_stops(tmp_path, state, state_type):
    db = HarborDB(tmp_path / "retry.sqlite")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    ticket = {"identifier": "MOCK-6", "title": "t", "state": state, "state_type": state_type, "owned": True,
              "hold": False}
    db.replace_tickets("mock", [ticket], emit_events=False)
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,terminal_reason,session_id,worktree,deadline_seconds) "
        "VALUES ('run-6','MOCK-6','failed','unexpected_exit','session-6',?,60)", (str(tmp_path),))
    db.connection.commit()
    retried = []

    class Dispatcher:
        workflow = SimpleNamespace(config={"states": WORKFLOW_STATES})

        async def retry(self, *args):
            retried.append(args)
            return {"status": "waiting", "summary": "retried"}
    poller = SimpleNamespace(project_name="mock", config=SimpleNamespace(blocker_policy=BlockerPolicy((0,))))
    supervisor = Supervisor(poller, db=db, dispatcher=Dispatcher())
    supervisor.lifecycle = Lifecycle.RUNNING
    supervisor.linear_healthy = True
    supervisor._fleet_tickets = (ticket,)

    async def scenario():
        await supervisor._retry_stopped_builders()
        scheduled = bool(supervisor._dispatch_tasks)
        await asyncio.gather(*supervisor._dispatch_tasks.values(), return_exceptions=True)
        return scheduled
    scheduled = asyncio.run(scenario())
    db.close()
    return not scheduled and not retried


GATES = {
    "first_mate": _first_mate_stops,
    "answer_queue": _answer_queue_stops,
    "unblock_ladder": _unblock_ladder_stops,
    "stopped_builder_retry": _stopped_builder_retry_stops,
}


@pytest.mark.parametrize(("state", "state_type", "stopped"), CASES, ids=IDS)
def test_every_resume_gate_agrees_on_stopped_states(tmp_path, state, state_type, stopped):
    verdicts = {}
    for name, gate in GATES.items():
        root = tmp_path / name
        root.mkdir()
        verdicts[name] = gate(root, state, state_type)
    assert verdicts == {name: stopped for name in GATES}, (state, state_type)


@pytest.mark.parametrize(("state", "state_type", "stopped"), CASES, ids=IDS)
def test_shared_helper_matches_the_gates(state, state_type, stopped):
    from harbor.ticket_states import is_stopped
    assert is_stopped(state, state_type, WORKFLOW_STATES) is stopped


def test_human_review_is_not_stopped(tmp_path):
    from harbor.ticket_states import is_stopped
    # Harbor parks escalations in Human Review and resumes from it, so none of
    # the gates that deliver answers or recover blockers treat it as stopped.
    # (Crash retry additionally waits there for PR feedback: a separate rule.)
    assert not is_stopped("Human Review", "started", WORKFLOW_STATES)
    for name in ("first_mate", "answer_queue", "unblock_ladder"):
        root = tmp_path / name
        root.mkdir()
        assert GATES[name](root, "Human Review", "started") is False, name


def test_helper_without_workflow_states_still_stops_builtin_names():
    from harbor.ticket_states import is_stopped
    assert is_stopped("Completed", "started")
    assert is_stopped(None, "completed")
    assert not is_stopped("On Hold", "started")
    assert not is_stopped("", None)
