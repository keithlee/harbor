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


def _startup_scenario(tmp_path, ladder_factory, notifier):
    """Run ``start()`` with two retained blocked runs carrying a deterministic blocker."""
    from harbor.poll.linear_poller import PollResult

    class Poller:
        project_name = "mock"
        config = SimpleNamespace()

        def __init__(self):
            self.calls = 0

        async def poll(self):
            self.calls += 1
            return PollResult((), f"snapshot-{self.calls}", False, ())

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
        ladder, cleanup = ladder_factory(db)
        poller = Poller()
        supervisor = Supervisor(poller, db=db, unblock_ladder=ladder, notifier=notifier)
        try:
            await supervisor.start()
            state = supervisor.lifecycle
            receipts = db.connection.execute(
                "SELECT run_id,last_reason FROM recovery_receipts ORDER BY run_id").fetchall() \
                if db.connection.execute("SELECT 1 FROM sqlite_master WHERE name='recovery_receipts'").fetchone() else []
        finally:
            await supervisor.stop()
            await cleanup()
            db.close()
        return state, poller.calls, [tuple(row) for row in receipts]

    return asyncio.run(scenario())


def test_startup_does_not_report_a_stopped_ticket_as_a_linear_failure(tmp_path):
    """A complete context for a Canceled ticket is an expected outcome, not a Linear fault."""
    from harbor.supervisor import Lifecycle

    canceled = complete_issue()
    canceled["state"] = {"name": "Canceled", "type": "canceled"}
    good = complete_issue()
    good["identifier"] = "MOCK-2"
    queries, resumes = [], []

    async def resumer(run_id, prompt):
        resumes.append(run_id)
        return True

    async def forbidden(*args, **kwargs):
        raise AssertionError("deterministic blocker reached First Mate or remediation")

    def factory(db):
        linear = per_ticket_linear({"MOCK-1": canceled, "MOCK-2": good}, queries)
        return (UnblockLadder(db, linear=linear, first_mate=forbidden, resumer=resumer,
                              remediation_executor=forbidden), linear.close)

    notifier = Notifier()
    state, polls, receipts = _startup_scenario(tmp_path, factory, notifier)
    assert state == Lifecycle.RUNNING and polls >= 1
    assert resumes == ["run-2"]
    assert all("Linear blocker context" not in message for message in notifier.messages), notifier.messages
    assert notifier.messages == []  # expected outcome: recorded, not paged on every restart
    assert receipts == []


def test_startup_reports_a_non_linear_error_accurately_and_logs_it(tmp_path, caplog):
    import logging
    from harbor.supervisor import Lifecycle

    handled = []

    class Ladder:
        def can_retry_deterministically(self, ticket, run_row, final):
            return True

        async def handle(self, ticket, run_row, final):
            if ticket["identifier"] == "MOCK-1":
                raise TypeError("unexpected keyword 'x'")
            handled.append(ticket["identifier"])
            return "resumed"

    async def nothing():
        return None

    notifier = Notifier()
    with caplog.at_level(logging.ERROR, logger="harbor.supervisor"):
        state, polls, receipts = _startup_scenario(tmp_path, lambda db: (Ladder(), nothing), notifier)
    assert state == Lifecycle.RUNNING and polls >= 1
    assert handled == ["MOCK-2"]
    assert len(notifier.messages) == 1, notifier.messages
    message = notifier.messages[0]
    assert "MOCK-1" in message and "could not retry deterministic blocker" in message and "TypeError" in message
    assert "Linear" not in message
    assert any(record.exc_info and record.exc_info[0] is TypeError for record in caplog.records)
    assert receipts == []


