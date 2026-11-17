"""Preserve observed delivery risk independently of blocker decisions."""
import re

TIERS = ('low','medium','high')


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
