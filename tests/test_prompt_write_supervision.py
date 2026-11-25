"""The builder stays supervised while Harbor writes its prompt.

Harbor writes the instruction to the builder's stdin.  A prompt larger than the
pipe buffer only drains as fast as the builder reads it, so the write itself
can stall.  While it does:

* the builder's output must already be read, or a builder that writes more
  than a pipe buffer of output before reading stdin deadlocks with Harbor;
* the run's deadline must already be watched, or a builder that holds stdin
  open without reading hangs Harbor forever;
* an operator exit (cancellation) or any unexpected error must stop the
  builder, record the run, and release ``runner.active``.

Every builder here is forced into its stalled state by construction (a prompt
far larger than any pipe buffer and a builder that does not read it), never by
timing sleeps.  ``asyncio.wait_for`` bounds only how long a regression may hang
before it fails.
"""

import asyncio
import contextlib
import os
import signal
import sys

import pytest

from harbor.runner.base import STREAM_BUFFER_LIMIT, process_group_alive
from harbor.runner.codex import CodexRunner
from test_runner_phase4 import runs_db

# Far larger than any platform's pipe buffer (64 KiB on Linux and macOS).
LARGE_PROMPT = "p" * (4 * 1024 * 1024)
# asyncio keeps reading a subprocess's stdout into the StreamReader buffer until
# it holds twice the stream limit, so the builder must write more than that
# (plus a pipe buffer) before Harbor's reader is the only thing draining it.
LARGE_OUTPUT_LINES = (2 * STREAM_BUFFER_LIMIT) // (1024 * 1024) + 8  # lines of ~1 MiB
HANG_BOUND = 30


def write_builder(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return str(path)


def chatty_builder(tmp_path):
    """Writes more output than asyncio will buffer before it reads the prompt."""

    return write_builder(tmp_path, "chatty.py", f'''import json, sys
print(json.dumps({{"type": "thread.started", "thread_id": "chatty-session"}}), flush=True)
line = json.dumps({{"type": "heartbeat", "pad": "x" * (1024 * 1024)}})
for _ in range({LARGE_OUTPUT_LINES}):
    sys.stdout.write(line + "\\n")
sys.stdout.flush()
prompt = sys.stdin.read()
print(json.dumps({{"type": "prompt.read", "length": len(prompt)}}), flush=True)
print(json.dumps({{"type": "result", "result": {{"status": "completed", "outcome": "pr_open", "waiting_on": "none", "commit_sha": "c0ffee1", "summary": "done"}}}}), flush=True)
''')


def stalled_builder(tmp_path):
    """Holds stdin open and never reads it, and never exits on its own."""

    return write_builder(tmp_path, "stalled.py", '''import json, signal
print(json.dumps({"type": "thread.started", "thread_id": "stalled-session"}), flush=True)
while True:
    signal.pause()
''')


@pytest.fixture
def spawned(monkeypatch):
    """Record every builder Harbor spawns; kill any a regression leaks."""

    processes = []
    entered_drain = asyncio.Event()
    real_spawn = asyncio.create_subprocess_exec

    async def recording_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        real_drain = process.stdin.drain

        async def drain():
            entered_drain.set()
            await real_drain()

        process.stdin.drain = drain
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_spawn)
    yield processes, entered_drain
    for process in processes:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)


def assert_builder_stopped(process):
    assert process.returncode is not None, "builder is still running unsupervised"
    assert not process_group_alive(process.pid)


def test_builder_that_writes_before_reading_its_prompt_does_not_deadlock(tmp_path, spawned):
    processes, _ = spawned

    async def scenario():
        db = runs_db()
        runner = CodexRunner(tmp_path, db=db, ticket="DEM-1", model="primary", executable=chatty_builder(tmp_path))
        result = await asyncio.wait_for(runner.start(LARGE_PROMPT, run_id="run"), HANG_BOUND)

        assert result.status == "completed", result.error
        assert result.exit_code == 0
        read = [event.data for event in runner.contexts["run"].events if (event.data or {}).get("type") == "prompt.read"]
        assert read == [{"type": "prompt.read", "length": len(LARGE_PROMPT)}]
        assert runner.active is None
        db.close()

    asyncio.run(scenario())
    assert_builder_stopped(processes[0])


def test_deadline_bounds_a_prompt_write_the_builder_never_reads(tmp_path, spawned):
    processes, _ = spawned

    async def scenario():
        db = runs_db()
        runner = CodexRunner(
            tmp_path, db=db, ticket="DEM-1", model="primary",
            executable=stalled_builder(tmp_path), deadline_seconds=0.5,
        )
        result = await asyncio.wait_for(runner.start(LARGE_PROMPT, run_id="run"), HANG_BOUND)

        assert result.status == "failed"
        assert result.error == "deadline exceeded"
        assert result.session_id == "stalled-session"
        row = dict(db.execute("SELECT * FROM runs WHERE id = 'run'").fetchone())
        assert row["status"] == "failed"
        assert row["terminal_reason"] == "deadline exceeded"
        assert row["finished_at"]
        assert runner.active is None
        db.close()

    asyncio.run(scenario())
    assert_builder_stopped(processes[0])


def test_operator_exit_during_a_stalled_prompt_write_stops_the_builder(tmp_path, spawned):
    processes, entered_drain = spawned

    async def scenario():
        db = runs_db()
        runner = CodexRunner(tmp_path, db=db, ticket="DEM-1", model="primary", executable=stalled_builder(tmp_path))
        task = asyncio.create_task(runner.start(LARGE_PROMPT, run_id="run"))
        # The builder never reads stdin and the prompt exceeds the pipe
        # buffer, so once drain() is entered the write is stalled.
        await asyncio.wait_for(entered_drain.wait(), HANG_BOUND)
        assert not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, HANG_BOUND)

        assert runner.active is None
        assert runner.contexts["run"].process is None
        row = dict(db.execute("SELECT * FROM runs WHERE id = 'run'").fetchone())
        assert row["status"] == "terminated"
        assert row["terminal_reason"] == "operator exit"
        assert row["finished_at"]
        db.close()

    asyncio.run(scenario())
    assert_builder_stopped(processes[0])


def test_unexpected_error_during_the_prompt_write_stops_the_builder(tmp_path, monkeypatch):
    real_spawn = asyncio.create_subprocess_exec
    processes = []

    async def spawn_with_failing_stdin(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)

        async def broken_drain():
            raise RuntimeError("unexpected stdin failure")

        process.stdin.drain = broken_drain
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_with_failing_stdin)

    async def scenario():
        db = runs_db()
        runner = CodexRunner(tmp_path, db=db, ticket="DEM-1", model="primary", executable=stalled_builder(tmp_path))
        with pytest.raises(RuntimeError, match="unexpected stdin failure"):
            await asyncio.wait_for(runner.start("prompt", run_id="run"), HANG_BOUND)

        assert runner.active is None
        row = dict(db.execute("SELECT * FROM runs WHERE id = 'run'").fetchone())
        assert row["status"] == "terminated"
        assert row["terminal_reason"] == "harbor error: RuntimeError"
        assert row["finished_at"]
        db.close()

    try:
        asyncio.run(scenario())
        assert_builder_stopped(processes[0])
    finally:
        for process in processes:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
