"""pre_merge risk = max(Linear floor, builder `## Risk Analysis` tier, reviewer rating)."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from harbor.cli import build_parser
from harbor.db import HarborDB
from harbor.wake import apply_phase3_migration
from harbor.workflow_cli import execute
from workflow_support import config

PR_URL = "https://github.com/example/repo/pull/1"
HEAD = "a" * 40
STALE = "b" * 40
DELIVERY = "sample-delivery"
OTHER = "someone-else"


def analysis(tier_line, sha=HEAD):
    return (
        "Summary of the change.\n\n## Risk Analysis\n"
        f"{tier_line}\nRationale: docs-only change, trivially reversible.\n"
        f"<!-- harbor:risk-sha:{sha} -->\n"
    )


def verdict(risk="low", *, verdict="approve", head=HEAD, author=DELIVERY, **extra):
    """One PR conversation comment whose body is exactly the reviewer's JSON verdict."""
    review = {"head_sha": head, "verdict": verdict, "proof_verified": True, "feedback_digest": "d", "risk": risk, **extra}
    return {"id": f"c-{author}-{head[:4]}-{verdict}-{risk}", "body": json.dumps({"review": review}),
            "author": {"login": author, "__typename": "User"}, "createdAt": "2026-01-01T00:00:00Z"}


def pre_merge(tmp_path, monkeypatch, capsys, *, description, body, comments=None, auto_merge_risks=("low",),
              pr_author=DELIVERY, sandbox_change=False, base_oid=None):
    """Run `harbor workflow check --event pre_merge` against mocked Linear/GitHub.

    ``comments`` defaults to one current-head low approval from the delivery identity.
    ``sandbox_change`` stubs the PR's ``[builder]`` diff check (True, False or None
    for unreadable); ``"real"`` runs the real check against ``tmp_path``.
    """
    if sandbox_change != "real":
        monkeypatch.setattr("harbor.device_proof.builder_sandbox_change", lambda *args: sandbox_change)
    comments = [verdict("low")] if comments is None else comments

    class Linear:
        def __init__(self, **kwargs):
            pass

        async def fetch_blocker_context(self, ticket):
            return {
                "identifier": ticket,
                "project": {"slugId": "canonical"},
                "description": description,
                "state": {"name": "In Progress", "type": "started"},
                "attachments": {"nodes": [{"sourceType": "github", "url": PR_URL}]},
            }

        async def close(self):
            pass

    class GitHub:
        @classmethod
        def from_profile(cls, profile, **kwargs):
            return cls()

        async def fetch_pull_requests(self, *args, **kwargs):
            return {"pr_1": {
                "number": 1, "headRefOid": HEAD, "baseRefOid": base_oid, "url": PR_URL, "merged": False,
                "mergeable": "MERGEABLE", "body": body,
                "author": {"login": pr_author, "__typename": "User"} if pr_author else None,
                "comments": {"nodes": comments},
                "statusCheckRollup": {"contexts": {"nodes": [{"name": "tests", "conclusion": "SUCCESS"}]}},
            }}

        async def close(self):
            pass

    monkeypatch.setattr("harbor.clients.linear.LinearClient", Linear)
    monkeypatch.setattr("harbor.clients.github.GitHubClient", GitHub)
    # Feedback stays advisory here: the reviewer's comment is not unreplied feedback to gate on.
    workflow = config(tmp_path, "autonomous", extra=(
        f"[delivery]\nauto_merge_risks = {json.dumps(list(auto_merge_risks))}\n[evidence]\nfeedback = \"advisory\"\n"))
    cfg = SimpleNamespace(
        name="A", linear_api_url="https://linear.invalid", linear_project_scope=("canonical",),
        github_repository="example/repo", required_checks=("tests",), workflow_path=workflow,
    )
    args = build_parser().parse_args(["workflow", "check", "A", "--ticket", "A-1", "--event", "pre_merge"])
    db = HarborDB(":memory:")
    try:
        apply_phase3_migration(db)
        db.replace_tickets("A", [{"identifier": "A-1", "state": "In Progress", "owned": True, "hold": False}])
        with db.connection:
            db.connection.execute("INSERT INTO runs (id, ticket, worktree, status) VALUES (?, ?, ?, ?)",
                                  ("run", "A-1", str(tmp_path), "running"))
        exit_code = asyncio.run(execute(args, cfg, db))
    finally:
        db.close()
    return exit_code, json.loads(capsys.readouterr().out)


RISK_FAIL = "risk is not eligible for automatic merge"


def passes(out):
    return out["ready"] is True and out["results"][0]["reason"] == "configured requirements satisfied"


