# Proof for keithlee/harbor PR #50

Pinned to commit `8a73059` on `fix/builder-github-token-from-gh`. Captured on macOS 27.2.1, Claude Code 2.1.332, gh 2.87.3. No real token value appears in any file; see `5-token-scan.txt`.

| File | What it shows |
| --- | --- |
| `1-before-sandboxed-builder.txt` | Before the fix. A Claude Code builder with Harbor's sandbox settings and no exported token. `curl` to api.github.com returns 200. `gh pr view 42` never returns, and the outer 150 s limit kills the builder. The host then finds `gh` still running. |
| `2-after-sandboxed-builder.txt` | After the fix. `harbor.cli._adopt_github_token` adopts `gh auth token` for a GitHub project, and does nothing for a Linear-only project. A sandboxed builder then runs `python -m harbor.github_rest pr-view 42` and gets PR #42 back in about 1 s, exit 0. |
| `3-doctor-start-throwaway.txt` | `harbor init` / `doctor` / `start --once` from a throwaway config, registry and database. (a) Ready: gh login reused. (b) Fail-closed: no token and no gh. (c) Linear-only: gh is never called and nothing is warned. |
| `4-scripts.txt` | The exact scripts used for 1-3 and 5. |
| `5-token-scan.txt` | The command and output of a scan of every proof artifact (and the throwaway ledgers) for the real `gh auth token` value. |
