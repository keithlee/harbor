"""Golden builder instructions for PR comments, merge approval and recovery.

These are the exact words a retained builder session receives.  A wording
drift here changes delivery behavior, so it must show up in review.
"""

from __future__ import annotations

import pytest

from harbor.pr_events import _is_merge_request, build_comment_instruction
from harbor.recovery import recovery_instruction

HEAD = "a" * 40
TICKET = {"identifier": "DEMO-7", "run_id": "builder-7", "pr_urls": ["https://github.com/acme/demo/pull/7"]}
FACTS = {"number": 7, "url": "https://github.com/acme/demo/pull/7", "headRefOid": HEAD, "state": "OPEN"}


def comment(body: str, **extra) -> dict:
    return {"id": "c-1", "body": body, "surface": "issue", "author": "owner",
            "created_at": "2026-09-02T10:01:00Z", "updated_at": "2026-09-02T10:01:00Z", **extra}


def test_plain_review_comment_instruction(golden) -> None:
    golden.text("instruction_plain_comment.txt",
                build_comment_instruction(TICKET, FACTS, comment("Please handle the <timeout> case.")))


def test_unverified_merge_request_instruction(golden) -> None:
    golden.text("instruction_merge_request_unverified.txt",
                build_comment_instruction(TICKET, FACTS, comment("merge")))


def test_verified_owner_merge_approval_instruction(golden) -> None:
    from harbor.github_rest import helper_invocation
    # The helper invocation names this machine's Python; keep the snapshot portable.
    golden.text("instruction_merge_approval_verified.txt",
                build_comment_instruction(TICKET, FACTS, comment("LGTM, merge."), merge_approval=True)
                .replace(helper_invocation(), "<python> -I <harbor>/github_rest.py"))


def test_inline_review_comment_carries_path_and_line(golden) -> None:
    golden.text("instruction_inline_comment.txt", build_comment_instruction(
        TICKET, FACTS, comment("off by one", surface="review_thread", path="src/a.py", line=12)))


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("merge", True), ("Merge!", True), ("merge.", True), ("lgtm merge", True), ("LGTM,  merge", True),
        ("  lgtm merge  ", True), ("lgtm", False), ("please merge", False), ("merge it", False),
        ("lgtm merge — after CI", False), ("> lgtm merge", False), ("don't merge", False), ("", False),
    ],
)
def test_merge_request_grammar_is_exact(body: str, expected: bool) -> None:
    assert _is_merge_request({"body": body}) is expected


def test_recovery_instruction(golden) -> None:
    row = {"id": "run-1", "session_id": "sess-1", "worktree": "/worktrees/DEMO-7", "terminal_reason": "unexpected_exit",
           "final_json": '{"summary": "edited two files", "next_action": "run tests"}'}
    golden.text("instruction_recovery.txt", recovery_instruction({"identifier": "DEMO-7"}, row))
