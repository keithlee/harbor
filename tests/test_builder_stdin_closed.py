"""A builder that stops reading stdin before Harbor writes its prompt.

Harbor writes the instruction to the builder's stdin right after spawning it.
If the builder has already exited, or has closed its stdin, that write fails
with a broken pipe.  The run must still be recorded as a failed run carrying
the builder's real exit status and output, never raise out of the runner.

Both cases are forced deterministically, without timing sleeps:

* ``exited``: the spawn is held until the child process has exited, so the
  stdin pipe is already closed when Harbor writes (the order seen in CI).
* ``stdin_closed``: the spawn is held until the child reports, through a FIFO,
  that it has closed its stdin.  The child is still running when Harbor
  writes, then prints more output and exits.
"""

import asyncio
import os
import sys

import pytest

from harbor.runner.codex import CodexRunner
from test_runner_phase4 import runs_db

EXIT_STATUS = 3


def builder(tmp_path, mode):
    fifo = tmp_path / "stdin-closed.fifo"
    path = tmp_path / "builder.py"
    path.write_text(f'''#!{sys.executable}
import json, os, sys
os.close(0)  # never read the prompt
if {mode!r} == "stdin_closed":
    with open({str(fifo)!r}, "w") as signal:
        signal.write("closed")
print(json.dumps({{"type": "thread.started", "thread_id": "early-exit-session"}}), flush=True)
print("builder failed before reading its prompt", file=sys.stderr, flush=True)
raise SystemExit({EXIT_STATUS})
''')
    path.chmod(0o755)
    if mode == "stdin_closed":
        os.mkfifo(fifo)
    return str(path), fifo


@pytest.mark.parametrize("mode", ["exited", "stdin_closed"])
def test_builder_that_stops_reading_stdin_is_a_recorded_failed_run(tmp_path, monkeypatch, mode):
    executable, fifo = builder(tmp_path, mode)
    real_spawn = asyncio.create_subprocess_exec

    async def spawn_then_wait_for_child(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        if mode == "exited":
            await asyncio.wait_for(process.wait(), timeout=30)
        else:
            # Blocks until the child has closed stdin and opened the FIFO.
            with open(fifo) as signal:
                assert signal.read() == "closed"
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_then_wait_for_child)

    async def scenario():
        db = runs_db()
        runner = CodexRunner(tmp_path, db=db, ticket="DEM-1", model="primary", executable=executable)
        result = await runner.start("a prompt the builder never reads", run_id="run")

        assert result.status == "failed"
        assert result.exit_code == EXIT_STATUS
        assert result.session_id == "early-exit-session"
        assert "schema-valid final result" in (result.error or "")
        raw = [event.raw for event in runner.contexts["run"].events]
        assert any("builder failed before reading its prompt" in line for line in raw)

        row = dict(db.execute("SELECT * FROM runs WHERE id = 'run'").fetchone())
        assert row["status"] == "failed"
        assert row["terminal_reason"] == "unexpected_exit"
        assert row["finished_at"]
        assert runner.active is None
        db.close()

    asyncio.run(scenario())


def test_other_stdin_write_errors_still_raise(tmp_path, monkeypatch):
    """Only a closed pipe is the builder's failure; other write errors are Harbor's."""

    executable, _ = builder(tmp_path, "exited")
    real_spawn = asyncio.create_subprocess_exec
    spawned = []

    async def spawn_with_failing_stdin(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        spawned.append(process)

        async def broken_drain():
            raise RuntimeError("unexpected stdin failure")

        process.stdin.drain = broken_drain
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_with_failing_stdin)

    async def scenario():
        db = runs_db()
        runner = CodexRunner(tmp_path, db=db, ticket="DEM-1", model="primary", executable=executable)
        with pytest.raises(RuntimeError, match="unexpected stdin failure"):
            await runner.start("prompt", run_id="run")
        await asyncio.wait_for(spawned[0].wait(), timeout=30)
        db.close()

    asyncio.run(scenario())
