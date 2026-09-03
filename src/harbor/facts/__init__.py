"""Deterministic, read-only facts derived from external trackers."""

from .ci import classify, classify_ci, classify_ci_state
from .threads import parse_severity, parse_thread, parse_threads

__all__ = [
    "classify",
    "classify_ci",
    "classify_ci_state",
    "parse_severity",
    "parse_thread",
    "parse_threads",
]
