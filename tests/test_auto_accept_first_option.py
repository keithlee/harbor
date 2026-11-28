"""Harbor and First Mate auto-accept a blocker's recommended FIRST option.

When the builder's recommendation picks option 1, Harbor records option 1 as
the escalation's answer and resumes the retained builder session instead of
waiting for a human, within the guardrails pinned here: no merge authority,
a loop guard and per-run cap, no secrets/credentials/money/data deletion, a
Linear comment plus a distinct ledger receipt, and a workflow opt-out.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
from pathlib import Path

import pytest

from harbor import auto_accept, first_mate_recovery as recovery
from harbor.auto_accept import AutoAcceptPolicy, policy_from_config, recommended_first_option
from harbor.config.workflow import load_workflow
from harbor.db import HarborDB
from harbor.readiness import evaluate
from harbor.recovery import BlockerPolicy
from harbor.runner.base import validate_result
from harbor.supervisor import Supervisor, _RecoverySuperseded
from harbor.unblock import UnblockLadder
from workflow_support import config

QUESTION = "The empty state has no copy yet. Which wording should it use?"
OPTIONS = ["Use the short wording from the design file", "Write a longer explanatory paragraph"]
# The review's original fixture: a merge question must never be auto-accepted.
MERGE_QUESTION = "The base branch is protected. How should the PR be merged?"
MERGE_OPTIONS = ["Keep the protection and merge it yourself", "Temporarily lift the protection so the builder can merge"]


def run(awaitable):
    return asyncio.run(awaitable)


def ticket():
    return {"identifier": "SAMPLE-7", "id": "linear-7", "title": "Blocked", "state": "In Progress"}


def blocker(recommendation="Take the first option.", *, question=QUESTION, options=OPTIONS, index=0, kind="product_decision"):
    """A builder blocker; ``index=None`` omits ``recommended_option_index``."""

    value = {"kind": kind, "question": question, "options": list(options), "recommendation": recommendation,
             "evidence": "the design file lists two variants"}
    if index is not None:
        value["recommended_option_index"] = index
    return value


class LinearFake:
    def __init__(self):
        self.comments = []
        self.states = []

    async def create_comment(self, issue_id, body):
        self.comments.append((issue_id, body))
        return {"comment": {"id": f"comment-{len(self.comments)}"}}

    async def update_issue_state(self, issue_id, state):
        self.states.append((issue_id, state))
        return {"success": True}


class Harness:
    def __init__(self, tmp_path: Path, **ladder):
        self.db = HarborDB(tmp_path / "harbor.db")
        self.linear = LinearFake()
        self.resumed: list[tuple[str, str]] = []
        self.notices: list[str] = []
        self.on_resume = None

        async def resume(run_id, prompt):
            self.resumed.append((run_id, prompt))
            if self.on_resume is not None:
                await self.on_resume()
            return True

        async def notify(reason):
            self.notices.append(reason)

        self.ladder = UnblockLadder(self.db, linear=self.linear, resumer=resume, notifier=notify,
                                    remediation_executor=lambda command: False, **ladder)

    def block(self, value, final=None):
        final = {**(final or {}), "blocker": value}
        return run(self.ladder.handle(ticket(), {"id": "builder-session"}, final))

    def receipts(self):
        return auto_accept.history(self.db, "SAMPLE-7")


@pytest.fixture
def harness(tmp_path):
    value = Harness(tmp_path)
    yield value
    value.db.close()


# --------------------------------------------------------------------------
# Deterministic decision
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    blocker(index=0, recommendation="Go with the short wording from the design file."),
    blocker(index=0, recommendation=None),
    blocker("Take the first option."),
    blocker("Option 1"),
    blocker("option #1: keep it"),
    blocker("I recommend option 1."),
    blocker("Choose the first option because it matches the design file"),
    blocker("Use the short wording from the design file"),  # exactly option 1
    blocker("Take option 1 because it matches the ticket criteria"),  # allowlisted words only
])
def test_first_option_recommendations_are_recognised(value):
    assert recommended_first_option(value).first


@pytest.mark.parametrize("value", [
    blocker(index=1, recommendation="Take the first option."),  # structured field says option 2
    blocker(index=0, recommendation="Option 2"),  # contradiction
    blocker(index=True),
    blocker(index=5),
    blocker("Option 2"),
    blocker("Take the second option."),
    blocker("Write a longer explanatory paragraph"),  # exactly option 2
    blocker("Do not take the first option."),
    blocker("Option 1 or option 2 both work"),
    blocker("The first option, unless the team wants speed"),
    # Review round 2, B3: there is no free-text fallback; without the index nothing counts.
    blocker("Use the short wording, from the design", index=None),
    blocker("Take the first option.", index=None),
    blocker("Option 1", index=None),
    blocker("Use the short wording from the design file", index=None),  # even exactly option 1
    blocker("", index=None),
    # ...and with index 0 the text may only agree (the reviewer's exact inputs).
    blocker("Option 1 was considered; recommend the longer paragraph"),
    blocker("Option 1 vs 2: the paragraph wins"),
    blocker("Option 1. Actually prefer the long paragraph"),
    blocker("Option 1 is suboptimal"),
    blocker("Option 1 except for the header"),
    blocker("Option 1: Write a longer explanatory paragraph"),
    blocker("Option ２"),  # full-width 2
    blocker("The 2nd option"),
    blocker("Plan B"),
    blocker("Option II"),
    blocker("Use 2"),
    blocker("Write the explanatory paragraph"),  # paraphrases option 2
    # Review round 3, B3: the text veto is an allowlist (the reviewer's inputs).
    blocker("Option 1 is incorrect"),
    blocker("Option 1 is invalid"),
    blocker("Option 1 is out of scope"),
    blocker("Option 1 is blocked by legal"),
    blocker("Skip option 1"),
    blocker("Option 1 is overkill; go lightweight"),
    blocker("~~Option 1~~"),
    blocker("Take the first option.", options=[]),
    # Review B3: conditions, hand-offs and letter labels are never a plain pick.
    blocker("Option 1 if the tests pass"),
    blocker("Option 1, but only after a human confirms"),
    blocker("Take option 1 once the reviewer approves"),
    blocker("Option 1 until we check with the team"),
    blocker("Option 1 provided nothing else breaks"),
    blocker("Go with option B", index=0),
    blocker("Option A is unsafe; take (b)", index=0),
    blocker("Take b)", index=0),
    blocker("The second one", index=0),
    blocker("The short wording is unsafe here", index=0),
    blocker("Not the short wording", index=0),
    blocker("Use the short wording if legal agrees", index=0),
])
def test_other_or_ambiguous_recommendations_are_not_first(value):
    assert not recommended_first_option(value).first


# --------------------------------------------------------------------------
# Ladder (rung 2)
# --------------------------------------------------------------------------

def test_recommended_first_option_is_auto_accepted_and_resumes_the_builder(harness):
    result = harness.block(blocker("Take the first option."))

    assert result.action == "resume" and result.resumed and result.same_session
    assert result.auto_accept["option"] == OPTIONS[0]
    escalation = harness.db.escalations()[-1]
    assert escalation["status"] == "answered" and escalation["answer"] == OPTIONS[0]
    assert escalation["citation"] == f"harbor:auto-accept-first-option:escalation-{escalation['id']}"
    assert escalation["resolution_id"] is None  # not a human answer, not reusable authority
    assert harness.db.connection.execute("SELECT count(*) FROM resolutions").fetchone()[0] == 0
    # Same retained session, answered with option 1, no merge authority.
    run_id, prompt = harness.resumed[0]
    assert run_id == "builder-session"
    assert f"Answer: {OPTIONS[0]}" in prompt
    assert "does not authorize a merge" in prompt
    # One Linear comment naming the escalation; no park, no page.
    [(issue, body)] = harness.linear.comments
    assert issue == "linear-7"
    assert f"auto-accepted option 1 for SAMPLE-7 (escalation #{escalation['id']})" in body
    assert "does not authorize a merge" in body
    assert harness.linear.states == [] and harness.notices == []
    [receipt] = harness.receipts()
    assert receipt["escalation_id"] == escalation["id"] and receipt["source"] == "ladder"
    assert receipt["option"] == OPTIONS[0] and receipt["method"] == "structured"


def test_structured_index_zero_accepts_a_paraphrased_recommendation(harness):
    result = harness.block(blocker("Go with the short wording from the design file.", index=0))
    assert result.action == "resume"
    assert harness.receipts()[0]["method"] == "structured"


@pytest.mark.parametrize("value", [
    blocker("Option 2"),
    blocker("Take the first option.", index=1),
    blocker("Take the first option.", index=None),  # no structured index: no fallback
    blocker("Option 1 was considered; recommend the longer paragraph"),
    blocker("Option 1: Write a longer explanatory paragraph"),
])
def test_option_two_or_ambiguous_recommendation_waits_for_a_human(harness, value):
    result = harness.block(value)

    assert result.action == "park" and not result.resumed
    assert harness.resumed == [] and harness.receipts() == []
    assert harness.db.escalations()[-1]["status"] == "open"
    assert harness.linear.states == [("linear-7", "Human Review")]
    assert "auto-accept" not in harness.linear.comments[0][1].casefold()


def test_loop_guard_leaves_a_repeated_question_for_a_human(harness):
    first = harness.block(blocker("Take the first option."))
    assert first.action == "resume"

    # The resumed builder blocks again with an equivalent question.
    again = harness.block(blocker("Take the first option.", question=QUESTION.replace("Which", "which") + " "))

    assert again.action == "park" and len(harness.resumed) == 1
    assert len(harness.receipts()) == 1
    assert harness.db.escalations()[-1]["status"] == "open"
    body = harness.linear.comments[-1][1]
    assert "Auto-accept of option 1 not applied" in body
    assert f"already auto-accepted this recommendation for escalation #{first.escalation['id']}" in body


def test_loop_guard_holds_when_the_builder_reblocks_inside_the_resumed_session(harness):
    nested = []

    async def reblock():
        harness.on_resume = None
        nested.append(await harness.ladder.handle(ticket(), {"id": "builder-session"},
                                                  {"blocker": blocker("Option 1")}))

    harness.on_resume = reblock
    outer = harness.block(blocker("Option 1"))

    assert outer.action == "resume"
    assert nested[0].action == "park"
    assert len(harness.receipts()) == 1


def test_at_most_n_auto_accepts_per_run(tmp_path):
    harness = Harness(tmp_path, auto_accept=AutoAcceptPolicy(max_auto_accepts=2))
    try:
        for number, layout in enumerate(("compact", "spacious", "centred")):
            harness.block(blocker("Option 1", question=f"Question {number} about layout {number * 7}",
                                  options=[f"Use the {layout} layout", "Something else"]))
        assert [len(harness.resumed), len(harness.receipts())] == [2, 2]
        assert harness.db.escalations()[-1]["status"] == "open"
        assert "limit of 2 auto-accepts per builder run" in harness.linear.comments[-1][1]
    finally:
        harness.db.close()


@pytest.mark.parametrize("first_option", [
    "Provide the production API key in the environment",
    "Add the deploy token to the repository secrets",
    "Supply the service account password",
    "Approve deleting the legacy user records",
    "Buy the paid plan for the service",
    # Review round 2, B2: env-var identifiers, .env, PATs, certificates, destructive data ops.
    "Set STRIPE_SECRET_KEY in the deployment environment",
    "Export GITHUB_TOKEN so the builder can push",
    "Add DATABASE_PASSWORD to the service config",
    "Drop the users table and recreate it",
    "Copy the values from .env into the container",
    "Generate a PAT for the bot account",
    "Upload the TLS certificate",
    "Run git reset --hard on the release branch",
    "Truncate the audit_records table",
])
def test_secret_credential_money_or_deletion_keywords_keep_the_question_with_a_human(harness, first_option):
    result = harness.block(blocker("Take the first option.", question="How should Harbor proceed?",
                                   options=[first_option, "Skip it"]))

    assert result.action == "park" and harness.resumed == [] and harness.receipts() == []
    assert "secrets, credentials, money or deleting data" in harness.linear.comments[0][1]


def test_a_protected_path_blocker_is_never_auto_accepted(tmp_path):
    harness = Harness(tmp_path, protected_paths=("infra/**",))
    try:
        result = harness.block({**blocker("Option 1"), "touched_paths": ["infra/deploy.yml"]})
        assert result.action == "park" and harness.resumed == [] and harness.receipts() == []
        assert "touches a protected path" in harness.linear.comments[0][1]
    finally:
        harness.db.close()


@pytest.mark.parametrize("ladder", [
    {"policy": BlockerPolicy(decision_mode="human")},  # triage stops at "project requires human decisions"
    {"rung_one_cap": 0},  # triage stops at "same-kind rung-1 cap reached"
    {"policy": BlockerPolicy(decision_mode="cite_only")},
])
def test_first_mate_never_auto_accepts_a_protected_path_blocker_the_ladder_parked(tmp_path, ladder):
    """Review B1: triage can escalate before its protected-path check, so the
    escalation reason need not say "protected path"; First Mate must still refuse."""

    harness = Harness(tmp_path, protected_paths=("infra/**",), **ladder)
    try:
        value = {**blocker("Option 1"), "touched_paths": ["infra/deploy.yml"]}
        parked = harness.block(value)
        assert parked.action == "park" and harness.resumed == []
        case = recovery.Case("Sample", "SAMPLE-7", "builder-session", "blocked", "blocked", value, linear_id="linear-7")
        decision = recovery.apply_auto_accept(harness.db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy())
        assert decision["action"] == "escalate"
        assert auto_accept.history(harness.db, "SAMPLE-7") == []
        assert harness.db.escalations()[-1]["status"] == "open"
    finally:
        harness.db.close()


def test_first_mate_refuses_an_escalation_without_a_recorded_assessment(tmp_path):
    db = HarborDB(tmp_path / "harbor.db")
    try:
        value = blocker("Option 1")
        db.record_escalation("SAMPLE-7", "builder-session", value["kind"], value["question"], 2, status="open")
        case = recovery.Case("Sample", "SAMPLE-7", "builder-session", "blocked", "blocked", value)
        decision = recovery.apply_auto_accept(db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy())
        assert decision["action"] == "escalate" and "no protected-path assessment" in decision["reason"]
    finally:
        db.close()


def test_human_decision_mode_disables_auto_accept(tmp_path):
    harness = Harness(tmp_path, policy=BlockerPolicy(decision_mode="human"))
    try:
        assert harness.block(blocker("Option 1", index=0)).action == "park"
        assert harness.resumed == [] and harness.receipts() == []
    finally:
        harness.db.close()
    policy = policy_from_config(load_workflow(config(tmp_path, extra='[decisions]\nmode = "human"\n')).config)
    assert not policy.enabled
    assert auto_accept.describe(policy) == 'Auto-accept first option: off (decisions.mode = "human")'


@pytest.mark.parametrize("question,first_option", [
    (MERGE_QUESTION, MERGE_OPTIONS[0]),
    ("How should Harbor proceed?", "Merge the PR now"),
    ("How should Harbor proceed?", "Force-push over main"),
    ("How should Harbor proceed?", "Disable branch protection"),
    ("How should Harbor proceed?", "Skip the failing CI check"),
    ("How should Harbor proceed?", "Mark the risk as low"),
    ("How should Harbor proceed?", "Waive the screenshot proof"),
    ("How should Harbor proceed?", "Edit workflow.toml to allow auto merge"),
    ("Should the reviewer be skipped?", "Yes, continue without it"),
    # N2 terms, added with the B2 normalization.
    ("How should Harbor proceed?", "Push the fix straight to main"),
    ("How should Harbor proceed?", "Mark the flaky test as xfail"),
    ("How should Harbor proceed?", "Remove the failing tests"),
    ("How should Harbor proceed?", "Lower the coverage threshold"),
    ("How should Harbor proceed?", "Add the bot to CODEOWNERS"),
    ("How should Harbor proceed?", "Set merge_mode to workflow_checked"),
])
def test_merge_and_gate_lowering_keywords_keep_the_question_with_a_human(harness, question, first_option):
    """Review B4: best-effort keyword heuristic for merge and gate topics (not a guarantee)."""

    result = harness.block(blocker("Take the first option.", question=question, options=[first_option, "Wait"]))
    assert result.action == "park" and harness.resumed == [] and harness.receipts() == []
    assert "merging or lowering a gate" in harness.linear.comments[0][1]


@pytest.mark.parametrize("first_option", [
    # Review round 3, B1: the reviewer's end-to-end inputs.
    "git push -f origin HEAD:main",
    "rm -fr data/ and regenerate",
    "Set STRIPE_KEY in the runner env",
    "Upgrade the Vercel plan to Pro at $20/month",
    "Land the PR now",
    "Commit with --no-verify",
    "Run git clean -fdx",
    "Squash and land",
    "Ship it straight to main",
    "Deploy to production",
    "Overwrite production data",
    "Discard the uncommitted work",
    "continue-on-error: true",
    "Share the pass­word with the bot",  # soft hyphen
    "Paste the sec​ret into the form",  # zero-width space
    "Paste the sеcret into the form",  # Cyrillic e
    "Use `make release`",
    "Open https://example.test/settings",
    "Bump the timeout to ninety seconds; it is fine",
])
def test_non_prose_first_options_are_not_auto_accepted(harness, first_option):
    result = harness.block(blocker("Option 1", question="How should Harbor proceed?", options=[first_option, "Wait"]))
    assert result.action == "park" and harness.resumed == [] and harness.receipts() == []
    assert "Auto-accept of option 1 not applied" in harness.linear.comments[0][1]


@pytest.mark.parametrize("recommendation", [
    "Option 1 is incorrect", "Option 1 is invalid", "Option 1 is out of scope", "Option 1 is blocked by legal",
    "Skip option 1", "Option 1 is overkill; go lightweight", "~~Option 1~~",
])
def test_negative_recommendations_with_index_zero_wait_for_a_human(harness, recommendation):
    """Review round 3, B3: the reviewer's inputs, end to end."""

    result = harness.block(blocker(recommendation))
    assert result.action == "park" and harness.resumed == [] and harness.receipts() == []


