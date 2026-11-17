from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from harbor.config.projects import ProjectConfig
from harbor.console import Console
from harbor.db import HarborDB
from harbor.poll.linear_poller import PollResult
from harbor.supervisor import Lifecycle, Supervisor, Wake, WorktreeDispatcher
from harbor.wake import WakeDecider
from harbor.worktree import DiskFloor


class BlockingTodoPoller:
    project_name = "SampleProject"

    def __init__(self) -> None:
        self.config = ProjectConfig(
            name="SampleProject",
            linear_project_slug="sample-project",
            poll_interval_seconds=60,
            heartbeat_seconds=0.001,
        )
        self.calls = 0

    async def poll(self) -> PollResult:
        self.calls += 1
        ticket = {
            "identifier": "SAMPLE-async",
            "title": "A long-running test ticket",
            "state": "Todo",
            "pr_urls": [],
        }
        return PollResult((ticket,), f"snapshot-{self.calls}", True, (ticket,))


class BlockingDispatcher:
    def __init__(self, callback_present: Any) -> None:
        self.callback_present = callback_present
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.callback_seen_before_work = False
        self.terminate_calls = 0

    async def dispatch(self, wake: Any) -> None:
        self.calls += 1
        self.callback_seen_before_work = bool(self.callback_present())
        self.started.set()
        await self.release.wait()

    async def terminate(self, *, reason: str = "") -> None:
        del reason
        self.terminate_calls += 1
        self.release.set()


def test_console_is_available_while_initial_dispatch_and_heartbeat_run() -> None:
    async def scenario() -> tuple[list[str], int, bool, Lifecycle]:
        poller = BlockingTodoPoller()
        # The dispatcher gets a probe instead of reaching into Console.  This
        # proves the heartbeat callback was installed before the first wake.
        dispatcher = BlockingDispatcher(lambda: supervisor.on_heartbeat is not None)
        supervisor = Supervisor(poller, dispatcher=dispatcher)
        output: list[str] = []
        console = Console(supervisor, output=output.append)
        try:
            await supervisor.start()
            assert supervisor.lifecycle == Lifecycle.RUNNING
            await asyncio.wait_for(dispatcher.started.wait(), timeout=1)

            # The runner is deliberately blocked.  A same-terminal status
            # query must still complete while it is in flight.
            response = await console.handle_command("/status")
            assert "SAMPLE-async" in response

            # A second poll sees the same Todo ticket but must not start a
            # second builder while the first dispatch is active.
            await supervisor.poll_once()
            assert dispatcher.calls == 1

            # The configurable heartbeat loop is active independently of the
            # runner.  Give it one scheduling turn at its 1ms interval.
            await asyncio.sleep(0.01)
            dispatcher.release.set()
            await asyncio.sleep(0)
            return output, dispatcher.calls, dispatcher.callback_seen_before_work, supervisor.lifecycle
        finally:
            dispatcher.release.set()
            await supervisor.exit()

    output, calls, callback_seen, lifecycle = asyncio.run(scenario())
    assert calls == 1
    assert callback_seen is True
    assert lifecycle == Lifecycle.RUNNING
    assert any(line.startswith("Harbor: heartbeat") for line in output)


def test_exit_terminates_and_drains_a_blocked_dispatch_once() -> None:
    async def scenario() -> tuple[str | None, int, int, Lifecycle]:
        poller = BlockingTodoPoller()
        supervisor: Supervisor
        dispatcher = BlockingDispatcher(lambda: supervisor.on_heartbeat is not None)
        supervisor = Supervisor(poller, dispatcher=dispatcher)
        console = Console(supervisor)
        await supervisor.start()
        await asyncio.wait_for(dispatcher.started.wait(), timeout=1)

        response = await console.handle_command("/exit")
        pending = sum(
            not task.done()
            for task in supervisor._dispatch_tasks.values()  # type: ignore[attr-defined]
        )
        # A repeated exit is idempotent and must not terminate twice.
        await supervisor.exit()
        return response, dispatcher.terminate_calls, pending, supervisor.lifecycle

    response, terminate_calls, pending, lifecycle = asyncio.run(scenario())
    assert response == "Harbor: STOPPED"
    assert terminate_calls == 1
    assert pending == 0
    assert lifecycle == Lifecycle.STOPPED


def test_exit_cancels_a_poll_task_that_is_blocked_in_event_delivery() -> None:
    async def scenario() -> tuple[bool, Lifecycle]:
        poller = BlockingTodoPoller()
        dispatcher = BlockingDispatcher(lambda: True)
        supervisor = Supervisor(poller, dispatcher=dispatcher)
        supervisor.lifecycle = Lifecycle.RUNNING
        never = asyncio.Event()
        poll_task = asyncio.create_task(never.wait(), name="blocked-pr-event-poll")
        supervisor._task = poll_task  # type: ignore[attr-defined]

        await asyncio.wait_for(supervisor.exit(), timeout=0.5)
        return poll_task.cancelled(), supervisor.lifecycle

    cancelled, lifecycle = asyncio.run(scenario())
    assert cancelled is True
    assert lifecycle == Lifecycle.STOPPED


