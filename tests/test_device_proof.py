"""Opt-in device and browser proof for Claude builders (``[builder] device_proof``)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harbor.config.workflow import DEVICE_PROOF_PLATFORMS, device_proof_settings, load_workflow
from harbor.runner.base import RunnerError
from harbor.runner.claude import (DEVICE_PROOF_RULES, IOS_MACH_SERVICES, ClaudeRunner, device_proof_network,
                                  device_proof_report, ios_mach_services)
from workflow_support import config as materialize

# Keys Harbor must never add for device proof: each one removes a whole class of isolation.
FORBIDDEN = {"allowAppleEvents", "allowAllUnixSockets", "excludedCommands", "disabled", "allowUnixSockets",
             "enableWeakerNetworkIsolation", "enableWeakerNestedSandbox"}


def sandbox(tmp_path: Path, platforms: tuple[str, ...] = ()) -> dict:
    runner = ClaudeRunner(tmp_path, model="claude-sonnet-5-5", reasoning_effort="medium", executable="claude",
                          writable_roots=(tmp_path / "ledger",))
    if platforms:
        runner.device_proof = platforms
    argv = runner.command("x", run_id="run")
    return json.loads(argv[argv.index("--settings") + 1])["sandbox"]


# The settings Harbor generated before device proof existed. Absent config must reproduce them exactly.
def baseline(tmp_path: Path) -> dict:
    return {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": True,
        "filesystem": {"allowWrite": [str((tmp_path / "ledger").resolve())]},
        "network": {"allowedDomains": ["github.com", "api.github.com", "api.linear.app", "registry.npmjs.org"]},
    }


def test_absent_device_proof_leaves_the_sandbox_unchanged(tmp_path: Path) -> None:
    assert sandbox(tmp_path) == baseline(tmp_path)


@pytest.mark.parametrize(("platforms", "added"), [
    (("android",), {"allowLocalBinding": True}),
    (("ios",), {"allowMachLookup": list(IOS_MACH_SERVICES)}),
    (("web",), {"allowLocalBinding": True}),
    (("android", "ios", "web"), {"allowLocalBinding": True, "allowMachLookup": list(IOS_MACH_SERVICES)}),
])
def test_each_platform_adds_exactly_its_rules(tmp_path: Path, platforms, added) -> None:
    expected = baseline(tmp_path)
    expected["network"].update(added)
    assert sandbox(tmp_path, platforms) == expected


def test_no_platform_widens_hosts_writes_or_protections(tmp_path: Path) -> None:
    every = sandbox(tmp_path, DEVICE_PROOF_PLATFORMS)
    base = baseline(tmp_path)
    assert every["network"]["allowedDomains"] == base["network"]["allowedDomains"]
    assert every["filesystem"] == base["filesystem"]
    assert every["enabled"] is True and every["failIfUnavailable"] is True
    assert every["allowUnsandboxedCommands"] is False
    keys = set(every) | set(every["network"]) | set(every["filesystem"])
    assert not keys & FORBIDDEN


def test_config_and_runner_know_the_same_platforms() -> None:
    assert set(DEVICE_PROOF_RULES) == set(DEVICE_PROOF_PLATFORMS)


def test_an_unknown_platform_on_the_runner_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RunnerError, match="unknown device_proof platform 'androd'"):
        sandbox(tmp_path, ("androd",))
    with pytest.raises(ValueError, match="unknown device_proof platform"):
        device_proof_network(["windows"])


@pytest.mark.parametrize(("extra", "expected"), [
    ("", ()),
    ("[builder]\n", ()),
    ("[builder]\ndevice_proof = []\n", ()),
    ('[builder]\ndevice_proof = ["ios", "web"]\n', ("ios", "web")),
])
def test_workflow_parses_device_proof(tmp_path: Path, extra: str, expected) -> None:
    workflow = load_workflow(materialize(tmp_path, extra=extra))
    assert device_proof_settings(workflow.config) == expected


@pytest.mark.parametrize(("extra", "message"), [
    ('[builder]\ndevice_proof = ["android", "Android"]\n', "unknown builder.device_proof platform"),
    ('[builder]\ndevice_proof = ["macos"]\n', "unknown builder.device_proof platform"),
    ('[builder]\ndevice_proof = "android"\n', "must be a list"),
    ("[builder]\ndevice_proof = [1]\n", "must be a list"),
    ('[builder]\ndevice_proof = ["web", "web"]\n', "twice"),
    ('[builder]\nallow_everything = true\n', "unknown builder setting"),
])
def test_workflow_refuses_a_bad_device_proof(tmp_path: Path, extra: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_workflow(materialize(tmp_path, extra=extra))


def test_report_names_each_grant_and_flags_linux() -> None:
    assert device_proof_report((), "darwin")[0].startswith("Device proof: off")
    mac = device_proof_report(("android", "ios"), "darwin")
    assert mac[0] == "Device proof: android, ios"
    assert any("network.allowLocalBinding" in line for line in mac)
    assert any("com.apple.CoreSimulator.CoreSimulatorService" in line and "sandbox escape" in line for line in mac)
    assert any("path out of the sandbox" in line for line in mac)
    assert not any("not effective" in line for line in mac)
    linux = device_proof_report(("web",), "linux")
    assert "not effective on this host" in linux[-1] and "Linux" in linux[-1]
    assert "iOS Simulator" not in linux[-1]
    assert "no iOS Simulator" in device_proof_report(("ios",), "linux")[-1]


def test_the_production_factory_reads_device_proof_per_turn(tmp_path: Path, monkeypatch) -> None:
    from harbor.wake import Wake
    from test_github_transport import claude_production

    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(tmp_path / "projects.md"))
    (tmp_path / "projects.md").write_text("", encoding="utf-8")
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "ledger" / "harbor.db"))
    monkeypatch.setattr("harbor.cli.sys.platform", "darwin")
    (dispatcher, _, factory), db = claude_production(tmp_path, monkeypatch,
                                                     workflow_extra='[builder]\ndevice_proof = ["ios"]\n')
    try:
        wake = Wake(ticket={"identifier": "SAMPLE-1"}, action="build")
        built = factory(tmp_path, wake)
        network = json.loads(built.command("x", run_id="r")[built.command("x", run_id="r").index("--settings") + 1])["sandbox"]["network"]
        assert network["allowMachLookup"] == list(IOS_MACH_SERVICES) and "allowLocalBinding" not in network
        assert built.device_proof == ("ios",) and built.device_preparer is not None
        # Removing the section on disk takes effect on the next launch, like any workflow edit.
        toml = dispatcher.workflow.package / "workflow.toml"
        toml.write_text(toml.read_text().replace('device_proof = ["ios"]', "device_proof = []"))
        built = factory(tmp_path, wake)
        network = json.loads(built.command("x", run_id="r")[built.command("x", run_id="r").index("--settings") + 1])["sandbox"]["network"]
        assert set(network) == {"allowedDomains"}
    finally:
        db.close()


def test_a_codex_builder_is_warned_that_device_proof_does_not_apply(tmp_path: Path, monkeypatch, capsys) -> None:
    from harbor.wake import Wake
    from test_github_transport import claude_production

    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/bin/echo")
    (_, _, factory), db = claude_production(tmp_path, monkeypatch, provider="codex",
                                            workflow_extra='[builder]\ndevice_proof = ["android"]\n')
    try:
        built = factory(tmp_path, Wake(ticket={"identifier": "SAMPLE-1"}, action="build"))
        assert "allowLocalBinding" not in " ".join(built.command("x", run_id="r"))
        assert "device_proof applies to Claude Code builders only" in capsys.readouterr().err
    finally:
        db.close()


def test_a_linux_host_is_warned_instead_of_silently_doing_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    from harbor.wake import Wake
    from test_github_transport import claude_production

    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(tmp_path / "projects.md"))
    (tmp_path / "projects.md").write_text("", encoding="utf-8")
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "ledger" / "harbor.db"))
    monkeypatch.setattr("harbor.cli.sys.platform", "linux")
    monkeypatch.setattr("harbor.runner.claude.sys.platform", "linux")
    (_, _, factory), db = claude_production(tmp_path, monkeypatch, workflow_extra='[builder]\ndevice_proof = ["web"]\n')
    try:
        factory(tmp_path, Wake(ticket={"identifier": "SAMPLE-1"}, action="build"))
        err = capsys.readouterr().err
        assert "[builder] device_proof = ['web'] is not effective on this host" in err and "Linux" in err
    finally:
        db.close()


def test_doctor_reports_device_proof_off_by_default(tmp_path: Path) -> None:
    from test_github_transport import run_doctor
    result = run_doctor(tmp_path, "claude", None)
    assert "Device proof: off" in result.stdout, result.stdout + result.stderr


def test_doctor_reports_what_device_proof_grants(tmp_path: Path) -> None:
    from test_github_transport import run_doctor
    result = run_doctor(tmp_path, "claude", None, workflow_extra='[builder]\ndevice_proof = ["android", "ios"]\n')
    out = result.stdout
    assert "Device proof: android, ios · applies to Claude builders (builder_provider)" in out, out + result.stderr
    assert "grants network.allowLocalBinding" in out
    assert f"grants network.allowMachLookup {', '.join(IOS_MACH_SERVICES)}" in out


def test_doctor_fails_closed_on_an_unknown_platform(tmp_path: Path) -> None:
    from test_github_transport import run_doctor
    result = run_doctor(tmp_path, "claude", None, workflow_extra='[builder]\ndevice_proof = ["andriod"]\n')
    # The registry loads the workflow first, so doctor refuses the project before any check runs.
    assert "Harbor: unknown builder.device_proof platform(s) 'andriod'; use any of android, ios, web" in result.stderr
    assert result.returncode == 2 and "Device proof" not in result.stdout


def test_doctor_says_device_proof_is_not_applied_for_codex(tmp_path: Path) -> None:
    from test_github_transport import run_doctor
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert "Device proof" not in run_doctor(tmp_path / "a", "codex", None).stdout
    result = run_doctor(tmp_path / "b", "codex", None, workflow_extra='[builder]\ndevice_proof = ["ios"]\n')
    assert "Device proof: not applied to Codex builders ([builder] device_proof applies to Claude Code builders only" in result.stdout
    assert "applies to Claude builders" not in result.stdout
    assert "cannot use xcrun simctl" in result.stdout


# --- narrowed iOS Mach services ------------------------------------------------------------

def test_ios_rule_lists_exact_live_verified_services_not_a_wildcard() -> None:
    assert ios_mach_services("arm64") == ("com.apple.CoreSimulator.CoreSimulatorService",
                                          "com.apple.CoreSimulator.SimLaunchHost-arm64")
    assert ios_mach_services("x86_64") == ("com.apple.CoreSimulator.CoreSimulatorService",
                                           "com.apple.CoreSimulator.SimLaunchHost-x86")
    granted = DEVICE_PROOF_RULES["ios"]["allowMachLookup"]
    assert granted == list(IOS_MACH_SERVICES)
    assert not any("*" in name for name in granted)
    other_arch = "x86" if granted[1].endswith("arm64") else "arm64"
    # Matched by the old com.apple.CoreSimulator.* wildcard, not needed by simctl install/launch/io screenshot.
    for name in ("com.apple.CoreSimulator.simdiskimaged", "com.apple.CoreSimulator.SimulatorTrampoline",
                 "com.apple.CoreSimulator.SimRenderingServices.display",
                 "com.apple.CoreSimulator.SimAudioProcessorServices.audio",
                 f"com.apple.CoreSimulator.SimLaunchHost-{other_arch}"):
        assert name not in granted


# --- doctor and factory share one decision -------------------------------------------------

LEDGER_SQL = (
    "CREATE TABLE tickets (identifier TEXT, project TEXT); CREATE TABLE runs (id TEXT, ticket TEXT);"
    "CREATE TABLE runner_identity (run_id TEXT, provider TEXT, model TEXT, effort TEXT);"
    "INSERT INTO tickets VALUES ('SAMPLE-1', 'SampleProject'); INSERT INTO runs VALUES ('r1', 'SAMPLE-1');"
    "INSERT INTO runner_identity VALUES ('r1', 'claude', 'claude-sonnet-5-5', 'medium');"
)


def test_builder_providers_include_fallback_and_retained_sessions(tmp_path: Path) -> None:
    import sqlite3
    from types import SimpleNamespace
    from harbor.device_proof import builder_providers, granted_platforms
    from harbor.quota_fallback import Profile

    config = SimpleNamespace(name="SampleProject", builder_provider="codex")
    assert builder_providers(config) == {"codex": ["builder_provider"]}
    policy = SimpleNamespace(builder=(Profile("codex", "gpt-x"), Profile("claude", "claude-sonnet-5-5")))
    assert builder_providers(config, policy) == {"codex": ["builder_provider"], "claude": ["quota_fallback builder"]}
    ledger = sqlite3.connect(tmp_path / "harbor.db")
    ledger.executescript(LEDGER_SQL)
    assert builder_providers(config, None, ledger) == {"codex": ["builder_provider"],
                                                       "claude": ["1 retained builder session(s)"]}
    ledger.close()
    workflow = {"builder": {"device_proof": ["ios"]}}
    assert granted_platforms(workflow, "claude") == ("ios",) and granted_platforms(workflow, "codex") == ()


def test_doctor_reports_the_grant_for_a_codex_project_with_a_claude_fallback(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from harbor import cli
    from harbor.quota_fallback import Profile

    workflow = load_workflow(materialize(tmp_path, extra='[builder]\ndevice_proof = ["android"]\n'))
    config = SimpleNamespace(name="SampleProject", builder_provider="codex")
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "absent.db"))
    monkeypatch.setattr(cli, "_quota_policy", lambda *args: SimpleNamespace(
        builder=(Profile("codex", "gpt-x"), Profile("claude", "claude-sonnet-5-5"))))
    lines = cli._device_proof_doctor(config, workflow)
    assert lines[0] == "Device proof: android · applies to Claude builders (quota_fallback builder)"
    assert any("grants network.allowLocalBinding" in line for line in lines)
    assert lines[-1].startswith("Device proof: not applied to Codex builders")
    assert not (tmp_path / "absent.db").exists()  # doctor never creates the ledger


def test_doctor_reports_the_grant_for_a_retained_claude_session(tmp_path: Path, monkeypatch) -> None:
    import sqlite3
    from types import SimpleNamespace
    from harbor import cli

    workflow = load_workflow(materialize(tmp_path, extra='[builder]\ndevice_proof = ["ios"]\n'))
    ledger = tmp_path / "harbor.db"
    connection = sqlite3.connect(ledger)
    connection.executescript(LEDGER_SQL)
    connection.close()
    monkeypatch.setenv("HARBOR_DB_PATH", str(ledger))
    monkeypatch.setattr(cli, "_quota_policy", lambda *args: None)
    lines = cli._device_proof_doctor(SimpleNamespace(name="SampleProject", builder_provider="codex"), workflow)
    assert lines[0] == "Device proof: ios · applies to Claude builders (1 retained builder session(s))"


def test_factory_grants_a_retained_claude_session_on_a_codex_project(tmp_path: Path, monkeypatch) -> None:
    import uuid
    from harbor.runner.quota import bind_runner_identity
    from harbor.wake import Wake
    from test_github_transport import claude_production

    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(tmp_path / "projects.md"))
    (tmp_path / "projects.md").write_text("", encoding="utf-8")
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "ledger" / "harbor.db"))
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/bin/echo")
    monkeypatch.setattr("harbor.cli.sys.platform", "darwin")
    (_, _, factory), db = claude_production(tmp_path, monkeypatch, provider="codex",
                                            workflow_extra='[builder]\ndevice_proof = ["ios"]\n')
    try:
        run_id = str(uuid.uuid4())
        bind_runner_identity(db.connection, run_id, "claude", "claude-sonnet-5-5", "medium")
        built = factory(tmp_path, Wake(ticket={"identifier": "SAMPLE-1"}, action="build", resume=run_id))
        assert type(built).__name__ == "ClaudeRunner" and built.device_proof == ("ios",)
    finally:
        db.close()


# --- [builder] changes never auto-merge ----------------------------------------------------

def test_readiness_blocks_a_builder_change_and_an_unreadable_diff() -> None:
    from harbor.readiness import BUILDER_SANDBOX_CHANGED, BUILDER_SANDBOX_UNKNOWN, builder_sandbox_failures
    assert builder_sandbox_failures({"builder_sandbox_change": False}) == []
    assert builder_sandbox_failures({"builder_sandbox_change": True}) == [BUILDER_SANDBOX_CHANGED]
    assert builder_sandbox_failures({"builder_sandbox_change": None}) == [BUILDER_SANDBOX_UNKNOWN]
    assert builder_sandbox_failures({}) == [BUILDER_SANDBOX_UNKNOWN]


@pytest.mark.parametrize(("change", "reason"), [
    (True, "the PR changes .harbor/"),
    (None, "could not read the PR diff"),
    ("real", "could not read the PR diff"),  # the run's worktree is not a git repository
])
def test_pre_merge_never_auto_merges_a_builder_change(tmp_path, monkeypatch, capsys, change, reason) -> None:
    from test_workflow_risk_analysis import analysis, pre_merge, verdict
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=analysis("Risk tier: low"),
                          comments=[verdict("low")], auto_merge_risks=("low", "medium", "high"), sandbox_change=change)
    assert code == 1 and out["ready"] is False
    assert reason in out["results"][0]["reason"]
    assert any(r["hook"] == "builder_sandbox" and reason in r["reason"] for r in out["results"])


def test_pre_merge_still_auto_merges_when_builder_is_unchanged(tmp_path, monkeypatch, capsys) -> None:
    from test_workflow_risk_analysis import analysis, passes, pre_merge, verdict
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=analysis("Risk tier: low"),
                          comments=[verdict("low")], sandbox_change=False)
    assert code == 0 and passes(out)


# --- Harbor boots devices outside the sandbox ----------------------------------------------

class FakeRun:
    """subprocess.run stand-in: maps an argv prefix to (code, stdout, stderr) and records calls."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, argv, **kwargs):
        import subprocess
        self.calls.append(argv)
        for prefix, (code, out, err) in self.table.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, code, out, err)
        return subprocess.CompletedProcess(argv, 0, "", "")


