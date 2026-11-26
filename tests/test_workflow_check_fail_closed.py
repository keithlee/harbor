"""`harbor workflow check`/`checks` fail closed with one actionable line.

Regression: on an empty or unmigrated ledger both commands crashed with
``sqlite3.OperationalError: no such table: runs``, and ``workflow check``
crashed with a raw ``LinearClientError`` traceback (for example "Entity not
found: Issue") when Linear could not return the ticket. Builders run these
commands non-interactively, so each failure must be one line that names the
problem and the fix, with a non-zero exit and no pass report.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
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


# Regression: every LinearClientError got "check the ticket identifier and
# that the Linear API key can read it", which is wrong advice for a timeout, a
# 5xx or a malformed/partial page. The real LinearClient runs here against a
# mock transport, so each error is the one Linear's failure actually produces.
def _blocker_issue(**overrides):
    page = {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
    issue = {
        "identifier": "SAMPLE-1",
        "attachments": page,
        "inverseRelations": page,
        "labels": page,
        "comments": page,
    }
    issue.update(overrides)
    return {"data": {"issue": issue}}


def _timeout(request):
    raise httpx.ReadTimeout("timed out", request=request)


LINEAR_FAILURES = pytest.mark.parametrize(
    ("handler", "expected", "forbidden"),
    [
        (
            lambda request: httpx.Response(200, json={"errors": [{"message": "Entity not found: Issue"}]}),
            "check the ticket identifier",
            "retry",
        ),
        (
            lambda request: httpx.Response(401, json={"errors": [{"message": "Authentication required"}]}),
            "Linear API key (LINEAR_API_KEY or HARBOR_LINEAR_API_KEY) is valid",
            "retry",
        ),
        (
            lambda request: httpx.Response(200, json={"errors": [{"message": "Authentication required, not authenticated"}]}),
            "Linear API key (LINEAR_API_KEY or HARBOR_LINEAR_API_KEY) is valid",
            "retry",
        ),
        (_timeout, "Linear is unreachable or returned an error; retry", "check the ticket identifier"),
        (
            lambda request: httpx.Response(503, text="service unavailable"),
            "Linear is unreachable or returned an error; retry",
            "check the ticket identifier",
        ),
        (
            lambda request: httpx.Response(200, json=_blocker_issue(labels={"nodes": []})),
            "incomplete or malformed ticket context; retry",
            "check the ticket identifier",
        ),
        (
            lambda request: httpx.Response(
                200, json=_blocker_issue(attachments={"nodes": [], "pageInfo": {"hasNextPage": True}})
            ),
            "incomplete or malformed ticket context; retry",
            "check the ticket identifier",
        ),
    ],
    ids=["not-found", "http-401", "graphql-unauthenticated", "timeout", "http-503", "malformed-page", "partial-page"],
)


@LINEAR_FAILURES
def test_linear_failure_message_matches_the_failure(project, capsys, monkeypatch, handler, expected, forbidden):
    import harbor.clients.linear as linear_module

    _seed_run(str(project))
    real = linear_module.LinearClient

    class Linear(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    class GitHub:
        @classmethod
        def from_profile(cls, profile, **kwargs):
            return cls()

        async def close(self):
            pass

    monkeypatch.setattr("harbor.clients.linear.LinearClient", Linear)
    monkeypatch.setattr("harbor.clients.github.GitHubClient", GitHub)
    monkeypatch.setenv("LINEAR_API_KEY", DUMMY_TOKEN)

    exit_code = cli.main(CHECK_ARGV)

    line = _assert_one_line_failure(capsys, exit_code, "Linear could not provide ticket SAMPLE-1", expected)
    assert forbidden not in line
    assert DUMMY_TOKEN not in line


# --- GitHub and worktree failures fail closed too ---------------------------
#
# Regression: ``workflow check`` let a GitHub failure escape as a raw
# ``GitHubError``/``GitHubGraphQLError`` (or, on the default transport, a raw
# ``httpx`` error) traceback with exit 1, the same code as a real "blocked"
# verdict. ``workflow checks`` let ``WorktreeConfinementError`` ("retained
# worktree custody mismatch") and a failing ``git rev-parse HEAD`` escape the
# same way.

PR_URL = "https://github.com/example/repo/pull/1"
DUMMY_TOKEN = "dummy-test-token-not-a-secret"
CHECK_ARGV = ["workflow", "check", "SampleProject", "--ticket", "SAMPLE-1", "--event", "pre_handoff"]
CHECKS_ARGV = ["workflow", "checks", "SampleProject", "--ticket", "SAMPLE-1"]


def _seed_run(worktree):
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
                ("run", "SAMPLE-1", worktree, "running"),
            )
    finally:
        db.close()


class _LinearWithPR:
    def __init__(self, **kwargs):
        pass

    async def fetch_blocker_context(self, ticket):
        return {
            "identifier": ticket,
            "project": {"slugId": "sample"},
            "state": {"name": "In Progress", "type": "started"},
            "attachments": {"nodes": [{"sourceType": "github", "url": PR_URL}]},
        }

    async def close(self):
        pass


def _mock_transport_client(handler):
    import harbor.clients.github as github_module

    real = github_module.GitHubClient

    class Client(real):
        """The real client, with only its transport swapped for a mock."""

        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return Client


class _UnreachableGitHub:
    """The default client's own path: no transport, so httpx errors escape raw."""

    @classmethod
    def from_profile(cls, profile, **kwargs):
        return cls()

    async def query(self, document, variables=None):
        raise httpx.ConnectError("All connection attempts failed")

    async def close(self):
        pass


