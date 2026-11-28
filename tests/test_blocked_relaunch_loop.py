"""A run blocked waiting on a human is relaunched only for genuinely new input.

Regression for a live loop: a human Linear comment re-engaged a retained
builder, the builder hit the same environment blocker and parked
``blocked``/``waiting_on=human``.  The runner closed that comment's queue
receipt as ``failed``, which the WakeDecider reads as "the wake never reached
the builder", so every poll re-enqueued the same comment version and started
another paid builder turn, which opened another superseding escalation.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB
from harbor.poll.linear_poller import PollResult
from harbor.runner.base import apply_phase4_migration
from harbor.runner.codex import CodexRunner
from harbor.supervisor import Supervisor
from harbor.unblock import UnblockLadder
from harbor.wake import (
    MAX_FAILED_DELIVERIES,
    REDELIVERY_CAP_REASON,
    Wake,
    WakeDecider,
    apply_phase3_migration,
    record_comment_receipt,
)

RUN_ID = "run-7"
QUESTION = "Can the active runner be given file edit permission for the protected path?"


def _ticket(comments: list[dict[str, object]]) -> dict[str, object]:
    return {
        "identifier": "SAMPLE-7",
        "id": "linear-sample-7",
        "title": "Blocked on a protected path",
        "state": "In Progress",
        "blocked_by": [],
        "owned": True,
        "hold": True,
        "linear_comments": comments,
    }


def _human(comment_id: str, body: str, at: str) -> dict[str, object]:
    return {
        "id": comment_id,
        "body": body,
        "created_at": at,
        "updated_at": at,
        "author": {"id": "owner-1", "name": "Owner"},
    }


def _builder(tmp_path: Path, mode: str = "blocked") -> str:
    """A fake provider CLI whose every turn ends the same way.

    ``blocked`` parks on the same human blocker, ``schema_failed`` reports a
    schema-valid ``failed`` result, and ``crash`` exits without a result.
    """

    path = tmp_path / "builder.py"
    final: dict[str, object] = {
        "status": "blocked",
        "outcome": "parked",
        "waiting_on": "human",
        "summary": "protected path still needs a human",
        "next_action": "wait for a human answer",
        "pr_url": None,
        "blocker": {
            "kind": "env_blocker",
            "question": QUESTION,
            "evidence": "edit denied for src/lib/schema.ts",
            "options": ["grant edit permission", "a person edits the file"],
        },
    }
    if mode == "schema_failed":
        final = {"status": "failed", "summary": "the gate failed", "next_action": None, "pr_url": None, "blocker": None}
    ending = (
        "raise SystemExit(1)\n"
        if mode == "crash"
        else f"print(json.dumps({{'type': 'result', 'result': {final!r}}}), flush=True)\n"
    )
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "with (pathlib.Path.cwd() / 'launches.jsonl').open('a') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'session-7'}), flush=True)\n"
        + ending
    )
    path.chmod(0o755)
    return str(path)


def _launches(tmp_path: Path) -> int:
    log = tmp_path / "launches.jsonl"
    return len(log.read_text().splitlines()) if log.exists() else 0


def _db(tmp_path: Path, ticket: dict[str, object]) -> HarborDB:
    db = HarborDB(tmp_path / "harbor.db")
    db.replace_tickets("SampleProject", [ticket])
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    db.connection.execute(
        "INSERT INTO runs(id, ticket, role, status, waiting_on, worktree, session_id, started_at) "
        "VALUES (?, 'SAMPLE-7', 'builder', 'blocked', 'human', ?, 'session-7', '2026-09-01T00:00:00+00:00')",
        (RUN_ID, str(tmp_path)),
    )
    db.connection.commit()
    return db


class _Linear:
    project_name = "SampleProject"
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project")
    db = None

    def __init__(self, ticket: dict[str, object]) -> None:
        self.ticket = ticket
        self.polls = 0

    async def poll(self) -> PollResult:
        self.polls += 1
        # A fresh snapshot each poll, as Linear reports after Harbor posts a
        # comment of its own.
        return PollResult((self.ticket,), f"linear-snapshot-{self.polls}", True, (self.ticket,))


async def _poll_and_settle(supervisor: Supervisor) -> None:
    await supervisor.poll_once()
    tasks = tuple(supervisor._dispatch_tasks.values())
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


def test_runner_closes_a_blocked_turn_as_delivered_not_failed(tmp_path: Path) -> None:
    comment = _human("human-1", "approve. the agent may update the schema", "2026-09-02T00:00:00+00:00")
    ticket = _ticket([comment])
    db = _db(tmp_path, ticket)
    try:
        wake = WakeDecider(db).decide(ticket, {})
        assert wake is not None and wake.resume == RUN_ID
        runner = CodexRunner(tmp_path, db=db, ticket="SAMPLE-7", executable=_builder(tmp_path), deadline_seconds=30)
        result = asyncio.run(runner.resume(RUN_ID, wake.instruction))
        assert result.status == "blocked"
        status = db.connection.execute(
            "SELECT status FROM work_queue WHERE idem_key=? ORDER BY id DESC LIMIT 1", (wake.idem_key,)
        ).fetchone()[0]
        # The wake reached the builder and the turn ended; it was not a
        # delivery failure that should be retried.
        assert status == "completed"
        assert WakeDecider(db).decide(ticket, {}) is None
    finally:
        db.close()


class _LinearWithComments(_Linear):
    """Linear double for both the poller and the unblock ladder.

    Harbor's own block comment lands on the polled ticket exactly as Linear
    would show it, so the self-trigger path is exercised for real.
    """

    def __init__(self, ticket: dict[str, object]) -> None:
        super().__init__(ticket)
        self.posted: list[str] = []

    async def fetch_blocker_context(self, identifier: str) -> dict[str, object]:
        return {
            "identifier": identifier,
            # Linear returns the issue id; Harbor keys its comment receipts on it.
            "id": self.ticket["id"],
            "description": "Update the schema.",
            "state": {"name": "In Progress", "type": "started"},
            "comments": {"nodes": list(self.ticket.get("linear_comments", []))},
        }

    async def create_comment(self, issue_id: str, body: str) -> dict[str, object]:
        self.posted.append(body)
        comment_id = f"harbor-{len(self.posted)}"
        stamp = f"2026-09-0{2 + len(self.posted)}T12:00:00+00:00"
        own = {"id": comment_id, "body": body, "created_at": stamp, "updated_at": stamp,
               "author": {"id": "owner-1", "name": "Owner"}}
        self.ticket = {**self.ticket, "linear_comments": [*self.ticket.get("linear_comments", []), own]}
        return {"id": comment_id}


def _escalations(db: HarborDB) -> list[tuple[str, str]]:
    return [
        (str(row[0]), str(row[1]))
        for row in db.connection.execute(
            "SELECT kind, COALESCE(status, '') FROM escalations WHERE ticket='SAMPLE-7' ORDER BY id"
        )
    ]


def _supervisor(tmp_path: Path, db: HarborDB, linear: _LinearWithComments, mode: str = "blocked") -> Supervisor:
    async def first_mate(prompt: str, **kwargs: object) -> dict[str, str]:
        return {"disposition": "escalate", "reason": "A human must grant edit permission."}

    ladder = UnblockLadder(db, linear=linear, first_mate=first_mate, repo_path=tmp_path)
    runner = CodexRunner(tmp_path, db=db, ticket="SAMPLE-7", executable=_builder(tmp_path, mode), deadline_seconds=30)
    supervisor = Supervisor(linear, db=db, runner=runner, unblock_ladder=ladder)
    supervisor._background_dispatch = True
    return supervisor


def test_blocked_waiting_on_human_is_not_relaunched_each_poll(tmp_path: Path) -> None:
    comment = _human("human-1", "approve. the agent may update the schema", "2026-09-02T00:00:00+00:00")
    ticket = _ticket([comment])
    db = _db(tmp_path, ticket)
    linear = _LinearWithComments(ticket)
    supervisor = _supervisor(tmp_path, db, linear)
    try:
        async def scenario() -> None:
            # The human comment is genuinely new input: exactly one turn, one
            # escalation, and one Harbor block comment on the ticket.
            await _poll_and_settle(supervisor)
            assert _launches(tmp_path) == 1
            assert _escalations(db) == [("env_blocker", "open")], supervisor.last_error
            assert len(linear.posted) == 1 and linear.posted[0].startswith("[harbor] Blocked SAMPLE-7")

            # Nothing new except Harbor's own block comment: no relaunch and
            # no superseding escalation for the same question.
            for _ in range(5):
                await _poll_and_settle(supervisor)
            assert _launches(tmp_path) == 1
            assert _escalations(db) == [("env_blocker", "open")]

            # A real human reply is new input and resumes the same session once.
            reply = _human("human-2", "Edit permission granted; continue.", "2026-09-09T00:00:00+00:00")
            linear.ticket = {**linear.ticket, "linear_comments": [*linear.ticket["linear_comments"], reply]}
            await _poll_and_settle(supervisor)
            assert _launches(tmp_path) == 2
            for _ in range(3):
                await _poll_and_settle(supervisor)
            assert _launches(tmp_path) == 2

        asyncio.run(scenario())
        launches = [json.loads(line) for line in (tmp_path / "launches.jsonl").read_text().splitlines()]
        assert all(args[:3] == ["exec", "resume", "session-7"] for args in launches)
    finally:
        db.close()


def test_harbor_block_comment_is_not_input_but_a_human_comment_is(tmp_path: Path) -> None:
    ticket = _ticket([])
    db = _db(tmp_path, ticket)
    try:
        body = f"[harbor] Blocked SAMPLE-7 ({QUESTION})"
        own = {"id": "harbor-block-1", "body": body, "created_at": "2026-09-03T00:00:00+00:00",
               "updated_at": "2026-09-03T00:00:00+00:00", "author": {"id": "owner-1", "name": "Owner"}}
        record_comment_receipt(db, "linear-sample-7", comment_id="harbor-block-1", body=body)
        assert WakeDecider(db).decide(_ticket([own]), {}) is None
        human = _human("human-9", "Go ahead.", "2026-09-04T00:00:00+00:00")
        wake = WakeDecider(db).decide(_ticket([own, human]), {})
        assert wake is not None and wake.facts["linear_comment"]["id"] == "human-9"
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["crash", "schema_failed"])
def test_failed_turns_on_the_same_comment_stop_at_the_redelivery_cap(tmp_path: Path, mode: str) -> None:
    comment = _human("human-1", "please continue", "2026-09-02T00:00:00+00:00")
    db = _db(tmp_path, _ticket([comment]))
    linear = _LinearWithComments(_ticket([comment]))
    supervisor = _supervisor(tmp_path, db, linear, mode)
    notices: list[str] = []

    async def notify(message: str) -> None:
        notices.append(message)

    supervisor.notifier = notify
    try:
        async def scenario() -> None:
            for _ in range(6):
                await _poll_and_settle(supervisor)
            # One delivery plus one retry, then Harbor stops offering it.
            assert _launches(tmp_path) == MAX_FAILED_DELIVERIES
            cap = [text for text in notices if REDELIVERY_CAP_REASON in text]
            assert len(cap) == 1 and "/retry SAMPLE-7" in cap[0]

            # A new human comment is new input with its own budget.
            reply = _human("human-2", "try again now", "2026-09-09T00:00:00+00:00")
            linear.ticket = {**linear.ticket, "linear_comments": [*linear.ticket["linear_comments"], reply]}
            for _ in range(4):
                await _poll_and_settle(supervisor)
            assert _launches(tmp_path) == 2 * MAX_FAILED_DELIVERIES

        asyncio.run(scenario())
        rows = db.connection.execute("SELECT status FROM work_queue WHERE action='reengage' ORDER BY id").fetchall()
        assert [row[0] for row in rows] == ["failed"] * (2 * MAX_FAILED_DELIVERIES)
    finally:
        db.close()


def test_one_failed_delivery_is_still_retried_once(tmp_path: Path) -> None:
    comment = _human("human-1", "please continue", "2026-09-02T00:00:00+00:00")
    ticket = _ticket([comment])
    db = _db(tmp_path, ticket)
    try:
        first = WakeDecider(db).decide(ticket, {})
        assert first is not None
        assert db.transition_queue("failed", idem_key=first.idem_key, error="resume refused")
        retry = WakeDecider(db).decide(ticket, {})
        assert retry is not None and retry.idem_key == first.idem_key
        assert db.transition_queue("failed", idem_key=first.idem_key, error="resume refused")
        stop = WakeDecider(db).decide(ticket, {})
        assert stop is not None and stop.action == "stop" and REDELIVERY_CAP_REASON in str(stop.reason)
        assert WakeDecider(db).decide(ticket, {}) is None
    finally:
        db.close()


def test_post_turn_failure_never_fails_an_older_live_queue_row(tmp_path: Path) -> None:
    db = _db(tmp_path, _ticket([]))
    try:
        connection = db.connection
        for key, status in (("SAMPLE-7@reengage@older", "waiting"), ("SAMPLE-7@reengage@newer", "completed")):
            connection.execute(
                "INSERT INTO work_queue(ticket, action, idem_key, status, payload_json) "
                "VALUES ('SAMPLE-7', 'reengage', ?, ?, '{}')",
                (key, status),
            )
        connection.commit()
        supervisor = Supervisor(_Linear(_ticket([])), db=db)
        wake = Wake(ticket={"identifier": "SAMPLE-7"}, action="reengage", resume=RUN_ID,
                    idem_key="SAMPLE-7@reengage@newer")
        # The runner already closed the newer row; handling its result raised.
        supervisor._record_dispatch_failure(wake, "post-turn handling failed")
        statuses = dict(connection.execute("SELECT idem_key, status FROM work_queue").fetchall())
        assert statuses == {"SAMPLE-7@reengage@older": "waiting", "SAMPLE-7@reengage@newer": "completed"}

        # A wake with no receipt of its own still closes the ticket's live row.
        orphan = Wake(ticket={"identifier": "SAMPLE-7"}, action="reengage", resume=RUN_ID, idem_key="")
        supervisor._record_dispatch_failure(orphan, "could not start")
        statuses = dict(connection.execute("SELECT idem_key, status FROM work_queue").fetchall())
        assert statuses["SAMPLE-7@reengage@older"] == "failed"
    finally:
        db.close()
