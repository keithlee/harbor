"""Regression coverage for the real HAN-33 / PR #226 merge receipt.

The builder returned the exact JSON below after PR #226 merged.  Harbor had
not yet observed the PR as merged, so the receipt was persisted with
``merge_verified=false`` and the worktree remained.  These tests keep that
fail-closed behavior for an unobserved claim while proving that a read-only
GitHub observation upgrades the durable receipt and cleanup state.
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


HAN33_PR = "https://github.com/keithlee/Bookgen/pull/226"
HAN33_FINAL_JSON = {
    "merge_verified": False,
    "outcome": "merged",
    "pr_url": HAN33_PR,
    "status": "completed",
    "summary": (
        "HAN-33 completed. Sol approved all ACs; the low-risk, comment-only change "
        "merged as 30f420dfe67be12ee76e3efc7acb80699eece557. Local gates passed "
        "(680/680 tests), required CI and post-merge image build passed, proof "
        "audit returned PASS, the remote branch was deleted, and Linear HAN-33 "
        "is Done. The isolated worktree remains clean."
    ),
    "waiting_on": "none",
}


class IdlePoller:
    project_name = "Bookgen"
    config = ProjectConfig(name="Bookgen", linear_project_slug="hanzireader", heartbeat_seconds=1)

    async def poll(self) -> PollResult:
        return PollResult((), "snapshot-1", False, ())


class MergeFacts:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def poll(self, tickets: Any) -> list[dict[str, Any]]:
        self.calls.append(tickets)
        return [
            {
                "ticket": "HAN-33",
                "number": 226,
                "url": HAN33_PR,
                "head_sha": "30f420dfe67be12ee76e3efc7acb80699eece557",
                "merged": True,
                "merged_at": "2026-09-03T21:33:22Z",
            }
        ]


class Manager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.removed: list[Path] = []
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, ticket: str) -> Path:
        path = self.root / ticket
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path_for(self, ticket: str) -> Path:
        return self.root / ticket

    def remove(self, target: Any) -> None:
        self.removed.append(Path(target))


def build_supervisor(tmp_path: Path, *, with_github: bool = True) -> tuple[Supervisor, Manager, HarborDB, MergeFacts | None]:
    db = HarborDB(":memory:")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    db.replace_tickets(
        "Bookgen",
        [{"identifier": "HAN-33", "title": "Receipt", "state": "Todo", "pr_urls": [], "owned": True, "hold": False}],
        emit_events=False,
    )
    db.mark_owned("HAN-33")
    manager = Manager(tmp_path / "worktrees")
    worktree = manager.create("HAN-33")
    run_id = "harbor-Bookgen-HAN-33-builder"
    db.connection.execute(
        "INSERT INTO runs(id, ticket, role, status, waiting_on, worktree, final_json, started_at) "
        "VALUES (?, ?, 'builder', 'completed', 'none', ?, ?, CURRENT_TIMESTAMP)",
        (run_id, "HAN-33", str(worktree), json.dumps(HAN33_FINAL_JSON, sort_keys=True)),
    )
    db.connection.commit()
    dispatcher = WorktreeDispatcher(manager, lambda wt, wake: object(), db=db, project="Bookgen")
    dispatcher.worktrees["HAN-33"] = worktree
    dispatcher.runners["HAN-33"] = object()
    github = MergeFacts() if with_github else None
    supervisor = Supervisor(
        IdlePoller(),
        db=db,
        dispatcher=dispatcher,
        worktree_manager=manager,
        github_poller=github,
    )
    return supervisor, manager, db, github


def wake() -> Wake:
    return Wake(
        ticket={"identifier": "HAN-33"},
        action="reengage",
        resume="harbor-Bookgen-HAN-33-builder",
    )


def test_observed_pr226_upgrades_receipt_cleans_worktree_and_digest(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[Path], dict[str, Any], str, int]:
        supervisor, manager, db, github = build_supervisor(tmp_path)
        await supervisor._handle_runner_result(wake(), dict(HAN33_FINAL_JSON))
        row = db.connection.execute(
            "SELECT merge_verified, cleanup_at, final_json FROM runs WHERE ticket = 'HAN-33'"
        ).fetchone()
        digest = supervisor.fleet_digest()
        await supervisor.exit()
        db.close()
        return manager.removed, dict(row), digest, len(github.calls if github else ())

    removed, row, digest, calls = asyncio.run(scenario())
    assert removed == [Path(tmp_path / "worktrees" / "HAN-33")]
    assert row["merge_verified"] == 1
    assert row["cleanup_at"]
    stored = json.loads(row["final_json"])
    assert stored["merge_verified"] is True
    assert stored["outcome"] == "merged"
    assert stored["pr_url"] == HAN33_PR
    assert calls == 1
    assert "outcome=merged" in digest
    assert f"pr_url={HAN33_PR}" in digest


def test_exact_unobserved_han33_receipt_stays_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[Path], dict[str, Any], str]:
        supervisor, manager, db, _ = build_supervisor(tmp_path, with_github=False)
        await supervisor._handle_runner_result(wake(), dict(HAN33_FINAL_JSON))
        row = db.connection.execute(
            "SELECT merge_verified, cleanup_at, final_json FROM runs WHERE ticket = 'HAN-33'"
        ).fetchone()
        digest = supervisor.fleet_digest()
        await supervisor.exit()
        db.close()
        return manager.removed, dict(row), digest

    removed, row, digest = asyncio.run(scenario())
    assert removed == []
    assert row["merge_verified"] == 0
    assert row["cleanup_at"] is None
    assert json.loads(row["final_json"])["merge_verified"] is False
    assert "outcome=merged" in digest
    assert f"pr_url={HAN33_PR}" in digest


def test_restart_reconciles_stale_han33_merge_after_linear_done(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[Path], dict[str, Any]]:
        supervisor, manager, db, _ = build_supervisor(tmp_path)
        await supervisor._reconcile_completed_merges()
        row = db.connection.execute(
            "SELECT merge_verified, cleanup_at FROM runs WHERE ticket = 'HAN-33'"
        ).fetchone()
        await supervisor.exit()
        db.close()
        return manager.removed, dict(row)

    removed, row = asyncio.run(scenario())
    assert removed == [Path(tmp_path / "worktrees" / "HAN-33")]
    assert row["merge_verified"] == 1
    assert row["cleanup_at"]


def test_cleanup_binds_an_older_receipt_to_its_exact_run_worktree(tmp_path: Path) -> None:
    """A stale completion must never remove a newer same-ticket worktree."""

    async def scenario() -> tuple[list[Path], Path, Path]:
        supervisor, manager, db, _ = build_supervisor(tmp_path)
        old_path = manager.path_for("HAN-33")
        newer_path = manager.root / "HAN-33-newer"
        newer_path.mkdir(parents=True, exist_ok=True)
        db.connection.execute(
            "INSERT INTO runs(id, ticket, role, status, waiting_on, worktree, final_json, started_at) "
            "VALUES (?, ?, 'builder', 'running', 'none', ?, ?, CURRENT_TIMESTAMP)",
            (
                "harbor-Bookgen-HAN-33-newer",
                "HAN-33",
                str(newer_path),
                json.dumps({"status": "running", "summary": "newer"}),
            ),
        )
        db.connection.commit()
        # The dispatcher now points at the newer session, while the stale
        # final result explicitly names the older durable run.
        supervisor.dispatcher.worktrees["HAN-33"] = newer_path
        stale_wake = Wake(
            ticket={"identifier": "HAN-33"},
            action="reengage",
            resume="harbor-Bookgen-HAN-33-builder",
        )
        await supervisor._handle_runner_result(stale_wake, dict(HAN33_FINAL_JSON))
        await supervisor.exit()
        db.close()
        return manager.removed, old_path, newer_path

    removed, old_path, newer_path = asyncio.run(scenario())
    assert removed == [old_path]
    assert newer_path not in removed


def test_startup_retries_a_verified_cleanup_without_reading_final_json(tmp_path: Path) -> None:
    """A durable receipt retries cleanup even when its final JSON is untrusted."""

    async def scenario() -> tuple[list[Path], dict[str, Any], str]:
        supervisor, manager, db, _ = build_supervisor(tmp_path, with_github=False)
        db.connection.execute(
            "UPDATE runs SET merge_verified = 1, cleanup_at = NULL, final_json = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "status": "completed",
                        "outcome": "pr_open",
                        "pr_url": HAN33_PR,
                        "merge_verified": False,
                        "summary": "untrusted builder artifact",
                    }
                ),
                "harbor-Bookgen-HAN-33-builder",
            ),
        )
        db.connection.commit()
        await supervisor._reconcile_completed_merges()
        row = db.connection.execute(
            "SELECT merge_verified, cleanup_at, final_json FROM runs WHERE ticket = 'HAN-33'"
        ).fetchone()
        digest = supervisor.fleet_digest()
        await supervisor.exit()
        db.close()
        return manager.removed, dict(row), digest

    removed, row, digest = asyncio.run(scenario())
    assert removed == [Path(tmp_path / "worktrees" / "HAN-33")]
    assert row["merge_verified"] == 1
    assert row["cleanup_at"]
    # Cleanup did not replace the artifact while retrying the receipt.
    stored = json.loads(row["final_json"])
    assert stored["outcome"] == "pr_open"
    assert stored["pr_url"] == HAN33_PR
    assert "outcome=pr_open" in digest
    assert f"pr_url={HAN33_PR}" in digest


def test_startup_retries_after_a_transient_cleanup_failure(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[Path], dict[str, Any]]:
        supervisor, manager, db, _ = build_supervisor(tmp_path, with_github=False)
        db.connection.execute(
            "UPDATE runs SET merge_verified = 1, cleanup_at = NULL WHERE id = ?",
            ("harbor-Bookgen-HAN-33-builder",),
        )
        db.connection.commit()
        original_remove = manager.remove
        attempts = 0

        def transient_remove(target: Any) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("device busy")
            original_remove(target)

        manager.remove = transient_remove  # type: ignore[method-assign]
        await supervisor._reconcile_completed_merges()
        pending = db.connection.execute(
            "SELECT cleanup_at FROM runs WHERE id = ?",
            ("harbor-Bookgen-HAN-33-builder",),
        ).fetchone()
        await supervisor._reconcile_completed_merges()
        row = db.connection.execute(
            "SELECT merge_verified, cleanup_at FROM runs WHERE id = ?",
            ("harbor-Bookgen-HAN-33-builder",),
        ).fetchone()
        await supervisor.exit()
        db.close()
        return manager.removed, {"attempts": attempts, "pending": pending[0], **dict(row)}

    removed, row = asyncio.run(scenario())
    assert row["attempts"] == 2
    assert row["pending"] is None
    assert removed == [Path(tmp_path / "worktrees" / "HAN-33")]
    assert row["merge_verified"] == 1
    assert row["cleanup_at"]