def _sdk(tmp_path: Path) -> dict:
    for sub, name in (("platform-tools", "adb"), ("emulator", "emulator")):
        tool = tmp_path / "sdk" / sub / name
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
    return {"PATH": "/nonexistent", "ANDROID_HOME": str(tmp_path / "sdk"), "HOME": str(tmp_path)}


def test_android_uses_an_attached_device_without_booting(tmp_path: Path) -> None:
    from harbor.device_boot import prepare_android
    env = _sdk(tmp_path)
    adb = env["ANDROID_HOME"] + "/platform-tools/adb"
    run = FakeRun({(adb, "devices"): (0, "List of devices attached\nemulator-5554\tdevice\n", "")})
    status = prepare_android(env, run, spawn=lambda *a, **k: pytest.fail("must not boot"))
    assert status.ready and "emulator-5554 already attached" in status.detail


def test_android_boots_the_first_avd_and_waits(tmp_path: Path) -> None:
    from harbor.device_boot import prepare_android
    env = _sdk(tmp_path)
    adb, emulator = env["ANDROID_HOME"] + "/platform-tools/adb", env["ANDROID_HOME"] + "/emulator/emulator"
    spawned = []

    class Process:
        returncode = None

        def poll(self):
            return None

    def spawn(argv, **kwargs):
        spawned.append(argv)
        return Process()

    run = FakeRun({(emulator, "-list-avds"): (0, "Zeta_Phone\nAlpha_Phone\n", ""),
                   (adb, "shell", "getprop"): (0, "1\n", "")})
    status = prepare_android(env, run, spawn=spawn, sleep=lambda s: None)
    assert spawned == [[emulator, "-avd", "Alpha_Phone", "-no-snapshot-save"]]
    assert status.ready and "booted AVD Alpha_Phone" in status.detail