def test_immediate_exit_marks_queued_dispatch_restartable(tmp_path) -> None:
    async def scenario() -> tuple[str, Wake | None]:
        db = HarborDB(tmp_path / "harbor.sqlite")
        db.replace_tickets(
            "SampleProject",
            [{"identifier": "SAMPLE-async", "title": "Queued", "state": "Todo", "owned": False, "hold": False}],
            emit_events=False,
        )
        decider = WakeDecider(db)
        ticket = db.tickets("SampleProject")[0]
        first = decider.decide(ticket)
        assert first is not None

        dispatcher = BlockingDispatcher(lambda: True)
        supervisor = Supervisor(BlockingTodoPoller(), db=db, dispatcher=dispatcher, decider=decider)
        supervisor.lifecycle = Lifecycle.RUNNING
        supervisor._background_dispatch = True  # type: ignore[attr-defined]
        supervisor._schedule_wake(first)  # type: ignore[attr-defined]
        await supervisor.exit()

        row = db.queue_item(idem_key="SAMPLE-async@build")
        restarted = WakeDecider(db).decide(db.tickets("SampleProject")[0])
        state = str(row["status"] if row else "missing")
        db.close()
        return state, restarted

    state, restarted = asyncio.run(scenario())
    assert state == "stopped"
    assert restarted is not None
    assert restarted.idem_key == "SAMPLE-async@build"


class RetryTodoPoller:
    project_name = "SampleProject"

    def __init__(self) -> None:
        self.config = ProjectConfig(
            name=self.project_name,
            linear_project_slug="sample-project",
            poll_interval_seconds=60,
            heartbeat_seconds=60,
        )
        self.calls = 0

    async def poll(self) -> PollResult:
        self.calls += 1
        ticket = {
            "identifier": "SAMPLE-34",
            "title": "Retry a pre-run dispatch",
            "state": "Todo",
            "pr_urls": [],
            "blocked_by": [],
        }
        return PollResult((ticket,), f"retry-snapshot-{self.calls}", True, (ticket,))


class ControlledWorktreeManager:
    def __init__(self, path: Path, *, fail_first: bool = False) -> None:
        self.path = path
        self.fail_first = fail_first
        self.create_calls = 0

    def create(self, ticket: str) -> Path:
        self.create_calls += 1
        if self.fail_first and self.create_calls == 1:
            raise DiskFloor(0.25, 1.0)
        self.path.mkdir()
        return self.path


class RecordingRunner:
    def __init__(self) -> None:
        self.start_calls = 0

    async def start(self, wake: Wake) -> None:
        del wake
        self.start_calls += 1


def test_pre_run_dispatch_failure_is_a_restartable_failed_queue_receipt(tmp_path) -> None:
    async def settle(supervisor: Supervisor) -> None:
        for _ in range(20):
            await asyncio.sleep(0)
            if not supervisor._dispatch_tasks:  # type: ignore[attr-defined]
                return
        raise AssertionError("background dispatch did not settle")

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], int, int, int]:
        db = HarborDB(tmp_path / "harbor.sqlite")
        db.replace_tickets(
            "SampleProject",
            [{"identifier": "SAMPLE-34", "title": "Retry", "state": "Todo", "hold": False}],
            emit_events=False,
        )

        first_manager = ControlledWorktreeManager(tmp_path / "first-worktree", fail_first=True)
        first_dispatcher = WorktreeDispatcher(
            first_manager,
            lambda path, wake: RecordingRunner(),
            db=db,
            project="SampleProject",
        )
        first = Supervisor(
            RetryTodoPoller(),
            db=db,
            dispatcher=first_dispatcher,
            decider=WakeDecider(db),
        )
        await first.start()
        await settle(first)
        first_row = db.queue_item(idem_key="SAMPLE-34@build")
        assert first_row is not None

        # Restart with a healthy worktree manager.  The old failed row remains
        # in the audit ledger, but it is outside the live-idem index, so the
        # same stable build key is enqueued once and delivered once.
        second_manager = ControlledWorktreeManager(tmp_path / "second-worktree")
        second_dispatcher = WorktreeDispatcher(
            second_manager,
            lambda path, wake: RecordingRunner(),
            db=db,
            project="SampleProject",
        )
        second = Supervisor(
            RetryTodoPoller(),
            db=db,
            dispatcher=second_dispatcher,
            decider=WakeDecider(db),
        )
        await second.start()
        await settle(second)
        await second.poll_once()

        rows = db.connection.execute(
            "SELECT status, error FROM work_queue WHERE ticket = 'SAMPLE-34' ORDER BY id"
        ).fetchall()
        owner = db.connection.execute(
            "SELECT owned FROM tickets WHERE identifier = 'SAMPLE-34'"
        ).fetchone()
        runner = second_dispatcher.runners["SAMPLE-34"]
        await first.exit()
        await second.exit()
        db.close()
        assert len(rows) == 2
        return (
            dict(rows[0]),
            dict(rows[1]),
            int(owner[0]),
            second_manager.create_calls,
            runner.start_calls,
        )

    failed, retried, owned, create_calls, starts = asyncio.run(scenario())
    assert failed["status"] == "failed"
    assert failed["error"] == "free disk floor is 1.00 GB; only 0.25 GB is available"
    assert retried["status"] == "queued"
    assert retried["error"] is None
    # Enqueue establishes custody before delivery; a preparation failure does
    # not release ownership because no alternate owner was acquired.
    assert owned == 1
    assert create_calls == 1
    assert starts == 1
