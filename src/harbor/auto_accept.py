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
  never auto-accepted, and neither is a blocker touching a protected path.
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
# Any sign that the recommendation is not a plain "option 1": another option,
# a negation, or an alternative.
_OTHER_RE = re.compile(
    r"\b(?:option\s+(?:[02-9]|\d{2,}|two|three|four|five)|second|third|fourth|fifth|last|other|"
    r"alternative|alternatively|instead|not|never|avoid|unless|don\s+t|do\s+not|or|either|neither|both)\b"
)
# An explicit reference to an option other than the first.
_OTHER_OPTION_RE = re.compile(
    r"\b(?:option\s+(?:[02-9]|\d{2,}|two|three|four|five)|(?:second|third|fourth|fifth|last|other)\s+option)\b"
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


@dataclass(frozen=True)
class AutoAcceptPolicy:
    """Project setting: ``[decisions]`` in ``.harbor/workflow/workflow.toml``."""

    enabled: bool = DEFAULT_ENABLED
    max_auto_accepts: int = DEFAULT_MAX_AUTO_ACCEPTS


def validate_settings(decisions: Mapping[str, Any]) -> None:
    """Fail closed on a malformed auto-accept setting (called by load_workflow)."""

    if "auto_accept_first_option" in decisions and type(decisions["auto_accept_first_option"]) is not bool:
        raise ValueError(f"{SETTING} must be true or false")
    if "max_auto_accepts" in decisions:
        value = decisions["max_auto_accepts"]
        if type(value) is not int or not 0 <= value <= MAX_AUTO_ACCEPTS_CEILING:
            raise ValueError(f"{LIMIT_SETTING} must be an integer from 0 to {MAX_AUTO_ACCEPTS_CEILING}")


def policy_from_config(config: Mapping[str, Any] | None) -> AutoAcceptPolicy:
    """The effective policy; absent keys take the defaults (on, 2 per run)."""

    decisions = (config or {}).get("decisions") or {}
    validate_settings(decisions)
    return AutoAcceptPolicy(
        enabled=decisions.get("auto_accept_first_option", DEFAULT_ENABLED),
        max_auto_accepts=decisions.get("max_auto_accepts", DEFAULT_MAX_AUTO_ACCEPTS),
    )


def describe(policy: AutoAcceptPolicy) -> str:
    """One ``harbor doctor`` line."""

    if not policy.enabled:
        return f"Auto-accept first option: off ({SETTING} = false)"
    return (
        f"Auto-accept first option: on · at most {policy.max_auto_accepts} per builder run · "
        f"never secrets, credentials, money or data deletion · never authorizes a merge "
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
        # The structured field wins over a paraphrase ("Keep the protection
        # and merge it yourself"), never over a text naming another option.
        if text and (_OTHER_OPTION_RE.search(text) or any(text == other for other in normalized[1:])):
            return Choice(False, reason="recommended_option_index contradicts the recommendation text")
        return Choice(True, method="structured")
    if not text:
        return Choice(False, reason="no recommendation")
    if text == normalized[0] and normalized.count(text) == 1:
        return Choice(True, method="exact")
    match = _FIRST_RE.match(text)
    if match is not None and not _OTHER_RE.search(match.group("rest")):
        return Choice(True, method="text")
    return Choice(False, reason="the recommendation does not unambiguously pick the first option")


def sensitive_reason(blocker: Mapping[str, Any]) -> str | None:
    """Why the first option must stay with a human, or ``None``."""

    options = _options(blocker)
    haystack = " ".join(
        [str(blocker.get("question") or ""), options[0] if options else "", str(blocker.get("recommendation") or "")]
    )
    match = _SENSITIVE_RE.search(haystack)
    if match is None:
        return None
    return (
        f"the option involves secrets, credentials, money or deleting data (matched {match.group(0)!r}); "
        "Harbor never auto-accepts those"
    )


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
) -> Assessment:
    if not policy.enabled:
        return Assessment("not_applicable", reason=f"{SETTING} is off")
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
        cursor = connection.execute(
            "INSERT INTO auto_accepts(escalation_id, ticket, run_id, source, kind, question, option, recommendation, method, at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                int(escalation_id) if escalation_id is not None else None, str(ticket), str(run_id or "") or None,
                source, kind, question, option, recommendation or None, method, now,
            ),
        )
        if escalation_id is not None:
            connection.execute(
                "UPDATE escalations SET status = 'answered', answered_at = ?, answer = ?, citation = ? WHERE id = ?",
                (now, option, f"{AUTO_ACCEPT_CITATION}:escalation-{escalation_id}", int(escalation_id)),
            )
        connection.commit()
    return int(cursor.lastrowid)


def reopen(db: Any, escalation_id: Any) -> None:
    """Return an auto-accepted escalation to a human after its resume failed."""

    connection = _connection(db)
    if connection is None or escalation_id is None:
        return
    with connection:
        connection.execute(
            "UPDATE escalations SET status = 'open', answered_at = NULL WHERE id = ?", (int(escalation_id),)
        )


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
