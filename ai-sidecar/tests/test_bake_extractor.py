from __future__ import annotations

import json
import types

import pytest

from inference_queue import QueueEvictedError
from knowledge.extractor_v2 import (
    BAKE_BUNDLE_PROMPT,
    BAKE_BUNDLE_RESPONSE_SCHEMA,
    BAKE_COMPACT_BUNDLE_PROMPT,
    BAKE_COMPACT_BUNDLE_RESPONSE_SCHEMA,
    BAKE_CONTEXT_WINDOW_TOKENS,
    BAKE_INPUT_TOKEN_BUDGET,
    BAKE_KNOWLEDGE_PROMPT,
    BAKE_NUM_PREDICT,
    BAKE_RETRY_NUM_PREDICT,
    BAKE_RETRY_REPEAT_PENALTY,
    BAKE_SHARED_PROMPT,
    BAKE_TIMEOUT_BUNDLE_RESPONSE_SCHEMA,
    BAKE_TIMEOUT_RETRY_NUM_PREDICT,
    BAKE_TIMEOUT_RETRY_REPEAT_PENALTY,
    BAKE_RESPONSE_SCHEMA,
    BakeModelRequestError,
    BakeOutputTruncatedError,
    KnowledgeExtractorV2,
    _extract_json_object,
    _extract_ollama_response_text,
    _ollama_compatible_format,
)


class MessageLike:
    def __init__(self, content: str = "", thinking: str = ""):
        self.content = content
        self.thinking = thinking


class ResponseLike:
    def __init__(self, message):
        self.message = message




SAMPLE_CANDIDATE = {
    "source_timeline_id": 1,
    "source_capture_id": 10,
    "source_capture_count": 3,
    "effective_capture_count": 3,
    "summary": "修复 bake pipeline 的 JSON 提炼链路",
    "overview": "定位 sidecar 返回空内容导致 bake 三类产物全部 rejected。",
    "details": "检查 extractor_v2 的 JSON 解析与 response shape 兼容逻辑，并补充测试覆盖。",
    "importance": 4,
    "occurrence_count": 1,
    "observed_at": 1710000000000,
    "event_time_start": None,
    "event_time_end": None,
    "history_view": False,
    "content_origin": "live_interaction",
    "activity_type": "coding",
    "evidence_strength": "high",
    "capture_ts": 1710000000000,
    "capture_app_name": "Cursor",
    "capture_win_title": "extractor_v2.py",
    "capture_ax_text": "修复 JSON 解析",
    "capture_ocr_text": "bake invalid_json",
    "capture_input_text": "",
    "capture_audio_text": "",
    "start_time": 1710000000000,
    "end_time": 1710000060000,
    "duration_minutes": 1,
    "key_timestamps": [
        {"capture_id": 10, "ts": 1710000000000},
        {"capture_id": 11, "ts": 1710000030000},
        {"capture_id": 12, "ts": 1710000060000},
    ],
    "action_trace": [
        {
            "capture_id": 10,
            "ts": 1710000000000,
            "event_type": "manual",
            "app_name": "Cursor",
            "win_title": "extractor_v2.py",
            "visible_text": "检查 bake bundle 分类逻辑",
            "input_text": "定位 primary_type 抑制",
        },
        {
            "capture_id": 11,
            "ts": 1710000030000,
            "event_type": "manual",
            "app_name": "Terminal",
            "win_title": "pytest",
            "visible_text": "运行 bundle 测试并发现 SOP 被拒绝",
        },
        {
            "capture_id": 12,
            "ts": 1710000060000,
            "event_type": "manual",
            "app_name": "Terminal",
            "win_title": "pytest",
            "visible_text": "修改后测试通过",
        },
    ],
    "entities": ["bake", "JSON", "sidecar"],
}


TEMPLATE_ONLY_CANDIDATE = {
    "source_timeline_id": 2,
    "source_capture_id": 20,
    "source_capture_count": 2,
    "summary": "整理周报撰写模板骨架",
    "overview": "抽象固定段落模板：背景、进展、风险、下周计划。",
    "details": "这次工作重点是沉淀一套可重复复用的周报结构与槽位，而不是总结某一周发生了什么。",
    "importance": 4,
    "occurrence_count": 1,
    "observed_at": 1710000000000,
    "event_time_start": None,
    "event_time_end": None,
    "history_view": False,
    "content_origin": "live_interaction",
    "activity_type": "writing",
    "evidence_strength": "high",
    "capture_ts": 1710000000000,
    "capture_app_name": "Cursor",
    "capture_win_title": "weekly_report_template.md",
    "capture_ax_text": "周报模板骨架 槽位 背景 进展 风险 下周计划",
    "capture_ocr_text": "模板 结构 段落 常用表达",
    "capture_input_text": "输出一个可复用的周报模板骨架，而不是总结本周内容。",
    "capture_audio_text": "",
    "entities": ["周报", "模板", "背景", "进展", "风险", "下周计划"],
}

ELIGIBLE_DOCUMENT_EVIDENCE = {
    "kind": "native_document",
    "source_surface": "document_editor",
    "has_document_url": False,
    "has_document_page_title": True,
    "has_substantive_document_body": True,
    "allows_auto_create": True,
}


class DummyClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)



def make_extractor() -> KnowledgeExtractorV2:
    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
    extractor.model = "mock-model"
    extractor._build_bake_candidate_text = types.MethodType(lambda self, candidate: "candidate-text", extractor)
    return extractor



def make_raw_extractor() -> KnowledgeExtractorV2:
    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
    extractor.model = "mock-model"
    return extractor


def _schema_contains_key(value, target: str) -> bool:
    if isinstance(value, dict):
        return target in value or any(
            _schema_contains_key(item, target) for item in value.values()
        )
    if isinstance(value, list):
        return any(_schema_contains_key(item, target) for item in value)
    return False


def test_ollama_compatible_format_removes_grammar_expanding_string_limits():
    compatible = _ollama_compatible_format(BAKE_BUNDLE_RESPONSE_SCHEMA)

    assert compatible is not BAKE_BUNDLE_RESPONSE_SCHEMA
    assert _schema_contains_key(BAKE_BUNDLE_RESPONSE_SCHEMA, "maxLength") is True
    assert _schema_contains_key(compatible, "maxLength") is False
    assert _schema_contains_key(compatible, "maxItems") is True
    assert compatible["required"] == ["classification", "knowledge", "design", "sop"]


