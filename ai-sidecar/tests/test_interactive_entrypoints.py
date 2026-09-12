"""Online model entrypoints must acquire P0 before starting any model work."""
import asyncio
import importlib
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

import inference_queue
import model_api_server
from inference_queue import InferenceQueue, LANE_P0_CREATION, LANE_P0_QUERY, Priority
from rag.pipeline import RagResult

creation_app = importlib.import_module('creation.app')


@pytest.fixture
def isolated_queue(monkeypatch, tmp_path):
    # Tests must never notify/preempt the user's running model processes.
    monkeypatch.setattr(inference_queue, '_INTERACTIVE_DEMAND_LOCK_FILE', str(tmp_path / 'demand'))
    monkeypatch.setattr(inference_queue, '_INTERACTIVE_DEMAND_PROBE_LOCK_FILE', str(tmp_path / 'probe'))
    monkeypatch.setattr(inference_queue, '_RAG_LOCK_FILE', str(tmp_path / 'rag'))
    monkeypatch.setattr(inference_queue, '_RAG_LOCK_OWNER_FILE', str(tmp_path / 'owner'))
    queue = InferenceQueue(max_concurrency=1, low_memory_threshold_mb=0)
    monkeypatch.setattr(creation_app, 'get_global_queue', lambda: queue)
    monkeypatch.setattr(model_api_server, 'get_global_queue', lambda: queue)
    yield queue
    queue.shutdown()


def assert_worker_priority(priority, lane):
    task = getattr(inference_queue._WORKER_STATE, 'task', None)
    assert task is not None, 'model work bypassed the inference queue'
    assert task.priority == priority
    assert task.lane == lane


@pytest.mark.asyncio
@pytest.mark.parametrize('path,payload,method,is_stream', [
    ('/creation/skills/analyze', {'document_title': '标题', 'document_content': '内容'}, 'analyze_creation_skill', False),
    ('/creation/skills/review', {}, 'review_skill', False),
    ('/creation/skills/match', {'prompt': '写方案', 'skills': []}, 'route_creation_skills', False),
    ('/creation/references', {'user_prompt': '找资料'}, 'retrieve_references', False),
    ('/creation/test_model', {'model': 'local', 'api_key': ''}, '_generate_cloud', True),
    ('/creation/chat', {'model': 'local', 'api_key': '', 'messages': [{'role': 'user', 'content': '你好'}]}, '_chat_cloud', True),
])
async def test_auxiliary_creation_models_are_p0_before_work(
    monkeypatch, isolated_queue, path, payload, method, is_stream,
):
    calls = []

    def check():
        assert_worker_priority(Priority.P0, LANE_P0_CREATION)
        calls.append(method)

    async def model(*args, **kwargs):
        check()
        return {'status': 'ok'}

    async def stream(*args, **kwargs):
        check()
        yield 'OK'

    if method == 'review_skill':
        monkeypatch.setattr('creation.skill_governance.review_skill', model)
    elif method == 'retrieve_references':
        def references(*args, **kwargs):
            check()
            return []
        monkeypatch.setattr(creation_app.creation_service, method, references)
    else:
        monkeypatch.setattr(creation_app.creation_service, method, stream if is_stream else model)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=creation_app.app), base_url='http://test',
    ) as client:
        response = await client.post(path, json=payload)

    assert response.status_code == 200, response.text
    assert calls == [method]
    assert isolated_queue.stats()['totals']['P0']['submitted'] == 1
    if path == '/creation/chat':
        assert '"done": true' in response.text
        assert '"content": "OK"' in response.text


