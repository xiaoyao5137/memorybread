"""New delivery gates use real validators and the production executor."""
import copy
import json

import pytest
from creation.agent_loop import CreationAgentLoop
from creation.delivery_contract import (CONTRACT_PROMPT, SOURCE_CAPABILITIES, assess_inputs,
    _is_supported_retrieval_provenance_clause, bind_declared_workflow_inputs, bind_resources,
    delivery_incomplete_message, review_delivery,
    validate_contract, validate_review, with_brainstorm_transform_delta_coverage)
from creation.operations import OperationError, document_nodes
from creation.service import CreationOptions, CreationService
from tests.test_creation_agent_loop import FakeCreationService
from tests.test_creation_operations import run_args


def contract(sources=()):
    return {"deliverable": "本轮文档", "inputs": [
        {"id": source, "need": "必要输入", "source": source, "state": "missing",
         "evidence": "", "query": source + " specific gap", "reason": "完成本轮目标必需"} for source in sources],
        "self_contained_reason": "仅处理给定内容", "acceptance": [{"id": "a", "criterion": "覆盖本轮需求"}]}


def review(status="pass", document="正文"):
    return {"status": status, "checks": [{"id": "a", "passed": status == "pass",
        "reason": "逐项检查", "evidence": document if status == "pass" else ""}],
        "corrections": ["补全本轮缺少的内容"] if status == "revise" else []}


def review_for_model(status="pass", document="正文"):
    """Model fixtures explicitly include the mandatory independent source check."""
    result = review(status, document)
    result["corrections"] = []
    result["source_audit"] = "__test_audit_all_segments__"
    for check_id in ("source_scope_grounding", "source_attributes_grounding", "source_obligations_grounding"):
        result["checks"].append({"id": check_id, "passed": status == "pass",
                                 "reason": "该项来源边界已独立核对", "evidence": document if status == "pass" else ""})
    return result


def test_blocked_delivery_reports_missing_inputs_when_raw_reason_is_not_display_safe():
    conditions = contract(("business_data", "public_facts"))
    conditions["inputs"][0]["need"] = "读取 https://example.com/gpu 并核对 2026-09-25 数据"
    conditions["inputs"][1]["need"] = "读取 TOKEN_USAGE_DAILY 长英文指标"
    report = {
        "status": "blocked",
        "checks": [{"id": "a", "passed": False, "reason": "TOKEN_USAGE_DAILY 未核对", "evidence": ""}],
        "corrections": [],
    }

    message = delivery_incomplete_message(report, contract=conditions)

    assert "缺少2项验收所需资料，请补充对应数据或来源" in message
    assert "正文仍有待核对内容" not in message


def test_blocked_delivery_prioritizes_missing_inputs_over_short_source_audit_noise():
    conditions = contract(("work_context", "business_data"))
    conditions["inputs"][0]["need"] = "本周会议纪要"
    conditions["inputs"][1]["need"] = "业务指标数据"
    report = {
        "status": "blocked",
        "checks": [{"id": "a", "passed": False,
                    "reason": "逐片段来源审计未找到支持或创作授权：本文档基于检索资料生成",
                    "evidence": "本文档基于检索资料生成"}],
        "corrections": ["逐片段来源审计未找到支持或创作授权：本文档基于检索资料生成"],
    }

    message = delivery_incomplete_message(report, contract=conditions)

    assert "缺少资料：本周会议纪要、业务指标数据" in message
    assert "逐片段来源审计" not in message


def test_blocked_delivery_lists_each_named_workflow_input_for_latest_weekly_report_case():
    conditions = contract(("work_context", "business_data"))
    conditions["inputs"][0]["need"] = "本周大模型性能成本优化周会会议纪要；AIGC进度总结"
    conditions["inputs"][1]["need"] = "GPU算力数据；Token数据"
    report = {"status": "blocked", "checks": [], "corrections": []}

    message = delivery_incomplete_message(report, attempts=1, contract=conditions)

    assert message.startswith("自动修正 1 次后仍未通过验收：缺少资料：")
    assert "本周大模型性能成本优化周会会议纪要" in message
    assert "AIGC进度总结" in message
    assert "GPU算力数据" in message
    assert "Token数据" in message


def test_retrieval_provenance_clause_requires_actual_matching_evidence():
    clause = "*本文档基于检索到的历史项目周报与会议纪要生成，"
    provided = {"retrieved_evidence": {
        "references": [{"title": "项目周报与会议纪要", "content": "历史材料"}],
        "data_results": [], "web_results": [],
    }}

    assert _is_supported_retrieval_provenance_clause(clause, provided)
    assert not _is_supported_retrieval_provenance_clause(
        "本文档基于检索到的实时运营看板生成，", provided)
    assert not _is_supported_retrieval_provenance_clause(
        "本文档基于检索到的历史项目周报与会议纪要生成，未包含2026年9月25日数据。",
        provided)


def test_transform_submission_binds_only_newly_confirmed_brainstorm_choice():
    conditions = contract()
    decisions = [
        {"id": "old-choice", "dimension": "执行模式", "value": "矩阵模式", "source": "user"},
        {"id": "new-choice", "dimension": "运营介入", "value": "全托管模式", "source": "user"},
        {"id": "open", "dimension": "账号关系", "value": "签名引流", "source": "agent_assumption"},
    ]
    result = with_brainstorm_transform_delta_coverage(
        conditions,
        decisions,
        "## 已确认\n\n- 矩阵模式\n",
        "请基于刚刚新增并提交的脑暴选择更新文档：落实全托管模式",
    )
    checks = {item["id"]: item for item in result["acceptance"]}
    assert "new-choice" in checks
    assert "全托管模式" in checks["new-choice"]["criterion"]
    assert "old-choice" not in checks
    assert "open" not in checks


def test_unrelated_transform_does_not_acquire_missing_brainstorm_choices():
    conditions = contract()
    result = with_brainstorm_transform_delta_coverage(
        conditions,
        [{"id": "choice", "dimension": "运营介入", "value": "全托管模式", "source": "user"}],
        "# 方案\n",
        "修正标题中的错别字",
    )
    assert [item["id"] for item in result["acceptance"]] == ["a"]


def audited_report(document, basis="unsupported", kind="obligation", source_id=""):
    from creation.delivery_contract import source_audit_segments
    result = review_for_model(document=document)
    result["source_audit"] = {key: {"text": text.strip(), "meaning": "候选表达：" + text.strip()[:180], "kind": kind,
        "source_id": source_id, "basis": basis}
        for key, text in source_audit_segments(document).items()}
    return result


@pytest.mark.parametrize("document", [
    "  # 标题\n\n时间：10:30，地点。Next sentence, please.\n| 列一 | 列二 |\n| 3.14 | 20 |\n最后无标点  ",
    "\n" + "\n".join("第{}段，材料{}；结尾{}。".format(i, i, i) for i in range(200)) + "\n尾部哨兵",
])
def test_source_audit_partitions_every_character_and_bounds_by_merging(document):
    from creation.delivery_contract import source_audit_segments
    segments = source_audit_segments(document)
    assert "".join(segments.values()) == document
    assert len(segments) <= 32
    position = 0
    for key, text in segments.items():
        _, start, end = key.split("-")
        assert int(start) == position and document[int(start):int(end)] == text
        position = int(end)
    assert position == len(document)


def test_source_audit_uses_actual_changes_and_recomputes_after_repair():
    from creation.delivery_contract import source_audit_segments
    original = "旧称谓来自原文。\n旧安排保持。"
    changed = original + "\n缺席需要登记。"
    assert source_audit_segments(original, original) == {}
    assert "缺席需要登记" in "".join(source_audit_segments(changed, original).values())
    assert "旧称谓" not in "".join(source_audit_segments(changed, original).values())
    assert source_audit_segments(original, original) == {}


def test_source_audit_keeps_prospective_marker_with_every_proposal_clause():
    from creation.delivery_contract import _source_audit_groups
    line = "拟交付：形成候选方案，沉淀可迁移的创作方法；保留后续评审记录。\n"
    groups = _source_audit_groups("### 交付产物\n" + line)
    parts = [part for values in groups.values() for part in values]
    assert line in parts
    assert not any(part.lstrip().startswith(("沉淀", "保留")) for part in parts)


def test_source_audit_many_separate_edits_never_reintroduce_unchanged_gaps():
    from creation.delivery_contract import _source_audit_groups, source_audit_segments
    original = "".join("旧事实{}。\n原值{}。\n".format(i, i) for i in range(80))
    changed = "".join("旧事实{}。\n新值{}。\n".format(i, i) for i in range(80))
    segments = source_audit_segments(changed, original)
    assert len(segments) <= 32
    text = "".join(segments.values())
    assert "旧事实" not in text and "原值" not in text
    assert all("新值{}。".format(i) in text for i in range(80))
    groups = _source_audit_groups(changed, original)
    assert all(part in changed for parts in groups.values() for part in parts)
    assert any(len(parts) > 1 for parts in groups.values())


@pytest.mark.parametrize("document", ["", "第一句，第二句。\n第三句。"])
def test_final_decoding_schema_fixes_audit_keys_without_repeated_arrays(document):
    from creation.delivery_contract import _audit_schema, source_audit_segments
    from model_schema import decoding_schema
    segments = source_audit_segments(document)
    schema = decoding_schema(_audit_schema(segments, {}))
    assert schema["type"] == "object"
    assert schema["required"] == list(segments)
    assert set(schema["properties"]) == set(segments)
    assert schema["additionalProperties"] is False
    assert all(value["type"] == "object" for value in schema["properties"].values())
    assert all(list(value["properties"]) == ["text", "source_id", "meaning", "kind", "basis"]
               for value in schema["properties"].values())


def test_source_audit_duplicate_json_keys_are_rejected_before_parsing_can_hide_them():
    from creation.delivery_contract import _unique_review_object
    with pytest.raises(OperationError, match="重复"):
        json.loads('{"source_audit":{"same":{},"same":{}}}', object_pairs_hook=_unique_review_object)


@pytest.mark.parametrize("basis", ["reasonable_inference", "unsupported"])
@pytest.mark.parametrize("kind,expected_id", [
    ("identity", "source_scope_grounding"), ("attribute", "source_attributes_grounding"),
    ("obligation", "source_obligations_grounding")])
def test_source_audit_negative_basis_overrides_model_all_pass(basis, kind, expected_id):
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    document = "新增的声明。"
    result = _apply_source_audit(audited_report(document, basis, kind), source_audit_segments(document), {}, with_source_scope_check(contract()), document)
    assert result["status"] == "revise"
    assert next(check for check in result["checks"] if check["id"] == expected_id)["passed"] is False
    assert result["checks"][0]["id"] == "a" and result["checks"][0]["passed"] is True
    assert document in result["corrections"][0]
    assert "source_audit" not in result


def test_source_audit_does_not_reject_verified_retrieval_provenance_note():
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    document = "*本文档基于检索到的历史项目周报与会议纪要生成，"
    provided = {"retrieved_evidence": {
        "references": [{"title": "项目周报与会议纪要", "content": "历史材料"}],
        "data_results": [], "web_results": [],
    }}

    result = _apply_source_audit(
        audited_report(document, "unsupported", "attribute"),
        source_audit_segments(document), provided,
        with_source_scope_check(contract()), document,
    )

    assert result["status"] == "pass"
    assert all(check["passed"] for check in result["checks"])
    assert result["corrections"] == []


@pytest.mark.parametrize("basis", ["reasonable_inference", "unsupported"])
def test_admitted_inference_with_valid_context_id_is_revised_without_format_retry(basis):
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    document = "各位同事。"
    provided = {"user_instruction_and_supplied_facts": "周三开会。"}
    result = _apply_source_audit(audited_report(document, basis, "identity", "user_instruction_and_supplied_facts"),
        source_audit_segments(document), provided, with_source_scope_check(contract()), document)
    assert result["status"] == "revise"


def test_source_audit_downgrade_targets_system_collision_id_without_changing_user_check():
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    document = "无依据的身份。"
    conditions = contract()
    conditions["acceptance"].append({"id": "source_scope_grounding", "criterion": "用户原有条件"})
    conditions = with_source_scope_check(conditions)
    report = audited_report(document, "unsupported", "identity")
    report["checks"] = [{"id": item["id"], "passed": True, "reason": "该条件已核对", "evidence": document}
                        for item in conditions["acceptance"]]
    result = _apply_source_audit(report, source_audit_segments(document), {}, conditions, document)
    by_id = {item["id"]: item["passed"] for item in result["checks"]}
    assert by_id["source_scope_grounding_"] is False
    assert by_id["source_scope_grounding"] is True
    assert by_id["a"] is True