@pytest.mark.parametrize("kind", ["env_blocker", "conflicting_instruction", "github_transport",
                                  "missing_proof_capability", "access_request", "permission"])
def test_only_pure_decision_kinds_are_auto_accepted(harness, kind):
    result = harness.block(blocker("Option 1", kind=kind))
    assert not result.resumed and harness.resumed == [] and harness.receipts() == []


def test_first_mate_never_auto_accepts_a_non_decision_kind(tmp_path):
    db = HarborDB(tmp_path / "harbor.db")
    try:
        case = _case(db, blocker("Option 1", kind="env_blocker"))
        decision = recovery.apply_auto_accept(db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy())
        assert decision["action"] == "escalate" and auto_accept.history(db, "SAMPLE-7") == []
    finally:
        db.close()


@pytest.mark.parametrize("value", [
    # Review round 3, B2: the reviewer's repro -- the path is only mentioned.
    blocker("Option 1", options=["Edit infra/deploy.yml to add FEATURE_FLAG", "Wait"]),
    # Case-folded, and mentioned only in the question.
    blocker("Option 1", question="Should Infra/Deploy.yml keep two replicas?", options=["Keep two replicas", "Wait"]),
    # The protected directory named as a bare word.
    blocker("Option 1", question="How should the manifests change?", options=["Update the infra manifests", "Wait"]),
])
def test_a_protected_path_that_is_only_mentioned_is_never_auto_accepted(tmp_path, value):
    harness = Harness(tmp_path, protected_paths=("infra/**",))
    try:
        result = harness.block(value)
        assert result.action == "park" and harness.resumed == [] and harness.receipts() == []
        escalation = harness.db.escalations()[-1]
        assert auto_accept.eligibility(harness.db, escalation["id"])["protected"] is True
    finally:
        harness.db.close()


