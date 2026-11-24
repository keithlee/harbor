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


# --- Project and workflow-state lookups -------------------------------------
#
# A configured slug with a hex suffix (Linear's URL form) is resolved against
# ``list_projects``; ``harbor doctor`` and the ticket-scope check use the same
# lookup.  Workflow-state names are resolved against the team's states.  Both
# lists are Linear connections, so a first page alone is a partial answer.

TARGET = {"id": "p-target", "name": "SampleProject", "slugId": "aaa111aaa111"}


def project(number: int) -> dict:
    return {"id": f"p-{number}", "name": f"Filler {number}", "slugId": f"{number:012x}"}


def connection_transport(
    projects: list[list[dict]],
    calls: list[tuple[str, dict]],
    issues: list[dict] | None = None,
    *,
    cursor=lambda index: f"cursor-{index}",
) -> httpx.MockTransport:
    """Serve paged ``projects`` and a one-page issues result, keyed by query."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        variables = payload["variables"]
        if "HarborProjects" in payload["query"]:
            # Serve pages in request order so a stuck cursor still advances.
            index = sum(kind == "projects" for kind, _ in calls)
            calls.append(("projects", variables))
            body = {
                "nodes": projects[index],
                "pageInfo": {"hasNextPage": index + 1 < len(projects), "endCursor": cursor(index)},
            }
            return httpx.Response(200, json={"data": {"projects": body}})
        calls.append(("issues", variables))
        body = {"nodes": issues or [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        return httpx.Response(200, json={"data": {"issues": body}})

    return httpx.MockTransport(handler)


def list_projects(transport: httpx.MockTransport) -> list[dict]:
    async def scenario() -> list[dict]:
        client = LinearClient(token="test-key", transport=transport)
        try:
            return await client.list_projects()
        finally:
            await client.close()

    return asyncio.run(scenario())


def test_list_projects_follows_cursor_past_the_first_fifty() -> None:
    calls: list[tuple[str, dict]] = []
    filler = [project(number) for number in range(1, 51)]

    result = list_projects(connection_transport([filler, [TARGET]], calls))

    assert [kind for kind, _ in calls] == ["projects", "projects"]
    assert "after" not in calls[0][1]
    assert calls[1][1]["after"] == "cursor-0"
    assert len(result) == 51
    assert TARGET in result


def test_a_url_form_slug_resolves_when_its_project_is_on_page_two() -> None:
    """Before: the slug resolved only against the first 50 projects, so a
    workspace with more projects failed every poll with ValueError."""

    calls: list[tuple[str, dict]] = []
    filler = [project(number) for number in range(1, 51)]
    transport = connection_transport([filler, [TARGET]], calls, issues=[issue(1)])

    async def scenario() -> list[dict]:
        client = LinearClient(token="test-key", transport=transport)
        try:
            return await client.fetch_issues("sampleproject-aaa111aaa111", ["In Progress"])
        finally:
            await client.close()

    result = asyncio.run(scenario())

    assert [node["identifier"] for node in result] == ["SAMPLE-1"]
    issue_calls = [variables for kind, variables in calls if kind == "issues"]
    assert issue_calls[0]["slugs"] == ["aaa111aaa111"]


def test_doctor_scope_check_finds_a_project_on_page_two(tmp_path) -> None:
    from harbor.cli import _check_project_scope

    filler = [project(number) for number in range(1, 51)]
    config = ProjectConfig(
        name="Sample", linear_project_slug="sampleproject-aaa111aaa111", repo_path=tmp_path
    )

    async def scenario() -> bool:
        client = LinearClient(token="test-key", transport=connection_transport([filler, [TARGET]], []))
        try:
            return await _check_project_scope(client, config)
        finally:
            await client.close()

    assert asyncio.run(scenario()) is True


def test_list_projects_fails_loudly_past_the_page_bound() -> None:
    calls: list[tuple[str, dict]] = []
    pages = [[project(index)] for index in range(LINEAR_POLL_MAX_PAGES + 1)]

    with pytest.raises(LinearClientError, match="pagination limit"):
        list_projects(connection_transport(pages, calls))
    assert len(calls) == LINEAR_POLL_MAX_PAGES


@pytest.mark.parametrize("bad_cursor", ["same", None, "", 7])
def test_list_projects_rejects_a_repeated_missing_or_malformed_cursor(bad_cursor) -> None:
    pages = [[project(1)], [project(2)], [TARGET]]

    with pytest.raises(LinearClientError, match="pagination"):
        list_projects(connection_transport(pages, [], cursor=lambda index: bad_cursor))


def test_list_projects_rejects_a_malformed_connection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"projects": {"nodes": "oops"}}})

    with pytest.raises(LinearClientError, match="malformed"):
        list_projects(httpx.MockTransport(handler))


BLOCKED_ID = "6876f9e0-76aa-4dac-ac4f-fabeceb1d54f"


def state(number: int) -> dict:
    return {"id": f"00000000-0000-4000-8000-{number:012d}", "name": f"Stage {number}", "type": "started"}


def states_transport(pages: list[list[dict]], calls: list[dict], *, cursor=lambda index: f"cursor-{index}"):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "HarborStates" in payload["query"]:
            # Serve pages in request order so a stuck cursor still advances.
            index = sum("HarborStates" in call["query"] for call in calls)
            calls.append(payload)
            states = {
                "nodes": pages[index],
                "pageInfo": {"hasNextPage": index + 1 < len(pages), "endCursor": cursor(index)},
            }
            return httpx.Response(200, json={"data": {"issue": {"id": "u", "team": {
                "id": "t", "name": "SAMPLE", "states": states}}}})
        calls.append(payload)
        return httpx.Response(200, json={"data": {"issueUpdate": {"success": True, "issue": {
            "id": "u", "identifier": "SAMPLE-1", "state": {"id": BLOCKED_ID, "name": "Blocked", "type": "started"}}}}})

    return httpx.MockTransport(handler)


def set_state(transport: httpx.MockTransport, name: str) -> dict:
    async def scenario() -> dict:
        client = LinearClient(token="test-key", transport=transport)
        try:
            return await client.update_issue_state("SAMPLE-1", name)
        finally:
            await client.close()

    return asyncio.run(scenario())


def test_a_workflow_state_on_page_two_resolves() -> None:
    """Before: only the first 50 team states were read, so a state past them
    was reported as missing and the park/Human Review write failed."""

    calls: list[dict] = []
    filler = [state(number) for number in range(1, 51)]
    blocked = {"id": BLOCKED_ID, "name": "Blocked", "type": "started"}

    set_state(states_transport([filler, [blocked]], calls), "Blocked")

    lookups = [call for call in calls if "HarborStates" in call["query"]]
    assert len(lookups) == 2
    assert lookups[1]["variables"]["after"] == "cursor-0"
    assert calls[-1]["variables"]["stateId"] == BLOCKED_ID


def test_a_terminal_state_on_page_two_is_still_refused() -> None:
    filler = [state(number) for number in range(1, 51)]
    done_id = "125951a0-e488-4ffb-b525-16d42657d0b7"
    released = {"id": done_id, "name": "Released", "type": "completed"}

    with pytest.raises(ValueError, match="terminal"):
        set_state(states_transport([filler, [released]], []), done_id)


@pytest.mark.parametrize("bad_cursor", ["same", None])
def test_workflow_state_lookup_rejects_a_repeated_or_missing_cursor(bad_cursor) -> None:
    calls: list[dict] = []
    pages = [[state(1)], [state(2)], [{"id": BLOCKED_ID, "name": "Blocked", "type": "started"}]]

    with pytest.raises(LinearClientError, match="pagination"):
        set_state(states_transport(pages, calls, cursor=lambda index: bad_cursor), "Blocked")
    assert not any("issueUpdate" in call["query"] for call in calls), "no write after a partial lookup"


def test_workflow_state_lookup_fails_loudly_past_the_page_bound() -> None:
    calls: list[dict] = []
    pages = [[state(index)] for index in range(LINEAR_POLL_MAX_PAGES + 1)]

    with pytest.raises(LinearClientError, match="pagination limit"):
        set_state(states_transport(pages, calls), "Blocked")
    assert len(calls) == LINEAR_POLL_MAX_PAGES
