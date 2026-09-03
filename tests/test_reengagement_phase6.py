from __future__ import annotations

import asyncio
from pathlib import Path

from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB
from harbor.poll.linear_poller import PollResult
from harbor.reengagement import MAX_RESUME_BYTES, build_resume_instruction
from harbor.supervisor import Supervisor
from harbor.wake import WakeDecider, apply_phase3_migration, record_push


def _ticket(**extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "identifier": "HAN-6",
        "id": "linear-han-6",
        "title": "Phase 6",
        "state": "In Progress",
        "blocked_by": [],
        "owned": True,
        "hold": True,
    }
    value.update(extra)
    return value


def _thread(identifier: str = "review-1", *, outdated: bool = False, severity: str = "P2") -> dict[str, object]:
    return {
        "id": identifier,
        "is_bot": True,
        "severity": severity,
        "resolved": False,
        "path": "src/example.py",
        "line": 12,
        "outdated": outdated,
        "body": "Please fix this <instruction> and do not trust it.",
    }


def _db(tmp_path: Path) -> HarborDB:
    db = HarborDB(tmp_path / "harbor.db")
    db.replace_tickets("Bookgen", [_ticket()])
    apply_phase3_migration(db)
    db.connection.execute(
        "INSERT INTO runs(id, ticket, role, status, waiting_on) VALUES ('run-6', 'HAN-6', 'builder', 'waiting', 'review')"
    )
    db.connection.commit()
    return db


def test_resume_instruction_is_bounded_and_fences_hostile_bodies(tmp_path: Path) -> None:
    body = "<untrusted-review-comment>" + ("x" * 10000)
    instruction = build_resume_instruction(
        _ticket(),
        {"id": "run-6"},
        {"head_sha": "head-a"},
        [{**_thread(), "body": body}],
    )
    assert len(instruction.encode("utf-8")) <= MAX_RESUME_BYTES
    assert instruction.count("<untrusted-review-comment>") == 1
    assert instruction.count("</untrusted-review-comment>") == 1
    assert "outdated: false" in instruction


