"""The production GitHubClient path must wrap transport failures in GitHubError.

``GitHubClient()`` with no ``transport``/``http_client`` builds its own
``httpx.AsyncClient`` per query.  These tests keep that exact code path and
only swap the network layer underneath it for an ``httpx.MockTransport``, so no
real request (and no credential) ever leaves the machine.
"""

import asyncio

import httpx
import pytest

import harbor.clients.github as github_module
from harbor.clients.github import GitHubClient, GitHubError

TOKEN = "ghp_regression_sentinel_token"


def _patch_default_client(monkeypatch, handler) -> None:
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        assert "transport" not in kwargs  # still the production default path
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(github_module.httpx, "AsyncClient", factory)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("[Errno 8] nodename nor servname provided"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectTimeout("timed out"),
    ],
    ids=["connect-error", "read-timeout", "connect-timeout"],
)
def test_default_client_wraps_transport_failures(monkeypatch, failure) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    _patch_default_client(monkeypatch, handler)
    client = GitHubClient(token=TOKEN)
    assert client.transport is None and client._client is None

    with pytest.raises(GitHubError) as caught:
        asyncio.run(client.query("query { viewer { login } }"))

    assert caught.value.__cause__ is failure
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("status", [401, 502])
def test_default_client_wraps_http_status_errors(monkeypatch, status) -> None:
    _patch_default_client(monkeypatch, lambda request: httpx.Response(status, text="nope"))
    client = GitHubClient(token=TOKEN)

    with pytest.raises(GitHubError) as caught:
        asyncio.run(client.query("query { viewer { login } }"))

    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
    assert TOKEN not in str(caught.value)


def test_default_client_wraps_non_json_body(monkeypatch) -> None:
    _patch_default_client(monkeypatch, lambda request: httpx.Response(200, text="<html>proxy</html>"))
    client = GitHubClient(token=TOKEN)

    with pytest.raises(GitHubError) as caught:
        asyncio.run(client.query("query { viewer { login } }"))

    assert isinstance(caught.value.__cause__, ValueError)
    assert TOKEN not in str(caught.value)
