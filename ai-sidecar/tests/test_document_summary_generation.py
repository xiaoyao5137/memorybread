import concurrent.futures
import json

import pytest

import model_api_server
from knowledge.extractor_v2 import KnowledgeExtractorV2


def source():
    return dict(document_id=953, source_snapshot_id=61, expected_updated_at=100,
                content_text="缓存命中率提升。尚未完成线上验收。")


def test_summary_uses_exact_body_and_preserves_source_revision():
    extractor = object.__new__(KnowledgeExtractorV2)
    calls = []
    def infer(caller, system, user, **kwargs):
        assert kwargs['capture_trace'] is False
        calls.append(json.loads(user))
        return {"summary": "缓存命中率提升，但线上验收未完成。",
                "evidence_block_ids": [1]}, {}
    extractor._call_bake_llm = infer
    value = source()
    value['summary'] = '旧错误摘要'
    value['title'] = '不可信标题'
    result = extractor.summarize_document_source(value)
    assert calls == [{'body_blocks': [{'id': 1, 'text': value['content_text']}]}]
    assert result['evidence_quotes'] == [value['content_text']]
    assert result['source_snapshot_id'] == 61
    assert result['expected_updated_at'] == 100
    assert result['generation_version'] == 'document-summary.v1'


@pytest.mark.parametrize('output', [None, {},
    {'summary': '结论', 'evidence_quotes': []},
    {'summary': '已经验收', 'evidence_quotes': ['已经完成线上验收。']},
    {'summary': '结论', 'evidence_quotes': [' ']},
    {'summary': 'x' * 501, 'evidence_quotes': ['缓存命中率提升。']},
    {'summary': '结论', 'evidence_block_ids': [True]},
    {'summary': '结论', 'evidence_block_ids': [2]},
    {'summary': '结论', 'evidence_block_ids': [0]},
])
def test_summary_rejects_missing_or_fabricated_evidence(output):
    extractor = object.__new__(KnowledgeExtractorV2)
    extractor._call_bake_llm = lambda *a, **k: (output, {})
    with pytest.raises(ValueError, match='DOCUMENT_SUMMARY_'):
        extractor.summarize_document_source(source())


def test_summary_endpoint_uses_background_queue_and_strips_extra_inputs(monkeypatch):
    calls = []
    class Extractor:
        def summarize_document_source(self, value):
            calls.append(value)
            return {'summary': '结果', 'source_snapshot_id': value['source_snapshot_id']}
    class Queue:
        def submit_sync(self, priority, fn, **kwargs):
            assert priority == model_api_server.Priority.P2
            assert kwargs['lane'] == model_api_server.LANE_P2_BAKE
            assert kwargs['timeout'] > 0 and kwargs['queue_timeout'] > 0
            return fn()
    monkeypatch.setattr(model_api_server, 'get_bake_extractor', Extractor)
    monkeypatch.setattr(model_api_server, 'get_global_queue', Queue)
    response = model_api_server.app.test_client().post('/bake/document_summary',
        json=dict(source(), summary='旧摘要', title='旧标题'))
    assert response.status_code == 200
    assert calls == [source()]


def test_summary_block_failure_reports_only_structure_counts():
    extractor = object.__new__(KnowledgeExtractorV2)
    extractor._call_bake_llm = lambda *a, **k: (
        {'summary': '结论', 'evidence_block_ids': ['1', 'sensitive raw value']}, {})
    with pytest.raises(ValueError) as failure:
        extractor.summarize_document_source(source())
    diagnostics = failure.value.diagnostics
    assert diagnostics['numeric_string_count'] == 1
    assert diagnostics['selected_integer_count'] == 0
    assert diagnostics['selected_count'] == 2
    assert all(type(v) in (int, bool) for v in diagnostics.values())


def test_summary_evidence_is_bounded_by_source_not_fixed_number_of_blocks():
    extractor = object.__new__(KnowledgeExtractorV2)
    extractor._call_bake_llm = lambda *a, **k: (
        {'summary': '多个章节的结论', 'evidence_block_ids': [1, 2, 3, 4, 5, 6, 7, 7]}, {})
    data = source()
    data['content_text'] = ''.join('章节%d的事实。\n' % i for i in range(1, 9))
    result = extractor.summarize_document_source(data)
    assert len(result['evidence_quotes']) == 7
    assert all(q in data['content_text'] for q in result['evidence_quotes'])
    assert sum(len(q) for q in result['evidence_quotes']) <= len(data['content_text'])


@pytest.mark.parametrize('field,value', [('document_id', True), ('source_snapshot_id', '61'),
    ('expected_updated_at', 0), ('content_text', ''), ('content_text', None)])
def test_summary_endpoint_rejects_invalid_input_without_model(monkeypatch, field, value):
    def unexpected():
        pytest.fail('model must not initialize for invalid input')
    monkeypatch.setattr(model_api_server, 'get_bake_extractor', unexpected)
    data = source()
    data[field] = value
    assert model_api_server.app.test_client().post('/bake/document_summary', json=data).status_code == 400


def test_summary_endpoint_does_not_truncate_over_budget_source(monkeypatch):
    monkeypatch.setattr('monitor.llm_tracker.estimate_tokens', lambda _: 10**9)
    monkeypatch.setattr(model_api_server, 'get_bake_extractor', lambda: pytest.fail('unexpected model'))
    response = model_api_server.app.test_client().post('/bake/document_summary', json=source())
    assert response.status_code == 413


@pytest.mark.parametrize('failure,status', [(concurrent.futures.TimeoutError(), 504),
    (ValueError('DOCUMENT_SUMMARY_EVIDENCE_INVALID'), 422)])
