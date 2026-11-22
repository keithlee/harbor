"""Provider fallback on usage limits: profiles, policy, readiness and hand-off text.

When a provider reports a usage limit, Harbor may continue on the other
provider instead of waiting for the reset. This module holds the pure parts:
which profile to use, whether the target is ready, and the deterministic
hand-off a fresh session starts from. The supervisor, runner factory and
First Mate call into it; it never talks to Linear or GitHub.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

ROLE_ORDER = ("controller", "planner", "reviewer", "implementor")


@dataclass(frozen=True)
class Profile:
    provider: str
    model: str
    effort: str | None = None


def default_readiness(profile: Profile, *, playwright_image: str | None = None) -> str | None:
    """Return why *profile* cannot run right now, or ``None`` when it can."""
    executable = os.environ.get(f"HARBOR_{profile.provider.upper()}_EXECUTABLE", profile.provider)
    if shutil.which(executable) is None:
        return f"{profile.provider} executable {executable!r} not found"
    if profile.provider != "claude":
        return None
    if playwright_image:
        return "Claude Code builders cannot use playwright_browser_image"
    try:
        result = subprocess.run([executable, "auth", "status"], capture_output=True, text=True, timeout=10)
        logged_in = json.loads(result.stdout or "{}").get("loggedIn") is True
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return "could not check the Claude Code login"
    return None if logged_in else "Claude Code is not logged in (run `claude auth login`)"


@dataclass(frozen=True)
class FallbackPolicy:
    """Two provider profiles per role; a run on one moves to the other on a usage limit."""

    max_switches: int
    builder: tuple[Profile, Profile] | None  # (primary, fallback)
    mate: tuple[Profile, Profile] | None
    roles: Mapping[str, Mapping[str, Mapping[str, Any]]]  # provider -> role -> {model, reasoning_effort}
    check: Callable[[Profile], str | None] = default_readiness

    def roles_for(self, provider: str) -> Mapping[str, Mapping[str, Any]]:
        return self.roles.get(provider, {})

    @staticmethod
    def _other(pair: tuple[Profile, Profile] | None, current: str) -> Profile | None:
        if pair is None:
            return None
        primary, fallback = pair
        if current == primary.provider:
            return fallback
        if current == fallback.provider:
            return primary
        return None

    def builder_target(self, current_provider: str) -> Profile | None:
        return self._other(self.builder, current_provider)

    def mate_target(self, current_provider: str) -> Profile | None:
        return self._other(self.mate, current_provider)

    def starting_builder_profile(self, connection: Any, now: float) -> Profile | None:
        """The fallback profile for a new ticket, only while the primary is known to be exhausted."""
        if self.builder is None:
            return None
        from harbor.recovery import provider_exhausted
        primary, fallback = self.builder
        if provider_exhausted(connection, primary.provider, now) is None:
            return None
        if provider_exhausted(connection, fallback.provider, now) is not None:
            return None
        return None if self.check(fallback) else fallback


def policy_from_config(
    project: Any, workflow_config: Mapping[str, Any], *, check: Callable[[Profile], str | None] | None = None
) -> FallbackPolicy | None:
    """Build the policy from the project and workflow config; ``None`` when not configured."""
    section = workflow_config.get("quota_fallback")
    if not section:
        return None
    builder = mate = None
    roles: dict[str, Mapping[str, Mapping[str, Any]]] = {}
    controller = section.get("controller")
    if controller is not None:
        primary_role = workflow_config["roles"]["controller"]
        primary = Profile(getattr(project, "builder_provider", "codex"), primary_role["model"], primary_role.get("reasoning_effort"))
        if controller["provider"] == primary.provider:
            raise ValueError(f"quota_fallback.controller.provider must differ from the builder provider ({primary.provider})")
        builder = (primary, Profile(controller["provider"], controller["model"], controller.get("reasoning_effort")))
        roles = {primary.provider: workflow_config["roles"], controller["provider"]: section["roles"]}
    mate_section = section.get("first_mate")
    if mate_section is not None:
        mate_role = workflow_config["first_mate"]
        primary = Profile(getattr(project, "firstmate_provider", "codex"), mate_role["model"], mate_role.get("reasoning_effort"))
        if mate_section["provider"] == primary.provider:
            raise ValueError(f"quota_fallback.first_mate.provider must differ from the First Mate provider ({primary.provider})")
        mate = (primary, Profile(mate_section["provider"], mate_section["model"], mate_section.get("reasoning_effort")))
    kwargs = {} if check is None else {"check": check}
    return FallbackPolicy(section["max_switches"], builder, mate, roles, **kwargs)


def _role_lines(roles: Mapping[str, Mapping[str, Any]]) -> list[str]:
    lines = []
    for name in ROLE_ORDER:
        role = roles.get(name)
        if role:
            effort = f", reasoning effort {role['reasoning_effort']}" if role.get("reasoning_effort") else ""
            lines.append(f"- {name}: {role['model']}{effort}")
    return lines


def handoff_text(
    *,
    ticket: str,
    run_id: str,
    worktree: str,
    source: str,
    target: Profile,
    summary: str,
    pr_urls: Sequence[str],
    open_questions: Sequence[str],
    answers: Sequence[str],
    roles: Mapping[str, Mapping[str, Any]],
) -> str:
    """Deterministic hand-off for a fresh session started because *source* hit a usage limit."""
    parts = [
        f"Harbor is continuing {ticket} (run {run_id}) on {target.provider} ({target.model}) because "
        f"{source} reported a usage limit. This is a fresh session: nothing from the previous conversation is available.",
        f"The retained worktree {worktree}, its branch, the ticket and any pull request are unchanged. "
        "Do not create another worktree, session or pull request. Re-read the current Linear ticket, GitHub facts and the "
        "worktree (git status, git log), reconcile partial edits, then continue the acceptance criteria and the delivery workflow.",
        f"Previous run summary: {(summary or 'none')[:1500]}",
    ]
    if pr_urls:
        parts.append("Pull requests: " + ", ".join(pr_urls))
    if open_questions:
        parts.append("Open questions before the stop:\n" + "\n".join(f"- {q}" for q in open_questions))
    if answers:
        parts.append("Operator answers not yet acted on (follow them):\n" + "\n".join(f"- {a}" for a in answers))
    role_lines = _role_lines(roles)
    if role_lines:
        parts.append("Role models for this session (these override the models in config.toml):\n" + "\n".join(role_lines))
    return "\n\n".join(parts)
