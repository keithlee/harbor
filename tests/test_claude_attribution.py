import json

import pytest

from harbor.runner.claude import ClaudeRunner


def settings(tmp_path, monkeypatch, attribution=None, sandbox=None):
    for name, value in (("HARBOR_CLAUDE_ATTRIBUTION", attribution), ("HARBOR_CLAUDE_SANDBOX", sandbox)):
        monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
    monkeypatch.setenv("HARBOR_CLAUDE_ALLOW_BASH", "git")
    command = ClaudeRunner(tmp_path, model="claude-sonnet-5-5", reasoning_effort="medium", executable="claude").command("go", run_id="run")
    return json.loads(command[command.index("--settings") + 1])


def test_default_leaves_claude_attribution_alone(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    assert "attribution" not in s and "includeCoAuthoredBy" not in s


@pytest.mark.parametrize("sandbox", [None, "off"])
def test_off_strips_commit_and_pr_attribution_in_every_sandbox_mode(tmp_path, monkeypatch, sandbox):
    s = settings(tmp_path, monkeypatch, attribution="off", sandbox=sandbox)
    assert s["attribution"] == {"commit": "", "pr": ""} and s["includeCoAuthoredBy"] is False


def test_only_off_or_unset_is_valid(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="HARBOR_CLAUDE_ATTRIBUTION"):
        settings(tmp_path, monkeypatch, attribution="no")
