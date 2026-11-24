"""Read-only Linear GraphQL client for Phase 1."""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Sequence

import httpx

DEFAULT_LINEAR_API_URL = "https://api.linear.app/graphql"


LINEAR_QUERY = """
query HarborIssues($slugs: [String!]!, $states: [String!]!) {
  issues(filter: {project: {slugId: {in: $slugs}}, state: {name: {in: $states}}}, first: 50) {
    nodes {
      id identifier title description url updatedAt branchName
      state { name type }
      attachments(first: 10) { nodes { url sourceType } }
      comments(first: 20) { nodes { id createdAt updatedAt body user { id name } } }
      inverseRelations(first: 20) { nodes { type issue { identifier state { type } } } }
    }
  }
}
"""

HEALTH_QUERY = "query HarborHealth { viewer { id } }"

LINEAR_PROJECTS_QUERY = "query HarborProjects { projects(first: 50) { nodes { id name slugId } } }"

LINEAR_ISSUE_STATUS_QUERY = """
query HarborIssueStatus($id: String!) {
  issue(id: $id) {
    id identifier title url updatedAt
    state { name type }
    project { slugId }
    attachments(first: 20) { nodes { url sourceType } }
  }
}
"""

# Linear's issueUpdate takes a workflow state UUID, never a name.  Harbor's
# config and the plan both speak in names ("Blocked", "Human Review"), so the
# client resolves a name to its id once and caches it.
LINEAR_STATES_QUERY = """
query HarborStates($issueId: String!) {
  issue(id: $issueId) {
    id
    team { id name states(first: 50) { nodes { id name type } } }
  }
}
"""

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# These mutations are deliberately narrow.  Harbor may report progress and
# move an owned ticket to Blocked/Human Review, but it never writes Done (the
# builder skill owns that transition after it has actually merged a PR).
LINEAR_COMMENT_MUTATION = """
mutation HarborComment($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) {
    success
    comment { id body createdAt }
  }
}
"""

LINEAR_STATE_MUTATION = """
mutation HarborProgress($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: {stateId: $stateId}) {
    success
    issue { id identifier state { id name type } }
  }
}
"""


def project_slugs(value: str | Sequence[str] | None) -> tuple[str, ...]:
    """Normalize the configured project scope to a de-duplicated slug tuple."""

    if value is None:
        return ()
    if isinstance(value, str):
        from harbor.config.projects import _split_list

        candidates: Sequence[str] = _split_list(value) or ((value.strip(),) if value.strip() else ())
    else:
        candidates = [str(item) for item in value]
    seen: list[str] = []
    for item in candidates:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


class LinearClientError(RuntimeError):
    """A network, HTTP, or GraphQL failure."""


def resolve_token(explicit: str | None = None) -> str | None:
    """Resolve a token without ever logging or persisting it."""

    return explicit or os.environ.get("LINEAR_API_KEY") or os.environ.get("HARBOR_LINEAR_API_KEY")