def test_startup_linear_failure_schedules_bounded_triage_retry(tmp_path):
    bad = mutate(complete_issue(), "labels", "null pageInfo")
    good = complete_issue()
    good["identifier"] = "MOCK-2"
    queries = []

    async def resumer(run_id, prompt):
        return True

    async def forbidden(*args, **kwargs):
        raise AssertionError("deterministic blocker reached First Mate or remediation")

    def factory(db):
        linear = per_ticket_linear({"MOCK-1": bad, "MOCK-2": good}, queries)
        return (UnblockLadder(db, linear=linear, first_mate=forbidden, resumer=resumer,
                              remediation_executor=forbidden), linear.close)

    notifier = Notifier()
    _, _, receipts = _startup_scenario(tmp_path, factory, notifier)
    assert receipts == [("triage:run-1", "Linear blocker context unavailable")]
    assert len(notifier.messages) == 1 and "Linear blocker context" in notifier.messages[0]


def test_linear_context_triage_retry_is_bounded_and_escalates_with_its_cause(tmp_path):
    """Replay through _retry_stopped_builders until exhaustion with retry_delays (0, 0)."""
    from harbor.recovery import BlockerPolicy
    from harbor.runner.base import apply_phase9_migration
    from harbor.supervisor import Lifecycle

    db = HarborDB(tmp_path / "ledger.sqlite")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    ticket = {"identifier": "MOCK-1", "id": "uuid-1", "title": "Color", "state": "In Progress",
              "owned": True, "hold": False}
    final = {"status": "blocked", "summary": "Need a color", "run_id": "run-1",
             "blocker": {"kind": "product_decision", "question": "What button color?"}}
    db.replace_tickets("mock", [ticket], emit_events=False)
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,worktree,session_id,deadline_seconds,final_json) VALUES (?,?,?,?,?,?,?)",
        ("run-1", "MOCK-1", "blocked", str(tmp_path), "session-1", 60, json.dumps(final)))
    db.connection.commit()
    queries = []
    linear = per_ticket_linear({"MOCK-1": mutate(complete_issue(), "inverseRelations", "missing pageInfo")}, queries)

    async def forbidden(*args, **kwargs):
        raise AssertionError("unprovable blocker context reached a model, resume or remediation")

    class Dispatcher:
        async def retry(self, *args):
            raise AssertionError("builder must not be retried while its blocker is untriaged")

    notifier = Notifier()
    poller = SimpleNamespace(project_name="mock", config=SimpleNamespace(blocker_policy=BlockerPolicy((0, 0))))
    supervisor = Supervisor(poller, db=db, dispatcher=Dispatcher(), notifier=notifier)
    supervisor.unblock_ladder = UnblockLadder(db, linear=linear, first_mate=forbidden, resumer=forbidden,
                                              remediation_executor=forbidden, repo_path=tmp_path)
    supervisor.lifecycle = Lifecycle.RUNNING
    supervisor.linear_healthy = True
    supervisor._fleet_tickets = (ticket,)
    wake = Wake(ticket=ticket, action="reengage", resume="run-1")

    async def scenario():
        try:
            await supervisor._handle_runner_result(wake, final)
            for _ in range(5):
                await asyncio.sleep(0.01)
                await supervisor._retry_stopped_builders()
                await asyncio.gather(*list(supervisor._dispatch_tasks.values()))
        finally:
            await linear.close()

    asyncio.run(scenario())
    fetches = [query for query in queries if "HarborBlocker" in query]
    assert len(fetches) == 3  # the original triage plus two bounded replays
    assert all("mutation" not in query for query in queries)
    context = [m for m in notifier.messages if "blocker context could not be verified" in m]
    exhausted = [m for m in notifier.messages if "automatic recovery exhausted" in m]
    assert len(context) == 1 and len(exhausted) == 1, notifier.messages
    assert "Linear blocker context" in exhausted[0]
    escalations = [row for row in db.escalations() if row["status"] == "open"]
    assert len(escalations) == 1
    assert escalations[0]["question"] == "Linear blocker context retries exhausted; how should this blocker be resolved?"
    assert db.connection.execute(
        "SELECT last_reason FROM recovery_receipts WHERE run_id='triage:run-1'").fetchone()[0] == \
        "Linear blocker context unavailable"
    db.close()


