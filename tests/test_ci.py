from harbor.facts.ci import classify_ci


def test_null_rollup_is_absent_even_with_successful_named_check() -> None:
    assert classify_ci(None, ["CI"]) == {"state": "absent", "required_seen": []}


def test_green_requires_every_named_check_and_success() -> None:
    rollup = {"state": "SUCCESS", "contexts": {"nodes": [
        {"name": "CI", "conclusion": "SUCCESS"},
        {"name": "typecheck", "conclusion": "SUCCESS"},
    ]}}
    assert classify_ci(rollup, ["CI", "typecheck"])["state"] == "green"


def test_missing_or_pending_required_check_is_not_green() -> None:
    rollup = {"state": "SUCCESS", "contexts": {"nodes": [{"name": "CI", "conclusion": "SUCCESS"}]}}
    result = classify_ci(rollup, ["CI", "e2e"])
    assert result["state"] == "pending"
    assert result["required_seen"] == ["CI"]


def test_failed_required_check_is_red() -> None:
    rollup = {"state": "FAILURE", "contexts": {"nodes": [{"name": "CI", "conclusion": "FAILURE"}]}}
    assert classify_ci(rollup, ["CI"])["state"] == "red"


def test_required_check_name_containing_a_comma_is_matched() -> None:
    """Bookgen's real required check is "Lint, typecheck and test (root)".

    Every config parser must split the list quote-aware.  A naive split makes
    one required check into two that can never both be SUCCESS, so CI reads
    `pending` forever and no wake ever fires on green.
    """

    from harbor.config.pipeline import _list
    from harbor.config.profile import _split_list as profile_split
    from harbor.config.projects import _as_list
    from harbor.facts.ci import classify_ci

    literal = '["Lint, typecheck and test (root)", "Test (apps/mobile)"]'
    expected = ("Lint, typecheck and test (root)", "Test (apps/mobile)")
    assert _as_list(literal, ()) == expected
    assert _list(literal) == expected
    assert profile_split(literal) == expected

    rollup = {
        "state": "SUCCESS",
        "contexts": {
            "nodes": [
                {"__typename": "CheckRun", "name": "Lint, typecheck and test (root)", "conclusion": "SUCCESS"},
                {"__typename": "CheckRun", "name": "Test (apps/mobile)", "conclusion": "SUCCESS"},
            ]
        },
    }
    assert classify_ci(rollup, required_checks=expected) == {
        "state": "green",
        "required_seen": list(expected),
    }


def test_the_github_query_selects_no_field_that_does_not_exist() -> None:
    """`isBot` is not a field on GitHub's User type; selecting it 400s the

    whole query, which recorded fixtures can never catch.
    """

    from harbor.clients.github import build_pull_requests_query

    query = build_pull_requests_query("keithlee", "Bookgen", [217, 221])
    assert "isBot" not in query
    # Bot detection uses __typename plus the `[bot]` login suffix instead.
    assert "__typename" in query
