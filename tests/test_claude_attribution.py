import json

import pytest

from harbor.runner import claude
from harbor.runner.claude import ClaudeRunner, validate_claude


def settings(tmp_path, monkeypatch, attribution=None):
    if attribution is None:
        monkeypatch.delenv("HARBOR_CLAUDE_ATTRIBUTION", raising=False)
    else:
        monkeypatch.setenv("HARBOR_CLAUDE_ATTRIBUTION", attribution)
    command = ClaudeRunner(tmp_path, model="claude-sonnet-5-5", reasoning_effort="medium", executable="claude").command("go", run_id="run")
    return json.loads(command[command.index("--settings") + 1])


@pytest.mark.parametrize("attribution", [None, "on"])
def test_default_leaves_claude_attribution_alone(tmp_path, monkeypatch, attribution):
    s = settings(tmp_path, monkeypatch, attribution)
    assert "attribution" not in s and "includeCoAuthoredBy" not in s


@pytest.mark.parametrize("attribution", ["off", " OFF "])
def test_off_strips_commit_and_pr_attribution_and_keeps_the_sandbox(tmp_path, monkeypatch, attribution):
    s = settings(tmp_path, monkeypatch, attribution)
    assert s["attribution"] == {"commit": "", "pr": ""}
    assert s["sandbox"]["enabled"] is True and s["sandbox"]["failIfUnavailable"] is True


def test_only_off_on_or_unset_is_valid(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="HARBOR_CLAUDE_ATTRIBUTION"):
        settings(tmp_path, monkeypatch, attribution="no")


def test_invalid_value_fails_startup_validation(monkeypatch):
    monkeypatch.setattr(claude.shutil, "which", lambda executable: "/usr/bin/" + executable)
    monkeypatch.setenv("HARBOR_CLAUDE_ATTRIBUTION", "bogus")
    with pytest.raises(ValueError, match="HARBOR_CLAUDE_ATTRIBUTION"):
        validate_claude("claude", "claude-sonnet-5-5", "medium")
    monkeypatch.setenv("HARBOR_CLAUDE_ATTRIBUTION", "off")
    validate_claude("claude", "claude-sonnet-5-5", "medium")