@pytest.mark.parametrize("first_option", [
    "Empty the orders table",
    "Add a service fee to every order",
    "Paste the shared passcode into the readme",
    "Comment out the flaky assertions so the suite goes green",
    "Edit the harbor workflow file so the builder runs unattended",
])
def test_documented_limitation_keyword_heuristics_are_not_semantic(harness, first_option):
    """DOCUMENTED LIMITATION, not a guarantee (review round 4, B1).

    The money, deletion, credential and gate exclusions are best-effort keyword
    heuristics.  A plain-English synonym is not recognised, so these reviewer
    inputs ARE auto-accepted.  The safeguards are the structural rules and the
    opt-out (``auto_accept_first_option = false`` / ``decisions.mode = "human"``),
    not this filter.  If this test starts failing because a word list grew,
    keep the docs' claim narrow anyway: no word list can decide meaning.
    """

    result = harness.block(blocker("Option 1", question="How should Harbor proceed?", options=[first_option, "Wait"]))
    assert result.action == "resume"
    assert "best-effort keyword heuristics" in harness.linear.comments[-1][1]


@pytest.mark.parametrize("value", [
    # The recommendation allowlist is lexical: every word is allowed, the meaning is not checked.
    blocker("Use the existing date picker", options=["Build a new date picker", "Reuse the old one"]),
    # A file name without a dot is just a word.
    blocker("Option 1", options=["Simplify the Dockerfile", "Wait"]),
])
def test_documented_limitation_lexical_checks_as_described(value):
    """DOCUMENTED LIMITATIONS (review round 5, B1): the docs describe these
    checks as lexical; this pins what the code does, not a guarantee."""

    # With no ledger, assess stops at the loop-guard step; reaching it means
    # every eligibility check above it passed.
    assessment = auto_accept.assess(None, "SAMPLE-7", "run", value, AutoAcceptPolicy())
    assert recommended_first_option(value).first
    assert assessment.status == "refused" and "no Harbor ledger" in assessment.reason


