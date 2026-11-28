"""Workflow event integration and conservative cleanup eligibility."""
from datetime import datetime, timezone

from harbor.hooks import execute_event
from harbor.workflow_store import bind, record
from harbor.safe_git import UnsafeGitDirectory, run_worktree_git


async def handoff(supervisor,wake,result):
    from harbor.workflow_cli import fresh_context
    workflow = supervisor.dispatcher.workflow
    identifier = supervisor._ticket_key(wake.ticket)
    run = supervisor._latest_run(identifier)
    if not run or run['id'] != supervisor._run_id_for_result(wake,result):
        raise ValueError("handoff has no exact retained run")
    bind(supervisor.db.connection,supervisor.project,identifier,run['id'],workflow)
    context = await fresh_context(supervisor.poller.config,supervisor.db,identifier,{**run,"pr_url":result.get("pr_url")},"pre_handoff")
    receipts = await execute_event(workflow,"pre_handoff",context,cwd=run['worktree'])
    for receipt in receipts:
        record(supervisor.db.connection,receipt)
    failures = [r['reason'] for r in receipts if r['blocking']]
    if failures:
        raise ValueError("; ".join(failures))
    return context


async def cleanup_allowed(supervisor,wake,result,target,run_id):
    workflow = getattr(supervisor.dispatcher, "workflow", None)
    if not workflow:  # only unit-test fakes run without a project workflow
        return True
    policy = workflow.config['cleanup']
    if policy['mode'] != 'after_merge':
        return False
    identifier = supervisor._ticket_key(wake.ticket)
    db = supervisor.db.connection
    run = supervisor._latest_run(identifier)
    if not run or run['id'] != run_id or run.get('status') != 'completed':
        return False
    from harbor.runner.base import process_alive, process_group_alive
    if process_alive(run.get('pid')) or process_group_alive(run.get('pgid')):
        return False
    if db.execute("SELECT 1 FROM escalations WHERE ticket=? AND status IN ('open','resume_pending')",(identifier,)).fetchone():
        return False
    ended = run.get('finished_at')
    if not ended:
        return False
    elapsed = (datetime.now(timezone.utc)-datetime.fromisoformat(ended.replace('Z','+00:00')).replace(tzinfo=timezone.utc)).total_seconds()
    if elapsed < policy['delay_seconds']:
        return False
    # Re-fetch after restart and immediately before deletion. A saved merge bit
    # cannot authorize deletion of newer local commits or a held ticket.
    context = await handoff(supervisor,wake,result)
    if not context['facts'].get('merged') or not context.get('terminal'):
        return False
    # The builder controls ``target``: refuse it unless its git metadata is the
    # project repository's, and read HEAD with no hooks or credentials.
    manager = getattr(supervisor, 'worktree_manager', None) or getattr(supervisor.dispatcher, 'worktree_manager', None)
    repo = getattr(manager, 'repo', None)
    if repo is None:
        return False
    try:
        head = run_worktree_git(target, repo, 'rev-parse', 'HEAD', check=True).stdout.strip()
    except UnsafeGitDirectory:
        return False
    if head != context['head_sha']:
        return False
    receipts = await execute_event(workflow,'pre_cleanup',context,cwd=target)
    for receipt in receipts:
        record(db,receipt)
    return not any(r['blocking'] for r in receipts)
