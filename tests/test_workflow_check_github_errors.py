"""`harbor workflow check` fails closed with one line and exit 2 when GitHub fails.

Runs the real CLI entry point and the production default GitHubClient path;
only the network layer under ``httpx.AsyncClient`` is a ``MockTransport``.
"""
from types import SimpleNamespace

import httpx
import pytest

import harbor.cli as cli
import harbor.clients.github as github_module
from harbor.db import HarborDB
from harbor.wake import apply_phase3_migration
from workflow_support import config

TOKEN = "dummy-test-token-not-a-secret"
PR_URL = "https://github.com/example/repo/pull/1"


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


def _unreachable(request):
    raise httpx.ConnectError("All connection attempts failed", request=request)


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(200, text="<html>proxy login</html>"),
        lambda request: httpx.Response(502, text="bad gateway"),
        _unreachable,
    ],
    ids=["non-json-body", "http-502", "unreachable"],
)
def test_workflow_check_github_failure_is_one_line_exit_2(tmp_path, monkeypatch, capsys, handler) -> None:
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        assert "transport" not in kwargs  # the production default client path
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(github_module.httpx, "AsyncClient", factory)
    monkeypatch.setattr("harbor.clients.linear.LinearClient", Linear)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text("```yaml\nrepo: example/repo\n```\n", encoding="utf-8")
    cfg = SimpleNamespace(
        name="A",
        linear_api_url="https://linear.invalid",
        linear_project_scope=("canonical",),
        github_repository="example/repo",
        required_checks=("tests",),
        pipeline_path=str(pipeline),
        workflow_path=config(tmp_path, "autonomous"),
    )
    db = HarborDB(":memory:")
    apply_phase3_migration(db)
    db.replace_tickets("A", [{"identifier": "A-1", "state": "In Progress", "owned": True, "hold": False}])
    with db.connection:
        db.connection.execute(
            "INSERT INTO runs (id, ticket, worktree, status) VALUES (?, ?, ?, ?)",
            ("run", "A-1", str(tmp_path), "running"),
        )
    monkeypatch.setattr(cli, "HarborDB", lambda path: db)
    monkeypatch.setattr(cli, "get_project", lambda name: cfg)

    exit_code = cli.main(["workflow", "check", "A", "--ticket", "A-1", "--event", "pre_merge"])

    err = capsys.readouterr().err
    assert exit_code == 2
    lines = err.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("Harbor: GitHub "), err
    assert "Traceback" not in err
    assert TOKEN not in err
