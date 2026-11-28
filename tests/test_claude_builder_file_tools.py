"""A Claude builder can edit its worktree, and projects can opt in to more hosts and /tmp.

Found live on HAN-94: under ``--permission-mode dontAsk`` with no Edit or
Write tool, a child implementor's ``mkdir -p tests && printf ... > tests/x.py``
was refused ("Claude Code is running in don't ask mode"), a build was refused
``fonts.googleapis.com:443``, and tests that open a database under ``/tmp``
failed. The builder gets Edit and Write, approved for the ticket worktree
only; the two network/filesystem widenings are project opt-ins under
``[builder]``, absent by default.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harbor.config.workflow import builder_allow_tmp, builder_allowed_domains, load_workflow
from harbor.runner.base import RunnerError
from harbor.runner.claude import DEFAULT_SANDBOX_DOMAINS, ClaudeRunner, tmp_write_paths
from workflow_support import config as materialize

FONTS = ("fonts.googleapis.com", "fonts.gstatic.com")


def runner(tmp_path: Path, **attrs) -> ClaudeRunner:
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    built = ClaudeRunner(worktree, model="claude-sonnet-5-5", reasoning_effort="medium", executable="claude",
                         writable_roots=(tmp_path / "ledger",))
    for key, value in attrs.items():
        setattr(built, key, value)
    return built


def settings(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--settings") + 1])


def tools(argv: list[str]) -> list[str]:
    return argv[argv.index("--tools") + 1].split(",")


@pytest.mark.parametrize("resume", [False, True])
def test_the_builder_has_edit_and_write_approved_for_its_worktree_only(tmp_path: Path, resume: bool) -> None:
    argv = runner(tmp_path).command("x", run_id="01a1e206-64a0-70e2-ae18-183a99c4cb79", resume=resume)
    assert {"Edit", "Write"} <= set(tools(argv))
    assert "NotebookEdit" not in tools(argv) and "WebFetch" not in tools(argv)
    allow = settings(argv)["permissions"]["allow"]
    # One Edit rule (it covers Write too), anchored at the filesystem root, for the worktree alone.
    edits = [rule for rule in allow if rule.startswith("Edit(")]
    assert edits == [f"Edit(/{(tmp_path / 'wt').resolve()}/**)"]
    # The ledger and git dir stay Bash-sandbox writable only; no edit rule reaches them.
    assert not any(str((tmp_path / "ledger").resolve()) in rule for rule in allow)
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"


def test_a_worktree_path_a_rule_cannot_express_is_refused(tmp_path: Path) -> None:
    worktree = tmp_path / "w*t"
    worktree.mkdir()
    built = ClaudeRunner(worktree, model="claude-sonnet-5-5", reasoning_effort="medium", executable="claude")
    with pytest.raises(RunnerError, match="worktree path .* contains a character"):
        built.command("x", run_id="r")


def test_absent_opt_ins_leave_hosts_and_writes_unchanged(tmp_path: Path) -> None:
    sandbox = settings(runner(tmp_path).command("x", run_id="r"))["sandbox"]
    assert sandbox["network"] == {"allowedDomains": list(DEFAULT_SANDBOX_DOMAINS)}
    assert sandbox["filesystem"] == {"allowWrite": [str((tmp_path / "ledger").resolve())]}


def test_allowed_domains_are_merged_and_a_bad_one_fails_closed(tmp_path: Path) -> None:
    sandbox = settings(runner(tmp_path, allowed_domains=FONTS).command("x", run_id="r"))["sandbox"]
    assert sandbox["network"]["allowedDomains"] == [*DEFAULT_SANDBOX_DOMAINS, *FONTS]
    with pytest.raises(RunnerError, match="sandbox host"):
        runner(tmp_path, allowed_domains=("*.googleapis.com",)).command("x", run_id="r")


@pytest.mark.parametrize(("system", "paths"), [("darwin", ["/tmp", "/private/tmp"]), ("linux", ["/tmp"])])
def test_allow_tmp_adds_only_the_shared_temp_directory(tmp_path: Path, monkeypatch, system: str, paths) -> None:
    monkeypatch.setattr("harbor.runner.claude.sys.platform", system)
    assert tmp_write_paths() == paths
    sandbox = settings(runner(tmp_path, allow_tmp=True).command("x", run_id="r"))["sandbox"]
    assert sandbox["filesystem"]["allowWrite"] == [str((tmp_path / "ledger").resolve()), *paths]


def test_opt_ins_keep_every_existing_protection(tmp_path: Path) -> None:
    argv = runner(tmp_path, allowed_domains=FONTS, allow_tmp=True).command("x", run_id="r")
    sandbox = settings(argv)["sandbox"]
    assert sandbox["enabled"] is True and sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False and sandbox["autoAllowBashIfSandboxed"] is True
    assert "excludedCommands" not in sandbox and set(sandbox["network"]) == {"allowedDomains"}
    assert set(sandbox["filesystem"]) == {"allowWrite"}


@pytest.mark.parametrize(("extra", "domains", "tmp"), [
    ("", (), False),
    ("[builder]\n", (), False),
    ('[builder]\nallowed_domains = []\nallow_tmp = false\n', (), False),
    ('[builder]\nallowed_domains = ["fonts.googleapis.com", "fonts.gstatic.com"]\nallow_tmp = true\n', FONTS, True),
])
def test_workflow_parses_the_opt_ins(tmp_path: Path, extra: str, domains, tmp: bool) -> None:
    workflow = load_workflow(materialize(tmp_path, extra=extra))
    assert builder_allowed_domains(workflow.config) == domains
    assert builder_allow_tmp(workflow.config) is tmp


@pytest.mark.parametrize(("extra", "message"), [
    ('[builder]\nallowed_domains = ["*.googleapis.com"]\n', "not a plain lower-case host name"),
    ('[builder]\nallowed_domains = ["https://fonts.googleapis.com"]\n', "not a plain lower-case host name"),
    ('[builder]\nallowed_domains = ["fonts.googleapis.com:443"]\n', "not a plain lower-case host name"),
    ('[builder]\nallowed_domains = ["fonts.googleapis.com/css2"]\n', "not a plain lower-case host name"),
    ('[builder]\nallowed_domains = ["Fonts.googleapis.com"]\n', "not a plain lower-case host name"),
    ('[builder]\nallowed_domains = ["127.0.0.1"]\n', "not a plain lower-case host name"),
    ('[builder]\nallowed_domains = ["localhost"]\n', "not a plain lower-case host name"),
    ('[builder]\nallowed_domains = [""]\n', "not a plain lower-case host name"),
    ('[builder]\nallowed_domains = "fonts.googleapis.com"\n', "must be a list"),
    ("[builder]\nallowed_domains = [1]\n", "must be a list"),
    ('[builder]\nallowed_domains = ["a.example", "a.example"]\n', "twice"),
    ('[builder]\nallow_tmp = "yes"\n', "allow_tmp must be true or false"),
    ("[builder]\nallow_tmp = 1\n", "allow_tmp must be true or false"),
    ('[builder]\nallow_write = ["/tmp"]\n', "unknown builder setting"),
])
def test_workflow_refuses_a_bad_opt_in(tmp_path: Path, extra: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_workflow(materialize(tmp_path, extra=extra))


def test_the_production_factory_applies_the_opt_ins_per_turn(tmp_path: Path, monkeypatch) -> None:
    from harbor.wake import Wake
    from test_github_transport import claude_production

    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(tmp_path / "projects.md"))
    (tmp_path / "projects.md").write_text("", encoding="utf-8")
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "ledger" / "harbor.db"))
    extra = '[builder]\nallowed_domains = ["fonts.googleapis.com"]\nallow_tmp = true\n'
    (dispatcher, _, factory), db = claude_production(tmp_path, monkeypatch, workflow_extra=extra)
    try:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        wake = Wake(ticket={"identifier": "SAMPLE-1"}, action="build")
        built = factory(worktree, wake)
        s = settings(built.command("x", run_id="r"))
        assert "fonts.googleapis.com" in s["sandbox"]["network"]["allowedDomains"]
        assert "/tmp" in s["sandbox"]["filesystem"]["allowWrite"]
        assert s["permissions"]["allow"][0] == f"Edit(/{worktree.resolve()}/**)"
        toml = dispatcher.workflow.package / "workflow.toml"
        toml.write_text(toml.read_text().replace('"fonts.googleapis.com"', "").replace("allow_tmp = true", "allow_tmp = false"))
        s = settings(factory(worktree, wake).command("x", run_id="r"))
        assert "fonts.googleapis.com" not in s["sandbox"]["network"]["allowedDomains"]
        assert "/tmp" not in s["sandbox"]["filesystem"]["allowWrite"]
    finally:
        db.close()


def test_doctor_reports_the_opt_ins(tmp_path: Path) -> None:
    from test_github_transport import run_doctor
    extra = '[builder]\nallowed_domains = ["fonts.googleapis.com", "fonts.gstatic.com"]\nallow_tmp = true\n'
    out = run_doctor(tmp_path, "claude", None, workflow_extra=extra).stdout
    assert "fonts.googleapis.com, fonts.gstatic.com" in out.split("Builder sandbox network: ", 1)[1].splitlines()[0]
    assert "includes [builder] allowed_domains: fonts.googleapis.com, fonts.gstatic.com" in out, out
    assert "Builder sandbox writes: [builder] allow_tmp adds /tmp" in out
    assert "Builder permissions: Edit and Write in the ticket worktree only" in out


def test_doctor_is_quiet_about_absent_opt_ins(tmp_path: Path) -> None:
    from test_github_transport import run_doctor
    out = run_doctor(tmp_path, "claude", None).stdout
    assert "allowed_domains" not in out and "allow_tmp" not in out, out
