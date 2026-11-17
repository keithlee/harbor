"""Adversarial regressions from the configurable-workflows review (Nov 17).

Tests marked ``xfail(strict=True)`` reproduce a confirmed defect.  They pass
today *because* the defect exists; fixing the defect turns them into an
XPASS, which strict mode reports as a failure so the marker gets removed and
the test becomes a permanent guard.  Unmarked tests pin adversarial cases the
code already handles correctly.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harbor.db import HarborDB
from harbor.digest import render_digest
from harbor.pr_events import NEW_COMMENT_VERSION, PREventError, PREventProcessor, _owner_merge_approval

HEAD = "a" * 40
PR_URL = "https://github.com/acme/demo/pull/7"
TICKET = {"identifier": "DEMO-7", "pr_urls": [PR_URL], "run_id": "builder-7"}


def facts(*, head: str = HEAD, comments: list[dict[str, Any]] | None = None, state: str = "OPEN") -> dict[str, Any]:
    return {"number": 7, "url": PR_URL, "createdAt": "2026-09-02T10:00:00Z", "headRefOid": head,
            "state": state, "ci": {"state": "green"}, "comments": comments or []}


def owner_comment(body: str = "lgtm merge", **extra) -> dict[str, Any]:
    return {"id": "approval", "body": body, "surface": "issue", "author": "owner",
            "author_association": "OWNER", "created_at": "2026-09-02T10:01:00Z", **extra}


PREVIOUS = {"head_sha": HEAD, "observed_at": "2026-09-02T10:00:00+00:00"}


# ---------------------------------------------------------------- confirmed defects


REVIEW = {"id": "c-1", "body": "Please fix the timeout.", "surface": "issue", "author": "reviewer",
          "created_at": "2026-09-02T10:01:00Z"}
FAILING_RUN = {"status": "failed", "terminal_reason": "unexpected_exit"}


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def failing_processor(tmp_path: Path, monkeypatch, result: dict | None = None):
    db = HarborDB(tmp_path / "harbor.sqlite")
    calls: list[Any] = []
    clock = Clock()
    monkeypatch.setattr("harbor.pr_events.time.time", clock)

    async def builder(event):
        calls.append(event)
        return result or {"status": "failed", "summary": "builder crashed"}

    return db, PREventProcessor(db, project="demo", builder=builder), calls, clock


def per_event(calls: list[Any]) -> dict[str, int]:
    """Builder offers per event: the PR yields PR_CREATED plus one comment event."""
    counts: dict[str, int] = {}
    for call in calls:
        counts[call["event_type"]] = counts.get(call["event_type"], 0) + 1
    return counts


def poll(processor, run=FAILING_RUN) -> Exception | None:
    try:
        asyncio.run(processor.process(TICKET, facts(comments=[REVIEW]), run=run))
    except PREventError as exc:
        return exc
    return None


def test_failed_pr_event_delivery_is_bounded(tmp_path: Path, monkeypatch) -> None:
    """F1: a builder that keeps failing is no longer resumed on every poll."""
    from harbor.pr_events import PREventDeliveryExhausted

    db, processor, calls, clock = failing_processor(tmp_path, monkeypatch)
    errors = []
    for _poll in range(60):  # one hour of one-minute polls
        errors.append(poll(processor))
        clock.now += 60
    db.close()
    # Each event: one first offer plus one retry per configured delay (30s, 120s, 300s).
    assert per_event(calls) == {"PR_CREATED": 4, NEW_COMMENT_VERSION: 4}, per_event(calls)
    assert sum(isinstance(error, PREventDeliveryExhausted) for error in errors) == 2


def test_failed_pr_event_backs_off_between_attempts(tmp_path: Path, monkeypatch) -> None:
    db, processor, calls, clock = failing_processor(tmp_path, monkeypatch)
    poll(processor)  # PR_CREATED fails first and stops this poll
    poll(processor)  # its backoff lets the comment event be offered
    clock.now += 29
    poll(processor)
    assert per_event(calls) == {"PR_CREATED": 1, NEW_COMMENT_VERSION: 1}, "retried before the 30s backoff elapsed"
    clock.now += 1
    poll(processor)
    assert per_event(calls)["PR_CREATED"] == 2
    db.close()


def test_empty_recovery_policy_parks_after_the_first_failure(tmp_path: Path, monkeypatch) -> None:
    from harbor.pr_events import PREventDeliveryExhausted

    db, processor, calls, clock = failing_processor(tmp_path, monkeypatch)
    processor.retry_delays = ()
    errors = [poll(processor) for _ in range(2)]
    assert all(isinstance(error, PREventDeliveryExhausted) for error in errors)
    assert [(error.ticket, error.attempts) for error in errors] == [("DEMO-7", 1), ("DEMO-7", 1)]
    clock.now += 3600
    assert poll(processor) is None and len(calls) == 2
    db.close()


def test_usage_limited_pr_event_failure_uses_no_retry_budget(tmp_path: Path, monkeypatch) -> None:
    from harbor.runner.quota import USAGE_LIMIT_ERROR

    db, processor, calls, clock = failing_processor(
        tmp_path, monkeypatch, {"status": "failed", "summary": "limit", "error": USAGE_LIMIT_ERROR})
    processor.retry_delays = ()
    for _ in range(3):
        error = poll(processor)
        assert type(error) is PREventError  # pending, not exhausted
    assert per_event(calls) == {"PR_CREATED": 3}
    assert db.connection.execute("SELECT count(*) FROM pr_event_attempts").fetchone()[0] == 0
    db.close()


def test_successful_handoff_clears_the_failure_budget(tmp_path: Path, monkeypatch) -> None:
    db, processor, calls, clock = failing_processor(tmp_path, monkeypatch)
    poll(processor)
    clock.now += 30

    async def recovered(event):
        calls.append(event)
        return {"status": "completed", "summary": "replied"}

    processor.builder = recovered
    assert poll(processor) is None
    assert per_event(calls) == {"PR_CREATED": 2, NEW_COMMENT_VERSION: 1}
    assert db.connection.execute("SELECT count(*) FROM pr_event_attempts").fetchone()[0] == 0
    db.close()


def test_operator_retry_rearms_a_parked_event(tmp_path: Path, monkeypatch) -> None:
    db, processor, calls, clock = failing_processor(tmp_path, monkeypatch)
    processor.retry_delays = ()
    poll(processor)
    poll(processor)
    assert per_event(calls) == {"PR_CREATED": 1, NEW_COMMENT_VERSION: 1}
    poll(processor)
    assert len(calls) == 2, "both events are parked"
    assert processor.rearm_failed_events(ticket="DEMO-7", project="demo") == 2
    poll(processor)
    assert per_event(calls)["PR_CREATED"] == 2
    assert processor.rearm_failed_events(ticket="OTHER-1", project="demo") == 0
    db.close()


def test_supervisor_notifies_once_when_pr_event_delivery_is_exhausted(tmp_path: Path) -> None:
    from harbor.config.projects import ProjectConfig
    from harbor.pr_events import PREventDeliveryExhausted
    from harbor.poll.linear_poller import PollResult
    from harbor.supervisor import Supervisor

    ticket = {"identifier": "DEMO-7", "state": "In Progress", "owned": True, "hold": False, "pr_urls": [PR_URL]}

    class Poller:
        project_name = "demo"
        config = ProjectConfig(name="demo", linear_project_slug="demo", heartbeat_seconds=1)

        async def poll(self):
            return PollResult((dict(ticket),), "snapshot", False, ())

    class GitHub:
        async def poll(self, tickets):
            return [{"ticket": "DEMO-7", **facts(comments=[REVIEW])}]

    class Processor:
        retry_delays = (30,)

        def process(self, ticket, facts, *, run=None, builder=None):
            raise PREventDeliveryExhausted("DEMO-7", "event-key-1", 2)

    notes: list[str] = []

    class Pager:
        async def notify(self, text):
            notes.append(text)

    processor = Processor()
    supervisor = Supervisor(Poller(), github_poller=GitHub(), pr_event_processor=processor, notifier=Pager())
    supervisor.blocker_policy = replace(supervisor.blocker_policy, retry_delays=(5, 10))

    async def scenario():
        await supervisor.refresh()
        await supervisor.refresh()

    asyncio.run(scenario())
    assert notes == ["DEMO-7: builder failed 2 times handling a PR event; inspect the retained session, then /retry DEMO-7."]
    assert processor.retry_delays == (5, 10), "the processor uses the project's recovery policy"


def quota_supervisor(tmp_path: Path, *, paused: bool = False, reset: float = 1):
    from harbor.recovery import defer_quota
    from harbor.runner.base import apply_phase4_migration, apply_phase9_migration
    from harbor.supervisor import Lifecycle, Supervisor
    from harbor.wake import apply_phase3_migration

    db = HarborDB(tmp_path / "harbor.db")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    ticket = {"identifier": "MOCK-1", "title": "x", "state": "In Progress", "owned": True, "hold": False}
    db.replace_tickets("mock", [ticket], emit_events=False)
    db.connection.execute(
        """INSERT INTO runs(id,ticket,status,terminal_reason,session_id,worktree,deadline_seconds,paused_at)
        VALUES ('run-1','MOCK-1','failed','usage_limit_exceeded','s',?,60,?)""",
        (str(tmp_path), "2026-11-17T10:00:00+00:00" if paused else None))
    defer_quota(db.connection, "run-1", "codex", reset, 0)
    calls: list[str] = []
    notes: list[str] = []

    class Dispatcher:
        async def retry(self, identifier, instruction):
            calls.append(identifier)

    supervisor = Supervisor(SimpleNamespace(project_name="mock"), db=db, dispatcher=Dispatcher())
    supervisor.blocker_policy = replace(supervisor.blocker_policy, retry_delays=())  # crash recovery disabled
    supervisor.lifecycle = Lifecycle.RUNNING
    supervisor.linear_healthy = True
    supervisor._fleet_tickets = (ticket,)

    async def notify(key, message):
        notes.append(message)

    supervisor._notify_operator = notify
    return db, supervisor, calls, notes


def run_retry_loop(supervisor, times: int = 3) -> None:
    async def scenario():
        for _ in range(times):
            await supervisor._retry_stopped_builders()
        await asyncio.gather(*supervisor._dispatch_tasks.values())

    asyncio.run(scenario())


def test_usage_limited_run_resumes_at_reset_even_with_recovery_disabled(tmp_path: Path) -> None:
    """F2: a usage-limit wait is not crash recovery; it resumes at the provider reset."""
    db, supervisor, calls, notes = quota_supervisor(tmp_path)
    run_retry_loop(supervisor)
    db.close()
    assert calls == ["MOCK-1"] and notes == []


def test_usage_limited_run_waits_for_the_reset(tmp_path: Path) -> None:
    import time

    db, supervisor, calls, notes = quota_supervisor(tmp_path, reset=time.time() + 3600)
    run_retry_loop(supervisor)
    db.close()
    assert calls == [] and notes == []


def test_paused_usage_limited_run_notifies_with_the_reset_time(tmp_path: Path) -> None:
    db, supervisor, calls, notes = quota_supervisor(tmp_path, paused=True, reset=1_800_000_000)
    run_retry_loop(supervisor)
    db.close()
    assert calls == []
    assert notes and notes[0] == (
        "MOCK-1: codex usage limit; resets 2027-01-15T08:00+00:00 (confirmed); "
        "automatic resume is blocked because the run is paused; run /retry MOCK-1 after then."
    )


def test_compact_status_labels_a_usage_limited_builder() -> None:
    status = {"project": "demo", "tickets": [{"identifier": "DEMO-1", "state": "In Progress", "owned": True}],
              "runs": [{"id": "r1", "ticket": "DEMO-1", "status": "failed", "terminal_reason": "usage_limit_exceeded"}]}
    assert "builder usage-limited" in render_digest(status, detailed=False)


@pytest.mark.xfail(strict=True, reason="F3: compact /status and the dashboard aggregate every historical "
                   "builder run, so finished failures read as current state")
def test_compact_status_reports_only_the_current_builder_state() -> None:
    status = {"project": "demo", "tickets": [{"identifier": "DEMO-1", "state": "In Progress", "owned": True}],
              "runs": [{"id": "r1", "ticket": "DEMO-1", "status": "failed"},
                       {"id": "r2", "ticket": "DEMO-1", "status": "terminated"},
                       {"id": "r3", "ticket": "DEMO-1", "status": "running"}]}
    line = next(line for line in render_digest(status, detailed=False).splitlines() if line.startswith("DEMO-1"))
    assert "failed" not in line and "terminated" not in line, line


@pytest.mark.xfail(strict=True, reason="F4: the ticket-lookup pattern is case-insensitive, so ordinary "
                   "tokens such as gpt-5, utf-8 and sha-256 trigger Linear lookups")
def test_question_lookup_ignores_non_ticket_tokens() -> None:
    from harbor.console import Console

    looked_up: list[str] = []

    class Supervisor:
        async def fresh_digest(self):
            return "digest"

        async def ticket_status_context(self, identifier):
            looked_up.append(identifier)
            return None

    class Mate:
        async def answer(self, question, *, digest):
            return "ok"

    console = Console(Supervisor(), first_mate=Mate())
    asyncio.run(console._answer_question("is gpt-5 fine with utf-8 and sha-256 for ABC-81?"))
    assert looked_up == ["ABC-81"]


@pytest.mark.xfail(strict=True, reason="F5: /api/chat is tokenless yet returns raw First Mate/Codex "
                   "stderr in its 503 body")
def test_dashboard_chat_does_not_echo_provider_stderr() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from harbor.dashboard import create_app
    from harbor.first_mate import FirstMateError

    async def ask(question, *, digest, interactive):
        raise FirstMateError("Error loading config /Users/someone/.codex/config.toml: bad key")

    console = SimpleNamespace(supervisor=SimpleNamespace(), ask=ask)

    async def scenario() -> str:
        import harbor.dashboard as dashboard

        original = dashboard._snapshot, dashboard.render_digest
        dashboard._snapshot, dashboard.render_digest = (lambda _: None), (lambda _: "digest")
        try:
            app = create_app(console, port=8765, token="t")
            async with TestClient(TestServer(app)) as client:
                async with client.post("/api/chat", json={"message": "hi"},
                                       headers={"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"}) as response:
                    return await response.text()
        finally:
            dashboard._snapshot, dashboard.render_digest = original

    assert "/Users/someone" not in asyncio.run(scenario())


# ---------------------------------------------------------------- merge-approval guards that hold


@pytest.mark.parametrize(
    ("label", "ticket", "fact_overrides", "comment", "previous"),
    [
        ("baseline approval", TICKET, {}, owner_comment(), PREVIOUS),
        ("bot owner", TICKET, {}, owner_comment(is_bot=True), PREVIOUS),
        ("anonymous author", TICKET, {}, owner_comment(author=""), PREVIOUS),
        ("member not owner", TICKET, {}, owner_comment(author_association="MEMBER"), PREVIOUS),
        ("review-thread surface", TICKET, {}, owner_comment(surface="review_thread"), PREVIOUS),
        ("plain merge without lgtm", TICKET, {}, owner_comment("merge"), PREVIOUS),
        ("lgtm without merge", TICKET, {}, owner_comment("lgtm"), PREVIOUS),
        ("closed PR", TICKET, {"state": "CLOSED"}, owner_comment(), PREVIOUS),
        ("merged PR", TICKET, {"state": "MERGED"}, owner_comment(), PREVIOUS),
        ("short head", TICKET, {"headRefOid": "abc123"}, owner_comment(), {**PREVIOUS, "head_sha": "abc123"}),
        ("uppercase head", TICKET, {"headRefOid": "A" * 40}, owner_comment(), {**PREVIOUS, "head_sha": "A" * 40}),
        ("held ticket", {**TICKET, "hold": True}, {}, owner_comment(), PREVIOUS),
        ("blocked ticket", {**TICKET, "blocked_by": ["DEMO-1"]}, {}, owner_comment(), PREVIOUS),
        ("no previous observation", TICKET, {}, owner_comment(), None),
        ("head changed since previous poll", TICKET, {"headRefOid": "b" * 40}, owner_comment(), PREVIOUS),
        ("comment predates previous poll", TICKET, {}, owner_comment(created_at="2026-09-02T09:59:59Z"), PREVIOUS),
        ("comment same second as poll", TICKET, {}, owner_comment(created_at="2026-09-02T10:00:00Z"), PREVIOUS),
        ("naive comment timestamp", TICKET, {}, owner_comment(created_at="2026-09-02T10:01:00"), PREVIOUS),
        ("garbage timestamp", TICKET, {}, owner_comment(created_at="yesterday"), PREVIOUS),
        ("missing timestamp", TICKET, {}, {k: v for k, v in owner_comment().items() if k != "created_at"}, PREVIOUS),
        ("edit after poll re-approves", TICKET, {},
         owner_comment(created_at="2026-09-01T00:00:00Z", updated_at="2026-09-02T10:05:00Z"), PREVIOUS),
    ],
)
def test_owner_merge_approval_matrix(label, ticket, fact_overrides, comment, previous) -> None:
    expected = label in {"baseline approval", "edit after poll re-approves"}
    assert _owner_merge_approval(ticket, {**facts(), **fact_overrides}, comment, previous) is expected, label


def test_withdrawn_or_edited_approval_is_not_delivered(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")
    delivered: list[Any] = []

    async def builder(event):
        delivered.append(event)
        return {"status": "completed", "summary": "handled"}

    processor = PREventProcessor(db, project="demo", builder=builder)
    processor.observe(TICKET, facts())
    db.connection.execute("UPDATE github_pr_states SET observed_at='2026-09-02T10:00:00+00:00'")
    db.connection.commit()
    processor.observe(TICKET, facts(comments=[owner_comment()]))
    # The owner edits the approval away before Harbor delivers it.
    asyncio.run(processor.process(TICKET, facts(comments=[owner_comment("never mind",
                                                                        updated_at="2026-09-02T10:03:00Z")])))
    approvals = [event for event in delivered
                 if event["event_type"] == NEW_COMMENT_VERSION and event["payload"].get("merge_approval")]
    assert approvals == []
    db.close()


def test_usage_limited_run_suppresses_pr_event_delivery(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")
    delivered: list[Any] = []

    async def builder(event):
        delivered.append(event)

    processor = PREventProcessor(db, project="demo", builder=builder)
    review = {"id": "c-1", "body": "fix it", "surface": "issue", "author": "r", "created_at": "2026-09-02T10:01:00Z"}
    asyncio.run(processor.process(TICKET, facts(comments=[review]),
                                  run={"status": "failed", "terminal_reason": "usage_limit_exceeded"}))
    assert delivered == []
    db.close()
