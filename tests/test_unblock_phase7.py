from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import httpx

from harbor.db import HarborDB
from harbor.clients.linear import LinearClient
from harbor.unblock import (
    UnblockLadder,
    model_merge_outcome,
    observe_manual_merge,
    triage,
)


def run(awaitable):
    return asyncio.run(awaitable)


def ticket(**extra):
    value = {"identifier": "SAMPLE-42", "id": "linear-42", "title": "Blocked", "state": "In Progress"}
    value.update(extra)
    return value


class LinearFake:
    def __init__(self):
        self.comments = []
        self.states = []

    async def create_comment(self, issue_id, body):
        self.comments.append((issue_id, body))
        return {"comment": {"id": f"comment-{len(self.comments)}"}}

    async def update_issue_state(self, issue_id, state):
        self.states.append((issue_id, state))
        return {"success": True}


def test_rung_zero_repairs_missing_module_and_resumes_same_session(tmp_path: Path):
    db = HarborDB(tmp_path / "harbor.db")
    commands = []
    resumed = []
    linear = LinearFake()

    async def execute(command):
        commands.append(command)
        return True

    async def resume(run_id, prompt):
        resumed.append((run_id, prompt))
        return True

    try:
        result = run(
            UnblockLadder(
                db,
                linear=linear,
                resumer=resume,
                remediation_executor=execute,
            ).handle(
                ticket(),
                {"id": "builder-session"},
                {"blocker": {"kind": "env_blocker", "question": "Cannot find module 'x'"}},
            )
        )
        assert result.rung == 0
        assert result.resumed and result.same_session
        assert commands == [["pnpm", "install"]]
        assert resumed[0][0] == "builder-session"
        assert linear.comments == [] and linear.states == []
        assert db.escalations()[0]["rung_resolved"] == 0
    finally:
        db.close()


def _github_profile(token_env: str = "SAMPLE_GITHUB_TOKEN"):
    from types import SimpleNamespace
    return SimpleNamespace(github_token_env=token_env, github_api_url="https://api.github.com/graphql",
                           repository="owner/repo", repository_owner="owner", repository_name="repo")


def test_rung_zero_verifies_the_project_token_and_resumes_with_the_rest_helper(tmp_path: Path, monkeypatch):
    # Regression: this used to run `gh auth status` on the host and resume the
    # builder with "use gh for GitHub mutations", which hangs in the sandbox.
    monkeypatch.setenv("SAMPLE_GITHUB_TOKEN", "dummy-project-token-not-a-secret")
    db = HarborDB(tmp_path / "harbor.db")
    commands = []
    resumed = []

    async def execute(command):
        commands.append(command)
        return {"returncode": 0}

    async def resume(run_id, prompt):
        resumed.append((run_id, prompt))
        return True

    try:
        result = run(
            UnblockLadder(db, resumer=resume, remediation_executor=execute,
                          github_profile=_github_profile()).handle(
                ticket(),
                {"id": "builder-session"},
                {"blocker": {"kind": "github_transport", "question": "Which approved GitHub mutation transport should be used?"}},
            )
        )
        assert result.rung == 0 and result.action == "resume" and result.resumed
        assert result.remediation is not None and result.remediation.plan is not None
        assert result.remediation.plan.kind == "github_transport"
        [command] = commands
        assert "gh" not in command and command[0] == "env"
        assert "HARBOR_GITHUB_TOKEN_ENV=SAMPLE_GITHUB_TOKEN" in command
        from harbor.github_rest import helper_command
        assert "HARBOR_GITHUB_REPOSITORY=owner/repo" in command
        assert command[4:] == [*helper_command(), "api", "GET", "/repos/owner/repo"]
        assert "-I" in command and "-m" not in command
        assert "dummy-project-token-not-a-secret" not in " ".join(command)
        assert resumed[0][0] == "builder-session"
        assert "REST helper" in resumed[0][1] and "never the gh CLI" in resumed[0][1]
        assert "use gh" not in resumed[0][1]
    finally:
        db.close()


