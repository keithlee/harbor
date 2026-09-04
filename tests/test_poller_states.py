from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from harbor.clients.linear import LINEAR_QUERY, LinearClient
from harbor.config.projects import ProjectConfig, parse_pipeline_markdown
from harbor.db import HarborDB
from harbor.poll.linear_poller import LinearPoller


def issue(identifier: str, state: str, *, title: str | None = None) -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": title or f"Title {identifier}",
        "url": f"https://linear.app/sample-project/issue/{identifier}",
        "updatedAt": "2026-09-02T20:00:00Z",
        "branchName": None,
        "state": {"name": state, "type": "started" if state != "Todo" else "backlog"},
        "attachments": {"nodes": [{"url": "https://github.com/example/SampleProject/pull/221", "sourceType": "github"}]},
        "comments": {"nodes": [{"id": f"comment-{identifier}", "createdAt": "2026-09-02T20:00:00Z", "body": "hello"}]},
        "inverseRelations": {"nodes": []},
    }


def fake_transport(nodes: list[dict], calls: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        assert "issues(filter:" in payload["query"]
        return httpx.Response(200, json={"data": {"issues": {"nodes": nodes}}})

    return httpx.MockTransport(handler)


def run(coro):
    return asyncio.run(coro)


def config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        name="SampleProject",
        linear_project_slug="sample-project",
        repo_path=tmp_path,
        poll_interval_seconds=3600,
    )


def test_poller_sends_only_configured_states_and_excludes_backlog(tmp_path: Path) -> None:
    nodes = [issue("SAMPLE-42", "Todo"), issue("SAMPLE-17", "In Review"), issue("SAMPLE-99", "Backlog")]
    calls: list[dict] = []
    client = LinearClient(token="test-key", transport=fake_transport(nodes, calls))
    poller = LinearPoller(client, config(tmp_path))
    try:
        result = run(poller.poll())
    finally:
        run(client.close())
    assert [ticket["identifier"] for ticket in result.tickets] == ["SAMPLE-17", "SAMPLE-42"]
    assert "Backlog" not in {ticket["state"] for ticket in result.tickets}
    assert calls[0]["variables"] == {
        "slugs": ["sample-project"],
        "states": ["Todo", "In Progress", "In Review", "Blocked", "Human Review"],
    }
    assert calls[0]["query"].strip().startswith("query HarborIssues")


def test_unchanged_snapshot_creates_no_second_events_row(tmp_path: Path) -> None:
    nodes = [issue("SAMPLE-42", "Todo")]
    calls: list[dict] = []
    db = HarborDB(tmp_path / "harbor.db")
    client = LinearClient(transport=fake_transport(nodes, calls))
    poller = LinearPoller(client, config(tmp_path), db=db)
    try:
        first, second = run(poller.poll()), run(poller.poll())
    finally:
        run(client.close())
        db.close()
    assert first.changed is True
    assert second.changed is False
    assert len(first.events) == 1
    assert second.events == ()
    reopened = HarborDB(tmp_path / "harbor.db")
    try:
        assert reopened.event_count("SampleProject") == 1
    finally:
        reopened.close()


def test_pipeline_scope_values_are_loaded_without_backlog(tmp_path: Path) -> None:
    values = parse_pipeline_markdown(
        """
        dispatch_states: [Todo]
        poll_states: [Todo, In Progress, In Review, Blocked, Human Review, Backlog]
        hold_all: false # shadow mode can be disabled explicitly
        """
    )
    assert values["dispatch_states"] == ("Todo",)
    assert values["hold_all"] is False
    # Config may describe the board, but the poller's fail-closed filter owns
    # the invariant that Backlog is never queried or displayed.
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", repo_path=tmp_path, **values)
    assert "Backlog" in config.poll_states


def test_additional_projects_widen_the_same_single_request(tmp_path: Path) -> None:
    """Adding a project is a config edit, not a code change, and stays 1 call."""

    nodes = [issue("SAMPLE-42", "Todo")]
    calls: list[dict] = []
    client = LinearClient(token="test-key", transport=fake_transport(nodes, calls))
    scoped = ProjectConfig(
        name="SampleProject",
        linear_project_slug="sample-project",
        linear_projects=("second-project", "third-project"),
        repo_path=tmp_path,
        poll_interval_seconds=3600,
    )
    poller = LinearPoller(client, scoped)
    try:
        run(poller.poll())
    finally:
        run(client.close())
    assert len(calls) == 1, "extra projects must not cost extra requests"
    assert calls[0]["variables"]["slugs"] == [
        "sample-project",
        "second-project",
        "third-project",
    ]


def test_a_duplicate_or_blank_project_slug_is_ignored(tmp_path: Path) -> None:
    scoped = ProjectConfig(
        name="SampleProject",
        linear_project_slug="sample-project",
        linear_projects=("sample-project", "  ", "other"),
        repo_path=tmp_path,
    )
    assert scoped.linear_project_scope == ("sample-project", "other")


def test_projects_may_be_configured_as_a_markdown_list_literal(tmp_path: Path) -> None:
    from harbor.config.projects import _split_list

    assert _split_list("[sample-project, second-project]") == ("sample-project", "second-project")