@pytest.mark.parametrize("status", ["revise", "blocked"])
def test_review_derives_corrections_from_validated_findings_and_keeps_blocked(status):
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    document = "现有正文。"
    conditions = with_source_scope_check(contract(("work_context",) if status == "blocked" else ()))
    report = audited_report(document, "neutral_expression", "neutral")
    report["status"] = status
    report["checks"][0].update(passed=False, reason="所需结论尚未呈现", evidence=document)
    if status == "revise":
        with pytest.raises(OperationError, match="修改问题"):
            validate_review({key: value for key, value in report.items() if key != "source_audit"}, conditions, document)
    result = _apply_source_audit(report, source_audit_segments(document), {}, conditions, document)
    assert result["status"] == status
    assert len(result["corrections"]) == 1
    assert all(text in result["corrections"][0] for text in ("覆盖本轮需求", "所需结论尚未呈现", document))
    validate_review(result, conditions, document)


def test_review_correction_derivation_preserves_every_failed_condition_with_bounded_entries():
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    document = "现有正文。"
    base = contract()
    base["acceptance"] = [{"id": "item-{}".format(i), "criterion": "条件{}完成".format(i)} for i in range(12)]
    conditions = with_source_scope_check(base)
    report = audited_report(document, "unsupported", "attribute")
    report.update(status="revise", checks=[{"id": item["id"], "passed": False,
        "reason": "未满足" + item["id"], "evidence": document} for item in conditions["acceptance"]])
    result = _apply_source_audit(report, source_audit_segments(document), {}, conditions, document)
    assert 1 <= len(result["corrections"]) <= 12
    text = "\n".join(result["corrections"])
    assert all("未满足" + item["id"] in text for item in conditions["acceptance"])
    assert all(item["criterion"] in text for item in base["acceptance"])


def test_internal_review_rejects_generated_repair_plan_even_when_findings_are_valid():
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    report = audited_report("正文", "unsupported", "identity")
    report["corrections"] = ["将无依据身份替换为另一个角色"]
    with pytest.raises(OperationError, match="必须为空"):
        _apply_source_audit(report, source_audit_segments("正文"), {}, with_source_scope_check(contract()), "正文")


def test_audit_downgrade_preserves_other_problem_already_found_by_same_check():
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    document = "例会由协调员主持。"
    report = audited_report(document, "unsupported", "attribute")
    next(iter(report["source_audit"].values()))["text"] = "例会"
    report["status"] = "revise"
    check = next(item for item in report["checks"] if item["id"] == "source_attributes_grounding")
    check.update(passed=False, reason="资料未提供协调员角色及主持职责", evidence="协调员主持")
    result = _apply_source_audit(report, source_audit_segments(document), {}, with_source_scope_check(contract()), document)
    assert result["status"] == "revise"
    corrections = "\n".join(result["corrections"])
    assert "例会" in corrections and "资料未提供协调员角色及主持职责" in corrections
    assert "协调员主持" in corrections


@pytest.mark.parametrize("basis,kind,source", [
    ("source_supported", "attribute", "user_instruction_and_supplied_facts"),
    ("authorized_creation", "attribute", "user_instruction_and_supplied_facts"),
    ("neutral_expression", "neutral", ""),
    ("neutral_expression", "identity", ""),
    ("neutral_expression", "attribute", "user_instruction_and_supplied_facts"),
    ("neutral_expression", "obligation", ""),
])
def test_source_audit_accepts_explicit_facts_authorization_and_neutral_wording(basis, kind, source):
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    document = "候选内容。"
    provided = {"user_instruction_and_supplied_facts": "这是一次例会；写虚构故事。"}
    result = _apply_source_audit(audited_report(document, basis, kind, source), source_audit_segments(document), provided, with_source_scope_check(contract()), document)
    assert result["status"] == "pass"


def test_source_audit_catalog_binds_usable_retrieval_and_excludes_metadata_and_assistant():
    from creation.delivery_contract import source_audit_catalog, review_evidence
    evidence = review_evidence({"references": [{"content": "可用记忆96万元", "query": "查询密词", "observed_at": "采集日期"},
        {"content": "不可用记忆999万元", "can_use": False}],
        "data_results": [{"can_use": True, "content_excerpt": "可用数据42项", "structured_data": {"value": 42, "query": "内部查询"}},
                         {"can_use": False, "content_excerpt": "不可用数据999项"}],
        "web_results": [{"snippet": "可用网页摘要", "url": "元信息网址"}],
        "tool_results": [{"status": "completed", "query": "仅工具查询"}]})
    catalog = source_audit_catalog({"retrieved_evidence": evidence, "conversation": [
        {"role": "assistant", "content": "助手推断"}, {"role": "user", "content": "用户事实"}]})
    text = json.dumps(catalog, ensure_ascii=False)
    assert all(value in text for value in ("可用记忆96万元", "可用数据42项", "可用网页摘要", "用户事实"))
    assert all(value not in text for value in ("999", "查询密词", "采集日期", "内部查询", "元信息网址", "仅工具查询", "助手推断"))
    assert not catalog["retrieved-fact-0"]["user_authorization"]


def test_null_reference_and_conversation_keep_the_empty_view_contract():
    from creation.delivery_contract import review_evidence, source_audit_catalog
    view = review_evidence({"references": None})
    assert view["references"] == []
    assert source_audit_catalog({"conversation": None, "retrieved_evidence": view}) == {}


@pytest.mark.parametrize("can_use", [True, False])
def test_shared_writer_reference_view_preserves_explicit_usability(can_use):
    from creation.prompt_evidence import CreationEvidencePrompts
    reference = {"source_id": 42, "content": "原始正文", "can_use": can_use}
    view = CreationEvidencePrompts._prompt_references([reference])
    assert view[0]["can_use"] is can_use
    assert view[0]["content"] == "原始正文"


@pytest.mark.parametrize("evidence", [
    {"references": [{"content": "收入96万元"}]},
    {"data_results": [{"can_use": True, "content_excerpt": "收入96万元"}]},
    {"web_results": [{"snippet": "收入96万元"}]},
])
def test_source_audit_accepts_literal_usable_retrieval_with_rephrased_candidate(evidence):
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check, review_evidence
    document = "收入为96万元。"
    provided = {"retrieved_evidence": review_evidence(evidence)}
    result = _apply_source_audit(audited_report(document, "source_supported", "attribute", "retrieved-fact-0"),
        source_audit_segments(document), provided, with_source_scope_check(contract()), document)
    assert result["status"] == "pass"


@pytest.mark.parametrize("source_id,basis", [
    ("unknown", "source_supported"),
    ("unknown", "neutral_expression"),
    ("unknown", "reasonable_inference"),
    ("", "source_supported"),
    ("", "authorized_creation"),
    ("retrieved-fact-0", "authorized_creation"),
    ("creation_brief", "authorized_creation"),
    ("original_document", "authorized_creation"),
])
def test_source_audit_rejects_unknown_or_missing_source_and_non_user_authorization(source_id, basis):
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    document = "候选内容。"
    provided = {"retrieved_evidence": {"references": [{"content": "可用原文"}]},
                "creation_brief": "待确认的虚构设定", "original_document": "旧正文，未授权。"}
    with pytest.raises(OperationError):
        _apply_source_audit(audited_report(document, basis, "attribute", source_id), source_audit_segments(document), provided, with_source_scope_check(contract()), document)


def test_audit_uses_catalog_original_text_and_rejects_model_supplied_quote():
    from creation.delivery_contract import _apply_source_audit, source_audit_catalog, source_audit_segments, with_source_scope_check
    document = "收入为96万元。"
    provided = {"user_instruction_and_supplied_facts": "项目：收入96万元。\n原文下一行。"}
    catalog = source_audit_catalog(provided)
    report = audited_report(document, "source_supported", "attribute", "user_instruction_and_supplied_facts")
    claim = next(iter(report["source_audit"].values()))
    assert catalog[claim["source_id"]]["text"] == provided["user_instruction_and_supplied_facts"]
    assert _apply_source_audit(report, source_audit_segments(document), provided, with_source_scope_check(contract()), document)["status"] == "pass"
    claim["source_quote"] = "模型改写的伪引用"
    with pytest.raises(OperationError):
        _apply_source_audit(report, source_audit_segments(document), provided, with_source_scope_check(contract()), document)


@pytest.mark.parametrize("sources", [(), ("work_context",), ("business_data",), ("public_facts",), tuple(SOURCE_CAPABILITIES)])
def test_dependencies_come_from_missing_inputs_not_initial_route(sources):
    value = validate_contract(contract(sources), "")
    decision = bind_resources({"tools": ["memory_search", "internet_search", "data_search", "github_search"], "agents": []}, value)
    assert decision["tools"] == ["github_search"] + [SOURCE_CAPABILITIES[s] for s in sources]
    assert decision["agents"] == []


def test_selected_skill_rebinds_merged_public_gap_to_declared_private_tools():
    value = contract(("public_facts",))
    value["inputs"][0].update(
        need="本周会议纪要、电商 GPU 信息平台数据、Token 看板数据",
        query=("调用记忆搜索获取本周大模型性能成本优化周会会议纪要；"
               "调用数据检索获取电商 GPU 信息平台数据；"
               "调用数据检索获取 LangBridge 模型中心运营看板 Token 数据"),
    )
    rebound = bind_declared_workflow_inputs(value, [
        {"tool_id": "memory_search", "title": "本周会议纪要",
         "objective": "用记忆搜索获取本周大模型性能成本优化周会会议纪要"},
        {"tool_id": "data_search", "title": "GPU算力数据",
         "objective": "用数据检索获取电商 GPU 信息平台数据"},
        {"tool_id": "data_search", "title": "Token数据",
         "objective": "用数据检索获取 LangBridge 模型中心运营看板 Token 数据"},
    ])

    assert [(item["id"], item["source"]) for item in rebound["inputs"]] == [
        ("workflow_memory_search", "work_context"),
        ("workflow_data_search", "business_data"),
    ]
    assert bind_resources({"tools": []}, rebound)["tools"] == [
        "memory_search", "data_search",
    ]


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(extra=True), lambda c: c.update(acceptance=[]),
    lambda c: c.update(self_contained_reason=""), lambda c: c.update(inputs="bad"),
    lambda c: c["acceptance"].append(c["acceptance"][0]),
    lambda c: c["acceptance"][0].update(criterion=""),
])
def test_invalid_contract_fails_closed(mutation):
    c = contract(); mutation(c)
    with pytest.raises(OperationError): validate_contract(c, "")


def test_supplied_evidence_must_match_literal_material_and_needs_no_search():
    c = contract(["business_data"]); c["inputs"][0].update(state="provided", evidence="转化率为 3%", query="")
    assert bind_resources({"tools": ["data_search"]}, validate_contract(c, "给定数据：转化率为 3%"))["tools"] == []
    with pytest.raises(OperationError): validate_contract(c, "任务是提升转化率")


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(checks=[]), lambda r: r["checks"][0].update(id=[]),
    lambda r: r["checks"][0].update(passed="true"), lambda r: r["checks"][0].update(evidence="编造"),
    lambda r: r["checks"][0].update(reason=""), lambda r: r["checks"][0].update(extra=1),
    lambda r: r.update(status="blocked"), lambda r: r.update(corrections=[1]),
])
def test_invalid_delivery_cannot_complete(mutation):
    r = review(); mutation(r)
    with pytest.raises(OperationError): validate_review(r, contract(), "正文")


