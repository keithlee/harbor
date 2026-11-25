# Harbor projects example

This checked-in file is an empty format example, never a source of real user
configuration. Create a private registry and select it with
`HARBOR_PROJECTS_FILE`. Paths may use `~` or environment variables. Every
runnable project must point at its live pipeline; Harbor fails closed instead
of silently reading `AGENTS.md` or inheriting another product's identity.

| name | repo_path | pipeline_path | linear_project_slug | linear_team | github_repository | base_branch |
| --- | --- | --- | --- | --- | --- | --- |

Optional endpoint columns are `linear_api_url` (alias `api_url`) and, for
GitHub Enterprise Server, `github_api_url` (aliases `github_graphql_url` and
`github_endpoint`; for example `https://ghe.example.com/api/graphql`). For
these endpoint columns, a non-empty cell wins over the same setting in the
project's pipeline, and an empty cell is ignored: Harbor uses the pipeline's
value, then the default (`https://api.linear.app/graphql` or
`https://api.github.com/graphql`).

The GitHub columns `github_repository` (alias `github_repo`), `required_checks`
(alias `ci_required_checks`, a list such as `[CI, "Lint, typecheck"]`) and
`bot_rework_cap` (alias `bot_rework_rounds`) follow the same rule: a non-empty
cell wins over the pipeline, an empty cell is ignored, and then the pipeline's
value or the default applies. A `github_repository` cell without an owner (a
bare name) keeps the owner the pipeline sets with `github_owner`. A
`bot_rework_cap` cell that is not a non-negative integer stops Harbor with an
error instead of being replaced by the default.

Harbor resolves the repository and required checks once per project (the
pipeline profile). The builder's `HARBOR_GITHUB_REPOSITORY`, the GitHub poller,
PR-event processing, `harbor workflow check` and `harbor doctor` all read that
resolved value. The supervisor's bot-rework decision reads `bot_rework_cap`
from the project configuration, which applies the same precedence and accepts
the same spellings.

Harbor says so when a registry column has no effect, instead of dropping it
silently. A column is reported when:

- Harbor does not read it: a typo, or a pipeline-only setting such as
  `disk_floor_gb`, which belongs in the project's `pipeline.md`;
- `.harbor/workflow` always replaces it: `builder_model`, `firstmate_model`,
  `builder_fallback_models`, `firstmate_fallback_models` and `workflow_path`;
- the value Harbor resolved is not the cell's value, for example
  `builder_effort` when `.harbor/workflow` sets `reasoning_effort`;
- another spelling of the same setting wins, so this one is skipped: an alias
  next to its column (`heartbeat_interval` beside `heartbeat_seconds`, or
  `slug` beside a `linear_project_slug` column, even an empty one), or a second
  identity column such as `repo`, `repository` or `pipeline` beside
  `repo_path`/`pipeline_path` (for the GitHub repository, use
  `github_repository`);
- the cell is empty and Harbor uses that empty value instead of the
  pipeline's value or the default. Today this happens for most columns: for
  example, an empty `base_branch` resolves to an empty branch and an empty
  `hold` resolves to hold off. The endpoint and GitHub columns
  (`linear_api_url`, `github_api_url`, `github_repository`, `required_checks`,
  `bot_rework_cap` and their aliases) ignore an empty cell, and so do
  `linear_project_slug` and `linear_team`. Fill in an empty cell or remove the
  column.

`harbor doctor` prints a `Registry:` line for each one. `harbor start`,
`harbor facts` and `harbor status` print a warning once on stderr (so `--json`
output on stdout is unchanged). Each names the project, the column and the
registry file, with a hint when one applies: the registry column you probably
meant, or where the setting belongs. The warnings do not change doctor's exit
status.
