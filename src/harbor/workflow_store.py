"""Operational workflow snapshots and receipts; never external source of truth."""
import json
from harbor.resources import runtime_asset


def initialize(connection):
    connection.executescript(runtime_asset("migrations", "011_workflow_receipts.sql").read_text())


def bind(connection, project, ticket, run_id, workflow):
    """Snapshot the policy a run uses. Edits never block a run: a changed
    package is adopted for the next turn and recorded as a receipt."""
    initialize(connection)
    row = connection.execute("SELECT policy_digest FROM workflow_runs WHERE project=? AND ticket=? AND run_id=?", (project, ticket, run_id)).fetchone()
    config = json.dumps(workflow.config, sort_keys=True)
    with connection:
        if row is None:
            connection.execute("INSERT INTO workflow_runs VALUES (?,?,?,?,?)", (project, ticket, run_id, workflow.digest, config))
            return
        if row[0] == workflow.digest:
            return
        connection.execute("UPDATE workflow_runs SET policy_digest=?, config_json=? WHERE project=? AND ticket=? AND run_id=?",
                           (workflow.digest, config, project, ticket, run_id))
    record(connection, {"project": project, "ticket": ticket, "run_id": run_id, "hook": "policy_change",
                        "previous": row[0], "policy_digest": workflow.digest})


def record(connection, receipt):
    initialize(connection)
    with connection:
        connection.execute("INSERT INTO workflow_receipts(project,ticket,run_id,kind,payload) VALUES (?,?,?,?,?)",
            (receipt["project"], receipt["ticket"], receipt["run_id"], receipt.get("hook", "readiness"), json.dumps(receipt, sort_keys=True)))
