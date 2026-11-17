"""Harbor polls exactly one Linear project unless told otherwise.

Team SAMPLE spans SampleProject, OtherProject and ThirdProject, all pointing at
different repositories.  Scoping by team instead of project would dispatch a
OtherProject ticket into the SampleProject checkout, so the project filter is a safety
boundary, not a convenience.
"""

from __future__ import annotations

import asyncio
from typing import Any

from harbor.cli import _check_project_scope
from harbor.config.projects import ProjectConfig

PROJECTS = [
    {"id": "1", "name": "ThirdProject", "slugId": "ccc333ccc333"},
    {"id": "2", "name": "OtherProject", "slugId": "bbb222bbb222"},
    {"id": "3", "name": "SampleProject", "slugId": "aaa111aaa111"},
]


class Client:
    async def list_projects(self) -> list[dict[str, Any]]:
        return list(PROJECTS)


def check(slug: str, extra: tuple[str, ...] = ()) -> bool:
    config = ProjectConfig(name="SampleProject", linear_project_slug=slug, linear_projects=extra)
    return asyncio.run(_check_project_scope(Client(), config))


def test_the_bare_slug_id_is_accepted() -> None:
    assert check("aaa111aaa111") is True


def test_the_url_form_name_dash_slug_is_accepted() -> None:
    """Linear's own project URL uses this form, so operators will paste it."""

    assert check("sampleproject-aaa111aaa111") is True


def test_a_typo_fails_doctor_instead_of_polling_an_empty_board() -> None:
    """A non-matching slug returns zero issues, never an error."""

    assert check("sampleprojct-typo") is False


def test_a_second_configured_project_is_also_validated() -> None:
    assert check("aaa111aaa111", ("bbb222bbb222",)) is True
    assert check("aaa111aaa111", ("not-a-project",)) is False


def test_the_configured_scope_excludes_the_sibling_projects() -> None:
    """The SampleProject scope must not resolve to OtherProject or ThirdProject."""

    config = ProjectConfig(name="SampleProject", linear_project_slug="sampleproject-aaa111aaa111")
    scope = config.linear_project_scope
    assert scope == ("sampleproject-aaa111aaa111",)
    for foreign in ("bbb222bbb222", "ccc333ccc333"):
        assert foreign not in scope
        assert not any(foreign in slug for slug in scope)


def test_a_client_without_the_listing_seam_does_not_fail_doctor() -> None:
    class Old:
        pass

    config = ProjectConfig(name="SampleProject", linear_project_slug="aaa111aaa111")
    assert asyncio.run(_check_project_scope(Old(), config)) is True
