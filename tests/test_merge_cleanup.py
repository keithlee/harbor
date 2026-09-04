"""Worktree removal after a merge (plan section 1.3, section 7 AC-8/AC-10).

The plan says the worktree "lives as long as the ticket: removed on merge, kept
while parked or waiting".  Removal is irreversible, so it is gated on a merge
Harbor observed itself -- never on the builder's own claim.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from harbor.config.projects import ProjectConfig
from harbor.poll.linear_poller import PollResult
from harbor.supervisor import Supervisor, Wake, WorktreeDispatcher


class IdlePoller:
    project_name = "Bookgen"
    config = ProjectConfig(name="Bookgen", linear_project_slug="hanzireader", heartbeat_seconds=1)

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


def supervisor_with(tmp_path: Path) -> tuple[Supervisor, Manager, WorktreeDispatcher]:
    manager = Manager(tmp_path / "wt")
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)
    dispatcher = WorktreeDispatcher(manager, lambda wt, wake: object(), project="Bookgen")
    dispatcher.worktrees["HAN-42"] = manager.create("HAN-42")
    dispatcher.runners["HAN-42"] = object()
    supervisor = Supervisor(IdlePoller(), dispatcher=dispatcher, worktree_manager=manager)
    return supervisor, manager, dispatcher


def wake(*, observed_merge: bool = True) -> Wake:
    facts = (
        {"number": 1, "url": "https://github.com/example/repo/pull/1", "merged": True}
        if observed_merge
        else {}
    )
    return Wake(ticket={"identifier": "HAN-42"}, action="reengage", facts=facts)


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
        assert manager.removed == [dispatcher.worktree_manager.path_for("HAN-42")]
        # The retained session is released along with its worktree.
        assert "HAN-42" not in dispatcher.runners
        assert "HAN-42" not in dispatcher.worktrees

    asyncio.run(scenario())


def test_an_unverified_merge_claim_keeps_the_worktree(tmp_path: Path) -> None:
    """A builder cannot cause an irreversible deletion by claiming a merge."""

    async def scenario() -> None:
        supervisor, manager, dispatcher = supervisor_with(tmp_path)
        await supervisor._handle_runner_result(wake(observed_merge=False), {**MERGED, "merge_verified": False})
        assert manager.removed == []
        assert "HAN-42" in dispatcher.runners

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
            Wake(ticket={"identifier": "HAN-42"}, action="reengage"),
            untrusted,
        )
        assert manager.removed == []
        assert "HAN-42" in dispatcher.worktrees

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
        facts = {"prs": [{"number": 217, "head_sha": "abc"}]}

        await supervisor._process_pr_events({"identifier": "HAN-7"}, facts)
        assert processor.calls == [], "an observed-only ticket must not reach the builder"

        await supervisor._process_pr_events({"identifier": "HAN-32", "owned": True}, facts)
        assert processor.calls == ["HAN-32"], "an owned ticket must still be processed"

    asyncio.run(scenario())
