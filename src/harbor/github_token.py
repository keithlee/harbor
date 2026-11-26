"""The project's GitHub API token: which environment variable holds it.

Every GitHub-facing part of Harbor uses one token per project: the GitHub
poller, the builder and its child agents (planner, implementor, reviewer),
the builder's REST helper, ``harbor workflow check`` and the rung-0 check of
a ``github_transport`` blocker (``harbor.facts.remediation``). The
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
# GitHub token *shapes*: a prefix, an underscore and a long body (gh[pousr]_…,
# github_pat_…), or a legacy 40-hex classic token. A genuine variable name such
# as GHS_TOKEN or GH_PAT_NAME does not match.
_TOKEN_SHAPES = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}", re.IGNORECASE),
    re.compile(r"[0-9a-fA-F]{40}"),
)


def looks_like_token(value: str) -> bool:
    return any(shape.fullmatch(value) for shape in _TOKEN_SHAPES)


def validate_token_env(value: Any, *, where: str = "github_token_env") -> str:
    """Return a valid variable name ('' when unset); refuse anything that is not one.

    ``github_token_env`` names a variable. A refused value is never echoed in
    the error: it might be a token pasted into the wrong place.
    """

    name = str(value or "").strip()
    if not name:
        return ""
    if looks_like_token(name):
        raise ValueError(
            f"{where} looks like a GitHub token value, not a variable name; put the token in an "
            "environment variable, put that variable's name here, and revoke the token if this "
            "config file was shared"
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
    for name in token_env_candidates(validate_token_env(getattr(profile, "github_token_env", ""))):
        if (env.get(name) or "").strip():
            return name
    return None


def preferred_token_env(profile: Any, environ: Mapping[str, str] | None = None) -> str:
    """The variable a Harbor command should read: inside a builder, the one Harbor named."""

    env = os.environ if environ is None else environ
    named = validate_token_env(env.get("HARBOR_GITHUB_TOKEN_ENV"), where="HARBOR_GITHUB_TOKEN_ENV")
    return named or validate_token_env(getattr(profile, "github_token_env", ""))


def missing_token_help(profile: Any) -> str:
    """Exactly what to set when a GitHub project has no token. Names no secret, never suggests gh."""

    try:
        configured = validate_token_env(getattr(profile, "github_token_env", ""))
    except ValueError:
        # Never echo a value that failed validation (it may be a token).
        configured = ""
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
