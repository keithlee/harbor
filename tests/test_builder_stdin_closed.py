"""A builder that stops reading stdin before Harbor writes its prompt.

Harbor writes the instruction to the builder's stdin right after spawning it.
If the builder has already exited, or has closed its stdin, that write fails
with a broken pipe.  The run must still be recorded as a failed run carrying
the builder's real exit status and output, never raise out of the runner.

Both cases are forced deterministically, without timing sleeps:

* ``exited``: the spawn is held until the child process has exited, so the
  stdin pipe is already closed when Harbor writes (the order seen in CI).
* ``stdin_closed``: the spawn is held until the child reports, through an
  inherited pipe, that it has closed its stdin.  The child then blocks on a
  second inherited pipe that the test releases only after Harbor has written
  the prompt, so the child is provably still running when Harbor writes; it
  then prints more output and exits.

The test never blocks the event loop: it waits for the child's report with a
bounded ``wait_for`` on a pipe, and a child that dies without reporting closes
that pipe, so the wait fails cleanly instead of hanging.
"""

import asyncio
import os
import sys

import pytest

from harbor.runner.codex import CodexRunner
from test_runner_phase4 import runs_db

EXIT_STATUS = 3
SIGNAL_BOUND = 30


class ChildPipes:
    """Pipes the ``stdin_closed`` child inherits: one to report, one to wait on."""

    def __init__(self):
        self.report_r, self.report_w = os.pipe()
        self.release_r, self.release_w = os.pipe()

    @property
    def child_fds(self):
        return (self.report_w, self.release_r)

    def close_child_ends(self):
        for name in ("report_w", "release_r"):
            self._close(name)

    def release(self):
        self._close("release_w")

    def close(self):
        for name in ("report_r", "report_w", "release_r", "release_w"):
            self._close(name)

    def _close(self, name):
        fd = getattr(self, name)
        if fd is not None:
            setattr(self, name, None)
            os.close(fd)

    async def report(self):
        """The child's report, or ``b""`` if it died without sending one."""

        loop = asyncio.get_running_loop()
        readable = loop.create_future()
        loop.add_reader(self.report_r, lambda: readable.done() or readable.set_result(None))
        try:
            await asyncio.wait_for(readable, SIGNAL_BOUND)
        finally:
            loop.remove_reader(self.report_r)
        return os.read(self.report_r, 64)


def builder(tmp_path, mode, pipes=None):
    path = tmp_path / "builder.py"
    report_w, release_r = pipes.child_fds if pipes else (-1, -1)
    path.write_text(f'''#!{sys.executable}
import json, os, sys
os.close(0)  # never read the prompt
if {mode!r} == "stdin_closed":
    os.write({report_w}, b"closed")
    os.close({report_w})
    os.read({release_r}, 1)  # held here until Harbor has written the prompt
print(json.dumps({{"type": "thread.started", "thread_id": "early-exit-session"}}), flush=True)
print("builder failed before reading its prompt", file=sys.stderr, flush=True)
raise SystemExit({EXIT_STATUS})
''')
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize("mode", ["exited", "stdin_closed"])
def test_builder_that_stops_reading_stdin_is_a_recorded_failed_run(tmp_path, monkeypatch, mode):
    pipes = ChildPipes() if mode == "stdin_closed" else None
    executable = builder(tmp_path, mode, pipes)
    real_spawn = asyncio.create_subprocess_exec
    alive_at_write = []

    async def spawn_then_wait_for_child(*args, **kwargs):
        if pipes is None:
            process = await real_spawn(*args, **kwargs)
            await asyncio.wait_for(process.wait(), timeout=SIGNAL_BOUND)
            return process
        process = await real_spawn(*args, pass_fds=pipes.child_fds, **kwargs)
        pipes.close_child_ends()
        # pytest.fail, not assert: the runner reports an Exception raised by
        # the spawn as a failed run, which would hide why the test failed.
        try:
            report = await pipes.report()
        except asyncio.TimeoutError:
            pytest.fail(f"builder did not report within {SIGNAL_BOUND}s")
        if report != b"closed":
            pytest.fail("builder died before closing its stdin")
        real_write = process.stdin.write

        def write_then_release(data):
            # The child is blocked on the release pipe, so it is running.
            alive_at_write.append(process.returncode is None)
            try:
                real_write(data)
            finally:
                pipes.release()

        process.stdin.write = write_then_release
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
        if mode == "stdin_closed":
            assert alive_at_write == [True]
        db.close()

    try:
        asyncio.run(scenario())
    finally:
        if pipes is not None:
            pipes.close()


def test_other_stdin_write_errors_still_raise(tmp_path, monkeypatch):
    """Only a closed pipe is the builder's failure; other write errors are Harbor's."""

    executable = builder(tmp_path, "exited")
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
