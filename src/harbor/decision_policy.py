"""Validate proposed bounded answers without granting delivery authority."""


def validate(policy, blocker, decision):
    mode = policy["mode"]
    if mode == "human":
        return {"status":"blocked","reason":"human decision required"}
    if not decision.get("answer") or (policy.get("require_citation",True) and not decision.get("citation")):
        return {"status":"fail","reason":"answer and authority citation required"}
    if policy.get("require_rationale",True) and not decision.get("reason"):
        return {"status":"fail","reason":"decision rationale required"}
    if decision.get("citation") == "project:bounded-decision":
        if mode != "bounded" or decision["answer"] not in blocker.get("options", []) or not decision.get("reason"):
            return {"status":"fail","reason":"bounded decision lacks supplied option or rationale"}
    return {"status":"pass","reason":"decision authority validated; merge authority unchanged"}