@pytest.mark.parametrize('endpoint', ['/query', '/query/stream', '/references'])
def test_consultation_preparation_retrieval_and_generation_share_one_p0(
    monkeypatch, isolated_queue, endpoint,
):
    phases = []
    original_intent = model_api_server._analyze_floating_assist_intent

    def intent(*args, **kwargs):
        assert_worker_priority(Priority.P0, LANE_P0_QUERY)
        phases.append('intent')
        return original_intent(*args, **kwargs)

    class Pipeline:
        def query(self, query, **kwargs):
            assert_worker_priority(Priority.P0, LANE_P0_QUERY)
            phases.append('references' if kwargs.get('references_only') else 'answer')
            if kwargs.get('on_contexts'):
                kwargs['on_contexts']([])
            if kwargs.get('on_delta'):
                kwargs['on_delta']('答案')
            return RagResult(answer='答案', contexts=[], model='local')

        def _build_context(self, contexts):
            return ''

    monkeypatch.setattr(model_api_server, '_rag_pipeline', Pipeline())
    monkeypatch.setattr(model_api_server, '_analyze_floating_assist_intent', intent)
    monkeypatch.setattr(model_api_server, '_build_rag_llm_override', lambda *a, **k: None)
    monkeypatch.setattr(model_api_server, '_save_rag_session', lambda *a, **k: 1)
    monkeypatch.setattr(model_api_server, 'log_llm_usage', lambda *a, **k: None)
    monkeypatch.setattr('model_registry_global.check_memory_pressure', lambda: 'normal')

    response = model_api_server.app.test_client().post(endpoint, json={'query': '本地问题'}, buffered=True)
    assert response.status_code == 200
    expected = {
        '/query': ['intent', 'answer'],
        '/query/stream': ['intent', 'references', 'answer'],
        '/references': ['references'],
    }
    assert phases == expected[endpoint]
    assert isolated_queue.stats()['totals']['P0']['submitted'] == 1


def test_model_experience_opens_and_consumes_transport_under_p0(monkeypatch, isolated_queue):
    phases = []

    class Connection:
        def __init__(self, *args, **kwargs):
            assert_worker_priority(Priority.P0, LANE_P0_QUERY)
            phases.append('connect')
            self.lines = iter([
                b'{"message":{"content":"OK"},"done":false}\n',
                b'{"message":{"content":""},"done":true}\n',
            ])

        def request(self, *args, **kwargs):
            assert_worker_priority(Priority.P0, LANE_P0_QUERY)
            phases.append('request')

        def getresponse(self):
            self.status = 200
            return self

        def readline(self):
            assert_worker_priority(Priority.P0, LANE_P0_QUERY)
            return next(self.lines, b'')

        def close(self):
            assert_worker_priority(Priority.P0, LANE_P0_QUERY)
            phases.append('close')

    monkeypatch.setattr('http.client.HTTPConnection', Connection)
    monkeypatch.setattr(model_api_server, 'get_model', lambda _id: SimpleNamespace(category='llm', provider='ollama'))
    monkeypatch.setattr(model_api_server.model_manager, '_ollama_names_for_model', lambda _id: ['local'])
    response = model_api_server.app.test_client().post('/api/models/fake/chat', json={
        'messages': [{'role': 'user', 'content': '测试'}],
    }, buffered=True)
    assert response.status_code == 200
    assert '"done": true' in response.get_data(as_text=True)
    assert phases == ['connect', 'request', 'close']
    assert isolated_queue.stats()['totals']['P0']['submitted'] == 1


@pytest.mark.asyncio
async def test_scheduled_creation_keeps_p2_and_yields_before_first_model_token(monkeypatch, isolated_queue):
    started = threading.Event()
    closed = threading.Event()

    async def background_run(**kwargs):
        assert_worker_priority(Priority.P2, 'p2_creation')
        started.set()
        try:
            await asyncio.sleep(60)
            yield {'type': 'document.delta', 'data': {'content': 'must not appear'}}
        finally:
            closed.set()

    monkeypatch.setattr(creation_app.creation_agent_loop, 'run', background_run)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=creation_app.app), base_url='http://test',
    ) as client:
        task = asyncio.create_task(client.post('/creation/agent/run', json={
            'user_prompt': '后台计划任务', 'design_templates': [], 'execution_origin': 'scheduled_task',
        }))
        assert await asyncio.to_thread(started.wait, 2)
        foreground = isolated_queue.submit(Priority.P0, lambda: closed.is_set(), lane=LANE_P0_QUERY)
        assert await asyncio.wait_for(asyncio.wrap_future(foreground), timeout=2) is True
        response = await asyncio.wait_for(task, timeout=2)

    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    failure = next(event for event in events if event['type'] == 'run.failed')
    assert failure['data']['error_code'] == 'INFERENCE_PREEMPTED'
    assert failure['data']['retryable'] is True
    assert not any(event['type'] == 'document.delta' for event in events)
    assert isolated_queue.stats()['totals']['P2']['submitted'] == 1


