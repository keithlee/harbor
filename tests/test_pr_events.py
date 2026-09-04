import asyncio
from typing import Any

import pytest

from harbor.clients.github import build_pull_requests_query
from harbor.db import HarborDB
from harbor.facts.threads import parse_comment_surfaces
from harbor.pr_events import (
    CommentContractError,
    NEW_COMMENT_VERSION,
    PREventProcessor,
    PR_CREATED,
    apply_phase8_migration,
    build_comment_instruction,
    comment_reply_marker,
    comment_version_key,
    normalize_comment_response,
    risk_analysis_marker,
)


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def pr(*, head: str = "sha-a") -> dict[str, Any]:
    return {
        "number": 7,
        "createdAt": "2026-09-02T10:00:00Z",
        "headRefOid": head,
        "ci": {"state": "green"},
        "issue": {
            "comments": {
                "nodes": [
                    {
                        "id": "issue-1",
                        "body": "Please handle the timeout.",
                        "createdAt": "2026-09-02T10:01:00Z",
                        "updatedAt": "2026-09-02T10:01:00Z",
                        "author": {"login": "reviewer", "__typename": "User", "isBot": False},
                    },
                    {
                        "id": "harbor-1",
                        "body": "<!-- harbor: reply --> already handled",
                        "author": {"login": "harbor", "__typename": "Bot"},
                    },
                ]
            }
        },
        "reviews": {
            "nodes": [
                {
                    "id": "review-1",
                    "body": "The approach looks sound.",
                    "submittedAt": "2026-09-02T10:02:00Z",
                    "updatedAt": "2026-09-02T10:02:00Z",
                    "author": {"login": "reviewer", "__typename": "User", "isBot": False},
                }
            ]
        },
        "reviewThreads": {
            "nodes": [
                {
                    "id": "thread-1",
                    "path": "src/service.py",
                    "line": 12,
                    "isResolved": False,
                    "comments": {
                        "nodes": [
                            {
                                "id": "inline-1",
                                "body": "Could this handle an empty response?",
                                "updatedAt": "2026-09-02T10:03:00Z",
                                "author": {"login": "reviewer", "__typename": "User", "isBot": False},
                            }
                        ]
                    },
                }
            ]
        },
    }


def test_query_reads_pr_creation_and_all_review_surfaces_without_mutation() -> None:
    query = build_pull_requests_query("owner", "repo", [7])
    assert "createdAt" in query
    assert "comments(first: 100)" in query
    assert "reviews(first: 100)" in query
    assert "reviewThreads(first: 100)" in query
    assert "mutation" not in query.lower()


def test_comment_facts_include_issue_review_inline_and_thread_and_suppress_marker() -> None:
    comments = parse_comment_surfaces(pr())
    assert {(item["surface"], item["id"]) for item in comments} == {
        ("issue", "issue-1"),
        ("review", "review-1"),
        ("thread", "inline-1"),
        ("issue", "harbor-1"),
    }
    assert next(item for item in comments if item["id"] == "harbor-1")["harbor_marked"] is True


