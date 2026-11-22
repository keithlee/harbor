import time
from types import SimpleNamespace

import pytest

from harbor.cli import _fallback_report, _quota_policy, build_production_dispatcher
from harbor.db import HarborDB
from harbor.quota_fallback import Profile
from harbor.recovery import record_provider_quota
from harbor.runner.claude import ClaudeRunner
from harbor.runner.codex import CodexRunner
from harbor.runner.quota import bind_runner_identity
from harbor.wake import Wake
from workflow_support import materialize

MODELS = {
    "controller": {"model": "claude-sonnet-5-5", "reasoning_effort": "medium"},
    "first_mate": {"model": "claude-haiku-5-5"},
    "quota_fallback": {
        "controller": {"provider": "codex", "model": "gpt-6.1-terra", "reasoning_effort": "medium"},
        "first_mate": {"provider": "codex", "model": "gpt-6-luna"},
    },
}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HARBOR_CLAUDE_EXECUTABLE", "/usr/bin/true")
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/usr/bin/true")
    db = HarborDB(tmp_path / "harbor.db")
    workflow = materialize(tmp_path, models=MODELS)
    config = SimpleNamespace(name="mock", builder_provider="claude", builder_model="claude-sonnet-5-5",
                             builder_effort="medium", firstmate_provider="claude",
                             workflow_path=str(workflow), install_command="none")
    policy = _quota_policy(config)
    policy = type(policy)(policy.max_switches, policy.builder, policy.mate, policy.roles, lambda profile: None)
    _, _, factory = build_production_dispatcher(config, db, worktree_manager=object(), quota_policy=policy)
    yield db, factory, config, policy
    db.close()


def new_ticket(ticket="MOCK-1"):
    return Wake({"identifier": ticket}, "build", resume=f"run-{ticket}")


def test_policy_built_from_project_and_workflow(setup):
    _, _, _, policy = setup
    assert policy.builder[0].provider == "claude" and policy.builder[1].provider == "codex"
    assert policy.mate[1] == Profile("codex", "gpt-6-luna", None)


def test_factory_starts_new_ticket_on_fallback_when_primary_exhausted(setup, tmp_path):
    db, factory, _, _ = setup
    record_provider_quota(db.connection, "claude", time.time() + 10_000, time.time())
    runner = factory(tmp_path, new_ticket())
    assert type(runner) is CodexRunner
    assert runner.model == "gpt-6.1-terra"


def test_factory_keeps_primary_when_not_exhausted(setup, tmp_path):
    _, factory, _, _ = setup
    assert type(factory(tmp_path, new_ticket())) is ClaudeRunner


def test_factory_keeps_primary_when_both_exhausted(setup, tmp_path):
    db, factory, _, _ = setup
    record_provider_quota(db.connection, "claude", time.time() + 10_000, time.time())
    record_provider_quota(db.connection, "codex", time.time() + 10_000, time.time())
    assert type(factory(tmp_path, new_ticket())) is ClaudeRunner


def test_factory_reengage_keeps_retained_identity(setup, tmp_path):
    db, factory, _, _ = setup
    record_provider_quota(db.connection, "claude", time.time() + 10_000, time.time())
    bind_runner_identity(db.connection, "old-run", "claude", "claude-sonnet-5-5", "medium")
    restored = factory(tmp_path, Wake({"identifier": "MOCK-2"}, "reengage", resume="old-run"))
    assert type(restored) is ClaudeRunner  # a retained builder is never moved by the factory


def test_no_section_means_no_policy(tmp_path):
    workflow = materialize(tmp_path, models={"controller": {"model": "claude-sonnet-5-5"}})
    config = SimpleNamespace(name="mock", builder_provider="claude", workflow_path=str(workflow))
    assert _quota_policy(config) is None
    assert _fallback_report(None) == ["Fallback: none"]


def test_doctor_report_lines(setup):
    _, _, _, policy = setup
    assert _fallback_report(policy) == [
        "Fallback builder: codex gpt-6.1-terra ready",
        "Fallback First Mate: codex gpt-6-luna ready",
    ]
    broken = type(policy)(policy.max_switches, policy.builder, None, policy.roles, lambda profile: "not logged in")
    assert _fallback_report(broken) == ["Fallback builder: codex gpt-6.1-terra not ready (not logged in)"]


def test_invalid_fallback_fails_policy_construction(tmp_path):
    models = {**MODELS, "quota_fallback": {"controller": {"provider": "claude", "model": "claude-opus-5-5", "reasoning_effort": "high"}}}
    workflow = materialize(tmp_path, models=models)
    config = SimpleNamespace(name="mock", builder_provider="claude", firstmate_provider="claude", workflow_path=str(workflow))
    with pytest.raises(ValueError, match="differ"):
        _quota_policy(config)