def test_model_request_error_does_not_include_provider_response_in_message():
    error = BakeModelRequestError(
        400,
        '{"error":"failed to parse grammar","model":"provider-model"}',
    )

    assert error.code == "BAKE_MODEL_REQUEST_INVALID"
    assert error.retryable is False
    assert error.status_code == 400
    assert "provider-model" not in str(error)


def test_missing_model_is_classified_as_service_error():
    error = BakeModelRequestError(404, '{"error":"model not found"}')

    assert error.code == "MODEL_UNAVAILABLE"
    assert error.retryable is True
    assert error.scope == "service"
    assert error.http_status == 503



def test_extract_json_object_accepts_python_like_dict():
    raw = "```json\n{'accepted': True, 'reason': None, 'payload': {'summary': 'ok'}}\n```"

    parsed = _extract_json_object(raw)

    assert parsed == {
        "accepted": True,
        "reason": None,
        "payload": {"summary": "ok"},
    }



def test_extract_json_object_repairs_stray_ascii_quote_in_cjk_text():
    # 本地模型在中文语境里会把引用闭引号写成半角，导致 json.loads 误判字符串提前结束。
    raw = (
        '```json\n'
        '{\n'
        '  "knowledge": {\n'
        '    "accepted": true,\n'
        '    "reason": "指标“速度: 0 bytes/s”且“进度显示0/0"通常表示任务处于暂停阶段。",\n'
        '    "payload": {"summary": "ok"}\n'
        '  }\n'
        '}\n'
        '```'
    )

    parsed = _extract_json_object(raw)

    assert parsed is not None
    assert parsed["knowledge"]["accepted"] is True
    assert "进度显示0/0" in parsed["knowledge"]["reason"]


def test_extract_json_object_repair_keeps_valid_json_unchanged():
    raw = '{"a": "带\\"转义引号\\"和中文的值", "b": ["x", "y"]}'

    parsed = _extract_json_object(raw)

    assert parsed == {"a": '带"转义引号"和中文的值', "b": ["x", "y"]}



def test_extract_ollama_response_text_falls_back_to_response_field():
    response = {"response": {"text": '{"accepted": false, "reason": "rejected", "payload": null}'}}

    text = _extract_ollama_response_text(response)

    assert text == '{"accepted": false, "reason": "rejected", "payload": null}'



def test_extract_ollama_response_text_reads_object_message_content_before_thinking():
    response = ResponseLike(
        message=MessageLike(
            content='{"accepted": true, "reason": null, "payload": {"summary": "ok"}}',
            thinking='Thinking Process: should not be used',
        )
    )

    text = _extract_ollama_response_text(response)

    assert text == '{"accepted": true, "reason": null, "payload": {"summary": "ok"}}'



def test_call_bake_llm_uses_structured_json_schema():
    extractor = make_extractor()
    client = DummyClient(
        {
            "model": "mock-model",
            "message": {"content": '{"accepted": false, "reason": "rejected", "payload": null}'},
            "prompt_eval_count": 10,
            "eval_count": 8,
        }
    )
    extractor._ollama_chat = client.chat

    parsed, meta = extractor._call_bake_llm("knowledge:1", "system", "user")

    assert parsed == {"accepted": False, "reason": "rejected", "payload": None}
    assert client.calls[0]["format"] == BAKE_RESPONSE_SCHEMA
    assert client.calls[0]["options"] == {
        "temperature": 0.0,
        "num_ctx": BAKE_CONTEXT_WINDOW_TOKENS,
        "num_predict": BAKE_NUM_PREDICT,
        "repeat_penalty": 1.0,
    }
    assert meta["empty_content"] is False



def test_extract_bake_artifact_marks_empty_content_as_degraded():
    extractor = make_extractor()
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt: (
            None,
            {
                "usage": {"prompt_tokens": 10, "completion_tokens": 0},
                "model": "mock-model",
                "raw_content": "",
                "raw_preview": "",
                "response_preview": "{}",
                "empty_content": True,
            },
        ),
        extractor,
    )

    artifact, meta = extractor._extract_bake_artifact(SAMPLE_CANDIDATE, "knowledge", "prompt")

    assert artifact == {
        "accepted": False,
        "reason": "empty_content",
        "payload": None,
    }
    assert meta["degraded"] is True



def test_extract_bake_artifact_marks_missing_payload_as_degraded():
    extractor = make_extractor()
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt: (
            {"accepted": True, "reason": None, "payload": None},
            {
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
                "model": "mock-model",
                "raw_content": '{"accepted": true}',
                "raw_preview": '{"accepted": true}',
                "response_preview": '{"accepted": true}',
                "empty_content": False,
            },
        ),
        extractor,
    )

    artifact, meta = extractor._extract_bake_artifact(SAMPLE_CANDIDATE, "template", "prompt")

    assert artifact == {
        "accepted": False,
        "reason": "accepted_without_payload",
        "payload": None,
    }
    assert meta["degraded"] is True



def test_extract_bake_bundle_uses_one_llm_call_for_three_artifacts():
    extractor = make_extractor()
    response_payload = {
        "classification": {
            "primary_type": "document",
            "reason": "主体是一份可复用周报",
        },
        "knowledge": {
            "accepted": False,
            "reason": "not_a_knowledge",
            "payload": None,
        },
        "design": {
            "accepted": True,
            "reason": None,
            "payload": {"name": "周报模板"},
        },
        "sop": {
            "accepted": False,
            "reason": "not_a_sop",
            "payload": None,
        },
    }
    client = DummyClient(
        {
            "model": "mock-model",
            "message": {"content": json.dumps(response_payload, ensure_ascii=False)},
            "prompt_eval_count": 7,
            "eval_count": 8,
        }
    )
    extractor._ollama_chat = client.chat

    result = extractor.extract_bake_bundle({
        **SAMPLE_CANDIDATE,
        "document_evidence": ELIGIBLE_DOCUMENT_EVIDENCE,
    })

    assert len(client.calls) == 1
    assert client.calls[0]["format"] == BAKE_BUNDLE_RESPONSE_SCHEMA
    assert client.calls[0]["options"]["num_ctx"] == 32768
    assert client.calls[0]["options"]["num_predict"] == 8192
    assert result["knowledge"]["reason"] == "not_a_knowledge"
    assert result["design"]["payload"]["name"] == "周报模板"
    assert result["sop"]["reason"] == "not_a_sop"
    assert result["primary_type"] == "document"
    assert result["usage"] == {"prompt_tokens": 7, "completion_tokens": 8}
    assert result["degraded"] is False
    assert set(result["stage_elapsed_ms"]) == {"bundle"}
    assert isinstance(result["total_elapsed_ms"], int)
    assert result["total_elapsed_ms"] >= 0


