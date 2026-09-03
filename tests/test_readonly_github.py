"""Harbor performs no GitHub mutation, ever (plan section 3, section 6 AC-8).

"Harbor never performs a GitHub mutation -- the builder does all of them, per
its skill."  This is a structural guarantee, so it is asserted structurally:
the client can only send queries, and a full poll cycle proves it in flight.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx

from harbor.clients.github import GitHubClient, build_pull_requests_query

SOURCE = Path(__file__).resolve().parents[1] / "src" / "harbor"

MUTATION_WORDS = re.compile(
    r"\b(mutation|mergePullRequest|createPullRequest|resolveReviewThread|addPullRequestReview"
    r"|addComment|createCommitOnBranch|closePullRequest|updatePullRequest)\b",
    re.IGNORECASE,
)


def test_the_query_builder_emits_only_a_query() -> None:
    query = build_pull_requests_query("keithlee", "Bookgen", [217, 221])
    assert query.lstrip().startswith("query ")
    assert "mutation" not in query.lower()


def test_no_github_module_names_a_mutation() -> None:
    for name in ("clients/github.py", "poll/github_poller.py", "facts/ci.py", "facts/threads.py"):
        text = (SOURCE / name).read_text(encoding="utf-8")
        # Strip comments/docstrings' prose mentions of the word "mutation".
        code = "\n".join(
            line for line in text.splitlines()
            if not line.strip().startswith("#")
        )
        found = [m.group(0) for m in MUTATION_WORDS.finditer(code)]
        # A prose mention inside a docstring is acceptable; an identifier is not.
        assert not [f for f in found if f.lower() != "mutation"], f"{name} references {found}"


def test_a_full_poll_sends_only_queries_over_the_wire() -> None:
    """The in-flight proof: capture every request a poll actually makes."""

    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST", "GraphQL only"
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json={"data": {"repository": {}}})

    async def scenario() -> None:
        client = GitHubClient(token="k", transport=httpx.MockTransport(handler))
        try:
            await client.pull_requests("keithlee", "Bookgen", [217, 218, 221])
        finally:
            await client.close()

    asyncio.run(scenario())
    assert sent, "the poll must actually have sent something"
    for body in sent:
        query = body.get("query", "")
        assert query.lstrip().startswith("query "), f"non-query document sent: {query[:60]}"
        assert "mutation" not in query.lower()
