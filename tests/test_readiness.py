from harbor.config.workflow import load_workflow
from harbor.readiness import evaluate
from test_workflow_config import config

CTX = {"event":"pre_handoff","fresh":True,"hold":False,"gaps":{},"facts":{"url":"https://example.test/pr/1","head_sha":"a","mergeable":"MERGEABLE","ci":"green"}}


def test_only_harbor_verifiable_facts_gate_readiness(tmp_path):
    policy = load_workflow(config(tmp_path,"reviewed")).config
    assert evaluate(policy,CTX)["status"] == "pass"
    assert evaluate(policy,{**CTX,"event":"pre_merge"})["status"] == "fail"  # human merge
    for change in ({"fresh":False},{"hold":True},{"facts":{**CTX["facts"],"ci":"red"}},{"facts":{**CTX["facts"],"mergeable":"CONFLICTING"}}):
        assert evaluate(policy,{**CTX,**change})["status"] == "fail"
    # Review, risk and proof receipts belong to the skill and never gate here.
    assert "review" not in evaluate(policy,CTX)["reason"]


def test_feedback_is_advisory_by_default_and_ci_can_be_relaxed(tmp_path):
    policy = load_workflow(config(tmp_path,"reviewed",'[evidence]\nci="advisory"\nfeedback="advisory"\n')).config
    result = evaluate(policy,{**CTX,"linear_feedback":[{"id":"c"}],"facts":{**CTX["facts"],"ci":"red"}})
    assert result["status"] == "pass"
    assert result["warnings"] == ["required CI not green","unreplied Linear feedback"]
    policy["evidence"]["ci"] = "required"
    assert evaluate(policy,{**CTX,"facts":{**CTX["facts"],"ci":"red"}})["status"] == "fail"