class LinearClient:
    """Async, injectable HTTP client.

    ``transport`` is intentionally exposed so tests can use ``httpx.MockTransport``
    and exercise the real request/response path without a live account.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        endpoint: str = DEFAULT_LINEAR_API_URL,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.token = resolve_token(token)
        self._state_ids: dict[str, str] = {}
        self._state_types: dict[str, str] = {}
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            # Linear accepts the raw API key.  Do not turn this into a bearer token.
            headers["Authorization"] = self.token
        return headers

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    aclose = close

    async def fetch_blocker_context(self, identifier: str) -> dict[str, Any]:
        """Read current AC, dependencies, PR links and comments on one issue."""
        query = """query HarborBlocker($id: String!, $after: String) {
          issue(id: $id) {
            id identifier title description url branchName updatedAt state { name type }
            project { slugId }
            labels(first: 100) { nodes { name } pageInfo { hasNextPage } }
            attachments(first: 100) { nodes { url sourceType } pageInfo { hasNextPage } }
            inverseRelations(first: 100) {
              nodes { type issue { identifier state { type } } }
              pageInfo { hasNextPage }
            }
            comments(first: 50, after: $after) {
              nodes { id body createdAt updatedAt user { id name } }
              pageInfo { hasNextPage endCursor }
            }
          }
        }"""
        comments: list[dict[str, Any]] = []
        after = None
        cursors: set[str] = set()
        for _ in range(100):
            data = await self._post(query, {"id": identifier, "after": after})
            issue = data.get("issue")
            if not isinstance(issue, dict) or issue.get("identifier") != identifier:
                raise LinearClientError("cannot verify blocked ticket identity")
            for field in ("attachments", "inverseRelations", "labels"):
                if issue.get(field, {}).get("pageInfo", {}).get("hasNextPage"):
                    raise LinearClientError(f"incomplete blocker {field}; refusing partial context")
            page = issue.get("comments", {})
            comments.extend(page.get("nodes", []))
            info = page.get("pageInfo", {})
            if not info.get("hasNextPage"):
                issue["comments"] = {"nodes": comments}
                return issue
            after = info.get("endCursor")
            if not after or after in cursors:
                raise LinearClientError("incomplete blocker comment pagination")
            cursors.add(after)
        raise LinearClientError("blocker context exceeds pagination limit")

    async def fetch_issue_status(self, identifier: str) -> dict[str, Any]:
        """Read the current status and attached PR links for one Linear issue."""
        issue_id = str(identifier or "").strip()
        if not issue_id:
            raise ValueError("Linear issue identifier is required")
        data = await self._post(LINEAR_ISSUE_STATUS_QUERY, {"id": issue_id})
        issue = data.get("issue")
        if not isinstance(issue, dict) or issue.get("identifier") != issue_id:
            raise LinearClientError("cannot verify Linear ticket identity")
        return issue

    async def _post(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self.endpoint,
                json={"query": query, "variables": dict(variables or {})},
                headers=self.headers,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LinearClientError(f"Linear request failed: {exc}") from exc
        if not isinstance(body, dict):
            raise LinearClientError("Linear returned a non-object response")
        errors = body.get("errors")
        if errors:
            messages = "; ".join(str(item.get("message", item)) for item in errors if isinstance(item, dict))
            raise LinearClientError(messages or "Linear GraphQL request failed")
        data = body.get("data")
        if not isinstance(data, dict):
            raise LinearClientError("Linear response did not contain data")
        return data

    async def fetch_issues(
        self,
        project_slug: str | Sequence[str],
        states: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Fetch the polled states for one or more configured Linear projects.

        ``project_slug`` accepts a single slug or a sequence, so adding a
        project is a config edit rather than a code change.  It stays one
        GraphQL request regardless of how many projects are included.
        """

        slugs = project_slugs(project_slug)
        if any(re.search(r"-[a-f0-9]{8,}$", value) for value in slugs):
            from harbor.project_identity import resolve
            slugs = resolve(slugs, await self.list_projects())
        if not states or not slugs:
            return []
        data = await self._post(
            LINEAR_QUERY,
            {"slugs": list(slugs), "states": list(states)},
        )
        issues = data.get("issues", {})
        nodes = issues.get("nodes", []) if isinstance(issues, dict) else []
        if not isinstance(nodes, list):
            raise LinearClientError("Linear issues response was malformed")
        return [node for node in nodes if isinstance(node, dict)]

    async def poll(
        self,
        project_slug: str | Sequence[str],
        states: Sequence[str],
    ) -> list[dict[str, Any]]:
        return await self.fetch_issues(project_slug, states)

    async def health(self) -> bool:
        await self._post(HEALTH_QUERY)
        return True

    async def create_comment(self, issue_id: str, body: str) -> dict[str, Any]:
        """Write one Linear comment and return the API payload.

        Idempotency belongs to the Harbor DB/layer above this transport.  The
        primitive itself intentionally performs exactly one request, making
        it straightforward to test and preventing hidden retries.
        """

        issue = str(issue_id).strip()
        text = str(body)
        if not issue:
            raise ValueError("Linear issue id is required")
        if not text.strip():
            raise ValueError("Linear comment body is required")
        data = await self._post(LINEAR_COMMENT_MUTATION, {"issueId": issue, "body": text})
        result = data.get("commentCreate")
        if not isinstance(result, dict):
            raise LinearClientError("Linear comment response was malformed")
        if result.get("success") is False:
            raise LinearClientError("Linear comment was not created")
        return result

    post_comment = create_comment
    add_comment = create_comment

    async def create_comment_once(
        self,
        issue_id: str,
        body: str,
        *,
        db: Any | None = None,
    ) -> dict[str, Any] | None:
        """Write a comment unless ``issue_id + body hash`` is already seen.

        The method accepts a duck-typed DB so the transport remains usable by
        tests and by callers that do not need local idempotency.
        """

        issue = str(issue_id)
        if db is not None:
            exists = getattr(db, "comment_receipt_exists", None)
            if callable(exists) and exists(issue, body=body):
                return None
        result = await self.create_comment(issue, body)
        if db is not None:
            record = getattr(db, "record_comment_receipt", None)
            if callable(record):
                record(issue, result.get("comment", {}).get("id"), body)
            else:
                # Import lazily to avoid a clients -> wake -> clients cycle.
                from harbor.wake import record_comment_receipt

                record_comment_receipt(db, issue, result.get("comment", {}).get("id"), body)
        return result

    async def list_projects(self) -> list[dict[str, Any]]:
        """List visible Linear projects, for doctor's scope validation."""

        data = await self._post(LINEAR_PROJECTS_QUERY)
        nodes = ((data.get("projects") or {}).get("nodes")) or []
        return [dict(node) for node in nodes if isinstance(node, Mapping)]

    async def _cache_team_states(self, issue_id: str) -> None:
        """Load names and terminal types for the issue's owning Linear team."""

        data = await self._post(LINEAR_STATES_QUERY, {"issueId": str(issue_id)})
        issue = data.get("issue") or {}
        team = issue.get("team") or {}
        nodes = ((team.get("states") or {}).get("nodes")) or []
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            label = str(node.get("name", "")).strip()
            identifier = node.get("id")
            if label and identifier:
                state_id = str(identifier)
                self._state_ids[label.casefold()] = state_id
                self._state_types[state_id] = str(node.get("type", "")).strip().casefold()

    async def resolve_state_id(self, issue_id: str, name: str) -> str | None:
        """Resolve a workflow state name to its id for the issue's team."""

        wanted = str(name).strip().casefold()
        if not wanted:
            return None
        cached = self._state_ids.get(wanted)
        if cached is not None:
            return cached
        await self._cache_team_states(issue_id)
        return self._state_ids.get(wanted)

    async def _resolve_state_type(self, issue_id: str, state_id: str) -> str | None:
        """Return the authoritative workflow type for a state id, if visible."""

        cached = self._state_types.get(state_id)
        if cached is not None:
            return cached
        await self._cache_team_states(issue_id)
        return self._state_types.get(state_id)

    async def update_issue_state(self, issue_id: str, state_id: str) -> dict[str, Any]:
        """Set a non-terminal Linear state by id/name.

        State names are accepted for deterministic tests and simple fakes; a
        production callers may pass a workflow state's id. Every target is
        checked against the owning team's authoritative workflow type, so a
        completed state with a custom label cannot bypass Harbor custody.
        """

        issue = str(issue_id).strip()
        state = str(state_id).strip()
        if not issue:
            raise ValueError("Linear issue id is required")
        if not state:
            raise ValueError("Linear state id is required")
        normalized_state = state.casefold()
        if normalized_state in {"done", "completed", "canceled", "cancelled"} or any(
            marker in normalized_state for marker in ("done", "completed", "canceled", "cancelled")
        ):
            raise ValueError("Harbor never sets a terminal Linear state")
        if not _UUID.match(state):
            # A name such as "Human Review".  Passing it straight through
            # produces Linear's opaque "Argument Validation Error", so resolve
            # it to the team's workflow state id first.
            resolved = await self.resolve_state_id(issue, state)
            if resolved is None:
                raise LinearClientError(
                    f"no Linear workflow state named {state!r} is available for issue {issue}"
                )
            state = resolved
        state_type = await self._resolve_state_type(issue, state)
        if state_type is None:
            raise LinearClientError(
                f"no Linear workflow state id {state!r} is available for issue {issue}"
            )
        if state_type in {"completed", "canceled", "cancelled"}:
            raise ValueError("Harbor never sets a terminal Linear state")
        data = await self._post(LINEAR_STATE_MUTATION, {"issueId": issue, "stateId": state})
        result = data.get("issueUpdate")
        if not isinstance(result, dict):
            raise LinearClientError("Linear issue update response was malformed")
        if result.get("success") is False:
            raise LinearClientError("Linear issue state was not updated")
        return result

    set_issue_state = update_issue_state
    set_state = update_issue_state

    async def update_progress(
        self,
        issue_id: str,
        *,
        state_id: str | None = None,
        state: str | None = None,
        body: str | None = None,
        db: Any | None = None,
    ) -> dict[str, Any]:
        """Apply an allowed state update and/or progress comment.

        This is a convenience for the unblock service, not a hidden workflow
        engine.  If both values are present the state is written first and the
        comment second; each operation is independently visible in tests.
        """

        chosen_state = state_id if state_id is not None else state
        state_result: dict[str, Any] | None = None
        comment_result: dict[str, Any] | None = None
        if chosen_state is not None:
            state_result = await self.update_issue_state(issue_id, chosen_state)
        if body is not None:
            comment_result = await self.create_comment_once(issue_id, body, db=db)
        return {"state": state_result, "comment": comment_result}

    update_issue_progress = update_progress
    write_progress = update_progress
    progress = update_progress


__all__ = ["HEALTH_QUERY", "LINEAR_QUERY", "LinearClient", "LinearClientError", "project_slugs", "resolve_token"]
