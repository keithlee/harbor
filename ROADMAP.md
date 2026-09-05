# Harbor Roadmap

## Next phase: portable onboarding and distribution

Harbor's supervision core is usable in a configured local environment. The
next phase makes that environment reproducible for someone who has neither
the Bookgen configuration nor local Codex skills and agent instructions.

### 1. Publishable package and release contract

- Create and maintain a GitHub repository, release notes, and versioned
  installation instructions.
- Verify a clean clone can run `uv sync` and `harbor --help` with Python 3.11.
- Document supported operating systems, required executables (`git`, `uv`,
  `codex`), Codex authentication, and the model-availability requirement.

**Done when:** a clean machine can install the released package without any
Bookgen path, token, or local-agent assumption.

### 2. User-owned project bootstrap

- Add `harbor init` to create a user-owned registry and a target repository
  `.harbor/pipeline.md` from generic examples.
- Move the checked-in Bookgen row out of the default registry. Ship
  `projects.example.md` and `pipeline.example.md` with placeholders, while
  accepting `HARBOR_PROJECTS_FILE` for a private registry.
- Collect or validate the target checkout, Linear project slug, GitHub repo,
  base branch, required checks, worktree root, and project install command.

**Done when:** a user can register `MyProject` without editing Harbor source
files or inheriting `keithlee/Bookgen` configuration.

### 3. Self-contained delivery contract

- Replace the symbolic default `skill: /work-pipeline` with a bundled,
  documented baseline delivery contract, or implement explicit skill-path
  resolution with a clear missing-skill error.
- Allow a project pipeline to contain the full plan, implementation, proof,
  review, tracker-update, and merge policy required by its builder.
- Provide an optional proof-audit contract; do not depend on Bookgen's
  `.claude/agents/pr-proof-auditor.md` or an external `AGENTS.md`.

**Done when:** a builder started from a clean user account receives actionable
delivery instructions and cannot silently treat an unresolved skill name as a
workflow.

### 4. Complete preflight and safe first-run validation

- Expand `harbor doctor` beyond Linear scope to check Codex availability and
  authentication, GitHub token access, target-repository `origin/<base>`,
  worktree permissions, disk floor, selected install command, and configured
  model names.
- Report secrets only as present or missing; never persist or print them.
- Make `harbor start <project> --shadow` the documented first live check and
  surface all failed prerequisites before a builder can be dispatched.

**Done when:** a passing doctor result means the machine can safely perform a
shadow poll and has every prerequisite for a real builder dispatch.

### 5. End-to-end proof and automated coverage

- Add a disposable-repository integration test covering bootstrap, shadow
  polling, isolated worktree creation, dependency installation, and a mocked
  Codex delivery run.
- Build a reusable opt-in low-risk canary command that creates a uniquely
  scoped docs-only Linear ticket, monitors Harbor through PR creation,
  re-engagement, review, CI, merge, and cleanup, and preserves transition-only
  evidence for diagnosis. Keep this out of the runtime until its external
  mutation contract and failure recovery have an independently reviewed spec.
- Run that canary against a non-production Linear/GitHub project before broad
  supervisor or lifecycle changes are merged.
- Keep the existing fail-closed behavior: no cleanup from a self-reported
  merge, no duplicate builder session, and no terminal Linear `Done` write by
  Harbor itself.

**Done when:** the documented setup has both deterministic CI coverage and a
fresh external canary record.

## Sequencing

Implement phases 1-3 before inviting external users. Complete phase 4 before
allowing a non-shadow dispatch. Phase 5 is the release gate for claiming
turnkey autonomous delivery.
