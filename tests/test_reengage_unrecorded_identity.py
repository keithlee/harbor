"""A re-engage wake whose run has no recorded runner identity fails closed.

The production factory used to assume such a run belonged to Codex and resume
it with a hard-coded Codex model, ignoring the project's configured provider
and models. A Claude-only project then launched Codex. Now a resume refuses
with a remedy, and a First Mate fresh session (which resumes no native
session) starts on the configured provider and records its identity.
"""
import asyncio
from types import SimpleNamespace

import pytest

from harbor.cli import build_production_dispatcher
from harbor.db import HarborDB
from harbor.runner.base import apply_phase4_migration
from harbor.runner.claude import ClaudeRunner
from harbor.runner.codex import CodexRunner
from harbor.runner.quota import bind_runner_identity, runner_identity
from harbor.supervisor import _MAX_RESUME_REASON_CHARS, UNRECORDED_IDENTITY
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


def retained_run(tmp_path):
    """A waiting run with a worktree and native session, but no runner_identity row."""
    db = HarborDB(tmp_path / "harbor.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    db.replace_tickets("SampleProject", [{"identifier": "SAMPLE-1", "title": "t", "state": "In Progress"}],
                       emit_events=False)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,session_id,worktree,deadline_seconds) "
        "VALUES ('run-1','SAMPLE-1','waiting','native-1',?,600)",
        (str(worktree),),
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
def test_fresh_session_without_identity_uses_configuration_and_records_it(tmp_path, monkeypatch, provider):
    config = project(tmp_path, monkeypatch, provider)
    db = retained_run(tmp_path)
    dispatcher, _, _ = build_production_dispatcher(config, db, worktree_manager=object())
    built = []

    async def runner_call(runner, wake, *, resume, before_spawn=None):
        # The real runner binds its identity in _execute; mirror that seam here.
        built.append((runner, wake, resume))
        bind_runner_identity(db.connection, wake.run_id, getattr(runner, "provider", "codex"), runner.model, runner.reasoning_effort)
        return {"status": "completed"}

    dispatcher._runner_call = runner_call
    asyncio.run(dispatcher.fresh_session("SAMPLE-1", "hand-off"))

    runner, wake, resume = built[0]
    assert type(runner) is RUNNERS[provider] and runner.model == CONFIGURED[provider]["model"]
    assert resume is False and wake.run_id == "run-1"
    assert tuple(runner_identity(db.connection, "run-1")) == (provider, CONFIGURED[provider]["model"], "low")
    # The next resume of the same run now rebuilds on the recorded identity.
    assert dispatcher.resume_unavailable_reason("SAMPLE-1") is None
    db.close()


def test_fresh_session_binds_identity_through_the_real_runner(tmp_path, monkeypatch):
    config = project(tmp_path, monkeypatch, "codex")
    db = retained_run(tmp_path)
    dispatcher, _, _ = build_production_dispatcher(config, db, worktree_manager=object())
    # /usr/bin/true exits at once; the identity is bound before the process spawns.
    asyncio.run(dispatcher.fresh_session("SAMPLE-1", "hand-off"))
    assert tuple(runner_identity(db.connection, "run-1")) == ("codex", "gpt-sample-configured", "low")
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
