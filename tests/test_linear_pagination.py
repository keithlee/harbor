"""The Linear poll must return every matching issue, never a silent first page."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from harbor.clients.linear import LINEAR_POLL_MAX_PAGES, LinearClient, LinearClientError
from harbor.config.projects import ProjectConfig
from harbor.poll.linear_poller import LinearPoller


def issue(number: int) -> dict:
    identifier = f"SAMPLE-{number}"
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title {identifier}",
        "url": f"https://linear.app/sample/issue/{identifier}",
        "updatedAt": "2026-09-02T20:00:00Z",
        "branchName": None,
        "state": {"name": "In Progress", "type": "started"},
        "attachments": {"nodes": []},
        "comments": {"nodes": []},
        "inverseRelations": {"nodes": []},
    }


def paged_transport(pages: list[list[dict]], calls: list[dict]) -> httpx.MockTransport:
    """Serve ``pages`` the way Linear does: cursor in, ``pageInfo`` out."""

    def handler(request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        calls.append(variables)
        after = variables.get("after")
        index = 0 if after is None else int(after.removeprefix("cursor-")) + 1
        has_next = index + 1 < len(pages)
        body = {
            "nodes": pages[index],
            "pageInfo": {"hasNextPage": has_next, "endCursor": f"cursor-{index}"},
        }
        return httpx.Response(200, json={"data": {"issues": body}})

    return httpx.MockTransport(handler)


def fetch(transport: httpx.MockTransport) -> list[dict]:
    async def scenario() -> list[dict]:
        client = LinearClient(token="test-key", transport=transport)
        try:
            return await client.fetch_issues("sample", ["In Progress"])
        finally:
            await client.close()

    return asyncio.run(scenario())


def test_fetch_issues_follows_cursor_past_the_first_fifty() -> None:
    nodes = [issue(number) for number in range(1, 76)]
    calls: list[dict] = []

    result = fetch(paged_transport([nodes[:50], nodes[50:]], calls))

    assert len(calls) == 2
    assert "after" not in calls[0]
    assert calls[1]["after"] == "cursor-0"
    assert calls[1]["slugs"] == ["sample"] and calls[1]["states"] == ["In Progress"]
    assert {node["identifier"] for node in result} == {f"SAMPLE-{n}" for n in range(1, 76)}
    assert len(result) == 75


def test_poll_snapshot_is_independent_of_page_boundaries(tmp_path) -> None:
    nodes = [issue(number) for number in range(1, 76)]
    config = ProjectConfig(name="Sample", linear_project_slug="sample", repo_path=tmp_path)

    def poll(pages: list[list[dict]]):
        client = LinearClient(token="test-key", transport=paged_transport(pages, []))
        poller = LinearPoller(client, config)

        async def scenario():
            try:
                return await poller.poll()
            finally:
                await client.close()

        return asyncio.run(scenario())

    split = poll([nodes[:50], nodes[50:]])
    reshuffled = poll([list(reversed(nodes[25:])), list(reversed(nodes[:25]))])

    assert len(split.tickets) == 75
    assert split.snapshot_hash == reshuffled.snapshot_hash


def test_fetch_issues_fails_loudly_past_the_page_bound() -> None:
    calls: list[dict] = []
    pages = [[issue(index)] for index in range(LINEAR_POLL_MAX_PAGES + 1)]

    with pytest.raises(LinearClientError, match="pagination limit"):
        fetch(paged_transport(pages, calls))
    assert len(calls) == LINEAR_POLL_MAX_PAGES


def test_fetch_issues_rejects_a_repeated_or_missing_cursor() -> None:
    def stuck(request: httpx.Request) -> httpx.Response:
        body = {"nodes": [issue(1)], "pageInfo": {"hasNextPage": True, "endCursor": "same"}}
        return httpx.Response(200, json={"data": {"issues": body}})

    def missing(request: httpx.Request) -> httpx.Response:
        body = {"nodes": [issue(1)], "pageInfo": {"hasNextPage": True, "endCursor": None}}
        return httpx.Response(200, json={"data": {"issues": body}})

    with pytest.raises(LinearClientError, match="pagination"):
        fetch(httpx.MockTransport(stuck))
    with pytest.raises(LinearClientError, match="pagination"):
        fetch(httpx.MockTransport(missing))


def test_supervisor_marks_linear_unhealthy_when_poll_exceeds_bound(tmp_path) -> None:
    from harbor.db import HarborDB
    from harbor.supervisor import Supervisor

    pages = [[issue(index)] for index in range(LINEAR_POLL_MAX_PAGES + 1)]
    config = ProjectConfig(name="Sample", linear_project_slug="sample", repo_path=tmp_path)
    db = HarborDB(tmp_path / "harbor.db")
    client = LinearClient(token="test-key", transport=paged_transport(pages, []))
    supervisor = Supervisor(LinearPoller(client, config, db=db), db=db, shadow=True)
    try:
        result = asyncio.run(supervisor.poll_once())
        status = supervisor.status()
    finally:
        asyncio.run(client.close())
        db.close()

    # No truncated snapshot is published: the cycle reports Linear unhealthy.
    assert result is None
    assert status.linear_healthy is False
    assert "pagination limit" in (supervisor.last_error or "")
