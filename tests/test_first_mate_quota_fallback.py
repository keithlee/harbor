import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from harbor.db import HarborDB
from harbor.first_mate import FirstMate, FirstMateError, FirstMateQuotaError
from harbor.quota_fallback import Profile

CODEX_FALLBACK = Profile("codex", "gpt-6-luna", "medium")
CLAUDE_FALLBACK = Profile("claude", "claude-haiku-5-5", "medium")


class Runner:
    """Records commands; returns text, or raises when told to."""

    def __init__(self, text="answer", quota=None, error=None):
        self.text, self.quota, self.error, self.commands = text, quota, error, []

    async def __call__(self, command, prompt):
        self.commands.append(list(command))
        if self.quota is not None:
            raise FirstMateQuotaError(*self.quota)
        if self.error is not None:
            raise self.error
        return self.text


def mate(primary, fallback_runner=None, fallback=CODEX_FALLBACK, **kwargs):
    return FirstMate(runner=primary, model="claude-opus-5-5", fallback=fallback, fallback_runner=fallback_runner, **kwargs)


def triage(first_mate, prompt="p"):
    return asyncio.run(first_mate.triage(prompt))


def test_falls_back_on_primary_quota():
    primary, fallback = Runner(quota=(None, "codex")), Runner("fallback answer")
    assert triage(mate(primary, fallback)) == "fallback answer"
    assert len(primary.commands) == 1 and len(fallback.commands) == 1
    assert "gpt-6-luna" in fallback.commands[0] and "codex" in fallback.commands[0][0]


def test_skips_primary_during_wait_then_returns_after_reset(monkeypatch):
    now = time.time()
    primary, fallback = Runner(quota=(now + 100, "codex")), Runner("fallback answer")
    first_mate = mate(primary, fallback)
    assert triage(first_mate) == "fallback answer"
    assert triage(first_mate) == "fallback answer"
    assert len(primary.commands) == 1  # the recorded wait skips the exhausted provider
    primary.quota, primary.text = None, "primary answer"
    monkeypatch.setattr("harbor.first_mate.time.time", lambda: now + 200)
    assert triage(first_mate) == "primary answer"
    assert len(primary.commands) == 2


def test_both_exhausted_raises_quota_error_and_then_waits_without_calling():
    now = time.time()
    primary = Runner(quota=(now + 100, "codex"))
    fallback = Runner(quota=(now + 100, "claude"))
    first_mate = mate(primary, fallback)
    with pytest.raises(FirstMateQuotaError):
        triage(first_mate)
    with pytest.raises(FirstMateQuotaError):
        triage(first_mate)
    assert len(primary.commands) == 1 and len(fallback.commands) == 1


def test_no_fallback_configured_raises_as_before():
    primary = Runner(quota=(None, "codex"))
    with pytest.raises(FirstMateQuotaError):
        triage(mate(primary, fallback=None))


def test_non_quota_errors_do_not_use_the_fallback():
    primary, fallback = Runner(error=FirstMateError("boom")), Runner("fallback answer")
    with pytest.raises(FirstMateError, match="boom"):
        triage(mate(primary, fallback))
    assert fallback.commands == []


def test_claude_fallback_reads_the_stream_result(monkeypatch):
    monkeypatch.setenv("HARBOR_CLAUDE_EXECUTABLE", "/usr/bin/true")
    stream = "\n".join(json.dumps(event) for event in (
        {"type": "system", "subtype": "init"},
        {"type": "result", "is_error": False, "result": "claude answer"},
    ))
    primary, fallback = Runner(quota=(None, "codex")), Runner(stream)
    assert triage(mate(primary, fallback, fallback=CLAUDE_FALLBACK)) == "claude answer"
    assert "claude-haiku-5-5" in fallback.commands[0]


def test_claude_fallback_quota_event_raises_quota_error(monkeypatch):
    monkeypatch.setenv("HARBOR_CLAUDE_EXECUTABLE", "/usr/bin/true")
    stream = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": time.time() + 500}})
    primary, fallback = Runner(quota=(None, "codex")), Runner(stream)
    with pytest.raises(FirstMateQuotaError):
        triage(mate(primary, fallback, fallback=CLAUDE_FALLBACK))


def test_receipt_written_once_per_switch_for_usage_limit(tmp_path):
    db = HarborDB(tmp_path / "harbor.db")
    source = SimpleNamespace(db=db, project="mock")
    primary, fallback = Runner(quota=(None, "codex")), Runner("fallback answer")
    first_mate = mate(primary, fallback, status_source=source)
    triage(first_mate)
    triage(first_mate)
    rows = db.connection.execute("SELECT payload FROM workflow_receipts WHERE kind='model_fallback'").fetchall()
    payloads = [json.loads(row[0]) for row in rows]
    assert len(payloads) == 1
    assert payloads[0]["reason"] == "usage_limit" and payloads[0]["selected"] == "codex:gpt-6-luna"
    db.close()
