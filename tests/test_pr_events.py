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
    normalize_comment_response,
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
