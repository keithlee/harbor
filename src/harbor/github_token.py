"""Reuse the GitHub CLI's login when a GitHub project exports no token.

``gh`` keeps its token in the OS keychain by default, so an operator who ran
``gh auth login`` often has no ``GITHUB_TOKEN``/``GH_TOKEN`` exported. Inside
Claude Code's macOS sandbox ``gh`` cannot reach the keychain or complete TLS,
so a builder that falls back to ``gh`` hangs instead of failing.

Harbor itself runs outside the builder sandbox, so for a project that names a
``github_repository`` it can ask ``gh`` once at launch (``gh auth token
--hostname HOST``, with a short timeout) and hold the answer in its own
process environment as ``GH_TOKEN``. From there it reaches Harbor's GitHub
poller and the builder's REST helper the same way an exported token would. A
project without a GitHub repository never gets one this way. The token is
never written to config, the ledger, logs, prompts, or output; callers only
ever learn where it came from.
"""
from __future__ import annotations

import enum
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, MutableMapping
from urllib.parse import urlsplit

from harbor.github_rest import token_env_name

GH_TOKEN_ENV = "GH_TOKEN"
GH_TIMEOUT_SECONDS = 10
# A token is one printable, whitespace-free word; anything else is not a token.
_TOKEN_SHAPE = re.compile(r"[\x21-\x7e]{1,4096}")


class TokenSource(enum.Enum):
    EXPORTED = "exported"          # a token variable was already set
    GH_CLI = "gh auth token"       # adopted from the GitHub CLI at launch
    NOT_FOUND = "not found"        # GitHub project, but no token and no gh login
    NOT_NEEDED = "not needed"      # no github_repository: Harbor did not look
    BAD_ENDPOINT = "bad endpoint"  # the GitHub URL names no host gh could know


@dataclass(frozen=True)
class TokenResolution:
    """Where this process's GitHub token comes from. Never holds the token."""

    source: TokenSource
    env_name: str | None = None
    hostname: str | None = None

    @property
    def gh_checked(self) -> bool:
        return self.source in (TokenSource.GH_CLI, TokenSource.NOT_FOUND)


def gh_hostname(graphql_url: str | None) -> str | None:
    """The host ``gh`` knows the login by, or ``None`` when the URL names no usable host.

    ``api.github.com`` -> ``github.com``; GHE.com data residency
    ``api.acme.ghe.com`` -> ``acme.ghe.com``; GitHub Enterprise Server keeps its
    host and any port (``ghe.example.test:8443``).
    """

    raw = (graphql_url or "").strip()
    if not raw:
        return "github.com"
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or not host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return None
    if host in ("api.github.com", "github.com"):
        return "github.com"
    if host.endswith(".ghe.com") and host.startswith("api."):
        host = host[len("api."):]
    return f"{host}:{port}" if port else host


def token_from_gh(
    hostname: str,
    *,
    executable: str | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    timeout: float = GH_TIMEOUT_SECONDS,
) -> str | None:
    """Return ``gh``'s token for ``hostname``, or ``None``. Never raises, never prints.

    ``gh`` runs in its own session so a timeout kills its whole process group,
    including helpers such as the keychain client it may have started.
    """

    gh = executable or os.environ.get("HARBOR_GH_EXECUTABLE") or "gh"
    try:
        process = popen(
            [gh, "auth", "token", "--hostname", hostname],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        return None
    except (OSError, subprocess.SubprocessError):
        _kill_group(process)
        return None
    if process.returncode != 0:
        return None
    token = str(stdout or "").strip()
    return token if _TOKEN_SHAPE.fullmatch(token) else None


def _kill_group(process: Any) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.communicate(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def adopt_gh_token(
    preferred: str | None,
    graphql_url: str | None,
    *,
    environ: MutableMapping[str, str] | None = None,
    **kwargs: Any,
) -> TokenResolution:
    """Make sure a GitHub token is in this process's environment.

    Only the in-memory environment is changed; nothing is persisted.
    """

    env = os.environ if environ is None else environ
    name = token_env_name(env, preferred)
    if name:
        return TokenResolution(TokenSource.EXPORTED, name)
    hostname = gh_hostname(graphql_url)
    if hostname is None:
        return TokenResolution(TokenSource.BAD_ENDPOINT)
    token = token_from_gh(hostname, **kwargs)
    if token is None:
        return TokenResolution(TokenSource.NOT_FOUND, hostname=hostname)
    env[GH_TOKEN_ENV] = token
    return TokenResolution(TokenSource.GH_CLI, GH_TOKEN_ENV, hostname)


def adopt_for_profile(
    profile: Any,
    *,
    environ: MutableMapping[str, str] | None = None,
    **kwargs: Any,
) -> TokenResolution:
    """``adopt_gh_token`` for a ``PipelineProfile``, only when it names a GitHub repository.

    A project without ``github_repository`` (for example Linear-only) needs no
    GitHub token, so Harbor never hands its builders the operator's gh login.
    """

    env = os.environ if environ is None else environ
    preferred = getattr(profile, "github_token_env", None)
    if not (getattr(profile, "repository_owner", "") and getattr(profile, "repository_name", "")):
        return TokenResolution(TokenSource.NOT_NEEDED, token_env_name(env, preferred))
    return adopt_gh_token(preferred, getattr(profile, "github_api_url", None), environ=env, **kwargs)


def missing_token_help(token_names: str, graphql_url: str | None) -> str:
    """How a user fixes a missing GitHub token, naming no secret."""

    host = gh_hostname(graphql_url)
    gh = (
        f", or run `gh auth login --hostname {host}` so Harbor can reuse `gh auth token` at launch"
        if host else ""
    )
    return f"export {token_names}{gh}"
