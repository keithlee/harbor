"""Pure policy evaluation over a freshly collected, scoped evidence context."""


def evaluate(policy, context):
    facts = context.get("facts", {})
    event = context.get("event", "pre_merge")
    failures = []
    if not context.get("fresh"):
        failures.append("fresh external evidence unavailable")
    if context.get("hold", True):
        failures.append("ticket is held or hold state unknown")
    if not facts.get("head_sha") or not facts.get("url"):
        failures.append("PR identity/head unavailable")
    if facts.get("merged") and event == "pre_merge":
        failures.append("PR already merged; reconcile completion instead")
    if not facts.get("merged") and str(facts.get("mergeable", "")).upper() != "MERGEABLE":
        failures.append("PR is not confirmed mergeable")
    ci = facts.get("ci")
    ci = ci.get("state", ci.get("status")) if isinstance(ci, dict) else ci
    evidence = policy.get("evidence", {})
    review_required = policy["review"]["enabled"]
    gaps = context.get("gaps")
    warnings, disabled = [], []
    problems = {
        "ci": [] if ci in {"green", "success", "passed"} else ["required CI not green"],
        "feedback": (["feedback receipts unavailable"] if gaps is None else
                     ["unreplied feedback"] if gaps.get("unreplied") or gaps.get("unreplied_comments") else [])
                    + (["unreplied Linear feedback"] if context.get("linear_feedback") else []),
        "risk": [] if gaps is not None and gaps.get("risk") in {"valid", "current", "satisfied"} else ["current-head risk receipt missing"],
        "proof": [] if context.get("proof_verified") else ["required proof receipt unavailable"],
    }
    for category, issues in problems.items():
        mode = evidence.get(category,"required" if category == "ci" or review_required else "off")
        if mode == "required":
            failures.extend(issues)
        elif mode == "advisory":
            warnings.extend(issues)
        else:
            disabled.append(category)
    if review_required and not context.get("review_verified"):
        failures.append("independent current-head reviewer receipt unavailable")
    if event == "pre_merge":
        if policy["delivery"]["merge_mode"] != "workflow_checked":
            failures.append("human merge required")
        if context.get("risk") not in policy["delivery"].get("auto_merge_risks", []):
            failures.append("risk is not eligible for automatic merge")
    return {"status": "fail" if failures else "pass", "reason": "; ".join(failures) or "configured requirements satisfied",
            "evidence": [facts["url"]] if facts.get("url") else [], "enforcement": "workflow_checked", "review_required": review_required, "warnings": warnings, "disabled": disabled}
