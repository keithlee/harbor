"""A malformed Linear API key is refused up front and never leaks.

These tests drive the real httpx -> httpcore -> h11 stack against a loopback
HTTP server, not ``httpx.MockTransport``: the leak only appears where h11
validates header values (``LocalProtocolError: Illegal header value b'...'``)
or where httpx encodes them (``UnicodeEncodeError.object``).
"""

from __future__ import annotations

import asyncio
import json
import threading
import traceback
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from harbor.clients.linear import LinearClient, LinearClientError

# A dummy value: never a real key.
SENTINEL = "lin_api_DUMMYSENTINELdoesnotwork0000"


class _Handler(BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Handler.seen.append(self.headers.get("Authorization", ""))
        body = json.dumps({"data": {"viewer": {"id": "user-1"}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def loopback() -> Iterator[str]:
    _Handler.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/graphql"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _chain(exc: BaseException) -> list[BaseException]:
    seen: set[int] = set()
    found: list[BaseException] = []
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        stack += [current.__cause__, current.__context__]
    return found


def _exposed_text(exc: BaseException) -> str:
    """Everything a log line, traceback or chain-walking formatter could print."""

    parts = ["".join(traceback.format_exception(exc))]
    for item in _chain(exc):
        parts += [str(item), repr(item), repr(item.args), repr(getattr(item, "object", ""))]
        request = getattr(item, "_request", None)
        if request is not None:
            parts.append(repr(list(request.headers.raw)))
    return "\n".join(parts)


def _health(endpoint: str, monkeypatch: pytest.MonkeyPatch, key: str) -> bool:
    monkeypatch.setenv("LINEAR_API_KEY", key)
    monkeypatch.delenv("HARBOR_LINEAR_API_KEY", raising=False)

    async def run() -> bool:
        client = LinearClient(endpoint=endpoint)
        try:
            return await client.health()
        finally:
            await client.close()

    return asyncio.run(run())


MALFORMED = {
    "trailing-space": SENTINEL + " ",
    "trailing-tab": SENTINEL + "\t",
    "crlf-dotenv-cr": SENTINEL + "\r",  # `set -a; . .env` on a CRLF file
    "trailing-crlf": SENTINEL + "\r\n",
    "trailing-lf": SENTINEL + "\n",
    "leading-space": " " + SENTINEL,
    "non-ascii": SENTINEL + "é",
    "non-ascii-lookalike": SENTINEL.replace("a", "а", 1),  # Cyrillic a
}


@pytest.mark.parametrize("key", MALFORMED.values(), ids=MALFORMED.keys())
def test_malformed_key_is_refused_before_any_request_without_echoing_it(
    loopback: str, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    with pytest.raises(LinearClientError) as caught:
        _health(loopback, monkeypatch, key)

    assert _Handler.seen == []  # refused before anything reached the socket
    message = str(caught.value)
    assert "trailing whitespace" in message
    # Nothing on the exception chain can carry the key.
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    exposed = _exposed_text(caught.value)
    assert key not in exposed and key.strip() not in exposed
    assert "SENTINEL" not in exposed and "lin_api" not in exposed


def test_key_with_embedded_newline_is_refused_without_echoing_either_half(
    loopback: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    head, tail = SENTINEL[:12], SENTINEL[12:]
    with pytest.raises(LinearClientError) as caught:
        _health(loopback, monkeypatch, f"{head}\r\n{tail}")

    assert _Handler.seen == []
    exposed = _exposed_text(caught.value)
    assert head not in exposed and tail not in exposed


def test_well_formed_key_still_reaches_linear_unchanged(loopback: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _health(loopback, monkeypatch, SENTINEL) is True
    # Linear takes the raw key, not a bearer token.
    assert _Handler.seen == [SENTINEL]


def test_transport_failure_does_not_leave_the_key_on_the_chained_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # A closed loopback port: a real ConnectError from httpcore, whose
    # ``request`` still holds the headers that were about to be sent.
    probe = HTTPServer(("127.0.0.1", 0), _Handler)
    port = probe.server_port
    probe.server_close()

    with pytest.raises(LinearClientError) as caught:
        _health(f"http://127.0.0.1:{port}/graphql", monkeypatch, SENTINEL)

    assert any(type(item).__name__ == "ConnectError" for item in _chain(caught.value))
    assert SENTINEL not in _exposed_text(caught.value)