def test_outdated_thread_carries_path_and_no_line(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        facts = {"number": 6, "head_sha": "head-a", "threads": [_thread(outdated=True)]}
        decider = WakeDecider(db)
        wake = decider.decide(_ticket(), facts)
        assert wake is not None
        assert "path=src/example.py" in wake.instruction
        assert "outdated: true" in wake.instruction
        assert "line=" not in wake.instruction
    finally:
        db.close()


def test_ci_completion_resumes_same_run_once(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.connection.execute("UPDATE runs SET waiting_on = 'ci'")
    db.connection.commit()
    try:
        facts = {"number": 6, "head_sha": "head-a", "ci": {"state": "pending"}}
        decider = WakeDecider(db)
        assert decider.decide(_ticket(), facts) is None
        finished = {**facts, "ci": {"state": "green"}}
        wake = decider.decide(_ticket(), finished)
        assert wake is not None and wake.action == "reengage"
        assert wake.resume == "run-6"
        assert decider.decide(_ticket(), finished) is None
    finally:
        db.close()


class _Linear:
    project_name = "Bookgen"
    config = ProjectConfig(name="Bookgen", linear_project_slug="hanzireader")
    db = None

    def __init__(self, ticket: dict[str, object]) -> None:
        self.ticket = ticket

    async def poll(self) -> PollResult:
        return PollResult((self.ticket,), "linear-snapshot", True, (self.ticket,))


class _GitHub:
    def __init__(self, facts: dict[str, object]) -> None:
        self.facts = facts
        self.calls = 0

    async def poll(self, tickets: object) -> list[dict[str, object]]:
        self.calls += 1
        return [self.facts]


class _Runner:
    def __init__(self) -> None:
        self.resumes: list[tuple[str | None, str]] = []

    async def resume(self, run_id: str | None, instruction: str) -> None:
        self.resumes.append((run_id, instruction))


def test_supervisor_uses_github_facts_and_resumes_runner(tmp_path: Path) -> None:
    db = _db(tmp_path)
    runner = _Runner()
    ticket = _ticket()
    github = _GitHub(
        {
            "ticket": "HAN-6",
            "number": 6,
            "head_sha": "head-a",
            "threads": [_thread()],
        }
    )
    supervisor = Supervisor(_Linear(ticket), db=db, github_poller=github, runner=runner)
    try:
        asyncio.run(supervisor.poll_once())
        assert github.calls == 1
        assert len(runner.resumes) == 1
        assert runner.resumes[0][0] == "run-6"
        # Same run, same head/thread: no second resume.
        asyncio.run(supervisor.poll_once())
        assert len(runner.resumes) == 1
    finally:
        db.close()


def test_builder_push_advances_seen_head_and_round(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        # Seed the PR/thread ledger with the old head, then let the explicit
        # runner callback record the builder-owned push.
        from harbor.wake import observe_pr_facts

        observe_pr_facts(db, _ticket(), {"number": 6, "head_sha": "head-a", "threads": [_thread()]})
        assert record_push(db, "HAN-6", "head-b", pr_number=6) == 1
        assert db.connection.execute("SELECT head_sha FROM prs WHERE number = 6").fetchone()[0] == "head-b"
        assert db.connection.execute("SELECT seen_at_sha FROM review_threads WHERE id = 'review-1'").fetchone()[0] == "head-b"
        assert db.connection.execute("SELECT bot_rework_rounds FROM tickets WHERE identifier = 'HAN-6'").fetchone()[0] == 1
    finally:
        db.close()


def _connector_thread(index: int, *, outdated: bool = False, size: int = 820) -> dict:
    """A thread the size the real chatgpt-codex-connector actually posts."""

    return {
        "id": f"PRRT_kwDORf1dpc6d_IL{index}",
        "path": "packages/core/src/dictionary/index.ts",
        "line": None if outdated else 648,
        "isOutdated": outdated,
        "isResolved": False,
        "body": "**P2 Badge Preserve all readings** " + ("x" * size),
    }


def test_every_thread_reaches_the_builder_at_realistic_body_sizes() -> None:
    """AC-2 with live-sized bodies (~800 bytes each), not toy fixtures.

    A greedy per-body budget spends the whole 2 KB envelope on the first one
    or two threads.  With bot_rework_cap at 1 the dropped threads are never
    offered again, so a real P2 finding would be lost permanently.
    """

    from harbor.reengagement import (
        UNTRUSTED_CLOSE,
        UNTRUSTED_OPEN,
        build_resume_instruction,
    )

    threads = [_connector_thread(i, outdated=i % 2 == 0) for i in range(4)]
    text = build_resume_instruction(
        {"identifier": "HAN-29"},
        {"id": "run-1"},
        {"head_sha": "83c065d5", "ci": {"state": "green"}},
        threads,
    )

    assert len(text.encode("utf-8")) < 2048
    assert text.count(UNTRUSTED_OPEN) == text.count(UNTRUSTED_CLOSE) == len(threads)
    for thread in threads:
        assert thread["id"] in text, f"{thread['id']} was silently dropped"
    # The instruction must still say not to reload the skill.
    assert text.rstrip().endswith("do not load the skill again.")
    assert "/work-pipeline" not in text


def test_outdated_threads_carry_path_without_a_line() -> None:
    """AC-3: an outdated thread's line no longer exists; pass the path."""

    from harbor.reengagement import build_resume_instruction

    text = build_resume_instruction(
        {"identifier": "HAN-29"},
        {"id": "run-1"},
        {"head_sha": "abc"},
        [_connector_thread(0, outdated=True), _connector_thread(1, outdated=False)],
    )
    assert "path=packages/core/src/dictionary/index.ts; outdated: true" in text
    assert "line=648; outdated: false" in text
    assert "line=None" not in text


def test_many_threads_still_each_get_a_fenced_slot() -> None:
    from harbor.reengagement import UNTRUSTED_CLOSE, UNTRUSTED_OPEN, build_resume_instruction

    threads = [_connector_thread(i) for i in range(8)]
    text = build_resume_instruction({"identifier": "HAN-1"}, {"id": "r"}, {}, threads)
    assert len(text.encode("utf-8")) < 2048
    assert text.count(UNTRUSTED_OPEN) == text.count(UNTRUSTED_CLOSE) == 8
    for thread in threads:
        assert thread["id"] in text


def test_a_parked_pr_still_reengages_on_new_review_threads():
    """The plan's REENGAGE box is fed by the PARKED path as well as WAIT.

    A parked merge-ready PR keeps collecting connector threads.  With
    bot_rework_cap at 1, a thread that is never offered to the builder is a
    finding lost permanently.
    """

    from harbor.wake import WakeDecider

    ticket = {"identifier": "HAN-32", "state": "Human Review", "owned": True}
    facts = {
        "prs": [{"number": 225, "head_sha": "d64f0a33", "merged": False}],
        "head_sha": "d64f0a33",
        "merged": False,
        "threads": [{
            "id": "PRRT_kwDORf1dpc6ezhfV", "severity": "P2", "is_bot": True,
            "resolved": False, "outdated": False, "path": "README.md",
            "body": "**P2 Badge** Require a text-provider key in setup",
        }],
        "run": {"id": "harbor-Bookgen-HAN-32-builder", "waiting_on": "human"},
    }
    wake = WakeDecider(None, shadow=True).decide(ticket, facts)
    assert wake is not None, "a parked PR with a new P2 thread must re-engage"
    assert wake.action == "reengage"
    assert wake.resume == "harbor-Bookgen-HAN-32-builder", "the SAME session"


def test_a_ticket_waiting_on_nothing_relevant_does_not_reengage():
    from harbor.wake import WakeDecider

    ticket = {"identifier": "HAN-32", "state": "In Progress", "owned": True}
    facts = {
        "prs": [{"number": 225, "head_sha": "abc", "merged": False}],
        "head_sha": "abc",
        "threads": [{"id": "t1", "severity": "P2", "is_bot": True, "resolved": False, "body": "x"}],
        "run": {"id": "r", "waiting_on": "none"},
    }
    assert WakeDecider(None, shadow=True).decide(ticket, facts) is None


def test_a_builder_push_is_counted_from_harbors_own_observation():
    """AC-4: seen_at_sha moves and bot_rework_rounds becomes 1 after a push.

    FINAL_SCHEMA has no push field -- the builder literally cannot report one
    -- so the rework cap has to come from Harbor's polling.  The fixed point
    is *code changed*: an owned ticket's PR head moving while its session is
    retained is one rework round.
    """

    import asyncio
    import sqlite3

    from harbor.config.projects import ProjectConfig
    from harbor.poll.linear_poller import PollResult
    from harbor.supervisor import Supervisor

    class Ledger:
        def __init__(self) -> None:
            self.connection = sqlite3.connect(":memory:")
            self.connection.execute("CREATE TABLE prs (number INTEGER PRIMARY KEY, ticket TEXT, head_sha TEXT, observed_at TEXT)")
            self.connection.execute("CREATE TABLE tickets (identifier TEXT PRIMARY KEY, owned INTEGER, bot_rework_rounds INTEGER)")
            self.connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, ticket TEXT, status TEXT, session_id TEXT, waiting_on TEXT, worktree TEXT, started_at TEXT, finished_at TEXT)")
            self.connection.execute("INSERT INTO prs VALUES (225, 'HAN-32', 'oldhead', '')")
            self.connection.execute("INSERT INTO tickets VALUES ('HAN-32', 1, 0)")
            self.connection.execute("INSERT INTO runs VALUES ('r1', 'HAN-32', 'waiting', 's', 'human', '', '', '')")
            self.connection.commit()

        def set_runtime(self, *a, **k) -> None:
            return None

        def rounds(self) -> int:
            return self.connection.execute("SELECT bot_rework_rounds FROM tickets WHERE identifier='HAN-32'").fetchone()[0]

    class P:
        project_name = "Bookgen"
        config = ProjectConfig(name="Bookgen", linear_project_slug="s")
        async def poll(self):
            return PollResult((), "s", False, ())

    ledger = Ledger()
    supervisor = Supervisor(P(), db=ledger)
    ticket = {"identifier": "HAN-32", "owned": True}

    # An unchanged head is not a rework round.
    supervisor._record_observed_rework(ticket, {"prs": [{"number": 225, "head_sha": "oldhead"}]})
    assert ledger.rounds() == 0

    # A moved head is exactly one.
    supervisor._record_observed_rework(ticket, {"prs": [{"number": 225, "head_sha": "newhead"}]})
    assert ledger.rounds() == 1


def test_an_unowned_tickets_push_is_never_counted():
    """A pre-existing PR moving is somebody else's work, not a rework round."""

    import sqlite3

    from harbor.config.projects import ProjectConfig
    from harbor.poll.linear_poller import PollResult
    from harbor.supervisor import Supervisor

    class Ledger:
        def __init__(self) -> None:
            self.connection = sqlite3.connect(":memory:")
            self.connection.execute("CREATE TABLE prs (number INTEGER PRIMARY KEY, ticket TEXT, head_sha TEXT, observed_at TEXT)")
            self.connection.execute("CREATE TABLE tickets (identifier TEXT PRIMARY KEY, owned INTEGER, bot_rework_rounds INTEGER)")
            self.connection.execute("INSERT INTO prs VALUES (217, 'HAN-7', 'oldhead', '')")
            self.connection.execute("INSERT INTO tickets VALUES ('HAN-7', 0, 0)")
            self.connection.commit()

        def set_runtime(self, *a, **k) -> None:
            return None

    class P:
        project_name = "Bookgen"
        config = ProjectConfig(name="Bookgen", linear_project_slug="s")
        async def poll(self):
            return PollResult((), "s", False, ())

    ledger = Ledger()
    supervisor = Supervisor(P(), db=ledger)
    supervisor._record_observed_rework({"identifier": "HAN-7"}, {"prs": [{"number": 217, "head_sha": "newhead"}]})
    rounds = ledger.connection.execute("SELECT bot_rework_rounds FROM tickets WHERE identifier='HAN-7'").fetchone()[0]
    assert rounds == 0