def test_github_transport_missing_token_escalates_with_the_doctor_fix_and_never_asks_gh(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SAMPLE_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-broader-token-not-a-secret")
    db = HarborDB(tmp_path / "harbor.db")
    commands = []
    notifications = []

    async def notify(message):
        notifications.append(message)

    try:
        result = run(
            UnblockLadder(db, notifier=notify, remediation_executor=lambda command: commands.append(command),
                          github_profile=_github_profile()).handle(
                ticket(),
                {"id": "builder-session"},
                {"blocker": {"kind": "github_transport", "question": "GitHub token missing"}},
            )
        )
        assert result.rung == 0 and result.action == "escalate" and not result.resumed
        assert commands == []  # nothing runs: there is no token to check, and never gh
        assert "export SAMPLE_GITHUB_TOKEN" in result.reason and "Contents: read and write" in result.reason
        assert "gh auth" not in result.reason and "dummy-broader-token" not in result.reason
        assert notifications and "needs the project's GitHub token" in notifications[0]
    finally:
        db.close()


def test_github_transport_auth_failure_skips_first_mate_and_linear_parking(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SAMPLE_GITHUB_TOKEN", "dummy-project-token-not-a-secret")
    db = HarborDB(tmp_path / "harbor.db")
    linear = LinearFake()
    first_mate_calls = []
    notifications = []

    async def first_mate(*args):
        first_mate_calls.append(args)
        return {"disposition": "resolve", "answer": "wrong", "citation": "AGENTS.md:1"}

    async def notify(message):
        notifications.append(message)

    try:
        result = run(
            UnblockLadder(
                db,
                linear=linear,
                first_mate=first_mate,
                notifier=notify,
                remediation_executor=lambda command: {"returncode": 1},
                github_profile=_github_profile(),
            ).handle(
                ticket(),
                {"id": "builder-session"},
                {"blocker": {"kind": "github_transport", "question": "Which approved GitHub mutation transport should be used?"}},
            )
        )
        assert result.rung == 0 and result.action == "escalate" and not result.resumed
        assert "GitHub rejected the project's token in SAMPLE_GITHUB_TOKEN" in result.reason
        assert "export SAMPLE_GITHUB_TOKEN" in result.reason and "gh auth" not in result.reason
        assert first_mate_calls == []
        assert linear.comments == [] and linear.states == []
        assert notifications and "needs the project's GitHub token" in notifications[0]
    finally:
        db.close()


def test_rung_one_cites_discoveries_without_paging(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "DISCOVERIES.md").write_text(
        "Use the existing reader cache when the cache is unavailable.\n", encoding="utf-8"
    )
    db = HarborDB(tmp_path / "harbor.db")
    linear = LinearFake()
    calls = []

    async def terra(prompt, blocker):
        calls.append(prompt)
        return {
            "disposition": "resolve",
            "answer": "Use the existing reader cache.",
            "citation": "docs/DISCOVERIES.md:1",
        }

    async def resume(run_id, prompt):
        return True

    try:
        result = run(
            UnblockLadder(
                db,
                linear=linear,
                first_mate=terra,
                resumer=resume,
                repo_path=tmp_path,
                remediation_executor=lambda command: False,
            ).handle(
                ticket(),
                {"id": "builder-session"},
                {"blocker": {"kind": "ambiguous_ac", "question": "How should cache be unavailable?"}},
            )
        )
        assert result.rung == 1 and result.resumed and not result.notified
        assert result.citation.startswith("docs/DISCOVERIES.md")
        assert len(calls) == 1
        assert len(linear.comments) == 1 and linear.states == []
    finally:
        db.close()


def test_unblock_uses_retained_worktree_authority_not_stale_base_checkout(tmp_path: Path):
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    base.mkdir()
    worktree.mkdir()
    (base / "AGENTS.md").write_text("Ask a human about every merge.\n", encoding="utf-8")
    (worktree / "AGENTS.md").write_text(
        "Low-risk PRs are pre-authorized to merge after the clean-context audit passes.\n",
        encoding="utf-8",
    )
    resumed = []

    async def terra(prompt, blocker):
        assert "pre-authorized" in prompt
        assert "Ask a human" not in prompt
        return {"disposition": "resolve", "answer": "Proceed after the audit.", "citation": "AGENTS.md:1"}

    async def resume(run_id, prompt):
        resumed.append((run_id, prompt))
        return True

    result = run(
        UnblockLadder(first_mate=terra, resumer=resume, repo_path=base, remediation_executor=lambda command: False).handle(
            ticket(), {"id": "builder-session", "worktree": str(worktree)},
            {"blocker": {"kind": "ambiguous_ac", "question": "May this low-risk PR merge?"}},
        )
    )
    assert result.rung == 1 and result.resumed
    assert resumed[0][0] == "builder-session"


def test_repository_agent_role_is_adapted_without_human_escalation(tmp_path: Path):
    role = tmp_path / ".claude" / "agents" / "pr-proof-auditor.md"
    role.parent.mkdir(parents=True)
    role.write_text("Audit the PR from clean context. Input: PR URL only.\n", encoding="utf-8")
    resumed = []

    async def resume(run_id, prompt):
        resumed.append((run_id, prompt))
        return True

    result = run(
        UnblockLadder(resumer=resume, repo_path=tmp_path).handle(
            ticket(), {"id": "builder-session", "worktree": str(tmp_path)},
            {"blocker": {"kind": "missing_proof_capability", "question": "Can the mandatory clean-context pr-proof-auditor be invoked?", "evidence": "unknown agent_type 'pr-proof-auditor'"}},
        )
    )
    assert result.rung == 0 and result.resumed and not result.notified
    assert "supported `default` subagent" in resumed[0][1]
    assert ".claude/agents/pr-proof-auditor.md:1" in resumed[0][1]


def test_approval_gated_linear_connector_is_adapted_without_human(tmp_path: Path):
    resumed = []

    async def resume(run_id, prompt):
        resumed.append((run_id, prompt))
        return True

    result = run(
        UnblockLadder(resumer=resume, repo_path=tmp_path).handle(
            ticket(), {"id": "builder-session", "worktree": str(tmp_path)},
            {"blocker": {"kind": "env_blocker", "question": "How should Linear be updated?", "evidence": "mcp__linear__save_issue returned: MCP tool call requires approval, but approval policy is never."}},
        )
    )
    assert result.rung == 0 and result.resumed and not result.notified
    assert "mcp__codex_apps__linear_*" in resumed[0][1]
    assert "mcp__linear__*" in resumed[0][1]


def test_novel_product_question_escalates_and_lists_sources(tmp_path: Path):
    db = HarborDB(tmp_path / "harbor.db")
    linear = LinearFake()
    terra_calls = []
    notices = []

    async def terra(*args):
        terra_calls.append(args)
        return {"disposition": "resolve", "answer": "invented", "citation": "made-up"}

    async def notify(reason):
        notices.append(reason)

    try:
        result = run(
            UnblockLadder(
                db,
                linear=linear,
                first_mate=terra,
                notifier=notify,
                repo_path=tmp_path,
                remediation_executor=lambda command: False,
            ).handle(
                ticket(),
                {"id": "builder-session"},
                {
                    "blocker": {
                        "kind": "product_decision",
                        "question": "Choose a new product behavior for empty state",
                    }
                },
            )
        )
        assert result.rung == 2 and result.action == "park" and result.notified
        assert terra_calls == []
        assert notices and "tried rung 0 and rung 1" in notices[0]
        assert linear.states == [("linear-42", "Human Review")]
        assert "Searched: none" in linear.comments[0][1]
        assert db.escalations()[0]["rung_resolved"] == 2
    finally:
        db.close()


def test_empty_resolution_citation_is_rejected(tmp_path: Path):
    db = HarborDB(tmp_path / "harbor.db")
    try:
        with pytest.raises(ValueError, match="citation"):
            db.record_resolution("env_blocker", "question", "answer", "  ")
    finally:
        db.close()


def test_third_same_kind_block_parks_after_two_rung_one_answers(tmp_path: Path):
    db = HarborDB(tmp_path / "harbor.db")
    linear = LinearFake()
    db.record_escalation("SAMPLE-42", "one", "env_blocker", "q1", 1, citation="AGENTS.md:1", answer="a")
    db.record_escalation("SAMPLE-42", "two", "env_blocker", "q2", 1, citation="AGENTS.md:2", answer="a")
    try:
        result = run(
            UnblockLadder(db, linear=linear, remediation_executor=lambda command: False).handle(
                ticket(), {"id": "three"}, {"blocker": {"kind": "env_blocker", "question": "q3"}}
            )
        )
        assert result.rung == 2
        assert db.escalations()[-1]["rung_resolved"] == 2
    finally:
        db.close()


def test_resolution_is_reused_at_rung_one_without_terra(tmp_path: Path):
    db = HarborDB(tmp_path / "harbor.db")
    db.record_resolution(
        "ambiguous_ac",
        "Which cache?",
        "Use local cache.",
        "resolutions#7",
        source_ticket="SAMPLE-41",
    )
    calls = []

    async def terra(*args):
        calls.append(args)
        return None

    try:
        result = run(
            triage(
                ticket(),
                {"kind": "ambiguous_ac", "question": "Which cache?"},
                db=db,
                first_mate=terra,
                repo_path=tmp_path,
            )
        )
        assert result.resolved and result.from_resolution
        assert result.answer == "Use local cache."
        assert calls == []
    finally:
        db.close()


def test_merge_model_is_a_builder_command_not_a_harbor_write():
    held = model_merge_outcome("low", True, ticket="SAMPLE-42")
    free = model_merge_outcome("low", False, ticket="SAMPLE-42")
    high = model_merge_outcome("high", False, ticket="SAMPLE-42")
    assert held.outcome == "parked" and held.waiting_on == "human" and held.command.action == "park"
    assert free.outcome == "merged" and free.command.action == "merge"
    assert high.outcome == "parked" and high.command.action == "park"
    assert held.harbor_writes == free.harbor_writes == high.harbor_writes == ()


def test_manual_merge_reengages_retained_builder_only():
    command = observe_manual_merge(
        ticket(), {"id": "builder-session", "waiting_on": "human"}, {"merged": True}
    )
    assert command is not None
    assert command.action == "reengage" and command.run_id == "builder-session"


@pytest.mark.parametrize("value", ["true", "MERGED", 1, "1"])
def test_manual_merge_requires_a_literal_boolean(value: object) -> None:
    assert (
        observe_manual_merge(
            ticket(),
            {"id": "builder-session", "waiting_on": "human"},
            {"merged": value},
        )
        is None
    )


def test_linear_progress_primitives_write_comment_and_nonterminal_state_only():
    requests = []

    def handler(request):
        requests.append(request)
        body = request.content.decode()
        if "commentCreate" in body:
            return httpx.Response(200, json={"data": {"commentCreate": {"success": True, "comment": {"id": "c"}}}})
        if "HarborStates" in body:
            # issueUpdate takes a workflow state UUID; a name is resolved first.
            return httpx.Response(200, json={"data": {"issue": {"id": "linear-42", "team": {
                "id": "t", "name": "HAN", "states": {"nodes": [
                    {"id": "6876f9e0-76aa-4dac-ac4f-fabeceb1d54f", "name": "Blocked", "type": "started"},
                ], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
        return httpx.Response(200, json={"data": {"issueUpdate": {"success": True, "issue": {"state": {"name": "Blocked"}}}}})

    async def exercise():
        client = LinearClient(token="test", transport=httpx.MockTransport(handler))
        try:
            await client.create_comment("linear-42", "[harbor] progress")
            await client.update_issue_state("linear-42", "Blocked")
            with pytest.raises(ValueError):
                await client.update_issue_state("linear-42", "Done")
        finally:
            await client.close()

    run(exercise())
    # comment, state-name lookup, issueUpdate -- and nothing for "Done",
    # which is refused before any network call.
    assert len(requests) == 3
    assert all("Done" not in request.content.decode() for request in requests)


def test_a_parked_merge_ready_run_goes_to_human_review_not_blocked():
    """AC-7: outcome=parked + waiting_on=human means the work is DONE.

    Only the merge click is the operator's, so the ticket belongs in Human
    Review even when the run also reported an incidental env blocker --
    routing it to Blocked misreports a finished ticket as a broken one.
    """

    from harbor.unblock import _is_parked_for_human

    assert _is_parked_for_human({"outcome": "parked", "waiting_on": "human"}) is True
    assert _is_parked_for_human({"outcome": "parked", "waiting_on": "review"}) is False
    assert _is_parked_for_human({"outcome": "no_pr", "waiting_on": "human"}) is False
    assert _is_parked_for_human({}) is False
    assert _is_parked_for_human(None) is False


def test_ticket_c_cites_ticket_bs_resolution_and_pages_nobody(tmp_path):
    """Plan section 6, tickets B and C, as a matched pair.

    C proves the citation came from the `resolutions` ledger and nowhere
    else: with B's answer stored, the same question resolves at rung 1 with
    nobody paged; with the ledger truncated, the SAME question pages the
    operator at rung 2 and cites nothing.
    """

    from harbor.db import HarborDB
    from harbor.unblock import UnblockLadder

    question = "Which team owns the pipeline concurrency limits?"

    class Terra:
        def __init__(self) -> None:
            self.calls = 0

        async def ask(self, *args, **kwargs):
            self.calls += 1
            return "ESCALATE: no authoritative source answers this."

        __call__ = ask

    class Pager:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def notify(self, reason: str) -> None:
            self.sent.append(reason)

    async def exercise(seed: bool):
        db = HarborDB(":memory:")
        try:
            if seed:
                db.record_resolution("product_decision", question, "Keith owns it.", "AGENTS.md:12", source_ticket="SAMPLE-B")
            terra, pager = Terra(), Pager()
            ladder = UnblockLadder(db, linear=None, first_mate=terra, notifier=pager, repo_path=tmp_path)
            result = await ladder.handle(
                {"identifier": "SAMPLE-C", "id": "uuid-c"},
                {"id": "builder-c"},
                {"status": "blocked", "summary": "s",
                 "blocker": {"kind": "product_decision", "question": question}},
            )
            return result, terra, pager
        finally:
            db.close()

    # Ticket C, with B's answer on file: resolved at rung 1, nobody paged.
    result, terra, pager = run(exercise(seed=True))
    assert result.action == "resume"
    assert result.rung == 1
    assert result.citation == "AGENTS.md:12"
    assert result.notified is False and pager.sent == []
    assert terra.calls == 0, "a stored resolution is authority; Terra is not needed"

    # The control: cold ledger, same question -> paged again, nothing cited.
    cold, _, cold_pager = run(exercise(seed=False))
    assert cold.action != "resume"
    assert cold.rung == 2
    assert cold.citation == ""
    assert cold.notified is True and len(cold_pager.sent) == 1


def test_a_protected_path_is_never_auto_resolved_even_with_a_stored_answer(tmp_path):
    """The protected-path bar is absolute; the resolutions ledger cannot lift it.

    Ticket C's reordering lets a stored answer clear a product decision. This
    pins the line that reordering must NOT cross.
    """

    from harbor.db import HarborDB
    from harbor.unblock import UnblockLadder

    question = "May I change the deploy pipeline?"

    async def exercise():
        db = HarborDB(":memory:")
        try:
            db.record_resolution("product_decision", question, "Yes, go ahead.", "AGENTS.md:1", source_ticket="SAMPLE-B")
            ladder = UnblockLadder(db, linear=None, notifier=None, repo_path=tmp_path, protected_paths=("deploy/",))
            return await ladder.handle(
                {"identifier": "SAMPLE-P"}, {"id": "b"},
                {"status": "blocked", "summary": "s",
                 "blocker": {"kind": "product_decision", "question": question,
                             "paths": ["deploy/prod.yml"]}},
            )
        finally:
            db.close()

    result = run(exercise())
    assert result.action != "resume", "a protected path must never auto-resolve"
    assert result.rung == 2


def test_a_protected_directory_covers_the_files_inside_it():
    """`deploy/` must guard `deploy/prod.yml`, and must not catch `deployment.md`.

    fnmatch alone matches neither, so a protected directory used to guard
    only itself -- the guarantee was silently empty for every real file.
    """

    from harbor.unblock import touches_protected_paths as touches

    def check(path: str, pattern: str) -> bool:
        return touches({"paths": [path]}, protected_paths=[pattern], repo_path=None)

    assert check("deploy/prod.yml", "deploy/") is True
    assert check("deploy/prod.yml", "deploy") is True
    assert check("apps/web/deploy/x.ts", "deploy/") is True
    assert check("deploy", "deploy/") is True
    assert check("src/a.ts", "src/*.ts") is True

    # Segment-wise matching: a prefix lookalike is not inside the directory.
    assert check("deployment.md", "deploy/") is False
    assert check("x/deployment.md", "deploy") is False
    assert check("docs/readme.md", "deploy/") is False
