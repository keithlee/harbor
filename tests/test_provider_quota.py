import sqlite3

import pytest

from harbor.recovery import clear_provider_quota, provider_exhausted, record_provider_quota
from harbor.runner.quota import bind_runner_identity, rebind_runner_identity, runner_identity


def test_record_then_exhausted_until_reset():
    db = sqlite3.connect(":memory:")
    record_provider_quota(db, "codex", 500, 100)
    assert provider_exhausted(db, "codex", 100) == 500
    assert provider_exhausted(db, "codex", 499) == 500
    assert provider_exhausted(db, "claude", 100) is None


def test_unconfirmed_uses_fifteen_minute_backoff():
    db = sqlite3.connect(":memory:")
    record_provider_quota(db, "codex", None, 100)
    assert provider_exhausted(db, "codex", 100) == 1000
    record_provider_quota(db, "codex", 99, 100)  # reset already in the past
    assert provider_exhausted(db, "codex", 100) == 1000


def test_expires_after_reset():
    db = sqlite3.connect(":memory:")
    record_provider_quota(db, "codex", 500, 100)
    assert provider_exhausted(db, "codex", 500) is None
    assert provider_exhausted(db, "codex", 501) is None


def test_clear_removes_state():
    db = sqlite3.connect(":memory:")
    record_provider_quota(db, "codex", 500, 100)
    clear_provider_quota(db, "codex")
    assert provider_exhausted(db, "codex", 100) is None
    clear_provider_quota(db, "codex")  # idempotent


def test_later_observation_replaces_earlier():
    db = sqlite3.connect(":memory:")
    record_provider_quota(db, "codex", 500, 100)
    record_provider_quota(db, "codex", 900, 200)
    assert provider_exhausted(db, "codex", 300) == 900


def test_rebind_changes_provider_and_bind_then_accepts_it():
    db = sqlite3.connect(":memory:")
    bind_runner_identity(db, "run", "codex", "gpt-6.1-terra", "medium")
    with pytest.raises(ValueError):
        bind_runner_identity(db, "run", "claude", "claude-sonnet-5-5", "medium")
    rebind_runner_identity(db, "run", "claude", "claude-sonnet-5-5", "medium")
    assert tuple(runner_identity(db, "run")) == ("claude", "claude-sonnet-5-5", "medium")
    bind_runner_identity(db, "run", "claude", "claude-sonnet-5-5", "medium")  # no longer a mismatch
    with pytest.raises(ValueError):
        bind_runner_identity(db, "run", "codex", "gpt-6.1-terra", "medium")


def test_rebind_creates_identity_when_missing():
    db = sqlite3.connect(":memory:")
    rebind_runner_identity(db, "fresh", "codex", "gpt-6.1-terra", "high")
    assert tuple(runner_identity(db, "fresh")) == ("codex", "gpt-6.1-terra", "high")
