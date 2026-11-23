# Harbor contributor instructions

Harbor is a deterministic supervisor. Preserve these boundaries in every
change:

- Linear and GitHub are authoritative for external state; SQLite stores only
  operational custody, idempotency, and receipts.
- Never persist or print Linear, GitHub, or model credentials.
- A ticket keeps the same builder session and worktree across re-engagement,
  unless First Mate recovery deliberately starts a fresh session (same
  worktree, recorded in `first_mate_recoveries`).
- Harbor observes GitHub but leaves GitHub mutations and terminal Linear
  completion to the configured delivery workflow.
- Configuration must fail closed; never substitute a product-specific project,
  team, repository, or workflow when required data is absent.

## Open source: design for a new user, not this machine

Harbor is a public, configurable tool. Write every change for someone who has
just cloned it onto a fresh machine. Assume they have no knowledge of how the
maintainer's setup works. When you design a feature, fix or default, answer these
questions in the PR:

- **What does a new user have to do?** List every new requirement: a CLI, an
  MCP server, a token or token scope, an OS permission, or a config key. Each one
  needs a `harbor doctor` check that names what is missing and how to fix it, and
  each one is documented in `README.md` or `docs/configurable-workflows.md` in the
  same change (or a linked docs PR that merges right after). A requirement you
  only discovered on the maintainer's machine is not documented until it is
  written down.
- **Does it still work if they configure nothing?** New capability is opt-in.
  With the new config absent, behaviour is unchanged, and Harbor says so instead
  of guessing. Defaults stay on the safe side: the builder sandbox stays on,
  `merge_mode` stays `human`, and nothing is auto-merged, auto-installed, or
  given more privilege (wider sandbox, network or permissions) without explicit
  config. Operator escalations for blockers are normal Harbor behaviour and are
  not affected by this rule.
- **Which environments does it cover?** State which of these the change was
  run on, and which it was not:
  - macOS and Linux
  - the Codex and Claude providers
  - GitHub.com and GitHub Enterprise Server, where the code path differs

  Platform-specific behaviour must be detected and explained, not left to a
  user to discover. For example, inside the macOS sandbox `gh` and
  `xcrun simctl` cannot reach system services, so a check that passes on Linux
  can hang on macOS.
- **Is there maintainer state baked in?** Remove it if so. That covers personal
  paths, repository or Linear names, model IDs, simulator names, keychain
  entries, local scripts, and shell aliases. Example values in docs and tests
  use neutral placeholders such as `SampleProject`.
- **How does the user supply credentials?** Read secrets from environment
  variables, or from the tool that already owns them (for example
  `gh auth token`). Never write them into config, MCP config files, the ledger,
  logs, prompts, or PR text. Prefer reusing a credential Harbor already requires
  (`GITHUB_TOKEN`/`GH_TOKEN` or the variable named by `github_token_env`;
  `LINEAR_API_KEY`/`HARBOR_LINEAR_API_KEY`) over asking for a new one.
  Document the minimum scopes the user needs.
- **Does it add a third-party tool?** This covers MCP servers, npm or pip
  packages, and hosted endpoints. Make it opt-in, pin its version, make the
  endpoint configurable (for example for GitHub Enterprise Server), document what
  it can access, and never install it silently. A hosted service is an
  availability and policy dependency for every user, so say so.
- **How do you prove it?** For onboarding, configuration or credential changes,
  capture `harbor init`/`harbor doctor`/`harbor start` output from a throwaway
  config, registry and database. Do not use the maintainer's live projects.
  Show both the success path and the fail-closed path.

## Pipeline Profile

```yaml
repo: keithlee/harbor
default_branch: main
platforms: [cli]
sources:
  notion_board: none
  productspec: none
  discoveries: none
commands:
  lint: uv run python -m compileall -q src tests
  build: uv build
  test: uv run pytest
  typecheck: none
proof:
  - fresh CLI output proving registry resolution and fail-closed configuration behavior
  - fresh isolated end-to-end test output for project, worktree, and session custody changes
  - for onboarding, configuration or credential changes, doctor/start output from a throwaway config showing both the ready and the fail-closed path
risk_budget:
  max_files: 50
  max_added_lines: 1500
  min_changed_line_coverage: 0.8
protected_paths:
  - migrations/**
  - schemas/**
  - .github/workflows/**
  - src/harbor/clients/**
  - src/harbor/worktree.py
required_secrets: []
design:
  themes: []
  system: none
deploy:
  preview: none
  health: none
domain_rules:
  - Linear and GitHub remain authoritative; SQLite is an operational ledger only
  - secrets are never persisted or logged
  - project configuration is explicit and never inherits product-specific defaults
  - same-ticket re-engagement resumes the retained builder session (First Mate recovery may start one fresh session)
  - protected-path or project-custody changes require review even when within the size budget
  - every new user-facing requirement is opt-in, doctor-checked and documented; defaults stay safe with no maintainer-specific values
  - state the platforms (macOS/Linux), providers (Codex/Claude) and GitHub hosts a change was verified on
spec_approval: auto
```
