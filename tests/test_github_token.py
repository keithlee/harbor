"""Each GitHub project has one configured GitHub token that every part of Harbor uses.

Regression context:

- Claude builders on macOS ran `gh pr view` / `gh pr list` and hung until
  their time limit: the operator exported no token (gh keeps its login in the
  keychain), and the prompt told the builder to try `gh`, which hangs inside
  Claude Code's macOS sandbox.
- `github_token_env` could not be configured at all: the profile only took it
  from internal defaults, so `github_token_env: SAMPLE_GITHUB_TOKEN` in a
  project's pipeline.md had no effect and doctor still asked for GITHUB_TOKEN.

The owner's decision: the project configures its token (``github_token_env``
names the variable), and builders, their child agents, the REST helper,
``workflow check`` and First Mate all use it. Harbor never uses the gh login.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor.github_token import (
    MINIMUM_SCOPES, missing_token_help, preferred_token_env, project_token_env, token_env_candidates,
    validate_token_env,
)
from test_github_transport import production, project, run_doctor, workflow

TOKEN = "dummy-project-token-not-a-secret"
OTHER = "dummy-broader-token-not-a-secret"
ROOT = Path(__file__).resolve().parents[1]


def profile(token_env: str = "", repository: bool = True) -> SimpleNamespace:
    return SimpleNamespace(github_token_env=token_env, github_api_url="https://api.github.com/graphql",
                           repository_owner="owner" if repository else "",
                           repository_name="repo" if repository else "",
                           repository="owner/repo" if repository else "")


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "SAMPLE_GITHUB_TOKEN", "HARBOR_GITHUB_TOKEN_ENV"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# --------------------------------------------------------- the key itself

@pytest.mark.parametrize("value", ["SAMPLE_GITHUB_TOKEN", " GITHUB_TOKEN ", "_x1", "GHS_TOKEN", "GHP_SECRET",
                                   "GITHUB_PAT_NAME", "gho_token", "DEADBEEF"])
def test_github_token_env_accepts_variable_names(value: str) -> None:
    # Genuine names that merely start like a token prefix are fine.
    assert validate_token_env(value) == value.strip()


def test_an_empty_github_token_env_means_unset() -> None:
    assert validate_token_env("") == "" and validate_token_env(None) == ""


@pytest.mark.parametrize("value", ["1TOKEN", "MY-TOKEN", "has space", "$GITHUB_TOKEN"])
def test_github_token_env_refuses_what_is_not_a_variable_name(value: str) -> None:
    with pytest.raises(ValueError, match="environment variable name"):
        validate_token_env(value)


CLASSIC_40_HEX = "0123456789abcdef0123456789abcdef01234567"
TOKEN_SHAPES = ["ghp_" + "a1" * 18, "github_pat_" + "B2_" * 14, "gho_" + "c" * 36, "ghs_" + "D4" * 18,
                CLASSIC_40_HEX, CLASSIC_40_HEX.upper()]


@pytest.mark.parametrize("value", TOKEN_SHAPES)
def test_a_pasted_token_is_refused_without_echoing_it(value: str) -> None:
    with pytest.raises(ValueError) as caught:
        validate_token_env(value)
    assert value not in str(caught.value) and "looks like a GitHub token value" in str(caught.value)


def test_a_legacy_40_hex_token_never_reaches_any_message(tmp_path: Path) -> None:
    # Regression: a 40-hex classic token passed as a variable name and was then
    # echoed by the doctor/start fix, the builder prompt and HARBOR_GITHUB_TOKEN_ENV.
    from harbor.cli import builder_github
    from harbor.github_rest import Client, GitHubRestError
    leaked = profile(CLASSIC_40_HEX)
    assert CLASSIC_40_HEX not in missing_token_help(leaked)
    with pytest.raises(ValueError) as caught:
        project_token_env(leaked, {})
    assert CLASSIC_40_HEX not in str(caught.value)
    with pytest.raises(GitHubRestError) as rest:
        Client({"HARBOR_GITHUB_TOKEN_ENV": CLASSIC_40_HEX})
    assert CLASSIC_40_HEX not in str(rest.value)
    with pytest.raises(ValueError) as prefer:
        preferred_token_env(profile(), {"HARBOR_GITHUB_TOKEN_ENV": CLASSIC_40_HEX})
    assert CLASSIC_40_HEX not in str(prefer.value)
    config = configured_project(tmp_path, token_env=CLASSIC_40_HEX)
    with pytest.raises(ValueError) as built:
        builder_github(config, workflow(), "claude", env={})
    assert CLASSIC_40_HEX not in str(built.value)


def test_a_40_hex_github_token_env_in_config_fails_closed_without_echo(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(ValueError) as caught:
        registry_project(tmp_path, monkeypatch, pipeline=f"github_token_env: {CLASSIC_40_HEX}\n")
    assert CLASSIC_40_HEX not in str(caught.value)


# ------------------------------------------------- configuring it (end to end)

def registry_project(tmp_path: Path, monkeypatch, *, pipeline: str = "", registry: dict[str, str] | None = None,
                     repository: str = "owner/repo"):
    from harbor.config.projects import get_project
    from workflow_support import materialize

    repo = tmp_path / "repo"
    repo.mkdir()
    materialize(repo)
    pipeline_path = tmp_path / "pipeline.md"
    pipeline_path.write_text("# Pipeline\nlinear_project_slug: sample\nlinear_team: SAMPLE\ninstall_command: none\n"
                             + (f"github_repository: {repository}\n" if repository else "") + pipeline)
    registry = registry or {}
    projects = tmp_path / "projects.md"
    projects.write_text(
        "| name | repo_path | pipeline_path |" + "".join(f" {key} |" for key in registry) + "\n"
        + "| --- | --- | --- |" + " --- |" * len(registry) + "\n"
        + f"| SampleProject | {repo} | {pipeline_path} |" + "".join(f" {value} |" for value in registry.values()) + "\n"
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(projects))
    return get_project("SampleProject")


def test_pipeline_github_token_env_reaches_the_profile(tmp_path: Path, monkeypatch) -> None:
    # Fails on main: the profile ignored the key and kept GITHUB_TOKEN.
    from harbor.config.profile import PipelineProfile
    config = registry_project(tmp_path, monkeypatch, pipeline="github_token_env: SAMPLE_GITHUB_TOKEN\n")
    assert PipelineProfile.from_project(config).github_token_env == "SAMPLE_GITHUB_TOKEN"


def test_a_registry_github_token_env_wins_over_the_pipeline(tmp_path: Path, monkeypatch) -> None:
    from harbor.config.profile import PipelineProfile
    config = registry_project(tmp_path, monkeypatch, pipeline="github_token_env: PIPELINE_TOKEN\n",
                              registry={"github_token_env": "REGISTRY_TOKEN"})
    assert PipelineProfile.from_project(config).github_token_env == "REGISTRY_TOKEN"


def test_an_empty_registry_github_token_env_cell_keeps_the_pipeline_value(tmp_path: Path, monkeypatch) -> None:
    from harbor.config.profile import PipelineProfile
    config = registry_project(tmp_path, monkeypatch, pipeline="github_token_env: PIPELINE_TOKEN\n",
                              registry={"github_token_env": ""})
    assert PipelineProfile.from_project(config).github_token_env == "PIPELINE_TOKEN"


@pytest.mark.parametrize("where", ["pipeline", "registry"])
def test_a_token_pasted_into_config_fails_closed_without_echoing_it(tmp_path: Path, monkeypatch, where: str) -> None:
    pasted = "ghp_" + "z" * 36
    with pytest.raises(ValueError) as caught:
        if where == "pipeline":
            registry_project(tmp_path, monkeypatch, pipeline=f"github_token_env: {pasted}\n")
        else:
            registry_project(tmp_path, monkeypatch, registry={"github_token_env": pasted})
    assert pasted not in str(caught.value) and "github_token_env" in str(caught.value)


# ------------------------------------------------------------- precedence

def test_a_configured_variable_is_the_only_source(clean_env) -> None:
    clean_env.setenv("GITHUB_TOKEN", OTHER)
    assert project_token_env(profile("SAMPLE_GITHUB_TOKEN")) is None  # never the broader export
    clean_env.setenv("SAMPLE_GITHUB_TOKEN", TOKEN)
    assert project_token_env(profile("SAMPLE_GITHUB_TOKEN")) == "SAMPLE_GITHUB_TOKEN"


def test_an_unconfigured_project_reads_github_token_then_gh_token(clean_env) -> None:
    assert token_env_candidates("") == ("GITHUB_TOKEN", "GH_TOKEN")
    clean_env.setenv("GH_TOKEN", OTHER)
    assert project_token_env(profile()) == "GH_TOKEN"
    clean_env.setenv("GITHUB_TOKEN", TOKEN)
    assert project_token_env(profile()) == "GITHUB_TOKEN"


def test_a_whitespace_only_value_is_unset_for_every_reader(clean_env) -> None:
    from harbor.clients.github import GitHubClient
    from harbor.github_rest import token_env_name
    clean_env.setenv("GITHUB_TOKEN", "   ")
    assert project_token_env(profile()) is None
    assert token_env_name(os.environ) is None
    assert GitHubClient.from_profile(profile()).token is None  # was "   ": a blank bearer and a 401


def test_the_poller_uses_only_the_configured_variable(clean_env) -> None:
    from harbor.clients.github import GitHubClient
    clean_env.setenv("GITHUB_TOKEN", OTHER)
    assert GitHubClient.from_profile(profile("SAMPLE_GITHUB_TOKEN")).token is None
    clean_env.setenv("SAMPLE_GITHUB_TOKEN", TOKEN)
    assert GitHubClient.from_profile(profile("SAMPLE_GITHUB_TOKEN")).token == TOKEN
    assert GitHubClient.from_profile(profile()).token == OTHER  # unconfigured: today's fallback


# ------------------------------------------------- builders and their children

def configured_project(tmp_path: Path, token_env: str = "SAMPLE_GITHUB_TOKEN") -> SimpleNamespace:
    config = project(tmp_path)
    config.github_token_env = token_env
    return config


def test_the_builder_is_given_the_configured_variable_by_name(tmp_path: Path, clean_env) -> None:
    from harbor.cli import builder_github
    github = builder_github(configured_project(tmp_path), workflow(), "claude",
                            env={"SAMPLE_GITHUB_TOKEN": TOKEN, "GITHUB_TOKEN": OTHER})
    assert github["token_env"] == "SAMPLE_GITHUB_TOKEN"
    assert github["env"]["HARBOR_GITHUB_TOKEN_ENV"] == "SAMPLE_GITHUB_TOKEN"
    assert TOKEN not in json.dumps(github) and OTHER not in json.dumps(github)
    assert "child agents (planner, implementor, reviewer) inherit" in github["fact"]


def test_the_rest_helper_never_falls_back_past_the_configured_variable(tmp_path: Path) -> None:
    from harbor.cli import builder_github
    from harbor.github_rest import Client, GitHubRestError
    env = {"GITHUB_TOKEN": OTHER}
    github = builder_github(configured_project(tmp_path), workflow(), "claude", env=env)
    assert github["token_env"] is None
    assert github["env"]["HARBOR_GITHUB_TOKEN_ENV"] == "SAMPLE_GITHUB_TOKEN"  # named even while unset
    with pytest.raises(GitHubRestError, match="SAMPLE_GITHUB_TOKEN") as caught:
        Client({**env, **github["env"]})
    assert OTHER not in str(caught.value)
    assert Client({**env, **github["env"], "SAMPLE_GITHUB_TOKEN": TOKEN})._token == TOKEN


def test_a_claude_builder_and_its_agent_children_get_the_project_token(tmp_path: Path, monkeypatch, clean_env) -> None:
    # Child agents run inside the builder's Claude Code process (the Agent
    # tool), so they inherit exactly this environment and sandbox.
    from harbor.runner.claude import ClaudeRunner
    from harbor.wake import Wake
    clean_env.setenv("SAMPLE_GITHUB_TOKEN", TOKEN)
    dispatcher, factory, db = production(tmp_path, monkeypatch, provider="claude", model="claude-sonnet-5-5",
                                         token="", token_env="SAMPLE_GITHUB_TOKEN")
    try:
        runner = factory(tmp_path, Wake(ticket={"identifier": "SAMPLE-1"}, action="build"))
        assert isinstance(runner, ClaudeRunner)
        env = runner.environment()
        assert env["SAMPLE_GITHUB_TOKEN"] == TOKEN
        assert env["HARBOR_GITHUB_TOKEN_ENV"] == "SAMPLE_GITHUB_TOKEN"
        argv = runner.command("x", run_id="harbor-run-1")
        assert "Agent" in argv[argv.index("--tools") + 1].split(",")
        assert TOKEN not in json.dumps(argv)
        assert "child agents (planner, implementor, reviewer) inherit" in dispatcher.github_fact
    finally:
        db.close()


# ------------------------------------------------- doctor, start, answer

def test_doctor_reads_a_pipeline_github_token_env(tmp_path: Path) -> None:
    # The live repro: github_token_env in pipeline.md, that variable exported,
    # nothing else. On main doctor said "no token: set GITHUB_TOKEN or GH_TOKEN".
    result = run_doctor(tmp_path, "claude", None, github_env={"SAMPLE_GITHUB_TOKEN": TOKEN},
                        pipeline_extra="github_token_env: SAMPLE_GITHUB_TOKEN\n")
    assert "token from SAMPLE_GITHUB_TOKEN" in result.stdout, result.stdout + result.stderr
    assert TOKEN not in result.stdout + result.stderr


def test_doctor_fails_a_github_project_without_its_token_and_says_what_to_set(tmp_path: Path) -> None:
    result = run_doctor(tmp_path, "claude", None, github_env={"GITHUB_TOKEN": OTHER},
                        pipeline_extra="github_token_env: SAMPLE_GITHUB_TOKEN\n")
    out = result.stdout
    assert result.returncode != 0
    assert "no GitHub token for project SampleProject: export SAMPLE_GITHUB_TOKEN" in out, out
    assert MINIMUM_SCOPES in out and "gh " not in out.split("GitHub transport:")[1].split("\n")[0]
    assert OTHER not in out + result.stderr


def test_the_missing_token_message_names_the_key_variable_and_scopes() -> None:
    unconfigured = missing_token_help(profile())
    assert "set github_token_env" in unconfigured and "GITHUB_TOKEN, then GH_TOKEN" in unconfigured
    assert MINIMUM_SCOPES in unconfigured and "owner/repo" in unconfigured
    assert "export SAMPLE_GITHUB_TOKEN" in missing_token_help(profile("SAMPLE_GITHUB_TOKEN"))
    for text in (unconfigured, missing_token_help(profile("SAMPLE_GITHUB_TOKEN"))):
        assert "gh auth" not in text and "`gh" not in text


def test_start_refuses_builders_for_a_github_project_without_its_token(tmp_path: Path, monkeypatch, clean_env) -> None:
    clean_env.setenv("GITHUB_TOKEN", OTHER)
    with pytest.raises(ValueError, match="export SAMPLE_GITHUB_TOKEN") as caught:
        production(tmp_path, monkeypatch, token=OTHER, token_env="SAMPLE_GITHUB_TOKEN")
    assert OTHER not in str(caught.value)


def test_a_project_without_github_needs_no_token_and_gets_no_warning(tmp_path: Path, monkeypatch, capsys, clean_env) -> None:
    dispatcher, _, db = production(tmp_path, monkeypatch, token="", repository="")
    try:
        assert "no github_repository" in dispatcher.github_fact
        assert "GitHub token" not in capsys.readouterr().err
    finally:
        db.close()


def cli_project(tmp_path: Path, monkeypatch, *, repository: str = "owner/repo", token: str = "") -> None:
    import harbor.cli as cli  # noqa: F401  (imported for monkeypatch targets below)
    registry_project(tmp_path, monkeypatch, repository=repository,
                     pipeline="builder_provider: codex\nlinear_api_url: https://linear.invalid/graphql\n"
                              "github_token_env: SAMPLE_GITHUB_TOKEN\n")
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "harbor.db"))
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/bin/echo")
    monkeypatch.setenv("SAMPLE_GITHUB_TOKEN", token)
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("HARBOR_LINEAR_API_KEY", raising=False)


def test_answer_refuses_with_the_fix_instead_of_a_traceback(tmp_path: Path, monkeypatch, capsys, clean_env) -> None:
    from harbor import cli
    cli_project(tmp_path, monkeypatch)
    code = asyncio.run(cli._answer("esc-unknown", "an answer", project_name="SampleProject"))
    err = capsys.readouterr().err
    assert code == 2 and "export SAMPLE_GITHUB_TOKEN" in err, err


# ------------------------------------------------ `harbor start` itself

def _start_with_recorded_github(tmp_path: Path, monkeypatch, pipeline_extra: str = ""):
    """Run a real shadow ``_start`` once; return (exit code, GitHub requests)."""

    import httpx
    import harbor.cli as cli
    import harbor.clients.github as github_module
    from harbor.config.projects import ProjectConfig

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"data": {"repository": {}}})

    class Recording(github_module.GitHubClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    class OneTicketLinear:
        async def fetch_issues(self, projects, states):
            return [{"id": "linear-id-1", "identifier": "A-1", "title": "A ticket",
                     "state": {"name": "In Progress", "type": "started"},
                     "attachments": {"nodes": [{"sourceType": "github", "url": "https://github.com/example/repo/pull/1"}]},
                     "comments": {"nodes": []}, "inverseRelations": {"nodes": []}}]

        async def close(self):
            pass

    monkeypatch.setattr(cli, "GitHubClient", Recording)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text(f"```yaml\nrepo: example/repo\n{pipeline_extra}```\n", encoding="utf-8")
    cfg = ProjectConfig(name="A", linear_project_slug="a", repo_path=tmp_path, pipeline_path=pipeline)
    monkeypatch.setattr(cli, "get_project", lambda _: cfg)
    monkeypatch.setattr(cli, "_db_path", lambda: tmp_path / "harbor.db")
    code = asyncio.run(cli._start("A", once=True, shadow=True, read_only=True, linear_client=OneTicketLinear(),
                                  first_mate=object(), notifier=object(), unblock_ladder=object()))
    return code, sent


def test_start_polls_github_with_the_configured_project_token(tmp_path: Path, monkeypatch, capsys, clean_env) -> None:
    clean_env.setenv("SAMPLE_GITHUB_TOKEN", TOKEN)
    clean_env.setenv("GITHUB_TOKEN", OTHER)
    code, sent = _start_with_recorded_github(tmp_path, monkeypatch, "github_token_env: SAMPLE_GITHUB_TOKEN\n")
    out = capsys.readouterr()
    assert code == 0, out
    assert sent and all(r.headers["Authorization"] == f"bearer {TOKEN}" for r in sent)
    assert TOKEN not in out.out + out.err


def test_start_never_sends_a_broader_token_when_the_configured_one_is_missing(tmp_path: Path, monkeypatch, capsys, clean_env) -> None:
    clean_env.setenv("GITHUB_TOKEN", OTHER)
    code, sent = _start_with_recorded_github(tmp_path, monkeypatch, "github_token_env: SAMPLE_GITHUB_TOKEN\n")
    assert code == 0, capsys.readouterr()  # shadow read-only start runs; it just has no token to send
    assert sent, "the poller sent no request at all"
    assert all("Authorization" not in r.headers for r in sent), [r.headers.get("Authorization") for r in sent]


# ------------------------------------------------ `harbor workflow check`

def _workflow_check(tmp_path: Path, monkeypatch, status: int):
    import httpx
    import harbor.cli as cli
    import harbor.clients.github as github_module
    from harbor.cli import build_parser
    from harbor.db import HarborDB
    from harbor.wake import apply_phase3_migration
    from test_workflow_github_endpoint import Linear
    from workflow_support import config

    # The Authorization value as sent (Harbor redacts it on the request object
    # once a request fails, so read it here).
    sent: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("Authorization"))
        return httpx.Response(status, json={"message": "Bad credentials"})

    class Recording(github_module.GitHubClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("harbor.clients.github.GitHubClient", Recording)
    monkeypatch.setattr("harbor.clients.linear.LinearClient", Linear)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text("```yaml\nrepo: example/repo\n```\n", encoding="utf-8")
    cfg = SimpleNamespace(name="A", linear_api_url="https://linear.invalid", linear_project_scope=("canonical",),
                          github_repository="example/repo", required_checks=("tests",),
                          pipeline_path=str(pipeline), workflow_path=config(tmp_path, "autonomous"))
    db_path = tmp_path / "harbor.db"
    db = HarborDB(db_path)
    try:
        apply_phase3_migration(db)
        db.replace_tickets("A", [{"identifier": "A-1", "state": "In Progress", "owned": True, "hold": False}])
        with db.connection:
            db.connection.execute("INSERT INTO runs (id, ticket, worktree, status) VALUES (?, ?, ?, ?)",
                                  ("run", "A-1", str(tmp_path), "running"))
    finally:
        db.close()
    monkeypatch.setattr(cli, "get_project", lambda _: cfg)
    monkeypatch.setattr(cli, "_db_path", lambda: db_path)
    args = build_parser().parse_args(["workflow", "check", "A", "--ticket", "A-1", "--event", "pre_merge"])
    return asyncio.run(cli._async_main(args)), sent


def test_workflow_check_in_a_builder_uses_the_variable_harbor_named(tmp_path: Path, monkeypatch, capsys, clean_env) -> None:
    clean_env.setenv("HARBOR_GITHUB_TOKEN_ENV", "SAMPLE_GITHUB_TOKEN")
    clean_env.setenv("SAMPLE_GITHUB_TOKEN", TOKEN)
    clean_env.setenv("GITHUB_TOKEN", OTHER)
    assert preferred_token_env(profile()) == "SAMPLE_GITHUB_TOKEN"
    _, sent = _workflow_check(tmp_path, monkeypatch, 401)
    assert sent == [f"bearer {TOKEN}"]


def test_workflow_check_points_a_401_at_the_project_token(tmp_path: Path, monkeypatch, capsys, clean_env) -> None:
    code, sent = _workflow_check(tmp_path, monkeypatch, 401)
    err = capsys.readouterr().err
    assert code == 2 and sent, err
    assert "401 Unauthorized" in err and "github_token_env" in err, err
    assert "gh auth" not in err


def test_the_pr_merge_harbor_check_never_fetches_with_a_broader_token(monkeypatch) -> None:
    """pr-merge's .harbor/ check (from #61) fell back to GITHUB_TOKEN past the named variable."""
    import harbor.device_proof as device_proof
    from harbor import github_rest

    seen = []
    monkeypatch.setattr(device_proof, "builder_sandbox_change",
                        lambda *a, token=None, **k: seen.append(token) or False)

    class Gh:
        def request(self, method, path, payload=None):
            return {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}}

    env = {"HARBOR_GITHUB_TOKEN_ENV": "SAMPLE_GITHUB_TOKEN", "GITHUB_TOKEN": OTHER, "HARBOR_GIT_DIR": "/nonexistent"}
    github_rest._refuse_builder_sandbox_change(Gh(), "/repos/o/r/pulls", 1, "a" * 40, env)
    github_rest._refuse_builder_sandbox_change(Gh(), "/repos/o/r/pulls", 1, "a" * 40,
                                               {**env, "SAMPLE_GITHUB_TOKEN": "dummy-project-token"})
    assert seen == [None, "dummy-project-token"]


