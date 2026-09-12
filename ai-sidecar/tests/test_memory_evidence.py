import pytest
from rag.memory_evidence import CitationStream, render_citations
from rag.retriever import RetrievedChunk
from tests.test_rag import _make_pipeline, MockLlmBackend


def candidates():
    return [RetrievedChunk(capture_id=i, text=text, metadata={'source_type': 'document', 'document_id': i,
            'url': 'https://example.com/%s' % i}, doc_key='document:%s' % i)
            for i, text in [(1, 'QCon 团队留任计划'), (2, 'SMACT 衡量 SM 活跃时间比例')]]


@pytest.mark.parametrize('stream', [False, True])
def test_one_generation_can_ignore_all_memories(stream):
    llm = MockLlmBackend('这是生日提醒。')
    pipeline = _make_pipeline()
    deltas = []
    result = pipeline.query('他表达了什么？本次图片：今天生日，小陈，请送贺卡。',
                            llm=llm, supplied_contexts=candidates(), on_delta=deltas.append if stream else None)
    assert llm.call_count == 1
    assert '候选历史记忆（宽松召回' in llm.last_prompt
    assert 'QCon' in llm.last_prompt
    assert '今天生日' in llm.last_prompt
    assert '也可以全部忽略' in llm.last_system
    assert result.cited_contexts == []
    assert result.answer == '这是生日提醒。'
    if stream:
        assert ''.join(deltas) == result.answer


def test_only_explicit_valid_citations_are_published():
    result = _make_pipeline().query('SMACT是什么', supplied_contexts=candidates(),
                                   llm=MockLlmBackend('SM 活跃时间比例。[M2] 不存在[M99] [Mabc]'))
    assert [x.metadata['document_id'] for x in result.cited_contexts] == [2]
    assert 'https://example.com/2' in result.answer
    assert 'https://example.com/1' not in result.answer
    assert 'M99' not in result.answer and 'Mabc' not in result.answer


def test_no_automatic_links_replace_no_match_answer():
    result = _make_pipeline().query('灵机招商文档链接', supplied_contexts=candidates(),
                                   llm=MockLlmBackend('这些资料无法确定目标文档。'))
    assert result.answer == '这些资料无法确定目标文档。'
    assert not result.cited_contexts


@pytest.mark.parametrize('size', [1, 2, 4, 9, 100])
def test_stream_citation_boundaries_match_final(size):
    text = '公式[M2]，无效[M999]，普通[标签](https://other.example)，再引[M1]。'
    out = []
    stream = CitationStream(out.append, candidates())
    for pos in range(0, len(text), size):
        stream.feed(text[pos:pos+size])
    stream.finish()
    assert ''.join(out) == render_citations(text, candidates())


@pytest.mark.parametrize('endpoint', ['/query', '/query/stream'])
@pytest.mark.parametrize('source', ['floating_assist', 'monitor'])
def test_api_single_inference_publishes_all_recall_with_adoption(monkeypatch, endpoint, source):
    import json
    import model_api_server as server
    llm = MockLlmBackend('SM 活跃时间比例。[M2]')
    pipeline = _make_pipeline()
    original_query = pipeline.query
    recalls = []
    saved = []
    def query(text, **kwargs):
        if 'supplied_contexts' not in kwargs:
            recalls.append(text)
            kwargs['supplied_contexts'] = candidates()
        return original_query(text, **kwargs)
    pipeline.query = query
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
    monkeypatch.setattr(server, '_rag_pipeline', pipeline)
    monkeypatch.setattr(server, 'get_global_queue', lambda: Queue())
    monkeypatch.setattr(server, '_build_rag_llm_override', lambda *a, **k: llm)
    monkeypatch.setattr(server, '_save_rag_session', lambda *a, **k: saved.append(a) or 1)
    monkeypatch.setattr(server, 'log_llm_usage', lambda *a, **k: None)
    monkeypatch.setattr('model_registry_global.check_memory_pressure', lambda: 'normal')
    response = server.app.test_client().post(endpoint, json={
        'query': '什么是SMACT', 'manual_instruction': '什么是SMACT', 'source': source,
    }, buffered=True)
    assert response.status_code == 200
    assert llm.call_count == 1
    assert len(recalls) == 1
    if endpoint.endswith('stream'):
        events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith('data: ')]
        refs = [e['contexts'] for e in events if e['type'] == 'references']
        # 召回完成即公布全部候选记忆，此时还没有采用标记
        assert [c['document_id'] for c in refs[0]] == [1, 2]
        assert [c['cited'] for c in refs[0]] == [False, False]
        assert [c['recall_index'] for c in refs[0]] == [1, 2]
        # 生成结束后按采用情况重排并标注
        assert [c['document_id'] for c in refs[-1]] == [2, 1]
        result = next(e for e in events if e['type'] == 'done')
    else:
        result = response.get_json()
    actual = [c for c in result['contexts'] if c.get('source_type') != 'floating_assist']
    assert [c['document_id'] for c in actual] == [2, 1]
    assert [c['cited'] for c in actual] == [True, False]
    assert [c['recall_index'] for c in actual] == [2, 1]
    persisted = [c for c in saved[-1][3] if c.get('source_type') != 'floating_assist']
    assert [c['document_id'] for c in persisted] == [2, 1]
    assert [c['cited'] for c in persisted] == [True, False]


