"""Operator surfaces the plan's test blocks invoke directly.

Phase 2 runs ``harbor facts``, Phase 3 runs ``harbor start --shadow``, and
Phase 7 AC-6 runs ``harbor answer``.  These pin the argument surface and the
shadow contract: decide, report, mutate nothing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import harbor.cli as cli
from harbor.cli import build_parser
from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB
from harbor.poll.linear_poller import PollResult
from harbor.supervisor import Supervisor


class TodoPoller:
    """One Todo ticket, which a live supervisor would dispatch."""

    project_name = "SampleProject"
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", heartbeat_seconds=1)

    async def poll(self) -> PollResult:
        ticket = {"identifier": "SAMPLE-42", "title": "A ticket", "state": "Todo", "pr_urls": []}
        return PollResult((ticket,), "snapshot-1", True, (ticket,))


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch(self, wake: Any) -> None:
        self.calls.append("dispatch")

    async def resume(self, wake: Any) -> None:
        self.calls.append("resume")


class ShadowLinearClient:
    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self.nodes = nodes
        self.fetches: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    async def fetch_issues(self, projects: tuple[str, ...], states: tuple[str, ...]) -> list[dict[str, Any]]:
        self.fetches.append((projects, states))
        return self.nodes

    async def close(self) -> None:
        return None


def test_parser_exposes_facts_shadow_and_answer() -> None:
    parser = build_parser()
    assert parser.parse_args(["facts", "SampleProject", "--json"]).json is True
    assert parser.parse_args(["start", "SampleProject", "--shadow"]).shadow is True
    answer = parser.parse_args(["answer", "7", "Keith", "owns", "it", "--project", "SampleProject"])
    assert answer.escalation == "7"
    assert " ".join(answer.text) == "Keith owns it"
    comment = parser.parse_args(["linear", "comment", "ABC-123", "--body", "progress"])
    assert comment.linear_command == "comment"
    assert comment.issue == "ABC-123"
    state = parser.parse_args(["linear", "state", "ABC-123", "--state", "In Review"])
    assert state.linear_command == "state"
    assert state.state == "In Review"


def test_linear_commands_use_direct_client_without_mcp(monkeypatch, capsys) -> None:
    class DirectLinearClient:
        instances: list["DirectLinearClient"] = []

        def __init__(self, *, endpoint: str) -> None:
            self.endpoint = endpoint
            self.token = "configured-key"
            self.comments: list[tuple[str, str]] = []
            self.states: list[tuple[str, str]] = []
            self.closed = False
            self.instances.append(self)

        async def create_comment(self, issue: str, body: str) -> dict[str, object]:
            self.comments.append((issue, body))
            return {"success": True}

        async def update_issue_state(self, issue: str, state: str) -> dict[str, object]:
            self.states.append((issue, state))
            return {"issue": {"state": {"name": state}}}

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(cli, "LinearClient", DirectLinearClient)

    assert asyncio.run(cli._linear_write("comment", "ABC-123", body="progress")) == 0
    assert DirectLinearClient.instances[0].comments == [("ABC-123", "progress")]
    assert DirectLinearClient.instances[0].closed is True
    assert asyncio.run(cli._linear_write("state", "ABC-123", state="In Review")) == 0
    assert DirectLinearClient.instances[1].states == [("ABC-123", "In Review")]
    assert "commented on ABC-123" in capsys.readouterr().out


def test_linear_commands_fail_clearly_without_an_api_key(monkeypatch, capsys) -> None:
    class UnauthenticatedLinearClient:
        token = None

        def __init__(self, *, endpoint: str) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(cli, "LinearClient", UnauthenticatedLinearClient)

    assert asyncio.run(cli._linear_write("comment", "ABC-123", body="progress")) == 2
    assert "HARBOR_LINEAR_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize("terminal", ["Done", "completed", "CANCELED", "cancelled"])
def test_linear_state_command_rejects_terminal_states_before_calling_the_transport(
    monkeypatch, capsys, terminal: str
) -> None:
    class RecordingLinearClient:
        instances: list["RecordingLinearClient"] = []

        def __init__(self, *, endpoint: str) -> None:
            self.token = "configured-key"
            self.states: list[tuple[str, str]] = []
            self.instances.append(self)

        async def update_issue_state(self, issue: str, state: str) -> dict[str, object]:
            self.states.append((issue, state))
            return {"success": True}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(cli, "LinearClient", RecordingLinearClient)

    assert asyncio.run(cli._linear_write("state", "ABC-123", state=terminal)) == 1
    assert RecordingLinearClient.instances[0].states == []
    assert "verified merge" in capsys.readouterr().err


def test_shadow_start_prints_observed_ticket_digest_without_dispatch(monkeypatch, tmp_path: Path, capsys) -> None:
    node = {
        "id": "linear-id-42",
        "identifier": "SAMPLE-42",
        "title": "A ticket",
        "state": {"name": "Todo", "type": "backlog"},
        "attachments": {"nodes": []},
        "comments": {"nodes": []},
        "inverseRelations": {"nodes": []},
    }
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", repo_path=tmp_path)
    client = ShadowLinearClient([node])
    dispatcher = RecordingDispatcher()
    monkeypatch.setattr(cli, "get_project", lambda _: config)
    monkeypatch.setattr(cli, "_db_path", lambda: tmp_path / "harbor.db")

    result = asyncio.run(
        cli._start(
            "SampleProject",
            shadow=True,
            linear_client=client,
            dispatcher=dispatcher,
            first_mate=object(),
            notifier=object(),
            unblock_ladder=object(),
        )
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "(shadow mode)" in output
    assert "Harbor: RUNNING · SampleProject · Linear healthy\nSAMPLE-42 (observed)  Todo  A ticket" in output
    assert "WOULD WAKE build SAMPLE-42 · idem_key=SAMPLE-42@build" in output
    assert dispatcher.calls == []
    assert len(client.fetches) == 1


def test_shadow_decides_a_build_but_never_dispatches() -> None:
    async def scenario() -> None:
        dispatcher = RecordingDispatcher()
        supervisor = Supervisor(TodoPoller(), dispatcher=dispatcher, shadow=True)
        await supervisor.poll_once()
        assert dispatcher.calls == []
        assert [w.action for w in supervisor.shadow_wakes] == ["build"]
        assert supervisor.shadow_wakes[0].idem_key == "SAMPLE-42@build"

    asyncio.run(scenario())


def test_a_live_supervisor_dispatches_the_same_ticket() -> None:
    """The control: shadow is the only reason nothing happened above."""

    async def scenario() -> None:
        dispatcher = RecordingDispatcher()
        supervisor = Supervisor(TodoPoller(), dispatcher=dispatcher)
        await supervisor.poll_once()
        assert dispatcher.calls == ["dispatch"]
        assert supervisor.shadow_wakes == []

    asyncio.run(scenario())


def test_two_shadow_cycles_produce_identical_decisions() -> None:
    """Phase 3: the diff of two consecutive shadow runs must be empty."""

    async def scenario() -> None:
        supervisor = Supervisor(TodoPoller(), dispatcher=RecordingDispatcher(), shadow=True)
        await supervisor.poll_once()
        first = [(w.action, w.idem_key) for w in supervisor.shadow_wakes]
        supervisor.shadow_wakes.clear()
        await supervisor.poll_once()
        second = [(w.action, w.idem_key) for w in supervisor.shadow_wakes]
        # The decider's shadow ledger makes the second cycle a no-op.
        assert second == []
        assert first == [("build", "SAMPLE-42@build")]

    asyncio.run(scenario())


def test_shadow_does_not_deliver_pr_events() -> None:
    class Processor:
        def __init__(self) -> None:
            self.calls = 0

        async def process(self, *args: Any, **kwargs: Any) -> None:
            self.calls += 1

    async def scenario() -> None:
        processor = Processor()
        supervisor = Supervisor(TodoPoller(), shadow=True, pr_event_processor=processor)
        await supervisor._process_pr_events({"identifier": "SAMPLE-42"}, {"prs": [{"number": 1}]})
        assert processor.calls == 0

    asyncio.run(scenario())


def test_uncited_answer_cites_the_operator_and_marks_the_escalation_answered(tmp_path) -> None:
    """A product call has no document to cite: the operator is the authority."""

    db = HarborDB(tmp_path / "harbor.db")

    async def scenario() -> None:
        db.replace_tickets("SampleProject", [{"identifier": "SAMPLE-42", "title": "A ticket", "state": "Blocked"}])
        escalation = db.record_escalation("SAMPLE-42", None, "product_decision", "Who owns it?", 2)
        supervisor = Supervisor(TodoPoller(), db=db, shadow=True)
        result = await supervisor.answer_escalation(str(escalation["id"]), "Keith owns it")
        assert result["answer"] == "Keith owns it"
        assert result["citation"] == f"operator:escalation-{escalation['id']}"
        stored = db.find_resolution("product_decision", "Who owns it?", project="SampleProject")
        assert stored and stored["citation"] == result["citation"]
        assert db.open_escalations() == []
        answered = db.escalations()[0]
        assert answered["status"] == "answered" and answered["resolution_id"] == stored["id"]

    try:
        asyncio.run(scenario())
    finally:
        db.close()


def test_answer_records_a_cited_resolution() -> None:
    """AC-6: harbor answer writes a resolutions row."""

    class Ledger:
        def __init__(self) -> None:
            self.recorded: list[tuple[Any, ...]] = []

        def set_runtime(self, *args: Any, **kwargs: Any) -> None:
            return None

        def escalations(self) -> list[dict[str, Any]]:
            return [{"id": "7", "kind": "product_decision", "question": "Who owns it?", "ticket": "SAMPLE-42"}]

        def record_resolution(self, kind: str, question: str, answer: str, citation: str, **kwargs: Any) -> int:
            self.recorded.append((kind, question, answer, citation))
            return 1

    async def scenario() -> None:
        ledger = Ledger()
        supervisor = Supervisor(TodoPoller(), db=ledger, shadow=True)
        result = await supervisor.answer_escalation("7", "Keith owns it\nCitation: AGENTS.md:12")
        assert result["citation"] == "AGENTS.md:12"
        assert result["answer"] == "Keith owns it"
        assert ledger.recorded == [("product_decision", "Who owns it?", "Keith owns it", "AGENTS.md:12")]

    asyncio.run(scenario())


class ShowLinearClient:
    """Answers the full-issue read the way Linear's GraphQL client does."""

    instances: list["ShowLinearClient"] = []
    description = ""

    def __init__(self, *, endpoint: str) -> None:
        self.endpoint = endpoint
        self.token = "lin_api_SECRET-NEVER-PRINTED"
        self.reads: list[str] = []
        self.writes: list[object] = []
        self.closed = False
        ShowLinearClient.instances.append(self)

    async def fetch_blocker_context(self, identifier: str) -> dict[str, object]:
        self.reads.append(identifier)
        return {
            "identifier": identifier, "title": "Long ticket", "url": "https://linear.app/x/issue/HAN-94",
            "state": {"name": "In Progress", "type": "started"},
            "description": self.description,
            "comments": {"nodes": [
                {"id": "c2", "createdAt": "2026-11-02T00:00:00Z", "body": "SECOND-COMMENT", "user": {"name": "B"}},
                {"id": "c1", "createdAt": "2026-11-01T00:00:00Z", "body": "FIRST-COMMENT", "user": {"name": "A"}},
            ]},
        }

    async def create_comment(self, *args: object) -> None:
        self.writes.append(args)

    async def update_issue_state(self, *args: object) -> None:
        self.writes.append(args)

    async def close(self) -> None:
        self.closed = True


