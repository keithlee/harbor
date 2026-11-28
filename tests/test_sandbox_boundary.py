"""The builder's sandbox: writable enough to commit, no wider (AC-3 vs AC-4).

A linked worktree's `.git` is a FILE pointing at `<repo>/.git/worktrees/<name>`.
Commits write objects, refs, reflogs and per-worktree metadata there, all
OUTSIDE the worktree, so a plain workspace-write sandbox cannot commit at all
-- the builder reported "Operation not permitted writing shared worktree
metadata" on a real run.  The parts of the repository's git directory that a
commit writes are therefore granted.  The operator's working tree, and the
shared config and hooks that git runs outside the sandbox, are deliberately not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harbor.runner.codex import SHARED_GIT_WRITABLE, CodexRunner


def linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / ".git" / "worktrees" / "SAMPLE-42").mkdir(parents=True)
    worktree = tmp_path / "wt" / "SAMPLE-42"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {repo / '.git' / 'worktrees' / 'SAMPLE-42'}\n", encoding="utf-8")
    return repo, worktree


def config_values(runner: CodexRunner, *, resume: bool = False) -> dict[str, str]:
    command = runner.command("x", run_id="r", resume=resume)
    out: dict[str, str] = {}
    for index, item in enumerate(command):
        if item == "-c" and index + 1 < len(command):
            key, _, value = command[index + 1].partition("=")
            out[key] = value
    return out


def commit_roots(repo: Path) -> list[str]:
    common = (repo / ".git").resolve()
    return [*(str(common / name) for name in SHARED_GIT_WRITABLE), str(common / "worktrees" / "SAMPLE-42")]


def test_the_shared_git_dir_is_discovered_and_its_commit_paths_made_writable(tmp_path: Path) -> None:
    repo, worktree = linked_worktree(tmp_path)
    runner = CodexRunner(worktree, ticket="SAMPLE-42")
    assert runner.git_dir == (repo / ".git").resolve()
    roots = json.loads(config_values(runner)["sandbox_workspace_write.writable_roots"])
    assert roots == commit_roots(repo)
    # The shared config and hooks run outside the sandbox, so they stay read-only.
    assert str((repo / ".git").resolve()) not in roots


def test_the_operators_working_tree_is_never_writable(tmp_path: Path) -> None:
    """AC-3, the part that actually matters: source files stay untouchable."""

    repo, worktree = linked_worktree(tmp_path)
    (repo / "src").mkdir()
    runner = CodexRunner(worktree, ticket="SAMPLE-42")
    roots = json.loads(config_values(runner)["sandbox_workspace_write.writable_roots"])
    for root in roots:
        assert not Path(root).samefile(repo) if Path(root).exists() else True
        assert root != str(repo.resolve()), "the repository root must not be writable"
        assert not str(repo / "src").startswith(root + "/") or root.endswith(".git")


def test_the_builder_gets_network_access_because_it_opens_the_pr(tmp_path: Path) -> None:
    _, worktree = linked_worktree(tmp_path)
    runner = CodexRunner(worktree, ticket="SAMPLE-42")
    assert config_values(runner)["sandbox_workspace_write.network_access"] == "true"


def test_network_access_can_be_disabled(tmp_path: Path) -> None:
    _, worktree = linked_worktree(tmp_path)
    runner = CodexRunner(worktree, ticket="SAMPLE-42", network_access=False)
    assert "sandbox_workspace_write.network_access" not in config_values(runner)


def test_a_plain_directory_git_is_handled(tmp_path: Path) -> None:
    """Not every checkout is a linked worktree."""

    plain = tmp_path / "plain"
    (plain / ".git").mkdir(parents=True)
    runner = CodexRunner(plain, ticket="SAMPLE-1")
    assert runner.git_dir == (plain / ".git").resolve()


def test_a_worktree_without_git_metadata_grants_nothing_extra(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    runner = CodexRunner(bare, ticket="SAMPLE-1")
    assert runner.git_dir is None
    assert runner.writable_roots == ()


def test_extra_writable_roots_are_honoured(tmp_path: Path) -> None:
    repo, worktree = linked_worktree(tmp_path)
    extra = tmp_path / "cache"
    extra.mkdir()
    runner = CodexRunner(worktree, ticket="SAMPLE-42", writable_roots=[extra])
    roots = json.loads(config_values(runner)["sandbox_workspace_write.writable_roots"])
    assert roots == [str(extra.resolve()), *commit_roots(repo)]


def test_the_resume_command_reapplies_config_without_unsupported_flags(tmp_path: Path) -> None:
    """A new resume process needs config overrides but rejects `-C` and `-s`."""

    repo, worktree = linked_worktree(tmp_path)
    runner = CodexRunner(worktree, ticket="SAMPLE-42")
    resumed = runner.command("x", run_id="r", resume=True)
    assert "resume" in resumed
    config = config_values(runner, resume=True)
    assert config["sandbox_workspace_write.network_access"] == "true"
    assert json.loads(config["sandbox_workspace_write.writable_roots"]) == commit_roots(repo)
    assert "-C" not in resumed and "-s" not in resumed