def _post_triage_failure(tmp_path, caplog, *, failing, disposition):
    """Complete context, First Mate decides, then a Linear write fails; replay 5 polls."""
    import logging
    from harbor.recovery import BlockerPolicy
    from harbor.runner.base import apply_phase9_migration
    from harbor.supervisor import Lifecycle

    queries = []

    def handler(request):
        query = json.loads(request.content)["query"]
        queries.append(query)
        if "HarborComment" in query:
            if failing == "comment":
                return httpx.Response(200, json={"data": {"commentCreate": {"success": False, "comment": None}}})
            return httpx.Response(200, json={"data": {"commentCreate": {
                "success": True, "comment": {"id": f"c-{len(queries)}", "body": "", "createdAt": "2026-09-05T00:00:00Z"}}}})
        if "HarborStates" in query:
            return httpx.Response(200, json={"data": {"issue": {"id": "uuid-1", "team": {"id": "team-1", "name": "Team", "states": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": "11111111-1111-1111-1111-111111111111", "name": "Human Review", "type": "started"},
                          {"id": "22222222-2222-2222-2222-222222222222", "name": "Blocked", "type": "started"}]}}}}})
        if "HarborProgress" in query:
            return httpx.Response(200, json={"data": {"issueUpdate": {"success": False, "issue": None}}})
        issue = complete_issue()
        issue["description"] = "The button color must be blue."
        issue["state"] = {"name": "In Progress", "type": "started"}
        return httpx.Response(200, json={"data": {"issue": issue}})

    db = HarborDB(tmp_path / "ledger.sqlite")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    ticket = {"identifier": "MOCK-1", "id": "uuid-1", "title": "Color", "state": "In Progress",
              "owned": True, "hold": False}
    final = {"status": "blocked", "summary": "Need a color", "run_id": "run-1",
             "blocker": {"kind": "product_decision", "question": "What button color?"}}
    db.replace_tickets("mock", [ticket], emit_events=False)
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,worktree,session_id,deadline_seconds,final_json) VALUES (?,?,?,?,?,?,?)",
        ("run-1", "MOCK-1", "blocked", str(tmp_path), "session-1", 60, json.dumps(final)))
    db.connection.commit()
    linear = LinearClient(token="mock", transport=httpx.MockTransport(handler))
    prompts = []

    async def mate(prompt, **kwargs):
        prompts.append(prompt)
        return disposition

    async def resume(run_id, prompt):
        return True

    class Dispatcher:
        async def retry(self, *args):
            raise AssertionError("builder must not be retried")

    notifier = Notifier()
    poller = SimpleNamespace(project_name="mock", config=SimpleNamespace(blocker_policy=BlockerPolicy((0, 0))))
    supervisor = Supervisor(poller, db=db, dispatcher=Dispatcher(), notifier=notifier)
    supervisor.unblock_ladder = UnblockLadder(db, linear=linear, first_mate=mate, resumer=resume, repo_path=tmp_path)
    supervisor.lifecycle = Lifecycle.RUNNING
    supervisor.linear_healthy = True
    supervisor._fleet_tickets = (ticket,)
    wake = Wake(ticket=ticket, action="reengage", resume="run-1")

    async def scenario():
        try:
            await supervisor._handle_runner_result(wake, final)
            for _ in range(5):
                await asyncio.sleep(0.01)
                await supervisor._retry_stopped_builders()
                await asyncio.gather(*list(supervisor._dispatch_tasks.values()))
        finally:
            await linear.close()

    with caplog.at_level(logging.ERROR, logger="harbor.supervisor"):
        asyncio.run(scenario())
    receipts = db.connection.execute(
        "SELECT run_id FROM recovery_receipts").fetchall() if db.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='recovery_receipts'").fetchone() else []
    db.close()
    return prompts, queries, notifier.messages, [row[0] for row in receipts], caplog.records


