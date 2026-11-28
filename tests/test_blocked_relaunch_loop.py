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

from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB
from harbor.poll.linear_poller import PollResult
from harbor.runner.base import apply_phase4_migration
from harbor.runner.codex import CodexRunner
from harbor.supervisor import Supervisor
from harbor.wake import WakeDecider, apply_phase3_migration, record_comment_receipt

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


def _blocked_builder(tmp_path: Path) -> str:
    """A fake provider CLI that always parks on the same human blocker."""

    path = tmp_path / "builder.py"
    final = {
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
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "with (pathlib.Path.cwd() / 'launches.jsonl').open('a') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'session-7'}), flush=True)\n"
        f"print(json.dumps({{'type': 'result', 'result': {final!r}}}), flush=True)\n"
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
        runner = CodexRunner(tmp_path, db=db, ticket="SAMPLE-7", executable=_blocked_builder(tmp_path), deadline_seconds=30)
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


def test_blocked_waiting_on_human_is_not_relaunched_each_poll(tmp_path: Path) -> None:
    comment = _human("human-1", "approve. the agent may update the schema", "2026-09-02T00:00:00+00:00")
    ticket = _ticket([comment])
    db = _db(tmp_path, ticket)
    linear = _Linear(ticket)
    runner = CodexRunner(tmp_path, db=db, ticket="SAMPLE-7", executable=_blocked_builder(tmp_path), deadline_seconds=30)
    supervisor = Supervisor(linear, db=db, runner=runner)
    supervisor._background_dispatch = True
    try:
        async def scenario() -> None:
            # The human comment is genuinely new input: exactly one turn.
            await _poll_and_settle(supervisor)
            assert _launches(tmp_path) == 1

            # Nothing new: the same comment, the same blocker.  No relaunch.
            for _ in range(4):
                await _poll_and_settle(supervisor)
            assert _launches(tmp_path) == 1

            # Harbor's own block comment is not new input either.
            own = {
                "id": "harbor-block-1",
                "body": f"[harbor] Blocked SAMPLE-7 ({QUESTION})",
                "created_at": "2026-09-03T00:00:00+00:00",
                "updated_at": "2026-09-03T00:00:00+00:00",
                "author": {"id": "owner-1", "name": "Owner"},
            }
            record_comment_receipt(db, "linear-sample-7", comment_id="harbor-block-1", body=str(own["body"]))
            linear.ticket = _ticket([comment, own])
            for _ in range(3):
                await _poll_and_settle(supervisor)
            assert _launches(tmp_path) == 1

            # A real human reply is new input and resumes the same session once.
            reply = _human("human-2", "Edit permission granted; continue.", "2026-09-04T00:00:00+00:00")
            linear.ticket = _ticket([comment, own, reply])
            await _poll_and_settle(supervisor)
            assert _launches(tmp_path) == 2
            for _ in range(3):
                await _poll_and_settle(supervisor)
            assert _launches(tmp_path) == 2

        asyncio.run(scenario())
        launches = [json.loads(line) for line in (tmp_path / "launches.jsonl").read_text().splitlines()]
        assert all(args[:3] == ["exec", "resume", "session-7"] for args in launches)
        superseded = db.connection.execute(
            "SELECT COUNT(*) FROM escalations WHERE ticket='SAMPLE-7' AND status='superseded'"
        ).fetchone()[0]
        # At most the one escalation from the first turn was replaced by the
        # turn the human reply started; no superseding chain without new input.
        assert superseded <= 1
    finally:
        db.close()
