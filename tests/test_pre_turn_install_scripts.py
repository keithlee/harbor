"""Builder-written install code cannot reach anything valuable, whatever it runs.

The pre-turn reinstall runs ``install_command`` on files the builder can edit:
lifecycle scripts and every file they call, ``.npmrc`` (``git=``,
``node-options=``), ``.pnpmfile.cjs``, ``packageManager``. Hashing those
inputs cannot establish that the code is reviewed, so Harbor runs every
install it starts in its own macOS install sandbox (harbor.install_sandbox).
These tests run the reviewer's exploits, and more, with real npm and pnpm
inside that sandbox and check that each one fails, while an install that needs
its scripts (a native-build-style postinstall) still succeeds.

They also cover rung-0 remediation: a failed install's output never selects a
command, rung 0 runs no package manager, and ``gh`` is never a planted one.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor import dependencies, install_sandbox
from harbor.dependencies import pre_turn_installer, without_scripts
from harbor.facts.remediation import execute_remediation, remediation_plan
from test_pre_turn_install import ALLOW, PASS, SECRETS, dispatcher_supervisor, fake_manager

NPM = shutil.which("npm")
PNPM = shutil.which("pnpm")
DENIED = {"EPERM", "EACCES"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _sandbox_works() -> bool:
    if not install_sandbox.available():
        return False
    probe = subprocess.run([install_sandbox.SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"],
                           capture_output=True)
    return probe.returncode == 0  # False when the tests themselves already run inside a sandbox


needs_sandbox = pytest.mark.skipif(not _sandbox_works(), reason="needs macOS sandbox-exec (not nested in another sandbox)")


@pytest.fixture
def home() -> Path:
    """The isolated HOME (tests/env_isolation.py) with credentials a readable install would leak."""

    path = Path(os.environ["HOME"])
    for relative, text in ((".config/gh/hosts.yml", "github.com:\n  oauth_token: gho_secret\n"),
                           (".ssh/id_ed25519", "PRIVATE KEY\n"), (".harbor/harbor.db", "ledger\n"),
                           (".npmrc", "//registry.npmjs.org/:_authToken=npm_secret\n"),
                           (".git-credentials", "https://x:secret@github.com\n")):
        (path / relative).parent.mkdir(parents=True, exist_ok=True)
        (path / relative).write_text(text)
    return path


def linked_worktree(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    """A repository holding ``files`` and a linked worktree of it, as Harbor makes them."""

    repo, worktree = tmp_path / "repo", tmp_path / "wt" / "SAMPLE-1"
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    for path, text in {".gitignore": "node_modules/\n", **files}.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-q", "-m", "base")
    worktree.parent.mkdir(parents=True)
    git(repo, "worktree", "add", "-q", "-b", "harbor/SAMPLE-1", str(worktree), "main")
    return repo, worktree


PROBE_JS = r"""
const fs = require('fs'), cp = require('child_process'), net = require('net'), path = require('path');
const config = JSON.parse(fs.readFileSync(path.join(process.cwd(), 'probe-config.json'), 'utf8'));
const label = process.env.PROBE_LABEL || config.label;
const out = {};
function attempt(name, action) {
  try { const value = action(); out[name] = 'ok' + (value === undefined ? '' : ':' + String(value).length); }
  catch (error) { out[name] = error.code || String(error.status) || String(error); }
}
attempt('read_gh', () => fs.readFileSync(path.join(config.home, '.config/gh/hosts.yml'), 'utf8'));
attempt('list_ssh', () => fs.readdirSync(path.join(config.home, '.ssh')).join(','));
attempt('read_harbor_ledger', () => fs.readFileSync(path.join(config.home, '.harbor/harbor.db'), 'utf8'));
attempt('read_user_npmrc', () => fs.readFileSync(path.join(config.home, '.npmrc'), 'utf8'));
attempt('read_git_credentials', () => fs.readFileSync(path.join(config.home, '.git-credentials'), 'utf8'));
attempt('read_main_checkout', () => fs.readdirSync(config.repo).join(','));
attempt('write_repo_hook', () => fs.writeFileSync(path.join(config.repo, '.git/hooks/post-checkout'), '#!/bin/sh\n'));
attempt('write_worktree_git_link', () => fs.appendFileSync(path.join(process.cwd(), '.git'), 'x'));
attempt('write_outside', () => fs.writeFileSync(config.outside, 'escaped'));
attempt('write_home', () => fs.writeFileSync(path.join(config.home, '.zshrc'), 'evil'));
attempt('write_launch_agent', () => { fs.mkdirSync(path.join(config.home, 'Library/LaunchAgents'), {recursive: true});
  fs.writeFileSync(path.join(config.home, 'Library/LaunchAgents/evil.plist'), 'x'); });
