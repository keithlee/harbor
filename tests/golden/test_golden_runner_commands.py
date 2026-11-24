"""Golden native CLI argv for builders and First Mate.

The argv is Harbor's whole sandbox/permission contract with each provider, so
any flag added or dropped must be an explicit, reviewed snapshot change.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harbor.runner.base import RunnerError
from harbor.runner.claude import ClaudeRunner, claude_read_only, validate_claude
from harbor.runner.codex import CodexRunner

IMAGE = "mcr.microsoft.com/playwright@sha256:" + "b" * 64
SESSION = "01a1e206-64a0-70e2-ae18-183a99c4cb79"


def linked_worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    worktree = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "DEMO-1", str(worktree)], check=True)
    return worktree


def scrub(argv: list[str], tmp_path: Path, schema: Path | None = None) -> list[str]:
    """Replace machine-specific paths so the snapshot is identical on every host."""
    real = str(tmp_path.resolve())
    argv = [item.replace(real, "<tmp>").replace(str(tmp_path), "<tmp>") for item in argv]
    return ["<run-result schema>" if schema is not None and item == str(schema) else item for item in argv]


def claude_runner(tmp_path: Path, **extra) -> ClaudeRunner:
    worktree = linked_worktree(tmp_path)
    return ClaudeRunner(worktree, model="claude-opus-5-5", reasoning_effort="high", executable="claude",
                        writable_roots=(tmp_path / "state",), **extra)


def test_claude_builder_fresh_command(tmp_path: Path, golden) -> None:
    argv = scrub(claude_runner(tmp_path).command("ignored", run_id="harbor-run-1"), tmp_path)
    # Expand the embedded JSON so the snapshot diff is readable.
    argv = [json.loads(item) if item.startswith("{") else item for item in argv]
    golden.json("argv_claude_builder_fresh.json", argv)


def test_claude_builder_with_github_mcp_command(tmp_path: Path, golden) -> None:
    runner = claude_runner(tmp_path)
    runner.github_mcp = {"url": "https://api.githubcopilot.com/mcp/", "token_env": "GITHUB_TOKEN"}
    argv = scrub(runner.command("ignored", run_id="harbor-run-1"), tmp_path)
    argv = [json.loads(item) if item.startswith("{") else item for item in argv]
    golden.json("argv_claude_builder_github_mcp.json", argv)


def test_github_helper_settings_reach_the_builder_environment(tmp_path: Path) -> None:
    runner = claude_runner(tmp_path)
    runner.github_env = {"HARBOR_GITHUB_REPOSITORY": "owner/repo", "HARBOR_GITHUB_TOKEN_ENV": "GITHUB_TOKEN"}
    env = runner.environment()
    assert env["HARBOR_GITHUB_REPOSITORY"] == "owner/repo" and env["HARBOR_GITHUB_TOKEN_ENV"] == "GITHUB_TOKEN"


def test_claude_builder_sandbox_adds_configured_hosts_and_refuses_wildcards(tmp_path: Path) -> None:
    runner = claude_runner(tmp_path)
    runner.sandbox_hosts = ("ghe.example.test", "github.com")
    argv = runner.command("x", run_id="r")
    allowed = json.loads(argv[argv.index("--settings") + 1])["sandbox"]["network"]["allowedDomains"]
    assert allowed == ["github.com", "api.github.com", "api.linear.app", "registry.npmjs.org", "ghe.example.test"]
    runner.sandbox_hosts = ("*.example.test",)
    with pytest.raises(RunnerError, match="sandbox host"):
        runner.command("x", run_id="r")


def test_claude_builder_resume_uses_the_native_session(tmp_path: Path) -> None:
    argv = claude_runner(tmp_path).command("ignored", run_id=SESSION, resume=True)
    assert argv[-2:] == ["--resume", SESSION]


def test_claude_builder_resume_refuses_a_non_native_session_id(tmp_path: Path) -> None:
    with pytest.raises(RunnerError, match="retained native session ID"):
        claude_runner(tmp_path).command("ignored", run_id="harbor-run-1", resume=True)


def test_claude_builder_never_receives_the_instruction_on_argv(tmp_path: Path) -> None:
    secret_marker = "INSTRUCTION-BODY-MARKER"
    assert not any(secret_marker in item for item in claude_runner(tmp_path).command(secret_marker, run_id="r"))


def test_claude_builder_sandbox_can_write_the_linked_git_admin_dir(tmp_path: Path) -> None:
    argv = claude_runner(tmp_path).command("x", run_id="r")
    settings = json.loads(argv[argv.index("--settings") + 1])
    allowed = settings["sandbox"]["filesystem"]["allowWrite"]
    assert str((tmp_path / "repo" / ".git").resolve()) in allowed
    assert str((tmp_path / "repo").resolve()) not in allowed  # the operator checkout stays read-only


def test_first_mate_claude_read_only_command(golden) -> None:
    golden.json("argv_claude_first_mate.json", {
        "with_effort": claude_read_only("claude", "claude-sonnet-5-5", "medium"),
        "without_effort": claude_read_only("claude", "claude-sonnet-5-5", None),
    })


def test_codex_builder_default_sandbox_command(tmp_path: Path, golden) -> None:
    runner = CodexRunner(linked_worktree(tmp_path), model="gpt-5.6-luna", reasoning_effort="xhigh",
                         executable="codex", writable_roots=(tmp_path / "state",))
    golden.json("argv_codex_builder_default.json",
                scrub(runner.command("x", run_id="harbor-run-1"), tmp_path, runner.schema_path))


def test_codex_builder_playwright_permission_profile(tmp_path: Path, golden) -> None:
    runner = CodexRunner(linked_worktree(tmp_path), model="gpt-5.6-luna", reasoning_effort="xhigh",
                         executable="codex", writable_roots=(tmp_path / "state",), playwright_browser_image=IMAGE)
    argv = runner.command("x", run_id="harbor-run-1")
    assert "-s" not in argv  # the named permission profile replaces the legacy sandbox flag
    golden.json("argv_codex_builder_playwright.json", scrub(argv, tmp_path, runner.schema_path))


@pytest.mark.parametrize(
    ("model", "effort", "message"),
    [
        ("gpt-5.6-luna", "high", "configure a Claude Code model explicitly"),
        ("   ", "high", "configure a Claude Code model explicitly"),
        ("claude-opus-5-5", "ultra", "unsupported Claude Code reasoning effort"),
        ("claude-opus-5-5", "minimal", "unsupported Claude Code reasoning effort"),
    ],
)
def test_validate_claude_fails_closed(monkeypatch, model: str, effort: str, message: str) -> None:
    monkeypatch.setattr("harbor.runner.claude.shutil.which", lambda _: "/usr/bin/claude")
    with pytest.raises(ValueError, match=message):
        validate_claude("claude", model, effort)


def test_validate_claude_requires_the_executable(monkeypatch) -> None:
    monkeypatch.setattr("harbor.runner.claude.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="executable unavailable"):
        validate_claude("claude", "claude-opus-5-5", "high")
