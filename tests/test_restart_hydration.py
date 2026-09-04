"""Restart recovery for a waiting same-agent session (plan section 1.2, AC-7).

``WorktreeDispatcher.runners`` is process-local, but the run row, its session
id, and the worktree all survive a restart.  A re-engagement after a restart
must therefore resume the SAME session -- never create a replacement agent, and
never silently drop the ticket.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from harbor.pr_events import PREventError
from harbor.supervisor import Wake, WorktreeDispatcher


class Ledger:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE runs (id TEXT PRIMARY KEY, ticket TEXT, status TEXT, "
            "session_id TEXT, worktree TEXT, started_at TEXT, finished_at TEXT)"
        )
        self.connection.commit()

    def add(self, run_id: str, ticket: str, status: str, worktree: Path | str) -> None:
        self.connection.execute(
            "INSERT INTO runs (id, ticket, status, session_id, worktree) VALUES (?, ?, ?, ?, ?)",
            (run_id, ticket, status, "session-123", str(worktree)),
        )
        self.connection.commit()


class Manager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.created: list[str] = []

    def create(self, ticket: str) -> Path:
        self.created.append(ticket)
        path = self.root / ticket
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path_for(self, ticket: str) -> Path:
        return self.root / ticket


class Runner:
    """Records which session id a resume targeted."""

    def __init__(self, worktree: Any, run_id: str | None) -> None:
        self.worktree = worktree
        self.default_run_id = run_id
        self.resumed: list[tuple[str, str]] = []

    async def resume(self, run_id: str, instruction: str) -> dict[str, Any]:
        self.resumed.append((run_id, instruction))
        return {"status": "waiting", "summary": "ok", "run_id": run_id}


def build(tmp_path: Path, status: str, *, worktree_exists: bool = True) -> tuple[WorktreeDispatcher, Ledger, Manager]:
    root = tmp_path / "wt"
    root.mkdir(parents=True, exist_ok=True)
    worktree = root / "HAN-42"
    if worktree_exists:
        worktree.mkdir(parents=True, exist_ok=True)
    ledger = Ledger()
    ledger.add("run-1", "HAN-42", status, worktree)
    manager = Manager(root)
    built: list[Runner] = []

    def factory(wt: Any, wake: Wake) -> Runner:
        runner = Runner(wt, wake.run_id)
        built.append(runner)
        return runner

    dispatcher = WorktreeDispatcher(manager, factory, db=ledger, project="Bookgen")
    dispatcher._built = built  # type: ignore[attr-defined]
    return dispatcher, ledger, manager


def reengage_wake() -> Wake:
    return Wake(ticket={"identifier": "HAN-42"}, action="reengage", instruction="CI finished. Continue.")


def test_resume_after_restart_rebuilds_the_same_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, _, manager = build(tmp_path, "waiting")
        assert dispatcher.runners == {}, "a restarted process starts with no runners"

        result = await dispatcher.resume(reengage_wake())

        assert result is not None
        runner = dispatcher.runners["HAN-42"]
        assert runner.resumed == [("run-1", "CI finished. Continue.")]
        # The persisted run id is reused, so no second logical session exists.
        assert runner.default_run_id == "run-1"
        # And no new worktree was created for the resume.
        assert manager.created == []

    asyncio.run(scenario())


def test_a_parked_run_is_also_recoverable(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, _, _ = build(tmp_path, "parked")
        assert await dispatcher.resume(reengage_wake()) is not None

    asyncio.run(scenario())


def test_a_running_row_is_left_to_the_orphan_reap(tmp_path: Path) -> None:
    """A live row must not get a second handle racing the reaper."""

    async def scenario() -> None:
        dispatcher, _, _ = build(tmp_path, "running")
        assert await dispatcher.resume(reengage_wake()) is None
        assert dispatcher.runners == {}

    asyncio.run(scenario())


def test_an_in_memory_runner_is_not_resumed_while_its_run_row_is_running(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, _, _ = build(tmp_path, "running")
        runner = Runner(tmp_path / "wt" / "HAN-42", "run-1")
        dispatcher.runners["HAN-42"] = runner
        assert await dispatcher.resume(reengage_wake()) is None
        assert runner.resumed == []
        with pytest.raises(PREventError, match="still running"):
            await dispatcher.handle_pr_event(
                {"ticket": "HAN-42", "instruction": "new review", "run_id": "run-1"}
            )
        assert runner.resumed == []

    asyncio.run(scenario())


def test_pr_event_cannot_enter_the_pre_ledger_row_dispatch_window(tmp_path: Path) -> None:
    """The runner is retained before BaseRunner inserts its running row."""

    async def scenario() -> None:
        root = tmp_path / "wt"
        root.mkdir(parents=True, exist_ok=True)
        manager = Manager(root)
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        class BlockingRunner:
            default_run_id = "run-window"

            async def start(self, wake: Wake) -> dict[str, Any]:
                del wake
                started.set()
                await release.wait()
                return {"status": "waiting", "summary": "paused"}

            async def resume(self, run_id: str, instruction: str) -> dict[str, Any]:
                calls.append(f"{run_id}:{instruction}")
                return {"status": "waiting", "summary": "resumed"}

        runner = BlockingRunner()
        dispatcher = WorktreeDispatcher(manager, lambda worktree, wake: runner)
        build = asyncio.create_task(
            dispatcher.dispatch(
                Wake(ticket={"identifier": "HAN-window"}, action="build", instruction="build")
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert dispatcher.runners["HAN-window"] is runner
        with pytest.raises(PREventError, match="already active"):
            await dispatcher.handle_pr_event(
                {"ticket": "HAN-window", "run_id": "run-window", "instruction": "review"}
            )
        assert await dispatcher.resume(
            Wake(ticket={"identifier": "HAN-window"}, action="reengage", resume="run-window")
        ) is None
        assert calls == []
        release.set()
        await build

    asyncio.run(scenario())


def test_a_removed_worktree_fails_closed(tmp_path: Path) -> None:
    """A merged ticket's worktree is gone; never rebuild one to resume into."""

    async def scenario() -> None:
        dispatcher, _, manager = build(tmp_path, "waiting", worktree_exists=False)
        assert await dispatcher.resume(reengage_wake()) is None
        assert manager.created == []

    asyncio.run(scenario())


