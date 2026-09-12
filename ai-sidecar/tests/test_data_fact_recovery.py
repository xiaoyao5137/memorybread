"""数据聚焦补提炼的结构约束、完整事实保留及无正文用量统计。"""

import json
import logging

import pytest
from inference_queue import QueueEvictedError
from model_schema import decoding_schema

from knowledge.extractor_v2 import (
    BAKE_BUNDLE_RESPONSE_SCHEMA,
    DATA_FACT_MAX_ITEMS,
    DATA_FACT_RECOVERY_RESPONSE_SCHEMA,
    BakeOutputTruncatedError,
    KnowledgeExtractorV2,
    _complete_data_fact_prefix,
    _DataFactRecoveryGuard,
    _ollama_compatible_format,
)


def _fact(index=0):
    subject = "演示项目%s" % index
    value = str(index + 1)
    quote = "%s已完成任务，任务耗时%s分钟" % (subject, value)
    return {
        "publishable": True,
        "needs_more_context": False,
        "title": subject + "已完成任务耗时",
        "subject": subject,
        "action": "完成任务",
        "target_context": subject,
        "dimension": "",
        "metric": "任务耗时",
        "value": value,
        "unit": "分钟",
        "statement": quote,
        "evidence_quote": quote,
        "semantic_relation": subject + "完成任务的耗时",
        "future_question": subject + "完成任务用了多久？",
        "decision_reason": "已完成任务的实测结果，供后续排期和效率验证使用",
        "confidence": "high",
    }


@pytest.fixture
def harness(monkeypatch):
    usage_logs = []
    monkeypatch.setattr("monitor.llm_tracker.log_llm_usage", lambda **kwargs: usage_logs.append(kwargs))
    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
    extractor.model = "mock-model"
    return extractor, usage_logs


def _response(facts, done_reason="stop"):
    return {
        "message": {"content": json.dumps({"data_facts": facts}, ensure_ascii=False)},
        "done_reason": done_reason,
        "prompt_eval_count": 100,
        "eval_count": 80,
    }


def test_empty_recovery_has_closed_schema_and_logs_only_stage_statistics(harness, caplog):
    extractor, usage_logs = harness
    calls = []

    def chat(**kwargs):
        calls.append(kwargs)
        return _response([])

    extractor._ollama_chat = chat
    source = _fact()["evidence_quote"]
    with caplog.at_level(logging.INFO, logger="knowledge.extractor_v2"):
        assert extractor._recover_missing_data_facts(source, {}, "test-empty") == ([], 0)

    assert len(calls) == 1
    assert calls[0]["format"] == decoding_schema(DATA_FACT_RECOVERY_RESPONSE_SCHEMA)
    schema = calls[0]["format"]
    assert schema["additionalProperties"] is False
    assert list(schema["properties"]) == ["data_facts"]
    item = schema["properties"]["data_facts"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])
    assert list(item["properties"])[:2] == ["evidence_quote", "value"]
    assert item["properties"]["value"]["minLength"] == 1
    for keyword in ("maxItems", "maxLength", "anyOf"):
        assert keyword not in json.dumps(schema)
    assert usage_logs[0]["caller_id"] == "data_recovery:test-empty"
    assert usage_logs[0]["prompt_tokens"] == 100
    assert usage_logs[0]["completion_tokens"] == 80
    assert usage_logs[0]["raw_preview"] is None
    assert usage_logs[0]["response_preview"] is None
    assert "status=success" in caplog.text
    assert "accepted=0 rejected=0" in caplog.text
    assert source not in caplog.text


def test_recovery_compacts_transport_but_preserves_local_contract_limits():
    item = DATA_FACT_RECOVERY_RESPONSE_SCHEMA["properties"]["data_facts"]["items"]
    assert item["properties"]["evidence_quote"]["maxLength"] == 500
    assert DATA_FACT_RECOVERY_RESPONSE_SCHEMA["properties"]["data_facts"]["maxItems"] == 24
    ordinary = _ollama_compatible_format(BAKE_BUNDLE_RESPONSE_SCHEMA)
    assert "maxLength" not in json.dumps(ordinary)
    assert ordinary == decoding_schema(BAKE_BUNDLE_RESPONSE_SCHEMA)


def test_recovery_keeps_all_24_distinct_grounded_facts(harness):
    extractor, _usage_logs = harness
    expected = [_fact(index) for index in range(DATA_FACT_MAX_ITEMS)]
    extractor._ollama_chat = lambda **kwargs: _response(expected)

    facts, rejected = extractor._recover_missing_data_facts(
        "\n".join(fact["evidence_quote"] for fact in expected), {}, "test-full-capacity",
    )

    assert len(facts) == 24
    assert rejected == 0
    assert [fact["value"] for fact in facts] == [fact["value"] for fact in expected]