@pytest.mark.parametrize("failing,disposition,mutation", [
    ("comment", {"disposition": "resolve", "answer": "Use blue", "citation": "ticket:MOCK-1:1",
                 "reason": "AC says blue."}, "HarborComment"),
    ("state", {"disposition": "escalate", "reason": "A human decision is required."}, "HarborProgress"),
])
def test_linear_write_failure_after_triage_is_not_replayed_as_context_failure(tmp_path, caplog, failing,
                                                                                disposition, mutation):
    from harbor.clients.linear import LinearClientError

    prompts, queries, messages, receipts, records = _post_triage_failure(
        tmp_path, caplog, failing=failing, disposition=disposition)
    assert len(prompts) == 1  # triage ran once and was not replayed
    assert len([q for q in queries if mutation in q]) == 1
    assert [r for r in receipts if r.startswith("triage:")] == []
    assert len(messages) == 1, messages
    assert "not triaged" not in messages[0] and "could not be verified" not in messages[0]
    assert "MOCK-1" in messages[0] and "after triage" in messages[0]
    assert any(record.exc_info and issubclass(record.exc_info[0], LinearClientError) for record in records)


def test_blocked_final_generic_error_is_logged_and_notified(tmp_path, caplog):
    import logging

    class Ladder:
        async def handle(self, ticket, run, final, *, blocker=None):
            raise KeyError("missing field")

    db = HarborDB(":memory:")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    notifier = Notifier()
    supervisor = Supervisor(SimpleNamespace(project_name="mock", config=SimpleNamespace()), db=db,
                            unblock_ladder=Ladder(), notifier=notifier)
    wake = Wake(ticket={"identifier": "MOCK-1"}, action="reengage", resume="logical")
    final = {"status": "blocked", "run_id": "logical", "blocker": {"kind": "ambiguous_ac", "question": "Which?"}}
    with caplog.at_level(logging.ERROR, logger="harbor.supervisor"):
        asyncio.run(supervisor._handle_runner_result(wake, final))
        asyncio.run(supervisor._handle_runner_result(wake, final))
    db.close()
    assert "missing field" in (supervisor.last_error or "")
    assert len(notifier.messages) == 1, notifier.messages  # deduplicated
    assert "MOCK-1" in notifier.messages[0] and "KeyError" in notifier.messages[0]
    assert "Linear" not in notifier.messages[0]
    assert any(record.exc_info and record.exc_info[0] is KeyError for record in caplog.records)


def test_context_notice_dedupes_on_cause_not_transport_detail(tmp_path):
    """A changing error text across replays must not page again."""
    from harbor.unblock import BlockerContextUnavailable

    db = HarborDB(":memory:")
    notifier = Notifier()
    supervisor = Supervisor(SimpleNamespace(project_name="mock", config=SimpleNamespace()), db=db, notifier=notifier)

    async def run():
        for detail in ("Linear request failed: timed out", "Linear request failed: connection reset"):
            await supervisor._report_unblock_failure("MOCK-1", "run-1", BlockerContextUnavailable(detail),
                                                     action="handle the blocked result")

    asyncio.run(run())
    db.close()
    assert len(notifier.messages) == 1 and "timed out" in notifier.messages[0]


