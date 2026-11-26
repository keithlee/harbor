"""GitHubClient wraps GitHub-side failures in GitHubError, and never leaks the token.

``GitHubClient()`` with no ``transport``/``http_client`` is the production
default: it builds its own ``httpx.AsyncClient`` per query.  The "default"
cases keep that exact code path and only swap the network layer underneath it
for an ``httpx.MockTransport``; the "injected" cases pass the mock transport
in.  No real request, and no real credential, ever leaves the machine.
"""

import asyncio

import httpx
import pytest

import harbor.clients.github as github_module
from harbor.clients.github import GitHubClient, GitHubError

TOKEN = "ghp_regression_sentinel_token"
QUERY = "query { viewer { login } }"


def _client(monkeypatch, path, handler, token=TOKEN):
    """A GitHubClient on ``path`` whose network layer is ``handler``."""

    if path == "injected":
        return GitHubClient(token=token, transport=httpx.MockTransport(handler))
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        assert "transport" not in kwargs  # still the production default path
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(github_module.httpx, "AsyncClient", factory)
    client = GitHubClient(token=token)
    assert client.transport is None and client._client is None
    return client


def _query(client, variables=None):
    async def go():
        try:
            return await client.query(QUERY, variables)
        finally:
            await client.close()

    return asyncio.run(go())


def _exposed_text(exc: BaseException) -> str:
    """Everything a logger or traceback walker could print from ``exc``'s chain."""

    seen: set[int] = set()
    parts: list[str] = []
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        parts += [str(current), repr(current), repr(current.args), repr(vars(current))]
        if isinstance(current, UnicodeError):
            parts.append(repr(getattr(current, "object", "")))
        if isinstance(current, httpx.HTTPError):
            try:
                request = current.request
            except RuntimeError:
                request = None
            if request is not None:
                # Headers' repr masks Authorization; read the real values.
                parts.append(repr(list(request.headers.items())))
        response = getattr(current, "response", None)
        if isinstance(response, httpx.Response):
            parts.append(repr(list(response.request.headers.items())))
        stack += [current.__cause__, current.__context__]
    return "\n".join(parts)


PATHS = pytest.mark.parametrize("path", ["default", "injected"])


@PATHS
@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout],
    ids=["connect-error", "read-timeout", "connect-timeout"],
)
def test_transport_failures_become_github_error_without_the_token(monkeypatch, path, failure) -> None:
    sent: list[str] = []
    raised: list[Exception] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["Authorization"])
        raised.append(failure("network down", request=request))
        raise raised[-1]

    with pytest.raises(GitHubError) as caught:
        _query(_client(monkeypatch, path, handler))

    assert sent == [f"bearer {TOKEN}"]  # the token really was on the request
    assert caught.value.__cause__ is raised[0]
    assert TOKEN not in _exposed_text(caught.value)


@PATHS
@pytest.mark.parametrize("status", [401, 502])
def test_http_error_status_becomes_one_line_github_error_without_the_token(monkeypatch, path, status) -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["Authorization"])
        return httpx.Response(status, text="nope")

    with pytest.raises(GitHubError) as caught:
        _query(_client(monkeypatch, path, handler))

    assert sent == [f"bearer {TOKEN}"]
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
    assert "\n" not in str(caught.value)
    assert TOKEN not in _exposed_text(caught.value)


@PATHS
def test_non_json_body_becomes_github_error(monkeypatch, path) -> None:
    # For example a GitHub Enterprise proxy answering with a login page.
    handler = lambda request: httpx.Response(200, text="<html>proxy login</html>")

    with pytest.raises(GitHubError) as caught:
        _query(_client(monkeypatch, path, handler))

    assert isinstance(caught.value.__cause__, ValueError)
    assert TOKEN not in _exposed_text(caught.value)


@PATHS
def test_unserializable_variables_are_a_caller_bug_not_a_github_error(monkeypatch, path) -> None:
    sent: list[httpx.Request] = []
    circular: dict = {}
    circular["self"] = circular

    with pytest.raises(ValueError) as caught:
        _query(_client(monkeypatch, path, sent.append), {"loop": circular})

    assert not isinstance(caught.value, GitHubError)
    assert sent == []


@PATHS
@pytest.mark.parametrize("token", ["ghp_tokén_sentinel", "ghp_tok\nen_sentinel"], ids=["non-ascii", "newline"])
def test_unsendable_token_is_refused_before_any_request_without_echoing_it(monkeypatch, path, token) -> None:
    sent: list[httpx.Request] = []

    with pytest.raises(ValueError) as caught:
        _query(_client(monkeypatch, path, sent.append, token=token))

    assert not isinstance(caught.value, GitHubError)
    assert sent == []
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    exposed = _exposed_text(caught.value)
    assert token not in exposed and "sentinel" not in exposed
