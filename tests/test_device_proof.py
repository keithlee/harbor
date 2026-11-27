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
    assert builder_sandbox_change(repo, head, base) is False
    for bad_base in ("main", "origin/main", base[:12], "HEAD"):
        assert builder_sandbox_change(repo, head, bad_base) is None
    assert builder_sandbox_change(repo, "feature", base) is None
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
    assert builder_sandbox_change(repo, head, base) is True


def test_any_case_folded_harbor_change_counts(tmp_path: Path) -> None:
    from harbor.device_proof import builder_sandbox_change
    for name, path in (("upper file", ".harbor/workflow/WORKFLOW.toml"), ("upper dir", ".HARBOR/workflow/workflow.toml"),
                       ("mixed", ".Harbor/workflow/config.toml"), ("config", ".harbor/workflow/config.toml"),
                       ("pipeline", ".harbor/pipeline.md")):
        repo, base = _repo_with_workflow(tmp_path / name.replace(" ", "-"))
        _write(repo, path, '[builder]\ndevice_proof = ["android"]\n')
        assert builder_sandbox_change(repo, _commit_all(repo, name), base) is True, path
    repo, base = _repo_with_workflow(tmp_path / "unrelated")
    _write(repo, "docs/harbor.md", "a .harbor mention outside .harbor/\n")
    assert builder_sandbox_change(repo, _commit_all(repo, "docs"), base) is False


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
    assert builder_sandbox_change(repo, pr1, base) is None
    # PR2, as if PR1 had merged: grant device_proof through the symlink target, outside .harbor/.
    _write(repo, "cfg/workflow.toml", 'version = 1\n[builder]\ndevice_proof = ["ios"]\n')
    pr2 = _commit_all(repo, "PR2: grant device_proof via cfg/")
    assert builder_sandbox_change(repo, pr2, pr1) is None
    # The runtime follows the link, which is why the check must not read "unchanged".
    assert tomllib.loads((repo / WORKFLOW).read_text())["builder"]["device_proof"] == ["ios"]


def test_symlinked_harbor_dir_workflow_file_submodule_and_missing_are_unreadable(tmp_path: Path) -> None:
    import os
    import shutil
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path / "harbor-link")
    shutil.move(str(repo / ".harbor"), str(repo / "elsewhere"))
    os.symlink("elsewhere", repo / ".harbor")
    assert builder_sandbox_change(repo, _commit_all(repo, "symlink .harbor"), base) is None
    repo, base = _repo_with_workflow(tmp_path / "file-link")
    (repo / WORKFLOW).rename(repo / ".harbor" / "workflow" / "real.toml")
    os.symlink("real.toml", repo / WORKFLOW)
    assert builder_sandbox_change(repo, _commit_all(repo, "symlink workflow.toml"), base) is None
    repo, base = _repo_with_workflow(tmp_path / "submodule")
    _git(repo, "rm", "-q", "-r", ".harbor/workflow")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},.harbor/workflow")
    _git(repo, "-c", "user.name=Sample", "-c", "user.email=sample@example.test", "commit", "-q", "-m", "submodule")
    head = _git(repo, "rev-parse", "HEAD")
    assert "160000 commit" in _git(repo, "ls-tree", head, "--", ".harbor/workflow")
    assert builder_sandbox_change(repo, head, base) is None
    repo, base = _repo_with_workflow(tmp_path / "missing")
    (repo / WORKFLOW).unlink()
    assert builder_sandbox_change(repo, _commit_all(repo, "delete workflow"), base) is None


def test_a_commit_missing_locally_fails_closed(tmp_path: Path) -> None:
    from harbor.device_proof import builder_sandbox_change
    repo, base = _repo_with_workflow(tmp_path)
    head = base
    absent = "1" * 40
    assert builder_sandbox_change(repo, absent, base) is None  # no origin to fetch it from
    assert builder_sandbox_change(repo, head, absent) is None


def test_a_missing_commit_is_fetched_into_a_private_ref(tmp_path: Path) -> None:
    from harbor.device_proof import CHECK_REF_PREFIX, builder_sandbox_change
    upstream, base = _repo_with_workflow(tmp_path / "upstream")
    _write(upstream, "src/app.py", "print('upstream')\n")
    head = _commit_all(upstream, "only upstream has this")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--no-local", "--single-branch", "--branch", "main", str(upstream), str(clone))
    assert builder_sandbox_change(clone, head, base, fetch=False) is None
    assert builder_sandbox_change(clone, head, base) is False
    assert _git(clone, "rev-parse", f"{CHECK_REF_PREFIX}{head}") == head


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
        run(["--repo", "o/r", "pr-merge", "4", "--sha", head], env={}, client=gh)
    assert [m for m, _, _ in gh.requests] == ["GET"]  # never sent the merge


def test_pr_merge_refuses_a_sha_that_is_not_the_pr_head(tmp_path: Path, monkeypatch) -> None:
    from harbor.github_rest import GitHubRestError, run
    head, base = _merge_repo(tmp_path, monkeypatch)
    gh = FakeGitHub(head, base)
    with pytest.raises(GitHubRestError, match="is not the PR's current head"):
        run(["--repo", "o/r", "pr-merge", "4", "--sha", base], env={}, client=gh)
    assert [m for m, _, _ in gh.requests] == ["GET"]


def test_pr_merge_refuses_when_the_diff_cannot_be_read(tmp_path: Path, monkeypatch) -> None:
    from harbor.github_rest import GitHubRestError, run
    monkeypatch.chdir(tmp_path)  # not a git checkout
    gh = FakeGitHub("a" * 40, "b" * 40)
    with pytest.raises(GitHubRestError, match="could not read the PR diff"):
        run(["--repo", "o/r", "pr-merge", "4", "--sha", "a" * 40], env={}, client=gh)
    assert [m for m, _, _ in gh.requests] == ["GET"]


def test_pr_merge_and_pr_comment_still_work(tmp_path: Path, monkeypatch) -> None:
    from harbor.github_rest import run
    head, base = _merge_repo(tmp_path, monkeypatch)
    gh = FakeGitHub(head, base)
    assert run(["--repo", "o/r", "pr-merge", "4", "--sha", head], env={}, client=gh) == {"merged": True}
    assert gh.requests[-1][:2] == ("PUT", "/repos/o/r/pulls/4/merge")
    note = tmp_path / "note.md"
    note.write_text("Thanks, fixed.")
    run(["--repo", "o/r", "pr-comment", "4", "--body-file", str(note)], env={}, client=gh)
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

    def record(worktree, head, base, **kwargs):
        seen.append((str(worktree), head, base))
        return False

    monkeypatch.setattr("harbor.device_proof.builder_sandbox_change", record)
    base = "b" * 40
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=analysis("Risk tier: low"),
                          comments=[verdict("low")], sandbox_change="real", base_oid=base)
    assert seen == [(str(tmp_path), HEAD, base)] and code == 0
    seen.clear()
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=analysis("Risk tier: low"),
                          comments=[verdict("low")], sandbox_change="real", base_oid=None)
    assert seen == [(str(tmp_path), HEAD, None)]
