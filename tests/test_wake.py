from __future__ import annotations

from pathlib import Path

import pytest

from harbor.db import HarborDB
from harbor.wake import (
    WakeDecider,
    apply_phase3_migration,
    new_comment_ids,
    observe_pr_facts,
    record_comment_receipt,
    record_push,
)


def ticket(identifier: str = "SAMPLE-42", state: str = "Todo", **extra: object) -> dict:
    value = {
        "identifier": identifier,
        "id": f"linear-{identifier}",
        "title": f"Title {identifier}",
        "state": state,
        "blocked_by": [],
        "owned": False,
        "hold": True,
    }
    value.update(extra)
    return value


def db_for(tmp_path: Path, value: dict) -> HarborDB:
    db = HarborDB(tmp_path / "harbor.db")
    db.replace_tickets("SampleProject", [value])
    apply_phase3_migration(db)
    return db


def run_row(db: HarborDB, *, run_id: str = "run-42", waiting_on: str = "review") -> None:
    db.connection.execute(
        "INSERT INTO runs(id, ticket, role, status, waiting_on) VALUES (?, ?, 'builder', 'waiting', ?)",
        (run_id, "SAMPLE-42", waiting_on),
    )
    db.connection.execute("UPDATE tickets SET owned = 1, state = 'In Progress' WHERE identifier = 'SAMPLE-42'")
    db.connection.commit()


def review_facts(head: str, *threads: dict) -> dict:
    return {"number": 221, "head_sha": head, "threads": list(threads)}


def bot_thread(thread_id: str, severity: str = "P2") -> dict:
    return {
        "id": thread_id,
        "is_bot": True,
        "severity": severity,
        "resolved": False,
        "path": "src/example.py",
        "line": 12,
        "body": f"**{severity}** please fix",
    }


