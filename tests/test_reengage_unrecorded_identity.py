"""A re-engage wake whose run has no recorded runner identity fails closed.

The production factory used to assume such a run belonged to Codex and resume
it with a hard-coded Codex model, ignoring the project's configured provider
and models. A Claude-only project then launched Codex. Now a resume refuses
with a remedy, First Mate routes the run to a fresh session without asking the
model, and the fresh session (which resumes no native session) starts like a
new ticket and records its identity.
"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

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
from workflow_support import materialize

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


def retained_run(tmp_path, status="waiting"):
    """A retained run with a worktree and native session, but no runner_identity row."""
    db = HarborDB(tmp_path / "harbor.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    db.replace_tickets("SampleProject", [{"identifier": "SAMPLE-1", "title": "t", "state": "In Progress",
                                          "owned": True, "hold": False}], emit_events=False)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,session_id,worktree,deadline_seconds) "
        "VALUES ('run-1','SAMPLE-1',?,'native-1',?,600)",
        (status, str(worktree)),
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


SETTINGS = {"max_recoveries": 3, "allow_fresh_session": True, "recheck_minutes": 10, "stall_minutes": 30}


@pytest.mark.parametrize("status", sorted(recovery.UNRECORDED_IDENTITY_STATUSES))
def test_first_mate_finds_every_retained_run_without_identity(tmp_path, status):
    db = retained_run(tmp_path, status)
    cases = recovery.find_cases(db.connection, "SampleProject", SETTINGS)
    assert [(c.run_id, c.trigger, c.status) for c in cases] == [("run-1", recovery.UNRECORDED_IDENTITY_TRIGGER, status)]
    db.close()


@pytest.mark.parametrize("status", ["running", "terminated", "completed"])
def test_live_stopped_and_identified_runs_are_not_unrecorded_cases(tmp_path, status):
    db = retained_run(tmp_path, status)
    assert all(c.trigger != recovery.UNRECORDED_IDENTITY_TRIGGER
               for c in recovery.find_cases(db.connection, "SampleProject", SETTINGS))
    db.connection.execute("UPDATE runs SET status='waiting'")
    bind_runner_identity(db.connection, "run-1", "codex", "gpt-sample-recorded", "high")
    assert recovery.find_cases(db.connection, "SampleProject", SETTINGS) == []
    db.close()


def test_run_without_a_worktree_is_not_an_unrecorded_case(tmp_path):
    db = retained_run(tmp_path)
    db.connection.execute("UPDATE runs SET worktree=NULL")
    db.connection.commit()
    assert recovery.find_cases(db.connection, "SampleProject", SETTINGS) == []
    db.close()


def test_validate_routes_an_unrecorded_identity_to_one_fresh_session():
    case = recovery.Case("SampleProject", "SAMPLE-1", "run-1", recovery.UNRECORDED_IDENTITY_TRIGGER, "waiting")
    for proposal in ({}, {"action": "resume", "instruction": "continue", "reason": "r"}, {"action": "wait"}):
        decision = recovery.validate(proposal, case, SETTINGS)
        assert decision["action"] == "fresh_session"
        assert decision["instruction"] == recovery.UNRECORDED_IDENTITY_INSTRUCTION
    # An operator answer to a human blocker is a resume, which cannot be delivered either.
    human = replace(case, blocker={"kind": "product_decision", "question": "Which copy?"})
    assert recovery.validate({}, human, SETTINGS)["action"] == "fresh_session"
    assert recovery.validate({}, case, {**SETTINGS, "allow_fresh_session": False})["action"] == "escalate"
    assert recovery.validate({}, replace(case, history=[{"action": "fresh_session"}]), SETTINGS)["action"] == "escalate"
    spent = replace(case, history=[{"action": "resume"}] * 3)
    assert recovery.validate({}, spent, SETTINGS)["action"] == "escalate"
    escalated = replace(case, history=[{"action": "fresh_session"}, {"action": "escalate"}])
    assert recovery.validate({}, escalated, SETTINGS)["action"] == "wait"  # escalate once, never loop


class Poller:
    project_name = "SampleProject"
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", heartbeat_seconds=1)

    async def poll(self):
        return PollResult((), "snapshot", False, ())


class Mate:
    def __init__(self):
        self.prompts = []

    async def triage(self, prompt):
        self.prompts.append(prompt)
        return '{"action": "resume", "instruction": "continue", "reason": "model guess"}'


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("status", ["waiting", "parked", "blocked", "failed"])
def test_supervisor_loop_recovers_an_unrecorded_run_on_the_configured_provider(tmp_path, monkeypatch, provider, status):
    """find_cases -> validate -> _apply_recovery -> fresh_session -> the real factory and runner."""
    config = project(tmp_path, monkeypatch, provider)
    db = retained_run(tmp_path, status)
    dispatcher, _, _ = build_production_dispatcher(config, db, worktree_manager=object())
    mate = Mate()
    ladder = SimpleNamespace(first_mate=mate, linear=None, workflow=dispatcher.workflow)
    supervisor = Supervisor(Poller(), db=db, dispatcher=dispatcher, unblock_ladder=ladder)

    async def poll_once():
        observation = PollResult(tuple(db.tickets("SampleProject")), "snapshot", True, ())
        supervisor.last_result, supervisor.linear_healthy = observation, True
        await supervisor._monitor_blocked(observation)
        await supervisor._drain_recoveries()

    asyncio.run(poll_once())
    assert mate.prompts == []  # deterministic: the model is not asked
    assert [r[0] for r in db.connection.execute("SELECT action FROM first_mate_recoveries")] == ["fresh_session"]
    assert tuple(runner_identity(db.connection, "run-1")) == (provider, CONFIGURED[provider]["model"], "low")
    assert type(dispatcher.runners["SAMPLE-1"]) is RUNNERS[provider]
    # Now identified, the run is no longer an unrecorded case and resumes like any other.
    assert all(c.trigger != recovery.UNRECORDED_IDENTITY_TRIGGER
               for c in recovery.find_cases(db.connection, "SampleProject", SETTINGS))
    db.close()
