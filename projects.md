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
