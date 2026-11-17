import sqlite3
from datetime import datetime, timezone

from harbor.recovery import claim_quota_retry, defer_quota
from harbor.runner.quota import quota_error


def test_native_quota_classification():
    now = datetime.now(timezone.utc)
    assert quota_error({"type": "turn.failed", "error": {
        "codex_error_info": "usage_limit_exceeded", "resets_at": 1800000000,
    }}, now) == (True, 1800000000)
    assert quota_error({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": 1800000000,
    }}, now) == (True, 1800000000)
    assert quota_error({"type": "error", "error": {"code": "rate_limit_exceeded"}}, now) == (False, None)
    assert quota_error({"type": "turn.failed", "error": {
        "codex_error_info": "usage_limit_exceeded",
    }}, now) == (True, None)


def test_quota_schedule_survives_restart_and_claims_once(tmp_path):
    path = tmp_path / "ledger.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE runs (id TEXT, status TEXT, terminal_reason TEXT)")
    db.execute("INSERT INTO runs VALUES ('r', 'failed', 'usage_limit_exceeded')")
    defer_quota(db, "r", "codex", 200, 100)
    db.close()
    with sqlite3.connect(path) as reopened:
        assert not claim_quota_retry(reopened, "r", 199)
        assert claim_quota_retry(reopened, "r", 200)
        assert not claim_quota_retry(reopened, "r", 200)
        defer_quota(reopened, "r", "codex", None, 201)
        assert not claim_quota_retry(reopened, "r", 1100)
        assert claim_quota_retry(reopened, "r", 1101)
        assert reopened.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='recovery_receipts'"
        ).fetchone()[0] == 0


def test_expired_provider_reset_uses_backoff_instead_of_immediate_retry():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE runs (id TEXT, status TEXT, terminal_reason TEXT)")
    db.execute("INSERT INTO runs VALUES ('triage:expired', 'blocked', NULL)")

    defer_quota(db, "triage:expired", "codex", 99, 100)

    retry_at, confirmed, failures = db.execute(
        "SELECT next_retry, confirmed, failures FROM quota_recovery WHERE run_id=?",
        ("triage:expired",),
    ).fetchone()
    assert retry_at == 400
    assert confirmed == 0
    assert failures == 1
    db.close()


def test_human_review_quota_recovery_uses_retained_run(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from harbor.db import HarborDB
    from harbor.runner.base import apply_phase4_migration, apply_phase9_migration
    from harbor.supervisor import Lifecycle, Supervisor
    from harbor.wake import apply_phase3_migration

    db = HarborDB(tmp_path / "harbor.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    ticket = {"identifier": "MOCK-83", "title": "Review", "state": "Human Review",
              "owned": True, "hold": False}
    db.replace_tickets("mock", [ticket], emit_events=False)
    db.connection.execute(
        """INSERT INTO runs(id,ticket,status,terminal_reason,session_id,worktree,deadline_seconds)
        VALUES ('run-83','MOCK-83','failed','usage_limit_exceeded','retained',?,60)""",
        (str(tmp_path),),
    )
    defer_quota(db.connection, "run-83", "codex", 1, 0)
    calls = []

    class Dispatcher:
        async def retry(self, identifier, instruction):
            calls.append((identifier, instruction))

    supervisor = Supervisor(SimpleNamespace(project_name="mock"), db=db, dispatcher=Dispatcher())
    supervisor.lifecycle = Lifecycle.RUNNING
    supervisor._fleet_tickets = (ticket,)

    async def scenario():
        supervisor.linear_healthy = False
        await supervisor._retry_stopped_builders()
        assert not supervisor._dispatch_tasks
        supervisor.linear_healthy = True
        await supervisor._retry_stopped_builders()
        await supervisor._retry_stopped_builders()
        await asyncio.gather(*supervisor._dispatch_tasks.values())

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0][0] == "MOCK-83"
    assert "retained session retained" in calls[0][1]
    assert "Keep the existing PR open" in calls[0][1]
    assert not db.connection.execute("SELECT * FROM recovery_receipts").fetchall()
    db.close()