@pytest.mark.parametrize('endpoint', ['/query', '/query/stream'])
def test_uncited_recall_still_published_as_references(monkeypatch, endpoint):
    """回归：模型漏标注 [M编号] 时，已召回并已被采用的记忆不得从参考资料里消失。"""
    import json
    import model_api_server as server
    llm = MockLlmBackend('SMACT 衡量 SM 活跃时间比例，英伟达阈值 80%。')
    pipeline = _make_pipeline()
    original_query = pipeline.query
    recalls = []
    def query(text, **kwargs):
        if 'supplied_contexts' not in kwargs:
            recalls.append(text)
            kwargs['supplied_contexts'] = candidates()
        return original_query(text, **kwargs)
    pipeline.query = query
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
    monkeypatch.setattr(server, '_rag_pipeline', pipeline)
    monkeypatch.setattr(server, 'get_global_queue', lambda: Queue())
    monkeypatch.setattr(server, '_build_rag_llm_override', lambda *a, **k: llm)
    monkeypatch.setattr(server, '_save_rag_session', lambda *a, **k: 1)
    monkeypatch.setattr(server, 'log_llm_usage', lambda *a, **k: None)
    monkeypatch.setattr('model_registry_global.check_memory_pressure', lambda: 'normal')
    response = server.app.test_client().post(endpoint, json={
        'query': 'SMACT文档', 'manual_instruction': 'SMACT文档', 'source': 'floating_assist',
    }, buffered=True)
    assert response.status_code == 200
    assert len(recalls) == 1
    if endpoint.endswith('stream'):
        events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith('data: ')]
        result = next(e for e in events if e['type'] == 'done')
    else:
        result = response.get_json()
    actual = [c for c in result['contexts'] if c.get('source_type') != 'floating_assist']
    assert [c['document_id'] for c in actual] == [1, 2]
    assert [c['cited'] for c in actual] == [False, False]


def test_answer_mentioning_recalled_document_gets_link_without_citation():
    """回归：确定性文档链接兜底必须接回生成链路，不依赖模型是否输出 [M编号]。"""
    chunks = [RetrievedChunk(
        capture_id=7, doc_key='document:7', text='正文：SMACT 定义',
        metadata={'source_type': 'document', 'document_id': 7,
                  'title': '容器云 GPU 指标采集项目',
                  'source_url': 'https://example.com/gpu'},
    )]
    result = _make_pipeline().query(
        'SMACT文档', supplied_contexts=chunks,
        llm=MockLlmBackend('结论来自《容器云 GPU 指标采集项目》，阈值 80%。'),
    )
    assert result.cited_contexts == []
    assert '[《容器云 GPU 指标采集项目》](https://example.com/gpu)' in result.answer


def test_truncated_citation_never_leaks_in_final_or_stream():
    out = []
    stream = CitationStream(out.append, candidates())
    stream.feed('解释[M')
    stream.feed('2')
    stream.finish()
    assert ''.join(out) == render_citations('解释[M2', candidates()) == '解释'


def test_memory_rules_forbid_refusing_general_knowledge_questions():
    """MEMORY_RULES 须与系统提示一致：通用知识问题用自身知识作答，缺失声明仅限私人/内部事实"""
    from rag.memory_evidence import MEMORY_RULES
    assert '召回偏宽松' in MEMORY_RULES
    assert '通用知识' in MEMORY_RULES
    assert '不得因候选记忆无关或为空而拒答' in MEMORY_RULES
    assert '唯一可以声明' in MEMORY_RULES
