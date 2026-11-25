"""Project configuration and the small Markdown format used by Harbor.

The project file is deliberately data-only.  A project's delivery skill remains an
opaque string to the supervisor; Phase 1 only needs the scope and polling settings.
"""

from __future__ import annotations

from harbor.clients.linear import DEFAULT_LINEAR_API_URL
from harbor.recovery import BlockerPolicy

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Sequence

from .pipeline import (
    DEFAULT_BASE_BRANCH,
    DEFAULT_BOT_REWORK_CAP,
    DEFAULT_BUILDER_EFFORT,
    DEFAULT_BUILDER_MODEL,
    DEFAULT_DEADLINE_MINUTES,
    DEFAULT_DISK_FLOOR_GB,
    DEFAULT_FIRST_MATE_EFFORT,
    DEFAULT_FIRST_MATE_MODEL,
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_SKILL,
    DEFAULT_WORKTREE_ROOT,
    PipelineConfig,
    load_projects,
    parse_pipeline_markdown as parse_pipeline_settings,
)


POLL_STATES = ("Todo", "In Progress", "In Review", "Blocked", "Human Review")
DISPATCH_STATES = ("Todo",)


def _as_bool(value: str, default: bool) -> bool:
    value = value.strip().lower()
    if value in {"true", "yes", "on", "1"}:
        return True
    if value in {"false", "no", "off", "0"}:
        return False
    return default


def _split_list(text: str | Sequence[str] | None) -> tuple[str, ...]:
    """Split a bracketed list on commas that are not inside a quoted item.

    Required check names may contain commas, so a naive ``split(",")`` can
    silently turn one check into several that can never all be observed.
    """

    if text is None:
        return ()
    if not isinstance(text, str):
        # Already a sequence (a parsed config value or a dataclass default).
        return tuple(str(item).strip() for item in text if str(item).strip())
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    items: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
            else:
                buffer.append(char)
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == ",":
            item = "".join(buffer).strip()
            if item:
                items.append(item)
            buffer = []
            continue
        buffer.append(char)
    trailing = "".join(buffer).strip()
    if trailing:
        items.append(trailing)
    return tuple(items)


def _as_list(value: str, default: Sequence[str]) -> tuple[str, ...]:
    return _split_list(value) or tuple(default)


