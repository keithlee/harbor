"""`harbor workflow check`/`checks` fail closed with one actionable line.

Regression: on an empty or unmigrated ledger both commands crashed with
``sqlite3.OperationalError: no such table: runs``, and ``workflow check``
crashed with a raw ``LinearClientError`` traceback (for example "Entity not
found: Issue") when Linear could not return the ticket. Builders run these
commands non-interactively, so each failure must be one line that names the
problem and the fix, with a non-zero exit and no pass report.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harbor import cli
from harbor.clients.linear import LinearClientError
from harbor.db import HarborDB
from harbor.wake import apply_phase3_migration
from workflow_support import materialize


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    materialize(repo)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text(
        "# Pipeline\nlinear_project_slug: sample\nlinear_team: SAMPLE\ngithub_repository: example/repo\n"
    )
    projects = tmp_path / "projects.md"
    projects.write_text(
        "| name | repo_path | pipeline_path |\n| --- | --- | --- |\n"
        f"| SampleProject | {repo} | {pipeline} |\n"
    )
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(projects))
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "ledger" / "harbor.db"))
    return tmp_path


def _assert_one_line_failure(capsys, exit_code, *needles):
    captured = capsys.readouterr()
    assert exit_code == 2, captured
    assert captured.out == "", "a failed check must not print a result or pass report"
    lines = captured.err.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("Harbor: "), captured.err
    assert "Traceback" not in captured.err
    for needle in needles:
        assert needle in lines[0], lines[0]
    return lines[0]


@pytest.mark.parametrize(
    "argv",
    [
        ["workflow", "check", "SampleProject", "--ticket", "SAMPLE-1", "--event", "pre_handoff"],
        ["workflow", "check", "SampleProject", "--ticket", "SAMPLE-1", "--event", "pre_merge"],
        ["workflow", "checks", "SampleProject", "--ticket", "SAMPLE-1"],
    ],
    ids=["check-pre_handoff", "check-pre_merge", "checks"],
)
def test_empty_ledger_fails_closed_with_start_hint(project, capsys, argv):
    exit_code = cli.main(argv)
    _assert_one_line_failure(capsys, exit_code, "no run records", "harbor start SampleProject")


def test_linear_client_error_fails_closed_with_ticket_hint(project, capsys, monkeypatch):
    db = HarborDB(cli._db_path())
    try:
        apply_phase3_migration(db)
        db.replace_tickets(
            "SampleProject",
            [{"identifier": "SAMPLE-1", "state": "In Progress", "owned": True, "hold": False}],
        )
        with db.connection:
            db.connection.execute(
                "INSERT INTO runs (id, ticket, worktree, status) VALUES (?, ?, ?, ?)",
                ("run", "SAMPLE-1", str(project), "running"),
            )
    finally:
        db.close()

    class Linear:
        def __init__(self, **kwargs):
            pass

        async def fetch_blocker_context(self, ticket):
            raise LinearClientError("Entity not found: Issue")

        async def close(self):
            pass

    class GitHub:
        @classmethod
        def from_profile(cls, profile, **kwargs):
            return cls()

        async def close(self):
            pass

    monkeypatch.setattr("harbor.clients.linear.LinearClient", Linear)
    monkeypatch.setattr("harbor.clients.github.GitHubClient", GitHub)

    exit_code = cli.main(
        ["workflow", "check", "SampleProject", "--ticket", "SAMPLE-1", "--event", "pre_merge"]
    )
    _assert_one_line_failure(
        capsys, exit_code, "SAMPLE-1", "Entity not found: Issue", "check the ticket identifier"
    )