@pytest.mark.parametrize("text,recommendation", [
    # Review round 6, B1: the character check runs on the raw text.
    ("Take option\n#\t1", True),
    ("Take option\x1c\x1d\x1e1", True),
    ("Take option  1", True),  # two spaces: not the literal phrase
    ("Take the first\toption", True),
    ("Use the ｓhort wording", False),  # full-width letter: NFKC would have folded it
    ("Use the short wording #", False),  # "#" only inside the literal "option #1"
    ("Take it: now", True),  # a colon only directly after a leading "option 1"
    # Review round 6, B2: no hyphen anywhere, so no wrapped flags.
    ("Remove it (-rf)", False),
    ('Run it "-v"', False),
    ("Run it '-x'", False),
    ("Use the dark-mode toggle", False),
    ("Use the dark--mode toggle", False),
    ("Use the dark-mode- toggle", False),
    # Review round 6, B3: dotfiles, inner dots and camelCase.
    ("Edit the .gitignore", False),
    ("Tune the .npmrc", False),
    ("Add a .prettierrc", False),
    ("Use e.g. the short one", False),
    ("Call fetchUserData again", False),
    ("Use the SHORT wording", False),
])
def test_plain_prose_rules_exactly_as_documented(text, recommendation):
    assert auto_accept.plain_prose_reason(text, recommendation=recommendation) is not None


@pytest.mark.parametrize("text,recommendation", [
    ("Option 1", True),
    ("option #1", True),
    ("Option 1: it matches the ticket criteria.", True),
    ("I recommend option 1.", True),
    ("Use the short wording from the design file.", False),
    ("Keep the wording (it's simplest)!", False),
])
def test_plain_prose_accepts_exactly_the_documented_set(text, recommendation):
    assert auto_accept.plain_prose_reason(text, recommendation=recommendation) is None


def test_docs_describe_the_rules_as_the_code_enforces_them():
    root = Path(__file__).resolve().parents[1]
    docs = (root / "docs" / "configurable-workflows.md").read_text()
    section = docs.split("### Auto-accepting the builder's first option", 1)[1].split("\n### ", 1)[0]
    structural, lexical = section.split("#### Lexical checks", 1)
    assert "firstmate_decisions" not in section  # a retired pipeline.md key
    assert "enforced exactly" not in docs
    assert "80% word overlap" in lexical and "Dockerfile" in lexical and "camelCase" in lexical
    # Review round 6: the character set is documented exactly as the code defines it.
    assert "the space character (0x20) and `, . ' \" ! ? ( )`" in " ".join(structural.split())
    assert set(auto_accept.PROSE_CHARACTERS) == set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz ,.'\"!?()")
    assert "directly (`Option 1: ...`)" in structural and "after NFKC" not in section
    assert "date picker" in lexical
    for word in ("overlap", "allowlist", "best-effort"):
        assert word not in structural.split("#### Structural checks", 1)[1], word
    readme = (root / "README.md").read_text()
    assert "equivalent question twice" not in readme and "80% word overlap" in readme
    skill = (root / "workflows" / "work-pipeline" / "skills" / "delivery" / "SKILL.md").read_text()
    for kind in sorted(auto_accept.ELIGIBLE_KINDS):
        assert kind in skill and kind in section
    assert "(no commands, paths, numbers or identifiers)" not in skill  # review round 6, B4
    assert "docs/configurable-workflows.md" in skill
    # Review round 9, N2: the skill states the "option 1" exception, and its own
    # example recommendation passes the rules it describes.
    flat_skill = " ".join(skill.split())
    assert "the literal phrase `option 1` or `option #1`" in flat_skill
    example = '"Take option 1 because it matches the ticket criteria"'
    assert example in flat_skill
    assert auto_accept.plain_prose_reason(example.strip('"'), recommendation=True) is None
    assert recommended_first_option(blocker(example.strip('"'))).first
    template = (root / "workflows" / "work-pipeline" / "workflow.toml").read_text()
    assert "twice for the same question" not in template


def test_a_protected_nested_directory_named_as_a_word_is_not_auto_accepted(tmp_path):
    """Review round 4, B2 (the reviewer's repro): every specific segment counts."""

    harness = Harness(tmp_path, protected_paths=("src/harbor/security/**",))
    try:
        result = harness.block(blocker("Option 1", question="How should the login flow change?",
                                       options=["Rewrite the security module", "Wait"]))
        assert result.action == "park" and harness.resumed == []
        assert auto_accept.eligibility(harness.db, harness.db.escalations()[-1]["id"])["protected"] is True
    finally:
        harness.db.close()


@pytest.mark.parametrize("pattern,option,protected", [
    ("src/harbor/worktree.py", "Simplify the worktree helper", True),  # file stem
    ("src/harbor/security/**", "Tidy the Security notes.", True),  # case-folded, sentence end
    ("src/harbor/security/**", "Refresh the source comments", False),  # generic "src" alone never counts
    ("infra/**", "Tune the infrastructure defaults", False),  # documented limitation: near-names
])
def test_protected_pattern_names_match_whole_words(tmp_path, pattern, option, protected):
    harness = Harness(tmp_path, protected_paths=(pattern,))
    try:
        harness.block(blocker("Option 1", question="Which wording should we use?", options=[option, "Wait"]))
        facts = auto_accept.eligibility(harness.db, harness.db.escalations()[-1]["id"])
        assert facts["protected"] is protected
    finally:
        harness.db.close()