def parse_pipeline_markdown(text: str, *, defaults: Mapping[str, object] | None = None) -> dict[str, object]:
    """Parse scalar scope keys from ``pipeline.md``.

    Both the plan's ``key: value`` notation and a Markdown table row such as
    ``| dispatch_states | [Todo] |`` are accepted.  Unknown keys are ignored so
    teams can add skill-specific settings without making Harbor parse them.
    """

    result: dict[str, object] = dict(defaults or {})
    scalar: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("<!--"):
            continue
        table = re.match(r"^\|\s*([A-Za-z][\w-]*)\s*\|\s*(.*?)\s*\|?\s*$", line)
        if table:
            scalar[table.group(1).replace("-", "_")] = table.group(2).split(" #", 1)[0].strip()
            continue
        match = re.match(r"^(?:[-*]\s*)?([A-Za-z][\w-]*)\s*:\s*(.*?)\s*$", line)
        if match:
            scalar[match.group(1).replace("-", "_")] = match.group(2).split(" #", 1)[0].strip().strip('`')

    if "dispatch_states" in scalar:
        result["dispatch_states"] = _as_list(scalar["dispatch_states"], DISPATCH_STATES)
    if "poll_states" in scalar:
        result["poll_states"] = _as_list(scalar["poll_states"], POLL_STATES)
    if "hold_all" in scalar:
        result["hold_all"] = _as_bool(scalar["hold_all"], bool(result.get("hold_all", True)))
    if "poll_interval_seconds" in scalar:
        try:
            result["poll_interval_seconds"] = float(scalar["poll_interval_seconds"])
        except ValueError:
            pass
    if "poll_interval" in scalar:
        try:
            result["poll_interval_seconds"] = float(scalar["poll_interval"])
        except ValueError:
            pass
    if "linear_project_slug" in scalar:
        result["linear_project_slug"] = scalar["linear_project_slug"]
    if "project_slug" in scalar:
        result["linear_project_slug"] = scalar["project_slug"]
    if "slug" in scalar:
        result["linear_project_slug"] = scalar["slug"]
    if "linear_team" in scalar:
        result["linear_team"] = scalar["linear_team"]
    if "team" in scalar:
        result["linear_team"] = scalar["team"]
    if "api_url" in scalar:
        result["linear_api_url"] = scalar["api_url"]
    # Keep this historical parser's scope behavior while exposing the newer
    # model/heartbeat fields to callers that import it directly.
    parsed_settings = parse_pipeline_settings(text, defaults=result)
    for key in (
        "builder_provider",
        "firstmate_provider",
        "builder_model",
        "builder_fallback_models",
        "firstmate_fallback_models",
        "builder_effort",
        "firstmate_model",
        "firstmate_effort",
        "heartbeat_seconds",
        "poll_interval_seconds",
        "linear_project_slug",
        "linear_projects",
        "linear_team",
        "linear_api_url",
        "dispatch_states",
        "poll_states",
        "hold_all",
        "github_repository",
        "required_checks",
        "bot_rework_cap",
        "skill",
        "deadline_minutes",
        "disk_floor_gb",
        "base_branch",
        "worktree_root",
        "hold",
        "hold_all",
        "install_command",
        "workflow_path",
        "playwright_browser_image",
    ):
        if key in parsed_settings:
            result[key] = parsed_settings[key]
    return result


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    linear_project_slug: str
    # Additional Linear projects to include in the same poll.  Adding a
    # project is a config edit, never a code change; the query stays one
    # request regardless of how many are listed.
    linear_projects: tuple[str, ...] = ()
    linear_team: str = ""
    repo_path: Path = field(default_factory=lambda: Path.cwd())
    pipeline_path: Path | None = None
    registry_path: Path | None = None
    # GitHub settings (``github_repository``, ``required_checks``,
    # ``bot_rework_cap``) that a non-empty registry cell set.  Only these win
    # over the pipeline file in ``PipelineProfile.from_project``; a value that
    # came from the pipeline or a default leaves the profile's own parse alone.
    registry_github_keys: frozenset[str] = frozenset()
    # Registry columns for this project that Harbor does not read; ``harbor
    # doctor`` and ``harbor start`` report them (``registry_key_warnings``).
    registry_unknown_keys: tuple[str, ...] = field(default=(), compare=False)
    # Registry settings whose value Harbor does not use, as (column, value,
    # reason); for example a role model that .harbor/workflow always sets.
    registry_unused_values: tuple[tuple[str, str, str], ...] = field(default=(), compare=False)
    linear_api_url: str = DEFAULT_LINEAR_API_URL
    # GitHub GraphQL endpoint set in the project registry (``github_api_url``,
    # or its aliases ``github_graphql_url`` / ``github_endpoint``).  Empty
    # means the registry does not set it, so ``PipelineProfile.from_project``
    # reads the pipeline file and then the GitHub.com default.  When set, it
    # wins over the pipeline file, like ``linear_api_url``.
    github_api_url: str = ""
    poll_states: tuple[str, ...] = POLL_STATES
    dispatch_states: tuple[str, ...] = DISPATCH_STATES
    poll_interval_seconds: float = 60.0
    hold_all: bool = True
    # Model and heartbeat values are read from the project pipeline document.
    # Defaults preserve the MVP contract while keeping direct test fixtures
    # and embedders source-compatible with Phase 1.
    builder_provider: str = "codex"
    firstmate_provider: str = "codex"
    builder_model: str = DEFAULT_BUILDER_MODEL
    builder_fallback_models: tuple[str, ...] = ()
    firstmate_fallback_models: tuple[str, ...] = ()
    builder_effort: str = DEFAULT_BUILDER_EFFORT
    firstmate_model: str = DEFAULT_FIRST_MATE_MODEL
    firstmate_effort: str = DEFAULT_FIRST_MATE_EFFORT
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    skill: str = DEFAULT_SKILL
    deadline_minutes: float = DEFAULT_DEADLINE_MINUTES
    # Free GB required before a builder worktree is created.
    disk_floor_gb: float = DEFAULT_DISK_FLOOR_GB
    base_branch: str = DEFAULT_BASE_BRANCH
    worktree_root: str = DEFAULT_WORKTREE_ROOT
    github_repository: str = ""
    required_checks: tuple[str, ...] = ()
    bot_rework_cap: int = DEFAULT_BOT_REWORK_CAP
    # Opaque to Harbor: it runs this in a new worktree, nothing more.
    install_command: str | None = None
    workflow_path: str | None = None
    playwright_browser_image: str | None = None
    # ``hold_all`` predates the project pipeline contract.  Accept both
    # spellings at construction time so parsed mappings from older embedders
    # remain source-compatible, then keep them synchronized.
    blocker_policy: BlockerPolicy = field(default_factory=BlockerPolicy)
    hold: bool | None = None

    def __post_init__(self) -> None:
        from harbor.runner.playwright import validate_image
        validate_image(self.playwright_browser_image)
        configured = self.hold_all if self.hold is None else bool(self.hold)
        object.__setattr__(self, "hold_all", configured)
        object.__setattr__(self, "hold", configured)
        object.__setattr__(self, "linear_projects", _split_list(self.linear_projects))
        from harbor.model_policy import validate_fallbacks
        for prefix in ("builder", "firstmate"):
            provider = getattr(self, prefix + "_provider")
            if provider not in {"codex", "claude"}:
                raise ValueError(f"invalid {prefix}_provider: use codex or claude")
            if provider == "claude" and getattr(self, prefix + "_model").startswith("gpt-"):
                raise ValueError(f"configure {prefix}_model explicitly for Claude Code")
            key = prefix + "_fallback_models"
            object.__setattr__(self, key, validate_fallbacks(getattr(self, prefix + "_model"), getattr(self, key)))

    @property
    def linear_project_scope(self) -> tuple[str, ...]:
        """Every Linear project slug this Harbor instance polls.

        ``linear_project_slug`` is the primary project and stays required, so
        an existing single-project config keeps working untouched.  Listing
        more in ``linear_projects`` widens the same single GraphQL request.
        """

        scope: list[str] = []
        for slug in (self.linear_project_slug, *self.linear_projects):
            text = str(slug).strip()
            if text and text not in scope:
                scope.append(text)
        return tuple(scope)

    @property
    def project_slug(self) -> str:
        """Alias useful to callers that use Linear's terminology."""

        return self.linear_project_slug

    @property
    def first_mate_model(self) -> str:
        return self.firstmate_model

    @property
    def first_mate_effort(self) -> str:
        return self.firstmate_effort

    @property
    def planner_model(self) -> str:
        return self.firstmate_model

    @property
    def planner_effort(self) -> str:
        return self.firstmate_effort

    @property
    def heartbeat_interval_seconds(self) -> float:
        return self.heartbeat_seconds

    @property
    def deadline_seconds(self) -> float:
        return self.deadline_minutes * 60.0

    @property
    def github_repo(self) -> str:
        return self.github_repository

    @property
    def pipeline_config(self) -> PipelineConfig:
        return PipelineConfig(
            builder_provider=self.builder_provider,
            firstmate_provider=self.firstmate_provider,
            builder_model=self.builder_model,
            builder_fallback_models=self.builder_fallback_models,
            firstmate_fallback_models=self.firstmate_fallback_models,
            builder_effort=self.builder_effort,
            firstmate_model=self.firstmate_model,
            firstmate_effort=self.firstmate_effort,
            heartbeat_seconds=self.heartbeat_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
            skill=self.skill,
            deadline_minutes=self.deadline_minutes,
            disk_floor_gb=self.disk_floor_gb,
            base_branch=self.base_branch,
            worktree_root=self.worktree_root,
            github_repository=self.github_repository,
            required_checks=self.required_checks,
            bot_rework_cap=self.bot_rework_cap,
            hold=self.hold,
            blocker_policy=self.blocker_policy,
        )

    @classmethod
    def from_pipeline(cls, name: str, path: Path, **overrides: object) -> "ProjectConfig":
        explicit_slug = overrides.pop("linear_project_slug", None)
        explicit_team = overrides.pop("linear_team", None)
        values: dict[str, object] = {
            "linear_project_slug": "",
            "linear_team": "",
            "repo_path": path.parent.parent,
            "pipeline_path": path,
        }
        values.update(parse_pipeline_markdown(path.read_text(encoding="utf-8"), defaults=values))
        pipeline = PipelineConfig.from_file(path, defaults=values)
        values.update(
            {
                "builder_provider": pipeline.builder_provider,
                "firstmate_provider": pipeline.firstmate_provider,
                "builder_model": pipeline.builder_model,
                "builder_fallback_models": pipeline.builder_fallback_models,
                "firstmate_fallback_models": pipeline.firstmate_fallback_models,
                "builder_effort": pipeline.builder_effort,
                "firstmate_model": pipeline.firstmate_model,
                "firstmate_effort": pipeline.firstmate_effort,
                "heartbeat_seconds": pipeline.heartbeat_seconds,
                "poll_interval_seconds": pipeline.poll_interval_seconds,
                "skill": pipeline.skill,
                "deadline_minutes": pipeline.deadline_minutes,
                "disk_floor_gb": pipeline.disk_floor_gb,
                "base_branch": pipeline.base_branch,
                "worktree_root": pipeline.worktree_root,
                "github_repository": pipeline.github_repository,
                "required_checks": pipeline.required_checks,
                "bot_rework_cap": pipeline.bot_rework_cap,
                "blocker_policy": pipeline.blocker_policy,
                "hold_all": pipeline.hold,
                "hold": pipeline.hold,
            }
        )
        values.update(overrides)
        if explicit_slug is not None and str(explicit_slug).strip():
            values["linear_project_slug"] = explicit_slug
        if explicit_team is not None and str(explicit_team).strip():
            values["linear_team"] = explicit_team
        values["poll_states"] = tuple(values.get("poll_states", POLL_STATES))
        values["dispatch_states"] = tuple(values.get("dispatch_states", DISPATCH_STATES))
        values["required_checks"] = tuple(values.get("required_checks", ()))
        values["repo_path"] = Path(values["repo_path"])
        values["pipeline_path"] = Path(values["pipeline_path"])
        for field_name in ("linear_project_slug", "linear_team"):
            if not str(values.get(field_name, "")).strip():
                raise ValueError(f"Harbor project {name} is missing required {field_name}")
        from harbor.config.pipeline import retired_keys
        from harbor.config.workflow import MIGRATION_HINT, load_workflow, project_workflow_path
        retired = retired_keys(path.read_text(encoding="utf-8"))
        if retired:
            verb = "no longer belongs" if len(retired) == 1 else "no longer belong"
            raise ValueError(f"{path}: {', '.join(retired)} {verb} in pipeline.md; {MIGRATION_HINT}")
        workflow_path = project_workflow_path(values["repo_path"]).resolve()
        workflow = load_workflow(workflow_path)
        values["workflow_path"] = str(workflow_path)
        policy = workflow.config
        values["deadline_minutes"] = policy.get("execution",{}).get("deadline_minutes", values["deadline_minutes"])
        for settings, prefix in ((policy["roles"]["controller"], "builder"), (policy["first_mate"], "firstmate")):
            values[prefix+"_model"] = settings["model"]
            values[prefix+"_fallback_models"] = tuple(settings.get("fallback_models", ()))
            if settings.get("reasoning_effort"):
                values[prefix+"_effort"] = settings["reasoning_effort"]
        from harbor.recovery import BlockerPolicy
        recovery = policy["recovery"]
        values["blocker_policy"] = BlockerPolicy(
            retry_delays=tuple(recovery["delays_seconds"][:recovery["max_attempts"]]) if recovery["enabled"] else (),
            decision_mode=policy["decisions"]["mode"],
            unblock_attempts=values["blocker_policy"].unblock_attempts,
            authorized_authors=values["blocker_policy"].authorized_authors,
        )
        if "states" in policy:
            states = policy["states"]
            values["dispatch_states"] = tuple(states.get("ready", values["dispatch_states"]))
            values["poll_states"] = tuple(dict.fromkeys((*values["poll_states"], *states.get("ready", []), *states.get("paused", []))))
        return cls(name=name, **values)  # type: ignore[arg-type]

    @classmethod
    def from_file(cls, name: str, path: Path, **overrides: object) -> "ProjectConfig":
        return cls.from_pipeline(name, Path(path), **overrides)