def test_first_observation_of_existing_board_is_observed_only(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    board = [
        ticket("SAMPLE-29", "In Progress"),
        ticket("SAMPLE-16", "In Progress"),
        ticket("SAMPLE-17", "In Review"),
        ticket("SAMPLE-7", "In Progress"),
    ]
    db.replace_tickets("SampleProject", board)
    try:
        decider = WakeDecider(db, shadow=True)
        assert [decider.decide(item, {}) for item in board] == [None] * 4
    finally:
        db.close()


def test_todo_is_build_with_stable_idempotency_key(tmp_path: Path) -> None:
    item = ticket()
    db = db_for(tmp_path, item)
    try:
        wake = WakeDecider(db).decide(item, {})
        assert wake is not None
        assert wake.action == "build"
        assert wake.idem_key == "SAMPLE-42@build"
        assert db.connection.execute("SELECT count(*) FROM work_queue").fetchone()[0] == 1
    finally:
        db.close()


def test_two_cycles_do_not_insert_duplicate_build_rows(tmp_path: Path) -> None:
    item = ticket()
    db = db_for(tmp_path, item)
    try:
        decider = WakeDecider(db)
        assert decider.decide(item, {}) is not None
        assert decider.decide(item, {}) is None
        assert db.connection.execute("SELECT count(*) FROM work_queue").fetchone()[0] == 1
    finally:
        db.close()


def test_harbor_comment_on_live_build_does_not_create_a_row(tmp_path: Path) -> None:
    item = ticket()
    db = db_for(tmp_path, item)
    try:
        decider = WakeDecider(db)
        assert decider.decide(item, {}) is not None
        assert record_comment_receipt(db, item["id"], "comment-1", "Harbor started SAMPLE-42")
        # A changed Linear updatedAt/comment list is still blocked by the
        # stable live build row; no external Linear write is attempted here.
        changed = {**item, "comment_ids": ["comment-1"], "updated_at": "later"}
        assert decider.decide(changed, {}) is None
        assert db.connection.execute("SELECT count(*) FROM work_queue").fetchone()[0] == 1
    finally:
        db.close()


def test_two_unresolved_p2_bot_threads_reengage_same_run(tmp_path: Path) -> None:
    item = ticket()
    db = db_for(tmp_path, item)
    run_row(db)
    facts = review_facts("head-a", bot_thread("thread-1"), bot_thread("thread-2"))
    try:
        observe_pr_facts(db, item, facts)
        wake = WakeDecider(db).decide({**item, "state": "In Progress", "owned": True}, facts)
        assert wake is not None
        assert wake.action == "reengage"
        assert wake.resume == "run-42"
        assert {thread["id"] for thread in wake.facts["threads"]} == {"thread-1", "thread-2"}
        assert "<untrusted-review-comment>" in wake.instruction
    finally:
        db.close()


def test_second_bot_wave_stops_after_one_recorded_push(tmp_path: Path) -> None:
    item = ticket()
    db = db_for(tmp_path, item)
    run_row(db)
    try:
        first = review_facts("head-a", bot_thread("thread-1"))
        observe_pr_facts(db, item, first)
        assert WakeDecider(db).decide({**item, "state": "In Progress", "owned": True}, first).action == "reengage"
        assert record_push(db, item, "head-b", pr_number=221) == 1
        second = review_facts("head-b", bot_thread("thread-2"))
        observe_pr_facts(db, item, second)
        wake = WakeDecider(db).decide({**item, "state": "In Progress", "owned": True}, second)
        assert wake is not None
        assert wake.action == "stop"
        assert wake.notify is True
        assert "cap" in (wake.reason or "")
    finally:
        db.close()


def test_p3_only_threads_do_not_wake(tmp_path: Path) -> None:
    item = ticket()
    db = db_for(tmp_path, item)
    run_row(db)
    facts = review_facts("head-a", bot_thread("thread-3", "P3"))
    try:
        observe_pr_facts(db, item, facts)
        assert WakeDecider(db).decide({**item, "state": "In Progress", "owned": True}, facts) is None
        assert db.connection.execute("SELECT count(*) FROM work_queue").fetchone()[0] == 0
    finally:
        db.close()


def test_receipted_harbor_comment_is_never_new_input(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    apply_phase3_migration(db)
    try:
        record_comment_receipt(db, "linear-42", "harbor-comment", "Applied facts")
        comments = [
            {"id": "harbor-comment", "body": "Applied facts"},
            {"id": "human-comment", "body": "Please clarify"},
        ]
        assert new_comment_ids("linear-42", comments, db=db) == ["human-comment"]
    finally:
        db.close()


@pytest.mark.parametrize("state", ["In Progress", "In Review", "Blocked", "Human Review"])
def test_non_todo_ticket_is_never_a_build_wake(tmp_path: Path, state: str) -> None:
    item = ticket(state=state)
    db = db_for(tmp_path, item)
    try:
        assert WakeDecider(db, shadow=True).decide(item, {}) is None
    finally:
        db.close()



def test_hold_on_instructs_a_full_build_that_stops_at_the_merge():
    """Plan section 1: hold gates the MERGE, it does not stop the work.

    A bare "hold: on" reads as "do nothing", which parks the ticket with no
    PR and no work -- and Phase 4 AC-4 requires a PR to be OPEN with hold on.
    """

    from harbor.wake import build_instruction

    text = build_instruction(
        {"identifier": "SAMPLE-32", "title": "Update docs", "state": "Todo", "hold": True}
    )
    lowered = text.lower()
    assert "merge only" in lowered
    assert "open the pull request" in lowered
    assert "outcome=parked" in lowered
    assert "do not merge" in lowered
    assert "do not set the ticket done" in lowered
    assert "SAMPLE-32" in text


def test_hold_off_leaves_the_merge_to_the_skills_own_gate():
    from harbor.wake import build_instruction

    text = build_instruction({"identifier": "SAMPLE-32", "title": "x", "state": "Todo", "hold": False})
    assert "hold: OFF" in text
    assert "your own risk gate" in text.lower()
    # Harbor never states a merge verdict of its own.
    assert "do not merge" not in text.lower()


def test_the_build_instruction_never_names_a_skill():
    """Harbor imports no skill and names none except as a config string."""

    from harbor.wake import build_instruction

    for held in (True, False):
        text = build_instruction({"identifier": "SAMPLE-1", "title": "t", "state": "Todo", "hold": held})
        assert "work-pipeline" not in text
        assert "luna" not in text.lower() and "sol" not in text.lower()
