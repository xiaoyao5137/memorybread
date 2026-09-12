"""单分段合并只做一次全量提炼，仍保留一致性、质量和数据证据门禁。"""

import json

import pytest

import knowledge.extractor_v2 as extractor_module
from inference_queue import QueueEvictedError
from knowledge.extractor_v2 import KnowledgeExtractorV2, discarded_knowledge


class _NoopTracker:
    def __init__(self, *args, **kwargs):
        self._prompt_tokens = 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def set_response(self, response):
        pass

    def set_tokens(self, prompt, completion):
        pass


@pytest.fixture
def extractor(monkeypatch):
    instance = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
    instance.model = "test-model"
    instance.user_identity = ""
    monkeypatch.setattr(extractor_module, "_rag_is_active", lambda: False)
    monkeypatch.setattr("monitor.llm_tracker.LLMCallTracker", _NoopTracker)
    return instance


def _captures(text="用户分析了工作队列持续堆积的原因并检查提炼阶段的调用情况。"):
    return [
        {
            "id": capture_id,
            "ts": 1710000000000 + index * 60000,
            "app_name": "Editor",
            "window_title": "Queue review",
            "ax_text": text,
            "url": "https://example.com/queue-review",
        }
        for index, capture_id in enumerate([101, 102, 103])
    ]


def _result(**overrides):
    return {
        "is_coherent": True,
        "coherence_reason": "采集均围绕工作队列提炼开销的排查",
        "capture_groups": [[101, 102, 103]],
        "overview": "用户排查了工作队列持续堆积的原因，并确认重复提炼导致处理开销增加。",
        "details": "检查各阶段调用并验证单分段可以复用合并后的摘要。",
        "entities": ["工作队列"],
        "category": "代码",
        "importance": 4,
        "data_facts": [],
        "data_pages": [],
        **overrides,
    }


def _responses(extractor, *results):
    calls = []
    pending = iter(results)

    def chat(**kwargs):
        calls.append(kwargs)
        return {"message": {"content": json.dumps(next(pending), ensure_ascii=False)}}

    extractor._ollama_chat = chat
    return calls


def _fact(value="6.28"):
    return {
        "title": "生服模特库在电商AIGC中复用的成本节省金额",
        "subject": "生服模特库",
        "action": "复用",
        "target_context": "电商AIGC",
        "dimension": "",
        "metric": "成本节省金额",
        "value": value,
        "unit": "万",
        "statement": "生服模特库在电商AIGC中的复用节省约%s万成本。" % value,
        "evidence_quote": "生服模特库在电商AIGC中的复用已成功合并，节省约%s万成本" % value,
        "confidence": "high",
        "semantic_relation": "生服模特库在电商AIGC复用产生的成本节省金额",
        "future_question": "生服模特库在电商AIGC中复用节省了多少成本？",
        "decision_reason": "可用于后续复用决策和收益验证，且事实关系与证据完整",
        "publishable": True,
        "needs_more_context": False,
    }


@pytest.mark.parametrize('failure', [None, 'json', 'exception', 'coherence', 'groups', 'overview'])
def test_merged_diagnostics_do_not_publish_model_text(extractor, monkeypatch, caplog, failure):
    import logging
    secret = 'PRIVATE_MODEL_BODY https://docs.example.com/private?token=SECRET'
    result = _result(overview=secret, details=secret)
    if failure == 'coherence':
        result['is_coherent'] = secret
    if failure == 'groups':
        result['capture_groups'] = secret
    if failure == 'overview':
        result['overview'] = 'SKIP'
    def chat(**kwargs):
        if failure == 'exception':
            raise RuntimeError(secret)
        return {'message': {'content': secret if failure == 'json' else json.dumps(result)}}
    extractor._ollama_chat = chat
    monkeypatch.setattr(extractor_module, '_overview_quality_reason', lambda *_args: None)
    monkeypatch.setattr(extractor, '_recover_missing_data_facts', lambda *_args: ([], 0))
    with caplog.at_level(logging.INFO):
        output = extractor.extract_merged(_captures())
    assert 'PRIVATE_MODEL_BODY' not in caplog.text and 'token=SECRET' not in caplog.text
    if failure is None:
        assert output['overview'] == secret and output['details'] == secret


