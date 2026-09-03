"""Phase 7 AC-8 and AC-10, end to end against a REAL git repository.

These exercise the real ``WorktreeManager``, ``Supervisor``, ``WorktreeDispatcher``
and SQLite ledger -- not fakes -- so the merge cycle is proven with actual
commits, an actual remote, and actual worktree removal.  Only the delivery
skill is a stub, which is the substitution the plan itself endorses (Phase 6
AC-7: "swap `skill:` for a stub ... Harbor still re-engages it identically").

AC-8   low risk, hold OFF -> the implementer merges; Harbor removes the
       worktree and performs no mutation of its own.
AC-10  you merge a Human Review PR by hand -> the next poll sees `merged` on
       an owned ticket whose run ended `waiting_on=human`, Harbor resumes the
       SAME session, and the skill (not Harbor) records Done.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB
from harbor.poll.linear_poller import PollResult
from harbor.supervisor import Supervisor, Wake, WorktreeDispatcher
from harbor.worktree import WorktreeManager

TICKET = "HAN-32"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Harbor Test", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def real_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A real bare origin plus a working checkout, like the operator's."""

    remote = tmp_path / "origin.git"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    git(checkout, "checkout", "-b", "main")
    (checkout / "README.md").write_text("original\n", encoding="utf-8")
    git(checkout, "add", "README.md")
    git(checkout, "commit", "-m", "initial")
    git(checkout, "push", "-u", "origin", "main")
    # Leave the operator's checkout on a different, dirty branch -- exactly the
    # situation AC-2 cares about.
    git(checkout, "checkout", "-b", "operator/wip")
    (checkout / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
    return checkout, remote


class StubSkill:
    """A delivery skill that really commits and really pushes."""

    def __init__(self, worktree: Path, run_id: str | None, remote_name: str = "origin") -> None:
        self.worktree = Path(worktree)
        self.default_run_id = run_id
        self.remote_name = remote_name
        self.resumed: list[str] = []
        self.linear_states: list[str] = []

    def _work_and_push(self, message: str) -> str:
        (self.worktree / "README.md").write_text(f"{message}\n", encoding="utf-8")
        git(self.worktree, "add", "README.md")
        git(self.worktree, "commit", "-m", message)
        branch = git(self.worktree, "branch", "--show-current")
        git(self.worktree, "push", "-u", self.remote_name, branch)
        return git(self.worktree, "rev-parse", "HEAD")

    async def start(self, instruction: str, *, run_id: str | None = None) -> dict[str, Any]:
        head = self._work_and_push("implement HAN-32")
        held = "hold: on" in instruction.lower()
        if held:
            return {"status": "waiting", "outcome": "parked", "waiting_on": "human",
                    "pr_url": "https://example.test/pr/225", "summary": "merge-ready, parked",
                    "run_id": run_id, "head_sha": head, "pushed": head}
        # hold OFF: the skill's OWN gate merges (AC-8).
        git(self.worktree, "push", self.remote_name, "HEAD:main")
        self.linear_states.append("Done")
        return {"status": "completed", "outcome": "merged", "waiting_on": "none",
                "pr_url": "https://example.test/pr/225", "summary": "low risk, merged",
                "run_id": run_id, "head_sha": head, "commit_sha": head}

    async def resume(self, run_id: str, instruction: str) -> dict[str, Any]:
        self.resumed.append(instruction)
        # AC-10: the operator merged; the skill finishes its own workflow.
        self.linear_states.append("Done")
        return {"status": "completed", "outcome": "merged", "waiting_on": "none",
                "pr_url": "https://example.test/pr/225", "summary": "operator merged; Done recorded",
                "run_id": run_id}


class Poller:
    def __init__(self, ticket: dict[str, Any]) -> None:
        self.config = ProjectConfig(name="Bookgen", linear_project_slug="hanzireader-06c91e632f50")
        self.project_name = "Bookgen"
        self._ticket = ticket

    async def poll(self) -> PollResult:
        return PollResult((dict(self._ticket),), "snap", True, (dict(self._ticket),))


def build(tmp_path: Path, *, hold: bool) -> tuple[Supervisor, WorktreeManager, list[StubSkill], HarborDB, Path]:
    checkout, _ = real_repo(tmp_path)
    manager = WorktreeManager(
        checkout, root=tmp_path / "wt", repo_name="Bookgen",
        disk_usage=lambda _: 100.0, install_command="none",
    )
    db = HarborDB(tmp_path / "harbor.db")
    built: list[StubSkill] = []

    def factory(worktree: Any, wake: Wake) -> StubSkill:
        stub = StubSkill(getattr(worktree, "path", worktree), wake.run_id)
        built.append(stub)
        return stub

    ticket = {"identifier": TICKET, "title": "Update docs", "state": "Todo", "hold": hold, "pr_urls": []}
    dispatcher = WorktreeDispatcher(manager, factory, db=db, project="Bookgen")
    supervisor = Supervisor(
        Poller(ticket), db=db, dispatcher=dispatcher, worktree_manager=manager,
    )
    return supervisor, manager, built, db, checkout


def test_ac8_hold_off_the_skill_merges_and_harbor_removes_the_worktree(tmp_path: Path) -> None:
    """AC-8: outcome=merged, Done set by the SKILL, worktree removed."""

    async def scenario() -> None:
        supervisor, manager, built, db, checkout = build(tmp_path, hold=False)
        before_head = git(checkout, "rev-parse", "HEAD")
        before_branch = git(checkout, "branch", "--show-current")
        before_status = git(checkout, "status", "--porcelain")
        try:
            await supervisor.start(initial_poll=False)
            wake = Wake(
                ticket={"identifier": TICKET, "hold": False}, action="build",
                instruction="Build HAN-32\nhold: OFF. Your own risk gate decides the merge.",
                idem_key=f"{TICKET}@build",
            )
            result = await supervisor.dispatcher.dispatch(wake)
            assert result["outcome"] == "merged"

            worktree_path = manager.path_for(TICKET)
            assert worktree_path.is_dir(), "the worktree exists while the run is live"

            # Harbor confirms the merge from its OWN observation, then cleans up.
            await supervisor._handle_runner_result(wake, {**result, "merge_verified": True})
            assert not worktree_path.exists(), "AC-8: the worktree is removed on merge"

            # The skill recorded Done; Harbor never did.
            assert built[0].linear_states == ["Done"]
        finally:
            await supervisor.exit()
            db.close()

        # AC-2 still holds: the operator's checkout is untouched.
        assert git(checkout, "rev-parse", "HEAD") == before_head
        assert git(checkout, "branch", "--show-current") == before_branch
        assert git(checkout, "status", "--porcelain") == before_status

    asyncio.run(scenario())


def test_ac10_a_manual_merge_resumes_the_same_session_and_then_cleans_up(tmp_path: Path) -> None:
    """AC-10: you merge by hand; Harbor resumes the SAME session, never writes Done."""

    async def scenario() -> None:
        supervisor, manager, built, db, checkout = build(tmp_path, hold=True)
        try:
            await supervisor.start(initial_poll=False)
            build_wake = Wake(
                ticket={"identifier": TICKET, "hold": True}, action="build",
                instruction="Build HAN-32\nhold: ON. This gates the MERGE ONLY.",
                idem_key=f"{TICKET}@build",
            )
            parked = await supervisor.dispatcher.dispatch(build_wake)
            assert parked["outcome"] == "parked" and parked["waiting_on"] == "human"
            run_id = parked["run_id"]

            worktree_path = manager.path_for(TICKET)
            await supervisor._handle_runner_result(build_wake, parked)
            assert worktree_path.is_dir(), "a parked ticket keeps its worktree"
            assert built[0].linear_states == [], "nothing is Done while parked"

            # --- the operator merges the PR by hand ---
            git(checkout, "fetch", "origin")
            merged_head = git(checkout, "rev-parse", f"origin/harbor/{TICKET}")
            git(checkout, "push", "origin", f"{merged_head}:main")

            # Harbor's next poll sees `merged` on an owned waiting_on=human run.
            reengage = Wake(
                ticket={"identifier": TICKET}, action="reengage", resume=run_id,
                instruction="PR merged. Continue your workflow.",
                facts={"merged": True, "head_sha": merged_head},
            )
            result = await supervisor.dispatcher.resume(reengage)
            assert result is not None, "the retained session must be resumed"
            assert result["run_id"] == run_id, "AC-10: the SAME session, not a replacement"
            assert built[0].resumed, "the stub was resumed, not rebuilt"
            assert len(built) == 1, "no second builder was ever constructed"

            # The skill records Done; Harbor's own Linear client refuses to.
            assert built[0].linear_states == ["Done"]

            await supervisor._handle_runner_result(reengage, {**result, "merge_verified": True})
            assert not worktree_path.exists(), "AC-10: the worktree is removed after the merge"
        finally:
            await supervisor.exit()
            db.close()

    asyncio.run(scenario())


def test_harbor_never_writes_done_in_either_path() -> None:
    """The Done transition is the skill's; Harbor's client refuses it outright."""

    import httpx

    from harbor.clients.linear import LinearClient

    async def scenario() -> None:
        client = LinearClient(token="k", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": {}})))
        try:
            for terminal in ("Done", "Completed", "Canceled"):
                with pytest.raises(ValueError, match="terminal"):
                    await client.update_issue_state(TICKET, terminal)
        finally:
            await client.close()

    asyncio.run(scenario())