attempt('write_inside', () => { fs.mkdirSync(path.join(process.cwd(), 'build/Release'), {recursive: true});
  fs.writeFileSync(path.join(process.cwd(), 'build/Release/addon.node'), 'native'); });
attempt('write_tmpdir', () => fs.writeFileSync(path.join(process.env.TMPDIR || '/nonexistent', 'scratch'), 'x'));
attempt('launchctl_submit', () => { const r = cp.spawnSync('/bin/launchctl', ['submit', '-l', 'harbor.evil', '--', '/usr/bin/true']);
  if (r.status !== 0) { const e = new Error('launchctl'); e.code = 'DENIED:' + r.status; throw e; } });
attempt('apple_event', () => { const r = cp.spawnSync('/usr/bin/osascript', ['-e', 'tell application "Finder" to get count of windows']);
  if (r.status !== 0) { const e = new Error('osascript'); e.code = 'DENIED:' + r.status; throw e; } });
attempt('github_token_env', () => { if (process.env.GITHUB_TOKEN) return process.env.GITHUB_TOKEN; const e = new Error(); e.code = 'ABSENT'; throw e; });
const finish = () => fs.writeFileSync(path.join(process.cwd(), 'probe-' + label + '.json'), JSON.stringify(out));
if (config.sock) {
  const client = net.connect(config.sock);
  client.on('connect', () => { out.unix_socket = 'ok'; client.end(); finish(); });
  client.on('error', (error) => { out.unix_socket = error.code; finish(); });
} else { finish(); }
"""


def write_probe(worktree: Path, home: Path, repo: Path, tmp_path: Path, label: str, sock: str | None = None) -> None:
    (worktree / "probe.js").write_text(PROBE_JS)
    (worktree / "probe-config.json").write_text(json.dumps(
        {"home": str(home), "repo": str(repo), "outside": str(tmp_path / "outside.txt"), "label": label, "sock": sock}))


def assert_contained(worktree: Path, tmp_path: Path, home: Path, label: str) -> dict[str, str]:
    result = json.loads((worktree / f"probe-{label}.json").read_text())
    print(f"probe {label}: {json.dumps(result, sort_keys=True)}")
    for name in ("read_gh", "list_ssh", "read_harbor_ledger", "read_user_npmrc", "read_git_credentials",
                 "read_main_checkout", "write_repo_hook", "write_worktree_git_link", "write_outside", "write_home",
                 "write_launch_agent"):
        assert result[name] in DENIED, (name, result[name])
    assert result["launchctl_submit"].startswith("DENIED"), result
    assert result["apple_event"].startswith("DENIED"), result
    assert result["github_token_env"] == "ABSENT"
    assert result["write_inside"] == "ok" and result["write_tmpdir"] == "ok"
    assert not (tmp_path / "outside.txt").exists() and (home / ".zshrc").exists() is False
    return result


def sandboxed(worktree: Path, repo: Path, tmp_path: Path, command, *, scripts: str = "on"):
    ledger = sqlite3.connect(":memory:")
    manager = SimpleNamespace(install_command=command, root=worktree.parent, install_timeout_seconds=180, repo=repo)
    isolation = install_sandbox.MacSandbox(repo=repo, caches=tmp_path / "cache")
    check = asyncio.run(pre_turn_installer(manager, ledger, isolation=isolation, scripts=scripts)(worktree))
    print(f"check: {check.line()}")
    return check


def manifest(**fields) -> str:
    return json.dumps({"name": "sample", "version": "1.0.0", "private": True, **fields}, indent=2) + "\n"


@pytest.fixture
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)


# --- the reviewer's exploits, live -------------------------------------------------------


@needs_sandbox
@pytest.mark.skipif(NPM is None, reason="needs npm")
def test_a_base_postinstall_calling_a_builder_edited_script_cannot_reach_anything(tmp_path, home, secrets):
    """Exploit 1: package.json is the base branch's; the file its postinstall runs is the builder's."""

    repo, worktree = linked_worktree(tmp_path, {"package.json": manifest(scripts={"postinstall": "node scripts/setup.js"}),
                                                 "scripts/setup.js": "console.log('reviewed setup')\n"})
    write_probe(worktree, home, repo, tmp_path, "setup")
    (worktree / "scripts/setup.js").write_text("require('../probe.js')\n")  # the builder's edit, not hashed
    check = sandboxed(worktree, repo, tmp_path, ("npm", "install"))
    assert check.action == "installed", check.line()
    assert_contained(worktree, tmp_path, home, "setup")
    assert not (repo / ".git/hooks/post-checkout").exists()


@needs_sandbox
@pytest.mark.skipif(NPM is None, reason="needs npm")
def test_an_uppercase_npmrc_node_options_require_is_contained(tmp_path, home, secrets):
    """Exploit 2: macOS resolves .NPMRC as .npmrc; node-options=--require runs the builder's file in every script."""

    repo, worktree = linked_worktree(tmp_path, {"package.json": manifest(scripts={"postinstall": "node -e 1"})})
    write_probe(worktree, home, repo, tmp_path, "npmrc")
    (worktree / ".NPMRC").write_text(f"node-options=--require {worktree / 'probe.js'}\n")
    check = sandboxed(worktree, repo, tmp_path, ("npm", "install"))
    assert check.action == "installed", check.line()
    assert_contained(worktree, tmp_path, home, "npmrc")