@pytest.mark.parametrize('failure', [None, 'json', 'exception'])
def test_single_capture_diagnostics_keep_model_text_private(extractor, monkeypatch, caplog, failure):
    import logging
    secret = 'PRIVATE_SINGLE_BODY https://docs.example.com/private?token=SECRET'
    def chat(**kwargs):
        if failure == 'exception':
            raise RuntimeError(secret)
        return {'message': {'content': secret if failure == 'json' else json.dumps(_result(overview=secret))}}
    extractor._ollama_chat = chat
    monkeypatch.setattr(extractor_module, '_overview_quality_reason', lambda *_args: None)
    monkeypatch.setattr(extractor, '_recover_missing_data_facts', lambda *_args: ([], 0))
    with caplog.at_level(logging.INFO):
        result = extractor.extract_sync(_captures()[0])
    assert secret not in caplog.text and 'PRIVATE_SINGLE_BODY' not in caplog.text
    if failure is None:
        assert result['overview'] == secret
    else:
        assert result is None


@pytest.mark.parametrize('failure', [False, True])
def test_segment_diagnostics_preserve_summary_only_in_result(extractor, monkeypatch, caplog, failure):
    import logging
    secret = 'PRIVATE_SEGMENT_BODY https://docs.example.com/private?token=SECRET'
    def extract(*_args):
        if failure:
            raise RuntimeError(secret)
        return {'summary': secret}
    monkeypatch.setattr(extractor, 'extract_sync', extract)
    with caplog.at_level(logging.INFO):
        result = extractor._generate_segments(_captures())
    assert 'PRIVATE_SEGMENT_BODY' not in caplog.text and 'token=SECRET' not in caplog.text
    if not failure:
        assert result[0][0]['summary'] == secret
        # Cached reuse must retain the business summary without logging it.
        assert extractor._generate_segments(_captures())[0][0]['summary'] == secret
    else:
        assert result == ([], [], [], [])


def test_same_segment_reuses_main_summary_and_preserves_all_capture_metadata(extractor):
    captures = _captures()
    page = {
        "url": captures[0]["url"],
        "page_kind": "data_content",
        "title": "Queue review",
    }
    calls = _responses(extractor, _result(data_pages=[page]))

    result = extractor.extract_merged(captures)

    assert result is not None
    assert len(calls) == 1
    assert "先判断它们是否确属同一个工作任务" in calls[0]["messages"][1]["content"]
    assert json.loads(result["capture_ids"]) == [101, 102, 103]
    assert json.loads(result["key_timestamps"]) == [{
        "capture_ids": [101, 102, 103],
        "start_ts": captures[0]["ts"],
        "end_ts": captures[-1]["ts"],
        "summary": result["summary"],
    }]
    assert result["duration_minutes"] == 2
    assert result["data_pages"] == [page]
    assert "_discarded_capture_ids" not in result


@pytest.mark.parametrize("overview,reason", [("SKIP", "no_value"), ("按钮菜单", "quality")])
def test_same_segment_preserves_deterministic_discard(extractor, overview, reason):
    calls = _responses(extractor, _result(overview=overview))

    assert extractor.extract_merged(_captures()) == discarded_knowledge(reason)
    assert len(calls) == 1


def test_same_window_still_splits_unrelated_tasks(extractor):
    calls = _responses(extractor, _result(
        is_coherent=False,
        coherence_reason="工作队列与另一个文档任务无关",
        capture_groups=[[101, 103], [102]],
    ))

    result = extractor.extract_merged(_captures())

    assert result["_split_required"] is True
    assert result["capture_groups"] == [[101, 103], [102]]
    assert "key_timestamps" not in result
    assert len(calls) == 1


@pytest.mark.parametrize("groups", [[[101, 102]], [[101, 102, 103, 104]], [[101, 101, 102, 103]]])
def test_same_segment_cannot_persist_invalid_capture_partition(extractor, groups):
    calls = _responses(extractor, _result(capture_groups=groups))

    assert extractor.extract_merged(_captures()) is None
    assert len(calls) == 1


