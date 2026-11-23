import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from harbor import first_mate_recovery as recovery
from harbor.answer_queue import enqueue
from harbor.db import HarborDB, apply_phase9_migration
from harbor.quota_fallback import FallbackPolicy, Profile
from harbor.recovery import defer_quota, record_provider_quota
from harbor.runner.base import apply_phase4_migration
from harbor.runner.quota import bind_runner_identity, runner_identity
from harbor.supervisor import Lifecycle, Supervisor, WorktreeDispatcher
from harbor.wake import apply_phase3_migration

CODEX = Profile("codex", "gpt-6.1-terra", "medium")
CLAUDE = Profile("claude", "claude-sonnet-5-5", "medium")
ROLES = {
    "codex": {"planner": {"model": "gpt-6.2-sol", "reasoning_effort": "medium"}},
    "claude": {"planner": {"model": "claude-opus-5-5", "reasoning_effort": "medium"}},
}


def policy(*, check=lambda profile: None, max_switches=2, builder=(CODEX, CLAUDE)):
    return FallbackPolicy(max_switches, builder, None, ROLES, check)


class Dispatcher:
    workflow = None

    def __init__(self):
        self.fresh, self.retries = [], []

    async def fresh_session(self, identifier, instruction, *, switch=None):
        self.fresh.append((identifier, instruction, switch))

    async def retry(self, identifier, instruction):
        self.retries.append((identifier, instruction))


class Notifier:
    def __init__(self):
        self.messages = []

    def notify(self, text):
        self.messages.append(text)


def stopped(tmp_path, *, provider="codex", quota_fallback=None, due=False):
    """A failed-on-quota run on *provider*, plus a supervisor watching it.

    ``due=False`` means the provider reset is still in the future (the run is waiting);
    ``due=True`` means the reset already passed, so the normal retry is allowed to resume it.
    """
    db = HarborDB(tmp_path / "harbor.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    ticket = {"identifier": "MOCK-87", "title": "Copy", "state": "In Progress", "owned": True, "hold": False,
              "pr_urls": ["https://github.com/o/r/pull/295"]}
    db.replace_tickets("mock", [ticket], emit_events=False)
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,terminal_reason,session_id,worktree,deadline_seconds,final_json) "
        "VALUES ('run-87','MOCK-87','failed','usage_limit_exceeded','native-1',?,60,?)",
        (str(tmp_path), json.dumps({"summary": "Provider usage limit reached"})),
    )
    bind_runner_identity(db.connection, "run-87", provider, "primary-model", "medium")
    defer_quota(db.connection, "run-87", provider, 1 if due else time.time() + 10_000, 0 if due else time.time())
    dispatcher, notifier = Dispatcher(), Notifier()
    supervisor = Supervisor(SimpleNamespace(project_name="mock"), db=db, dispatcher=dispatcher,
                            notifier=notifier, quota_fallback=quota_fallback)
    supervisor.lifecycle = Lifecycle.RUNNING
    supervisor.linear_healthy = True
    supervisor._fleet_tickets = (ticket,)
    return db, supervisor, dispatcher, notifier


def run_pass(supervisor, passes=1):
    async def scenario():
        for _ in range(passes):
            await supervisor._retry_stopped_builders()
        if supervisor._dispatch_tasks:
            await asyncio.gather(*supervisor._dispatch_tasks.values())

    asyncio.run(scenario())


def receipts(db):
    recovery.initialize(db.connection)  # the table only exists once something was recorded
    rows =db.connection.execute("SELECT action, reason FROM first_mate_recoveries ORDER BY id").fetchall()
    return [tuple(row) for row in rows]


def test_quota_run_switches_to_fallback_instead_of_waiting(tmp_path):
    db, supervisor, dispatcher, notifier = stopped(tmp_path, quota_fallback=policy())
    run_pass(supervisor)
    assert dispatcher.retries == []
    [(identifier, instruction, switch)] = dispatcher.fresh
    assert identifier == "MOCK-87" and switch == CLAUDE
    assert "claude" in instruction and "claude-opus-5-5" in instruction  # role override for the new provider
    assert receipts(db) == [("quota_fallback", "quota_fallback: codex -> claude")]
    assert len(notifier.messages) == 1 and "claude" in notifier.messages[0]
    db.close()


def test_no_switch_when_target_exhausted_waits_normally(tmp_path):
    db, supervisor, dispatcher, _ = stopped(tmp_path, quota_fallback=policy())
    record_provider_quota(db.connection, "claude", time.time() + 10_000, time.time())
    run_pass(supervisor)
    assert dispatcher.fresh == [] and dispatcher.retries == []  # still waiting on the codex reset
    assert receipts(db) == []
    db.close()


def test_no_switch_and_single_notice_when_target_not_ready(tmp_path):
    db, supervisor, dispatcher, notifier = stopped(tmp_path, quota_fallback=policy(check=lambda p: "not logged in"))
    run_pass(supervisor, passes=2)
    assert dispatcher.fresh == [] and dispatcher.retries == []
    assert [m for m in notifier.messages if "not logged in" in m] != []
    assert len([m for m in notifier.messages if "not logged in" in m]) == 1
    db.close()


