from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from harbor.db import HarborDB, apply_phase9_migration
from harbor.runner.base import _insert_run, apply_phase4_migration, reap_orphan_runs
from harbor.runner.stream import extract_final, final_payload, parse_line
from harbor.supervisor import Supervisor, WorktreeDispatcher
from harbor.wake import apply_phase3_migration


def _db(tmp_path: Path, ticket: str = "HAN-35") -> HarborDB:
    db = HarborDB(tmp_path / "harbor.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    db.replace_tickets(
        "Bookgen",
        [{"identifier": ticket, "title": ticket, "state": "Todo"}],
        emit_events=False,
    )
    return db


class _Poller:
    project_name = "Bookgen"
    config = SimpleNamespace(poll_interval_seconds=3600.0, hold_all=False)

    async def poll(self) -> Any:
        raise AssertionError("the startup regression does not poll")


def test_shadow_startup_does_not_reap_a_live_run(tmp_path: Path, monkeypatch: Any) -> None:
    """A read-only observer must not kill or rewrite the live console's row."""

    import harbor.supervisor as supervisor_module

    db = _db(tmp_path)
    db.connection.execute(
        "INSERT INTO runs(id, ticket, role, status, pid, pgid) "
        "VALUES ('run-35', 'HAN-35', 'builder', 'running', 123, 123)"
    )
    db.connection.commit()
    calls: list[tuple[Any, ...]] = []

    async def reap(*args: Any, **kwargs: Any) -> list[str]:
        calls.append((args, kwargs))
        return ["run-35"]

    monkeypatch.setattr(supervisor_module, "reap_orphan_runs", reap)

    async def scenario() -> None:
        supervisor = Supervisor(_Poller(), db=db, shadow=True, migrations=[])
        try:
            await supervisor.start(initial_poll=False)
            assert calls == []
            status = db.connection.execute(
                "SELECT status FROM runs WHERE id = 'run-35'"
            ).fetchone()[0]
            assert status == "running"
        finally:
            await supervisor.exit()

    try:
        asyncio.run(scenario())
    finally:
        db.close()


def test_reaper_compare_and_set_preserves_a_newer_result(tmp_path: Path, monkeypatch: Any) -> None:
    """A result written during the termination grace period wins the race."""

    import harbor.runner.base as runner_base

    db = _db(tmp_path)
    db.connection.execute(
        "INSERT INTO runs(id, ticket, role, status, pid, pgid) "
        "VALUES ('run-race', 'HAN-35', 'builder', 'running', 123, 123)"
    )
    db.connection.commit()
    signals: list[int] = []
    transitioned = False

    def process_group_alive(_: int | None) -> bool:
        nonlocal transitioned
        if not transitioned:
            transitioned = True
            db.connection.execute(
                "UPDATE runs SET status = 'waiting', waiting_on = 'human' "
                "WHERE id = 'run-race'"
            )
            db.connection.commit()
        return False

    monkeypatch.setattr(runner_base, "process_alive", lambda _: True)
    monkeypatch.setattr(runner_base, "process_group_alive", process_group_alive)
    monkeypatch.setattr(
        runner_base,
        "signal_process_group",
        lambda pid, pgid, sig=None: signals.append(int(pid)) or True,
    )

    try:
        orphaned = asyncio.run(reap_orphan_runs(db, grace_seconds=0.05))
        row = db.connection.execute(
            "SELECT status, waiting_on, terminal_reason FROM runs WHERE id = 'run-race'"
        ).fetchone()
    finally:
        db.close()

    assert orphaned == []
    assert signals == [123]
    assert tuple(row) == ("waiting", "human", None)


def test_resume_session_uses_dispatcher_running_guard(tmp_path: Path) -> None:
    """An operator answer cannot launch a second process for a running run."""

    db = _db(tmp_path)
    db.connection.execute(
        "INSERT INTO runs(id, ticket, role, status, worktree) "
        "VALUES ('run-answer', 'HAN-35', 'builder', 'running', ?)",
        (str(tmp_path),),
    )
    db.connection.commit()
    resumed: list[tuple[str, str]] = []

    class Runner:
        default_run_id = "run-answer"

        async def resume(self, run_id: str, instruction: str) -> dict[str, str]:
            resumed.append((run_id, instruction))
            return {"status": "waiting", "summary": "resumed"}

    dispatcher = WorktreeDispatcher(object(), lambda *_: Runner(), db=db, project="Bookgen")
    dispatcher.runners["HAN-35"] = Runner()
    supervisor = Supervisor(_Poller(), db=db, dispatcher=dispatcher, shadow=True)

    try:
        result = asyncio.run(supervisor.resume_session("run-answer", "Operator answer"))
    finally:
        db.close()

    assert result is False
    assert resumed == []


def test_resume_upsert_preserves_durable_run_fields(tmp_path: Path) -> None:
    """Reopening one logical run updates lifecycle state without replacing evidence."""

    db = _db(tmp_path)
    db.connection.execute(
        "INSERT INTO runs(id, ticket, role, status, final_json, started_at, finished_at, "
        "commit_sha, artifact_json) VALUES (?, ?, 'builder', 'waiting', ?, ?, ?, ?, ?)",
        (
            "run-upsert",
            "HAN-35",
            '{"status":"waiting","summary":"previous result"}',
            "2026-09-04T07:00:00+00:00",
            "2026-09-04T07:10:00+00:00",
            "old-sha",
            '{"proof":"retained"}',
        ),
    )
    db.connection.commit()
    _insert_run(
        db,
        {
            "id": "run-upsert",
            "ticket": "HAN-35",
            "role": "builder",
            "status": "running",
            "waiting_on": None,
            "pid": 456,
            "pgid": 456,
            "session_id": "session-35",
            "started_at": "2026-09-04T07:20:00+00:00",
        },
    )

    try:
        row = db.connection.execute(
            "SELECT count(*), status, final_json, started_at, finished_at, commit_sha, artifact_json "
            "FROM runs WHERE id = 'run-upsert'"
        ).fetchone()
    finally:
        db.close()

    assert tuple(row) == (
        1,
        "running",
        '{"status":"waiting","summary":"previous result"}',
        "2026-09-04T07:00:00+00:00",
        None,
        "old-sha",
        '{"proof":"retained"}',
    )


def test_progress_json_is_not_final_without_terminal_codex_event() -> None:
    progress = {"status": "waiting", "summary": "still verifying"}
    progress_event = parse_line(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "content": [{"type": "Text", "text": json.dumps(progress)}],
                    },
                },
            }
        )
    )
    assert progress_event is not None
    assert final_payload(progress_event) is None
    assert extract_final([progress_event]) is None

    final = {"status": "waiting", "summary": "turn finished"}
    terminal_event = parse_line(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": json.dumps(final),
                },
            }
        )
    )
    assert terminal_event is not None
    assert final_payload(terminal_event) == final
    assert extract_final([progress_event, terminal_event]) == final
