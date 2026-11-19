---
name: harbor-risk
description: Assess current-head delivery risk and proof requirements for a Harbor ticket.
---
Assess changed paths, blast radius, reversibility and acceptance criteria. Preserve
any existing ticket risk floor. Medium/high risk requires a human merge unless the project changed `auto_merge_risks`.
Write a `## Risk Analysis` section in the PR body, including rationale, risk tier,
proof links, and `<!-- harbor:risk-sha:HEAD_SHA -->` with the current full commit SHA.
A wording decision cannot downgrade delivery risk. Reassess after every push.
