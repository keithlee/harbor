"""`harbor workflow check` must send GitHub requests to the configured host.

A GitHub Enterprise project's token must never be sent to api.github.com.
Every request here goes through ``httpx.MockTransport``; nothing touches the
network and the token is a dummy value.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

import harbor.clients.github as github_module
from harbor.cli import build_parser
from harbor.db import HarborDB
from harbor.wake import apply_phase3_migration
from harbor.workflow_cli import execute
from workflow_support import config

# Harbor's PR-link matcher only recognises github.com-form links today, so the
# Linear attachment uses that form; the GraphQL endpoint is what is under test.
PR_URL = "https://github.com/example/repo/pull/1"
DUMMY_TOKEN = "dummy-test-token-not-a-secret"


class Linear:
    def __init__(self, **kwargs):
        pass

    async def fetch_blocker_context(self, ticket):
        return {
            "identifier": ticket,
            "project": {"slugId": "canonical"},
            "description": "Risk: low",
            "state": {"name": "In Progress", "type": "started"},
            "attachments": {"nodes": [{"sourceType": "github", "url": PR_URL}]},
        }

    async def close(self):
        pass


def _run_workflow_check(tmp_path, monkeypatch, pipeline_text):
    """Run `harbor workflow check A --ticket A-1 --event pre_merge`; return captured requests."""

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"data": {"repository": {"pr_1": {
            "number": 1,
            "headRefOid": "abc",
            "url": PR_URL,
            "merged": False,
            "mergeable": "MERGEABLE",
            "statusCheckRollup": {"contexts": {"nodes": [{"name": "tests", "conclusion": "SUCCESS"}]}},
        }}}})

    real_client = github_module.GitHubClient

    class MockTransportGitHubClient(real_client):
        """The real client, with only its transport swapped for a mock."""

        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("harbor.clients.linear.LinearClient", Linear)
    monkeypatch.setattr("harbor.clients.github.GitHubClient", MockTransportGitHubClient)
    monkeypatch.setenv("GITHUB_TOKEN", DUMMY_TOKEN)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text(pipeline_text, encoding="utf-8")
    cfg = SimpleNamespace(
        name="A",
        linear_api_url="https://linear.invalid",
        linear_project_scope=("canonical",),
        github_repository="example/repo",
        required_checks=("tests",),
        pipeline_path=str(pipeline),
        workflow_path=config(tmp_path, "autonomous"),
    )
    args = build_parser().parse_args(["workflow", "check", "A", "--ticket", "A-1", "--event", "pre_merge"])
    db = HarborDB(":memory:")
    try:
        apply_phase3_migration(db)
        db.replace_tickets("A", [{"identifier": "A-1", "state": "In Progress", "owned": True, "hold": False}])
        with db.connection:
            db.connection.execute(
                "INSERT INTO runs (id, ticket, worktree, status) VALUES (?, ?, ?, ?)",
                ("run", "A-1", str(tmp_path), "running"),
            )
        exit_code = asyncio.run(execute(args, cfg, db))
    finally:
        db.close()
    return sent, exit_code


def test_workflow_check_sends_github_token_only_to_configured_enterprise_host(tmp_path, monkeypatch, capsys):
    sent, exit_code = _run_workflow_check(
        tmp_path,
        monkeypatch,
        "```yaml\nrepo: example/repo\ngithub_api_url: https://ghe.example.com/api/graphql\n```\n",
    )

    assert sent, "workflow check must have queried GitHub"
    hosts = {request.url.host for request in sent}
    assert hosts == {"ghe.example.com"}, f"GitHub requests went to {sorted(hosts)}"
    assert "api.github.com" not in hosts
    for request in sent:
        assert str(request.url) == "https://ghe.example.com/api/graphql"
        assert request.headers["Authorization"] == f"bearer {DUMMY_TOKEN}"
    output = json.loads(capsys.readouterr().out)
    assert output["head_sha"] == "abc"
    assert exit_code == 0, output


def test_workflow_check_defaults_to_github_dot_com_without_configured_host(tmp_path, monkeypatch, capsys):
    sent, _ = _run_workflow_check(tmp_path, monkeypatch, "```yaml\nrepo: example/repo\n```\n")

    assert sent
    assert {str(request.url) for request in sent} == {"https://api.github.com/graphql"}
    assert json.loads(capsys.readouterr().out)["head_sha"] == "abc"


# --- Unsafe endpoints fail closed: no request, a clear error, non-zero exit ---

UNSAFE_ENDPOINTS = pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("http://ghe.example.com/api/graphql", "the GitHub API URL must use https"),
        ("ghe.example.com/api/graphql", "the GitHub API URL must use https"),
    ],
    ids=["plain-http", "scheme-less"],
)


def _recording_github_client(monkeypatch, target):
    """Patch ``target`` with the real client on a mock transport; return the request log."""

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"data": {"repository": {}}})

    real_client = github_module.GitHubClient

    class MockTransportGitHubClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(target, MockTransportGitHubClient)
    monkeypatch.setenv("GITHUB_TOKEN", DUMMY_TOKEN)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    return sent


def _pipeline(tmp_path, endpoint):
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text(f"```yaml\nrepo: example/repo\ngithub_api_url: {endpoint}\n```\n", encoding="utf-8")
    return pipeline


@UNSAFE_ENDPOINTS
def test_workflow_check_refuses_unsafe_github_endpoint_without_a_request(
    tmp_path, monkeypatch, capsys, endpoint, message
):
    import harbor.cli as cli

    sent = _recording_github_client(monkeypatch, "harbor.clients.github.GitHubClient")
    monkeypatch.setattr("harbor.clients.linear.LinearClient", Linear)
    cfg = SimpleNamespace(
        name="A",
        linear_api_url="https://linear.invalid",
        linear_project_scope=("canonical",),
        github_repository="example/repo",
        required_checks=("tests",),
        pipeline_path=str(_pipeline(tmp_path, endpoint)),
        workflow_path=config(tmp_path, "autonomous"),
    )
    db_path = tmp_path / "harbor.db"
    db = HarborDB(db_path)
    try:
        apply_phase3_migration(db)
        db.replace_tickets("A", [{"identifier": "A-1", "state": "In Progress", "owned": True, "hold": False}])
        with db.connection:
            db.connection.execute(
                "INSERT INTO runs (id, ticket, worktree, status) VALUES (?, ?, ?, ?)",
                ("run", "A-1", str(tmp_path), "running"),
            )
    finally:
        db.close()
    monkeypatch.setattr(cli, "get_project", lambda _: cfg)
    monkeypatch.setattr(cli, "_db_path", lambda: db_path)

    args = build_parser().parse_args(["workflow", "check", "A", "--ticket", "A-1", "--event", "pre_merge"])
    exit_code = asyncio.run(cli._async_main(args))

    assert sent == [], f"a request was sent to {[str(r.url) for r in sent]}"
    assert exit_code == 2
    assert f"Harbor: {message}" in capsys.readouterr().err


@UNSAFE_ENDPOINTS
def test_facts_refuses_unsafe_github_endpoint_without_a_request(tmp_path, monkeypatch, capsys, endpoint, message):
    import harbor.cli as cli
    from harbor.config.projects import ProjectConfig

    sent = _recording_github_client(monkeypatch, "harbor.cli.GitHubClient")
    node = {
        "id": "linear-id-1",
        "identifier": "A-1",
        "title": "A ticket",
        "state": {"name": "In Progress", "type": "started"},
        "attachments": {"nodes": [{"sourceType": "github", "url": PR_URL}]},
        "comments": {"nodes": []},
        "inverseRelations": {"nodes": []},
    }

    class FactsLinear:
        def __init__(self, **kwargs):
            pass

        async def fetch_issues(self, projects, states):
            return [node]

        async def close(self):
            pass

    monkeypatch.setattr(cli, "LinearClient", FactsLinear)
    cfg = ProjectConfig(
        name="A",
        linear_project_slug="a",
        repo_path=tmp_path,
        pipeline_path=_pipeline(tmp_path, endpoint),
    )
    monkeypatch.setattr(cli, "get_project", lambda _: cfg)
    monkeypatch.setattr(cli, "_db_path", lambda: tmp_path / "harbor.db")

    exit_code = asyncio.run(cli._async_main(build_parser().parse_args(["facts", "A"])))

    assert sent == [], f"a request was sent to {[str(r.url) for r in sent]}"
    assert exit_code == 2
    assert f"Harbor: {message}" in capsys.readouterr().err


def test_facts_queries_the_configured_https_endpoint(tmp_path, monkeypatch, capsys):
    """Control for the refusal tests: a valid https endpoint is queried, with the token."""
    import harbor.cli as cli
    from harbor.config.projects import ProjectConfig

    sent = _recording_github_client(monkeypatch, "harbor.cli.GitHubClient")
    node = {
        "id": "linear-id-1",
        "identifier": "A-1",
        "title": "A ticket",
        "state": {"name": "In Progress", "type": "started"},
        "attachments": {"nodes": [{"sourceType": "github", "url": PR_URL}]},
        "comments": {"nodes": []},
        "inverseRelations": {"nodes": []},
    }

    class FactsLinear:
        def __init__(self, **kwargs):
            pass

        async def fetch_issues(self, projects, states):
            return [node]

        async def close(self):
            pass

    monkeypatch.setattr(cli, "LinearClient", FactsLinear)
    cfg = ProjectConfig(
        name="A",
        linear_project_slug="a",
        repo_path=tmp_path,
        pipeline_path=_pipeline(tmp_path, "https://ghe.example.com/api/graphql"),
    )
    monkeypatch.setattr(cli, "get_project", lambda _: cfg)
    monkeypatch.setattr(cli, "_db_path", lambda: tmp_path / "harbor.db")

    assert asyncio.run(cli._async_main(build_parser().parse_args(["facts", "A"]))) == 0
    assert sent and {str(r.url) for r in sent} == {"https://ghe.example.com/api/graphql"}
    assert all(r.headers["Authorization"] == f"bearer {DUMMY_TOKEN}" for r in sent)


@UNSAFE_ENDPOINTS
def test_shadow_start_refuses_unsafe_github_endpoint_without_a_request(
    tmp_path, monkeypatch, capsys, endpoint, message
):
    """Shadow/read-only starts skip the builder checks, so the poll client must check itself."""
    import harbor.cli as cli
    from harbor.config.projects import ProjectConfig

    sent = _recording_github_client(monkeypatch, "harbor.cli.GitHubClient")

    class FactsLinear:
        async def fetch_issues(self, projects, states):
            return [{
                "id": "linear-id-1",
                "identifier": "A-1",
                "title": "A ticket",
                "state": {"name": "In Progress", "type": "started"},
                "attachments": {"nodes": [{"sourceType": "github", "url": PR_URL}]},
                "comments": {"nodes": []},
                "inverseRelations": {"nodes": []},
            }]

        async def close(self):
            pass

    cfg = ProjectConfig(
        name="A",
        linear_project_slug="a",
        repo_path=tmp_path,
        pipeline_path=_pipeline(tmp_path, endpoint),
    )
    monkeypatch.setattr(cli, "get_project", lambda _: cfg)
    monkeypatch.setattr(cli, "_db_path", lambda: tmp_path / "harbor.db")

    exit_code = asyncio.run(cli._start(
        "A",
        once=True,
        shadow=True,
        read_only=True,
        linear_client=FactsLinear(),
        first_mate=object(),
        notifier=object(),
        unblock_ladder=object(),
    ))

    assert sent == [], f"a request was sent to {[str(r.url) for r in sent]}"
    assert exit_code == 2
    assert f"Harbor: {message}" in capsys.readouterr().err