class StreamService:
    _stream_complete_agent_output = CreationService._stream_complete_agent_output
    def __init__(self, replies): self.replies = iter(replies); self.calls = []
    async def _stream_direct_completion(self, **kwargs):
        self.calls.append(kwargs)
        reply = next(self.replies)
        try:
            value = json.loads(reply)
            # Fixtures use the public list for direct validator tests, then
            # explicitly encode the actual model-only fixed-key protocol.
            if isinstance(value, dict) and isinstance(value.get('checks'), list):
                checks = value['checks']
                ids = [item.get('id') for item in checks if isinstance(item, dict)]
                if all(isinstance(key, str) for key in ids) and len(set(ids)) == len(checks):
                    value['checks'] = {item['id']: {key: entry for key, entry in item.items() if key != 'id'} for item in checks}
            if isinstance(value, dict) and value.get("source_audit") == "__test_audit_all_segments__":
                payload, _ = json.JSONDecoder().raw_decode(kwargs["user_prompt"])
                scope = {key: kind for key, kind in zip(
                    (item['id'] for item in payload['contract']['acceptance'][:3]),
                    ('identity', 'attribute', 'obligation'))}
                checks = value.get('checks') if isinstance(value.get('checks'), dict) else {}
                negative = [key for key in scope if checks.get(key, {}).get('passed') is False]
                for key in negative:
                    if not value['checks'][key]['evidence'] and payload['candidate_document_lines']:
                        value['checks'][key]['evidence'] = next(key for key, line in payload['candidate_document_lines'].items() if line.strip())
                def audit_part(part):
                    if isinstance(part, dict):
                        values = []
                        for piece in part['pieces']:
                            if 'line_range' in piece:
                                start, end = piece['line_range']
                                values.extend(payload['candidate_document_lines']['line-{}'.format(i)] for i in range(start, end + 1))
                            else:
                                values.append(payload['candidate_document_lines'][piece['line_id']] if 'line_id' in piece else piece['text'])
                        return ''.join(values)
                    return part
                value["source_audit"] = {key: {
                    "text": audit_part(parts[0]).strip(), "meaning": "测试已核对此片段",
                    "kind": scope[negative[0]] if negative else "neutral", "source_id": "",
                    "basis": "unsupported" if negative else "neutral_expression"}
                    for key, parts in payload["candidate_source_segments"].items()}
            reply = json.dumps(value)
        except (ValueError, TypeError):
            pass
        yield reply


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("source_audit"),
    lambda value: value["source_audit"].clear(),
    lambda value: value["source_audit"].update({"unknown-segment": copy.deepcopy(next(iter(value["source_audit"].values())))}),
    lambda value: value.update(source_audit=[]),
    lambda value: next(iter(value["source_audit"].values())).update(text="其他候选原文"),
])
@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.asyncio
async def test_source_audit_invalid_coverage_or_quote_requires_bounded_repair(mutation, recover):
    good = audited_report("正文", "neutral_expression", "neutral")
    for check in good["checks"]:
        check["evidence"] = "line-1"
    bad = copy.deepcopy(good)
    mutation(bad)
    service = StreamService([json.dumps(bad), json.dumps(good if recover else bad)])
    if recover:
        assert (await review_delivery(service, "整理正文", "正文", contract(), {}))["status"] == "pass"
    else:
        with pytest.raises(OperationError, match="无法核验"):
            await review_delivery(service, "整理正文", "正文", contract(), {})
    assert len(service.calls) == 2
    assert "source_audit" in service.calls[1]["user_prompt"]


@pytest.mark.asyncio
async def test_assessment_retries_invalid_claim_once():
    bad = contract(["work_context"]); bad["inputs"][0].update(state="provided", evidence="伪造")
    service = StreamService([json.dumps(bad), json.dumps(contract())])
    assert await assess_inputs(service, "整理给定材料", "正文", "transform", []) == {**contract(), "deliverable": "整理给定材料"}
    assert len(service.calls) == 2
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["current_document"] == "正文"
    assert payload["supplied_lines"]["document-1"] == "正文"


@pytest.mark.asyncio
async def test_assessment_prompt_discloses_substantive_gap_and_broadened_sources():
    # 修复：新增全新主题内容时，“现有正文仅有相关背景”不算实质覆盖；
    # 且来源含义需覆盖方法论/最佳实践/参考范例与用户过往创作，而非仅“事实”。
    service = StreamService([json.dumps(contract())])
    await assess_inputs(service, "增加口播带货视频的剧本设计章节", "正文", "generate", [])
    system = service.calls[0]["system_prompt"]
    # 输入类别不再局限于“事实”
    assert "事实、素材、方法或参考" in system
    # 全新实质内容 + 现有正文未真正包含 => 资料缺口，需按来源判断检索
    assert "全新实质内容" in system
    assert "不因文档已有相关背景就当作自足" in system
    # 背景/主题相邻/泛泛提及不算实质覆盖
    assert "泛泛提及不算实质覆盖" in system
    # 来源含义已拓宽：私有历史创作/素材 + 公开方法论/最佳实践
    assert "用户过往的同类创作" in system
    assert "方法论、最佳实践" in system


@pytest.mark.asyncio
async def test_assessment_prompt_treats_non_official_working_definition_as_self_contained():
    service = StreamService([json.dumps(contract())])
    await assess_inputs(service, "增加L0商家的定义介绍", "# 面向非L0商家的方案", "transform", [])
    system = service.calls[0]["system_prompt"]
    assert "本文工作定义" in system
    assert "官方定义不是完成本轮所必需的输入" in system
    assert "明确要求官方定义" in system


@pytest.mark.asyncio
async def test_failed_source_receipt_blocks_before_review_model():
    service = StreamService([])
    with pytest.raises(OperationError, match="必要资料"):
        await review_delivery(service, "写方案", "正文", contract(["work_context"]), {"input_receipts": {"memory_search": "failed"}})
    assert not service.calls


class DeliveryService(FakeCreationService):
    def __init__(self, sources=(), reports=("pass",), operation=None):
        super().__init__(); self.contract = contract(sources); self.reports = iter(reports)
        self.operation = operation or {"kind": "generate"}; self.checked = []; self.writes = 0; self.assessed = 0
    async def route_capabilities(self, **kwargs):
        return {"tools": [], "agents": [], "operation": self.operation, "source": "model"}
    async def assess_creation_inputs(self, *args, **kwargs):
        self.assessed += 1; return validate_contract(copy.deepcopy(self.contract), args[0] + args[1])
    async def review_creation_delivery(self, instruction, document, c, environment):
        self.checked.append(copy.deepcopy(environment))
        for source in c["inputs"]:
            assert environment["input_receipts"][SOURCE_CAPABILITIES[source["source"]]] == "completed"
        status = next(self.reports)
        if status == "transport_error": raise RuntimeError("review transport failed")
        result = review(status, document)
        result["checks"] = [{
            "id": item["id"], "passed": status == "pass", "reason": "逐项检查",
            "evidence": document if status == "pass" else "",
        } for item in c["acceptance"]]
        return validate_review(result, c, document)
    async def stream_agent_document(self, **kwargs):
        self.writes += 1
        text = "# 文档\n\n## 目标\n覆盖本轮需求的正文内容。\n\n## 方法\n按已给定的事实说明具体建议。\n"
        if 'previous_attempt_findings' in kwargs['user_prompt']:
            text += "\n已补全第 {} 次内容。".format(self.writes)
        yield text
    async def run_specialist_agent(self, **kwargs):
        if kwargs.get("json_mode") and kwargs["agent_id"] in {"delivery_repair", "post_loop_delivery_repair"}:
            self.writes += 1
            payload = json.JSONDecoder().raw_decode(kwargs["user_prompt"])[0]
            document = payload["document"]
            return json.dumps({"patches": [{"action": "replace", "target": {"text": document},
                "content": document + "\n已补全第 {} 次内容。".format(self.writes)}]})
        return await super().run_specialist_agent(**kwargs)


async def execute(service, doc="", instruction="创作一篇文档", **options):
    args = run_args(doc, instruction); args["options"] = CreationOptions(**options)
    return [e async for e in CreationAgentLoop(service).run(**args)]


@pytest.mark.asyncio
async def test_full_creation_repairs_route_omission_and_checks_after_sources():
    service = DeliveryService(tuple(SOURCE_CAPABILITIES))
    events = await execute(service, enabled_tools=list(SOURCE_CAPABILITIES.values()))
    assert events[-1]["type"] == "run.completed"
    assert service.writes == 1 and len(service.checked) == 1
    for tool in SOURCE_CAPABILITIES.values():
        assert events[-1]["data"]["input_receipts"][tool] == "completed"
    actions = [(e["type"], e["actor"]["id"]) for e in events]
    assert not any(actor == "solution_design_agent" for _, actor in actions)
    assert service.reference_queries == ["work_context specific gap"]
    assert service.data_queries == ["business_data specific gap"]


@pytest.mark.asyncio
async def test_self_contained_creation_does_not_retrieve_or_polish():
    service = DeliveryService()
    events = await execute(service)
    assert events[-1]["type"] == "run.completed"
    assert service.writes == 1 and not service.reference_queries and not service.data_queries
    assert not any(e["actor"]["id"] == "delivery_repair" for e in events)


@pytest.mark.asyncio
async def test_simple_patch_bypasses_assessment_and_review_even_with_complex_root():
    doc = "# 文档\n\n## 实施步骤与资源规划\n待删除内容。\n\n## 保留\n原文。\n"
    target = document_nodes(doc)[1]["id"]
    service = DeliveryService(operation={"kind": "patch", "patches": [{"action": "delete", "target": target}]})
    events = await execute(service, doc, "去除实施步骤与资源规划章节")
    assert events[-1]["data"]["document"] == "# 文档\n\n## 保留\n原文。\n"
    assert service.assessed == service.writes == 0 and not service.checked


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "transport_error"])
async def test_failed_acceptance_never_emits_completion(status):
    service = DeliveryService(reports=[status]); events = []
    with pytest.raises((OperationError, RuntimeError)):
        async for e in CreationAgentLoop(service).run(**run_args("", "写文档")): events.append(e)
    assert not any(e["type"] == "run.completed" for e in events)


@pytest.mark.asyncio
async def test_unavailable_required_resource_is_not_silently_removed(monkeypatch):
    monkeypatch.setattr("creation.agent_loop.normalize_creation_tool_ids", lambda value: ())
    service = DeliveryService(["public_facts"])
    with pytest.raises(OperationError, match="未启用"):
        await execute(service, enabled_tools=[])
    assert service.writes == 0


@pytest.mark.asyncio
async def test_required_resource_failure_stops_before_writer():
    service = DeliveryService(["work_context"])
    def fail(*args): raise RuntimeError("source unavailable")
    service.retrieve_references = fail
    with pytest.raises(OperationError, match="资料检索失败"):
        await execute(service, enabled_tools=["memory_search"])
    assert service.writes == 0

@pytest.mark.asyncio
@pytest.mark.parametrize("reports,complete", [(("revise", "pass"), True), (("revise", "revise", "revise"), False)])
async def test_polish_only_for_observed_gap_and_bounded_to_repair_budget(reports, complete):
    service = DeliveryService(reports=reports)
    events = []
    try:
        async for e in CreationAgentLoop(service).run(**run_args("", "写文档")): events.append(e)
    except OperationError:
        assert not complete
    assert any(e["type"] == "run.completed" for e in events) == complete
    assert any(e["actor"]["id"] == "delivery_repair" for e in events)

@pytest.mark.asyncio
async def test_transform_repair_binds_original_scope_and_preserves_outside():
    doc = "# 标题\n\n## 范围\n原句。\n\n## 其他\n原样保留。\n"
    target = document_nodes(doc)[1]["id"]
    service = DeliveryService(reports=["revise", "pass"], operation={"kind": "transform", "targets": [target]})
    async def write(*args, **kwargs):
        service.writes += 1
        yield json.dumps({"patches": [{"action": "replace", "target": {"text": "原句。"},
            "content": "初次改写。" if service.writes == 1 else "符合要求的改写。"}]}, ensure_ascii=False)
    service.stream_agent_document = write
    service.stream_specialist_agent = write
    writer_prompts = []
    async def run_write(**kwargs):
        writer_prompts.append(kwargs)
        return "".join([part async for part in write(**kwargs)])
    service.run_specialist_agent = run_write
    events = await execute(service, doc, "改写范围内的原句，其他保持原样")
    assert events[-1]["data"]["document"] == doc.replace("原句。", "符合要求的改写。")
    assert service.writes == 2
    assert "document 是补丁唯一绑定的不可变基线" in writer_prompts[-1]["system_prompt"]
    assert "candidate_document 仅用于理解失败内容" in writer_prompts[-1]["system_prompt"]


@pytest.mark.asyncio
async def test_bare_working_definition_uses_safe_patch_without_model_writer():
    doc = "# 方案\n\n## 原有正文\n\n保持原样。\n"
    root = document_nodes(doc)[0]["id"]
    service = DeliveryService(operation={"kind": "transform", "targets": [root]})
    events = await execute(service, doc, "增加灵机L0商家的定义介绍")
    completed = next(event for event in events if event["type"] == "run.completed")
    result = completed["data"]["document"]
    assert result.startswith(doc)
    assert "### 灵机L0商家定义" in result
    assert "不代表平台官方分级标准" in result
    assert "不以规模、人设、资产、资质、能力或行为特征" in result
    assert service.writes == 0

@pytest.mark.parametrize("source", list(SOURCE_CAPABILITIES))
def test_source_groups_require_explicit_coverage_without_forcing_retrieval(source):
    from creation.delivery_contract import assessment_schema, decode_contract
    schema = assessment_schema()["properties"]["requirements_by_source"]
    assert set(schema["required"]) == set(SOURCE_CAPABILITIES)
    c = contract(); c["inputs"] = {name: [] for name in SOURCE_CAPABILITIES}
    assert decode_contract(c, "")["inputs"] == []
    del c["inputs"][source]
    with pytest.raises(OperationError): decode_contract(c, "")

@pytest.mark.asyncio
async def test_invalid_contract_retries_are_bounded_and_never_select_defaults():
    service = StreamService(["{}", "{}"])
    with pytest.raises(OperationError): await assess_inputs(service, "需求", "", "generate", [])
    assert len(service.calls) == 2

