"""A resumed creation must remain bound to its session and document base."""

import concurrent.futures
import importlib
import json
import threading

import httpx
import pytest

from creation.agent_loop import CreationAgentLoop
from creation.operations import OperationError
from creation.service import CreationOptions


BASE = '## Plan\nMonday meeting.\n'
EDITED = '## Plan\nTuesday meeting.\n'
DECISION = {'tools': [], 'agents': [], 'operation': {'kind': 'patch', 'patches': [
    {'action': 'replace', 'target': {'text': 'Monday'}, 'content': 'Tuesday'}]}}


class ResumeService:
    def __init__(self):
        self.route_calls = 0

    async def route_capabilities(self, **kwargs):
        self.route_calls += 1
        return {**DECISION, 'source': 'model'}

    def build_routing_prompts(self, *args):
        return 'routing', 'context'

    def parse_routing_decision(self, value):
        return json.loads(value)

    @staticmethod
    def _clip(value, limit):
        return value[:limit]


def args(document=BASE, session='session-a'):
    return {'user_message': 'Replace Monday with Tuesday', 'root_request': 'Write a plan',
            'current_document': document, 'conversation': [], 'selected_skills': [],
            'options': CreationOptions(), 'session_id': session, 'run_id': 'run-a'}


async def paused_route(loop):
    events = [event async for event in loop.run(**args(), model_mode='external')]
    return events[-1]['data']['continuation']


async def completed_patch(loop):
    events = [event async for event in loop.run(**args())]
    assert events[-1]['data']['document'] == EDITED
    return [event['data']['checkpoint'] for event in events if event['type'] == 'operation.checkpoint'][-1]


@pytest.mark.asyncio
async def test_external_resume_cannot_apply_a_different_sessions_checkpoint():
    loop = CreationAgentLoop(ResumeService())
    checkpoint = await paused_route(loop)
    with pytest.raises(OperationError) as error:
        [event async for event in loop.run(**args(session='session-b'), resume_state=checkpoint,
                                          model_result=json.dumps(DECISION))]
    assert error.value.code == 'CREATION_RESUME_MISSING'


@pytest.mark.asyncio
@pytest.mark.parametrize('document', ['## Plan\nManual newer edit.\n', ''])
async def test_external_resume_cannot_overwrite_intervening_document_edits(document):
    loop = CreationAgentLoop(ResumeService())
    checkpoint = await paused_route(loop)
    with pytest.raises(OperationError) as error:
        [event async for event in loop.run(**args(document), resume_state=checkpoint,
                                          model_result=json.dumps(DECISION))]
    assert error.value.code == 'CREATION_BASE_CHANGED'


@pytest.mark.asyncio
async def test_retry_checkpoint_cannot_overwrite_an_unrelated_current_document():
    loop = CreationAgentLoop(ResumeService())
    checkpoint = await completed_patch(loop)
    with pytest.raises(OperationError) as error:
        [event async for event in loop.run(**args('## Plan\nManual newer edit.\n'),
                                          resume_checkpoint=checkpoint)]
    assert error.value.code == 'CREATION_BASE_CHANGED'


@pytest.mark.asyncio
@pytest.mark.parametrize('document', [BASE, EDITED])
async def test_completed_checkpoint_accepts_its_base_or_saved_result_without_reapplying(document):
    service = ResumeService()
    loop = CreationAgentLoop(service)
    checkpoint = await completed_patch(loop)
    events = [event async for event in loop.run(**args(document), resume_checkpoint=checkpoint)]
    assert events[-1]['data']['document'] == EDITED
    assert not any(event['type'] == 'document.patch.applied' for event in events)
    assert service.route_calls == 1


@pytest.mark.asyncio
async def test_external_resume_accepts_its_unchanged_base():
    loop = CreationAgentLoop(ResumeService())
    checkpoint = await paused_route(loop)
    events = [event async for event in loop.run(**args(), resume_state=checkpoint,
                                               model_result=json.dumps(DECISION))]
    assert events[-1]['data']['document'] == EDITED