def test_android_failures_quote_the_literal_error(tmp_path: Path) -> None:
    from harbor.device_boot import prepare_android
    env = _sdk(tmp_path)
    adb, emulator = env["ANDROID_HOME"] + "/platform-tools/adb", env["ANDROID_HOME"] + "/emulator/emulator"
    run = FakeRun({(adb, "start-server"): (1, "", "could not install *smartsocket* listener: Operation not permitted")})
    status = prepare_android(env, run)
    assert not status.ready and "could not install *smartsocket* listener" in status.detail
    run = FakeRun({(emulator, "-list-avds"): (0, "", "")})
    assert "no AVD to boot" in prepare_android(env, run).detail
    assert "adb not found" in prepare_android({"PATH": "/nonexistent", "HOME": str(tmp_path / "none")}).detail


def test_ios_boots_the_first_iphone_on_the_newest_runtime(monkeypatch) -> None:
    from harbor.device_boot import prepare_ios
    monkeypatch.setattr("harbor.device_boot.shutil.which", lambda name, **kw: "/usr/bin/" + name)
    devices = {"devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
            {"name": "iPhone 16", "udid": "OLD", "state": "Shutdown", "isAvailable": True}],
        "com.apple.CoreSimulator.SimRuntime.iOS-27-2": [
            {"name": "iPad Air", "udid": "PAD", "state": "Shutdown", "isAvailable": True},
            {"name": "iPhone Air", "udid": "NEW", "state": "Shutdown", "isAvailable": True}],
    }}
    run = FakeRun({("xcrun", "simctl", "list"): (0, json.dumps(devices), "")})
    status = prepare_ios(run)
    assert status.ready and status.detail == "booted iPhone Air (NEW)"
    assert ["xcrun", "simctl", "boot", "NEW"] in run.calls
    devices["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-18-0"][0]["state"] = "Booted"
    run = FakeRun({("xcrun", "simctl", "list"): (0, json.dumps(devices), "")})
    assert prepare_ios(run).detail == "iPhone 16 (OLD) already booted"
    assert not any(call[:3] == ["xcrun", "simctl", "boot"] for call in run.calls)
    devices["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-18-0"][0]["state"] = "Shutdown"
    run = FakeRun({("xcrun", "simctl", "list"): (0, json.dumps(devices), ""),
                   ("xcrun", "simctl", "boot"): (149, "", "Unable to boot device in current state: Booting")})
    failed = prepare_ios(run)
    assert not failed.ready and "Unable to boot device in current state: Booting" in failed.detail


def test_the_builder_prompt_carries_the_boot_result(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    from harbor.device_boot import DeviceStatus
    from harbor.runner.codex import CodexRunner

    seen = {}

    async def fake_execute(self, context, instruction, *, resume, before_spawn=None):
        seen["instruction"] = instruction

    monkeypatch.setattr(CodexRunner, "_execute", fake_execute)
    runner = ClaudeRunner(tmp_path, model="claude-sonnet-5-5", reasoning_effort="medium", executable="claude")
    runner.device_proof = ("android", "ios", "web")
    runner.device_preparer = lambda platforms: [
        DeviceStatus("android", False, "`emulator -avd Sample` exited 1: bootstrap_check_in Permission denied (1100)"),
        DeviceStatus("ios", True, "iPhone Air (NEW) already booted")]
    asyncio.run(runner._execute(runner._new_context(None), "Build SAMPLE-1", resume=False))
    text = seen["instruction"]
    assert text.startswith("Build SAMPLE-1\n\nHarbor device proof:")
    assert "- android: NOT ready: `emulator -avd Sample` exited 1: bootstrap_check_in Permission denied (1100)" in text
    assert "- ios: ready: iPhone Air (NEW) already booted" in text and "Quote the error above" in text
    runner.device_proof = ()
    asyncio.run(runner._execute(runner._new_context(None), "Build SAMPLE-1", resume=False))
    assert seen["instruction"] == "Build SAMPLE-1"


# --- the .harbor/ diff check: GitHub SHAs only, any .harbor change, plain paths ---------------

def _git(repo: Path, *args: str) -> str:
    import subprocess
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Sample", "-c", "user.email=sample@example.test", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _write(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


WORKFLOW = ".harbor/workflow/workflow.toml"


def _repo_with_workflow(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose main has a regular workflow, checked out on a feature branch; returns (repo, base SHA)."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, WORKFLOW, 'version = 1\n[delivery]\nmerge_mode = "human"\n')
    _write(repo, "src/app.py", "print('hi')\n")
    base = _commit_all(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo, base


def test_only_github_shas_are_accepted(tmp_path: Path) -> None:
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path)
    _write(repo, "src/app.py", "print('changed')\n")
    head = _commit_all(repo, "code only")
    assert builder_sandbox_change(repo / ".git", head, base) is False
    for bad_base in ("main", "origin/main", base[:12], "HEAD"):
        assert builder_sandbox_change(repo / ".git", head, bad_base) is None
    assert builder_sandbox_change(repo / ".git", "feature", base) is None
    assert builder_sandbox_change(tmp_path / "missing", head, base) is None
    assert builder_sandbox_change(None, head, base) is None


@pytest.mark.parametrize("forgery", [("tag", "origin/main"), ("update-ref", "refs/remotes/origin/main")])
def test_a_forged_origin_base_cannot_hide_a_harbor_change(tmp_path: Path, forgery) -> None:
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path)
    _write(repo, WORKFLOW, 'version = 1\n[delivery]\nmerge_mode = "human"\n[builder]\ndevice_proof = ["ios"]\n')
    head = _commit_all(repo, "grant device_proof")
    command, ref = forgery
    _git(repo, command, ref, head)  # what a sandboxed builder can run in its own worktree
    # The forgery works against anything that trusts the local ref: the merge base becomes the head itself.
    assert _git(repo, "merge-base", ref, head) == head
    assert builder_sandbox_change(repo / ".git", head, base) is True


def test_any_case_folded_harbor_change_counts(tmp_path: Path) -> None:
    from harbor.device_proof import builder_sandbox_change
    for name, path in (("upper file", ".harbor/workflow/WORKFLOW.toml"), ("upper dir", ".HARBOR/workflow/workflow.toml"),
                       ("mixed", ".Harbor/workflow/config.toml"), ("config", ".harbor/workflow/config.toml"),
                       ("pipeline", ".harbor/pipeline.md")):
        repo, base = _repo_with_workflow(tmp_path / name.replace(" ", "-"))
        _write(repo, path, '[builder]\ndevice_proof = ["android"]\n')
        assert builder_sandbox_change(repo / ".git", _commit_all(repo, name), base) is True, path
    repo, base = _repo_with_workflow(tmp_path / "unrelated")
    _write(repo, "docs/harbor.md", "a .harbor mention outside .harbor/\n")
    assert builder_sandbox_change(repo / ".git", _commit_all(repo, "docs"), base) is False


def test_a_symlinked_workflow_dir_cannot_hide_a_builder_grant(tmp_path: Path) -> None:
    import os
    import tomllib
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path)
    # PR1: move workflow.toml to cfg/ and make .harbor/workflow a symlink to it.
    (repo / "cfg").mkdir()
    (repo / WORKFLOW).rename(repo / "cfg" / "workflow.toml")
    (repo / ".harbor" / "workflow").rmdir()
    os.symlink("../cfg", repo / ".harbor" / "workflow")
    pr1 = _commit_all(repo, "PR1: symlink the workflow dir")
    # PR1 itself touches .harbor/ (True); either way it is refused, never False.
    assert builder_sandbox_change(repo / ".git", pr1, base) is True
    # PR2, as if PR1 had merged: grant device_proof through the symlink target, outside .harbor/.
    _write(repo, "cfg/workflow.toml", 'version = 1\n[builder]\ndevice_proof = ["ios"]\n')
    pr2 = _commit_all(repo, "PR2: grant device_proof via cfg/")
    assert builder_sandbox_change(repo / ".git", pr2, pr1) is None
    # The runtime follows the link, which is why the check must not read "unchanged".
    assert tomllib.loads((repo / WORKFLOW).read_text())["builder"]["device_proof"] == ["ios"]


