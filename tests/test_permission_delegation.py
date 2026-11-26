"""Permission blockers go to First Mate's operator run before a person.

A Claude builder's sandbox denies some commands outright (``gh`` TLS inside the
macOS sandbox, ``adb``/``simctl`` services, writes outside the worktree). The
builder reports them as a ``permission_blocker`` with the exact commands; when
the project opts in, Harbor runs them through First Mate (Claude Code, auto
permission mode, no sandbox), records a receipt, and resumes the same builder
session with the output. Anything it will not or cannot run falls through to
the ordinary human escalation.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

from harbor.db import HarborDB
from harbor.first_mate import FirstMate
from harbor.permission_delegation import (
    ClaudeOperator,
    MAX_DELEGATIONS_PER_TICKET,
    github_operator_env,
    is_delegable,
    operator_for,
)
from harbor.runner.base import validate_result
from harbor.runner.claude import claude_read_only
from harbor.unblock import UnblockLadder

TOKEN = "ghp_examplesecretvalue1234567890"


def run(awaitable):
    return asyncio.run(awaitable)


def ticket():
    return {"identifier": "SAMPLE-7", "id": "linear-7", "title": "Blocked", "state": "In Progress"}


class LinearFake:
    def __init__(self):
        self.comments = []
        self.states = []

    async def create_comment(self, issue_id, body):
        self.comments.append((issue_id, body))
        return {"comment": {"id": f"comment-{len(self.comments)}"}}

    async def update_issue_state(self, issue_id, state):
        self.states.append((issue_id, state))
        return {"success": True}


class FakeClaude:
    """Stands in for `claude --print`: runs the prompt's commands with bash, like the operator would."""

    def __init__(self, *, refuse: str | None = None, leak: str = ""):
        self.calls = []
        self.refuse = refuse
        self.leak = leak

    async def __call__(self, argv, prompt, cwd, env, timeout):
        self.calls.append({"argv": list(argv), "prompt": prompt, "cwd": Path(cwd), "env": dict(env)})
        block = prompt.split("<commands>\n", 1)[1].split("\n</commands>", 1)[0]
        commands = [re.sub(r"^\d+\. ", "", line) for line in block.splitlines()]
        if self.refuse:
            output = {"status": "refused", "reason": self.refuse, "results": []}
        else:
            results = []
            for command in commands:
                done = subprocess.run(["bash", "-c", command], cwd=cwd, env=env, capture_output=True, text=True)
                results.append({"command": command, "ran": True, "exit_code": done.returncode,
                                "stdout": done.stdout + self.leak, "stderr": done.stderr})
            output = {"status": "ran", "reason": "", "results": results}
        envelope = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(output),
                    "structured_output": output, "permission_denials": []}
        return 0, json.dumps(envelope), ""


@pytest.fixture()
def db(tmp_path):
    ledger = HarborDB(tmp_path / "harbor.db")
    yield ledger
    ledger.close()


@pytest.fixture()
def worktree(tmp_path):
    path = tmp_path / "wt" / "SAMPLE-7"
    path.mkdir(parents=True)
    return path


def blocker(*commands, kind="permission_blocker", evidence="Operation not permitted", cwd=None,
            question="The sandbox denied the commands below."):
    return {"kind": kind, "question": question, "options": [],
            "recommendation": None, "evidence": evidence, "commands": list(commands), "cwd": cwd}


def ladder(db, *, operator=None, linear=None, resumed=None, notified=None, protected=()):
    async def resume(run_id, prompt):
        resumed.append((run_id, prompt))
        return True

    async def notify(message):
        notified.append(message)

    # Without an operator the ladder is built exactly as before this change.
    extra = {"operator": operator} if operator is not None else {}
    return UnblockLadder(db, linear=linear, resumer=resume if resumed is not None else None,
                         notifier=notify if notified is not None else None,
                         protected_paths=protected, **extra)


def operator(fake, **kwargs):
    return ClaudeOperator(executable="claude", model="claude-haiku-5-5", effort="medium",
                          process_runner=fake, **kwargs)


def handle(lad, worktree, block):
    return run(lad.handle(ticket(), {"id": "builder-session", "worktree": str(worktree)},
                          {"status": "blocked", "blocker": block}))


# --- reproduction: what happens without the operator rung ----------------------------------


