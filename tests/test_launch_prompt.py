"""Review findings #1 and #5: every launch renders the prompt from the reloaded workflow."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor.cli import _pipeline_text
from harbor.config.projects import ProjectConfig
from harbor.config.workflow import load_workflow
from harbor.db import HarborDB, apply_phase9_migration
from harbor.runner.base import apply_phase4_migration
from harbor.supervisor import WorktreeDispatcher
from harbor.wake import Wake, apply_phase3_migration
from workflow_support import config


class Recorder:
    def __init__(self):
        self.instructions = []

    async def start(self, instruction, *, run_id=None):
        self.instructions.append(instruction)
        return {"status": "completed", "outcome": "no_pr", "summary": "ok", "run_id": run_id}


def setup(tmp_path, monkeypatch, *, merge_mode="workflow_checked", renderer=None):
    registry = tmp_path / "projects.md"
    registry.write_text("# registry")
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "state.db"))
    path = config(tmp_path, extra=f'[delivery]\nmerge_mode="{merge_mode}"\nauto_merge_risks=["low"]\n')
    cfg = ProjectConfig(name="Demo", linear_project_slug="demo", repo_path=tmp_path, workflow_path=str(path),
                        registry_path=registry, install_command="none")
    db = HarborDB(tmp_path / "state.db")
    apply_phase3_migration(db); apply_phase4_migration(db); apply_phase9_migration(db)
    runner = Recorder()
    worktree = tmp_path / "wt" / "DEM-1"
    worktree.mkdir(parents=True)
    manager = SimpleNamespace(create=lambda ticket: worktree, attach=lambda ticket: worktree)
    dispatcher = WorktreeDispatcher(manager, lambda wt, wake: runner, db=db, project="Demo",
                                    prompt_renderer=renderer or (lambda current: _pipeline_text(cfg, current)))
    dispatcher.workflow = load_workflow(path)
    return dispatcher, runner, path, db


def test_dispatch_prompt_follows_reloaded_policy(tmp_path, monkeypatch):
    dispatcher, runner, path, db = setup(tmp_path, monkeypatch)
    path.write_text(path.read_text().replace('merge_mode = "workflow_checked"', 'merge_mode = "human"'))
    asyncio.run(dispatcher.dispatch(Wake(ticket={"identifier": "DEM-1"}, action="build", instruction="Build DEM-1")))
    sent = runner.instructions[0]
    assert '"merge_mode": "human"' in sent and '"merge_mode": "workflow_checked"' not in sent
    assert "Merge authority" in sent and sent.rstrip().endswith("Build DEM-1")
    stored = json.loads(db.connection.execute("SELECT config_json FROM workflow_runs").fetchone()[0])
    assert stored["delivery"]["merge_mode"] == "human"
    db.close()


def test_policy_is_bound_once_per_launch(tmp_path, monkeypatch):
    dispatcher, runner, path, db = setup(tmp_path, monkeypatch)
    asyncio.run(dispatcher.dispatch(Wake(ticket={"identifier": "DEM-1"}, action="build", instruction="Build")))
    assert db.connection.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM workflow_receipts WHERE kind='policy_change'").fetchone()[0] == 0
    db.close()


def test_fresh_session_receives_full_delivery_prompt(tmp_path, monkeypatch):
    dispatcher, runner, path, db = setup(tmp_path, monkeypatch, merge_mode="human")
    db.replace_tickets("Demo", [{"identifier": "DEM-1", "state": "Blocked", "title": "t", "owned": True, "hold": False}],
                       emit_events=False)
    db.connection.execute("INSERT INTO runs(id,ticket,status,session_id,worktree,started_at,finished_at) VALUES "
                          "('run-1','DEM-1','blocked','old-session',?,'2026-01-01T00:00:00+00:00','2026-01-01T00:01:00+00:00')",
                          (str(tmp_path / "wt" / "DEM-1"),))
    db.connection.commit()
    asyncio.run(dispatcher.fresh_session("DEM-1", "FIRST-MATE-HANDOFF: respawn all roles"))
    sent = runner.instructions[0]
    for marker in ("# Delivery workflow", "## Merge authority", '"merge_mode": "human"',
                   "Authoritative runtime invocation", "Merge authority: the policy's delivery.merge_mode"):
        assert marker in sent, marker
    # The hand-off follows the complete launch prompt.
    assert sent.index("FIRST-MATE-HANDOFF") > sent.index("Authoritative runtime invocation")
    assert sent.rstrip().endswith("FIRST-MATE-HANDOFF: respawn all roles")
    db.close()


def test_fresh_session_is_told_its_github_transport(tmp_path, monkeypatch):
    """A First Mate or quota-fallback session starts with no memory of the REST helper.

    Without the transport fact it fell back to `gh`, which fails in the sandbox
    (HAN-94: "gh pr list fails with x509: OSStatus -26276").
    """
    dispatcher, runner, path, db = setup(tmp_path, monkeypatch)
    dispatcher.github_fact = "GitHub transport: REST helper. TRANSPORT-SENTINEL"
    db.replace_tickets("Demo", [{"identifier": "DEM-1", "state": "Blocked", "title": "t", "owned": True, "hold": False}],
                       emit_events=False)
    db.connection.execute("INSERT INTO runs(id,ticket,status,session_id,worktree,started_at,finished_at) VALUES "
                          "('run-1','DEM-1','blocked','old-session',?,'2026-01-01T00:00:00+00:00','2026-01-01T00:01:00+00:00')",
                          (str(tmp_path / "wt" / "DEM-1"),))
    db.connection.commit()
    asyncio.run(dispatcher.fresh_session("DEM-1", "FIRST-MATE-HANDOFF"))
    sent = runner.instructions[0]
    assert "TRANSPORT-SENTINEL" in sent
    assert sent.index("Authoritative runtime invocation") < sent.index("TRANSPORT-SENTINEL") < sent.index("FIRST-MATE-HANDOFF")
    db.close()


def test_prompt_renderer_failure_is_reported_not_silent(tmp_path, monkeypatch):
    def broken(current):
        raise ValueError("workflow delivery requires its authoritative project registry")

    dispatcher, runner, path, db = setup(tmp_path, monkeypatch, renderer=broken)
    with pytest.raises(ValueError, match="authoritative project registry"):
        asyncio.run(dispatcher.dispatch(Wake(ticket={"identifier": "DEM-1"}, action="build", instruction="Build")))
    assert runner.instructions == []
    db.close()
