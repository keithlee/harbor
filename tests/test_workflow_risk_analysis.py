"""pre_merge risk = max(Linear floor, current-head `## Risk Analysis` tier)."""
import asyncio
import json
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


def analysis(tier_line, sha=HEAD):
    return (
        "Summary of the change.\n\n## Risk Analysis\n"
        f"{tier_line}\nRationale: docs-only change, trivially reversible.\n"
        f"<!-- harbor:risk-sha:{sha} -->\n"
    )


def pre_merge(tmp_path, monkeypatch, capsys, *, description, body, auto_merge_risks=("low",)):
    """Run `harbor workflow check --event pre_merge` against mocked Linear/GitHub."""

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
                "number": 1, "headRefOid": HEAD, "url": PR_URL, "merged": False,
                "mergeable": "MERGEABLE", "body": body,
                "statusCheckRollup": {"contexts": {"nodes": [{"name": "tests", "conclusion": "SUCCESS"}]}},
            }}

        async def close(self):
            pass

    monkeypatch.setattr("harbor.clients.linear.LinearClient", Linear)
    monkeypatch.setattr("harbor.clients.github.GitHubClient", GitHub)
    workflow = config(tmp_path, "autonomous", extra=f"[delivery]\nauto_merge_risks = {json.dumps(list(auto_merge_risks))}\n")
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


def test_no_floor_and_current_low_analysis_passes(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="No risk line here.", body=analysis("Risk tier: low"))
    assert out["ready"] is True and code == 0, out["results"][0]["reason"]
    assert out["results"][0]["reason"] == "configured requirements satisfied"


def test_medium_floor_is_not_lowered_by_low_analysis(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: medium", body=analysis("Risk tier: low"))
    assert out["ready"] is False and code == 1
    assert RISK_FAIL in out["results"][0]["reason"]
    # And medium is the effective tier: allowing medium lets it through.
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: medium", body=analysis("Risk tier: low"),
                          auto_merge_risks=("medium",))
    assert out["ready"] is True, out["results"][0]["reason"]


def test_high_analysis_raises_a_low_floor(tmp_path, monkeypatch, capsys):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=analysis("Risk tier: high"))
    assert out["ready"] is False and RISK_FAIL in out["results"][0]["reason"]
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=analysis("Risk tier: high"),
                          auto_merge_risks=("low", "medium"))
    assert out["ready"] is False and RISK_FAIL in out["results"][0]["reason"]
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=analysis("Risk tier: high"),
                          auto_merge_risks=("high",))
    assert out["ready"] is True, out["results"][0]["reason"]


@pytest.mark.parametrize("body", [
    pytest.param(analysis("Risk tier: low", sha=STALE), id="stale-sha"),
    pytest.param(analysis("Rationale says this is low risk."), id="missing-tier"),
    pytest.param(analysis("Risk tier: lowish"), id="unparseable-tier"),
    pytest.param(analysis("Risk: low"), id="wrong-format"),
    pytest.param("## Summary\nRisk tier: low\n", id="missing-analysis"),
    pytest.param(None, id="no-body"),
])
def test_unusable_analysis_fails_closed_even_with_a_low_floor(tmp_path, monkeypatch, capsys, body):
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="Risk: low", body=body)
    assert out["ready"] is False and code == 1
    assert RISK_FAIL in out["results"][0]["reason"]


def test_marker_and_tier_must_sit_in_the_risk_analysis_section(tmp_path, monkeypatch, capsys):
    body = f"## Risk Analysis\nRisk tier: high\n\n## Notes\nRisk tier: low\n<!-- harbor:risk-sha:{HEAD} -->\n"
    code, out = pre_merge(tmp_path, monkeypatch, capsys, description="", body=body)
    assert out["ready"] is False and RISK_FAIL in out["results"][0]["reason"]


def test_analysis_tier_parser():
    from harbor.risk_evidence import analysis_tier

    assert analysis_tier(analysis("Risk tier: low"), HEAD) == "low"
    assert analysis_tier(analysis("  Risk tier: MEDIUM  "), HEAD) == "medium"
    assert analysis_tier(analysis("Risk tier: low\nRisk tier: high"), HEAD) == "high"
    assert analysis_tier(analysis("Risk tier: low"), STALE) is None
    assert analysis_tier(analysis("**Risk tier:** low"), HEAD) is None
    assert analysis_tier(analysis("Risk tier: low"), None) is None
    assert analysis_tier(None, HEAD) is None


def test_merge_risk_retains_the_floor_across_heads():
    import sqlite3
    from harbor.risk_evidence import merge_risk

    db = sqlite3.connect(":memory:")
    raw = {"description": ""}
    assert merge_risk(db, "A", "A-1", "run", raw, analysis("Risk tier: high"), HEAD) == "high"
    # A later push re-analysed as low cannot drop the observed tier.
    assert merge_risk(db, "A", "A-1", "run", raw, analysis("Risk tier: low", sha=STALE), STALE) == "high"
    # A head with no current analysis is unknown, whatever was retained.
    assert merge_risk(db, "A", "A-1", "run", raw, analysis("Risk tier: low"), STALE) is None
    # A Linear floor seen without any analysis is still retained.
    assert merge_risk(db, "A", "A-2", "run", {"description": "Risk: medium"}, None, HEAD) is None
    assert merge_risk(db, "A", "A-2", "run", raw, analysis("Risk tier: low"), HEAD) == "medium"