def test_an_unknown_ticket_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, _, _ = build(tmp_path, "waiting")
        wake = Wake(ticket={"identifier": "HAN-99"}, action="reengage", instruction="x")
        assert await dispatcher.resume(wake) is None

    asyncio.run(scenario())


def test_hydration_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, _, _ = build(tmp_path, "waiting")
        first = await dispatcher.hydrate("HAN-42")
        second = await dispatcher.hydrate("HAN-42")
        assert first is second
        assert len(dispatcher._built) == 1  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_a_pr_event_after_restart_reaches_the_same_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, _, _ = build(tmp_path, "waiting")
        await dispatcher.handle_pr_event(
            {"ticket": "HAN-42", "instruction": "2 new review threads.", "run_id": "run-1"}
        )
        assert dispatcher.runners["HAN-42"].resumed == [("run-1", "2 new review threads.")]

    asyncio.run(scenario())


def test_an_operator_answer_in_a_new_process_resumes_the_same_session(tmp_path: Path) -> None:
    """`harbor answer` is its own command, so its runner map starts empty.

    The run row, session id and worktree all survive, so reporting "builder
    session not available" instead of hydrating strands the ticket.
    """

    async def scenario() -> None:
        dispatcher, _, manager = build(tmp_path, "waiting")
        assert dispatcher.runners == {}

        resumed = await dispatcher.resume_session("run-1", "Operator answer: proceed.")

        assert resumed is True
        runner = dispatcher.runners["HAN-42"]
        assert runner.resumed == [("run-1", "Operator answer: proceed.")]
        assert manager.created == [], "no new worktree for an answer resume"

    asyncio.run(scenario())


def test_an_answer_for_an_unknown_run_reports_unavailable(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, _, _ = build(tmp_path, "waiting")
        assert await dispatcher.resume_session("no-such-run", "hello") is False

    asyncio.run(scenario())
