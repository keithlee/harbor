"""Git invocations made by Harbor itself, outside any builder sandbox.

Harbor runs git in the project repository and in builder worktrees with the
operator's environment, including the GitHub token.  A builder can write parts
of that repository's git metadata (its worktree's admin directory, and the
whole git directory of a plain checkout), so a repository-supplied hook or
``core.fsmonitor`` program must never run in Harbor's own git calls.  None of
Harbor's commands needs either, so every call disables both.
"""

from __future__ import annotations

# ``-c`` overrides every configuration file, including anything they include.
# ``/dev/null`` is not a directory, so no hook can be found under it.
HARDENING: tuple[str, ...] = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


def git_argv(*args: str) -> list[str]:
    """Return ``git <args>`` with repository hooks and fsmonitor disabled."""

    return ["git", *HARDENING, *args]


__all__ = ["HARDENING", "git_argv"]
