import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from harbor.config.workflow import load_models
from harbor.quota_fallback import FallbackPolicy, Profile, default_readiness, handoff_text, policy_from_config
from harbor.recovery import record_provider_quota

CONFIG = """
[controller]
model = "claude-sonnet-5-5"
reasoning_effort = "medium"
[planner]
model = "claude-opus-5-5"
[first_mate]
model = "claude-haiku-5-5"
[quota_fallback.controller]
provider = "codex"
model = "gpt-6.1-terra"
reasoning_effort = "medium"
[quota_fallback.planner]
model = "gpt-6.2-sol"
[quota_fallback.first_mate]
provider = "codex"
model = "gpt-6-luna"
"""
PROJECT = SimpleNamespace(builder_provider="claude", firstmate_provider="claude")
CLAUDE = Profile("claude", "claude-sonnet-5-5", "medium")
CODEX = Profile("codex", "gpt-6.1-terra", "medium")


def policy(tmp_path, text=CONFIG, check=lambda profile: None, project=PROJECT):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return policy_from_config(project, load_models(path), check=check)


def test_policy_none_without_section(tmp_path):
    assert policy(tmp_path, '[controller]\nmodel = "claude-sonnet-5-5"\n') is None


def test_builder_target_is_other_profile_both_directions(tmp_path):
    p = policy(tmp_path)
    assert p.builder == (CLAUDE, CODEX)
    assert p.builder_target("claude") == CODEX
    assert p.builder_target("codex") == CLAUDE
    assert p.builder_target("other") is None
    assert p.mate == (Profile("claude", "claude-haiku-5-5", None), Profile("codex", "gpt-6-luna", None))
    assert p.mate_target("claude").provider == "codex"
    assert p.mate_target("codex").provider == "claude"


def test_roles_follow_the_provider(tmp_path):
    p = policy(tmp_path)
    assert p.roles_for("codex")["planner"]["model"] == "gpt-6.2-sol"
    assert p.roles_for("codex")["implementor"]["model"] == "gpt-6.1-terra"
    assert p.roles_for("claude")["planner"]["model"] == "claude-opus-5-5"


def test_same_provider_rejected(tmp_path):
    with pytest.raises(ValueError, match="differ"):
        policy(tmp_path, project=SimpleNamespace(builder_provider="codex", firstmate_provider="claude"))
    with pytest.raises(ValueError, match="differ"):
        policy(tmp_path, project=SimpleNamespace(builder_provider="claude", firstmate_provider="codex"))


def test_builder_only_policy_has_no_mate(tmp_path):
    p = policy(tmp_path, CONFIG.split("[quota_fallback.first_mate]")[0])
    assert p.mate is None and p.mate_target("claude") is None


def test_starting_profile_uses_fallback_only_when_primary_exhausted(tmp_path):
    p = policy(tmp_path)
    db = sqlite3.connect(":memory:")
    assert p.starting_builder_profile(db, 100) is None
    record_provider_quota(db, "claude", 500, 100)
    assert p.starting_builder_profile(db, 100) == CODEX
    assert p.starting_builder_profile(db, 501) is None  # primary is back


def test_starting_profile_none_when_both_exhausted(tmp_path):
    p = policy(tmp_path)
    db = sqlite3.connect(":memory:")
    record_provider_quota(db, "claude", 500, 100)
    record_provider_quota(db, "codex", 500, 100)
    assert p.starting_builder_profile(db, 100) is None


def test_starting_profile_none_when_fallback_not_ready(tmp_path):
    p = policy(tmp_path, check=lambda profile: "not logged in")
    db = sqlite3.connect(":memory:")
    record_provider_quota(db, "claude", 500, 100)
    assert p.starting_builder_profile(db, 100) is None


def test_policy_is_a_frozen_dataclass(tmp_path):
    assert isinstance(policy(tmp_path), FallbackPolicy)
    with pytest.raises(Exception):
        policy(tmp_path).max_switches = 9


def fake_claude(monkeypatch, *, logged_in=True, installed=True, broken=False):
    monkeypatch.setattr("harbor.quota_fallback.shutil.which", lambda exe: "/bin/x" if installed else None)

    def run(command, **kwargs):
        if broken:
            raise subprocess.TimeoutExpired(command, 10)
        return SimpleNamespace(stdout=json.dumps({"loggedIn": logged_in}), returncode=0)

    monkeypatch.setattr("harbor.quota_fallback.subprocess.run", run)


def test_readiness_reasons(monkeypatch):
    fake_claude(monkeypatch)
    assert default_readiness(CLAUDE) is None
    assert default_readiness(CODEX) is None
    assert "playwright" in default_readiness(CLAUDE, playwright_image="img").lower()
    assert default_readiness(CODEX, playwright_image="img") is None
    fake_claude(monkeypatch, logged_in=False)
    assert "not logged in" in default_readiness(CLAUDE)
    fake_claude(monkeypatch, broken=True)
    assert "login" in default_readiness(CLAUDE)
    fake_claude(monkeypatch, installed=False)
    assert "not found" in default_readiness(CLAUDE)
    assert "not found" in default_readiness(CODEX)


def test_handoff_contains_queued_answer_and_role_override_and_no_new_worktree():
    text = handoff_text(
        ticket="HAN-87", run_id="run-1", worktree="/wt/HAN-87", source="codex", target=CLAUDE,
        summary="Provider usage limit reached", pr_urls=["https://github.com/o/r/pull/295"],
        open_questions=["Can an iOS simulator be controlled?"],
        answers=["Use Maestro at ~/.maestro/bin/maestro"],
        roles={"planner": {"model": "claude-opus-5-5", "reasoning_effort": "medium"}},
    )
    assert "HAN-87" in text and "/wt/HAN-87" in text
    assert "Use Maestro at ~/.maestro/bin/maestro" in text
    assert "Can an iOS simulator be controlled?" in text
    assert "https://github.com/o/r/pull/295" in text
    assert "claude-opus-5-5" in text and "override" in text
    assert "Do not create another worktree" in text
    assert "codex" in text and "claude-sonnet-5-5" in text
