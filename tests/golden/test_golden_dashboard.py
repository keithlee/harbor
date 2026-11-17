"""Golden dashboard contracts: HTTP access matrix and the exposed status payload."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from harbor.dashboard import _dashboard_status, create_app
from harbor.supervisor import SupervisorStatus

PORT = 8765
TOKEN = "launch-token"
ORIGIN = f"http://127.0.0.1:{PORT}"
HOST = {"Host": f"127.0.0.1:{PORT}"}


def status() -> SupervisorStatus:
    return SupervisorStatus(
        project="demo",
        lifecycle="RUNNING",
        linear_healthy=True,
        github_healthy=True,
        last_error="linear: token=lin_api_SECRET rejected",
        github_error=None,
        last_activity="2026-11-17T10:00:00+00:00",
        tickets=(
            {"identifier": "DEMO-1", "state": "Human Review", "title": "Ship", "url": "https://linear.app/x/DEMO-1",
             "owned": True, "hold": False, "pr_urls": ["https://github.com/acme/demo/pull/1"],
             "description": "PRIVATE ticket body", "assignee_email": "someone@example.invalid"},
        ),
        runs=(
            {"id": "run-1", "ticket": "DEMO-1", "role": "builder", "status": "waiting", "waiting_on": "review",
             "worktree": "/Users/someone/private/worktree", "session_id": "native-session-secret",
             "final_json": "{}", "next_action": "Owner approves", "pr_url": "https://github.com/acme/demo/pull/1"},
            {"id": "run-x", "ticket": "OTHER-9", "role": "builder", "status": "running",
             "worktree": "/other/project"},
        ),
        escalations=({"id": 4, "ticket": "DEMO-1", "kind": "blocker", "question": "Which API?",
                      "context_json": "{\"secret\": true}"},),
    )


class Supervisor:
    read_only = False
    shadow = False

    def __init__(self) -> None:
        self.answers: list[tuple[str, str]] = []

    def status(self) -> SupervisorStatus:
        return status()

    async def answer_escalation(self, escalation, message, *, background):
        self.answers.append((escalation, message))
        return {"queued": True}


def console(supervisor: Supervisor) -> SimpleNamespace:
    async def ask(question, *, digest, interactive):
        return "answer"
    return SimpleNamespace(supervisor=supervisor, ask=ask, cancel_answer=lambda: None)


def test_status_payload_exposes_only_allowlisted_fields(golden) -> None:
    golden.json("dashboard_status_payload.json", _dashboard_status(console(Supervisor())))


REQUESTS = {
    "page": ("GET", "/", None),
    "status": ("GET", "/api/status", None),
    "chat": ("POST", "/api/chat", {"message": "What is running?"}),
    "answer": ("POST", "/api/escalations/4/answer", {"message": "Use v2"}),
    "answer_unknown": ("POST", "/api/escalations/99/answer", {"message": "Use v2"}),
    "unknown_api": ("GET", "/api/secrets", None),
}

HEADERS = {
    "browser_same_origin": {**HOST, "Origin": ORIGIN},
    "browser_with_token": {**HOST, "Origin": ORIGIN, "Authorization": f"Bearer {TOKEN}"},
    "no_origin": dict(HOST),
    "no_origin_with_token": {**HOST, "Authorization": f"Bearer {TOKEN}"},
    "wrong_token": {**HOST, "Origin": ORIGIN, "Authorization": "Bearer nope"},
    "token_without_bearer": {**HOST, "Origin": ORIGIN, "Authorization": TOKEN},
    "dns_rebinding_host": {"Host": f"attacker.example:{PORT}", "Origin": f"http://attacker.example:{PORT}"},
    "localhost_alias": {"Host": f"localhost:{PORT}", "Origin": f"http://localhost:{PORT}"},
    "cross_origin": {**HOST, "Origin": "https://attacker.example", "Authorization": f"Bearer {TOKEN}"},
    "cross_site_fetch": {**HOST, "Sec-Fetch-Site": "cross-site", "Authorization": f"Bearer {TOKEN}"},
    "same_site_fetch": {**HOST, "Origin": ORIGIN, "Sec-Fetch-Site": "same-site", "Authorization": f"Bearer {TOKEN}"},
    "null_origin": {**HOST, "Origin": "null", "Authorization": f"Bearer {TOKEN}"},
}


def test_dashboard_access_matrix(golden) -> None:
    supervisor = Supervisor()

    async def scenario() -> dict:
        table: dict[str, dict[str, int]] = {}
        app = create_app(console(supervisor), port=PORT, token=TOKEN)
        async with TestClient(TestServer(app)) as client:
            for request_name, (method, path, body) in REQUESTS.items():
                row = table.setdefault(request_name, {})
                for header_name, headers in HEADERS.items():
                    kwargs = {"headers": headers, "allow_redirects": False}
                    if body is not None:
                        kwargs["json"] = body
                    async with client.request(method, path, **kwargs) as response:
                        row[header_name] = response.status
        return table

    table = asyncio.run(scenario())
    golden.json("dashboard_access_matrix.json", table)
    # Only the launch-token holder, from the dashboard's own origin, records decisions.
    assert supervisor.answers == [("4", "Use v2")]


def test_api_responses_carry_hardening_headers() -> None:
    async def scenario() -> dict:
        app = create_app(console(Supervisor()), port=PORT, token=TOKEN)
        async with TestClient(TestServer(app)) as client:
            async with client.get("/api/status", headers=HOST) as response:
                return dict(response.headers)

    headers = asyncio.run(scenario())
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "unsafe-inline" not in headers["Content-Security-Policy"]
