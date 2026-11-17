"""One-request GitHub poller for PRs attached to active Linear tickets."""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable, Mapping
from typing import Any

from harbor.clients.github import GitHubClient, build_pull_requests_query
from harbor.config.profile import PipelineProfile
from harbor.facts.ci import classify_ci
from harbor.facts.threads import parse_comment_surfaces, parse_threads
from harbor.pr_events import PREventProcessor


_PR_URL = re.compile(
    r"https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#]+)/pulls?/(?P<number>\d+)",
    re.IGNORECASE,
)


def pull_number(value: Any) -> int | None:
    """Extract a GitHub PR number from an attachment URL or number value."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        if value.strip().isdigit():
            number = int(value.strip())
            return number if number > 0 else None
        match = _PR_URL.search(value.strip())
        return int(match.group("number")) if match else None
    if isinstance(value, Mapping):
        for key in ("url", "href", "number"):
            number = pull_number(value.get(key))
            if number is not None:
                return number
    return None


def _ticket_attachments(ticket: Mapping[str, Any]) -> Iterable[Any]:
    for key in ("pr_urls", "attachments", "github_prs", "pull_requests"):
        value = ticket.get(key)
        if value is None:
            continue
        if isinstance(value, Mapping) and "nodes" in value:
            yield from value.get("nodes", ())
        elif isinstance(value, (str, Mapping)):
            yield value
        else:
            yield from value
    # A convenient shape for direct tests and fakes.
    for key in ("pr_url", "github_pr", "pull_request"):
        if ticket.get(key) is not None:
            yield ticket[key]


def attached_pull_requests(
    tickets: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    *,
    repository: str | None = None,
) -> tuple[tuple[int, str | None], ...]:
    """Return unique ``(number, ticket_identifier)`` pairs in stable order."""

    if isinstance(tickets, Mapping):
        # A single ticket dict is common in unit tests; otherwise map values
        # are treated as ticket records.
        if any(key in tickets for key in ("attachments", "pr_urls", "identifier", "id")):
            source: Iterable[tuple[Any, Mapping[str, Any]]] = ((None, tickets),)
        else:
            source = tickets.items()
    else:
        source = ((None, ticket) for ticket in tickets)
    expected_owner = expected_repo = None
    if repository:
        repo_parts = repository.rstrip("/").split("/")
        if len(repo_parts) >= 2:
            expected_owner, expected_repo = repo_parts[-2].casefold(), repo_parts[-1].removesuffix(".git").casefold()

    result: list[tuple[int, str | None]] = []
    seen: set[int] = set()
    for map_key, ticket in source:
        if not isinstance(ticket, Mapping):
            continue
        state = ticket.get("state")
        state_name = state.get("name") if isinstance(state, Mapping) else state
        if isinstance(state_name, str) and state_name.casefold() == "backlog":
            continue
        identifier = ticket.get("identifier", map_key)
        for attachment in _ticket_attachments(ticket):
            if isinstance(attachment, Mapping):
                source_type = str(attachment.get("sourceType", attachment.get("source_type", ""))).casefold()
                if source_type and source_type not in {"github", "github_pr", "pull_request"}:
                    continue
                url = attachment.get("url") or attachment.get("href")
            else:
                url = attachment
            match = _PR_URL.search(str(url)) if url is not None else None
            if match and expected_owner and expected_repo:
                if (match.group("owner").casefold(), match.group("repo").removesuffix(".git").casefold()) != (
                    expected_owner,
                    expected_repo,
                ):
                    continue
            number = pull_number(url)
            if number is None or number in seen:
                continue
            seen.add(number)
            result.append((number, str(identifier) if identifier is not None else None))
    return tuple(result)


def _connection(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("nodes", ())
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _head_sha(pr: Mapping[str, Any]) -> str | None:
    for key in ("headRefOid", "head_sha", "headSha"):
        value = pr.get(key)
        if value:
            return str(value)
    head = pr.get("head")
    if isinstance(head, Mapping):
        value = head.get("oid") or head.get("sha")
        if value:
            return str(value)
    return None


def parse_pull_request(
    pull_request: Mapping[str, Any],
    *,
    required_checks: Iterable[str] = (),
    ticket: str | None = None,
) -> dict[str, Any]:
    """Convert one GraphQL PR node to the stable PRFacts shape."""

    number = pull_number(pull_request.get("number"))
    if number is None:
        raise ValueError("GitHub pull request node has no positive number")
    head_sha = _head_sha(pull_request)
    threads = parse_threads(pull_request.get("reviewThreads"), seen_at_sha=None)
    comments = parse_comment_surfaces(pull_request, pr_number=number)
    # Only GitHub's canonical boolean is merge authority.  A state label or
    # merge-looking SHA must never synthesize an irreversible merge receipt.
    merged = pull_request.get("merged") is True
    facts: dict[str, Any] = {
        "number": number,
        "body": pull_request.get("body"),
        "head_sha": head_sha,
        "mergeable": pull_request.get("mergeable"),
        "state": pull_request.get("state"),
        "merged": merged,
        "ci": classify_ci(pull_request.get("statusCheckRollup"), required_checks),
        "threads": threads,
        "comments": comments,
        # ``comment_versions`` is intentionally a descriptive alias.  The
        # event layer computes the version key from id/body/timestamps so a
        # changed body is observable without trusting an API-only updatedAt.
        "comment_versions": comments,
        "issue_comments": [item for item in comments if item.get("surface") == "issue"],
        "review_bodies": [item for item in comments if item.get("surface") == "review"],
        "inline_comments": [
            item for item in comments if item.get("surface") in {"inline", "thread"}
        ],
    }
    for key in ("url", "title", "state", "createdAt", "created_at"):
        if key in pull_request:
            target = "created_at" if key == "createdAt" else key
            facts[target] = pull_request[key]
    if ticket is not None:
        facts["ticket"] = ticket
    return facts


class GitHubPoller:
    """Poll only PRs named by the supplied active tickets."""

    def __init__(
        self,
        client: Any,
        profile: PipelineProfile | Any | None = None,
        *,
        owner: str | None = None,
        repo: str | None = None,
        required_checks: Iterable[str] | None = None,
        db: Any | None = None,
        event_processor: Any | None = None,
        project: str | None = None,
    ) -> None:
        self.client = client
        self.profile = profile
        self.owner = owner or getattr(profile, "repository_owner", None) or getattr(profile, "owner", None) or ""
        self.repo = repo or getattr(profile, "repository_name", None) or getattr(profile, "repo", None) or ""
        if (not self.owner or "/" in self.repo) and profile is not None:
            repository = getattr(profile, "repository", None) or getattr(profile, "github_repository", None)
            if isinstance(repository, str) and "/" in repository:
                parts = repository.rstrip("/").split("/")
                self.owner = self.owner or parts[-2]
                self.repo = parts[-1]
        if "/" in self.repo:
            parts = self.repo.rstrip("/").split("/")
            self.owner = self.owner or parts[-2]
            self.repo = parts[-1]
        self.required_checks = tuple(
            required_checks
            if required_checks is not None
            else getattr(profile, "required_checks", ())
        )
        self.event_processor = event_processor
        if self.event_processor is None:
            # Keep event identity stable across polls in a process even when
            # callers do not supply a database.  A DB-backed processor can be
            # supplied explicitly (or through ``db``) for restart durability.
            self.event_processor = PREventProcessor(
                db,
                project=project or getattr(profile, "name", "") or f"{self.owner}/{self.repo}",
                profile=profile,
            )

    async def poll(
        self,
        tickets: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
        *,
        owner: str | None = None,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        # A direct sequence of PR URLs/numbers is accepted as a convenience,
        # while the normal path is a Linear ticket mapping with attachments.
        if not isinstance(tickets, Mapping):
            ticket_items = list(tickets)
            if ticket_items and all(not isinstance(item, Mapping) for item in ticket_items):
                tickets = [{"pr_urls": ticket_items}]
            else:
                tickets = ticket_items
        attached = attached_pull_requests(
            tickets,
            repository=(
                f"{owner or self.owner}/{repo or self.repo}"
                if (owner or self.owner) and (repo or self.repo)
                else None
            ),
        )
        attached = tuple(sorted(attached, key=lambda item: item[0]))
        if not attached:
            return []
        owner = owner or self.owner
        repo = repo or self.repo
        if not owner or not repo:
            raise ValueError("GitHub owner and repository are required")
        numbers = tuple(number for number, _ in attached)
        response = await self._fetch(owner, repo, numbers)
        repository = response.get("repository") if isinstance(response, Mapping) else None
        if repository is None and isinstance(response, Mapping):
            data = response.get("data")
            repository = data.get("repository") if isinstance(data, Mapping) else None
        if not isinstance(repository, Mapping):
            # A test double may return ``{number: node}`` directly.
            repository = response if isinstance(response, Mapping) else {}
        by_number: dict[int, Mapping[str, Any]] = {}
        for number, alias in ((number, f"pr_{number}") for number, _ in attached):
            node = repository.get(alias)
            if isinstance(node, Mapping):
                by_number[number] = node
        # Also accept raw nodes keyed by their number, which is handy for a
        # fake client while keeping the public result deterministic.
        for number in numbers:
            node = repository.get(str(number)) or repository.get(number)
            if isinstance(node, Mapping):
                by_number[number] = node
        result: list[dict[str, Any]] = []
        for number, ticket in attached:
            if number not in by_number:
                continue
            node = dict(by_number[number])
            # GitHub test doubles and some reduced query adapters omit `url`.
            # The poll target is already constrained by the configured
            # owner/repository and Linear attachment, so reconstruct only that
            # exact canonical URL rather than falling back to PR-number identity.
            node.setdefault("url", f"https://github.com/{owner}/{repo}/pull/{number}")
            facts = parse_pull_request(
                node, required_checks=self.required_checks, ticket=ticket
            )
            processor = self.event_processor
            if processor is not None:
                observe = getattr(processor, "observe", None)
                if callable(observe):
                    events = observe(
                        {"identifier": ticket, "pr_urls": [facts["url"]]}
                        if ticket is not None
                        else {},
                        facts,
                    )
                    if inspect.isawaitable(events):
                        events = await events
                    facts["events"] = list(events or ())
            result.append(facts)
        return result

    async def poll_events(
        self,
        tickets: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
        *,
        owner: str | None = None,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        """Poll PR facts and flatten newly observed PR events deterministically."""

        facts = await self.poll(tickets, owner=owner, repo=repo)
        events: list[dict[str, Any]] = []
        for item in facts:
            values = item.get("events", ())
            if isinstance(values, Mapping):
                values = values.values()
            events.extend(value for value in values if isinstance(value, Mapping))
        return events

    async def _fetch(self, owner: str, repo: str, numbers: tuple[int, ...]) -> Mapping[str, Any]:
        method = getattr(self.client, "pull_requests", None)
        if method is None:
            method = getattr(self.client, "fetch_pull_requests", None)
        if method is not None:
            result = method(owner, repo, numbers)
            return await result if inspect.isawaitable(result) else result
        # This fallback supports a deliberately minimal fake exposing only a
        # query(document, variables) public method.
        result = self.client.query(
            build_pull_requests_query(owner, repo, numbers),
            {"owner": owner, "repo": repo},
        )
        return await result if inspect.isawaitable(result) else result


async def poll_github(
    client: Any,
    tickets: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    *,
    profile: PipelineProfile | Any | None = None,
    owner: str | None = None,
    repo: str | None = None,
    required_checks: Iterable[str] | None = None,
    db: Any | None = None,
    event_processor: Any | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    return await GitHubPoller(
        client,
        profile,
        owner=owner,
        repo=repo,
        required_checks=required_checks,
        db=db,
        event_processor=event_processor,
        project=project,
    ).poll(tickets)


__all__ = [
    "GitHubPoller",
    "attached_pull_requests",
    "parse_pull_request",
    "poll_github",
    "pull_number",
]