@needs_sandbox
@pytest.mark.skipif(NPM is None, reason="needs npm")
def test_a_builder_chosen_git_executable_runs_only_in_the_sandbox(tmp_path, home, secrets):
    """Exploit 3a: .npmrc git=<script> plus a git+file dependency makes npm run the builder's 'git'."""

    dep = tmp_path / "dep.git"
    git(tmp_path, "init", "-q", "--bare", str(dep))
    repo, worktree = linked_worktree(tmp_path, {"package.json": manifest()})
    evil = worktree / "evil-git.sh"
    evil.write_text(
        "#!/bin/sh\n"
        f"echo ran > {worktree}/evil-git-ran\n"
        f"cat {home}/.config/gh/hosts.yml > {worktree}/leak-gh 2>/dev/null\n"
        f"ls {home}/.ssh > {worktree}/leak-ssh 2>/dev/null\n"
        f"touch {repo}/.git/hooks/pre-commit 2>/dev/null\n"
        f"touch {tmp_path}/outside.txt 2>/dev/null\n"
        "exit 1\n")
    evil.chmod(0o755)
    (worktree / ".npmrc").write_text(f"git={evil}\n")
    (worktree / "package.json").write_text(manifest(dependencies={"dep": f"git+file://{dep}"}))
    check = sandboxed(worktree, repo, tmp_path, ("npm", "install"))
    assert (worktree / "evil-git-ran").exists(), "npm did not run the configured git: the probe proved nothing"
    assert check.failed  # its fake git exits 1
    assert (worktree / "leak-gh").read_text() == "" and (worktree / "leak-ssh").read_text() == ""
    assert not (repo / ".git/hooks/pre-commit").exists() and not (tmp_path / "outside.txt").exists()