def test_auto_accept_never_authorizes_a_merge(tmp_path):
    """The merge gate is read from workflow policy only; an answer changes none of it."""

    workflow = load_workflow(config(tmp_path))
    harness = Harness(tmp_path)
    harness.ladder.workflow = workflow
    harness.ladder.workflow_project = "Sample"
    before = copy.deepcopy(workflow.config)
    context = {"fresh": True, "hold": False, "risk": "low", "gaps": {},
               "facts": {"head_sha": "abc", "url": "https://example.test/pr/1", "mergeable": "MERGEABLE", "ci": "green"}}
    gate_before = evaluate(workflow.config, context)
    try:
        # A merge-ready run waiting on the human merge click is never answered.
        parked = harness.block(blocker("Option 1"), final={"outcome": "parked", "waiting_on": "human"})
        assert parked.action == "park" and harness.resumed == []
        assert "never stands in for merge approval" in harness.linear.comments[-1][1]

        accepted = harness.block(blocker("Option 1", question="Which wording should the empty state use?",
                                         options=["Use the short wording", "Use the long wording"]))
        assert accepted.action == "resume"
        assert "does not authorize a merge" in harness.resumed[-1][1]
        assert harness.ladder.workflow.config == before
        gate_after = evaluate(harness.ladder.workflow.config, context)
        assert gate_after == gate_before
        assert gate_after["status"] == "fail" and "human merge required" in gate_after["reason"]
    finally:
        harness.db.close()


def test_workflow_opt_out_keeps_every_escalation_for_a_human(tmp_path):
    workflow = load_workflow(config(tmp_path, extra="[decisions]\nauto_accept_first_option = false\n"))
    harness = Harness(tmp_path)
    harness.ladder.workflow = workflow
    harness.ladder.workflow_project = "Sample"
    try:
        result = harness.block(blocker("Option 1", index=0))
        assert result.action == "park" and harness.resumed == [] and harness.receipts() == []
        assert "auto-accept" not in harness.linear.comments[0][1].casefold()
    finally:
        harness.db.close()


def test_workflow_setting_is_validated_and_defaults_on(tmp_path):
    assert policy_from_config(load_workflow(config(tmp_path)).config) == AutoAcceptPolicy(True, 2)
    assert policy_from_config(load_workflow(config(tmp_path, extra="[decisions]\nmax_auto_accepts = 1\n")).config) \
        == AutoAcceptPolicy(True, 1)
    for bad in ('auto_accept_first_option = "yes"', "max_auto_accepts = 11", "max_auto_accepts = true"):
        with pytest.raises(ValueError, match="decisions"):
            load_workflow(config(tmp_path, extra=f"[decisions]\n{bad}\n"))


def test_doctor_line_shows_the_setting():
    assert auto_accept.describe(AutoAcceptPolicy()).startswith("Auto-accept first option: on")
    assert "max" not in auto_accept.describe(AutoAcceptPolicy(enabled=False))
    assert "off" in auto_accept.describe(AutoAcceptPolicy(enabled=False))


def test_run_result_schema_carries_recommended_option_index():
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "run-result.json").read_text())
    node = schema["properties"]["blocker"]
    assert node["properties"]["recommended_option_index"]["type"] == ["integer", "null"]
    assert "recommended_option_index" in node["required"]
    base = {"status": "blocked", "summary": "s", "blocker": blocker(index=0)}
    assert validate_result(base) == []
    assert validate_result({**base, "blocker": blocker(index="0")}) == [
        "blocker.recommended_option_index must be an integer or null"]


# --------------------------------------------------------------------------
# First Mate recovery (escalate-or-recover)
# --------------------------------------------------------------------------

def _case(db, value, *, run_id="builder-session"):
    row = db.record_escalation("SAMPLE-7", run_id, value["kind"], value["question"], 2, status="open")
    auto_accept.record_eligibility(db, row["id"], value, protected=False, human_decisions=False)
    return recovery.Case("Sample", "SAMPLE-7", run_id, "blocked", "blocked", value, linear_id="linear-7")


def test_first_mate_never_answers_an_older_escalation_with_a_newer_blockers_option(tmp_path):
    """Review round 2, B1 (reviewer's repro): a resumed session keeps its run id.

    E1 (an unprotected question) is still open when the run re-blocks with Q2 on
    a protected path, and the ladder raises before rung 2, so no E2 is recorded.
    First Mate must not answer E1 with Q2's option."""

    harness = Harness(tmp_path, protected_paths=("infra/**",))
    try:
        first = harness.block(blocker("Option 2", question="Which colour should the banner use?",
                                      options=["Blue", "Green"], index=1))
        assert first.action == "park"
        e1 = harness.db.escalations()[-1]
        assert auto_accept.eligibility(harness.db, e1["id"])["protected"] is False
        q2 = {**blocker("Option 1", question="Should the deploy manifest set replicas to 3?",
                        options=["Set replicas to three", "Keep one replica"]),
              "touched_paths": ["infra/deploy.yml"]}
        case = recovery.Case("Sample", "SAMPLE-7", "builder-session", "blocked", "blocked", q2, linear_id="linear-7")
        decision = recovery.apply_auto_accept(harness.db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy())
        assert decision["action"] == "escalate"
        assert "different question" in decision["reason"]
        e1_after = harness.db.escalations()[-1]
        assert e1_after["id"] == e1["id"] and e1_after["status"] == "open" and e1_after["answer"] is None
        assert auto_accept.history(harness.db, "SAMPLE-7") == []
    finally:
        harness.db.close()


def test_first_mate_refuses_when_the_assessment_was_for_a_different_blocker(tmp_path):
    db = HarborDB(tmp_path / "harbor.db")
    try:
        recorded = _case(db, blocker("Option 1"))
        # Same question and kind, but the options changed since Harbor assessed it.
        case = recovery.Case("Sample", "SAMPLE-7", "builder-session", "blocked", "blocked",
                             {**recorded.blocker, "options": ["Use the tiny wording", OPTIONS[1]]}, linear_id="linear-7")
        decision = recovery.apply_auto_accept(db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy())
        assert decision["action"] == "escalate" and "different blocker" in decision["reason"]
    finally:
        db.close()


