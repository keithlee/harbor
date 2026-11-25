"""Linear workflow-state writes (plan section 1.4, section 7 AC-7/AC-9).

`issueUpdate` takes a workflow state UUID, never a name.  Harbor's config and
the plan both speak in names ("Blocked", "Human Review"), so passing a name
straight through produced Linear's opaque "Argument Validation Error" and the
park path could never work without hand-configured UUIDs.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from harbor.clients.linear import LinearClient, LinearClientError

HUMAN_REVIEW = "c845aec8-ce2d-4d6b-971c-566efe72167a"
BLOCKED = "6876f9e0-76aa-4dac-ac4f-fabeceb1d54f"
RELEASED = "125951a0-e488-4ffb-b525-16d42657d0b7"


def transport(calls: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "HarborStates" in body["query"]:
            return httpx.Response(200, json={"data": {"issue": {"id": "u", "team": {
                "id": "t", "name": "HAN", "states": {"nodes": [
                    {"id": HUMAN_REVIEW, "name": "Human Review", "type": "started"},
                    {"id": BLOCKED, "name": "Blocked", "type": "started"},
                    {"id": RELEASED, "name": "Released", "type": "completed"},
                ]}}}}})
        return httpx.Response(200, json={"data": {"issueUpdate": {"success": True, "issue": {
            "id": "u", "identifier": "SAMPLE-32", "state": {"id": HUMAN_REVIEW, "name": "Human Review", "type": "started"}}}}})

    return httpx.MockTransport(handler)


def test_a_state_name_is_resolved_to_its_id_before_the_mutation() -> None:
    async def scenario() -> None:
        calls: list[dict[str, Any]] = []
        client = LinearClient(token="k", transport=transport(calls))
        try:
            await client.update_issue_state("SAMPLE-32", "Human Review")
        finally:
            await client.close()
        assert "HarborStates" in calls[0]["query"], "the name must be resolved first"
        assert calls[1]["variables"]["stateId"] == HUMAN_REVIEW, "a name must never be sent as stateId"

    asyncio.run(scenario())


def test_a_uuid_is_validated_before_the_mutation() -> None:
    async def scenario() -> None:
        calls: list[dict[str, Any]] = []
        client = LinearClient(token="k", transport=transport(calls))
        try:
            await client.update_issue_state("SAMPLE-32", BLOCKED)
        finally:
            await client.close()
        assert "HarborStates" in calls[0]["query"], "a UUID must be type-checked first"
        assert calls[1]["variables"]["stateId"] == BLOCKED

    asyncio.run(scenario())


def test_the_resolved_id_is_cached_across_calls() -> None:
    async def scenario() -> None:
        calls: list[dict[str, Any]] = []
        client = LinearClient(token="k", transport=transport(calls))
        try:
            await client.update_issue_state("SAMPLE-32", "Human Review")
            await client.update_issue_state("SAMPLE-32", "Blocked")
        finally:
            await client.close()
        assert sum("HarborStates" in c["query"] for c in calls) == 1, "one lookup populates every state"

    asyncio.run(scenario())


def test_an_unknown_state_name_fails_loudly() -> None:
    async def scenario() -> None:
        client = LinearClient(token="k", transport=transport([]))
        try:
            with pytest.raises(LinearClientError, match="no Linear workflow state"):
                await client.update_issue_state("SAMPLE-32", "Not A State")
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["Done", "done", "Completed", "Canceled", "Duplicate done"])
def test_harbor_never_writes_a_terminal_state(state: str) -> None:
    """The builder's skill owns the Done transition; Harbor never writes it."""

    async def scenario() -> None:
        calls: list[dict[str, Any]] = []
        client = LinearClient(token="k", transport=transport(calls))
        try:
            with pytest.raises(ValueError, match="terminal"):
                await client.update_issue_state("SAMPLE-32", state)
        finally:
            await client.close()
        assert calls == [], "a terminal state must be refused before any network call"

    asyncio.run(scenario())


