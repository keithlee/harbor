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
    # ~/.curlrc is ignored (-q must be first) and URL globbing is off.
    assert call["argv"][1] == "-q"
    assert call["config"].splitlines()[0] == "globoff"


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
    # The fake answers every request with this body: the PR read (head deadbeef) and the merge.
    monkeypatch.setenv("FAKE_BODY", json.dumps({"merged": True, "head": {"sha": "deadbeef"}, "base": {"sha": "cafe"}}))
    # deadbeef is not a real commit; this test is about the request, so .harbor/ is unchanged.
    monkeypatch.setattr("harbor.device_proof.builder_sandbox_change", lambda *args: False)
    run(["--repo", "o/r", "pr-merge", "4", "--sha", "deadbeef"], env={}, client=client(fake_curl))
    config = fake_curl.calls()[-1]["config"]
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
    assert "no GitHub token" in github["fact"] and "blocker" in github["fact"]
    assert "If the gh CLI works" in github["fact"]  # keychain-only gh users are not stranded


# --------------------------------------------------------------- builder view

def test_the_fact_never_advertises_gh_and_reaches_the_instruction() -> None:
    fact = github_transport_fact("rest", helper="python -m harbor.github_rest", repository="o/r")
    assert "Prefer this over the gh CLI" in fact and "pr-create" in fact
    assert "Available and authenticated" not in fact
    text = build_instruction({"identifier": "SAMPLE-1", "title": "t", "state": "Todo"}, github_fact=fact)
    assert fact in text
    assert github_transport_fact("rest", helper="") == ""


def test_claude_mcp_config_references_the_token_variable_not_its_value() -> None:
    config = github_mcp_config({"url": DEFAULT_GITHUB_MCP_URL, "token_env": "GITHUB_TOKEN"})
    assert config["mcpServers"]["github"]["headers"] == {"Authorization": "Bearer ${GITHUB_TOKEN}"}
    assert github_mcp_config(None) is None



def test_mcp_on_github_enterprise_requires_an_explicit_endpoint(tmp_path: Path) -> None:
    ghes = project(tmp_path)
    ghes.github_api_url = "https://ghe.example.test/api/graphql"
    with pytest.raises(ValueError, match="mcp_url is required"):
        builder_github(ghes, workflow({"transport": "mcp"}), "claude", env={"GITHUB_TOKEN": TOKEN})
    own = "https://ghe.example.test/mcp/"
    github = builder_github(ghes, workflow({"transport": "mcp", "mcp_url": own}), "claude", env={"GITHUB_TOKEN": TOKEN})
    assert github["mcp"]["url"] == own
    assert github["env"]["HARBOR_GITHUB_API_URL"] == "https://ghe.example.test/api/v3"


# ------------------------------------------------- production wiring (HAN-87)

def production(tmp_path: Path, monkeypatch, *, extra: str = "", provider: str = "codex", model: str = "test-model"):
    import subprocess
    from harbor.cli import build_production_dispatcher
    from harbor.config.projects import ProjectConfig
    from harbor.db import HarborDB
    from workflow_support import materialize

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    path = materialize(tmp_path, extra=extra, models={"controller": {"model": model, "reasoning_effort": "medium"}})
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("HARBOR_CLAUDE_EXECUTABLE", "/bin/echo")
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample", repo_path=tmp_path,
                           workflow_path=str(path), install_command="none", github_repository="owner/repo",
                           builder_provider=provider, builder_model=model)
    db = HarborDB(tmp_path / "state.db")
    dispatcher, _, factory = build_production_dispatcher(config, db)
    return dispatcher, factory, db