def test_extract_bake_bundle_primary_type_does_not_suppress_independent_assets():
    extractor = make_extractor()
    response_payload = {
        "classification": {
            "primary_type": "data",
            "reason": "主体是会变化的指标卡和明细表",
        },
        "knowledge": {
            "accepted": True,
            "reason": None,
            "payload": {"summary": "指标异常由采集口径变更导致"},
        },
        "design": {"accepted": False, "reason": "not_a_document", "payload": None},
        "sop": {
            "accepted": True,
            "reason": None,
            "payload": {
                "summary": "修复采集口径并验证指标",
                "steps": ["修改采集配置", "重启采集任务", "检查指标恢复"],
            },
        },
    }
    client = DummyClient({
        "model": "mock-model",
        "message": {"content": json.dumps(response_payload, ensure_ascii=False)},
        "prompt_eval_count": 7,
        "eval_count": 8,
    })
    extractor._ollama_chat = client.chat

    result = extractor.extract_bake_bundle({
        **SAMPLE_CANDIDATE,
        "source_capture_count": 3,
    })

    assert result["primary_type"] == "data"
    assert result["knowledge"]["accepted"] is True
    assert result["sop"]["accepted"] is True
    assert result["sop"]["payload"]["step_evidence"] == [
        {"step_index": 1, "capture_ids": ["10", "11", "12"]},
        {"step_index": 2, "capture_ids": ["10", "11", "12"]},
        {"step_index": 3, "capture_ids": ["10", "11", "12"]},
    ]


def test_bake_prompts_classify_progress_results_and_conclusions_as_knowledge_facts():
    """进度事实必须稳定落在 knowledge，不能再被泛化的“状态快照”挤到 data。"""
    assert "事实不要求永远不变" in BAKE_KNOWLEDGE_PROMPT
    assert "项目进度、工作状态和执行结果" in BAKE_KNOWLEDGE_PROMPT
    assert "不能仅因为载体是聊天" in BAKE_KNOWLEDGE_PROMPT
    assert "不得把计划中的动作改写成已经完成" in BAKE_KNOWLEDGE_PROMPT
    assert "Agent Demo 尚未挂到 AIGC 页面" in BAKE_KNOWLEDGE_PROMPT
    assert "已确认的结论、决定、责任人" in BAKE_KNOWLEDGE_PROMPT

    assert "项目进度、工作状态变化、非数值执行结果" in BAKE_BUNDLE_PROMPT
    assert "语义状态和结果应优先归 knowledge" in BAKE_BUNDLE_PROMPT
    assert "以“对象 + 指标 + 数值”为核心" in BAKE_BUNDLE_PROMPT
    assert "不能因为它们会变化或来自聊天就归 data/none" in BAKE_BUNDLE_PROMPT
    assert "只有没有实质事实的自动动作壳才 reject" in BAKE_BUNDLE_PROMPT
    assert "它不构成其他资产的拒绝理由" in BAKE_BUNDLE_PROMPT
    assert "可以有多个 accepted=true" in BAKE_BUNDLE_PROMPT
    assert "禁止使用 not_primary_type" in BAKE_COMPACT_BUNDLE_PROMPT
    assert "不得仅因记录来自过去、他人、群聊或动态流而拒绝" in BAKE_SHARED_PROMPT

    # 失败后的紧凑重试复用同一分类契约，不能在重试时丢失事实边界。
    assert BAKE_BUNDLE_PROMPT in BAKE_COMPACT_BUNDLE_PROMPT


def test_extract_bake_bundle_preserves_work_progress_primary_knowledge():
    extractor = make_extractor()
    response_payload = {
        "classification": {
            "primary_type": "knowledge",
            "reason": "主体是有明确对象、当前状态和后续承诺的项目进度事实",
        },
        "knowledge": {
            "accepted": True,
            "reason": None,
            "payload": {
                "summary": "Agent Demo 尚未上线，导演 Agent 集成仍在测试",
                "details": (
                    "Agent Demo 尚未挂到 AIGC 页面，负责人计划两天内处理；"
                    "导演 Agent 集成存在单 Tool 性能问题，当前正在测试。"
                ),
                "match_score": 0.9,
                "match_level": "high",
                "review_status": "auto_created",
            },
        },
        "design": {"accepted": False, "reason": "not_a_document", "payload": None},
        "sop": {"accepted": False, "reason": "not_a_sop", "payload": None},
    }
    client = DummyClient({
        "model": "mock-model",
        "message": {"content": json.dumps(response_payload, ensure_ascii=False)},
        "prompt_eval_count": 9,
        "eval_count": 12,
    })
    extractor._ollama_chat = client.chat

    result = extractor.extract_bake_bundle({
        **SAMPLE_CANDIDATE,
        "source_timeline_id": 4168,
        "summary": "同步两个 Agent 项目的上线进度",
        "capture_app_name": "Kim",
        "capture_win_title": "Kim",
        "capture_ax_text": (
            "Agent Demo 尚未挂到 AIGC 页面，计划两天内处理。"
            "导演 Agent 集成存在单 Tool 性能问题，当前正在测试。"
        ),
    })

    assert result["primary_type"] == "knowledge"
    assert result["knowledge"]["accepted"] is True
    assert "尚未上线" in result["knowledge"]["payload"]["summary"]
    assert result["design"]["accepted"] is False
    assert result["sop"]["accepted"] is False


