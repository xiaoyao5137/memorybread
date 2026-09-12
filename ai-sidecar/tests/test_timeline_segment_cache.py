"""长多段任务恢复、完整父证据复核与分段事实复用。"""

import json

import pytest

from inference_queue import QueueEvictedError
from knowledge import extractor_v2 as module
from knowledge.extractor_v2 import _SegmentExtractionCache, _validated_data_facts, discarded_knowledge
from tests.test_timeline_single_segment import _captures, _fact, _responses, _result, extractor


def _separate_captures():
    captures = _captures(_fact()["evidence_quote"])
    for index, capture in enumerate(captures):
        capture["app_name"] = "App%s" % index
    return captures


def test_cache_is_bounded_lru_and_deep_copies_both_directions():
    cache = _SegmentExtractionCache(capacity=2)
    value = {"summary": "摘要", "data_facts": [{"value": "1"}]}
    cache.put("first", value)
    value["data_facts"][0]["value"] = "mutated"
    cached = cache.get("first")
    assert cached["data_facts"][0]["value"] == "1"
    cached["data_facts"][0]["value"] = "mutated-again"
    assert cache.get("first")["data_facts"][0]["value"] == "1"
    cache.put("second", {"summary": "二"})
    cache.get("first")
    cache.put("third", {"summary": "三"})
    assert cache.get("second") is None
    assert cache.get("first") and cache.get("third")


def test_preemption_reuses_completed_segments_and_does_not_cache_interrupted_one(extractor):
    captures = _separate_captures()
    calls = []
    def extract(capture):
        calls.append(capture["id"])
        if len(calls) == 3:
            raise QueueEvictedError("P0")
        return {"summary": "已完成的工作摘要", "data_facts": [_fact()]}
    extractor.extract_sync = extract
    with pytest.raises(QueueEvictedError):
        extractor._generate_segments(captures)
    segments, discarded, pages, facts = extractor._generate_segments(captures)
    assert calls == [101, 102, 103, 103]
    assert len(segments) == len(facts) == 3
    assert discarded == pages == []


@pytest.mark.parametrize("failure", [None, RuntimeError("temporary")])
def test_none_or_exception_is_never_cached(extractor, failure):
    captures = _captures()[:1]
    calls = []
    def extract(capture):
        calls.append(capture["id"])
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return None
        return {"summary": "本次成功摘要"}
    extractor.extract_sync = extract
    extractor._generate_segments(captures)
    extractor._generate_segments(captures)
    assert calls == [101, 101]


@pytest.mark.parametrize("change", ["model", "identity", "source", "member_id", "member_added"])
def test_cache_fingerprint_covers_model_identity_full_source_and_all_members(extractor, change):
    captures = _captures("正文" * 10000)[:2]
    calls = []
    def extract(capture):
        calls.append(capture["id"])
        return {"summary": "模型已经完成摘要"}
    extractor.extract_sync = extract
    extractor._generate_segments(captures)
    if change == "model":
        extractor.model = "another-model"
    elif change == "identity":
        extractor.user_identity = "another-user"
    elif change == "source":
        captures[1]["ax_text"] += "末尾新增证据"  # 模型窗口外的原文变化也必须失效。
    elif change == "member_id":
        captures[1]["id"] = 999
    else:
        captures.append({**captures[1], "id": 999})
    extractor._generate_segments(captures)
    assert len(calls) == 2


def test_parent_preemption_after_segments_reuses_successful_segment_results(extractor):
    captures = _separate_captures()
    segment_calls = []
    extractor.extract_sync = lambda capture: segment_calls.append(capture["id"]) or {"summary": "完成摘要"}
    checks = iter([False, True])
    with pytest.raises(QueueEvictedError):
        extractor.extract_merged(captures, preempt_check=lambda: next(checks))
    _responses(extractor, _result())
    assert extractor.extract_merged(captures) is not None
    assert segment_calls == [101, 102, 103]


