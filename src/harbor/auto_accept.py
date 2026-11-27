"""Auto-accept a blocker's first option when the builder recommends it.

When a builder blocks with ``options`` and a recommendation that picks the
FIRST option, Harbor records that option as the escalation's answer and
resumes the retained builder session instead of waiting for a human.  The
decision is deterministic:

1. ``blocker.recommended_option_index`` (0-based) is the preferred signal;
   ``0`` is the first option.  A recommendation text that names a different
   option contradicts it, and a contradiction is never auto-accepted.
2. Without the structured field, only a strict text match counts: the
   recommendation is exactly the first option's text, or it starts with
   "first option" / "option 1" (optionally after "take", "choose" and so on)
   and names no other option, negation or alternative.
3. Anything else is ambiguous and stays with a human.

Guardrails, each enforced here and covered by tests:

* An answer never authorizes a merge.  The merge gate (``merge_mode`` and
  ``auto_merge_risks`` in pre_merge readiness) is untouched, and the answer
  text says so.  A run already parked waiting on a human merge is never
  auto-accepted.
* Loop guard: once Harbor auto-accepted an option for a ticket, an equivalent
  question (or the same option) is left for a human.  At most
  ``max_auto_accepts`` auto-accepts happen per builder run.
* An option that involves secrets, credentials, money or deleting data is
  never auto-accepted, and neither is one about merging or lowering a gate,
  nor a blocker touching a protected path.  ``decisions.mode = "human"``
  turns auto-accept off.
* A recommendation with a condition, a hand-off, a negative description or a
  reference to any other option is ambiguous: when in doubt, refuse.
* The answer is recorded only when the resume launches and announced only
  once delivered; any other exit reopens the escalation with its answer
  cleared.
* Each auto-accept is recorded in ``auto_accepts`` (distinct from operator
  answers, which go to ``resolutions``) and announced on Linear.

The auto-accepted answer is deliberately NOT stored as a reusable resolution:
it is the builder's own recommendation, not authority a later rung-1 triage
may cite.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Sequence

AUTO_ACCEPT_CITATION = "harbor:auto-accept-first-option"
SETTING = "decisions.auto_accept_first_option"
LIMIT_SETTING = "decisions.max_auto_accepts"
DEFAULT_ENABLED = True
DEFAULT_MAX_AUTO_ACCEPTS = 2
MAX_AUTO_ACCEPTS_CEILING = 10
# Token overlap at or above this counts as the same question re-asked.
EQUIVALENT_QUESTION_OVERLAP = 0.8

# A recommendation that begins by picking the first option.  Matched against
# the normalized text (lowercase, punctuation replaced by spaces).
_FIRST_RE = re.compile(
    r"^(?:(?:i\s+)?(?:recommend(?:ation)?|suggest(?:ion)?|propose)\s+(?:is\s+)?(?:to\s+)?)?"
    r"(?:(?:take|choose|pick|select|use|accept|go\s+with|adopt|proceed\s+with)\s+)?"
    r"(?:the\s+)?(?:first\s+option|option\s+(?:1|one))(?P<rest>(?:\s.*)?)$"
)
# Any reference to an option other than the first: a number, a letter label
# ("option B", "(b)", "take c"), or an ordinal.  Matched against the
# normalized text, where "(b)" has become " b ".
_OTHER_OPTION_RE = re.compile(
    r"\b(?:option\s+(?:[02-9]|\d{2,}|[b-z]|two|three|four|five|six)|"
    r"(?:take|choose|pick|select|use|accept|go\s+with|prefer|adopt)\s+(?:the\s+)?(?:[b-z]|[2-9])|"
    r"second|third|fourth|fifth|sixth|last|latter|other|another|alternative|alternatively|instead)\b"
)
# A raw "(b)", "b)" or "[b]" label, checked before normalization.
_LETTER_LABEL_RE = re.compile(r"(?:\(\s*[b-zB-Z]\s*\)|\[\s*[b-zB-Z]\s*\]|(?<![\w(])[b-zB-Z]\))")
# Negation or a negative description of the option ("unsafe", "risky").
_NEGATIVE_RE = re.compile(
    r"\b(?:not|never|no|avoid|don\s+t|do\s+not|doesn\s+t|isn\s+t|won\s+t|shouldn\s+t|reject|against|"
    r"unsafe|risky|dangerous|wrong|bad|worse|broken|harmful|insecure|discouraged|neither|nor|or|either|both)\b"
)
# A condition, a deferral or a human hand-off: the recommendation is not an
# unconditional pick, so a human decides.
_QUALIFIER_RE = re.compile(
    r"\b(?:if|but|only|after|before|once|unless|until|provided|providing|assuming|when|whenever|"
    r"as\s+long\s+as|depending|depends|maybe|perhaps|probably|possibly|might|could|would|"
    r"confirm|confirms|confirmed|confirmation|approve|approves|approved|approval|ask|asks|check\s+with|"
    r"human|humans|person|operator|someone|team|reviewer|manually)\b"
)
# Options asking the human for secrets, credentials or money, or to approve
# deleting data.  Matched against the question, the first option and the
# recommendation; a match keeps the escalation open for a human.
_SENSITIVE_RE = re.compile(
    r"\b(?:secrets?|credentials?|passwords?|passphrases?|api[\s_-]?keys?|access[\s_-]?keys?|"
    r"private[\s_-]?keys?|ssh[\s_-]?keys?|signing[\s_-]?keys?|tokens?|oauth|2fa|mfa|otp|"
    r"log\s?in|sign\s?in|auth|authenticate|keychain|"
    r"money|pay|pays|paid|payments?|billing|bill|invoices?|purchases?|buy|subscriptions?|"
    r"credit[\s_-]?cards?|budgets?|spend|spending|costs?|charges?|refunds?|funds?|funding|pric(?:e|es|ing)|"
    r"delet(?:e|es|ed|ing|ion)|drop\s+(?:the\s+)?(?:table|database|column|data)|truncate|wipe|purge|"
    r"destroy|erase|rm\s+-rf|data\s+loss|irreversible)\b",
    re.IGNORECASE,
)
# Options about merging or about lowering a gate.  The merge gate is
# cooperative (the builder honours it), so an answer must never steer the
# builder past it: merging, force-pushing, branch protection, skipping or
# disabling CI/checks/review, changing the risk tier, waiving proof, or
# editing protected paths or the gate configuration.
_GATE_RE = re.compile(
    r"\b(?:merg(?:e|es|ed|ing)|auto[\s_-]?merge|merge[\s_-]?mode|force[\s_-]?push(?:es|ed|ing)?|push\s+--force|"
    r"push(?:es|ed|ing)?\s+(?:directly\s+)?to\s+(?:the\s+)?(?:main|master|base|default)|"
    r"branch[\s_-]?protection|protect(?:ed|ion|ions)?|rulesets?|bypass(?:es|ed|ing)?|"
    r"skip(?:s|ped|ping)?|disabl(?:e|es|ed|ing)|ignor(?:e|es|ed|ing)|waiv(?:e|es|ed|er|ing)|"
    r"overrid(?:e|es|den|ing)|turn\s+off|admin|ci|checks?|reviews?|reviewers?|approvals?|"
    r"risk|risks|risk[\s_-]?tier|proof|proofs|gates?|readiness|workflow\.toml|agents\.md|"
    r"auto_merge_risks|pre_merge)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AutoAcceptPolicy:
    """Project setting: ``[decisions]`` in ``.harbor/workflow/workflow.toml``."""

    enabled: bool = DEFAULT_ENABLED
    max_auto_accepts: int = DEFAULT_MAX_AUTO_ACCEPTS
    disabled_by: str = ""  # the setting that turned it off, for doctor


def validate_settings(decisions: Mapping[str, Any]) -> None:
    """Fail closed on a malformed auto-accept setting (called by load_workflow)."""

    if "auto_accept_first_option" in decisions and type(decisions["auto_accept_first_option"]) is not bool:
        raise ValueError(f"{SETTING} must be true or false")
    if "max_auto_accepts" in decisions:
        value = decisions["max_auto_accepts"]
        if type(value) is not int or not 0 <= value <= MAX_AUTO_ACCEPTS_CEILING:
            raise ValueError(f"{LIMIT_SETTING} must be an integer from 0 to {MAX_AUTO_ACCEPTS_CEILING}")


def policy_from_config(config: Mapping[str, Any] | None) -> AutoAcceptPolicy:
    """The effective policy; absent keys take the defaults (on, 2 per run).

    ``decisions.mode = "human"`` says humans make every decision, so it turns
    auto-accept off whatever ``auto_accept_first_option`` says.
    """

    decisions = (config or {}).get("decisions") or {}
    validate_settings(decisions)
    limit = decisions.get("max_auto_accepts", DEFAULT_MAX_AUTO_ACCEPTS)
    if decisions.get("mode") == "human":
        return AutoAcceptPolicy(False, limit, 'decisions.mode = "human"')
    enabled = decisions.get("auto_accept_first_option", DEFAULT_ENABLED)
    return AutoAcceptPolicy(enabled, limit, "" if enabled else f"{SETTING} = false")


def describe(policy: AutoAcceptPolicy) -> str:
    """One ``harbor doctor`` line."""

    if not policy.enabled:
        return f"Auto-accept first option: off ({policy.disabled_by or f'{SETTING} = false'})"
    return (
        f"Auto-accept first option: on · at most {policy.max_auto_accepts} per builder run · "
        f"never secrets, credentials, money, data deletion, merges or gate changes · never authorizes a merge "
        f"(opt out: {SETTING} = false)"
    )


def _norm(text: Any) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(text or "").casefold()).replace("_", " ").split())


def _options(blocker: Mapping[str, Any]) -> list[str]:
    value = blocker.get("options")
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, str)]


@dataclass(frozen=True)
class Choice:
    """Whether the recommendation picks the first option, and how Harbor knows."""

    first: bool
    method: str = ""  # "structured" | "exact" | "text" when first
    reason: str = ""


def recommended_first_option(blocker: Mapping[str, Any]) -> Choice:
    """Decide, deterministically, whether the recommendation is option 1."""

    options = _options(blocker)
    if not options or not options[0].strip() or len(options) != len(blocker.get("options") or []):
        return Choice(False, reason="the blocker lists no usable options")
    recommendation = str(blocker.get("recommendation") or "")
    text = _norm(recommendation)
    normalized = [_norm(option) for option in options]
    index = blocker.get("recommended_option_index")
    if index is not None:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(options):
            return Choice(False, reason="recommended_option_index is not a valid option index")
        if index != 0:
            return Choice(False, reason=f"the recommendation is option {index + 1}")
        # The structured field wins over a plain paraphrase of option 1,
        # never over a text that names another option, speaks against
        # option 1, or attaches a condition.  When in doubt, refuse.
        if text and text != normalized[0]:
            if _LETTER_LABEL_RE.search(recommendation) or _OTHER_OPTION_RE.search(text) \
                    or any(text == other for other in normalized[1:]):
                return Choice(False, reason="recommended_option_index contradicts the recommendation text")
            if _NEGATIVE_RE.search(text) or _QUALIFIER_RE.search(text):
                return Choice(False, reason="the recommendation is negative or conditional")
        return Choice(True, method="structured")
    if not text:
        return Choice(False, reason="no recommendation")
    if text == normalized[0] and normalized.count(text) == 1:
        return Choice(True, method="exact")
    match = _FIRST_RE.match(text)
    if match is None:
        return Choice(False, reason="the recommendation does not unambiguously pick the first option")
    rest = match.group("rest")
    if (_LETTER_LABEL_RE.search(recommendation) or _OTHER_OPTION_RE.search(rest)
            or _NEGATIVE_RE.search(rest) or _QUALIFIER_RE.search(rest)):
        return Choice(False, reason="the recommendation is conditional, negative or names another option")
    return Choice(True, method="text")


def sensitive_reason(blocker: Mapping[str, Any]) -> str | None:
    """Why the first option must stay with a human, or ``None``."""

    options = _options(blocker)
    haystack = " ".join(
        [str(blocker.get("question") or ""), options[0] if options else "", str(blocker.get("recommendation") or "")]
    )
    match = _SENSITIVE_RE.search(haystack)
    if match is not None:
        return (
            f"the option involves secrets, credentials, money or deleting data (matched {match.group(0)!r}); "
            "Harbor never auto-accepts those"
        )
    match = _GATE_RE.search(haystack)
    if match is not None:
        return (
            f"the option is about merging or lowering a gate (matched {match.group(0)!r}); an auto-accepted "
            "answer never steers the builder past the merge gate"
        )
    return None


def initialize(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS auto_accepts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            escalation_id INTEGER,
            ticket TEXT NOT NULL,
            run_id TEXT,
            source TEXT NOT NULL,
            kind TEXT,
            question TEXT NOT NULL,
            option TEXT NOT NULL,
            recommendation TEXT,
            method TEXT,
            at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS auto_accepts_ticket_idx ON auto_accepts(ticket, run_id)")
    connection.commit()


def _connection(db: Any) -> sqlite3.Connection | None:
    if isinstance(db, sqlite3.Connection):
        return db
    connection = getattr(db, "connection", None)
    return connection if isinstance(connection, sqlite3.Connection) else None


def history(db: Any, ticket: str) -> list[dict[str, Any]]:
    connection = _connection(db)
    if connection is None:
        return []
    initialize(connection)
    rows = connection.execute(
        "SELECT id, escalation_id, ticket, run_id, source, kind, question, option, recommendation, method, at "
        "FROM auto_accepts WHERE ticket = ? ORDER BY id",
        (str(ticket),),
    ).fetchall()
    keys = ("id", "escalation_id", "ticket", "run_id", "source", "kind", "question", "option", "recommendation", "method", "at")
    return [dict(zip(keys, tuple(row))) for row in rows]


def _equivalent(left: str, right: str) -> bool:
    a, b = _norm(left), _norm(right)
    if a == b:
        return True
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= EQUIVALENT_QUESTION_OVERLAP


@dataclass(frozen=True)
class Assessment:
    """``accept``: auto-accept now.  ``refused``: option 1 was recommended but a
    guardrail keeps it with a human (``reason`` says why, for the Linear
    comment).  ``not_applicable``: the policy is off or the recommendation is
    not unambiguously option 1; Harbor escalates exactly as before."""

    status: Literal["accept", "refused", "not_applicable"]
    option: str = ""
    method: str = ""
    reason: str = ""
    count: int = 0  # auto-accepts already made in this run

    @property
    def accepted(self) -> bool:
        return self.status == "accept"


def assess(
    db: Any,
    ticket: str,
    run_id: str,
    blocker: Mapping[str, Any],
    policy: AutoAcceptPolicy,
    *,
    parked_for_human: bool = False,
    protected: bool = False,
    human_decisions: bool = False,
) -> Assessment:
    if not policy.enabled:
        return Assessment("not_applicable", reason=f"auto-accept is off ({policy.disabled_by or SETTING})")
    if human_decisions:
        return Assessment("not_applicable", reason="the project's decision mode is human")
    choice = recommended_first_option(blocker)
    if not choice.first:
        return Assessment("not_applicable", reason=choice.reason)
    option = _options(blocker)[0].strip()
    if protected:
        return Assessment(
            "refused", option, choice.method, "the blocker touches a protected path, which always needs a human",
        )
    if parked_for_human:
        return Assessment(
            "refused", option, choice.method,
            "the run is merge-ready and waiting on a human merge; an answer never stands in for merge approval",
        )
    sensitive = sensitive_reason(blocker)
    if sensitive:
        return Assessment("refused", option, choice.method, sensitive)
    if _connection(db) is None:
        # No durable ledger means no loop guard; fail closed.
        return Assessment("refused", option, choice.method, "no Harbor ledger is available to enforce the loop guard")
    prior = history(db, ticket)
    question = str(blocker.get("question") or "")
    for row in prior:
        if _equivalent(row["question"], question) or _norm(row["option"]) == _norm(option):
            label = f"escalation #{row['escalation_id']}" if row.get("escalation_id") is not None else "an earlier escalation"
            return Assessment(
                "refused", option, choice.method,
                f"Harbor already auto-accepted this recommendation for {label} and the builder blocked again "
                "with an equivalent question, so a human must decide",
            )
    count = sum(1 for row in prior if str(row.get("run_id") or "") == str(run_id or ""))
    if count >= policy.max_auto_accepts:
        return Assessment(
            "refused", option, choice.method,
            f"the limit of {policy.max_auto_accepts} auto-accepts per builder run is reached ({LIMIT_SETTING})",
            count,
        )
    return Assessment("accept", option, choice.method, count=count)


def record(
    db: Any,
    *,
    escalation_id: Any,
    ticket: str,
    run_id: str,
    source: str,
    kind: str,
    question: str,
    option: str,
    recommendation: str,
    method: str,
) -> int:
    """Store the auto-accept receipt and mark the escalation answered by Harbor.

    The escalation's ``citation`` is ``harbor:auto-accept-first-option`` and
    it has no ``resolution_id``, so it is distinguishable from an operator
    answer (which always links a ``resolutions`` row).
    """

    connection = _connection(db)
    if connection is None:
        raise RuntimeError("auto-accept needs a durable Harbor ledger")
    initialize(connection)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lock = getattr(db, "_lock", None)
    from contextlib import nullcontext

    with lock if lock is not None else nullcontext():
        if escalation_id is not None:
            # Only an escalation still waiting on a human is answered.  First
            # Mate records at launch, so a human may have answered first.
            updated = connection.execute(
                "UPDATE escalations SET status = 'answered', answered_at = ?, answer = ?, citation = ? "
                "WHERE id = ? AND COALESCE(status, 'open') = 'open'",
                (now, option, f"{AUTO_ACCEPT_CITATION}:escalation-{escalation_id}", int(escalation_id)),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise EscalationNotOpen(f"escalation {escalation_id} is no longer open")
        cursor = connection.execute(
            "INSERT INTO auto_accepts(escalation_id, ticket, run_id, source, kind, question, option, recommendation, method, at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                int(escalation_id) if escalation_id is not None else None, str(ticket), str(run_id or "") or None,
                source, kind, question, option, recommendation or None, method, now,
            ),
        )
        connection.commit()
    return int(cursor.lastrowid)


class EscalationNotOpen(RuntimeError):
    """The escalation was answered or superseded before Harbor could auto-accept it."""


def reopen(db: Any, escalation_id: Any) -> None:
    """Return an auto-accepted escalation to a human when nothing was delivered.

    The auto-accepted answer and citation are cleared, so the row reads as an
    unanswered question again.  The ``auto_accepts`` receipt stays: it still
    counts for the loop guard.
    """

    connection = _connection(db)
    if connection is None or escalation_id is None:
        return
    with connection:
        connection.execute(
            "UPDATE escalations SET status = 'open', answered_at = NULL, answer = NULL, citation = NULL "
            "WHERE id = ? AND citation LIKE ?",
            (int(escalation_id), f"{AUTO_ACCEPT_CITATION}:%"),
        )


def _eligibility_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS auto_accept_eligibility (
            escalation_id INTEGER PRIMARY KEY,
            protected INTEGER NOT NULL,
            human_decisions INTEGER NOT NULL,
            at TEXT NOT NULL
        )"""
    )