def test_same_segment_still_repairs_missing_contract_fields(extractor):
    missing = _result()
    del missing["capture_groups"]
    calls = _responses(extractor, missing, {"capture_groups": [[101, 102, 103]]})

    result = extractor.extract_merged(_captures())

    assert result is not None
    assert len(calls) == 2
    assert "上一次输出缺少字段:capture_groups" in calls[1]["messages"][1]["content"]


def test_same_segment_validates_facts_without_a_second_full_extraction(extractor):
    captures = _captures(_fact()["evidence_quote"])
    calls = _responses(extractor, _result(data_facts=[_fact(), _fact("99.99")]))

    result = extractor.extract_merged(captures)

    assert result is not None
    assert len(calls) == 1
    assert [fact["value"] for fact in result["data_facts"]] == ["6.28"]
    assert result["data_fact_rejected_count"] == 1


def test_same_segment_runs_focused_data_recovery_only_once(extractor):
    captures = _captures(_fact()["evidence_quote"])
    calls = _responses(extractor, _result(), {"data_facts": [_fact(), _fact("99.99")]})

    result = extractor.extract_merged(captures)

    assert result is not None
    assert len(calls) == 2
    assert "独立核验是否真的遗漏事实" in calls[1]["messages"][1]["content"]
    assert [fact["value"] for fact in result["data_facts"]] == ["6.28"]
    assert result["data_fact_rejected_count"] == 1


def test_multiple_segments_still_exclude_discarded_captures_and_reuse_pages(extractor, monkeypatch):
    captures = _captures()
    captures[0]["app_name"] = "Menu"
    captures[0]["ax_text"] = "低价值界面片段不应进入后续合并文本"
    page = {"url": captures[1]["url"], "page_kind": "data_content", "title": "Queue review"}
    segment_calls = []

    def extract_segment(capture, db_conn=None):
        segment_calls.append(capture["id"])
        if capture["id"] == 101:
            return discarded_knowledge("no_value")
        return {"summary": "有效工作内容", "data_pages": [page]}

    monkeypatch.setattr(extractor, "extract_sync", extract_segment)
    calls = _responses(extractor, _result(capture_groups=[[102, 103]]))

    result = extractor.extract_merged(captures)

    assert result is not None
    assert segment_calls == [101, 102]
    assert len(calls) == 1
    assert "低价值界面片段" not in calls[0]["messages"][1]["content"]
    assert json.loads(result["capture_ids"]) == [102, 103]
    assert result["_discarded_capture_ids"] == [101]
    assert json.loads(result["key_timestamps"])[0]["capture_ids"] == [102, 103]
    assert result["data_pages"] == [page]


@pytest.mark.parametrize("checks", [[True], [False, True]])
def test_explicit_preemption_is_not_a_content_failure(extractor, checks):
    pending = iter(checks)
    with pytest.raises(QueueEvictedError):
        extractor.extract_merged(_captures(), preempt_check=lambda: next(pending))


@pytest.mark.parametrize("capture_count", [1, 3])
def test_rag_activity_is_not_a_content_failure(extractor, monkeypatch, capture_count):
    monkeypatch.setattr(extractor_module, "_rag_is_active", lambda: True)
    with pytest.raises(QueueEvictedError):
        extractor.extract_merged(_captures()[:capture_count])


@pytest.mark.parametrize("capture_count", [1, 3])
def test_model_io_preemption_propagates_from_timeline_extractors(extractor, capture_count):
    def chat(**kwargs):
        raise QueueEvictedError("test-preempted")

    extractor._ollama_chat = chat
    with pytest.raises(QueueEvictedError):
        extractor.extract_merged(_captures()[:capture_count])


def test_multi_segment_preemption_is_not_swallowed(extractor, monkeypatch):
    captures = _captures()
    captures[0]["app_name"] = "Other"

    def preempted(capture):
        raise QueueEvictedError("test-preempted")

    monkeypatch.setattr(extractor, "extract_sync", preempted)
    with pytest.raises(QueueEvictedError):
        extractor.extract_merged(captures)


def test_contract_repair_preemption_is_not_swallowed(extractor):
    missing = _result()
    del missing["capture_groups"]
    calls = []

    def chat(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"message": {"content": json.dumps(missing)}}
        raise QueueEvictedError("test-preempted")

    extractor._ollama_chat = chat
    with pytest.raises(QueueEvictedError):
        extractor.extract_merged(_captures())