@pytest.mark.asyncio
async def test_append_literal_never_calls_assessment_review_or_search():
    doc = "# 文档\n\n## 附录\n旧说明。\n\n## 保留\n原文。\n"
    service = DeliveryService(operation={"kind": "patch", "patches": [{"action": "replace", "target": {"text": "旧说明。"}, "content": "旧说明。请提前准备材料。"}]})
    events = await execute(service, doc, "在旧说明。后追加请提前准备材料。")
    assert events[-1]["data"]["document"] == doc.replace("旧说明。", "旧说明。请提前准备材料。")
    assert service.assessed == service.writes == 0 and not service.checked

@pytest.mark.asyncio
async def test_final_document_mutation_is_rechecked_before_completion():
    service = DeliveryService(reports=["pass", "revise"])
    loop = CreationAgentLoop(service)
    loop._guard_generated_placeholders = lambda document, requirement: (document + "\n最终变更。", [{"change": True}])
    events = []
    with pytest.raises(OperationError, match="补全本轮缺少的内容") as error:
        async for e in loop.run(**run_args("", "写文档")): events.append(e)
    # 失败原因必须带上验收给出的具体缺口，并短到能完整穿过客户端敏感词过滤；
    # 只说“已保留待修正内容”等于把排障工作丢回给用户。
    assert "未通过验收" in str(error.value)
    assert len(str(error.value)) <= 160
    assert not any(char in str(error.value) for char in "{}")
    # in-loop check (pass) + post-loop check (revise) + repair review attempt (fails).
    assert len(service.checked) >= 2
    assert not any(e["type"] == "run.completed" for e in events)

@pytest.mark.asyncio
async def test_resume_at_acceptance_preserves_receipts_and_does_not_repeat_sources():
    service = DeliveryService(["work_context"])
    events = await execute(service)
    checkpoints = [e["data"]["checkpoint"] for e in events if e["type"] == "operation.checkpoint"]
    checkpoint = next(c for c in checkpoints if c["cursor"] < len(c["plan"]) and c["plan"][c["cursor"]]["action"] == "delivery_check")
    resumed_service = DeliveryService(["work_context"])
    resumed = [e async for e in CreationAgentLoop(resumed_service).run(**run_args("", "继续"), resume_checkpoint=checkpoint)]
    assert resumed[-1]["type"] == "run.completed"
    assert resumed_service.writes == resumed_service.assessed == 0
    assert not resumed_service.reference_queries and len(resumed_service.checked) == 1
    assert resumed[-1]["data"]["input_receipts"] == {"memory_search": "completed"}

@pytest.mark.asyncio
async def test_review_retries_invalid_literal_evidence_once():
    service = StreamService([json.dumps(review_for_model(document="不存在的片段")), json.dumps(review_for_model(document="line-1"))])
    report = await review_delivery(service, "写正文", "正文", contract(), {})
    assert report["status"] == "pass" and len(service.calls) == 2

@pytest.mark.asyncio
async def test_review_never_defaults_to_pass_after_retry_exhaustion():
    service = StreamService(["{}", "{}"])
    with pytest.raises(OperationError): await review_delivery(service, "写正文", "正文", contract(), {})
    assert len(service.calls) == 2

@pytest.mark.asyncio
async def test_authored_patch_uses_same_decoding_contract_as_direct_patch():
    from creation.service import CreationService
    from creation.operations import patch_response_schema
    service = CreationService.__new__(CreationService); service.model = "test-model"
    calls = []
    async def stream(**kwargs):
        calls.append(kwargs); yield '{"patches":[{"action":"delete","target":"node-1"}]}'
    service._stream_complete_agent_output = stream
    service._log_creation_usage = lambda **kwargs: None
    await service.run_specialist_agent(agent_id="document_transform", system_prompt="", user_prompt="",
        json_mode=True, json_schema=patch_response_schema())
    assert calls[0]["temperature"] == 0.0
    schema = calls[0]["json_schema"]
    assert schema["additionalProperties"] is False
    assert "maxItems" not in schema["properties"]["patches"]
    for variant in schema["properties"]["patches"]["items"]["anyOf"]:
        assert variant["additionalProperties"] is False

@pytest.mark.asyncio
async def test_review_evidence_ids_are_resolved_to_actual_document_lines():
    service = StreamService([json.dumps(review_for_model(document="line-3"))])
    report = await review_delivery(service, "写正文", "# 标题\n\n正文", contract(), {})
    assert report["checks"][0]["evidence"] == "正文"
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["provided_materials"]["user_instruction_and_supplied_facts"] == "写正文"
    assert payload["candidate_document_lines"]["line-3"] == "正文"
    for key, item in service.calls[0]["json_schema"]["properties"]["checks"]["properties"].items():
        assert item['oneOf'][0]['properties']['evidence']['enum'] == ["line-1", "line-3"]
        assert item['oneOf'][0]['properties']['passed'] == {"const": True}
        assert item['oneOf'][1]['properties']['evidence']['enum'] == (
            ["line-1", "line-3"] if key.startswith('source_') else ["", "line-1", "line-3"])
        assert item['oneOf'][1]['properties']['passed'] == {"const": False}
    assert service.calls[0]["json_schema"]["properties"]["corrections"]["maxItems"] == 0
    assert service.calls[0]["json_schema"]["properties"]["corrections"]["minItems"] == 0

def test_corrected_resource_plan_does_not_display_stale_no_search_reason():
    original = {"tools": [], "agents": [], "reasoning": "无需检索"}
    updated = bind_resources(original, contract(["business_data"]))
    assert updated["tools"] == ["data_search"]
    assert updated["reasoning"] != original["reasoning"]
    assert original["tools"] == []

@pytest.mark.asyncio
@pytest.mark.parametrize("intent,operation", [("answer", {"kind": "respond", "response": "未经检索的事实"}), ("respond", {"kind": "answer"}), ("edit", {"kind": "generate"}), ("create", {"kind": "answer"}), ("patch", {"kind": "generate"})])
async def test_reply_and_information_answer_cannot_bypass_intent_boundary(intent, operation):
    from types import SimpleNamespace
    state = SimpleNamespace(current_document="", environment={"requirement": {"task_intent": {"action": intent}}})
    with pytest.raises(OperationError, match="主目标不一致"):
        async for _ in CreationAgentLoop(DeliveryService())._apply_routing_decision(state, {},
            {"tools": [], "agents": [], "operation": operation, "source": "model"}): pass

@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["answer", "respond"])
async def test_model_decoding_excludes_the_wrong_reply_executor(intent):
    from creation.service import CreationService
    service = CreationService.__new__(CreationService); service.model = "test-model"
    calls = []
    async def stream(**kwargs):
        calls.append(kwargs)
        operation = {"kind": intent}
        if intent == "respond": operation["response"] = "已理解"
        yield json.dumps({"tools": [], "agents": [], "reasoning": "本轮目标", "operation": operation})
    service._stream_direct_completion = stream
    service._log_creation_usage = lambda **kwargs: None
    result = await service.route_capabilities(query="本轮请求", requirement={"task_intent": {"action": intent}, "operation_context": {"current_document": "正文"}}, selected_skills=[])
    assert result["source"] == "model"
    variants = calls[0]["json_schema"]["properties"]["operation"]["anyOf"]
    assert [v["properties"]["kind"]["const"] for v in variants] == [intent]

@pytest.mark.parametrize("state,tools", [("not_needed", []), ("provided", []), ("missing", ["data_search"])])
def test_explicit_source_states_distinguish_absent_material_from_absent_dependency(state, tools):
    from creation.delivery_contract import decode_contract
    c = contract(); c.pop("inputs")
    c["requirements_by_source"] = {s: {"need": "", "reason": "本轮不依赖", "state": "not_needed", "evidence": "", "query": ""} for s in SOURCE_CAPABILITIES}
    c["requirements_by_source"]["business_data"] = {"need": "收入", "reason": "核对本轮指标需求", "state": state, "evidence": "收入100万元" if state=="provided" else "", "query": "查询收入" if state=="missing" else ""}
    decoded = decode_contract(c, "用户给定：收入100万元")
    assert bind_resources({"tools": []}, decoded)["tools"] == tools

@pytest.mark.asyncio
async def test_missing_independent_intent_cannot_disable_answer_guard(monkeypatch):
    from creation.service import CreationService
    async def failed(*args): return {}
    monkeypatch.setattr("creation.skill_governance.task_intent", failed)
    service = CreationService.__new__(CreationService)
    with pytest.raises(OperationError, match="未能核验本轮动作"):
        await service.route_capabilities(query="查资料后回答", requirement={"operation_context": {"current_document": "正文"}})

@pytest.mark.parametrize("index,text", [(0, "新标题"), (1, "周三下午讨论项目进展。")])
def test_short_replacement_cannot_erase_an_entire_heading_subtree(index, text):
    from creation.operations import validate_literal_patch
    doc = "# 会议说明\n\n## 会议安排\n周一上午讨论项目进展。\n\n## 保留\n不应消失。\n"
    with pytest.raises(OperationError, match="text 选择器"):
        validate_literal_patch(doc, [{"action": "replace", "target": document_nodes(doc)[index]["id"], "content": text}], "改为" + text)


def test_source_quote_ids_restore_literal_text_without_newline_escaping():
    from creation.delivery_contract import decode_contract
    c = contract(); c.pop("inputs")
    c["requirements_by_source"] = {s: {"need": "", "reason": "本轮不依赖", "state": "not_needed", "evidence": "", "query": ""} for s in SOURCE_CAPABILITIES}
    c["requirements_by_source"]["work_context"] = {"need": "会议时间", "reason": "保留已有时间", "state": "provided", "evidence": "document-4", "query": ""}
    decoded = decode_contract(c, "## 会议安排\n周一上午讨论项目进展。", {"document-4": "周一上午讨论项目进展。"})
    assert decoded["inputs"][0]["evidence"] == "周一上午讨论项目进展。"


def test_routing_does_not_promote_lossy_intent_summary_over_user_scope():
    service = CreationService.__new__(CreationService)
    instruction = "在会议安排末尾补充一句邀请话，其他部分不动"
    _, prompt = service.build_routing_prompts(instruction, {
        "task_intent": {"action": "edit", "deliverable": "文档", "primary_goal": "在联系方式之后追加"},
        "operation_context": {"current_document": "## 会议安排\n周一讨论。\n\n## 联系方式\n联系项目组。"}},
        selected_skills=[], enabled_tool_ids=[])
    assert prompt.endswith(instruction)
    assert "在联系方式之后追加" not in prompt
    assert "edit" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("instruction,document,expected", [
    ("写一份报告", "", ["generated"]),
    ("写一份报告，标题为《我的报告》", "", ["user"]),
    ("重写整份报告", "# 原标题\n正文", ["generated", "existing"]),
])
async def test_title_decoder_offers_only_supported_provenance(instruction, document, expected):
    from creation.skill_governance import IDENTITY_SCHEMA
    before = copy.deepcopy(IDENTITY_SCHEMA)
    service = CreationService.__new__(CreationService); service.model = "test-model"
    calls = []
    async def stream(**kwargs):
        calls.append(kwargs)
        yield json.dumps({"tools": [], "agents": [], "reasoning": "需要澄清", "operation": {"kind": "respond", "response": "请提供材料"}})
    service._stream_direct_completion = stream
    service._log_creation_usage = lambda **kwargs: None
    await service.route_capabilities(query=instruction, requirement={"task_intent": {"action": "create"}, "operation_context": {"current_document": document}}, selected_skills=[])
    variants = calls[0]["json_schema"]["properties"]["operation"]["anyOf"]
    for variant in variants:
        identity = variant["properties"].get("document_identity")
        if identity: assert identity["properties"]["source"]["enum"] == expected
    assert IDENTITY_SCHEMA == before


@pytest.mark.parametrize("escaped", [r"## 附录\n旧说明。", r"## 附录\\n旧说明。"])
def test_selector_json_newline_transport_is_repaired_without_content_changes(escaped):
    from creation.operations import normalize_operation_selectors
    operation = {"kind": "transform", "targets": [{"text": escaped, "occurrence": 1}]}
    normalized = normalize_operation_selectors("## 附录\n旧说明。", operation)
    assert normalized["targets"][0]["text"] == "## 附录\n旧说明。"
    assert operation["targets"][0]["text"] == escaped


def test_selector_transport_preserves_literal_backslashes_and_rejects_guesses():
    from creation.operations import normalize_operation_selectors
    operation = {"kind": "patch", "patches": [{"action": "replace", "target": {"text": r"a\nb", "occurrence": 1}, "content": r"c\nd"}]}
    assert normalize_operation_selectors(r"a\nb" + "\na\nb", operation) == operation
    assert normalize_operation_selectors("a b", operation) == operation
    repaired = normalize_operation_selectors("a\nb", operation)
    assert repaired["patches"][0]["content"] == r"c\nd"


