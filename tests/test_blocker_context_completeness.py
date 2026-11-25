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


class Notifier:
    def __init__(self):
        self.messages = []

    async def notify(self, text):
        self.messages.append(text)


def per_ticket_linear(issues, queries):
    def handler(request):
        payload = json.loads(request.content)
        queries.append(payload["query"])
        identifier = payload["variables"]["id"]
        return httpx.Response(200, json={"data": {"issue": issues[identifier]}})
    return LinearClient(token="mock", transport=httpx.MockTransport(handler))


def test_startup_retry_survives_one_tickets_unprovable_context(tmp_path):
    """A malformed blocker context for one ticket must not wedge ``start()``.

    Startup replays deterministic capability blockers for retained blocked
    runs.  One ticket's ``LinearClientError`` used to escape ``start()``,
    leaving Harbor in STARTING with no poll, so every ticket stalled.
    """
    from harbor.poll.linear_poller import PollResult
    from harbor.supervisor import Lifecycle

    class Poller:
        project_name = "mock"
        config = SimpleNamespace()

        def __init__(self):
            self.calls = 0

        async def poll(self):
            self.calls += 1
            return PollResult((), f"snapshot-{self.calls}", False, ())

    good = complete_issue()
    good["identifier"] = "MOCK-2"
    bad = mutate(complete_issue(), "labels", "null pageInfo")
    queries, resumes = [], []
    linear = per_ticket_linear({"MOCK-1": bad, "MOCK-2": good}, queries)

    async def resumer(run_id, prompt):
        resumes.append(run_id)
        return True

    async def forbidden(*args, **kwargs):
        raise AssertionError("deterministic blocker reached First Mate or remediation")

    async def scenario():
        db = HarborDB(tmp_path / "ledger.sqlite")
        apply_phase3_migration(db)
        apply_phase4_migration(db)
        db.replace_tickets("mock", [
            {"identifier": "MOCK-1", "title": "One", "state": "Blocked", "owned": True, "hold": False},
            {"identifier": "MOCK-2", "title": "Two", "state": "Blocked", "owned": True, "hold": False},
        ])
        final = json.dumps({"status": "blocked", "blocker": {
            "kind": "env_blocker", "question": "Linear connector requires approval"}})
        for run_id, ticket, finished in (("run-1", "MOCK-1", "2026-09-05T02:00:00+00:00"),
                                         ("run-2", "MOCK-2", "2026-09-05T01:00:00+00:00")):
            db.connection.execute(
                "INSERT INTO runs(id,ticket,role,status,final_json,finished_at) VALUES (?,?,'builder','blocked',?,?)",
                (run_id, ticket, final, finished))
        db.connection.commit()
        ladder = UnblockLadder(db, linear=linear, first_mate=forbidden, resumer=resumer,
                               remediation_executor=forbidden)
        notifier = Notifier()
        poller = Poller()
        supervisor = Supervisor(poller, db=db, unblock_ladder=ladder, notifier=notifier)
        try:
            await supervisor.start()
            state = supervisor.lifecycle
        finally:
            await supervisor.stop()
            await linear.close()
            db.close()
        return supervisor, poller, notifier, state

    supervisor, poller, notifier, state = asyncio.run(scenario())
    assert state == Lifecycle.RUNNING
    assert poller.calls >= 1
    assert resumes == ["run-2"]  # the other ticket was still processed
    assert all("mutation" not in query for query in queries)
    # last_error is reset by the successful initial poll, so the durable
    # operator signal is the notice.
    assert len(notifier.messages) == 1
    assert "MOCK-1" in notifier.messages[0] and "blocker labels" in notifier.messages[0]


def test_blocked_final_with_unprovable_context_notifies_once_and_schedules_bounded_retry(tmp_path):
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
    linear = per_ticket_linear({"MOCK-1": mutate(complete_issue(), "inverseRelations", "missing pageInfo")}, queries)

    async def forbidden(*args, **kwargs):
        raise AssertionError("unprovable blocker context reached a model, resume or remediation")

    notifier = Notifier()
    ladder = UnblockLadder(db, linear=linear, first_mate=forbidden, resumer=forbidden,
                           remediation_executor=forbidden, repo_path=tmp_path)
    supervisor = Supervisor(SimpleNamespace(project_name="mock", config=SimpleNamespace()), db=db,
                            unblock_ladder=ladder, notifier=notifier)
    wake = Wake(ticket={"identifier": "MOCK-1"}, action="reengage", resume="logical")
    final = {"status": "blocked", "run_id": "logical", "summary": "Color selection failed",
             "blocker": {"kind": "ambiguous_ac", "question": "Which color?"}}

    async def run():
        try:
            await supervisor._handle_runner_result(wake, final)
            await supervisor._handle_runner_result(wake, final)
        finally:
            await linear.close()

    asyncio.run(run())
    assert len(notifier.messages) == 1, notifier.messages
    assert "MOCK-1" in notifier.messages[0] and "blocker inverseRelations" in notifier.messages[0]
    receipt = db.connection.execute(
        "SELECT attempts,last_reason FROM recovery_receipts WHERE run_id='triage:logical'").fetchone()
    assert receipt is not None and receipt[0] == 0 and "Linear" in receipt[1]
    db.close()
