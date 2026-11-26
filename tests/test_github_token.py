"""A builder with no exported GitHub token must reach GitHub or fail fast, never hang.

Regression context: on macOS ``gh`` keeps its token in the keychain, so an
operator often has no ``GITHUB_TOKEN``/``GH_TOKEN`` exported. Harbor's REST
helper then had no token, the launch prompt told the builder to try ``gh``, and
inside Claude Code's macOS sandbox ``gh pr view``/``gh pr list`` hung until the
run's time limit. Harbor now asks ``gh auth token`` once at launch, outside the
sandbox, and keeps the answer in its process environment only.
"""
from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path

import pytest

from harbor.github_token import adopt_gh_token, gh_hostname, token_from_gh
from test_github_transport import project, run_doctor, workflow

TOKEN = "gho_FAKEGHLOGIN000000000000000000000000"


def fake_gh(tmp_path: Path, body: str) -> Path:
    """A ``gh`` stand-in that records its argv and then runs ``body``."""

    path = tmp_path / "gh"
    log = tmp_path / "gh-argv.json"
    path.write_text(
        f"#!{sys.executable}\nimport json, sys, time\n"
        f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n" + body + "\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def logged_in(tmp_path: Path) -> Path:
    return fake_gh(tmp_path, f"print({TOKEN!r})")


def argv(tmp_path: Path) -> list[str] | None:
    log = tmp_path / "gh-argv.json"
    return json.loads(log.read_text()) if log.exists() else None


def test_no_exported_token_adopts_the_gh_login_into_the_environment_only(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    source = adopt_gh_token("GITHUB_TOKEN", "https://api.github.com/graphql",
                            environ=env, executable=str(logged_in(tmp_path)))
    assert source == "gh auth token"
    assert env == {"GH_TOKEN": TOKEN}
    assert argv(tmp_path) == ["auth", "token", "--hostname", "github.com"]
    # The builder's REST transport now finds a token by name, never by value.
    github = builder_github_for(tmp_path, env)
    assert github["token_env"] == "GH_TOKEN"
    assert github["env"]["HARBOR_GITHUB_TOKEN_ENV"] == "GH_TOKEN"
    assert "GitHub transport: REST helper" in github["fact"]
    assert TOKEN not in json.dumps({k: v for k, v in github.items() if k != "env"}) + json.dumps(github["env"])


def builder_github_for(tmp_path: Path, env: dict[str, str]):
    from harbor.cli import builder_github
    return builder_github(project(tmp_path), workflow(), "claude", env=env)


def test_an_exported_token_wins_and_gh_is_not_asked(tmp_path: Path) -> None:
    env = {"CUSTOM_TOKEN": "exported"}
    source = adopt_gh_token("CUSTOM_TOKEN", None, environ=env, executable=str(logged_in(tmp_path)))
    assert source == "CUSTOM_TOKEN" and env == {"CUSTOM_TOKEN": "exported"}
    assert argv(tmp_path) is None


@pytest.mark.parametrize("body", ["sys.exit(1)", "print('')", "print('two words')"])
def test_no_gh_login_leaves_the_environment_alone(tmp_path: Path, body: str) -> None:
    env: dict[str, str] = {}
    assert adopt_gh_token(None, None, environ=env, executable=str(fake_gh(tmp_path, body))) is None
    assert env == {}


def test_a_missing_gh_is_not_an_error(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    assert adopt_gh_token(None, None, environ=env, executable=str(tmp_path / "absent")) is None
    assert env == {}


def test_a_hanging_gh_cannot_hang_harbor(tmp_path: Path) -> None:
    started = time.monotonic()
    assert token_from_gh("github.com", executable=str(fake_gh(tmp_path, "time.sleep(30)")), timeout=0.5) is None
    assert time.monotonic() - started < 10


@pytest.mark.parametrize(("graphql", "host"), [
    (None, "github.com"),
    ("https://api.github.com/graphql", "github.com"),
    ("https://ghe.example.test/api/graphql", "ghe.example.test"),
])
def test_gh_is_asked_for_the_configured_host(graphql: str | None, host: str) -> None:
    assert gh_hostname(graphql) == host


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


def test_start_warns_when_builders_have_no_token(tmp_path: Path, monkeypatch, capsys) -> None:
    from test_github_transport import production
    monkeypatch.delenv("GH_TOKEN", raising=False)
    # production() exports test_github_transport.TOKEN as GITHUB_TOKEN; an
    # empty value is "not set" to Harbor, as it is to gh.
    monkeypatch.setattr("test_github_transport.TOKEN", "", raising=True)
    dispatcher, _, db = production(tmp_path, monkeypatch)
    try:
        assert "no GitHub token is available" in dispatcher.github_fact
        assert "Do not try the gh CLI" in dispatcher.github_fact
        err = capsys.readouterr().err
        assert "Harbor: warning: no GitHub token for builders" in err
    finally:
        db.close()
