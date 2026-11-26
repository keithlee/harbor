"""Which parts of the developer environment the test suite is sealed off from."""

from __future__ import annotations

from pathlib import Path

# The developer's real home, captured before any test runs, so the isolation
# guard can prove that no test resolves ``~`` (and so ``~/.harbor``) to it.
REAL_HOME = Path.home().resolve()

# Variables Harbor reads (or hands to builders) that would otherwise leak from
# the developer's shell into the suite: registry/ledger/worktree locations,
# executables, GitHub/Linear endpoints and credentials.  Every ``HARBOR_*``
# variable is cleared as well, whatever its name.
ISOLATED_ENV_VARS = (
    "LINEAR_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GH_HOST",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_OWNER",
    "GITHUB_REPOSITORY",
    "CODEX_HOME",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def is_isolated_env_var(name: str) -> bool:
    return name.startswith("HARBOR_") or name in ISOLATED_ENV_VARS
