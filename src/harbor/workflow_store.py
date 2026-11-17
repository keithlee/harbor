"""Operational workflow snapshots and receipts; never external source of truth."""
import json
from harbor.resources import runtime_asset


def initialize(connection):
    connection.executescript(runtime_asset("migrations", "011_workflow_receipts.sql").read_text())


def bind(connection, project, ticket, run_id, workflow):
    workflow.verify_package()
    initialize(connection)
    row = connection.execute("SELECT policy_digest FROM workflow_runs WHERE project=? AND ticket=? AND run_id=?", (project, ticket, run_id)).fetchone()
    if row and row[0] != workflow.digest:
        raise ValueError("workflow changed: retained run requires explicit policy reconciliation")
    with connection:
        connection.execute("INSERT OR IGNORE INTO workflow_runs VALUES (?,?,?,?,?)", (project, ticket, run_id, workflow.digest, json.dumps(workflow.config, sort_keys=True)))


def record(connection, receipt):
    initialize(connection)
    with connection:
        connection.execute("INSERT INTO workflow_receipts(project,ticket,run_id,kind,payload) VALUES (?,?,?,?,?)",
            (receipt["project"], receipt["ticket"], receipt["run_id"], receipt.get("hook", "readiness"), json.dumps(receipt, sort_keys=True)))


def reconcile(connection,project,ticket,run,workflow,expected):
    initialize(connection)
    row = connection.execute("SELECT policy_digest FROM workflow_runs WHERE project=? AND ticket=? AND run_id=?",(project,ticket,run['id'])).fetchone()
    if not row or row[0] != expected or run.get('status') in {'running','starting'}:
        raise ValueError("reconciliation requires the previous digest and an inactive retained run")
    from harbor.role_bindings import initialize as roles_init
    roles_init(connection)
    if connection.execute("SELECT 1 FROM workflow_agents WHERE project=? AND ticket=? AND run_id=? AND state='running'",(project,ticket,run['id'])).fetchone():
        raise ValueError("stop active child agents before reconciliation")
    with connection:
        connection.execute("UPDATE workflow_runs SET policy_digest=?,config_json=? WHERE project=? AND ticket=? AND run_id=?",(workflow.digest,json.dumps(workflow.config,sort_keys=True),project,ticket,run['id']))
        connection.execute("UPDATE workflow_agents SET verdict=NULL WHERE project=? AND ticket=? AND run_id=?",(project,ticket,run['id']))
    record(connection,{"project":project,"ticket":ticket,"run_id":run['id'],"hook":"policy_reconciliation","previous":expected,"policy_digest":workflow.digest})
