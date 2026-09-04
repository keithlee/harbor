"""Harbor reports which delivery tools work in the builder's sandbox.

Found the hard way: a builder abandoned an APPROVED merge, reporting
"gh-axi returned api.github.com UNKNOWN" and "MCP tool call requires
approval, but approval policy is never" -- while plain `gh` was
authenticated and working in that very sandbox the whole time.

A non-interactive `codex exec` has no human to approve anything, so any MCP
tool that asks is refused. An agent that reaches only for MCP or a wrapper
strands itself. Which tools are usable is an environment fact Harbor knows
and the builder does not, so Harbor reports it -- the same kind of fact as
CI state. It says what IS, never what to do.
"""

from __future__ import annotations

from harbor.wake import build_instruction, delivery_tools_fact, detect_delivery_tools


def test_available_and_unavailable_tools_are_both_named() -> None:
    text = delivery_tools_fact({"gh": True, "git": True, "gh-axi": False})
    assert "Available and authenticated in your sandbox: gh, git." in text
    assert "Not usable in this run: gh-axi." in text


def test_the_non_interactive_approval_limit_is_stated() -> None:
    """The builder cannot discover this by trying; it just fails opaquely."""

    text = delivery_tools_fact({"gh": True})
    assert "non-interactive" in text
    assert "refused" in text
    assert "prefer a shell tool listed as available" in text


def test_no_configured_tools_adds_nothing() -> None:
    assert delivery_tools_fact({}) == ""
    assert delivery_tools_fact(None) == ""


def test_gh_needs_a_token_to_count_as_available() -> None:
    """`gh` without a token prompts, and a non-interactive run cannot answer."""

    with_token = detect_delivery_tools({"GH_TOKEN": "x"})
    assert with_token["gh"] is True

    without = detect_delivery_tools({})
    assert without["gh"] is False
    assert any("GH_TOKEN" in name for name in without), "say WHY it is unusable"


def test_either_token_variable_counts() -> None:
    assert detect_delivery_tools({"GITHUB_TOKEN": "x"})["gh"] is True
    assert detect_delivery_tools({"GH_TOKEN": "x"})["gh"] is True


def test_detection_is_read_only() -> None:
    """It must be a PATH lookup and a token check -- never a network call."""

    import inspect

    source = inspect.getsource(detect_delivery_tools)
    for forbidden in ("requests", "httpx", "urlopen", "subprocess", "run(", "Popen"):
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
