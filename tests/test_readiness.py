from harbor.config.workflow import load_workflow
from harbor.readiness import evaluate
from test_workflow_config import config


def test_human_minimal_and_review_requirements(tmp_path):
    ctx = {"event":"pre_handoff","fresh":True,"hold":False,"facts":{"url":"https://example.test/pr/1","head_sha":"a","mergeable":"MERGEABLE","ci":"green"}}
    minimal = load_workflow(config(tmp_path)).config
    assert evaluate(minimal,ctx)["status"] == "pass"
    assert evaluate(minimal,{**ctx,"event":"pre_merge"})["status"] == "fail"
    reviewed = load_workflow(config(tmp_path,"reviewed")).config
    assert evaluate(reviewed,ctx)["status"] == "fail"
    assert evaluate(reviewed,{**ctx,"gaps":{"risk":"valid"},"review_verified":True,"proof_verified":True})["status"] == "pass"
    assert evaluate(minimal,{**ctx,"fresh":False})["status"] == "fail"


def test_advisory_evidence_reports_failures_and_off_does_not_require_receipts(tmp_path):
    policy=load_workflow(config(tmp_path,'reviewed','[evidence]\nci="advisory"\nfeedback="off"\nrisk="off"\nproof="off"\n')).config
    context={'event':'pre_handoff','fresh':True,'hold':False,'review_verified':True,'facts':{'url':'https://example.test/pr/1','head_sha':'abc','mergeable':'MERGEABLE','ci':'red'}}
    result=evaluate(policy,context)
    assert result['status']=='pass' and result['warnings']==['required CI not green']
    assert set(result['disabled'])=={'feedback','risk','proof'}
    policy['evidence']['ci']='required'
    assert evaluate(policy,context)['status']=='fail'
