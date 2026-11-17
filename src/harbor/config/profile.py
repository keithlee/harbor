"""Configuration needed by Harbor's read-only GitHub facts poller.

The delivery skill owns policy and merge decisions.  This module only parses the
small amount of repository metadata that Harbor needs to make a GitHub read:
the repository, the names of required checks, and the review rework cap.  The
parser intentionally accepts both the ``key: value`` notation used by the
pipeline document and its Markdown-table equivalent.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


def _split_list(value: str | Sequence[str] | None) -> tuple[str, ...]:
    """Split a data-only list value. Never eval, never YAML.

    Quote-aware, because a required check name legitimately contains a comma
    -- SampleProject's is literally ``Lint, typecheck and test (root)``.  Splitting
    it naively yields two check names that can never both be SUCCESS, so CI is
    reported ``pending`` forever and no wake ever fires on green.
    """

    if value is None:
        return ()
    if isinstance(value, str):
        items: list[str] = []
        for line in value.splitlines():
            items.extend(_split_quoted_list(line))
        return tuple(items)
    return tuple(
        str(item).strip().strip("'\"")
        for item in value
        if str(item).strip().strip("'\"")
    )


def _split_quoted_list(text: str) -> tuple[str, ...]:
    from harbor.config.projects import _split_list as _shared

    return _shared(text)


def _keys(text: str) -> dict[str, str]:
    """Extract scalar Markdown settings without interpreting prose."""

    values: dict[str, str] = {}
    list_key: str | None = None
    list_items: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue
        # YAML-like list values are common in AGENTS.md's fenced Pipeline
        # Profile (``required_checks:\n  - CI``).  Keep this tiny parser
        # intentionally limited to scalars and lists.
        list_item = re.match(r"^-\s+(.+?)\s*$", line)
        if list_item and list_key:
            list_items.append(list_item.group(1).strip().strip("'\""))
            continue
        if list_key and not list_item:
            values[list_key] = "[" + ",".join(list_items) + "]"
            list_key = None
            list_items = []
        # ``| github_repo | owner/repo |``
        table = re.match(
            r"^\|\s*([A-Za-z][\w.-]*)\s*\|\s*(.*?)\s*\|?\s*$", line
        )
        if table:
            key, value = table.groups()
            if set(value.replace("-", "").strip()) <= {""}:
                continue
            normalized = key.lower().replace("-", "_")
            values[normalized] = value.strip().strip("`")
            if not value.strip():
                list_key = normalized
            continue
        # ``required_checks: [CI, tests]`` and Markdown list bullets.
        match = re.match(
            r"^(?:[-*]\s*)?([A-Za-z][\w.-]*)\s*:\s*(.*?)\s*$", line
        )
        if match:
            key, value = match.groups()
            normalized = key.lower().replace("-", "_")
            value = value.strip().strip("`")
            if value:
                values[normalized] = value
            else:
                list_key = normalized
                list_items = []
                values[normalized] = ""
    if list_key:
        values[list_key] = "[" + ",".join(list_items) + "]"
    return values


def _first(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = values.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _repository_parts(value: str | None) -> tuple[str, str]:
    if not value:
        return "", ""
    value = value.strip().strip("`").rstrip("/")
    # Accept owner/repo, github.com/owner/repo, and the SSH remote form.
    value = re.sub(r"^(?:https?://|ssh://)?(?:[^/@:]+@)?(?:github\.com[:/])", "", value)
    value = value.removesuffix(".git")
    bits = [bit for bit in value.split("/") if bit]
    if len(bits) >= 2:
        return bits[-2], bits[-1]
    return "", bits[-1] if bits else ""


@dataclass(frozen=True)
class PipelineProfile:
    """Read-only GitHub settings for one Harbor project.

    ``required_checks`` is intentionally explicit.  A successful GitHub
    rollup with no named checks must never be presented as green.
    """

    repository_owner: str = ""
    repository_name: str = ""
    required_checks: tuple[str, ...] = ()
    github_api_url: str = "https://api.github.com/graphql"
    bot_rework_cap: int = 1
    github_token_env: str = "GITHUB_TOKEN"

    @property
    def owner(self) -> str:
        return self.repository_owner

    @property
    def repo(self) -> str:
        return self.repository_name

    @property
    def github_owner(self) -> str:
        return self.repository_owner

    @property
    def github_repo(self) -> str:
        return self.repository_name

    @property
    def repository(self) -> str:
        if self.repository_owner and self.repository_name:
            return f"{self.repository_owner}/{self.repository_name}"
        return self.repository_name

    @classmethod
    def from_markdown(
        cls,
        text: str,
        *,
        defaults: Mapping[str, object] | None = None,
    ) -> "PipelineProfile":
        defaults = defaults or {}
        values = _keys(text)
        repository_default_keys = (
            "github_repository",
            "github_repo",
            "repo",
            "repository",
        )
        has_repository_default = any(key in defaults for key in repository_default_keys)
        default_repository = next(
            (str(defaults[key]) for key in repository_default_keys if defaults.get(key)),
            "",
        )
        repository = _first(
            values,
            "github_repository",
            "github_repo",
            "repository",
            "repo",
        ) or default_repository
        if not repository and not has_repository_default:
            repository = os.environ.get("GITHUB_REPOSITORY", "")
        owner, repo = _repository_parts(repository)
        owner = _first(values, "github_owner", "owner") or str(defaults.get("github_owner") or owner)
        if not owner and not has_repository_default:
            owner = os.environ.get("GITHUB_OWNER", "")
        repo = _first(values, "github_name", "repository_name", "repo_name") or str(
            defaults.get("github_name") or defaults.get("repository_name") or repo
        )
        required_raw = _first(
            values,
            "required_checks",
            "required_check",
            "ci_required_checks",
            "github_required_checks",
        )
        required = _split_list(
            required_raw
            if required_raw is not None
            else defaults.get("required_checks")  # type: ignore[arg-type]
        )
        endpoint = _first(values, "github_api_url", "github_graphql_url", "github_endpoint") or str(
            defaults.get("github_api_url") or "https://api.github.com/graphql"
        )
        cap_raw = _first(values, "bot_rework_cap", "bot_rework_rounds")
        try:
            cap = int(cap_raw if cap_raw is not None else defaults.get("bot_rework_cap", 1))
        except (TypeError, ValueError):
            cap = 1
        return cls(
            repository_owner=owner,
            repository_name=repo,
            required_checks=required,
            github_api_url=endpoint,
            bot_rework_cap=max(0, cap),
            github_token_env=str(defaults.get("github_token_env", "GITHUB_TOKEN")),
        )

    @classmethod
    def from_file(cls, path: Path, **kwargs: object) -> "PipelineProfile":
        path = Path(path)
        if path.is_dir():
            pipeline = path / ".harbor" / "pipeline.md"
            path = pipeline if pipeline.is_file() else path / "AGENTS.md"
        return cls.from_markdown(path.read_text(encoding="utf-8"), defaults=kwargs)

    @classmethod
    def from_project(cls, project: object) -> "PipelineProfile":
        """Build a profile from a ProjectConfig-like object.

        Keeping this duck-typed avoids making the facts layer depend on the
        Linear/project poller and makes fakes straightforward in tests.
        """

        path = getattr(project, "pipeline_path", None)
        defaults: dict[str, object] = {}
        for name in (
            "github_repository",
            "github_owner",
            "github_repo",
            "repository_name",
            "required_checks",
            "github_api_url",
            "bot_rework_cap",
        ):
            if hasattr(project, name):
                defaults[name] = getattr(project, name)
        if path and Path(path).is_file():
            return cls.from_markdown(Path(path).read_text(encoding="utf-8"), defaults=defaults)
        repo_path = getattr(project, "repo_path", None)
        if repo_path:
            for candidate in (Path(repo_path) / ".harbor" / "pipeline.md", Path(repo_path) / "AGENTS.md"):
                if candidate.is_file():
                    return cls.from_markdown(candidate.read_text(encoding="utf-8"), defaults=defaults)
        return cls.from_markdown("", defaults=defaults)


def parse_pipeline_profile(
    text: str, *, defaults: Mapping[str, object] | None = None
) -> PipelineProfile:
    return PipelineProfile.from_markdown(text, defaults=defaults)


def load_profile(path: Path) -> PipelineProfile:
    return PipelineProfile.from_file(path)


__all__ = ["PipelineProfile", "load_profile", "parse_pipeline_profile"]
