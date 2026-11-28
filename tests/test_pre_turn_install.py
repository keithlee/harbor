"""Harbor reinstalls a worktree's dependencies outside the sandbox before each turn.

Regression for HAN-94: a sandboxed Claude builder merged main (which added a
dependency) and ran ``CI=true pnpm install --frozen-lockfile`` itself. pnpm
deleted node_modules and the sandbox refused to write it back
(``ERR_PNPM_EPERM: reflink to .../resolve_tmp_13868_2/.gitmodules, Operation
not permitted``), leaving the worktree unrunnable. Harbor now runs the
project's install_command before the turn whenever the lockfile changed or
node_modules is missing, and tells the builder not to install.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor import dependencies
from harbor.dependencies import install_environment, pre_turn_installer, prepare_dependencies, record
from harbor.runner.base import BaseRunner, apply_phase4_migration
from harbor.runner.claude import ClaudeRunner
from harbor.runner.codex import CodexRunner

EPERM = ("ERR_PNPM_EPERM  EPERM: operation not permitted, copyfile '/store/v10/files/fd/d8e7' -> "
         "'node_modules/.pnpm/dep@1.0.0/node_modules/dep_tmp_23614/.gitmodules'")
SECRETS = {
    "GITHUB_TOKEN": "ghp-harbor", "GH_TOKEN": "gho-harbor", "LINEAR_API_KEY": "lin-harbor",
    "HARBOR_LINEAR_API_KEY": "lin-harbor-2", "ANTHROPIC_API_KEY": "sk-ant-harbor",
    "OPENAI_API_KEY": "sk-openai-harbor", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-harbor",
    "HARBOR_GITHUB_TOKEN_ENV": "SAMPLE_GH_PAT", "SAMPLE_GH_PAT": "ghp-named-by-pointer",
}


def _installer_script(tmp_path: Path) -> Path:
    """A stand-in for ``pnpm install``: logs each run, dumps its env, writes node_modules."""

    script = tmp_path / "fake-install.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        f"log = pathlib.Path({str(tmp_path / 'events.log')!r})\n"
        "with log.open('a') as handle:\n"
        "    handle.write('install\\n')\n"
        f"pathlib.Path({str(tmp_path / 'install-env.json')!r}).write_text(json.dumps(dict(os.environ)))\n"
        f"if pathlib.Path({str(tmp_path / 'fail-install')!r}).exists():\n"
        f"    sys.stderr.write({EPERM!r} + '\\n')\n"
        "    raise SystemExit(1)\n"
        "modules = pathlib.Path.cwd() / 'node_modules'\n"
        "modules.mkdir(exist_ok=True)\n"
        "(modules / '.modules.yaml').write_text('layoutVersion: 5\\n')\n"
    )
    return script


def _builder_script(tmp_path: Path) -> Path:
    """A fake provider CLI that logs its launch and the prompt it received."""

    path = tmp_path / "builder.py"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"with pathlib.Path({str(tmp_path / 'events.log')!r}).open('a') as handle:\n"
        "    handle.write('launch\\n')\n"
        f"pathlib.Path({str(tmp_path / 'prompt.txt')!r}).write_text(sys.stdin.read())\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'session-1'}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'result': {'status': 'completed', 'outcome': 'pr_open', "
        "'waiting_on': 'none', 'commit_sha': 'c0ffee1', 'summary': 'done'}}), flush=True)\n"
    )
    path.chmod(0o755)
    return path


def _worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "wt" / "SAMPLE-1"
    worktree.mkdir(parents=True)
    (worktree / "package.json").write_text(json.dumps({"name": "sample", "dependencies": {"left-pad": "1.3.0"}}))
    (worktree / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\npackages: {left-pad@1.3.0: {}}\n")
    return worktree


def _ledger() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, ticket TEXT, role TEXT, worktree TEXT, pid INTEGER, pgid INTEGER, "
        "status TEXT, waiting_on TEXT, deadline_at TEXT, cost_usd REAL, final_json TEXT, started_at TEXT, finished_at TEXT)"
    )
    connection.commit()
    apply_phase4_migration(connection)
    return connection


class Setup:
    def __init__(self, tmp_path: Path, command: str | None = None) -> None:
        self.tmp = tmp_path
        self.worktree = _worktree(tmp_path)
        self.command = (sys.executable, str(_installer_script(tmp_path))) if command is None else command
        self.ledger = _ledger()
        self.manager = SimpleNamespace(install_command=self.command, root=tmp_path / "wt", install_timeout_seconds=60)

    def installed(self) -> None:
        """The state ``WorktreeManager.create`` leaves: installed, and recorded in Harbor's ledger."""

        modules = self.worktree / "node_modules"
        modules.mkdir(exist_ok=True)
        (modules / ".modules.yaml").write_text("layoutVersion: 5\n")
        record(self.ledger, self.worktree, self.command, status="installed")

    def runner(self, kind: type = CodexRunner, **extra: object) -> CodexRunner:
        options = dict(db=self.ledger, ticket="SAMPLE-1", run_id="run-1", deadline_seconds=30,
                       executable=str(_builder_script(self.tmp)))
        if kind is ClaudeRunner:
            options.update(model="claude-sonnet-5-5", reasoning_effort="medium")
        runner = kind(self.worktree, **{**options, **extra})
        runner.dependency_preparer = pre_turn_installer(self.manager, self.ledger, project="Sample")
        return runner

    def events(self) -> list[str]:
        log = self.tmp / "events.log"
        return log.read_text().splitlines() if log.exists() else []

    def turn(self, runner: CodexRunner | None = None):
        runner = runner or self.runner()
        return asyncio.run(runner.start("Build SAMPLE-1", run_id="run-1"))


