from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harbor.worktree import (
    DiskFloor,
    WorktreeConfinementError,
    WorktreeInstallError,
    WorktreeManager,
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def disposable_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    git(checkout, "checkout", "-b", "main")
    (checkout / "README.md").write_text("main\n", encoding="utf-8")
    git(checkout, "add", "README.md")
    git(checkout, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    git(checkout, "push", "-u", "origin", "main")
    return checkout, remote


class RecordingInstaller:
    """Stands in for `pnpm install` so the test needs no node toolchain."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def __call__(self, path: Path, command: tuple[str, ...]) -> None:
        self.calls.append((path, tuple(command)))


def test_create_uses_fresh_origin_main_and_preserves_main(tmp_path: Path) -> None:
    repo, _ = disposable_repo(tmp_path)
    before_head = git(repo, "rev-parse", "HEAD")
    before_branch = git(repo, "branch", "--show-current")
    before_status = git(repo, "status", "--porcelain")
    installer = RecordingInstaller()
    manager = WorktreeManager(
        repo, root=tmp_path / "wt", disk_usage=lambda _: 5.0, installer=installer
    )

    worktree = manager.create("SAMPLE-42")

    # A fresh worktree has the repo's files but none of its packages; without
    # this the builder's first command fails on a missing module.
    assert installer.calls == [(worktree.path, ("pnpm", "install"))]

    assert worktree.path == (tmp_path / "wt" / "SAMPLE-42").resolve()
    assert git(worktree.path, "branch", "--show-current") == "harbor/SAMPLE-42"
    assert git(worktree.path, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")
    assert git(repo, "rev-parse", "HEAD") == before_head
    assert git(repo, "branch", "--show-current") == before_branch
    assert git(repo, "status", "--porcelain") == before_status
    manager.remove(worktree)
    assert not worktree.path.exists()


def test_worktree_rejects_escape_and_disk_floor(tmp_path: Path) -> None:
    repo, _ = disposable_repo(tmp_path)
    manager = WorktreeManager(repo, root=tmp_path / "wt", disk_usage=lambda _: 4.99)
    with pytest.raises(DiskFloor):
        manager.create("SAMPLE-42")
    with pytest.raises(WorktreeConfinementError):
        manager.path_for("../outside")



def test_install_command_is_configurable_and_can_be_disabled(tmp_path: Path) -> None:
    repo, _ = disposable_repo(tmp_path)
    installer = RecordingInstaller()
    manager = WorktreeManager(
        repo,
        root=tmp_path / "wt",
        disk_usage=lambda _: 30.0,
        install_command="npm ci",
        installer=installer,
    )
    worktree = manager.create("SAMPLE-1")
    assert installer.calls == [(worktree.path, ("npm", "ci"))]

    disabled = RecordingInstaller()
    off = WorktreeManager(
        repo,
        root=tmp_path / "wt2",
        disk_usage=lambda _: 30.0,
        install_command="none",
        installer=disabled,
    )
    off.create("SAMPLE-2")
    assert disabled.calls == []


def test_a_failed_install_is_its_own_error_type(tmp_path: Path) -> None:
    """Rung 0 remediates a failed install; it cannot remediate a git failure."""

    repo, _ = disposable_repo(tmp_path)
    manager = WorktreeManager(
        repo,
        root=tmp_path / "wt",
        disk_usage=lambda _: 30.0,
        install_command=["false"],
    )
    with pytest.raises(WorktreeInstallError) as excinfo:
        manager.create("SAMPLE-42")
    assert excinfo.value.returncode != 0
    assert excinfo.value.command == ("false",)


def test_install_refuses_a_path_outside_the_harbor_root(tmp_path: Path) -> None:
    repo, _ = disposable_repo(tmp_path)
    manager = WorktreeManager(repo, root=tmp_path / "wt", disk_usage=lambda _: 30.0)
    with pytest.raises(WorktreeConfinementError):
        manager.install(tmp_path / "elsewhere")