def _context_recovers_then(tmp_path, *, later_issue, comment_ok):
    """First blocker-context fetch is malformed; later fetches return ``later_issue``.

    Drives the blocked final once, then 5 polls of ``_retry_stopped_builders``
    with ``BlockerPolicy((0, 0))``.  Returns First Mate prompts, Linear
    queries, operator notices, escalations and remaining recovery receipts.
    """
    from harbor.recovery import BlockerPolicy
    from harbor.runner.base import apply_phase9_migration
    from harbor.supervisor import Lifecycle

    queries = []
    fetches = []

    def handler(request):
        query = json.loads(request.content)["query"]
        queries.append(query)
        if "HarborComment" in query:
            if not comment_ok:
                return httpx.Response(200, json={"data": {"commentCreate": {"success": False, "comment": None}}})
            return httpx.Response(200, json={"data": {"commentCreate": {
                "success": True, "comment": {"id": f"c-{len(queries)}", "body": "", "createdAt": "2026-09-05T00:00:00Z"}}}})
        fetches.append(query)
        issue = mutate(complete_issue(), "labels", "missing pageInfo") if len(fetches) == 1 else later_issue()
        issue["description"] = "The button color must be blue."
        issue["state"] = {"name": "In Progress", "type": "started"}
        return httpx.Response(200, json={"data": {"issue": issue}})

    db = HarborDB(tmp_path / "ledger.sqlite")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    ticket = {"identifier": "MOCK-1", "id": "uuid-1", "title": "Color", "state": "In Progress",
              "owned": True, "hold": False}
    final = {"status": "blocked", "summary": "Need a color", "run_id": "run-1",
             "blocker": {"kind": "product_decision", "question": "What button color?"}}
    db.replace_tickets("mock", [ticket], emit_events=False)
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,worktree,session_id,deadline_seconds,final_json) VALUES (?,?,?,?,?,?,?)",
        ("run-1", "MOCK-1", "blocked", str(tmp_path), "session-1", 60, json.dumps(final)))
    db.connection.commit()
    linear = LinearClient(token="mock", transport=httpx.MockTransport(handler))
    prompts = []

    async def mate(prompt, **kwargs):
        prompts.append(prompt)
        return {"disposition": "resolve", "answer": "Use blue", "citation": "ticket:MOCK-1:1", "reason": "AC says blue."}

    async def resume(run_id, prompt):
        return True

    class Dispatcher:
        async def retry(self, *args):
            raise AssertionError("builder must not be retried")

    notifier = Notifier()
    poller = SimpleNamespace(project_name="mock", config=SimpleNamespace(blocker_policy=BlockerPolicy((0, 0))))
    supervisor = Supervisor(poller, db=db, dispatcher=Dispatcher(), notifier=notifier)
    supervisor.unblock_ladder = UnblockLadder(db, linear=linear, first_mate=mate, resumer=resume, repo_path=tmp_path)
    supervisor.lifecycle = Lifecycle.RUNNING
    supervisor.linear_healthy = True
    supervisor._fleet_tickets = (ticket,)
    wake = Wake(ticket=ticket, action="reengage", resume="run-1")

    async def scenario():
        try:
            await supervisor._handle_runner_result(wake, final)
            for _ in range(5):
                await asyncio.sleep(0.01)
                await supervisor._retry_stopped_builders()
                await asyncio.gather(*list(supervisor._dispatch_tasks.values()))
        finally:
            await linear.close()

    asyncio.run(scenario())
    escalations = [(row["status"], row["question"]) for row in db.escalations()]
    receipts = [tuple(row) for row in db.connection.execute("SELECT run_id,last_reason FROM recovery_receipts")]
    db.close()
    return prompts, queries, fetches, notifier.messages, escalations, receipts


def test_replay_that_fails_on_a_write_after_triage_is_not_replayed_again(tmp_path):
    """Review finding A: the context recovers, First Mate resolves, the comment write fails."""
    prompts, queries, fetches, messages, escalations, receipts = _context_recovers_then(
        tmp_path, later_issue=complete_issue, comment_ok=False)
    assert len(fetches) == 2  # the failed read, then one replay that got past it
    assert len(prompts) == 1
    assert len([q for q in queries if "HarborComment" in q]) == 1
    assert receipts == []
    assert not [m for m in messages if "exhausted" in m or "First Mate" in m], messages
    assert not [e for e in escalations if "retries exhausted" in e[1]], escalations
    assert len([m for m in messages if "after triage" in m]) == 1


