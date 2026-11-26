"""The project's GitHub API token: which environment variable holds it.

Every GitHub-facing part of Harbor uses one token per project: the GitHub
poller, the builder and its child agents (planner, implementor, reviewer),
the builder's REST helper, ``harbor workflow check`` and First Mate. The
project names the variable with ``github_token_env`` (registry row or
``.harbor/pipeline.md``); the secret itself stays in the environment and never
appears in config, the ledger, logs, prompts or PR text.

Precedence:

- ``github_token_env`` set: only that variable is used. If it is unset or
  blank, the project has no token; Harbor never falls back to a broader
  ``GITHUB_TOKEN``/``GH_TOKEN`` the operator happens to have exported.
- ``github_token_env`` not set: ``GITHUB_TOKEN``, then ``GH_TOKEN`` (Harbor's
  behaviour before the key existed).

A whitespace-only value counts as unset everywhere. Harbor never asks the
GitHub CLI for a token: inside Claude Code's macOS sandbox ``gh`` hangs, and
the operator's ``gh`` login is not the project's token.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping

DEFAULT_TOKEN_ENVS = ("GITHUB_TOKEN", "GH_TOKEN")
# Fine-grained personal access token permissions on the project's repository.
MINIMUM_SCOPES = "Contents: read and write, Pull requests: read and write, Checks: read, Commit statuses: read"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
# Prefixes of GitHub token values; a variable name never starts with one.
_TOKEN_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")


def validate_token_env(value: Any, *, where: str = "github_token_env") -> str:
    """Return a valid variable name ('' when unset); refuse anything that is not one.

    ``github_token_env`` names a variable. A pasted token is refused without
    echoing it.
    """

    name = str(value or "").strip()
    if not name:
        return ""
    if name.lower().startswith(_TOKEN_PREFIXES):
        raise ValueError(
            f"{where} must name an environment variable, not hold a token; "
            "move the token into an environment variable and put that variable's name here"
        )
    if not _ENV_NAME.fullmatch(name):
        raise ValueError(f"{where} must be an environment variable name such as SAMPLE_GITHUB_TOKEN")
    return name


def token_env_candidates(configured: str | None) -> tuple[str, ...]:
    """The variables to read, in order (see the module docstring)."""

    name = (configured or "").strip()
    return (name,) if name else DEFAULT_TOKEN_ENVS


def project_token_env(profile: Any, environ: Mapping[str, str] | None = None) -> str | None:
    """Name of the variable holding the project's token, or ``None``. Never reads the value out."""

    env = os.environ if environ is None else environ
    for name in token_env_candidates(getattr(profile, "github_token_env", "")):
        if (env.get(name) or "").strip():
            return name
    return None


def preferred_token_env(profile: Any, environ: Mapping[str, str] | None = None) -> str:
    """The variable a Harbor command should read: inside a builder, the one Harbor named."""

    env = os.environ if environ is None else environ
    return (env.get("HARBOR_GITHUB_TOKEN_ENV") or "").strip() or str(getattr(profile, "github_token_env", "") or "")


def missing_token_help(profile: Any) -> str:
    """Exactly what to set when a GitHub project has no token. Names no secret, never suggests gh."""

    configured = str(getattr(profile, "github_token_env", "") or "").strip()
    repository = str(getattr(profile, "repository", "") or "the project's repository")
    if configured:
        what = f"export {configured} (named by this project's github_token_env)"
    else:
        what = (
            "set github_token_env in the project's registry row or .harbor/pipeline.md to the name of an "
            "environment variable (for example SAMPLE_GITHUB_TOKEN) and export it "
            "(without github_token_env, Harbor reads GITHUB_TOKEN, then GH_TOKEN)"
        )
    return (
        f"{what}, holding a fine-grained GitHub token for {repository} with {MINIMUM_SCOPES}; "
        "never put the token itself in a config file"
    )
