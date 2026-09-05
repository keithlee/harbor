from pathlib import Path


README = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")


def test_readme_explains_why_harbor_exists() -> None:
    assert "## Why Harbor exists" in README
    assert "Monitoring should be cheap" in README
    assert "Delivery policy belongs to the project" in README


def test_readme_documents_authority_and_architecture() -> None:
    assert "## Architecture" in README
    assert "```mermaid" in README
    assert 'subgraph harbor["Harbor control plane - model-free"]' in README
    assert "Harbor's GitHub client is query-only" in README
    assert "Linear and GitHub remain external authorities" in README


def test_readme_documents_linear_to_done_lifecycle() -> None:
    assert "## Example workflow: Linear to Done" in README
    assert "one idempotent build wake" in README
    assert "resumes the retained builder session" in README
    assert "records the merge receipt" in README


def test_readme_documents_model_usage_boundaries() -> None:
    assert "## Token efficiency" in README
    assert "| Linear/GitHub polling | None |" in README
    assert "| Builder | Yes |" in README
    assert "| First Mate | Yes, on demand |" in README
    assert "even when that Linear snapshot is unchanged" in README
    assert "cycles with no actionable wake do not invoke an LLM" in README
    assert "An unchanged cycle does not invoke a model" not in README
    assert "does not\nenforce a per-task token limit" in README


def test_readme_documents_supported_customization_points() -> None:
    assert "## What each developer can swap" in README
    for choice in (
        "Project identity",
        "Builder and First Mate models",
        "Delivery flow",
        "Supervision policy",
        "Execution harness",
        "Tracker",
    ):
        assert f"| {choice} |" in README


def test_readme_preserves_project_agnostic_manual_setup() -> None:
    assert "## Manual multi-project setup" in README
    assert 'export HARBOR_PROJECTS_FILE="$HOME/.harbor/projects.md"' in README
    assert "| SampleProject | ~/Projects/SampleProject" in README
    assert "| AnotherProject | ~/Projects/AnotherProject" in README
    assert "Run one Harbor process per named repository" in README