def test_reproduce_permission_blocker_without_the_operator_goes_straight_to_a_person(db, worktree):
    """Today's behaviour, and still the behaviour when the project does not opt in."""
    linear, resumed, notified = LinearFake(), [], []
    result = handle(ladder(db, linear=linear, resumed=resumed, notified=notified), worktree,
                    blocker("gh pr list --state open"))
    assert result.rung == 2 and result.action == "park" and not result.resumed
    assert resumed == [] and notified and linear.states == [("linear-7", "Blocked")]


def test_permission_blocker_is_a_valid_builder_result():
    payload = {"status": "blocked", "outcome": "no_pr", "waiting_on": "human", "pr_url": None, "summary": "s",
               "next_action": None, "blocker": blocker("gh pr list", cwd="app")}
    assert validate_result(payload) == []
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "run-result.json").read_text())
    properties = schema["properties"]["blocker"]["properties"]
    assert "permission_blocker" in properties["kind"]["enum"]
    assert {"commands", "cwd"} <= set(schema["properties"]["blocker"]["required"])
    bad = {**payload, "blocker": {**payload["blocker"], "commands": "gh pr list"}}
    assert "blocker.commands must be an array of strings or null" in validate_result(bad)


# --- auto permission mode on every Claude launch -------------------------------------------


def test_first_mate_read_only_claude_command_uses_auto_mode_and_no_tools():
    command = claude_read_only("claude", "claude-haiku-5-5", "low")
    assert command[command.index("--permission-mode") + 1] == "auto"
    assert command[command.index("--tools") + 1] == ""


def test_first_mate_answers_and_fallback_launch_claude_in_auto_mode(monkeypatch):
    monkeypatch.setenv("HARBOR_CLAUDE_EXECUTABLE", "/usr/bin/true")
    from harbor.quota_fallback import Profile
    from harbor.first_mate import FirstMateQuotaError
    stream = json.dumps({"type": "result", "is_error": False, "result": "ok"})
    commands = []

    async def claude(command, prompt):
        commands.append(list(command))
        return stream

    run(FirstMate(runner=claude, model="claude-sonnet-5-5", provider="claude").triage("p"))

    async def out_of_usage(command, prompt):
        raise FirstMateQuotaError(None, "codex")

    run(FirstMate(runner=out_of_usage, model="gpt-6-luna", fallback=Profile("claude", "claude-haiku-5-5", "low"),
                  fallback_runner=claude).triage("p"))
    assert len(commands) == 2
    for command in commands:
        assert command[command.index("--permission-mode") + 1] == "auto"
        assert command[command.index("--tools") + 1] == ""


def test_operator_command_is_auto_mode_bash_only_and_unsandboxed():
    command = operator(FakeClaude()).command()
    assert command[command.index("--permission-mode") + 1] == "auto"
    assert command[command.index("--tools") + 1] == "Bash"
    assert json.loads(command[command.index("--settings") + 1]) == {"sandbox": {"enabled": False}}
    assert command[command.index("--setting-sources") + 1] == ""
    assert "--no-session-persistence" in command and "--strict-mcp-config" in command


# --- the operator rung ---------------------------------------------------------------------


def test_operator_runs_the_blocked_command_and_resumes_the_builder_with_its_output(db, worktree):
    (worktree / "app").mkdir()
    fake, linear, resumed, notified = FakeClaude(), LinearFake(), [], []
    result = handle(ladder(db, operator=operator(fake), linear=linear, resumed=resumed, notified=notified),
                    worktree, blocker("printf 'hello' > out.txt && cat out.txt", "ls missing-file", cwd="app"))
    # It runs in the requested directory, once, and resumes the same session.
    assert len(fake.calls) == 1 and fake.calls[0]["cwd"] == (worktree / "app").resolve()
    assert (worktree / "app" / "out.txt").read_text() == "hello"
    assert result.rung == 1 and result.action == "resume" and result.resumed and result.same_session
    assert notified == [] and linear.states == []
    run_id, prompt = resumed[0]
    assert run_id == "builder-session"
    assert "$ printf 'hello' > out.txt && cat out.txt\nexit code: 0\nstdout:\nhello" in prompt
    assert "$ ls missing-file\nexit code: 1" in prompt and "No such file" in prompt
    assert "do not load the skill again" in prompt
    receipt = dict(db.connection.execute("SELECT * FROM permission_delegations").fetchone())
    assert receipt["status"] == "ran" and receipt["ticket"] == "SAMPLE-7" and receipt["model"] == "claude-haiku-5-5"
    assert [item["exit_code"] for item in json.loads(receipt["results_json"])] == [0, 1]
    assert "permission delegation receipt #" in linear.comments[-1][1]
    escalation = db.escalations()[-1]
    assert escalation["rung_resolved"] == 1 and escalation["citation"].startswith("first_mate:operator:")