def test_symlinked_harbor_dir_workflow_file_submodule_and_missing_are_unreadable(tmp_path: Path) -> None:
    import os
    import shutil
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path / "harbor-link")
    shutil.move(str(repo / ".harbor"), str(repo / "elsewhere"))
    os.symlink("elsewhere", repo / ".harbor")
    assert builder_sandbox_change(repo / ".git", _commit_all(repo, "symlink .harbor"), base) is not False
    repo, base = _repo_with_workflow(tmp_path / "file-link")
    (repo / WORKFLOW).rename(repo / ".harbor" / "workflow" / "real.toml")
    os.symlink("real.toml", repo / WORKFLOW)
    assert builder_sandbox_change(repo / ".git", _commit_all(repo, "symlink workflow.toml"), base) is not False
    repo, base = _repo_with_workflow(tmp_path / "submodule")
    _git(repo, "rm", "-q", "-r", ".harbor/workflow")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},.harbor/workflow")
    _git(repo, "-c", "user.name=Sample", "-c", "user.email=sample@example.test", "commit", "-q", "-m", "submodule")
    head = _git(repo, "rev-parse", "HEAD")
    assert "160000 commit" in _git(repo, "ls-tree", head, "--", ".harbor/workflow")
    assert builder_sandbox_change(repo / ".git", head, base) is not False
    # Deleting the tracked workflow is a .harbor/ change: True, which needs a human merge just like None.
    repo, base = _repo_with_workflow(tmp_path / "missing")
    (repo / WORKFLOW).unlink()
    assert builder_sandbox_change(repo / ".git", _commit_all(repo, "delete workflow"), base) is True


def test_a_commit_missing_locally_fails_closed(tmp_path: Path) -> None:
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path)
    head = base
    absent = "1" * 40
    assert builder_sandbox_change(repo / ".git", absent, base) is None  # no origin to fetch it from
    assert builder_sandbox_change(repo / ".git", head, absent) is None


def test_a_missing_commit_is_fetched_from_harbors_url_into_a_throwaway_repo(tmp_path: Path) -> None:
    import subprocess
    from harbor.device_proof import builder_sandbox_change
    upstream, base = _repo_with_workflow(tmp_path / "upstream")
    _write(upstream, "src/app.py", "print('upstream')\n")
    head = _commit_all(upstream, "only upstream has this")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--no-local", "--single-branch", "--branch", "main", str(upstream), str(clone))
    assert builder_sandbox_change(clone / ".git", head, base) is None  # no fetch URL: fail closed
    assert builder_sandbox_change(clone / ".git", head, base, fetch_url=str(upstream / ".git")) is False
    # The fetch went into Harbor's private repository; the builder's repository is not written.
    missing = subprocess.run(["git", "-C", str(clone), "cat-file", "-e", head], capture_output=True)
    assert missing.returncode != 0


# --- Harbor's merge command ------------------------------------------------------------------

class FakeGitHub:
    """Client stand-in for harbor.github_rest.run: answers GET with a PR and records every request."""

    def __init__(self, head: str, base: str):
        self.pr = {"number": 4, "head": {"sha": head}, "base": {"sha": base, "ref": "main"}}
        self.requests = []

    def request(self, method, path, payload=None):
        self.requests.append((method.upper(), path, payload))
        return self.pr if method.upper() == "GET" else {"merged": True}


def _merge_repo(tmp_path: Path, monkeypatch, workflow: str | None = None) -> tuple[str, str]:
    repo, base = _repo_with_workflow(tmp_path)
    if workflow is None:
        _write(repo, "src/app.py", "print('feature')\n")
    else:
        _write(repo, WORKFLOW, workflow)
    head = _commit_all(repo, "PR head")
    monkeypatch.chdir(repo)
    return head, base


GRANT = 'version = 1\n[delivery]\nmerge_mode = "human"\n[builder]\ndevice_proof = ["android"]\n'