def test_extract_bake_bundle_rejects_chat_document_mentions_even_if_model_accepts_design():
    extractor = make_extractor()
    response_payload = {
        "knowledge": {
            "accepted": True,
            "reason": None,
            "payload": {"summary": "会议中要求查看剧本创作规范"},
        },
        "design": {
            "accepted": True,
            "reason": "聊天中出现了云文档标题",
            "payload": {
                "name": "[云文档] AIGC 剧本创作规范（推测）",
                "full_content": "模型从聊天内容中错误整理出的文档正文",
                "match_score": 0.95,
                "match_level": "high",
                "review_status": "auto_created",
            },
        },
        "sop": {
            "accepted": False,
            "reason": "not_a_sop",
            "payload": None,
        },
    }
    client = DummyClient(
        {
            "model": "mock-model",
            "message": {"content": json.dumps(response_payload, ensure_ascii=False)},
            "prompt_eval_count": 7,
            "eval_count": 8,
        }
    )
    extractor._ollama_chat = client.chat
    candidate = {
        **SAMPLE_CANDIDATE,
        "source_timeline_id": 1674,
        "timeline_category": "会议",
        "capture_app_name": "Kim",
        "capture_win_title": "Kim",
        "capture_url": None,
        "capture_webpage_title": None,
        "capture_ax_text": (
            "会议群聊天：你之前设计的那个剧本库文档看一下。"
            "[云文档] AIGC Agentic 架构方案；AIGC 剧本创作规范。"
        ) * 20,
        "document_evidence": {
            "kind": "insufficient",
            "source_surface": "chat",
            "has_document_url": False,
            "has_document_page_title": False,
            "has_substantive_document_body": True,
            "allows_auto_create": False,
        },
    }

    result = extractor.extract_bake_bundle(candidate)

    assert result["knowledge"]["accepted"] is True
    assert result["design"] == {
        "accepted": False,
        "reason": "insufficient_document_evidence",
        "payload": None,
    }
    assert result["sop"]["accepted"] is False


def test_document_evidence_fallback_accepts_real_browser_document():
    extractor = make_raw_extractor()
    evidence = extractor._resolve_document_evidence({
        **SAMPLE_CANDIDATE,
        "capture_app_name": "Google Chrome",
        "capture_win_title": "AIGC 剧本创作规范 - 云文档",
        "capture_webpage_title": "AIGC 剧本创作规范 - 云文档",
        "capture_url": "https://docs.example.com/d/home/document-id",
        "capture_ax_text": "文档正文" * 80,
    })

    assert evidence["kind"] == "document_url"
    assert evidence["source_surface"] == "browser"
    assert evidence["allows_auto_create"] is True


def test_document_evidence_fallback_rejects_chat_with_document_link_but_no_document_view():
    extractor = make_raw_extractor()
    evidence = extractor._resolve_document_evidence({
        **SAMPLE_CANDIDATE,
        "capture_app_name": "Kim",
        "capture_win_title": "Kim",
        "capture_webpage_title": None,
        "capture_url": "https://docs.example.com/d/home/document-id",
        "capture_ax_text": "聊天中分享了一份云文档，请大家看一下。" * 40,
    })

    assert evidence["source_surface"] == "chat"
    assert evidence["has_document_url"] is True
    assert evidence["has_substantive_document_body"] is True
    assert evidence["kind"] == "insufficient"
    assert evidence["allows_auto_create"] is False


def test_bake_bundle_prompt_estimate_includes_schema_and_candidate():
    extractor = make_raw_extractor()

    small = extractor.estimate_bake_bundle_prompt_tokens(SAMPLE_CANDIDATE)
    large = extractor.estimate_bake_bundle_prompt_tokens({
        **SAMPLE_CANDIDATE,
        "capture_ax_text": "长文档内容" * 20_000,
    })

    assert small > 0
    assert large > small


def test_bake_bundle_candidate_is_fitted_to_shared_input_budget():
    extractor = make_raw_extractor()
    candidate = {
        **SAMPLE_CANDIDATE,
        "capture_ax_text": "主采集正文" * 4_000,
        "url_aggregated_text": "累计文档正文" * 8_000,
        "url_aggregated_capture_count": 8,
    }

    prepared = extractor._prepare_bake_bundle_candidate(candidate)

    assert extractor._bundle_prompt_token_estimate(prepared) <= BAKE_INPUT_TOKEN_BUDGET
    assert len(prepared["url_aggregated_text"]) < len(candidate["url_aggregated_text"])
    assert len(prepared["capture_ax_text"]) < len(candidate["capture_ax_text"])
    assert candidate["url_aggregated_text"] == "累计文档正文" * 8_000


def test_head_tail_context_honors_tiny_budget():
    extractor = make_raw_extractor()
    assert extractor._head_tail_context("abcdef", 1) == "a"
    assert extractor._head_tail_context("abcdef", 0) == ""


def test_bake_bundle_uses_compact_output_on_bounded_retry():
    extractor = make_raw_extractor()
    response_payload = {
        "knowledge": {"accepted": False, "reason": "not_a_knowledge", "payload": None},
        "design": {"accepted": False, "reason": "not_a_document", "payload": None},
        "sop": {"accepted": False, "reason": "not_a_sop", "payload": None},
    }
    client = DummyClient({
        "model": "mock-model",
        "message": {"content": json.dumps(response_payload, ensure_ascii=False)},
        "prompt_eval_count": 18_000,
        "eval_count": 100,
        "done_reason": "stop",
    })
    extractor._ollama_chat = client.chat
    candidate = {
        **SAMPLE_CANDIDATE,
        "capture_ax_text": "主采集正文" * 4_000,
        "url_aggregated_text": "累计文档正文" * 8_000,
        "url_aggregated_capture_count": 8,
    }

    result = extractor.extract_bake_bundle(candidate, retry_attempt=1)

    assert len(client.calls) == 1
    assert client.calls[0]["format"] == BAKE_COMPACT_BUNDLE_RESPONSE_SCHEMA
    assert client.calls[0]["options"]["num_predict"] == BAKE_RETRY_NUM_PREDICT
    assert client.calls[0]["options"]["repeat_penalty"] == BAKE_RETRY_REPEAT_PENALTY
    assert result["degraded"] is False


