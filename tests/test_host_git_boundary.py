"""Harbor's host-side git must not run code a builder planted, nor hand it Harbor's secrets.

Harbor runs git on the host, outside any sandbox, in directories a builder
controls, with Harbor's environment (GitHub token, Linear and model keys).  A
builder can rewrite its worktree's ``.git`` file to ``gitdir: <worktree>/
planted-gitdir`` whose ``config`` sets ``core.fsmonitor`` or a filter driver;
git then runs that program for Harbor.  These tests plant exactly that (the
reviewer's reproduction), plus tampered ``commondir``/``gitdir`` pointers, and
drive every Harbor path that runs git in a builder worktree.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import harbor.safe_git as safe_git
from harbor.safe_git import UnsafeGitDirectory, git_env, run_worktree_git, worktree_git_dirs
from harbor.db import HarborDB
from harbor.unblock import UnblockLadder
from harbor.worktree import WorktreeConfinementError, WorktreeError, WorktreeManager

TOKEN = "ghp_planted_gitdir_probe_token"
SECRETS = {
    "GITHUB_TOKEN": TOKEN, "GH_TOKEN": TOKEN, "LINEAR_API_KEY": "lin_probe",
    "ANTHROPIC_API_KEY": "sk-ant-probe", "OPENAI_API_KEY": "sk-probe",
}
IDENTITY = ("-c", "user.name=Builder", "-c", "user.email=builder@example.invalid")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    # A private HOME: no operator global config leaks in, and tests can add one.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(key, raising=False)
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    remote, checkout = tmp_path / "remote.git", tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True, capture_output=True)
    (checkout / "README.md").write_text("main\n", encoding="utf-8")
    git(checkout, "add", "README.md")
    git(checkout, *IDENTITY, "commit", "-q", "-m", "initial")
    git(checkout, "push", "-q", "origin", "HEAD:main")
    # Durable custody, as ``harbor start`` has: the ladder reads each
    # worktree's recorded base commit from it.
    db = HarborDB(":memory:")
    manager = WorktreeManager(checkout, root=tmp_path / "wt", install_command="none", custody=db.connection)
    worktree = manager.create("SAMPLE-1")
    marker = tmp_path / "PAYLOAD-RAN"
    payload = tmp_path / "payload.sh"
    # Records that it ran and whether it saw the token; then behaves as a
    # pass-through clean filter / failing fsmonitor so git carries on.
    payload.write_text(f"#!/bin/sh\necho \"$1 token=${{GITHUB_TOKEN:-absent}} linear=${{LINEAR_API_KEY:-absent}}\" >> '{marker}'\ncat\n",
                       encoding="utf-8")
    payload.chmod(0o755)
    return SimpleNamespace(tmp=tmp_path, home=home, checkout=checkout, manager=manager, worktree=worktree, db=db,
                           head=git(worktree.path, "rev-parse", "HEAD"), marker=marker, payload=payload)


def plant_gitdir(p: SimpleNamespace) -> Path:
    """The reviewer's attack: ``.git`` -> a git dir inside the worktree whose config runs the payload."""

    wt = p.worktree.path
    planted = wt / "planted-gitdir"
    subprocess.run(["git", "clone", "-q", "--bare", str(p.checkout), str(planted)], check=True, capture_output=True)
    admin = Path((wt / ".git").read_text().split(":", 1)[1].strip())
    shutil.copy(admin / "index", planted / "index")
    (planted / "HEAD").write_text("ref: refs/heads/harbor/SAMPLE-1\n")
    for key, value in (("core.bare", "false"), ("core.fsmonitor", str(p.payload)),
                       ("filter.evil.clean", f"{p.payload} clean")):
        git(p.tmp, "--git-dir", str(planted), "config", key, value)
    (planted / "info").mkdir(exist_ok=True)
    (planted / "info" / "attributes").write_text("* filter=evil\n")
    (wt / ".git").write_text(f"gitdir: {planted}\n")
    (wt / "README.md").write_text("builder edit\n")  # stat-dirty, so git cleans it
    return planted


