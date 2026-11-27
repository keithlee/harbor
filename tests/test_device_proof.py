"""Opt-in device and browser proof for Claude builders (``[builder] device_proof``)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harbor.config.workflow import DEVICE_PROOF_PLATFORMS, device_proof_settings, load_workflow
from harbor.runner.base import RunnerError
from harbor.runner.claude import DEVICE_PROOF_RULES, ClaudeRunner, device_proof_network, device_proof_report
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
    (("ios",), {"allowMachLookup": ["com.apple.CoreSimulator.*"]}),
    (("web",), {"allowLocalBinding": True}),
    (("android", "ios", "web"), {"allowLocalBinding": True, "allowMachLookup": ["com.apple.CoreSimulator.*"]}),
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
    assert any("com.apple.CoreSimulator.*" in line for line in mac)
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
        assert network["allowMachLookup"] == ["com.apple.CoreSimulator.*"] and "allowLocalBinding" not in network
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
    assert "Device proof: android, ios" in out, out + result.stderr
    assert "grants network.allowLocalBinding" in out and "grants network.allowMachLookup com.apple.CoreSimulator.*" in out


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
    assert "Device proof: not applied ([builder] device_proof applies to Claude Code builders only" in result.stdout
    assert "cannot use xcrun simctl" in result.stdout
