"""A runner factory that cannot rebuild a retained builder must say why.

``WorktreeDispatcher.hydrate`` fails closed when the runner factory raises (a
bad model or effort, a missing provider executable, an invalid sandbox host).
Failing closed is right, but the factory's message is the only thing that tells
the operator what to fix. An operator answer or ``/retry`` must surface it
instead of a generic "could not resume" / "cannot attach its worktree".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from harbor.answer_queue import deliver, enqueue
from harbor.db import HarborDB, apply_phase9_migration
from harbor.runner.base import apply_phase4_migration
from harbor.supervisor import Supervisor, Wake, WorktreeDispatcher
from harbor.wake import apply_phase3_migration
from test_integration_console_supervisor import FakePoller
from test_restart_hydration import Manager

REASON = "unsupported Claude effort 'turbo'; use one of low, medium, high"


def _dispatcher(tmp_path: Path, status: str) -> tuple[WorktreeDispatcher, HarborDB]:
    root = tmp_path / "wt"
    worktree = root / "SAMPLE-42"
    worktree.mkdir(parents=True)
    ledger = HarborDB(tmp_path / "harbor.db")
    for migrate in (apply_phase3_migration, apply_phase4_migration, apply_phase9_migration):
        migrate(ledger)
    ledger.replace_tickets(
        "SampleProject", [{"identifier": "SAMPLE-42", "state": "Blocked"}], emit_events=False
    )
    with ledger.connection:
        ledger.connection.execute(
            "INSERT INTO runs(id, ticket, status, session_id, worktree) VALUES ('run-1', 'SAMPLE-42', ?, 'session-1', ?)",
            (status, str(worktree)),
        )

    def factory(wt: Any, wake: Wake) -> Any:
        raise ValueError(REASON)

    return WorktreeDispatcher(Manager(root), factory, db=ledger, project="SampleProject"), ledger


def test_retry_reports_the_factory_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, _ = _dispatcher(tmp_path, "terminated")
        with pytest.raises(RuntimeError) as caught:
            await dispatcher.retry("SAMPLE-42")
        assert REASON in str(caught.value)
        assert dispatcher.runners == {}, "a failed rebuild must not retain a runner"

    asyncio.run(scenario())


def test_answer_resume_records_and_notifies_the_factory_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, ledger = _dispatcher(tmp_path, "waiting")
        notices: list[str] = []
        supervisor = Supervisor(FakePoller(), db=ledger, dispatcher=dispatcher, notifier=notices.append)
        enqueue(ledger.connection, "7", "SampleProject", "SAMPLE-42", "run-1", "Use option A")

        delivered = await deliver(supervisor, "7", "run-1", "Use option A")

        assert delivered is False, "the builder must not resume"
        assert dispatcher.runners == {}
        assert supervisor.last_error and REASON in supervisor.last_error
        assert len(notices) == 1 and REASON in notices[0]

    asyncio.run(scenario())


def test_a_successful_hydrate_clears_a_stale_factory_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, _ = _dispatcher(tmp_path, "waiting")
        assert await dispatcher.hydrate("SAMPLE-42") is None
        assert dispatcher.resume_unavailable_reason("SAMPLE-42") == REASON
        dispatcher.runner_factory = lambda wt, wake: object()
        assert await dispatcher.hydrate("SAMPLE-42") is not None
        assert dispatcher.resume_unavailable_reason("SAMPLE-42") is None

    asyncio.run(scenario())
