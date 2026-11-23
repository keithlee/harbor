"""Durable operator-answer delivery receipts, with no detached subprocesses."""
import asyncio

from harbor.ticket_states import is_stopped, workflow_states


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS answer_queue (escalation TEXT PRIMARY KEY, project TEXT, ticket TEXT, run_id TEXT, instruction TEXT, status TEXT)")
    db.commit()


def enqueue(db,escalation,project,ticket,run_id,instruction):
    initialize(db)
    with db:
        db.execute("INSERT INTO answer_queue VALUES (?,?,?,?,?,'queued') ON CONFLICT(escalation) DO UPDATE SET instruction=excluded.instruction,status='queued' WHERE answer_queue.status IN ('failed','stopped')", (str(escalation),project,ticket,run_id,instruction))
    return db.execute("SELECT status FROM answer_queue WHERE escalation=?",(str(escalation),)).fetchone()[0]


async def deliver(supervisor,escalation,run_id,instruction):
    db = supervisor.db.connection
    with db:
        claimed = db.execute("UPDATE answer_queue SET status='dispatching' WHERE escalation=? AND status='queued'",(str(escalation),)).rowcount
    if not claimed:
        return False
    state = 'failed'
    try:
        resumed = await supervisor.resume_session(run_id,instruction)
        state = 'delivered' if resumed else 'failed'
        if not resumed:
            lookup = getattr(supervisor,'resume_failure_reason',None)
            reason = lookup(run_id) if callable(lookup) else None
            cause = f': {reason}' if reason else ''
            await supervisor._notify_operator(f'answer-failed:{escalation}',f'Answer {escalation} retained, but the builder could not resume{cause}. Use /retry after correcting its custody or state.')
        return resumed
    except asyncio.CancelledError:
        state = 'stopped'
        raise
    finally:
        with db:
            db.execute("UPDATE answer_queue SET status=? WHERE escalation=?",(state,str(escalation)))


def schedule(supervisor,row):
    from harbor.wake import Wake
    escalation,project,ticket,run_id,instruction,status = row
    if ticket in supervisor._dispatch_tasks and not supervisor._dispatch_tasks[ticket].done():
        return
    task = asyncio.create_task(deliver(supervisor,escalation,run_id,instruction),name=f'answer-{escalation}')
    supervisor._dispatch_tasks[ticket] = task
    supervisor._dispatch_wakes[ticket] = Wake(ticket={'identifier':ticket,'run_id':run_id},action='reengage',resume=run_id,instruction=instruction)
    task.add_done_callback(lambda done: supervisor._dispatch_finished(ticket,done))


def drain(supervisor):
    db = getattr(supervisor.db,'connection',None)
    if db is None or supervisor.read_only or supervisor._dispatch_suspended:
        return
    initialize(db)
    tickets = {t['identifier']:t for t in supervisor._fleet_tickets}
    for row in db.execute("SELECT * FROM answer_queue WHERE project=? AND status='queued'",(supervisor.project,)).fetchall():
        ticket = tickets.get(row[2])
        run = supervisor._latest_run(row[2]) or {}
        if not ticket or not supervisor._in_scope(ticket) or ticket.get('hold') or ticket.get('blocked_by'):
            continue
        # Human Review is deliberately not stopped: Harbor parks product-decision
        # escalations there, so an operator answer must still reach the builder.
        states = workflow_states(getattr(supervisor,'dispatcher',None))
        if run.get('id') != row[3] or is_stopped(ticket.get('state'),ticket.get('state_type'),states):
            continue
        if str(run.get('terminal_reason','')).startswith('operator'):
            with db:
                db.execute("UPDATE answer_queue SET status='stopped' WHERE escalation=?",(row[0],))
            continue
        schedule(supervisor,row)
