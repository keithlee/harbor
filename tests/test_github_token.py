"""A builder with no exported GitHub token must reach GitHub or fail fast, never hang.

Regression context: on macOS ``gh`` keeps its token in the keychain, so an
operator often has no ``GITHUB_TOKEN``/``GH_TOKEN`` exported. Harbor's REST
helper then had no token, the launch prompt told the builder to try ``gh``, and
inside Claude Code's macOS sandbox ``gh pr view``/``gh pr list`` hung until the
run's time limit. Harbor now asks ``gh auth token`` once at launch, outside the
sandbox, for a project that names a GitHub repository, and keeps the answer in
its process environment only.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor.github_token import (
    TokenResolution, TokenSource, adopt_for_profile, adopt_gh_token, gh_hostname, gh_lookup, missing_token_help,
)
from test_github_transport import project, run_doctor, workflow

TOKEN = "gho_FAKEGHLOGIN000000000000000000000000"


def fake_gh(tmp_path: Path, body: str) -> Path:
    """A ``gh`` stand-in that records its argv and then runs ``body``."""

    path = tmp_path / "gh"
    log = tmp_path / "gh-argv.json"
    path.write_text(
        f"#!{sys.executable}\nimport json, os, subprocess, sys, time\n"
        f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n" + body + "\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def logged_in(tmp_path: Path) -> Path:
    return fake_gh(tmp_path, f"print({TOKEN!r})")


def argv(tmp_path: Path) -> list[str] | None:
    log = tmp_path / "gh-argv.json"
    return json.loads(log.read_text()) if log.exists() else None


def github_profile(repository: bool = True, url: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(github_token_env="GITHUB_TOKEN", github_api_url=url,
                           repository_owner="owner" if repository else "",
                           repository_name="repo" if repository else "")


# ------------------------------------------------------------- adoption itself

def test_no_exported_token_adopts_the_gh_login_into_the_environment_only(tmp_path: Path) -> None:
    from harbor.cli import builder_github
    env: dict[str, str] = {}
    resolution = adopt_for_profile(github_profile(), environ=env, executable=str(logged_in(tmp_path)))
    assert resolution.source is TokenSource.GH_CLI and resolution.gh_checked
    assert env == {"GH_TOKEN": TOKEN}
    assert argv(tmp_path) == ["auth", "token", "--hostname", "github.com"]
    assert TOKEN not in repr(resolution)
    # The builder's REST transport now finds a token by name, never by value.
    github = builder_github(project(tmp_path), workflow(), "claude", env=env, token_resolution=resolution)
    assert github["token_env"] == "GH_TOKEN"
    assert github["env"]["HARBOR_GITHUB_TOKEN_ENV"] == "GH_TOKEN"
    assert "GitHub transport: REST helper" in github["fact"]
    assert TOKEN not in json.dumps(github)


def test_a_project_without_a_github_repository_never_gets_the_gh_login(tmp_path: Path) -> None:
    # Linear-only: no config asked for GitHub, so builders must not carry the
    # operator's broad gh OAuth token.
    env: dict[str, str] = {}
    resolution = adopt_for_profile(github_profile(repository=False), environ=env,
                                   executable=str(logged_in(tmp_path)))
    assert resolution.source is TokenSource.NOT_NEEDED and not resolution.gh_checked
    assert env == {} and argv(tmp_path) is None


def test_an_exported_token_wins_and_gh_is_not_asked(tmp_path: Path) -> None:
    env = {"CUSTOM_TOKEN": "exported"}
    resolution = adopt_gh_token("CUSTOM_TOKEN", None, environ=env, executable=str(logged_in(tmp_path)))
    assert resolution.source is TokenSource.EXPORTED and resolution.env_name == "CUSTOM_TOKEN"
    assert env == {"CUSTOM_TOKEN": "exported"} and argv(tmp_path) is None


def test_a_whitespace_only_export_is_unset_for_the_builder_and_the_poller_alike(tmp_path: Path, monkeypatch) -> None:
    from harbor.clients.github import GitHubClient
    from harbor.github_rest import token_env_name
    monkeypatch.setenv("GITHUB_TOKEN", "   ")
    monkeypatch.setenv("GH_TOKEN", "")
    assert token_env_name(os.environ, "GITHUB_TOKEN") is None
    assert GitHubClient(token_env="GITHUB_TOKEN").token is None  # was "   ": a blank bearer and a 401
    resolution = adopt_gh_token("GITHUB_TOKEN", None, executable=str(logged_in(tmp_path)))
    assert resolution.source is TokenSource.GH_CLI
    # Both now use the adopted token, not the blank export.
    assert token_env_name(os.environ, "GITHUB_TOKEN") == "GH_TOKEN"
    assert GitHubClient(token_env="GITHUB_TOKEN").token == TOKEN


@pytest.mark.parametrize("body", ["sys.exit(1)", "print('')", "print('two words')"])
def test_no_gh_login_leaves_the_environment_alone(tmp_path: Path, body: str) -> None:
    env: dict[str, str] = {}
    resolution = adopt_gh_token(None, None, environ=env, executable=str(fake_gh(tmp_path, body)))
    assert resolution.source is TokenSource.NOT_FOUND and resolution.gh_checked
    assert env == {}


def test_a_missing_gh_is_not_an_error(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    assert adopt_gh_token(None, None, environ=env, executable=str(tmp_path / "absent")).source is TokenSource.NOT_FOUND
    assert env == {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_hanging_gh_and_everything_it_started_are_killed(tmp_path: Path) -> None:
    # gh may start helpers (on macOS, the keychain client); a timeout must not
    # leave them behind.
    pids = tmp_path / "pids.json"
    body = (
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pids)!r}, 'w').write(json.dumps([os.getpid(), child.pid, os.getpgrp()]))\n"
        "time.sleep(60)"
    )
    started = time.monotonic()
    assert gh_lookup("github.com", executable=str(fake_gh(tmp_path, body)), timeout=1.5) == (TokenSource.TIMED_OUT, None)
    assert time.monotonic() - started < 10
    gh_pid, child_pid, group = json.loads(pids.read_text())
    assert group == gh_pid  # gh ran in its own session
    deadline = time.monotonic() + 5
    while _alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(gh_pid) and not _alive(child_pid)
    with pytest.raises(ProcessLookupError):
        os.killpg(group, 0)


@pytest.mark.parametrize(("graphql", "host"), [
    (None, "github.com"),
    ("https://api.github.com/graphql", "github.com"),
    ("https://ghe.example.test/api/graphql", "ghe.example.test"),
    ("https://ghe.example.test:8443/api/graphql", "ghe.example.test:8443"),
    ("https://ghe.example.test:443/api/graphql", "ghe.example.test"),
    ("https://api.github.com:443/graphql", "github.com"),
    ("https://api.acme.ghe.com/graphql", "acme.ghe.com"),
    ("http://ghe.example.test/api/graphql", None),
    ("https:///graphql", None),
    ("https://ghe.example.test:notaport/api/graphql", None),
    ("https://user@ghe example/graphql", None),
])
def test_gh_is_asked_for_the_configured_host(graphql: str | None, host: str | None) -> None:
    assert gh_hostname(graphql) == host


def test_an_unparseable_endpoint_never_asks_gh(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    resolution = adopt_for_profile(github_profile(url="https://ghe.example.test:bad/api/graphql"), environ=env,
                                   executable=str(logged_in(tmp_path)))
    assert resolution.source is TokenSource.BAD_ENDPOINT and env == {} and argv(tmp_path) is None
    assert "gh auth login" not in missing_token_help("GITHUB_TOKEN", "https://ghe.example.test:bad/api/graphql")


# ------------------------------------------------------------------ the prompt

def test_the_no_token_prompt_says_only_what_harbor_checked() -> None:
    from harbor.wake import github_transport_fact
    helper = "python -m harbor.github_rest"
    checked = github_transport_fact(helper=helper, repository="o/r", token_available=False, gh_result="none")
    timed_out = github_transport_fact(helper=helper, repository="o/r", token_available=False, gh_result="timed out")
    unchecked = github_transport_fact(helper=helper, repository="o/r", token_available=False)
    linear_only = github_transport_fact(helper=helper, repository="", token_available=False)
    assert "`gh auth token` had none at launch" in checked
    assert "`gh auth token` did not answer at launch" in timed_out
    assert "gh auth token" not in unchecked and "(none exported)" in unchecked
    for fact in (checked, timed_out, unchecked):
        assert "a GitHub token is missing" in fact
    assert "no github_repository" in linear_only and "token is missing" not in linear_only
    for fact in (checked, timed_out, unchecked, linear_only):
        assert "Do not try the gh CLI" in fact and "may use it" not in fact


# ------------------------------------------------------- the CLI entry points

def test_doctor_reuses_the_gh_login_without_printing_it(tmp_path: Path) -> None:
    gh = logged_in(tmp_path)
    result = run_doctor(tmp_path, "claude", None, github_env={"HARBOR_GH_EXECUTABLE": str(gh)})
    out = result.stdout + result.stderr
    assert TOKEN not in out
    assert "token from GH_TOKEN, reused from `gh auth token` at launch (held in memory only)" in result.stdout, out


def test_doctor_without_any_token_names_both_fixes_and_fails(tmp_path: Path) -> None:
    result = run_doctor(tmp_path, "claude", None, github_env={})
    assert ("no GitHub token for builders: export GITHUB_TOKEN or GH_TOKEN, or run "
            "`gh auth login --hostname github.com`") in result.stdout, result.stdout + result.stderr
    assert result.returncode != 0


def test_start_warns_when_a_github_project_has_no_token(tmp_path: Path, monkeypatch, capsys) -> None:
    from test_github_transport import production
    monkeypatch.setenv("GH_TOKEN", "")
    dispatcher, _, db = production(tmp_path, monkeypatch, token="")
    try:
        assert "no GitHub token is available" in dispatcher.github_fact
        assert "Do not try the gh CLI" in dispatcher.github_fact
        assert "Harbor: warning: no GitHub token for builders" in capsys.readouterr().err
    finally:
        db.close()


def test_start_does_not_warn_for_a_project_without_a_github_repository(tmp_path: Path, monkeypatch, capsys) -> None:
    from test_github_transport import production
    monkeypatch.setenv("GH_TOKEN", "")
    dispatcher, _, db = production(tmp_path, monkeypatch, token="", repository="")
    try:
        assert "no github_repository" in dispatcher.github_fact
        assert "GitHub token" not in capsys.readouterr().err
    finally:
        db.close()


def cli_project(tmp_path: Path, monkeypatch, *, repository: str = "owner/repo") -> Path:
    """A registry project with an unreachable Linear endpoint and a fake gh login."""

    import subprocess
    from workflow_support import materialize

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    materialize(repo)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text("# Pipeline\nlinear_project_slug: sample\nlinear_team: SAMPLE\ninstall_command: none\n"
                        "builder_provider: codex\nlinear_api_url: https://linear.invalid/graphql\n"
                        + (f"github_repository: {repository}\n" if repository else ""))
    projects = tmp_path / "projects.md"
    projects.write_text("| name | repo_path | pipeline_path |\n| --- | --- | --- |\n"
                        f"| SampleProject | {repo} | {pipeline} |\n")
    gh_dir = tmp_path / "gh-home"
    gh_dir.mkdir()
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(projects))
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "harbor.db"))
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/bin/echo")
    monkeypatch.setenv("HARBOR_GH_EXECUTABLE", str(logged_in(gh_dir)))
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("GH_TOKEN", "")
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("HARBOR_LINEAR_API_KEY", raising=False)
    return gh_dir


def _run(coroutine) -> int:
    """Run a CLI coroutine; it must return an exit code, never raise."""

    code = asyncio.run(coroutine)
    assert isinstance(code, int), code
    return code


@pytest.mark.parametrize("command", ["facts", "answer"])
def test_facts_and_answer_adopt_the_gh_login_for_a_github_project(tmp_path: Path, monkeypatch, capsys, command) -> None:
    from harbor import cli
    gh_dir = cli_project(tmp_path, monkeypatch)
    _run(cli._facts("SampleProject") if command == "facts"
         else cli._answer("esc-unknown", "an answer", project_name="SampleProject"))
    assert argv(gh_dir) == ["auth", "token", "--hostname", "github.com"]
    assert os.environ["GH_TOKEN"] == TOKEN
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


@pytest.mark.parametrize("command", ["facts", "answer"])
def test_facts_and_answer_leave_a_project_without_github_alone(tmp_path: Path, monkeypatch, command) -> None:
    from harbor import cli
    gh_dir = cli_project(tmp_path, monkeypatch, repository="")
    _run(cli._facts("SampleProject") if command == "facts"
         else cli._answer("esc-unknown", "an answer", project_name="SampleProject"))
    assert argv(gh_dir) is None and os.environ["GH_TOKEN"] == ""


# ------------------------------------------- GitHub Enterprise Server (finding 5)

def test_a_ghes_login_is_held_as_gh_enterprise_token_never_gh_token(tmp_path: Path) -> None:
    # A child `gh` sends GH_TOKEN to github.com; an Enterprise token must not be there.
    env: dict[str, str] = {}
    resolution = adopt_gh_token("GITHUB_TOKEN", "https://ghe.example.test/api/graphql",
                                environ=env, executable=str(logged_in(tmp_path)))
    assert resolution == TokenResolution(TokenSource.GH_CLI, "GH_ENTERPRISE_TOKEN", "ghe.example.test")
    assert env == {"GH_ENTERPRISE_TOKEN": TOKEN}
    assert argv(tmp_path) == ["auth", "token", "--hostname", "ghe.example.test"]


def test_a_ghe_com_login_is_held_as_gh_token(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    resolution = adopt_gh_token(None, "https://api.acme.ghe.com/graphql", environ=env,
                                executable=str(logged_in(tmp_path)))
    assert resolution.env_name == "GH_TOKEN" and env == {"GH_TOKEN": TOKEN}


def test_an_exported_gh_enterprise_token_is_used_not_replaced(tmp_path: Path) -> None:
    env = {"GH_ENTERPRISE_TOKEN": "exported"}
    resolution = adopt_gh_token(None, "https://ghe.example.test/api/graphql", environ=env,
                                executable=str(logged_in(tmp_path)))
    assert resolution.source is TokenSource.EXPORTED and resolution.env_name == "GH_ENTERPRISE_TOKEN"
    assert env == {"GH_ENTERPRISE_TOKEN": "exported"} and argv(tmp_path) is None


def test_harbor_clients_read_an_adopted_gh_enterprise_token(tmp_path: Path, monkeypatch) -> None:
    from harbor.cli import builder_github
    from harbor.clients.github import GitHubClient
    from harbor.github_rest import Client
    from harbor.github_token import preferred_token_env
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, "")
    monkeypatch.delenv("HARBOR_GITHUB_TOKEN_ENV", raising=False)
    ghes = project(tmp_path)
    ghes.github_api_url = "https://ghe.example.test/api/graphql"
    profile = SimpleNamespace(github_token_env="GITHUB_TOKEN", github_api_url=ghes.github_api_url,
                              repository_owner="owner", repository_name="repo")
    resolution = adopt_for_profile(profile, executable=str(logged_in(tmp_path)))
    # The poller.
    assert GitHubClient.from_profile(profile, token_env=resolution.env_name).token == TOKEN
    # The builder: Harbor names the variable, and the REST helper reads it.
    github = builder_github(ghes, workflow(), "codex", token_resolution=resolution)
    assert github["env"]["HARBOR_GITHUB_TOKEN_ENV"] == "GH_ENTERPRISE_TOKEN"
    child = dict(os.environ, **github["env"])
    assert Client(child)._token == TOKEN
    # `harbor workflow check` inside that builder.
    monkeypatch.setenv("HARBOR_GITHUB_TOKEN_ENV", "GH_ENTERPRISE_TOKEN")
    assert GitHubClient.from_profile(profile, token_env=preferred_token_env(profile)).token == TOKEN
    assert os.environ["GH_TOKEN"] == ""  # never placed where gh would send it to github.com


# ------------------------------------------------------- gh timeout (finding 7)

def test_a_gh_timeout_gets_its_own_source_and_advice(tmp_path: Path) -> None:
    from harbor.cli import builder_github
    env: dict[str, str] = {}
    resolution = adopt_gh_token(None, None, environ=env, executable=str(fake_gh(tmp_path, "time.sleep(30)")),
                                timeout=0.5)
    assert resolution.source is TokenSource.TIMED_OUT and env == {}
    advice = missing_token_help("GITHUB_TOKEN or GH_TOKEN", None, resolution)
    assert "did not answer within 10 s" in advice and "gh auth login" not in advice
    github = builder_github(project(tmp_path), workflow(), "claude", env=env, token_resolution=resolution)
    assert "did not answer within" in github["missing_token"] and "gh auth login" not in github["missing_token"]
    assert "`gh auth token` did not answer at launch" in github["fact"]


# ------------------------------------------------- the opt-in seam (finding 1)

def test_switching_gh_reuse_off_in_its_one_place_stops_the_lookup(tmp_path: Path, monkeypatch) -> None:
    import harbor.github_token as github_token
    monkeypatch.setattr(github_token, "gh_reuse_enabled", lambda profile: False)
    env: dict[str, str] = {}
    resolution = adopt_for_profile(github_profile(), environ=env, executable=str(logged_in(tmp_path)))
    assert resolution.source is TokenSource.REUSE_OFF and env == {} and argv(tmp_path) is None
    assert "gh auth login" not in missing_token_help("GITHUB_TOKEN", None, resolution)


# ------------------------------------------------ `harbor start` itself (finding 2)

def _start_with_recorded_github(tmp_path: Path, monkeypatch, endpoint: str, pr_url: str):
    """Run a real shadow ``_start`` once; return (exit code, GitHub requests, gh argv)."""

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
                     "attachments": {"nodes": [{"sourceType": "github", "url": pr_url}]},
                     "comments": {"nodes": []}, "inverseRelations": {"nodes": []}}]

        async def close(self):
            pass

    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, "")
    gh_dir = tmp_path / "gh-home"
    gh_dir.mkdir()
    monkeypatch.setenv("HARBOR_GH_EXECUTABLE", str(logged_in(gh_dir)))
    monkeypatch.setattr(cli, "GitHubClient", Recording)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text(f"```yaml\nrepo: example/repo\ngithub_api_url: {endpoint}\n```\n", encoding="utf-8")
    cfg = ProjectConfig(name="A", linear_project_slug="a", repo_path=tmp_path, pipeline_path=pipeline)
    monkeypatch.setattr(cli, "get_project", lambda _: cfg)
    monkeypatch.setattr(cli, "_db_path", lambda: tmp_path / "harbor.db")
    code = asyncio.run(cli._start("A", once=True, shadow=True, read_only=True, linear_client=OneTicketLinear(),
                                  first_mate=object(), notifier=object(), unblock_ladder=object()))
    return code, sent, argv(gh_dir)


def test_start_adopts_the_gh_login_for_its_github_poller(tmp_path: Path, monkeypatch, capsys) -> None:
    code, sent, gh_argv = _start_with_recorded_github(
        tmp_path, monkeypatch, "https://api.github.com/graphql", "https://github.com/example/repo/pull/1")
    out = capsys.readouterr()
    assert code == 0, out
    assert gh_argv == ["auth", "token", "--hostname", "github.com"]
    assert sent, "start sent no GitHub request"
    assert all(r.headers["Authorization"] == f"bearer {TOKEN}" for r in sent)
    assert os.environ["GH_TOKEN"] == TOKEN
    assert TOKEN not in out.out + out.err


def test_start_on_ghes_sends_the_adopted_enterprise_token_only_to_its_host(tmp_path: Path, monkeypatch, capsys) -> None:
    code, sent, gh_argv = _start_with_recorded_github(
        tmp_path, monkeypatch, "https://ghe.example.test/api/graphql", "https://ghe.example.test/example/repo/pull/1")
    assert code == 0, capsys.readouterr()
    assert gh_argv == ["auth", "token", "--hostname", "ghe.example.test"]
    assert sent and all(r.url.host == "ghe.example.test" for r in sent)
    assert all(r.headers["Authorization"] == f"bearer {TOKEN}" for r in sent)
    assert os.environ["GH_ENTERPRISE_TOKEN"] == TOKEN and os.environ["GH_TOKEN"] == ""


# ------------------------------------------------ `harbor workflow check` (finding 4)

def _workflow_check(tmp_path: Path, monkeypatch, status: int, token: str):
    import httpx
    import harbor.cli as cli
    import harbor.clients.github as github_module
    from harbor.cli import build_parser
    from harbor.db import HarborDB
    from harbor.wake import apply_phase3_migration
    from test_workflow_github_endpoint import Linear, PR_URL
    from workflow_support import config

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status, json={"message": "Bad credentials"})

    class Recording(github_module.GitHubClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("harbor.clients.github.GitHubClient", Recording)
    monkeypatch.setattr("harbor.clients.linear.LinearClient", Linear)
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setenv("GH_TOKEN", "")
    monkeypatch.delenv("HARBOR_GITHUB_TOKEN_ENV", raising=False)
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


def test_workflow_check_without_a_token_says_how_to_fix_it_and_never_asks_gh(tmp_path: Path, monkeypatch, capsys) -> None:
    gh_dir = tmp_path / "gh-home"
    gh_dir.mkdir()
    monkeypatch.setenv("HARBOR_GH_EXECUTABLE", str(logged_in(gh_dir)))
    code, sent = _workflow_check(tmp_path, monkeypatch, 401, "")
    err = capsys.readouterr().err
    assert code == 2 and sent and "Authorization" not in sent[0].headers, err
    assert "401 Unauthorized (the token is missing" in err and "export GITHUB_TOKEN or GH_TOKEN" in err, err
    assert "does not reuse `gh auth token`" in err
    assert argv(gh_dir) is None  # it runs inside the sandbox, where gh hangs


def test_workflow_check_points_a_401_at_the_fix(tmp_path: Path, monkeypatch, capsys) -> None:
    code, sent = _workflow_check(tmp_path, monkeypatch, 401, "stale-not-a-real-token")
    err = capsys.readouterr().err
    assert code == 2 and sent, err
    assert sent[0].headers["Authorization"] == "bearer stale-not-a-real-token"
    assert "401 Unauthorized" in err and "export GITHUB_TOKEN or GH_TOKEN" in err, err
    assert "stale-not-a-real-token" not in err
