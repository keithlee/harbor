from harbor.config.profile import PipelineProfile


def test_profile_accepts_pipeline_table_and_scalar_forms() -> None:
    profile = PipelineProfile.from_markdown(
        """
        | github_repository | keithlee/Bookgen |
        required_checks: [CI, typecheck]
        bot_rework_cap: 1
        """
    )
    assert profile.repository == "keithlee/Bookgen"
    assert profile.required_checks == ("CI", "typecheck")
    assert profile.bot_rework_cap == 1


def test_required_check_names_may_contain_commas() -> None:
    """Bookgen's real check is literally "Lint, typecheck and test (root)".

    A naive split turns one required check into two that can never both be
    SUCCESS, so CI would never be reported green.
    """

    from harbor.config.projects import _split_list

    assert _split_list('["Lint, typecheck and test (root)", "Test (apps/mobile)"]') == (
        "Lint, typecheck and test (root)",
        "Test (apps/mobile)",
    )
    assert _split_list("[Todo, In Progress]") == ("Todo", "In Progress")
    assert _split_list("[]") == ()
    assert _split_list("[Todo]") == ("Todo",)


def test_live_bookgen_pipeline_parses_when_present() -> None:
    """Guards the checked-in registry against a config that cannot load."""

    from pathlib import Path

    from harbor.config.projects import get_project

    if not (Path.home() / "Projects" / "Bookgen" / ".harbor" / "pipeline.md").is_file():
        import pytest

        pytest.skip("live Bookgen pipeline.md is not installed on this machine")
    config = get_project("Bookgen")
    assert config.dispatch_states == ("Todo",)
    assert "Backlog" not in config.poll_states
    # hold is an operational toggle the operator flips; only assert it parses
    # as a bool, never a particular value.
    assert isinstance(config.hold_all, bool)
    assert config.hold_all == config.hold
    assert config.required_checks == (
        "Lint, typecheck and test (root)",
        "Test (apps/mobile)",
    )
