"""Preserve observed delivery risk independently of blocker decisions."""
import json
import re

from harbor.pr_events import risk_analysis_marker

TIERS = ('low','medium','high')
RISK_ANALYSIS_HEADING = '## Risk Analysis'
# The only accepted form: a line that is exactly `Risk tier: low|medium|high`
# inside the `## Risk Analysis` section. Free text is never interpreted.
_ANALYSIS_TIER = re.compile(r'(?im)^[ \t]*risk tier:[ \t]*(low|medium|high)[ \t]*$')
_HEADING = re.compile(r'(?m)^##[ \t]+\S.*$')


def analysis_tier(body, head_sha):
    """The tier a PR body's current-head `## Risk Analysis` section states, or None.

    A section counts only when it carries ``<!-- harbor:risk-sha:HEAD -->`` for
    ``head_sha`` itself (stale or missing marker -> None) and at least one exact
    ``Risk tier: <tier>`` line. Conflicting lines resolve to the highest tier.
    """
    if not isinstance(body, str) or not head_sha:
        return None
    marker = risk_analysis_marker(head_sha)
    headings = list(_HEADING.finditer(body))
    tiers = []
    for index, heading in enumerate(headings):
        if heading.group(0).rstrip() != RISK_ANALYSIS_HEADING:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        section = body[heading.end():end]
        if marker in section:
            tiers += [tier.casefold() for tier in _ANALYSIS_TIER.findall(section)]
    return max(tiers, key=TIERS.index) if tiers else None


_VERDICTS = ('approve', 'reject')


class _Malformed(Exception):
    """A delivery-identity verdict comment Harbor cannot read unambiguously."""


def _verdict(comment, author):
    """The review object of one delivery-identity verdict comment, or None.

    A comment counts only when its author is the PR's opener and its whole
    body is a JSON object of the form ``{"review": {...}}``; any other comment
    is ignored. A ``review`` value that is not an object, or that lacks a
    string ``head_sha``, raises ``_Malformed``.
    """
    login = comment.get('author')
    if not isinstance(login, str) or login.casefold() != author.casefold():
        return None
    try:
        data = json.loads(str(comment.get('body') or '').strip())
    except ValueError:
        return None
    if not isinstance(data, dict) or 'review' not in data:
        return None
    review = data['review']
    if not isinstance(review, dict) or not isinstance(review.get('head_sha'), str):
        raise _Malformed
    return review


def reviewer_rating(facts, head_sha):
    """The reviewer's risk tier for ``head_sha``, or None when it cannot be used.

    Only PR conversation comments whose author is the PR's opener (the
    delivery identity) count, and only verdicts whose ``head_sha`` is
    ``head_sha``. Every such verdict must be ``approve`` with a
    ``low|medium|high`` ``risk``. A reject, a malformed or ambiguous verdict,
    or no verdict at all gives None. Several approvals give the highest tier.
    """
    author = facts.get('author') if isinstance(facts, dict) else None
    if not isinstance(author, str) or not author or not head_sha:
        return None
    comments = facts.get('issue_comments')
    if comments is None:
        comments = [c for c in facts.get('comments') or [] if c.get('surface') == 'issue']
    tiers = []
    try:
        for comment in comments or []:
            review = _verdict(comment, author)
            if review is None or review['head_sha'] != head_sha:
                continue
            if review.get('verdict') not in _VERDICTS or review.get('risk') not in TIERS:
                return None
            if review['verdict'] != 'approve':
                return None
            tiers.append(review['risk'])
    except _Malformed:
        return None
    return max(tiers, key=TIERS.index) if tiers else None


def merge_risk(db, project, ticket, run_id, raw, facts):
    """Effective pre_merge risk: the highest of three tiers.

    The tiers are the Linear floor, the builder's current-head
    ``## Risk Analysis`` tier and the reviewer's current-head rating; none can
    lower another. The combined tier is retained per run, so a later, lower
    tier cannot drop it. If the builder tier or the reviewer rating is missing
    for the current head, the effective risk is unknown (None) and an automatic
    merge fails closed; the tiers that were observed are still retained.
    """
    head = facts.get('head_sha')
    analysis = analysis_tier(facts.get('body'), head)
    review = reviewer_rating(facts, head)
    tiers = [tier for tier in (observed_floor(raw, {}), analysis, review) if tier]
    retained = retain_floor(db, project, ticket, run_id, max(tiers, key=TIERS.index) if tiers else None)
    return retained if analysis and review else None


def observed_floor(raw, review):
    tiers = [review['risk']] if review.get('risk') in TIERS else []
    description = raw.get('description') or ''
    tiers += [tier.casefold() for tier in re.findall(r'(?im)\brisk(?:[ _-]tier)?\s*[:=]\s*\**(low|medium|high)\b',description)]
    labels = raw.get('labels',{}).get('nodes',[])
    for label in labels:
        name=str(label.get('name','')).casefold()
        match=re.fullmatch(r'risk[: /_-]+(low|medium|high)',name)
        if match:
            tiers.append(match.group(1))
    return max(tiers,key=TIERS.index) if tiers else None


def retain_floor(db,project,ticket,run_id,risk):
    db.execute('CREATE TABLE IF NOT EXISTS workflow_risk_floor (project TEXT,ticket TEXT,run_id TEXT,risk TEXT,PRIMARY KEY(project,ticket,run_id))')
    row=db.execute('SELECT risk FROM workflow_risk_floor WHERE project=? AND ticket=? AND run_id=?',(project,ticket,run_id)).fetchone()
    if row and (risk not in TIERS or TIERS.index(row[0]) > TIERS.index(risk)):
        risk=row[0]
    if risk in TIERS:
        with db:
            db.execute('INSERT OR REPLACE INTO workflow_risk_floor VALUES (?,?,?,?)',(project,ticket,run_id,risk))
    return risk