def test_self_contained_delivery_failure_requires_actionable_revision():
    with pytest.raises(OperationError, match="输入已齐备"):
        validate_review(review("blocked"), contract(), "正文")
    assert validate_review(review("revise"), contract(), "正文")["status"] == "revise"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["assessment", "review"])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_contract_nodes_recover_length_without_accepting_partial_json(stage, exhausted):
    service = CreationService.__new__(CreationService)
    calls = []
    response = audited_report("正文", "neutral_expression", "neutral")
    for check in response["checks"]:
        check["evidence"] = "line-1"
    response['checks'] = {item['id']: {key: value for key, value in item.items() if key != 'id'} for item in response['checks']}
    complete = json.dumps(contract() if stage == "assessment" else response)
    async def stream(**kwargs):
        calls.append(kwargs)
        yield complete
        if exhausted or len(calls) == 1:
            raise OperationError("CREATION_DOCUMENT_TRUNCATED", "length")
    service._stream_direct_completion = stream
    async def execute_node():
        if stage == "assessment":
            return await assess_inputs(service, "写正文", "正文", "generate", [])
        return await review_delivery(service, "写正文", "正文", contract(), {})
    if exhausted:
        with pytest.raises(OperationError) as error:
            await execute_node()
        assert error.value.code == ("CREATION_INPUT_CONTRACT_INVALID" if stage == "assessment" else "CREATION_DELIVERY_UNVERIFIED")
        assert "自动重试" in str(error.value)
        assert calls[-1]["num_predict"] == 16384
        assert len(calls) == 3
    else:
        result = await execute_node()
        if stage == 'assessment':
            assert result == {**contract(), 'deliverable': '写正文'}
        else:
            assert result['status'] == 'pass' and result['corrections'] == []
            assert {item['id']: item for item in result['checks']} == {item['id']: item for item in review_for_model()['checks']}
        assert len(calls) == 2
    assert calls[1]["num_predict"] == calls[0]["num_predict"] * 4
    assert all(call["user_prompt"] == calls[0]["user_prompt"] for call in calls)
    assert all(call["json_schema"] == calls[0]["json_schema"] for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_chars", [45000, 50000])
async def test_contract_context_guard_rejects_request_without_output_room_before_transport(prompt_chars):
    service = CreationService.__new__(CreationService)
    service.model = "local-test-model"
    calls = []

    async def stream(**kwargs):
        calls.append(kwargs)
        yield "{}"

    service._stream_direct_completion = stream
    with pytest.raises(OperationError) as error:
        async for _ in service._stream_complete_agent_output(
            system_prompt="验" * prompt_chars,
            user_prompt="",
            creation_model=None,
            creation_api_key=None,
            creation_base_url=None,
            num_predict=3200,
            temperature=0.0,
            disable_thinking=True,
            json_mode=True,
            json_schema={"type": "object"},
            context_guard=True,
        ):
            pass
    assert error.value.code == "CREATION_CONTEXT_BUDGET_EXCEEDED"
    assert error.value.retryable is True
    assert calls == []


def test_delivery_prompt_materials_keep_inventory_without_repeating_fact_bodies():
    from creation.prompt_evidence import CreationEvidencePrompts
    provided = {
        "user_instruction_and_supplied_facts": "生成周报",
        "retrieved_evidence": {
            "data_results": [{
                "source_id": "report-1", "title": "GPU报表", "can_use": True,
                "content_excerpt": "GPU利用率为80%" * 500,
                "structured_data": {"metric": "GPU利用率", "value": "80%"},
            }, {
                "source_id": "report-2", "title": "缺失报表", "can_use": False,
                "unavailable_reason": "页面未加载",
            }],
            "source_view": {"excerpted": True, "counts": {"data_results": {"available": 2, "included": 2}}},
        },
    }
    compact = CreationEvidencePrompts.delivery_prompt_materials(provided)
    encoded = json.dumps(compact, ensure_ascii=False)
    assert "GPU利用率为80%" not in encoded
    assert "structured_data" not in encoded
    assert compact["retrieved_evidence"]["data_results"][0]["source_id"] == "report-1"
    assert compact["retrieved_evidence"]["data_results"][0]["can_use"] is True
    assert compact["retrieved_evidence"]["data_results"][1]["unavailable_reason"] == "页面未加载"
    assert provided["retrieved_evidence"]["data_results"][0]["structured_data"]["value"] == "80%"


@pytest.mark.asyncio
async def test_review_transport_failure_is_not_reclassified_as_length():
    import httpx
    service = CreationService.__new__(CreationService)
    async def stream(**kwargs):
        yield '{'
        raise httpx.ReadError("disconnected")
    service._stream_direct_completion = stream
    with pytest.raises(httpx.ReadError):
        await review_delivery(service, "写正文", "正文", contract(), {})


def test_review_sources_share_writer_fact_policy_without_raw_payloads():
    from creation.delivery_contract import review_evidence
    from creation.prompt_evidence import CreationEvidencePrompts
    environment = {
        'data_results': [
            {'source_id': 'usable', 'source_url': 'https://example.test/report',
             'can_use': True, 'content_excerpt': '收入100万元',
             'structured_data': {'metric': 100, 'raw_html': 'private raw' * 20000},
             'provenance': {'period': '2026-08'}, 'history': [{'raw_html': 'old' * 20000}]},
            {'source_id': 'unavailable', 'can_use': False, 'unavailable_reason': '不可引用',
             'structured_data': {'metric': 999}, 'content_excerpt': '不可引用999'}],
        'references': [{'source_id': 1, 'content': '资料正文。', 'source_url': 'https://example.test/doc'}],
        'tool_results': [{'tool_id': 'data_search', 'status': 'completed', 'raw_html': 'raw' * 20000}],
    }
    original = copy.deepcopy(environment)
    evidence = review_evidence(environment)
    assert environment == original
    assert evidence['data_results'] == CreationEvidencePrompts._prompt_data_results(environment['data_results'])
    assert evidence['references'] == CreationEvidencePrompts._prompt_references(environment['references'])
    assert evidence['data_results'][0]['structured_data']['metric'] == 100
    assert evidence['data_results'][0]['provenance']['period'] == '2026-08'
    assert evidence['data_results'][1]['can_use'] is False
    serialized = json.dumps(evidence, ensure_ascii=False)
    assert len(serialized) < 2500
    assert '999' not in serialized and 'raw_html' not in serialized
    assert evidence['source_view']['excerpted'] is True


def test_review_many_sources_are_bounded_and_omissions_are_explicit():
    from creation.delivery_contract import review_evidence
    environment = {
        'references': [{'source_id': i, 'content': '来源事实' * 10000} for i in range(30)],
        'data_results': [{'source_id': i, 'can_use': True, 'content_excerpt': '经营指标' * 10000,
                         'structured_data': {'value': i}, 'history': [{'raw_html': 'raw' * 10000}]} for i in range(30)]}
    evidence = review_evidence(environment)
    assert len(json.dumps(evidence, ensure_ascii=False)) < 42000
    for kind in ('references', 'data_results'):
        count = evidence['source_view']['counts'][kind]
        assert count['available'] == 30 and 0 < count['included'] <= 30
    assert evidence['source_view']['counts']['references']['included'] < 30


def test_reviewer_keeps_the_same_source_coverage_as_the_author():
    from creation.delivery_contract import review_evidence
    from creation.prompt_evidence import CreationEvidencePrompts as Author
    environment = {
        'references': [{'source_id': i, 'content': '可引用的项目事实。' * 400} for i in range(10)],
        'data_results': [{'source_id': i, 'can_use': True, 'content_excerpt': '指标原文。' * 200,
                         'structured_data': {'value': i}} for i in range(30)]}
    evidence = review_evidence(environment)
    assert evidence['references'] == Author._prompt_references(environment['references'])
    assert evidence['data_results'] == Author._prompt_data_results(environment['data_results'])


def _weekly_report_skill():
    """三个步骤声明互不相同取数对象的已安装 Skill。"""
    return {
        "id": "gpu-weekly",
        "title": "GPU成本优化周报模板",
        "executionSteps": [
            {"id": "meeting", "title": "本周会议纪要",
             "objective": "用@记忆搜索 Tool 获取本周大模型性能成本优化周会会议纪要",
             "tools": ["memory_search"], "agents": [], "skills": []},
            {"id": "aigc", "title": "AIGC进度总结",
             "objective": "用@记忆搜索 Tool 获取本周AIGC共建项目的进展",
             "tools": ["memory_search"], "agents": [], "skills": []},
            {"id": "metrics", "title": "GPU算力数据",
             "objective": "用@数据检索 Tool 获取电商GPU信息平台的数据并按口径制表",
             "tools": ["data_search"], "agents": [], "skills": []},
        ],
    }


class WorkflowContractService(DeliveryService):
    """按根请求造缺口，并记录契约评估看到的本轮工作流声明。"""

    def __init__(self, sources=(), operation=None):
        super().__init__(
            sources=sources or tuple(SOURCE_CAPABILITIES),
            operation=operation or {"kind": "execute_skill", "skill_ids": ["gpu-weekly"]},
        )
        self.workflow_plans = []

    async def assess_creation_inputs(self, *args, **kwargs):
        self.workflow_plans.append(list(kwargs.get("workflow_plan") or []))
        self.assessed += 1
        return validate_contract(copy.deepcopy(self.contract), args[0] + args[1])


async def route_with_contract(service, instruction, explicit=("gpu-weekly",)):
    loop = CreationAgentLoop(service)
    state = loop._new_state(
        user_message=instruction, root_request=instruction, current_document="",
        conversation=[], selected_skills=[_weekly_report_skill()],
        options=CreationOptions(
            enabled_tools=("memory_search", "data_search", "internet_search")
        ),
        model_mode="local", session_id="session-skill-contract", run_id="run-skill-contract",
    )
    state.environment["explicit_skill_ids"] = list(explicit)
    decision = await service.route_capabilities(query=instruction)
    events = [
        event async for event in loop._apply_routing_decision(
            state,
            {"id": "creation_main_agent", "name": "创作 Agent", "action": "route"},
            decision,
        )
    ]
    return state, events


@pytest.mark.asyncio
async def test_contract_gap_query_never_replaces_skill_step_retrieval_target():
    # 回归：会话 ae89c068 中技能已显式选定，但同一 Tool 的多个 Skill 步骤被按
    # Tool id 匹配的契约查询整体顶掉，各步骤检索词与召回结果完全相同。
    service = WorkflowContractService()
    state, _events = await route_with_contract(
        service, "@GPU成本优化周报模板 请生成GPU成本优化的周报"
    )
    assert state.environment["strict_skill_workflow"] is True
    steps = [
        item
        for item in state.plan
        if item.get("skill_step_id") and item.get("kind") == "tool"
    ]
    assert [(item["id"], item["skill_step_id"]) for item in steps] == [
        ("memory_search", "meeting"),
        ("memory_search", "aigc"),
        ("data_search", "metrics"),
    ]
    # 缺口仍然记账，交付验收据此确认资料已尝试检索；步骤归属仍标为技能。
    assert all(item["input_requirement_ids"] for item in steps)
    assert all(item["decision_source"] == "skill" for item in steps)
    # 但根请求派生的查询不得改写技能声明的取数对象。
    assert not [item for item in steps if item.get("input_query")]
    queries = [CreationAgentLoop._step_context_query(state, item) for item in steps]
    assert len(set(queries)) == len(queries)
    assert "本周大模型性能成本优化周会会议纪要" in queries[0]
    assert "本周AIGC共建项目的进展" in queries[1]
    assert "电商GPU信息平台" in queries[2]
    assert not [query for query in queries if "specific gap" in query]


@pytest.mark.asyncio
async def test_contract_supplemental_capability_runs_after_strict_skill_steps():
    # 技能未声明的补位能力不得先于技能步骤执行：它的结果会注入每个步骤的写作上下文。
    service = WorkflowContractService()
    state, _events = await route_with_contract(service, "@GPU成本优化周报模板 请生成周报")
    actions = [(item.get("action"), item["id"]) for item in state.plan]
    skill_step_positions = [
        index
        for index, item in enumerate(state.plan)
        if item.get("skill_step_id") and item.get("kind") == "tool"
    ]
    supplemental = [
        index
        for index, item in enumerate(state.plan)
        if item["id"] == "internet_search" and not item.get("skill_step_id")
    ]
    assert supplemental, actions
    assert min(skill_step_positions) < supplemental[0]
    assert state.plan[supplemental[0]]["input_query"] == "public_facts specific gap"
    assert state.plan[-1]["action"] == "delivery_check"


@pytest.mark.asyncio
async def test_contract_assessment_sees_declared_workflow_of_explicit_skill():
    service = WorkflowContractService()
    await route_with_contract(service, "@GPU成本优化周报模板 请生成GPU成本优化的周报")
    assert len(service.workflow_plans) == 1
    declared = service.workflow_plans[0]
    assert len(declared) == 3
    assert all(line.startswith("GPU成本优化周报模板：") for line in declared)
    assert any("电商GPU信息平台" in line for line in declared)


@pytest.mark.asyncio
async def test_contract_assessment_gets_no_workflow_plan_without_explicit_skill():
    service = WorkflowContractService(
        operation={"kind": "generate", "document_identity": None}
    )
    await route_with_contract(
        service, "请生成GPU成本优化的周报", explicit=()
    )
    assert service.workflow_plans == [[]]


@pytest.mark.asyncio
async def test_assessment_prompt_discloses_workflow_plan_only_when_declared():
    service = StreamService([json.dumps(contract())])
    await assess_inputs(
        service, "写周报", "正文", "execute_skill", [],
        workflow_plan=["GPU周报模板：本周会议纪要｜取纪要"],
    )
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["workflow_plan"] == ["GPU周报模板：本周会议纪要｜取纪要"]
    assert "不得再当作未落实的缺口" in service.calls[0]["system_prompt"]
    assert payload["supplied_lines"]["instruction-1"] == "写周报"


@pytest.mark.asyncio
async def test_assessment_prompt_keeps_base_rules_without_workflow_plan():
    service = StreamService([json.dumps(contract())])
    await assess_inputs(service, "写周报", "正文", "generate", [])
    assert "workflow_plan" not in json.loads(service.calls[0]["user_prompt"])
    assert service.calls[0]["system_prompt"] == CONTRACT_PROMPT


def test_legacy_checkpoint_input_query_cannot_shadow_skill_step_target():
    # 修复前落盘的断点仍带契约查询，恢复执行时必须让位于步骤声明的取数对象。
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="请生成周报", root_request=None, current_document="", conversation=[],
        selected_skills=[], options=CreationOptions(), model_mode="local",
        session_id="session-legacy-query", run_id="run-legacy-query",
    )
    step = {"id": "memory_search", "skill_step_id": "aigc", "skill_step_title": "AIGC进度总结",
            "skill_step_objective": "获取本周AIGC共建项目的进展",
            "input_query": "查询该项目的 GPU 成本历史数据"}
    query = CreationAgentLoop._step_context_query(state, step)
    assert "本周AIGC共建项目的进展" in query
    assert "查询该项目的 GPU 成本历史数据" not in query
    # 没有自身检索对象的补位步骤继续沿用契约查询。
    assert CreationAgentLoop._step_context_query(
        state, {"id": "internet_search", "input_query": "public gap query"}
    ) == "public gap query"


