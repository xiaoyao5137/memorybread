"""Keep current materials authoritative in single-pass consultation answers."""
import pytest
import json

from model_api_server import _build_floating_assist_rag_query
from rag.retriever import RetrievedChunk
from tests.test_rag import MockLlmBackend, _make_pipeline
from rag.llm.ollama import OllamaBackend


COMPARISON = ('【图片1】\n相对 Orion：\n服务层\n'
              '对象 | 延迟 avg/P95/P99(ms)\n'
              'Orion | 10 / 20 / 30\nAurora | 8 / 25 / 15\n【图片1结束】')


@pytest.mark.parametrize('source', ['consultation', 'floating_assist'])
@pytest.mark.parametrize('stream', [False, True])
def test_general_report_conclusion_keeps_bound_facts_instead_of_model_misstatements(source, stream):
    query = _build_floating_assist_rag_query('这份报告是什么结论？', {
        'source': source, 'manual_instruction': '这份报告是什么结论？', 'ocr_text': COMPARISON,
    })
    memory = RetrievedChunk(capture_id=1, doc_key='document:1', text='以前的一次试验。',
                            metadata={'source_type': 'document', 'document_id': 1})
    llm = MockLlmBackend('伪造结论：TTFT从10ms上升至8ms[M1]。')
    deltas = []
    result = _make_pipeline().query(query, supplied_contexts=[memory], llm=llm,
                                   on_delta=deltas.append if stream else None)
    assert llm.call_count == 1
    assert query.material_comparison_summary
    assert 'Aurora' in result.answer and '延迟' in result.answer
    assert 'TTFT' not in result.answer and '伪造结论' not in result.answer
    assert result.cited_contexts == []
    assert '[记忆' not in result.answer
    if stream:
        assert ''.join(deltas) == result.answer
        assert all('伪造结论' not in delta for delta in deltas)


@pytest.mark.parametrize('question', [
    '请把这份报告翻译成英文。', '不要总结这份报告，请提取每一项原始读数。',
    '这份报告与上次测试相比是什么结论？',
    '这两份报告体现什么结论？请只输出JSON',
    '总结这两份报告，并解释为什么副本数减少',
    '总结这份报告并解释为什么副本数减少',
    'Summarize these reports in English.',
])
def test_specific_tasks_and_history_comparisons_keep_the_requested_generation(question):
    query = _build_floating_assist_rag_query(question, {
        'source': 'consultation', 'manual_instruction': question, 'ocr_text': COMPARISON,
    })
    assert not query.material_comparison_summary
    llm = MockLlmBackend('按具体要求生成的回答。')
    result = _make_pipeline().query(query, supplied_contexts=[], llm=llm)
    assert result.answer == '按具体要求生成的回答。'
    assert llm.call_count == 1


@pytest.mark.parametrize('source', ['consultation', 'floating_assist'])
def test_raw_legacy_wrapper_does_not_reintroduce_unbound_chart_ticks(source):
    from copy import deepcopy

    question = '这张图说明什么？'
    material = ('【图片2】\n响应延迟 | ms\n最近30秒已完成请求\n'
                '— P50 | ---- P95 | — 平均 | P99 | MAX\n'
                '11.4s\n22.8s\n1m 20s | 2m 40s | 4m 00s\n'
                '相对开始时间\n【图片2结束】')
    raw = '工作场景助手\n用户手工指令：' + question + '\n当前屏幕 OCR：\n' + material
    metadata = {'source': source, 'manual_instruction': question, 'ocr_text': material,
                'attachments': [{'name': '图片2', 'path': '/local/chart.png'}]}
    original = deepcopy(metadata)
    query = _build_floating_assist_rag_query(raw, metadata)
    llm = MockLlmBackend('图片2的曲线读数无法从这些文字确认。')
    _make_pipeline().query(query, supplied_contexts=[], llm=llm)
    assert llm.call_count == 1
    assert '11.4s' not in llm.last_prompt and '22.8s' not in llm.last_prompt
    assert '2m 40s' not in llm.last_prompt
    assert '响应延迟' in llm.last_prompt and '最近30秒' in llm.last_prompt
    assert llm.last_prompt.endswith(question)
    assert metadata == original  # Projection must not edit the retained source.


