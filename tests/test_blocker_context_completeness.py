"""The blocker context must prove every connection it read is complete.

``fetch_blocker_context`` feeds the unblock ladder and First Mate with an
issue's labels, attachments, blocking relations and comments.  A response that
omits a connection or its ``pageInfo`` cannot show that the first page was the
whole set, so a blocking relation, PR link or risk label beyond it would be
invisible.  Such a response must fail closed instead of being read as complete.
"""
import asyncio
import json

import httpx
import pytest

from harbor.clients.linear import LinearClient, LinearClientError
from harbor.db import HarborDB
from harbor.runner.base import apply_phase4_migration
from harbor.supervisor import Supervisor
from harbor.unblock import UnblockLadder
from harbor.wake import Wake, apply_phase3_migration
from types import SimpleNamespace

FIELDS = ("labels", "attachments", "inverseRelations", "comments")


def complete_issue():
    done = {"hasNextPage": False, "endCursor": None}
    return {
        "id": "uuid-1",
        "identifier": "MOCK-1",
        "description": "The button color is blue.",
        "state": {"name": "Blocked", "type": "started"},
        "labels": {"nodes": [{"name": "risk: high"}], "pageInfo": dict(done)},
        "attachments": {"nodes": [{"url": "https://github.com/mock/repo/pull/1", "sourceType": "github"}],
                        "pageInfo": dict(done)},
        "inverseRelations": {"nodes": [], "pageInfo": dict(done)},
        "comments": {"nodes": [], "pageInfo": dict(done)},
    }


def mutate(issue, field, defect):
    if defect == "missing pageInfo":
        del issue[field]["pageInfo"]
    elif defect == "null pageInfo":
        issue[field]["pageInfo"] = None
    elif defect == "pageInfo without hasNextPage":
        issue[field]["pageInfo"] = {}
    elif defect == "missing connection":
        del issue[field]
    elif defect == "null connection":
        issue[field] = None
    elif defect == "nodes not a list":
        issue[field]["nodes"] = None
    else:  # pragma: no cover - guards the parametrization itself
        raise AssertionError(defect)
    return issue


def client_for(issue, requests=None):
    def handler(request):
        payload = json.loads(request.content)
        if requests is not None:
            requests.append(payload["query"])
        return httpx.Response(200, json={"data": {"issue": issue}})
    return LinearClient(token="mock", transport=httpx.MockTransport(handler))


async def fetch(issue):
    client = client_for(issue)
    try:
        return await client.fetch_blocker_context("MOCK-1")
    finally:
        await client.close()


DEFECTS = ("missing pageInfo", "null pageInfo", "pageInfo without hasNextPage",
           "missing connection", "null connection", "nodes not a list")


@pytest.mark.parametrize("defect", DEFECTS)
@pytest.mark.parametrize("field", FIELDS)
def test_unprovable_blocker_connection_fails_closed(field, defect):
    issue = mutate(complete_issue(), field, defect)
    with pytest.raises(LinearClientError, match=f"blocker {field}"):
        asyncio.run(fetch(issue))


def test_complete_blocker_context_is_returned():
    issue = asyncio.run(fetch(complete_issue()))
    assert issue["labels"]["nodes"] == [{"name": "risk: high"}]
    assert issue["attachments"]["nodes"][0]["url"].endswith("/pull/1")
    assert issue["inverseRelations"]["nodes"] == []
    assert issue["comments"] == {"nodes": []}


@pytest.mark.parametrize("field", ("inverseRelations", "labels"))
def test_ladder_does_not_resume_or_mutate_on_unprovable_context(tmp_path, field):
    """A blocked final whose fresh context is unprovable stops at the error.

    The supervisor's blocked-final path (``Supervisor._handle_runner_result``)
    catches the ladder's exception and records it in ``last_error``.  It must
    not reach First Mate, a resume, or a Linear mutation.
    """

    db = HarborDB(":memory:")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    db.replace_tickets("mock", [{"identifier": "MOCK-1", "title": "Color", "state": "Blocked"}])
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,session_id,worktree,deadline_seconds) VALUES (?,?,?,?,?,?)",
        ("logical", "MOCK-1", "blocked", "native", str(tmp_path), 123),
    )
    db.connection.commit()
    queries = []
    linear = client_for(mutate(complete_issue(), field, "missing pageInfo"), queries)

    async def forbidden(*args, **kwargs):
        raise AssertionError("unprovable blocker context reached a model, resume or remediation")

    ladder = UnblockLadder(db, linear=linear, first_mate=forbidden, resumer=forbidden,
                           remediation_executor=forbidden, repo_path=tmp_path)
    supervisor = Supervisor(SimpleNamespace(project_name="mock", config=SimpleNamespace()), db=db,
                            unblock_ladder=ladder)
    wake = Wake(ticket={"identifier": "MOCK-1"}, action="reengage", resume="logical")
    final = {"status": "blocked", "run_id": "logical", "summary": "Color selection failed",
             "blocker": {"kind": "ambiguous_ac", "question": "Which color?"}}

    async def run():
        try:
            await supervisor._handle_runner_result(wake, final)
        finally:
            await linear.close()

    asyncio.run(run())
    assert f"blocker {field}" in (supervisor.last_error or "")
    assert supervisor.last_unblock is None
    assert queries and all("mutation" not in query for query in queries)
    assert db.connection.execute("SELECT COUNT(*) FROM escalations").fetchone()[0] == 0
    db.close()
