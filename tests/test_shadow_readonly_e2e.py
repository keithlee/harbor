"""Whole-run proof that shadow supervision is read-only (Phase 3 AC-9)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

import httpx

from harbor.clients.github import GitHubClient
from harbor.clients.linear import LinearClient
from harbor.config.projects import ProjectConfig
from harbor.config.profile import PipelineProfile
from harbor.db import HarborDB
from harbor.poll.github_poller import GitHubPoller
from harbor.poll.linear_poller import LinearPoller
from harbor.supervisor import Supervisor


class RecordingGraphQL:
    """An HTTP transport that records the actual GraphQL request envelope."""

    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = dict(response)
        self.calls: list[dict[str, Any]] = []
        self.mutation_attempts: list[dict[str, Any]] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        call = {
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "body": payload,
        }
        self.calls.append(call)
        encoded = json.dumps(payload, sort_keys=True).casefold()
        if "mutation" in request.url.path.casefold() or "mutation" in encoded:
            self.mutation_attempts.append(call)
        return httpx.Response(200, json=self.response)


class RecordingDelivery:
    """Dispatcher/resumer seams that must remain untouched in shadow mode."""

    def __init__(self) -> None:
        self.dispatches: list[Any] = []
        self.resumes: list[Any] = []

    async def dispatch(self, wake: Any) -> None:
        self.dispatches.append(wake)

    async def resume(self, wake: Any) -> None:
        self.resumes.append(wake)


class RecordingPREventProcessor:
    """Keep observation available while recording any attempted delivery."""

    def __init__(self) -> None:
        self.observations: list[tuple[Any, Mapping[str, Any]]] = []
        self.deliveries: list[tuple[Any, Mapping[str, Any]]] = []

    def observe(self, ticket: Any, facts: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.observations.append((ticket, facts))
        return []

    async def process(self, ticket: Any, facts: Mapping[str, Any], **_: Any) -> None:
        self.deliveries.append((ticket, facts))


def _linear_issue() -> dict[str, Any]:
    return {
        "id": "linear-id-42",
        "identifier": "SAMPLE-42",
        "title": "Exercise the shadow wake boundary",
        "url": "https://linear.app/sample-project/issue/SAMPLE-42",
        "updatedAt": "2026-09-03T12:00:00Z",
        "branchName": "sample-42-shadow-proof",
        "state": {"name": "Todo", "type": "backlog"},
        "attachments": {
            "nodes": [
                {
                    "url": "https://github.com/example/SampleProject/pull/42",
                    "sourceType": "github",
                }
            ]
        },
        "comments": {"nodes": []},
        "inverseRelations": {"nodes": []},
    }


def _github_pull_request() -> dict[str, Any]:
    return {
        "number": 42,
        "url": "https://github.com/example/SampleProject/pull/42",
        "title": "Exercise the shadow wake boundary",
        "body": "",
        "state": "OPEN",
        "merged": False,
        "mergedAt": None,
        "mergeable": "MERGEABLE",
        "createdAt": "2026-09-03T12:00:00Z",
        "headRefOid": "0123456789abcdef0123456789abcdef01234567",
        "comments": {"nodes": []},
        "reviews": {"nodes": []},
        "statusCheckRollup": {
            "state": "SUCCESS",
            "contexts": {
                "nodes": [
                    {
                        "__typename": "CheckRun",
                        "name": "CI",
                        "conclusion": "SUCCESS",
                        "status": "COMPLETED",
                    }
                ]
            },
        },
        "reviewThreads": {"nodes": []},
    }


def test_shadow_full_poll_cycle_would_wake_but_performs_no_delivery_or_mutation(
    tmp_path: Path,
) -> None:
    """Real clients/pollers may read, but shadow mode cannot deliver anything."""

    async def scenario() -> dict[str, Any]:
        linear_wire = RecordingGraphQL(
            {"data": {"issues": {"nodes": [_linear_issue()]}}}
        )
        github_wire = RecordingGraphQL(
            {
                "data": {
                    "repository": {"pr_42": _github_pull_request()},
                }
            }
        )
        linear_client = LinearClient(
            token="linear-test-token",
            endpoint="https://linear.test/graphql",
            transport=linear_wire.transport,
        )
        github_client = GitHubClient(
            token="github-test-token",
            endpoint="https://github.test/graphql",
            transport=github_wire.transport,
        )
        db = HarborDB(tmp_path / "shadow-e2e.db")
        delivery = RecordingDelivery()
        pr_events = RecordingPREventProcessor()
        config = ProjectConfig(
            name="SampleProject",
            linear_project_slug="sample-project",
            repo_path=tmp_path,
            github_repository="example/SampleProject",
            required_checks=("CI",),
            poll_interval_seconds=3600,
        )
        linear_poller = LinearPoller(linear_client, config, db=db)
        github_poller = GitHubPoller(
            github_client,
            PipelineProfile(
                repository_owner="example",
                repository_name="SampleProject",
                required_checks=("CI",),
            ),
            db=db,
            event_processor=pr_events,
            project="SampleProject",
        )
        supervisor = Supervisor(
            linear_poller,
            db=db,
            github_poller=github_poller,
            dispatcher=delivery,
            runner=delivery,
            pr_event_processor=pr_events,
            shadow=True,
        )
        wake_callback_calls: list[Any] = []
        supervisor.on_wake = wake_callback_calls.append

        try:
            first = await supervisor.poll_once()
            first_wakes = [
                (wake.action, wake.idem_key) for wake in supervisor.shadow_wakes
            ]
            supervisor.shadow_wakes.clear()
            second = await supervisor.poll_once()
            second_wakes = [
                (wake.action, wake.idem_key) for wake in supervisor.shadow_wakes
            ]
            return {
                "first": first,
                "second": second,
                "first_wakes": first_wakes,
                "second_wakes": second_wakes,
                "linear_wire": linear_wire,
                "github_wire": github_wire,
                "delivery": delivery,
                "pr_events": pr_events,
                "wake_callback_calls": wake_callback_calls,
                "status": supervisor.status(),
            }
        finally:
            await linear_client.close()
            await github_client.close()
            db.close()

    result = asyncio.run(scenario())

    assert result["first"] is not None and result["first"].changed is True
    assert result["second"] is not None and result["second"].changed is False
    assert result["first"].tickets[0]["identifier"] == "SAMPLE-42"
    assert result["first"].tickets[0]["state"] == "Todo"
    assert result["first_wakes"] == [("build", "SAMPLE-42@build")]
    assert result["second_wakes"] == []

    # A Todo ticket was actionable, but shadow mode did not hand it to any
    # dispatcher/resumer or to the PR-event delivery path.
    assert result["delivery"].dispatches == []
    assert result["delivery"].resumes == []
    assert result["pr_events"].deliveries == []
    assert result["wake_callback_calls"] == []
    assert result["pr_events"].observations

    # Every actual outbound request from both real clients was a GraphQL query
    # to the read endpoint; no mutation operation or mutation endpoint/body was
    # even attempted.
    for wire in (result["linear_wire"], result["github_wire"]):
        assert wire.calls
        assert wire.mutation_attempts == []
        for call in wire.calls:
            assert call["method"] == "POST"
            assert call["path"] == "/graphql"
            operation = call["body"]["query"]
            assert operation.lstrip().startswith("query ")
            assert "mutation" not in operation.casefold()

    assert result["status"].linear_healthy is True
    assert result["status"].github_healthy is True
