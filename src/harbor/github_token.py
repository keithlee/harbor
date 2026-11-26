"""Reuse the GitHub CLI's login when no GitHub token is exported.

``gh`` keeps its token in the OS keychain by default, so an operator who ran
``gh auth login`` often has no ``GITHUB_TOKEN``/``GH_TOKEN`` exported. Inside
Claude Code's macOS sandbox ``gh`` cannot reach the keychain or complete TLS,
so a builder that falls back to ``gh`` hangs instead of failing.

Harbor itself runs outside the builder sandbox, so it can ask ``gh`` once at
launch (``gh auth token --hostname HOST``, with a short timeout) and hold the
answer in its own process environment as ``GH_TOKEN``. From there it reaches
Harbor's GitHub poller and the builder's REST helper the same way an exported
token would. The token is never written to config, the ledger, logs, prompts,
or output; callers only ever learn where it came from.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Callable, MutableMapping
from urllib.parse import urlsplit

from harbor.github_rest import token_env_name

GH_TOKEN_ENV = "GH_TOKEN"
GH_TIMEOUT_SECONDS = 10
# A token is one printable, whitespace-free word; anything else is not a token.
_TOKEN_SHAPE = re.compile(r"[\x21-\x7e]{1,4096}")


def gh_hostname(graphql_url: str | None) -> str:
    """The host ``gh`` knows the login by: ``github.com`` for GitHub.com, else the GHES host."""

    host = (urlsplit((graphql_url or "").strip()).hostname or "").lower()
    if not host or host in ("api.github.com", "github.com"):
        return "github.com"
    return host


def token_from_gh(
    hostname: str,
    *,
    executable: str | None = None,
    run: Callable[..., Any] = subprocess.run,
    timeout: float = GH_TIMEOUT_SECONDS,
) -> str | None:
    """Return ``gh``'s token for ``hostname``, or ``None``. Never raises, never prints."""

    gh = executable or os.environ.get("HARBOR_GH_EXECUTABLE") or "gh"
    try:
        result = run(
            [gh, "auth", "token", "--hostname", hostname],
            capture_output=True, text=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    token = str(getattr(result, "stdout", "") or "").strip()
    return token if _TOKEN_SHAPE.fullmatch(token) else None


def adopt_gh_token(
    preferred: str | None,
    graphql_url: str | None,
    *,
    environ: MutableMapping[str, str] | None = None,
    **kwargs: Any,
) -> str | None:
    """Make sure a GitHub token is in this process's environment.

    Returns where the token comes from: the name of an exported variable that
    was already set, ``"gh auth token"`` when it was adopted from the GitHub
    CLI, or ``None`` when there is no token anywhere. Only the in-memory
    environment is changed; nothing is persisted.
    """

    env = os.environ if environ is None else environ
    name = token_env_name(env, preferred)
    if name:
        return name
    token = token_from_gh(gh_hostname(graphql_url), **kwargs)
    if token is None:
        return None
    env[GH_TOKEN_ENV] = token
    return "gh auth token"


def adopt_for_profile(profile: Any, **kwargs: Any) -> str | None:
    """``adopt_gh_token`` for a ``PipelineProfile`` (token variable and endpoint)."""

    return adopt_gh_token(
        getattr(profile, "github_token_env", None), getattr(profile, "github_api_url", None), **kwargs,
    )


def missing_token_help(token_names: str, graphql_url: str | None) -> str:
    """How a user fixes a missing GitHub token, naming no secret."""

    return (
        f"export {token_names}, or run `gh auth login --hostname {gh_hostname(graphql_url)}` "
        "so Harbor can reuse `gh auth token` at launch"
    )