def test_run_on_fallback_moves_back_to_primary(tmp_path):
    db, supervisor, dispatcher, _ = stopped(tmp_path, provider="claude", quota_fallback=policy())
    run_pass(supervisor)
    [(_, instruction, switch)] = dispatcher.fresh
    assert switch == CODEX and "gpt-6.2-sol" in instruction
    assert receipts(db) == [("quota_fallback", "quota_fallback: claude -> codex")]
    db.close()


def test_max_switches_counts_ledger_across_restart(tmp_path):
    db, supervisor, dispatcher, _ = stopped(tmp_path, quota_fallback=policy(max_switches=2))
    for _ in range(2):
        recovery.record(db.connection, recovery.Case("mock", "MOCK-87", "run-87", "failed", "failed"),
                        {"action": "quota_fallback", "instruction": "", "reason": "quota_fallback: codex -> claude"})
    run_pass(supervisor)
    assert dispatcher.fresh == [] and dispatcher.retries == []
    db.close()


def test_zero_max_switches_disables(tmp_path):
    db, supervisor, dispatcher, _ = stopped(tmp_path, quota_fallback=policy(max_switches=0))
    run_pass(supervisor)
    assert dispatcher.fresh == [] and dispatcher.retries == []
    db.close()


def test_first_mate_budget_not_consumed_by_quota_fallback():
    case = recovery.Case("p", "T", "r", "failed", "failed",
                         history=[{"action": "quota_fallback"}, {"action": "wait"}])
    assert case.attempts == 0 and case.fresh_sessions == 0
    case.history.append({"action": "resume"})
    assert case.attempts == 1


def test_handoff_includes_queued_answer_and_open_question(tmp_path):
    db, supervisor, dispatcher, _ = stopped(tmp_path, quota_fallback=policy())
    enqueue(db.connection, "9", "mock", "MOCK-87", "run-87", "Operator answer: use Maestro at ~/.maestro/bin/maestro")
    db.record_escalation("MOCK-87", "run-87", "missing_proof_capability", "Can an iOS simulator be controlled?", 2)
    run_pass(supervisor)
    [(_, instruction, _)] = dispatcher.fresh
    assert "use Maestro at ~/.maestro/bin/maestro" in instruction
    assert "Can an iOS simulator be controlled?" in instruction
    assert "github.com/o/r/pull/295" in instruction
    db.close()


def test_passed_reset_resumes_in_place_instead_of_switching(tmp_path):
    # Harbor was down across the reset (the HAN-87 situation): the provider is back,
    # so the retained session, with its memory, beats a fresh session elsewhere.
    db, supervisor, dispatcher, notifier = stopped(tmp_path, quota_fallback=policy(), due=True)
    run_pass(supervisor)
    assert dispatcher.fresh == [] and len(dispatcher.retries) == 1
    assert receipts(db) == [] and notifier.messages == []
    db.close()


def test_no_policy_keeps_current_behaviour(tmp_path):
    db, supervisor, dispatcher, _ = stopped(tmp_path, quota_fallback=None, due=True)
    run_pass(supervisor)
    assert dispatcher.fresh == [] and len(dispatcher.retries) == 1
    db.close()


def test_run_without_identity_is_left_to_the_normal_wait(tmp_path):
    db, supervisor, dispatcher, _ = stopped(tmp_path, quota_fallback=policy())
    db.connection.execute("DELETE FROM runner_identity")
    run_pass(supervisor)
    assert dispatcher.fresh == [] and dispatcher.retries == []
    db.close()


class Runner:
    def __init__(self, started):
        self.started = started

    async def start(self, instruction, *, run_id=None):
        self.started.append((instruction, run_id))
        return {"status": "waiting", "outcome": "no_pr", "summary": "continuing", "run_id": run_id}


def test_fresh_session_switch_rebinds_identity_and_drops_quota_row(tmp_path):
    db, _, _, _ = stopped(tmp_path)
    seen, started = [], []
    dispatcher = WorktreeDispatcher(SimpleNamespace(), lambda *a: None, db=db, project="mock")
    dispatcher.runner_factory = lambda worktree, wake: (
        seen.append(tuple(runner_identity(db.connection, wake.run_id))), Runner(started))[1]
    asyncio.run(dispatcher.fresh_session("MOCK-87", "hand-off text", switch=CLAUDE))
    assert seen == [("claude", "claude-sonnet-5-5", "medium")]
    assert started and "hand-off text" in started[0][0] and started[0][1] == "run-87"
    assert db.connection.execute("SELECT count(*) FROM quota_recovery").fetchone()[0] == 0
    assert Path(db.connection.execute("SELECT worktree FROM runs WHERE id='run-87'").fetchone()[0]).is_dir()
    db.close()


def test_fresh_session_without_switch_keeps_identity(tmp_path):
    db, _, _, _ = stopped(tmp_path)
    seen, started = [], []
    dispatcher = WorktreeDispatcher(SimpleNamespace(), lambda *a: None, db=db, project="mock")
    dispatcher.runner_factory = lambda worktree, wake: (
        seen.append(tuple(runner_identity(db.connection, wake.run_id))), Runner(started))[1]
    asyncio.run(dispatcher.fresh_session("MOCK-87", "hand-off text"))
    assert seen == [("codex", "primary-model", "medium")]
    db.close()
