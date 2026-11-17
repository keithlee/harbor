"""Resolve equivalent configured project identifiers against authoritative data."""
import re


def resolve(scope, projects):
    result = []
    for identifier in scope:
        matches = []
        for project in projects:
            slug, name = project.get("slugId"), project.get("name", "")
            aliases = {slug, project.get("id"), f"{name.casefold()}-{slug}", f"{re.sub(r'[^a-z0-9]+', '-', name.casefold()).strip('-')}-{slug}"}
            if identifier in aliases and slug:
                matches.append(slug)
        if len(set(matches)) != 1:
            raise ValueError(f"unknown or ambiguous Linear project: {identifier}")
        result.extend(matches)
    return tuple(dict.fromkeys(result))


async def require_scope(client, scope, observed):
    slug = observed.get("slugId")
    if slug and slug in scope:
        return
    lister = getattr(client, "list_projects", None)
    if not callable(lister) or slug not in resolve(scope, await lister()):
        raise ValueError("ticket left the configured Linear project scope")
