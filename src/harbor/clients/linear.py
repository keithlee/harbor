"""Read-only Linear GraphQL client for Phase 1."""

from __future__ import annotations

import os
import re
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

import httpx

from harbor.url_credentials import redact_http_error

DEFAULT_LINEAR_API_URL = "https://api.linear.app/graphql"


LINEAR_QUERY = """
query HarborIssues($slugs: [String!]!, $states: [String!]!, $after: String) {
  issues(filter: {project: {slugId: {in: $slugs}}, state: {name: {in: $states}}},
         first: 50, after: $after, orderBy: createdAt) {
    pageInfo { hasNextPage endCursor }
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

# Every Linear connection Harbor reads (the issue poll, the project list used
# to resolve the configured scope, and a team's workflow states) follows
# Linear's cursor pagination, so no ticket, project or state is ever invisible
# because it sorted past the first page.  The bound (40 pages of 50, i.e. 2000
# nodes) only stops a runaway or misbehaving server; reaching it is an error,
# never a silently truncated result.
LINEAR_POLL_MAX_PAGES = 40

HEALTH_QUERY = "query HarborHealth { viewer { id } }"

LINEAR_PROJECTS_QUERY = """
query HarborProjects($after: String) {
  projects(first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { id name slugId }
  }
}
"""

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
query HarborStates($issueId: String!, $after: String) {
  issue(id: $issueId) {
    id
    team {
      id name
      states(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id name type }
      }
    }
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


def _team_states_connection(data: dict[str, Any]) -> Any:
    """Pick ``issue.team.states`` out of a states response.

    A null or missing link anywhere on the path returns ``None`` so the
    pager reports the response as malformed, the same as the poll does for a
    null ``issues``, instead of reading it as an empty last page.
    """

    node: Any = data
    for key in ("issue", "team", "states"):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


class LinearClientError(RuntimeError):
    """A network, HTTP, or GraphQL failure."""


def _is_header_safe(token: str) -> bool:
    """True when ``token`` is visible ASCII, the only thing the header may carry.

    Anything else (a trailing space or ``\\r`` from a CRLF ``.env``, a newline,
    a non-ASCII character) makes httpx/h11 raise an error that echoes the whole
    header value: ``LocalProtocolError("Illegal header value b'...'")`` or a
    ``UnicodeEncodeError`` whose ``object`` is the key.
    """

    return all("!" <= char <= "~" for char in token)


_UNSENDABLE_KEY = (
    "the configured Linear API key contains characters that cannot be sent in an HTTP header "
    "(often trailing whitespace or a carriage return from a CRLF .env file, or a non-ASCII "
    "character); re-export LINEAR_API_KEY or HARBOR_LINEAR_API_KEY without them"
)


def _redact_authorization(exc: BaseException) -> None:
    """Drop the key from the request an httpx error carries on the chain.

    ``httpx`` keeps the real header value in ``exc.request.headers``; a logger
    or traceback formatter walking ``__cause__`` could otherwise print it.
    """

    try:
        request = getattr(exc, "request", None)
    except RuntimeError:  # a RequestError raised before a request was attached
        return
    if request is not None and "Authorization" in request.headers:
        request.headers["Authorization"] = "[redacted]"


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
        # Workflow states belong to a Linear team, and different teams reuse
        # the same names ("Blocked", "Human Review") with distinct ids.  Both
        # maps are therefore scoped by team, and an issue is only looked up in
        # the team its own state query reported.
        self._issue_teams: dict[str, str] = {}
        self._state_ids: dict[str, dict[str, str]] = {}
        self._state_types: dict[str, dict[str, str]] = {}
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
        def connection(issue: dict[str, Any], field: str) -> tuple[list[Any], dict[str, Any]]:
            # Completeness must be proven, not assumed: a connection or
            # pageInfo that Linear omitted cannot show the first page was the
            # whole set, so a blocking relation, PR link or risk label past it
            # would be invisible.  Fail closed instead.
            page = issue.get(field)
            nodes = page.get("nodes") if isinstance(page, dict) else None
            info = page.get("pageInfo") if isinstance(page, dict) else None
            if (not isinstance(nodes, list) or not isinstance(info, dict)
                    or not isinstance(info.get("hasNextPage"), bool)):
                raise LinearClientError(f"malformed blocker {field}; refusing unprovable context")
            return nodes, info

        comments: list[dict[str, Any]] = []
        after = None
        cursors: set[str] = set()
        for _ in range(100):
            data = await self._post(query, {"id": identifier, "after": after})
            issue = data.get("issue")
            if not isinstance(issue, dict) or issue.get("identifier") != identifier:
                raise LinearClientError("cannot verify blocked ticket identity")
            for field in ("attachments", "inverseRelations", "labels"):
                if connection(issue, field)[1]["hasNextPage"]:
                    raise LinearClientError(f"incomplete blocker {field}; refusing partial context")
            nodes, info = connection(issue, "comments")
            comments.extend(nodes)
            if not info["hasNextPage"]:
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
        if self.token and not _is_header_safe(self.token):
            # Refuse before any request is built, outside any ``except``, so
            # the error has no cause or context that could carry the key.
            raise LinearClientError(_UNSENDABLE_KEY)
        failure: LinearClientError | None = None
        try:
            response = await self._client.post(
                self.endpoint,
                json={"query": query, "variables": dict(variables or {})},
                headers=self.headers,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.InvalidURL:
            # Not an HTTPError, and its message repeats part of the URL (for
            # example "Invalid port: '<password>＠host'"), so say nothing of it.
            failure = LinearClientError("Linear request failed: the Linear API URL is not a valid URL")
        except (httpx.HTTPError, ValueError) as exc:
            # The chained request must not carry the key, and httpx repeats
            # the request URL in its messages, so userinfo must not either.
            _redact_authorization(exc)
            message, carries_userinfo = redact_http_error(str(exc), self.endpoint)
            failure = LinearClientError(f"Linear request failed: {message}")
            if not carries_userinfo:
                raise failure from exc
        if failure is not None:
            # Raised outside the ``except`` block, so neither ``__cause__`` nor
            # ``__context__`` holds the httpx error, whose message and
            # ``request.url`` carry the endpoint's userinfo.
            raise failure
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
        GraphQL query per page regardless of how many projects are included;
        every page is followed (see ``LINEAR_POLL_MAX_PAGES``).
        """

        slugs = project_slugs(project_slug)
        if any(re.search(r"-[a-f0-9]{8,}$", value) for value in slugs):
            from harbor.project_identity import resolve
            slugs = resolve(slugs, await self.list_projects())
        if not states or not slugs:
            return []
        # ``after`` is omitted on the first page (GraphQL treats it as null).
        variables: dict[str, Any] = {"slugs": list(slugs), "states": list(states)}
        collected: dict[str, dict[str, Any]] = {}
        unkeyed: list[dict[str, Any]] = []
        async for nodes in self._paged_nodes(
            LINEAR_QUERY,
            variables,
            lambda data: data.get("issues"),
            what="issue",
            partial="poll",
            response="issues",
        ):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                key = node.get("id")
                if key is None:
                    unkeyed.append(node)
                else:
                    # A page boundary can repeat an issue; keep one copy.
                    collected[str(key)] = node
        # Page order depends on Linear's cursor boundaries; the snapshot hash
        # must not, so return a canonical order.
        result = [*collected.values(), *unkeyed]
        result.sort(key=lambda node: (str(node.get("identifier", "")), str(node.get("id", ""))))
        return result

    async def _paged_nodes(
        self,
        query: str,
        variables: Mapping[str, Any],
        connection: Callable[[dict[str, Any]], Any],
        *,
        what: str,
        partial: str,
        response: str | None = None,
    ) -> AsyncIterator[list[Any]]:
        """Yield each page's ``nodes`` of one Linear connection, following cursors.

        ``connection`` picks the connection object out of a response's data.
        The query must take ``$after`` (omitted on the first page; GraphQL
        treats it as null) and select ``pageInfo { hasNextPage endCursor }``.
        A malformed page (a missing/null connection or ``nodes`` list, or a ``pageInfo`` or
        ``hasNextPage`` that is absent or not the expected type), a
        missing/repeated cursor, or more than ``LINEAR_POLL_MAX_PAGES`` pages
        raises: callers never see a partial list presented as complete.
        ``response`` names the connection in the malformed-response errors
        (defaults to ``what``).
        """

        label = response or what
        variables = dict(variables)
        variables.pop("after", None)
        cursors: set[str] = set()
        for _ in range(LINEAR_POLL_MAX_PAGES):
            data = await self._post(query, variables)
            page = connection(data)
            if not isinstance(page, dict):
                raise LinearClientError(f"Linear {label} response was malformed")
            # The query selects nodes, so an absent nodes list is malformed,
            # not an empty page: reading it as empty would drop this page.
            nodes = page.get("nodes")
            if not isinstance(nodes, list):
                raise LinearClientError(f"Linear {label} response was malformed")
            # The query selects pageInfo, so an absent or non-boolean
            # hasNextPage is a malformed page, not a last page: treating it
            # as the end would silently truncate the list to this page.
            info = page.get("pageInfo")
            if not isinstance(info, dict) or not isinstance(info.get("hasNextPage"), bool):
                raise LinearClientError(f"Linear {label} pageInfo was malformed")
            yield nodes
            if not info["hasNextPage"]:
                return
            after = info.get("endCursor")
            if not after or not isinstance(after, str) or after in cursors:
                raise LinearClientError(f"incomplete Linear {what} pagination; refusing a partial {partial}")
            cursors.add(after)
            variables["after"] = after
        raise LinearClientError(
            f"Linear {partial} exceeds pagination limit of {LINEAR_POLL_MAX_PAGES} pages; refusing a partial {partial}"
        )

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
        """List every visible Linear project.

        Used to resolve a configured URL-form slug, by doctor's scope
        validation, and by the ticket-scope check, so it must be complete:
        a project past the first page is still a configured project.
        """

        projects: list[dict[str, Any]] = []
        async for nodes in self._paged_nodes(
            LINEAR_PROJECTS_QUERY,
            {},
            lambda data: data.get("projects"),
            what="project",
            partial="project list",
        ):
            projects.extend(dict(node) for node in nodes if isinstance(node, Mapping))
        return projects

    async def _cache_team_states(self, issue_id: str) -> str:
        """Load names and terminal types for the issue's owning Linear team.

        Returns the cache scope (the team id) the issue's states live under.
        """

        # Read every page before touching the cache, so a failed lookup never
        # leaves a partial view of the team's workflow behind.
        issue = str(issue_id)
        teams: set[str] = set()

        def team_states(data: dict[str, Any]) -> Any:
            # A null or missing issue/team/states yields None, which the
            # pager reports as malformed before anything is cached.
            states = _team_states_connection(data)
            if isinstance(states, dict):
                team_id = data["issue"]["team"].get("id")
                if team_id:
                    teams.add(str(team_id))
            return states

        nodes: list[Any] = []
        async for page in self._paged_nodes(
            LINEAR_STATES_QUERY,
            {"issueId": issue},
            team_states,
            what="workflow state",
            partial="workflow state lookup",
        ):
            nodes.extend(page)
        if len(teams) > 1:
            raise LinearClientError(f"Linear workflow state pages for issue {issue} named more than one team")
        # Without a team id the states can only be trusted for this one issue,
        # so they are never shared with another issue's lookup.
        scope = next(iter(teams)) if teams else f"issue:{issue}"
        state_ids: dict[str, str] = {}
        state_types: dict[str, str] = {}
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            label = str(node.get("name", "")).strip()
            identifier = node.get("id")
            if label and identifier:
                state_id = str(identifier)
                state_ids[label.casefold()] = state_id
                state_types[state_id] = str(node.get("type", "")).strip().casefold()
        self._state_ids[scope] = state_ids
        self._state_types[scope] = state_types
        self._issue_teams[issue] = scope
        return scope

    async def _team_scope(self, issue_id: str) -> tuple[str, bool]:
        """Return the issue's cache scope and whether it was loaded just now."""

        scope = self._issue_teams.get(str(issue_id))
        if scope is not None:
            return scope, False
        return await self._cache_team_states(issue_id), True

    async def resolve_state_id(self, issue_id: str, name: str) -> str | None:
        """Resolve a workflow state name to its id for the issue's team."""

        wanted = str(name).strip().casefold()
        if not wanted:
            return None
        scope, fresh = await self._team_scope(issue_id)
        cached = self._state_ids.get(scope, {}).get(wanted)
        if cached is not None or fresh:
            return cached
        # A state added to the team since the cached lookup: refresh once.
        scope = await self._cache_team_states(issue_id)
        return self._state_ids.get(scope, {}).get(wanted)

    async def _resolve_state_type(self, issue_id: str, state_id: str) -> str | None:
        """Return the authoritative workflow type for a state id, if visible."""

        scope, fresh = await self._team_scope(issue_id)
        cached = self._state_types.get(scope, {}).get(state_id)
        if cached is not None or fresh:
            return cached
        scope = await self._cache_team_states(issue_id)
        return self._state_types.get(scope, {}).get(state_id)

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


__all__ = ["HEALTH_QUERY", "LINEAR_POLL_MAX_PAGES", "LINEAR_QUERY", "LinearClient", "LinearClientError", "project_slugs", "resolve_token"]
