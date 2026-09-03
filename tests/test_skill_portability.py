"""Phase 6 AC-7: swap the skill, Harbor behaves identically.

"Nothing in Harbor imports a skill or names one except as a config string."
The proof is a stub builder that opens an empty PR and ends waiting_on=review:
Harbor must dispatch, retain, and re-engage it exactly as it would the real
delivery skill, resuming the SAME session rather than starting a new one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from harbor.supervisor import Wake, WorktreeDispatcher


class StubSkill:
    """Opens an 'empty PR', then waits on review. Knows nothing about Harbor."""

    def __init__(self, worktree: Any, run_id: str | None) -> None:
        self.worktree = worktree
        self.default_run_id = run_id
        self.log: list[tuple[str, str]] = []

    async def start(self, instruction: str, *, run_id: str | None = None) -> dict[str, Any]:
        self.log.append(("start", instruction))
        return {"status": "waiting", "outcome": "pr_open", "waiting_on": "review",
                "pr_url": "https://example.test/pr/900", "summary": "stub opened a PR",
                "run_id": run_id or self.default_run_id}

    async def resume(self, run_id: str, instruction: str) -> dict[str, Any]:
        self.log.append(("resume", instruction))
        return {"status": "waiting", "outcome": "pr_open", "waiting_on": "review",
                "summary": "stub handled the threads", "run_id": run_id}


def harness(tmp_path: Path) -> tuple[WorktreeDispatcher, list[StubSkill]]:
    root = tmp_path / "wt"
    root.mkdir(parents=True, exist_ok=True)

    class Manager:
        def create(self, ticket: str) -> Path:
            path = root / ticket
            path.mkdir(parents=True, exist_ok=True)
            return path

        def path_for(self, ticket: str) -> Path:
            return root / ticket

    built: list[StubSkill] = []

    def factory(worktree: Any, wake: Wake) -> StubSkill:
        stub = StubSkill(worktree, wake.run_id)
        built.append(stub)
        return stub

    return WorktreeDispatcher(Manager(), factory, project="Bookgen"), built


def test_a_stub_skill_is_dispatched_and_reengaged_in_the_same_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, built = harness(tmp_path)

        first = await dispatcher.dispatch(
            Wake(ticket={"identifier": "HAN-99"}, action="build", instruction="Build HAN-99")
        )
        assert first["waiting_on"] == "review"
        run_id = first["run_id"]
        assert run_id, "the dispatcher assigns a durable logical run id"

        second = await dispatcher.resume(
            Wake(ticket={"identifier": "HAN-99"}, action="reengage",
                 instruction="2 new review threads. Continue your workflow.")
        )
        assert second["run_id"] == run_id, "re-engagement must resume the SAME session"

        # Exactly one builder was ever constructed for this ticket.
        assert len(built) == 1
        assert [kind for kind, _ in built[0].log] == ["start", "resume"]

    asyncio.run(scenario())


def test_harbor_never_tells_the_stub_what_to_do_only_that_something_changed(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher, built = harness(tmp_path)
        await dispatcher.dispatch(Wake(ticket={"identifier": "HAN-99"}, action="build", instruction="Build HAN-99"))
        await dispatcher.resume(Wake(ticket={"identifier": "HAN-99"}, action="reengage",
                                     instruction="CI finished: green. Continue your workflow."))
        for _, instruction in built[0].log:
            lowered = instruction.lower()
            assert "work-pipeline" not in lowered, "Harbor must not name a skill"
            assert "luna" not in lowered and "sol" not in lowered
            assert "merge the pr" not in lowered, "Harbor never issues a merge instruction"

    asyncio.run(scenario())


def test_a_stub_that_never_opens_a_pr_is_still_retained(tmp_path: Path) -> None:
    """Portability must not depend on the stub behaving like work-pipeline."""

    async def scenario() -> None:
        root = tmp_path / "wt2"
        root.mkdir(parents=True, exist_ok=True)

        class Manager:
            def create(self, ticket: str) -> Path:
                (root / ticket).mkdir(parents=True, exist_ok=True)
                return root / ticket

            def path_for(self, ticket: str) -> Path:
                return root / ticket

        class Minimal:
            def __init__(self, worktree: Any, run_id: str | None) -> None:
                self.default_run_id = run_id
                self.resumed = 0

            async def start(self, instruction: str, *, run_id: str | None = None) -> dict[str, Any]:
                return {"status": "waiting", "waiting_on": "ci", "summary": "waiting on CI"}

            async def resume(self, run_id: str, instruction: str) -> dict[str, Any]:
                self.resumed += 1
                return {"status": "waiting", "waiting_on": "review", "summary": "ok"}

        dispatcher = WorktreeDispatcher(Manager(), lambda w, k: Minimal(w, k.run_id), project="B")
        await dispatcher.dispatch(Wake(ticket={"identifier": "HAN-98"}, action="build", instruction="go"))
        await dispatcher.resume(Wake(ticket={"identifier": "HAN-98"}, action="reengage", instruction="CI finished"))
        assert dispatcher.runners["HAN-98"].resumed == 1

    asyncio.run(scenario())