def test_pr_merge_refuses_a_harbor_change_even_with_forged_refs(tmp_path: Path, monkeypatch) -> None:
    from harbor.github_rest import GitHubRestError, run
    head, base = _merge_repo(tmp_path, monkeypatch, GRANT)
    _git(Path.cwd(), "tag", "origin/main", head)
    _git(Path.cwd(), "update-ref", "refs/remotes/origin/main", head)
    gh = FakeGitHub(head, base)
    with pytest.raises(GitHubRestError, match=r"refusing to merge PR #4: human merge required: the PR changes \.harbor/"):
        run(["--repo", "o/r", "pr-merge", "4", "--sha", head], env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert [m for m, _, _ in gh.requests] == ["GET"]  # never sent the merge


def test_pr_merge_refuses_a_sha_that_is_not_the_pr_head(tmp_path: Path, monkeypatch) -> None:
    from harbor.github_rest import GitHubRestError, run
    head, base = _merge_repo(tmp_path, monkeypatch)
    gh = FakeGitHub(head, base)
    with pytest.raises(GitHubRestError, match="is not the PR's current head"):
        run(["--repo", "o/r", "pr-merge", "4", "--sha", base], env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert [m for m, _, _ in gh.requests] == ["GET"]


def test_pr_merge_refuses_when_the_diff_cannot_be_read(tmp_path: Path, monkeypatch) -> None:
    from harbor.github_rest import GitHubRestError, run
    monkeypatch.chdir(tmp_path)  # not a git checkout
    gh = FakeGitHub("a" * 40, "b" * 40)
    with pytest.raises(GitHubRestError, match="could not read the PR diff"):
        run(["--repo", "o/r", "pr-merge", "4", "--sha", "a" * 40], env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert [m for m, _, _ in gh.requests] == ["GET"]


def test_pr_merge_and_pr_comment_still_work(tmp_path: Path, monkeypatch) -> None:
    from harbor.github_rest import run
    head, base = _merge_repo(tmp_path, monkeypatch)
    gh = FakeGitHub(head, base)
    assert run(["--repo", "o/r", "pr-merge", "4", "--sha", head], env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh) == {"merged": True}
    assert gh.requests[-1][:2] == ("PUT", "/repos/o/r/pulls/4/merge")
    note = tmp_path / "note.md"
    note.write_text("Thanks, fixed.")
    run(["--repo", "o/r", "pr-comment", "4", "--body-file", str(note)], env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert gh.requests[-1] == ("POST", "/repos/o/r/issues/4/comments", {"body": "Thanks, fixed."})


# --- the api helper: an allowlist ------------------------------------------------------------

@pytest.mark.parametrize(("method", "path", "body"), [
    # The reviewer's bypasses of the old deny-list.
    ("PUT", "/repositories/123/pulls/4/merge", '{"sha": "x"}'),
    ("PUT", "/repos/o/r/pulls/+4/merge", '{"sha": "x"}'),
    ("POST", "/repositories/123/merges", '{"base": "main", "head": "feature"}'),
    ("POST", "/graphql", '{"query": "mutation { \\u006dergePullRequest(input: {pullRequestId: \\"x\\"}) { clientMutationId } }"}'),
    # Merges, refs, contents and branch protection.
    ("PUT", "/repos/o/r/pulls/4/merge", '{"sha": "x"}'),
    ("POST", "/repos/o/r/merges", '{"base": "main", "head": "feature"}'),
    ("POST", "/repos/o/r/git/refs", '{"ref": "refs/heads/main", "sha": "x"}'),
    ("PATCH", "/repos/o/r/git/refs/heads/main", '{"sha": "x", "force": true}'),
    ("PUT", "/repos/o/r/contents/.harbor/workflow/workflow.toml", '{"message": "m", "content": "x"}'),
    ("DELETE", "/repos/o/r/branches/main/protection", None),
    ("PUT", "/repos/o/r/pulls/4/merge?x=1", '{"sha": "x"}'),
    ("POST", "/repos/o/r/issues/4/comments/../../../merges", "{}"),
    ("POST", "/repos/other/repo/issues/4/comments", '{"body": "x"}'),
    # GraphQL that the old regex missed or that does not parse.
    ("POST", "/graphql", '{"query": "mutation { resolveReviewThread: mergePullRequest(input: {}) { clientMutationId } }"}'),
    ("POST", "/graphql", '{"query": "query { viewer { login } }\\n# harmless\\nmutation { enablePullRequestAutoMerge(input: {}) { clientMutationId } }"}'),
    ("POST", "/graphql", '{"query": "mutation { ...M }  fragment M on Mutation { mergePullRequest(input: {}) { clientMutationId } }"}'),
    ("POST", "/graphql", '{"query": "mutation { updateRef(input: {}) { clientMutationId } }"}'),
    ("POST", "/graphql", '[{"query": "query { viewer { login } }"}]'),
    ("POST", "/graphql", '{"query": "mutation { resolveReviewThread(input: {threadId: \\"x\\"}) { thread { id } "}'),
    ("POST", "/graphql", "not json"),
])
def test_the_api_helper_refuses_everything_off_the_allowlist(method, path, body, tmp_path) -> None:
    from harbor.github_rest import GitHubRestError, run
    gh = FakeGitHub("a" * 40, "b" * 40)
    argv = ["api", method, path]
    if body is not None:
        (tmp_path / "body.json").write_text(body)
        argv += ["--input", str(tmp_path / "body.json")]
    with pytest.raises(GitHubRestError, match="refusing api call"):
        run(argv, env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert gh.requests == []


@pytest.mark.parametrize(("method", "path", "body"), [
    ("GET", "/repos/o/r/pulls/4", None),
    ("GET", "/repositories/123/pulls/4/merge", None),
    ("HEAD", "/repos/o/r", None),
    ("POST", "/repos/o/r/issues/4/comments", '{"body": "x"}'),
    ("PATCH", "/repos/o/r/issues/comments/99", '{"body": "x"}'),
    ("POST", "/repos/o/r/pulls/4/comments/99/replies", '{"body": "x"}'),
    ("POST", "/repos/o/r/pulls/4/reviews", '{"event": "COMMENT", "body": "x"}'),
    ("POST", "/repos/o/r/issues/4/labels", '{"labels": ["needs-review"]}'),
    ("DELETE", "/repos/o/r/issues/4/labels/needs-review", None),
    ("POST", "/graphql", '{"query": "query { viewer { login } }"}'),
    ("POST", "/graphql", '{"query": "mutation Reply($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }", "variables": {"id": "x"}}'),
    ("POST", "/graphql", '{"query": "mutation { r: addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: \\"x\\", body: \\"mutation { mergePullRequest }\\"}) { comment { id } } }"}'),
])
def test_the_api_helper_allows_reads_and_the_workflow_writes(method, path, body, tmp_path) -> None:
    from harbor.github_rest import run
    gh = FakeGitHub("a" * 40, "b" * 40)
    argv = ["api", method, path]
    if body is not None:
        (tmp_path / "body.json").write_text(body)
        argv += ["--input", str(tmp_path / "body.json")]
    run(argv, env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert gh.requests and gh.requests[0][:2] == (method, path)


def test_pre_merge_checks_githubs_head_and_base_shas_not_local_refs(tmp_path, monkeypatch, capsys) -> None:
    from test_workflow_risk_analysis import HEAD, analysis, pre_merge, verdict
    seen = []

    def record(git_dir, head, base, **kwargs):
        seen.append((git_dir, head, base, kwargs.get("fetch_url")))
        return False

    monkeypatch.setattr("harbor.device_proof.builder_sandbox_change", record)
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    base = "b" * 40
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=analysis("Risk tier: low"),
                          comments=[verdict("low")], sandbox_change="real", base_oid=base, repo_path=str(main))
    # GitHub's SHAs, the MAIN repository's git dir from Harbor's config (not the run's worktree, which is
    # tmp_path here), and Harbor's own fetch URL rather than the worktree's origin.
    assert seen == [((main / ".git").resolve(), HEAD, base, "https://github.com/example/repo.git")] and code == 0
    seen.clear()
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=analysis("Risk tier: low"),
                          comments=[verdict("low")], sandbox_change="real", base_oid=None, repo_path=str(main))
    assert [entry[2] for entry in seen] == [None]


# --- fourth review: untracked .harbor/, planted git config, shallow files ----------------------

def _repo_without_harbor(tmp_path: Path) -> tuple[Path, str]:
    """A project that keeps .harbor/ untracked (Harbor reads it from the main checkout)."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "app.txt", "v1\n")
    base = _commit_all(repo, "base without .harbor")
    _write(repo, WORKFLOW, 'version = 1\n[builder]\ndevice_proof = ["ios"]\n')  # untracked, like Bookgen's
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo, base


def test_an_untracked_harbor_dir_does_not_block_app_only_prs(tmp_path: Path) -> None:
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_without_harbor(tmp_path)
    _write(repo, "app.txt", "v2\n")
    _git(repo, "add", "app.txt")
    _git(repo, "-c", "user.name=Sample", "-c", "user.email=sample@example.test", "commit", "-q", "-m", "app only")
    head = _git(repo, "rev-parse", "HEAD")
    assert "?? .harbor/" in _git(repo, "status", "--porcelain")
    assert _git(repo, "ls-tree", base, "--", ".harbor") == ""
    assert builder_sandbox_change(repo / ".git", head, base) is False


@pytest.mark.parametrize("path", [WORKFLOW, ".HARBOR/workflow/workflow.toml", ".harbor/pipeline.md"])
def test_a_pr_that_starts_tracking_harbor_counts(tmp_path: Path, path: str) -> None:
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_without_harbor(tmp_path)
    _write(repo, path, '[builder]\ndevice_proof = ["android"]\n')
    _git(repo, "add", "-f", path)
    _git(repo, "-c", "user.name=Sample", "-c", "user.email=sample@example.test", "commit", "-q", "-m", "track .harbor")
    assert builder_sandbox_change(repo / ".git", _git(repo, "rev-parse", "HEAD"), base) is True


def test_planted_git_config_never_runs(tmp_path: Path) -> None:
    """The reviewer's repro: the supervisor's fetch ran remote.origin.uploadpack from config the builder controls."""
    from harbor.device_proof import builder_sandbox_change
    upstream, base = _repo_with_workflow(tmp_path / "upstream")
    _write(upstream, WORKFLOW, 'version = 1\n[builder]\ndevice_proof = ["ios"]\n')
    head = _commit_all(upstream, "grant, only upstream has it")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--no-local", "--single-branch", "--branch", "main", str(upstream), str(clone))
    pwned = tmp_path / "PWNED"
    hooks = clone / ".git" / "evil-hooks"
    hooks.mkdir()
    for hook in ("post-checkout", "reference-transaction", "post-merge", "fsmonitor-watchman"):
        (hooks / hook).write_text(f"#!/bin/sh\ntouch {pwned}-{hook}\n")
        (hooks / hook).chmod(0o755)
    for key, value in (("remote.origin.uploadpack", f"touch {pwned}; git-upload-pack"),
                       ("core.fsmonitor", f"touch {pwned}-fsmonitor; false"), ("core.hooksPath", str(hooks)),
                       ("core.sshCommand", f"touch {pwned}-ssh; false"),
                       ("uploadpack.packObjectsHook", f"touch {pwned}-pack; git"),
                       ("core.alternateRefsCommand", f"touch {pwned}-altrefs; true")):
        _git(clone, "config", key, value)
    # Harbor fetches from its own configured URL, never from the planted origin, and reads none of that config.
    assert builder_sandbox_change(clone / ".git", head, base, fetch_url=str(upstream / ".git")) is True
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("PWNED")) == []


def test_a_shallow_file_in_the_repository_is_ignored(tmp_path: Path) -> None:
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path)
    _write(repo, WORKFLOW, 'version = 1\n[builder]\ndevice_proof = ["ios"]\n')
    grant = _commit_all(repo, "grant")
    _write(repo, "src/app.py", "print('later')\n")
    head = _commit_all(repo, "later")
    # The builder can write .git/shallow: cut the history at the grant so its parent (the base) disappears.
    (repo / ".git" / "shallow").write_text(f"{grant}\n")
    assert _git(repo, "rev-list", "--count", head) == "2"  # git in the builder's repo now sees a cut history
    assert builder_sandbox_change(repo / ".git", head, base) is True


@pytest.mark.parametrize(("body", "allowed"), [
    ('{"event": "APPROVE", "body": "lgtm"}', False),
    ('{"body": "pending review, no event"}', False),
    ('{"event": "approve"}', False),
    ("not json", False),
    ('{"event": "COMMENT", "body": "x"}', True),
    ('{"event": "REQUEST_CHANGES", "body": "x"}', True),
])
def test_a_builder_can_review_but_never_approve(body, allowed, tmp_path) -> None:
    from harbor.github_rest import GitHubRestError, run
    (tmp_path / "review.json").write_text(body)
    gh = FakeGitHub("a" * 40, "b" * 40)
    argv = ["api", "POST", "/repos/o/r/pulls/4/reviews", "--input", str(tmp_path / "review.json")]
    if allowed:
        run(argv, env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
        assert gh.requests[0][:2] == ("POST", "/repos/o/r/pulls/4/reviews")
    else:
        with pytest.raises(GitHubRestError, match="refusing api call"):
            run(argv, env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
        assert gh.requests == []


def test_the_configured_repository_wins_over_a_builder_supplied_repo(tmp_path) -> None:
    from harbor.github_rest import GitHubRestError, run
    gh = FakeGitHub("a" * 40, "b" * 40)
    with pytest.raises(GitHubRestError, match="--repo attacker/repo is not the configured repository o/r"):
        run(["--repo", "attacker/repo", "pr-view", "4"], env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    with pytest.raises(GitHubRestError, match="no repository configured"):
        run(["--repo", "o/r", "pr-view", "4"], env={}, client=gh)
    with pytest.raises(GitHubRestError, match="no repository configured"):
        run(["api", "GET", "/repos/o/r/pulls/4"], env={}, client=gh)
    assert gh.requests == []
    run(["--repo", "O/R", "pr-view", "4"], env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert gh.requests == [("GET", "/repos/o/r/pulls/4", None)]


def test_the_web_platform_tells_the_builder_how_to_launch_chromium(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    from harbor.runner.codex import CodexRunner

    seen = {}

    async def fake_execute(self, context, instruction, *, resume, before_spawn=None):
        seen["instruction"] = instruction

    monkeypatch.setattr(CodexRunner, "_execute", fake_execute)
    runner = ClaudeRunner(tmp_path, model="claude-sonnet-5-5", reasoning_effort="medium", executable="claude")
    runner.device_proof = ("web",)
    runner.device_preparer = lambda platforms: pytest.fail("web needs no device boot")
    asyncio.run(runner._execute(runner._new_context(None), "Build SAMPLE-1", resume=False))
    assert "--single-process" in seen["instruction"] and "MachPortRendezvousServer" in seen["instruction"]
    assert "setViewportSize" in seen["instruction"]


# --- fifth review: authenticate the missing-commit fetch with Harbor's token -------------------

FAKE_TOKEN = "ghp_sampleTOKEN0123456789abcdefABCDEF"


def _capture_fetch(monkeypatch, stderr: str = "") -> list:
    """Record git fetch calls made by the check instead of touching the network; other git runs go through."""
    import subprocess
    import harbor.device_proof as dp
    calls, real = [], subprocess.run

    def fake(argv, **kwargs):
        if "fetch" in argv:
            calls.append((list(argv), dict(kwargs.get("env") or {})))
            return subprocess.CompletedProcess(argv, 128, "", stderr)
        return real(argv, **kwargs)

    monkeypatch.setattr(dp.subprocess, "run", fake)
    return calls


def test_the_fetch_sends_the_token_as_a_scoped_header_in_the_environment(tmp_path: Path, monkeypatch) -> None:
    import base64
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path)
    missing = "1" * 40
    calls = _capture_fetch(monkeypatch)
    why: list[str] = []
    assert builder_sandbox_change(repo / ".git", base, missing, fetch_url="https://github.com/o/r.git",
                                  token=FAKE_TOKEN, reasons=why) is None
    [(argv, env)] = calls
    basic = base64.b64encode(f"x-access-token:{FAKE_TOKEN}".encode()).decode()
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"  # this origin only
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {basic}"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert not any(FAKE_TOKEN in arg or basic in arg for arg in argv)  # never visible in ps
    assert "--upload-pack=git-upload-pack" in argv and "https://github.com/o/r.git" in argv
    assert why and FAKE_TOKEN not in why[0] and basic not in why[0]


def test_fetch_errors_never_carry_the_token(tmp_path: Path, monkeypatch) -> None:
    import base64
    from harbor.device_proof import builder_sandbox_change
    from harbor.github_rest import GitHubRestError, run
    basic = base64.b64encode(f"x-access-token:{FAKE_TOKEN}".encode()).decode()
    leak = f"fatal: unable to access 'https://x-access-token:{FAKE_TOKEN}@github.com/o/r.git/': header {basic} rejected"
    repo, base = _repo_with_workflow(tmp_path)
    _capture_fetch(monkeypatch, stderr=leak)
    why: list[str] = []
    builder_sandbox_change(repo / ".git", base, "1" * 40, fetch_url="https://github.com/o/r.git",
                           token=FAKE_TOKEN, reasons=why)
    assert "could not fetch commit" in why[0] and "[redacted]" in why[0]
    assert FAKE_TOKEN not in why[0] and basic not in why[0]
    monkeypatch.chdir(repo)
    gh = FakeGitHub(base, "1" * 40)
    env = {"HARBOR_GITHUB_REPOSITORY": "o/r", "HARBOR_GIT_FETCH_URL": "https://github.com/o/r.git",
           "HARBOR_GITHUB_TOKEN_ENV": "SAMPLE_TOKEN", "SAMPLE_TOKEN": FAKE_TOKEN}
    with pytest.raises(GitHubRestError) as caught:
        run(["pr-merge", "4", "--sha", base], env=env, client=gh)
    message = str(caught.value)
    assert "could not fetch commit" in message
    assert FAKE_TOKEN not in message and basic not in message


def test_no_token_fails_closed_with_a_clear_reason(tmp_path: Path, monkeypatch) -> None:
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path)
    calls = _capture_fetch(monkeypatch)
    why: list[str] = []
    assert builder_sandbox_change(repo / ".git", base, "1" * 40, fetch_url="https://github.com/o/r.git",
                                  token=None, reasons=why) is None
    assert calls == []  # nothing to authenticate with, so no fetch is attempted
    assert "no GitHub token is configured" in why[0]


# --- sixth review: criss-cross merges ------------------------------------------------------

def _dated_commit(repo: Path, message: str, when: int, *parents_to_merge: str) -> str:
    import os
    import subprocess
    env = {**os.environ, "GIT_AUTHOR_DATE": f"@{when} +0000", "GIT_COMMITTER_DATE": f"@{when} +0000",
           "GIT_AUTHOR_NAME": "Sample", "GIT_AUTHOR_EMAIL": "sample@example.test",
           "GIT_COMMITTER_NAME": "Sample", "GIT_COMMITTER_EMAIL": "sample@example.test"}
    if parents_to_merge:
        subprocess.run(["git", "-C", str(repo), "merge", "-q", "--no-ff", "--no-commit", "-s", "ours",
                        *parents_to_merge], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", message],
                   check=True, capture_output=True, env=env)
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("x1_first", [True, False], ids=["X1-older", "X2-older"])
def test_a_criss_cross_merge_cannot_hide_a_harbor_change(tmp_path: Path, x1_first: bool) -> None:
    """The reviewer's repro: a single merge base (X1) shows no change, but the merge GitHub makes lands 'v1 grant'."""
    from harbor.device_proof import builder_sandbox_change
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    t = 1_700_000_000
    _write(repo, WORKFLOW, "v1 grant\n")
    _write(repo, "app.txt", "0\n")
    y = _dated_commit(repo, "Y: v1 grant", t)
    x1_time, x2_time = (t + 10, t + 20) if x1_first else (t + 20, t + 10)
    _git(repo, "checkout", "-q", "-b", "x1", y)
    _write(repo, "app.txt", "x1\n")
    x1 = _dated_commit(repo, "X1: leaves workflow.toml alone", x1_time)
    _git(repo, "checkout", "-q", "main")
    _write(repo, WORKFLOW, "v2 nogrant\n")
    x2 = _dated_commit(repo, "X2: v2 nogrant", x2_time)
    # Main merges X1 (keeping v2 nogrant): B.
    _git(repo, "checkout", "-q", "main")
    _write(repo, "app.txt", "x1\n")
    b = _dated_commit(repo, "B: merge X1 into main", t + 30, x1)
    # The PR, on X1, merges X2 and resets the file to v1 grant in the merge commit: H.
    _git(repo, "checkout", "-q", "-B", "pr", x1)
    _write(repo, WORKFLOW, "v1 grant\n")
    h = _dated_commit(repo, "H: merge X2, keep v1 grant", t + 40, x2)
    bases = set(_git(repo, "merge-base", "--all", b, h).split())
    assert bases == {x1, x2}  # criss-cross: two merge bases
    landed = _git(repo, "merge-tree", "--write-tree", b, h).splitlines()[0]
    assert _git(repo, "cat-file", "-p", f"{landed}:{WORKFLOW}") == "v1 grant"  # what GitHub would merge
    assert _git(repo, "cat-file", "-p", f"{b}:{WORKFLOW}") == "v2 nogrant"
    assert builder_sandbox_change(repo / ".git", h, b) is True


def test_a_conflicting_merge_is_unknown(tmp_path: Path) -> None:
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path)
    _write(repo, "src/app.py", "print('pr')\n")
    head = _commit_all(repo, "pr side")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "src/app.py", "print('main')\n")
    main = _commit_all(repo, "main side")
    why: list[str] = []
    assert builder_sandbox_change(repo / ".git", head, main, reasons=why) is None
    assert "conflicts" in why[0]


def test_a_change_that_main_already_has_does_not_count(tmp_path: Path) -> None:
    """The merge result is compared with the base, so a .harbor/ edit already on main is not the PR's change."""
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path)
    _git(repo, "checkout", "-q", "main")
    _write(repo, WORKFLOW, 'version = 1\n[delivery]\nmerge_mode = "workflow_checked"\n')
    main = _commit_all(repo, "main edits .harbor/ itself")
    _git(repo, "checkout", "-q", "feature")
    _write(repo, "src/app.py", "print('feature')\n")
    head = _commit_all(repo, "app only")
    assert builder_sandbox_change(repo / ".git", head, main) is False


# --- seventh review: rebase replay, GHES fetch host, the no-write-tree fallback ----------------

IOS = 'version = 1\n[builder]\ndevice_proof = ["ios"]\n'
NONE = 'version = 1\n[builder]\ndevice_proof = []\n'


def test_a_rebase_cannot_replay_a_revoked_grant_back(tmp_path: Path) -> None:
    """The reviewer's repro: the ort merge keeps main's revocation, but `git rebase` lands ios again."""
    from harbor.device_proof import builder_sandbox_change
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, WORKFLOW, IOS)
    _write(repo, "app.txt", "0\n")
    m0 = _commit_all(repo, "M0: ios")
    _write(repo, WORKFLOW, NONE)
    m1 = _commit_all(repo, "M1: a human revokes it")
    _git(repo, "checkout", "-q", "-b", "pr", m0)
    _write(repo, WORKFLOW, NONE)
    _commit_all(repo, "c1: the same change as M1")
    _write(repo, WORKFLOW, IOS)
    _write(repo, "app.txt", "1\n")
    head = _commit_all(repo, "c2: ios again, plus an app edit")
    landed = _git(repo, "merge-tree", "--write-tree", m1, head).splitlines()[0]
    assert _git(repo, "cat-file", "-p", f"{landed}:{WORKFLOW}") == NONE.strip()  # ort keeps the revocation
    _git(repo, "checkout", "-q", "-b", "rebased", head)
    _git(repo, "-c", "user.name=Sample", "-c", "user.email=sample@example.test", "rebase", "-q", m1)
    assert (repo / WORKFLOW).read_text() == IOS  # ...but a rebase merge lands ios
    assert builder_sandbox_change(repo / ".git", head, m1) is True


