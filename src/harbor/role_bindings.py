"""Agent identity is observed from runner events, never from review prose."""
import json
from harbor.resources import runtime_asset


def initialize(db):
    db.executescript(runtime_asset("migrations", "012_workflow_role_bindings.sql").read_text())


def observe(db, project, ticket, run_id, event):
    if not isinstance(event, dict):
        return
    payload = event.get("payload")
    item = event.get("item") or (payload.get("item") if isinstance(payload, dict) else None) or {}
    if isinstance(item, dict) and item.get("type") in ("SubAgentActivity", "sub_agent_activity"):
        _observe_sub_agent_activity(db, project, ticket, run_id, item)
        return
    if not isinstance(item, dict) or item.get("type") not in ("collab_agent_tool_call", "CollabAgentToolCall", "collab_tool_call"):
        return
    initialize(db)
    if item.get("status") != "completed":
        return
    tool = item.get("tool")
    ids = item.get("receiver_thread_ids", [])
    states = item.get("agents_states", {})
    if not isinstance(tool, str) or tool not in {"spawn_agent", "send_input", "resume_agent", "close_agent", "wait"}:
        return
    if not isinstance(ids, list) or not all(isinstance(agent, str) and agent for agent in ids):
        return
    if not isinstance(states, dict) or not all(isinstance(agent, str) and agent for agent in states):
        return
    with db:
        for agent in ids:
            if tool == "spawn_agent":
                db.execute("INSERT OR IGNORE INTO workflow_agents VALUES (?,?,?,?,?,NULL)", (project,ticket,run_id,agent,"running"))
            elif tool in {"send_input", "resume_agent"}:
                db.execute("UPDATE workflow_agents SET state='running', verdict=NULL WHERE project=? AND ticket=? AND run_id=? AND agent=?", (project,ticket,run_id,agent))
            elif tool == "close_agent":
                db.execute("UPDATE workflow_agents SET state='closed' WHERE project=? AND ticket=? AND run_id=? AND agent=?", (project,ticket,run_id,agent))
        for agent, state in states.items():
            # Current CLI serializes agent status as an externally tagged enum.
            if isinstance(state, str):
                state = {"status": state}
            elif isinstance(state, dict) and "status" not in state:
                if len(state) != 1:
                    continue
                key, value = next(iter(state.items()))
                state = {"status": key, "message": value} if key in {"completed", "errored", "shutdown"} else {}
            if not isinstance(state, dict):
                continue
            status = state.get("status")
            if not isinstance(status, str) or status not in {"completed", "errored", "shutdown"}:
                continue
            if status == "shutdown":
                db.execute("UPDATE workflow_agents SET state='closed' WHERE project=? AND ticket=? AND run_id=? AND agent=?", (project,ticket,run_id,agent))
                continue
            verdict = None
            try:
                message = json.loads(state.get("message") or "{}")
                review = message.get("review", {})
                if review.get("head_sha") and review.get("verdict") in {"approve", "reject"}:
                    verdict = json.dumps({"head_sha":review["head_sha"], "verdict":review["verdict"], "proof_verified":review.get("proof_verified") is True, "risk":review.get("risk"), "feedback_digest":review.get("feedback_digest")})
            except (ValueError, TypeError, AttributeError):
                pass
            db.execute("UPDATE workflow_agents SET state=?, verdict=? WHERE project=? AND ticket=? AND run_id=? AND agent=?", (status,verdict,project,ticket,run_id,agent))


def _observe_sub_agent_activity(db, project, ticket, run_id, item):
    # Multi-agent V2 names children by task path; the controller passes that path to `workflow role`.
    path = item.get("agent_path")
    thread = item.get("agent_thread_id")
    if item.get("kind") != "started" or not isinstance(path, str) or not path or path == "/root":
        return
    if not isinstance(thread, str) or not thread:
        return
    initialize(db)
    with db:
        db.execute("INSERT OR IGNORE INTO workflow_agents VALUES (?,?,?,?,?,NULL)", (project, ticket, run_id, path, "running"))


def register(db, project, ticket, run_id, role, agent, controller):
    initialize(db)
    if role not in {"planner", "reviewer", "implementor"} or agent == controller:
        raise ValueError("invalid role or self-review identity")
    if not db.execute("SELECT 1 FROM workflow_agents WHERE project=? AND ticket=? AND run_id=? AND agent=?", (project,ticket,run_id,agent)).fetchone():
        raise ValueError("agent was not observed by this run's adapter; cannot verify role provenance")
    bindings = dict(db.execute("SELECT role, agent FROM workflow_roles WHERE project=? AND ticket=? AND run_id=?", (project,ticket,run_id)))
    if role in bindings and bindings[role] != agent:
        raise ValueError("retained role cannot be replaced")
    other = "implementor" if role == "reviewer" else "reviewer" if role == "implementor" else None
    if other and bindings.get(other) == agent:
        raise ValueError("reviewer and implementor must differ")
    with db:
        db.execute("INSERT OR IGNORE INTO workflow_roles VALUES (?,?,?,?,?)", (project,ticket,run_id,role,agent))


def review_receipt(db, project, ticket, run_id, head):
    initialize(db)
    row = db.execute("SELECT a.verdict FROM workflow_roles r JOIN workflow_agents a USING(project,ticket,run_id,agent) WHERE r.project=? AND r.ticket=? AND r.run_id=? AND r.role='reviewer' AND a.state IN ('completed','closed')", (project,ticket,run_id)).fetchone()
    result = json.loads(row[0]) if row and row[0] else {}
    return result if result.get("head_sha") == head and result.get("verdict") == "approve" else {}
