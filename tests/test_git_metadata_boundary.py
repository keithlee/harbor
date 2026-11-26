"""A builder must not be able to plant code that Harbor's own git later runs.

Harbor runs git in the project repository outside any sandbox, with the
operator's environment and GitHub token: ``git fetch`` and ``git worktree add``
for every new ticket, ``git status`` when it verifies or removes a worktree.
Git runs programs named by repository metadata: hooks, ``core.fsmonitor``,
``core.sshCommand``, credential helpers, filter drivers.  So:

* the builder's sandbox grants only the git metadata a commit and push write,
  never the shared ``config``, ``hooks/`` or ``info/``; and
* Harbor's own git calls never run repository hooks or fsmonitor, because a
  builder can still write its own worktree's admin directory.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from harbor.runner.claude import ClaudeRunner
from harbor.runner.codex import CodexRunner
from harbor.worktree import WorktreeManager

IMAGE = "mcr.microsoft.com/playwright@sha256:" + "b" * 64
IDENTITY = ("-c", "user.name=Builder", "-c", "user.email=builder@example.invalid")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, WorktreeManager]:
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True, capture_output=True)
    (checkout / "README.md").write_text("main\n", encoding="utf-8")
    git(checkout, "add", "README.md")
    git(checkout, *IDENTITY, "commit", "-q", "-m", "initial")
    git(checkout, "push", "-q", "origin", "HEAD:main")
    manager = WorktreeManager(checkout, root=tmp_path / "wt", install_command="none")
    return checkout, manager


def covered(path: Path, roots: list[str]) -> bool:
    path = path.resolve() if path.exists() else path.parent.resolve() / path.name
    return any(path == Path(root) or Path(root) in path.parents for root in roots)


def builder_roots(worktree: Path) -> dict[str, list[str]]:
    """The writable paths each provider's builder sandbox is actually given."""

    codex = CodexRunner(worktree, ticket="SAMPLE-1")
    argv = codex.command("x", run_id="r")
    legacy = next(item.partition("=")[2] for item in argv if item.startswith("sandbox_workspace_write.writable_roots="))
    playwright = CodexRunner(worktree, ticket="SAMPLE-1", playwright_browser_image=IMAGE)
    profile = next(item for item in playwright.command("x", run_id="r") if item.startswith("permissions.harbor-playwright.filesystem="))
    granted = [part.split('"="write"')[0].lstrip('{ "') for part in profile.partition("=")[2].strip("{}").split(", ")]
    claude = ClaudeRunner(worktree, ticket="SAMPLE-1", model="claude-test", reasoning_effort="high")
    settings = json.loads(claude.command("x", run_id="r")[claude.command("x", run_id="r").index("--settings") + 1])
    return {
        "codex": json.loads(legacy),
        "codex-playwright": granted,
        "claude": settings["sandbox"]["filesystem"]["allowWrite"],
    }


def test_builders_cannot_write_the_shared_git_config_hooks_or_info(project) -> None:
    checkout, manager = project
    worktree = manager.create("SAMPLE-1").path
    common = (checkout / ".git").resolve()
    for provider, roots in builder_roots(worktree).items():
        for planted in ("config", "config.lock", "hooks/post-checkout", "info/attributes", "modules/sub/config"):
            assert not covered(common / planted, roots), f"{provider} builder can write .git/{planted}: {roots}"
        # ...but still everything a commit needs.
        for needed in ("objects/ab/cdef", "refs/heads/harbor/SAMPLE-1", "logs/HEAD", "packed-refs", "worktrees/SAMPLE-1/index"):
            assert covered(common / needed, roots), f"{provider} builder cannot write .git/{needed}: {roots}"


def snapshot(directory: Path) -> dict[Path, tuple[int, int]]:
    out = {}
    for path in directory.rglob("*"):
        if path.is_file():
            info = path.stat()
            out[path] = (info.st_mtime_ns, info.st_size)
    return out


def test_builder_commit_fetch_push_and_branch_cleanup_write_only_granted_paths(project) -> None:
    """Every file a builder's git workflow touches is inside the narrowed roots."""

    checkout, manager = project
    worktree = manager.create("SAMPLE-1").path
    common = (checkout / ".git").resolve()
    roots = CodexRunner(worktree, ticket="SAMPLE-1").writable_roots
    git(worktree, "pack-refs", "--all")  # so deleting a branch rewrites packed-refs
    before = snapshot(common)
    (worktree / "change.txt").write_text("builder\n", encoding="utf-8")
    git(worktree, "add", "change.txt")
    git(worktree, *IDENTITY, "commit", "-q", "-m", "builder change")
    git(worktree, "fetch", "-q", "origin")
    git(worktree, "push", "-q", "origin", "HEAD")
    git(worktree, "checkout", "-q", "-b", "scratch")
    git(worktree, "checkout", "-q", "harbor/SAMPLE-1")
    git(worktree, "branch", "-q", "-D", "scratch")
    after = snapshot(common)
    changed = [path for path, state in after.items() if before.get(path) != state]
    changed += [path for path in before if path not in after]
    assert changed, "the workflow should have written git metadata"
    outside = [str(path.relative_to(common)) for path in changed if not covered(path, list(roots))]
    # Deleting a branch also drops its (never written) section from the
    # shared config; git warns and carries on when that write is refused.
    assert outside in ([], ["config"]), outside


def plant(path: Path, marker: Path, label: str, *, status: int = 0) -> None:
    """A payload that records it ran and otherwise lets git carry on."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho {label} >> '{marker}'\nexit {status}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_harbors_own_git_never_runs_repository_hooks_or_fsmonitor(project, tmp_path: Path, monkeypatch) -> None:
    """A planted hook or fsmonitor must not run when Harbor creates, verifies or removes a worktree."""

    checkout, manager = project
    first = manager.create("SAMPLE-1")
    common = (checkout / ".git").resolve()
    marker = tmp_path / "PAYLOAD-RAN"
    for hook in ("post-checkout", "reference-transaction", "post-index-change"):
        plant(common / "hooks" / hook, marker, hook)
    # A failing fsmonitor makes git fall back to a full scan, so it is silent.
    plant(tmp_path / "fsmonitor.sh", marker, "fsmonitor", status=1)
    git(checkout, "config", "core.fsmonitor", str(tmp_path / "fsmonitor.sh"))
    monkeypatch.setenv("GITHUB_TOKEN", "placeholder-token")
    second = manager.create("SAMPLE-2")
    manager.verify(second)
    manager.remove(first)
    assert not marker.exists(), f"repository code ran in Harbor's git: {marker.read_text()}"
    assert second.path.is_dir() and not first.path.exists()


def test_git_argv_disables_hooks_and_fsmonitor() -> None:
    from harbor.safe_git import git_argv

    argv = git_argv("status")
    assert argv[0] == "git" and argv[-1] == "status"
    assert "core.hooksPath=/dev/null" in argv and "core.fsmonitor=false" in argv