@pytest.mark.parametrize(
    ("make_client", "needle"),
    [
        (
            lambda: _mock_transport_client(lambda request: httpx.Response(502, text="bad gateway")),
            "GitHub request failed",
        ),
        (
            lambda: _mock_transport_client(
                lambda request: httpx.Response(200, json={"errors": [{"message": "Could not resolve to a Repository"}]})
            ),
            "Could not resolve to a Repository",
        ),
        (lambda: _UnreachableGitHub, "All connection attempts failed"),
    ],
    ids=["http-502", "graphql-error", "unreachable-host"],
)
def test_github_failure_fails_closed_instead_of_blocking(project, capsys, monkeypatch, make_client, needle):
    _seed_run(str(project))
    monkeypatch.setattr("harbor.clients.linear.LinearClient", _LinearWithPR)
    monkeypatch.setattr("harbor.clients.github.GitHubClient", make_client())
    monkeypatch.setenv("GITHUB_TOKEN", DUMMY_TOKEN)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    exit_code = cli.main(CHECK_ARGV)

    line = _assert_one_line_failure(
        capsys, exit_code, "GitHub could not provide the linked PR for SAMPLE-1", needle, "check github_api_url"
    )
    assert DUMMY_TOKEN not in line


@pytest.mark.parametrize("worktree", [None, "outside"], ids=["no-worktree-custody", "worktree-outside-root"])
def test_checks_without_retained_worktree_fails_closed(project, capsys, worktree):
    _seed_run(None if worktree is None else str(project / worktree))

    exit_code = cli.main(CHECKS_ARGV)

    _assert_one_line_failure(
        capsys,
        exit_code,
        "SAMPLE-1 has no retained Harbor worktree",
        "retained worktree custody mismatch",
        "harbor start SampleProject",
    )


def test_checks_with_unreadable_head_fails_closed(project, capsys, monkeypatch):
    from harbor.worktree import Worktree, WorktreeManager

    unborn = project / "unborn"
    unborn.mkdir()
    subprocess.run(["git", "init", "-q", str(unborn)], check=True)
    _seed_run(str(unborn))
    monkeypatch.setattr(
        WorktreeManager,
        "attach",
        lambda self, ticket: Worktree(unborn, unborn, "harbor/SAMPLE-1", "origin/main"),
    )

    exit_code = cli.main(CHECKS_ARGV)

    _assert_one_line_failure(
        capsys, exit_code, "retained worktree for SAMPLE-1 has no readable HEAD commit", str(unborn)
    )