def test_a_changed_lockfile_reinstalls_before_the_builder_launches(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.installed()
    # A merge of main adds a dependency: only the lockfile and package.json change.
    (setup.worktree / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\npackages: {left-pad@1.3.0: {}, is-odd@3.0.1: {}}\n")
    result = setup.turn()
    assert result.status == "completed", result.error
    assert setup.events() == ["install", "launch"]
    prompt = (tmp_path / "prompt.txt").read_text()
    assert "because pnpm-lock.yaml changed since Harbor's last install; it succeeded" in prompt
    # The new inputs are now the recorded ones: the next turn does not reinstall.
    setup.turn()
    assert setup.events() == ["install", "launch", "launch"]


def test_an_unchanged_lockfile_does_not_reinstall(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.installed()
    setup.turn()
    setup.turn()
    assert setup.events() == ["launch", "launch"]
    assert "found the dependencies up to date, so no install was needed" in (tmp_path / "prompt.txt").read_text()


@pytest.mark.parametrize("damage", ["deleted", "half-written"])
def test_missing_or_incomplete_node_modules_reinstalls(tmp_path: Path, damage: str) -> None:
    setup = Setup(tmp_path)
    setup.installed()
    if damage == "deleted":
        import shutil
        shutil.rmtree(setup.worktree / "node_modules")
        why = "node_modules is missing"
    else:
        # What the sandboxed pnpm left in HAN-94: node_modules/.pnpm but no finished install.
        (setup.worktree / "node_modules" / ".modules.yaml").unlink()
        (setup.worktree / "node_modules" / ".pnpm").mkdir()
        why = "node_modules is incomplete"
    result = setup.turn()
    assert result.status == "completed", result.error
    assert setup.events() == ["install", "launch"]
    assert why in (tmp_path / "prompt.txt").read_text()


def test_a_failed_install_blocks_with_the_literal_error_and_launches_nothing(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.installed()
    setup.ledger.execute("INSERT INTO runs(id, ticket, role, worktree, status, waiting_on, session_id) "
                         "VALUES ('run-1', 'SAMPLE-1', 'builder', ?, 'waiting', 'review', 'session-1')",
                         (str(setup.worktree),))
    setup.ledger.commit()
    (setup.worktree / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\npackages: {is-odd@3.0.1: {}}\n")
    (tmp_path / "fail-install").write_text("")
    runner = setup.runner()
    result = asyncio.run(runner.resume("run-1", "Review feedback arrived"))
    assert setup.events() == ["install"]  # the builder was never launched
    assert result.status == "blocked" and result.waiting_on == "human"
    assert result.blocker["kind"] == "env_blocker"
    assert EPERM in result.blocker["evidence"]
    assert EPERM in result.summary and "did not start this builder turn" in result.summary
    assert "answer this blocker" in result.blocker["recommendation"] and "SAMPLE-1" in result.blocker["recommendation"]
    row = setup.ledger.execute("SELECT status, terminal_reason, session_id, final_json FROM runs WHERE id='run-1'").fetchone()
    assert (row["status"], row["terminal_reason"], row["session_id"]) == ("blocked", "dependency_install_failed", "session-1")
    assert EPERM in json.loads(row["final_json"])["blocker"]["evidence"]
    # The failure is recorded, so the next turn tries again rather than trusting the new lockfile.
    assert dependencies.recorded(setup.ledger, setup.worktree).status == "failed"
    (tmp_path / "fail-install").unlink()
    result = asyncio.run(runner.resume("run-1", "Install fixed"))
    assert setup.events() == ["install", "install", "launch"]
    assert "because the previous install did not succeed; it succeeded" in (tmp_path / "prompt.txt").read_text()


def test_harbor_secrets_do_not_reach_the_pre_turn_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("NPM_TOKEN", "the-project-registry-token")
    setup = Setup(tmp_path)
    setup.turn()
    env = json.loads((tmp_path / "install-env.json").read_text())
    leaked = {name for name, value in env.items() if value in SECRETS.values()}
    assert leaked == set()
    for name in ("GITHUB_TOKEN", "LINEAR_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SAMPLE_GH_PAT"):
        assert name not in env
    # The install's own needs stay: PATH, HOME, and the project's registry token.
    assert env["PATH"] and env["HOME"] and env["NPM_TOKEN"] == "the-project-registry-token"


def test_the_github_token_named_by_the_transport_is_scrubbed() -> None:
    env = install_environment({"PATH": "/bin", "ACME_GH": "secret", "OTHER": "kept"}, extra_secrets=("ACME_GH",))
    assert env == {"PATH": "/bin", "OTHER": "kept"}


def test_no_install_command_changes_nothing(tmp_path: Path) -> None:
    setup = Setup(tmp_path, command="none")
    assert pre_turn_installer(setup.manager, setup.ledger) is None
    runner = setup.runner()
    assert runner.dependency_preparer is None
    import shutil
    shutil.rmtree(setup.worktree / "node_modules", ignore_errors=True)
    result = setup.turn(runner)
    assert result.status == "completed"
    assert setup.events() == ["launch"]
    assert (tmp_path / "prompt.txt").read_text() == "Build SAMPLE-1"
    tables = {row[0] for row in setup.ledger.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "dependency_installs" not in tables


def test_claude_builders_get_the_same_pre_turn_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def launched(self, context, instruction, *, resume, before_spawn=None):
        seen.append(instruction)
        return SimpleNamespace(status="waiting")

    monkeypatch.setattr(BaseRunner, "_execute", launched)
    setup = Setup(tmp_path)
    setup.installed()
    (setup.worktree / "package.json").write_text(json.dumps({"name": "sample", "dependencies": {"is-odd": "3.0.1"}}))
    runner = setup.runner(ClaudeRunner)
    asyncio.run(runner._execute(runner._new_context("run-1"), "Build SAMPLE-1", resume=False))
    assert setup.events() == ["install"] and len(seen) == 1
    assert "package.json changed since Harbor's last install" in seen[0]
    (tmp_path / "fail-install").write_text("")
    (setup.worktree / "pnpm-lock.yaml").write_text("changed\n")
    result = asyncio.run(runner._execute(runner._new_context("run-1"), "Build SAMPLE-1", resume=False))
    assert result.status == "blocked" and EPERM in result.blocker["evidence"]
    assert len(seen) == 1  # no Claude turn was launched


def test_the_builder_is_told_not_to_install_itself(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.installed()
    setup.turn()
    prompt = (tmp_path / "prompt.txt").read_text()
    assert prompt.startswith("Build SAMPLE-1\n\nHarbor dependencies: before each turn Harbor")
    assert "Do not run install or reinstall commands yourself" in prompt
    assert "deletes or rewrites node_modules" in prompt
    assert "finish the turn: Harbor installs before the next one" in prompt


def test_worktree_creation_records_its_install_so_the_first_turn_does_not_repeat_it(tmp_path: Path) -> None:
    from harbor.worktree import WorktreeManager

    def git(cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    remote, checkout = tmp_path / "remote.git", tmp_path / "checkout"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    git(checkout, "checkout", "-b", "main")
    (checkout / "package.json").write_text(json.dumps({"name": "sample", "dependencies": {"left-pad": "1.3.0"}}))
    (checkout / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (checkout / ".gitignore").write_text("node_modules/\n")
    git(checkout, "add", "-A")
    git(checkout, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-m", "init")
    git(checkout, "push", "-u", "origin", "main")
    ledger = sqlite3.connect(":memory:")
    command = (sys.executable, str(_installer_script(tmp_path)))
    manager = WorktreeManager(checkout, root=tmp_path / "wt", disk_usage=lambda _: 100.0,
                              install_command=list(command), custody=ledger)
    worktree = manager.create("SAMPLE-1")
    assert (tmp_path / "events.log").read_text().splitlines() == ["install"]
    check = asyncio.run(pre_turn_installer(manager, ledger)(worktree.path))
    assert check.action == "current"
    assert (tmp_path / "events.log").read_text().splitlines() == ["install"]


def test_prepare_refuses_a_worktree_outside_harbors_root(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    check = asyncio.run(prepare_dependencies(outside, command=setup.command, connection=setup.ledger, root=tmp_path / "wt"))
    assert check.failed and "refusing to install outside" in check.error
    assert setup.events() == []


def test_a_failed_install_escalates_once_and_is_not_retried_every_poll(tmp_path: Path) -> None:
    """End to end through the supervisor and unblock ladder: one install attempt, no launch, one escalation."""

    from test_blocked_relaunch_loop import _db, _escalations, _human, _LinearWithComments, _poll_and_settle, _ticket
    from harbor.supervisor import Supervisor
    from harbor.unblock import UnblockLadder

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "package.json").write_text(json.dumps({"name": "sample", "dependencies": {"left-pad": "1.3.0"}}))
    (worktree / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (tmp_path / "fail-install").write_text("")
    comment = _human("human-1", "main moved; please merge it and continue", "2026-09-02T00:00:00+00:00")
    ticket = _ticket([comment])
    db = _db(worktree, ticket)
    linear = _LinearWithComments(ticket)

    async def first_mate(prompt: str, **kwargs: object) -> dict[str, str]:
        return {"disposition": "escalate", "reason": "A person must fix the dependency install."}

    runner = CodexRunner(worktree, db=db, ticket="SAMPLE-7", executable=str(_builder_script(tmp_path)), deadline_seconds=30)
    manager = SimpleNamespace(install_command=(sys.executable, str(_installer_script(tmp_path))), root=tmp_path,
                              install_timeout_seconds=60)
    runner.dependency_preparer = pre_turn_installer(manager, db, project="SampleProject")
    supervisor = Supervisor(linear, db=db, runner=runner,
                            unblock_ladder=UnblockLadder(db, linear=linear, first_mate=first_mate, repo_path=worktree))
    supervisor._background_dispatch = True
    events = tmp_path / "events.log"
    try:
        async def scenario() -> None:
            await _poll_and_settle(supervisor)
            assert events.read_text().splitlines() == ["install"], supervisor.last_error
            assert _escalations(db) == [("env_blocker", "open")], supervisor.last_error
            assert len(linear.posted) == 1 and "pre-turn dependency install" in linear.posted[0]
            for _ in range(5):
                await _poll_and_settle(supervisor)
            assert events.read_text().splitlines() == ["install"]
            assert _escalations(db) == [("env_blocker", "open")]
            assert len(linear.posted) == 1
            row = db.connection.execute("SELECT status, terminal_reason, final_json FROM runs WHERE id='run-7'").fetchone()
            assert (row[0], row[1]) == ("blocked", "dependency_install_failed")
            assert EPERM in json.loads(row[2])["blocker"]["evidence"]

            # The operator fixes the install and answers on the ticket: Harbor
            # installs first, then resumes the same session.
            (tmp_path / "fail-install").unlink()
            reply = _human("human-2", "Fixed the registry; continue.", "2026-09-09T00:00:00+00:00")
            linear.ticket = {**linear.ticket, "linear_comments": [*linear.ticket["linear_comments"], reply]}
            await _poll_and_settle(supervisor)
            assert events.read_text().splitlines() == ["install", "install", "launch"], supervisor.last_error

        asyncio.run(scenario())
    finally:
        db.close()


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("command,wired", [(("pnpm", "install"), True), ("none", False)])
def test_the_production_factory_wires_the_pre_turn_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           provider: str, command: object, wired: bool) -> None:
    from test_quota_fallback_switch_factory import project
    from harbor.cli import build_production_dispatcher
    from harbor.db import HarborDB

    other = "claude" if provider == "codex" else "codex"
    config = project(tmp_path, monkeypatch, provider, other)
    db = HarborDB(tmp_path / "harbor.db")
    try:
        manager = SimpleNamespace(install_command=command, root=tmp_path, install_timeout_seconds=60)
        _, _, factory = build_production_dispatcher(config, db, worktree_manager=manager)
        worktree = tmp_path / "SAMPLE-1"
        worktree.mkdir()
        runner = factory(worktree, SimpleNamespace(ticket={"identifier": "SAMPLE-1"}, run_id=None, action="build"))
        assert isinstance(runner, ClaudeRunner if provider == "claude" else CodexRunner)
        assert (runner.dependency_preparer is not None) is wired
    finally:
        db.close()
