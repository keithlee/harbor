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


def _v2(path="/root/planner", thread="thread-1", kind="started", item_type="SubAgentActivity", **extra):
    item = {"type": item_type, "kind": kind, "agent_thread_id": thread, "agent_path": path, "model": "gpt-6.2-sol"}
    item.update(extra)
    return {"item": item}


def test_v2_sub_agent_start_registers_by_path_and_keeps_independence():
    db = sqlite3.connect(":memory:")
    observe(db, "A", "A-1", "r", _v2("/root/planner", "t-planner"))
    observe(db, "A", "A-1", "r", {"payload": _v2("/root/impl", "t-impl")})
    register(db, "A", "A-1", "r", "planner", "/root/planner", "controller")
    register(db, "A", "A-1", "r", "implementor", "/root/impl", "controller")
    with pytest.raises(ValueError):
        register(db, "A", "A-1", "r", "reviewer", "/root/impl", "controller")
    with pytest.raises(ValueError):
        register(db, "A", "A-1", "r", "reviewer", "/root/never-spawned", "controller")
    with pytest.raises(ValueError):
        register(db, "A", "A-1", "r", "reviewer", "t-planner", "controller")
    db.close()


@pytest.mark.parametrize("patch", [
    {"kind": "completed"}, {"kind": None}, {"agent_path": None}, {"agent_path": ""},
    {"agent_path": ["/root/x"]}, {"agent_thread_id": None}, {"agent_thread_id": ""},
    {"agent_path": "/root"},
])
def test_v2_malformed_or_root_activity_does_not_create_identity(patch):
    db = sqlite3.connect(":memory:")
    event = _v2()
    event["item"].update(patch)
    observe(db, "A", "A-1", "r", event)
    table = db.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_agents'").fetchone()
    assert not table or db.execute("SELECT count(*) FROM workflow_agents").fetchone()[0] == 0
    db.close()


def test_v2_activity_is_scoped_to_its_run():
    db = sqlite3.connect(":memory:")
    observe(db, "A", "A-1", "r1", _v2())
    with pytest.raises(ValueError):
        register(db, "A", "A-1", "r2", "planner", "/root/planner", "controller")
    db.close()


def test_v2_exec_stream_snake_case_item_type_is_observed():
    db = sqlite3.connect(":memory:")
    observe(db, "A", "A-1", "r", _v2("/root/reviewer", "t-r", item_type="sub_agent_activity"))
    register(db, "A", "A-1", "r", "reviewer", "/root/reviewer", "controller")
    db.close()