@pytest.mark.asyncio
@pytest.mark.parametrize('resume_kind', ['resume_state', 'resume_checkpoint'])
@pytest.mark.parametrize('brief_change', ['source_limit', 'clear'])
async def test_resume_cannot_replay_old_sources_after_user_saves_new_brief(resume_kind, brief_change):
    loop = CreationAgentLoop(ResumeService())
    old_brief = {
        'root_request': '编写活动说明，可以检索个人资料',
        'revision': 1,
        'user_input_revisions': {'root_request': 1},
        'decisions': [],
        'brief_markdown': '## 历史记忆参考\nPRIVATE-CACHED-EVIDENCE',
    }
    initial_args = {**args(), 'creation_mode': 'brainstorm', 'creation_brief': old_brief}
    first = [event async for event in loop.run(**initial_args, model_mode='external')]
    checkpoint = first[-1]['data']['continuation']
    assert checkpoint['environment']['input_context']['schema_version'] == 'creation.input-context.v1'
    assert 'PRIVATE-CACHED-EVIDENCE' in checkpoint['environment']['input_context']['creation_brief']

    # The user saved a new source limit without changing the instruction or
    # document base, then retried that instruction from its old checkpoint.
    latest_brief = {
        **old_brief, 'revision': 2,
        'brief_edits': {'root_request': '编写活动说明，不检索个人资料'},
        'user_input_revisions': {'root_request': 2},
    }
    if brief_change == 'clear':
        latest_brief = {}
    retry_args = {**initial_args, 'creation_brief': latest_brief, resume_kind: checkpoint}
    if resume_kind == 'resume_state':
        retry_args['model_result'] = json.dumps(DECISION)
    with pytest.raises(OperationError):
        [event async for event in loop.run(**retry_args)]


@pytest.mark.asyncio
@pytest.mark.parametrize('resume_kind', ['resume_state', 'resume_checkpoint'])
@pytest.mark.parametrize('empty_brief', [False, True])
async def test_resume_accepts_the_same_explicit_brief(resume_kind, empty_brief):
    loop = CreationAgentLoop(ResumeService())
    brief = {
        'root_request': '编写活动说明，不检索个人资料',
        'revision': 2,
        'user_input_revisions': {'root_request': 2},
        'decisions': [],
        'brief_markdown': '# 创作简报\n只使用本次输入。',
    }
    if empty_brief:
        brief = {}
    initial_args = {**args(), 'creation_mode': 'brainstorm', 'creation_brief': brief}
    first = [event async for event in loop.run(**initial_args, model_mode='external')]
    checkpoint = first[-1]['data']['continuation']
    retry_args = {**initial_args, 'creation_brief': json.loads(json.dumps(brief)),
                  resume_kind: checkpoint}
    if resume_kind == 'resume_state':
        retry_args['model_result'] = json.dumps(DECISION)
    events = [event async for event in loop.run(**retry_args)]
    assert events[-1]['type'] == 'run.completed'
    assert events[-1]['data']['document'] == EDITED


@pytest.mark.asyncio
async def test_sidecar_http_rejects_cross_session_resume_before_emitting_other_sessions_events(monkeypatch):
    creation_app = importlib.import_module('creation.app')
    loop = CreationAgentLoop(ResumeService())
    checkpoint = await paused_route(loop)

    class ThreadQueue:
        def submit(self, priority, fn, lane=None):
            future = concurrent.futures.Future()
            def run():
                try:
                    future.set_result(fn())
                except Exception as exc:
                    future.set_exception(exc)
            threading.Thread(target=run, daemon=True).start()
            return future

    monkeypatch.setattr(creation_app, 'creation_agent_loop', loop)
    monkeypatch.setattr(creation_app, 'get_global_queue', lambda: ThreadQueue())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=creation_app.app), base_url='http://test') as client:
        response = await client.post('/creation/agent/run', json={
            'user_prompt': 'Replace Monday with Tuesday', 'current_document': BASE, 'design_templates': [],
            'session_id': 'session-b', 'run_id': 'run-b', 'model_mode': 'external',
            'resume_state': checkpoint, 'model_result': json.dumps(DECISION)})
    assert response.status_code == 200, response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert events[-1]['type'] == 'run.failed'
    assert events[-1]['data']['error_code'] == 'CREATION_RESUME_MISSING'
    assert all(event['session_id'] == 'session-b' for event in events)
