"""A re-engage wake whose run has no recorded runner identity fails closed.

The production factory used to assume such a run belonged to Codex and resume
it with a hard-coded Codex model, ignoring the project's configured provider
and models. A Claude-only project then launched Codex. Now a resume refuses.
Only a real re-engagement (a PR event, an operator answer, a retry or a
recovery) is delivered into a fresh session, which carries the blocker
question and every undelivered message verbatim, starts like a new ticket and
records its identity. An idle run is left alone, and a human blocker waits for
the human.
"""
import asyncio
import json
import tomllib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harbor import answer_queue
from harbor import first_mate_recovery as recovery
from harbor.cli import build_production_dispatcher
from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB
from harbor.poll.linear_poller import PollResult
from harbor.runner.base import apply_phase4_migration
from harbor.runner.claude import ClaudeRunner
from harbor.runner.codex import CodexRunner
from harbor.runner.quota import bind_runner_identity, runner_identity
from harbor.supervisor import _MAX_RESUME_REASON_CHARS, UNRECORDED_IDENTITY, Supervisor
from harbor.wake import Wake, apply_phase3_migration
from workflow_support import dumps, materialize

CONFIGURED = {
    "claude": {"model": "claude-sonnet-5-5", "reasoning_effort": "low"},
    "codex": {"model": "gpt-sample-configured", "reasoning_effort": "low"},
}
RUNNERS = {"claude": ClaudeRunner, "codex": CodexRunner}


def project(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("HARBOR_CLAUDE_EXECUTABLE", "/usr/bin/true")
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/usr/bin/true")
    monkeypatch.delenv("HARBOR_PROJECTS_FILE", raising=False)  # hermetic: never the operator's registry
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "harbor.db"))
    registry = tmp_path / "projects.md"
    registry.write_text("# Harbor projects\n")
    workflow = materialize(tmp_path, models={"controller": CONFIGURED[provider]})
    return SimpleNamespace(name="SampleProject", builder_provider=provider, builder_model=CONFIGURED[provider]["model"],
                           builder_effort="low", workflow_path=str(workflow), install_command="none",
                           registry_path=str(registry))


def retained_run(tmp_path, status="waiting", final=None, finished_minutes_ago=None):
    """A retained run with a worktree and native session, but no runner_identity row."""
    db = HarborDB(tmp_path / "harbor.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    db.replace_tickets("SampleProject", [{"identifier": "SAMPLE-1", "title": "t", "state": "In Progress",
                                          "owned": True, "hold": False}], emit_events=False)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    finished = None
    if finished_minutes_ago is not None:
        finished = (datetime.now(timezone.utc) - timedelta(minutes=finished_minutes_ago)).isoformat(timespec="seconds")
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,session_id,worktree,deadline_seconds,final_json,finished_at) "
        "VALUES ('run-1','SAMPLE-1',?,'native-1',?,600,?,?)",
        (status, str(worktree), json.dumps(final) if final is not None else None, finished),
    )
    db.connection.commit()
    assert runner_identity(db.connection, "run-1") is None
    return db