def test_first_mate_escalation_becomes_a_resume_with_option_one(tmp_path):
    db = HarborDB(tmp_path / "harbor.db")
    try:
        case = _case(db, blocker("Option 1"))
        escalated = recovery.validate({"action": "escalate", "reason": "needs a human"}, case, {})
        assert escalated["action"] == "escalate"

        decision = recovery.apply_auto_accept(db, case, escalated, AutoAcceptPolicy())

        assert decision["action"] == "resume"
        assert decision["instruction"].startswith(f"Answer: {OPTIONS[0]}")
        assert "does not authorize a merge" in decision["instruction"]
        # Deciding writes nothing; the answer is recorded at launch.
        assert db.escalations()[-1]["status"] == "open" and auto_accept.history(db, "SAMPLE-7") == []
        recovery.record_auto_accept(db, case, decision)
        escalation = db.escalations()[-1]
        assert f"First Mate auto-accepted option 1 for SAMPLE-7 (escalation #{escalation['id']})" \
            in recovery.auto_accept_comment(case, decision)
        assert escalation["status"] == "answered"
        assert auto_accept.history(db, "SAMPLE-7")[0]["source"] == "first_mate"

        # The same recommendation is never accepted twice: escalate, and say why.
        case = _case(db, blocker("Option 1"))
        again = recovery.apply_auto_accept(db, case, escalated, AutoAcceptPolicy())
        assert again["action"] == "escalate"
        assert "auto-accept of option 1 not applied: Harbor already auto-accepted" in again["reason"]
    finally:
        db.close()


@pytest.mark.parametrize("value,policy", [
    (blocker("Option 2"), AutoAcceptPolicy()),
    (blocker("Option 1"), AutoAcceptPolicy(enabled=False)),
    (blocker("Option 1", options=["Rotate the GitHub token", "Wait"]), AutoAcceptPolicy()),
])
def test_first_mate_keeps_escalating_option_two_opt_out_and_secrets(tmp_path, value, policy):
    db = HarborDB(tmp_path / "harbor.db")
    try:
        case = _case(db, value)
        decision = recovery.apply_auto_accept(db, case, {"action": "escalate", "instruction": "", "reason": "r"}, policy)
        assert decision["action"] == "escalate"
        assert db.escalations()[-1]["status"] == "open"
        assert auto_accept.history(db, "SAMPLE-7") == []
    finally:
        db.close()


# --------------------------------------------------------------------------
# First Mate delivery: answered only when delivered (review B2)
# --------------------------------------------------------------------------

class RecoveryHost:
    """Just the supervisor state ``Supervisor._schedule_recovery`` touches."""

    _schedule_recovery = Supervisor._schedule_recovery
    _auto_accept_withdrawn = Supervisor._auto_accept_withdrawn

    def __init__(self, db, *, eligible, apply, dispatcher=None, unblock_ladder=None):
        self.db = db
        self.dispatcher = dispatcher
        self.unblock_ladder = unblock_ladder
        self._recovery_tickets: set[str] = set()
        self._dispatch_tasks: dict = {}
        self.eligible = list(eligible)  # successive _first_mate_can_recover answers
        self.apply = apply
        self.comments: list[str] = []
        self.notices: list[str] = []

    def _first_mate_can_recover(self, case, reload_policy=False, attempted=False):
        return self.eligible.pop(0) if self.eligible else True

    async def _apply_recovery(self, case, decision, *, before_launch):
        await self.apply(before_launch)

    async def _post_recovery_comment(self, case, decision):
        self.comments.append(recovery.comment(case, decision))

    async def _notify_operator(self, key, message):
        self.notices.append(message)

    def _dispatch_finished(self, key, task):
        pass


async def _launched(before_launch):
    before_launch()


def _builder_started():
    """What a Harbor runner reports once the builder process exists."""

    from harbor.runner.base import SPAWN_OBSERVER

    observer = SPAWN_OBSERVER.get()
    observer.attempt()
    observer.start()


def _runner_failed_before_start():
    """A runner invocation that fails before creating the process."""

    from harbor.runner.base import SPAWN_OBSERVER

    SPAWN_OBSERVER.get().attempt()


async def _superseded_after_start(before_launch):
    before_launch()
    _builder_started()
    raise _RecoverySuperseded


async def _failed_after_start(before_launch):
    before_launch()
    _builder_started()
    raise RuntimeError("builder crashed mid-turn")


async def _cancelled_after_start(before_launch):
    before_launch()
    _builder_started()
    raise asyncio.CancelledError


async def _resume_failed(before_launch):
    # Models a NON-WorktreeDispatcher resume target (Supervisor.resume_session's
    # ``resume_session``/``resume`` seams), which calls before_launch() and then
    # fails, so _apply_recovery raises "not available to resume".  The real
    # WorktreeDispatcher returns None BEFORE before_launch when no retained
    # runner exists; see test_auto_accept_end_to_end for that case.
    before_launch()
    raise RuntimeError("retained builder session is not available to resume")


async def _runner_could_not_start(before_launch):
    before_launch()
    _runner_failed_before_start()  # deadline exhausted / executable missing; resume_session still returns True


def _schedule(tmp_path, *, eligible=(), apply=_launched, before=None):
    db = HarborDB(tmp_path / "harbor.db")
    case = _case(db, blocker("Option 1"))
    decision = recovery.apply_auto_accept(db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy())
    assert decision["action"] == "resume"
    if before is not None:
        before(db)
    host = RecoveryHost(db, eligible=eligible, apply=apply)

    async def go():
        host._schedule_recovery(case, decision)
        await asyncio.gather(*host._dispatch_tasks.values(), return_exceptions=True)

    run(go())
    return db, host


def test_first_mate_records_and_announces_only_a_delivered_answer(tmp_path):
    db, host = _schedule(tmp_path)
    try:
        escalation = db.escalations()[-1]
        assert escalation["status"] == "answered" and escalation["answer"] == OPTIONS[0]
        [receipt] = auto_accept.history(db, "SAMPLE-7")
        assert receipt["delivered"] == 1
        [body] = host.comments
        assert f"First Mate auto-accepted option 1 for SAMPLE-7 (escalation #{escalation['id']})" in body
    finally:
        db.close()


@pytest.mark.parametrize("eligible,apply", [
    ([False], _launched),  # held before the run starts: early return
    ([True, False], _launched),  # held at launch: _RecoverySuperseded from before_launch
])
def test_first_mate_leaves_the_escalation_open_when_nothing_is_handed_over(tmp_path, eligible, apply):
    db, host = _schedule(tmp_path, eligible=eligible, apply=apply)
    try:
        escalation = db.escalations()[-1]
        assert escalation["status"] == "open"
        assert escalation["answer"] is None and escalation["citation"] is None
        assert host.comments == []  # no comment claims a resume that did not happen
        assert auto_accept.history(db, "SAMPLE-7") == []
    finally:
        db.close()


@pytest.mark.parametrize("apply", [_resume_failed, _runner_could_not_start])
def test_first_mate_reopens_when_no_builder_started(tmp_path, apply):
    """Review round 8, B1: before_launch ran, but no builder process started
    (session unavailable, deadline exhausted, executable missing).  The
    answer never reached a builder: reopened, receipt undelivered, no
    comment, and no false "blocked again" on the next pass."""

    db, host = _schedule(tmp_path, apply=apply)
    try:
        escalation = db.escalations()[-1]
        assert escalation["status"] == "open" and escalation["answer"] is None
        assert [row["delivered"] for row in auto_accept.history(db, "SAMPLE-7")] == [0]
        assert host.comments == []
        _assert_truthful_undelivered_records(db, host, expect_undelivered_notice=apply is _runner_could_not_start)
        case = recovery.Case("Sample", "SAMPLE-7", "builder-session", "blocked", "blocked", blocker("Option 1"),
                             linear_id="linear-7")
        again = recovery.apply_auto_accept(db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy())
        assert again["action"] == "resume" and "blocked again" not in again["reason"]
    finally:
        db.close()


