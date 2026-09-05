"""Worktree removal after a merge (plan section 1.3, section 7 AC-8/AC-10).

The plan says the worktree "lives as long as the ticket: removed on merge, kept
while parked or waiting".  Removal is irreversible, so it is gated on a merge
Harbor observed itself -- never on the builder's own claim.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB, apply_phase9_migration
from harbor.poll.linear_poller import PollResult
from harbor.runner.base import apply_phase4_migration
from harbor.supervisor import Supervisor, Wake, WorktreeDispatcher
from harbor.wake import apply_phase3_migration


class IdlePoller:
    project_name = "SampleProject"
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", heartbeat_seconds=1)

    async def poll(self) -> PollResult:
        return PollResult((), "snapshot-1", False, ())


class Manager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.removed: list[Path] = []

    def create(self, ticket: str) -> Path:
        path = self.root / ticket
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path_for(self, ticket: str) -> Path:
        return self.root / ticket

    def remove(self, target: Any) -> None:
        self.removed.append(Path(target))


class WrongPRFacts:
    async def poll(self, tickets: Any) -> list[dict[str, Any]]:
        identifier = str(tuple(tickets)[0].get("identifier"))
        return [
            {
                "ticket": identifier,
                "number": 101,
                "url": "https://github.com/example/repo/pull/101",
                "head_sha": "head-101",
                "merged": True,
            }
        ]


def supervisor_with(tmp_path: Path) -> tuple[Supervisor, Manager, WorktreeDispatcher]:
    manager = Manager(tmp_path / "wt")
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)
    dispatcher = WorktreeDispatcher(manager, lambda wt, wake: object(), project="SampleProject")
    dispatcher.worktrees["SAMPLE-42"] = manager.create("SAMPLE-42")
    dispatcher.runners["SAMPLE-42"] = object()
    supervisor = Supervisor(IdlePoller(), dispatcher=dispatcher, worktree_manager=manager)
    return supervisor, manager, dispatcher


def wake(*, observed_merge: bool = True) -> Wake:
    facts = (
        {"number": 1, "url": "https://github.com/example/repo/pull/1", "merged": True}
        if observed_merge
        else {}
    )
    return Wake(
        ticket={
            "identifier": "SAMPLE-42",
            "pr_urls": ["https://github.com/example/repo/pull/1"],
        },
        action="reengage",
        facts=facts,
    )


MERGED = {
    "status": "completed",
    "outcome": "merged",
    "summary": "Merged",
    "pr_url": "https://github.com/example/repo/pull/1",
}


def test_a_verified_merge_removes_the_worktree(tmp_path: Path) -> None:
    async def scenario() -> None:
        supervisor, manager, dispatcher = supervisor_with(tmp_path)
        await supervisor._handle_runner_result(wake(), {**MERGED, "merge_verified": True})
        assert manager.removed == [dispatcher.worktree_manager.path_for("SAMPLE-42")]
        # The retained session is released along with its worktree.
        assert "SAMPLE-42" not in dispatcher.runners
        assert "SAMPLE-42" not in dispatcher.worktrees

    asyncio.run(scenario())


def test_an_unverified_merge_claim_keeps_the_worktree(tmp_path: Path) -> None:
    """A builder cannot cause an irreversible deletion by claiming a merge."""

    async def scenario() -> None:
        supervisor, manager, dispatcher = supervisor_with(tmp_path)
        await supervisor._handle_runner_result(wake(observed_merge=False), {**MERGED, "merge_verified": False})
        assert manager.removed == []
        assert "SAMPLE-42" in dispatcher.runners

    asyncio.run(scenario())


def test_builder_true_merge_receipt_without_observed_fact_keeps_the_worktree(tmp_path: Path) -> None:
    """A direct cleanup call cannot bypass Harbor's observed-receipt gate."""

    async def scenario() -> None:
        supervisor, manager, dispatcher = supervisor_with(tmp_path)
        untrusted = {
            **MERGED,
            "merge_verified": True,
            "merged_at": "2026-09-03T21:33:22Z",
            "merge_commit_sha": "30f420dfe67be12ee76e3efc7acb80699eece557",
        }
        await supervisor._cleanup_merged_worktree(
            Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage"),
            untrusted,
        )
        assert manager.removed == []
        assert "SAMPLE-42" in dispatcher.worktrees

    asyncio.run(scenario())