def ran(p: SimpleNamespace) -> str:
    return p.marker.read_text() if p.marker.exists() else ""


@pytest.fixture
def spy(monkeypatch) -> list[dict]:
    """Every environment Harbor's safe_git hands a git subprocess."""

    calls: list[dict] = []
    real = subprocess.run

    def run(argv, *args, **kwargs):
        if argv and argv[0] == "git":
            calls.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(safe_git.subprocess, "run", run)
    return calls


def assert_clean_env(calls: list[dict], *, pinned: bool = True) -> None:
    assert calls, "Harbor ran no git at all"
    for call in calls:
        env, argv = call["env"], call["argv"]
        assert env, f"git ran with the inherited environment: {argv}"
        leaked = {key for key in env if key in SECRETS or TOKEN in env[key]}
        assert not leaked, f"{argv} got Harbor's secrets: {leaked}"
        assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
        assert {k for k in env if k.startswith("GIT_")} <= {
            "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT", "GIT_NO_REPLACE_OBJECTS",
            "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"}
        assert "core.hooksPath=/dev/null" in argv and "core.fsmonitor=false" in argv
        assert "--no-replace-objects" in argv and "protocol.ext.allow=never" in argv
        if pinned:
            assert {"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"} <= set(env), argv


# --- the attack is real --------------------------------------------------------------


def test_reviewers_planted_gitdir_runs_code_with_the_token_in_unhardened_git(project) -> None:
    """Guard: the fixture reproduces the defect, so the tests below exercise a real vector."""

    plant_gitdir(project)
    for argv in (["git", "diff", "--name-only", "HEAD"],
                 # #59's first hardening: fsmonitor and hooks off, but the planted config still applies.
                 ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "status", "--porcelain"]):
        subprocess.run(argv, cwd=project.worktree.path, capture_output=True, text=True)
    assert f"token={TOKEN}" in ran(project)
    assert "clean" in ran(project)


# --- unblock ladder ------------------------------------------------------------------


def _ladder_args(worktree: Path):
    return ({"identifier": "SAMPLE-1", "description": "Preapproved wording."}, {"id": "r", "worktree": str(worktree)},
            {"blocker": {"kind": "product_decision", "question": "Which wording?", "options": ["A", "B"]}})


async def _forbidden(*args, **kwargs):
    raise AssertionError("must not reach model resolution")


def test_unblock_ladder_refuses_a_planted_gitdir_without_running_it(project) -> None:
    plant_gitdir(project)
    ladder = UnblockLadder(project.db, first_mate=_forbidden, repo_path=project.checkout, protected_paths=("README.md",))
    with pytest.raises(UnsafeGitDirectory, match="points outside"):
        asyncio.run(ladder.handle(*_ladder_args(project.worktree.path)))
    assert ran(project) == ""


def test_unblock_ladder_git_gets_no_secrets_even_for_the_operators_own_filters(project, spy) -> None:
    """A legitimate worktree is still inspected, with an environment holding no credential."""

    # The operator's own global filter, applied by the builder's .gitattributes.
    (project.home / ".gitconfig").write_text(f"[filter \"probe\"]\n\tclean = {project.payload} clean\n")
    (project.worktree.path / ".gitattributes").write_text("* filter=probe\n")
    (project.worktree.path / "README.md").write_text("builder edit\n")
    ladder = UnblockLadder(project.db, first_mate=_forbidden, repo_path=project.checkout, protected_paths=("README.md",))
    result = asyncio.run(ladder.handle(*_ladder_args(project.worktree.path)))
    assert result.reason == "protected path" and not result.resumed
    assert "clean token=absent linear=absent" in ran(project) and TOKEN not in ran(project)
    # Git in the worktree is pinned to its verified git dirs; reading the
    # protected list from the operator's checkout at the base is not in a
    # worktree, but is just as credential-free.
    assert_clean_env([call for call in spy if "-C" not in call["argv"]])
    assert_clean_env([call for call in spy if "-C" in call["argv"]], pinned=False)