def test_linear_show_prints_the_full_description_and_every_comment(monkeypatch, capsys) -> None:
    """HAN-94: a builder could only see a truncated ticket and had no way to read the rest."""

    tail = "CRITERION-3-TAIL-SENTINEL"
    ShowLinearClient.instances = []
    ShowLinearClient.description = "1. first\n2. second\n3. " + ("x" * 40_000) + tail
    monkeypatch.setattr(cli, "LinearClient", ShowLinearClient)

    args = build_parser().parse_args(["linear", "show", "han-94"])
    assert asyncio.run(cli._async_main(args)) == 0
    out, err = capsys.readouterr()
    assert out.rstrip().endswith("SECOND-COMMENT")
    assert tail in out and len(out) > 40_000
    assert out.index("FIRST-COMMENT") < out.index("SECOND-COMMENT")
    assert "Comments (2, oldest first)" in out
    assert "SECRET-NEVER-PRINTED" not in out + err
    client = ShowLinearClient.instances[0]
    assert client.reads == ["HAN-94"] and client.writes == [] and client.closed


def test_linear_show_json_and_missing_key(monkeypatch, capsys) -> None:
    import json

    ShowLinearClient.instances = []
    ShowLinearClient.description = "body"
    monkeypatch.setattr(cli, "LinearClient", ShowLinearClient)
    assert asyncio.run(cli._linear_show("HAN-94", as_json=True)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["description"] == "body" and len(data["comments"]["nodes"]) == 2

    class NoKey(ShowLinearClient):
        def __init__(self, *, endpoint: str) -> None:
            super().__init__(endpoint=endpoint)
            self.token = None

    monkeypatch.setattr(cli, "LinearClient", NoKey)
    assert asyncio.run(cli._linear_show("HAN-94")) == 2
    assert "LINEAR_API_KEY" in capsys.readouterr().err
    assert asyncio.run(cli._linear_show("not an id")) == 2