def test_merged_fact_for_a_different_pr_never_cleans_up(tmp_path: Path) -> None:
    assert (
        Supervisor._matching_pr_fact(
            {
                "number": 202,
                "url": "https://github.com/example/repo-a/pull/202",
                "merged": True,
            },
            "https://github.com/example/repo-b/pull/202",
        )
        is None
    )

    async def scenario() -> None:
        supervisor, manager, dispatcher = supervisor_with(tmp_path)
        mismatched = Wake(
            ticket={"identifier": "SAMPLE-42"},
            action="reengage",
            facts={
                "number": 101,
                "url": "https://github.com/example/repo/pull/101",
                "merged": True,
            },
        )
        result = {
            **MERGED,
            "pr_url": "https://github.com/example/repo/pull/202",
            "merge_verified": True,
        }
        await supervisor._handle_runner_result(mismatched, result)
        assert manager.removed == []
        assert "SAMPLE-42" in dispatcher.worktrees

    asyncio.run(scenario())


def test_direct_cleanup_requires_the_wake_fact_to_match_the_result_pr(tmp_path: Path) -> None:
    async def scenario() -> None:
        supervisor, manager, dispatcher = supervisor_with(tmp_path)
        wake_for_101 = Wake(
            ticket={"identifier": "SAMPLE-42"},
            action="reengage",
            facts={
                "number": 101,
                "url": "https://github.com/example/repo/pull/101",
                "merged": True,
            },
        )
        forged = {
            **MERGED,
            "pr_url": "https://github.com/example/repo/pull/202",
            "merge_verified": True,
        }
        await supervisor._cleanup_merged_worktree(wake_for_101, forged)
        assert manager.removed == []
        assert "SAMPLE-42" in dispatcher.worktrees

    asyncio.run(scenario())