def record_eligibility(db: Any, escalation_id: Any, *, protected: bool, human_decisions: bool) -> None:
    """Store the ladder's protected-path and decision-mode facts for an escalation.

    First Mate has no protected-path configuration or worktree diff of its
    own, so it reads these facts instead of re-deriving them from a reason
    string.  An escalation without a row is never auto-accepted by First Mate.
    """

    connection = _connection(db)
    if connection is None or escalation_id is None:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connection:
        _eligibility_schema(connection)
        connection.execute(
            "INSERT OR REPLACE INTO auto_accept_eligibility(escalation_id, protected, human_decisions, at) "
            "VALUES (?,?,?,?)",
            (int(escalation_id), int(bool(protected)), int(bool(human_decisions)), now),
        )


def eligibility(db: Any, escalation_id: Any) -> dict[str, bool] | None:
    """The ladder's recorded facts for ``escalation_id``, or ``None`` when none were recorded."""

    connection = _connection(db)
    if connection is None or escalation_id is None:
        return None
    _eligibility_schema(connection)
    row = connection.execute(
        "SELECT protected, human_decisions FROM auto_accept_eligibility WHERE escalation_id = ?",
        (int(escalation_id),),
    ).fetchone()
    if row is None:
        return None
    return {"protected": bool(row[0]), "human_decisions": bool(row[1])}


