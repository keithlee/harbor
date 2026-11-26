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
    TokenSource, adopt_for_profile, adopt_gh_token, gh_hostname, missing_token_help, token_from_gh,
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
    assert token_from_gh("github.com", executable=str(fake_gh(tmp_path, body)), timeout=1.5) is None
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
    checked = github_transport_fact(helper=helper, repository="o/r", token_available=False, gh_checked=True)
    unchecked = github_transport_fact(helper=helper, repository="o/r", token_available=False)
    linear_only = github_transport_fact(helper=helper, repository="", token_available=False)
    assert "`gh auth token` had none at launch" in checked
    assert "gh auth token" not in unchecked and "(none exported)" in unchecked
    assert "a GitHub token is missing" in checked and "a GitHub token is missing" in unchecked
    assert "no github_repository" in linear_only and "token is missing" not in linear_only
    for fact in (checked, unchecked, linear_only):
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


def _run(coroutine) -> None:
    try:
        asyncio.run(coroutine)
    except Exception:  # Linear is unreachable on purpose; adoption happens first.
        pass


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
