from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import harbor.cli as cli
from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB, apply_phase9_migration
from harbor.poll.linear_poller import PollResult
from harbor.pr_events import PREventError
from harbor.runner.base import apply_phase4_migration
from harbor.supervisor import Supervisor, WorktreeDispatcher
from harbor.wake import TicketScope, Wake, WakeDecider, apply_phase3_migration


def _ticket(identifier: str, *, state: str = "Todo") -> dict[str, Any]:
    return {
        "identifier": identifier,
        "id": f"linear-{identifier}",
        "title": f"Title {identifier}",
        "state": state,
        "blocked_by": [],
        "pr_urls": [f"https://github.com/example/repo/pull/{identifier.rsplit('-', 1)[-1]}"],
        "owned": False,
    }


class FleetPoller:
    project_name = "SampleProject"
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project")

    def __init__(self, tickets: list[dict[str, Any]]) -> None:
        self.tickets = tickets

    async def poll(self) -> PollResult:
        return PollResult(tuple(self.tickets), "fleet", True, tuple(self.tickets))


class RecordingDispatcher:
    def __init__(self) -> None:
        self.wakes: list[Wake] = []

    async def dispatch(self, wake: Wake) -> None:
        self.wakes.append(wake)


def test_exact_case_insensitive_scope_decides_only_target_but_status_shows_fleet(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
        tickets = [_ticket("SAMPLE-34"), _ticket("SAMPLE-35")]
        db = HarborDB(tmp_path / "scope.db")
        apply_phase3_migration(db)
        db.replace_tickets("SampleProject", tickets)
        dispatcher = RecordingDispatcher()
        calls: list[str] = []

        class SpyDecider:
            def decide(self, ticket: dict[str, Any], facts: dict[str, Any]) -> Wake | None:
                calls.append(ticket["identifier"])
                return Wake(ticket=ticket, action="build", idem_key=f"{ticket['identifier']}@build")

        supervisor = Supervisor(
            FleetPoller(tickets),
            db=db,
            ticket_scope=" sample-34 ",
            decider=SpyDecider(),
            dispatcher=dispatcher,
        )
        try:
            await supervisor.poll_once()
            status = supervisor.status()
            queued = [row[0] for row in db.connection.execute("SELECT ticket FROM work_queue")]
            return (
                calls,
                [wake.ticket["identifier"] for wake in dispatcher.wakes],
                list(status.tickets),
                queued,
            )
        finally:
            await supervisor.exit()
            db.close()

    calls, dispatched, visible, queued = asyncio.run(scenario())
    assert calls == ["SAMPLE-34"]
    assert dispatched == ["SAMPLE-34"]
    assert {ticket["identifier"] for ticket in visible} == {"SAMPLE-34", "SAMPLE-35"}
    # A custom decider is intentionally used above to prove Supervisor gates
    # before decide; no queue mutation can occur without the real decider.
    assert queued == []


def test_wake_decider_scope_is_defense_in_depth_for_queue_and_ownership(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "decider.db")
    tickets = [_ticket("SAMPLE-34"), _ticket("SAMPLE-35")]
    db.replace_tickets("SampleProject", tickets)
    try:
        decider = WakeDecider(db, ticket_scope="sample-34")
        assert decider.decide(tickets[1], {}) is None
        assert db.connection.execute("SELECT count(*) FROM work_queue").fetchone()[0] == 0
        assert not db.connection.execute("SELECT owned FROM tickets WHERE identifier='SAMPLE-35'").fetchone()[0]
        assert decider.decide(tickets[0], {}) is not None
        assert db.connection.execute("SELECT ticket FROM work_queue").fetchone()[0] == "SAMPLE-34"
    finally:
        db.close()


def test_scope_rejects_blank_selectors() -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        TicketScope(" \t\n ")
    assert asyncio.run(cli._start("SampleProject", ticket=" ")) == 2


def test_worktree_dispatcher_rejects_foreign_direct_entry_points(tmp_path: Path) -> None:
    class Manager:
        def __init__(self) -> None:
            self.created: list[str] = []

        def create(self, identifier: str) -> Path:
            self.created.append(identifier)
            return tmp_path

    class Runner:
        async def start(self, wake: Wake) -> None:
            return None

    async def scenario() -> tuple[Any, list[str]]:
        manager = Manager()
        dispatcher = WorktreeDispatcher(manager, lambda path, wake: Runner(), ticket_scope="SAMPLE-34")
        assert await dispatcher.dispatch(Wake(ticket={"identifier": "sample-35"}, action="build")) is None
        assert await dispatcher.resume(Wake(ticket={"identifier": "SAMPLE-35"}, action="reengage")) is None
        assert await dispatcher.hydrate("SAMPLE-35") is None
        with pytest.raises(PREventError, match="outside invocation scope"):
            await dispatcher.handle_pr_event({"ticket": "SAMPLE-35", "instruction": "foreign"})
        return dispatcher.runners, manager.created

    runners, created = asyncio.run(scenario())
    assert runners == {}
    assert created == []


def test_hydration_and_merge_reconcile_are_scoped(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        db = HarborDB(tmp_path / "restart.db")
        apply_phase3_migration(db)
        apply_phase4_migration(db)
        apply_phase9_migration(db)
        db.replace_tickets("SampleProject", [_ticket("SAMPLE-34"), _ticket("SAMPLE-35")])
        target_path = tmp_path / "target"
        foreign_path = tmp_path / "foreign"
        target_path.mkdir()
        foreign_path.mkdir()
        db.connection.executemany(
            "INSERT INTO runs(id, ticket, role, status, worktree, final_json, merge_verified) "
            "VALUES (?, ?, 'builder', ?, ?, ?, ?)",
            [
                ("run-target", "SAMPLE-34", "waiting", str(target_path), None, 0),
                ("run-foreign", "SAMPLE-35", "waiting", str(foreign_path), None, 0),
                (
                    "merge-target",
                    "SAMPLE-34",
                    "completed",
                    str(target_path),
                    '{"status":"completed","outcome":"merged","pr_url":"https://github.com/example/repo/pull/34"}',
                    1,
                ),
                (
                    "merge-foreign",
                    "SAMPLE-35",
                    "completed",
                    str(foreign_path),
                    '{"status":"completed","outcome":"merged","pr_url":"https://github.com/example/repo/pull/35"}',
                    1,
                ),
            ],
        )
        db.connection.commit()
        hydrated: list[str] = []
        reconciled: list[str] = []

        class Manager:
            def attach(self, identifier: str) -> Path:
                hydrated.append(identifier)
                return target_path if identifier == "SAMPLE-34" else foreign_path

        dispatcher = WorktreeDispatcher(Manager(), lambda path, wake: object(), db=db, ticket_scope="SAMPLE-34")
        supervisor = Supervisor(
            FleetPoller([]),
            db=db,
            dispatcher=dispatcher,
            ticket_scope="sample-34",
        )

        async def cleanup(wake: Wake, result: dict[str, Any]) -> None:
            reconciled.append(wake.ticket["identifier"])

        supervisor._cleanup_merged_worktree = cleanup  # type: ignore[method-assign]
        try:
            await supervisor._hydrate_retained_builders()
            await supervisor._reconcile_completed_merges()
            return hydrated, reconciled
        finally:
            await supervisor.exit()
            db.close()

    hydrated, reconciled = asyncio.run(scenario())
    assert hydrated == ["SAMPLE-34"]
    assert reconciled == ["SAMPLE-34"]


def test_foreign_pr_event_and_escalation_cannot_reach_builder(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, bool]:
        db = HarborDB(tmp_path / "events.db")
        db.record_escalation("SAMPLE-35", "run-foreign", "decision", "Foreign?", 2)
        event_calls = 0

        class Processor:
            async def process(self, *args: Any, **kwargs: Any) -> None:
                nonlocal event_calls
                event_calls += 1

        supervisor = Supervisor(
            FleetPoller([]),
            db=db,
            ticket_scope="SAMPLE-34",
            pr_event_processor=Processor(),
        )
        try:
            await supervisor._process_pr_events(_ticket("SAMPLE-35"), {"number": 1})
            with pytest.raises(ValueError, match="outside invocation scope"):
                await supervisor.answer_escalation("1", "Citation: AGENTS.md:1 proceed")
            return event_calls, db.find_resolution("decision", "Foreign?") is not None
        finally:
            await supervisor.exit()
            db.close()

    event_calls, resolution_exists = asyncio.run(scenario())
    assert event_calls == 0
    assert resolution_exists is False


def test_scoped_startup_reaps_selected_orphan_only(tmp_path: Path, monkeypatch: Any) -> None:
    """A scoped restart must not orphan or signal a foreign running row."""

    import harbor.runner.base as runner_base

    db = HarborDB(tmp_path / "orpsample-scope.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    db.replace_tickets("SampleProject", [_ticket("SAMPLE-34"), _ticket("SAMPLE-35")])
    db.connection.executemany(
        "INSERT INTO runs(id, ticket, role, status, pid, pgid) VALUES (?, ?, 'builder', 'running', ?, ?)",
        [("run-target", "SAMPLE-34", 111, 211), ("run-foreign", "SAMPLE-35", 112, 212)],
    )
    db.connection.commit()
    signals: list[int] = []
    monkeypatch.setattr(runner_base, "process_alive", lambda pid: True)
    monkeypatch.setattr(runner_base, "process_group_alive", lambda pgid: False)
    monkeypatch.setattr(
        runner_base,
        "signal_process_group",
        lambda pid, pgid, sig=None: signals.append(int(pid)) or True,
    )

    async def scenario() -> None:
        supervisor = Supervisor(
            FleetPoller([]),
            db=db,
            ticket_scope="sample-34",
            migrations=[],
        )
        await supervisor._apply_startup_migrations()

    try:
        asyncio.run(scenario())
        statuses = {
            row[0]: row[1]
            for row in db.connection.execute("SELECT id, status FROM runs ORDER BY id")
        }
    finally:
        db.close()
    assert statuses == {"run-foreign": "running", "run-target": "orphaned"}
    assert signals == [111]


def test_scoped_dispatcher_lifecycle_and_stop_filter_retained_runners_and_tasks(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], list[str], list[str], bool, bool]:
        db = HarborDB(tmp_path / "lifecycle-scope.db")
        apply_phase3_migration(db)
        db.replace_tickets("SampleProject", [_ticket("SAMPLE-34"), _ticket("SAMPLE-35")])
        db.enqueue("SAMPLE-34", "build", "SAMPLE-34@build", {})
        db.enqueue("SAMPLE-35", "build", "SAMPLE-35@build", {})
        calls: dict[str, list[str]] = {"target": [], "foreign": []}

        class Runner:
            def __init__(self, key: str) -> None:
                self.key = key

            async def pause(self, reason: str | None = None) -> None:
                calls[self.key].append(f"pause:{reason}")

            async def resume_deadline(self) -> None:
                calls[self.key].append("resume_deadline")

            async def terminate(self, *, reason: str = "") -> None:
                calls[self.key].append(f"terminate:{reason}")

        dispatcher = WorktreeDispatcher(
            object(),
            lambda path, wake: object(),
            db=db,
            ticket_scope="SAMPLE-34",
        )
        dispatcher.runners = {"SAMPLE-34": Runner("target"), "SAMPLE-35": Runner("foreign")}
        supervisor = Supervisor(
            FleetPoller([]),
            db=db,
            dispatcher=dispatcher,
            ticket_scope="sample-34",
        )
        target_gate = asyncio.Event()
        foreign_gate = asyncio.Event()
        target_task = asyncio.create_task(target_gate.wait())
        foreign_task = asyncio.create_task(foreign_gate.wait())
        supervisor._dispatch_tasks = {"SAMPLE-34": target_task, "SAMPLE-35": foreign_task}
        supervisor._dispatch_wakes = {
            "SAMPLE-34": Wake(ticket={"identifier": "SAMPLE-34"}, action="build", idem_key="SAMPLE-34@build"),
            "SAMPLE-35": Wake(ticket={"identifier": "SAMPLE-35"}, action="build", idem_key="SAMPLE-35@build"),
        }
        try:
            await dispatcher.pause("operator")
            await dispatcher.resume_deadline()
            await supervisor._stop_active_dispatches()
            queue_status = [
                row[0]
                for row in db.connection.execute(
                    "SELECT ticket || ':' || status FROM work_queue ORDER BY ticket"
                )
            ]
            return (
                calls["target"],
                calls["foreign"],
                queue_status,
                target_task.cancelled(),
                foreign_task.done(),
            )
        finally:
            if not foreign_task.done():
                foreign_task.cancel()
            await asyncio.gather(target_task, foreign_task, return_exceptions=True)
            db.close()

    target, foreign, queue_status, target_cancelled, foreign_done = asyncio.run(scenario())
    assert target == ["pause:operator", "resume_deadline", "terminate:operator exit"]
    assert foreign == []
    assert queue_status == ["SAMPLE-34:stopped", "SAMPLE-35:queued"]
    assert target_cancelled is True
    assert foreign_done is False