@pytest.mark.parametrize("state", [RELEASED, "Released"])
def test_harbor_rejects_terminal_state_ids_and_custom_labels(state: str) -> None:
    """A UUID or a benign-looking label must not bypass terminal-state custody."""

    async def scenario() -> None:
        calls: list[dict[str, Any]] = []
        client = LinearClient(token="k", transport=transport(calls))
        try:
            with pytest.raises(ValueError, match="terminal"):
                await client.update_issue_state("ABC-32", state)
        finally:
            await client.close()
        assert len(calls) == 1
        assert "HarborStates" in calls[0]["query"]

    asyncio.run(scenario())


def comment_transport(posts: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"commentCreate": {
            "success": True, "comment": {"id": f"c{len(posts)}"}}}})

    return httpx.MockTransport(handler)


def test_an_identical_comment_body_is_posted_only_once() -> None:
    """Plan section 4: dedupe on (issue_id, sha256(body)) BEFORE the POST.

    SAMPLE-7 already carries two duplicate "Applied... PR #217..." comments a
    minute apart, and a duplicate Linear comment is an explicit abort
    condition for the acceptance run.
    """

    from harbor.db import HarborDB

    async def scenario() -> None:
        posts: list[dict[str, Any]] = []
        db = HarborDB(":memory:")
        client = LinearClient(token="k", transport=comment_transport(posts))
        body = "[harbor] Blocked SAMPLE-32 after deterministic remediation and rung-1 triage."
        try:
            await client.create_comment_once("u-32", body, db=db)
            await client.create_comment_once("u-32", body, db=db)
            await client.create_comment_once("u-32", body + " (updated)", db=db)
            # A different issue with the same body is a different receipt.
            await client.create_comment_once("u-99", body, db=db)
        finally:
            await client.close()
            db.close()
        bodies = [p["variables"]["body"] for p in posts]
        assert bodies.count(body) == 2, "once per issue, never twice for the same issue"
        assert len(posts) == 3

    asyncio.run(scenario())


def test_dedupe_depends_on_the_receipt_ledger_surviving() -> None:
    """A fresh ledger cannot suppress a repeat: the receipts ARE the guard.

    Wiping harbor.db between runs re-posts every prior comment, which is how a
    duplicate reaches the board even though the dedupe itself is correct.
    """

    from harbor.db import HarborDB

    async def scenario() -> None:
        posts: list[dict[str, Any]] = []
        body = "[harbor] parked"
        for _ in range(2):
            db = HarborDB(":memory:")  # a new ledger each time, as `rm harbor.db` would
            client = LinearClient(token="k", transport=comment_transport(posts))
            try:
                await client.create_comment_once("u-32", body, db=db)
            finally:
                await client.close()
                db.close()
        assert len(posts) == 2, "this documents the failure mode, it is not desired behavior"

    asyncio.run(scenario())


# Two Linear teams with the same workflow-state names but distinct ids.  Each
# team owns its states, so "Blocked" for a TEAMB issue must resolve to TEAMB's
# id even after TEAMA's states are already cached.
TEAM_A_BLOCKED = "aaaaaaaa-0000-4000-8000-00000000000b"
TEAM_A_REVIEW = "aaaaaaaa-0000-4000-8000-000000000001"
TEAM_A_RELEASED = "aaaaaaaa-0000-4000-8000-00000000000d"
TEAM_B_BLOCKED = "bbbbbbbb-0000-4000-8000-00000000000b"
TEAM_B_REVIEW = "bbbbbbbb-0000-4000-8000-000000000001"
TEAM_B_RELEASED = "bbbbbbbb-0000-4000-8000-00000000000d"


