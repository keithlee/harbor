from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from harbor.clients.linear import LinearClient
from harbor.config.projects import ProjectConfig
from harbor.console import Console
from harbor.db import HarborDB
from harbor.poll.linear_poller import LinearPoller
from harbor.supervisor import Lifecycle, Supervisor


def run(coro):
    return asyncio.run(coro)


def _issue(title: str = "A ticket", updated_at: str = "2026-09-03T20:00:00Z") -> dict:
    return {
        "id": "linear-han-42",
        "identifier": "HAN-42",
        "title": title,
        "url": "https://linear.app/hanzireader/issue/HAN-42",
        "updatedAt": updated_at,
        "state": {"name": "Todo", "type": "backlog"},
        "attachments": {"nodes": []},
        "comments": {"nodes": []},
        "inverseRelations": {"nodes": []},
    }


def _console(tmp_path: Path, nodes: list[dict]) -> tuple[Console, Supervisor, HarborDB, LinearClient]:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"issues": {"nodes": nodes}}})

    config = ProjectConfig(
        name="Bookgen",
        linear_project_slug="hanzireader",
        repo_path=tmp_path,
        poll_interval_seconds=3600,
    )
    db = HarborDB(tmp_path / "harbor.db")
    client = LinearClient(transport=httpx.MockTransport(handler))
    supervisor = Supervisor(LinearPoller(client, config, db=db), db=db)
    return Console(supervisor), supervisor, db, client


def test_hold_on_and_off_are_ticket_scoped_and_do_not_pause(tmp_path: Path) -> None:
    console, supervisor, db, client = _console(tmp_path, [_issue()])
    try:
        run(supervisor.poll_once())
        assert run(console.handle_command("/hold off HAN-42")) == "Harbor: HOLD OFF · HAN-42"
        assert db.tickets("Bookgen")[0]["hold"] is False
        assert supervisor.lifecycle == Lifecycle.STOPPED
        assert run(console.handle_command("/hold on HAN-42")) == "Harbor: HOLD ON · HAN-42"
        assert db.tickets("Bookgen")[0]["hold"] is True
        assert supervisor.lifecycle == Lifecycle.STOPPED
    finally:
        run(supervisor.exit())
        run(client.close())
        db.close()


def test_hold_rejects_invalid_syntax_without_mutating_the_ticket(tmp_path: Path) -> None:
    console, supervisor, db, client = _console(tmp_path, [_issue()])
    try:
        run(supervisor.poll_once())
        for command in ("/hold", "/hold maybe HAN-42", "/hold off", "/hold off HAN-42 extra"):
            assert run(console.handle_command(command)) == "Usage: /hold on|off <ticket>"
        assert db.tickets("Bookgen")[0]["hold"] is True
    finally:
        run(supervisor.exit())
        run(client.close())
        db.close()


def test_hold_rejects_an_unknown_ticket(tmp_path: Path) -> None:
    console, supervisor, db, client = _console(tmp_path, [_issue()])
    try:
        run(supervisor.poll_once())
        assert run(console.handle_command("/hold off HAN-999")) == "Harbor: unknown ticket · HAN-999"
        assert db.tickets("Bookgen")[0]["hold"] is True
    finally:
        run(supervisor.exit())
        run(client.close())
        db.close()


def test_hold_persists_across_a_changed_linear_poll(tmp_path: Path) -> None:
    nodes = [_issue()]
    console, supervisor, db, client = _console(tmp_path, nodes)
    try:
        run(supervisor.poll_once())
        assert run(console.handle_command("/hold off HAN-42")) == "Harbor: HOLD OFF · HAN-42"

        # A changed remote observation exercises the normal upsert path.  The
        # poller must preserve Harbor's local hold instead of Linear's default.
        nodes[0]["title"] = "A ticket with a changed observation"
        nodes[0]["updatedAt"] = "2026-09-03T20:01:00Z"
        result = run(supervisor.poll_once())
        assert result.changed is True
        assert result.tickets[0]["hold"] is False
        assert supervisor.status().tickets[0]["hold"] is False
        assert db.tickets("Bookgen")[0]["hold"] is False
    finally:
        run(supervisor.exit())
        run(client.close())
        db.close()
