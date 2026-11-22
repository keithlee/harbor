import json

import pytest

from harbor.runner.claude import ClaudeRunner


def build(tmp_path, monkeypatch, sandbox=None, allow=None):
    for name, value in (("HARBOR_CLAUDE_SANDBOX", sandbox), ("HARBOR_CLAUDE_ALLOW_BASH", allow)):
        monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
    runner = ClaudeRunner(tmp_path, model="claude-sonnet-5-5", reasoning_effort="medium", executable="claude")
    command = runner.command("go", run_id="run")
    return command, json.loads(command[command.index("--settings") + 1])


def test_default_keeps_the_os_sandbox_and_nothing_else(tmp_path, monkeypatch):
    command, settings = build(tmp_path, monkeypatch)
    assert settings["sandbox"]["enabled"] is True and settings["sandbox"]["failIfUnavailable"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert "permissions" not in settings and "--append-system-prompt" not in command
    assert command[command.index("--tools") + 1] == "Bash,Read,Glob,Grep,Skill"  # no edit tools in the default mode


def test_allowlist_without_turning_the_sandbox_off_changes_nothing(tmp_path, monkeypatch):
    command, settings = build(tmp_path, monkeypatch, allow="git,gh")
    assert settings["sandbox"]["enabled"] is True and "permissions" not in settings


def test_sandbox_off_swaps_it_for_an_explicit_allowlist(tmp_path, monkeypatch):
    command, settings = build(tmp_path, monkeypatch, sandbox="off", allow=" git, gh ,/usr/bin/env HARBOR_PROJECTS_FILE=,, ")
    assert settings["sandbox"] == {"enabled": False}
    assert settings["permissions"]["allow"] == [
        "Bash(git:*)", "Bash(gh:*)", "Bash(/usr/bin/env HARBOR_PROJECTS_FILE=:*)", "Edit(./**)"]  # edits only inside the worktree
    tools = command[command.index("--tools") + 1].split(",")
    assert {"Edit", "Write"} <= set(tools)
    note = command[command.index("--append-system-prompt") + 1]
    assert "gh" in note and "git" in note and "denied" in note
    assert command[command.index("--permission-mode") + 1] == "dontAsk"  # anything not listed is denied


def test_sandbox_off_without_an_allowlist_is_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="HARBOR_CLAUDE_ALLOW_BASH"):
        build(tmp_path, monkeypatch, sandbox="off")
    with pytest.raises(ValueError, match="HARBOR_CLAUDE_ALLOW_BASH"):
        build(tmp_path, monkeypatch, sandbox="off", allow=" , ")


@pytest.mark.parametrize("value", ["false", "OFF ", "1", "yes"])
def test_only_off_is_a_valid_sandbox_value(tmp_path, monkeypatch, value):
    if value.strip().lower() == "off":
        build(tmp_path, monkeypatch, sandbox=value, allow="git")  # tolerant of case and spaces
    else:
        with pytest.raises(ValueError, match="HARBOR_CLAUDE_SANDBOX"):
            build(tmp_path, monkeypatch, sandbox=value, allow="git")


@pytest.mark.parametrize("bad", ["gh;rm -rf /", "$(whoami)", "gh|sh", "gh*", "gh:*", "git`id`", "a\nb"])
def test_allowlist_entries_cannot_smuggle_shell_or_rule_syntax(tmp_path, monkeypatch, bad):
    with pytest.raises(ValueError, match="HARBOR_CLAUDE_ALLOW_BASH"):
        build(tmp_path, monkeypatch, sandbox="off", allow=bad)
