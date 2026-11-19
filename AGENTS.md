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
spec_approval: auto
```