def test_the_production_dispatcher_tells_a_new_build_its_transport(tmp_path: Path, monkeypatch) -> None:
    from harbor.wake import Wake
    dispatcher, factory, db = production(tmp_path, monkeypatch)
    try:
        wake = dispatcher._initial_wake(Wake(ticket={"identifier": "SAMPLE-1"}, action="build", instruction="ticket"))
        assert "GitHub transport: REST helper" in wake.instruction and "owner/repo" in wake.instruction
        assert "Available and authenticated in your sandbox: gh" not in wake.instruction
        runner = factory(tmp_path, Wake(ticket={"identifier": "SAMPLE-1"}, action="build"))
        env = runner.environment()
        assert env["HARBOR_GITHUB_REPOSITORY"] == "owner/repo"
        assert env["HARBOR_GITHUB_TOKEN_ENV"] == "GITHUB_TOKEN"
        assert env["HARBOR_GITHUB_API_URL"] == "https://api.github.com"
        assert getattr(runner, "github_mcp", None) is None
    finally:
        db.close()


def test_the_production_claude_runner_gets_the_github_mcp_server(tmp_path: Path, monkeypatch) -> None:
    from harbor.runner.claude import ClaudeRunner
    from harbor.wake import Wake
    dispatcher, factory, db = production(tmp_path, monkeypatch, extra='[github]\ntransport = "mcp"\n',
                                         provider="claude", model="claude-sonnet-5-5")
    try:
        assert "GitHub transport: MCP" in dispatcher.github_fact
        runner = factory(tmp_path, Wake(ticket={"identifier": "SAMPLE-1"}, action="build"))
        assert isinstance(runner, ClaudeRunner)
        argv = runner.command("x", run_id="harbor-run-1")
        mcp = json.loads(argv[argv.index("--mcp-config") + 1])
        assert mcp["mcpServers"]["github"]["headers"]["Authorization"] == "Bearer ${GITHUB_TOKEN}"
        assert TOKEN not in json.dumps(argv)
    finally:
        db.close()


