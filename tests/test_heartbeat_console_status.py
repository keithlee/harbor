from __future__ import annotations

import asyncio
from types import SimpleNamespace

from harbor.console import Console
from harbor.heartbeat import Heartbeat


class _Supervisor:
    project = "SampleProject"
    on_update = None
    on_heartbeat = None


def run(coro):
    return asyncio.run(coro)


def test_heartbeat_captures_active_fleet_and_console_renders_idle_duration() -> None:
    async def scenario() -> tuple[object, list[str]]:
        heartbeat = Heartbeat(
            "SampleProject",
            status_provider=lambda: {
                "project": "SampleProject",
                "lifecycle": "RUNNING",
                "linear_healthy": True,
                "last_activity": 90.0,
                "runs": [
                    {"ticket": "SAMPLE-9", "status": "blocked", "waiting_on": "human"},
                    {"ticket": "SAMPLE-2", "status": "waiting", "waiting_on": "review"},
                    {"ticket": "SAMPLE-1", "status": "running", "waiting_on": "none"},
                    {"ticket": "SAMPLE-3", "status": "completed", "outcome": "parked", "waiting_on": "human"},
                    {"ticket": "SAMPLE-4", "status": "failed", "waiting_on": "none"},
                ],
            },
            clock=lambda: 135.0,
        )
        event = await heartbeat.tick()
        output: list[str] = []
        await Console(_Supervisor(), output=output.append)._on_heartbeat(event)
        return event, output

    event, output = run(scenario())
    assert [run.ticket for run in event.active_runs] == ["SAMPLE-1", "SAMPLE-2", "SAMPLE-9", "SAMPLE-3"]
    assert output == [
        "Harbor: heartbeat · SampleProject · RUNNING · healthy · "
        "fleet=SAMPLE-1 running waiting_on=none; SAMPLE-2 waiting waiting_on=review; "
        "SAMPLE-9 blocked waiting_on=human; SAMPLE-3 parked waiting_on=human · idle=45s"
    ]


def test_heartbeat_console_reports_idle_and_sanitizes_untrusted_facts() -> None:
    async def scenario() -> list[str]:
        output: list[str] = []
        console = Console(_Supervisor(), output=output.append)
        event = SimpleNamespace(
            project="SampleProject\nforged",
            lifecycle="PAUSED\tmode",
            healthy=False,
            active_runs=[
                {"ticket": "SAMPLE-42\n", "status": "waiting", "waiting_on": "human\x1b[31m"},
                {"ticket": "SAMPLE-99", "status": "completed"},
            ],
            idle_seconds=0,
            last_activity=None,
        )
        await console._on_heartbeat(event)
        return output

    assert run(scenario()) == [
        "Harbor: heartbeat · SampleProject forged · PAUSED mode · unavailable · "
        "fleet=SAMPLE-42 waiting waiting_on=human [31m · last_activity=unknown"
    ]
