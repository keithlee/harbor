import json
from pathlib import Path

from harbor.facts.threads import parse_severity, parse_threads


def fixture() -> dict:
    path = Path(__file__).parent / "fixtures" / "github_prs.json"
    return json.loads(path.read_text())


def test_badge_severity_does_not_use_arbitrary_prose() -> None:
    assert parse_severity("P2 is worth considering") is None
    assert parse_severity("![P2](https://img.shields.io/badge/P2-yellow)") == "P2"


def test_threads_preserve_outdated_path_but_clear_line_and_identify_bot() -> None:
    nodes = fixture()["data"]["repository"]["pr_221"]["reviewThreads"]
    result = parse_threads(nodes)
    assert [item["severity"] for item in result] == ["P1", "P2", "P2", "P2"]
    assert sum(item["outdated"] for item in result) == 3
    assert all(item["is_bot"] for item in result)
    assert result[0]["path"] == "docs/specs/proof/sample-29/live-web.md"
    assert result[0]["line"] is None
    assert result[1]["line"] == 648