def test_legacy_classification_uses_latest_completed_turn(tmp_path, monkeypatch):
    import json

    from harbor.runner.quota import legacy_codex_quota

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    session = "01a1e206-64a0-70e2-ae18-183a99c4cb79"
    directory = tmp_path / "sessions" / "2026" / "11" / "15"
    directory.mkdir(parents=True)
    path = directory / f"rollout-2026-11-15T17-43-52-{session}.jsonl"
    when = datetime(2026, 11, 16, 21, 17, tzinfo=timezone.utc)
    quota = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete", "completed_at": when.timestamp(),
            "error": {"codex_error_info": "usage_limit_exceeded", "resets_at": 1800000000},
        },
    }
    path.write_text(json.dumps(quota) + "\n", encoding="utf-8")
    row = {"session_id": session, "finished_at": when.isoformat()}
    assert legacy_codex_quota(row) == (True, 1800000000)
    with path.open("a", encoding="utf-8") as stream:
        replay = {"type": "event_msg", "payload": {
            "type": "task_complete", "completed_at": when.timestamp() + 60,
        }}
        stream.write(json.dumps(replay) + "\n")
    assert legacy_codex_quota(row) == (False, None)


def test_configured_claude_factory_preserves_recorded_provider(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from harbor.cli import build_production_dispatcher
    from harbor.db import HarborDB
    from harbor.runner.claude import ClaudeRunner
    from harbor.runner.codex import CodexRunner
    from harbor.runner.quota import bind_runner_identity
    from harbor.wake import Wake

    monkeypatch.setenv("HARBOR_CLAUDE_EXECUTABLE", "/usr/bin/true")
    db = HarborDB(tmp_path / "harbor.db")
    config = SimpleNamespace(name="mock", builder_provider="claude", builder_model="sonnet", builder_effort="low")
    _, _, factory = build_production_dispatcher(config, db, worktree_manager=object())
    new = Wake({"identifier": "MOCK-1"}, "build", resume="new-logical-id")
    assert isinstance(factory(tmp_path, new), ClaudeRunner)
    bind_runner_identity(db.connection, "old-logical-id", "codex", "gpt-5.6-luna", "xhigh")
    existing = Wake({"identifier": "MOCK-2"}, "reengage", resume="old-logical-id")
    restored = factory(tmp_path, existing)
    assert type(restored) is CodexRunner
    assert restored.model == "gpt-5.6-luna"
    db.close()


def test_quota_recovery_replays_only_matching_operator_retry(tmp_path):
    # Use the production queue schema, including the failure receipt columns.
    from datetime import datetime

    from harbor.db import HarborDB, apply_phase9_migration
    from harbor.recovery import interrupted_retry_instruction
    from harbor.wake import apply_phase3_migration

    db = HarborDB(tmp_path / "harbor.db")
    apply_phase3_migration(db)
    apply_phase9_migration(db)
    ticket, run_id, session_id = "MOCK-1", "retained", "native"
    db.replace_tickets("mock", [{"identifier": ticket, "title": "Merge request"}], emit_events=False)
    key = f"{ticket}@retry@{run_id}"
    instruction = "Continue the approved merge and run the delivery gates."
    db.enqueue(ticket, "reengage", key, {
        "run_id": run_id, "session_id": session_id, "instruction": instruction,
    })
    db.transition_queue("failed", idem_key=key, error="Provider usage limit reached")
    row = {"id": run_id, "session_id": session_id, "finished_at": datetime.now(timezone.utc).isoformat()}
    assert interrupted_retry_instruction(db.connection, ticket, row) == instruction
    assert interrupted_retry_instruction(db.connection, ticket, {**row, "session_id": "other"}) is None
    assert interrupted_retry_instruction(db.connection, ticket, {
        **row, "finished_at": "2020-01-01T00:00:00Z",
    }) is None
    db.close()