@pytest.mark.parametrize('source', ['consultation', 'floating_assist'])
def test_field_bindings_are_supplied_to_the_single_answer_generation(source):
    material = ('【图片2】\n应用层\n对象 | 延迟 avg / P95 (s) | 总耗时 avg / P95 (s)\n'
                'Orion-X | 8 (+20%) / 30 (+10%) | 12 (+5%) / 40 (-15%)\n【图片2结束】')
    query = _build_floating_assist_rag_query('这份报告说明什么？', {
        'source': source, 'manual_instruction': '这份报告说明什么？', 'ocr_text': material,
    })
    llm = MockLlmBackend('Orion-X 平均延迟为 8 秒，总耗时 P95 为 40 秒。')
    _make_pipeline().query(query, supplied_contexts=[], llm=llm)
    assert llm.call_count == 1
    assert '图片2 / 应用层 / Orion-X' in llm.last_prompt
    assert '延迟 avg（s） = 8' in llm.last_prompt
    assert '总耗时 P95（s） = 40' in llm.last_prompt
    assert '(+20%)' not in llm.last_prompt
    assert '(-15%)' not in llm.last_prompt


@pytest.mark.parametrize('source', ['consultation', 'floating_assist'])
@pytest.mark.parametrize('stream', [False, True])
def test_material_is_last_evidence_and_original_instruction_remains_the_task(source, stream):
    question = '这两份报告说明什么？'
    material = '【图片1】\nOrion-X | 12 | 30\nAurora-Z | 8 | 20\n【图片1结束】'
    query = _build_floating_assist_rag_query(question, {
        'source': source, 'manual_instruction': question, 'ocr_text': material,
    })
    memory = RetrievedChunk(capture_id=1, doc_key='document:1',
                            text='Orion-X 的上月数据 ' + '历史背景 ' * 1000,
                            metadata={'source_type': 'document', 'document_id': 1})
    llm = MockLlmBackend('材料中的 Aurora-Z 耗时较低。')
    deltas = []
    result = _make_pipeline().query(query, supplied_contexts=[memory], llm=llm,
                                   on_delta=deltas.append if stream else None)
    assert llm.call_count == 1
    assert llm.last_prompt.index('历史背景') < llm.last_prompt.index(material)
    assert llm.last_prompt.endswith(question)
    assert '相对基线改善不能改写成优于另一个方案' in llm.last_prompt
    assert '对象名称照录材料' in llm.last_prompt
    # Recall remains visible without forcing adoption or a second review call.
    assert result.contexts == [memory]
    assert result.cited_contexts == []
    if stream:
        assert ''.join(deltas) == result.answer


@pytest.mark.parametrize('source', ['consultation', 'floating_assist'])
@pytest.mark.parametrize('structured', [False, True])
def test_material_sampling_reaches_transport_without_exposing_reasoning(monkeypatch, source, structured):
    requests = []

    def stream(url, body, *, timeout, on_chunk):
        requests.append(body)
        for event in [
            {'thinking': 'private internal trace'},
            {'response': 'Orion-X 的数值为 12。'},
            {'done': True, 'eval_count': 20, 'done_reason': 'stop'},
        ]:
            on_chunk(event)

    monkeypatch.setattr('rag.llm.ollama.stream_inference_json', stream)
    query = _build_floating_assist_rag_query('这份报告说明什么？', {
        'source': source, 'manual_instruction': '这份报告说明什么？',
        'ocr_text': ('Name | Delay avg/P95(s)\nOrion-X | 12.45 / 30.25'
                     if structured else 'Orion-X | 12 | 30'),
    })
    deltas = []
    result = _make_pipeline().query(query, supplied_contexts=[],
        llm=OllamaBackend(), on_delta=deltas.append)
    assert len(requests) == 1
    assert requests[0]['think'] is False
    assert set(requests[0]['options']) == {'num_predict', 'temperature', 'top_p'}
    assert result.answer == ''.join(deltas) == 'Orion-X 的数值为 12。'
    assert 'private' not in result.answer

    OllamaBackend().complete('普通问题')
    assert requests[-1]['think'] is False
    assert 'presence_penalty' not in requests[-1]['options']


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('done_reason', ['length', 'stop'])
@pytest.mark.parametrize('raw_answer', ['\n', '[M999]', '[M'])
def test_empty_final_answer_is_a_generation_failure(stream, done_reason, raw_answer):
    from rag.llm.base import LlmResponse

    class EmptyBackend(MockLlmBackend):
        def complete(self, *args, **kwargs):
            self.call_count += 1
            return LlmResponse(text=raw_answer, model='mock', tokens=8192, done_reason=done_reason)

        def complete_stream(self, *args, **kwargs):
            return self.complete(*args, **kwargs)

    backend = EmptyBackend('')
    with pytest.raises(RuntimeError, match='未返回可用答案'):
        _make_pipeline().query('材料说明什么？', supplied_contexts=[], llm=backend,
                               on_delta=(lambda value: None) if stream else None)
    assert backend.call_count == 1