def risk_blocked(out):
    return out["ready"] is False and RISK_FAIL in out["results"][0]["reason"]


# --- reviewer rating -------------------------------------------------------------------------

def test_reviewer_low_and_builder_low_passes(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="No risk line here.",
                          body=analysis("Risk tier: low"), comments=[verdict("low")])
    assert passes(out) and code == 0, out["results"][0]["reason"]


def test_no_reviewer_rating_fails_closed(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=analysis("Risk tier: low"), comments=[])
    assert risk_blocked(out) and code == 1


def test_reviewer_medium_is_not_lowered_by_builder_low(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=analysis("Risk tier: low"),
                          comments=[verdict("medium")])
    assert risk_blocked(out)
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=analysis("Risk tier: low"),
                          comments=[verdict("medium")], auto_merge_risks=("medium",))
    assert passes(out), out["results"][0]["reason"]


def test_reviewer_low_does_not_lower_a_medium_linear_floor(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: medium", body=analysis("Risk tier: low"),
                          comments=[verdict("low")])
    assert risk_blocked(out)
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: medium", body=analysis("Risk tier: low"),
                          comments=[verdict("low")], auto_merge_risks=("medium",))
    assert passes(out), out["results"][0]["reason"]


def test_reviewer_low_does_not_lower_a_high_builder_tier(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=analysis("Risk tier: high"),
                          comments=[verdict("low")], auto_merge_risks=("low", "medium"))
    assert risk_blocked(out)


def test_stale_head_reviewer_comment_is_ignored(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=analysis("Risk tier: low"),
                          comments=[verdict("low", head=STALE)])
    assert risk_blocked(out)


def test_verdict_from_a_non_delivery_author_is_ignored(tmp_path, monkeypatch, capsys):
    # A third party cannot supply the rating ...
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=analysis("Risk tier: low"),
                          comments=[verdict("low", author=OTHER)])
    assert risk_blocked(out)
    # ... nor lower the delivery reviewer's rating ...
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=analysis("Risk tier: low"),
                          comments=[verdict("medium"), verdict("low", author=OTHER)])
    assert risk_blocked(out)
    # ... nor veto it with a malformed or rejecting comment.
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=analysis("Risk tier: low"),
                          comments=[verdict("low"), verdict("low", verdict="reject", author=OTHER)])
    assert passes(out), out["results"][0]["reason"]


def test_reject_verdict_fails(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=analysis("Risk tier: low"),
                          comments=[verdict("low", verdict="reject")])
    assert risk_blocked(out)
    # A reject for the current head is not outweighed by an approval of the same head.
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=analysis("Risk tier: low"),
                          comments=[verdict("low"), verdict("low", verdict="reject")])
    assert risk_blocked(out)


def test_pr_without_a_known_author_fails_closed(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=analysis("Risk tier: low"),
                          pr_author=None)
    assert risk_blocked(out)


def test_pr_query_and_facts_carry_the_pr_author():
    from harbor.clients.github import build_pull_requests_query
    from harbor.poll.github_poller import parse_pull_request

    query = build_pull_requests_query("example", "repo", [1])
    assert "headRefOid\n        baseRefOid\n        author {\n          login" in query
    node = {"number": 1, "headRefOid": HEAD, "url": PR_URL, "author": {"login": DELIVERY, "__typename": "User"}}
    assert parse_pull_request(node)["author"] == DELIVERY
    assert parse_pull_request({**node, "author": None})["author"] is None


def test_reviewer_rating_parser():
    from harbor.risk_evidence import reviewer_rating

    def facts(*comments, author=DELIVERY):
        return {"author": author, "issue_comments": [
            {"author": c["author"]["login"], "body": c["body"], "surface": "issue"} for c in comments]}

    assert reviewer_rating(facts(verdict("low")), HEAD) == "low"
    assert reviewer_rating(facts(verdict("low"), verdict("high"), verdict("medium")), HEAD) == "high"
    # Login comparison is case-insensitive, as GitHub logins are.
    assert reviewer_rating(facts(verdict("low", author=DELIVERY.upper())), HEAD) == "low"
    assert reviewer_rating(facts(verdict("low")), STALE) is None
    assert reviewer_rating(facts(verdict("low"), author=None), HEAD) is None
    # Ordinary prose and other JSON from the delivery identity are ignored.
    prose = {"author": {"login": DELIVERY}, "body": "Looks good, low risk."}
    other_json = {"author": {"login": DELIVERY}, "body": json.dumps({"summary": "x"})}
    assert reviewer_rating(facts(prose, other_json, verdict("low")), HEAD) == "low"
    # Malformed or ambiguous current-head verdicts fail closed.
    for bad in (verdict("lowish"), verdict(None), verdict("low", verdict="APPROVE"), verdict("low", verdict="maybe")):
        assert reviewer_rating(facts(verdict("low"), bad), HEAD) is None
    no_head = {"author": {"login": DELIVERY}, "body": json.dumps({"review": {"verdict": "approve", "risk": "low"}})}
    not_object = {"author": {"login": DELIVERY}, "body": json.dumps({"review": "approve"})}
    assert reviewer_rating(facts(verdict("low"), no_head), HEAD) is None
    assert reviewer_rating(facts(verdict("low"), not_object), HEAD) is None
    # Prose wrapped around the JSON is not the exact verdict object.
    wrapped = {"author": {"login": DELIVERY}, "body": "Verdict:\n" + verdict("low")["body"]}
    assert reviewer_rating(facts(wrapped), HEAD) is None


