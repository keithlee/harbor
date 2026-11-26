"""A sandboxed Claude builder can run Harbor and read its workflow package.

Regression context: Claude builders run with ``--permission-mode dontAsk``.
Sandboxed Bash is auto-allowed there, except a command that sets
``PYTHONPATH`` or assigns a variable in front of an absolute executable path:
exactly the authoritative ``harbor`` runtime invocation in the builder prompt. Read, Glob and Grep are
limited to the working directory, and the workflow package lives in the
operator's clone, outside the ticket worktree. Default-configured builders
therefore stopped with "Permission to use Bash has been denied because Claude
Code is running in don't ask mode" on every ``harbor workflow check``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harbor.runner.base import RunnerError
from harbor.runner.claude import ClaudeRunner

INVOCATION = "/usr/bin/env HARBOR_PROJECTS_FILE=/r/projects.md HARBOR_DB_PATH=/r/harbor.db PYTHONPATH=/h/src /h/python -P -m harbor.cli"


def settings(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--settings") + 1])


def runner(tmp_path: Path) -> ClaudeRunner:
    return ClaudeRunner(tmp_path / "wt", model="claude-sonnet-5-5", reasoning_effort="medium", executable="claude",
                        writable_roots=(tmp_path / "state",))


def test_an_unwired_runner_adds_no_permissions(tmp_path: Path) -> None:
    (tmp_path / "wt").mkdir()
    assert "permissions" not in settings(runner(tmp_path).command("x", run_id="run"))


def test_only_the_runtime_invocation_and_the_package_are_pre_approved(tmp_path: Path) -> None:
    (tmp_path / "wt").mkdir()
    package = tmp_path / "repo" / ".harbor" / "workflow"
    package.mkdir(parents=True)
    plain = settings(runner(tmp_path).command("x", run_id="run"))
    wired = runner(tmp_path)
    wired.harbor_invocation = INVOCATION
    wired.workflow_package = package
    s = settings(wired.command("x", run_id="run"))
    assert s["permissions"] == {"allow": [f"Bash({INVOCATION}:*)", f"Read(/{package.resolve()}/**)"]}
    # Nothing else widens: the OS sandbox, its writable roots and network stay as they were.
    assert s["sandbox"] == plain["sandbox"]
    assert s["sandbox"]["enabled"] is True and s["sandbox"]["allowUnsandboxedCommands"] is False


def test_the_github_mcp_approval_is_kept_alongside(tmp_path: Path) -> None:
    (tmp_path / "wt").mkdir()
    wired = runner(tmp_path)
    wired.harbor_invocation = INVOCATION
    wired.github_mcp = {"url": "https://api.githubcopilot.com/mcp/", "token_env": "GITHUB_TOKEN"}
    assert settings(wired.command("x", run_id="run"))["permissions"]["allow"] == [f"Bash({INVOCATION}:*)", "mcp__github"]


@pytest.mark.parametrize("invocation", ["/bin/harbor*", "/bin/harbor\nrm -rf /"])
def test_an_invocation_a_rule_cannot_express_fails_closed(tmp_path: Path, invocation: str) -> None:
    (tmp_path / "wt").mkdir()
    wired = runner(tmp_path)
    wired.harbor_invocation = invocation
    with pytest.raises(RunnerError, match="runtime invocation"):
        wired.command("x", run_id="run")


@pytest.mark.parametrize("name", ["missing", "with*star", "with[class]"])
def test_a_package_a_rule_cannot_express_fails_closed(tmp_path: Path, name: str) -> None:
    (tmp_path / "wt").mkdir()
    if name != "missing":
        (tmp_path / name).mkdir()
    wired = runner(tmp_path)
    wired.workflow_package = tmp_path / name
    with pytest.raises(RunnerError, match="workflow package"):
        wired.command("x", run_id="run")


def test_the_production_claude_builder_may_run_the_invocation_its_prompt_names(tmp_path: Path, monkeypatch) -> None:
    from harbor.cli import _pipeline_text
    from harbor.wake import Wake
    from test_github_transport import claude_production

    registry = tmp_path / "projects.md"
    registry.write_text("", encoding="utf-8")
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "ledger" / "harbor.db"))
    (dispatcher, _, factory), db = claude_production(tmp_path, monkeypatch)
    try:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        built = factory(worktree, Wake(ticket={"identifier": "SAMPLE-1"}, action="build"))
        s = settings(built.command("x", run_id="harbor-run-1"))
        prompt = _pipeline_text(SimpleConfig(dispatcher), dispatcher.workflow)
        invocation = next(line.split(": ", 1)[1] for line in prompt.splitlines()
                          if line.startswith("Authoritative runtime invocation: "))
        package = dispatcher.workflow.package.resolve()
        assert s["permissions"]["allow"] == [f"Bash({invocation}:*)", f"Read(/{package}/**)"]
        assert not worktree.resolve().is_relative_to(package)
        assert s["sandbox"]["enabled"] is True and s["sandbox"]["failIfUnavailable"] is True
        assert s["sandbox"]["filesystem"]["allowWrite"][0] == str((tmp_path / "ledger").resolve())
    finally:
        db.close()


class SimpleConfig:
    """The project fields ``_pipeline_text`` reads, taken from the dispatcher."""

    def __init__(self, dispatcher) -> None:
        self.name = dispatcher.project
        self.registry_path = None
        self.workflow_path = str(dispatcher.workflow.package / "workflow.toml")


def test_doctor_reports_the_claude_builder_permissions(tmp_path: Path) -> None:
    from test_github_transport import run_doctor
    result = run_doctor(tmp_path, "claude", None)
    package = (tmp_path / "repo" / ".harbor" / "workflow").resolve()
    assert (f"Builder permissions: harbor runtime invocation (sandboxed) · read-only {package}"
            in result.stdout), result.stdout + result.stderr


def test_doctor_fails_closed_when_a_path_cannot_be_approved(tmp_path: Path) -> None:
    from test_github_transport import run_doctor
    root = tmp_path / "old*copy"
    root.mkdir()
    result = run_doctor(root, "claude", None)
    assert "Builder permissions: invalid (the Harbor runtime invocation" in result.stdout, result.stdout + result.stderr
    assert result.returncode == 1


def test_doctor_reports_no_builder_permissions_for_a_codex_project(tmp_path: Path) -> None:
    from test_github_transport import run_doctor
    result = run_doctor(tmp_path, "codex", None)
    assert "GitHub transport: rest" in result.stdout, result.stdout + result.stderr
    assert "Builder permissions" not in result.stdout


def test_parentheses_and_spaces_in_paths_are_approved_literally(tmp_path: Path) -> None:
    # Checked against Claude Code 2.1.332: rules match such paths exactly.
    (tmp_path / "wt").mkdir()
    package = tmp_path / "Projects (old)" / "workflow"
    package.mkdir(parents=True)
    wired = runner(tmp_path)
    wired.harbor_invocation = "/usr/bin/env PYTHONPATH='/h (copy)/src' /h/python -P -m harbor.cli"
    wired.workflow_package = package
    assert settings(wired.command("x", run_id="run"))["permissions"]["allow"] == [
        "Bash(/usr/bin/env PYTHONPATH='/h (copy)/src' /h/python -P -m harbor.cli:*)", f"Read(/{package.resolve()}/**)"]


def test_doctor_fails_closed_when_the_package_path_cannot_be_approved(tmp_path: Path) -> None:
    import os
    import shutil
    import subprocess
    import sys
    from test_github_transport import run_doctor

    run_doctor(tmp_path, "claude", None)  # a clean registry path, with the checkout set up beside it
    moved = tmp_path / "old*copy" / "repo"
    moved.parent.mkdir()
    shutil.copytree(tmp_path / "repo", moved, symlinks=True)
    projects = tmp_path / "projects.md"
    projects.write_text(projects.read_text().replace(f"| {tmp_path / 'repo'} |", f"| {moved} |"))
    env = {k: v for k, v in os.environ.items() if k not in ("LINEAR_API_KEY", "HARBOR_LINEAR_API_KEY")}
    env.update(HARBOR_PROJECTS_FILE=str(projects), HARBOR_DB_PATH=str(tmp_path / "harbor.db"),
               GITHUB_TOKEN="dummy-token-value", HARBOR_CLAUDE_EXECUTABLE="/bin/echo")
    result = subprocess.run([sys.executable, "-c", "import sys; from harbor.cli import main; sys.exit(main())",
                             "doctor", "SampleProject"], env=env, capture_output=True, text=True, timeout=60)
    package = (moved / ".harbor" / "workflow").resolve()
    assert (f"Builder permissions: invalid (the workflow package path {package} contains a character"
            in result.stdout), result.stdout + result.stderr
    assert result.returncode == 1


def test_a_claude_builder_is_refused_when_its_registry_disappears(tmp_path: Path, monkeypatch) -> None:
    # A retained Claude ticket whose registry was moved or deleted must not be
    # resumed with only the Read rule: it would hit the dontAsk denial again.
    from harbor.runner.quota import bind_runner_identity
    from harbor.wake import Wake
    from test_github_transport import claude_production

    registry = tmp_path / "projects.md"
    registry.write_text("", encoding="utf-8")
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))
    (_, _, factory), db = claude_production(tmp_path, monkeypatch)
    try:
        bind_runner_identity(db.connection, "harbor-run-1", "claude", "claude-sonnet-5-5", "medium")
        registry.unlink()
        for wake in (Wake(ticket={"identifier": "SAMPLE-1"}, action="build"),
                     Wake(ticket={"identifier": "SAMPLE-1"}, action="build", resume="harbor-run-1")):
            with pytest.raises(ValueError, match="Claude builder for SAMPLE-1 cannot be approved to run harbor: "
                                                 "workflow delivery requires its authoritative project registry"):
                factory(tmp_path, wake)
    finally:
        db.close()