def _resolve_registry_github(name: str, overrides: dict[str, object]) -> frozenset[str]:
    """Normalize the registry's GitHub columns in place; return the keys set.

    Precedence for ``github_repository``, ``required_checks`` and
    ``bot_rework_cap`` is registry -> pipeline -> default, the same as the
    endpoint columns: a non-empty registry cell wins, and an empty cell is
    dropped so it never blanks the pipeline's value.  Registry cells are raw
    strings, so they are parsed here; otherwise ``[CI, tests]`` would become
    a tuple of characters.  A ``bot_rework_cap`` that is not a non-negative
    integer fails closed rather than silently replacing the pipeline's cap.
    """

    present: set[str] = set()
    for key, names in REGISTRY_GITHUB_ALIASES.items():
        aliases = (key, *names)
        value = next(
            (overrides[alias] for alias in aliases if str(overrides.get(alias) or "").strip()),
            None,
        )
        if value is None:
            overrides.pop(key, None)
            continue
        present.add(key)
        if key == "required_checks":
            overrides[key] = _split_list(value)  # type: ignore[arg-type]
        elif key == "bot_rework_cap":
            text = str(value).strip()
            if not text.isdigit():
                raise ValueError(
                    f"Harbor project {name}: registry bot_rework_cap must be a non-negative integer, got {text!r}"
                )
            overrides[key] = int(text)
        else:
            overrides[key] = str(value).strip()
    return frozenset(present)