def test_pr_and_comment_versions_are_exactly_once_and_edits_are_new_versions(tmp_path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")
    apply_phase8_migration(db)
    processor = PREventProcessor(db, project="demo")
    ticket = {"identifier": "DEMO-7", "run_id": "builder-7"}
    facts = pr()
    facts["comments"] = parse_comment_surfaces(facts)
    first = processor.observe(ticket, facts)
    second = processor.observe(ticket, facts)
    assert [item["event_type"] for item in first] == [PR_CREATED, NEW_COMMENT_VERSION, NEW_COMMENT_VERSION, NEW_COMMENT_VERSION]
    assert second == []
    edited = pr()
    edited_comments = parse_comment_surfaces(edited)
    issue_index = next(index for index, item in enumerate(edited_comments) if item["id"] == "issue-1")
    edited_comments[issue_index] = {**edited_comments[issue_index], "body": "Please handle the timeout and add a test.", "updated_at": "2026-09-02T10:04:00Z"}
    edited["comments"] = edited_comments
    changed = processor.observe(ticket, edited)
    assert [item["event_type"] for item in changed] == [NEW_COMMENT_VERSION]
    assert len(processor.events()) == 5
    db.close()


def test_comment_response_contract_accepts_valid_and_rejects_missing_reply_or_evidence() -> None:
    valid = normalize_comment_response(
        {"classification": "valid", "fix": "guard empty response", "tests": "pytest tests/test_service.py", "reply": "Fixed and tested."}
    )
    assert valid["classification"] == "valid"
    with pytest.raises(CommentContractError, match="reply"):
        normalize_comment_response({"classification": "valid", "fix": "change", "tests": "test"})
    with pytest.raises(CommentContractError, match="evidence"):
        normalize_comment_response({"classification": "out-of-scope", "reply": "No change."})


def test_comment_instruction_carries_a_source_version_bound_reply_marker() -> None:
    source = next(item for item in parse_comment_surfaces(pr()) if item["id"] == "issue-1")
    version = comment_version_key(source)
    marker = comment_reply_marker(version)
    instruction = build_comment_instruction(
        {"identifier": "DEMO-7", "run_id": "run-7"},
        {"number": 7, "head_sha": "sha-a"},
        source,
    )
    assert marker in instruction
    assert "post the reply on github yourself" in instruction.casefold()


def test_risk_instruction_carries_the_exact_head_bound_marker() -> None:
    instruction = next(
        item
        for item in PREventProcessor(project="demo").observe(
            {"identifier": "DEMO-7"},
            {**pr(head="sha-a"), "body": None},
        )
        if item["event_type"] == PR_CREATED
    )["instruction"]
    assert risk_analysis_marker("sha-a") in instruction


def test_running_builder_observes_events_but_defers_same_session_delivery(tmp_path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")
    calls: list[dict[str, Any]] = []
    source = next(item for item in parse_comment_surfaces(pr()) if item["id"] == "issue-1")
    facts = {**pr(), "comments": [source]}

    async def builder(event: dict[str, Any]) -> dict[str, Any] | None:
        calls.append(event)
        if event["event_type"] == PR_CREATED:
            return None
        return {"status": "waiting", "summary": "reply instruction accepted"}

    processor = PREventProcessor(db, project="demo", builder=builder)
    ticket = {"identifier": "DEMO-7", "run_id": "run-7"}

    assert run(processor.process(ticket, facts, run={"id": "run-7", "status": "running"})) == []
    assert calls == []
    assert len(processor.events()) == 2, "both PR and comment receipts were persisted"

    delivered = run(processor.process(ticket, facts, run={"id": "run-7", "status": "waiting"}))
    assert [item["event_type"] for item in delivered] == [PR_CREATED, NEW_COMMENT_VERSION]
    assert len(calls) == 2

    # A one-minute poll while the run is waiting must not resume the same
    # session again while the marker is still absent.
    assert run(processor.process(ticket, facts, run={"id": "run-7", "status": "waiting"})) == []
    assert len(calls) == 2
    db.close()


def test_normal_run_result_is_offered_but_not_falsely_receipted(tmp_path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")
    source = next(item for item in parse_comment_surfaces(pr()) if item["id"] == "issue-1")
    facts = {**pr(), "comments": [source]}

    async def builder(event: dict[str, Any]) -> dict[str, Any] | None:
        if event["event_type"] == PR_CREATED:
            return None
        return {"status": "completed", "summary": "builder returned its normal run result", "outcome": "pr_open"}

    processor = PREventProcessor(db, project="demo", builder=builder)
    ticket = {"identifier": "DEMO-7", "run_id": "run-7"}
    delivered = run(processor.process(ticket, facts, run={"id": "run-7", "status": "waiting"}))
    assert any(item["event_type"] == NEW_COMMENT_VERSION and item["reply_observed"] is False for item in delivered)
    pending = processor.pending_comments()
    assert len(pending) == 1
    assert pending[0]["delivery_state"] == "awaiting_reply_observation"
    row = db.connection.execute(
        "SELECT offered, replied FROM github_pr_events WHERE event_type=?", (NEW_COMMENT_VERSION,)
    ).fetchone()
    assert tuple(row) == (1, 0)
    db.close()


def test_normal_pr_run_result_stays_pending_until_current_head_risk_is_observed(tmp_path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")

    async def builder(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "completed",
            "summary": "risk instruction accepted",
            "outcome": "pr_open",
            "pr_url": "https://github.com/acme/demo/pull/7",
        }

    processor = PREventProcessor(db, project="demo", builder=builder)
    ticket = {"identifier": "DEMO-7", "run_id": "run-7"}
    facts = {**pr(), "url": "https://github.com/acme/demo/pull/7", "body": "No risk section yet."}
    delivered = run(processor.process(ticket, facts, run={"id": "run-7", "status": "waiting"}))
    assert any(item["event_type"] == PR_CREATED for item in delivered)
    pending = processor.events(pending_only=True)
    assert [item["event_type"] for item in pending] == [PR_CREATED]

    current = {
        **facts,
        "body": f"## Risk Analysis\nRisk: low\nHead: sha-a\n{risk_analysis_marker('sha-a')}",
    }
    assert run(processor.process(ticket, current, run={"id": "run-7", "status": "waiting"})) == []
    assert processor.risk_is_valid(ticket, current)
    assert processor.events(pending_only=True) == []
    db.close()


def test_db_less_pending_recovery_matches_sqlite_after_current_head_risk(tmp_path) -> None:
    ticket = {"identifier": "DEMO-7"}
    facts = {
        **pr(),
        "body": f"## Risk Analysis\nRisk: low\n{risk_analysis_marker('sha-a')}",
    }

    async def unexpected_builder(event: dict[str, Any]) -> None:
        raise AssertionError(f"valid risk receipt was delivered: {event}")

    memory = PREventProcessor(project="demo", builder=unexpected_builder)
    db = HarborDB(tmp_path / "harbor.sqlite")
    durable = PREventProcessor(db, project="demo", builder=unexpected_builder)
    try:
        for processor in (memory, durable):
            processor.observe(ticket, facts)
            assert processor.risk_is_valid(ticket, facts)
            assert run(processor.process(ticket, facts)) == []
            assert processor.events(pending_only=True) == []
    finally:
        db.close()


def test_old_pr_body_risk_marker_is_not_rebound_to_a_new_head(tmp_path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")
    processor = PREventProcessor(db, project="demo")
    ticket = {"identifier": "DEMO-7"}
    old = pr(head="sha-a")
    old["body"] = f"## Risk Analysis\nLow\n{risk_analysis_marker('sha-a')}"
    processor.observe(ticket, old)
    assert processor.risk_is_valid(ticket, old)

    pushed = pr(head="sha-b")
    pushed["body"] = old["body"]
    processor.observe(ticket, pushed)
    assert processor.risk_status(ticket, pushed) == "invalidated"
    assert not processor.risk_is_valid(ticket, pushed)

    rerun = {**pushed, "body": f"## Risk Analysis\nLow\n{risk_analysis_marker('sha-b')}"}
    processor.observe(ticket, rerun)
    assert processor.risk_is_valid(ticket, rerun)
    db.close()


def test_observed_marker_receipts_source_version_without_harbor_mutation(tmp_path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")
    source = next(item for item in parse_comment_surfaces(pr()) if item["id"] == "issue-1")
    facts = {**pr(), "comments": [source]}

    async def builder(event: dict[str, Any]) -> dict[str, Any] | None:
        if event["event_type"] == PR_CREATED:
            return None
        return {"status": "waiting", "summary": "reply submitted"}

    processor = PREventProcessor(db, project="demo", builder=builder)
    ticket = {"identifier": "DEMO-7", "run_id": "run-7"}
    run(processor.process(ticket, facts, run={"id": "run-7", "status": "waiting"}))
    event = next(item for item in processor.events() if item["event_type"] == NEW_COMMENT_VERSION)
    marker = event["payload"]["reply_marker"]

    # The reply arrives through the next read-only GitHub observation.  The
    # source itself carrying a copied marker is not accepted as evidence.
    copied_source = {**source, "body": f"Original text {marker}"}
    assert run(processor.process(ticket, {**facts, "comments": [copied_source]}, run={"id": "run-7", "status": "waiting"})) == []
    assert len(processor.pending_comments()) == 1

    reply = {"id": "reply-7", "surface": "issue", "body": f"Addressed. {marker}"}
    acknowledged = run(processor.process(ticket, {**facts, "comments": [source, reply]}, run={"id": "run-7", "status": "waiting"}))
    assert acknowledged == []
    assert processor.pending_comments() == []
    row = db.connection.execute(
        "SELECT offered, replied FROM github_pr_events WHERE event_type=?", (NEW_COMMENT_VERSION,)
    ).fetchone()
    assert tuple(row) == (1, 1)
    db.close()


def test_concurrent_polls_do_not_duplicate_a_comment_resume(tmp_path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")
    source = next(item for item in parse_comment_surfaces(pr()) if item["id"] == "issue-1")
    facts = {**pr(), "comments": [source]}
    calls: list[str] = []

    async def builder(event: dict[str, Any]) -> dict[str, Any]:
        calls.append(event["event_key"])
        await asyncio.sleep(0.01)
        return {"status": "waiting", "summary": "accepted"}

    processor = PREventProcessor(db, project="demo")
    ticket = {"identifier": "DEMO-7", "run_id": "run-7"}
    initial = processor.observe(ticket, facts)
    comment = next(item for item in initial if item["event_type"] == NEW_COMMENT_VERSION)
    async def concurrent_dispatch() -> list[list[dict[str, Any]]]:
        return await asyncio.gather(
            processor.dispatch([comment], builder=builder),
            processor.dispatch([comment], builder=builder),
        )

    results = run(concurrent_dispatch())
    assert sum(len(result) for result in results) == 1
    assert calls == [comment["event_key"]]
    db.close()


def test_same_builder_receives_comment_and_reply_is_recorded(tmp_path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")
    calls: list[dict[str, Any]] = []

    async def builder(event: dict[str, Any]) -> dict[str, Any]:
        calls.append(event)
        if event["event_type"] == PR_CREATED:
            return None
        return {
            "classification": "invalid",
            "evidence": "The acceptance criteria do not require this behavior.",
            "reply": "Explained why this is out of scope.",
        }

    processor = PREventProcessor(db, project="demo", builder=builder)
    facts = pr()
    facts["comments"] = parse_comment_surfaces(facts)
    events = run(processor.process({"identifier": "DEMO-7", "run_id": "builder-7"}, facts))
    assert len(calls) == 4
    assert len(events) == 4
    pending = processor.pending_comments()
    assert pending == []
    db.close()


def test_failed_comment_contract_remains_pending_for_same_builder_retry(tmp_path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")
    attempts = 0

    async def builder(event: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal attempts
        if event["event_type"] == PR_CREATED:
            return None
        attempts += 1
        if attempts == 1:
            return {"classification": "valid", "reply": "Missing required fields."}
        return {"classification": "ambiguous", "clarify": "Should we handle this now?", "reply": "Please clarify."}

    processor = PREventProcessor(db, project="demo", builder=builder)
    facts = pr()
    facts["comments"] = parse_comment_surfaces(facts)
    with pytest.raises(CommentContractError):
        run(processor.process({"identifier": "DEMO-7", "run_id": "builder-7"}, facts))
    assert processor.pending_comments()
    delivered = run(processor.process({"identifier": "DEMO-7", "run_id": "builder-7"}, facts))
    assert len(delivered) == 3
    assert processor.pending_comments() == []
    db.close()


def test_pending_recovery_delivers_only_current_project_ticket_and_pr(tmp_path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")
    foreign = PREventProcessor(db, project="other")
    processor = PREventProcessor(db, project="demo")

    foreign_source = next(item for item in parse_comment_surfaces(pr()) if item["id"] == "issue-1")
    target_source = {**foreign_source, "id": "issue-34"}
    foreign_facts = {**pr(), "comments": [foreign_source]}
    target_facts = {**pr(), "number": 34, "comments": [target_source]}
    foreign.observe({"identifier": "HAN-7"}, foreign_facts)
    processor.observe({"identifier": "HAN-34"}, target_facts)

    delivered_tickets: list[str] = []

    class ScopedDispatcher:
        async def handle_pr_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
            # An unscoped recovery query would offer the older HAN-7 row here,
            # just as TicketScope would reject it in the supervisor.
            if event["ticket"] != "HAN-34":
                raise RuntimeError("outside invocation scope")
            delivered_tickets.append(event["ticket"])
            if event["event_type"] == PR_CREATED:
                return None
            return {"status": "waiting", "summary": "accepted"}

    delivered = run(
        processor.process(
            {"identifier": "HAN-34"},
            target_facts,
            builder=ScopedDispatcher(),
        )
    )

    assert delivered
    assert delivered_tickets == ["HAN-34"] * len(delivered)
    assert {item["ticket"] for item in delivered} == {"HAN-34"}
    rows = db.connection.execute(
        "SELECT project, ticket, pr_number, offered FROM github_pr_events ORDER BY id"
    ).fetchall()
    assert [(row[0], row[1], row[2], row[3]) for row in rows] == [
        ("other", "HAN-7", 7, 0),
        ("other", "HAN-7", 7, 0),
        ("demo", "HAN-34", 34, 1),
        ("demo", "HAN-34", 34, 1),
    ]
    db.close()


def test_risk_result_is_invalidated_when_head_changes(tmp_path) -> None:
    db = HarborDB(tmp_path / "harbor.sqlite")
    processor = PREventProcessor(db, project="demo")
    ticket = {"identifier": "DEMO-7"}
    first = pr(head="sha-a")
    processor.observe(ticket, first)
    processor.mark_risk_valid(ticket, first, {"head_sha": "sha-a", "body": "## Risk Analysis\nLow"})
    assert processor.risk_is_valid(ticket, first)
    second = pr(head="sha-b")
    processor.observe(ticket, second)
    assert processor.risk_status(ticket, second) == "invalidated"
    assert not processor.risk_is_valid(ticket, second)
    db.close()