@pytest.mark.parametrize('endpoint', ['/query', '/query/stream'])
@pytest.mark.parametrize('source', ['consultation', 'floating_assist'])
@pytest.mark.parametrize('done_reason', ['length', 'stop'])
@pytest.mark.parametrize('raw_answer', ['\n', '[M999]', '[M'])
def test_api_empty_answer_reports_failure_without_success_or_adoption(
        monkeypatch, endpoint, source, done_reason, raw_answer):
    import model_api_server as server
    from rag.llm.base import LlmResponse

    class EmptyBackend(MockLlmBackend):
        def complete(self, *args, **kwargs):
            self.call_count += 1
            return LlmResponse(text=raw_answer, model='mock-local', tokens=8192,
                               done_reason=done_reason)

        def complete_stream(self, *args, **kwargs):
            return self.complete(*args, **kwargs)

    class Queue:
        def submit(self, priority, func, **kwargs):
            import concurrent.futures
            future = concurrent.futures.Future()
            try:
                future.set_result(self.submit_sync(priority, func, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            return future

        def submit_sync(self, priority, func, **kwargs):
            return func()

    backend = EmptyBackend('')
    memory = RetrievedChunk(capture_id=1, doc_key='document:1',
                            text='Orion-X 的历史数据',
                            metadata={'source_type': 'document', 'document_id': 1})
    pipeline = _make_pipeline()
    original_query = pipeline.query
    recalls = []
    saved = []
    usage = []

    def query(text, **kwargs):
        if 'supplied_contexts' not in kwargs:
            recalls.append(text)
            kwargs['supplied_contexts'] = [memory]
        return original_query(text, **kwargs)

    pipeline.query = query
    monkeypatch.setattr(server, '_rag_pipeline', pipeline)
    monkeypatch.setattr(server, 'get_global_queue', lambda: Queue())
    monkeypatch.setattr(server, '_build_rag_llm_override', lambda *a, **k: backend)
    monkeypatch.setattr(server, '_save_rag_session', lambda *a, **k: saved.append(a) or 1)
    monkeypatch.setattr(server, 'log_llm_usage', lambda **kwargs: usage.append(kwargs))
    monkeypatch.setattr('model_registry_global.check_memory_pressure', lambda: 'normal')
    monkeypatch.setattr('model_registry_global.get_active_ollama_model', lambda: 'mock-local')

    response = server.app.test_client().post(endpoint, json={
        'query': '这份报告说明什么？', 'manual_instruction': '这份报告说明什么？',
        'source': source, 'ocr_text': 'Orion-X | 12 | 30',
    }, buffered=True)

    assert backend.call_count == 1
    assert len(recalls) == 1
    assert len(usage) == 1
    assert usage[0]['status'] == 'failed'
    assert '未返回可用答案' in usage[0]['error_msg']
    if endpoint.endswith('/stream'):
        # HTTP headers precede generation; failure must therefore be an SSE
        # error with no successful done event or adopted references.
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
                  if line.startswith('data: ')]
        errors = [event for event in events if event['type'] == 'error']
        assert len(errors) == 1
        assert errors[0]['code'] == 'RAG_STREAM_FAILED'
        assert not any(event['type'] == 'done' for event in events)
        references = [event['contexts'] for event in events if event['type'] == 'references']
        assert len(references) == 1
        assert references[0][0]['doc_key'] == 'document:1'
        assert references[0][0]['cited'] is False
        assert saved and saved[-1][2].strip()
        assert all(not context.get('cited') for record in saved for context in record[3])
        assert all(record[2].strip() for record in saved)
    else:
        assert response.status_code == 500
        assert '未返回可用答案' in response.get_json()['error']
        assert 'answer' not in response.get_json()
        assert saved == []
