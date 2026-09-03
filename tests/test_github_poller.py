import asyncio
import json
from pathlib import Path

from harbor.poll.github_poller import GitHubPoller


class FakeGitHubClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def pull_requests(self, owner, repo, numbers):
        self.calls.append((owner, repo, tuple(numbers)))
        return self.response


def run(awaitable):
    return asyncio.run(awaitable)


def test_poll_queries_only_prs_attached_to_active_tickets() -> None:
    response = json.loads((Path(__file__).parent / "fixtures" / "github_prs.json").read_text())
    client = FakeGitHubClient(response)
    tickets = [
        {"identifier": "HAN-29", "pr_urls": ["https://github.com/keithlee/Bookgen/pull/221"]},
        # This legacy PR is not attached and must not be queried.
        {"identifier": "HAN-17", "pr_urls": []},
    ]
    facts = run(GitHubPoller(client, owner="keithlee", repo="Bookgen", required_checks=["CI"]).poll(tickets))
    assert client.calls == [("keithlee", "Bookgen", (221,))]
    assert [item["number"] for item in facts] == [221]
    assert facts[0]["ci"]["state"] == "green"
    assert len(facts[0]["threads"]) == 4


def test_no_attached_prs_means_no_github_call() -> None:
    client = FakeGitHubClient({})
    assert run(GitHubPoller(client, owner="o", repo="r").poll([{"identifier": "HAN-1", "pr_urls": []}])) == []
    assert client.calls == []