@pytest.mark.parametrize('resume_field', ['resume_state', 'resume_checkpoint'])
@pytest.mark.asyncio
async def test_validated_legacy_resume_seeds_saved_delivery_failures(resume_field):
    from creation.agent_loop import LoopState
    from creation.delivery_contract import prior_delivery_failures
    document = '# 文档\n\n待修正文。'
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args(document), model_mode='local')
    state.environment.update(input_contract=contract(), delivery_review=review('revise'), delivery_repair_count=1)
    checkpoint = LoopState.restore(state.serializable()).serializable()
    captured = []
    async def stop_after_seed(restored):
        captured.append(restored)
        raise RuntimeError('stop after validated migration')
    loop._recover_document_identity = stop_after_seed
    with pytest.raises(RuntimeError, match='validated migration'):
        async for _ in loop.run(**run_args(document), **{resume_field: checkpoint}):
            pass
    restored = captured[0]
    assert prior_delivery_failures(restored.user_message, restored.environment, contract())['checks']
    assert restored.environment['delivery_review'] == review('revise')
    assert LoopState.restore(restored.serializable()).environment['delivery_pending_review'] == restored.environment['delivery_pending_review']


@pytest.mark.parametrize('count,status', [(0, 'revise'), (1, 'pass')])
def test_legacy_delivery_migration_skips_new_or_completed_chain(count, status):
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args('正文'), model_mode='local')
    state.environment.update(input_contract=contract(), delivery_review=review(status), delivery_repair_count=count)
    loop._seed_restored_delivery_failures(state)
    assert 'delivery_pending_review' not in state.environment


@pytest.mark.asyncio
async def test_cross_session_resume_is_rejected_before_legacy_failure_seed():
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args('正文'), model_mode='local')
    state.environment.update(input_contract=contract(), delivery_review=review('revise'), delivery_repair_count=1)
    calls = []
    loop._seed_restored_delivery_failures = lambda restored: calls.append(restored)
    args = run_args('正文'); args['session_id'] = 'other-session'
    with pytest.raises(OperationError, match='不属于当前会话'):
        async for _ in loop.run(**args, resume_checkpoint=state.serializable()):
            pass
    assert not calls


@pytest.mark.asyncio
async def test_post_loop_repair_failure_preserves_pending_findings_before_model_call():
    from creation.agent_loop import LoopState
    from creation.delivery_contract import remember_delivery_failures, prior_delivery_failures
    service = DeliveryService(reports=['pass', 'revise'])
    original_review = service.review_creation_delivery
    async def remember_review(instruction, document, conditions, environment):
        report = await original_review(instruction, document, conditions, environment)
        remember_delivery_failures(instruction, environment, conditions, report['checks'], report)
        return report
    service.review_creation_delivery = remember_review
    loop = CreationAgentLoop(service)
    loop._guard_generated_placeholders = lambda document, requirement: (document + '\n最终变更。', [{'change': True}])
    async def fail_before_model(*args, **kwargs):
        raise RuntimeError('repair interrupted before model')
        yield
    loop._repair_delivery = fail_before_model
    events = []
    with pytest.raises(OperationError):
        async for event in loop.run(**run_args('', '写文档')):
            events.append(event)
    saved = [e['data']['checkpoint'] for e in events if e['type'] == 'operation.checkpoint'][-1]
    restored = LoopState.restore(saved)
    assert restored.environment['delivery_repair_count'] == 1
    assert restored.environment['delivery_review']['status'] == 'revise'
    assert restored.current_document.endswith('最终变更。')
    assert prior_delivery_failures(restored.user_message, restored.environment, contract())['checks']
    assert not any(e['type'] == 'run.completed' for e in events)


def test_legacy_repair_report_keeps_stale_passed_evidence_as_history_only():
    from creation.delivery_contract import prior_delivery_failures
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args('周三下午开会。'), model_mode='local')
    old = review('revise')
    old['checks'].append({'id': 'old_extra', 'passed': True, 'reason': '旧稿已给时间', 'evidence': '各位同事，周三下午开会。'})
    state.environment.update(input_contract=contract(), delivery_review=old, delivery_repair_count=1)
    loop._seed_restored_delivery_failures(state)
    assert prior_delivery_failures(state.user_message, state.environment, contract())['checks'] == [
        {'id': 'a', 'reason': '逐项检查', 'evidence': ''}]
    with pytest.raises(OperationError):
        validate_review(old, {'inputs': [], 'acceptance': [{'id': 'a'}, {'id': 'old_extra'}]}, state.current_document)


def test_legacy_blocked_report_uses_saved_missing_input_contract():
    from creation.delivery_contract import prior_delivery_failures
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args('候选'), model_mode='local')
    conditions = contract(['work_context'])
    state.environment.update(input_contract=conditions, delivery_review=review('blocked'), delivery_repair_count=1)
    loop._seed_restored_delivery_failures(state)
    assert prior_delivery_failures(state.user_message, state.environment, conditions)['checks']


@pytest.mark.parametrize('failure', ['missing', 'unknown', 'array', 'embedded_id', 'duplicate'])
@pytest.mark.parametrize('recover', [False, True])
@pytest.mark.asyncio
async def test_fixed_review_keys_require_complete_unique_checks_and_keep_bounded_repair(failure, recover):
    from creation.delivery_contract import with_source_scope_check
    good = audited_report('正文', 'neutral_expression', 'neutral')
    good['checks'] = {check['id']: {key: ('line-1' if key == 'evidence' else value)
        for key, value in check.items() if key != 'id'} for check in good['checks']}
    bad = copy.deepcopy(good)
    if failure == 'missing': bad['checks'].pop('a')
    elif failure == 'unknown': bad['checks']['unknown'] = copy.deepcopy(bad['checks']['a'])
    elif failure == 'array': bad['checks'] = [{'id': key, **value} for key, value in bad['checks'].items()]
    elif failure == 'embedded_id': bad['checks']['a']['id'] = 'source_scope_grounding'
    bad_text = json.dumps(bad)
    if failure == 'duplicate':
        bad_text = bad_text.replace('"checks": {', '"checks": {"a": ' + json.dumps(bad['checks']['a']) + ', ', 1)
    class RawService(StreamService):
        async def _stream_direct_completion(self, **kwargs):
            self.calls.append(kwargs)
            yield next(self.replies)
    service = RawService([bad_text, json.dumps(good) if recover else bad_text])
    if recover:
        report = await review_delivery(service, '整理正文', '正文', contract(), {})
        assert set(report) == {'checks', 'status', 'corrections'}
        assert [check['id'] for check in report['checks']] == [check['id'] for check in with_source_scope_check(contract())['acceptance']]
        assert validate_review(report, with_source_scope_check(contract()), '正文')['status'] == 'pass'
    else:
        with pytest.raises(OperationError, match='无法核验'):
            await review_delivery(service, '整理正文', '正文', contract(), {})
    assert len(service.calls) == 2
    schema = service.calls[0]['json_schema']['properties']['checks']
    assert schema['type'] == 'object' and schema['additionalProperties'] is False
    assert set(schema['properties']) == set(schema['required'])


@pytest.mark.parametrize('action,step_fields,strict,expected', [
    ('writer', {}, False, True), ('polisher', {}, False, True),
    ('writer', {'skill_step_id': 's1'}, True, False),
    ('writer', {'skill_step_id': 's1', 'skill_step_output_role': 'document'}, True, True),
    ('skill_step', {}, False, False), ('skill_step', {'skill_step_output_role': 'document'}, True, True),
    ('specialist', {}, False, False), ('specialist', {'skill_step_output_role': 'document'}, False, True),
    ('answer_writer', {'skill_step_output_role': 'document'}, False, False),
    ('patch_writer', {'skill_step_output_role': 'document'}, False, False),
    ('data_query_plan', {'skill_step_output_role': 'document'}, False, False),
    ('polisher', {'delivery_repair': True}, False, True)])
@pytest.mark.parametrize('mode', ['local', 'external'])
@pytest.mark.asyncio
async def test_title_output_instruction_matches_actual_local_and_external_step(action, step_fields, strict, expected, mode):
    service = DeliveryService()
    captured = []
    async def capture(**kwargs):
        captured.append(kwargs)
        raise RuntimeError('captured actual request')
    async def capture_stream(**kwargs):
        await capture(**kwargs)
        yield ''
    service.run_specialist_agent = capture
    service.stream_agent_document = capture_stream
    loop = CreationAgentLoop(service)
    state = loop._new_state(**run_args('# 用户标题\n\n用户正文。'), model_mode=mode)
    state.environment.update(document_identity={'title': '用户标题'}, strict_skill_workflow=strict,
        operation={'kind': 'generate', 'targets': []}, input_contract=contract(), delivery_review=review('revise'))
    step = {'kind': 'agent', 'id': 'output_probe', 'name': '输出步骤', 'action': action, **step_fields}
    async def execute():
        return [event async for event in loop._execute_step(state, step,
            creation_model=None, creation_api_key=None, creation_base_url=None)]
    if mode == 'local':
        with pytest.raises(RuntimeError, match='captured actual request'):
            await execute()
        assert len(captured) == 1
        prompt = captured[0]['user_prompt']
    else:
        events = await execute()
        request = next(event for event in events if event['type'] == 'model.request')
        prompt = request['data']['messages'][1]['content']
        assert prompt == state.pending_model_step['user_prompt']
        if step_fields.get('delivery_repair'):
            assert state.pending_model_step['step']['action'] == 'writer'
    assert ('完整文档必须沿用该标题' in prompt) is expected
    if action != 'data_query_plan':
        assert '文档身份（不是技能名称）' in prompt and '用户标题' in prompt


