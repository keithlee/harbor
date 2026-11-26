---
name: harbor-risk
description: Assess current-head delivery risk and proof requirements for a Harbor ticket.
---
Assess changed paths, blast radius, reversibility and acceptance criteria. Preserve
any existing ticket risk floor. Medium/high risk requires a human merge unless the project changed `auto_merge_risks`.
Write a `## Risk Analysis` section in the PR body, including rationale, risk tier,
proof links, and `<!-- harbor:risk-sha:HEAD_SHA -->` with the current full commit SHA.
State the tier on its own line inside that section, in exactly this form:

    Risk tier: low

(`low`, `medium` or `high`; no bold, bullets or other text on the line). Harbor
reads only that line, and only when the section carries the current head's marker.
At `pre_merge` the effective risk is the higher of the ticket's Linear floor and
this tier; a missing, stale or unparseable tier leaves the risk unknown and the
automatic merge fails closed.
A wording decision cannot downgrade delivery risk. Reassess after every push.
