"""Harbor reports which delivery tools work in the builder's sandbox.

Found the hard way: a builder abandoned an APPROVED merge after an unavailable
GitHub transport and an MCP call reported "approval policy is never" -- while
plain `gh` was authenticated and working in that very sandbox the whole time.

A non-interactive `codex exec` has no human to approve anything, so any MCP
tool that asks is refused. An agent that reaches only for MCP strands itself.
Which tools are usable is an environment fact Harbor knows and the builder does
not, so Harbor reports it -- the same kind of fact as CI state. Harbor's
mutation policy is to use `gh`, not third-party wrappers.
"""

from __future__ import annotations

import subprocess

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


def test_gh_without_a_token_or_stored_auth_is_unavailable(monkeypatch) -> None:
    """A present binary is insufficient when both auth mechanisms fail."""

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "subprocess.run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1)
    )

    with_token = detect_delivery_tools({"GH_TOKEN": "x"})
    assert with_token["gh"] is True

    without = detect_delivery_tools({})
    assert without["gh"] is False
    assert any("auth status failed" in name for name in without), "say WHY it is unusable"


def test_either_token_variable_counts() -> None:
    assert detect_delivery_tools({"GITHUB_TOKEN": "x"})["gh"] is True
    assert detect_delivery_tools({"GH_TOKEN": "x"})["gh"] is True


def test_gh_keyring_auth_counts_when_token_variables_are_absent(monkeypatch) -> None:
    """`gh auth status` can prove non-interactive auth stored outside env vars."""

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    observed: dict[str, object] = {}

    def authenticated(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("subprocess.run", authenticated)

    assert detect_delivery_tools({"PATH": "/usr/bin", "HOME": "/tmp/test-home"})["gh"] is True
    assert observed["command"] == ["/usr/bin/gh", "auth", "status"]
    assert observed["kwargs"]["timeout"] > 0
    assert observed["kwargs"]["stdout"] is subprocess.DEVNULL
    assert observed["kwargs"]["stderr"] is subprocess.DEVNULL


def test_gh_auth_probe_fails_closed_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def times_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("subprocess.run", times_out)

    tools = detect_delivery_tools({"PATH": "/usr/bin", "HOME": "/tmp/test-home"})
    assert tools["gh"] is False
    assert any("auth status failed" in name for name in tools)


def test_detection_is_read_only() -> None:
    """The fallback may inspect auth, but it must not issue mutations."""

    import inspect

    source = inspect.getsource(detect_delivery_tools)
    for forbidden in ("requests", "httpx", "urlopen", "Popen", " auth login", " pr "):
        assert forbidden not in source, f"detection must not {forbidden}"


def test_the_fact_reaches_the_build_instruction() -> None:
    text = build_instruction(
        {"identifier": "SAMPLE-32", "title": "t", "state": "Todo", "hold": False},
        delivery_tools={"gh": True, "gh-axi": False},
    )
    assert "Available and authenticated in your sandbox: gh." in text
    # It is a fact, not an order: Harbor still issues no merge instruction.
    assert "merge the pr" not in text.lower()
    assert "your own risk gate" in text.lower()


def test_the_instruction_is_unchanged_when_nothing_is_reported() -> None:
    plain = build_instruction({"identifier": "SAMPLE-1", "title": "t", "state": "Todo", "hold": True})
    assert "Available and authenticated" not in plain