@needs_sandbox
@pytest.mark.skipif(PNPM is None, reason="needs pnpm")
def test_pnpm_never_downloads_the_package_manager_the_worktree_names(tmp_path, home):
    """Exploit 3b: packageManager plus a builder-set registry made pnpm 10 download and run that version."""

    repo, worktree = linked_worktree(tmp_path, {"package.json": manifest()})
    (worktree / "package.json").write_text(manifest(packageManager="pnpm@9.15.9"))
    (worktree / ".npmrc").write_text("registry=http://127.0.0.1:9/\n")
    check = sandboxed(worktree, repo, tmp_path, ("pnpm", "install"))
    assert check.action == "installed", check.line()
    assert "--config.manage-package-manager-versions=false" in check.ran_command


@needs_sandbox
@pytest.mark.skipif(PNPM is None, reason="needs pnpm")
def test_a_builder_pnpmfile_and_postinstall_are_contained(tmp_path, home, secrets):
    repo, worktree = linked_worktree(tmp_path, {"package.json": manifest()})
    write_probe(worktree, home, repo, tmp_path, "pnpmfile")
    (worktree / ".PNPMFILE.cjs").write_text("require('./probe.js')\nmodule.exports = {}\n")
    (worktree / "package.json").write_text(manifest(scripts={"postinstall": "node -e 1"}))
    check = sandboxed(worktree, repo, tmp_path, ("pnpm", "install"))
    assert check.action == "installed", check.line()
    assert_contained(worktree, tmp_path, home, "pnpmfile")


@needs_sandbox
@pytest.mark.skipif(NPM is None, reason="needs npm")
def test_a_postinstall_cannot_use_a_local_unix_socket(tmp_path, home):
    """No ssh agent, Docker socket or other local service: only DNS's socket is reachable."""

    sock_dir = Path("/private/tmp") / f"harbor-sock-{os.getpid()}"
    sock_dir.mkdir(exist_ok=True)
    path = sock_dir / "agent.sock"
    server = socket.socket(socket.AF_UNIX)
    path.unlink(missing_ok=True)
    server.bind(str(path))
    server.listen(1)
    def accept() -> None:
        try:
            server.accept()
        except OSError:
            pass  # closed at the end of the test: nothing connected, as intended

    threading.Thread(target=accept, daemon=True).start()
    try:
        repo, worktree = linked_worktree(tmp_path, {"package.json": manifest()})
        write_probe(worktree, home, repo, tmp_path, "socket", sock=str(path))
        (worktree / "package.json").write_text(manifest(scripts={"postinstall": "node probe.js"}))
        check = sandboxed(worktree, repo, tmp_path, ("npm", "install"))
        assert check.action == "installed", check.line()
        result = json.loads((worktree / "probe-socket.json").read_text())
        assert result["unix_socket"] in DENIED, result
    finally:
        server.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


@needs_sandbox
@pytest.mark.skipif(NPM is None, reason="needs npm")
def test_an_install_that_needs_its_scripts_still_succeeds(tmp_path, home):
    """A native-build-style postinstall writes its output in the worktree and its temp files in TMPDIR."""

    repo, worktree = linked_worktree(tmp_path, {"package.json": manifest(scripts={"postinstall": (
        "node -e \"const fs=require('fs'),os=require('os'),p=require('path');"
        "fs.writeFileSync(p.join(os.tmpdir(),'obj.o'),'o');"
        "fs.mkdirSync('build/Release',{recursive:true});fs.writeFileSync('build/Release/addon.node','native')\"")})})
    check = sandboxed(worktree, repo, tmp_path, ("npm", "install"))
    assert check.action == "installed" and check.scripts is True, check.line()
    assert (worktree / "build/Release/addon.node").read_text() == "native"