def two_team_transport(calls: list[dict[str, Any]]) -> httpx.MockTransport:
    teams = {
        "A": ("team-a", [
            {"id": TEAM_A_BLOCKED, "name": "Blocked", "type": "started"},
            {"id": TEAM_A_REVIEW, "name": "Human Review", "type": "started"},
            {"id": TEAM_A_RELEASED, "name": "Released", "type": "completed"},
        ]),
        "B": ("team-b", [
            {"id": TEAM_B_BLOCKED, "name": "Blocked", "type": "started"},
            {"id": TEAM_B_REVIEW, "name": "Human Review", "type": "started"},
            {"id": TEAM_B_RELEASED, "name": "Released", "type": "completed"},
        ]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        issue = body["variables"]["issueId"]
        team_id, nodes = teams[issue.split("-")[0][-1]]
        if "HarborStates" in body["query"]:
            return httpx.Response(200, json={"data": {"issue": {"id": issue, "team": {
                "id": team_id, "name": team_id, "states": {"nodes": nodes}}}}})
        state_id = body["variables"]["stateId"]
        if state_id not in {node["id"] for node in nodes}:
            # Linear refuses a state that belongs to another team.
            return httpx.Response(200, json={"errors": [{"message": "Argument Validation Error"}]})
        return httpx.Response(200, json={"data": {"issueUpdate": {"success": True, "issue": {
            "id": issue, "identifier": issue, "state": {"id": state_id, "name": "Blocked", "type": "started"}}}}})

    return httpx.MockTransport(handler)


def test_a_state_name_resolves_against_the_issues_own_team_after_another_teams_cache_is_warm() -> None:
    async def scenario() -> None:
        calls: list[dict[str, Any]] = []
        client = LinearClient(token="k", transport=two_team_transport(calls))
        try:
            await client.update_issue_state("TEAMA-1", "Blocked")
            await client.update_issue_state("TEAMB-7", "Blocked")
        finally:
            await client.close()
        mutations = [c["variables"]["stateId"] for c in calls if "HarborProgress" in c["query"]]
        assert mutations == [TEAM_A_BLOCKED, TEAM_B_BLOCKED], "each issue must get its own team's state id"

    asyncio.run(scenario())


def test_another_teams_state_id_is_never_validated_from_the_cache() -> None:
    """A UUID that belongs to team A is not a workflow state of a team-B issue."""

    async def scenario() -> None:
        calls: list[dict[str, Any]] = []
        client = LinearClient(token="k", transport=two_team_transport(calls))
        try:
            await client.update_issue_state("TEAMA-1", "Blocked")
            with pytest.raises(LinearClientError, match="no Linear workflow state id"):
                await client.update_issue_state("TEAMB-7", TEAM_A_BLOCKED)
        finally:
            await client.close()
        assert [c["variables"]["stateId"] for c in calls if "HarborProgress" in c["query"]] == [TEAM_A_BLOCKED]

    asyncio.run(scenario())


def test_a_terminal_label_is_refused_per_team_after_another_teams_cache_is_warm() -> None:
    async def scenario() -> None:
        calls: list[dict[str, Any]] = []
        client = LinearClient(token="k", transport=two_team_transport(calls))
        try:
            await client.update_issue_state("TEAMA-1", "Blocked")
            with pytest.raises(ValueError, match="terminal"):
                await client.update_issue_state("TEAMB-7", "Released")
        finally:
            await client.close()
        assert sum("HarborProgress" in c["query"] for c in calls) == 1

    asyncio.run(scenario())


def test_one_lookup_serves_every_state_of_the_same_issue() -> None:
    async def scenario() -> None:
        calls: list[dict[str, Any]] = []
        client = LinearClient(token="k", transport=two_team_transport(calls))
        try:
            await client.update_issue_state("TEAMB-7", "Human Review")
            await client.update_issue_state("TEAMB-7", "Blocked")
            await client.update_issue_state("TEAMA-1", "Blocked")
            await client.update_issue_state("TEAMA-1", "Human Review")
        finally:
            await client.close()
        assert sum("HarborStates" in c["query"] for c in calls) == 2, "one lookup per issue's team"

    asyncio.run(scenario())


def test_an_unknown_name_for_a_new_issue_costs_one_lookup_and_fails_closed() -> None:
    async def scenario() -> None:
        calls: list[dict[str, Any]] = []
        client = LinearClient(token="k", transport=two_team_transport(calls))
        try:
            await client.update_issue_state("TEAMA-1", "Blocked")
            with pytest.raises(LinearClientError, match="no Linear workflow state named"):
                await client.update_issue_state("TEAMB-7", "Not A State")
        finally:
            await client.close()
        assert sum("HarborStates" in c["query"] for c in calls) == 2
        assert sum("HarborProgress" in c["query"] for c in calls) == 1

    asyncio.run(scenario())