@pytest.mark.parametrize("tamper", ["commondir", "other_ticket", "config_worktree", "symlink"])
def test_unblock_ladder_refuses_tampered_worktree_pointers(project, tamper) -> None:
    wt = project.worktree.path
    admin = Path((wt / ".git").read_text().split(":", 1)[1].strip())
    if tamper == "commondir":
        planted = plant_gitdir(project)
        (wt / ".git").write_text(f"gitdir: {admin}\n")
        (admin / "commondir").write_text(str(planted) + "\n")
    elif tamper == "other_ticket":
        other = project.manager.create("SAMPLE-2")
        (wt / ".git").write_text((other.path / ".git").read_text())
    elif tamper == "config_worktree":
        (admin / "config.worktree").write_text(f"[core]\n\tfsmonitor = {project.payload}\n")
    else:
        (wt / ".git").unlink()
        (wt / ".git").symlink_to(admin)
    ladder = UnblockLadder(project.db, first_mate=_forbidden, repo_path=project.checkout, protected_paths=("README.md",))
    with pytest.raises(UnsafeGitDirectory):
        asyncio.run(ladder.handle(*_ladder_args(wt)))
    assert ran(project) == ""


# --- worktree manager: attach, verify, remove, retry ---------------------------------


def test_worktree_manager_refuses_a_planted_gitdir_in_verify_and_remove(project) -> None:
    plant_gitdir(project)
    with pytest.raises(WorktreeConfinementError, match="repository mismatch"):
        project.manager.verify(project.worktree)
    with pytest.raises(WorktreeConfinementError, match="repository mismatch"):
        project.manager.remove(project.worktree)
    assert project.worktree.path.is_dir() and ran(project) == ""


def test_worktree_manager_attach_and_workflow_check_refuse_a_planted_gitdir(project, monkeypatch, spy) -> None:
    from harbor import cli, workflow_cli

    db = SimpleNamespace(connection=sqlite3.connect(":memory:"))
    db.connection.execute("CREATE TABLE runs (id TEXT, ticket TEXT, worktree TEXT, started_at TEXT, finished_at TEXT)")
    db.connection.execute("INSERT INTO runs VALUES ('r','SAMPLE-1',?, '1', '2')", (str(project.worktree.path),))
    monkeypatch.setattr(cli, "_worktree_root", lambda config: project.tmp / "wt")
    config = SimpleNamespace(repo_path=project.checkout, name="checkout")
    worktree, head = workflow_cli._attach_retained_worktree(config, db, "SAMPLE-1")
    assert head == project.head
    assert_clean_env(spy)  # every call was in the worktree, so all are pinned
    plant_gitdir(project)
    spy.clear()
    with pytest.raises(ValueError, match="repository mismatch"):
        workflow_cli._attach_retained_worktree(config, db, "SAMPLE-1")
    # Refused by reading files: git never ran in the planted directory.
    assert not [c for c in spy if "-C" not in c["argv"]]
    assert ran(project) == ""


def test_worktree_manager_git_in_worktrees_is_pinned_and_secret_free(project, spy) -> None:
    project.manager.verify(project.worktree)
    worktree_calls = [c for c in spy if "GIT_WORK_TREE" in c["env"]]
    assert_clean_env(worktree_calls)
    repo_calls = [c for c in spy if "GIT_WORK_TREE" not in c["env"]]
    assert_clean_env(repo_calls, pinned=False)
    assert all(c["env"]["GIT_DIR"].endswith("/worktrees/SAMPLE-1") for c in worktree_calls)