# --- builder `## Risk Analysis` tier -------------------------------------------------------------

def test_medium_floor_is_not_lowered_by_low_analysis(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: medium", body=analysis("Risk tier: low"))
    assert risk_blocked(out) and code == 1
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: medium", body=analysis("Risk tier: low"),
                          auto_merge_risks=("medium",))
    assert passes(out), out["results"][0]["reason"]


def test_high_analysis_raises_a_low_floor(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=analysis("Risk tier: high"),
                          auto_merge_risks=("low", "medium"))
    assert risk_blocked(out)
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=analysis("Risk tier: high"),
                          auto_merge_risks=("low", "high"))
    assert passes(out), out["results"][0]["reason"]


@pytest.mark.parametrize("body", [
    pytest.param(analysis("Risk tier: low", sha=STALE), id="stale-sha"),
    pytest.param(analysis("Rationale says this is low risk."), id="missing-tier"),
    pytest.param(analysis("Risk tier: lowish"), id="unparseable-tier"),
    pytest.param(analysis("Risk: low"), id="wrong-format"),
    pytest.param("## Summary\nRisk tier: low\n", id="missing-analysis"),
    pytest.param(None, id="no-body"),
])
def test_unusable_analysis_fails_closed_even_with_low_floor_and_reviewer(tmp_path, monkeypatch, capsys, body):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=body)
    assert risk_blocked(out) and code == 1


def test_marker_and_tier_must_sit_in_the_risk_analysis_section(tmp_path, monkeypatch, capsys):
    body = f"## Risk Analysis\nRisk tier: high\n\n## Notes\nRisk tier: low\n<!-- harbor:risk-sha:{HEAD} -->\n"
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=body)
    assert risk_blocked(out)


def test_analysis_tier_parser():
    from harbor.risk_evidence import analysis_tier

    assert analysis_tier(analysis("Risk tier: low"), HEAD) == "low"
    assert analysis_tier(analysis("  Risk tier: MEDIUM  "), HEAD) == "medium"
    assert analysis_tier(analysis("Risk tier: low\nRisk tier: high"), HEAD) == "high"
    assert analysis_tier(analysis("Risk tier: low"), STALE) is None
    assert analysis_tier(analysis("**Risk tier:** low"), HEAD) is None
    assert analysis_tier(analysis("Risk tier: low"), None) is None
    assert analysis_tier(None, HEAD) is None


def test_merge_risk_retains_the_highest_tier_across_heads():
    from harbor.risk_evidence import merge_risk

    def facts(body, head, *comments):
        return {"head_sha": head, "body": body, "author": DELIVERY, "issue_comments": [
            {"author": c["author"]["login"], "body": c["body"], "surface": "issue"} for c in comments]}

    db = sqlite3.connect(":memory:")
    raw = {"description": ""}
    assert merge_risk(db, "A", "A-1", "run", raw, facts(analysis("Risk tier: low"), HEAD, verdict("high"))) == "high"
    # A later head rated low by both builder and reviewer cannot drop the observed tier.
    later = facts(analysis("Risk tier: low", sha=STALE), STALE, verdict("low", head=STALE))
    assert merge_risk(db, "A", "A-1", "run", raw, later) == "high"
    # A head with no current reviewer rating, or no builder tier, is unknown.
    assert merge_risk(db, "A", "A-1", "run", raw, facts(analysis("Risk tier: low"), HEAD)) is None
    assert merge_risk(db, "A", "A-1", "run", raw, facts(None, HEAD, verdict("low"))) is None
    # A Linear floor seen while the risk is still unknown is retained.
    assert merge_risk(db, "A", "A-2", "run", {"description": "Risk: medium"}, facts(None, HEAD)) is None
    assert merge_risk(db, "A", "A-2", "run", raw, facts(analysis("Risk tier: low"), HEAD, verdict("low"))) == "medium"
