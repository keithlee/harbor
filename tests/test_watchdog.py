import json
import time

from harbor.db import HarborDB
from harbor.watchdog import tick
from test_first_mate_recovery import iso, ledger


def kinds(flags):
    return sorted(f["kind"] for f in flags)


def test_cleanup_stuck_after_merge(tmp_path):
    db = ledger(tmp_path, status="completed", final={"outcome": "merged", "pr_url": "https://x/pr/1"}, finished=45)
    db.connection.execute("UPDATE runs SET merge_verified=1")
    db.connection.commit()
    assert kinds(tick(db.connection, "Demo", minutes=30)) == ["cleanup_stuck"]
    assert tick(db.connection, "Demo", minutes=30) == []
    db.close()


def test_same_question_answered_over_and_over(tmp_path):
    db = ledger(tmp_path, status="blocked", finished=1)
    for _ in range(3):
        db.record_escalation("HAN-86", "run-HAN-86", "env_blocker", "Can Harbor clear /root/planner?", 2)
    db.connection.execute("UPDATE escalations SET status='answered'")
    db.connection.commit()
    assert kinds(tick(db.connection, "Demo", minutes=30)) == ["repeated_answer"]
    db.close()


def test_stale_heartbeat_and_heartbeat_file(tmp_path):
    db = HarborDB(tmp_path / "ledger.db")
    path = db.heartbeat_path("Demo")
    assert kinds(tick(db.connection, "Demo", heartbeat_path=path)) == ["supervisor_stale"]
    db.record_heartbeat("Demo", {"fleet": "RUNNING"})
    assert json.loads(path.read_text())["fleet"] == "RUNNING"
    assert tick(db.connection, "Demo", heartbeat_path=path) == []
    assert kinds(tick(db.connection, "Demo", heartbeat_path=path, now=time.time() + 3600)) == ["supervisor_stale"]
    db.close()


def test_cleanup_stuck_flag_refreshes_pr_links_and_cleans_up(tmp_path):
    """Live HAN-86: Done in Linear before Harbor saw the PR attachment."""
    import asyncio
    from test_first_mate_recovery import supervisor_for

    pr = "https://github.com/keithlee/Bookgen/pull/294"
    db = ledger(tmp_path, status="completed", final={"status": "completed", "outcome": "merged", "pr_url": pr}, finished=45)
    assert db.connection.execute("SELECT pr_urls_json FROM tickets").fetchone()[0] == "[]"
    assert [f["kind"] for f in tick(db.connection, "Demo", minutes=30)] == ["cleanup_stuck"]
    supervisor, _, _ = supervisor_for(tmp_path, db, {"action": "wait"})

    class Linear:
        async def fetch_issue_status(self, identifier):
            return {"identifier": identifier, "state": {"name": "Done", "type": "completed"},
                    "attachments": {"nodes": [{"url": pr, "sourceType": "github"}]}}

    supervisor.unblock_ladder.linear = Linear()  # production wiring: the ladder owns the client
    assert not hasattr(supervisor, "linear")
    supervisor.github_poller = object()
    cleaned = []

    async def reconcile(wake, result):
        return {**result, "merge_verified": True}

    async def cleanup(wake, result):
        cleaned.append((wake.ticket["identifier"], result["pr_url"]))

    supervisor._reconcile_runner_result = reconcile
    supervisor._cleanup_merged_worktree = cleanup
    asyncio.run(supervisor._monitor_blocked())
    assert cleaned == [("HAN-86", pr)]
    row = db.connection.execute("SELECT pr_urls_json, state_type FROM tickets").fetchone()
    assert json.loads(row[0]) == [pr] and row[1] == "completed"
    db.close()


def test_cleanup_without_a_linear_attachment_is_never_trusted(tmp_path):
    import asyncio
    from test_first_mate_recovery import supervisor_for

    db = ledger(tmp_path, status="completed", final={"status": "completed", "outcome": "merged",
                                                     "pr_url": "https://github.com/other/repo/pull/1"}, finished=45)
    tick(db.connection, "Demo", minutes=30)
    supervisor, _, _ = supervisor_for(tmp_path, db, {"action": "wait"})

    class Linear:
        async def fetch_issue_status(self, identifier):
            return {"identifier": identifier, "state": {"name": "Done", "type": "completed"}, "attachments": {"nodes": []}}

    supervisor.unblock_ladder.linear = Linear()
    supervisor.github_poller = object()
    cleaned = []

    async def cleanup(wake, result):
        cleaned.append(result)

    supervisor._cleanup_merged_worktree = cleanup
    asyncio.run(supervisor._monitor_blocked())
    assert cleaned == []  # the builder's claim alone never authorizes cleanup
    db.close()


def test_cleanup_stuck_reflags_hourly(tmp_path):
    db = ledger(tmp_path, status="completed", final={"outcome": "merged"}, finished=45)
    now = time.time()
    assert len(tick(db.connection, "Demo", minutes=30, now=now)) == 1
    assert tick(db.connection, "Demo", minutes=30, now=now + 60) == []
    assert len(tick(db.connection, "Demo", minutes=30, now=now + 3600)) == 1
    db.close()


def test_verified_merge_closes_open_escalations(tmp_path):
    import asyncio
    from test_first_mate_recovery import supervisor_for
    from harbor.wake import Wake

    db = ledger(tmp_path, status="completed", final={"status": "completed", "outcome": "merged"}, finished=45)
    db.record_escalation("HAN-86", "run-HAN-86", "conflicting_instruction", "Permit unregistered roles?", 2)
    supervisor, _, _ = supervisor_for(tmp_path, db, {"action": "wait"})

    async def observed(wake, result):
        return {"merged": True, "url": "https://github.com/x/y/pull/1", "head_sha": "a"}

    supervisor._observe_merge_for_result = observed
    supervisor._fact_confirms_merge = lambda fact: bool(fact and fact.get("merged"))
    wake = Wake(ticket={"identifier": "HAN-86"}, action="reengage", resume="run-HAN-86")
    asyncio.run(supervisor._reconcile_runner_result(wake, {"status": "completed", "outcome": "merged",
                                                            "pr_url": "https://github.com/x/y/pull/1", "run_id": "run-HAN-86"}))
    assert db.connection.execute("SELECT status FROM escalations").fetchone()[0] == "resolved"
    db.close()


def test_retry_of_an_already_verified_merge_also_closes_escalations(tmp_path):
    import asyncio
    from test_first_mate_recovery import supervisor_for

    pr = "https://github.com/x/y/pull/1"
    db = ledger(tmp_path, status="completed", final={"status": "completed", "outcome": "merged", "pr_url": pr}, finished=45)
    db.connection.execute("UPDATE runs SET merge_verified=1")
    db.connection.execute("UPDATE tickets SET pr_urls_json=?", (json.dumps([pr]),))
    db.connection.commit()
    db.record_escalation("HAN-86", "run-HAN-86", "conflicting_instruction", "Permit unregistered roles?", 2)
    supervisor, _, _ = supervisor_for(tmp_path, db, {"action": "wait"})
    statuses = []

    async def cleanup(wake, result):
        statuses.append(db.connection.execute("SELECT status FROM escalations").fetchone()[0])

    supervisor._cleanup_merged_worktree = cleanup
    asyncio.run(supervisor._reconcile_completed_merges())
    assert statuses == ["resolved"]  # closed before cleanup is attempted
    db.close()