def test_bake_bundle_timeout_retry_uses_smaller_input_and_output_budget():
    extractor = make_raw_extractor()
    response_payload = {
        "classification": {"primary_type": "none", "reason": "证据不足"},
        "knowledge": {"accepted": False, "reason": "not_primary_type", "payload": None},
        "design": {"accepted": False, "reason": "not_primary_type", "payload": None},
        "sop": {"accepted": False, "reason": "not_primary_type", "payload": None},
    }
    client = DummyClient({
        "model": "mock-model",
        "message": {"content": json.dumps(response_payload, ensure_ascii=False)},
        "prompt_eval_count": 10_000,
        "eval_count": 80,
        "done_reason": "stop",
    })
    extractor._ollama_chat = client.chat
    candidate = {
        **SAMPLE_CANDIDATE,
        "capture_ax_text": "主采集正文" * 4_000,
        "url_aggregated_text": "累计文档正文" * 8_000,
    }

    extractor.extract_bake_bundle(
        candidate,
        retry_attempt=1,
        retry_error_code="INFERENCE_TIMEOUT",
    )

    assert client.calls[0]["format"] == BAKE_TIMEOUT_BUNDLE_RESPONSE_SCHEMA
    assert client.calls[0]["options"]["num_predict"] == BAKE_TIMEOUT_RETRY_NUM_PREDICT
    assert (
        client.calls[0]["options"]["repeat_penalty"]
        == BAKE_TIMEOUT_RETRY_REPEAT_PENALTY
    )


def test_bake_bundle_initial_preemption_stays_retryable():
    extractor = make_extractor()

    with pytest.raises(QueueEvictedError, match="在线咨询或创作任务"):
        extractor.extract_bake_bundle(
            SAMPLE_CANDIDATE,
            preempt_check=lambda: True,
        )


def test_extract_bake_bundle_surfaces_invalid_output_as_failure():
    extractor = make_extractor()
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt, response_schema, **_kwargs: (
            None,
            {
                "empty_content": False,
                "done_reason": "length",
            },
        ),
        extractor,
    )

    with pytest.raises(BakeOutputTruncatedError, match="truncated_json"):
        extractor.extract_bake_bundle(SAMPLE_CANDIDATE)


def test_extract_bake_bundle_does_not_turn_llm_exception_into_success():
    extractor = make_extractor()

    def fail(*_args, **_kwargs):
        raise TimeoutError("cancelled")

    extractor._call_bake_llm = fail

    with pytest.raises(TimeoutError, match="cancelled"):
        extractor.extract_bake_bundle(SAMPLE_CANDIDATE)



def test_extract_bake_knowledge_rejects_template_like_candidate_after_llm_accepts():
    extractor = make_extractor()
    payload = {
        "summary": "周报撰写四段式模板骨架",
        "overview": "沉淀背景、进展、风险、下周计划四段式结构。",
        "entities": ["周报", "模板"],
        "importance": 4,
        "occurrence_count": 1,
        "observed_at": 1710000000000,
        "event_time_start": None,
        "event_time_end": None,
        "history_view": False,
        "content_origin": "live_interaction",
        "activity_type": "writing",
        "evidence_strength": "high",
        "evidence_summary": "来源强调模板骨架与槽位复用。",
        "match_score": 0.95,
        "match_level": "high",
        "review_status": "auto_created",
    }
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt: (
            {"accepted": True, "reason": None, "payload": payload},
            {
                "usage": {"prompt_tokens": 18, "completion_tokens": 24},
                "model": "mock-model",
                "raw_content": '{"accepted": true}',
                "raw_preview": '{"accepted": true}',
                "response_preview": '{"accepted": true}',
                "empty_content": False,
                "elapsed_ms": 31,
            },
        ),
        extractor,
    )

    artifact, meta = extractor._extract_bake_artifact(TEMPLATE_ONLY_CANDIDATE, "knowledge", "prompt")

    assert artifact == {
        "accepted": False,
        "reason": "template_like_content",
        "payload": None,
    }
    assert meta["degraded"] is False
    assert meta["elapsed_ms"] >= 0



def test_extract_bake_knowledge_rejects_sop_like_candidate_after_llm_accepts():
    extractor = make_extractor()
    sop_candidate = {
        **SAMPLE_CANDIDATE,
        "source_timeline_id": 3,
        "source_capture_id": 30,
        "summary": "启动失败排查步骤",
        "overview": "按步骤排查本地服务启动失败。",
        "details": "先检查 /health，再检查端口监听与日志输出。",
        "activity_type": "coding",
        "entities": ["排查", "步骤", "health"],
    }
    payload = {
        "summary": "启动失败排查步骤",
        "overview": "按步骤检查 health、端口与日志。",
        "entities": ["health", "port"],
        "importance": 4,
        "occurrence_count": 1,
        "observed_at": 1710000000000,
        "event_time_start": None,
        "event_time_end": None,
        "history_view": False,
        "content_origin": "live_interaction",
        "activity_type": "coding",
        "evidence_strength": "high",
        "evidence_summary": "来自一次启动失败排查记录。",
        "match_score": 0.9,
        "match_level": "high",
        "review_status": "auto_created",
    }
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt: (
            {"accepted": True, "reason": None, "payload": payload},
            {
                "usage": {"prompt_tokens": 18, "completion_tokens": 24},
                "model": "mock-model",
                "raw_content": '{"accepted": true}',
                "raw_preview": '{"accepted": true}',
                "response_preview": '{"accepted": true}',
                "empty_content": False,
                "elapsed_ms": 31,
            },
        ),
        extractor,
    )

    artifact, meta = extractor._extract_bake_artifact(sop_candidate, "knowledge", "prompt")

    assert artifact == {
        "accepted": False,
        "reason": "sop_like_content",
        "payload": None,
    }
    assert meta["degraded"] is False
    assert meta["elapsed_ms"] >= 0


