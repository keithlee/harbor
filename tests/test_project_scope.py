"""Harbor polls exactly one Linear project unless told otherwise.

Team HAN spans Hanzireader, Messsucher and FirstMate, all pointing at
different repositories.  Scoping by team instead of project would dispatch a
Messsucher ticket into the Bookgen checkout, so the project filter is a safety
boundary, not a convenience.
"""

from __future__ import annotations

import asyncio
from typing import Any

from harbor.cli import _check_project_scope
from harbor.config.projects import ProjectConfig

PROJECTS = [
    {"id": "1", "name": "FirstMate", "slugId": "a146aad2cf37"},
    {"id": "2", "name": "Messsucher", "slugId": "729853ec4ffb"},
    {"id": "3", "name": "Hanzireader", "slugId": "06c91e632f50"},
]


class Client:
    async def list_projects(self) -> list[dict[str, Any]]:
        return list(PROJECTS)


def check(slug: str, extra: tuple[str, ...] = ()) -> bool:
    config = ProjectConfig(name="Bookgen", linear_project_slug=slug, linear_projects=extra)
    return asyncio.run(_check_project_scope(Client(), config))


def test_the_bare_slug_id_is_accepted() -> None:
    assert check("06c91e632f50") is True


def test_the_url_form_name_dash_slug_is_accepted() -> None:
    """Linear's own project URL uses this form, so operators will paste it."""

    assert check("hanzireader-06c91e632f50") is True


def test_a_typo_fails_doctor_instead_of_polling_an_empty_board() -> None:
    """A non-matching slug returns zero issues, never an error."""

    assert check("hanzireadr-typo") is False


def test_a_second_configured_project_is_also_validated() -> None:
    assert check("06c91e632f50", ("729853ec4ffb",)) is True
    assert check("06c91e632f50", ("not-a-project",)) is False


def test_the_configured_scope_excludes_the_sibling_projects() -> None:
    """The Bookgen scope must not resolve to Messsucher or FirstMate."""

    config = ProjectConfig(name="Bookgen", linear_project_slug="hanzireader-06c91e632f50")
    scope = config.linear_project_scope
    assert scope == ("hanzireader-06c91e632f50",)
    for foreign in ("729853ec4ffb", "a146aad2cf37"):
        assert foreign not in scope
        assert not any(foreign in slug for slug in scope)


def test_a_client_without_the_listing_seam_does_not_fail_doctor() -> None:
    class Old:
        pass

    config = ProjectConfig(name="Bookgen", linear_project_slug="06c91e632f50")
    assert asyncio.run(_check_project_scope(Old(), config)) is True
