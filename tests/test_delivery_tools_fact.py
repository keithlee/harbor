"""Harbor reports which delivery tools work in the builder's sandbox.

Found the hard way: a builder abandoned an APPROVED merge after an unavailable
GitHub transport and an MCP call reported "approval policy is never" -- while
plain `gh` was authenticated and working in that very sandbox the whole time.

A non-interactive `codex exec` has no human to approve anything, so any MCP
tool that asks is refused. An agent that reaches only for MCP strands itself.
Which tools are usable is an environment fact Harbor knows and the builder does
not, so Harbor reports it -- the same kind of fact as CI state. Harbor verifies
the CLI with `gh auth status`, and its mutation policy is to use `gh`, not
third-party wrappers.
"""

from __future__ import annotations

import harbor.wake as wake
from harbor.wake import build_instruction, delivery_tools_fact, detect_delivery_tools


def test_available_and_unavailable_tools_are_both_named() -> None:
    text = delivery_tools_fact({"gh": True, "git": True, "other-tool": False})
    assert "Available and authenticated in your sandbox: gh, git." in text
    assert "Not usable in this run: other-tool." in text


def test_the_non_interactive_approval_limit_is_stated() -> None:
    """The builder cannot discover this by trying; it just fails opaquely."""

    text = delivery_tools_fact({"gh": True})
    assert "non-interactive" in text
    assert "refused" in text
    assert "use the available `gh` CLI" in text
    assert "do not use gh-axi" in text


def test_the_instruction_reports_a_delivery_blocker_when_gh_is_unavailable() -> None:
    text = delivery_tools_fact({"gh": False})
    assert "Not usable in this run: gh." in text
    assert "no GitHub mutation transport is available" in text
    assert "use the available `gh` CLI" not in text


def test_no_configured_tools_adds_nothing() -> None:
    assert delivery_tools_fact({}) == ""
    assert delivery_tools_fact(None) == ""


def test_stored_gh_auth_counts_as_available(monkeypatch) -> None:
    monkeypatch.setattr(wake.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    calls = []

    def fake_auth_status(path, env):
        calls.append((path, env))
        return True

    monkeypatch.setattr(wake, "_gh_auth_status", fake_auth_status)

    assert detect_delivery_tools({})["gh"] is True
    assert calls == [("/usr/bin/gh", {})]


def test_gh_auth_status_command_is_exact(monkeypatch) -> None:
    monkeypatch.setattr(wake.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    seen = {}

    class Completed:
        returncode = 0

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(wake.subprocess, "run", fake_run)

    assert detect_delivery_tools({"GH_TOKEN": "x"})["gh"] is True
    assert seen["args"] == ["/usr/bin/gh", "auth", "status", "--hostname", "github.com"]


def test_gh_token_is_forwarded_to_auth_status(monkeypatch) -> None:
    monkeypatch.setattr(wake.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    seen = {}

    def fake_auth_status(path, env):
        seen["env"] = env
        return True

    monkeypatch.setattr(wake, "_gh_auth_status", fake_auth_status)

    assert detect_delivery_tools({"GH_TOKEN": "secret"})["gh"] is True
    assert seen["env"]["GH_TOKEN"] == "secret"


def test_failed_gh_auth_status_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(wake.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    monkeypatch.setattr(wake, "_gh_auth_status", lambda path, env: False)

    result = detect_delivery_tools({})

    assert result["gh"] is False
    assert "gh (auth status failed)" in result


def test_validator_accepts_github_transport_blocker() -> None:
    from harbor.runner.base import validate_result

    assert validate_result(
        {
            "status": "blocked",
            "outcome": "parked",
            "waiting_on": "human",
            "pr_url": None,
            "summary": "GitHub auth is unavailable",
            "next_action": "Authenticate gh",
            "blocker": {
                "kind": "github_transport",
                "question": "Can GitHub auth be restored?",
                "options": ["Authenticate gh"],
                "recommendation": "Authenticate gh",
                "evidence": "gh auth status failed",
            },
        }
    ) == []


def test_the_fact_reaches_the_build_instruction() -> None:
    text = build_instruction(
        {"identifier": "SAMPLE-32", "title": "t", "state": "Todo", "hold": False},
        delivery_tools={"gh": True, "other-tool": False},
    )
    assert "Available and authenticated in your sandbox: gh." in text
    # It is a fact, not an order: Harbor still issues no merge instruction.
    assert "merge the pr" not in text.lower()
    assert "your own risk gate" in text.lower()


def test_the_instruction_is_unchanged_when_nothing_is_reported() -> None:
    plain = build_instruction(
        {"identifier": "SAMPLE-1", "title": "t", "state": "Todo", "hold": True}
    )
    assert "Available and authenticated" not in plain