def test_disconnected_consultation_cancels_unadmitted_p0(monkeypatch):
    import concurrent.futures

    submitted = threading.Event()
    future = concurrent.futures.Future()

    class PendingQueue:
        def submit(self, priority, fn, lane=None):
            assert priority == Priority.P0
            assert lane == LANE_P0_QUERY
            submitted.set()
            return future

    monkeypatch.setattr(model_api_server, 'get_global_queue', lambda: PendingQueue())
    monkeypatch.setattr(model_api_server, '_rag_pipeline', object())
    monkeypatch.setattr(model_api_server, '_build_rag_llm_override', lambda *a, **k: None)
    monkeypatch.setattr(model_api_server, '_save_rag_session', lambda *a, **k: 1)
    monkeypatch.setattr('model_registry_global.check_memory_pressure', lambda: 'normal')
    response = model_api_server.app.test_client().post(
        '/query/stream', json={'query': '尚未运行的咨询'}, buffered=False,
    )
    assert submitted.wait(1)
    response.close()
    assert future.cancelled()
    assert not future.set_running_or_notify_cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['skills', 'inline'])
async def test_cancelled_creation_endpoint_never_starts_queued_model(monkeypatch, endpoint):
    import concurrent.futures

    future = concurrent.futures.Future()

    class PendingQueue:
        def submit(self, priority, fn, lane=None):
            assert priority == Priority.P0
            assert lane == LANE_P0_CREATION
            return future

    monkeypatch.setattr(creation_app, 'get_global_queue', lambda: PendingQueue())
    if endpoint == 'skills':
        coroutine = creation_app.match_creation_skills(creation_app.MatchCreationSkillsRequest(prompt='本轮需求'))
    else:
        coroutine = creation_app.run_creation_inline_edit(creation_app.InlineEditRequest(
            schema_version='creation.inline-edit.v1', request_id='priority-cancel-test',
            action='polish', selected_markdown='待润色的段落',
        ))
    task = asyncio.create_task(coroutine)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert future.cancelled()
    assert not future.set_running_or_notify_cancel()


class BlockingModelTransport(httpx.AsyncBaseTransport):
    """A real async HTTP boundary, with cleanup held open to test slot ownership."""

    def __init__(self, stage):
        self.stage = stage
        self.blocked = threading.Event()
        self.cleanup_started = threading.Event()
        self.allow_cleanup = threading.Event()
        self.closed = threading.Event()
        self.delivered = []

    async def finish_body(self):
        self.cleanup_started.set()
        while not self.allow_cleanup.is_set():
            await asyncio.sleep(0.005)

    async def handle_async_request(self, request):
        assert_worker_priority(Priority.P0, LANE_P0_CREATION)
        if self.stage == 'before_headers':
            self.blocked.set()
            try:
                await asyncio.sleep(60)
            finally:
                await self.finish_body()
        transport = self

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                if transport.stage == 'between_chunks':
                    yield b'first'
                transport.blocked.set()
                await asyncio.sleep(60)
                yield b'unreachable'

            async def aclose(self):
                await transport.finish_body()

        return httpx.Response(200, stream=Body())

    async def aclose(self):
        self.closed.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['before_headers', 'before_first_token', 'between_chunks'])
async def test_creation_chat_disconnect_closes_async_http_before_releasing_slot(
    monkeypatch, isolated_queue, stage,
):
    model_http = BlockingModelTransport(stage)

    async def chat(*args, **kwargs):
        async with httpx.AsyncClient(transport=model_http) as model_client:
            async with model_client.stream('POST', 'http://model.test/chat') as response:
                async for data in response.aiter_bytes():
                    model_http.delivered.append(data)
                    yield data.decode()

    monkeypatch.setattr(creation_app.creation_service, '_chat_cloud', chat)
    request_task = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=creation_app.app), base_url='http://test',
        ) as client:
            request_task = asyncio.create_task(client.post('/creation/chat', json={
                'model': 'test', 'api_key': '', 'messages': [{'role': 'user', 'content': '测试'}],
            }))
            assert await asyncio.to_thread(model_http.blocked.wait, 2)
            following = isolated_queue.submit(
                Priority.P0, lambda: model_http.closed.is_set(), lane=LANE_P0_QUERY,
            )
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            assert await asyncio.to_thread(model_http.cleanup_started.wait, 2)
            assert not following.done(), 'slot was released before async HTTP cleanup'
            model_http.allow_cleanup.set()
            assert await asyncio.wait_for(asyncio.wrap_future(following), timeout=2) is True
            assert model_http.delivered == ([b'first'] if stage == 'between_chunks' else [])
    finally:
        model_http.allow_cleanup.set()
        if request_task is not None:
            request_task.cancel()