def test_extract_bake_artifact_accepts_valid_payload():
    extractor = make_extractor()
    payload = {
        "summary": "保留 bake JSON hardening",
        "overview": "确保 sidecar 返回可解析结果。",
        "entities": ["bake", "JSON"],
        "importance": 4,
        "occurrence_count": 1,
        "observed_at": 1710000000000,
        "event_time_start": None,
        "event_time_end": None,
        "history_view": False,
        "content_origin": "live_interaction",
        "activity_type": "coding",
        "evidence_strength": "high",
        "evidence_summary": "多次排查 sidecar 空响应。",
        "match_score": 0.93,
        "match_level": "high",
        "review_status": "auto_created",
    }
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt: (
            {"accepted": True, "reason": None, "payload": payload},
            {
                "usage": {"prompt_tokens": 20, "completion_tokens": 40},
                "model": "mock-model",
                "raw_content": '{"accepted": true}',
                "raw_preview": '{"accepted": true}',
                "response_preview": '{"accepted": true}',
                "empty_content": False,
                "elapsed_ms": 44,
            },
        ),
        extractor,
    )

    artifact, meta = extractor._extract_bake_artifact(SAMPLE_CANDIDATE, "knowledge", "prompt")

    assert artifact == {
        "accepted": True,
        "reason": None,
        "payload": payload,
    }
    assert meta["degraded"] is False
    assert meta["model"] == "mock-model"
    assert meta["elapsed_ms"] >= 0



def test_extract_bake_sop_accepts_valid_payload():
    extractor = make_extractor()
    payload = {
        "title": "启动失败排查 SOP",
        "preconditions": ["具备服务日志访问权限"],
        "steps": [
            {"index": 1, "action": "访问 /health", "expected": "返回 200"},
            {"index": 2, "action": "检查端口监听", "expected": "端口处于 LISTEN"},
            {"index": 3, "action": "查看错误日志", "expected": "定位异常堆栈"},
        ],
        "checkpoints": ["health ok", "port ok"],
        "outcome": "定位问题原因并给出修复建议",
    }
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt: (
            {"accepted": True, "reason": None, "payload": payload},
            {
                "usage": {"prompt_tokens": 16, "completion_tokens": 28},
                "model": "mock-model",
                "raw_content": '{"accepted": true}',
                "raw_preview": '{"accepted": true}',
                "response_preview": '{"accepted": true}',
                "empty_content": False,
                "elapsed_ms": 33,
            },
        ),
        extractor,
    )

    artifact, meta = extractor._extract_bake_artifact(SAMPLE_CANDIDATE, "sop", "prompt")

    assert artifact["accepted"] is True
    assert artifact["reason"] is None
    assert artifact["payload"]["steps"] == payload["steps"]
    assert artifact["payload"]["step_evidence"] == [
        {"step_index": 1, "capture_ids": ["10", "11", "12"]},
        {"step_index": 2, "capture_ids": ["10", "11", "12"]},
        {"step_index": 3, "capture_ids": ["10", "11", "12"]},
    ]
    assert meta["degraded"] is False
    assert meta["model"] == "mock-model"
    assert meta["elapsed_ms"] >= 0


def test_extract_bake_sop_rejects_single_capture_even_when_model_accepts():
    """单帧是硬约束：模型即使产出完整步骤，也不能形成操作。"""
    extractor = make_extractor()
    payload = {
        "summary": "单帧推测出的配置流程",
        "overview": "从设置页按钮推测配置步骤。",
        "details": "## 行动路线\n1. 打开设置\n2. 修改选项\n3. 保存",
        "source_title": "设置页",
        "trigger_keywords": ["设置"],
        "extracted_problem": "如何配置选项",
        "steps": ["打开设置", "修改选项", "保存"],
        "linked_knowledge_ids": [],
        "confidence": "high",
        "evidence_summary": "单个设置页面。",
        "match_score": 0.95,
        "match_level": "high",
        "review_status": "auto_created",
    }
    calls = []

    def fake_call(self, caller_id, system_prompt, user_prompt):
        calls.append(caller_id)
        return (
            {"accepted": True, "reason": None, "payload": payload},
            {
                "usage": {"prompt_tokens": 16, "completion_tokens": 28},
                "model": "mock-model",
                "raw_content": '{"accepted": true}',
                "raw_preview": '{"accepted": true}',
                "response_preview": '{"accepted": true}',
                "empty_content": False,
                "elapsed_ms": 20,
            },
        )

    extractor._call_bake_llm = types.MethodType(fake_call, extractor)
    artifact, meta = extractor._extract_bake_artifact(
        {**SAMPLE_CANDIDATE, "source_capture_count": 1},
        "sop",
        "prompt",
    )

    assert len(calls) == 1
    assert artifact == {
        "accepted": False,
        "reason": "insufficient_multi_capture_evidence",
        "payload": None,
    }
    assert meta["degraded"] is False


def test_bake_candidate_exposes_multi_capture_context_to_existing_bundle_call():
    """多帧动作沿用现有 bundle 输入，不引入第二次推理。"""
    extractor = make_raw_extractor()
    text = extractor._build_bake_candidate_text({
        **SAMPLE_CANDIDATE,
        "source_capture_count": 5,
        "url_aggregated_capture_count": 4,
        "url_aggregated_text": (
            "--- capture#10 ---\n打开配置页\n\n"
            "--- capture#11 ---\n修改推理参数\n\n"
            "--- capture#12 ---\n运行并验证结果"
        ),
    })

    assert "source_capture_count: 5" in text
    assert "effective_capture_count: 3" in text
    assert "multi_capture_context" in text
    assert "action_trace（严格按 ts 排序" in text
    assert text.index("capture_id=10") < text.index("capture_id=11") < text.index("capture_id=12")
    assert 'key_timestamps:' in text
    assert "打开配置页" in text
    assert "运行并验证结果" in text


def test_build_bake_candidate_text_strips_score_metadata_from_details():
    extractor = make_raw_extractor()
    candidate = {
        **SAMPLE_CANDIDATE,
        "details": {
            "summary": "保留语义内容",
            "match_score": 0.95,
            "match_level": "high",
            "review_status": "auto_created",
            "inner": {
                "confidence": "high",
                "facts": "需要保留",
            },
        },
    }

    text = extractor._build_bake_candidate_text(candidate)

    assert "match_score" not in text
    assert "match_level" not in text
    assert "review_status" not in text
    assert "保留语义内容" in text
    assert "需要保留" in text


def test_build_bake_candidate_text_keeps_long_document_capture_beyond_3000_chars():
    extractor = make_raw_extractor()
    long_tail = "长文档尾部关键信息"
    candidate = {
        **SAMPLE_CANDIDATE,
        "capture_ax_text": "正文" * 3500 + long_tail,
        "capture_ocr_text": "",
    }

    text = extractor._build_bake_candidate_text(candidate)

    assert long_tail in text


