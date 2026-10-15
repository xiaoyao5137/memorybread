"""Scope, evidence, configuration, and failure regressions for work review."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from creation import work_quality
from creation.operations import OperationError, document_hash
from creation.work_quality import (load_work_quality_profiles, normalize_work_context,
    review_work_document, work_acceptance_checks, work_context_guidance)


DOCUMENT = "# 方案\n\n建议先在现有输入范围开展试验，依据实际结果决定是否扩大。\n比较试验前后的实际指标，指标口径由相关人员确认。"
CONTEXT = {"review_enabled": True, "profile_id": "proposal_review", "purpose": "方案评审",
    "decision_goal": "是否开始试验", "constraints": "不增加已确认范围外的栏目"}


def response(profile_id="proposal_review", **overrides):
    profile = next(item for item in load_work_quality_profiles()["profiles"] if item["id"] == profile_id)
    rows = {key: {"reason": "本轮实际内容已体现该维度，未新增全篇要求。", "passed": True,
        "evidence_line": "line-3", "correction": ""} for key in profile["criteria"]}
    for key, value in overrides.items():
        rows[key].update(value)
    return rows


class ReviewService:
    def __init__(self, result=None, error=None, delay=0):
        self.result = result if result is not None else response()
        self.error, self.delay = error, delay
        self.calls = []

    async def _stream_direct_completion(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        yield self.result if isinstance(self.result, str) else json.dumps(self.result, ensure_ascii=False)


def test_default_context_is_disabled_and_does_not_add_obligations():
    context = normalize_work_context(None)
    assert context == {"schema_version": "creation.work-context.v1", "purpose": "", "decision_goal": "",
        "reader_context": "", "constraints": "", "style_samples": [], "profile_id": "general", "review_enabled": False}
    assert work_context_guidance(context) == ""
    assert work_acceptance_checks(context) == []


@pytest.mark.parametrize("value", [[], "proposal_review", {"unknown": True},
    {"schema_version": "v2"}, {"review_enabled": "true"}, {"review_enabled": 1},
    {"purpose": []}, {"decision_goal": "字" * 1501}, {"constraints": None},
    {"style_samples": "范文"}, {"style_samples": [""]}, {"style_samples": ["a"] * 4},
    {"style_samples": ["a" * 1501]}, {"profile_id": "unknown"}])
def test_invalid_user_context_is_not_silently_truncated_or_defaulted(value):
    with pytest.raises(OperationError) as error:
        normalize_work_context(value)
    assert error.value.code == "CREATION_WORK_CONTEXT_INVALID"


def test_samples_and_inputs_are_preserved_without_mutating_user_data():
    value = {**CONTEXT, "style_samples": ["  先说明变化，再说需要协调什么。  "]}
    original = copy.deepcopy(value)
    result = normalize_work_context(value)
    assert value == original
    assert result["style_samples"] == ["先说明变化，再说需要协调什么。"]
    guidance = work_context_guidance(result)
    assert "不是事实材料" in guidance
    assert "不得复制" in guidance
    assert "待确认假设" in guidance
    assert "不以缺少精确数字为由拒绝设计" in guidance


def test_profiles_are_configured_and_default_uses_only_three_dimensions():
    general = work_acceptance_checks({"review_enabled": True})
    assert {item["id"] for item in general} == {
        "work_natural_expression", "work_information_increment", "work_assumption_distinction"}
    assert len(work_acceptance_checks(CONTEXT)) == 5
    assert all("业务事实" in item["criterion"] for item in general)
    local = work_acceptance_checks(CONTEXT, {"scope": "chapter"})
    assert all("不要求其他章节、全篇摘要" in item["criterion"] for item in local)


def test_profiles_independently_check_facts_before_subjective_quality_and_use_task_granularity():
    config = load_work_quality_profiles()
    assert all(item["criteria"][0] == "assumption_distinction" for item in config["profiles"])
    concrete = next(item["criterion"] for item in config["criteria"] if item["id"] == "actionable_specificity")
    assert "purpose与decision_goal" in concrete
    assert "阅读全文" in concrete
    assert "只有原请求明确要求实施规范" in concrete
    assert "X天等占位参数" in concrete


@pytest.mark.asyncio
async def test_pass_is_bound_to_current_body_and_one_read_only_model_call():
    service = ReviewService()
    context = copy.deepcopy(CONTEXT)
    report = await review_work_document(service, "写方案", DOCUMENT, context)
    assert report["status"] == "pass"
    assert report["issues"] == []
    assert report["document_hash"] == document_hash(DOCUMENT)
    assert all(item["evidence"] in DOCUMENT for item in report["checks"])
    assert {item["id"] for item in report["checks"]} == {item["id"] for item in work_acceptance_checks(context)}
    assert len(service.calls) == 1
    assert context == CONTEXT
    assert "effective_contract" not in json.loads(service.calls[0]["user_prompt"])


@pytest.mark.asyncio
async def test_actual_strict_fact_table_contract_does_not_acquire_decision_columns():
    document = "# 事实核对\n\n| 事实 | 来源 |\n| --- | --- |\n| 素材需求通过群消息交接。 | 唯一给定事实 |"
    contract = {"deliverable": "仅输出已给定事实核对表，保留事实与来源两列。",
        "operation_kind": "execute_skill", "strict_skill_workflow": True,
        "strict_skill_ids": ["fact-table"],
        "acceptance": [{"id": "facts_only", "criterion": "只写既有事实，不增加建议栏目。"},
            {"id": "work_decision_help", "criterion": "不应成为其自身范围授权"}],
        "skill_constraints": [{"id": "fact-table", "name": "事实核对表", "workflow_role": "primary",
            "strict_structure": True, "skill_instructions": "仅两列：事实、来源。禁止扩列或新增方案建议。",
            "execution_steps": [{"id": "table", "title": "事实核对", "objective": "保留给定事实的原始强度",
                "output": "事实与来源两列表格", "output_role": "document"}]}]}
    original = copy.deepcopy(contract)
    service = ReviewService(response(decision_help={"reason": "本轮严格事实表未授权判断建议，该维度按当前范围满足。"},
        actionable_specificity={"reason": "本轮只要求事实核对，不承担实施安排。"}))
    report = await review_work_document(service, "按已采用 Skill 仅输出事实表", document, CONTEXT,
        effective_contract=contract)
    assert report["status"] == "pass"
    assert report["issues"] == []
    assert contract == original
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["effective_contract"]["skill_constraints"] == contract["skill_constraints"]
    assert [item["id"] for item in payload["effective_contract"]["acceptance"]] == ["facts_only"]
    prompt = service.calls[0]["system_prompt"]
    assert "不增加建议栏目" in payload["effective_contract"]["acceptance"][0]["criterion"]
    assert "不得为了决策帮助或可执行性增加白名单外" in prompt
    assert "不能因未写建议判失败" in prompt
    assert all(check["evidence"] in document for check in report["checks"])


@pytest.mark.asyncio
async def test_effective_contract_projects_only_actual_rules_and_support_does_not_expand_workflow():
    marker = "NOT_AN_AUTHORIZED_SOURCE_OR_SKILL"
    contract = {"deliverable": "仅完成本轮已给定内容", "operation_kind": "transform",
        "strict_skill_workflow": False, "strict_skill_ids": [],
        "selected_skills": [{"id": marker}], "source_materials": marker,
        "acceptance": [{"id": "preserve", "criterion": "保留当前边界", "evidence": marker}],
        "skill_constraints": [{"id": 7, "name": "核对能力", "workflow_role": "support",
            "strict_structure": True, "skill_instructions": "仅核对当前事实强度。",
            "voice_style": [marker], "field_examples": {"example": marker}, "example_document": marker,
            "execution_steps": [{"id": 1, "title": "核对", "objective": "核对当前正文",
                "output": "核对结果", "output_role": "process", "query": marker, "tools": [marker]}]}]}
    service = ReviewService()
    report = await review_work_document(service, "仅改当前表达", DOCUMENT, CONTEXT,
        effective_contract=contract)
    assert report["status"] == "pass"
    supplied = service.calls[0]["user_prompt"]
    assert marker not in supplied
    payload = json.loads(supplied)["effective_contract"]
    assert payload["strict_skill_ids"] == []
    assert payload["skill_constraints"][0]["id"] == "7"
    assert payload["skill_constraints"][0]["execution_steps"][0]["id"] == "1"
    assert "support Skill步骤只是本轮既有职责中的能力参考" in service.calls[0]["system_prompt"]
    assert "不是事实材料" in service.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_explicit_empty_adopted_skill_projection_never_falls_back_to_candidates():
    service = ReviewService()
    await review_work_document(service, "本轮未采用 Skill", DOCUMENT, CONTEXT,
        effective_contract={"skill_constraints": [], "strict_skill_ids": [],
            "selected_skills": [{"id": "candidate-only", "skill_instructions": "新增判断栏目"}]})
    contract = json.loads(service.calls[0]["user_prompt"])["effective_contract"]
    assert contract == {"skill_constraints": [], "strict_skill_ids": []}
    assert "candidate-only" not in service.calls[0]["user_prompt"]


@pytest.mark.parametrize("contract", ["not-a-contract", {"deliverable": []},
    {"strict_skill_workflow": "true"}, {"strict_skill_ids": "candidate"},
    {"acceptance": [{"id": "a", "criterion": None}]}, {"skill_constraints": [{}], "acceptance": {}},
    {"skill_constraints": [{"strict_structure": "true"}]},
    {"skill_constraints": [{"execution_steps": [{"output": ["invalid"]}]}]}])
@pytest.mark.asyncio
async def test_malformed_actual_contract_is_unverified_before_model_request(contract):
    service = ReviewService()
    report = await review_work_document(service, "本轮实际契约", DOCUMENT, CONTEXT, effective_contract=contract)
    assert report["status"] == "unverified"
    assert report["document_hash"] == document_hash(DOCUMENT)
    assert service.calls == []


@pytest.mark.asyncio
async def test_effective_contract_counts_toward_budget_without_silent_truncation():
    service = ReviewService()
    report = await review_work_document(service, "按原结构", DOCUMENT, CONTEXT,
        effective_contract={"skill_constraints": [{"skill_instructions": "真实约束" * 10000}]})
    assert report["status"] == "unverified"
    assert "输入预算" in report["issues"][0]["reason"]
    assert service.calls == []


@pytest.mark.asyncio
async def test_specific_failure_has_stable_code_exact_quote_and_targeted_correction():
    service = ReviewService(response(actionable_specificity={"passed": False,
        "reason": "验证说明未说明比较哪个结果，读者无法执行。", "evidence_line": "line-4",
        "correction": "在已有材料范围说明试验结果的比较对象与观察方式，不补造阈值。"}))
    report = await review_work_document(service, "写方案", DOCUMENT, CONTEXT)
    assert report["status"] == "revise"
    issue = report["issues"][0]
    assert issue["code"] == "work_actionable_specificity"
    assert issue["severity"] == "warning"
    assert issue["quote"] == DOCUMENT.splitlines()[3]
    assert "不补造阈值" in issue["correction"]


@pytest.mark.asyncio
async def test_missing_whole_scope_item_may_have_no_existing_quote():
    service = ReviewService(response(decision_help={"passed": False, "evidence_line": "",
        "reason": "本轮要求的关键判断没有在整篇给出。", "correction": "依据已提供材料说明需要决定的试验范围。"}))
    report = await review_work_document(service, "明确试验范围判断", DOCUMENT, CONTEXT)
    assert report["status"] == "revise"
    assert report["issues"][0]["quote"] == ""


@pytest.mark.parametrize("mutate", [
    lambda raw: raw.pop("natural_expression"),
    lambda raw: raw.update({"unexpected": raw["natural_expression"]}),
    lambda raw: raw["natural_expression"].update({"evidence_line": "invented quote"}),
    lambda raw: raw["natural_expression"].update({"evidence_line": ""}),
    lambda raw: raw["natural_expression"].update({"passed": "true"}),
    lambda raw: raw["natural_expression"].update({"passed": False, "correction": ""}),
    lambda raw: raw["natural_expression"].update({"correction": "仍需修复"}),
    lambda raw: raw["natural_expression"].update({"reason": ""}),
])
@pytest.mark.asyncio
async def test_invalid_response_is_explicitly_unverified_without_retry(mutate):
    raw = response()
    mutate(raw)
    service = ReviewService(raw)
    report = await review_work_document(service, "写方案", DOCUMENT, CONTEXT)
    assert report["status"] == "unverified"
    assert report["checks"] == []
    assert report["issues"][0]["code"] == "work_review_unverified"
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_duplicate_model_fields_cannot_overwrite_a_failure_into_pass():
    encoded = json.dumps(response())
    duplicate = encoded[:-1] + ', "natural_expression": ' + json.dumps(response()["natural_expression"]) + "}"
    report = await review_work_document(ReviewService(duplicate), "写方案", DOCUMENT, CONTEXT)
    assert report["status"] == "unverified"


@pytest.mark.asyncio
async def test_transport_error_is_redacted_and_does_not_claim_pass():
    service = ReviewService(error=RuntimeError("PRIVATE_PROMPT_AND_KEY"))
    report = await review_work_document(service, "写方案", DOCUMENT, CONTEXT)
    assert report["status"] == "unverified"
    assert "PRIVATE_PROMPT_AND_KEY" not in json.dumps(report)
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_timeout_does_not_add_a_second_request(monkeypatch):
    monkeypatch.setattr(work_quality, "REVIEW_TIMEOUT_SECONDS", 0.005)
    service = ReviewService(delay=1)
    report = await review_work_document(service, "写方案", DOCUMENT, CONTEXT)
    assert report["status"] == "unverified"
    assert "超时" in report["issues"][0]["reason"]
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed():
    with pytest.raises(asyncio.CancelledError):
        await review_work_document(ReviewService(error=asyncio.CancelledError()), "写方案", DOCUMENT, CONTEXT)


@pytest.mark.asyncio
async def test_disabled_review_does_not_call_model_or_claim_pass():
    service = ReviewService()
    report = await review_work_document(service, "写方案", DOCUMENT, None)
    assert report["status"] == "unverified"
    assert service.calls == []


@pytest.mark.asyncio
async def test_exact_chapter_scope_excludes_previous_and_future_chapters():
    base = "# 已完成\n\n此前正文必须保持。\n\n"
    chapter = "## 当前章\n\n建议先试验，依据实际结果调整。"
    service = ReviewService(response())
    report = await review_work_document(service, "只追加当前章", base + chapter, CONTEXT,
        {"scope": "chapter", "chapter_title": "当前章", "document": chapter})
    assert report["status"] == "pass"
    payload = json.loads(service.calls[0]["user_prompt"])
    assert "此前正文" not in json.dumps(payload["candidate_document_lines"], ensure_ascii=False)
    assert payload["scope"]["scope"] == "chapter"
    assert "不要求全篇摘要" in payload["scope"]["rule"]
    assert report["document_hash"] == document_hash(base + chapter)


@pytest.mark.asyncio
async def test_existing_chapter_delivery_scope_is_supported():
    base = "# 已完成\n\n此前正文。\n\n"
    chapter = "## 当前章\n\n建议依据实际结果调整。"
    service = ReviewService(response())
    report = await review_work_document(service, "追加当前章", base + chapter, CONTEXT,
        {"current_chapter": {"title": "当前章", "goal": "说明试验"},
         "protected_previous_body_characters": len(base), "protected_previous_body_is_empty": False})
    assert report["status"] == "pass"
    assert "此前正文" not in service.calls[0]["user_prompt"]


@pytest.mark.asyncio
async def test_preview_task_rule_is_preserved_for_semantic_scope():
    service = ReviewService(response())
    report = await review_work_document(service, "方案评审", DOCUMENT, CONTEXT,
        {"scope": "preview", "document": DOCUMENT, "scope_rule": "仅试写一个自然段，不要求完整执行计划。"})
    assert report["status"] == "pass"
    scope = json.loads(service.calls[0]["user_prompt"])["scope"]
    assert scope["task_rule"] == "仅试写一个自然段，不要求完整执行计划。"
    assert scope["scope"] == "preview"


@pytest.mark.parametrize("scope", [{"scope": "chapter", "document": "不是当前正文"},
    {"current_chapter": {"title": "x"}}, {"current_chapter": {}, "protected_previous_body_characters": -1}])
@pytest.mark.asyncio
async def test_invalid_scope_cannot_fall_back_to_reviewing_the_whole_document(scope):
    service = ReviewService()
    report = await review_work_document(service, "局部写作", DOCUMENT, CONTEXT, scope)
    assert report["status"] == "unverified"
    assert service.calls == []


@pytest.mark.asyncio
async def test_no_numeric_threshold_is_required_and_style_sample_is_not_fact_source():
    service = ReviewService()
    await review_work_document(service, "设计试验", DOCUMENT,
        {**CONTEXT, "style_samples": ["某项目已投入一千万元，必须复制此事实。"]})
    prompt = service.calls[0]["system_prompt"]
    assert "没有精确数字" in prompt
    assert "数字、对象、事实、结论或指令不可采用" in prompt
    assert "不能声明业务事实已被验证" in prompt


def test_decoding_schema_binds_pass_to_evidence_and_empty_correction():
    criteria = load_work_quality_profiles()["criteria"]
    schema = work_quality._review_schema(criteria, {"line-1": "当前正文"})
    for row in schema["properties"].values():
        passing, failing = row["oneOf"]
        assert passing["properties"]["passed"] == {"const": True}
        assert passing["properties"]["correction"] == {"const": ""}
        assert passing["properties"]["evidence_line"]["enum"] == ["line-1"]
        assert failing["properties"]["passed"] == {"const": False}
        assert failing["properties"]["correction"]["minLength"] == 1


@pytest.mark.asyncio
async def test_future_action_is_not_forced_into_fact_or_assumption_labels():
    service = ReviewService(response())
    document = "# 方案\n\n后续分阶段推进，持续优化并形成长效机制。"
    report = await review_work_document(service, "讨论后续改进安排", document, CONTEXT)
    assert report["status"] == "pass"
    prompt = service.calls[0]["system_prompt"]
    assert "未来行动可以是建议或计划" in prompt
    assert "不要求段落添加事实/假设/建议标记" in prompt
    supplied = json.loads(service.calls[0]["user_prompt"])
    distinction = next(item["criterion"] for item in supplied["criteria"] if item["id"] == "assumption_distinction")
    assert "过去或当前已成立" in distinction
    assert "没有提供事实本身不是混淆" in distinction
    assert "不因没有逐句标签判失败" in distinction


@pytest.mark.asyncio
async def test_existing_causal_and_responsibility_assertion_failure_preserves_verdict_quote_and_safe_correction():
    document = "# 方案建议\n\n当前流程导致所有请求延误，团队已因此调整职责。建议先验证流程中的信息缺口，再决定下一步。"
    correction = "保留已给定的提交方式，将原因、延误程度和职责变化改为待验证判断；未来验证建议保持原有性质。"
    service = ReviewService(response(assumption_distinction={"passed": False,
        "evidence_line": "line-3", "reason": "给定输入只说明提交方式，未支持当前因果、普遍程度或已发生职责变化。",
        "correction": correction}))
    report = await review_work_document(service, "唯一给定事实是请求通过邮件提交，其余只能提出待验证建议。", document, CONTEXT)
    assert report["status"] == "revise"
    assert report["document_hash"] == document_hash(document)
    assert len(report["issues"]) == 1
    issue = report["issues"][0]
    assert issue["code"] == "work_assumption_distinction"
    assert issue["quote"] == document.splitlines()[2]
    assert issue["correction"] == correction
    assert next(item for item in report["checks"] if item["id"] == issue["code"])["passed"] is False
    assert all(item["passed"] for item in report["checks"] if item["id"] != issue["code"])
    assert len(service.calls) == 1
    assert "逐句核对当前因果、痛点程度、已有职责或缺陷" in service.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_only_future_design_has_no_fact_failure_after_the_stronger_existing_assertion_rule():
    document = "# 方案\n\n建议先验证信息是否完整；若发现信息缺口，再讨论调整交接职责与记录方式。"
    service = ReviewService(response())
    report = await review_work_document(service, "提出一个验证设计，尚未形成既有结论或制度。", document, CONTEXT)
    assert report["status"] == "pass"
    assert report["issues"] == []
    assert report["document_hash"] == document_hash(document)
    assert all(item["passed"] and item["evidence"] in document for item in report["checks"])
    assert len(service.calls) == 1
    assert "未来行动可以是建议或计划，不是已完成结果" in service.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_review_budget_does_not_silently_drop_the_tail():
    service = ReviewService()
    document = "字" * (work_quality.MAX_REVIEW_DOCUMENT_CHARS + 1)
    report = await review_work_document(service, "写方案", document, CONTEXT)
    assert report["status"] == "unverified"
    assert "未截断正文" in report["issues"][0]["reason"]
    assert service.calls == []


@pytest.mark.asyncio
async def test_total_input_budget_accounts_for_instruction_and_samples():
    service = ReviewService()
    report = await review_work_document(service, "字" * 36000, DOCUMENT,
        {**CONTEXT, "style_samples": ["表达" * 700]})
    assert report["status"] == "unverified"
    assert "输入预算" in report["issues"][0]["reason"]
    assert service.calls == []


@pytest.mark.asyncio
async def test_local_context_budget_is_checked_before_sending_any_request(monkeypatch):
    monkeypatch.setenv("MEMORYBREAD_CREATION_CONTEXT_TOKENS", "8192")
    service = ReviewService()
    report = await review_work_document(service, "设计方案", "字" * 18000, CONTEXT)
    assert report["status"] == "unverified"
    assert "上下文" in report["issues"][0]["reason"]
    assert service.calls == []


def test_frozen_runtime_resolves_packaged_configuration(monkeypatch, tmp_path):
    target = tmp_path / "creation_work_quality"
    target.mkdir()
    original = work_quality._profile_path()
    (target / "profiles.json").write_bytes(original.read_bytes())
    monkeypatch.setattr(work_quality.sys, "frozen", True, raising=False)
    monkeypatch.setattr(work_quality.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert len(load_work_quality_profiles()["profiles"]) == 5
    assert work_quality._profile_path() == target / "profiles.json"


def test_invalid_configuration_fails_explicitly_instead_of_defaulting(monkeypatch, tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text('{"schema_version":"wrong"}', encoding="utf-8")
    monkeypatch.setattr(work_quality, "_profile_path", lambda: path)
    with pytest.raises(OperationError) as error:
        normalize_work_context(CONTEXT)
    assert error.value.code == "CREATION_WORK_QUALITY_CONFIG_INVALID"


def test_build_script_packages_the_same_profile_directory():
    root = Path(__file__).resolve().parents[2]
    build = (root / "scripts/build-macos.sh").read_text(encoding="utf-8")
    assert '--add-data "$PROJECT_ROOT/shared/creation-work-quality:creation_work_quality"' in build