@pytest.mark.parametrize('identity', [False, True])
@pytest.mark.parametrize('retain_facts', [False, True])
@pytest.mark.asyncio
async def test_regeneration_replaces_failed_draft_and_reviews_real_short_output(identity, retain_facts):
    service = DeliveryService()
    output = '# 通知\n\n周三下午，二楼会议室。请准时参加。' if retain_facts else '# 通知\n\n请准时参加。'
    captured = []; checked = []
    async def write(**kwargs):
        captured.append(kwargs)
        yield output[:5]
        yield output[5:]
    async def assess(instruction, document, conditions, environment):
        checked.append(document)
        return validate_review(review('pass' if '周三下午' in document and '二楼会议室' in document else 'revise', document), conditions, document)
    service.stream_agent_document = write
    service.review_creation_delivery = assess
    loop = CreationAgentLoop(service)
    failed = '# 通知\n\n' + '\n\n'.join('无依据的第{}段说明需要删除。'.format(i) for i in range(60))
    state = loop._new_state(**run_args('', '会议在周三下午、二楼会议室。只用这些材料写通知。'), model_mode='local')
    state.current_document = failed; state.mode = 'revision'
    state.environment.update(document=failed, input_base_document='', operation={'kind': 'generate'},
        input_contract=contract(), delivery_review=review('revise'), delivery_repair_count=2,
        strict_skill_workflow=True, edit_intent={'operation': 'append_section', 'target_sections': ['旧局部']})
    if identity: state.environment['document_identity'] = {'title': '通知'}
    step = {'kind': 'agent', 'id': 'delivery_repair', 'name': '修正', 'action': 'polisher', 'delivery_repair': True}
    events = [event async for event in loop._execute_step(state, step, creation_model=None, creation_api_key=None, creation_base_url=None)]
    assert state.current_document == output and state.environment['document'] == output
    assert state.environment['strict_skill_document_polished'] is True
    assert state.environment['strict_skill_document_owned_by_agent'] == 'delivery_repair'
    assert not any(event['type'] in {'document.delta', 'document.patch.delta', 'document.mutation.rejected', 'document.mutation.salvaged'} for event in events)
    assert any(event['type'] == 'document.preview' and '无依据' not in event['data']['content'] for event in events)
    assert any(event['type'] == 'document.replaced' and event['data']['content'] == output for event in events)
    payload = json.JSONDecoder().raw_decode(captured[0]['user_prompt'])[0]
    assert failed not in captured[0]['user_prompt']
    assert not {'candidate_document', 'document', 'target_fragments', 'issue_catalog', 'dispositions'} & set(payload)
    assert payload['provided_materials']['user_instruction_and_supplied_facts'] == state.user_message
    async def check():
        return [event async for event in loop._execute_step(state, {'kind': 'agent', 'id': 'delivery_check', 'name': '验收', 'action': 'delivery_check'},
            creation_model=None, creation_api_key=None, creation_base_url=None)]
    if retain_facts: await check()
    else:
        with pytest.raises(OperationError, match='自动修正 2 次'):
            await check()
    assert checked == [output] and state.environment['delivery_review']['status'] == ('pass' if retain_facts else 'revise')


@pytest.mark.asyncio
async def test_regeneration_external_pending_freezes_sources_and_accepts_markdown_after_restore():
    from creation.agent_loop import LoopState
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args('', '周三下午、二楼会议室。写通知。'), model_mode='external')
    state.current_document = '# 通知\n\n未通过的长稿。'
    state.environment.update(document=state.current_document, operation={'kind': 'generate'}, input_contract=contract(), delivery_review=review('revise'))
    step = {'kind': 'agent', 'id': 'delivery_repair', 'name': '修正', 'action': 'polisher', 'delivery_repair': True}
    async for _ in loop._execute_step(state, step, creation_model=None, creation_api_key=None, creation_base_url=None): pass
    restored = LoopState.restore(state.serializable())
    saved = restored.pending_model_step['step']
    assert saved['action'] == 'writer' and saved['delivery_regenerate'] is True
    payload = json.loads(restored.pending_model_step['user_prompt'])
    assert payload == saved['delivery_regeneration_input']
    assert payload['provided_materials']['user_instruction_and_supplied_facts'] == state.user_message
    output = '# 通知\n\n周三下午，二楼会议室。'
    async for _ in loop._apply_model_result(restored, output): pass
    assert restored.current_document == output


@pytest.mark.asyncio
async def test_post_loop_completed_regeneration_rechecks_unchanged_valid_text():
    service = DeliveryService(reports=['pass']); loop = CreationAgentLoop(service)
    document = '# 通知\n\n周三下午，二楼会议室。'
    async def keep(**kwargs): yield document
    service.stream_agent_document = keep
    state = loop._new_state(**run_args('', '周三下午、二楼会议室。写通知。'), model_mode='local')
    state.current_document = document
    report = review('revise')
    state.environment.update(document=document, operation={'kind': 'generate'}, input_contract=contract(), delivery_review=report, delivery_repair_count=1)
    events = [event async for event in loop._repair_delivery(state, document, report, creation_model=None, creation_api_key=None, creation_base_url=None)]
    assert len(service.checked) == 1 and state.environment['delivery_review']['status'] == 'pass'
    assert any(event['type'] == 'delivery.rechecked' for event in events)
    assert state.current_document == document and state.environment['delivery_repair_count'] == 1


def test_regeneration_preserves_same_condition_multiple_findings_and_skill_rules():
    from creation.delivery_contract import remember_delivery_failures
    loop = CreationAgentLoop(DeliveryService()); state = loop._new_state(**run_args('', '写通知'), model_mode='local')
    state.environment.update(input_contract=contract(), delivery_review=review('revise'),
        applied_skills=[{'name': '明确规则', 'writing_design': ['每段仅一句。'], 'field_examples': {'example': '例子不能当事实'}}])
    findings = [{'id': 'a', 'reason': '关系未给定', 'evidence': '同事'}, {'id': 'a', 'reason': '职责未给定', 'evidence': '负责人'}]
    remember_delivery_failures(state.user_message, state.environment, contract(), findings, review('revise'))
    state.environment['delivery_repair_count'] = 1
    payload = loop._delivery_regeneration_input(state)
    assert {item['previous_quote'] for item in payload['previous_attempt_findings']} >= {'同事', '负责人'}
    assert payload['skill_constraints'][0]['writing_design'] == ['每段仅一句。']
    assert '例子不能当事实' not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("source", ["请先告诉我会议时间，再据此写一段可以直接发出的通知。", "会议定在周三下午。"])
@pytest.mark.parametrize("other_source", [False, True])
def test_calendar_source_binding_reads_actual_quote_not_lossy_meaning_or_other_source(source, other_source):
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    document = "本周三下午"
    report = audited_report(document, "source_supported", "neutral", "user_instruction_and_supplied_facts")
    next(iter(report["source_audit"].values()))["meaning"] = "会议定在周三下午"
    provided = {"user_instruction_and_supplied_facts": source}
    if other_source:
        provided['conversation'] = [{"role": "user", "content": "另一场活动在本周三下午。"}]
    original = copy.deepcopy(report)
    if other_source:
        with pytest.raises(OperationError, match='不能证明支持同一事件'):
            _apply_source_audit(report, source_audit_segments(document), provided, with_source_scope_check(contract()), document)
        assert report == original
        return
    result = _apply_source_audit(report, source_audit_segments(document), provided, with_source_scope_check(contract()), document)
    assert result["status"] == "revise" and report == original
    failed = next(check for check in result["checks"] if check["id"] == "source_attributes_grounding")
    assert not failed["passed"] and failed["evidence"] == document
    assert "所选来源 user_instruction_and_supplied_facts" in failed["reason"]
    assert "本周三" in "\n".join(result["corrections"])


@pytest.mark.parametrize("candidate,source", [
    ("2026年9月8日", "2026-09-08"), ("2026/9/8", "2026年09月08号"),
    ("2026年9月", "2026/09"), ("2026年第03周", "2026年第3周"),
    ("周三", "星期三"), ("周日", "星期天"), ("本周三", "这周三"),
    ("上周三", "上一星期三"), ("下个月", "下月"),
])
def test_calendar_normalization_keeps_explicit_same_value_and_precision(candidate, source):
    from creation.delivery_contract import _calendar_labels, _calendar_source_conflicts
    assert set(_calendar_labels(candidate).values()) == set(_calendar_labels(source).values())
    assert _calendar_source_conflicts(candidate, source) == []


@pytest.mark.parametrize("candidate,source", [
    ("本周三", "周三"), ("上周三", "周三"), ("下星期三", "星期三"),
    ("2026-09-08", "2026年9月"), ("2026-02-30", "2026-03-02"),
])
def test_calendar_normalization_never_equates_new_anchor_precision_or_invalid_date(candidate, source):
    from creation.delivery_contract import _calendar_labels
    assert set(_calendar_labels(candidate).values()) != set(_calendar_labels(source).values())


@pytest.mark.parametrize("basis,document,instruction", [
    ("authorized_creation", "下周三月亮城庆典开始。", "请写一篇虚构故事。"),
    ("authorized_creation", "建议本周三开展活动。", "请设计活动方案，提出可供选择的时间。"),
    ("neutral_expression", "待确认：是否安排在本周三？", "保留待确认问题。"),
])
def test_calendar_source_constraint_preserves_authorized_and_nonassertive_review_branches(basis, document, instruction):
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    report = audited_report(document, basis, "attribute", "user_instruction_and_supplied_facts")
    result = _apply_source_audit(report, source_audit_segments(document),
        {"user_instruction_and_supplied_facts": instruction}, with_source_scope_check(contract()), document)
    assert result["status"] == "pass"


def test_document_local_working_definition_is_an_authorized_creation_branch():
    from creation.delivery_contract import (
        FACT_GROUNDING_RULE, _apply_source_audit, source_audit_segments,
        with_source_scope_check,
    )
    document = "本文工作定义：A类指本文讨论的目标对象；官方规则与阈值待确认。"
    instruction = "增加A类的定义介绍"
    report = audited_report(
        document, "authorized_creation", "attribute",
        "user_instruction_and_supplied_facts",
    )
    result = _apply_source_audit(
        report, source_audit_segments(document),
        {"user_instruction_and_supplied_facts": instruction},
        with_source_scope_check(contract()), document,
    )
    assert result["status"] == "pass"
    assert "本文工作定义" in FACT_GROUNDING_RULE
    assert "不是平台官方分级标准" in FACT_GROUNDING_RULE
    assert "不能反向作为目标群体的定义" in FACT_GROUNDING_RULE


def test_transform_contract_cannot_expand_a_local_addition_to_adjacent_categories():
    from creation.delivery_contract import bind_transform_contract_scope
    original = contract(["work_context"])
    original["inputs"][0].update(
        need="A类定义以及与B类、C类的区别",
        query="检索A类、B类、C类的正式标准",
    )
    original["acceptance"] = [{
        "id": "expanded",
        "criterion": "新增A类定义，并与B类、C类进行比较。",
    }]
    result = bind_transform_contract_scope(original, "增加A类的定义介绍", "transform")
    assert [item["id"] for item in result["acceptance"]] == [
        "transform_instruction", "transform_preserve", "working_definition_boundary",
    ]
    assert "B类" not in json.dumps(result, ensure_ascii=False)
    assert "增加A类的定义介绍" in json.dumps(result, ensure_ascii=False)
    assert result["inputs"] == []
    assert "本文工作定义" in result["self_contained_reason"]
    assert original["acceptance"][0]["id"] == "expanded"

    official = bind_transform_contract_scope(
        original, "增加A类的官方正式分级定义", "transform"
    )
    assert official["inputs"][0]["state"] == "missing"
    assert official["inputs"][0]["query"] == "增加A类的官方正式分级定义"


def test_bare_definition_request_gets_safe_deterministic_working_fragment():
    from creation.delivery_contract import minimal_working_definition_fragment
    fragment = minimal_working_definition_fragment("增加灵机L0商家的定义介绍")
    assert fragment.startswith("### 灵机L0商家定义")
    assert "不代表平台官方分级标准" in fragment
    assert "不以规模、人设、资产、资质、能力或行为特征" in fragment
    assert minimal_working_definition_fragment(
        "增加灵机L0商家的官方正式分级定义"
    ) == ""
    assert minimal_working_definition_fragment(
        "增加灵机L0商家的定义：指年GMV低于某阈值的商家"
    ) == ""


def test_bare_definition_requirement_survives_legacy_generate_routing():
    from creation.delivery_contract import (with_bare_working_definition_safety,
                                             with_brainstorm_open_flag_safety)
    conditions = with_brainstorm_open_flag_safety(
        contract(), ["L0 商家的官方准入阈值是什么？"], "generate",
        "增加灵机L0商家的定义介绍",
    )
    conditions = with_bare_working_definition_safety(
        conditions, "增加灵机L0商家的定义介绍"
    )
    checks = {item["id"]: item["criterion"] for item in conditions["acceptance"]}
    assert "working_definition_boundary" in checks
    assert "正文必须实际包含" in checks["working_definition_boundary"]
    assert "不降低或替代本条件" in checks["a"]

    legacy = copy.deepcopy(conditions)
    legacy["acceptance"][0]["criterion"] = (
        "覆盖本轮需求 对结构化 open_flags，本条件只核对它们仍明确标注为待确认；"
        "用户未提交答案时，原样保留即满足，不得要求提出、选择或补写答案，"
        "也不得因未代用户回答而判失败。"
    )
    migrated = with_brainstorm_open_flag_safety(
        legacy, ["仍待确认"], "generate", "增加灵机L0商家的定义介绍"
    )
    assert "本条件只核对" not in migrated["acceptance"][0]["criterion"]
    assert "不降低或替代本条件" in migrated["acceptance"][0]["criterion"]