@pytest.mark.asyncio
async def test_creation_auxiliary_request_cancel_closes_async_http_before_releasing_slot(
    monkeypatch, isolated_queue,
):
    model_http = BlockingModelTransport('before_headers')

    async def analyze(*args, **kwargs):
        async with httpx.AsyncClient(transport=model_http) as model_client:
            response = await model_client.post('http://model.test/generate')
            return response.json()

    monkeypatch.setattr(creation_app.creation_service, 'analyze_creation_skill', analyze)
    request_task = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=creation_app.app), base_url='http://test',
        ) as client:
            request_task = asyncio.create_task(client.post('/creation/skills/analyze', json={
                'document_title': '测试', 'document_content': '测试内容',
            }))
            assert await asyncio.to_thread(model_http.blocked.wait, 2)
            following = isolated_queue.submit(
                Priority.P0, lambda: model_http.closed.is_set(), lane=LANE_P0_QUERY,
            )
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            assert await asyncio.to_thread(model_http.cleanup_started.wait, 2)
            assert not following.done()
            model_http.allow_cleanup.set()
            assert await asyncio.wait_for(asyncio.wrap_future(following), timeout=2) is True
    finally:
        model_http.allow_cleanup.set()
        if request_task is not None:
            request_task.cancel()


def test_creation_cancellation_before_worker_registration_never_starts_model():
    import concurrent.futures

    cancellation = creation_app._InteractiveCreationCancellation()
    called = []

    async def operation():
        called.append(True)

    cancellation.cancel()
    with pytest.raises(concurrent.futures.CancelledError):
        cancellation.run(operation)
    assert called == []
    assert cancellation._loop is None
    assert cancellation._task is None


def test_creation_cancellation_after_closed_worker_loop_is_harmless():
    cancellation = creation_app._InteractiveCreationCancellation()

    async def operation():
        return 'finished'

    assert cancellation.run(operation) == 'finished'
    cancellation.cancel()
    cancellation.cancel()
    assert cancellation._loop is None
    assert cancellation._task is None


def test_creation_cancellation_tolerates_loop_close_after_copy():
    cancellation = creation_app._InteractiveCreationCancellation()
    calls = []

    class ClosedLoop:
        def call_soon_threadsafe(self, callback):
            calls.append(callback)
            raise RuntimeError('Event loop is closed')

    cancellation._loop = ClosedLoop()
    cancellation._task = SimpleNamespace(cancel=lambda: None)
    cancellation.cancel()
    cancellation.cancel()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_creation_stream_cancel_while_consumer_sends_chunk_still_closes_model(isolated_queue):
    model_http = BlockingModelTransport('between_chunks')
    sending = asyncio.Event()

    async def model_stream():
        async with httpx.AsyncClient(transport=model_http) as model_client:
            async with model_client.stream('POST', 'http://model.test/chat') as response:
                async for data in response.aiter_bytes():
                    yield data.decode()

    # Keep the iterator alive after the consumer ends: cancellation must not
    # depend on garbage collection calling the nested generator's finally.
    iterator = creation_app._interactive_creation_chunks(model_stream)

    async def consume():
        async for _chunk in iterator:
            sending.set()
            await asyncio.sleep(60)

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(sending.wait(), timeout=2)
        assert await asyncio.to_thread(model_http.blocked.wait, 2)
        following = isolated_queue.submit(
            Priority.P0, lambda: model_http.closed.is_set(), lane=LANE_P0_QUERY,
        )
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert await asyncio.to_thread(model_http.cleanup_started.wait, 2)
        assert not following.done()
        model_http.allow_cleanup.set()
        assert await asyncio.wait_for(asyncio.wrap_future(following), timeout=2) is True
    finally:
        model_http.allow_cleanup.set()
        consumer.cancel()
        await iterator.aclose()
