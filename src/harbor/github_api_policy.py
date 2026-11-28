"""Which ``harbor.github_rest api`` calls a builder may make: an allowlist, not a deny-list.

Reads (GET, HEAD) go through. A write is allowed only on the REST endpoints
Harbor's delivery workflow uses (comments, replies, reviews, labels, PR create
and edit) under ``/repos/<owner>/<repo>/``, and a GraphQL mutation only when
every top-level field is on the mutation allowlist. Everything else is refused:
merges (only ``pr-merge`` merges, with its checks), refs, contents, branch
protection, ``/repositories/<id>/...`` and anything Harbor cannot parse.

This is a guard on Harbor's own helper, not a security boundary: a builder
holding the token can call GitHub without it (see the device proof docs).
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

READ_METHODS = {"GET", "HEAD"}
_NUMBER = r"[1-9][0-9]{0,9}"
# (method, path under /repos/<owner>/<repo>) the delivery workflow writes.
REST_WRITES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern)) for method, pattern in (
        ("POST", r"pulls"),                                           # pr-create
        ("PATCH", rf"pulls/{_NUMBER}"),                               # title and body only (_check_pr_edit)
        ("POST", rf"issues/{_NUMBER}/comments"),                      # PR conversation comments, verdicts
        ("PATCH", rf"issues/comments/{_NUMBER}"),                     # edit own comment
        ("POST", rf"pulls/{_NUMBER}/comments"),                       # inline review comment
        ("POST", rf"pulls/{_NUMBER}/comments/{_NUMBER}/replies"),     # reply to a review comment
        ("PATCH", rf"pulls/comments/{_NUMBER}"),                      # edit own review comment
        ("POST", rf"pulls/{_NUMBER}/reviews"),                        # submit a review with comments
        ("POST", rf"issues/{_NUMBER}/labels"),                        # add labels
        ("DELETE", rf"issues/{_NUMBER}/labels/[A-Za-z0-9._%-]+"),     # remove a label
    )
)
GRAPHQL_MUTATIONS = {
    "addPullRequestReviewThreadReply",  # reply in a review thread
    "resolveReviewThread",
    "unresolveReviewThread",
    "addComment",
}
_OWNER_REPO = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}"


class ApiRefused(ValueError):
    """The call is not on Harbor's allowlist."""


def check_api_call(method: str, path: str, body: str | None, repository: str | None = None) -> None:
    """Raise ApiRefused unless ``method path`` (with ``body``) is allowed."""

    verb = method.upper()
    parts = urlsplit(path)
    if verb in READ_METHODS:
        if body:
            raise ApiRefused(f"{verb} {path}: a read may not carry a request body")
        return
    if parts.query or parts.fragment or "%2" in path.lower() or "\\" in path:
        raise ApiRefused(f"{verb} {path}: writes may not carry a query string, fragment or encoded separator")
    clean = parts.path
    if clean in {"/graphql", "/api/graphql"} and verb == "POST":
        check_graphql(body)
        return
    match = re.fullmatch(rf"/repos/({_OWNER_REPO})/(.+)", clean)
    if not match or "//" in clean or any(seg in {".", ".."} for seg in clean.split("/")):
        raise ApiRefused(f"{verb} {path}: only /repos/<owner>/<repo>/... endpoints on the allowlist may be written")
    if repository and match.group(1).casefold() != repository.casefold():
        raise ApiRefused(f"{verb} {path}: writes are limited to the configured repository {repository}")
    rest = match.group(2)
    if verb == "POST" and re.fullmatch(rf"pulls/{_NUMBER}/reviews", rest):
        _check_review(body)
    if verb == "PATCH" and re.fullmatch(rf"pulls/{_NUMBER}", rest):
        _check_pr_edit(body)
    if not any(verb == allowed and pattern.fullmatch(rest) for allowed, pattern in REST_WRITES):
        raise ApiRefused(
            f"{verb} {path} is not on Harbor's write allowlist (comments, replies, reviews, labels, PR create/edit). "
            "Merge with `pr-merge N --sha SHA`; refs, contents, branch protection and merges are refused"
        )


PR_EDIT_FIELDS = {"title", "body"}


def _check_pr_edit(body: str | None) -> None:
    """``PATCH pulls/N`` may change the title and body only: never ``base``, ``state`` or anything else."""

    try:
        payload: Any = json.loads(body or "")
    except ValueError as exc:
        raise ApiRefused("PATCH pulls/N: the body must be JSON") from exc
    if not isinstance(payload, dict) or not payload or set(payload) - PR_EDIT_FIELDS:
        extra = sorted(set(payload) - PR_EDIT_FIELDS) if isinstance(payload, dict) else ["(not an object)"]
        raise ApiRefused(f"PATCH pulls/N may set only {', '.join(sorted(PR_EDIT_FIELDS))}; refused: "
                         f"{', '.join(extra) or '(empty)'}")


REVIEW_EVENTS = {"COMMENT", "REQUEST_CHANGES"}


