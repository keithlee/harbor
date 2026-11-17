from harbor.decision_policy import validate


def test_medium_ticket_choice_does_not_grant_merge_authority():
    blocker = {"options":["Approved wording"],"ticket_risk":"medium"}
    decision = {"answer":"Approved wording","citation":"project:bounded-decision","reason":"preapproved reversible wording"}
    assert validate({"mode":"bounded"},blocker,decision)["status"] == "pass"
    assert blocker["ticket_risk"] == "medium"
    assert validate({"mode":"human"},blocker,decision)["status"] == "blocked"
    assert validate({"mode":"cite_only"},blocker,decision)["status"] == "fail"
    assert validate({"mode":"bounded"},blocker,{**decision,"answer":"invented"})["status"] == "fail"


def test_configured_rationale_is_required():
    decision={'answer':'A','citation':'project:existing-policy'}
    assert validate({'mode':'cite_only','require_rationale':True},{},decision)['status']=='fail'
    assert validate({'mode':'cite_only','require_rationale':False},{},decision)['status']=='pass'


def test_ladder_cannot_fabricate_a_required_rationale(tmp_path):
    import asyncio,json
    from harbor.db import HarborDB
    from harbor.unblock import UnblockLadder
    from harbor.config.workflow import load_workflow
    from test_workflow_config import config
    docs=tmp_path/'docs';docs.mkdir()
    (docs/'DISCOVERIES.md').write_text('Use the existing reader cache when cache is unavailable.\n')
    workflow=load_workflow(config(tmp_path,extra='[decisions]\nrequire_rationale=true\n'))
    db=HarborDB(':memory:')
    calls=[]
    async def mate(*args):
        return {'disposition':'resolve','answer':'Use the existing reader cache.','citation':'docs/DISCOVERIES.md:1'}
    async def resume(*args):calls.append(args);return True
    ladder=UnblockLadder(db,first_mate=mate,resumer=resume,repo_path=tmp_path,remediation_executor=lambda _:False)
    ladder.workflow=workflow;ladder.workflow_project='Demo'
    result=asyncio.run(ladder.handle({'identifier':'SAMPLE-42','id':'linear-42','title':'Blocked','state':'In Progress'},{'id':'builder-session'},{'blocker':{'kind':'ambiguous_ac','question':'How should cache be unavailable?'}}))
    assert not result.resumed and not calls
    receipt=json.loads(db.connection.execute("SELECT payload FROM workflow_receipts WHERE kind='decision_policy'").fetchone()[0])
    assert receipt['status']=='fail' and receipt['blocking']
    db.close()
