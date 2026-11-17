"""Golden quota classification table and retry schedule."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from harbor.recovery import claim_quota_retry, defer_quota
from harbor.runner.quota import quota_error

OBSERVED = datetime(2026, 11, 16, 21, 17, tzinfo=timezone.utc)

CASES = {
    "codex_turn_failed_with_reset": {"type": "turn.failed", "error": {"codex_error_info": "usage_limit_exceeded", "resets_at": 1800000000}},
    "codex_turn_failed_no_reset": {"type": "turn.failed", "error": {"codex_error_info": "usage_limit_exceeded"}},
    "codex_code_field": {"type": "error", "code": "usage_limit_exceeded", "reset_at": "2027-01-15T08:00:00+00:00"},
    "naive_iso_reset_is_ignored": {"type": "error", "code": "usage_limit_exceeded", "reset_at": "2027-01-15T08:00:00"},
    "boolean_reset_is_ignored": {"type": "error", "code": "usage_limit_exceeded", "resets_at": True},
    "nan_reset_is_ignored": {"type": "error", "code": "usage_limit_exceeded", "resets_at": "nan"},
    "absurd_reset_is_ignored": {"type": "error", "code": "usage_limit_exceeded", "resets_at": 1e20},
    "flattened_message_curly_quote": {"type": "error", "message": "You’ve hit your usage limit. Upgrade or try again later."},
    "flattened_message_straight_quote": {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}},
    "nested_json_message": {"type": "error", "message": json.dumps({"error": {"type": "usage_limit_exceeded", "resets_at": 1800000600}})},
    "claude_rate_limit_rejected": {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1800000900}},
    "claude_rate_limit_allowed_warning": {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "resetsAt": 1800000900}},
    "rate_limit_exceeded_is_not_quota": {"type": "error", "error": {"code": "rate_limit_exceeded"}},
    "assistant_message_quoting_limit_is_not_quota": {"type": "item.completed", "message": "You've hit your usage limit."},
    "message_not_at_start_is_not_quota": {"type": "error", "message": "Note: You've hit your usage limit."},
    "unrelated_event": {"type": "thread.started", "thread_id": "x"},
}


def test_quota_classification_table(golden) -> None:
    table = {name: list(quota_error(event, OBSERVED)) for name, event in CASES.items()}
    golden.json("quota_classification.json", table)


def test_quota_backoff_schedule_without_provider_reset(golden) -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE runs (id TEXT, status TEXT, terminal_reason TEXT)")
    db.execute("INSERT INTO runs VALUES ('r', 'failed', 'usage_limit_exceeded')")
    schedule = []
    now = 1_000.0
    for _ in range(5):
        defer_quota(db, "r", "codex", None, now)
        retry_at, confirmed, failures = db.execute(
            "SELECT next_retry, confirmed, failures FROM quota_recovery WHERE run_id='r'").fetchone()
        schedule.append({"failures": failures, "delay": retry_at - now, "confirmed": confirmed})
        assert not claim_quota_retry(db, "r", retry_at - 1)
        assert claim_quota_retry(db, "r", retry_at)
        now = retry_at + 1
    golden.json("quota_backoff_schedule.json", schedule)


def test_quota_claim_requires_the_run_to_still_be_quota_failed() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE runs (id TEXT, status TEXT, terminal_reason TEXT)")
    db.execute("INSERT INTO runs VALUES ('r', 'completed', NULL)")
    defer_quota(db, "r", "codex", 10, 0)
    assert not claim_quota_retry(db, "r", 100)
