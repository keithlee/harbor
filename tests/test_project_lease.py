from __future__ import annotations

import asyncio
from pathlib import Path

import harbor.cli as cli
from harbor.config.projects import ProjectConfig
from harbor.lease import ProjectLease, ProjectLeaseError


def test_second_live_project_supervisor_is_refused_with_owner_metadata(tmp_path: Path) -> None:
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


def test_start_refuses_to_enter_supervisor_when_project_lease_is_held(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project")
    monkeypatch.setattr(cli, "get_project", lambda _: config)
    monkeypatch.setattr(cli, "_db_path", lambda: tmp_path / "harbor.db")

    async def forbidden_start(*args, **kwargs):
        raise AssertionError("the losing process must not initialize or poll a supervisor")

    monkeypatch.setattr(cli, "_start", forbidden_start)
    owner = ProjectLease(tmp_path / "harbor.db", "SampleProject").acquire()
    try:
        args = cli.build_parser().parse_args(["start", "SampleProject"])
        assert asyncio.run(cli._async_main(args)) == 2
    finally:
        owner.release()

    error = capsys.readouterr().err
    assert "start refused" in error
    assert "already has a live Harbor supervisor" in error