@needs_sandbox
@pytest.mark.skipif(NPM is None, reason="needs npm")
def test_worktree_creation_installs_in_the_sandbox_too(tmp_path, home, monkeypatch):
    from harbor.worktree import WorktreeManager

    remote, checkout = tmp_path / "remote.git", tmp_path / "checkout"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "clone", "-q", str(remote), str(checkout))
    git(checkout, "checkout", "-q", "-b", "main")
    (checkout / ".gitignore").write_text("node_modules/\nprobe-*.json\n")
    (checkout / "package.json").write_text(manifest(scripts={"postinstall": "node probe.js"}))
    (checkout / "probe.js").write_text(PROBE_JS)
    git(checkout, "add", "-A")
    git(checkout, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-q", "-m", "base")
    git(checkout, "push", "-q", "-u", "origin", "main")
    monkeypatch.setattr(install_sandbox, "cache_root", lambda: tmp_path / "cache")
    manager = WorktreeManager(checkout, root=tmp_path / "wt", disk_usage=lambda _: 100.0, install_command="npm install")
    target = manager.path_for("SAMPLE-1")
    # The probe's config is written by the test, before the install runs inside the new worktree.
    original = manager.install

    def install(worktree):
        (Path(worktree.path) / "probe-config.json").write_text(json.dumps(
            {"home": str(home), "repo": str(checkout), "outside": str(tmp_path / "outside.txt"), "label": "create"}))
        original(worktree)

    manager.install = install
    worktree = manager.create("SAMPLE-1")
    assert worktree.path == target
    assert_contained(target, tmp_path, home, "create")


# --- hosts without the sandbox, and the optional scripts-off setting ------------------------------


def test_without_an_install_sandbox_the_reinstall_is_refused(tmp_path: Path) -> None:
    worktree = tmp_path / "wt" / "SAMPLE-1"
    worktree.mkdir(parents=True)
    (worktree / "package.json").write_text(manifest(dependencies={"left-pad": "1.3.0"}))
    command = (str(fake_manager(tmp_path)), "install")
    manager = SimpleNamespace(install_command=command, root=worktree.parent, install_timeout_seconds=60)
    check = asyncio.run(pre_turn_installer(manager, sqlite3.connect(":memory:"), isolation=None)(worktree))
    assert check.failed and not check.ran and "no install sandbox" in check.error
    assert not (tmp_path / "events.log").exists()
    assert dependencies.blocker(check, worktree, "SAMPLE-1")["kind"] == "dependency_install"


def test_the_default_isolation_is_the_macos_sandbox_only_where_it_exists(monkeypatch) -> None:
    monkeypatch.setattr(install_sandbox, "available", lambda: False)
    assert install_sandbox.default_isolation() is None
    monkeypatch.setattr(install_sandbox, "available", lambda: True)
    assert isinstance(install_sandbox.default_isolation(), install_sandbox.MacSandbox)


def test_a_planted_package_manager_is_never_the_one_that_runs(tmp_path: Path, monkeypatch) -> None:
    worktree = tmp_path / "wt" / "SAMPLE-1"
    (worktree / "node_modules/.bin").mkdir(parents=True)
    for directory in (worktree, worktree / "node_modules/.bin"):
        planted = directory / "pnpm"
        planted.write_text("#!/bin/sh\necho planted\n")
        planted.chmod(0o755)
    real = fake_manager(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([str(worktree / "node_modules/.bin"), ".", "", str(real.parent),
                                                os.environ["PATH"]]))
    monkeypatch.chdir(worktree)
    resolved, why = dependencies.resolve_command(("pnpm", "install"), worktree)
    assert resolved == (str(real), "install"), why
    assert dependencies.resolve_command((str(worktree / "pnpm"), "install"), worktree)[0] is None


@pytest.mark.parametrize("command,expected", [
    (("pnpm", "install"), ("pnpm", "install", "--ignore-scripts", "--ignore-pnpmfile")),
    (("corepack", "pnpm", "install"), ("corepack", "pnpm", "install", "--ignore-scripts", "--ignore-pnpmfile")),
    (("/opt/bin/npm", "ci"), ("/opt/bin/npm", "ci", "--ignore-scripts")),
    (("bun", "install"), ("bun", "install", "--ignore-scripts")),
    (("yarn",), ("yarn", "--ignore-scripts")),
    (("uv", "sync"), ("uv", "sync", "--no-install-project", "--no-install-workspace", "--no-install-local", "--no-build")),
])
def test_scripts_off_adds_each_managers_flags(command, expected) -> None:
    assert without_scripts(command, {"package.json": "file:x"}) == (expected, "")


@pytest.mark.parametrize("command,inputs,why", [
    (("yarn", "install"), {".yarnrc.yml": "file:x"}, "Yarn loads .yarnrc.yml"),
    (("make", "setup"), {}, "no way to switch off install scripts for `make setup`"),
])
def test_scripts_off_refuses_what_it_cannot_switch_off(command, inputs, why) -> None:
    effective, reason = without_scripts(command, inputs)
    assert effective is None and why in reason


def test_scripts_off_is_applied_when_the_project_asks(tmp_path: Path) -> None:
    worktree = tmp_path / "wt" / "SAMPLE-1"
    worktree.mkdir(parents=True)
    (worktree / "package.json").write_text("{}")
    command = (str(fake_manager(tmp_path)), "install")
    manager = SimpleNamespace(install_command=command, root=worktree.parent, install_timeout_seconds=60)
    check = asyncio.run(pre_turn_installer(manager, sqlite3.connect(":memory:"), isolation=PASS, scripts="off")(worktree))
    assert check.action == "installed" and check.scripts is False
    assert check.ran_command[1:] == ("install", "--ignore-scripts", "--ignore-pnpmfile",
                                     "--config.manage-package-manager-versions=false")


def test_the_dependency_scripts_setting_fails_closed() -> None:
    from harbor.config.workflow import builder_dependency_scripts

    assert builder_dependency_scripts({}) == "on"
    assert builder_dependency_scripts({"builder": {"dependency_scripts": "off"}}) == "off"
    for bad in (True, "always", "base"):
        with pytest.raises(ValueError, match="dependency_scripts"):
            builder_dependency_scripts({"builder": {"dependency_scripts": bad}})


def test_case_variants_and_patched_dependencies_count_as_inputs(tmp_path: Path) -> None:
    (tmp_path / ".NPMRC").write_text("x")
    (tmp_path / ".PNPMFILE.cjs").write_text("x")
    (tmp_path / "vendor/fixes").mkdir(parents=True)
    (tmp_path / "vendor/fixes/left-pad.patch").write_text("x")
    (tmp_path / "package.json").write_text(json.dumps({"pnpm": {"patchedDependencies": {
        "left-pad@1.3.0": "vendor/fixes/left-pad.patch", "evil": "../outside.patch"}}}))
    assert {".NPMRC", ".PNPMFILE.cjs", "vendor/fixes/left-pad.patch", "package.json"} <= set(dependencies.scan_inputs(tmp_path))


def test_profile_denies_credentials_and_git_and_allows_only_the_worktree_and_caches(tmp_path: Path) -> None:
    profile = install_sandbox.Profile(worktree=tmp_path / "wt", tmpdir=tmp_path / "tmp", home=tmp_path / "home",
                                      git_dirs=(tmp_path / "repo/.git",), denied=(tmp_path / "repo",),
                                      caches=(tmp_path / "cache",)).render()
    assert profile.startswith("(version 1)\n(deny default)")
    assert '(subpath "' + os.path.realpath(tmp_path / "home") + '/.ssh")' in profile
    allowed = next(line for line in profile.splitlines() if line.startswith("(allow mach-lookup"))
    assert "SecurityServer" not in allowed and "launchservicesd" not in allowed and "appleevents" not in allowed
    assert "(deny appleevent-send)" in profile
    lines = profile.splitlines()
    worktree_allow = next(i for i, line in enumerate(lines) if line.startswith("(allow file-read* file-write*"))
    git_deny = next(i for i, line in enumerate(lines) if '.git")' in line and line.startswith("(deny"))
    credential_deny = next(i for i, line in enumerate(lines) if "/.ssh" in line)
    assert credential_deny < worktree_allow < git_deny  # later rules win in SBPL


# --- rung-0 remediation -----------------------------------------------------------------------

NODE_GYP = "Error: Cannot find module 'node-gyp/bin/node-gyp.js'"


def test_remediation_never_acts_on_a_pre_turn_install_blocker() -> None:
    blocker = {"kind": "dependency_install", "question": "install failed", "evidence": NODE_GYP}
    assert remediation_plan(blocker) is None
    result = asyncio.run(execute_remediation(blocker, executor=lambda command: pytest.fail("ran " + str(command))))
    assert not result.attempted


def test_rung_zero_never_runs_a_package_manager_and_takes_the_ledger_lock(tmp_path: Path, monkeypatch, secrets) -> None:
    fake_manager(tmp_path)  # tmp_path/bin/pnpm, first on PATH: any `pnpm` run would be logged
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    blocker = {"kind": "env_blocker", "evidence": NODE_GYP}
    ledger = sqlite3.connect(":memory:")
    worktree = tmp_path / "linked"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere\n")
    (worktree / "package.json").write_text("{}")
    dependencies.record(ledger, worktree, ("pnpm", "install"), status="installed")
    for cwd, store in ((worktree, None), (None, ledger), (tmp_path, ledger)):
        result = asyncio.run(execute_remediation(blocker, cwd=cwd, ledger=store))
        assert result.attempted and not result.fixed and result.error

    class Lock:
        held = 0

        def __enter__(self):
            Lock.held += 1

        def __exit__(self, *exc):
            return False

    result = asyncio.run(execute_remediation(blocker, cwd=worktree, ledger=ledger, ledger_lock=Lock()))
    assert result.fixed and result.plan.commands == (("harbor", "schedule-dependency-reinstall"),)
    assert Lock.held == 1
    assert not (tmp_path / "events.log").exists()
    assert dependencies.recorded(ledger, worktree).status == "stale"


def test_gh_auth_status_cannot_be_redirected_to_a_planted_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "wt" / "SAMPLE-1"
    planted_bin, harbor_bin = worktree / "node_modules" / ".bin", tmp_path / "harbor-bin"
    for directory, label in ((planted_bin, "planted"), (worktree, "planted-cwd"), (harbor_bin, "harbor")):
        directory.mkdir(parents=True, exist_ok=True)
        gh = directory / "gh"
        gh.write_text(f"#!/bin/sh\necho \"$@\" > {tmp_path / label}\n")
        gh.chmod(0o755)
    monkeypatch.chdir(worktree)
    monkeypatch.setenv("PATH", os.pathsep.join([str(planted_bin), ".", "", str(harbor_bin), os.environ["PATH"]]))
    result = asyncio.run(execute_remediation({"kind": "github_transport"}, cwd=worktree))
    assert result.fixed, result.error
    assert (tmp_path / "harbor").read_text().strip() == "auth status --hostname github.com"
    assert not (tmp_path / "planted").exists() and not (tmp_path / "planted-cwd").exists()
    assert result.outputs[0].args[0] == str(harbor_bin / "gh")


def test_a_failed_install_that_prints_cannot_find_module_does_not_run_remediation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first review's probe: the pre-turn install fails with "Cannot find module", GITHUB_TOKEN is set,
    and the real ladder (default remediation executor) handles the blocker. pnpm must not run again."""

    from test_blocked_relaunch_loop import _db, _escalations, _human, _LinearWithComments, _poll_and_settle, _ticket
    from test_pre_turn_install import _builder_script
    from harbor.runner.codex import CodexRunner
    from harbor.unblock import UnblockLadder

    monkeypatch.setenv("GITHUB_TOKEN", "ghp-harbor")
    fake_manager(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
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
    runner.dependency_preparer = pre_turn_installer(manager, db, project="SampleProject", isolation=PASS)
    supervisor = dispatcher_supervisor(linear, db, worktree, runner,
                                       UnblockLadder(db, linear=linear, first_mate=first_mate, repo_path=worktree))
    try:
        async def scenario() -> None:
            for _ in range(3):
                await _poll_and_settle(supervisor)

        asyncio.run(scenario())
        args = [json.loads(line) for line in (tmp_path / "args.log").read_text().splitlines()]
        assert args == [["install", "--config.manage-package-manager-versions=false"]], args
        env = json.loads((tmp_path / "install-env.json").read_text())
        assert "GITHUB_TOKEN" not in env
        assert _escalations(db) == [("dependency_install", "open")], supervisor.last_error
    finally:
        db.close()
