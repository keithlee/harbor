"""A dontAsk Claude builder can run Harbor's GitHub helper, set env vars, and serve a dev server.

Regression context (Bookgen HAN-94): a Claude builder in ``--permission-mode
dontAsk`` had these Bash commands refused with "Claude Code is running in
don't ask mode": the GitHub helper run with Harbor's ``/usr/bin/env K=V ...``
prefix, ``pnpm dev`` with placeholder variables in front, and a production
build retried with a scratch database path set the same way. Sandbox
auto-allow does not cover a command that starts with a variable assignment
(``FOO=1 cmd``, ``env FOO=1 cmd``, ``/usr/bin/env FOO=1 cmd``); only an exact
allow rule does, and only the CLI had one.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from harbor.cli import builder_github
from harbor.github_rest import builder_helper_invocation, helper_command
from harbor.runner.base import RunnerError
from harbor.runner.claude import ClaudeRunner, workflow_permissions

TOKEN = "ghp_TESTTOKEN0000000000000000000000000000"
INVOCATION = "/usr/bin/env HARBOR_PROJECTS_FILE=/r/projects.md HARBOR_DB_PATH=/r/harbor.db PYTHONPATH=/h/src /h/python -P -m harbor.cli"
ENV = {"HARBOR_GITHUB_API_URL": "https://api.github.com", "HARBOR_GITHUB_REPOSITORY": "owner/repo",
       "HARBOR_GITHUB_TOKEN_ENV": "GH_TOKEN", "HARBOR_GIT_FETCH_URL": "https://github.com/owner/repo.git"}


def settings(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--settings") + 1])


def wired(tmp_path: Path) -> ClaudeRunner:
    (tmp_path / "wt").mkdir(exist_ok=True)
    runner = ClaudeRunner(tmp_path / "wt", model="claude-sonnet-5-5", reasoning_effort="medium", executable="claude",
                          writable_roots=(tmp_path / "state",))
    runner.harbor_invocation = INVOCATION
    runner.github_helper_invocation = builder_helper_invocation(ENV)
    return runner


# ------------------------------------------------------------- GitHub helper

def test_the_helper_invocation_pins_its_configuration_and_names_the_token_only() -> None:
    helper = builder_helper_invocation({**ENV, "GH_TOKEN": TOKEN, "UNRELATED": "x"})
    argv = shlex.split(helper)
    assert argv[0] == "/usr/bin/env"
    assert argv[1:4] == ["HARBOR_GITHUB_API_URL=https://api.github.com", "HARBOR_GITHUB_REPOSITORY=owner/repo",
                         "HARBOR_GITHUB_TOKEN_ENV=GH_TOKEN"]
    assert argv[4:] == helper_command()
    # The token is named, never inlined; clone paths stay out of the rule (they live in the env only).
    assert TOKEN not in helper and "UNRELATED" not in helper and "HARBOR_GIT_FETCH_URL" not in helper


def test_the_helper_gets_its_own_exact_permission_rule(tmp_path: Path) -> None:
    runner = wired(tmp_path)
    allow = settings(runner.command("x", run_id="run"))["permissions"]["allow"]
    assert f"Bash({runner.github_helper_invocation}:*)" in allow
    assert f"Bash({INVOCATION}:*)" in allow
    # Nothing broad: every Bash rule is one of the two exact invocations.
    assert [rule for rule in allow if rule.startswith("Bash(")] == [
        f"Bash({INVOCATION}:*)", f"Bash({runner.github_helper_invocation}:*)"]
    assert TOKEN not in json.dumps(allow)


@pytest.mark.parametrize("helper", ["/bin/helper*", "/bin/helper\nrm -rf /", "/bin/[x]"])
def test_a_helper_a_rule_cannot_express_fails_closed(tmp_path: Path, helper: str) -> None:
    with pytest.raises(ValueError, match="GitHub helper invocation"):
        workflow_permissions(INVOCATION, None, helper)
    runner = wired(tmp_path)
    runner.github_helper_invocation = helper
    with pytest.raises(RunnerError, match="GitHub helper invocation"):
        runner.command("x", run_id="run")


def test_the_builder_helper_command_runs_harbors_helper_from_any_cwd(tmp_path: Path) -> None:
    # A planted package in the cwd (the worktree) or on PYTHONPATH must not stand in for Harbor's helper.
    planted = tmp_path / "harbor"
    planted.mkdir()
    (planted / "__init__.py").write_text("")
    (planted / "github_rest.py").write_text("print('PLANTED'); raise SystemExit(0)\n")
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(shlex.split(builder_helper_invocation(ENV)) + ["--help"], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "PLANTED" not in result.stdout and "pr-view" in result.stdout


def test_builder_github_names_the_exact_helper_and_never_the_token(tmp_path: Path) -> None:
    from types import SimpleNamespace
    config = SimpleNamespace(name="SampleProject", github_repository="owner/repo", pipeline_path=None,
                             repo_path=str(tmp_path), github_api_url="https://api.github.com/graphql")
    github = builder_github(config, SimpleNamespace(config={}), "claude", env={"GH_TOKEN": TOKEN})
    assert github["helper"] == builder_helper_invocation(github["env"])
    assert f"`{github['helper']} <op>`" in github["fact"]
    assert TOKEN not in json.dumps(github)


# ------------------------------------------------------- per-turn shell note

def test_the_shell_note_names_the_exact_helper_and_the_env_var_forms() -> None:
    from harbor.runner.claude import builder_shell_note
    helper = builder_helper_invocation(ENV)
    note = builder_shell_note(helper)
    assert f"`{helper} <op>`" in note
    assert "export" in note and ".env.local" in note
    for refused in ("`FOO=1 cmd`", "`env FOO=1 cmd`", "`/usr/bin/env FOO=1 cmd`", "`export FOO=1`"):
        assert refused in note
    assert "node --env-file=.env.local" in note and "never commit" in note
    assert "script file" in note  # live: `node -e "console.log('FOO=' + x)"` is refused too
    # Without a helper (Codex-only wiring, MCP-less tests) the note still carries the env-var rules.
    bare = builder_shell_note("")
    assert bare.startswith("Harbor shell note") and "GitHub helper: run it exactly" not in bare


@pytest.mark.parametrize("resume", [False, True])
def test_every_turn_fresh_or_resumed_carries_the_shell_note(tmp_path: Path, monkeypatch, resume: bool) -> None:
    import asyncio
    from harbor.runner.codex import CodexRunner
    runner = wired(tmp_path)
    seen: list[str] = []

    async def fake_execute(self, context, instruction, *, resume, before_spawn=None):
        seen.append(instruction)

    monkeypatch.setattr(CodexRunner, "_execute", fake_execute)
    asyncio.run(runner._execute(None, "resume prompt" if resume else "launch prompt", resume=resume))
    assert seen and seen[0].startswith("resume prompt" if resume else "launch prompt")
    assert f"`{runner.github_helper_invocation} <op>`" in seen[0]
    assert "export" in seen[0]
