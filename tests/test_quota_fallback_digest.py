from harbor import first_mate_recovery as recovery
from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB, apply_phase9_migration
from harbor.digest import render_digest
from harbor.poll.linear_poller import PollResult
from harbor.runner.base import apply_phase4_migration
from harbor.supervisor import Supervisor
from harbor.wake import apply_phase3_migration


class Poller:
    project_name = "SampleProject"
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", heartbeat_seconds=1)

    async def poll(self):
        return PollResult((), "snapshot-1", False, ())


def test_digest_renders_a_switched_run():
    digest = render_digest({
        "project": "P", "lifecycle": "RUNNING", "linear_healthy": True, "tickets": [],
        "runs": [{"id": "run-1", "status": "waiting", "provider_switch": {"from": "codex", "to": "claude"}}],
    })
    assert "switched codex -> claude after usage limit" in digest


def test_digest_omits_the_line_for_unswitched_runs():
    digest = render_digest({
        "project": "P", "lifecycle": "RUNNING", "linear_healthy": True, "tickets": [],
        "runs": [{"id": "run-1", "status": "waiting"}],
    })
    assert "switched" not in digest


def test_status_surfaces_the_ledger_receipt(tmp_path):
    db = HarborDB(tmp_path / "harbor.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    db.replace_tickets("SampleProject", [{"identifier": "SAMPLE-42", "state": "In Progress", "title": "Copy",
                                          "owned": True, "hold": False}], emit_events=False)
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,session_id,worktree) VALUES ('run-42','SAMPLE-42','waiting','s',?)",
        (str(tmp_path),),
    )
    recovery.record(db.connection, recovery.Case("SampleProject", "SAMPLE-42", "run-42", "failed", "failed"),
                    {"action": "quota_fallback", "instruction": "", "reason": "quota_fallback: codex -> claude"})
    digest = render_digest(Supervisor(Poller(), db=db).status())
    assert "run-42" in digest and "switched codex -> claude after usage limit" in digest
    db.close()