def test_a_pr_whose_commits_never_touch_harbor_is_still_unchanged(tmp_path: Path) -> None:
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path)
    for n in range(3):
        _write(repo, "src/app.py", f"print({n})\n")
        _commit_all(repo, f"app {n}")
    _git(repo, "merge", "-q", "--no-edit", "main")  # a merge commit that brings nothing new
    head = _git(repo, "rev-parse", "HEAD")
    assert builder_sandbox_change(repo / ".git", head, base) is False


def test_pr_merge_rebase_is_refused_when_the_per_commit_check_cannot_run(tmp_path: Path, monkeypatch) -> None:
    import harbor.device_proof as dp
    from harbor.github_rest import GitHubRestError, run
    head, base = _merge_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_commits_touching_harbor", lambda *args: None)
    gh = FakeGitHub(head, base)
    with pytest.raises(GitHubRestError, match="could not list the commits"):
        run(["pr-merge", "4", "--sha", head, "--method", "rebase"], env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert [m for m, _, _ in gh.requests] == ["GET"]


@pytest.mark.parametrize(("api_url", "fetch_url"), [
    (None, "https://github.com/o/r.git"),
    ("https://api.github.com/graphql", "https://github.com/o/r.git"),
    ("https://api.corp.example/api/graphql", "https://api.corp.example/o/r.git"),
    ("https://ghe.corp.example/api/graphql", "https://ghe.corp.example/o/r.git"),
    ("https://api.octo.ghe.com/graphql", "https://octo.ghe.com/o/r.git"),
    ("https://ghe.corp.example:8443/api/graphql", "https://ghe.corp.example:8443/o/r.git"),
])
def test_the_fetch_url_keeps_the_configured_host(api_url, fetch_url) -> None:
    from harbor.device_proof import git_fetch_url
    assert git_fetch_url(api_url, "o/r") == fetch_url


def test_a_ghes_token_goes_to_the_api_host_never_a_stripped_one(tmp_path: Path, monkeypatch) -> None:
    from harbor.device_proof import builder_sandbox_change, git_fetch_url
    url = git_fetch_url("https://api.corp.example/api/graphql", "o/r")
    repo, base = _repo_with_workflow(tmp_path)
    calls = _capture_fetch(monkeypatch)
    builder_sandbox_change(repo / ".git", base, "1" * 40, fetch_url=url, token=FAKE_TOKEN)
    [(argv, env)] = calls
    assert env["GIT_CONFIG_KEY_0"] == "http.https://api.corp.example/.extraheader"
    assert "https://api.corp.example/o/r.git" in argv
    assert not any("//corp.example" in value for value in [*argv, *env.values()])


def test_without_merge_tree_write_tree_every_merge_base_is_checked(tmp_path: Path, monkeypatch) -> None:
    """git before 2.38 exits 129 on `merge-tree --write-tree`; the check falls back to merge-base --all."""
    import subprocess
    import harbor.device_proof as dp
    real = subprocess.run

    def old_git(argv, **kwargs):
        if "merge-tree" in argv:
            return subprocess.CompletedProcess(argv, 129, "", "usage: git merge-tree <base-tree> <branch1> <branch2>\n")
        return real(argv, **kwargs)

    monkeypatch.setattr(dp.subprocess, "run", old_git)
    repo, base = _repo_with_workflow(tmp_path / "unchanged")
    _write(repo, "src/app.py", "print('x')\n")
    assert dp.builder_sandbox_change(repo / ".git", _commit_all(repo, "app"), base) is False
    repo, base = _repo_with_workflow(tmp_path / "changed")
    _write(repo, WORKFLOW, IOS)
    assert dp.builder_sandbox_change(repo / ".git", _commit_all(repo, "grant"), base) is True


# --- eighth review: rebase is refused when .harbor/ is tracked; tighter allowlist -------------

W = 'version = 1\n[delivery]\nmerge_mode = "human"\n[builder]\ndevice_proof = []\n'


def _rename_recipe(tmp_path: Path) -> tuple[Path, str, str]:
    """The reviewer's recipe: a rebase lands c2's edit to docs/x.toml in .harbor/workflow/workflow.toml."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, WORKFLOW, W)
    a = _commit_all(repo, "A: tracks .harbor/workflow/workflow.toml = W")
    _write(repo, "docs/x.toml", W)
    _commit_all(repo, "PR1: docs/x.toml = W")
    (repo / "docs" / "x.toml").unlink()
    base = _commit_all(repo, "PR2: delete docs/x.toml")
    _git(repo, "checkout", "-q", "-b", "attack", a)
    _write(repo, "docs/x.toml", W)
    _commit_all(repo, "c1: docs/x.toml = W (same patch as PR1)")
    _write(repo, "docs/x.toml", W.replace("device_proof = []", 'device_proof = ["ios"]'))
    head = _commit_all(repo, "c2: edit docs/x.toml to ios")
    return repo, base, head


def test_the_check_does_not_model_the_rename_recipe(tmp_path: Path) -> None:
    """The check sees no .harbor/ change for the recipe: correct for squash/merge (the ort result leaves
    .harbor/ alone), but it does not model a rebase's rename detection, so rebase is refused by policy."""
    from harbor.device_proof import builder_sandbox_change
    repo, base, head = _rename_recipe(tmp_path)
    facts: dict = {}
    assert builder_sandbox_change(repo / ".git", head, base, facts=facts) is False
    assert facts["harbor_tracked"] is True


def test_pr_merge_refuses_rebase_when_harbor_is_tracked(tmp_path: Path, monkeypatch) -> None:
    from harbor.github_rest import GitHubRestError, run
    repo, base, head = _rename_recipe(tmp_path)
    monkeypatch.chdir(repo)
    env = {"HARBOR_GITHUB_REPOSITORY": "o/r"}
    gh = FakeGitHub(head, base)
    with pytest.raises(GitHubRestError, match=r"human merge required: rebase merges can't be checked for \.harbor/ changes"):
        run(["pr-merge", "4", "--sha", head, "--method", "rebase"], env=env, client=gh)
    assert [m for m, _, _ in gh.requests] == ["GET"]  # no PUT
    # The default (squash) merge lands the ort result, which leaves .harbor/ alone, so it still goes through.
    gh = FakeGitHub(head, base)
    assert run(["pr-merge", "4", "--sha", head], env=env, client=gh) == {"merged": True}
    assert gh.requests[-1][:2] == ("PUT", "/repos/o/r/pulls/4/merge")
    assert gh.requests[-1][2]["merge_method"] == "squash"


def test_an_untracked_harbor_project_may_still_rebase(tmp_path: Path, monkeypatch) -> None:
    from harbor.github_rest import run
    repo, base = _repo_without_harbor(tmp_path)
    _write(repo, "app.txt", "v2\n")
    _git(repo, "add", "app.txt")
    _git(repo, "-c", "user.name=Sample", "-c", "user.email=sample@example.test", "commit", "-q", "-m", "app")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    gh = FakeGitHub(head, base)
    run(["pr-merge", "4", "--sha", head, "--method", "rebase"], env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert gh.requests[-1][2]["merge_method"] == "rebase"


@pytest.mark.parametrize(("method", "path", "body"), [
    ("GET", "/repos/o/r/pulls/4", '{"anything": 1}'),
    ("HEAD", "/repos/o/r", "x"),
    ("PATCH", "/repos/o/r/pulls/4", '{"base": "other"}'),
    ("PATCH", "/repos/o/r/pulls/4", '{"state": "closed"}'),
    ("PATCH", "/repos/o/r/pulls/4", '{"title": "t", "maintainer_can_modify": true}'),
    ("PATCH", "/repos/o/r/pulls/4", "{}"),
    ("PATCH", "/repos/o/r/pulls/4", "not json"),
])
def test_reads_carry_no_body_and_pr_edits_only_set_title_and_body(method, path, body, tmp_path) -> None:
    from harbor.github_rest import GitHubRestError, run
    (tmp_path / "body.json").write_text(body)
    gh = FakeGitHub("a" * 40, "b" * 40)
    with pytest.raises(GitHubRestError, match="refusing api call"):
        run(["api", method, path, "--input", str(tmp_path / "body.json")],
            env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert gh.requests == []


def test_a_pr_edit_of_title_and_body_is_allowed(tmp_path) -> None:
    from harbor.github_rest import run
    (tmp_path / "body.json").write_text('{"title": "New title", "body": "New body"}')
    gh = FakeGitHub("a" * 40, "b" * 40)
    run(["api", "PATCH", "/repos/o/r/pulls/4", "--input", str(tmp_path / "body.json")],
        env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert gh.requests[0][:2] == ("PATCH", "/repos/o/r/pulls/4")


# --- ninth review: case-variant symlinks anywhere under .harbor/ ------------------------------

def _repo_with_links(tmp_path: Path, links: dict[str, str], files: dict[str, str]) -> tuple[Path, str]:
    """A base that tracks ``files`` plus the given symlinks (path -> target), on a feature branch."""
    import os
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.ignorecase", "false")  # keep the exact case on any filesystem
    for path, text in files.items():
        _write(repo, path, text)
    for path, target in links.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, repo / path)
    base = _commit_all(repo, "base with links")
    for path in links:
        assert "120000" in _git(repo, "ls-tree", base, "--", path)
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo, base


@pytest.mark.parametrize(("links", "files", "edit"), [
    ({".HARBOR": "cfg"}, {"cfg/workflow/workflow.toml": NONE}, "cfg/workflow/workflow.toml"),
    ({".harbor/WORKFLOW": "../cfg"}, {"cfg/workflow.toml": NONE, ".harbor/pipeline.md": "x\n"}, "cfg/workflow.toml"),
    ({".harbor/workflow/config.toml": "../../cfg.toml"},
     {".harbor/workflow/workflow.toml": "version = 1\n", "cfg.toml": "[controller]\nmodel = \"m\"\n"}, "cfg.toml"),
])
def test_a_case_variant_or_nested_symlink_under_harbor_refuses(tmp_path: Path, monkeypatch, links, files, edit) -> None:
    from harbor.device_proof import builder_sandbox_change
    from harbor.github_rest import GitHubRestError, run
    repo, base = _repo_with_links(tmp_path, links, files)
    _write(repo, edit, IOS)  # the PR edits only the symlink's target, outside .harbor/
    _git(repo, "add", edit)
    _git(repo, "-c", "user.name=Sample", "-c", "user.email=sample@example.test", "commit", "-q", "-m", "edit target")
    head = _git(repo, "rev-parse", "HEAD")
    why: list[str] = []
    assert builder_sandbox_change(repo / ".git", head, base, reasons=why) is None
    assert "symlink" in why[0]
    monkeypatch.chdir(repo)
    gh = FakeGitHub(head, base)
    with pytest.raises(GitHubRestError, match="could not read the PR diff"):
        run(["pr-merge", "4", "--sha", head], env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert [m for m, _, _ in gh.requests] == ["GET"]  # squash is not PUT


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/graphql"), ("GET", "/graphql?query=mutation%7BmergePullRequest%7D"), ("HEAD", "/graphql"),
    ("GET", "/api/graphql"), ("GET", "/graphql/"),
])
def test_graphql_over_get_or_head_is_refused(method, path) -> None:
    from harbor.github_rest import GitHubRestError, run
    gh = FakeGitHub("a" * 40, "b" * 40)
    with pytest.raises(GitHubRestError, match="GraphQL is POST only"):
        run(["api", method, path], env={"HARBOR_GITHUB_REPOSITORY": "o/r"}, client=gh)
    assert gh.requests == []