def test_env_blocker_with_a_sandbox_denial_and_commands_is_delegated():
    assert is_delegable(blocker("adb devices", kind="env_blocker",
                                evidence="adb smartsocket listener: Operation not permitted"))
    assert not is_delegable(blocker("adb devices", kind="env_blocker", evidence="adb crashed",
                                    question="Android proof failed."))
    assert not is_delegable(blocker(kind="permission_blocker"))  # nothing to run
    assert not is_delegable(blocker("gh pr list", kind="product_decision"))


def test_first_mate_refusal_falls_through_to_a_person(db, worktree):
    fake, linear, resumed, notified = FakeClaude(refuse="this would delete the repository"), LinearFake(), [], []
    result = handle(ladder(db, operator=operator(fake), linear=linear, resumed=resumed, notified=notified),
                    worktree, blocker("git status"))
    assert len(fake.calls) == 1
    assert result.rung == 2 and result.action == "park" and resumed == [] and notified
    assert "First Mate refused" in result.reason and "delete the repository" in result.reason
    assert db.connection.execute("SELECT status FROM permission_delegations").fetchone()[0] == "refused"


def test_operator_failure_falls_through_to_a_person(db, worktree):
    async def crashed(argv, prompt, cwd, env, timeout):
        return 1, "", "Error: model unavailable"

    resumed, notified = [], []
    result = handle(ladder(db, operator=operator(crashed), resumed=resumed, notified=notified), worktree,
                    blocker("git status"))
    assert result.rung == 2 and resumed == [] and notified
    assert "could not run" in result.reason and "model unavailable" in result.reason


@pytest.mark.parametrize("command, why", [
    ("touch migrations/0001.sql", "protected path"),
    ("printf x > ../other-ticket/file", "outside the ticket worktree"),
    ("cp notes.txt /etc/notes.txt", "outside the ticket worktree"),
    ("cd / && ls", "outside the ticket worktree"),
    ("echo `whoami`", "command substitution"),
])
def test_commands_that_leave_the_worktree_or_touch_protected_paths_never_run(db, worktree, command, why):
    fake, resumed, notified = FakeClaude(), [], []
    result = handle(ladder(db, operator=operator(fake), resumed=resumed, notified=notified,
                           protected=("migrations/**",)), worktree, blocker(command))
    assert fake.calls == [] and resumed == [] and result.rung == 2 and notified
    assert why in result.reason


def test_cwd_outside_the_worktree_never_runs(db, worktree):
    fake = FakeClaude()
    result = handle(ladder(db, operator=operator(fake), notified=[]), worktree, blocker("ls", cwd="../.."))
    assert fake.calls == [] and "outside the ticket worktree" in result.reason


def test_temp_directory_writes_are_allowed(db, worktree, tmp_path):
    import tempfile
    target = Path(tempfile.gettempdir()) / "harbor-permission-delegation-test.txt"
    fake, resumed = FakeClaude(), []
    result = handle(ladder(db, operator=operator(fake), resumed=resumed), worktree,
                    blocker(f"printf ok > {target} && cat {target} && rm {target}"))
    assert result.rung == 1 and len(fake.calls) == 1 and "stdout:\nok" in resumed[0][1]


def test_the_same_commands_never_run_twice(db, worktree):
    fake, resumed, notified = FakeClaude(), [], []
    lad = ladder(db, operator=operator(fake), resumed=resumed, notified=notified)
    first = handle(lad, worktree, blocker("printf one >> count.txt"))
    again = handle(lad, worktree, blocker("printf one >> count.txt"))
    assert first.rung == 1 and len(fake.calls) == 1
    assert (worktree / "count.txt").read_text() == "one"
    assert again.rung == 2 and "already handled these exact commands" in again.reason and notified


def test_per_ticket_cap_falls_through_to_a_person(db, worktree):
    fake, resumed, notified = FakeClaude(), [], []
    lad = ladder(db, operator=operator(fake), resumed=resumed, notified=notified)
    for index in range(MAX_DELEGATIONS_PER_TICKET):
        assert handle(lad, worktree, blocker(f"printf {index}")).rung == 1
    capped = handle(lad, worktree, blocker("printf last"))
    assert len(fake.calls) == MAX_DELEGATIONS_PER_TICKET
    assert capped.rung == 2 and "cap" in capped.reason and notified


