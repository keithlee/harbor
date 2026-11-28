---
name: harbor-review
description: Review current-head implementation and durable proof against ticket acceptance criteria.
---
# Review
Read the live ticket, current diff, proof and CI; do not consume implementor reasoning.
Report PASS/FAIL per acceptance criterion with reproducible findings.
Approve only the exact reviewed head. You must not be the implementor of this change.
Do not claim proof for missing live evidence. Do not merge. A human-review handoff does not authorize merge.

Reproduce adversarial cases and verify each acceptance criterion against the current
commit and actual test/proof artifacts. Give findings to the controller, which
returns them to the retained implementor (Codex) or to a fresh implementor spawned
with your findings (Claude Code). Return ONLY one JSON object for your entire final message (no leading prose or Markdown fence). Put AC verdicts and findings in additional fields inside `review` if needed:
{"review":{"head_sha":"<full reviewed commit>","verdict":"approve or reject","proof_verified":true,"feedback_digest":"<current readiness feedback_digest>","risk":"low or medium or high","risk_rationale":"<why this tier>"}}
Set proof_verified=false when required proof is missing.

## Risk rating (required)
You rate the delivery risk of the exact reviewed head. `risk` is required and must be exactly
`low`, `medium` or `high` (lowercase). Rate it yourself from the current diff: changed paths, blast
radius, reversibility, data or configuration it touches, and any protected paths in the project's
pipeline profile. Never copy the builder's `## Risk Analysis` tier or its reasoning; form your rating
before you read it. Explain your reasoning in `risk_rationale`. Keep any existing ticket risk floor;
an implementation or wording change cannot downgrade it. When unsure between two tiers, choose the
higher one.

At `pre_merge` Harbor uses the highest of the Linear floor, the builder's tier and your rating. It
counts your rating only from an `approve` verdict comment for the PR's current head, posted by the
identity that opened the PR. A reject, a stale head, or a missing or invalid `risk` stops an automatic merge.

Post the verdict as a PR comment whose body is exactly that JSON verdict object (no prose around it),
so the human merger can see it and the controller can count review rounds from the PR.

Obtain feedback_digest from the current `harbor workflow check` result before review. A failed check still reports this digest. New feedback invalidates the previous approval.