def workflow_binds(db, run_id):
    try:
        return db.connection.execute("SELECT COUNT(*) FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()[0]
    except Exception:  # table not created: nothing was bound
        return 0


def test_refusal_names_a_remedy_within_the_digest_limit():
    assert len(UNRECORDED_IDENTITY) <= _MAX_RESUME_REASON_CHARS
    assert "fresh session" in UNRECORDED_IDENTITY


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("run_id", ["run-without-identity", None])
def test_reengage_without_identity_refuses_instead_of_guessing(tmp_path, monkeypatch, provider, run_id):
    config = project(tmp_path, monkeypatch, provider)
    db = HarborDB(tmp_path / "harbor.db")
    _, _, factory = build_production_dispatcher(config, db, worktree_manager=object())
    with pytest.raises(ValueError, match="no recorded runner identity"):
        factory(tmp_path, Wake({"identifier": "SAMPLE-1"}, "reengage", resume=run_id))
    # A refused attempt writes no workflow binding, so a polling loop does not churn receipts.
    assert workflow_binds(db, run_id) == 0
    db.close()


def test_build_uses_configuration_and_recorded_run_uses_its_record(tmp_path, monkeypatch):
    config = project(tmp_path, monkeypatch, "claude")
    db = HarborDB(tmp_path / "harbor.db")
    _, _, factory = build_production_dispatcher(config, db, worktree_manager=object())
    built = factory(tmp_path, Wake({"identifier": "SAMPLE-1"}, "build", resume="new-run"))
    assert type(built) is ClaudeRunner and built.model == "claude-sonnet-5-5"
    # A recorded run on the other provider resumes there, on its RECORDED model.
    bind_runner_identity(db.connection, "recorded-run", "codex", "gpt-sample-recorded", "high")
    resumed = factory(tmp_path, Wake({"identifier": "SAMPLE-2"}, "reengage", resume="recorded-run"))
    assert type(resumed) is CodexRunner
    assert (resumed.model, resumed.reasoning_effort) == ("gpt-sample-recorded", "high")
    db.close()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_hydrating_a_run_without_identity_reports_the_reason(tmp_path, monkeypatch, provider):
    config = project(tmp_path, monkeypatch, provider)
    db = retained_run(tmp_path)
    dispatcher, _, _ = build_production_dispatcher(config, db, worktree_manager=object())

    assert asyncio.run(dispatcher.hydrate("SAMPLE-1")) is None
    assert "SAMPLE-1" not in dispatcher.runners
    assert dispatcher.resume_unavailable_reason("SAMPLE-1") == UNRECORDED_IDENTITY
    assert workflow_binds(db, "run-1") == 0
    db.close()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_fresh_session_binds_identity_through_the_real_runner(tmp_path, monkeypatch, provider):
    config = project(tmp_path, monkeypatch, provider)
    db = retained_run(tmp_path)
    dispatcher, _, _ = build_production_dispatcher(config, db, worktree_manager=object())
    # Nothing stubs the runner: /usr/bin/true exits at once, and the real
    # _execute records the identity before the process spawns.
    asyncio.run(dispatcher.fresh_session("SAMPLE-1", "hand-off"))
    runner = dispatcher.runners["SAMPLE-1"]
    assert type(runner) is RUNNERS[provider] and runner.model == CONFIGURED[provider]["model"]
    assert tuple(runner_identity(db.connection, "run-1")) == (provider, CONFIGURED[provider]["model"], "low")
    db.close()


def test_fresh_session_without_identity_applies_the_quota_starting_profile(tmp_path, monkeypatch):
    config = project(tmp_path, monkeypatch, "claude")
    db = retained_run(tmp_path)
    fallback = SimpleNamespace(provider="codex", model="gpt-sample-fallback", effort="high")
    policy = SimpleNamespace(starting_builder_profile=lambda connection, now: fallback)
    dispatcher, _, _ = build_production_dispatcher(config, db, worktree_manager=object(), quota_policy=policy)
    # The configured provider is out of usage, so the fresh session starts on the fallback.
    asyncio.run(dispatcher.fresh_session("SAMPLE-1", "hand-off"))
    assert type(dispatcher.runners["SAMPLE-1"]) is CodexRunner
    assert tuple(runner_identity(db.connection, "run-1")) == ("codex", "gpt-sample-fallback", "high")
    db.close()


def test_fresh_session_names_the_factory_reason(tmp_path, monkeypatch):
    config = project(tmp_path, monkeypatch, "claude")
    db = retained_run(tmp_path)

    def refusing_factory(worktree, wake):
        raise ValueError("sample factory refusal")

    dispatcher, _, _ = build_production_dispatcher(config, db, worktree_manager=object(),
                                                   runner_factory=refusing_factory)
    with pytest.raises(RuntimeError, match="cannot start a fresh session for SAMPLE-1: sample factory refusal"):
        asyncio.run(dispatcher.fresh_session("SAMPLE-1", "hand-off"))
    db.close()


# --- Only a real re-engagement starts a fresh session -------------------------

QUESTION = "Which onboarding copy should ship, A or B?"
HUMAN_BLOCKER = {"summary": "needs a product decision", "blocker": {"kind": "product_decision", "question": QUESTION}}


class Poller:
    project_name = "SampleProject"
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", heartbeat_seconds=1)

    async def poll(self):
        return PollResult((), "snapshot", False, ())


class Mate:
    """First Mate's model: always proposes a resume, and records each prompt."""

    def __init__(self):
        self.prompts = []

    async def triage(self, prompt):
        self.prompts.append(prompt)
        return json.dumps({"action": "resume", "instruction": "continue", "reason": "model guess"})


def harbor_for(tmp_path, monkeypatch, provider="codex", **run):
    """The production dispatcher and a supervisor over a throwaway ledger with one unrecorded run.

    ``_runner_call`` is wrapped only to capture each instruction; the real
    runner still runs (/usr/bin/true) and records its own identity.
    """
    config = project(tmp_path, monkeypatch, provider)
    db = retained_run(tmp_path, **run)
    dispatcher, _, _ = build_production_dispatcher(config, db, worktree_manager=object())
    launched = []
    real_call = dispatcher._runner_call

    async def spy(runner, wake, **kwargs):
        launched.append(wake.instruction)
        return await real_call(runner, wake, **kwargs)

    dispatcher._runner_call = spy
    mate = Mate()
    supervisor = Supervisor(Poller(), db=db, dispatcher=dispatcher,
                            unblock_ladder=SimpleNamespace(first_mate=mate, linear=None, workflow=dispatcher.workflow))
    return SimpleNamespace(db=db, dispatcher=dispatcher, supervisor=supervisor, mate=mate, launched=launched)


def monitor_pass(harbor):
    """One real First Mate monitor pass, then wait for any recovery it scheduled."""
    async def run():
        observation = PollResult(tuple(harbor.db.tickets("SampleProject")), "snapshot", True, ())
        harbor.supervisor.last_result, harbor.supervisor.linear_healthy = observation, True
        await harbor.supervisor._monitor_blocked(observation)
        await harbor.supervisor._drain_recoveries()
    asyncio.run(run())


def fresh_sessions(db):
    recovery.initialize(db.connection)
    return [tuple(r) for r in db.connection.execute(
        "SELECT trigger, action FROM first_mate_recoveries WHERE action='fresh_session'")]


@pytest.mark.parametrize("status", ["parked", "waiting"])
def test_an_idle_run_without_identity_is_left_alone(tmp_path, monkeypatch, status):
    # For example a merge-ready hand-off parked in Human Review, with nothing pending.
    harbor = harbor_for(tmp_path, monkeypatch, status=status, final={"outcome": "parked", "summary": "merge-ready"})
    monitor_pass(harbor)
    asyncio.run(harbor.dispatcher.hydrate("SAMPLE-1"))  # a restart rebuilds handles, nothing more
    assert harbor.launched == [] and harbor.mate.prompts == []
    assert fresh_sessions(harbor.db) == []
    assert runner_identity(harbor.db.connection, "run-1") is None
    assert harbor.dispatcher.resume_unavailable_reason("SAMPLE-1") == UNRECORDED_IDENTITY
    harbor.db.close()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_pr_event_on_an_unrecorded_waiting_run_starts_one_fresh_session_carrying_it(tmp_path, monkeypatch, provider):
    harbor = harbor_for(tmp_path, monkeypatch, provider)
    event = {"ticket": "SAMPLE-1", "run_id": "run-1", "instruction": "Review comment on src/app.py:12: rename helper()"}
    asyncio.run(harbor.dispatcher.handle_pr_event(event))
    assert len(harbor.launched) == 1 and event["instruction"] in harbor.launched[0]
    assert fresh_sessions(harbor.db) == [(recovery.UNRECORDED_IDENTITY_TRIGGER, "fresh_session")]
    assert tuple(runner_identity(harbor.db.connection, "run-1")) == (provider, CONFIGURED[provider]["model"], "low")
    # The next event resumes the now-identified session; no second fresh session.
    harbor.db.connection.execute("UPDATE runs SET status='waiting', session_id='native-2' WHERE id='run-1'")
    harbor.db.connection.commit()
    asyncio.run(harbor.dispatcher.handle_pr_event({**event, "instruction": "CI failed: lint"}))
    assert len(fresh_sessions(harbor.db)) == 1
    assert harbor.launched[-1].startswith("CI failed: lint")
    harbor.db.close()


def test_a_reengage_wake_on_an_unrecorded_run_is_delivered_into_a_fresh_session(tmp_path, monkeypatch):
    harbor = harbor_for(tmp_path, monkeypatch, "claude")
    wake = Wake({"identifier": "SAMPLE-1"}, "reengage", resume="run-1", instruction="CI failed on main: test_login")
    assert asyncio.run(harbor.dispatcher.resume(wake)) is not None
    assert len(harbor.launched) == 1 and "CI failed on main: test_login" in harbor.launched[0]
    assert tuple(runner_identity(harbor.db.connection, "run-1"))[0] == "claude"
    harbor.db.close()


def test_turning_fresh_sessions_off_in_config_toml_while_running_keeps_the_refusal(tmp_path, monkeypatch):
    harbor = harbor_for(tmp_path, monkeypatch)
    assert harbor.dispatcher.workflow.config["first_mate"]["allow_fresh_session"] is True  # loaded at start
    # The operator edits the project's config.toml while Harbor runs; nothing touches the in-memory copy.
    models = harbor.dispatcher.workflow.models_path
    data = tomllib.loads(models.read_text())
    data.setdefault("first_mate", {})["allow_fresh_session"] = False
    models.write_text(dumps(data))
    with pytest.raises(Exception, match="no retained builder"):
        asyncio.run(harbor.dispatcher.handle_pr_event({"ticket": "SAMPLE-1", "run_id": "run-1", "instruction": "x"}))
    assert harbor.launched == [] and fresh_sessions(harbor.db) == []
    assert runner_identity(harbor.db.connection, "run-1") is None
    harbor.db.close()


def test_an_unreadable_workflow_authorizes_no_fresh_session(tmp_path, monkeypatch):
    harbor = harbor_for(tmp_path, monkeypatch)
    harbor.dispatcher.workflow.models_path.write_text("[first_mate\n")  # invalid TOML
    with pytest.raises(Exception, match="no retained builder"):
        asyncio.run(harbor.dispatcher.handle_pr_event({"ticket": "SAMPLE-1", "run_id": "run-1", "instruction": "x"}))
    assert harbor.launched == [] and runner_identity(harbor.db.connection, "run-1") is None
    harbor.db.close()


def test_a_product_decision_escalates_and_is_never_fresh_sessioned_before_the_answer(tmp_path, monkeypatch):
    harbor = harbor_for(tmp_path, monkeypatch, status="blocked", final=HUMAN_BLOCKER, finished_minutes_ago=15)
    monitor_pass(harbor)
    actions = [r[0] for r in harbor.db.connection.execute("SELECT action FROM first_mate_recoveries")]
    assert actions == ["escalate"]  # the human-blocker guard, at its normal recheck cadence
    # A PR event before the human answers does not route around the question.
    with pytest.raises(Exception, match="no retained builder"):
        asyncio.run(harbor.dispatcher.handle_pr_event({"ticket": "SAMPLE-1", "run_id": "run-1", "instruction": "x"}))
    assert harbor.launched == [] and fresh_sessions(harbor.db) == []
    assert runner_identity(harbor.db.connection, "run-1") is None
    harbor.db.close()


def answered_escalation(db, answer):
    escalation = db.record_escalation("SAMPLE-1", "run-1", "product_decision", QUESTION, 2, status="open")
    db.update_escalation(escalation["id"], status="answered")
    instruction = f"Operator answer for escalation {escalation['id']}: {answer}\nAuthority citation: operator"
    assert answer_queue.enqueue(db.connection, escalation["id"], "SampleProject", "SAMPLE-1", "run-1", instruction) == "queued"
    return escalation["id"], instruction


def test_a_queued_answer_is_replayed_into_the_fresh_session(tmp_path, monkeypatch):
    harbor = harbor_for(tmp_path, monkeypatch, "claude", status="blocked", final=HUMAN_BLOCKER)
    escalation, instruction = answered_escalation(harbor.db, "Ship copy B.")
    assert asyncio.run(answer_queue.deliver(harbor.supervisor, escalation, "run-1", instruction)) is True
    status = harbor.db.connection.execute("SELECT status FROM answer_queue WHERE escalation=?", (str(escalation),)).fetchone()[0]
    assert status == "delivered"
    handoff = harbor.launched[0]
    assert instruction in handoff and QUESTION in handoff  # both verbatim
    assert fresh_sessions(harbor.db) == [(recovery.UNRECORDED_IDENTITY_TRIGGER, "fresh_session")]
    assert tuple(runner_identity(harbor.db.connection, "run-1")) == ("claude", "claude-sonnet-5-5", "low")
    harbor.db.close()


def test_an_answer_still_queued_rides_along_with_the_event_that_starts_the_session(tmp_path, monkeypatch):
    harbor = harbor_for(tmp_path, monkeypatch, status="blocked", final=HUMAN_BLOCKER)
    escalation, instruction = answered_escalation(harbor.db, "Ship copy A.")
    asyncio.run(harbor.dispatcher.handle_pr_event({"ticket": "SAMPLE-1", "run_id": "run-1", "instruction": "Rebase on main"}))
    handoff = harbor.launched[0]
    assert "Rebase on main" in handoff and instruction in handoff and QUESTION in handoff
    status = harbor.db.connection.execute("SELECT status FROM answer_queue WHERE escalation=?", (str(escalation),)).fetchone()[0]
    assert status == "delivered"  # not left to be refused and marked failed later
    harbor.db.close()


def test_a_failed_run_retry_is_delivered_into_a_fresh_session(tmp_path, monkeypatch):
    harbor = harbor_for(tmp_path, monkeypatch, "codex", status="failed")
    asyncio.run(harbor.dispatcher.retry("SAMPLE-1", "Operator: retry after the flaky network"))
    assert len(harbor.launched) == 1 and "Operator: retry after the flaky network" in harbor.launched[0]
    assert tuple(runner_identity(harbor.db.connection, "run-1"))[0] == "codex"
    harbor.db.close()


def test_first_mate_recovery_resume_is_delivered_into_a_fresh_session(tmp_path, monkeypatch):
    """find_cases -> First Mate -> validate -> _apply_recovery -> resume refused -> fresh session."""
    final = {"summary": "tests flaky", "blocker": {"kind": "env_blocker", "question": "builder stuck on a flaky test"}}
    harbor = harbor_for(tmp_path, monkeypatch, "claude", status="blocked", final=final, finished_minutes_ago=15)
    monitor_pass(harbor)
    assert len(harbor.mate.prompts) == 1  # an ordinary case: the model chose resume
    assert len(harbor.launched) == 1
    assert "continue" in harbor.launched[0] and "builder stuck on a flaky test" in harbor.launched[0]
    actions = [r[0] for r in harbor.db.connection.execute("SELECT action FROM first_mate_recoveries ORDER BY id")]
    assert actions == ["resume", "fresh_session"]
    assert tuple(runner_identity(harbor.db.connection, "run-1"))[0] == "claude"
    harbor.db.close()