# Registry columns ``get_project`` passes to ``ProjectConfig``.  Any other
# column is ignored, and reported by ``registry_key_warnings``.
REGISTRY_SETTING_KEYS = frozenset({
    "linear_project_slug",
    "linear_projects",
    "linear_team",
    "linear_api_url",
    "github_api_url",
    "poll_interval_seconds",
    "heartbeat_seconds",
    "poll_states",
    "dispatch_states",
    "hold_all",
    "builder_provider",
    "firstmate_provider",
    "builder_model",
    "builder_fallback_models",
    "firstmate_fallback_models",
    "builder_effort",
    "firstmate_model",
    "firstmate_effort",
    "skill",
    "workflow_path",
    "playwright_browser_image",
    "deadline_minutes",
    "base_branch",
    "worktree_root",
    "github_repository",
    "required_checks",
    "bot_rework_cap",
    "hold",
    "repo_path",
    "install_command",
})
# Alternative registry spellings, by the setting they resolve to.  A scope
# alias applies only when the setting's own column is absent; an endpoint
# alias is taken when it is the first non-empty column in (setting, *aliases).
REGISTRY_SCOPE_ALIASES: dict[str, tuple[str, ...]] = {
    "linear_project_slug": ("project_slug", "slug"),
    "linear_team": ("team",),
    "poll_interval_seconds": ("poll_interval",),
    "heartbeat_seconds": ("heartbeat_interval",),
}
REGISTRY_ENDPOINT_ALIASES: dict[str, tuple[str, ...]] = {
    "linear_api_url": ("api_url",),
    "github_api_url": ("github_graphql_url", "github_endpoint"),
}
# GitHub columns ``_resolve_registry_github`` parses: the first non-empty
# column in (setting, *aliases) wins, like the endpoint columns.
REGISTRY_GITHUB_ALIASES: dict[str, tuple[str, ...]] = {
    "github_repository": ("github_repo",),
    "required_checks": ("ci_required_checks",),
    "bot_rework_cap": ("bot_rework_rounds",),
}
REGISTRY_ALIAS_TABLES = (REGISTRY_SCOPE_ALIASES, REGISTRY_ENDPOINT_ALIASES, REGISTRY_GITHUB_ALIASES)
REGISTRY_ALIAS_KEYS = frozenset(
    alias for table in REGISTRY_ALIAS_TABLES for aliases in table.values() for alias in aliases
)
# Registry columns ``ProjectConfig.from_pipeline`` always replaces with the
# value from ``.harbor/workflow``: setting one in the registry has no effect.
WORKFLOW_OWNED_KEYS = frozenset({
    "builder_model", "firstmate_model", "builder_fallback_models", "firstmate_fallback_models", "workflow_path",
})


