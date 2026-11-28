"""Auto-accept a blocker's first option when the builder recommends it.

When a builder blocks with ``options`` and a recommendation that picks the
FIRST option, Harbor records that option as the escalation's answer and
resumes the retained builder session instead of waiting for a human.  The
decision is deterministic and fails closed:

1. Only the structured ``blocker.recommended_option_index == 0`` counts.  There
   is no free-text fallback: deny-lists over prose keep leaking.
2. The recommendation text must be consistent with that index.  It may not
   refer to any other option (a digit other than 1, a number word, a letter,
   a roman numeral, an ordinal, "plan B", "other", "instead"...), may not
   paraphrase another option at least as closely as option 1, and may not be
   negative or conditional.
3. Anything else stays with a human.

What this is NOT: a security boundary.  An auto-answer gives the builder no
capability it did not already have.  The real boundaries are the builder's
sandbox, the merge gate (with the reviewer and the risk rating) and
server-side branch protection.  Projects where product decisions carry cost
or data risk should set ``auto_accept_first_option = false`` or
``decisions.mode = "human"``.

Structural checks (exact; they check fields, characters and ledger state):

* Only the reported kinds in ``ELIGIBLE_KINDS`` are eligible.
* ``auto_accept_first_option = false`` or ``decisions.mode = "human"`` turns
  auto-accept off.
* A blocker whose reported or observed changed paths match a protected
  pattern is not auto-accepted.
* A run parked waiting on a human merge is not auto-accepted, and the answer
  does not authorize a merge: the merge gate (``merge_mode`` and
  ``auto_merge_risks`` in pre_merge readiness) is untouched, and the answer
  text says so.
* Option 1 and the recommendation, as raw text, may contain only ASCII
  letters, the space character and ``, . ' " ! ? ( )`` (``PROSE_CHARACTERS``);
  the recommendation may also contain the literal phrase "option 1" or
  "option #1", followed by a colon only when it leads the text.
* At most ``max_auto_accepts`` auto-accepts per builder run, undelivered ones
  included.

Lexical checks (word matching; they cannot detect meaning):

* the recommendation may use only option 1's words plus a fixed affirmative
  list -- "Use the existing date picker" passes for "Build a new date picker";
* other-option references, negation and qualifier words are refused;
* ALL_CAPS and camelCase words, dotfiles and words with an inner ``.`` are
  refused; a file name without a dot (``Dockerfile``) passes;
* a question, option or recommendation that names a protected path or a
  specific segment of a protected pattern is refused;
* loop guard: an earlier delivered or in-flight auto-accept with the same
  option or a question with at least 80% word overlap is left for a human;
  rewordings below that are bounded only by the per-run cap;
* best-effort keyword heuristics for secrets, credentials, money, deleting
  data, merging or lowering a gate.  A plain-English synonym ("Empty the
  orders table") is not recognised.

Delivery:

* The answer is recorded just before the launch and the receipt is marked
  delivered at the hand-off: when Harbor's runner has created the builder
  process (``harbor.runner.base.SpawnObserver``).  A runner failing before
  that (deadline exhausted, executable missing, session unavailable) is not
  a hand-off.  From the hand-off on, the Linear comment is posted on every
  exit, cancellation included, and the escalation stays answered.  Without
  it the escalation is reopened with its answer cleared and the receipt
  marked undelivered.
* Each auto-accept is recorded in ``auto_accepts`` (distinct from operator
  answers, which go to ``resolutions``) and announced on Linear.

The auto-accepted answer is deliberately NOT stored as a reusable resolution:
it is the builder's own recommendation, not authority a later rung-1 triage
may cite.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
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

# All patterns below run on ``_norm`` text: NFKC (so full-width digits become
# ASCII), casefolded, apostrophes dropped ("don't" -> "dont"), and every other
# non-letter-or-digit -- including ``_ - . /`` -- turned into a space, so
# ``STRIPE_SECRET_KEY`` reads "stripe secret key" and "(b)" reads "b".

# Any reference to an option other than the first.  A bare digit sequence
# other than "1" is handled separately (``_digits_other_than_one``).
_OTHER_OPTION_RE = re.compile(
    r"\b(?:[b-h]|[j-z]|ii|iii|iv|vi|vii|viii|ix|"  # letter labels (not "a"/"i") and roman numerals >= 2
    r"zero|two|three|four|five|six|seven|eight|nine|ten|"
    r"second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|latter|former|"
    r"other|others|another|alternative|alternatives|alternatively|instead|rather|versus|vs|"
    r"both|either|neither|nor|or)\b"
)
# Negation or a negative description of option 1.
_NEGATIVE_RE = re.compile(
    r"\b(?:not|never|no|none|avoid|dont|doesnt|isnt|wont|shouldnt|cant|cannot|reject|rejected|against|"
    r"unsafe|risky|dangerous|wrong|bad|worse|worst|broken|harmful|insecure|discouraged|suboptimal|inferior|"
    r"weaker|poor|problematic|flawed|considered|rejecting|drawback|downside)\b"
)
# A condition, a deferral, an exception, a change of mind or a human hand-off:
# the recommendation is not an unconditional pick of option 1.
_QUALIFIER_RE = re.compile(
    r"\b(?:if|but|only|after|before|once|unless|until|provided|providing|assuming|when|whenever|while|"
    r"except|excluding|apart|besides|however|although|though|actually|wait|instead|rather|prefer|prefers|"
    r"preferred|wins|better|beats|over|than|"
    r"as long as|depending|depends|maybe|perhaps|probably|possibly|might|could|would|"
    r"confirm|confirms|confirmed|confirmation|approve|approves|approved|approval|ask|asks|check with|"
    r"human|humans|person|people|operator|someone|team|reviewer|manually)\b"
)
# Words that say nothing about WHICH option was picked; ignored by the
# paraphrase check.
_FILLER = frozenset(
    "a an the to of for and with in on at by is it its this that we i you go use take choose pick select "
    "accept adopt proceed recommend recommended recommendation suggest suggested propose option options first "
    "one 1 please just".split()
)

# --------------------------------------------------------------------------
# Fail-closed allowlists.  The deny-lists in this module are an extra layer;
# these decide.  This is defense in depth, NOT a security boundary: the
# builder's sandbox and server-side branch protection are the boundary.
# --------------------------------------------------------------------------

# Only pure decision kinds are ever auto-accepted.  An environment, access,
# transport, proof or conflicting-instruction blocker never is: answering an
# access question cannot grant access, and the other kinds are not choices.
# Kept equal to the decision kinds the run-result schema can express
# (``schemas/run-result.json`` blocker.kind enum); a test pins that.
ELIGIBLE_KINDS = frozenset({"product_decision", "ambiguous_ac"})

# The only words, besides option 1's own words, a recommendation may use.
AFFIRMATIVE_WORDS = frozenset(
    "take choose pick go with recommend recommended recommendation suggest use the first option one 1 "
    "because it its is simplest safest simple safe matches match fits fit ticket criteria acceptance "
    "a an this that keep i we our to of for and as per best cleanest clearest smallest minimal "
    "reversible within existing here please ahead".split()
)

# Plain prose, checked on the RAW text (no normalization first): exactly the
# ASCII letters, the space character (0x20) and ``, . ' " ! ? ( )``.  Every
# other character -- digits, ``-``, ``:``, ``#``, tabs, newlines, control
# characters, any non-ASCII character -- makes the text ineligible.
PROSE_CHARACTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz ,.'\"!?()"
_PROSE_CHARS_RE = re.compile(r"[A-Za-z ,.'\"!?()]*")
# The one exception, in a recommendation only: the literal phrase "option 1"
# or "option #1" (any letter case, exactly one space).  When it starts the
# recommendation it may be followed directly by a colon.
# ``re.ASCII``: case folding is ASCII-only, so "opt\u0131on 1" or "OPT\u0130ON 1" never match.
_LEADING_OPTION_ONE_RE = re.compile(r"option #?1:?(?![0-9])", re.IGNORECASE | re.ASCII)
_OPTION_ONE_PHRASE_RE = re.compile(r"(?<![A-Za-z0-9])option #?1(?![0-9])", re.IGNORECASE | re.ASCII)
# Verbs and nouns of delivery, release, destruction, money, credentials and
# gates.  Matched against normalized words of option 1 and the
# recommendation; any match keeps the question with a human.
_ACTION_WORD_RE = re.compile(
    r"\b(?:push\w*|merg\w*|land|lands|landed|landing|ship|ships|shipped|shipping|deploy\w*|releas\w*|"
    r"rebas\w*|reset\w*|clean|cleans|cleaned|cleaning|forc\w*|squash\w*|revert\w*|delet\w*|drop\w*|"
    r"remov\w*|wip(?:e|es|ed|ing)|purg\w*|discard\w*|overwrit\w*|truncat\w*|publish\w*|upgrad\w*|"
    r"purchas\w*|pay|pays|paid|paying|payment\w*|buy\w*|subscri\w*|plan|plans|billing|production|prod|"
    r"main|master|branch\w*|secret\w*|key|keys|token\w*|password\w*|passwd|credential\w*|creds?|"
    r"cookie\w*|session\w*|jwt\w*|env|envs|ci|test|tests|tested|testing|check\w*|coverage|risk\w*|"
    r"review\w*|protect\w*|codeowners?|hook\w*|verif\w*)\b"
)


def plain_prose_reason(text: Any, *, recommendation: bool = False) -> str | None:
    """Why ``text`` is not plain prose (so never auto-accepted), or ``None``."""

    value = str(text or "")
    if recommendation:
        # The literal exception, removed from the raw text: a leading
        # "option 1" (optionally with a colon directly after it), then any
        # other literal "option 1" / "option #1".
        leading = _LEADING_OPTION_ONE_RE.match(value)
        if leading:
            value = " " + value[leading.end():]
        value = _OPTION_ONE_PHRASE_RE.sub(" ", value)
    if not _PROSE_CHARS_RE.fullmatch(value):
        bad = next(char for char in value if char not in PROSE_CHARACTERS)
        return f"it is not plain prose (contains {bad!r})"
    words = [word.strip(",'\"!?()") for word in value.split(" ")]
    if any(re.match(r"[A-Z]{2,}", word) for word in words):
        return "it contains a word starting with two capitals (an ALL_CAPS identifier)"
    if any(re.search(r"[a-z][A-Z]", word) for word in words):
        return "it contains a camelCase identifier"
    if any(word.strip(".") and (word.startswith(".") or "." in word.rstrip(".")) for word in words):
        return "it contains a dotted name (a file name, dotfile or identifier)"
    match = _ACTION_WORD_RE.search(_norm(value))
    if match is not None:
        return f"it mentions {match.group(0)!r}, a keyword Harbor leaves to a human"
    return None


# A path-like token: anything with a slash or backslash, or a name.ext.
_PATH_TOKEN_RE = re.compile(r"[^\s`'\"()<>,;]*[/\\][^\s`'\"()<>,;]*|[\w.\-]+\.[A-Za-z0-9]{1,8}\b")


def mentioned_paths(blocker: Mapping[str, Any]) -> list[str]:
    """Path-like tokens and bare words in the question, options and recommendation, case-folded.

    The ladder matches them against the protected patterns, so a protected
    path that is only mentioned (not yet touched) still needs a human.
    """

    texts = [str(blocker.get("question") or ""), str(blocker.get("recommendation") or "")]
    texts += [str(item) for item in blocker.get("options") or [] if isinstance(item, str)]
    found: list[str] = []
    for text in texts:
        value = unicodedata.normalize("NFKC", text).casefold()
        for token in _PATH_TOKEN_RE.findall(value):
            token = token.replace("\\", "/").strip(".:")
            if token:
                found.append(token)
        for word in re.findall(r"[\w.\-]+", value):
            found.append(word)
            found.append(word.strip(".-"))  # "module." at a sentence end
        found.extend(re.findall(r"\w+", value))  # whole words, so "security-module" still names "security"
    return found
# Options asking the human for secrets, credentials or money, or to approve
# deleting data.  Matched against the question, the first option and the
# recommendation; a match keeps the escalation open for a human.
_SENSITIVE_RE = re.compile(
    r"\b(?:secrets?|credentials?|creds?|passwords?|passwd|passphrases?|pats?|apikeys?|api keys?|access keys?|"
    r"private keys?|ssh keys?|signing keys?|keys? pairs?|tokens?|oauth|2fa|mfa|otp|"
    r"log ?in|sign ?in|auth|authenticate|keychains?|certificates?|certs?|pem|dotenv|"
    r"money|pay|pays|paid|payments?|billing|bill|invoices?|purchases?|buy|subscriptions?|"
    r"credit cards?|budgets?|spend|spending|costs?|charges?|refunds?|funds?|funding|pric(?:e|es|ing)|"
    r"delet(?:e|es|ed|ing|ion)|truncat(?:e|es|ed|ing)|wip(?:e|es|ed|ing)|purg(?:e|es|ed|ing)|"
    r"destroy|erase|rm rf|reset hard|data loss|irreversible|"
    r"(?:drop|dropp(?:ed|ing)|remov(?:e|es|ed|ing)|clear(?:s|ed|ing)?|reset)(?: \w+){0,3} "
    r"(?:tables?|databases?|dbs?|data|datasets?|users?|records?|rows?|branch(?:es)?|files?|directory|"
    r"directories|folders?|buckets?|schemas?|collections?|columns?|indexes|volumes?))\b"
)
# ``.env`` is read before normalization (normalized it is just "env").
_DOTENV_RE = re.compile(r"(?:^|[\s/'\"`(])\.env\b", re.IGNORECASE)
# Options about merging or about lowering a gate.  The merge gate is
# cooperative (the builder honours it), so an answer must never steer the
# builder past it: merging, pushing to the base branch, branch protection,
# skipping, disabling or xfailing CI/checks/tests/review, lowering coverage,
# changing the risk tier, waiving proof, or editing protected paths, CODEOWNERS
# or the gate configuration.
_GATE_RE = re.compile(
    r"\b(?:merg(?:e|es|ed|ing)|automerge|force push(?:es|ed|ing)?|push(?:es|ed|ing)? force|"
    r"push(?:es|ed|ing)?(?: \w+){0,4} (?:to|into|onto) (?:the )?(?:main|master|base|default|trunk|release)|"
    r"branch protection|protect(?:ed|ion|ions)?|rulesets?|bypass(?:es|ed|ing)?|"
    r"skip(?:s|ped|ping)?|disabl(?:e|es|ed|ing)|ignor(?:e|es|ed|ing)|waiv(?:e|es|ed|er|ing)|xfail\w*|"
    r"overrid(?:e|es|den|ing)|turn off|admin|ci|checks?|reviews?|reviewers?|approvals?|"
    r"(?:remov(?:e|es|ed|ing)|delet(?:e|es|ed|ing)|comment(?:s|ed|ing)? out)(?: \w+){0,3} tests?|"
    r"coverage|codeowners|risk|risks|proof|proofs|gates?|readiness|workflow toml|agents md|pre merge)\b"
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
        f"pure decisions in plain prose only · best-effort keyword heuristics for money, deletion, credentials "
        f"and gates · does not authorize a merge "
        f"(opt out: {SETTING} = false)"
    )


def _norm(text: Any) -> str:
    """NFKC, casefold, drop apostrophes, and split on everything but letters and digits."""

    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    value = re.sub(r"['’‘`]", "", value)
    return " ".join(re.sub(r"[\W_]+", " ", value).split())


def _tokens(text: str) -> set[str]:
    return {token for token in text.split() if token not in _FILLER}


def _digits_other_than_one(text: str) -> bool:
    """Any digit sequence except a lone "1" (covers "2", "2nd", "10", full-width forms)."""

    return any(match != "1" for match in re.findall(r"\d+", text))


def _options(blocker: Mapping[str, Any]) -> list[str]:
    value = blocker.get("options")
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, str)]


@dataclass(frozen=True)
class Choice:
    """Whether the recommendation picks the first option, and how Harbor knows."""

    first: bool
    method: str = ""  # "structured" when first
    reason: str = ""


def recommended_first_option(blocker: Mapping[str, Any]) -> Choice:
    """Decide, deterministically and failing closed, whether the builder picked option 1.

    Only ``recommended_option_index == 0`` counts; the recommendation text can
    only veto it.
    """

    options = _options(blocker)
    if not options or not options[0].strip() or len(options) != len(blocker.get("options") or []):
        return Choice(False, reason="the blocker lists no usable options")
    index = blocker.get("recommended_option_index")
    if index is None:
        return Choice(False, reason="the builder did not set recommended_option_index")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(options):
        return Choice(False, reason="recommended_option_index is not a valid option index")
    if index != 0:
        return Choice(False, reason=f"the recommendation is option {index + 1}")
    recommendation = str(blocker.get("recommendation") or "")
    text = _norm(recommendation)
    normalized = [_norm(option) for option in options]
    if recommendation == "":  # no recommendation text (null or ""); whitespace is not empty
        return Choice(True, method="structured")
    if not recommendation.strip():
        return Choice(False, reason="the recommendation text is only whitespace")
    prose = plain_prose_reason(recommendation, recommendation=True)
    if prose:
        return Choice(False, reason=f"the recommendation text is ineligible: {prose}")
    if text == normalized[0]:
        return Choice(True, method="structured")
    # Allowlist: every word must be one of option 1's own words or a fixed
    # affirmative word.  Anything else ("incorrect", "overkill", "skip",
    # "out of scope") means the text may be saying something other than
    # "take option 1", so a human decides.
    unknown = sorted(set(text.split()) - set(normalized[0].split()) - AFFIRMATIVE_WORDS)
    if unknown:
        return Choice(False, reason=f"the recommendation text uses words Harbor cannot vouch for ({', '.join(unknown[:5])})")
    if any(text == other for other in normalized[1:]):
        return Choice(False, reason="the recommendation text is another option")
    if _digits_other_than_one(text) or _OTHER_OPTION_RE.search(text):
        return Choice(False, reason="the recommendation text refers to another option")
    if _NEGATIVE_RE.search(text) or _QUALIFIER_RE.search(text):
        return Choice(False, reason="the recommendation text is negative or conditional")
    # The text must not read more like another option than like option 1.
    words = _tokens(text)
    first = len(words & _tokens(normalized[0]))
    if any(len(words & _tokens(other)) >= max(first, 1) for other in normalized[1:]):
        return Choice(False, reason="the recommendation text paraphrases another option")
    return Choice(True, method="structured")


def sensitive_reason(blocker: Mapping[str, Any]) -> str | None:
    """Why the first option must stay with a human, or ``None``."""

    options = _options(blocker)
    raw = " ".join(
        [str(blocker.get("question") or ""), options[0] if options else "", str(blocker.get("recommendation") or "")]
    )
    haystack = _norm(raw)  # splits STRIPE_SECRET_KEY, GITHUB-TOKEN, db.password, a/b paths
    match = _SENSITIVE_RE.search(haystack) or _DOTENV_RE.search(raw)
    if match is not None:
        return (
            f"the option may involve secrets, credentials, money or deleting data (keyword {match.group(0)!r}); "
            "Harbor's best-effort heuristics leave that to a human"
        )
    match = _GATE_RE.search(haystack)
    if match is not None:
        return (
            f"the option may be about merging or lowering a gate (keyword {match.group(0)!r}); Harbor's "
            "best-effort heuristics leave that to a human"
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
            at TEXT NOT NULL,
            delivered INTEGER
        )"""
    )
    # ``delivered``: NULL while the resume is in flight, 1 once delivered, 0
    # when nothing reached the builder.  Added after the table first shipped
    # on this branch, so an existing table gains the column here.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(auto_accepts)")}
    if "delivered" not in columns:
        connection.execute("ALTER TABLE auto_accepts ADD COLUMN delivered INTEGER")
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
        "SELECT id, escalation_id, ticket, run_id, source, kind, question, option, recommendation, method, at, "
        "delivered FROM auto_accepts WHERE ticket = ? ORDER BY id",
        (str(ticket),),
    ).fetchall()
    keys = ("id", "escalation_id", "ticket", "run_id", "source", "kind", "question", "option", "recommendation",
            "method", "at", "delivered")
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
    kind = " ".join(str(blocker.get("kind") or "").casefold().replace("-", "_").split())
    if kind not in ELIGIBLE_KINDS:
        return Assessment("not_applicable", reason=f"{kind or 'unknown'} blockers are never auto-accepted")
    choice = recommended_first_option(blocker)
    if not choice.first:
        return Assessment("not_applicable", reason=choice.reason)
    raw_option = _options(blocker)[0]
    option = raw_option.strip()
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
    # The allowlist that decides; the deny-lists above only explain the
    # common cases more precisely.
    prose = plain_prose_reason(raw_option)  # the unstripped raw text
    if prose:
        return Assessment("refused", option, choice.method, f"option 1 is ineligible: {prose}")
    if _connection(db) is None:
        # No durable ledger means no loop guard; fail closed.
        return Assessment("refused", option, choice.method, "no Harbor ledger is available to enforce the loop guard")
    prior = history(db, ticket)
    question = str(blocker.get("question") or "")
    # The loop guard counts only answers that reached (or are reaching) the
    # builder: a receipt marked undelivered never resumed anything, so "the
    # builder blocked again" would be false.
    for row in prior:
        if row.get("delivered") == 0:
            continue
        if _equivalent(row["question"], question) or _norm(row["option"]) == _norm(option):
            label = f"escalation #{row['escalation_id']}" if row.get("escalation_id") is not None else "an earlier escalation"
            return Assessment(
                "refused", option, choice.method,
                f"Harbor already auto-accepted this recommendation for {label} and the builder blocked again "
                "with an equivalent question (80% word overlap or the same option), so a human must decide",
            )
    in_run = [row for row in prior if str(row.get("run_id") or "") == str(run_id or "")]
    count = len(in_run)
    if count >= policy.max_auto_accepts:
        undelivered = sum(1 for row in in_run if row.get("delivered") == 0)
        why = (f"; {undelivered} earlier auto-accept(s) in this run were not delivered" if undelivered else "")
        return Assessment(
            "refused", option, choice.method,
            f"the limit of {policy.max_auto_accepts} auto-accepts per builder run is reached ({LIMIT_SETTING}){why}",
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
    unanswered question again.  The ``auto_accepts`` receipt stays, marked
    undelivered: it still counts toward the per-run cap, but not for the loop
    guard (the builder never received it, so it cannot have "blocked again").
    """

    connection = _connection(db)
    if connection is None or escalation_id is None:
        return
    initialize(connection)
    with connection:
        connection.execute(
            "UPDATE escalations SET status = 'open', answered_at = NULL, answer = NULL, citation = NULL "
            "WHERE id = ? AND citation LIKE ?",
            (int(escalation_id), f"{AUTO_ACCEPT_CITATION}:%"),
        )
        connection.execute(
            "UPDATE auto_accepts SET delivered = 0 WHERE escalation_id = ? AND delivered IS NULL",
            (int(escalation_id),),
        )


def mark_delivered(db: Any, escalation_id: Any) -> None:
    """Mark the in-flight receipt for ``escalation_id`` delivered (the resume reached the builder)."""

    connection = _connection(db)
    if connection is None or escalation_id is None:
        return
    initialize(connection)
    with connection:
        connection.execute(
            "UPDATE auto_accepts SET delivered = 1 WHERE escalation_id = ? AND delivered IS NULL",
            (int(escalation_id),),
        )


# The decision fields plus every path-bearing field ``touches_protected_paths``
# reads, so an assessment never covers a blocker that later reports paths.
_DIGEST_FIELDS = (
    "kind", "question", "options", "recommendation", "recommended_option_index",
    "path", "file", "paths", "files", "touched_paths", "touchedFiles", "protected_paths", "protectedPaths",
)


class AutoAcceptWithdrawn(RuntimeError):
    """Auto-accept was turned off between the decision and the launch."""


def withdraw(db: Any, escalation_id: Any) -> None:
    """Undo an auto-accept abandoned before launch: no receipt, escalation open."""

    connection = _connection(db)
    if connection is None or escalation_id is None:
        return
    initialize(connection)
    with connection:
        connection.execute(
            "UPDATE escalations SET status = 'open', answered_at = NULL, answer = NULL, citation = NULL "
            "WHERE id = ? AND citation LIKE ?",
            (int(escalation_id), f"{AUTO_ACCEPT_CITATION}:%"),
        )
        connection.execute(
            "DELETE FROM auto_accepts WHERE escalation_id = ? AND delivered IS NULL", (int(escalation_id),),
        )


def blocker_digest(blocker: Mapping[str, Any]) -> str:
    """A stable digest of the builder-reported blocker fields an answer depends on.

    It binds an escalation's recorded facts to the exact blocker they were
    computed for: a resumed session keeps its run id, so an older open
    escalation for the same run is normal and must not lend its facts to a
    newer blocker.
    """

    payload = {name: blocker.get(name) for name in _DIGEST_FIELDS}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _eligibility_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS auto_accept_assessments (
            escalation_id INTEGER PRIMARY KEY,
            blocker_digest TEXT NOT NULL,
            protected INTEGER NOT NULL,
            human_decisions INTEGER NOT NULL,
            at TEXT NOT NULL
        )"""
    )


