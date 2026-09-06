from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import harbor.cli as cli
from harbor.config.projects import ProjectConfig
from harbor.console import Console
from harbor.db import HarborDB
from harbor.lease import ProjectLease, ProjectLeaseError, _lease_path
from harbor.supervisor import Supervisor
from harbor.wake import Wake


def test_second_live_project_lease_reports_owner_metadata(tmp_path: Path) -> None:
    first = ProjectLease(tmp_path / "harbor.db", "SampleProject").acquire()
    try:
        second = ProjectLease(tmp_path / "harbor.db", "SampleProject")
        try:
            second.acquire()
        except ProjectLeaseError as exc:
            message = str(exc)
        else:  # pragma: no cover - a broken flock would make this unsafe
            raise AssertionError("a second supervisor acquired the same project lease")
        assert "SampleProject already has a live Harbor supervisor" in message
        assert '"pid":' in message
        assert '"cwd":' in message
    finally:
        first.release()


def test_released_project_lease_can_be_reacquired(tmp_path: Path) -> None:
    first = ProjectLease(tmp_path / "harbor.db", "SampleProject").acquire()
    first.release()
    second = ProjectLease(tmp_path / "harbor.db", "SampleProject").acquire()
    second.release()


def test_distinct_project_names_cannot_share_a_lease_path(tmp_path: Path) -> None:
    db_path = tmp_path / "harbor.db"

    assert _lease_path(db_path, "Acme API") != _lease_path(db_path, "Acme/API")


def test_duplicate_start_notifies_and_enters_read_only_mode(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project")
    monkeypatch.setattr(cli, "get_project", lambda _: config)
    monkeypatch.setattr(cli, "_db_path", lambda: tmp_path / "harbor.db")

    class RecordingNotifier:
        reasons: list[str] = []

        async def notify(self, reason: str) -> None:
            self.reasons.append(reason)

    notifier = RecordingNotifier()
    monkeypatch.setattr(cli, "Notifier", lambda: notifier)
    calls: list[dict[str, object]] = []

    async def read_only_start(project: str, **kwargs: object) -> int:
        calls.append({"project": project, **kwargs})
        return 0

    monkeypatch.setattr(cli, "_start", read_only_start)
    owner = ProjectLease(tmp_path / "harbor.db", "SampleProject").acquire()
    try:
        args = cli.build_parser().parse_args(["start", "SampleProject"])
        assert asyncio.run(cli._async_main(args)) == 0
    finally:
        owner.release()

    assert calls == [
        {
            "project": "SampleProject",
            "once": False,
            "shadow": False,
            "read_only": True,
            "ticket": None,
        }
    ]
    assert len(notifier.reasons) == 1
    assert "already running" in notifier.reasons[0]
    assert "read-only" in notifier.reasons[0]
    assert "read-only" in capsys.readouterr().err


def test_owner_start_releases_lease_after_start_returns(monkeypatch, tmp_path: Path) -> None:
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project")
    monkeypatch.setattr(cli, "get_project", lambda _: config)
    monkeypatch.setattr(cli, "_db_path", lambda: tmp_path / "harbor.db")
    calls: list[dict[str, object]] = []

    async def live_start(project: str, **kwargs: object) -> int:
        calls.append({"project": project, **kwargs})
        return 0

    monkeypatch.setattr(cli, "_start", live_start)
    args = cli.build_parser().parse_args(["start", "SampleProject"])
    assert asyncio.run(cli._async_main(args)) == 0
    assert calls == [
        {
            "project": "SampleProject",
            "once": False,
            "shadow": False,
            "read_only": False,
            "ticket": None,
        }
    ]

    reacquired = ProjectLease(tmp_path / "harbor.db", "SampleProject").acquire()
    reacquired.release()


def test_explicit_shadow_start_does_not_own_runtime(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def shadow_start(project: str, **kwargs: object) -> int:
        calls.append({"project": project, **kwargs})
        return 0

    monkeypatch.setattr(cli, "_start", shadow_start)
    args = cli.build_parser().parse_args(["start", "SampleProject", "--shadow"])

    assert asyncio.run(cli._async_main(args)) == 0
    assert calls == [
        {
            "project": "SampleProject",
            "once": False,
            "shadow": True,
            "read_only": True,
            "ticket": None,
        }
    ]


class _NoPoller:
    project_name = "SampleProject"
    config = SimpleNamespace(
        poll_interval_seconds=3600.0,
        heartbeat_seconds=3600.0,
        hold_all=False,
        dispatch_states=("Todo",),
    )

    async def poll(self):
        raise AssertionError("read-only lifecycle test must not poll")


def test_read_only_supervisor_preserves_owner_runtime_row(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    db.set_runtime("SampleProject", "RUNNING")
    supervisor = Supervisor(_NoPoller(), db=db, shadow=True, read_only=True, migrations=[])
    try:
        asyncio.run(supervisor.start(initial_poll=False))
        assert db.runtime("SampleProject")["lifecycle"] == "RUNNING"
        asyncio.run(supervisor.exit())
        assert db.runtime("SampleProject")["lifecycle"] == "RUNNING"
    finally:
        db.close()


def test_read_only_console_disables_mutating_commands() -> None:
    supervisor = Supervisor(_NoPoller(), shadow=True, read_only=True)
    console = Console(supervisor)
    assert "(read-only mode)" in console.banner()
    for command in ("/pause", "/hold on SAMPLE-1", "/resume", "/answer 1 yes", "/kill"):
        response = asyncio.run(console.handle_command(command))
        assert response is not None
        assert "read-only mode" in response


def test_read_only_supervisor_suppresses_delivery_without_shadow_flag() -> None:
    delivered: list[Wake] = []
    supervisor = Supervisor(_NoPoller(), read_only=True, on_wake=delivered.append)
    wake = Wake(ticket={"identifier": "SAMPLE-1"}, action="build")

    asyncio.run(supervisor._emit_wake(wake))

    assert delivered == []
    assert supervisor.shadow_wakes == [wake]