@pytest.mark.parametrize("apply", [_superseded_after_start, _failed_after_start, _cancelled_after_start])
def test_first_mate_keeps_an_answer_the_builder_already_received(tmp_path, apply):
    """Review round 7, B3: once a builder process started with option 1, a
    later failure or cancellation (/exit) does not make it "undelivered":
    the escalation stays answered, the receipt is delivered (so the loop guard
    holds), and the Linear comment is posted."""

    db, host = _schedule(tmp_path, apply=apply)
    try:
        escalation = db.escalations()[-1]
        assert escalation["status"] == "answered" and escalation["answer"] == OPTIONS[0]
        assert [row["delivered"] for row in auto_accept.history(db, "SAMPLE-7")] == [1]
        [body] = host.comments
        assert "First Mate auto-accepted option 1" in body
        # The same recommendation is not auto-accepted a second time.
        case = _case(db, blocker("Option 1"))
        again = recovery.apply_auto_accept(db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy())
        assert again["action"] == "escalate" and "already auto-accepted" in again["reason"]
    finally:
        db.close()


def test_ladder_keeps_an_answer_the_builder_received_before_a_cancellation(tmp_path):
    """Review round 7, B3, ladder path: a builder process starts with option 1,
    then the turn is cancelled."""

    harness = Harness(tmp_path)

    async def launch_then_cancel(run_id, prompt):
        _builder_started()
        harness.resumed.append((run_id, prompt))
        raise asyncio.CancelledError

    harness.ladder.resumer = launch_then_cancel
    try:
        with pytest.raises(asyncio.CancelledError):
            harness.block(blocker("Option 1"))
        escalation = harness.db.escalations()[-1]
        assert escalation["status"] == "answered" and escalation["answer"] == OPTIONS[0]
        assert [row["delivered"] for row in harness.receipts()] == [1]
        assert any("Harbor auto-accepted option 1" in body for _, body in harness.linear.comments)
    finally:
        harness.db.close()


def test_ladder_reopens_only_when_nothing_was_handed_over(tmp_path):
    harness = Harness(tmp_path)

    async def refuse_before_launch(run_id, prompt, *, before_launch=None):
        return False

    harness.ladder.resumer = refuse_before_launch
    try:
        assert harness.block(blocker("Option 1")).action == "park"
        assert harness.db.escalations()[-1]["status"] == "open"
        assert [row["delivered"] for row in harness.receipts()] == [0]
    finally:
        harness.db.close()


# --------------------------------------------------------------------------
# Review round 8, B1: a real CodexRunner decides whether the builder started
# --------------------------------------------------------------------------

def _codex(tmp_path, scenario):
    from harbor.runner.codex import CodexRunner

    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    if scenario == "missing_executable":
        return CodexRunner(worktree, ticket="SAMPLE-7", executable=str(tmp_path / "no-such-codex"))
    if scenario == "deadline_zero":
        return CodexRunner(worktree, ticket="SAMPLE-7", executable="/bin/sh", deadline_seconds=0)
    script = tmp_path / "fake-codex"
    script.write_text("#!/bin/sh\ncat > /dev/null\nexit 0\n")
    script.chmod(0o755)
    return CodexRunner(worktree, ticket="SAMPLE-7", executable=str(script), deadline_seconds=30)


@pytest.mark.parametrize("scenario", ["missing_executable", "deadline_zero"])
def test_ladder_with_a_real_runner_that_cannot_start_does_not_claim_delivery(tmp_path, scenario):
    runner = _codex(tmp_path, scenario)
    harness = Harness(tmp_path)

    async def resume_session(run_id, prompt):
        result = await runner.resume(run_id, prompt)
        assert result.status == "failed"
        return True  # Supervisor.resume_session returns True for any runner payload

    harness.ladder.resumer = resume_session
    try:
        result = harness.block(blocker("Option 1"))
        assert result.action == "park"
        escalation = harness.db.escalations()[-1]
        assert escalation["status"] == "open" and escalation["answer"] is None
        assert [row["delivered"] for row in harness.receipts()] == [0]
        assert not any("auto-accepted option 1" in body for _, body in harness.linear.comments)
        # The next pass does not claim the builder "blocked again".
        case = recovery.Case("Sample", "SAMPLE-7", "builder-session", "blocked", "blocked", blocker("Option 1"),
                             linear_id="linear-7")
        again = recovery.apply_auto_accept(harness.db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy())
        assert "blocked again" not in again["reason"]
    finally:
        harness.db.close()


def test_ladder_with_a_real_runner_that_starts_marks_delivery(tmp_path):
    runner = _codex(tmp_path, "starts")
    harness = Harness(tmp_path)

    async def resume_session(run_id, prompt):
        await runner.resume(run_id, prompt)
        return True

    harness.ladder.resumer = resume_session
    try:
        assert harness.block(blocker("Option 1")).action == "resume"
        assert [row["delivered"] for row in harness.receipts()] == [1]
        assert harness.db.escalations()[-1]["status"] == "answered"
        assert any("Harbor auto-accepted option 1" in body for _, body in harness.linear.comments)
    finally:
        harness.db.close()


@pytest.mark.parametrize("scenario,delivered", [("missing_executable", 0), ("deadline_zero", 0), ("starts", 1)])
def test_first_mate_with_a_real_runner(tmp_path, scenario, delivered):
    runner = _codex(tmp_path, scenario)

    async def apply(before_launch):
        await runner.resume("builder-session", "Answer: option 1", before_spawn=before_launch)
        # Supervisor.resume_session returns True for any payload, so _apply_recovery does not raise.

    db, host = _schedule(tmp_path, apply=apply)
    try:
        escalation = db.escalations()[-1]
        assert [row["delivered"] for row in auto_accept.history(db, "SAMPLE-7")] == [delivered]
        assert escalation["status"] == ("answered" if delivered else "open")
        assert bool(host.comments) is bool(delivered)
        if delivered:
            assert any("auto-accepted the recommended first option" in notice for notice in host.notices)
        else:
            _assert_truthful_undelivered_records(db, host, expect_undelivered_notice=True)
    finally:
        db.close()