def unknown_registry_keys(values: Mapping[str, object]) -> tuple[str, ...]:
    """Registry columns of one project that Harbor does not read."""

    return tuple(key for key in values if key and key not in REGISTRY_SETTING_KEYS | REGISTRY_ALIAS_KEYS)


def _registry_column(setting: str, values: Mapping[str, object]) -> str:
    """The column ``get_project`` reads for *setting* (the setting or an alias).

    A scope alias is read only when the setting's own column is absent (an
    empty own column still wins); endpoint and GitHub columns take the first
    non-empty one of (setting, *aliases).
    """

    if setting in REGISTRY_SCOPE_ALIASES:
        return next((column for column in (setting, *REGISTRY_SCOPE_ALIASES[setting]) if column in values), setting)
    aliases = next((table[setting] for table in REGISTRY_ALIAS_TABLES if setting in table), ())
    for column in (setting, *aliases):
        if str(values.get(column) or "").strip():
            return column
    return setting


def _shown(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def _registry_value_used(raw: object, used: object) -> bool:
    """Whether the registry cell *raw* is the value Harbor resolved (*used*)."""

    text = str(raw).strip()
    if isinstance(used, bool):
        return _as_bool(text, not used) == used
    if isinstance(used, (tuple, list)):
        return _split_list(text) == tuple(str(item) for item in used)
    if isinstance(used, (int, float)):
        try:
            return float(text) == float(used)
        except ValueError:
            return False
    return used is not None and text == str(used).strip()


def _workflow_settings(config: "ProjectConfig") -> frozenset[str]:
    """Settings the project's ``.harbor/workflow`` sets over the registry."""

    if not config.workflow_path:
        return frozenset()
    from harbor.config.workflow import load_workflow

    try:
        policy = load_workflow(Path(config.workflow_path)).config
    except (OSError, ValueError):
        return frozenset()
    owned: set[str] = set()
    for role, prefix in (("controller", "builder"), (None, "firstmate")):
        settings = policy["roles"][role] if role else policy["first_mate"]
        if settings.get("reasoning_effort"):
            owned.add(prefix + "_effort")
    if "deadline_minutes" in policy.get("execution", {}):
        owned.add("deadline_minutes")
    if "ready" in policy.get("states", {}):
        owned.add("dispatch_states")
    if policy.get("states", {}).get("ready") or policy.get("states", {}).get("paused"):
        owned.add("poll_states")
    return frozenset(owned)


def _pipeline_only(config: "ProjectConfig") -> "ProjectConfig | None":
    """*config*'s project resolved from its pipeline alone, without the registry."""

    if config.pipeline_path is None:
        return None
    try:
        return ProjectConfig.from_pipeline(
            config.name, Path(config.pipeline_path), repo_path=config.repo_path,
            linear_project_slug=config.linear_project_slug, linear_team=config.linear_team,
        )
    except (OSError, ValueError):
        return None


def registry_values_not_used(
    config: "ProjectConfig",
    settings: Mapping[str, object],
    values: Mapping[str, object],
    identity: Mapping[str, object] | None = None,
) -> tuple[tuple[str, str, str], ...]:
    """Registry cells whose value is not the one Harbor resolved.

    *settings* are the registry settings ``get_project`` passed on, *values*
    the raw setting columns and *identity* the raw identity columns.  Each
    result is ``(column, registry value, reason)``.  Reported:

    - a workflow-owned column (``.harbor/workflow`` always replaces it);
    - a column whose value differs from the value Harbor resolved;
    - a column Harbor skips because another spelling of the same setting
      wins (an alias, or a second identity column such as ``repository``);
    - an empty cell whose empty value Harbor resolves although the pipeline
      (or its default) sets a non-empty one.  Once resolution ignores empty
      cells, Harbor resolves the pipeline value and this stops firing.
    """

    from harbor.config.pipeline import REGISTRY_IDENTITY_COLUMNS
    from harbor.config.workflow import MIGRATION_HINT

    workflow = _workflow_settings(config)
    result: list[tuple[str, str, str]] = []
    pipeline_only: list["ProjectConfig | None"] = []

    def filled(column: str, table: Mapping[str, object]) -> str:
        return str(table.get(column) or "").strip()

    for target, columns in REGISTRY_IDENTITY_COLUMNS.items():
        written = identity or {}
        winner = next((column for column in columns if filled(column, written)), None)
        for column in columns:
            if winner and column != winner and filled(column, written) and filled(column, written) != filled(winner, written):
                reason = f"Harbor ignores it: it reads `{winner}` (`{filled(winner, written)}`)"
                if column == "repository":
                    reason += "; for the GitHub repository, use `github_repository`"
                result.append((column, filled(column, written), reason))

    for setting in settings:
        if setting == "repo_path":
            continue
        column = _registry_column(setting, values)
        if column not in values:
            continue  # derived by get_project (``hold_all`` from ``hold``), not written by the operator
        used = getattr(config, setting, None)
        aliases = next((table[setting] for table in REGISTRY_ALIAS_TABLES if setting in table), ())
        for other in (setting, *aliases):
            if other != column and filled(other, values) and not _registry_value_used(filled(other, values), used):
                reads = f"`{column}`" + (", which is empty" if not filled(column, values) else "")
                result.append((other, filled(other, values), f"Harbor ignores it: it reads {reads}, and uses `{_shown(used)}`"))
        # Compare the cell the operator wrote, not get_project's parsed copy
        # (``required_checks`` is a tuple and ``bot_rework_cap`` an int there).
        raw = values[column]
        if not str(raw).strip():
            if not pipeline_only:
                pipeline_only.append(_pipeline_only(config))
            fallback = getattr(pipeline_only[0], setting, None) if pipeline_only[0] is not None else None
            if not used and fallback and fallback != used:
                empty = "the empty value" + (f" (`{_shown(used)}`)" if _shown(used) else "")
                result.append((column, "", f"Harbor uses {empty} instead of `{_shown(fallback)}` from the pipeline "
                               "or its default; fill in the cell or remove the column"))
            continue
        if setting in WORKFLOW_OWNED_KEYS:
            result.append((column, str(raw), f"Harbor ignores it: `{setting}` comes from .harbor/workflow; {MIGRATION_HINT}"))
            continue
        if _registry_value_used(raw, used):
            continue
        reason = f"Harbor uses `{_shown(used)}`"
        if setting in workflow:
            reason += ", set in .harbor/workflow; change it there"
        result.append((column, str(raw), reason))
    return tuple(result)


def _registry_key_hint(key: str, pipeline: Path | None) -> str:
    import difflib
    from .pipeline import PIPELINE_KEY_ALIASES, RETIRED_KEYS
    from .workflow import MIGRATION_HINT

    where = f"set it in {pipeline}" if pipeline else "set it in the project's pipeline.md"
    if key in RETIRED_KEYS:
        return f" `{key}` belongs in .harbor/workflow; {MIGRATION_HINT}."
    pipeline_aliases = {
        **PIPELINE_KEY_ALIASES,
        "heartbeat_seconds": ("heartbeat_seconds", "heartbeat_interval_seconds", "heartbeat_interval"),
        "poll_interval_seconds": ("poll_interval_seconds", "poll_interval"),
    }
    canonical = {alias: setting for setting, aliases in pipeline_aliases.items() for alias in aliases}
    # Columns worth suggesting: ones whose registry value Harbor actually uses.
    # ``poll_states``/``dispatch_states`` are left out while a registry list is
    # still split into characters (a separate fix).
    columns = (REGISTRY_SETTING_KEYS | REGISTRY_ALIAS_KEYS) - WORKFLOW_OWNED_KEYS - set(RETIRED_KEYS) - {
        "repo_path", "poll_states", "dispatch_states",
    }
    if canonical.get(key) in columns:
        return f" The registry column is `{canonical[key]}`."
    pipeline_keys = set(canonical) - set(RETIRED_KEYS)
    if key in pipeline_keys:
        return f" `{key}` is a pipeline setting: {where}."
    match = difflib.get_close_matches(key, sorted(columns), n=1, cutoff=0.8)
    if match:
        return f" Did you mean `{match[0]}`?"
    match = difflib.get_close_matches(key, sorted(pipeline_keys - columns), n=1, cutoff=0.8)
    if match:
        return f" Did you mean the pipeline setting `{match[0]}`? If so, {where}."
    return ""


def registry_key_warnings(config: "ProjectConfig") -> tuple[str, ...]:
    """One line per registry column of *config* whose value Harbor does not use."""

    where = f" in {config.registry_path}" if config.registry_path else ""
    unknown = tuple(
        f"{config.name} sets unknown column `{key}`{where}; Harbor ignores it."
        + _registry_key_hint(key, config.pipeline_path)
        for key in config.registry_unknown_keys
    )
    unused = tuple(
        (f"{config.name} sets `{column}` to `{raw}`{where}" if raw else f"{config.name} leaves `{column}` empty{where}")
        + f", but {reason}."
        for column, raw, reason in config.registry_unused_values
    )
    return unknown + unused


def get_project(name: str, *, config_root: Path | None = None) -> ProjectConfig:
    """Resolve a named project, optionally from a local config root."""

    if not name or not name.strip():
        raise ValueError("project name is required")
    normalized = name.strip().lower()

    # The registry is the source of truth for named projects.  It may be
    # selected explicitly for tests/embeddings, or via HARBOR_PROJECTS_FILE.
    # The repository-level projects.md is the normal CLI default.
    base = Path(config_root).expanduser() if config_root else Path.cwd()
    configured = os.environ.get("HARBOR_PROJECTS_FILE")
    candidates: list[Path] = []
    if configured:
        selected = Path(configured).expanduser().resolve()
        if not selected.is_file():
            raise ValueError(f"Harbor project registry does not exist: {selected}")
        candidates.append(selected)
    else:
        if config_root:
            candidates.append(base / "projects.md")
        candidates.extend(
            [
                base / "projects.md",
                Path(__file__).resolve().parents[3] / "projects.md",
            ]
        )
    seen: set[Path] = set()
    registry_loaded = False
    for registry_path in candidates:
        registry_path = registry_path.resolve()
        if registry_path in seen or not registry_path.is_file():
            continue
        seen.add(registry_path)
        try:
            definitions = load_projects(registry_path)
        except (OSError, UnicodeError):
            continue
        registry_loaded = True
        for definition in definitions:
            if definition.name.casefold() != normalized:
                continue
            pipeline = definition.pipeline_path
            if pipeline is None:
                candidate = definition.repo_path / ".harbor" / "pipeline.md"
                pipeline = candidate
            overrides: dict[str, object] = dict(definition.values)
            # Registry values are intentionally explicit and win over a
            # repository's optional pipeline file for identity/scope fields.
            for key, aliases in REGISTRY_SCOPE_ALIASES.items():
                if key in overrides:
                    continue
                for alias in aliases:
                    if alias in overrides:
                        overrides[key] = overrides[alias]
                        break
            # Endpoint columns may use the same aliases a pipeline file accepts.
            # Take the first one with a value; an empty cell is ignored, so it
            # never replaces the pipeline's value (or the default) with "" and
            # never hides a filled-in alias.
            for key, aliases in REGISTRY_ENDPOINT_ALIASES.items():
                for alias in (key, *aliases):
                    url = str(overrides.get(alias) or "").strip()
                    if url:
                        overrides[key] = url
                        break
                else:
                    overrides.pop(key, None)
            registry_github_keys = _resolve_registry_github(definition.name, overrides)
            if "hold" in overrides and "hold_all" not in overrides:
                overrides["hold_all"] = _as_bool(str(overrides["hold"]), True)
            overrides["repo_path"] = definition.repo_path
            overrides = {
                key: value
                for key, value in overrides.items()
                if key in REGISTRY_SETTING_KEYS
            }
            if pipeline is None or not pipeline.is_file():
                expected = pipeline or (definition.repo_path / ".harbor" / "pipeline.md")
                raise ValueError(
                    f"Harbor project {definition.name} is missing its live pipeline: expected {expected}; "
                    "refusing to fall back to AGENTS.md"
                )
            config = replace(
                ProjectConfig.from_pipeline(definition.name, pipeline, **overrides),
                registry_path=registry_path,
                registry_github_keys=registry_github_keys,
                registry_unknown_keys=unknown_registry_keys(definition.values),
            )
            return replace(
                config, registry_unused_values=registry_values_not_used(
                    config, overrides, definition.values, definition.identity_columns
                )
            )

    if registry_loaded:
        raise ValueError(f"unknown Harbor project: {name}")

    if not registry_loaded:
        expected = Path(configured).expanduser() if configured else base / "projects.md"
        raise ValueError(
            f"no Harbor project registry found; create {expected} or set HARBOR_PROJECTS_FILE"
        )


__all__ = [
    "DISPATCH_STATES",
    "POLL_STATES",
    "ProjectConfig",
    "get_project",
    "load_project",
    "registry_key_warnings",
    "registry_values_not_used",
    "unknown_registry_keys",
    "parse_pipeline_markdown",
]

load_project = get_project