def test_summary_endpoint_reports_failed_generation(monkeypatch, failure, status):
    class Queue:
        def submit_sync(self, *args, **kwargs):
            raise failure
    monkeypatch.setattr(model_api_server, 'get_bake_extractor', lambda: object())
    monkeypatch.setattr(model_api_server, 'get_global_queue', Queue)
    assert model_api_server.app.test_client().post('/bake/document_summary', json=source()).status_code == status


def test_hierarchical_summary_reads_all_sections_and_keeps_original_evidence(monkeypatch):
    # Force multiple reduction levels so tail content and provenance cannot hide in one call.
    monkeypatch.setattr('knowledge.extractor_v2.BAKE_INPUT_TOKEN_BUDGET', 790)
    monkeypatch.setattr('monitor.llm_tracker.estimate_tokens', len)
    extractor = object.__new__(KnowledgeExtractorV2)
    calls = []
    def infer(caller, system, user, **kwargs):
        blocks = json.loads(user)['body_blocks']
        calls.append(blocks)
        assert len(user) * 1.35 + 600 <= 790
        assert kwargs['capture_trace'] is False
        return {'summary': '保留条件和末尾限制。',
                'evidence_block_ids': [b['id'] for b in blocks]}, {}
    extractor._call_bake_llm = infer
    data = source()
    data['content_text'] = ''.join('第%d节：%s尚未验收。\n' % (i, '正文' * 20) for i in range(9))
    result = extractor.summarize_document_source(data)
    original = extractor.document_summary_blocks(data['content_text'])
    assert [b['text'] for call in calls[:9] for b in call] == [b['text'] for b in original]
    assert result['evidence_quotes'] == [b['text'] for b in original]
    assert len(calls) > 10  # leaves, multiple intermediate nodes, root
    assert result['source_snapshot_id'] == data['source_snapshot_id']


def test_hierarchy_failure_never_returns_partial_summary(monkeypatch):
    monkeypatch.setattr('knowledge.extractor_v2.BAKE_INPUT_TOKEN_BUDGET', 790)
    monkeypatch.setattr('monitor.llm_tracker.estimate_tokens', len)
    extractor = object.__new__(KnowledgeExtractorV2)
    calls = []
    def infer(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            return {'summary': '不能发布', 'evidence_block_ids': [99]}, {}
        return {'summary': '第一节', 'evidence_block_ids': [1]}, {}
    extractor._call_bake_llm = infer
    data = source()
    data['content_text'] = ('这是必须读取的正文。' * 5 + '\n') * 3
    with pytest.raises(ValueError, match='DOCUMENT_SUMMARY_BLOCK_INVALID'):
        extractor.summarize_document_source(data)
    assert len(calls) == 2


def test_hierarchy_checks_cancellation_between_calls(monkeypatch):
    monkeypatch.setattr('knowledge.extractor_v2.BAKE_INPUT_TOKEN_BUDGET', 790)
    monkeypatch.setattr('monitor.llm_tracker.estimate_tokens', len)
    calls = []
    def checkpoint():
        if calls:
            raise concurrent.futures.TimeoutError()
    monkeypatch.setattr('inference_queue.raise_if_preempted', checkpoint)
    extractor = object.__new__(KnowledgeExtractorV2)
    def infer(*args, **kwargs):
        calls.append(1)
        return {'summary': '第一节', 'evidence_block_ids': [1]}, {}
    extractor._call_bake_llm = infer
    data = source()
    data['content_text'] = ('这是必须读取的正文。' * 5 + '\n') * 3
    with pytest.raises(concurrent.futures.TimeoutError):
        extractor.summarize_document_source(data)
    assert len(calls) == 1


def test_endpoint_routes_whole_long_body_to_hierarchical_generation(monkeypatch):
    monkeypatch.setattr('knowledge.extractor_v2.BAKE_INPUT_TOKEN_BUDGET', 790)
    monkeypatch.setattr('monitor.llm_tracker.estimate_tokens', len)
    data = source()
    data['content_text'] = ('这是必须读取的正文。' * 5 + '\n') * 3
    class Extractor:
        def summarize_document_source(self, value):
            assert value == data
            return {'summary': '全部内容'}
    class Queue:
        def submit_sync(self, priority, fn, **kwargs):
            assert kwargs['timeout'] == model_api_server.BAKE_LONG_INFERENCE_TIMEOUT_SECONDS
            return fn()
    monkeypatch.setattr(model_api_server, 'get_bake_extractor', Extractor)
    monkeypatch.setattr(model_api_server, 'get_global_queue', Queue)
    assert model_api_server.app.test_client().post('/bake/document_summary', json=data).status_code == 200


def test_oversized_intermediate_summary_fails_without_dropping_sections(monkeypatch):
    monkeypatch.setattr('knowledge.extractor_v2.BAKE_INPUT_TOKEN_BUDGET', 790)
    monkeypatch.setattr('monitor.llm_tracker.estimate_tokens', len)
    extractor = object.__new__(KnowledgeExtractorV2)
    calls = []
    def infer(*args, **kwargs):
        calls.append(1)
        return {'summary': '过长中间结果' * 60, 'evidence_block_ids': [1]}, {}
    extractor._call_bake_llm = infer
    data = source()
    data['content_text'] = ('这是必须读取的正文。' * 5 + '\n') * 3
    with pytest.raises(ValueError, match='DOCUMENT_SUMMARY_INPUT_BUDGET'):
        extractor.summarize_document_source(data)
    assert len(calls) == 3
