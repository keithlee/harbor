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