def test_no_token_value_in_prompt_receipt_resume_or_comment(db, worktree, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("SAMPLE_GH_TOKEN", TOKEN + "x")
    fake, linear, resumed = FakeClaude(leak=f" token={TOKEN}"), LinearFake(), []
    op = operator(fake, env=github_operator_env({"HARBOR_GITHUB_TOKEN_ENV": "SAMPLE_GH_TOKEN"},
                                                {"SAMPLE_GH_TOKEN": TOKEN + "x"}),
                  secret_names=("SAMPLE_GH_TOKEN",))
    result = handle(ladder(db, operator=op, linear=linear, resumed=resumed), worktree,
                    blocker("printf done", evidence=f"gh failed with {TOKEN}"))
    assert result.rung == 1
    call = fake.calls[0]
    # The token reaches the operator only through its environment.
    assert call["env"]["GITHUB_TOKEN"] == TOKEN and TOKEN not in call["prompt"]
    assert TOKEN not in " ".join(call["argv"])
    rows = [json.dumps(dict(row)) for row in db.connection.execute("SELECT * FROM permission_delegations")]
    rows += [json.dumps(dict(row)) for row in db.escalations()]
    for text in (*rows, resumed[0][1], *(body for _, body in linear.comments)):
        assert TOKEN not in text
    assert "[redacted]" in resumed[0][1]


def test_a_command_containing_a_credential_never_runs(db, worktree, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    fake = FakeClaude()
    result = handle(ladder(db, operator=operator(fake), notified=[]), worktree,
                    blocker(f"curl -H 'Authorization: Bearer {TOKEN}' https://api.github.com"))
    assert fake.calls == [] and result.rung == 2 and "credential" in result.reason
    assert TOKEN not in result.reason


def test_github_operator_env_copies_the_configured_token_for_gh():
    env = github_operator_env({"HARBOR_GITHUB_TOKEN_ENV": "SAMPLE_TOKEN", "HARBOR_GITHUB_API_URL": "https://api.github.com"},
                              {"SAMPLE_TOKEN": "value-1234567"})
    assert env["GH_TOKEN"] == "value-1234567"
    enterprise = github_operator_env({"HARBOR_GITHUB_TOKEN_ENV": "SAMPLE_TOKEN",
                                      "HARBOR_GITHUB_API_URL": "https://ghe.example.com/api/v3"},
                                     {"SAMPLE_TOKEN": "value-1234567"})
    assert enterprise["GH_ENTERPRISE_TOKEN"] == "value-1234567" and "GH_TOKEN" not in enterprise
    # gh's own variable wins when already set.
    assert "GH_TOKEN" not in github_operator_env({"HARBOR_GITHUB_TOKEN_ENV": "SAMPLE_TOKEN"},
                                                 {"SAMPLE_TOKEN": "a-12345678", "GH_TOKEN": "b-12345678"})


# --- configuration -------------------------------------------------------------------------


def _operator(**overrides):
    values = dict(enabled=True, firstmate_provider="claude", firstmate_model="claude-haiku-5-5",
                  firstmate_effort="low", builder_provider="claude", builder_model="claude-sonnet-5-5",
                  builder_effort="medium", executable="claude")
    values.update(overrides)
    return operator_for(**values)


def test_operator_is_off_unless_the_project_opts_in():
    assert _operator(enabled=False)[0] is None
    op, line = _operator()
    assert op.model == "claude-haiku-5-5" and "First Mate model" in line
    op, line = _operator(firstmate_provider="codex", firstmate_model="gpt-6-luna")
    assert op.model == "claude-sonnet-5-5" and "controller model" in line
    op, line = _operator(firstmate_provider="codex", builder_provider="codex", builder_model="gpt-6")
    assert op is None and "needs Claude Code" in line


def test_delegate_permissions_must_be_boolean(tmp_path):
    from harbor.config.workflow import load_workflow
    from workflow_support import config
    path = config(tmp_path)
    text = path.read_text()
    assert "delegate_permissions = false" in text
    assert load_workflow(path).config["decisions"]["delegate_permissions"] is False
    path.write_text(text.replace("delegate_permissions = false", 'delegate_permissions = "yes"'))
    with pytest.raises(ValueError, match="delegate_permissions"):
        load_workflow(path)


def test_launch_prompt_and_skill_tell_the_builder_to_report_permission_blockers(tmp_path, monkeypatch):
    from harbor.cli import _pipeline_text
    from harbor.config.projects import ProjectConfig
    from workflow_support import config
    registry = tmp_path / "registry.md"
    registry.write_text("# registry")
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "ledger.db"))
    cfg = ProjectConfig(name="SampleProject", linear_project_slug="sample", workflow_path=str(config(tmp_path)))
    prompt = " ".join(_pipeline_text(cfg).split())
    assert 'blocker.kind = "permission_blocker"' in prompt and "`blocker.commands`" in prompt
    assert "do not ask a person to widen your permissions" in prompt
    skill = (Path(__file__).resolve().parents[1] / "workflows/work-pipeline/skills/delivery/SKILL.md").read_text()
    assert 'kind = "permission_blocker"' in " ".join(skill.split())


