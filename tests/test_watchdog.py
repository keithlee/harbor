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
