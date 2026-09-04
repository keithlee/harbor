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


def test_console_lifecycle_commands_work_without_agents(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"issues": {"nodes": []}}})

    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", repo_path=tmp_path)
    db = HarborDB(tmp_path / "harbor.db")
    client = LinearClient(transport=httpx.MockTransport(handler))
    supervisor = Supervisor(LinearPoller(client, config, db=db), db=db)
    output: list[str] = []
    console = Console(supervisor, output=output.append)
    try:
        run(supervisor.start())
        assert supervisor.lifecycle == Lifecycle.RUNNING
        responses = run(console.run_lines(["/status", "/pause Stopping for the evening", "/resume", "/exit"]))
    finally:
        run(supervisor.exit())
        run(client.close())
        db.close()
    assert "Linear healthy" in responses[0]
    assert responses[1] == "Harbor: PAUSED · Stopping for the evening"
    assert responses[2] == "Harbor: RUNNING"
    assert responses[3] == "Harbor: STOPPED"
    assert supervisor.lifecycle == Lifecycle.STOPPED


def test_console_banner_reports_live_and_shadow_mode(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"issues": {"nodes": []}}})

    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", repo_path=tmp_path)
    client = LinearClient(transport=httpx.MockTransport(handler))
    live = Supervisor(LinearPoller(client, config))
    shadow = Supervisor(LinearPoller(client, config), shadow=True)
    try:
        live_banner = Console(live).banner()
        shadow_banner = Console(shadow).banner()
    finally:
        run(live.exit())
        run(shadow.exit())
        run(client.close())

    assert "(live mode)" in live_banner
    assert "(shadow mode)" in shadow_banner


def test_free_form_input_is_not_dispatched_in_phase_one(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"issues": {"nodes": []}}})

    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", repo_path=tmp_path)
    client = LinearClient(transport=httpx.MockTransport(handler))
    supervisor = Supervisor(LinearPoller(client, config))
    console = Console(supervisor)
    try:
        response = run(console.handle_command("What is waiting?"))
    finally:
        run(supervisor.exit())
        run(client.close())
    assert response is not None
    assert "no model call" in response
    assert calls == []


def test_status_renders_observed_tickets_and_never_backlog(tmp_path: Path) -> None:
    def node(identifier: str, state: str) -> dict:
        return {
            "id": identifier,
            "identifier": identifier,
            "title": f"Title {identifier}",
            "state": {"name": state, "type": "started"},
            "attachments": {"nodes": []},
            "comments": {"nodes": []},
            "inverseRelations": {"nodes": []},
        }

    nodes = [
        node("SAMPLE-29", "In Progress"),
        node("SAMPLE-16", "In Progress"),
        node("SAMPLE-17", "In Review"),
        node("SAMPLE-7", "Blocked"),
        node("SAMPLE-99", "Backlog"),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"issues": {"nodes": nodes}}})

    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", repo_path=tmp_path)
    client = LinearClient(transport=httpx.MockTransport(handler))
    supervisor = Supervisor(LinearPoller(client, config))
    console = Console(supervisor)
    try:
        run(supervisor.start())
        rendered = console.status_text()
    finally:
        run(supervisor.exit())
        run(client.close())
    for identifier, state in (("SAMPLE-29", "In Progress"), ("SAMPLE-16", "In Progress"), ("SAMPLE-17", "In Review"), ("SAMPLE-7", "Blocked")):
        assert f"{identifier} (observed)  {state}" in rendered
    assert "SAMPLE-99" not in rendered
    assert "Backlog" not in rendered


def test_background_output_is_emitted_under_patch_stdout(tmp_path: Path, monkeypatch) -> None:
    """AC-5: a background event must not corrupt a partially typed line.

    prompt_toolkit's patch_stdout redraws the prompt below anything written
    while the user is mid-line.  The guarantee is that the console's read loop
    runs INSIDE that context, so this asserts the wiring rather than trying to
    drive a real terminal.
    """

    import harbor.console as console_module

    entered: list[str] = []

    class Patch:
        def __init__(self, raw: bool = False) -> None:
            self.raw = raw

        def __enter__(self):
            entered.append("in")
            return self

        def __exit__(self, *exc):
            entered.append("out")
            return False

    monkeypatch.setattr(console_module, "patch_stdout", lambda *, raw=False: Patch(raw))

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"issues": {"nodes": []}}})

    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", repo_path=tmp_path)
    db = HarborDB(tmp_path / "harbor.db")
    client = LinearClient(transport=httpx.MockTransport(handler))
    supervisor = Supervisor(LinearPoller(client, config, db=db), db=db)
    console = console_module.Console(supervisor, output=lambda _: None)
    console.exiting = True  # leave after one pass
    try:
        run(console.run())
    finally:
        run(supervisor.exit())
        run(client.close())
        db.close()
    assert entered[:1] == ["in"], "the console read loop must run inside patch_stdout"
