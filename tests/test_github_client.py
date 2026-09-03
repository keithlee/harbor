import asyncio
import json

import httpx
import pytest

from harbor.clients.github import GitHubClient
from harbor.poll.github_poller import GitHubPoller


def run(awaitable):
    return asyncio.run(awaitable)


def test_graphql_transport_is_one_read_request_and_not_a_mutation() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"repository": {
            "pr_221": {
                "number": 221,
                "headRefOid": "head-221",
                "merged": False,
                "mergeable": "MERGEABLE",
                "statusCheckRollup": None,
                "reviewThreads": {"nodes": []},
            }
        }}})

    client = GitHubClient(token="secret", transport=httpx.MockTransport(handler))
    try:
        facts = run(GitHubPoller(client, owner="keithlee", repo="Bookgen").poll([
            {"identifier": "HAN-29", "pr_urls": ["https://github.com/keithlee/Bookgen/pull/221"]}
        ]))
    finally:
        run(client.close())
    assert len(calls) == 1
    assert calls[0].method == "POST"  # GraphQL reads are HTTP POSTs.
    payload = json.loads(calls[0].content)
    assert payload["query"].lstrip().startswith("query ")
    assert "mutation" not in payload["query"].lower()
    assert facts[0]["ci"]["state"] == "absent"


def test_client_rejects_graphql_mutations() -> None:
    client = GitHubClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    with pytest.raises(ValueError):
        run(client.query("mutation Dangerous { deleteSomething { id } }"))