def test_merge_document_appends_patch_without_rewriting_existing_long_content():
    extractor = make_raw_extractor()
    existing_content = "# 原文\n" + "已有正文。" * 1800
    captured_call = {}

    def call_bake_llm(self, caller_id, system_prompt, user_prompt, response_schema):
        captured_call.update({
            "caller_id": caller_id,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_schema": response_schema,
        })
        return {
            "no_change": False,
            "title": "长文档",
            "summary": "补充了新章节",
            "content_patch": "## 新章节\n这是新增内容。",
            "insert_mode": "append",
            "target_section_index": None,
        }, {}

    extractor._call_bake_llm = types.MethodType(call_bake_llm, extractor)

    result = extractor._merge_with_llm_once(
        {
            "title": "长文档",
            "summary": "旧摘要",
            "full_content": existing_content,
            "sections_json": "[]",
        },
        SAMPLE_CANDIDATE,
        "新 capture 的完整证据",
    )

    assert result["no_change"] is False
    assert result["full_content"].startswith(existing_content)
    assert result["full_content"].endswith("## 新章节\n这是新增内容。")
    assert len(result["full_content"]) > len(existing_content)
    assert "content_patch" in captured_call["response_schema"]["properties"]
    assert "前3000字" not in captured_call["user_prompt"]
    assert "旧正文不在模型侧重写" in captured_call["system_prompt"]


def test_merge_document_rejects_legacy_full_content_that_drops_existing_tail():
    extractor = make_raw_extractor()
    existing_content = "A" * 3000 + "不可丢失的旧正文尾部"
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt, response_schema: (
            {
                "no_change": False,
                "title": "长文档",
                "full_content": existing_content[:3000],
            },
            {},
        ),
        extractor,
    )

    result = extractor._merge_with_llm_once(
        {
            "title": "长文档",
            "full_content": existing_content,
            "sections_json": "[]",
        },
        SAMPLE_CANDIDATE,
        "新 capture",
    )

    assert result == {"title": "长文档", "no_change": True}


def test_merge_document_no_change_keeps_existing_title_when_model_omits_it():
    extractor = make_raw_extractor()
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt, response_schema: (
            {"no_change": True},
            {},
        ),
        extractor,
    )

    result = extractor._merge_with_llm_once(
        {
            "title": "已有文档标题",
            "full_content": "已有正文",
            "sections_json": "[]",
        },
        SAMPLE_CANDIDATE,
        "重复的新 capture",
    )

    assert result == {"no_change": True, "title": "已有文档标题"}


def test_merge_document_no_change_cannot_drop_wenz_product_alias():
    extractor = make_raw_extractor()
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt, response_schema: (
            {"no_change": True, "title": "[更新日志] Wenz - 广告消耗异动归因系统"},
            {},
        ),
        extractor,
    )
    candidate = {
        **SAMPLE_CANDIDATE,
        "source_timeline_id": 1884,
        "summary": "记录广告消耗异动归因系统 Wenz 的更新日志",
        "details": "文档详细梳理了稳柱系统在 V2.0、V2.1 和 V2.2 的迭代过程。",
        "capture_ax_text": "📍稳柱是一款面向商业体系的核心业务指标异动归因系统。",
        "capture_ocr_text": "",
        "capture_input_text": "",
        "capture_audio_text": "",
        "entities": ["Wenz", "稳柱系统"],
    }
    existing_content = "# Wenz 更新日志\n\n记录 V2.0 至 V2.2 的版本功能演进。"

    result = extractor.merge_bake_document(
        {
            "title": "[更新日志] Wenz - 广告消耗异动归因系统",
            "summary": "Wenz 版本演进记录",
            "full_content": existing_content,
            "sections_json": "[]",
            "tags": '["Wenz", "更新日志"]',
        },
        candidate,
    )

    assert result["no_change"] is False
    assert result["full_content"].startswith(existing_content)
    assert "## 产品、项目与别名" in result["full_content"]
    assert "- 稳柱" in result["full_content"]
    assert "稳柱" in result["evidence_summary"]


def test_document_identity_extraction_covers_product_project_and_alias_fields():
    identities = KnowledgeExtractorV2._extract_document_identities(
        {
            "capture_ax_text": "产品名：Atlas；项目名称：北斗；该产品又称星图。",
            "product_names": ["Atlas"],
            "project_names": ["北斗专项"],
            "aliases": ["星图"],
            "entities": [],
        }
    )

    assert set(identities) == {"Atlas", "北斗", "星图"}


def test_merge_document_accepts_no_change_when_alias_field_covers_source_name():
    extractor = make_raw_extractor()
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt, response_schema: (
            {"no_change": True},
            {},
        ),
        extractor,
    )
    candidate = {
        **SAMPLE_CANDIDATE,
        "capture_ax_text": "📍稳柱是一款面向商业体系的核心业务指标异动归因系统。",
        "capture_ocr_text": "",
        "capture_input_text": "",
        "capture_audio_text": "",
        "entities": ["Wenz", "稳柱系统"],
    }

    result = extractor.merge_bake_document(
        {
            "title": "[更新日志] Wenz - 广告消耗异动归因系统",
            "full_content": "Wenz 更新日志正文",
            "sections_json": "[]",
            "entity_aliases": ["稳柱"],
        },
        candidate,
    )

    assert result == {
        "no_change": True,
        "title": "[更新日志] Wenz - 广告消耗异动归因系统",
    }


def test_merge_document_keeps_existing_title_when_model_returns_incremental_name():
    extractor = make_raw_extractor()
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt, response_schema: (
            {
                "no_change": False,
                "title": "文档增量：已有文档标题",
                "content_patch": "## 新章节\n新增事实",
            },
            {},
        ),
        extractor,
    )

    result = extractor._merge_with_llm_once(
        {
            "title": "已有文档标题",
            "full_content": "已有正文",
            "sections_json": "[]",
        },
        SAMPLE_CANDIDATE,
        "新 capture",
    )

    assert result["title"] == "已有文档标题"
    assert result["full_content"] == "已有正文\n\n## 新章节\n新增事实"