@pytest.mark.parametrize("done_reason", ["length", "repetition", "stop"])
def test_truncated_recovery_retains_only_complete_grounded_prefix(harness, done_reason):
    extractor, usage_logs = harness
    good = _fact()
    ungrounded = _fact(90)
    prefix = '{"data_facts": [' + json.dumps(good, ensure_ascii=False) + ','
    prefix += json.dumps(ungrounded, ensure_ascii=False) + ',{"subject":"incomplete'

    def chat(**kwargs):
        if done_reason == "repetition":
            raise BakeOutputTruncatedError("repetition", partial_content=prefix)
        return {"message": {"content": prefix}, "done_reason": done_reason}

    extractor._ollama_chat = chat
    facts, rejected = extractor._recover_missing_data_facts(
        good["evidence_quote"], {}, "test-truncated",
    )

    assert len(facts) == 1
    assert facts[0]["subject"] == good["subject"]
    assert rejected == 1
    assert usage_logs[0]["status"] == "failed"
    assert usage_logs[0]["raw_preview"] is None
    assert usage_logs[0]["response_preview"] is None


def test_prefix_parser_respects_escaped_braces_and_does_not_accept_nested_arrays():
    fact = {"evidence_quote": 'text with { } and "quotes"', "value": "4"}
    raw = '{"data_facts": [' + json.dumps(fact) + ',{"value":'
    assert _complete_data_fact_prefix(raw) == [fact]
    assert _complete_data_fact_prefix('{"other": {"data_facts": [{}]}}') == []
    assert _complete_data_fact_prefix('{"data_facts": [{"value":') == []


def test_recovery_still_rejects_false_false_noise_when_response_ignores_schema(harness):
    extractor, _usage_logs = harness
    noise = {**_fact(), "publishable": False, "needs_more_context": False}
    extractor._ollama_chat = lambda **kwargs: _response([noise])

    facts, rejected = extractor._recover_missing_data_facts(
        noise["evidence_quote"], {}, "test-rejected-noise",
    )

    assert facts == []
    assert rejected == 1


def test_recovery_error_telemetry_does_not_copy_upstream_response(harness, caplog):
    extractor, usage_logs = harness
    private_response = "private-upstream-response-body"

    def chat(**kwargs):
        raise RuntimeError(private_response)

    extractor._ollama_chat = chat
    with caplog.at_level(logging.INFO, logger="knowledge.extractor_v2"):
        assert extractor._recover_missing_data_facts(
            _fact()["evidence_quote"], {}, "test-private-error",
        ) == ([], 0)

    assert usage_logs[0]["status"] == "failed"
    assert usage_logs[0]["error_msg"] == "RuntimeError"
    assert private_response not in json.dumps(usage_logs)
    assert private_response not in caplog.text


@pytest.mark.parametrize('metadata', [
    {'done_reason': 'PRIVATE_METADATA'},
    {'done_reason': {'body': 'PRIVATE_METADATA'}},
    {'usage': 'PRIVATE_METADATA', 'prompt_eval_count': 'PRIVATE_METADATA'},
    {'usage': {'prompt_tokens': 'PRIVATE_METADATA', 'completion_tokens': ['PRIVATE_METADATA']},
     'prompt_eval_count': -1, 'eval_count': True},
    {'usage': {'prompt_tokens': 2**80, 'completion_tokens': -3}, 'prompt_eval_count': False, 'eval_count': -1},
])
def test_recovery_rejects_private_or_malformed_usage_metadata(harness, caplog, metadata):
    extractor, logs = harness
    fact = _fact()
    response = {**_response([fact]), **metadata}
    extractor._ollama_chat = lambda **_kwargs: response
    with caplog.at_level(logging.INFO):
        accepted, rejected = extractor._recover_missing_data_facts(fact['evidence_quote'], {}, 'metadata-test')
    assert len(accepted) == 1 and rejected == 0
    assert 'PRIVATE_METADATA' not in json.dumps(logs) + caplog.text
    assert type(logs[0]['prompt_tokens']) is int and logs[0]['prompt_tokens'] >= 0
    assert type(logs[0]['completion_tokens']) is int and logs[0]['completion_tokens'] >= 0


def test_recovery_keeps_explicit_zero_usage(harness):
    extractor, logs = harness
    fact = _fact()
    response = {**_response([fact]), 'usage': {'prompt_tokens': 0, 'completion_tokens': 0}}
    extractor._ollama_chat = lambda **_kwargs: response
    extractor._recover_missing_data_facts(fact['evidence_quote'], {}, 'zero-usage')
    assert logs[0]['prompt_tokens'] == logs[0]['completion_tokens'] == 0


