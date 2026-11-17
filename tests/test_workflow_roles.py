import sqlite3
import pytest
from harbor.role_bindings import observe, register, review_receipt


def test_observed_roles_independence_and_revision():
    db = sqlite3.connect(":memory:")
    def event(tool, ids, states=None):
        observe(db,"A","A-1","run",{"item":{"type":"collab_agent_tool_call","status":"completed","tool":tool,"receiver_thread_ids":ids,"agents_states":states or {}}})
    event("spawn_agent",["sol","luna"])
    register(db,"A","A-1","run","planner","sol","controller")
    register(db,"A","A-1","run","reviewer","sol","controller")
    register(db,"A","A-1","run","implementor","luna","controller")
    with pytest.raises(ValueError):
        register(db,"A","A-1","run","reviewer","controller","controller")
    with pytest.raises(ValueError):
        register(db,"B","B-1","run","reviewer","sol","controller")
    with pytest.raises(ValueError):
        register(db,"A","A-1","run","implementor","sol","controller")
    event("wait",["sol"],{"sol":{"status":"completed","message":'{"review":{"head_sha":"abc","verdict":"approve","risk":"low","proof_verified":true}}'}})
    assert review_receipt(db,"A","A-1","run","abc")["risk"] == "low"
    assert not review_receipt(db,"A","A-1","run","changed")
    event("close_agent",["sol"],{"sol":{"status":"shutdown"}})
    assert review_receipt(db,"A","A-1","run","abc")["risk"] == "low"
    event("send_input",["sol"])
    assert not review_receipt(db,"A","A-1","run","abc")


@pytest.mark.parametrize("event_type", ["CollabAgentToolCall", "collab_tool_call"])
def test_current_cli_agent_events_and_tagged_verdict(event_type):
    db = sqlite3.connect(':memory:')
    def event(tool, states):
        observe(db,'A','A-1','r',{'item':{'type':event_type,'status':'completed','tool':tool,'receiver_thread_ids':['reviewer'],'agents_states':states}})
    event('spawn_agent', {'reviewer':'pending_init'})
    register(db,'A','A-1','r','reviewer','reviewer','controller')
    event('wait', {'reviewer':{'completed':'{"review":{"head_sha":"head","verdict":"approve","proof_verified":true,"risk":"low","feedback_digest":"digest"}}'}})
    assert review_receipt(db,'A','A-1','r','head')['feedback_digest']=='digest'
    event('close_agent', {'reviewer':'shutdown'})
    assert review_receipt(db,'A','A-1','r','head')['risk']=='low'
    event('send_input', {'reviewer':'running'})
    assert not review_receipt(db,'A','A-1','r','head')
    db.close()


@pytest.mark.parametrize('patch', [
    {'receiver_thread_ids':None}, {'receiver_thread_ids':'child'},
    {'receiver_thread_ids':['valid',None]}, {'agents_states':[]},
    {'agents_states':None}, {'tool':'unknown'}, {'tool':[]}, {'type':[]},
])
def test_malformed_agent_event_does_not_create_identity(patch):
    db=sqlite3.connect(':memory:')
    item={'type':'CollabAgentToolCall','tool':'spawn_agent','status':'completed','receiver_thread_ids':['child'],'agents_states':{}}
    item.update(patch)
    observe(db,'A','A-1','r',{'item':item})
    table=db.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_agents'").fetchone()
    assert not table or db.execute('SELECT count(*) FROM workflow_agents').fetchone()[0]==0
    db.close()


def test_malformed_envelope_is_ignored():
    db=sqlite3.connect(':memory:')
    for event in ({'payload':[1]}, {'item':['bad']}, None):
        observe(db,'A','A-1','r',event)
    db.close()


@pytest.mark.parametrize('state', [
    {'completed':'{"review":{"head_sha":"head","verdict":"approve"}}','errored':'failed'},
    {'status':[]}, {'status':{}},
])
def test_ambiguous_or_malformed_verdict_never_approves(state):
    db=sqlite3.connect(':memory:')
    item={'type':'CollabAgentToolCall','tool':'spawn_agent','status':'completed','receiver_thread_ids':['reviewer'],'agents_states':{}}
    observe(db,'A','A-1','r',{'item':item})
    register(db,'A','A-1','r','reviewer','reviewer','controller')
    item.update(tool='wait',agents_states={'reviewer':state})
    observe(db,'A','A-1','r',{'item':item})
    assert not review_receipt(db,'A','A-1','r','head')
