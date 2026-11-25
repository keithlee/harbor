"""A tiny read-only GitHub GraphQL client.

Harbor observes pull requests; the implementing skill is the only component
allowed to mutate GitHub.  Keeping this client query-only makes that boundary
auditable and lets tests inject a transport without contacting GitHub.
"""

from __future__ import annotations

import inspect
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

import httpx


class GitHubError(RuntimeError):
    """Base error for a failed GitHub read."""


class GitHubGraphQLError(GitHubError):
    """GitHub returned a valid HTTP response containing GraphQL errors."""


GITHUB_TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")


def _github_token_from_environment(preferred: str) -> str | None:
    """Read the configured token name, then GitHub's two standard aliases."""

    names = tuple(dict.fromkeys(name for name in (preferred, *GITHUB_TOKEN_ENV_VARS) if name))
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _aliases(numbers: Iterable[int]) -> tuple[tuple[str, int], ...]:
    seen: set[int] = set()
    result: list[tuple[str, int]] = []
    for number in numbers:
        try:
            number = int(number)
        except (TypeError, ValueError):
            continue
        if number <= 0 or number in seen:
            continue
        seen.add(number)
        result.append((f"pr_{number}", number))
    return tuple(result)


def build_pull_requests_query(owner: str, repo: str, numbers: Iterable[int]) -> str:
    """Build one aliased query for exactly ``numbers``.

    Pull request numbers are validated and emitted as integer GraphQL literals;
    owner/repository remain variables.  This avoids a broad ``pullRequests``
    connection that would accidentally include unrelated legacy PRs.
    """

    aliases = _aliases(numbers)
    fields = "\n".join(
        f"""      {alias}: pullRequest(number: {number}) {{
        number
        url
        title
        body
        state
        merged
        mergedAt
        mergeable
        createdAt
        headRefOid
        comments(first: 100) {{
          nodes {{
            id
            body
            authorAssociation
            createdAt
            updatedAt
            url
            author {{
              login
              __typename
            }}
          }}
        }}
        reviews(first: 100) {{
          nodes {{
            id
            body
            state
            submittedAt
            createdAt
            updatedAt
            url
            author {{
              login
              __typename
            }}
          }}
        }}
        statusCheckRollup {{
          state
          contexts(first: 100) {{
            nodes {{
              __typename
              ... on CheckRun {{ name conclusion status }}
              ... on StatusContext {{ context state }}
            }}
          }}
        }}
        reviewThreads(first: 100) {{
          nodes {{
            id
            isResolved
            isOutdated
            path
            line
            comments(first: 100) {{
              nodes {{
                id
                body
                createdAt
                updatedAt
                url
                path
                line
                author {{
                  login
                  __typename
                }}
              }}
            }}
          }}
        }}
      }}"""
        for alias, number in aliases
    )
    # A query with no aliases is not useful and is never sent by the poller,
    # but keeping a valid document helps direct callers and fakes.
    return f"""query HarborPullRequests($owner: String!, $repo: String!) {{
  repository(owner: $owner, name: $repo) {{
{fields}
  }}
}}"""


_QUERY_START = re.compile(r"^\s*(?:query(?:\s|\{|\()|\{)", re.IGNORECASE)


class GitHubClient:
    """Execute GraphQL queries against GitHub, with an injectable transport.

    ``transport`` may be an async callable accepting ``(query, variables)`` or
    an object exposing ``post``/``request`` like an ``httpx`` client.  It is
    intentionally a transport seam, not a second GitHub API abstraction.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        endpoint: str = "https://api.github.com/graphql",
        transport: Any | None = None,
        timeout: float = 20.0,
        token_env: str = "GITHUB_TOKEN",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token = token or _github_token_from_environment(token_env)
        self.endpoint = endpoint
        self.transport = transport
        self.timeout = timeout
        self._owns_client = http_client is None and isinstance(transport, httpx.AsyncBaseTransport)
        self._client = http_client
        if self._client is None and self._owns_client:
            self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    @classmethod
    def from_profile(cls, profile: object, **kwargs: Any) -> "GitHubClient":
        # A ProjectConfig carries github_api_url = "" when the registry does
        # not set it; never post to an empty URL.
        endpoint = getattr(profile, "github_api_url", "") or "https://api.github.com/graphql"
        token_env = getattr(profile, "github_token_env", "GITHUB_TOKEN")
        return cls(endpoint=endpoint, token_env=token_env, **kwargs)

    async def query(
        self,
        document: str,
        variables: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Perform one read-only GraphQL operation and return its JSON object."""

        if not _QUERY_START.match(document):
            raise ValueError("Harbor's GitHub client accepts query operations only")
        payload = {"query": document, "variables": dict(variables or {})}
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"bearer {self.token}"

        if self._client is not None:
            try:
                response = await self._client.post(self.endpoint, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise GitHubError(f"GitHub request failed: {exc}") from exc
        elif self.transport is not None:
            result = await self._transport_call(payload, headers)
        else:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.endpoint, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()
        if not isinstance(result, Mapping):
            raise GitHubError("GitHub response was not a JSON object")
        errors = result.get("errors")
        if errors:
            raise GitHubGraphQLError(f"GitHub GraphQL errors: {errors!r}")
        return result

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _transport_call(self, payload: Mapping[str, Any], headers: Mapping[str, str]) -> Any:
        transport = self.transport
        if callable(transport):
            result = transport(payload["query"], payload["variables"])
        elif hasattr(transport, "post"):
            result = transport.post(self.endpoint, json=payload, headers=dict(headers))
        elif hasattr(transport, "request"):
            result = transport.request("POST", self.endpoint, json=payload, headers=dict(headers))
        else:
            raise TypeError("GitHub transport must be callable or expose post/request")
        result = await result if inspect.isawaitable(result) else result
        # Fakes often return an httpx-like response.  Accept either that or a
        # decoded mapping so tests can focus on facts rather than HTTP details.
        if hasattr(result, "raise_for_status"):
            result.raise_for_status()
        if hasattr(result, "json"):
            result = result.json()
            result = await result if inspect.isawaitable(result) else result
        return result

    async def pull_requests(
        self,
        owner: str,
        repo: str,
        numbers: Iterable[int],
    ) -> Mapping[str, Any]:
        numbers = tuple(numbers)
        if not _aliases(numbers):
            return {"data": {"repository": {}}}
        return await self.query(
            build_pull_requests_query(owner, repo, numbers),
            {"owner": owner, "repo": repo},
        )

    # Descriptive alias for callers that prefer an explicit read name.
    fetch_pull_requests = pull_requests


__all__ = [
    "GitHubClient",
    "GitHubError",
    "GitHubGraphQLError",
    "build_pull_requests_query",
]
