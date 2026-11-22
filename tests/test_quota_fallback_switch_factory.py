"""A provider switch builds its runner through the REAL cli factory, both directions."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from harbor.cli import _quota_policy, build_production_dispatcher
from harbor.db import HarborDB, apply_phase9_migration
from harbor.recovery import defer_quota
from harbor.runner.base import apply_phase4_migration
from harbor.runner.claude import ClaudeRunner
from harbor.runner.codex import CodexRunner
from harbor.runner.quota import bind_runner_identity, runner_identity
from harbor.wake import Wake, apply_phase3_migration
from workflow_support import materialize

PROFILES = {
    "codex": {"model": "gpt-6.1-terra", "reasoning_effort": "minimal"},  # Codex-only effort
    "claude": {"model": "claude-sonnet-5-5", "reasoning_effort": "max"},
}
DIRECTIONS = [("codex", "claude"), ("claude", "codex")]


def project(tmp_path, monkeypatch, primary, fallback, *, fallback_effort=True):
    monkeypatch.setenv("HARBOR_CLAUDE_EXECUTABLE", "/usr/bin/true")
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/usr/bin/true")
    monkeypatch.delenv("HARBOR_PROJECTS_FILE", raising=False)  # hermetic: never the operator's registry
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "harbor.db"))
    registry = tmp_path / "projects.md"
    registry.write_text("# Harbor projects\n")
    profile = {"provider": fallback, "model": PROFILES[fallback]["model"]}
    if fallback_effort:
        profile["reasoning_effort"] = "high"
    workflow = materialize(tmp_path, models={"controller": PROFILES[primary], "quota_fallback": {"controller": profile}})
    return SimpleNamespace(name="mock", builder_provider=primary, builder_model=PROFILES[primary]["model"],
                           builder_effort=PROFILES[primary]["reasoning_effort"], firstmate_provider="codex",
                           workflow_path=str(workflow), install_command="none", registry_path=str(registry))


def waiting_run(tmp_path, primary):
    db = HarborDB(tmp_path / "harbor.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    db.replace_tickets("mock", [{"identifier": "MOCK-1", "title": "t", "state": "In Progress"}], emit_events=False)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,terminal_reason,session_id,worktree,deadline_seconds,final_json) "
        "VALUES ('run-1','MOCK-1','failed','usage_limit_exceeded','native-1',?,60,?)",
        (str(worktree), json.dumps({"summary": "usage limit"})),
    )
    bind_runner_identity(db.connection, "run-1", primary, PROFILES[primary]["model"], PROFILES[primary]["reasoning_effort"])
    defer_quota(db.connection, "run-1", primary, time.time() + 10_000, time.time())
    return db


def switch(config, db, policy):
    dispatcher, _, _ = build_production_dispatcher(config, db, worktree_manager=object(), quota_policy=policy)
    built = []

    async def runner_call(runner, wake, *, resume):
        built.append(runner)
        return {"status": "completed"}

    dispatcher._runner_call = runner_call
    asyncio.run(dispatcher.fresh_session("MOCK-1", "hand-off", switch=policy.builder[1]))
    return built[0]


def state(db):
    run = db.connection.execute("SELECT status, session_id, terminal_reason FROM runs WHERE id='run-1'").fetchone()
    waiting = db.connection.execute("SELECT provider FROM quota_recovery WHERE run_id='run-1'").fetchone()
    return tuple(run), tuple(runner_identity(db.connection, "run-1")), waiting and tuple(waiting)


@pytest.mark.parametrize("primary,fallback", DIRECTIONS)
def test_fallback_profile_without_effort_fails_closed(tmp_path, monkeypatch, primary, fallback):
    config = project(tmp_path, monkeypatch, primary, fallback, fallback_effort=False)
    with pytest.raises(ValueError, match="quota_fallback.controller.reasoning_effort"):
        _quota_policy(config)


@pytest.mark.parametrize("primary,fallback", DIRECTIONS)
def test_switch_builds_fallback_runner_through_real_factory(tmp_path, monkeypatch, primary, fallback):
    config = project(tmp_path, monkeypatch, primary, fallback)
    db = waiting_run(tmp_path, primary)
    runner = switch(config, db, _quota_policy(config))
    assert type(runner) is (ClaudeRunner if fallback == "claude" else CodexRunner)
    assert (runner.model, runner.reasoning_effort) == (PROFILES[fallback]["model"], "high")
    assert tuple(runner_identity(db.connection, "run-1")) == (fallback, PROFILES[fallback]["model"], "high")
    assert db.connection.execute("SELECT 1 FROM quota_recovery WHERE run_id='run-1'").fetchone() is None
    db.close()


@pytest.mark.parametrize("primary,fallback", DIRECTIONS)
def test_new_ticket_on_fallback_uses_fallback_effort(tmp_path, monkeypatch, primary, fallback):
    from harbor.recovery import record_provider_quota
    config = project(tmp_path, monkeypatch, primary, fallback)
    db = HarborDB(tmp_path / "harbor.db")
    record_provider_quota(db.connection, primary, time.time() + 10_000, time.time())
    policy = _quota_policy(config)
    policy = type(policy)(policy.max_switches, policy.builder, policy.mate, policy.roles, lambda profile: None)
    _, _, factory = build_production_dispatcher(config, db, worktree_manager=object(), quota_policy=policy)
    runner = factory(tmp_path, Wake({"identifier": "MOCK-2"}, "build", resume="run-2"))
    assert type(runner) is (ClaudeRunner if fallback == "claude" else CodexRunner)
    assert (runner.model, runner.reasoning_effort) == (PROFILES[fallback]["model"], "high")
    db.close()


@pytest.mark.parametrize("primary,fallback", DIRECTIONS)
def test_failed_switch_leaves_run_waiting_for_reset(tmp_path, monkeypatch, primary, fallback):
    config = project(tmp_path, monkeypatch, primary, fallback)
    db = waiting_run(tmp_path, primary)
    policy = _quota_policy(config)
    before = state(db)
    # The fallback runner cannot be built (e.g. its CLI disappeared after the readiness check).
    if fallback == "claude":
        monkeypatch.setenv("HARBOR_CLAUDE_EXECUTABLE", str(tmp_path / "missing"))
    else:
        monkeypatch.setattr(CodexRunner, "__init__", lambda *a, **k: (_ for _ in ()).throw(ValueError("codex gone")))
    with pytest.raises(RuntimeError, match="cannot be attached"):
        switch(config, db, policy)
    assert state(db) == before
    assert before[0] == ("failed", "native-1", "usage_limit_exceeded") and before[2] == (primary,)
    db.close()