def test_worktree_create_keeps_credentials_only_for_fetch_and_worktree_add(project, spy) -> None:
    project.manager.create("SAMPLE-3")
    network = [c for c in spy if "fetch" in c["argv"] or "add" in c["argv"]]
    assert len(network) == 2
    for call in network:
        assert call["env"].get("GH_TOKEN") == TOKEN  # the operator's credential helper may need it
        assert "LINEAR_API_KEY" not in call["env"] and "ANTHROPIC_API_KEY" not in call["env"]
        assert not {k for k in call["env"] if k.startswith("GIT_")} - {"GIT_TERMINAL_PROMPT", "GIT_NO_REPLACE_OBJECTS"}
    assert_clean_env([c for c in spy if c not in network], pinned=False)


# --- watchdog ------------------------------------------------------------------------


def test_watchdog_head_refuses_a_planted_gitdir_and_needs_the_repository(project, spy) -> None:
    from harbor.watchdog import _head

    assert _head(str(project.worktree.path), project.checkout) == project.head
    assert_clean_env(spy)
    assert _head(str(project.worktree.path), None) == ""  # nothing to verify against: no git at all
    plant_gitdir(project)
    assert _head(str(project.worktree.path), project.checkout) == ""
    assert ran(project) == ""


# --- post-merge cleanup ---------------------------------------------------------------


def _cleanup_supervisor(project) -> SimpleNamespace:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE escalations (ticket TEXT, status TEXT)")
    workflow = SimpleNamespace(config={"cleanup": {"mode": "after_merge", "delay_seconds": 0}})
    run = {"id": "r", "status": "completed", "finished_at": "2020-01-01T00:00:00Z", "pid": None, "pgid": None}
    return SimpleNamespace(dispatcher=SimpleNamespace(workflow=workflow), _ticket_key=lambda t: t,
                           db=SimpleNamespace(connection=db), _latest_run=lambda ticket: run,
                           worktree_manager=project.manager)


def test_cleanup_reads_head_only_from_a_verified_worktree(project, monkeypatch, spy) -> None:
    import harbor.workflow_lifecycle as lifecycle

    async def handoff(supervisor, wake, result):
        return {"facts": {"merged": True}, "terminal": True, "head_sha": project.head}

    async def no_event(*args, **kwargs):
        return []

    monkeypatch.setattr(lifecycle, "handoff", handoff)
    monkeypatch.setattr(lifecycle, "execute_event", no_event)
    supervisor = _cleanup_supervisor(project)
    wake = SimpleNamespace(ticket="SAMPLE-1")
    allowed = lambda: asyncio.run(lifecycle.cleanup_allowed(supervisor, wake, {}, project.worktree.path, "r"))
    assert allowed() is True
    assert_clean_env(spy)
    plant_gitdir(project)  # its HEAD names the same commit, so only verification can refuse it
    assert allowed() is False
    assert ran(project) == ""
    supervisor.worktree_manager = None
    assert allowed() is False  # no repository to verify against: keep the worktree


# --- safe_git unit -------------------------------------------------------------------


