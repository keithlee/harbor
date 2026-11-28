"""The unblock ladder's protected-path gate against ordinary builder git actions.

A blocker whose builder touched a protected path must never be auto-resolved.
Each test drives a real ``harbor start``-shaped setup: a bare remote, the
operator's checkout, a worktree made by ``WorktreeManager`` with durable
custody, and a ladder whose First Mate is a spy that must never be called.
Every builder action here is one a builder can take inside its own worktree.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor.db import HarborDB
from harbor.recovery import BlockerPolicy
from harbor.unblock import ChangedPathsUnobservable, UnblockLadder, observed_changed_paths
from harbor.worktree import WorktreeError, WorktreeManager

IDENTITY = ("-c", "user.name=Builder", "-c", "user.email=builder@example.invalid")
AGENTS = "# Rules\n\n```yaml\nprotected_paths:\n  - deploy/**\n```\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(key, raising=False)
    remote, checkout = tmp_path / "remote.git", tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True, capture_output=True)
    (checkout / "README.md").write_text("main\n", encoding="utf-8")
    (checkout / "AGENTS.md").write_text(AGENTS, encoding="utf-8")
    (checkout / "deploy").mkdir()
    (checkout / "deploy" / "prod.yml").write_text("replicas: 1\n", encoding="utf-8")
    (checkout / "src").mkdir()
    (checkout / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    (checkout / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(checkout, "add", ".")
    git(checkout, *IDENTITY, "commit", "-q", "-m", "initial")
    git(checkout, "push", "-q", "origin", "HEAD:main")
    db = HarborDB(":memory:")
    manager = WorktreeManager(checkout, root=tmp_path / "wt", install_command="none", custody=db.connection)
    worktree = manager.create("SAMPLE-1")
    calls: list[tuple] = []

    async def first_mate(*args, **kwargs):
        calls.append(args)
        return {"disposition": "resolve", "answer": "A", "citation": "project:bounded-decision",
                "reason": "Preapproved reversible wording within the existing AC."}

    async def resume(run_id, prompt):
        return True

    def ladder(protected=("README.md", "secrets/**")) -> UnblockLadder:
        return UnblockLadder(db, first_mate=first_mate, resumer=resume, repo_path=checkout,
                             protected_paths=protected, policy=BlockerPolicy())

    return SimpleNamespace(tmp=tmp_path, checkout=checkout, db=db, manager=manager, worktree=worktree,
                           wt=worktree.path, calls=calls, ladder=ladder)


def handle(p: SimpleNamespace, *, run: dict | None = None, protected=("README.md", "secrets/**"), ticket: str = "SAMPLE-1"):
    run = {"id": "r", "worktree": str(p.wt)} if run is None else run
    return asyncio.run(p.ladder(protected).handle(
        {"identifier": ticket, "description": "Use A or B; both preapproved. Risk: medium."}, run,
        {"next_action": "Apply the wording.",
         "blocker": {"kind": "product_decision", "question": "Which wording?", "options": ["A", "B"]}}))


def assert_parked(p: SimpleNamespace, result) -> None:
    assert result.reason == "protected path" and result.rung == 2 and not result.resumed
    assert p.calls == [], "a protected change reached First Mate"


def commit(wt: Path, message: str = "builder change") -> None:
    git(wt, *IDENTITY, "commit", "-q", "-m", message)


# --- control: the gate is not simply closed ----------------------------------------------------


def test_an_unprotected_change_still_reaches_first_mate(project) -> None:
    (project.wt / "src" / "app.py").write_text("print('changed')\n")
    (project.wt / "node_modules").mkdir()
    (project.wt / "node_modules" / "dep.js").write_text("ignored install output\n")
    result = handle(project)
    assert result.resumed and len(project.calls) == 1


# --- 1. a rename lists only its new name ------------------------------------------------------


@pytest.mark.parametrize("committed", [False, True])
def test_moving_a_protected_file_away_is_a_protected_change(project, committed) -> None:
    git(project.wt, "mv", "README.md", "moved.md")
    if committed:
        commit(project.wt, "rename")
    assert_parked(project, handle(project))


# --- 2. core.quotePath C-quotes non-ASCII names ------------------------------------------------


@pytest.mark.parametrize("committed", [False, True])
def test_a_non_ascii_protected_path_is_read_unquoted(project, committed) -> None:
    (project.wt / "secrets").mkdir()
    (project.wt / "secrets" / "naïve.txt").write_text("dummy-not-a-secret\n")
    if committed:
        git(project.wt, "add", "secrets")
        commit(project.wt)
    assert_parked(project, handle(project))


def test_observed_paths_are_exact_for_quoted_and_renamed_names(project) -> None:
    git(project.wt, "mv", "README.md", "moved.md")
    (project.wt / "secrets").mkdir()
    (project.wt / "secrets" / "naïve.txt").write_text("dummy\n")
    base = git(project.wt, "rev-parse", "HEAD")
    observed = observed_changed_paths(project.wt, project.checkout, base, ("secrets/**",))
    assert {"README.md", "moved.md", "secrets/naïve.txt"} <= set(observed.paths)


# --- 3. assume-unchanged / skip-worktree hide an edit ----------------------------------------


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_an_edit_hidden_from_git_fails_closed(project, flag) -> None:
    (project.wt / "README.md").write_text("builder edit\n")
    git(project.wt, "update-index", flag, "README.md")
    assert git(project.wt, "status", "--porcelain") == ""  # guard: git really does not see it
    assert_parked(project, handle(project))


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_a_hidden_entry_counts_as_protected_whatever_its_path(project, flag) -> None:
    # Harbor cannot tell what a hidden file holds, so it never auto-resolves.
    (project.wt / "src" / "app.py").write_text("print('hidden edit')\n")
    git(project.wt, "update-index", flag, "src/app.py")
    assert_parked(project, handle(project, protected=("README.md",)))


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_cleanup_keeps_a_worktree_with_hidden_edits(project, flag) -> None:
    (project.wt / "README.md").write_text("builder work git was told not to see\n")
    git(project.wt, "update-index", flag, "README.md")
    with pytest.raises(WorktreeError, match="assume-unchanged or skip-worktree"):
        project.manager.remove(project.worktree)
    assert (project.wt / "README.md").read_text() == "builder work git was told not to see\n"


def test_cleanup_still_removes_a_clean_worktree(project) -> None:
    project.manager.remove(project.worktree)
    assert not project.wt.exists()


# --- 4. a forged base ref ---------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["refs/remotes/origin/main", "refs/heads/main"])
def test_moving_the_base_ref_to_head_does_not_hide_committed_changes(project, ref) -> None:
    (project.wt / "README.md").write_text("builder edit\n")
    git(project.wt, "add", "README.md")
    commit(project.wt)
    git(project.wt, "update-ref", ref, "HEAD")
    assert_parked(project, handle(project))


def test_a_worktree_without_a_recorded_base_fails_closed(project) -> None:
    project.db.connection.execute("DELETE FROM worktree_setup")
    with pytest.raises(ChangedPathsUnobservable, match="no recorded base commit"):
        handle(project)
    assert project.calls == []


# --- 5. a .gitignore the builder wrote -------------------------------------------------------


@pytest.mark.parametrize("ignore", ["secrets/", "*.txt", "*"])
def test_an_ignored_protected_file_still_counts(project, ignore) -> None:
    (project.wt / "secrets").mkdir()
    (project.wt / "secrets" / "key.txt").write_text("dummy-not-a-secret\n")
    with (project.wt / ".gitignore").open("a") as handle_:
        handle_.write(f"{ignore}\n")
    assert git(project.wt, "ls-files", "--others", "--exclude-standard") == ""  # guard: hidden from the old listing
    assert_parked(project, handle(project))


def test_an_ignored_file_under_a_dotted_protected_directory_still_counts(project) -> None:
    workflows = project.wt / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "deploy.yml").write_text("on: push\n")
    with (project.wt / ".gitignore").open("a") as handle_:
        handle_.write(".github/\n")
    assert_parked(project, handle(project, protected=(".github/workflows/**",)))


def test_an_ignored_protected_file_under_a_nested_gitignore_still_counts(project) -> None:
    (project.wt / "secrets").mkdir()
    (project.wt / "secrets" / ".gitignore").write_text("*\n")
    (project.wt / "secrets" / "key.txt").write_text("dummy-not-a-secret\n")
    assert_parked(project, handle(project))


# --- 6. the protected list comes from the operator's checkout at the base --------------------


@pytest.mark.parametrize("edit", ["delete", "rewrite"])
def test_a_builder_cannot_drop_its_own_agents_protections(project, edit) -> None:
    (project.wt / "deploy" / "prod.yml").write_text("replicas: 0\n")
    if edit == "delete":
        (project.wt / "AGENTS.md").unlink()
    else:
        (project.wt / "AGENTS.md").write_text("# Rules\n\n```yaml\nprotected_paths: []\n```\n")
    git(project.wt, "add", "-A")
    commit(project.wt)
    assert_parked(project, handle(project, protected=()))


def test_agents_protections_are_read_at_the_base_not_from_the_operators_working_files(project) -> None:
    (project.wt / "deploy" / "prod.yml").write_text("replicas: 0\n")
    # A stray edit in the operator's checkout is not the reviewed policy either.
    (project.checkout / "AGENTS.md").write_text("no profile\n")
    assert_parked(project, handle(project, protected=()))


# --- 7. no fallback to the operator's checkout ------------------------------------------------


def test_a_run_without_a_recorded_worktree_fails_closed(project) -> None:
    (project.wt / "README.md").write_text("builder edit\n")
    with pytest.raises(ChangedPathsUnobservable, match="no recorded worktree"):
        handle(project, run={"id": "r"})
    assert project.calls == []


def test_a_run_whose_worktree_is_gone_fails_closed(project) -> None:
    moved = project.tmp / "elsewhere"
    shutil.move(str(project.wt), str(moved))
    with pytest.raises(ChangedPathsUnobservable, match="worktree is missing"):
        handle(project)
    assert project.calls == []


def test_a_run_naming_the_operators_checkout_fails_closed(project) -> None:
    (project.wt / "README.md").write_text("builder edit\n")
    with pytest.raises(ChangedPathsUnobservable, match="operator's checkout"):
        handle(project, run={"id": "r", "worktree": str(project.checkout)})
    assert project.calls == []


def test_a_run_naming_another_tickets_clean_worktree_fails_closed(project) -> None:
    (project.wt / "README.md").write_text("builder edit\n")
    other = project.manager.create("SAMPLE-2")
    with pytest.raises(ChangedPathsUnobservable, match="no recorded base commit"):
        handle(project, run={"id": "r", "worktree": str(other.path)})
    assert project.calls == []
