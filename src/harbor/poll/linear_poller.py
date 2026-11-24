"""Linear ticket polling and normalization."""

from __future__ import annotations

import inspect
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB

from .snapshot import SnapshotTracker, canonical_json, snapshot_hash


def _nodes(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("nodes", [])
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _blocked_by(node: Mapping[str, Any]) -> list[str]:
    blocked: list[str] = []
    for relation in _nodes(node.get("inverseRelations")):
        if relation.get("type") != "blocks":
            continue
        related = relation.get("issue")
        if not isinstance(related, Mapping):
            continue
        state = related.get("state")
        state_type = state.get("type") if isinstance(state, Mapping) else None
        if state_type not in {"completed", "canceled"}:
            identifier = related.get("identifier")
            if identifier:
                blocked.append(str(identifier))
    return blocked


def normalize_issue(node: Mapping[str, Any], *, hold_all: bool = True) -> dict[str, Any] | None:
    """Convert one GraphQL issue node into Harbor's stable ticket shape."""

    identifier = node.get("identifier")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    state = node.get("state")
    state_name = state.get("name") if isinstance(state, Mapping) else state
    if not state_name:
        return None
    attachments = _nodes(node.get("attachments"))
    pr_urls = [
        str(item["url"])
        for item in attachments
        if item.get("url") and str(item.get("sourceType", "")).lower() == "github"
    ]
    comments = _nodes(node.get("comments"))
    comment_ids = [str(item["id"]) for item in comments if item.get("id")]
    linear_comments = [
        {
            "id": str(item["id"]),
            "body": str(item.get("body", "")),
            "created_at": item.get("createdAt"),
            "updated_at": item.get("updatedAt"),
            "author": dict(item["user"]) if isinstance(item.get("user"), Mapping) else None,
        }
        for item in comments
        if item.get("id")
    ]
    return {
        "identifier": identifier,
        "id": node.get("id"),
        "title": str(node.get("title", "")),
        "description": str(node.get("description") or ""),
        "url": node.get("url"),
        "state": str(state_name),
        "state_type": state.get("type") if isinstance(state, Mapping) else None,
        "branch_name": node.get("branchName"),
        "updated_at": node.get("updatedAt"),
        "pr_urls": pr_urls,
        "comment_ids": comment_ids,
        "linear_comments": linear_comments,
        "blocked_by": _blocked_by(node),
        "owned": False,
        "hold": hold_all,
    }


@dataclass(frozen=True)
class PollResult:
    tickets: tuple[dict[str, Any], ...]
    snapshot_hash: str
    changed: bool
    raw: tuple[dict[str, Any], ...]

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return self.tickets if self.changed else ()


class LinearPoller:
    def __init__(
        self,
        client: Any,
        config: ProjectConfig,
        *,
        db: HarborDB | None = None,
        project_name: str | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.db = db
        self.project_name = project_name or config.name
        self.tracker = SnapshotTracker(db.snapshot_hash(self.project_name) if db else None)
        self.last_result: PollResult | None = None

    def _normalize(
        self, raw: Iterable[dict[str, Any]],
    ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
        allowed = {state for state in self.config.poll_states if state.casefold() != "backlog"}
        normalized: list[dict[str, Any]] = []
        accepted: list[dict[str, Any]] = []
        prior: dict[str, Mapping[str, Any]] = {}
        if self.db is not None:
            # ``owned``/``hold`` are local custody flags.  Expose the durable
            # values in the poll result too, so consumers that do not consult
            # the database cannot mistake Linear's observation defaults for a
            # release of an already-claimed ticket.
            try:
                prior = {
                    str(item.get("identifier")): item
                    for item in self.db.tickets(self.project_name)
                    if item.get("identifier")
                }
            except (AttributeError, sqlite3.Error):
                prior = {}
        for node in raw:
            state = node.get("state")
            state_name = state.get("name") if isinstance(state, Mapping) else state
            # Keep this guard even though the GraphQL filter is state-scoped:
            # it makes Backlog exclusion fail-safe with a faulty/mocked server.
            if str(state_name) not in allowed:
                continue
            ticket = normalize_issue(node, hold_all=self.config.hold_all)
            if ticket is None:
                continue
            previous = prior.get(ticket["identifier"])
            if previous is not None:
                ticket["owned"] = bool(previous.get("owned", ticket.get("owned", False)))
                ticket["hold"] = bool(previous.get("hold", ticket.get("hold", self.config.hold_all)))
            normalized.append(ticket)
            accepted.append(node)
        normalized.sort(key=lambda item: item["identifier"])
        return tuple(normalized), tuple(accepted)

    async def poll(self) -> PollResult:
        requested_states = tuple(
            state for state in self.config.poll_states if state.casefold() != "backlog"
        )
        fetch = getattr(self.client, "fetch_issues", None) or getattr(self.client, "poll", None)
        if fetch is None:
            raise TypeError("Linear poller client must expose fetch_issues or poll")
        # Every configured project in one request; a single-project config
        # resolves to a one-element scope and behaves exactly as before.
        scope = getattr(self.config, "linear_project_scope", None) or (
            self.config.linear_project_slug,
        )
        raw_nodes = fetch(tuple(scope), requested_states)
        if inspect.isawaitable(raw_nodes):
            raw_nodes = await raw_nodes
        if isinstance(raw_nodes, Mapping):
            # Keep the poller friendly to a small fake client that returns the
            # decoded GraphQL envelope instead of the client's unwrapped list.
            data = raw_nodes.get("data", raw_nodes)
            issues = data.get("issues", {}) if isinstance(data, Mapping) else {}
            raw_nodes = issues.get("nodes", []) if isinstance(issues, Mapping) else []
        raw = tuple(dict(node) for node in raw_nodes if isinstance(node, Mapping))
        # Hash only nodes that produced a ticket. Out-of-scope states and
        # malformed identifiers cannot make an idle cycle look changed.
        tickets, filtered_raw = self._normalize(raw)
        payload: dict[str, Any] = {"tickets": list(filtered_raw)}
        digest = snapshot_hash(payload)
        changed = digest != self.tracker.previous_hash
        if changed and self.db:
            self.db.record_observation(self.project_name, digest, payload, tickets)
        self.tracker.previous_hash = digest
        result = PollResult(tickets=tickets, snapshot_hash=digest, changed=changed, raw=raw)
        self.last_result = result
        return result

    async def poll_once(self) -> PollResult:
        return await self.poll()


__all__ = ["LinearPoller", "PollResult", "normalize_issue"]