def _doctor_with_delegation(tmp_path, provider):
    import os
    import sys
    from test_github_transport import run_doctor
    run_doctor(tmp_path, provider, None)  # builds the throwaway registry, pipeline and repo
    workflow = tmp_path / "repo" / ".harbor" / "workflow" / "workflow.toml"
    workflow.write_text(workflow.read_text().replace("delegate_permissions = false", "delegate_permissions = true"))
    env = {k: v for k, v in os.environ.items() if k not in ("LINEAR_API_KEY", "HARBOR_LINEAR_API_KEY")}
    env.update(HARBOR_PROJECTS_FILE=str(tmp_path / "projects.md"), HARBOR_DB_PATH=str(tmp_path / "harbor.db"),
               GITHUB_TOKEN="dummy-token-value", HARBOR_CLAUDE_EXECUTABLE="/bin/echo",
               HARBOR_CODEX_EXECUTABLE="/bin/echo")
    return subprocess.run([sys.executable, "-c", "import sys; from harbor.cli import main; sys.exit(main())",
                           "doctor", "SampleProject"], env=env, capture_output=True, text=True, timeout=60)


def test_doctor_reports_delegation_off_by_default(tmp_path):
    from test_github_transport import run_doctor
    result = run_doctor(tmp_path, "claude", None)
    assert "Permission delegation: off (set [decisions] delegate_permissions = true" in result.stdout


def test_doctor_reports_delegation_on_for_a_claude_project(tmp_path):
    result = _doctor_with_delegation(tmp_path, "claude")
    assert ("Permission delegation: on: Claude Code claude-sonnet-5-5 (controller model) in auto permission mode, "
            "outside the builder sandbox") in result.stdout, result.stdout + result.stderr
    assert "dummy-token-value" not in result.stdout + result.stderr


def test_doctor_fails_closed_when_delegation_has_no_claude_provider(tmp_path):
    result = _doctor_with_delegation(tmp_path, "codex")
    assert "Permission delegation: off: delegate_permissions needs Claude Code" in result.stdout, result.stdout
    # The exit status is 1 here anyway (the throwaway Linear endpoint is unreachable); the ``ok`` flag that
    # makes doctor fail for this alone is covered by the permission_operator test below.


def test_doctor_and_start_report_fail_closed_when_delegation_cannot_run(monkeypatch):
    from types import SimpleNamespace
    import harbor.cli as cli
    monkeypatch.setattr(cli, "builder_github", lambda *a, **k: {"env": {}, "token_env": None})
    monkeypatch.setattr(cli.PipelineProfile, "from_project", staticmethod(lambda config: SimpleNamespace()))
    workflow = SimpleNamespace(config={"decisions": {"delegate_permissions": True}})
    codex = SimpleNamespace(firstmate_provider="codex", firstmate_model="gpt-6-luna", builder_provider="codex",
                            builder_model="gpt-6", firstmate_effort=None, builder_effort=None)
    operator, line, ok = cli.permission_operator(codex, workflow, env={})
    assert operator is None and not ok and "needs Claude Code" in line
    claude = SimpleNamespace(**{**vars(codex), "firstmate_provider": "claude", "firstmate_model": "claude-haiku-5-5"})
    operator, line, ok = cli.permission_operator(claude, workflow, env={"HARBOR_CLAUDE_EXECUTABLE": "/no/such/claude"})
    assert operator is None and not ok and "unavailable" in line
    operator, line, ok = cli.permission_operator(claude, workflow, env={"HARBOR_CLAUDE_EXECUTABLE": "/bin/sh"})
    assert operator is not None and ok and line.startswith("on:")
    off = SimpleNamespace(config={"decisions": {}})
    assert cli.permission_operator(codex, off, env={})[2] is True  # off by default, nothing to fix
