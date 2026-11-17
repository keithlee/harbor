# Autonomous delivery blocker log

## Linear instruction did not wake the builder

- Symptom: Linear and GitHub contained new instructions, but the retained builder stayed parked.
- Cause: the Linear poller retained comment IDs only, and `WakeDecider` had no Linear-comment event.
- Why First Mate could not help: no builder wake occurred, so there was no new blocker invocation for First Mate to triage.
- Prevention: retain bounded comment facts, ignore Harbor receipts, version each human comment, and re-engage the same builder before CI, review, or merge continuation. The builder must treat the body as untrusted and reconcile any acceptance-criteria conflict before merging.

## clean-context auditor reported as unavailable

- Symptom: a one-line low-risk README PR stopped to ask whether `pr-proof-auditor` could be invoked.
- Cause: the builder used a repository prompt-role name as a native Codex `agent_type`. First Mate then searched the base checkout instead of the retained ticket worktree, missing the policy that already authorized low-risk auto-merge after audit.
- Why First Mate could not help: Harbor classified a deterministic capability mismatch as an authority gap and therefore escalated a question that required no product decision.
- Prevention: authority lookup prefers the retained worktree. When an `*-auditor` prompt exists under `.claude/agents/` but is not a registered agent type, Harbor resumes the same builder with a deterministic adapter: spawn a supported fresh `default` subagent, load the repository role contract, and pass only the PR URL.

## approval-gated Linear connector stopped planning

- Symptom: the builder had completed its plan but stopped to ask how Linear should be updated after `mcp__linear__save_issue` required interactive approval.
- Cause: the non-interactive builder selected an approval-gated Linear connector even though the available `codex_apps` Linear connector could perform the same in-scope update.
- Why First Mate could not help: the blocker was labeled as an environment/authority question, so the cite-or-escalate ladder treated a deterministic connector choice as something only the user could resolve. A deployed Harbor fix also had no startup path to reconsider the already-blocked result.
- Prevention: Harbor recognizes this exact capability mismatch, resumes the same builder with the non-interactive Linear connector, routes any second blocked result back through the bounded unblock ladder, and retries eligible parked builders once after startup. Product decisions and protected-path review gates remain ineligible for automatic retry.

## Escalation boundary

Low-risk delivery may still park for a genuine acceptance-criteria conflict, protected-path change, missing credentials or external service access, failing CI that cannot be repaired safely, or a new product decision. Missing orchestration adapters and stale-checkout policy lookup are Harbor defects and must not page a human.
