"""Preserve observed delivery risk independently of blocker decisions."""
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


def merge_risk(db, project, ticket, run_id, raw, body, head_sha):
    """Effective pre_merge risk: max(retained Linear floor, current-head analysis tier).

    The analysis can only raise the Linear floor. The combined tier is retained
    per run, so neither a Linear edit nor a later, lower analysis can drop it.
    Without a current-head analysis tier the effective risk is unknown (None),
    so an automatic merge fails closed; the Linear floor is still retained.
    """
    tier = analysis_tier(body, head_sha)
    retained = retain_floor(db, project, ticket, run_id, observed_floor(raw, {'risk': tier} if tier else {}))
    return retained if tier else None


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