def test_merge_document_l1_compares_visible_source_text_not_prompt_hash():
    extractor = make_raw_extractor()
    existing_content = "同一份文档正文，包含稳定的背景、方案和落地计划。"
    candidate = {
        **SAMPLE_CANDIDATE,
        "capture_ax_text": f"  {existing_content}\n",
        "capture_ocr_text": "",
        "capture_input_text": "",
        "capture_audio_text": "",
    }
    extractor._merge_with_llm_once = types.MethodType(
        lambda *_args, **_kwargs: pytest.fail("正文完全相同时不应调用 LLM"),
        extractor,
    )

    result = extractor.merge_bake_document(
        {
            "title": "已有文档标题",
            "full_content": existing_content,
            "content_hash": "生成正文的旧 hash",
        },
        candidate,
    )

    assert result == {"no_change": True, "title": "已有文档标题"}


def test_merge_document_high_embedding_similarity_does_not_drop_small_change():
    class VectorResult:
        def __init__(self):
            self.vector = [1.0, 0.5, 0.25]

    class FakeEmbeddingModel:
        def encode(self, texts):
            return [VectorResult() for _ in texts]

    extractor = make_raw_extractor()
    extractor.embedding_model = FakeEmbeddingModel()
    extractor._merge_with_llm_once = types.MethodType(
        lambda *_args, **_kwargs: {
            "no_change": False,
            "title": "已有文档标题",
            "full_content": "保留小幅修订",
        },
        extractor,
    )
    candidate = {
        **SAMPLE_CANDIDATE,
        "capture_ax_text": "候选正文。" * 80,
        "capture_ocr_text": "",
        "capture_input_text": "",
        "capture_audio_text": "",
    }

    result = extractor.merge_bake_document(
        {
            "title": "已有文档标题",
            "full_content": "已有正文。" * 80,
        },
        candidate,
    )

    assert result == {
        "no_change": False,
        "title": "已有文档标题",
        "full_content": "保留小幅修订",
    }


def test_merge_document_accepts_null_section_notes():
    extractor = make_raw_extractor()
    captured_call = {}

    def call_bake_llm(self, caller_id, system_prompt, user_prompt, response_schema):
        captured_call["user_prompt"] = user_prompt
        return {"no_change": True}, {}

    extractor._call_bake_llm = types.MethodType(call_bake_llm, extractor)

    result = extractor._merge_with_llm_once(
        {
            "title": "已有文档标题",
            "full_content": "已有正文",
            "sections_json": '[{"title":"背景","notes":null}]',
        },
        SAMPLE_CANDIDATE,
        "重复的新 capture",
    )

    assert result == {"no_change": True, "title": "已有文档标题"}
    assert "1. 背景:" in captured_call["user_prompt"]



def test_extract_bake_design_downgrades_sop_like_high_score_payload():
    extractor = make_extractor()
    sop_like_candidate = {
        **SAMPLE_CANDIDATE,
        "summary": "启动故障排查步骤",
        "overview": "按步骤执行排查流程",
        "details": "触发条件: 启动失败；前置条件: 有日志；步骤: 检查 health、检查端口、验证结果",
        "entities": ["步骤", "排查", "触发条件"],
        "document_evidence": ELIGIBLE_DOCUMENT_EVIDENCE,
    }
    payload = {
        "name": "启动故障排查记录",
        "category": "analysis",
        "status": "active",
        "tags": ["排查"],
        "applicable_tasks": ["creation"],
        "linked_knowledge_ids": [],
        "structure_sections": [{"title": "步骤", "keywords": ["检查"], "notes": "逐条执行"}],
        "style_phrases": ["先检查再验证"],
        "replacement_rules": [],
        "prompt_hint": "按步骤排查",
        "diagram_code": None,
        "image_assets": [],
        "evidence_summary": "原始候选强调流程步骤",
        "match_score": 0.96,
        "match_level": "high",
        "review_status": "auto_created",
    }
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt: (
            {"accepted": True, "reason": None, "payload": payload},
            {
                "usage": {"prompt_tokens": 20, "completion_tokens": 22},
                "model": "mock-model",
                "raw_content": '{"accepted": true}',
                "raw_preview": '{"accepted": true}',
                "response_preview": '{"accepted": true}',
                "empty_content": False,
                "elapsed_ms": 18,
            },
        ),
        extractor,
    )

    artifact, _ = extractor._extract_bake_artifact(sop_like_candidate, "design", "prompt")

    assert artifact["accepted"] is True
    assert artifact["payload"]["match_level"] == "low"
    assert artifact["payload"]["review_status"] == "auto_created"
    assert artifact["payload"]["match_score"] <= 0.49



def test_extract_bake_sop_downgrades_template_like_high_score_payload():
    extractor = make_extractor()
    template_like_candidate = {
        **TEMPLATE_ONLY_CANDIDATE,
        "summary": "周报模板结构沉淀",
        "details": "模板骨架包含背景、进展、风险、计划四段；按槽位填写。",
        "entities": ["模板", "骨架", "槽位"],
    }
    payload = {
        "summary": "周报输出 SOP",
        "overview": "执行周报产出流程",
        "source_title": "周报模板实践",
        "trigger_keywords": ["周报", "产出"],
        "extracted_problem": "如何稳定产出周报",
        "steps": ["收集素材", "填充结构", "输出结果"],
        "linked_knowledge_ids": [],
        "confidence": "high",
        "evidence_summary": "候选中出现模板骨架",
        "match_score": 0.94,
        "match_level": "high",
        "review_status": "auto_created",
    }
    extractor._call_bake_llm = types.MethodType(
        lambda self, caller_id, system_prompt, user_prompt: (
            {"accepted": True, "reason": None, "payload": payload},
            {
                "usage": {"prompt_tokens": 20, "completion_tokens": 22},
                "model": "mock-model",
                "raw_content": '{"accepted": true}',
                "raw_preview": '{"accepted": true}',
                "response_preview": '{"accepted": true}',
                "empty_content": False,
                "elapsed_ms": 18,
            },
        ),
        extractor,
    )

    artifact, _ = extractor._extract_bake_artifact(template_like_candidate, "sop", "prompt")

    assert artifact["accepted"] is True
    assert artifact["payload"]["match_level"] == "low"
    assert artifact["payload"]["review_status"] == "auto_created"
    assert artifact["payload"]["match_score"] <= 0.49
