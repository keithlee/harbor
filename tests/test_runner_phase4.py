from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import stat
import sys
import time
from pathlib import Path

import pytest

from harbor.runner import (
    CodexRunner,
    Deadline,
    RunState,
    apply_phase4_migration,
    parse_line,
    process_alive,
    reap_orphan_runs,
    validate_result,
)
from harbor.runner.stream import extract_final


def fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "fake-codex.py"
    script.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys, time
root = pathlib.Path(os.environ['HARBOR_WORKTREE'])
(root / '.fake-cwd').write_text(os.getcwd())
args = sys.argv[1:]
(root / '.fake-args').write_text(json.dumps(args))
print(json.dumps({'type': 'session.started', 'session_id': 'session-123'}), flush=True)
if 'resume' not in args:
    print(json.dumps({'type': 'result', 'result': {'status': 'waiting', 'outcome': 'pr_open', 'waiting_on': 'review', 'pr_url': 'https://example.test/pr/1', 'summary': 'PR is open'}}), flush=True)
else:
    print(json.dumps({'type': 'result', 'result': {'status': 'completed', 'outcome': 'merged', 'waiting_on': 'none', 'summary': 'Merged'}}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def slow_codex(tmp_path: Path, *, spawn_child: bool = False) -> Path:
    script = tmp_path / ("slow-child.py" if spawn_child else "slow.py")
    child = "child = __import__('subprocess').Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n(root / '.child-pid').write_text(str(child.pid))\n" if spawn_child else ""
    script.write_text(
        f"""#!/usr/bin/env python3
import json, os, pathlib, sys, time
root = pathlib.Path(os.environ['HARBOR_WORKTREE'])
{child}print(json.dumps({{'type': 'heartbeat'}}), flush=True)
time.sleep(0.25)
print(json.dumps({{'type': 'result', 'result': {{'status': 'completed', 'outcome': 'pr_open', 'waiting_on': 'none', 'commit_sha': 'c0ffee1', 'summary': 'done'}}}}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def paused_codex(tmp_path: Path) -> Path:
    script = tmp_path / "paused.py"
    script.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, time
root = pathlib.Path(os.environ['HARBOR_WORKTREE'])
print(json.dumps({'type': 'heartbeat'}), flush=True)
while not (root / '.release').exists():
    time.sleep(0.01)
print(json.dumps({'type': 'result', 'result': {'status': 'completed', 'outcome': 'pr_open', 'waiting_on': 'none', 'commit_sha': 'c0ffee1', 'summary': 'done'}}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def runs_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, ticket TEXT, role TEXT, worktree TEXT, pid INTEGER, pgid INTEGER, status TEXT, waiting_on TEXT, deadline_at TEXT, cost_usd REAL, final_json TEXT, started_at TEXT, finished_at TEXT)")
    connection.commit()
    apply_phase4_migration(connection)
    return connection


def test_stream_liveness_parser_keeps_bad_lines_and_valid_final() -> None:
    malformed = parse_line(b"not json\n")
    assert malformed is not None and malformed.parse_error
    assert validate_result({"status": "waiting", "summary": "idle"}) == []
    assert validate_result({"status": "wat", "summary": "bad"})


def test_codex_start_resume_schema_and_persistent_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        shared_git = tmp_path / "shared-git"
        shared_git.mkdir()
        runner = CodexRunner(
            worktree,
            ticket="SAMPLE-42",
            executable=str(fake_codex(tmp_path)),
            db=runs_db(),
            deadline_seconds=5,
            writable_roots=(shared_git,),
            network_access=True,
        )
        first = await runner.start("build", run_id="run-1")
        assert first.status == "waiting"
        assert first.outcome == "pr_open"
        assert first.pr_url == "https://example.test/pr/1"
        assert runner.contexts["run-1"].session_id == "session-123"
        second = await runner.resume("run-1", "continue")
        assert second.status == "completed"
        assert second.outcome == "merged"
        args = json.loads((worktree / ".fake-args").read_text(encoding="utf-8"))
        assert "model_reasoning_effort=xhigh" in args
        assert "session-123" in args
        assert f"sandbox_workspace_write.writable_roots={json.dumps([str(shared_git)])}" in args
        assert "sandbox_workspace_write.network_access=true" in args
        assert "-C" not in args
        assert "-s" not in args
        assert (worktree / ".fake-cwd").read_text(encoding="utf-8") == str(worktree)

    asyncio.run(scenario())


def test_event_stream_touches_activity_without_agent_calls(tmp_path: Path) -> None:
    class Store:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection
            self.touches = 0

        def touch_activity(self, run_id: str, event: object) -> None:
            del run_id, event
            self.touches += 1

    async def scenario() -> None:
        connection = runs_db()
        store = Store(connection)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        runner = CodexRunner(worktree, ticket="SAMPLE-42", executable=str(slow_codex(tmp_path)), db=store, deadline_seconds=5)
        result = await runner.start("build")
        assert result.status == "completed"
        assert store.touches >= 2

    asyncio.run(scenario())


def test_pause_suspends_process_deadline(tmp_path: Path) -> None:
    async def scenario() -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        runner = CodexRunner(worktree, executable=str(paused_codex(tmp_path)), deadline_seconds=0.1)
        task = asyncio.create_task(runner.start("build"))
        for _ in range(100):
            if runner.active is not None:
                break
            await asyncio.sleep(0.005)
        await runner.pause()
        await asyncio.sleep(0.25)
        assert not task.done()
        (worktree / ".release").touch()
        await runner.resume_deadline()
        result = await task
        assert result.status == "completed"

    asyncio.run(scenario())


def test_termination_kills_process_group_and_child(tmp_path: Path) -> None:
    async def scenario() -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        runner = CodexRunner(worktree, executable=str(slow_codex(tmp_path, spawn_child=True)), deadline_seconds=5)
        task = asyncio.create_task(runner.start("build"))
        child_file = worktree / ".child-pid"
        for _ in range(100):
            if runner.active is not None and child_file.exists():
                break
            await asyncio.sleep(0.01)
        assert child_file.exists()
        child_pid = int(child_file.read_text(encoding="utf-8"))
        await runner.terminate(reason="test kill")
        result = await task
        assert result.status == RunState.TERMINATED.value
        await asyncio.sleep(0.05)
        assert not process_alive(child_pid)

    asyncio.run(scenario())


def test_deadline_suspends_while_paused() -> None:
    now = [100.0]
    deadline = Deadline(10.0, started_at=now[0], clock=lambda: now[0])
    now[0] = 108.0
    deadline.pause()
    now[0] = 1008.0
    assert not deadline.expired()
    assert deadline.remaining() == pytest.approx(2.0)
    deadline.resume()
    now[0] = 1010.0
    assert deadline.expired()


def test_restart_reaps_tracked_run() -> None:
    connection = runs_db()
    connection.execute("INSERT INTO runs(id, ticket, role, status, pid, pgid) VALUES ('run-1', 'SAMPLE-42', 'builder', 'running', 999999, 999999)")
    connection.commit()
    orphaned = asyncio.run(reap_orphan_runs(connection))
    assert orphaned == ["run-1"]
    assert connection.execute("SELECT status FROM runs WHERE id='run-1'").fetchone()[0] == RunState.ORPHANED.value


def test_an_oversized_stream_line_does_not_kill_the_supervisor() -> None:
    """A large Codex event must not crash the whole process.

    `StreamReader.readline` raises ValueError past its buffer limit, and that
    exception propagates out of the poll loop and orphans a live builder.
    Recorded fixtures all emit tiny lines, so only a real run surfaces this.
    """

    from harbor.runner.stream import iter_stream

    async def scenario() -> None:
        reader = asyncio.StreamReader(limit=1024)  # small limit forces the overrun
        huge = "x" * 4096
        reader.feed_data(json.dumps({"type": "heartbeat", "note": huge}).encode() + b"\n")
        reader.feed_data(json.dumps({"type": "result", "result": {
            "status": "completed", "outcome": "pr_open", "waiting_on": "none",
            "commit_sha": "c0ffee1", "summary": "done"}}).encode() + b"\n")
        reader.feed_eof()

        events = [event async for event in iter_stream(reader)]
        # The oversized line is surfaced as a parse error rather than killing
        # the process, and the stream stays aligned so the final result that
        # follows it is still parsed.
        assert len(events) == 2
        assert events[0].parse_error and "oversized" in events[0].parse_error
        final = extract_final(events)
        assert final is not None and final["status"] == "completed"

    asyncio.run(scenario())


def test_the_runner_raises_the_subprocess_stream_buffer() -> None:
    from harbor.runner.base import STREAM_BUFFER_LIMIT

    assert STREAM_BUFFER_LIMIT >= 1024 * 1024, "64 KiB default is far too small for Codex events"


def test_restart_terminates_a_still_live_process_group() -> None:
    """AC-7, the load-bearing half: a restart must not leave the old builder running.

    The previous coverage reaped a row whose pid was already dead, which
    never exercises the termination path. A real Harbor restart finds a LIVE
    process it cannot claim, and leaving it alive is the duplicate-dispatch
    window the reaper exists to close.
    """

    import os
    import signal as signal_module
    import subprocess

    from harbor.runner.base import process_alive

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        assert process_alive(child.pid), "the stand-in builder must actually be running"
        pgid = os.getpgid(child.pid)

        connection = runs_db()
        connection.execute(
            "INSERT INTO runs(id, ticket, role, status, pid, pgid) VALUES (?, ?, ?, ?, ?, ?)",
            ("run-live", "SAMPLE-42", "builder", "running", child.pid, pgid),
        )
        connection.commit()

        orphaned = asyncio.run(reap_orphan_runs(connection, grace_seconds=2.0))
        assert orphaned == ["run-live"]

        for _ in range(100):
            if child.poll() is not None:
                break
            time.sleep(0.05)
        assert child.poll() is not None, "the reaper must terminate the live process group"
        assert connection.execute(
            "SELECT status FROM runs WHERE id='run-live'"
        ).fetchone()[0] == RunState.ORPHANED.value
    finally:
        if child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(child.pid), signal_module.SIGKILL)
            child.wait(timeout=5)


def test_an_orphaned_run_does_not_cause_a_second_dispatch() -> None:
    """AC-7's other half: reaping must not re-open the ticket for a fresh build."""

    from harbor.wake import WakeDecider, apply_phase3_migration

    connection = runs_db()
    connection.execute(
        "CREATE TABLE tickets (identifier TEXT PRIMARY KEY, owned INTEGER DEFAULT 0, "
        "hold INTEGER DEFAULT 1, bot_rework_rounds INTEGER DEFAULT 0)"
    )
    connection.execute("INSERT INTO tickets(identifier, owned) VALUES ('SAMPLE-42', 1)")
    connection.execute(
        "INSERT INTO runs(id, ticket, role, status, pid, pgid) VALUES "
        "('run-1', 'SAMPLE-42', 'builder', 'running', 999999, 999999)"
    )
    connection.commit()
    apply_phase3_migration(connection)

    asyncio.run(reap_orphan_runs(connection))
    assert connection.execute("SELECT status FROM runs WHERE id='run-1'").fetchone()[0] == RunState.ORPHANED.value

    # An orphaned run is finished, not waiting: it must not be resumed, and an
    # owned ticket must not be rebuilt from scratch behind the operator's back.
    decider = WakeDecider(connection, shadow=True)
    wake = decider.decide(
        {"identifier": "SAMPLE-42", "state": "In Progress", "owned": True},
        {"prs": [{"number": 1, "head_sha": "abc", "merged": False}], "head_sha": "abc", "threads": []},
    )
    assert wake is None, f"an orphaned run must not produce a second dispatch, got {wake}"