def _check_review(body: str | None) -> None:
    """A builder may comment on or request changes to a PR, never approve it."""

    try:
        payload: Any = json.loads(body or "")
    except ValueError as exc:
        raise ApiRefused("POST pulls/N/reviews: the body must be JSON") from exc
    event = payload.get("event") if isinstance(payload, dict) else None
    if event not in REVIEW_EVENTS:
        raise ApiRefused(f"POST pulls/N/reviews: event must be one of {', '.join(sorted(REVIEW_EVENTS))}, "
                         f"not {event!r}; a builder never approves a PR")


# --- GraphQL -------------------------------------------------------------------------------

def _tokens(document: str) -> list[str]:
    """GraphQL lexical tokens without strings, comments, commas or whitespace; raise on bad input."""

    tokens, i, n = [], 0, len(document)
    while i < n:
        c = document[i]
        if c in " \t\r\n,﻿":
            i += 1
        elif c == "#":
            while i < n and document[i] not in "\r\n":
                i += 1
        elif document.startswith('"""', i):
            end = i + 3
            while True:
                end = document.find('"""', end)
                if end == -1:
                    raise ApiRefused("GraphQL: unterminated block string")
                if document[end - 1] != "\\":
                    break
                end += 3
            tokens.append('"')
            i = end + 3
        elif c == '"':
            i += 1
            while i < n and document[i] != '"':
                if document[i] in "\r\n":
                    raise ApiRefused("GraphQL: unterminated string")
                i += 2 if document[i] == "\\" else 1
            if i >= n:
                raise ApiRefused("GraphQL: unterminated string")
            tokens.append('"')
            i += 1
        elif document.startswith("...", i):
            tokens.append("...")
            i += 3
        elif c in "!$&()[]{}:=@|":
            tokens.append(c)
            i += 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (document[j].isalnum() or document[j] == "_"):
                j += 1
            tokens.append(document[i:j])
            i = j
        elif c.isdigit() or c in "-+.":
            j = i + 1
            while j < n and (document[j].isalnum() or document[j] in ".+-"):
                j += 1
            tokens.append("0")
            i = j
        else:
            raise ApiRefused(f"GraphQL: unexpected character {c!r}")
    return tokens


def _skip_balanced(tokens: list[str], i: int, open_: str, close: str) -> int:
    depth = 0
    while i < len(tokens):
        if tokens[i] == open_:
            depth += 1
        elif tokens[i] == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ApiRefused("GraphQL: unbalanced brackets")


def _skip_directives(tokens: list[str], i: int) -> int:
    while i < len(tokens) and tokens[i] == "@":
        i += 2
        if i < len(tokens) and tokens[i] == "(":
            i = _skip_balanced(tokens, i, "(", ")")
    return i


def mutation_fields(document: str) -> list[list[str]]:
    """Top-level field names of every mutation (or subscription) operation in ``document``."""

    tokens = _tokens(document)
    operations, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        if token == "{":  # anonymous query
            i = _skip_balanced(tokens, i, "{", "}")
            continue
        if token not in {"query", "mutation", "subscription", "fragment"}:
            raise ApiRefused(f"GraphQL: unexpected {token!r} at the top level")
        kind, i = token, i + 1
        while i < len(tokens) and tokens[i] not in {"{", "(", "@"}:
            i += 1  # operation name, or fragment name + 'on' + type
        if i < len(tokens) and tokens[i] == "(":
            i = _skip_balanced(tokens, i, "(", ")")
        i = _skip_directives(tokens, i)
        if i >= len(tokens) or tokens[i] != "{":
            raise ApiRefused("GraphQL: operation without a selection set")
        end = _skip_balanced(tokens, i, "{", "}")
        if kind in {"mutation", "subscription"}:
            operations.append(_top_fields(tokens[i + 1:end - 1]))
        i = end
    return operations


def _top_fields(selection: list[str]) -> list[str]:
    fields, i = [], 0
    while i < len(selection):
        token = selection[i]
        if token == "...":
            raise ApiRefused("GraphQL: fragments are not allowed in a mutation")
        if not (token[0].isalpha() or token[0] == "_"):
            raise ApiRefused(f"GraphQL: unexpected {token!r} in a mutation")
        name, i = token, i + 1
        if i < len(selection) and selection[i] == ":":  # alias: field
            if i + 1 >= len(selection):
                raise ApiRefused("GraphQL: alias without a field")
            name, i = selection[i + 1], i + 2
        if i < len(selection) and selection[i] == "(":
            i = _skip_balanced(selection, i, "(", ")")
        i = _skip_directives(selection, i)
        if i < len(selection) and selection[i] == "{":
            i = _skip_balanced(selection, i, "{", "}")
        fields.append(name)
    return fields


def check_graphql(body: str | None) -> None:
    try:
        payload: Any = json.loads(body or "")
    except ValueError as exc:
        raise ApiRefused("GraphQL: the request body must be JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
        raise ApiRefused("GraphQL: the body must be one JSON object with a \"query\" string")
    for fields in mutation_fields(payload["query"]):
        refused = [field for field in fields if field not in GRAPHQL_MUTATIONS] or ([] if fields else ["(empty)"])
        if refused:
            raise ApiRefused(f"GraphQL mutation {', '.join(refused)} is not on Harbor's allowlist "
                             f"({', '.join(sorted(GRAPHQL_MUTATIONS))}); merge with `pr-merge N --sha SHA`")
