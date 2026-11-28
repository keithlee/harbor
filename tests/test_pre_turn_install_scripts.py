"""Builder-written install code never runs outside the sandbox by default.

The pre-turn reinstall runs ``install_command`` on files the builder can edit
(package.json lifecycle scripts, .pnpmfile.cjs, .npmrc). Harbor lets install
scripts run only when every dependency input matches the base branch tip, read
by Harbor itself from its own remote, or the inputs of Harbor's last trusted
install. Otherwise it installs with scripts and hooks off, or refuses, unless
the project opted in with ``[builder] dependency_scripts = "always"``.

It also covers the remediation path: a failed pre-turn install must never make
rung-0 remediation run ``pnpm install`` with Harbor's credentials.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor import dependencies
from harbor.dependencies import BaseSource, pre_turn_installer, without_scripts
from harbor.facts.remediation import execute_remediation, remediation_plan
from test_pre_turn_install import SECRETS, fake_manager

NPM = shutil.which("npm")
PNPM = shutil.which("pnpm")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def base_repo(tmp_path: Path, files: dict[str, str]) -> tuple[str, Path]:
    """A bare remote whose main holds ``files``; returns its file:// URL and a fresh worktree copy of main."""

    remote, seed = tmp_path / "remote.git", tmp_path / "seed"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(remote))
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    for path, text in files.items():
        (seed / path).parent.mkdir(parents=True, exist_ok=True)
        (seed / path).write_text(text)
    git(seed, "add", "-A")
    git(seed, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-q", "-m", "base")
    git(seed, "push", "-q", str(remote), "main")
    worktree = tmp_path / "wt" / "SAMPLE-1"
    worktree.parent.mkdir(parents=True)
    git(tmp_path, "clone", "-q", str(remote), str(worktree))
    return remote.resolve().as_uri(), worktree


def prepare(worktree: Path, command, url: str | None, *, policy: str = "base", ledger=None):
    ledger = ledger if ledger is not None else sqlite3.connect(":memory:")
    manager = SimpleNamespace(install_command=command, root=worktree.parent, install_timeout_seconds=120)
    base = BaseSource(url, "main") if url else None
    installer = pre_turn_installer(manager, ledger, base=base, scripts_policy=policy)
    return asyncio.run(installer(worktree)), ledger


@pytest.fixture
def npm_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Offline and quiet: no audit, fund or update checks, and a private cache.
    monkeypatch.setenv("npm_config_cache", str(tmp_path / "npm-cache"))
    for name in ("audit", "fund", "update_notifier"):
        monkeypatch.setenv(f"npm_config_{name}", "false")


def npm_project(postinstall_marker: Path) -> dict[str, str]:
    manifest = {"name": "sample", "version": "1.0.0", "private": True,
                "scripts": {"postinstall": f"node -e \"require('fs').writeFileSync('{postinstall_marker}', 'ran')\""}}
    lock = {"name": "sample", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
            "packages": {"": {"name": "sample", "version": "1.0.0"}}}
    return {"package.json": json.dumps(manifest, indent=2) + "\n", "package-lock.json": json.dumps(lock, indent=2) + "\n"}


@pytest.mark.skipif(NPM is None, reason="needs npm")
def test_base_identical_inputs_run_install_scripts(tmp_path: Path, npm_env: None) -> None:
    marker = tmp_path / "postinstall-ran"
    url, worktree = base_repo(tmp_path, npm_project(marker))
    check, _ = prepare(worktree, ("npm", "install"), url)
    assert check.action == "installed", check.line()
    assert check.scripts is True and "every input matches origin/main at" in check.trust
    assert marker.read_text() == "ran"


@pytest.mark.skipif(NPM is None, reason="needs npm")
def test_builder_modified_inputs_do_not_run_postinstall(tmp_path: Path, npm_env: None) -> None:
    marker, planted = tmp_path / "postinstall-ran", tmp_path / "builder-code-ran"
    url, worktree = base_repo(tmp_path, npm_project(marker))
    # The builder rewrites the postinstall in its own worktree.
    (worktree / "package.json").write_text(npm_project(planted)["package.json"])
    check, _ = prepare(worktree, ("npm", "install"), url)
    assert check.action == "installed", check.line()
    assert check.scripts is False and check.ran_command == ("npm", "install", "--ignore-scripts")
    assert "the builder's inputs differ from origin/main at" in check.trust and "package.json" in check.trust
    assert not planted.exists() and not marker.exists()
    assert "Install scripts were switched off" in dependencies.builder_note(check)


@pytest.mark.skipif(NPM is None, reason="needs npm")
def test_opting_in_runs_scripts_on_builder_modified_inputs(tmp_path: Path, npm_env: None) -> None:
    marker, planted = tmp_path / "postinstall-ran", tmp_path / "builder-code-ran"
    url, worktree = base_repo(tmp_path, npm_project(marker))
    (worktree / "package.json").write_text(npm_project(planted)["package.json"])
    check, _ = prepare(worktree, ("npm", "install"), url, policy="always")
    assert check.action == "installed" and check.scripts is True
    assert planted.read_text() == "ran"


@pytest.mark.skipif(NPM is None, reason="needs npm")
def test_a_forged_local_base_ref_does_not_make_builder_inputs_trusted(tmp_path: Path, npm_env: None) -> None:
    marker, planted = tmp_path / "postinstall-ran", tmp_path / "builder-code-ran"
    url, worktree = base_repo(tmp_path, npm_project(marker))
    (worktree / "package.json").write_text(npm_project(planted)["package.json"])
    # What a sandboxed builder can do to the refs in its own repository.
    git(worktree, "-c", "user.name=B", "-c", "user.email=b@example.test", "commit", "-qam", "evil")
    git(worktree, "update-ref", "refs/remotes/origin/main", "HEAD")
    check, _ = prepare(worktree, ("npm", "install"), url)
    assert check.scripts is False and not planted.exists()


@pytest.mark.skipif(PNPM is None, reason="needs pnpm")
def test_builder_modified_pnpmfile_and_postinstall_do_not_run(tmp_path: Path) -> None:
    marker, planted, hook = tmp_path / "postinstall-ran", tmp_path / "builder-code-ran", tmp_path / "pnpmfile-ran"
    files = npm_project(marker)
    del files["package-lock.json"]
    url, worktree = base_repo(tmp_path, files)
    (worktree / "package.json").write_text(npm_project(planted)["package.json"])
    (worktree / ".pnpmfile.cjs").write_text(f"require('fs').writeFileSync({str(hook)!r}, 'ran')\nmodule.exports = {{}}\n")
    command = ("pnpm", "install", "--store-dir", str(tmp_path / "store"))
    check, _ = prepare(worktree, command, url)
    assert check.action == "installed", check.line()
    assert check.ran_command == command + ("--ignore-scripts", "--ignore-pnpmfile")
    assert not planted.exists() and not hook.exists() and not marker.exists()


@pytest.mark.skipif(PNPM is None, reason="needs pnpm")
def test_base_identical_pnpm_inputs_run_their_postinstall(tmp_path: Path) -> None:
    marker = tmp_path / "postinstall-ran"
    files = npm_project(marker)
    del files["package-lock.json"]
    url, worktree = base_repo(tmp_path, files)
    check, _ = prepare(worktree, ("pnpm", "install", "--store-dir", str(tmp_path / "store")), url)
    assert check.action == "installed" and check.scripts is True, check.line()
    assert marker.read_text() == "ran"


def test_without_a_readable_base_inputs_are_untrusted(tmp_path: Path) -> None:
    worktree = tmp_path / "wt" / "SAMPLE-1"
    worktree.mkdir(parents=True)
    (worktree / "package.json").write_text("{}")
    command = (str(fake_manager(tmp_path)), "install")
    check, _ = prepare(worktree, command, (tmp_path / "missing.git").as_uri())
    assert check.scripts is False and "could not read the base branch" in check.trust
    assert check.ran_command[-2:] == ("--ignore-scripts", "--ignore-pnpmfile")


@pytest.mark.parametrize("command,expected", [
    (("pnpm", "install"), ("pnpm", "install", "--ignore-scripts", "--ignore-pnpmfile")),
    (("pnpm", "install", "--frozen-lockfile"), ("pnpm", "install", "--frozen-lockfile", "--ignore-scripts", "--ignore-pnpmfile")),
    (("pnpm",), ("pnpm", "--ignore-scripts", "--ignore-pnpmfile")),
    (("corepack", "pnpm", "install"), ("corepack", "pnpm", "install", "--ignore-scripts", "--ignore-pnpmfile")),
    (("/opt/bin/npm", "ci"), ("/opt/bin/npm", "ci", "--ignore-scripts")),
    (("npm", "install"), ("npm", "install", "--ignore-scripts")),
    (("bun", "install"), ("bun", "install", "--ignore-scripts")),
    (("yarn", "install"), ("yarn", "install", "--ignore-scripts")),
    (("yarn",), ("yarn", "--ignore-scripts")),
    (("uv", "sync", "--frozen"), ("uv", "sync", "--frozen", "--no-install-project", "--no-install-workspace",
                                  "--no-install-local", "--no-build")),
])
def test_each_package_manager_gets_its_own_no_scripts_flags(command, expected) -> None:
    assert without_scripts(command, {"package.json": "file:x"}) == (expected, "")


@pytest.mark.parametrize("command,inputs,why", [
    (("yarn", "install"), {".yarnrc.yml": "file:x"}, "Yarn loads .yarnrc.yml"),
    (("yarn", "install"), {".yarn/releases/yarn-4.cjs": "file:x"}, "Yarn loads .yarn/releases/yarn-4.cjs"),
    (("make", "setup"), {}, "no way to switch off install scripts for `make setup`"),
    (("poetry", "install"), {}, "no way to switch off install scripts"),
    (("pnpm", "run", "setup"), {}, "no way to switch off install scripts"),
])
def test_managers_whose_scripts_cannot_be_switched_off_are_refused(command, inputs, why) -> None:
    effective, reason = without_scripts(command, inputs)
    assert effective is None and why in reason


def test_a_refused_install_blocks_without_running_anything(tmp_path: Path) -> None:
    worktree = tmp_path / "wt" / "SAMPLE-1"
    worktree.mkdir(parents=True)
    (worktree / "requirements.txt").write_text("evil @ file:///tmp/evil\n")
    tool = fake_manager(tmp_path, "make")
    check, ledger = prepare(worktree, (str(tool), "setup"), None)
    assert check.failed and not check.ran and not (tmp_path / "events.log").exists()
    assert 'dependency_scripts = "always"' in check.error
    assert dependencies.blocker(check, worktree, "SAMPLE-1")["kind"] == "dependency_install"
    assert dependencies.recorded(ledger, worktree).status == "refused"


def test_the_dependency_scripts_setting_fails_closed() -> None:
    from harbor.config.workflow import builder_dependency_scripts

    assert builder_dependency_scripts({}) == "base"
    assert builder_dependency_scripts({"builder": {"dependency_scripts": "always"}}) == "always"
    with pytest.raises(ValueError, match="dependency_scripts"):
        builder_dependency_scripts({"builder": {"dependency_scripts": True}})


# --- B1: a failed pre-turn install never reaches rung-0 remediation --------------------------

NODE_GYP = "Error: Cannot find module 'node-gyp/bin/node-gyp.js'"


def test_remediation_never_acts_on_a_pre_turn_install_blocker() -> None:
    blocker = {"kind": "dependency_install", "question": "install failed", "evidence": NODE_GYP}
    assert remediation_plan(blocker) is None
    result = asyncio.run(execute_remediation(blocker, executor=lambda command: pytest.fail("ran " + str(command))))
    assert not result.attempted


def linked_base_repo(tmp_path: Path, files: dict[str, str]) -> tuple[str, Path]:
    """Like ``base_repo``, but the worktree is a linked ``git worktree`` of a clone, as Harbor makes it."""

    (tmp_path / "remote-side").mkdir()
    url, _ = base_repo(tmp_path / "remote-side", files)
    checkout, worktree = tmp_path / "checkout", tmp_path / "wt" / "SAMPLE-1"
    git(tmp_path, "clone", "-q", url, str(checkout))
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(checkout, "worktree", "add", "-q", "-b", "harbor/SAMPLE-1", str(worktree), "origin/main")
    return url, worktree


def test_rung_zero_never_runs_a_package_manager_for_cannot_find_module(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    fake_manager(tmp_path)  # tmp_path/bin/pnpm, first on PATH: any `pnpm` run would be logged
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    blocker = {"kind": "env_blocker", "evidence": NODE_GYP}
    ledger = sqlite3.connect(":memory:")
    worktree = tmp_path / "linked"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere\n")
    (worktree / "package.json").write_text("{}")
    dependencies.record(ledger, worktree, ("pnpm", "install"), status="installed", trusted=True)
    # No ledger, Harbor's own directory, or a directory that is not a linked worktree: refused.
    for cwd, store in ((worktree, None), (None, ledger), (tmp_path, ledger)):
        result = asyncio.run(execute_remediation(blocker, cwd=cwd, ledger=store))
        assert result.attempted and not result.fixed and result.error
    result = asyncio.run(execute_remediation(blocker, cwd=worktree, ledger=ledger))
    assert result.fixed and result.plan.commands == (("harbor", "schedule-dependency-reinstall"),)
    assert not (tmp_path / "events.log").exists()  # nothing was installed now
    assert dependencies.recorded(ledger, worktree).status == "stale"  # the next turn reinstalls


@pytest.mark.skipif(NPM is None, reason="needs npm")
def test_a_builder_postinstall_plus_a_reported_missing_module_never_runs_the_script(
        tmp_path: Path, npm_env: None) -> None:
    """A builder writes a postinstall, then reports "Cannot find module" itself. Rung 0 must not run it,
    and the reinstall it schedules must run with scripts off."""

    from harbor.db import HarborDB
    from harbor.unblock import UnblockLadder

    marker, planted = tmp_path / "postinstall-ran", tmp_path / "builder-code-ran"
    url, worktree = linked_base_repo(tmp_path, npm_project(marker))
    command = ("npm", "install")
    db = HarborDB(tmp_path / "harbor.db")
    resumed: list[str] = []

    async def resume(run_id: str, prompt: str) -> bool:
        resumed.append(run_id)
        return True

    try:
        # What WorktreeManager.create leaves: the base branch's inputs, installed and trusted.
        dependencies.record(db.connection, worktree, command, status="installed", trusted=True)
        (worktree / "package.json").write_text(npm_project(planted)["package.json"])
        final = {"status": "blocked", "summary": "tests fail",
                 "blocker": {"kind": "env_blocker", "question": "Error: Cannot find module 'left-pad'"}}
        result = asyncio.run(UnblockLadder(db, resumer=resume, repo_path=worktree).handle(
            {"identifier": "SAMPLE-1"}, {"id": "run-1", "worktree": str(worktree)}, final))
        assert result.rung == 0 and result.resumed and resumed == ["run-1"]
        assert result.citation == "deterministic:missing_module"
        assert not planted.exists() and not marker.exists()  # rung 0 itself ran nothing
        # The resumed turn's pre-turn install: the project's command, scripts off on builder-changed inputs.
        manager = SimpleNamespace(install_command=command, root=worktree.parent, install_timeout_seconds=120)
        check = asyncio.run(pre_turn_installer(manager, db, base=BaseSource(url, "main"))(worktree))
        assert check.action == "installed", check.line()
        assert "reported a missing module" in check.line()
        assert check.scripts is False and check.ran_command == ("npm", "install", "--ignore-scripts")
        assert not planted.exists() and not marker.exists()
    finally:
        db.close()


def test_gh_auth_status_cannot_be_redirected_to_a_planted_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "wt" / "SAMPLE-1"
    planted_bin, harbor_bin = worktree / "node_modules" / ".bin", tmp_path / "harbor-bin"
    for directory, label in ((planted_bin, "planted"), (worktree, "planted-cwd"), (harbor_bin, "harbor")):
        directory.mkdir(parents=True, exist_ok=True)
        gh = directory / "gh"
        gh.write_text(f"#!/bin/sh\necho \"$@\" > {tmp_path / label}\n")
        gh.chmod(0o755)
    monkeypatch.chdir(worktree)
    # A builder-influenced PATH: a worktree entry, a relative entry and an empty entry come first.
    monkeypatch.setenv("PATH", os.pathsep.join([str(planted_bin), ".", "", str(harbor_bin), os.environ["PATH"]]))
    result = asyncio.run(execute_remediation({"kind": "github_transport"}, cwd=worktree))
    assert result.fixed, result.error
    assert (tmp_path / "harbor").read_text().strip() == "auth status --hostname github.com"
    assert not (tmp_path / "planted").exists() and not (tmp_path / "planted-cwd").exists()
    assert result.outputs[0].args[0] == str(harbor_bin / "gh")


def test_a_failed_install_that_prints_cannot_find_module_does_not_run_remediation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reviewer's probe: the pre-turn install fails with "Cannot find module", GITHUB_TOKEN is set,
    and the real ladder (default remediation executor) handles the blocker. pnpm must not run again."""

    from test_blocked_relaunch_loop import _db, _escalations, _human, _LinearWithComments, _poll_and_settle, _ticket
    from test_pre_turn_install import _builder_script
    from harbor.runner.codex import CodexRunner
    from harbor.supervisor import Supervisor
    from harbor.unblock import UnblockLadder

    monkeypatch.setenv("GITHUB_TOKEN", "ghp-harbor")
    fake_manager(tmp_path)
    # Rung-0 remediation runs a bare `pnpm install`: make that resolve to the logging fake.
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    # A real linked worktree, so remediation's own confinement would let it run.
    repo, worktree = tmp_path / "repo", tmp_path / "wt"
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    (repo / "package.json").write_text(json.dumps({"name": "sample", "dependencies": {"left-pad": "1.3.0"}}))
    (repo / ".gitignore").write_text("node_modules/\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-q", "-m", "base")
    git(repo, "worktree", "add", "-q", "-b", "harbor/SAMPLE-7", str(worktree), "main")
    (tmp_path / "fail-install").write_text(NODE_GYP)
    ticket = _ticket([_human("human-1", "please continue", "2026-09-02T00:00:00+00:00")])
    db = _db(worktree, ticket)
    linear = _LinearWithComments(ticket)

    async def first_mate(prompt: str, **kwargs: object) -> dict[str, str]:
        return {"disposition": "escalate", "reason": "A person must fix the install."}

    runner = CodexRunner(worktree, db=db, ticket="SAMPLE-7", executable=str(_builder_script(tmp_path)), deadline_seconds=30)
    manager = SimpleNamespace(install_command=("pnpm", "install"), root=tmp_path, install_timeout_seconds=60)
    runner.dependency_preparer = pre_turn_installer(manager, db, project="SampleProject")
    supervisor = Supervisor(linear, db=db, runner=runner,
                            unblock_ladder=UnblockLadder(db, linear=linear, first_mate=first_mate, repo_path=worktree))
    supervisor._background_dispatch = True
    try:
        async def scenario() -> None:
            for _ in range(3):
                await _poll_and_settle(supervisor)

        asyncio.run(scenario())
        # One pnpm run: Harbor's pre-turn install (scripts off, secrets removed). None from remediation.
        args = [json.loads(line) for line in (tmp_path / "args.log").read_text().splitlines()]
        assert args == [["install", "--ignore-scripts", "--ignore-pnpmfile"]], args
        env = json.loads((tmp_path / "install-env.json").read_text())
        assert "GITHUB_TOKEN" not in env
        assert _escalations(db) == [("dependency_install", "open")], supervisor.last_error
    finally:
        db.close()