def test_workflow_check_pre_merge_never_fetches_with_a_broader_token(clean_env) -> None:
    from harbor.github_token import token_name
    clean_env.setenv("GITHUB_TOKEN", OTHER)
    # The resolver the pre_merge check uses: strict once a variable is named.
    assert token_name("SAMPLE_GITHUB_TOKEN") is None
    assert token_name("") == "GITHUB_TOKEN"
    clean_env.setenv("SAMPLE_GITHUB_TOKEN", "dummy-project-token")
    assert token_name("SAMPLE_GITHUB_TOKEN") == "SAMPLE_GITHUB_TOKEN"
    source = (ROOT / "src" / "harbor" / "workflow_cli.py").read_text(encoding="utf-8")
    assert "token_env_name(" not in source


# ------------------------------------------------ the rung-0 probe

def test_the_rung_zero_probe_cannot_be_shadowed_by_the_worktree(tmp_path: Path, clean_env) -> None:
    """The probe runs on the host, unsandboxed, with the project token in its env.

    Its cwd is the builder's worktree. Under ``python -m harbor.github_rest`` the
    cwd is sys.path[0], so a ``harbor/`` package the builder wrote there ran in
    place of Harbor's helper. ``PYTHONPATH`` was a second way in.
    """
    from harbor.facts.remediation import execute_remediation

    worktree = tmp_path / "wt"
    planted = worktree / "harbor"
    planted.mkdir(parents=True)
    marker = tmp_path / "SHADOWED"
    (planted / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    (planted / "github_rest.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    clean_env.setenv("SAMPLE_GITHUB_TOKEN", "dummy-project-token-not-a-secret")
    clean_env.setenv("PYTHONPATH", str(worktree))
    github = SimpleNamespace(github_token_env="SAMPLE_GITHUB_TOKEN", github_api_url="https://ghe.invalid/api/graphql",
                             repository="owner/repo", repository_owner="owner", repository_name="repo")
    result = asyncio.run(execute_remediation({"kind": "github_transport"}, cwd=worktree, github_profile=github))
    assert result.attempted and not result.fixed  # ghe.invalid does not resolve
    assert not marker.exists()
    [command] = result.plan.commands
    assert "-I" in command and "-m" not in command
    stderr = result.outputs[-1].stderr if result.outputs else str(result.error)
    assert "dummy-project-token" not in stderr


def test_the_rung_zero_probe_passes_when_github_accepts_the_token(tmp_path: Path, clean_env) -> None:
    """After main's #61 the helper's `api` needs HARBOR_GITHUB_REPOSITORY; the probe never set it.

    Every check then failed with "no repository configured", and Harbor told the
    operator GitHub had rejected a token it never sent.
    """
    from harbor.facts.remediation import execute_remediation

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\ncat >/dev/null\nprintf '{\"full_name\": \"owner/repo\"}\\n200'\n")
    curl.chmod(0o755)
    clean_env.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    clean_env.setenv("SAMPLE_GITHUB_TOKEN", "dummy-project-token-not-a-secret")
    github = SimpleNamespace(github_token_env="SAMPLE_GITHUB_TOKEN", github_api_url="https://api.github.com/graphql",
                             repository="owner/repo", repository_owner="owner", repository_name="repo")
    result = asyncio.run(execute_remediation({"kind": "github_transport"}, cwd=tmp_path, github_profile=github))
    assert result.fixed, result.error
    [command] = result.plan.commands
    assert "HARBOR_GITHUB_REPOSITORY=owner/repo" in command

    linear_only = SimpleNamespace(github_token_env="SAMPLE_GITHUB_TOKEN", github_api_url="", repository="",
                                  repository_owner="", repository_name="")
    result = asyncio.run(execute_remediation({"kind": "github_transport"}, cwd=tmp_path, github_profile=linear_only))
    assert not result.fixed and result.plan.commands == ()
    assert "github_repository" in str(result.error)


def test_the_builder_helper_command_runs_harbors_helper_from_any_cwd(tmp_path: Path) -> None:
    import subprocess
    from harbor.github_rest import helper_command

    planted = tmp_path / "harbor"
    planted.mkdir()
    marker = tmp_path / "SHADOWED"
    for name in ("__init__.py", "github_rest.py"):
        (planted / name).write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(tmp_path),
           "HARBOR_GITHUB_API_URL": "https://ghe.invalid/api/v3", "HARBOR_GITHUB_REPOSITORY": "o/r"}
    result = subprocess.run([*helper_command(), "pr-view", "1"], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    assert not marker.exists()
    # Harbor's real helper ran, from Harbor's own source, and failed closed.
    assert result.returncode == 1 and "harbor.github_rest: no GitHub token" in result.stderr, result.stderr


# ------------------------------------------------ the prompt and gh

def test_the_no_token_prompt_never_steers_to_gh() -> None:
    from harbor.wake import github_transport_fact
    helper = "python -m harbor.github_rest"
    github = github_transport_fact(helper=helper, repository="o/r", token_available=False,
                                   token_names="SAMPLE_GITHUB_TOKEN")
    linear_only = github_transport_fact(helper=helper, repository="", token_available=False)
    assert "(SAMPLE_GITHUB_TOKEN is not set)" in github and "a GitHub token is missing" in github
    assert "no github_repository" in linear_only and "token is missing" not in linear_only
    for fact in (github, linear_only):
        assert "Do not try the gh CLI" in fact and "may use it" not in fact and "gh auth" not in fact


GH_COMMAND = re.compile(r"\bgh\s+(auth|pr|api|issue|repo|run|release|workflow|secret|gist)\b")


def _code_strings(path: Path) -> list[str]:
    """Every string literal Harbor can send somewhere (docstrings excluded)."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value) for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
    }
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]


def test_harbor_never_runs_gh_or_tells_anyone_to() -> None:
    # The owner's rule: Harbor never pulls the operator's gh login (no `gh auth`
    # anything), and no builder-facing prompt, remediation answer, escalation
    # or skill tells a builder or operator to run a gh command.
    offenders = []
    for path in (ROOT / "src" / "harbor").rglob("*.py"):
        for text in _code_strings(path):
            if text == "gh" or GH_COMMAND.search(text):
                offenders.append(f"{path.relative_to(ROOT)}: {text[:120]!r}")
    for path in list((ROOT / "workflows").rglob("*.md")) + list((ROOT / "workflows").rglob("*.toml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if GH_COMMAND.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:120]}")
    assert offenders == []


def test_the_owner_approved_merge_prompt_uses_the_rest_helpers_head_pinned_merge() -> None:
    from harbor.github_rest import helper_invocation
    from harbor.pr_events import build_comment_instruction
    head = "0123456789abcdef0123456789abcdef01234567"
    text = build_comment_instruction(
        {"identifier": "SAMPLE-1"},
        {"number": 7, "url": "https://github.com/owner/repo/pull/7", "head_sha": head},
        {"body": "approved, merge it", "id": "c1", "author": "owner"},
        merge_approval=True,
    )
    assert f"`{helper_invocation()} pr-merge 7 --sha {head}`" in text
    assert not GH_COMMAND.search(text) and "never the gh CLI" in text