def test_replay_that_finds_an_open_dependency_does_not_blame_first_mate(tmp_path):
    """Review finding B: the context recovers but the ticket still has a blocking dependency."""
    def dependent():
        issue = complete_issue()
        issue["inverseRelations"]["nodes"] = [
            {"type": "blocks", "issue": {"identifier": "MOCK-2", "state": {"type": "started"}}}]
        return issue

    prompts, queries, fetches, messages, escalations, receipts = _context_recovers_then(
        tmp_path, later_issue=dependent, comment_ok=True)
    assert len(fetches) == 2
    assert prompts == []
    assert all("mutation" not in q for q in queries)
    assert receipts == []
    assert not [m for m in messages if "exhausted" in m or "First Mate" in m], messages
    assert escalations == []
    assert len([m for m in messages if "could not be verified" in m]) == 1  # only the real context failure


def test_context_retry_that_succeeds_is_not_replayed(tmp_path):
    """The context recovers and triage succeeds: one First Mate call, no lingering receipt."""
    prompts, queries, fetches, messages, escalations, receipts = _context_recovers_then(
        tmp_path, later_issue=complete_issue, comment_ok=True)
    assert len(fetches) == 2
    assert len(prompts) == 1
    assert len([q for q in queries if "HarborComment" in q]) == 1
    assert receipts == []
    assert not [m for m in messages if "exhausted" in m], messages


def test_first_mate_retry_that_succeeds_is_not_replayed(tmp_path):
    """A First Mate triage retry that succeeds ends its receipt (also wrong on main)."""
    from harbor.first_mate import FirstMateError
    from harbor.recovery import BlockerPolicy
    from harbor.runner.base import apply_phase9_migration
    from harbor.supervisor import Lifecycle

    db = HarborDB(tmp_path / "ledger.sqlite")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    ticket = {"identifier": "MOCK-1", "id": "uuid-1", "title": "T", "description": "Color must be blue.",
              "state": "In Progress", "owned": True, "hold": False}
    final = {"status": "blocked", "run_id": "run-1", "blocker": {"kind": "product_decision", "question": "What color?"}}
    db.replace_tickets("mock", [ticket], emit_events=False)
    db.connection.execute(
        "INSERT INTO runs(id,ticket,status,worktree,session_id,deadline_seconds,final_json) VALUES (?,?,?,?,?,?,?)",
        ("run-1", "MOCK-1", "blocked", str(tmp_path), "session-1", 60, json.dumps(final)))
    db.connection.commit()
    calls = []

    async def mate(prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            raise FirstMateError("First Mate was killed")
        return {"disposition": "resolve", "answer": "Use blue", "citation": "ticket:MOCK-1:1", "reason": "AC says blue."}

    async def resume(run_id, prompt):
        return True

    class Dispatcher:
        async def retry(self, *args):
            raise AssertionError("builder must not be retried")

    poller = SimpleNamespace(project_name="mock", config=SimpleNamespace(blocker_policy=BlockerPolicy((0, 0))))
    supervisor = Supervisor(poller, db=db, dispatcher=Dispatcher())
    supervisor.unblock_ladder = UnblockLadder(db, first_mate=mate, resumer=resume, repo_path=tmp_path)
    supervisor.lifecycle = Lifecycle.RUNNING
    supervisor.linear_healthy = True
    supervisor._fleet_tickets = (ticket,)

    async def scenario():
        await supervisor._handle_runner_result(Wake(ticket=ticket, action="reengage", resume="run-1"), final)
        for _ in range(5):
            await asyncio.sleep(0.01)
            await supervisor._retry_stopped_builders()
            await asyncio.gather(*list(supervisor._dispatch_tasks.values()))

    asyncio.run(scenario())
    escalations = [(row["status"], row["question"]) for row in db.escalations()]
    db.close()
    assert len(calls) == 2  # the killed attempt and one successful retry
    assert escalations == [("resume_pending", "What color?")]