MERGE_NOTE = (
    "This answer does not authorize a merge. The merge gate is unchanged: merge only when the workflow's "
    "pre_merge readiness allows it (delivery.merge_mode = \"workflow_checked\" and an eligible risk in "
    "auto_merge_risks); otherwise leave the merge to a human."
)


def answer_text(escalation_id: Any, option: str) -> str:
    """The instruction delivered to the retained builder session."""

    label = f"escalation #{escalation_id}" if escalation_id is not None else "this escalation"
    return (
        f"{option}\n"
        f"(Harbor auto-accepted option 1 for {label} because your recommendation picked the first option; "
        f"policy {SETTING}.) {MERGE_NOTE}"
    )


def comment(
    ticket: str,
    escalation_id: Any,
    *,
    question: str,
    option: str,
    recommendation: str,
    count: int,
    policy: AutoAcceptPolicy,
    source: str = "Harbor",
) -> str:
    label = f"escalation #{escalation_id}" if escalation_id is not None else "escalation"
    return (
        f"[harbor] {source} auto-accepted option 1 for {ticket} ({label}) under the auto-accept-first-option "
        f"policy ({SETTING}).\n"
        f"Question: {question}\n"
        f"Answer (option 1): {option}\n"
        f"Builder recommendation: {recommendation or '(structured recommended_option_index = 0)'}\n"
        f"Auto-accept {count + 1} of at most {policy.max_auto_accepts} for this builder run. {MERGE_NOTE}\n"
        "Harbor resumed the same builder session. To redirect it, reply on this ticket; to turn this off, set "
        "`auto_accept_first_option = false` under [decisions] in .harbor/workflow/workflow.toml."
    )


def refusal_line(assessment: Assessment) -> str:
    return f"Auto-accept of option 1 not applied: {assessment.reason}.\n"


__all__: Sequence[str] = (
    "AUTO_ACCEPT_CITATION", "AutoAcceptPolicy", "Assessment", "Choice", "answer_text", "assess", "comment",
    "describe", "history", "initialize", "policy_from_config", "record", "recommended_first_option", "refusal_line",
    "reopen", "sensitive_reason", "validate_settings",
)
