"""Adversarial regressions from the independent pre-PR review."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from harbor.db import HarborDB
from harbor.recovery import BlockerPolicy
from harbor.runner.base import apply_phase4_migration
from harbor.supervisor import Supervisor
from harbor.unblock import UnblockLadder, triage
from harbor.wake import Wake, apply_phase3_migration


def test_current_ac_is_not_filtered_out_by_precedent_keywords(tmp_path):
    db = HarborDB(':memory:')
    db.replace_tickets('mock', [{'identifier':'MOCK-1','title':'Color','state':'Blocked'}])
    db.record_resolution('product_decision','Which button color?','Use blue','operator:prior',source_ticket='MOCK-1')
    prompts = []
    async def mate(prompt, **kwargs):
        prompts.append(prompt)
        return {'disposition':'escalate','reason':'Current AC conflicts with the prior answer.'}
    result = asyncio.run(triage(
        {'identifier':'MOCK-1','project':'mock','description':'Render the control in red; blue is forbidden.'},
        {'kind':'product_decision','question':'Which button color?'},
        db=db, first_mate=mate, repo_path=tmp_path, policy=BlockerPolicy()))
    assert not result.resolved
    assert len(prompts) == 1 and 'blue is forbidden' in prompts[0] and 'Use blue' in prompts[0]
    db.close()


@pytest.mark.parametrize('author', [None, {}, {'id':''}, {'id':'outsider'}, {'id':'owner'}])
def test_restricted_decision_authors_are_checked_before_delivery(author):
    resumes = []
    class Dispatcher:
        async def resume(self, wake):
            resumes.append(wake)
            return {'status':'waiting','summary':'accepted'}
    poller = SimpleNamespace(project_name='mock',config=SimpleNamespace(blocker_policy=BlockerPolicy(authorized_authors=('owner',))))
    supervisor = Supervisor(poller,dispatcher=Dispatcher())
    wake = Wake(ticket={'identifier':'MOCK-1'},action='reengage',resume='retained',reason='linear-comment-version',
                facts={'linear_comment':{'body':'Answer: use a different policy','author':author}})
    asyncio.run(supervisor._deliver_wake(wake))
    assert len(resumes) == (1 if author == {'id':'owner'} else 0)


@pytest.mark.parametrize('state,kind', [('Backlog','backlog'),('Paused','started'),('Done','completed'),('Canceled','canceled')])
def test_fresh_stopped_ticket_cannot_reach_remediation_or_resume(tmp_path,state,kind):
    class Linear:
        async def fetch_blocker_context(self, identifier):
            return {'identifier':identifier,'description':'Use blue.','state':{'name':state,'type':kind}}
    async def forbidden(*args, **kwargs):
        raise AssertionError('stopped ticket reached a mutation or model')
    ladder = UnblockLadder(linear=Linear(),first_mate=forbidden,resumer=forbidden,remediation_executor=forbidden,repo_path=tmp_path)
    with pytest.raises(ValueError,match='no longer active'):
        asyncio.run(ladder.handle({'identifier':'MOCK-1'}, {'id':'retained'}, {'blocker':{'kind':'env_blocker','question':'Cannot find module'}}))


def test_actual_first_mate_prompt_contains_custody_attempts_and_next_step(tmp_path):
    db = HarborDB(':memory:')
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    db.replace_tickets('mock',[{'identifier':'MOCK-1','title':'Color','state':'Blocked'}])
    db.connection.execute('INSERT INTO runs(id,ticket,status,session_id,worktree,deadline_seconds) VALUES (?,?,?,?,?,?)',
                          ('logical','MOCK-1','blocked','native',str(tmp_path),123))
    db.connection.commit()
    db.record_escalation('MOCK-1','logical','ambiguous_ac','Which color?',0,status='resolved')
    prompts = []
    class Linear:
        async def fetch_blocker_context(self, identifier):
            return {'identifier':identifier,'description':'The button color is blue.','state':{'name':'Blocked','type':'started'}}
    async def mate(prompt, **kwargs):
        prompts.append(prompt)
        return {'disposition':'escalate','reason':'A human decision is required.'}
    ladder = UnblockLadder(db,linear=Linear(),first_mate=mate,repo_path=tmp_path)
    supervisor = Supervisor(SimpleNamespace(project_name='mock',config=SimpleNamespace()),db=db,unblock_ladder=ladder)
    wake = Wake(ticket={'identifier':'MOCK-1'},action='reengage',resume='logical')
    final = {'status':'blocked','run_id':'logical','summary':'Color selection failed','next_action':'Apply the approved style and rerun the UI test',
             'blocker':{'kind':'ambiguous_ac','question':'Which color?','evidence':'The style assertion failed'}}
    asyncio.run(supervisor._handle_runner_result(wake,final))
    assert len(prompts) == 1, supervisor.last_error
    packet = json.loads(next(line.removeprefix('Blocker context: ') for line in prompts[0].splitlines() if line.startswith('Blocker context: ')))
    assert (packet['ticket'],packet['run_id'],packet['session_id'],packet['worktree']) == ('MOCK-1','logical','native',str(tmp_path))
    assert packet['attempts'] == {'deterministic':1,'first_mate':0}
    assert packet['remaining_execution_seconds'] == 123
    assert packet['failed_operation'] == 'The style assertion failed'
    assert packet['next_action'] == final['next_action']
    assert packet['current_facts']['state'] == 'Blocked'
    db.close()


@pytest.mark.parametrize('relations', [None, {'nodes':[{'type':'blocks','issue':{'identifier':'MOCK-2','state':{'type':'started'}}}]}])
def test_fresh_or_retained_dependency_prevents_unblock(tmp_path, relations):
    class Linear:
        async def fetch_blocker_context(self, identifier):
            issue = {'identifier':identifier,'state':{'name':'Blocked','type':'started'}}
            if relations is not None:
                issue['inverseRelations'] = relations
            return issue
    async def forbidden(*args, **kwargs):
        raise AssertionError('dependency-blocked ticket reached a mutation or model')
    ladder = UnblockLadder(linear=Linear(),first_mate=forbidden,resumer=forbidden,remediation_executor=forbidden,repo_path=tmp_path)
    with pytest.raises(ValueError,match='unresolved dependencies'):
        asyncio.run(ladder.handle({'identifier':'MOCK-1','blocked_by':['MOCK-2'] if relations is None else []},
                                  {'id':'retained'}, {'blocker':{'kind':'env_blocker','question':'Cannot find module'}}))


@pytest.mark.parametrize('incomplete', [None, 'attachments', 'inverseRelations'])
def test_blocker_context_fetches_dependency_and_pr_facts(incomplete):
    import httpx
    from harbor.clients.linear import LinearClient, LinearClientError
    def transport(request):
        payload = json.loads(request.content)
        assert 'inverseRelations(first: 100)' in payload['query']
        assert 'attachments(first: 100)' in payload['query']
        issue = {'identifier':'MOCK-1','comments':{'nodes':[],'pageInfo':{'hasNextPage':False}},
                 'attachments':{'nodes':[{'url':'https://github.com/mock/repo/pull/1','sourceType':'github'}],'pageInfo':{'hasNextPage':incomplete == 'attachments'}},
                 'inverseRelations':{'nodes':[{'type':'blocks','issue':{'identifier':'MOCK-2','state':{'type':'started'}}}],'pageInfo':{'hasNextPage':incomplete == 'inverseRelations'}}}
        return httpx.Response(200,json={'data':{'issue':issue}})
    async def run():
        client = LinearClient(token='mock',transport=httpx.MockTransport(transport))
        try:
            if incomplete:
                with pytest.raises(LinearClientError,match='refusing partial context'):
                    await client.fetch_blocker_context('MOCK-1')
            else:
                issue = await client.fetch_blocker_context('MOCK-1')
                assert issue['attachments']['nodes'][0]['url'].endswith('/pull/1')
                assert issue['inverseRelations']['nodes'][0]['issue']['identifier'] == 'MOCK-2'
        finally:
            await client.close()
    asyncio.run(run())


def test_deterministic_remedy_resume_includes_authority_and_next_action():
    prompts = []
    async def execute(command):
        return True
    async def resume(run_id, prompt):
        prompts.append(prompt)
        return True
    result = asyncio.run(UnblockLadder(remediation_executor=execute,resumer=resume).handle(
        {'identifier':'MOCK-1'}, {'id':'retained'},
        {'blocker':{'kind':'env_blocker','question':'Cannot find module checkout'},
         'next_action':'Rerun pnpm test --filter checkout and verify the checkout AC.'}))
    assert result.resumed and result.citation.startswith('deterministic:')
    assert 'Authority citation: '+result.citation in prompts[0]
    assert 'Next step: Rerun pnpm test --filter checkout' in prompts[0]
    assert 'Answer: '+result.answer in prompts[0]

@pytest.mark.parametrize('base_state', ['remote_only', 'stale_local', 'missing'])
def test_blocker_path_audit_uses_configured_remote_base(tmp_path, base_state):
    import subprocess
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True)
    git('init', '-b', 'main')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '--allow-empty', '-m', 'base')
    base = git('rev-parse', 'HEAD').stdout.strip()
    git('checkout', '-b', 'feature')
    if base_state != 'missing':
        git('update-ref', 'refs/remotes/origin/main', base)
    if base_state != 'stale_local':
        git('branch', '-D', 'main')
    # A stale local branch must not hide a protected-path change from audit.
    (repo / 'secret-policy.py').write_text('protected change\n')
    git('add', 'secret-policy.py')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-m', 'change')
    if base_state == 'stale_local':
        git('branch', '-f', 'main', 'HEAD')
    calls = []
    async def mate(*args, **kwargs):
        calls.append(args)
        raise AssertionError('protected changes must not reach model resolution')
    ladder = UnblockLadder(first_mate=mate, repo_path=repo, protected_paths=('secret-policy.py',))
    args = ({'identifier':'MOCK-BASE','description':'Preapproved wording.'}, {'id':'retained','worktree':str(repo)},
            {'blocker':{'kind':'product_decision','question':'Which wording?','options':['A','B']}})
    if base_state == 'missing':
        with pytest.raises(ValueError, match='configured base branch'):
            asyncio.run(ladder.handle(*args))
    else:
        result = asyncio.run(ladder.handle(*args))
        assert not result.resumed and result.rung == 2 and result.reason == 'protected path'
    assert not calls


def test_remote_only_base_allows_cited_wording_resume(tmp_path):
    import subprocess
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True)
    git('init', '-b', 'main')
    git('-c','user.name=Test','-c','user.email=test@example.test','commit','--allow-empty','-m','base')
    git('update-ref','refs/remotes/origin/main',git('rev-parse','HEAD').stdout.strip())
    git('checkout','-b','feature')
    git('branch','-D','main')
    resumes=[]
    async def mate(*args, **kwargs):
        return {'disposition':'resolve','answer':'A','citation':'project:bounded-decision','reason':'Preapproved reversible wording within the existing AC.'}
    async def resume(run_id, prompt):
        resumes.append((run_id, prompt))
        return True
    ladder=UnblockLadder(first_mate=mate,resumer=resume,repo_path=repo,policy=BlockerPolicy())
    result=asyncio.run(ladder.handle({'identifier':'MOCK-BASE','description':'Use A or B; both preapproved. Risk: medium.'},
        {'id':'retained','worktree':str(repo)}, {'next_action':'Apply wording and preserve medium risk.',
        'blocker':{'kind':'product_decision','question':'Which wording?','options':['A','B']}}))
    assert result.resumed and result.answer=='A' and result.reason
    assert len(resumes)==1 and resumes[0][0]=='retained'
    assert 'preserve medium risk' in resumes[0][1]


@pytest.mark.parametrize('state', ['In Progress', 'Human Review', 'human review'])
def test_second_blocker_from_builder_resumed_in_human_review_is_escalated(tmp_path, state):
    """An operator answer delivered on a Human Review ticket resumes the builder;
    a second blocker it reports must be escalated and paged, not silently dropped."""
    from harbor.supervisor import WorktreeDispatcher
    db = HarborDB(':memory:')
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    db.replace_tickets('mock', [{'identifier': 'MOCK-1', 'title': 'Color', 'state': state}])
    db.connection.execute('INSERT INTO runs(id,ticket,status,session_id,worktree) VALUES (?,?,?,?,?)',
                          ('retained', 'MOCK-1', 'blocked', 'native', str(tmp_path)))
    db.connection.commit()
    db.record_escalation('MOCK-1', 'retained', 'product_decision', 'Which color?', 2)

    class Linear:
        def __init__(self):
            self.comments, self.states = [], []
        async def fetch_blocker_context(self, identifier):
            return {'identifier': identifier, 'id': 'linear-1', 'description': 'Pick a color.',
                    'state': {'name': state, 'type': 'started'}}
        async def create_comment(self, issue_id, body):
            self.comments.append((issue_id, body))
            return {'id': f'comment-{len(self.comments)}'}
        async def update_issue_state(self, issue_id, name, **kwargs):
            self.states.append((issue_id, name))
            return True

    async def mate(prompt, **kwargs):
        return {'disposition': 'escalate', 'reason': 'A human decision is required.'}

    notices = []
    linear = Linear()
    ladder = UnblockLadder(db, linear=linear, first_mate=mate, notifier=notices.append,
                           repo_path=tmp_path, remediation_executor=lambda command: False)

    class Dispatcher(WorktreeDispatcher):
        def __init__(self):
            self.resumed = []
        async def resume(self, wake):
            self.resumed.append(wake)
            # The builder applied the operator's answer, then hit a new blocker.
            return {'status': 'blocked', 'run_id': 'retained', 'summary': 'Font choice is unspecified',
                    'blocker': {'kind': 'product_decision', 'question': 'Which font?'}}

    dispatcher = Dispatcher()
    supervisor = Supervisor(SimpleNamespace(project_name='mock', config=SimpleNamespace()),
                            db=db, dispatcher=dispatcher, unblock_ladder=ladder)
    assert asyncio.run(supervisor.resume_session('retained', 'Answer: use blue'))
    assert len(dispatcher.resumed) == 1
    assert supervisor.last_error is None, supervisor.last_error
    assert supervisor.last_unblock is not None and supervisor.last_unblock.rung == 2
    opened = [row for row in db.escalations() if row['question'] == 'Which font?']
    assert len(opened) == 1 and opened[0]['rung_resolved'] == 2 and opened[0]['notified']
    assert len(notices) == 1 and 'Which font?' in notices[0]
    db.close()


@pytest.mark.parametrize('state,escalates', [
    ('On Hold', False), ('on hold', False),   # workflow [states] paused
    ('Shipped', False), ('SHIPPED', False),   # workflow [states] terminal
    ('In Progress', True), ('Human Review', True),  # controls: still active
])
def test_blocked_final_in_workflow_configured_stopped_state_is_refused(tmp_path, state, escalates):
    """A blocked final on a ticket the operator moved to a workflow-configured
    paused or terminal state (Linear type ``started``) must not resume the
    builder, run remediation, or page the operator."""
    from harbor.config.workflow import load_workflow
    from harbor.supervisor import WorktreeDispatcher
    from workflow_support import config
    workflow = load_workflow(config(tmp_path / 'project', extra='[states]\npaused=["On Hold"]\nterminal=["Shipped"]\n'))
    db = HarborDB(':memory:')
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    db.replace_tickets('mock', [{'identifier': 'MOCK-1', 'title': 'Color', 'state': state}])
    db.connection.execute('INSERT INTO runs(id,ticket,status,session_id,worktree) VALUES (?,?,?,?,?)',
                          ('retained', 'MOCK-1', 'running', 'native', str(tmp_path)))
    db.connection.commit()

    class Linear:
        def __init__(self):
            self.comments, self.states = [], []
        async def fetch_blocker_context(self, identifier):
            return {'identifier': identifier, 'id': 'linear-1', 'description': 'Pick a color.',
                    'state': {'name': state, 'type': 'started'}}
        async def create_comment(self, issue_id, body):
            self.comments.append((issue_id, body))
            return {'id': f'comment-{len(self.comments)}'}
        async def update_issue_state(self, issue_id, name, **kwargs):
            self.states.append((issue_id, name))
            return True

    calls = []
    async def mate(prompt, **kwargs):
        calls.append('first_mate')
        return {'disposition': 'escalate', 'reason': 'A human decision is required.'}
    async def resume(run_id, prompt):
        calls.append('resume')
        return True
    def remediate(command):
        calls.append('remediation')
        return False

    notices = []
    linear = Linear()
    ladder = UnblockLadder(db, linear=linear, first_mate=mate, notifier=notices.append, resumer=resume,
                           repo_path=tmp_path, remediation_executor=remediate)

    class Dispatcher(WorktreeDispatcher):
        def __init__(self):
            self.workflow = workflow

    # Production wiring (``harbor start``) hands the ladder the dispatcher's workflow.
    ladder.workflow, ladder.workflow_project = workflow, 'mock'
    supervisor = Supervisor(SimpleNamespace(project_name='mock', config=SimpleNamespace()),
                            db=db, dispatcher=Dispatcher(), unblock_ladder=ladder)
    wake = Wake(ticket={'identifier': 'MOCK-1'}, action='reengage', resume='retained')
    final = {'status': 'blocked', 'run_id': 'retained', 'summary': 'Font choice is unspecified',
             'blocker': {'kind': 'product_decision', 'question': 'Which font?'}}
    asyncio.run(supervisor._handle_runner_result(wake, final))
    opened = [row for row in db.escalations() if row['question'] == 'Which font?']
    if escalates:
        assert supervisor.last_error is None, supervisor.last_error
        assert supervisor.last_unblock is not None and supervisor.last_unblock.rung == 2
        assert len(opened) == 1 and len(notices) == 1
    else:
        assert supervisor.last_error == 'blocked ticket is no longer active'
        assert supervisor.last_unblock is None
        assert calls == [] and opened == [] and notices == []
        assert linear.comments == [] and linear.states == []
    db.close()