def test_restart_reconciliation_rejects_a_different_merged_pr(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = HarborDB(":memory:")
        apply_phase3_migration(db)
        apply_phase4_migration(db)
        apply_phase9_migration(db)
        db.replace_tickets(
            "SampleProject",
            [{"identifier": "SAMPLE-A", "title": "A", "state": "Done"}],
            emit_events=False,
        )
        manager = Manager(tmp_path / "wt-pr-mismatch")
        worktree = manager.create("SAMPLE-A")
        dispatcher = WorktreeDispatcher(
            manager, lambda wt, wake: object(), db=db, project="SampleProject"
        )
        dispatcher.worktrees["SAMPLE-A"] = worktree
        final = {
            **MERGED,
            "pr_url": "https://github.com/example/repo/pull/202",
            "run_id": "run-a",
        }
        db.connection.execute(
            "INSERT INTO runs(id, ticket, role, worktree, status, final_json, merge_verified) "
            "VALUES ('run-a', 'SAMPLE-A', 'builder', ?, 'completed', ?, 0)",
            (str(worktree), json.dumps(final)),
        )
        db.connection.commit()
        supervisor = Supervisor(
            IdlePoller(),
            db=db,
            dispatcher=dispatcher,
            worktree_manager=manager,
            github_poller=WrongPRFacts(),
        )
        try:
            await supervisor._reconcile_completed_merges()
            row = db.connection.execute(
                "SELECT merge_verified, cleanup_at FROM runs WHERE id = 'run-a'"
            ).fetchone()
            assert row is not None and (row[0], row[1]) == (0, None)
            assert manager.removed == []
            assert worktree.exists()
        finally:
            db.close()

    asyncio.run(scenario())


def test_result_run_id_cannot_move_merge_receipt_across_tickets(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = HarborDB(":memory:")
        apply_phase3_migration(db)
        apply_phase4_migration(db)
        apply_phase9_migration(db)
        db.replace_tickets(
            "SampleProject",
            [
                {"identifier": "SAMPLE-A", "title": "A", "state": "In Review"},
                {"identifier": "SAMPLE-B", "title": "B", "state": "In Review"},
            ],
            emit_events=False,
        )
        manager = Manager(tmp_path / "wt-cross-ticket")
        dispatcher = WorktreeDispatcher(
            manager, lambda wt, wake: object(), project="SampleProject"
        )
        for identifier, run_id in (("SAMPLE-A", "run-a"), ("SAMPLE-B", "run-b")):
            worktree = manager.create(identifier)
            dispatcher.worktrees[identifier] = worktree
            dispatcher.runners[identifier] = object()
            db.connection.execute(
                "INSERT INTO runs(id, ticket, role, worktree, status, final_json, merge_verified) "
                "VALUES (?, ?, 'builder', ?, 'completed', ?, 0)",
                (run_id, identifier, str(worktree), json.dumps(MERGED)),
            )
        db.connection.commit()
        supervisor = Supervisor(
            IdlePoller(), db=db, dispatcher=dispatcher, worktree_manager=manager
        )
        forged = {**MERGED, "run_id": "run-b", "merge_verified": True}
        trusted_wake = Wake(
            ticket={"identifier": "SAMPLE-A"},
            action="reengage",
            resume="run-a",
            facts={"number": 1, "merged": True},
        )
        try:
            await supervisor._handle_runner_result(trusted_wake, forged)
            await supervisor._reconcile_completed_merges()
            rows = db.connection.execute(
                "SELECT ticket, merge_verified, cleanup_at FROM runs ORDER BY ticket"
            ).fetchall()
            assert [(row[0], row[1], row[2]) for row in rows] == [
                ("SAMPLE-A", 0, None),
                ("SAMPLE-B", 0, None),
            ]
            assert manager.removed == []
            assert (tmp_path / "wt-cross-ticket" / "SAMPLE-A").exists()
            assert (tmp_path / "wt-cross-ticket" / "SAMPLE-B").exists()
        finally:
            db.close()

    asyncio.run(scenario())


def test_a_parked_run_keeps_its_worktree(tmp_path: Path) -> None:
    async def scenario() -> None:
        supervisor, manager, _ = supervisor_with(tmp_path)
        parked = {"status": "completed", "outcome": "parked", "waiting_on": "human", "summary": "Merge is yours", "merge_verified": False}
        await supervisor._handle_runner_result(wake(), parked)
        assert manager.removed == []

    asyncio.run(scenario())


def test_a_waiting_run_keeps_its_worktree(tmp_path: Path) -> None:
    async def scenario() -> None:
        supervisor, manager, _ = supervisor_with(tmp_path)
        waiting = {"status": "waiting", "outcome": "pr_open", "waiting_on": "review", "summary": "PR up"}
        await supervisor._handle_runner_result(wake(), waiting)
        assert manager.removed == []

    asyncio.run(scenario())


def test_a_failed_removal_is_reported_not_raised(tmp_path: Path) -> None:
    """A retained worktree is untidy, never unsafe."""

    async def scenario() -> None:
        supervisor, manager, _ = supervisor_with(tmp_path)

        def boom(target: Any) -> None:
            raise OSError("device busy")

        manager.remove = boom  # type: ignore[method-assign]
        await supervisor._handle_runner_result(wake(), {**MERGED, "merge_verified": True})
        assert "device busy" in (supervisor.last_error or "")

    asyncio.run(scenario())


def test_pr_events_are_never_offered_for_an_unowned_ticket(tmp_path: Path) -> None:
    """Pre-existing tickets are (observed) and never woken (plan, MVP scope).

    Offering their review threads to a builder crosses ticket boundaries and
    logs a spurious "no retained builder" error for work Harbor never started.
    """

    class Processor:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def process(self, ticket: Any, row: Any, **kwargs: Any) -> None:
            self.calls.append(str(ticket.get("identifier")))

    async def scenario() -> None:
        processor = Processor()
        supervisor = Supervisor(IdlePoller(), pr_event_processor=processor)
        pr_url = "https://github.com/example/repo/pull/217"
        facts = {"prs": [{"number": 217, "url": pr_url, "head_sha": "abc"}]}

        await supervisor._process_pr_events({"identifier": "SAMPLE-7"}, facts)
        assert processor.calls == [], "an observed-only ticket must not reach the builder"

        await supervisor._process_pr_events(
            {"identifier": "SAMPLE-32", "owned": True, "pr_urls": [pr_url]},
            facts,
        )
        assert processor.calls == ["SAMPLE-32"], "an owned ticket must still be processed"

    asyncio.run(scenario())


def test_pr_events_ignore_a_foreign_url_even_when_facts_claim_the_ticket() -> None:
    class Processor:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def process(self, ticket: Any, row: Any, **kwargs: Any) -> None:
            self.calls.append(str(row.get("url")))

    async def scenario() -> None:
        processor = Processor()
        supervisor = Supervisor(IdlePoller(), pr_event_processor=processor)
        attached = "https://github.com/good/repo/pull/7"
        foreign = {
            "ticket": "SAMPLE-32",
            "number": 7,
            "url": "https://github.com/evil/other/pull/7",
            "head_sha": "evil-sha",
        }
        await supervisor._process_pr_events(
            {
                "identifier": "SAMPLE-32",
                "owned": True,
                "pr_urls": [attached],
            },
            {"prs": [foreign]},
        )
        assert processor.calls == []

    asyncio.run(scenario())
