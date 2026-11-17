"""Golden operator digests: the compact ``/status`` and First Mate's full view."""

from __future__ import annotations

from harbor.digest import render_digest

FLEET = {
    "project": "demo",
    "lifecycle": "RUNNING",
    "linear_healthy": True,
    "github_healthy": True,
    "tickets": [
        {"identifier": "DEMO-2", "state": "In Progress", "title": "Add  retry\nbudget", "owned": True,
         "pr_urls": ["https://github.com/acme/demo/pull/12"]},
        {"identifier": "DEMO-1", "state": "Human Review", "title": "Ship dashboard", "owned": True},
        {"identifier": "DEMO-3", "state": "Todo", "title": "Pre-existing", "owned": False},
    ],
    "runs": [
        {"id": "run-b", "ticket": "DEMO-2", "role": "builder", "status": "running", "waiting_on": "none"},
        {"id": "run-a", "ticket": "DEMO-1", "role": "builder", "status": "waiting", "waiting_on": "ci",
         "pr_url": "https://github.com/acme/demo/pull/11", "next_action": "Owner comments `lgtm merge` on the PR"},
        {"id": "run-c", "ticket": "DEMO-1", "role": "builder", "status": "completed"},
        {"id": "run-d", "ticket": "DEMO-3", "role": "builder", "status": "failed",
         "terminal_reason": "usage_limit_exceeded",
         "quota_recovery": {"provider": "codex", "next_retry": 1_800_000_000, "confirmed": 1}},
        {"id": "run-e", "ticket": "DEMO-2", "role": "reviewer", "status": "failed"},
    ],
    "escalations": [
        {"id": 10, "ticket": "DEMO-1", "kind": "blocker", "question": "Which  API\nversion?"},
    ],
}


def test_compact_status_digest(golden) -> None:
    golden.text("digest_compact.txt", render_digest(FLEET, detailed=False))


def test_full_status_digest(golden) -> None:
    golden.text("digest_full.txt", render_digest(FLEET, detailed=True))


def test_compact_digest_hides_empty_escalations_and_run_hint_when_idle(golden) -> None:
    idle = {**FLEET, "runs": [], "escalations": []}
    golden.text("digest_compact_idle.txt", render_digest(idle, detailed=False))


def test_estimated_quota_retry_is_labeled_as_an_estimate(golden) -> None:
    status = {
        "project": "demo",
        "tickets": [],
        "runs": [{"id": "triage", "ticket": "DEMO-9", "status": "blocked",
                  "quota_recovery": {"provider": "claude", "next_retry": 1_800_000_300.5, "confirmed": 0}}],
    }
    golden.text("digest_quota_estimate.txt", render_digest(status))


def test_digest_is_byte_stable_under_input_reordering() -> None:
    shuffled = {**FLEET, "tickets": list(reversed(FLEET["tickets"])), "runs": list(reversed(FLEET["runs"]))}
    for detailed in (False, True):
        assert render_digest(shuffled, detailed=detailed) == render_digest(FLEET, detailed=detailed)
