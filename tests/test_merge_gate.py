"""The completed-run artifact contract (plan section 5 Phase 4, section 7 AC-8/AC-10).

A builder cannot certify its own merge -- ``FINAL_SCHEMA`` has no field for it.
The authority is what Harbor observed on the PR.  These tests pin both
directions: an unobserved merge claim must not be destroyed, and it must not be
treated as verified either.
"""

from __future__ import annotations

from harbor.runner.base import merge_is_verified, normalize_final_result


MERGED = {"status": "completed", "outcome": "merged", "waiting_on": "none", "summary": "Merged", "pr_url": "https://example.test/pr/1"}


def test_observed_merge_verifies_a_merged_outcome() -> None:
    result = normalize_final_result(MERGED, observed={"merged": True, "merge_commit_sha": "abc123"})
    assert result["status"] == "completed"
    assert result["outcome"] == "merged"
    assert result["merge_verified"] is True
    assert result["merge_commit_sha"] == "abc123"


def test_unobserved_merge_is_retained_but_not_verified() -> None:
    """AC-10: Harbor confirms on its next poll rather than rewriting the run."""

    result = normalize_final_result(MERGED)
    assert result["status"] == "completed"
    assert result["outcome"] == "merged"
    assert result["merge_verified"] is False


def test_observed_open_pr_does_not_verify_a_merge_claim() -> None:
    result = normalize_final_result(MERGED, observed={"merged": False})
    assert result["merge_verified"] is False


def test_merged_claim_without_any_artifact_is_blocked() -> None:
    payload = {"status": "completed", "outcome": "merged", "summary": "Merged"}
    result = normalize_final_result(payload)
    assert result["status"] == "blocked"
    assert result["outcome"] == "no_pr"
    assert result["waiting_on"] == "human"
    assert result["blocker"]["kind"] == "missing_proof_capability"


def test_known_pr_url_from_an_earlier_turn_rescues_a_merged_final() -> None:
    """A resumed session that merges its own PR omits pr_url; the ledger has it."""

    payload = {"status": "completed", "outcome": "merged", "summary": "Merged"}
    result = normalize_final_result(payload, known_pr_url="https://example.test/pr/1")
    assert result["status"] == "completed"
    assert result["outcome"] == "merged"
    assert result["pr_url"] == "https://example.test/pr/1"


def test_completed_with_an_open_pr_becomes_waiting_on_review() -> None:
    payload = {"status": "completed", "outcome": "pr_open", "summary": "PR up", "pr_url": "https://example.test/pr/2"}
    result = normalize_final_result(payload)
    assert result["status"] == "waiting"
    assert result["waiting_on"] == "review"
    assert result["merge_verified"] is False


def test_observed_merge_promotes_a_completed_pr_result(  ) -> None:
    """AC-10: you merged by hand; Harbor's observation is what upgrades it."""

    payload = {"status": "completed", "outcome": "pr_open", "summary": "PR up", "pr_url": "https://example.test/pr/2"}
    result = normalize_final_result(payload, observed={"merged": True})
    assert result["outcome"] == "merged"
    assert result["merge_verified"] is True


def test_completed_with_neither_pr_nor_commit_is_blocked() -> None:
    result = normalize_final_result({"status": "completed", "summary": "done"})
    assert result["status"] == "blocked"
    assert result["outcome"] == "no_pr"


def test_completed_with_commits_but_no_pr_is_left_alone() -> None:
    result = normalize_final_result({"status": "completed", "summary": "done", "commit_sha": "c0ffee1"})
    assert result["status"] == "completed"


def test_non_completed_results_are_never_rewritten() -> None:
    for status in ("waiting", "blocked", "failed"):
        payload = {"status": status, "summary": "s", "outcome": "merged"}
        assert normalize_final_result(payload) == payload


def test_builder_self_report_alone_never_verifies_a_merge() -> None:
    assert merge_is_verified({"outcome": "merged"}) is False
    assert merge_is_verified({}, observed={"merged": True}) is True