def test_git_env_keeps_only_what_local_git_needs() -> None:
    environ = {**SECRETS, "PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "STRIPE_SECRET": "x",
               "GIT_DIR": "/evil", "GIT_CONFIG_PARAMETERS": "'core.fsmonitor=/evil'", "GIT_SSH_COMMAND": "evil"}
    local = git_env(environ=environ)
    assert local == {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
                     "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
    network = git_env(network=True, environ=environ)
    assert network["GH_TOKEN"] == TOKEN and network["STRIPE_SECRET"] == "x"
    assert not {"LINEAR_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GIT_DIR", "GIT_CONFIG_PARAMETERS",
                "GIT_SSH_COMMAND"} & set(network)


def test_git_argv_blanks_transport_programs_only_for_local_commands() -> None:
    local, network = safe_git.git_argv("status"), safe_git.git_argv("fetch", network=True)
    assert "core.sshCommand=" in local and "core.sshCommand=" not in network
    for argv in (local, network):
        assert argv[:2] == ["git", "--no-replace-objects"] and "protocol.ext.allow=never" in argv


def test_worktree_git_dirs_accepts_the_repository_itself_and_its_linked_worktrees(project) -> None:
    assert worktree_git_dirs(project.checkout, project.checkout)[1] == (project.checkout / ".git").resolve()
    work, admin, common = worktree_git_dirs(project.worktree.path, project.checkout)
    assert (work, admin.parent, common) == (project.worktree.path, common / "worktrees", (project.checkout / ".git").resolve())
    with pytest.raises(UnsafeGitDirectory):
        worktree_git_dirs(project.tmp / "home", project.checkout)
    assert run_worktree_git(project.worktree.path, project.checkout, "rev-parse", "HEAD").stdout.strip() == project.head


# --- B1: a planted submodule ------------------------------------------------------------


def plant_submodule(p: SimpleNamespace, *, committed: bool) -> Path:
    """A nested repository, added as a gitlink, whose own config runs the payload as a clean filter.

    Its ``.gitmodules`` entry sets ``ignore = none``, which overrides a
    ``diff.ignoreSubmodules`` config default, so only the command-line
    ``--ignore-submodules`` holds.  A file inside it is edited afterwards, so
    a ``status`` or ``diff`` that checks the submodule's work tree runs git in
    it, and that git runs the builder's filter.
    """

    wt = p.worktree.path
    sub = wt / "sub"
    git(wt, "init", "-q", "sub")
    (sub / "f.txt").write_text("one\n")
    git(sub, "add", "f.txt")
    git(sub, *IDENTITY, "commit", "-q", "-m", "sub")
    git(sub, "config", "filter.evil.clean", f"{p.payload} clean")
    (sub / ".git" / "info").mkdir(exist_ok=True)
    (sub / ".git" / "info" / "attributes").write_text("* filter=evil\n")
    (wt / ".gitmodules").write_text('[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n\tignore = none\n')
    git(wt, "add", ".gitmodules", "sub")
    if committed:
        git(wt, *IDENTITY, "commit", "-q", "-m", "add sub")
    (sub / "f.txt").write_text("two\n")
    return sub


def test_planted_submodule_runs_code_without_the_command_line_ignore(project) -> None:
    """Guard: the submodule vector is real, and a config default alone does not stop it."""

    plant_submodule(project, committed=True)
    subprocess.run(["git", "-c", "diff.ignoreSubmodules=all", "status", "--porcelain"],
                   cwd=project.worktree.path, capture_output=True, text=True)
    assert "clean" in ran(project)


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("args", [
    ("status", "--porcelain"),
    ("status", "--porcelain", "--ignored", "--untracked-files=all"),
    ("diff", "--name-only", "HEAD"),
    ("diff", "--ignore-submodules=none", "--name-only", "HEAD"),  # a caller cannot opt back in
])
def test_harbor_git_never_enters_a_planted_submodule(project, committed, args) -> None:
    plant_submodule(project, committed=committed)
    out = run_worktree_git(project.worktree.path, project.checkout, *args, check=True).stdout
    assert ran(project) == ""
    if not committed:  # an added gitlink is still reported, without entering it
        assert "sub" in out


def test_verify_and_remove_never_enter_a_planted_submodule(project) -> None:
    from harbor.worktree import WorktreeError

    plant_submodule(project, committed=False)
    with pytest.raises(WorktreeError, match="dirty"):
        project.manager.verify(project.worktree)
    with pytest.raises(WorktreeError, match="submodule or nested git repository"):
        project.manager.remove(project.worktree)
    assert ran(project) == ""
    git(project.worktree.path, *IDENTITY, "commit", "-q", "-m", "add sub")
    with pytest.raises(WorktreeError, match="submodule or nested git repository"):
        project.manager.remove(project.worktree)
    assert ran(project) == ""


def test_unblock_ladder_sees_a_submodule_at_a_protected_path_without_entering_it(project) -> None:
    plant_submodule(project, committed=True)
    ladder = UnblockLadder(project.db, first_mate=_forbidden, repo_path=project.checkout, protected_paths=("sub",))
    result = asyncio.run(ladder.handle(*_ladder_args(project.worktree.path)))
    assert result.reason == "protected path" and not result.resumed
    assert ran(project) == ""


# --- N4: a planted git on PATH --------------------------------------------------------------


def test_relative_empty_and_worktree_path_entries_never_find_a_planted_git(project) -> None:
    wt = project.worktree.path
    for planted in (wt / "git", wt / "bin" / "git"):
        planted.parent.mkdir(exist_ok=True)
        planted.write_text(f"#!/bin/sh\necho planted-git >> '{project.marker}'\n")
        planted.chmod(0o755)
    real = str(Path(shutil.which("git")).parent)
    environ = {"PATH": os.pathsep.join(["", ".", "bin", str(wt / "bin"), real]), "HOME": str(project.home)}
    # Guard: Python looks the program up on the child's PATH, relative to cwd.
    subprocess.run(["git", "--version"], cwd=wt, env=environ, capture_output=True)
    assert "planted-git" in ran(project)
    project.marker.unlink()
    assert run_worktree_git(wt, project.checkout, "rev-parse", "HEAD", environ=environ).stdout.strip() == project.head
    # In the operator's checkout there is no worktree to exclude; relative and empty entries still go.
    for planted in (project.checkout / "git", project.checkout / "bin" / "git"):
        planted.parent.mkdir(exist_ok=True)
        shutil.copy(wt / "git", planted)
    relative_only = {**environ, "PATH": os.pathsep.join(["", ".", "bin", real])}
    assert safe_git.run_git(project.checkout, "rev-parse", "HEAD", environ=relative_only).stdout.strip() == project.head
    assert ran(project) == ""
    assert safe_git.safe_path(environ["PATH"]) == os.pathsep.join([str(wt / "bin"), real])
    assert safe_git.safe_path(environ["PATH"], outside=wt.resolve()) == real
    assert safe_git.safe_path(":.:bin") == os.defpath


# --- a deleted or broken .git must not skip the ladder's observed diff ------------------------


@pytest.mark.parametrize("damage", ["deleted", "unreadable", "directory"])
def test_unblock_ladder_fails_closed_when_the_builder_breaks_its_git_file(project, damage) -> None:
    """Without the observed diff the protected-path gate would trust the builder's own path claims."""

    wt = project.worktree.path
    (wt / "PROTECTED.md").write_text("builder change\n")
    dot = wt / ".git"
    if damage == "deleted":
        dot.unlink()
    elif damage == "unreadable":
        dot.chmod(0)
    else:
        dot.unlink()
        dot.mkdir()
    decided = []

    async def mate(*args, **kwargs):
        decided.append(args)
        return {"disposition": "resolve", "answer": "A", "citation": "project:x", "reason": "preapproved"}

    async def resume(run_id, prompt):
        return True

    ladder = UnblockLadder(project.db, first_mate=mate, resumer=resume, repo_path=project.checkout, protected_paths=("PROTECTED.md",))
    try:
        with pytest.raises(UnsafeGitDirectory):
            asyncio.run(ladder.handle(*_ladder_args(wt)))
    finally:
        if damage == "unreadable":
            dot.chmod(0o644)
    assert not decided, "an unverified worktree must never reach automatic resolution"


# --- cleanup must not delete work Harbor cannot see ------------------------------------------


def test_remove_keeps_a_committed_submodule_with_uncommitted_work(project) -> None:
    sub = plant_submodule(project, committed=True)  # leaves sub/f.txt modified, uncommitted
    assert run_worktree_git(project.worktree.path, project.checkout, "status", "--porcelain").stdout == ""
    with pytest.raises(WorktreeError, match=r"submodule or nested git repository \(sub\)"):
        project.manager.remove(project.worktree)
    assert (sub / "f.txt").read_text() == "two\n"
    assert ran(project) == ""


def test_remove_keeps_a_worktree_with_an_ignored_nested_repository(project) -> None:
    (project.checkout / ".git" / "info" / "exclude").write_text("vendor/\n")
    nested = project.worktree.path / "vendor" / "lib"
    git(project.worktree.path, "init", "-q", str(nested))
    (nested / "work.txt").write_text("uncommitted\n")
    assert run_worktree_git(project.worktree.path, project.checkout, "status", "--porcelain").stdout == ""
    with pytest.raises(WorktreeError, match=r"nested git repository \(vendor/lib\)"):
        project.manager.remove(project.worktree)
    assert (nested / "work.txt").exists()


def test_remove_still_deletes_a_clean_worktree(project) -> None:
    project.manager.remove(project.worktree)
    assert not project.worktree.path.exists()


# --- a forged commit-graph must not change the ladder's base...HEAD diff ----------------------

_GRAPH_PARENT_NONE = 0x70000000


def _forge_commit_graph(graph: Path, parents: dict, levels: dict) -> None:
    """Rewrite first parents and topological levels in a v1 commit-graph, as a builder could."""

    data = bytearray(graph.read_bytes())
    assert data[:4] == b"CGPH"
    table = {}
    for i in range(data[6] + 1):
        chunk, offset = struct.unpack(">4sQ", data[8 + 12 * i: 20 + 12 * i])
        table[chunk] = offset
    oidl, cdat = table[b"OIDL"], table[b"CDAT"]
    oids = [bytes(data[oidl + 20 * i: oidl + 20 * (i + 1)]).hex() for i in range((cdat - oidl) // 20)]
    for child, parent in parents.items():
        index = _GRAPH_PARENT_NONE if parent is None else oids.index(parent)
        struct.pack_into(">I", data, cdat + 36 * oids.index(child) + 20, index)
    for oid, level in levels.items():
        entry = cdat + 36 * oids.index(oid) + 28
        word = struct.unpack(">Q", data[entry: entry + 8])[0]
        struct.pack_into(">Q", data, entry, (level << 34) | (word & ((1 << 34) - 1)))
    graph.unlink()  # git writes it read-only; objects/info/ is builder-writable
    graph.write_bytes(bytes(data))


def test_a_forged_commit_graph_does_not_change_the_merge_base_diff(project) -> None:
    wt, base = project.worktree.path, project.head
    (wt / "PROTECTED.md").write_text("x\n")
    git(wt, "add", "PROTECTED.md")
    git(wt, *IDENTITY, "commit", "-q", "-m", "C1")
    c1 = git(wt, "rev-parse", "HEAD")
    (wt / "other.txt").write_text("y\n")
    git(wt, "add", "other.txt")
    git(wt, *IDENTITY, "commit", "-q", "-m", "C2")
    c2 = git(wt, "rev-parse", "HEAD")
    git(project.checkout, "-c", "commitGraph.generationVersion=1", "commit-graph", "write", "--reachable")
    graph = project.checkout / ".git" / "objects" / "info" / "commit-graph"
    _forge_commit_graph(graph, {c1: None, base: c1}, {c1: 1, base: 2, c2: 2})
    # Guard: git trusts the forged graph unless told not to.
    forged = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=wt, capture_output=True, text=True)
    assert forged.returncode != 0 or "PROTECTED.md" not in forged.stdout
    result = run_worktree_git(wt, project.checkout, "diff", "--name-only", f"{base}...HEAD")
    assert result.returncode == 0 and set(result.stdout.split()) == {"PROTECTED.md", "other.txt"}