def test_harbor_start_reports_a_transport_misconfiguration_without_a_traceback(tmp_path: Path) -> None:
    import subprocess
    import sys
    from workflow_support import materialize

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    materialize(repo, extra='[github]\ntransport = "mcp"\n')
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text("# Pipeline\nlinear_project_slug: sample\nlinear_team: SAMPLE\ninstall_command: none\n")
    projects = tmp_path / "projects.md"
    projects.write_text(
        "| name | repo_path | pipeline_path | linear_project_slug | linear_team | github_repository |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        f"| SampleProject | {repo} | {pipeline} | sample | SAMPLE | owner/repo |\n"
    )
    env = {k: v for k, v in __import__("os").environ.items() if k not in ("LINEAR_API_KEY", "HARBOR_LINEAR_API_KEY")}
    env.update(HARBOR_PROJECTS_FILE=str(projects), HARBOR_DB_PATH=str(tmp_path / "harbor.db"), GITHUB_TOKEN="dummy")
    result = subprocess.run([sys.executable, "-c", "import sys; from harbor.cli import main; sys.exit(main())",
                             "start", "SampleProject", "--once"], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 2, result.stderr
    assert "Harbor: github.transport = \"mcp\" requires builder_provider: claude" in result.stderr
    assert "Traceback" not in result.stderr


# ------------------------------------------- builder sandbox network allowlist

GITHUB_COM_DOMAINS = ["github.com", "api.github.com", "api.linear.app", "registry.npmjs.org"]


def sandbox_domains(argv: list[str]) -> list[str]:
    settings = json.loads(argv[argv.index("--settings") + 1])
    return settings["sandbox"]["network"]["allowedDomains"]


def claude_production(tmp_path: Path, monkeypatch, pipeline: str = "", *, provider: str = "claude",
                      workflow_extra: str = "", **config):
    import subprocess
    from harbor.cli import build_production_dispatcher
    from harbor.config.projects import ProjectConfig
    from harbor.db import HarborDB
    from workflow_support import materialize

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    path = materialize(tmp_path, extra=workflow_extra,
                       models={"controller": {"model": "claude-sonnet-5-5", "reasoning_effort": "medium"}})
    if pipeline:
        (tmp_path / ".harbor" / "pipeline.md").write_text(f"# Pipeline\n{pipeline}\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("HARBOR_CLAUDE_EXECUTABLE", "/bin/echo")
    project_config = ProjectConfig(name="SampleProject", linear_project_slug="sample", repo_path=tmp_path,
                                   workflow_path=str(path), install_command="none", github_repository="owner/repo",
                                   builder_provider=provider, builder_model="claude-sonnet-5-5", **config)
    db = HarborDB(tmp_path / "state.db")
    try:
        return build_production_dispatcher(project_config, db), db
    except BaseException:
        db.close()
        raise


def production_claude_domains(tmp_path: Path, monkeypatch, pipeline: str = "", **config) -> list[str]:
    from harbor.wake import Wake
    (_, _, factory), db = claude_production(tmp_path, monkeypatch, pipeline, **config)
    try:
        runner = factory(tmp_path, Wake(ticket={"identifier": "SAMPLE-1"}, action="build"))
        return sandbox_domains(runner.command("x", run_id="harbor-run-1"))
    finally:
        db.close()


IP_GHES = "github_api_url: https://10.0.0.5/api/graphql"


@pytest.mark.parametrize("pipeline", [IP_GHES, "github_api_url: https://github/api/graphql"])
def test_a_codex_project_is_not_held_to_the_claude_sandbox_host_rules(tmp_path: Path, monkeypatch, pipeline) -> None:
    # Codex's sandbox does not filter by host, so a GHES on an IP literal or a
    # single-label name must keep working for Codex builders.
    from harbor.runner.codex import CodexRunner
    from harbor.wake import Wake
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/bin/echo")
    (_, _, factory), db = claude_production(tmp_path, monkeypatch, pipeline, provider="codex")
    try:
        runner = factory(tmp_path, Wake(ticket={"identifier": "SAMPLE-1"}, action="build"))
        assert type(runner) is CodexRunner
        assert runner.environment()["HARBOR_GITHUB_API_URL"].endswith("/api/v3")
    finally:
        db.close()


def test_a_resumed_claude_session_on_a_codex_project_still_gets_the_ghes_host(tmp_path: Path, monkeypatch) -> None:
    from harbor.runner.claude import ClaudeRunner
    from harbor.runner.quota import bind_runner_identity
    from harbor.wake import Wake
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/bin/echo")
    (_, _, factory), db = claude_production(
        tmp_path, monkeypatch, "github_api_url: https://ghe.example.test/api/graphql", provider="codex")
    try:
        bind_runner_identity(db.connection, "harbor-run-1", "claude", "claude-sonnet-5-5", "medium")
        runner = factory(tmp_path, Wake(ticket={"identifier": "SAMPLE-1"}, action="build", resume="harbor-run-1"))
        assert isinstance(runner, ClaudeRunner)
        assert sandbox_domains(runner.command("x", run_id="harbor-run-1"))[-1] == "ghe.example.test"
    finally:
        db.close()


def test_a_resumed_claude_session_refuses_an_ip_literal_ghes(tmp_path: Path, monkeypatch) -> None:
    from harbor.runner.quota import bind_runner_identity
    from harbor.wake import Wake
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/bin/echo")
    (_, _, factory), db = claude_production(tmp_path, monkeypatch, IP_GHES, provider="codex")
    try:
        bind_runner_identity(db.connection, "harbor-run-1", "claude", "claude-sonnet-5-5", "medium")
        with pytest.raises(ValueError, match="GitHub API URL"):
            factory(tmp_path, Wake(ticket={"identifier": "SAMPLE-1"}, action="build", resume="harbor-run-1"))
    finally:
        db.close()


def test_a_resumed_claude_session_reports_why_it_cannot_resume(tmp_path: Path, monkeypatch, capsys) -> None:
    # The project is now Codex, so start and doctor never checked the Claude
    # host rules. The resume must still fail closed, but say why, once.
    from harbor.runner.quota import bind_runner_identity
    from harbor.wake import Wake
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/bin/echo")
    (_, _, factory), db = claude_production(tmp_path, monkeypatch, IP_GHES, provider="codex")
    try:
        assert capsys.readouterr().err == ""
        bind_runner_identity(db.connection, "harbor-run-1", "claude", "claude-sonnet-5-5", "medium")
        wake = Wake(ticket={"identifier": "SAMPLE-1"}, action="reengage", resume="harbor-run-1")
        for _ in range(2):  # every poll retries the resume
            with pytest.raises(ValueError, match="SAMPLE-1 cannot resume.*GitHub API URL"):
                factory(tmp_path, wake)
        err = capsys.readouterr().err
        assert err.count("Harbor: warning: the retained Claude builder session for SAMPLE-1 cannot resume") == 1
        # The URL is read once when harbor start builds the dispatcher, and the
        # warning is not repeated, so it must say a restart is needed, and name
        # the aliases an operator may have used instead of github_api_url.
        assert ("set github_api_url (or github_graphql_url / github_endpoint) to a host name the Claude "
                "sandbox can allow, then restart Harbor") in err
    finally:
        db.close()


def test_a_resumed_claude_builder_that_cannot_hydrate_is_not_silent(tmp_path: Path, monkeypatch, capsys) -> None:
    import asyncio
    from harbor.runner.quota import bind_runner_identity
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/bin/echo")
    (dispatcher, _, _), db = claude_production(tmp_path, monkeypatch, IP_GHES, provider="codex")
    try:
        worktree = tmp_path / "wt" / "SAMPLE-1"
        worktree.mkdir(parents=True)
        dispatcher.worktree_manager = SimpleNamespace()  # attach the retained path as-is
        from harbor.runner.base import apply_phase4_migration
        from harbor.wake import apply_phase3_migration
        apply_phase3_migration(db)
        apply_phase4_migration(db)
        with db.connection:
            db.connection.execute(
                "INSERT INTO tickets(identifier, project, title, state, owned, observed_at) "
                "VALUES ('SAMPLE-1', 'SampleProject', 'Sample', 'In Progress', 1, '2026-01-01T00:00:00Z')"
            )
            db.connection.execute(
                "INSERT INTO runs(id, ticket, worktree, status, started_at) "
                "VALUES ('harbor-run-1', 'SAMPLE-1', ?, 'waiting', '2026-01-01T00:00:00Z')",
                (str(worktree),),
            )
        bind_runner_identity(db.connection, "harbor-run-1", "claude", "claude-sonnet-5-5", "medium")
        assert asyncio.run(dispatcher.hydrate("SAMPLE-1")) is None  # still fail closed
        assert "retained Claude builder session for SAMPLE-1 cannot resume" in capsys.readouterr().err
    finally:
        db.close()


def test_a_resumed_claude_session_warns_about_an_unexpressible_linear_url(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    from harbor.runner.quota import bind_runner_identity
    from harbor.wake import Wake
    monkeypatch.setenv("HARBOR_CODEX_EXECUTABLE", "/bin/echo")
    (_, _, factory), db = claude_production(tmp_path, monkeypatch, provider="codex", linear_api_url=LOCAL_LINEAR_STUB)
    try:
        assert capsys.readouterr().err == ""  # a Codex project is not held to the Claude rules at start
        bind_runner_identity(db.connection, "harbor-run-1", "claude", "claude-sonnet-5-5", "medium")
        wake = Wake(ticket={"identifier": "SAMPLE-1"}, action="reengage", resume="harbor-run-1")
        runner = factory(tmp_path, wake)
        factory(tmp_path, wake)
        assert sandbox_domains(runner.command("x", run_id="harbor-run-1")) == GITHUB_COM_DOMAINS  # not widened
        err = capsys.readouterr().err
        assert err.count("Harbor: warning: the Linear API URL must be an https URL") == 1
        assert "if you change linear_api_url (or api_url), restart Harbor" in err
    finally:
        db.close()


def test_a_github_com_project_keeps_the_default_sandbox_allowlist(tmp_path: Path, monkeypatch) -> None:
    assert production_claude_domains(tmp_path, monkeypatch) == GITHUB_COM_DOMAINS


def test_a_github_enterprise_project_lets_the_builder_sandbox_reach_its_host(tmp_path: Path, monkeypatch) -> None:
    # The REST helper calls https://ghe.example.test/api/v3 and git pushes to
    # ghe.example.test; the hardcoded GitHub.com allowlist blocked both.
    domains = production_claude_domains(tmp_path, monkeypatch, "github_api_url: https://ghe.example.test/api/graphql")
    assert domains == [*GITHUB_COM_DOMAINS, "ghe.example.test"]


def test_a_custom_linear_endpoint_is_allowed_through_the_sandbox(tmp_path: Path, monkeypatch) -> None:
    domains = production_claude_domains(tmp_path, monkeypatch, linear_api_url="https://linear.example.test/graphql")
    assert domains == [*GITHUB_COM_DOMAINS, "linear.example.test"]


def test_an_api_host_also_allows_its_git_host() -> None:
    from harbor.runner.claude import sandbox_hosts
    assert sandbox_hosts("https://api.acme.ghe.com/graphql") == ("api.acme.ghe.com", "acme.ghe.com")
    assert sandbox_hosts("https://api.github.com/graphql") == ("api.github.com", "github.com")
    assert sandbox_hosts("https://GHE.example.test:8443/api/graphql") == ("ghe.example.test",)


@pytest.mark.parametrize("url", [
    "http://ghe.example.test/api/graphql",
    "https://*.example.test/api/graphql",
    "https://ghe example.test/api/graphql",
    "https://user@ghe.example.test/api/graphql",
    "https:///api/graphql",
    "https://ghe.example.test\n/api/graphql",
    "https://-bad.example.test/graphql",
    "https://[::1]/graphql",
    "https://10.0.0.1/api/graphql",
    # Numeric forms URL parsers and inet_aton read as IPv4 (all 127.0.0.1).
    "https://0x7f.0x0.0x0.0x1/api/graphql",
    "https://127.0.0.0x1/api/graphql",
    "https://0177.0.0.0x01/api/graphql",
    "https://example.0x10/api/graphql",
    "https://example.0x/api/graphql",
    "ghe.example.test",
    "",
])
def test_a_malformed_endpoint_fails_closed_instead_of_widening_the_sandbox(url: str) -> None:
    from harbor.runner.claude import sandbox_hosts
    with pytest.raises(ValueError):
        sandbox_hosts(url)


@pytest.mark.parametrize(("url", "host"), [
    ("https://xn--bcher-kva.example/api/graphql", "xn--bcher-kva.example"),
    ("https://0x7f.example.test/api/graphql", "0x7f.example.test"),
    ("https://ghe.0xfoo/api/graphql", "ghe.0xfoo"),
    ("https://10.0.0.5.example.test/api/graphql", "10.0.0.5.example.test"),
])
def test_names_that_only_look_numeric_are_still_host_names(url: str, host: str) -> None:
    from harbor.runner.claude import sandbox_hosts
    assert sandbox_hosts(url) == (host,)


LOCAL_LINEAR_STUB = "http://127.0.0.1:8080/graphql"


def test_a_local_linear_stub_does_not_stop_a_claude_project(tmp_path: Path, monkeypatch, capsys) -> None:
    # LinearClient accepts http, and a Claude project on a local Linear stub ran
    # before the sandbox allowlist existed. Skip that host; never widen to fit it.
    domains = production_claude_domains(tmp_path, monkeypatch, linear_api_url=LOCAL_LINEAR_STUB)
    assert domains == GITHUB_COM_DOMAINS
    err = capsys.readouterr().err
    assert "Harbor: warning: the Linear API URL must be an https URL" in err
    assert "if you change linear_api_url (or api_url), restart Harbor" in err  # harbor start's warning


def test_a_linear_url_the_sandbox_cannot_express_is_reported_not_raised(tmp_path: Path) -> None:
    config = project(tmp_path)
    config.linear_api_url = LOCAL_LINEAR_STUB
    github = builder_github(config, workflow(), "claude", env={"GITHUB_TOKEN": TOKEN})
    assert github["sandbox_hosts"] == ("api.github.com", "github.com")
    assert "Linear API URL" in github["linear_sandbox_warning"]


def test_the_default_linear_endpoint_has_one_source() -> None:
    from harbor.cli import claude_sandbox_hosts
    from harbor.clients.linear import DEFAULT_LINEAR_API_URL, LinearClient
    from harbor.config.projects import ProjectConfig
    assert ProjectConfig(name="SampleProject", linear_project_slug="sample").linear_api_url is DEFAULT_LINEAR_API_URL
    assert LinearClient(token="x").endpoint is DEFAULT_LINEAR_API_URL
    assert claude_sandbox_hosts(SimpleNamespace(linear_api_url=""), "https://api.github.com") == (
        "api.github.com", "github.com", "api.linear.app")


def test_the_production_factory_refuses_a_malformed_github_endpoint(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="GitHub API URL"):
        claude_production(tmp_path, monkeypatch, "github_api_url: https://*.example.test/api/graphql")


def run_doctor(tmp_path: Path, provider: str, github_api_url: str | None,
               linear_api_url: str = "https://linear.invalid/graphql", *, registry: dict[str, str] | None = None,
               workflow_extra: str = ""):
    import os
    import subprocess
    import sys
    from workflow_support import materialize

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    materialize(repo, extra=workflow_extra,
                models={"controller": {"model": "claude-sonnet-5-5", "reasoning_effort": "medium"}})
    pipeline = tmp_path / "pipeline.md"
    # HARBOR_WORKTREE_ROOT does not override a loaded config (worktree_root is "" not None),
    # so set worktree_root in the pipeline to keep doctor's disk check out of the real home.
    worktree_root = tmp_path / "wt"
    pipeline.write_text("# Pipeline\nlinear_project_slug: sample\nlinear_team: SAMPLE\ninstall_command: none\n"
                        f"builder_provider: {provider}\nlinear_api_url: {linear_api_url}\n"
                        f"worktree_root: {worktree_root}\n"
                        + (f"github_api_url: {github_api_url}\n" if github_api_url is not None else ""))
    extra = registry or {}
    projects = tmp_path / "projects.md"
    projects.write_text(
        "| name | repo_path | pipeline_path | linear_project_slug | linear_team | github_repository |"
        + "".join(f" {key} |" for key in extra) + "\n"
        + "| --- | --- | --- | --- | --- | --- |" + " --- |" * len(extra) + "\n"
        + f"| SampleProject | {repo} | {pipeline} | sample | SAMPLE | owner/repo |"
        + "".join(f" {value} |" for value in extra.values()) + "\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("LINEAR_API_KEY", "HARBOR_LINEAR_API_KEY")}
    env.update(HARBOR_PROJECTS_FILE=str(projects), HARBOR_DB_PATH=str(tmp_path / "harbor.db"),
               GITHUB_TOKEN="dummy-token-value", HARBOR_CLAUDE_EXECUTABLE="/bin/echo",
               HARBOR_CODEX_EXECUTABLE="/bin/echo")
    result = subprocess.run([sys.executable, "-c", "import sys; from harbor.cli import main; sys.exit(main())",
                             "doctor", "SampleProject"], env=env, capture_output=True, text=True, timeout=60)
    assert "dummy-token-value" not in result.stdout + result.stderr
    # Exit 2 means doctor refused the project config and stopped before its
    # disk check, so there is no worktree root line to look for.
    if result.returncode != 2:
        assert f" at {worktree_root.resolve()} " in result.stdout, result.stdout
    return result


def test_doctor_lists_the_builder_sandbox_hosts_for_github_enterprise(tmp_path: Path) -> None:
    result = run_doctor(tmp_path, "claude", "https://ghe.example.test/api/graphql")
    assert ("Builder sandbox network: github.com, api.github.com, api.linear.app, registry.npmjs.org, "
            "ghe.example.test, linear.invalid") in result.stdout, result.stdout + result.stderr


def test_doctor_does_not_call_an_ip_literal_ghes_invalid_for_a_codex_builder(tmp_path: Path) -> None:
    result = run_doctor(tmp_path, "codex", "https://10.0.0.5/api/graphql")
    assert "GitHub transport: rest" in result.stdout, result.stdout + result.stderr
    assert "invalid" not in result.stdout and "Builder sandbox network" not in result.stdout


def test_doctor_reports_an_ip_literal_ghes_invalid_for_a_claude_builder(tmp_path: Path) -> None:
    result = run_doctor(tmp_path, "claude", "https://10.0.0.5/api/graphql")
    assert "GitHub transport: invalid (the GitHub API URL must be" in result.stdout, result.stdout
    assert result.returncode == 1


def test_doctor_labels_a_linear_url_problem_as_linear_not_github(tmp_path: Path) -> None:
    result = run_doctor(tmp_path, "claude", "https://ghe.example.test/api/graphql", LOCAL_LINEAR_STUB)
    assert "GitHub transport: invalid" not in result.stdout, result.stdout
    assert "GitHub transport: rest" in result.stdout, result.stdout
    assert "Linear sandbox access: not added (the Linear API URL must be an https URL" in result.stdout, result.stdout
    # doctor is not a running process, and a local stub is a supported setup.
    assert "restart harbor" not in (result.stdout + result.stderr).lower(), result.stdout + result.stderr
    assert ("Builder sandbox network: github.com, api.github.com, api.linear.app, registry.npmjs.org, "
            "ghe.example.test\n") in result.stdout, result.stdout


# ------------------------------------------- github_api_url in the project registry
#
# Regression: the docs say the registry can set github_api_url, but the registry
# loader dropped the key, so a GitHub Enterprise host set only there was replaced
# by https://api.github.com without a word (and the token was sent there).

GHES_GRAPHQL = "https://ghe.example.test/api/graphql"


def registry_project(tmp_path: Path, monkeypatch, registry: dict[str, str], pipeline: str = ""):
    from harbor.config.projects import get_project
    from workflow_support import materialize

    repo = tmp_path / "repo"
    repo.mkdir()
    materialize(repo)
    pipeline_path = tmp_path / "pipeline.md"
    pipeline_path.write_text(f"# Pipeline\nlinear_project_slug: sample\nlinear_team: SAMPLE\n{pipeline}\n")
    projects = tmp_path / "projects.md"
    projects.write_text(
        "| name | repo_path | pipeline_path |" + "".join(f" {key} |" for key in registry) + "\n"
        + "| --- | --- | --- |" + " --- |" * len(registry) + "\n"
        + f"| SampleProject | {repo} | {pipeline_path} |" + "".join(f" {value} |" for value in registry.values()) + "\n"
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(projects))
    return get_project("SampleProject")


@pytest.mark.parametrize("key", ["github_api_url", "github_graphql_url", "github_endpoint"])
def test_a_registry_github_endpoint_reaches_the_builder(tmp_path: Path, monkeypatch, key: str) -> None:
    from harbor.config.profile import PipelineProfile
    config = registry_project(tmp_path, monkeypatch, {key: GHES_GRAPHQL})
    assert config.github_api_url == GHES_GRAPHQL
    assert PipelineProfile.from_project(config).github_api_url == GHES_GRAPHQL
    github = builder_github(config, workflow(), "claude", env={"GITHUB_TOKEN": TOKEN})
    assert github["env"]["HARBOR_GITHUB_API_URL"] == "https://ghe.example.test/api/v3"
    assert "ghe.example.test" in github["sandbox_hosts"]


def test_the_registry_github_endpoint_wins_over_the_pipeline_file(tmp_path: Path, monkeypatch) -> None:
    from harbor.config.profile import PipelineProfile
    config = registry_project(tmp_path, monkeypatch, {"github_api_url": GHES_GRAPHQL},
                              "github_api_url: https://api.github.com/graphql")
    assert PipelineProfile.from_project(config).github_api_url == GHES_GRAPHQL


def test_an_empty_registry_github_column_keeps_the_pipeline_value(tmp_path: Path, monkeypatch) -> None:
    from harbor.config.profile import PipelineProfile
    config = registry_project(tmp_path, monkeypatch, {"github_api_url": "", "github_endpoint": ""},
                              f"github_api_url: {GHES_GRAPHQL}")
    assert config.github_api_url == ""
    assert PipelineProfile.from_project(config).github_api_url == GHES_GRAPHQL


def test_an_empty_registry_github_column_falls_through_to_a_filled_alias(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch, {"github_api_url": "", "github_endpoint": GHES_GRAPHQL})
    assert config.github_api_url == GHES_GRAPHQL


def test_without_any_github_endpoint_the_default_is_github_com(tmp_path: Path, monkeypatch) -> None:
    from harbor.config.profile import PipelineProfile
    config = registry_project(tmp_path, monkeypatch, {})
    assert PipelineProfile.from_project(config).github_api_url == "https://api.github.com/graphql"


def test_a_malformed_registry_github_endpoint_fails_closed_for_a_claude_builder(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch, {"github_api_url": "https://*.example.test/api/graphql"})
    with pytest.raises(ValueError, match="GitHub API URL"):
        builder_github(config, workflow(), "claude", env={"GITHUB_TOKEN": TOKEN})


def test_the_production_factory_uses_a_registry_github_endpoint(tmp_path: Path, monkeypatch) -> None:
    domains = production_claude_domains(tmp_path, monkeypatch, github_api_url=GHES_GRAPHQL)
    assert domains == [*GITHUB_COM_DOMAINS, "ghe.example.test"]


def test_doctor_uses_a_github_endpoint_set_only_in_the_registry(tmp_path: Path) -> None:
    result = run_doctor(tmp_path, "claude", None, registry={"github_api_url": GHES_GRAPHQL})
    assert ("Builder sandbox network: github.com, api.github.com, api.linear.app, registry.npmjs.org, "
            "ghe.example.test, linear.invalid") in result.stdout, result.stdout + result.stderr


def test_doctor_fails_closed_on_a_malformed_registry_github_endpoint(tmp_path: Path) -> None:
    result = run_doctor(tmp_path, "claude", None, registry={"github_api_url": "https://10.0.0.5/api/graphql"})
    assert "GitHub transport: invalid (the GitHub API URL must be" in result.stdout, result.stdout
    assert result.returncode == 1


# ------------------------------------------- empty registry endpoint cells
#
# Regression: an empty linear_api_url registry cell was kept as "" and replaced
# the pipeline's Linear endpoint, so LinearClient posted to an empty URL.

LINEAR_CUSTOM = "https://linear.example.test/graphql"


def test_an_empty_registry_linear_cell_keeps_the_pipeline_value(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch, {"linear_api_url": "", "github_api_url": ""},
                              f"linear_api_url: {LINEAR_CUSTOM}")
    assert config.linear_api_url == LINEAR_CUSTOM


def test_an_empty_registry_linear_cell_falls_back_to_the_default(tmp_path: Path, monkeypatch) -> None:
    from harbor.clients.linear import DEFAULT_LINEAR_API_URL
    config = registry_project(tmp_path, monkeypatch, {"linear_api_url": "", "api_url": ""})
    assert config.linear_api_url == DEFAULT_LINEAR_API_URL


def test_an_empty_registry_linear_cell_falls_through_to_a_filled_alias(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch, {"linear_api_url": "", "api_url": LINEAR_CUSTOM},
                              "linear_api_url: https://other.example.test/graphql")
    assert config.linear_api_url == LINEAR_CUSTOM


def test_a_registry_linear_value_wins_over_the_pipeline(tmp_path: Path, monkeypatch) -> None:
    config = registry_project(tmp_path, monkeypatch, {"linear_api_url": LINEAR_CUSTOM},
                              "linear_api_url: https://other.example.test/graphql")
    assert config.linear_api_url == LINEAR_CUSTOM


def test_a_github_client_built_from_a_project_config_never_gets_an_empty_endpoint(tmp_path: Path, monkeypatch) -> None:
    from harbor.clients.github import GitHubClient
    config = registry_project(tmp_path, monkeypatch, {})
    assert config.github_api_url == ""
    assert GitHubClient.from_profile(config).endpoint == "https://api.github.com/graphql"
    (tmp_path / "ghes").mkdir()
    configured = registry_project(tmp_path / "ghes", monkeypatch, {"github_api_url": GHES_GRAPHQL})
    assert GitHubClient.from_profile(configured).endpoint == GHES_GRAPHQL