def test_data_recovery_preemption_propagates_with_type_only_telemetry(harness):
    extractor, usage_logs = harness

    def chat(**kwargs):
        raise QueueEvictedError("upstream-details-must-not-be-logged")

    extractor._ollama_chat = chat
    with pytest.raises(QueueEvictedError):
        extractor._recover_missing_data_facts(_fact()["evidence_quote"], {}, "test-preempt")
    assert usage_logs[0]["error_msg"] == "QueueEvictedError"


@pytest.mark.parametrize("value", ["$4.99", "87%", "3-5", "16分31秒", "1小时20分钟"])
def test_recovery_guard_keeps_grounded_prices_percentages_ranges_and_compound_values(value):
    quote = "演示项目已观测结果为" + value
    guard = _DataFactRecoveryGuard(quote)
    fact = {"value": value, "evidence_quote": quote, "subject": "演示项目"}
    assert not guard(json.dumps({"data_facts": [fact]}, ensure_ascii=False))
    assert guard.checked_count == 1 and guard.invalid_streak == 0


def test_recovery_guard_stops_only_after_three_complete_ungrounded_items():
    guard = _DataFactRecoveryGuard("演示项目已完成，耗时16分31秒")
    bad = {"value": "", "evidence_quote": "无数值正文"}
    prefix = '{"data_facts":['
    for index in range(3):
        prefix += ("," if index else "") + json.dumps(bad, ensure_ascii=False)
        assert guard(prefix) is (index == 2)
        if index < 2:
            assert not guard(prefix + ',{"value":')  # 不重数旧对象或未闭合字段。
    assert guard.checked_count == 3


def test_recovery_guard_resets_on_grounded_fact_and_keeps_24_capacity():
    expected = [_fact(index) for index in range(24)]
    source = "\n".join(fact["evidence_quote"] for fact in expected)
    guard = _DataFactRecoveryGuard(source)
    bad = {"value": "9999", "evidence_quote": "不存在的项目耗时9999秒"}
    prefix = '{"data_facts":[' + json.dumps(bad) + ',' + json.dumps(bad)
    assert not guard(prefix) and guard.invalid_streak == 2
    assert not guard(prefix + ',' + json.dumps(expected[0], ensure_ascii=False))
    assert guard.invalid_streak == 0
    guard = _DataFactRecoveryGuard(source)
    assert not guard(json.dumps({"data_facts": expected}, ensure_ascii=False))
    assert guard.checked_count == 24 and guard.invalid_streak == 0


def test_recovery_guard_uses_existing_evidence_realignment():
    guard = _DataFactRecoveryGuard("演示项目已完成任务，耗时16分31秒")
    fact = {"subject": "演示项目", "value": "16分31秒", "evidence_quote": "模型概括并非逐字引文"}
    assert not guard(json.dumps({"data_facts": [fact]}, ensure_ascii=False))
    assert guard.invalid_streak == 0


def test_guard_early_stop_preserves_preceding_valid_fact(harness):
    extractor, usage_logs = harness
    good = _fact()
    bad = {"value": "", "evidence_quote": "无数值正文"}
    content = json.dumps({"data_facts": [good, bad, bad, bad]}, ensure_ascii=False)
    def chat(**kwargs):
        assert kwargs["output_guard"](content)
        raise BakeOutputTruncatedError("统计早停", partial_content=content, stop_reason="ungrounded")
    extractor._ollama_chat = chat
    accepted, rejected = extractor._recover_missing_data_facts(good["evidence_quote"], {}, "early-stop")
    assert len(accepted) == 1 and accepted[0]["value"] == good["value"]
    assert rejected == 3
    assert usage_logs[0]["error_msg"] == "data_fact_recovery_ungrounded"


def test_guard_uses_final_anchor_gate_even_when_value_and_quote_match_source():
    good = _fact()
    bad = {**good, "subject": "不存在的对象", "target_context": "", "metric": "未指明的指标"}
    guard = _DataFactRecoveryGuard(good["evidence_quote"])
    assert not guard(json.dumps({"data_facts": [bad, bad]}, ensure_ascii=False))
    assert guard(json.dumps({"data_facts": [bad, bad, bad]}, ensure_ascii=False))
    assert guard.checked_count == 3


def test_valid_shadow_resets_guard_and_preserves_current_publication_context():
    fact = {**_fact(), "publishable": False, "needs_more_context": True}
    bad = {"value": "", "evidence_quote": "无数值正文"}
    guard = _DataFactRecoveryGuard(
        fact["evidence_quote"], publication_context={"importance": 1, "activity_type": "reading"},
    )
    assert not guard(json.dumps({"data_facts": [bad, bad, fact]}, ensure_ascii=False))
    assert guard.invalid_streak == 0
