"""A re-engage wake whose run has no recorded runner identity fails closed.

The production factory used to assume such a run belonged to Codex and resume
it with a hard-coded Codex model, ignoring the project's configured provider
and models. A Claude-only project then launched Codex.
"""
import asyncio
from types import SimpleNamespace

import pytest

from harbor.cli import build_production_dispatcher
from harbor.db import HarborDB
from harbor.runner.base import apply_phase4_migration
from harbor.runner.claude import ClaudeRunner
from harbor.runner.quota import bind_runner_identity, runner_identity
from harbor.wake import Wake, apply_phase3_migration
from workflow_support import materialize


def claude_project(tmp_path, monkeypatch):
    monkeypatch.setenv("HARBOR_CLAUDE_EXECUTABLE", "/usr/bin/true")
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/usr/bin/true")
    monkeypatch.delenv("HARBOR_PROJECTS_FILE", raising=False)  # hermetic: never the operator's registry
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "harbor.db"))
    registry = tmp_path / "projects.md"
    registry.write_text("# Harbor projects\n")
    workflow = materialize(tmp_path, models={"controller": {"model": "sonnet", "reasoning_effort": "low"}})
    return SimpleNamespace(name="SampleProject", builder_provider="claude", builder_model="sonnet",
                           builder_effort="low", workflow_path=str(workflow), install_command="none",
                           registry_path=str(registry))


@pytest.mark.parametrize("run_id", ["run-without-identity", None])
def test_reengage_without_identity_refuses_instead_of_guessing_codex(tmp_path, monkeypatch, run_id):
    config = claude_project(tmp_path, monkeypatch)
    db = HarborDB(tmp_path / "harbor.db")
    _, _, factory = build_production_dispatcher(config, db, worktree_manager=object())
    with pytest.raises(ValueError, match="no recorded runner identity"):
        factory(tmp_path, Wake({"identifier": "SAMPLE-1"}, "reengage", resume=run_id))
    db.close()


def test_build_and_recorded_reengage_still_use_configuration(tmp_path, monkeypatch):
    config = claude_project(tmp_path, monkeypatch)
    db = HarborDB(tmp_path / "harbor.db")
    _, _, factory = build_production_dispatcher(config, db, worktree_manager=object())
    built = factory(tmp_path, Wake({"identifier": "SAMPLE-1"}, "build", resume="new-run"))
    assert type(built) is ClaudeRunner and built.model == "sonnet"
    bind_runner_identity(db.connection, "recorded-run", "claude", "sonnet", "low")
    resumed = factory(tmp_path, Wake({"identifier": "SAMPLE-1"}, "reengage", resume="recorded-run"))
    assert type(resumed) is ClaudeRunner and resumed.model == "sonnet"
    db.close()


def test_hydrating_a_run_without_identity_reports_the_reason(tmp_path, monkeypatch):
    config = claude_project(tmp_path, monkeypatch)
    db = HarborDB(tmp_path / "harbor.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    db.replace_tickets("SampleProject", [{"identifier": "SAMPLE-1", "title": "t", "state": "In Progress"}],
                       emit_events=False)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,session_id,worktree,deadline_seconds) "
        "VALUES ('run-1','SAMPLE-1','waiting','native-1',?,60)",
        (str(worktree),),
    )
    db.connection.commit()
    assert runner_identity(db.connection, "run-1") is None
    dispatcher, _, _ = build_production_dispatcher(config, db, worktree_manager=object())

    assert asyncio.run(dispatcher.hydrate("SAMPLE-1")) is None
    assert "SAMPLE-1" not in dispatcher.runners
    assert "no recorded runner identity" in dispatcher._resume_errors["sample-1"].reason
    db.close()
