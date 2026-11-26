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
{"review":{"head_sha":"<full reviewed commit>","verdict":"approve or reject","proof_verified":true,"feedback_digest":"<current readiness feedback_digest>","risk":"low or medium or high"}}
Set proof_verified=false when required proof is missing. Keep any existing ticket
risk floor; an implementation or wording change cannot downgrade it.
Post the verdict as a PR comment so the human merger can see it.

Obtain feedback_digest from the current `harbor workflow check` result before review. A failed check still reports this digest. New feedback invalidates the previous approval.
