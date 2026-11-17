"""Loop-closing guarantees: every builder result is routed, every park is
answerable, and a PR is not handed off with unanswered feedback unnoticed."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB
from harbor.digest import render_digest
from harbor.poll.linear_poller import PollResult
from harbor.pr_events import (
    PREventProcessor,
    handoff_gate_text,
    has_handoff_gaps,
    risk_analysis_marker,
)
from harbor.runner.base import apply_phase4_migration, normalize_final_result
from harbor.supervisor import Supervisor, Wake, WorktreeDispatcher
from harbor.unblock import UnblockLadder


PR_URL = "https://github.com/example/SampleProject/pull/7"
PRODUCT_QUESTION = "Choose a new product behavior for empty state"


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


class IdlePoller:
    project_name = "SampleProject"
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", heartbeat_seconds=1)

    async def poll(self) -> PollResult:
        return PollResult((), "snapshot-1", False, ())


class Pager:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def notify(self, reason: str) -> None:
        self.sent.append(reason)


class LinearFake:
    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.states: list[tuple[str, str]] = []

    async def create_comment(self, issue_id: str, body: str) -> dict[str, Any]:
        self.comments.append((issue_id, body))
        return {"comment": {"id": f"comment-{len(self.comments)}"}}

    async def update_issue_state(self, issue_id: str, state: str) -> dict[str, Any]:
        self.states.append((issue_id, state))
        return {"success": True}


def source_comment(body: str = "Please handle the timeout.", updated: str = "2026-09-02T10:01:00Z") -> dict[str, Any]:
    return {"id": "issue-1", "surface": "issue", "body": body, "updated_at": updated, "is_bot": False}


def pr_facts(*, head: str = "sha-a", body: str = "No risk section yet.", comments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"number": 7, "url": PR_URL, "head_sha": head, "body": body, "comments": [source_comment()] if comments is None else comments}


def later(minutes: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def seed_ticket(db: HarborDB) -> None:
    db.replace_tickets(
        "SampleProject",
        [{"identifier": "SAMPLE-42", "id": "linear-42", "title": "Empty state", "state": "Human Review", "pr_urls": [PR_URL]}],
    )


# 1. PR-event turns reach the ladder -------------------------------------------------


def test_blocked_result_from_a_pr_event_turn_reaches_the_unblock_ladder(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    blocked = {
        "status": "blocked",
        "outcome": None,
        "waiting_on": "human",
        "pr_url": None,
        "summary": "Needs a product call",
        "next_action": None,
        "blocker": {"kind": "product_decision", "question": PRODUCT_QUESTION, "options": [], "recommendation": None, "evidence": None},
    }

    class Dispatcher:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        async def handle_pr_event(self, event: dict[str, Any]) -> dict[str, Any]:
            self.events.append(event)
            return blocked

    class Ladder:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, Any]] = []

        async def handle(self, ticket: Any, run: Any, final: Any, *, blocker: Any = None) -> None:
            self.calls.append((ticket["identifier"], blocker["question"]))

    dispatcher, ladder = Dispatcher(), Ladder()
    supervisor = Supervisor(
        IdlePoller(), db=db, dispatcher=dispatcher, unblock_ladder=ladder,
        pr_event_processor=PREventProcessor(db, project="SampleProject"),
    )
    try:
        run(supervisor._process_pr_events({"identifier": "SAMPLE-42", "owned": True, "pr_urls": [PR_URL]}, pr_facts()))
        assert [event["event_type"] for event in dispatcher.events] == ["PR_CREATED", "NEW_COMMENT_VERSION"]
        assert ladder.calls == [("SAMPLE-42", PRODUCT_QUESTION)] * 2
    finally:
        db.close()


# 2. A completed park keeps waiting on the human --------------------------------------


def test_completed_park_with_a_pr_stays_waiting_on_the_human() -> None:
    parked = normalize_final_result(
        {"status": "completed", "outcome": "parked", "waiting_on": "human", "pr_url": PR_URL, "summary": "Merge is yours"}
    )
    assert (parked["status"], parked["outcome"], parked["waiting_on"]) == ("waiting", "parked", "human")
    opened = normalize_final_result({"status": "completed", "outcome": "pr_open", "pr_url": PR_URL, "summary": "PR up"})
    assert (opened["status"], opened["waiting_on"]) == ("waiting", "review"), "an open PR still waits on review"
    merged = normalize_final_result(
        {"status": "completed", "outcome": "parked", "waiting_on": "human", "pr_url": PR_URL, "summary": "s"},
        observed={"url": PR_URL, "merged": True},
    )
    assert merged["outcome"] == "merged", "an observed merge still wins over the park claim"


# 3 and 4. Escalations are durable, identified, and answerable ------------------------


def product_ladder(db: HarborDB, linear: LinearFake, notifier: Any, tmp_path: Path) -> UnblockLadder:
    return UnblockLadder(db, linear=linear, notifier=notifier, repo_path=tmp_path, remediation_executor=lambda command: False)


def test_rung_two_records_the_escalation_before_a_failing_notification(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    linear = LinearFake()

    async def broken_notifier(reason: str) -> None:
        raise FileNotFoundError("osascript")

    blocker = {"blocker": {"kind": "product_decision", "question": PRODUCT_QUESTION}}
    try:
        result = run(product_ladder(db, linear, broken_notifier, tmp_path).handle({"identifier": "SAMPLE-42", "id": "linear-42"}, {"id": "builder"}, blocker))
        assert result.rung == 2 and result.action == "park" and result.notified is False
        [row] = db.open_escalations()
        assert (row["status"], row["notified"], row["reason"]) == ("open", 0, "genuinely new product decision")
        assert f"escalation #{row['id']}" in linear.comments[0][1]
        assert f"/answer {row['id']} <answer>" in linear.comments[0][1]
        assert "Reply on this ticket" in linear.comments[0][1]

        pager = Pager()
        run(product_ladder(db, LinearFake(), pager, tmp_path).handle({"identifier": "SAMPLE-43", "id": "linear-43"}, {"id": "builder"}, blocker))
        second = db.open_escalations(["SAMPLE-43"])[0]
        assert second["notified"] == 1 and f"escalation #{second['id']}" in pager.sent[0]
    finally:
        db.close()


def test_status_digest_lists_open_escalations_with_what_was_tried(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    try:
        seed_ticket(db)
        db.record_escalation("SAMPLE-42", "run-1", "ambiguous_ac", "Which timeout applies?", 1)
        escalation = db.record_escalation(
            "SAMPLE-42", "run-1", "ambiguous_ac", "Which timeout applies?", 2,
            reason="no authoritative answer found", searched=["AGENTS.md:3", "ticket:SAMPLE-42"],
        )
        db.record_escalation("SAMPLE-99", "run-9", "env_blocker", "Another project's question", 2)
        digest = render_digest(Supervisor(IdlePoller(), db=db).status())
        assert "Escalations: 1 open" in digest
        assert (
            f"Escalation #{escalation['id']}  SAMPLE-42  ambiguous_ac  Which timeout applies?  "
            "(tried rung 0 and rung 1; no authoritative answer found; searched AGENTS.md:3, ticket:SAMPLE-42)  "
            f"→ /answer {escalation['id']} <answer>"
        ) in digest
        assert "SAMPLE-99" not in digest, "an escalation outside this project is not answerable here"
    finally:
        db.close()


def test_legacy_rows_migrate_and_a_newer_ladder_result_supersedes_an_open_one(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE escalations (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket TEXT, run_id TEXT, kind TEXT NOT NULL, "
        "question TEXT NOT NULL, rung_resolved INTEGER NOT NULL, citation TEXT, answer TEXT, "
        "notified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO escalations(ticket, kind, question, rung_resolved, created_at) VALUES ('SAMPLE-1', 'product_decision', 'Old park', 2, 'then')")
    connection.execute("INSERT INTO escalations(ticket, kind, question, rung_resolved, created_at) VALUES ('SAMPLE-1', 'env_blocker', 'Old fix', 1, 'then')")
    connection.commit()
    connection.close()

    db = HarborDB(path)
    try:
        assert [row["question"] for row in db.open_escalations()] == ["Old park"]
        db.record_escalation("SAMPLE-1", "run-2", "env_blocker", "Cannot find module", 0)
        assert db.open_escalations() == []
        assert [row["status"] for row in db.escalations()] == ["superseded", "resolved", "resolved"]
    finally:
        db.close()


def test_linear_reply_answers_the_open_escalation_and_becomes_reusable(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")

    class Resumer:
        async def resume(self, wake: Wake) -> dict[str, Any]:
            return {"status": "waiting", "outcome": "pr_open", "waiting_on": "review", "summary": "continuing"}

    try:
        seed_ticket(db)
        escalation = db.record_escalation("SAMPLE-42", "run-1", "product_decision", PRODUCT_QUESTION, 2)
        wake = Wake(
            ticket={"identifier": "SAMPLE-42"}, action="reengage", resume="run-1", instruction="reply",
            reason="linear-comment-version",
            facts={"linear_comment": {"id": "c-9", "body": "Answer: Use the illustrated empty state.", "created_at": later()}},
        )
        run(Supervisor(IdlePoller(), db=db, dispatcher=Resumer())._deliver_wake(wake))
        resolution = db.find_resolution("product_decision", PRODUCT_QUESTION, project="SampleProject")
        assert resolution["answer"] == "Use the illustrated empty state."
        assert resolution["citation"] == "linear:SAMPLE-42#comment-c-9"
        [row] = db.escalations()
        assert row["id"] == escalation["id"] and row["status"] == "answered" and row["resolution_id"] == resolution["id"]
    finally:
        db.close()


# 5. Handoff gate ----------------------------------------------------------------------


def test_handoff_gaps_track_newest_comment_versions_and_current_head_risk(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    processor = PREventProcessor(db, project="SampleProject")
    ticket = {"identifier": "SAMPLE-42", "pr_urls": [PR_URL]}
    try:
        processor.observe(ticket, pr_facts())
        gaps = processor.handoff_gaps("SAMPLE-42", PR_URL, head_sha="sha-a", comment_ids={"issue-1"})
        assert gaps["summary"] == "1 unreplied external comment; risk analysis missing"

        edited = source_comment("Please handle the timeout and add a test.", "2026-09-02T10:05:00Z")
        processor.observe(ticket, pr_facts(comments=[edited]))
        gaps = processor.handoff_gaps("SAMPLE-42", PR_URL, head_sha="sha-a", comment_ids={"issue-1"})
        [pending] = gaps["unreplied"]
        gate = handoff_gate_text([gaps])
        assert pending["reply_marker"] in gate and "Do not merge" in gate

        risk = f"## Risk Analysis\nLow\n{risk_analysis_marker('sha-a')}"
        reply = {"id": "reply-1", "surface": "issue", "body": f"Added the test. {pending['reply_marker']}"}
        processor.observe(ticket, pr_facts(body=risk, comments=[edited, reply]))
        gaps = processor.handoff_gaps("SAMPLE-42", PR_URL, head_sha="sha-a", comment_ids={"issue-1", "reply-1"})
        assert not has_handoff_gaps(gaps) and gaps["summary"] == "clear" and handoff_gate_text([gaps]) == ""

        processor.observe(ticket, pr_facts(head="sha-b", body=risk, comments=[edited, reply]))
        assert processor.handoff_gaps("SAMPLE-42", PR_URL, head_sha="sha-b")["risk"] == "stale"
    finally:
        db.close()


def test_park_with_open_gaps_escalates_and_a_clear_park_pages_merge_ready(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    processor = PREventProcessor(db, project="SampleProject")
    linear, pager = LinearFake(), Pager()
    parked = {"status": "waiting", "outcome": "parked", "waiting_on": "human", "pr_url": PR_URL, "summary": "Merge is yours"}
    wake = Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage")
    try:
        seed_ticket(db)
        processor.observe({"identifier": "SAMPLE-42", "pr_urls": [PR_URL]}, pr_facts())
        ladder = UnblockLadder(db, linear=linear, notifier=pager)
        supervisor = Supervisor(IdlePoller(), db=db, pr_event_processor=processor, unblock_ladder=ladder, notifier=pager)

        run(supervisor._handle_runner_result(wake, parked))
        run(supervisor._handle_runner_result(wake, parked))
        [gate] = db.open_escalations()
        assert gate["kind"] == "handoff_gate"
        assert gate["question"] == (
            f"SAMPLE-42 parked for human review {PR_URL} with 1 unreplied external comment; risk analysis missing."
        )
        assert len(linear.comments) == 1 and linear.comments[0][0] == "linear-42", "an identical gate pages once"
        assert linear.states == [], "the PR is already in human hands; Linear state is left alone"
        assert len(pager.sent) == 1 and f"escalation #{gate['id']}" in pager.sent[0]
        assert "handoff gate: 1 unreplied external comment; risk analysis missing" in render_digest(
            {"tickets": [{"identifier": "SAMPLE-42", "state": "Human Review", "handoff": [processor.handoff_gaps("SAMPLE-42", PR_URL)]}]}
        )

        event = next(row for row in processor.events() if row["event_type"] == "NEW_COMMENT_VERSION")
        marker = event["payload"]["reply_marker"]
        risk = f"## Risk Analysis\nLow\n{risk_analysis_marker('sha-a')}"
        processor.observe(
            {"identifier": "SAMPLE-42", "pr_urls": [PR_URL]},
            pr_facts(body=risk, comments=[source_comment(), {"id": "reply-1", "surface": "issue", "body": f"Done. {marker}"}]),
        )
        run(supervisor._handle_runner_result(wake, parked))
        assert db.open_escalations() == [], "the builder's clean park closes the gate"
        assert pager.sent[-1] == f"SAMPLE-42 is merge-ready and waiting on you: {PR_URL}"
    finally:
        db.close()


def test_handoff_gate_rereads_github_so_a_just_posted_reply_is_not_escalated(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    processor = PREventProcessor(db, project="SampleProject")
    ticket = {"identifier": "SAMPLE-42", "pr_urls": [PR_URL]}
    pager = Pager()
    try:
        seed_ticket(db)
        processor.observe(ticket, pr_facts())
        event = next(row for row in processor.events() if row["event_type"] == "NEW_COMMENT_VERSION")
        replied = {
            **pr_facts(
                body=f"## Risk Analysis\nLow\n{risk_analysis_marker('sha-a')}",
                comments=[source_comment(), {"id": "reply-1", "surface": "issue", "body": f"Done. {event['payload']['reply_marker']}"}],
            ),
            "ticket": "SAMPLE-42",
        }

        class GitHub:
            async def poll(self, tickets: Any) -> list[dict[str, Any]]:
                return [replied]

        supervisor = Supervisor(
            IdlePoller(), db=db, github_poller=GitHub(), pr_event_processor=processor,
            unblock_ladder=UnblockLadder(db, linear=LinearFake(), notifier=pager), notifier=pager,
        )
        parked = {"status": "waiting", "outcome": "parked", "waiting_on": "human", "pr_url": PR_URL, "summary": "Merge is yours"}
        run(supervisor._handle_runner_result(Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage"), parked))
        assert db.open_escalations() == []
        assert pager.sent == [f"SAMPLE-42 is merge-ready and waiting on you: {PR_URL}"]
    finally:
        db.close()


def test_every_same_session_resume_carries_the_open_handoff_gate() -> None:
    class Runner:
        def __init__(self) -> None:
            self.instructions: list[str] = []

        async def resume(self, run_id: str, instruction: str) -> dict[str, Any]:
            self.instructions.append(instruction)
            return {"status": "waiting", "summary": "ok"}

    runner = Runner()
    dispatcher = WorktreeDispatcher(object(), lambda worktree, wake: runner, project="SampleProject")
    dispatcher.runners["SAMPLE-42"] = runner
    dispatcher.handoff_gate = lambda identifier: f"GATE for {identifier}"
    run(dispatcher.resume(Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage", resume="run-1", instruction="CI finished.")))
    run(dispatcher.handle_pr_event({"ticket": "SAMPLE-42", "instruction": "Reply to the comment.", "run_id": "run-1"}))
    assert runner.instructions == ["CI finished.\n\nGATE for SAMPLE-42", "Reply to the comment.\n\nGATE for SAMPLE-42"]


def test_supervisor_gate_lists_unreplied_comments_for_attached_unmerged_prs(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    processor = PREventProcessor(db, project="SampleProject")
    try:
        seed_ticket(db)
        processor.observe({"identifier": "SAMPLE-42", "pr_urls": [PR_URL]}, pr_facts())
        dispatcher = WorktreeDispatcher(object(), lambda worktree, wake: None, project="SampleProject")
        supervisor = Supervisor(IdlePoller(), db=db, dispatcher=dispatcher, pr_event_processor=processor)
        gate = dispatcher.handoff_gate("SAMPLE-42")
        assert gate.startswith("Harbor handoff gate") and "issue comment issue-1" in gate
        supervisor.last_pr_facts = ({**pr_facts(), "ticket": "SAMPLE-42", "merged": True},)
        assert dispatcher.handoff_gate("SAMPLE-42") == "", "a merged PR no longer needs a pre-merge reminder"
    finally:
        db.close()


# 6. Stopped builders page the operator ------------------------------------------------


def test_stopped_builders_page_once_with_the_retry_command(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    pager = Pager()
    supervisor = Supervisor(IdlePoller(), db=db, notifier=pager)
    failed = {"status": "failed", "summary": "deadline exceeded", "error": "deadline exceeded", "waiting_on": "none", "run_id": "run-1"}
    wake = Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage", resume="run-1")
    try:
        run(supervisor._handle_runner_result(wake, failed))
        run(supervisor._handle_runner_result(wake, failed))
        assert pager.sent == ["SAMPLE-42 builder failed: deadline exceeded. Run /retry SAMPLE-42 to resume the same session."]

        seed_ticket(db)
        db.replace_tickets("OtherProject", [{"identifier": "OTHER-9", "title": "Elsewhere", "state": "In Progress"}])
        db.connection.execute("INSERT INTO runs(id, ticket, status) VALUES ('run-2', 'SAMPLE-42', 'orphaned')")
        db.connection.execute("INSERT INTO runs(id, ticket, status) VALUES ('run-9', 'OTHER-9', 'orphaned')")
        run(supervisor._notify_orphaned_runs(["run-2", "run-9"]))
        assert pager.sent[-1] == (
            "SAMPLE-42 builder orphaned: Harbor restarted while it was running. Run /retry SAMPLE-42 to resume the same session."
        )
        assert len(pager.sent) == 2, "another project's orphan is not paged here"
    finally:
        db.close()


def test_undeliverable_wakes_page_only_when_the_operator_must_act(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    pager = Pager()
    supervisor = Supervisor(IdlePoller(), db=db, notifier=pager)
    try:
        run(supervisor._notify_dispatch_failure(Wake(ticket={"identifier": "SAMPLE-42"}, action="build"), "disk floor"))
        assert pager.sent == ["Harbor could not start the builder for SAMPLE-42: disk floor"]

        seed_ticket(db)
        apply_phase4_migration(db)
        comment = Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage", reason="linear-comment-version")
        db.connection.execute("INSERT INTO runs(id, ticket, status) VALUES ('run-1', 'SAMPLE-42', 'waiting')")
        run(supervisor._notify_dispatch_failure(comment, "retained builder refused re-engagement delivery"))
        assert len(pager.sent) == 1, "a refused resume of a live builder is a race retried next poll"

        db.connection.execute("UPDATE runs SET status = 'failed' WHERE id = 'run-1'")
        run(supervisor._notify_dispatch_failure(comment, "retained builder refused re-engagement delivery"))
        assert pager.sent[-1] == "SAMPLE-42 has new work but its builder is failed. Run /retry SAMPLE-42 to resume the same session."
    finally:
        db.close()


def test_a_failing_notifier_never_breaks_result_routing(tmp_path: Path) -> None:
    class BrokenPager:
        async def notify(self, reason: str) -> None:
            raise FileNotFoundError("osascript")

    supervisor = Supervisor(IdlePoller(), notifier=BrokenPager())
    wake = Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage", resume="run-1")
    run(supervisor._handle_runner_result(wake, {"status": "terminated", "summary": "builder process terminated"}))
    assert supervisor.last_error == "operator notification failed: osascript"


# Review findings: regression guards -------------------------------------------------


def test_chatter_older_than_the_escalation_is_never_stored_as_its_answer(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")

    class Resumer:
        async def resume(self, wake: Wake) -> dict[str, Any]:
            return {"status": "waiting", "summary": "ok", "waiting_on": "human"}

    try:
        seed_ticket(db)
        escalation = db.record_escalation("SAMPLE-42", "run-1", "product_decision", PRODUCT_QUESTION, 2)
        chatter = Wake(
            ticket={"identifier": "SAMPLE-42"}, action="reengage", resume="run-1", instruction="x",
            reason="linear-comment-version",
            facts={"linear_comment": {"id": "c-old", "body": "Also rename the button.", "updated_at": later(-5)}},
        )
        supervisor = Supervisor(IdlePoller(), db=db, dispatcher=Resumer())
        run(supervisor._deliver_wake(chatter))
        assert db.find_resolution("product_decision", PRODUCT_QUESTION, project="SampleProject") is None
        assert [row["id"] for row in db.open_escalations()] == [escalation["id"]]

        untimed = Wake(**{**chatter.__dict__, "facts": {"linear_comment": {"id": "c-2", "body": "No timestamp."}}})
        run(supervisor._deliver_wake(untimed))
        assert db.find_resolution("product_decision", PRODUCT_QUESTION, project="SampleProject") is None, "an unprovable reply is not stored"

        db.record_resolution("product_decision", PRODUCT_QUESTION, "Wrong inferred answer", "linear:SAMPLE-42#comment-c-1")

        async def resumed(run_id: str, prompt: str) -> bool:
            return True

        supervisor.resume_session = resumed
        run(supervisor.answer_escalation(str(escalation["id"]), "Use the illustrated empty state"))
        assert db.find_resolution("product_decision", PRODUCT_QUESTION, project="SampleProject")["answer"] == "Use the illustrated empty state"
        run(supervisor.answer_escalation(str(escalation["id"]), "Actually use the plain empty state"))
        assert db.find_resolution("product_decision", PRODUCT_QUESTION, project="SampleProject")["answer"] == "Actually use the plain empty state"
    finally:
        db.close()


def test_ordinary_or_foreign_results_never_close_open_escalations(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    processor = PREventProcessor(db, project="SampleProject")
    wake = Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage", resume="run-1")
    try:
        seed_ticket(db)
        processor.observe({"identifier": "SAMPLE-42", "pr_urls": [PR_URL]}, pr_facts(comments=[]))
        supervisor = Supervisor(IdlePoller(), db=db, pr_event_processor=processor)
        question = db.record_escalation("SAMPLE-42", "run-1", "product_decision", PRODUCT_QUESTION, 2)
        for result in (
            {"status": "waiting", "outcome": "pr_open", "waiting_on": "review", "pr_url": PR_URL, "summary": "replied"},
            {"status": "completed", "outcome": "merged", "pr_url": PR_URL, "run_id": "run-9", "merge_verified": True, "summary": "foreign"},
        ):
            run(supervisor._report_result(wake, result))
        assert [row["id"] for row in db.open_escalations()] == [question["id"]]

        clear = f"## Risk Analysis\nLow\n{risk_analysis_marker('sha-a')}"
        processor.observe({"identifier": "SAMPLE-42", "pr_urls": [PR_URL]}, pr_facts(body=clear, comments=[]))
        merged = {"status": "completed", "outcome": "merged", "pr_url": PR_URL, "merge_verified": True, "summary": "merged"}
        run(supervisor._report_result(wake, {**merged, "merge_verified": False}))
        assert [row["id"] for row in db.open_escalations()] == [question["id"]], "an unverified merge closes only gates"
        run(supervisor._report_result(wake, merged))
        assert db.open_escalations() == [], "a verified merge finishes the ticket's escalations"
    finally:
        db.close()


def test_unobserved_handoff_is_rechecked_and_escalated_as_unverified(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    processor = PREventProcessor(db, project="SampleProject")
    pager = Pager()
    parked = {"status": "waiting", "outcome": "parked", "waiting_on": "human", "pr_url": PR_URL, "summary": "Merge is yours"}
    wake = Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage")
    try:
        db.replace_tickets("SampleProject", [{"identifier": "SAMPLE-42", "id": "linear-42", "title": "t", "state": "In Review", "pr_urls": []}])
        ladder = UnblockLadder(db, linear=LinearFake(), notifier=pager)
        supervisor = Supervisor(IdlePoller(), db=db, pr_event_processor=processor, unblock_ladder=ladder, notifier=pager)
        run(supervisor._handle_runner_result(wake, parked))
        assert pager.sent == [] and db.open_escalations() == [], "an unobserved PR is neither merge-ready nor clear"
        run(supervisor._retry_pending_handoffs())
        run(supervisor._retry_pending_handoffs())
        assert pager.sent == []
        run(supervisor._retry_pending_handoffs())
        [gate] = db.open_escalations()
        assert gate["kind"] == "handoff_gate" and "no Harbor observation of the PR after 3 polls" in gate["question"]

        # Once Linear attaches the PR and it is clean, a fresh park pages merge-ready.
        seed_ticket(db)
        clear = f"## Risk Analysis\nLow\n{risk_analysis_marker('sha-a')}"
        processor.observe({"identifier": "SAMPLE-42", "pr_urls": [PR_URL]}, pr_facts(body=clear, comments=[]))
        run(supervisor._handle_runner_result(wake, {**parked, "summary": "Merge is yours, again"}))
        assert db.open_escalations() == []
        assert pager.sent[-1] == f"SAMPLE-42 is merge-ready and waiting on you: {PR_URL}"
    finally:
        db.close()


def test_answer_refuses_superseded_and_foreign_escalations(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    try:
        seed_ticket(db)
        stale = db.record_escalation("SAMPLE-42", "run-1", "product_decision", "Old question", 2)
        db.record_escalation("SAMPLE-42", "run-1", "product_decision", PRODUCT_QUESTION, 2)
        db.replace_tickets("OtherProject", [{"identifier": "OTHER-9", "title": "Elsewhere", "state": "Blocked"}])
        foreign = db.record_escalation("OTHER-9", "run-9", "product_decision", "Another project", 2)
        supervisor = Supervisor(IdlePoller(), db=db, shadow=True)
        for escalation, message in ((stale, "is superseded"), (foreign, "not a ticket observed in project SampleProject")):
            try:
                run(supervisor.answer_escalation(str(escalation["id"]), "Some answer"))
            except ValueError as exc:
                assert message in str(exc)
            else:
                raise AssertionError(f"escalation {escalation['id']} must be refused")
        assert db.resolutions() == []
    finally:
        db.close()


def test_changing_dispatch_errors_page_once_and_stop_notices_never_raise(tmp_path: Path) -> None:
    pager = Pager()
    supervisor = Supervisor(IdlePoller(), notifier=pager)
    build = Wake(ticket={"identifier": "SAMPLE-42"}, action="build")
    for attempt in range(3):
        run(supervisor._notify_dispatch_failure(build, f"disk floor: {attempt} GB free"))
    assert len(pager.sent) == 1

    class BrokenPager:
        async def notify(self, reason: Any) -> None:
            raise FileNotFoundError("osascript")

    broken = Supervisor(IdlePoller(), notifier=BrokenPager())
    run(broken._deliver_wake(Wake(ticket={"identifier": "SAMPLE-42"}, action="stop", notify=True, reason="bot rework cap reached (1)")))
    assert broken.last_error == "operator notification failed: osascript"


# Second review: regression guards -------------------------------------------------------


def seed_run(db: HarborDB, status: str, final: dict[str, Any], *, run_id: str = "run-1", finished: str = "2026-09-14T10:00:00+00:00") -> None:
    import json

    from harbor.wake import apply_phase3_migration

    apply_phase3_migration(db)
    apply_phase4_migration(db)
    db.connection.execute(
        "INSERT OR REPLACE INTO runs(id, ticket, role, status, waiting_on, final_json, started_at, finished_at) "
        "VALUES (?, 'SAMPLE-42', 'builder', ?, ?, ?, ?, ?)",
        (run_id, status, final.get("waiting_on"), json.dumps(final), finished, finished),
    )
    db.connection.commit()


def test_only_an_explicit_answer_reply_created_after_the_escalation_is_stored(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")

    class Resumer:
        async def resume(self, wake: Wake) -> dict[str, Any]:
            return {"status": "waiting", "summary": "ok", "waiting_on": "human"}

    def reply(body: str, *, created: str, comment_id: str) -> Wake:
        comment = {"id": comment_id, "body": body, "created_at": created, "updated_at": later(5)}
        return Wake(
            ticket={"identifier": "SAMPLE-42"}, action="reengage", resume="run-1", instruction="x",
            reason="linear-comment-version", facts={"linear_comment": comment},
        )

    try:
        seed_ticket(db)
        escalation = db.record_escalation("SAMPLE-42", "run-1", "product_decision", PRODUCT_QUESTION, 2)
        supervisor = Supervisor(IdlePoller(), db=db, dispatcher=Resumer())
        run(supervisor._deliver_wake(reply("Looking into it, will decide tomorrow.", created=later(), comment_id="c-1")))
        for index, chatter in enumerate(("Answer pending, will check with design tomorrow", "/answer 12 use cache", "Answer me this why")):
            run(supervisor._deliver_wake(reply(chatter, created=later(), comment_id=f"c-chatter-{index}")))
        run(supervisor._deliver_wake(reply("Answer: rename the button", created=later(-5), comment_id="c-0")))
        assert db.find_resolution("product_decision", PRODUCT_QUESTION, project="SampleProject") is None
        assert [row["id"] for row in db.open_escalations()] == [escalation["id"]]

        run(supervisor._deliver_wake(reply("answer: Use the illustrated empty state.", created=later(2), comment_id="c-2")))
        stored = db.find_resolution("product_decision", PRODUCT_QUESTION, project="SampleProject")
        assert (stored["answer"], stored["citation"]) == ("Use the illustrated empty state.", "linear:SAMPLE-42#comment-c-2")
        assert db.open_escalations() == []

        # A later Linear reply to a reopened question never overwrites the operator's console answer.
        reopened = db.record_escalation("SAMPLE-42", "run-1", "product_decision", PRODUCT_QUESTION, 2)

        async def resumed(run_id: str, prompt: str) -> bool:
            return True

        supervisor.resume_session = resumed
        run(supervisor.answer_escalation(str(reopened["id"]), "Operator decision: plain empty state"))
        again = db.record_escalation("SAMPLE-42", "run-1", "product_decision", PRODUCT_QUESTION, 2)
        run(supervisor._deliver_wake(reply("Answer: no idea, ask Keith", created=later(3), comment_id="c-3")))
        assert db.find_resolution("product_decision", PRODUCT_QUESTION, project="SampleProject")["answer"] == "Operator decision: plain empty state"
        assert db.escalations()[-1]["id"] == again["id"] and db.escalations()[-1]["status"] == "answered"
    finally:
        db.close()


def test_a_stale_pending_handoff_never_supersedes_a_newer_blocker(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    processor = PREventProcessor(db, project="SampleProject")
    parked = {"status": "waiting", "outcome": "parked", "waiting_on": "human", "pr_url": PR_URL, "summary": "Merge is yours"}
    wake = Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage")
    try:
        db.replace_tickets("SampleProject", [{"identifier": "SAMPLE-42", "id": "linear-42", "title": "t", "state": "In Review", "pr_urls": []}])
        supervisor = Supervisor(
            IdlePoller(), db=db, pr_event_processor=processor, notifier=Pager(),
            unblock_ladder=UnblockLadder(db, linear=LinearFake(), notifier=Pager()),
        )
        run(supervisor._report_result(wake, parked))
        assert "SAMPLE-42" in supervisor._pending_handoffs
        run(supervisor._report_result(wake, {"status": "blocked", "summary": "s", "blocker": {"kind": "product_decision", "question": "q"}}))
        assert supervisor._pending_handoffs == {}, "a newer result replaces the pending handoff"

        run(supervisor._report_result(wake, parked))
        seed_run(db, "blocked", {"status": "blocked", "outcome": None, "waiting_on": "human", "pr_url": None})
        question = db.record_escalation("SAMPLE-42", "run-1", "product_decision", PRODUCT_QUESTION, 2)
        for _ in range(3):
            run(supervisor._retry_pending_handoffs())
        assert [row["id"] for row in db.open_escalations()] == [question["id"]]
    finally:
        db.close()


def test_a_parked_handoff_from_before_a_restart_is_rederived_from_the_ledger(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    processor = PREventProcessor(db, project="SampleProject")
    pager = Pager()
    try:
        seed_ticket(db)
        processor.observe({"identifier": "SAMPLE-42", "pr_urls": [PR_URL]}, pr_facts())
        seed_run(db, "waiting", {"status": "waiting", "outcome": "parked", "waiting_on": "human", "pr_url": PR_URL, "summary": "Merge is yours"})
        restarted = Supervisor(
            IdlePoller(), db=db, pr_event_processor=processor, notifier=pager,
            unblock_ladder=UnblockLadder(db, linear=LinearFake(), notifier=pager),
        )
        restarted._fleet_tickets = ({"identifier": "SAMPLE-42", "owned": True},)
        run(restarted._retry_pending_handoffs())
        run(restarted._retry_pending_handoffs())
        [gate] = db.open_escalations()
        assert gate["kind"] == "handoff_gate" and "1 unreplied external comment" in gate["question"]
        assert len(pager.sent) == 1, "the re-derived handoff is checked once per process"
    finally:
        db.close()


def test_merge_with_gaps_escalates_and_unplaceable_escalations_are_refused(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    processor = PREventProcessor(db, project="SampleProject")
    pager = Pager()
    try:
        seed_ticket(db)
        processor.observe({"identifier": "SAMPLE-42", "pr_urls": [PR_URL]}, pr_facts())
        supervisor = Supervisor(
            IdlePoller(), db=db, pr_event_processor=processor, notifier=pager,
            unblock_ladder=UnblockLadder(db, linear=LinearFake(), notifier=pager),
        )
        merged = {"status": "completed", "outcome": "merged", "pr_url": PR_URL, "merge_verified": True, "summary": "merged"}
        run(supervisor._report_result(Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage"), merged))
        [gate] = db.open_escalations()
        assert gate["question"].startswith(f"SAMPLE-42 merged {PR_URL} with 1 unreplied external comment")

        unplaced = db.record_escalation("GHOST-1", "run-x", "product_decision", "No ticket row", 2)
        try:
            run(supervisor.answer_escalation(str(unplaced["id"]), "Answer anyway"))
        except ValueError as exc:
            assert "not a ticket observed in project SampleProject" in str(exc)
        else:
            raise AssertionError("an escalation the ledger cannot place must be refused")
    finally:
        db.close()


def test_status_keeps_explicit_open_escalations_outside_the_poll_but_hides_legacy_rows(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    try:
        seed_ticket(db)
        db.replace_tickets("SampleProject", [{"identifier": "SAMPLE-7", "title": "Parked elsewhere", "state": "Human Review"}])
        explicit = db.record_escalation("SAMPLE-7", "run-7", "product_decision", "Still needs you", 2)
        db.connection.execute(
            "INSERT INTO escalations(ticket, kind, question, rung_resolved, created_at) VALUES ('SAMPLE-42', 'env_blocker', 'Legacy question', 2, 'then')"
        )
        db.connection.commit()
        supervisor = Supervisor(IdlePoller(), db=db)
        supervisor._fleet_tickets = ({"identifier": "SAMPLE-99", "owned": True},)
        rows = supervisor.status().escalations
        assert [row["id"] for row in rows] == [explicit["id"]]
    finally:
        db.close()


# PR review (request changes): regression guards ----------------------------------------


def test_only_a_human_park_is_normalized_and_gated_as_a_handoff(tmp_path: Path) -> None:
    for waiting_on, expected in (("ci", "review"), ("review", "review"), ("human", "human")):
        result = normalize_final_result(
            {"status": "completed", "outcome": "parked", "waiting_on": waiting_on, "pr_url": PR_URL, "summary": "s"}
        )
        assert result["waiting_on"] == expected, f"a park waiting on {waiting_on} became {result['waiting_on']}"

    db = HarborDB(tmp_path / "harbor.db")
    processor = PREventProcessor(db, project="SampleProject")
    pager = Pager()
    try:
        seed_ticket(db)
        processor.observe({"identifier": "SAMPLE-42", "pr_urls": [PR_URL]}, pr_facts())
        supervisor = Supervisor(
            IdlePoller(), db=db, pr_event_processor=processor, notifier=pager,
            unblock_ladder=UnblockLadder(db, linear=LinearFake(), notifier=pager),
        )
        on_ci = {"status": "waiting", "outcome": "parked", "waiting_on": "ci", "pr_url": PR_URL, "summary": "CI running"}
        run(supervisor._report_result(Wake(ticket={"identifier": "SAMPLE-42"}, action="reengage"), on_ci))
        assert db.open_escalations() == [] and pager.sent == [], "a park waiting on CI is not a human handoff"
    finally:
        db.close()


def test_one_linear_answer_answers_only_the_newest_open_escalation(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")

    class Resumer:
        async def resume(self, wake: Wake) -> dict[str, Any]:
            return {"status": "waiting", "summary": "ok", "waiting_on": "human"}

    try:
        seed_ticket(db)
        earlier = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
        for question in ("Which colour for the banner?", "Which font for the footer?"):
            db.connection.execute(
                "INSERT INTO escalations(ticket, kind, question, rung_resolved, created_at) VALUES ('SAMPLE-42', 'product_decision', ?, 2, ?)",
                (question, earlier),
            )
        db.connection.commit()
        wake = Wake(
            ticket={"identifier": "SAMPLE-42"}, action="reengage", resume="run-1", instruction="x",
            reason="linear-comment-version",
            facts={"linear_comment": {"id": "c-1", "body": "Answer: blue", "created_at": later()}},
        )
        run(Supervisor(IdlePoller(), db=db, dispatcher=Resumer())._deliver_wake(wake))
        assert [row["question"] for row in db.open_escalations()] == ["Which colour for the banner?"]
        assert [row["question"] for row in db.resolutions()] == ["Which font for the footer?"]
    finally:
        db.close()


def test_a_failed_notification_is_retried_rather_than_deduplicated() -> None:
    class FlakyPager:
        def __init__(self) -> None:
            self.attempts = 0
            self.sent: list[str] = []

        async def notify(self, reason: str) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("notifier busy")
            self.sent.append(reason)

    pager = FlakyPager()
    supervisor = Supervisor(IdlePoller(), notifier=pager)
    for _ in range(3):
        run(supervisor._notify_operator("parked:SAMPLE-42", "SAMPLE-42 is merge-ready"))
    assert pager.attempts == 2 and pager.sent == ["SAMPLE-42 is merge-ready"]


def test_one_comment_id_on_two_surfaces_needs_both_replies(tmp_path: Path) -> None:
    db = HarborDB(tmp_path / "harbor.db")
    processor = PREventProcessor(db, project="SampleProject")
    ticket = {"identifier": "SAMPLE-42", "pr_urls": [PR_URL]}
    inline = {"id": "rc-1", "surface": "inline", "body": "Handle the empty case.", "path": "a.ts", "updated_at": "2026-09-02T10:01:00Z"}
    thread = {**inline, "surface": "thread", "thread_id": "t-1"}
    risk = f"## Risk Analysis\nLow\n{risk_analysis_marker('sha-a')}"
    try:
        processor.observe(ticket, pr_facts(body=risk, comments=[inline, thread]))
        gaps = processor.handoff_gaps("SAMPLE-42", PR_URL, head_sha="sha-a")
        assert sorted(item["surface"] for item in gaps["unreplied"]) == ["inline", "thread"]
        inline_marker = next(item["reply_marker"] for item in gaps["unreplied"] if item["surface"] == "inline")
        reply = {"id": "reply-1", "surface": "issue", "body": f"Handled. {inline_marker}"}
        processor.observe(ticket, pr_facts(body=risk, comments=[inline, thread, reply]))
        gaps = processor.handoff_gaps("SAMPLE-42", PR_URL, head_sha="sha-a")
        assert has_handoff_gaps(gaps), "the thread surface still has no observed reply"
        assert [item["surface"] for item in gaps["unreplied"]] == ["thread"]
    finally:
        db.close()