def _assert_truthful_undelivered_records(db, host, *, expect_undelivered_notice):
    """Review round 9, B1: nothing says "auto-accepted" when no builder started."""

    assert not any("auto-accepted" in notice for notice in host.notices), host.notices
    if expect_undelivered_notice:
        assert any("was not delivered (builder did not start); escalation reopened" in notice
                   for notice in host.notices), host.notices
    reasons = [row[0] for row in db.connection.execute(
        "SELECT reason FROM first_mate_recoveries WHERE action = 'resume' ORDER BY id")]
    assert reasons and all("was not delivered (builder did not start)" in reason for reason in reasons), reasons
    assert not any("auto-accepted" in reason for reason in reasons)


def test_a_failing_hand_off_callback_never_leaves_a_builder_running(tmp_path):
    """Review round 9, N1: on_start raising (e.g. a locked ledger) must stop the
    builder process and clear the runner's custody."""

    from harbor.runner.base import SPAWN_OBSERVER, SpawnObserver

    runner = _codex(tmp_path, "starts")
    script = tmp_path / "fake-codex"
    script.write_text("#!/bin/sh\nsleep 30\n")  # would outlive the test if orphaned
    seen = {}

    def locked():
        seen["pid"] = runner.active.pid if runner.active else None
        raise sqlite3.OperationalError("database is locked")

    async def go():
        token = SPAWN_OBSERVER.set(SpawnObserver(on_start=locked))
        try:
            with pytest.raises(sqlite3.OperationalError):
                await runner.resume("builder-session", "Answer: option 1")
        finally:
            SPAWN_OBSERVER.reset(token)

    run(go())
    assert seen["pid"]
    assert runner.active is None
    with pytest.raises(ProcessLookupError):
        os.kill(seen["pid"], 0)  # the builder was stopped and reaped


@pytest.mark.parametrize("recommendation", ["\t", "\n", "　", " ", "\x1c\x1d", "   "])
def test_whitespace_only_recommendation_is_not_accepted(recommendation):
    """Review round 7, B1 (the reviewer's probes)."""

    assert not recommended_first_option(blocker(recommendation)).first


def test_empty_recommendation_lets_the_index_decide():
    assert recommended_first_option(blocker("")).first
    assert recommended_first_option(blocker(None)).first


@pytest.mark.parametrize("first_option", ["\tUse the short wording\n", "　Use the short wording ",
                                          "Use the short wording\x1f"])
def test_option_one_is_checked_unstripped(harness, first_option):
    """Review round 7, B1 (the reviewer's probes)."""

    result = harness.block(blocker(None, options=[first_option, OPTIONS[1]]))
    assert result.action != "resume" and harness.resumed == []


@pytest.mark.parametrize("recommendation", ["optıon 1", "OPTİON 1"])
def test_option_one_phrase_matches_ascii_letters_only(recommendation):
    """Review round 7, B2: Unicode case folding must not widen the exception."""

    assert auto_accept.plain_prose_reason(recommendation, recommendation=True) is not None


def test_eligible_kinds_exist_in_the_run_result_schema():
    """Review round 7, B4: a builder can only report kinds in the schema enum."""

    from harbor.runner.base import BLOCKER_KINDS

    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "run-result.json").read_text())
    enum = set(schema["properties"]["blocker"]["properties"]["kind"]["enum"])
    assert auto_accept.ELIGIBLE_KINDS <= enum and auto_accept.ELIGIBLE_KINDS <= BLOCKER_KINDS
    assert auto_accept.ELIGIBLE_KINDS == {"product_decision", "ambiguous_ac"}


def test_documented_exception_option_i_is_the_pronoun_and_means_option_one():
    """Review round 7, B4: the letter check skips "i" (and "a"); documented."""

    assert recommended_first_option(blocker("Take option i.")).first


def test_first_mate_never_overwrites_an_answer_a_human_gave_first(tmp_path):
    def human_answers(db):
        db.update_escalation(db.escalations()[-1]["id"], status="answered")

    db, host = _schedule(tmp_path, before=human_answers)
    try:
        escalation = db.escalations()[-1]
        assert escalation["status"] == "answered" and escalation["citation"] is None
        assert auto_accept.history(db, "SAMPLE-7") == [] and host.comments == []
    finally:
        db.close()


def test_an_undelivered_ladder_auto_accept_does_not_block_first_mate_with_a_false_reason(tmp_path):
    """Review round 5, B2 (the reviewer's repro): the ladder's resume fails and
    the escalation is reopened; the receipt is marked undelivered, so First
    Mate on the same run may still deliver option 1 instead of claiming the
    builder "blocked again"."""

    harness = Harness(tmp_path)

    async def no_session(run_id, prompt):
        return False

    harness.ladder.resumer = no_session
    try:
        value = blocker("Option 1")
        assert harness.block(value).action == "park"
        [receipt] = harness.receipts()
        assert receipt["delivered"] == 0
        case = recovery.Case("Sample", "SAMPLE-7", "builder-session", "blocked", "blocked", value, linear_id="linear-7")
        decision = recovery.apply_auto_accept(harness.db, case, {"action": "escalate", "reason": "r"}, AutoAcceptPolicy())
        assert decision["action"] == "resume", decision["reason"]
        assert decision["instruction"].startswith(f"Answer: {OPTIONS[0]}")
        assert "blocked again" not in decision["reason"]
    finally:
        harness.db.close()


def test_undelivered_receipts_still_count_toward_the_cap_with_a_true_reason(tmp_path):
    harness = Harness(tmp_path, auto_accept=AutoAcceptPolicy(max_auto_accepts=1))

    async def no_session(run_id, prompt):
        return False

    harness.ladder.resumer = no_session
    try:
        value = blocker("Option 1")
        harness.block(value)
        case = recovery.Case("Sample", "SAMPLE-7", "builder-session", "blocked", "blocked", value, linear_id="linear-7")
        decision = recovery.apply_auto_accept(harness.db, case, {"action": "escalate", "reason": "r"},
                                              AutoAcceptPolicy(max_auto_accepts=1))
        assert decision["action"] == "escalate"
        assert "were not delivered" in decision["reason"] and "blocked again" not in decision["reason"]
    finally:
        harness.db.close()


def test_delivered_receipts_are_marked_delivered(harness):
    assert harness.block(blocker("Option 1")).action == "resume"
    assert harness.receipts()[0]["delivered"] == 1


def test_a_failed_ladder_resume_clears_the_auto_accepted_answer(tmp_path):
    harness = Harness(tmp_path)

    async def no_session(run_id, prompt):
        return False

    harness.ladder.resumer = no_session
    try:
        result = harness.block(blocker("Option 1"))
        assert result.action == "park"
        escalation = harness.db.escalations()[-1]
        assert escalation["status"] == "open"
        assert escalation["answer"] is None and escalation["citation"] is None
        # The only comment is the park; none claims a resume.
        assert len(harness.linear.comments) == 1
        assert "auto-accepted option 1" not in harness.linear.comments[0][1]
        assert "could not be resumed" in harness.linear.comments[0][1]
    finally:
        harness.db.close()
