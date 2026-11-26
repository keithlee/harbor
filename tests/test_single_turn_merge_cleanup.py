"""A builder that merges in ONE turn still gets its merge verified and cleaned up.

Regression for the single-turn auto-merge path.  Within one builder turn the
delivery skill opens a PR, attaches it to the Linear ticket, merges it and
moves the ticket to Done.  Harbor's ledger copy of the ticket was written at
the poll *before* the attachment existed, and once the ticket is Done it is
outside ``poll_states`` so the normal Linear poll never refreshes it.  Harbor
must still re-read the ticket's attachments from Linear (the authority for
which PR belongs to the ticket), verify the merge against GitHub, and remove
the retained worktree during ordinary supervision -- without a separate
``harbor watch`` process.

This runs the real ``Supervisor``, ``WorktreeDispatcher``, ``WorktreeManager``,
``LinearPoller``, ``CodexRunner`` and SQLite ledger over a real git repository.  The builder is the
real ``CodexRunner`` over a model-free executable; only the Linear API and the
GitHub read model are fakes.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from typing import Any

from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB
from harbor.poll.linear_poller import LinearPoller
from harbor.runner.codex import CodexRunner
from harbor.supervisor import Supervisor, Wake, WorktreeDispatcher
from harbor.worktree import WorktreeManager
from test_merge_cycle_e2e import real_repo

TICKET = "SAMPLE-41"
PR_URL = "https://github.com/SampleOrg/SampleProject/pull/225"


FAKE_BUILDER = r"""#!{python}
import json, os, pathlib, subprocess
root = pathlib.Path(os.environ['HARBOR_WORKTREE'])
state = pathlib.Path({state!r})
attach = {attach!r}
def git(*args):
    return subprocess.run(['git', '-c', 'user.name=Harbor Test', '-c', 'user.email=t@example.com', *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout.strip()
print(json.dumps({{'type': 'session.started', 'session_id': 'session-41'}}), flush=True)
(root / 'README.md').write_text('implement SAMPLE-41\n')
git('add', 'README.md')
git('commit', '-m', 'implement SAMPLE-41')
git('push', '-u', 'origin', git('branch', '--show-current'))
head = git('rev-parse', 'HEAD')
facts = {{'head_sha': head}}                                  # 1. open the PR
state.write_text(json.dumps(facts))
if attach:
    facts['attached'] = True                                 # 2. attach it to the Linear ticket
    state.write_text(json.dumps(facts))
git('push', 'origin', 'HEAD:main')                           # 3. readiness gate passes; merge
facts['merged'] = True
facts['done'] = True                                         # 4. Linear -> Done
state.write_text(json.dumps(facts))
print(json.dumps({{'type': 'result', 'result': {{          # 5. final result
    'status': 'completed', 'outcome': 'merged', 'waiting_on': 'none', 'pr_url': {pr_url!r},
    'summary': 'low risk, merged', 'head_sha': head, 'commit_sha': head}}}}), flush=True)
"""


def fake_builder(tmp_path: Path, state: Path, *, attach: bool) -> Path:
    script = tmp_path / "fake-builder.py"
    script.write_text(FAKE_BUILDER.format(python=sys.executable, state=str(state), attach=attach, pr_url=PR_URL),
                      encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def read_state(state: Path) -> dict[str, Any]:
    return json.loads(state.read_text()) if state.exists() else {}


class FakeLinear:
    """Linear as the builder leaves it: the attachment and Done arrive mid-turn."""

    def __init__(self, state: Path) -> None:
        self.state = state
        self.status_fetches: list[str] = []

    def _issue(self) -> dict[str, Any]:
        facts = read_state(self.state)
        return {
            "id": "issue-41",
            "identifier": TICKET,
            "title": "Update docs",
            "state": ({"name": "Done", "type": "completed"} if facts.get("done")
                      else {"name": "Todo", "type": "unstarted"}),
            "attachments": {"nodes": [{"url": PR_URL, "sourceType": "github"}] if facts.get("attached") else []},
        }

    async def fetch_issues(self, scope: Any, states: Any) -> list[dict[str, Any]]:
        issue = self._issue()
        return [issue] if issue["state"]["name"] in states else []

    async def fetch_issue_status(self, identifier: str) -> dict[str, Any]:
        self.status_fetches.append(identifier)
        return self._issue()


class FakeGitHub:
    """GitHub's read model: it reports only PRs attached to the tickets it is given."""

    def __init__(self, state: Path) -> None:
        self.state = state

    async def poll(self, tickets: Any) -> list[dict[str, Any]]:
        facts = read_state(self.state)
        rows = []
        for ticket in tickets:
            urls = [str(url).rstrip("/") for url in ticket.get("pr_urls") or ()]
            if PR_URL in urls and facts.get("head_sha"):
                merged = bool(facts.get("merged"))
                rows.append({
                    "ticket": ticket["identifier"], "number": 225, "url": PR_URL, "head_sha": facts["head_sha"],
                    "merged": merged, "state": "MERGED" if merged else "OPEN",
                })
        return rows


def build(tmp_path: Path, *, attach: bool = True) -> tuple[Supervisor, WorktreeManager, HarborDB, FakeLinear]:
    checkout, _ = real_repo(tmp_path)
    manager = WorktreeManager(
        checkout, root=tmp_path / "wt", repo_name="SampleProject",
        disk_usage=lambda _: 100.0, install_command="none",
    )
    db = HarborDB(tmp_path / "harbor.db")
    state = tmp_path / "external-state.json"
    linear = FakeLinear(state)
    executable = fake_builder(tmp_path, state, attach=attach)

    def factory(worktree: Any, wake: Wake) -> CodexRunner:
        return CodexRunner(getattr(worktree, "path", worktree), db=db, ticket=TICKET,
                           executable=str(executable), run_id=wake.run_id)

    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project-abc123def456", hold_all=False)
    poller = LinearPoller(linear, config, db=db)
    dispatcher = WorktreeDispatcher(manager, factory, db=db, project="SampleProject")
    supervisor = Supervisor(poller, db=db, dispatcher=dispatcher, worktree_manager=manager,
                            github_poller=FakeGitHub(state))
    return supervisor, manager, db, linear


async def poll_and_drain(supervisor: Supervisor) -> None:
    """One normal supervision cycle, waiting for any builder it dispatched."""

    await supervisor.poll_once()
    pending = [task for task in supervisor._dispatch_tasks.values() if not task.done()]
    if pending:
        await asyncio.gather(*pending)


def run_row(db: HarborDB) -> dict[str, Any]:
    row = db.connection.execute(
        "SELECT status, final_json, merge_verified, cleanup_at FROM runs WHERE ticket = ?", (TICKET,)
    ).fetchone()
    assert row is not None, "the builder run must be recorded"
    return {"status": row[0], "outcome": json.loads(row[1] or "{}").get("outcome"),
            "merge_verified": row[2], "cleanup_at": row[3]}


def test_single_turn_merge_is_verified_and_cleaned_up_by_normal_polling(tmp_path: Path) -> None:
    async def scenario() -> None:
        supervisor, manager, db, linear = build(tmp_path)
        try:
            await supervisor.start(initial_poll=False)
            # Poll 1 sees the Todo ticket with no PR, records it, and dispatches
            # the builder, which merges and goes Done inside this one turn.
            await poll_and_drain(supervisor)
            worktree = manager.path_for(TICKET)
            row = run_row(db)
            assert (row["status"], row["outcome"]) == ("completed", "merged")

            # Poll 2: the ticket is Done, so it is no longer in the Linear poll.
            await poll_and_drain(supervisor)

            row = run_row(db)
            assert row["merge_verified"] == 1, f"merge was never verified: {row}"
            assert row["cleanup_at"] is not None, f"cleanup receipt missing: {row}"
            assert not worktree.exists(), "the merged worktree must be removed"
            stored = db.connection.execute(
                "SELECT pr_urls_json, state_type FROM tickets WHERE identifier = ?", (TICKET,)
            ).fetchone()
            assert json.loads(stored[0]) == [PR_URL] and stored[1] == "completed"

            # Bounded: further polls make no more Linear status reads.
            fetches = len(linear.status_fetches)
            assert fetches >= 1
            await poll_and_drain(supervisor)
            await poll_and_drain(supervisor)
            assert len(linear.status_fetches) == fetches
        finally:
            await supervisor.exit()
            db.close()

    asyncio.run(scenario())


def test_builder_claim_without_a_linear_attachment_never_cleans_up(tmp_path: Path) -> None:
    """The refresh trusts Linear, not the builder's ``pr_url``, and runs once."""

    async def scenario() -> None:
        supervisor, manager, db, linear = build(tmp_path, attach=False)  # the skill never attaches the PR
        try:
            await supervisor.start(initial_poll=False)
            await poll_and_drain(supervisor)
            worktree = manager.path_for(TICKET)
            for _ in range(3):
                await poll_and_drain(supervisor)
            row = run_row(db)
            assert row["merge_verified"] in (0, None) and row["cleanup_at"] is None
            assert worktree.is_dir(), "an unattached PR claim must never remove the worktree"
            assert linear.status_fetches == [TICKET], "polls inside the spacing make no further Linear reads"
        finally:
            await supervisor.exit()
            db.close()

    asyncio.run(scenario())


def test_pr_link_refresh_is_bounded_and_spaced(tmp_path: Path, monkeypatch: Any) -> None:
    import time

    supervisor, _, db, _ = build(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    merged = {"status": "completed", "outcome": "merged"}
    spacing = supervisor.PR_LINK_REFRESH_SPACING_SECONDS
    try:
        claim = supervisor._should_refresh_pr_links
        assert claim("run-a", 0, merged) is True
        assert claim("run-a", 0, merged) is False, "a second refresh must wait for the spacing"
        clock[0] += spacing
        assert claim("run-a", 0, merged) is True
        clock[0] += spacing
        assert claim("run-a", 0, merged) is True
        clock[0] += spacing * 10
        assert claim("run-a", 0, merged) is False, "attempts are capped per run"
        assert claim("run-b", 1, merged) is False, "a verified run needs no refresh"
        assert claim("run-c", 0, {"status": "completed", "outcome": "pr_open"}) is False
        supervisor.github_poller = None
        assert claim("run-d", 0, merged) is False, "without GitHub the merge cannot be verified anyway"
    finally:
        db.close()
