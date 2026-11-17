import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from harbor.dashboard import create_app


def test_dashboard_status_and_chat_are_tokenless_but_decisions_are_protected(monkeypatch):
    origin = "http://127.0.0.1:8765"
    host = {"Host": "127.0.0.1:8765"}
    operator = {**host, "Authorization": "Bearer private-launch-token"}
    status = {"tickets": [{"identifier": "ABC-83", "state": "Human Review"}]}
    monkeypatch.setattr("harbor.dashboard._dashboard_status", lambda _: status)
    monkeypatch.setattr("harbor.dashboard._snapshot", lambda _: None)
    monkeypatch.setattr("harbor.dashboard.render_digest", lambda _: "Current activity")

    async def ask(question, *, digest, interactive):
        assert (question, digest, interactive) == ("What is running?", "Current activity", False)
        return "The project is idle."

    async def scenario():
        app = create_app(SimpleNamespace(ask=ask), port=8765, token="private-launch-token")
        async with TestClient(TestServer(app)) as client:
            async with client.get("/status", headers=host) as response:
                assert response.status == 200
                assert "Ticket activity" in await response.text()
            async with client.get("/api/status", headers=host) as response:
                assert await response.json() == {**status, "actions_authorized": False}
                assert response.headers["Cache-Control"] == "no-store"
            async with client.get("/api/status", headers=operator) as response:
                assert await response.json() == {**status, "actions_authorized": True}
            for headers in [
                {**host, "Authorization": "Bearer old-token"},
                {**host, "Sec-Fetch-Site": "same-origin"},
            ]:
                async with client.get("/api/status", headers=headers) as response:
                    assert response.status == 200
                    assert not (await response.json())["actions_authorized"]
            for headers in [
                {"Host": "hostile.example"},
                {**host, "Origin": "https://hostile.example"},
                {**host, "Sec-Fetch-Site": "cross-site"},
            ]:
                async with client.get("/api/status", headers=headers) as response:
                    assert response.status == 403
            async with client.post("/api/chat", headers={**host, "Origin": origin}, json={"message": "What is running?"}) as response:
                assert await response.json() == {"reply": "The project is idle."}
            for headers in [host, {**host, "Origin": "https://hostile.example"}]:
                async with client.post("/api/chat", headers=headers) as response:
                    assert response.status == 403
            path = "/api/escalations/15/answer"
            async with client.post(path, headers={**host, "Origin": origin}) as response:
                assert response.status == 401
            async with client.post(path, headers={**operator, "Origin": origin}) as response:
                assert response.status == 400  # The handler rejected the missing payload.

    asyncio.run(scenario())