def record_eligibility(
    db: Any, escalation_id: Any, blocker: Mapping[str, Any], *, protected: bool, human_decisions: bool,
) -> None:
    """Store the ladder's protected-path and decision-mode facts for one escalation and blocker.

    First Mate has no protected-path configuration or worktree diff of its
    own, so it reads these facts instead of re-deriving them from a reason
    string.  An escalation without a row, or whose row was recorded for a
    different blocker, is never auto-accepted by First Mate.
    """

    connection = _connection(db)
    if connection is None or escalation_id is None:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connection:
        _eligibility_schema(connection)
        connection.execute(
            "INSERT OR REPLACE INTO auto_accept_assessments(escalation_id, blocker_digest, protected, "
            "human_decisions, at) VALUES (?,?,?,?,?)",
            (int(escalation_id), blocker_digest(blocker), int(bool(protected)), int(bool(human_decisions)), now),
        )


def eligibility(db: Any, escalation_id: Any) -> dict[str, Any] | None:
    """The ladder's recorded facts for ``escalation_id``, or ``None`` when none were recorded."""

    connection = _connection(db)
    if connection is None or escalation_id is None:
        return None
    _eligibility_schema(connection)
    row = connection.execute(
        "SELECT blocker_digest, protected, human_decisions FROM auto_accept_assessments WHERE escalation_id = ?",
        (int(escalation_id),),
    ).fetchone()
    if row is None:
        return None
    return {"blocker_digest": str(row[0]), "protected": bool(row[1]), "human_decisions": bool(row[2])}


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
        "The answer gives the builder no capability it did not already have; its exclusions for money, "
        "deletion, credentials and gates are best-effort keyword heuristics, not a review of this option.\n"
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