def test_delivery_gate_inserts_missing_working_definition_without_copying_body():
    document = (
        "# 快手灵机方案\n\n## 范围与现实约束\n\n保留原有方案正文。\n\n"
        "## 待确认事项\n\n- 官方准入阈值待确认\n"
    )
    updated, changed = CreationAgentLoop._ensure_bare_working_definition(
        document, "增加灵机L0商家的定义介绍"
    )
    assert changed is True
    assert updated.count("# 快手灵机方案") == 1
    assert updated.count("保留原有方案正文。") == 1
    assert "### 灵机L0商家定义" in updated
    assert "**本文工作定义**" in updated
    assert updated.index("### 灵机L0商家定义") < updated.index("## 待确认事项")
    repeated, changed_again = CreationAgentLoop._ensure_bare_working_definition(
        updated, "增加灵机L0商家的定义介绍"
    )
    assert changed_again is False
    assert repeated == updated

    misplaced = document + "\n### 灵机L0商家定义\n\n**本文工作定义**：已有定义。\n"
    relocated, moved = CreationAgentLoop._ensure_bare_working_definition(
        misplaced, "增加灵机L0商家的定义介绍"
    )
    assert moved is True
    assert relocated.count("### 灵机L0商家定义") == 1
    assert relocated.index("### 灵机L0商家定义") < relocated.index("## 待确认事项")
    assert relocated.count("保留原有方案正文。") == 1


def test_calendar_conversion_is_not_code_support_and_cannot_clear_independent_failure():
    from creation.delivery_contract import _apply_source_audit, source_audit_segments, with_source_scope_check
    document = "本周三下午"
    report = audited_report(document, "source_supported", "attribute", "user_instruction_and_supplied_facts")
    report["status"] = "revise"
    report["checks"].append({"id": "calendar_grounding", "passed": False,
        "reason": "存在日期仍未证明本次事件的换算关系", "evidence": document})
    conditions = with_source_scope_check(contract())
    conditions["acceptance"].append({"id": "calendar_grounding", "criterion": "必须核对真实基准与换算授权"})
    result = _apply_source_audit(report, source_audit_segments(document),
        {"user_instruction_and_supplied_facts": "另一份记录日期为2026-09-08。"}, conditions, document)
    assert result["status"] == "revise"
    assert result["checks"][-1]["reason"] == "存在日期仍未证明本次事件的换算关系"


@pytest.mark.asyncio
@pytest.mark.parametrize('document', ['正文', ' \n\n'])
async def test_review_decoding_couples_pass_with_actual_nonempty_line(document):
    service = StreamService([json.dumps(review_for_model(status='revise'))] * 2)
    if document.strip():
        result = await review_delivery(service, '整理正文', document, contract(), {})
        assert result['status'] == 'revise'
    else:
        # A whitespace-only candidate cannot furnish a nonempty audit quote;
        # its decoding schema must also provide no route to a passed check.
        with pytest.raises(OperationError, match='无法核验'):
            await review_delivery(service, '整理正文', document, contract(), {})
    checks = service.calls[0]['json_schema']['properties']['checks']['properties']
    for key, item in checks.items():
        branches = item['oneOf']
        assert len(branches) == (2 if document.strip() else 1)
        for branch in branches:
            props = branch['properties']
            if props['passed']['const']:
                assert props['evidence']['enum'] == ['line-1']
            else:
                assert props['evidence']['enum'] == (
                    (['line-1'] if key.startswith('source_') else ['', 'line-1']) if document.strip() else [''])
            assert branch['required'] == ['evidence', 'reason', 'passed']


@pytest.mark.asyncio
@pytest.mark.parametrize('recover', [True, False])
async def test_pass_without_evidence_still_fails_closed_or_repairs_once(recover):
    good = review_for_model(document='line-1')
    bad = copy.deepcopy(good)
    bad['checks'][0]['evidence'] = ''
    service = StreamService([json.dumps(bad), json.dumps(good if recover else bad)])
    if recover:
        assert (await review_delivery(service, '整理正文', '正文', contract(), {}))['status'] == 'pass'
    else:
        with pytest.raises(OperationError, match='无法核验'):
            await review_delivery(service, '整理正文', '正文', contract(), {})
    assert len(service.calls) == 2


@pytest.mark.parametrize('candidate,source', [
    ('明天', '明日'), ('昨天', '昨日'), ('2026年9月8日', '2026.09.08'),
])
def test_common_relative_aliases_and_numeric_date_separators_are_equivalent(candidate, source):
    from creation.delivery_contract import _calendar_labels, _calendar_source_conflicts
    assert set(_calendar_labels(candidate).values()) == set(_calendar_labels(source).values())
    assert _calendar_source_conflicts(candidate, source) == []


@pytest.mark.parametrize('source', [
    '会议在九月八日举行。', '会议在9月8日举行。', '会议在08/09/2026举行。',
    'The meeting is tomorrow.', 'The meeting is on 8 September.',
    '会议在秋季举行。', '会议定于周三，即9月8日。',
])
def test_unparsed_calendar_clue_is_uncertainty_not_absence_or_source_proof(source):
    from creation.delivery_contract import _calendar_source_conflicts, _has_unparsed_calendar_information
    assert _has_unparsed_calendar_information(source)
    assert _calendar_source_conflicts('本周三下午举行会议。', source) == []


def test_calendar_clue_probe_does_not_turn_action_request_or_time_of_day_into_week_baseline():
    from creation.delivery_contract import _calendar_source_conflicts, _has_unparsed_calendar_information
    instruction = '请先告诉我会议时间，再据此写一段可以直接发出的通知。'
    assert not _has_unparsed_calendar_information(instruction)
    assert _calendar_source_conflicts('本周三下午', instruction) == ['本周三']
    assert _calendar_source_conflicts('本周三下午', '会议定在周三下午，地点二楼会议室。') == ['本周三']


@pytest.mark.parametrize('source', ['会议明早举行。', '会议昨晚举行。', '会议翌晨举行。', '会议次日清晨举行。'])
def test_unparsed_relative_day_and_daypart_do_not_mean_no_calendar_information(source):
    from creation.delivery_contract import _calendar_source_conflicts, _has_unparsed_calendar_information
    assert _has_unparsed_calendar_information(source)
    assert _calendar_source_conflicts('会议明天上午举行。', source) == []


def _v22_review_record(identity):
    from pathlib import Path
    records = json.loads((Path(__file__).parent / 'fixtures' / 'creation_review_consistency_v22.json').read_text())
    return next(item for item in records if item['id'] == identity)


@pytest.mark.parametrize('identity', ['accept-explicit-audience-option', 'accept-authorized-fictional-relationships'])
@pytest.mark.parametrize('recover', [False, True])
@pytest.mark.asyncio
async def test_real_v22_source_or_consistency_mismatch_uses_original_two_attempts(identity, recover):
    record = _v22_review_record(identity)
    first = record['raw_review']
    repaired = copy.deepcopy(first)
    if identity == 'accept-explicit-audience-option':
        for claim in repaired['source_audit'].values():
            if '周三' in claim['text']:
                claim['source_id'] = 'conversation-user-0'
    elif identity == 'accept-authorized-fictional-relationships':
        for check in repaired['checks'].values():
            check.update(passed=True, reason='用户明确要求虚构故事，所述关系和情节处于该授权范围。')
        repaired['status'] = 'pass'
    replies = [json.dumps(first), json.dumps(repaired if recover else first)]
    unresolved_conflict = identity == 'accept-authorized-fictional-relationships' and not recover
    if unresolved_conflict:
        replies.extend([json.dumps({'resolutions': {}})] * 2)
    service = StreamService(replies)
    supplied = record['provided_materials']
    environment = {'input_context': {'conversation': supplied['conversation'], 'user_options': supplied['user_options']}}
    # Use the original user acceptance; the production call attaches the same
    # three independent system criteria, with no fixture-generated pass filling.
    conditions = {**record['contract'], 'acceptance': record['contract']['acceptance'][3:]}
    if recover:
        result = await review_delivery(service, supplied['user_instruction_and_supplied_facts'], record['candidate_document'], conditions, environment)
        assert result['status'] == 'pass'
    elif identity == 'accept-explicit-audience-option':
        result = await review_delivery(service, supplied['user_instruction_and_supplied_facts'], record['candidate_document'], conditions, environment)
        assert result['status'] == 'revise'
        assert any('同一事件' in item for item in result['corrections'])
    else:
        with pytest.raises(OperationError, match='无法核验'):
            await review_delivery(service, supplied['user_instruction_and_supplied_facts'], record['candidate_document'], conditions, environment)
    assert len(service.calls) == (4 if unresolved_conflict else 2)
    assert '上次验收格式校验失败' in service.calls[1]['user_prompt']
    if identity == 'accept-explicit-audience-option':
        assert 'conversation-user-0' in service.calls[1]['user_prompt']
        assert '不能证明支持同一事件' in service.calls[1]['user_prompt']
    else:
        assert '来源审计没有同类负面片段' in service.calls[1]['user_prompt']


@pytest.mark.asyncio
async def test_repeated_wrong_calendar_source_becomes_repairable_uncertainty_not_terminal_error():
    document = '当前支持599个音色。音色库更新于2026-09-12。'
    conditions = contract()
    first = audited_report(document, 'source_supported', 'attribute',
                           'user_instruction_and_supplied_facts')
    first['checks'] = {check['id']: {
        key: ('line-1' if key == 'evidence' else value)
        for key, value in check.items() if key != 'id'
    } for check in first['checks']}
    provided = {
        'input_context': {},
        'data_results': [{
            'can_use': True,
            'content_excerpt': '系统当前支持599个音色，音色库更新于2026-09-12。',
        }],
    }
    service = StreamService([json.dumps(first), json.dumps(first)])

    result = await review_delivery(service, '整理现有材料', document,
                                   conditions, provided)

    assert result['status'] == 'revise'
    assert len(service.calls) == 2
    assert any('来源支持尚未核验' in item for item in result['corrections'])
    assert any('同一事件' in item for item in result['corrections'])


@pytest.mark.asyncio
async def test_real_v22_consistent_rejection_does_not_turn_wrong_source_into_date_correction():
    record = _v22_review_record('reject-unprovided-audience')
    service = StreamService([json.dumps(record['raw_review'])])
    supplied = record['provided_materials']
    result = await review_delivery(service, supplied['user_instruction_and_supplied_facts'], record['candidate_document'],
        {**record['contract'], 'acceptance': record['contract']['acceptance'][3:]},
        {'input_context': {'conversation': supplied['conversation'], 'user_options': supplied['user_options']}})
    assert result['status'] == 'revise' and len(service.calls) == 1
    assert next(check for check in result['checks'] if check['id'] == 'source_scope_grounding')['passed'] is False
    assert next(check for check in result['checks'] if check['id'] == 'source_attributes_grounding')['passed'] is True
    assert '所选来源' not in '\n'.join(result['corrections'])


@pytest.mark.asyncio
async def test_real_negative_audit_preserves_independent_event_failure_without_rejudging_rejection():
    from creation.delivery_contract import _review_delivery_batch
    record = _v22_review_record('reject-unprovided-event-process')
    # The audit misses the meeting-nature qualifier, but independently finds
    # an unsupported reporting procedure. Both discovered failures must survive.
    claims = record['raw_review']['source_audit'].values()
    assert all(claim['basis'] not in {'unsupported', 'reasonable_inference'}
        for claim in claims if claim['kind'] == 'attribute')
    assert any(claim['kind'] == 'obligation' and claim['basis'] == 'unsupported' for claim in claims)
    service = StreamService([json.dumps(record['raw_review'])])
    supplied = record['provided_materials']
    # This recorded response tests one batch's rejection semantics. Source-batch
    # partitioning is independently exercised against current dynamic schemas.
    result = await _review_delivery_batch(service, supplied['user_instruction_and_supplied_facts'], record['candidate_document'],
        {**record['contract'], 'acceptance': record['contract']['acceptance'][3:]},
        {'input_context': {'conversation': supplied['conversation'], 'user_options': supplied['user_options']}})
    assert len(service.calls) == 1 and result['status'] == 'revise'
    checks = {check['id']: check for check in result['checks']}
    assert not checks['source_attributes_grounding']['passed']
    assert not checks['source_obligations_grounding']['passed']
    assert '例会' in checks['source_attributes_grounding']['reason']
    assert '例会' in '\n'.join(result['corrections']) and '报备' in '\n'.join(result['corrections'])
