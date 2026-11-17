---
name: harbor-review
description: Review current-head implementation and durable proof against ticket acceptance criteria.
---
# Independent review
Read the live ticket, current diff, proof and CI; do not consume implementor reasoning.
Report PASS/FAIL per acceptance criterion with reproducible findings.
Approve only the exact reviewed head and record your actual registered agent ID.
Do not claim proof for missing live evidence. Do not merge. A human-review handoff does not authorize merge.

Reproduce adversarial cases and verify each acceptance criterion against the current
commit and actual test/proof artifacts. Give findings to the controller for the
retained implementor. Return ONLY one JSON object for your entire final message (no leading prose or Markdown fence). Put AC verdicts and findings in additional fields inside `review` if needed:
{"review":{"head_sha":"<full reviewed commit>","verdict":"approve or reject","proof_verified":true,"feedback_digest":"<current readiness feedback_digest>","risk":"low or medium or high"}}
Set proof_verified=false when required proof is missing. Keep any existing ticket
risk floor; an implementation or wording change cannot downgrade it.
A verdict is only accepted from observed adapter events for the registered reviewer.

Obtain feedback_digest from the current `harbor workflow check` result before review. A failed check still reports this digest. New feedback invalidates the previous approval.
