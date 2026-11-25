"""PR links are recognized on the project's configured GitHub host only."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from harbor.clients.github import GitHubClient
from harbor.config.profile import PipelineProfile
from harbor.db import HarborDB
from harbor.poll import github_poller
from harbor.poll.github_poller import GitHubPoller, attached_pull_requests

GHES_API = "https://ghe.example.test/api/graphql"
GHES_PR = "https://ghe.example.test/example/SampleProject/pull/7"
DOTCOM_PR = "https://github.com/example/SampleProject/pull/7"


def run(awaitable):
    return asyncio.run(awaitable)


def profile(api_url: str | None = None) -> PipelineProfile:
    kwargs = {"github_api_url": api_url} if api_url else {}
    return PipelineProfile(repository_owner="example", repository_name="SampleProject", **kwargs)


class FakeGitHubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[int, ...]]] = []

    async def pull_requests(self, owner, repo, numbers):
        self.calls.append((owner, repo, tuple(numbers)))
        return {"repository": {f"pr_{n}": {"number": n, "headRefOid": "abc"} for n in numbers}}


def poll(api_url: str | None, url: str) -> tuple[FakeGitHubClient, list]:
    client = FakeGitHubClient()
    facts = run(GitHubPoller(client, profile(api_url)).poll([{"identifier": "SAMPLE-1", "pr_urls": [url]}]))
    return client, facts


@pytest.mark.parametrize(
    ("api_url", "host"),
    [
        (None, "github.com"),
        ("", "github.com"),
        ("https://api.github.com/graphql", "github.com"),
        ("https://ghe.example.test/api/graphql", "ghe.example.test"),
        ("https://ghe.example.test/api/v3", "ghe.example.test"),
        ("https://api.corp.example.test/api/graphql", "api.corp.example.test"),
        ("https://api.sample.ghe.com/graphql", "sample.ghe.com"),
        ("https://ghe.example.test:8443/api/graphql", "ghe.example.test:8443"),
        ("https://10.0.0.5/api/graphql", "10.0.0.5"),
    ],
)
def test_web_host_follows_the_configured_api_endpoint(api_url, host) -> None:
    assert github_poller.github_web_host(api_url) == host


@pytest.mark.parametrize("api_url", ["not a url", "https:///api/graphql", "ftp://ghe.example.test/api/graphql"])
def test_a_malformed_endpoint_fails_closed(api_url) -> None:
    with pytest.raises(ValueError):
        github_poller.github_web_host(api_url)
    with pytest.raises(ValueError):
        GitHubPoller(FakeGitHubClient(), profile(api_url))


def test_a_ghes_link_is_recognized_for_a_ghes_project() -> None:
    client, facts = poll(GHES_API, GHES_PR)
    assert client.calls == [("example", "SampleProject", (7,))]
    # A reduced node with no url gets the canonical URL on the configured host.
    assert facts[0]["url"] == GHES_PR


def test_a_ghe_com_link_is_recognized_for_a_ghe_com_project() -> None:
    client, _ = poll("https://api.sample.ghe.com/graphql", "https://sample.ghe.com/example/SampleProject/pull/7")
    assert client.calls == [("example", "SampleProject", (7,))]


@pytest.mark.parametrize(
    ("api_url", "url"),
    [
        (GHES_API, DOTCOM_PR),
        (GHES_API, "https://ghe.other.test/example/SampleProject/pull/7"),
        (None, GHES_PR),
        (None, "https://evil.example.test/example/SampleProject/pull/7"),
        (None, "https://github.com.evil.example.test/example/SampleProject/pull/7"),
        # The configured host appearing later in a foreign link does not make it ours.
        (None, f"https://evil.example.test/redirect?to={DOTCOM_PR}"),
        (GHES_API, f"https://evil.example.test/{GHES_PR}"),
    ],
)
def test_a_link_on_another_host_is_never_queried(api_url, url) -> None:
    client, facts = poll(api_url, url)
    assert client.calls == []
    assert facts == []


def test_github_com_default_is_unchanged() -> None:
    client, facts = poll(None, DOTCOM_PR)
    assert client.calls == [("example", "SampleProject", (7,))]
    assert facts[0]["url"] == DOTCOM_PR
    assert attached_pull_requests(
        [{"identifier": "SAMPLE-1", "pr_urls": ["https://www.github.com/example/SampleProject/pulls/8#files"]}],
        repository="example/SampleProject",
    ) == ((8, "SAMPLE-1"),)


def test_ghes_poll_reaches_only_the_configured_endpoint_through_a_mock_transport() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"repository": {
            "pr_7": {"number": 7, "url": GHES_PR, "headRefOid": "abc", "merged": False},
        }}})

    ghes = profile(GHES_API)
    client = GitHubClient(token="placeholder", endpoint=ghes.github_api_url, transport=httpx.MockTransport(handler))
    try:
        facts = run(GitHubPoller(client, ghes).poll([
            {"identifier": "SAMPLE-1", "pr_urls": [
                GHES_PR,
                "https://github.com/example/SampleProject/pull/8",
                "https://evil.example.test/example/SampleProject/pull/9",
            ]},
        ]))
    finally:
        run(client.close())
    assert [str(request.url) for request in requests] == [GHES_API]
    payload = json.loads(requests[0].content)
    assert payload["variables"] == {"owner": "example", "repo": "SampleProject"}
    # Only the PR linked on the configured host is asked for.
    assert "pullRequest(number: 7)" in payload["query"]
    assert "number: 8" not in payload["query"] and "number: 9" not in payload["query"]
    assert [(fact["number"], fact["url"]) for fact in facts] == [(7, GHES_PR)]


def test_workflow_check_finds_the_linked_ghes_pr(tmp_path, monkeypatch) -> None:
    from harbor.workflow_cli import fresh_context
    from workflow_support import config

    raw = {
        "identifier": "A-1",
        "project": {"slugId": "canonical"},
        "description": "Risk: low",
        "state": {"name": "In Progress", "type": "started"},
        "comments": {"nodes": []},
        "attachments": {"nodes": [{"sourceType": "github", "url": GHES_PR}]},
        "inverseRelations": {"nodes": []},
    }

    class Linear:
        def __init__(self, **kwargs):
            pass

        async def fetch_blocker_context(self, ticket):
            return raw

        async def close(self):
            pass

    class GitHub:
        def __init__(self, **kwargs):
            pass

        async def fetch_pull_requests(self, owner, repo, numbers):
            return {f"pr_{n}": {"number": n, "headRefOid": "abc", "url": GHES_PR, "merged": False} for n in numbers}

        async def close(self):
            pass

    monkeypatch.setattr("harbor.clients.linear.LinearClient", Linear)
    monkeypatch.setattr("harbor.clients.github.GitHubClient", GitHub)
    cfg = SimpleNamespace(
        name="A",
        linear_api_url="https://linear.invalid",
        linear_project_scope=("canonical",),
        github_repository="example/SampleProject",
        github_api_url=GHES_API,
        required_checks=(),
        workflow_path=str(config(tmp_path)),
    )
    db = HarborDB(":memory:")
    try:
        db.replace_tickets("A", [{"identifier": "A-1", "state": "In Progress", "owned": True, "hold": False}])
        context = run(fresh_context(cfg, db, "A-1", {"id": "run", "pr_url": GHES_PR}, "pre_merge"))
    finally:
        db.close()
    assert context["facts"]["url"] == GHES_PR
    assert context["head_sha"] == "abc"