@pytest.mark.parametrize("coherent,groups", [(False, [[101], [102, 103]]), (True, [[101, 102]])])
def test_partition_gate_runs_before_reusing_segment_facts(extractor, monkeypatch, coherent, groups):
    captures = _separate_captures()
    extractor.extract_sync = lambda capture: {"summary": "完成摘要", "data_facts": [_fact()]}
    _responses(extractor, _result(is_coherent=coherent, capture_groups=groups))
    def forbidden(*args, **kwargs):
        raise AssertionError("coherence must precede parent fact reuse")
    monkeypatch.setattr(module, "_validated_data_facts", forbidden)
    result = extractor.extract_merged(captures)
    if coherent:
        assert result is None
    else:
        assert result["_split_required"] is True


def test_completed_facts_survive_prompt_truncation_and_dedupe_with_main_result(extractor):
    first, second, foreign = _fact("6.28"), _fact("9.31"), _fact("99.99")
    captures = _captures(first["evidence_quote"])
    captures[1]["app_name"] = captures[2]["app_name"] = "Another App"
    captures[1]["ax_text"] = "无数字背景正文" * 2000 + second["evidence_quote"]
    validated, _ = _validated_data_facts([first], first["evidence_quote"])
    validated_second, _ = _validated_data_facts([second], second["evidence_quote"])
    extractor.extract_sync = lambda capture: {
        "summary": "完成工作结果整理",
        "data_facts": validated if capture["id"] == 101 else validated_second + [foreign],
    }
    extractor._build_merged_blocks = lambda captures: first["evidence_quote"]
    calls = _responses(extractor, _result(data_facts=[first]))
    def forbidden(*args, **kwargs):
        raise AssertionError("validated segment facts must avoid another recovery")
    extractor._recover_missing_data_facts = forbidden
    result = extractor.extract_merged(captures)
    assert len(calls) == 1
    assert {fact["value"] for fact in result["data_facts"]} == {"6.28", "9.31"}
    assert all(fact["decision_state"] == "published" for fact in result["data_facts"])
    assert len(json.loads(result["key_timestamps"])) == 2


def test_discarded_segment_facts_cannot_enter_parent_or_satisfy_its_evidence(extractor):
    first, second = _fact("6.28"), _fact("9.31")
    captures = _captures(second["evidence_quote"])
    captures[0].update(app_name="Discarded App", ax_text=first["evidence_quote"])
    def extract(capture):
        if capture["id"] == 101:
            return {**discarded_knowledge("no_value"), "data_facts": [first]}
        return {"summary": "完成工作结果整理", "data_facts": [first, second]}
    extractor.extract_sync = extract
    _responses(extractor, _result(capture_groups=[[102, 103]]))
    result = extractor.extract_merged(captures)
    assert [fact["value"] for fact in result["data_facts"]] == ["9.31"]
    assert result["_discarded_capture_ids"] == [101]


def test_duplicate_or_invalid_prefix_does_not_consume_24_fact_capacity():
    facts = []
    for index in range(24):
        fact = _fact(str(index + 1))
        facts.append(fact)
    source = "\n".join(fact["evidence_quote"] for fact in facts)
    accepted, _ = _validated_data_facts([{}] * 24 + [facts[0]] * 24 + facts, source)
    assert len(accepted) == 24


def test_segment_publication_is_rechecked_against_current_parent_context(extractor):
    fact = {
        **_fact("5"), "subject": "样例", "action": "", "target_context": "",
        "dimension": "", "metric": "参考值", "unit": "", "title": "样例参考值",
        "statement": "样例参考值为5", "evidence_quote": "样例参考值为5",
    }
    accepted, _ = _validated_data_facts(
        [fact], fact["evidence_quote"], publication_context={"importance": 4},
    )
    assert accepted[0]["decision_state"] == "published"
    captures = _captures(fact["evidence_quote"])
    captures[0]["app_name"] = "Other App"
    extractor.extract_sync = lambda capture: {"summary": "查看样例数据", "data_facts": accepted}
    _responses(extractor, _result(importance=1, activity_type="reading"))
    result = extractor.extract_merged(captures)
    assert len(result["data_facts"]) == 1
    assert result["data_facts"][0]["decision_state"] == "shadow"
