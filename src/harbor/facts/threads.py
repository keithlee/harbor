"""Turn GitHub review-thread nodes into stable, untrusted facts."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


# GitHub's connector puts badges in the URL (for example
# ``img.shields.io/badge/P2-yellow``).  Restricting this to a badge URL avoids
# treating arbitrary prose mentioning "P2" as a severity signal.
_SEVERITY_BADGE = re.compile(r"(?:^|/)badge/(P\d+)(?:[-_]|$)", re.IGNORECASE)
# A marker is intentionally required to be an explicit Harbor token.  Merely
# mentioning the product name in an ordinary review must remain external input.
_HARBOR_MARKER = re.compile(
    r"(?:<!--\s*harbor(?:\b|[-_:])|\[harbor\])",
    re.IGNORECASE,
)


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).casefold() in {"true", "yes", "1", "on"}


def _first_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        nodes = value.get("nodes")
        if isinstance(nodes, list):
            return next((node for node in nodes if isinstance(node, Mapping)), None)
        return value
    if isinstance(value, list):
        return next((node for node in value if isinstance(node, Mapping)), None)
    return None


def _author_info(thread: Mapping[str, Any], comment: Mapping[str, Any] | None) -> tuple[str | None, bool]:
    value: Any = thread.get("author")
    if not isinstance(value, Mapping):
        value = comment.get("author") if comment else None
    if not isinstance(value, Mapping):
        return None, False
    nested = value.get("user")
    if isinstance(nested, Mapping):
        value = nested
    login = value.get("login") or value.get("name")
    # GitHub has no ``isBot`` field on ``User``; a bot account is its own
    # ``Bot`` type, and an app bot's login ends in ``[bot]``.  ``isBot`` and
    # ``botActor`` are still read because REST payloads and test fixtures
    # supply them, but ``__typename``/login below is what the live API gives.
    bot_value = value.get("isBot")
    if bot_value is None:
        bot_value = value.get("botActor")
        if isinstance(bot_value, Mapping):
            login = login or bot_value.get("login")
            bot_value = True
    type_name = str(value.get("__typename", "")).casefold()
    is_bot = bool(bot_value) if bot_value is not None else type_name in {"bot", "userbot"}
    if not is_bot and isinstance(login, str):
        is_bot = login.endswith("[bot]") or login.casefold() in {
            "chatgpt-codex-connector",
            "github-actions",
            "github-actions[bot]",
        }
    return (str(login) if login is not None else None), is_bot


def parse_severity(body: str | None) -> str | None:
    """Extract a ``P1``/``P2`` badge from a review body, if present."""

    if not body:
        return None
    match = _SEVERITY_BADGE.search(body)
    if not match:
        return None
    return match.group(1).upper()


def is_harbor_marked(body: str | None) -> bool:
    """Return whether *body* carries Harbor's explicit output marker.

    GitHub has no mutable marker field that is reliable across issue comments,
    reviews, and review threads.  The body marker is therefore the common
    suppression boundary.  It is deliberately narrower than a case-insensitive
    search for ``harbor`` so user discussion about Harbor is never swallowed.
    """

    return bool(_HARBOR_MARKER.search(body or ""))


def _thread_comment(thread: Mapping[str, Any]) -> Mapping[str, Any] | None:
    comments = thread.get("comments")
    if isinstance(comments, Mapping):
        comments = comments.get("nodes", ())
    if isinstance(comments, Iterable) and not isinstance(comments, (str, bytes, Mapping)):
        return next((item for item in comments if isinstance(item, Mapping)), None)
    return None


def _connection_nodes(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("nodes", ())
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _author_fields(value: Any) -> tuple[str | None, bool]:
    if not isinstance(value, Mapping):
        return None, False
    nested = value.get("user")
    if isinstance(nested, Mapping):
        value = nested
    login = value.get("login") or value.get("name")
    bot_value = value.get("isBot")
    if bot_value is None:
        bot_value = value.get("botActor")
        if isinstance(bot_value, Mapping):
            login = login or bot_value.get("login")
            bot_value = True
    typename = str(value.get("__typename", "")).casefold()
    is_bot = bool(bot_value) if bot_value is not None else typename in {"bot", "userbot"}
    if not is_bot and isinstance(login, str):
        is_bot = login.endswith("[bot]") or login.casefold() in {
            "chatgpt-codex-connector",
            "github-actions",
            "github-actions[bot]",
        }
    return (str(login) if login is not None else None), is_bot


def parse_comment(
    comment: Mapping[str, Any],
    *,
    surface: str = "issue",
    pr_number: int | None = None,
    thread_id: str | None = None,
    review_id: str | None = None,
    path: str | None = None,
    line: int | None = None,
) -> dict[str, Any]:
    """Normalize one issue/review/inline comment into a stable fact.

    ``updatedAt`` is retained because a changed body is a new version of the
    same external comment and must be offered again.  The parser never decides
    whether a comment is actionable; it only labels its source and preserves
    enough provenance for a builder to answer safely.
    """

    body = comment.get("body")
    body = str(body) if body is not None else ""
    author, is_bot = _author_fields(comment.get("author"))
    comment_id = comment.get("id") or comment.get("databaseId")
    comment_path = comment.get("path", path)
    comment_line = comment.get("line", line)
    if comment_line is not None:
        try:
            comment_line = int(comment_line)
        except (TypeError, ValueError):
            comment_line = None
    return {
        "id": str(comment_id) if comment_id is not None else "",
        "surface": str(surface),
        "source": str(surface),
        "pr_number": pr_number,
        "thread_id": thread_id,
        "review_id": review_id,
        "body": body,
        "author": author,
        "is_bot": is_bot,
        "created_at": comment.get("createdAt", comment.get("created_at")),
        "updated_at": comment.get("updatedAt", comment.get("updated_at")),
        "url": comment.get("url", comment.get("htmlUrl")),
        "path": comment_path,
        "line": comment_line,
        "harbor_marked": is_harbor_marked(body),
    }


def parse_comment_surfaces(
    pull_request: Mapping[str, Any], *, pr_number: int | None = None
) -> list[dict[str, Any]]:
    """Collect all external-comment surfaces exposed by a PR GraphQL node.

    The four surfaces are issue comments, review bodies, inline review
    comments, and review-thread replies.  Inline comments are kept separate
    from their thread so every individual edited body has its own version key.
    """

    number = pr_number
    if number is None:
        raw_number = pull_request.get("number")
        try:
            number = int(raw_number) if raw_number is not None else None
        except (TypeError, ValueError):
            number = None
    result: list[dict[str, Any]] = []

    issue = pull_request.get("issue")
    issue_comments = issue.get("comments") if isinstance(issue, Mapping) else None
    if issue_comments is None:
        # A concise fake often puts the issue connection directly on the PR.
        issue_comments = pull_request.get("issueComments", pull_request.get("comments"))
    for comment in _connection_nodes(issue_comments):
        result.append(parse_comment(comment, surface="issue", pr_number=number))

    reviews = pull_request.get("reviews")
    for review in _connection_nodes(reviews):
        body = review.get("body")
        # Empty review bodies have no externally actionable version.  Retain
        # review metadata only when a body is present.
        if body is not None and str(body).strip():
            result.append(
                parse_comment(
                    review,
                    surface="review",
                    pr_number=number,
                    review_id=str(review.get("id")) if review.get("id") is not None else None,
                )
            )
        # Some API adapters expose inline comments below each review as well as
        # on reviewThreads.  The thread pass below deduplicates by comment id.
        for comment in _connection_nodes(review.get("comments")):
            result.append(
                parse_comment(
                    comment,
                    surface="inline",
                    pr_number=number,
                    review_id=str(review.get("id")) if review.get("id") is not None else None,
                )
            )

    thread_nodes = _connection_nodes(pull_request.get("reviewThreads"))
    for thread in thread_nodes:
        thread_id = str(thread.get("id")) if thread.get("id") is not None else None
        thread_path = thread.get("path")
        thread_line = thread.get("line")
        comments = _connection_nodes(thread.get("comments"))
        if not comments and (thread.get("body") is not None or thread.get("id") is not None):
            comments = [thread]
        for comment in comments:
            result.append(
                parse_comment(
                    comment,
                    surface="thread",
                    pr_number=number,
                    thread_id=thread_id,
                    path=thread_path,
                    line=thread_line,
                )
            )

    # A fake may expose the same inline comment through both a review and its
    # thread.  Preserve first provenance while ensuring it is offered once.
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for comment in result:
        identity = (comment["id"], comment["surface"])
        if identity[0] and identity in deduped:
            existing = deduped[identity]
            # Review and thread connections can expose the same inline
            # comment. Preserve whichever surface has richer location data.
            for key in ("thread_id", "review_id", "path", "line", "url", "created_at", "updated_at"):
                if existing.get(key) is None and comment.get(key) is not None:
                    existing[key] = comment[key]
            continue
        deduped[identity] = comment
    return sorted(
        deduped.values(),
        key=lambda item: (
            str(item.get("updated_at") or item.get("created_at") or ""),
            str(item.get("id") or ""),
            str(item.get("surface") or ""),
        ),
    )


def parse_thread(thread: Mapping[str, Any], *, seen_at_sha: str | None = None) -> dict[str, Any]:
    """Parse one GraphQL ``ReviewThread`` node.

    Review comments can be represented either directly on a test double or as
    the first comment in GitHub's ``comments`` connection.  The path is kept
    for outdated comments while their line is intentionally cleared: GitHub
    no longer guarantees that an old line exists at the current head.
    """

    comment = _thread_comment(thread)
    body = thread.get("body")
    if not isinstance(body, str) and comment:
        body = comment.get("body")
    body = str(body) if body is not None else ""
    author, is_bot = _author_info(thread, comment)
    outdated = _boolean(thread.get("isOutdated", thread.get("outdated", False)))
    line = thread.get("line")
    if outdated:
        line = None
    elif line is not None:
        try:
            line = int(line)
        except (TypeError, ValueError):
            line = None
    comment_id = comment.get("id") if comment else None
    comment_created_at = comment.get("createdAt", comment.get("created_at")) if comment else None
    comment_updated_at = comment.get("updatedAt", comment.get("updated_at")) if comment else None
    comment_facts = [
        parse_comment(
            item,
            surface="thread",
            thread_id=str(thread.get("id")) if thread.get("id") is not None else None,
            path=thread.get("path"),
            line=thread.get("line"),
        )
        for item in _connection_nodes(thread.get("comments"))
    ]
    return {
        "id": str(thread.get("id", "")),
        "comment_id": str(comment_id) if comment_id is not None else None,
        "author": author,
        "is_bot": is_bot,
        "body": body,
        "comments": comment_facts,
        "comment_created_at": comment_created_at,
        "comment_updated_at": comment_updated_at,
        "harbor_marked": is_harbor_marked(body),
        "severity": parse_severity(body),
        "resolved": _boolean(thread.get("isResolved", thread.get("resolved", False))),
        "outdated": outdated,
        "path": thread.get("path"),
        "line": line,
        "seen_at_sha": thread.get("seen_at_sha", seen_at_sha),
    }


def parse_threads(
    threads: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
    *,
    seen_at_sha: str | None = None,
) -> list[dict[str, Any]]:
    """Parse a GitHub connection or iterable in source order."""

    if threads is None:
        return []
    if isinstance(threads, Mapping):
        threads = threads.get("nodes", ())
    if not isinstance(threads, Iterable):
        return []
    return [
        parse_thread(thread, seen_at_sha=seen_at_sha)
        for thread in threads
        if isinstance(thread, Mapping)
    ]


thread_facts = parse_threads


parse_pr_comments = parse_comment_surfaces
parse_review_comments = parse_comment_surfaces


__all__ = [
    "is_harbor_marked",
    "parse_comment",
    "parse_comment_surfaces",
    "parse_pr_comments",
    "parse_review_comments",
    "parse_severity",
    "parse_thread",
    "parse_threads",
    "thread_facts",
]
