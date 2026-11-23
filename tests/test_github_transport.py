"""The builder's GitHub transport: REST helper by default, GitHub MCP opt-in.

Regression context: Harbor used to probe ``gh auth status`` outside the builder
sandbox and tell the builder ``gh`` was usable inside it. In Claude Code's macOS
sandbox ``gh`` hangs on TLS, so the builder stalled on every GitHub call.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor.cli import builder_github
from harbor.config.workflow import DEFAULT_GITHUB_MCP_URL, github_settings
from harbor.github_rest import (
    Client, GitHubRestError, curl_config, rest_base_url, run, token_env_name,
)
from harbor.runner.claude import github_mcp_config
from harbor.wake import build_instruction, github_transport_fact

TOKEN = "ghp_TESTTOKEN0000000000000000000000000000"


# ---------------------------------------------------------------- REST helper

@pytest.mark.parametrize(("graphql", "rest"), [
    ("https://api.github.com/graphql", "https://api.github.com"),
    ("https://ghe.example.test/api/graphql", "https://ghe.example.test/api/v3"),
    ("https://api.github.com", "https://api.github.com"),
    ("", "https://api.github.com"),
])
def test_rest_base_is_derived_from_the_graphql_setting(graphql: str, rest: str) -> None:
    assert rest_base_url(graphql) == rest


def test_rest_base_refuses_plain_http() -> None:
    with pytest.raises(ValueError, match="https"):
        rest_base_url("http://ghe.example.test/api/graphql")


def test_token_env_prefers_the_configured_name_and_reports_only_the_name() -> None:
    env = {"GH_TOKEN": TOKEN, "CUSTOM": TOKEN}
    assert token_env_name(env, "CUSTOM") == "CUSTOM"
    assert token_env_name(env) == "GH_TOKEN"
    assert token_env_name({"GITHUB_TOKEN": "  "}) is None


def test_curl_config_escapes_values_and_refuses_file_references() -> None:
    text = curl_config("POST", "https://api.github.com/x", TOKEN, json.dumps({"body": 'a "quote"\nline'}))
    assert 'data-binary = "{\\"body\\": \\"a \\\\\\"quote\\\\\\"\\\\nline\\"}"' in text
    with pytest.raises(GitHubRestError, match="JSON"):
        curl_config("POST", "https://api.github.com/x", TOKEN, "@/etc/passwd")


@pytest.fixture
def fake_curl(tmp_path: Path):
    """A curl stand-in that records argv and the stdin config, then answers."""
    log = tmp_path / "calls.jsonl"
    script = tmp_path / "curl"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "cfg = sys.stdin.read()\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv, 'config': cfg}}) + '\\n')\n"
        "print(os.environ.get('FAKE_BODY', '{}'))\n"
        "sys.stdout.write(os.environ.get('FAKE_STATUS', '200'))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def calls() -> list[dict]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return SimpleNamespace(path=str(script), calls=calls)


def client(fake_curl, **env) -> Client:
    base = {"GITHUB_TOKEN": TOKEN, "HARBOR_GITHUB_API_URL": "https://api.github.com"}
    return Client({**base, **env}, curl=fake_curl.path)


def test_the_token_reaches_curl_on_stdin_never_on_argv(fake_curl, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_BODY", json.dumps({"login": "octo"}))
    assert client(fake_curl).request("GET", "/user") == {"login": "octo"}
    [call] = fake_curl.calls()
    assert not any(TOKEN in arg for arg in call["argv"])
    assert f'header = "Authorization: Bearer {TOKEN}"' in call["config"]
    assert 'url = "https://api.github.com/user"' in call["config"]


def test_an_http_error_is_reported_without_the_token(fake_curl, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_BODY", json.dumps({"message": "Bad credentials"}))
    monkeypatch.setenv("FAKE_STATUS", "401")
    with pytest.raises(GitHubRestError) as raised:
        client(fake_curl).request("GET", "/user")
    assert "401" in str(raised.value) and "Bad credentials" in str(raised.value)
    assert TOKEN not in str(raised.value)


@pytest.mark.parametrize("path", ["https://evil.example/x", "//evil.example/x", "repos/o/r"])
def test_requests_never_leave_the_configured_host(fake_curl, path: str) -> None:
    with pytest.raises(GitHubRestError, match="relative"):
        client(fake_curl).request("GET", path)
    assert fake_curl.calls() == []


def test_a_missing_token_fails_before_any_request() -> None:
    with pytest.raises(GitHubRestError, match="no GitHub token"):
        Client({"HARBOR_GITHUB_API_URL": "https://api.github.com"})


def test_pr_create_posts_to_the_configured_repository(fake_curl, monkeypatch, tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text("Why\n\nBecause.")
    monkeypatch.setenv("FAKE_BODY", json.dumps({"number": 9, "html_url": "u", "head": {"sha": "abc"}}))
    env = {"HARBOR_GITHUB_REPOSITORY": "owner/repo"}
    result = run(["pr-create", "--head", "feat", "--base", "main", "--title", "T", "--body-file", str(body)],
                 env=env, client=client(fake_curl))
    assert result["number"] == 9 and result["head_sha"] == "abc"
    config = fake_curl.calls()[0]["config"]
    assert 'url = "https://api.github.com/repos/owner/repo/pulls"' in config
    assert 'request = "POST"' in config and "Because." in config


def test_pr_merge_pins_the_head_sha(fake_curl, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_BODY", json.dumps({"merged": True}))
    run(["--repo", "o/r", "pr-merge", "4", "--sha", "deadbeef"], env={}, client=client(fake_curl))
    config = fake_curl.calls()[0]["config"]
    assert 'request = "PUT"' in config and "/repos/o/r/pulls/4/merge" in config
    assert '\\"sha\\": \\"deadbeef\\"' in config and '\\"merge_method\\": \\"squash\\"' in config


def test_pr_checks_reports_how_many_legacy_statuses_exist(fake_curl, monkeypatch) -> None:
    responses = iter([
        {"head": {"sha": "abc"}},
        {"check_runs": [{"name": "tests", "status": "completed", "conclusion": "success"}]},
        {"state": "pending", "total_count": 0},
    ])

    class Scripted(Client):
        def request(self, method, path, payload=None):  # noqa: ARG002
            return next(responses)

    result = run(["--repo", "o/r", "pr-checks", "4"], env={}, client=Scripted({"GITHUB_TOKEN": TOKEN}))
    assert result["check_runs"] == [{"name": "tests", "status": "completed", "conclusion": "success"}]
    assert result["combined_status"] == "pending" and result["combined_status_count"] == 0


def test_a_repository_is_required_for_pr_operations(fake_curl) -> None:
    with pytest.raises(GitHubRestError, match="no repository"):
        run(["pr-view", "1"], env={}, client=client(fake_curl))


# ------------------------------------------------------------- configuration

def test_rest_is_the_default_transport() -> None:
    assert github_settings({}) == {"transport": "rest", "mcp_url": DEFAULT_GITHUB_MCP_URL}


def test_mcp_is_opt_in_with_a_configurable_endpoint() -> None:
    url = "https://ghe-mcp.example.test/mcp/"
    assert github_settings({"github": {"transport": "mcp", "mcp_url": url}})["mcp_url"] == url


@pytest.mark.parametrize(("section", "error"), [
    ({"transport": "gh"}, "transport"),
    ({"transport": "mcp", "mcp_url": "http://insecure.example/mcp"}, "https"),
    ({"mcp_url": "https://api.githubcopilot.com/mcp/"}, "only used"),
])
def test_invalid_github_settings_fail_closed(section: dict, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        github_settings({"github": section})


def project(tmp_path: Path, repository: str = "owner/repo") -> SimpleNamespace:
    return SimpleNamespace(name="SampleProject", github_repository=repository, pipeline_path=None,
                           repo_path=str(tmp_path), github_api_url="https://api.github.com/graphql")


def workflow(section: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(config={"github": section} if section else {})


def test_rest_transport_passes_names_and_endpoints_never_the_token(tmp_path: Path) -> None:
    github = builder_github(project(tmp_path), workflow(), "claude", env={"GH_TOKEN": TOKEN})
    assert github["env"] == {
        "HARBOR_GITHUB_API_URL": "https://api.github.com",
        "HARBOR_GITHUB_REPOSITORY": "owner/repo",
        "HARBOR_GITHUB_TOKEN_ENV": "GH_TOKEN",
    }
    assert github["mcp"] is None
    assert "-m harbor.github_rest" in github["fact"] and "owner/repo" in github["fact"]
    assert TOKEN not in json.dumps(github)


def test_mcp_transport_is_claude_only(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="builder_provider: claude"):
        builder_github(project(tmp_path), workflow({"transport": "mcp"}), "codex", env={"GITHUB_TOKEN": TOKEN})


def test_mcp_transport_configures_the_server_with_a_token_reference(tmp_path: Path) -> None:
    github = builder_github(project(tmp_path), workflow({"transport": "mcp"}), "claude", env={"GITHUB_TOKEN": TOKEN})
    assert github["mcp"] == {"url": DEFAULT_GITHUB_MCP_URL, "token_env": "GITHUB_TOKEN"}
    assert "GitHub transport: MCP" in github["fact"]


def test_without_a_token_the_builder_is_told_to_report_a_blocker(tmp_path: Path) -> None:
    github = builder_github(project(tmp_path), workflow(), "claude", env={})
    assert github["token_env"] is None and "HARBOR_GITHUB_TOKEN_ENV" not in github["env"]
    assert "No GitHub token" in github["fact"] and "blocker" in github["fact"]


# --------------------------------------------------------------- builder view

def test_the_fact_never_advertises_gh_and_reaches_the_instruction() -> None:
    fact = github_transport_fact("rest", helper="python -m harbor.github_rest", repository="o/r")
    assert "Do not use the gh CLI" in fact and "pr-create" in fact
    text = build_instruction({"identifier": "SAMPLE-1", "title": "t", "state": "Todo"}, github_fact=fact)
    assert fact in text
    assert github_transport_fact("rest", helper="") == ""


def test_claude_mcp_config_references_the_token_variable_not_its_value() -> None:
    config = github_mcp_config({"url": DEFAULT_GITHUB_MCP_URL, "token_env": "GITHUB_TOKEN"})
    assert config["mcpServers"]["github"]["headers"] == {"Authorization": "Bearer ${GITHUB_TOKEN}"}
    assert github_mcp_config(None) is None

