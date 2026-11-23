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
from harbor.console import Console
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
        dispatcher, ledger = _dispatcher(tmp_path, "terminated")
        console = Console(Supervisor(FakePoller(), db=ledger, dispatcher=dispatcher))
        output = await console.handle_command("/retry SAMPLE-42")
        print(output)
        assert "retry refused" in output and REASON in output
        assert dispatcher.runners == {}, "a failed rebuild must not retain a runner"
        ledger.close()

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
        assert supervisor.resume_failure_reason("run-1") == REASON
        assert supervisor.last_error is None
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


def test_retry_does_not_report_a_stale_factory_error_after_the_worktree_is_removed(
    tmp_path: Path,
) -> None:
    """The operator fixed the config, then the worktree went away: say that."""

    async def scenario() -> None:
        dispatcher, _ = _dispatcher(tmp_path, "terminated")
        with pytest.raises(RuntimeError, match="cannot be rebuilt"):
            await dispatcher.retry("SAMPLE-42")
        dispatcher.runner_factory = lambda wt, wake: object()
        (tmp_path / "wt" / "SAMPLE-42").rmdir()

        with pytest.raises(RuntimeError) as caught:
            await dispatcher.retry("SAMPLE-42")

        assert "cannot attach its existing worktree" in str(caught.value)
        assert REASON not in str(caught.value)
        assert dispatcher.resume_unavailable_reason("SAMPLE-42") is None

    asyncio.run(scenario())


def test_a_resume_that_never_reaches_the_factory_reports_no_reason(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, _ = _dispatcher(tmp_path, "waiting")
        assert await dispatcher.hydrate("SAMPLE-42") is None
        wake = Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage", resume="run-1")
        async with dispatcher._execution_lock("SAMPLE-42"):
            assert await dispatcher.resume(wake) is None
        assert dispatcher.resume_unavailable_reason("SAMPLE-42") is None

    asyncio.run(scenario())


def test_a_second_answer_for_another_run_does_not_carry_the_first_reason(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, ledger = _dispatcher(tmp_path, "waiting")
        notices: list[str] = []
        supervisor = Supervisor(FakePoller(), db=ledger, dispatcher=dispatcher, notifier=notices.append)
        enqueue(ledger.connection, "7", "SampleProject", "SAMPLE-42", "run-1", "Use option A")
        enqueue(ledger.connection, "8", "SampleProject", "OTHER-1", "no-such-run", "Use option B")

        assert await deliver(supervisor, "7", "run-1", "Use option A") is False
        assert await deliver(supervisor, "8", "no-such-run", "Use option B") is False

        assert len(notices) == 2
        assert REASON in notices[0]
        assert REASON not in notices[1]

    asyncio.run(scenario())


def test_a_long_factory_error_keeps_the_retry_guidance_in_the_notice(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, ledger = _dispatcher(tmp_path, "waiting")

        def factory(wt: Any, wake: Wake) -> Any:
            raise ValueError("x" * 2000)

        dispatcher.runner_factory = factory
        notices: list[str] = []
        supervisor = Supervisor(FakePoller(), db=ledger, dispatcher=dispatcher, notifier=notices.append)
        enqueue(ledger.connection, "7", "SampleProject", "SAMPLE-42", "run-1", "Use option A")

        assert await deliver(supervisor, "7", "run-1", "Use option A") is False
        assert len(notices) == 1 and "Use /retry" in notices[0]

    asyncio.run(scenario())


def test_console_shows_the_run_failure_after_a_healthy_poll(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, ledger = _dispatcher(tmp_path, "waiting")
        supervisor = Supervisor(FakePoller(), db=ledger, dispatcher=dispatcher)
        console = Console(supervisor)
        await supervisor.poll_once()
        assert await supervisor.resume_session("run-1", "Use option A") is False

        before = await console.handle_command("/status")
        print(f"/status before refreshing Linear:\n{before}")
        await supervisor.poll_once()
        after = await console.handle_command("/status")
        print(f"/status after refreshing Linear:\n{after}")
        assert "Linear healthy" in before and "Linear healthy" in after
        assert "Linear error:" not in before and "Linear error:" not in after
        reason = f"Builder resume error for SAMPLE-42 (run run-1): {REASON}"
        assert reason in before and reason in after
        assert reason in await console.handle_command("/status --full")

        class AvailableRunner:
            async def resume(self, run_id: str, prompt: str) -> dict[str, str]:
                assert run_id == "run-1"
                return {"run_id": run_id, "status": "waiting"}

        dispatcher.runner_factory = lambda _wt, _wake: AvailableRunner()
        result = await dispatcher.resume(
            Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage", resume="run-1"),
        )
        assert result == {"run_id": "run-1", "status": "waiting"}
        await supervisor.route_pr_event_result({"ticket": "SAMPLE-42", "run_id": "run-1"}, result)
        recovered = await console.handle_command("/status")
        print(f"/status after successfully resuming:\n{recovered}")
        assert reason not in recovered
        ledger.close()

    asyncio.run(scenario())


def test_changed_factory_failure_notifies_on_reanswering_same_escalation(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, ledger = _dispatcher(tmp_path, "waiting")
        escalation = ledger.record_escalation("SAMPLE-42", "run-1", "ambiguous_ac", "Which option?", 2)
        escalation_id = str(escalation["id"])
        notices: list[str] = []
        supervisor = Supervisor(FakePoller(), db=ledger, dispatcher=dispatcher, notifier=notices.append)
        console = Console(supervisor)
        second_reason = "Claude executable is unavailable"

        def unavailable_factory(_worktree: Any, _wake: Wake) -> Any:
            raise ValueError(second_reason)

        for reply in ("Use option A", "Use option B", "Use option B again"):
            output = await console.handle_command(f"/answer {escalation_id} {reply}")
            print(output)
            assert "same builder resume queued" in output
            await supervisor._dispatch_tasks["SAMPLE-42"]
            dispatcher.runner_factory = unavailable_factory

        for notice in notices:
            print(notice)
        assert len(notices) == 2
        assert all("Use /retry" in notice for notice in notices)
        assert REASON in notices[0] and second_reason in notices[1]
        ledger.close()

    asyncio.run(scenario())
