"""Real integration behavior with isolated source/semantic model boundaries."""

import asyncio
import copy
import json

import pytest

from creation import delivery_contract, work_integration, work_quality
from creation.operations import OperationError, document_hash
from creation.work_integration import (bind_work_acceptance, preview_work_creation,
    review_work_delivery, work_fact_options)
from creation.work_quality import work_acceptance_checks


DOCUMENT = "# 项目方案\n\n建议先试验，依据实际结果决定下一步。\n现有材料未确认固定成本或收益。"
CONTEXT = {"profile_id": "proposal_review", "review_enabled": True,
    "purpose": "方案评审", "decision_goal": "是否开始试验"}
BASE_CONTRACT = {"deliverable": "项目方案", "inputs": [], "self_contained_reason": "已有输入齐备",
    "acceptance": [{"id": "a", "criterion": "回应本轮要求"}]}


def source_report(status="pass"):
    return {"status": status, "checks": [{"id": "a", "passed": status == "pass",
        "reason": "本轮原要求核验", "evidence": DOCUMENT.splitlines()[2] if status == "pass" else ""}],
        "corrections": [] if status == "pass" else ["修正当前资料对应的事实强度。"]}


def semantic_report(context=CONTEXT, document=DOCUMENT, failed_id=None, status=None):
    checks = [{"id": item["id"], "passed": item["id"] != failed_id, "reason": "核验具体工作质量",
        "evidence": document.splitlines()[-1]} for item in work_acceptance_checks(context)]
    issues = [] if failed_id is None else [{"code": failed_id, "severity": "warning",
        "quote": document.splitlines()[-1], "reason": "本轮动作没有明确结果观察方式。",
        "correction": "依据现有材料说明如何观察试验结果。"}]
    return {"status": status or ("revise" if failed_id else "pass"), "checks": checks,
        "issues": issues, "document_hash": document_hash(document)}


def environment(context=CONTEXT):
    return {"document": DOCUMENT, "current_document": DOCUMENT, "work_context": copy.deepcopy(context),
        "input_context": {"schema_version": "creation.input-context.v1", "root_request": "写方案",
            "conversation": [], "creation_brief": "", "user_options": {"work_context": copy.deepcopy(context)}}}


def install_reviews(monkeypatch, source_status="pass", semantic=None):
    calls = []

    async def source(service, instruction, document, contract, env):
        calls.append(("source", copy.deepcopy(contract), copy.deepcopy(env)))
        return source_report(source_status)

    async def work(service, instruction, document, context, chapter_scope=None, effective_contract=None):
        calls.append(("semantic", document, copy.deepcopy(context), copy.deepcopy(chapter_scope), copy.deepcopy(effective_contract)))
        return copy.deepcopy(semantic if semantic is not None else semantic_report(context, document))

    monkeypatch.setattr(delivery_contract, "review_delivery", source)
    monkeypatch.setattr(work_integration, "review_work_document", work)
    return calls


def test_binding_uses_configured_codes_once_and_preserves_original_contract():
    original = copy.deepcopy(BASE_CONTRACT)
    bound = bind_work_acceptance(original, CONTEXT)
    again = bind_work_acceptance(bound, CONTEXT)
    assert original == BASE_CONTRACT
    assert bound == again
    assert len(bound["acceptance"]) == 6
    assert bound["acceptance"][0] == original["acceptance"][0]
    assert bind_work_acceptance(original, None) is original


def test_fact_settings_never_include_expression_samples():
    context = {**CONTEXT, "style_samples": ["STYLE_FACT_SENTINEL: 已投入七亿元。"]}
    original = copy.deepcopy(context)
    facts = work_fact_options(context)
    assert "style_samples" not in facts
    assert "STYLE_FACT_SENTINEL" not in json.dumps(facts)
    assert context == original


@pytest.mark.asyncio
async def test_original_sources_must_pass_before_semantic_and_work_ids_are_not_double_reviewed(monkeypatch):
    calls = install_reviews(monkeypatch)
    env = environment()
    contract = bind_work_acceptance(BASE_CONTRACT, CONTEXT)
    result = await review_work_delivery(object(), "写方案", DOCUMENT, contract, env)
    assert [item[0] for item in calls] == ["source", "semantic"]
    assert calls[0][1]["acceptance"] == BASE_CONTRACT["acceptance"]
    assert result["status"] == "pass"
    assert {item["id"] for item in result["checks"]} == {item["id"] for item in contract["acceptance"]}
    assert env["document"] == DOCUMENT
    assert env["current_document"] == DOCUMENT


@pytest.mark.parametrize("status", ["revise", "blocked"])
@pytest.mark.asyncio
async def test_failed_source_never_runs_semantic_or_makes_work_conditions_pass(monkeypatch, status):
    calls = install_reviews(monkeypatch, source_status=status)
    env = environment()
    contract = bind_work_acceptance(BASE_CONTRACT, CONTEXT)
    result = await review_work_delivery(object(), "写方案", DOCUMENT, contract, env)
    assert [item[0] for item in calls] == ["source"]
    assert result["status"] == status
    assert all(not item["passed"] for item in result["checks"])
    assert "work_quality_review" not in env
    assert env["document"] == DOCUMENT


@pytest.mark.asyncio
async def test_disabled_or_unbound_work_preferences_preserve_existing_delivery_behavior(monkeypatch):
    calls = install_reviews(monkeypatch)
    result = await review_work_delivery(object(), "只改标题", DOCUMENT, BASE_CONTRACT, environment())
    assert result == source_report()
    assert [item[0] for item in calls] == ["source"]


@pytest.mark.asyncio
async def test_source_fact_catalog_cannot_receive_samples_even_from_unfiltered_callers(monkeypatch):
    context = {**CONTEXT, "style_samples": ["STYLE_FACT_SENTINEL: 已投入七亿元。"]}
    env = environment(context)
    original = copy.deepcopy(env)
    inspected = []

    async def source(service, instruction, document, contract, source_env):
        materials = delivery_contract.delivery_source_materials(instruction, source_env)
        inspected.append(json.dumps(materials, ensure_ascii=False))
        assert "STYLE_FACT_SENTINEL" not in inspected[-1]
        return source_report()

    async def work(service, instruction, document, given, chapter_scope=None, effective_contract=None):
        assert given["style_samples"] == context["style_samples"]
        return semantic_report(given, document)

    monkeypatch.setattr(delivery_contract, "review_delivery", source)
    monkeypatch.setattr(work_integration, "review_work_document", work)
    result = await review_work_delivery(object(), "写方案", DOCUMENT, bind_work_acceptance(BASE_CONTRACT, context), env)
    assert result["status"] == "pass"
    assert inspected
    assert env["document"] == original["document"]
    assert env["work_context"]["style_samples"] == original["work_context"]["style_samples"]


@pytest.mark.asyncio
async def test_narrow_contract_does_not_inherit_failure_or_correction_from_unbound_dimensions(monkeypatch):
    report = semantic_report(failed_id="work_actionable_specificity")
    install_reviews(monkeypatch, semantic=report)
    only_natural = next(item for item in work_acceptance_checks(CONTEXT) if item["id"] == "work_natural_expression")
    contract = {**BASE_CONTRACT, "acceptance": BASE_CONTRACT["acceptance"] + [only_natural]}
    result = await review_work_delivery(object(), "只调整表达", DOCUMENT, contract, environment())
    assert result["status"] == "pass"
    assert result["corrections"] == []
    assert {item["id"] for item in result["checks"]} == {"a", "work_natural_expression"}


@pytest.mark.asyncio
async def test_semantic_revision_merges_targeted_corrections_and_preserves_body(monkeypatch):
    install_reviews(monkeypatch, semantic=semantic_report(failed_id="work_actionable_specificity"))
    env = environment()
    result = await review_work_delivery(object(), "写方案", DOCUMENT, bind_work_acceptance(BASE_CONTRACT, CONTEXT), env)
    assert result["status"] == "revise"
    assert result["corrections"] == ["依据现有材料说明如何观察试验结果。"]
    assert env["document"] == DOCUMENT


@pytest.mark.asyncio
async def test_unverified_review_is_saved_for_retry_and_blocks_delivery_without_changing_body(monkeypatch):
    report = {"status": "unverified", "checks": [], "issues": [{"code": "work_review_unverified",
        "severity": "warning", "quote": "", "reason": "尚未取得有效审阅", "correction": "重试审阅"}],
        "document_hash": document_hash(DOCUMENT)}
    calls = install_reviews(monkeypatch, semantic=report)
    env = environment()
    contract = bind_work_acceptance(BASE_CONTRACT, CONTEXT)
    for _ in range(2):
        with pytest.raises(OperationError) as error:
            await review_work_delivery(object(), "写方案", DOCUMENT, contract, env)
        assert error.value.code == "CREATION_DELIVERY_UNVERIFIED"
        assert error.value.retryable
    assert env["work_quality_review"]["status"] == "unverified"
    assert env["document"] == DOCUMENT
    assert [item[0] for item in calls].count("semantic") == 2


@pytest.mark.asyncio
async def test_missing_semantic_coverage_cannot_be_merged_into_pass(monkeypatch):
    incomplete = semantic_report()
    incomplete["checks"] = incomplete["checks"][:1]
    install_reviews(monkeypatch, semantic=incomplete)
    env = environment()
    with pytest.raises(OperationError) as error:
        await review_work_delivery(object(), "写方案", DOCUMENT, bind_work_acceptance(BASE_CONTRACT, CONTEXT), env)
    assert error.value.code == "CREATION_DELIVERY_UNVERIFIED"
    assert env["document"] == DOCUMENT


@pytest.mark.asyncio
async def test_unchanged_binding_reuses_semantic_but_context_document_and_instruction_changes_invalidate(monkeypatch):
    calls = install_reviews(monkeypatch)
    env = environment()
    contract = bind_work_acceptance(BASE_CONTRACT, CONTEXT)
    await review_work_delivery(object(), "写方案", DOCUMENT, contract, env)
    await review_work_delivery(object(), "写方案", DOCUMENT, contract, env)
    assert [item[0] for item in calls].count("semantic") == 1
    await review_work_delivery(object(), "改为试验设计", DOCUMENT, contract, env)
    await review_work_delivery(object(), "改为试验设计", DOCUMENT + "\n新输入。", contract, env)
    env["work_context"]["decision_goal"] = "决定是否扩大试验"
    await review_work_delivery(object(), "改为试验设计", DOCUMENT + "\n新输入。", contract, env)
    assert [item[0] for item in calls].count("semantic") == 4


@pytest.mark.asyncio
async def test_effective_deliverable_and_adopted_skill_boundaries_are_passed_and_invalidate_cache(monkeypatch):
    calls = install_reviews(monkeypatch)
    env = environment()
    env.update(strict_skill_workflow=True, strict_skill_ids=["facts"],
        routing_decision={"operation": {"kind": "execute_skill"}},
        work_quality_skill_constraints=[{"id": "facts", "strict_structure": True,
            "skill_instructions": "只输出事实表格，不新增判断栏目", "execution_steps": []}])
    contract = bind_work_acceptance({**BASE_CONTRACT, "deliverable": "事实表格",
        "inputs": [{"evidence": "MATERIAL_NOT_A_SCOPE_RULE"}]}, CONTEXT)
    await review_work_delivery(object(), "按已采用Skill执行", DOCUMENT, contract, env)
    boundary = [item for item in calls if item[0] == "semantic"][-1][4]
    assert boundary["deliverable"] == "事实表格"
    assert boundary["operation_kind"] == "execute_skill"
    assert boundary["skill_constraints"][0]["id"] == "facts"
    assert "inputs" not in boundary
    assert all(not item["id"].startswith("work_") for item in boundary["acceptance"])
    assert "MATERIAL_NOT_A_SCOPE_RULE" not in json.dumps(boundary)
    await review_work_delivery(object(), "按已采用Skill执行", DOCUMENT, contract, env)
    assert [item[0] for item in calls].count("semantic") == 1
    env["work_quality_skill_constraints"][0]["skill_instructions"] = "仅允许已有两列"
    await review_work_delivery(object(), "按已采用Skill执行", DOCUMENT, contract, env)
    contract["acceptance"][0]["criterion"] = "只核验本轮两列"
    await review_work_delivery(object(), "按已采用Skill执行", DOCUMENT, contract, env)
    assert [item[0] for item in calls].count("semantic") == 3


@pytest.mark.asyncio
async def test_context_loaded_from_user_options_preserves_samples_and_reuses_unchanged_review(monkeypatch):
    context = {**CONTEXT, "style_samples": ["先说明关键变化，再说明下一步。"]}
    calls = install_reviews(monkeypatch)
    env = environment(context)
    env.pop("work_context")
    contract = bind_work_acceptance(BASE_CONTRACT, context)
    await review_work_delivery(object(), "写方案", DOCUMENT, contract, env)
    await review_work_delivery(object(), "写方案", DOCUMENT, contract, env)
    semantic = [item for item in calls if item[0] == "semantic"]
    assert len(semantic) == 1
    assert semantic[0][2]["style_samples"] == context["style_samples"]
    retained = env.get("work_context") or env["input_context"]["user_options"]["work_context"]
    assert retained["style_samples"] == context["style_samples"]


@pytest.mark.asyncio
async def test_profile_rubric_changes_with_same_codes_invalidate_cached_review(monkeypatch):
    calls = install_reviews(monkeypatch)
    env = environment()
    contract = bind_work_acceptance(BASE_CONTRACT, CONTEXT)
    await review_work_delivery(object(), "写方案", DOCUMENT, contract, env)
    original = work_integration.work_acceptance_checks

    def changed(context, chapter_scope=None):
        checks = original(context, chapter_scope)
        checks[0]["criterion"] += "新版表达约束。"
        return checks

    monkeypatch.setattr(work_integration, "work_acceptance_checks", changed)
    updated = bind_work_acceptance(BASE_CONTRACT, CONTEXT)
    await review_work_delivery(object(), "写方案", DOCUMENT, updated, env)
    assert [item[0] for item in calls].count("semantic") == 2


@pytest.mark.asyncio
async def test_scope_changes_invalidate_review_and_real_chapter_scope_excludes_previous_body(monkeypatch):
    calls = install_reviews(monkeypatch)
    env = environment()
    contract = bind_work_acceptance(BASE_CONTRACT, CONTEXT)
    await review_work_delivery(object(), "写方案", DOCUMENT, contract, env)
    chapter = {"id": "c1", "title": "项目方案", "goal": "说明试验安排"}
    env["creation_brief"] = {"chapter_context": {**chapter, "index": 1, "total": 1,
        "outline": [chapter], "confirmed_decisions": []}}
    env["chapter_base_document"] = ""
    await review_work_delivery(object(), "写方案", DOCUMENT, contract, env)
    semantic = [item for item in calls if item[0] == "semantic"]
    assert len(semantic) == 2
    assert semantic[-1][3]["scope"] == "chapter"
    assert semantic[-1][3]["document"] == DOCUMENT.split("\n\n", 1)[1]
    assert "# 项目方案" not in semantic[-1][3]["document"]


@pytest.mark.asyncio
async def test_completed_previous_body_is_frozen_and_not_semantically_reviewed_with_current_chapter(monkeypatch):
    calls = install_reviews(monkeypatch)
    base = "# 项目方案\n\n## 第一章 背景\n已经完成并保持的此前正文。\n"
    current = "\n## 第二章 试验\n\n本章建议依据实际结果调整试验安排。"
    document = base + current
    outline = [{"id": "c1", "title": "第一章 背景", "goal": "背景"},
        {"id": "c2", "title": "第二章 试验", "goal": "试验"},
        {"id": "c3", "title": "第三章 后续", "goal": "后续判断"}]
    env = environment()
    env.update({"document": document, "chapter_base_document": base,
        "creation_brief": {"chapter_context": {**outline[1], "index": 2, "total": 3,
            "outline": outline, "confirmed_decisions": []}}})
    result = await review_work_delivery(object(), "仅追加第二章", document,
        bind_work_acceptance(BASE_CONTRACT, CONTEXT), env)
    assert result["status"] == "pass"
    scope = next(item[3] for item in calls if item[0] == "semantic")
    assert scope["document"] == "本章建议依据实际结果调整试验安排。"
    assert "此前正文" not in scope["document"]
    assert "第三章" not in scope["document"]
    assert env["document"] == document
    assert env["chapter_base_document"] == base


@pytest.mark.asyncio
async def test_enabled_condition_set_changes_invalidate_cached_review(monkeypatch):
    calls = install_reviews(monkeypatch)
    env = environment()
    complete = bind_work_acceptance(BASE_CONTRACT, CONTEXT)
    await review_work_delivery(object(), "调整表达", DOCUMENT, complete, env)
    narrow = {**BASE_CONTRACT, "acceptance": BASE_CONTRACT["acceptance"] +
        [item for item in complete["acceptance"] if item["id"] == "work_natural_expression"]}
    await review_work_delivery(object(), "调整表达", DOCUMENT, narrow, env)
    assert [item[0] for item in calls].count("semantic") == 2


class SemanticService:
    def __init__(self, delay=0):
        self.delay = delay
        self.calls = []

    async def _stream_direct_completion(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        yield "{}"


@pytest.mark.asyncio
async def test_real_semantic_budget_failure_blocks_delivery_before_work_model_call(monkeypatch):
    install_reviews(monkeypatch)
    monkeypatch.setattr(work_integration, "review_work_document", work_quality.review_work_document)
    service = SemanticService()
    document = "字" * 24001
    env = environment()
    env["document"] = document
    with pytest.raises(OperationError) as error:
        await review_work_delivery(service, "审阅方案", document, bind_work_acceptance(BASE_CONTRACT, CONTEXT), env)
    assert error.value.code == "CREATION_DELIVERY_UNVERIFIED"
    assert service.calls == []
    assert env["work_quality_review"]["status"] == "unverified"
    assert env["document"] == document


@pytest.mark.asyncio
async def test_real_semantic_timeout_remains_unverified_in_delivery_and_preserves_body(monkeypatch):
    install_reviews(monkeypatch)
    monkeypatch.setattr(work_integration, "review_work_document", work_quality.review_work_document)
    monkeypatch.setattr(work_quality, "REVIEW_TIMEOUT_SECONDS", 0.005)
    service = SemanticService(delay=1)
    env = environment()
    with pytest.raises(OperationError) as error:
        await review_work_delivery(service, "审阅方案", DOCUMENT, bind_work_acceptance(BASE_CONTRACT, CONTEXT), env)
    assert error.value.code == "CREATION_DELIVERY_UNVERIFIED"
    assert len(service.calls) == 1
    assert env["work_quality_review"]["status"] == "unverified"
    assert "超时" in env["work_quality_review"]["issues"][0]["reason"]
    assert env["document"] == DOCUMENT


class PreviewService:
    def __init__(self, result="建议先开展试验，再根据实际结果调整。", delay=0):
        self.result, self.delay = result, delay
        self.calls = []
        self.original_document = DOCUMENT

    async def _stream_complete_agent_output(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        yield self.result


@pytest.mark.parametrize("action", ["trial", "summary"])
@pytest.mark.asyncio
async def test_preview_has_its_own_candidate_and_never_changes_original_document(monkeypatch, action):
    calls = install_reviews(monkeypatch)
    service = PreviewService()
    context = {**CONTEXT, "style_samples": ["STYLE_FACT_SENTINEL: 收益已达七亿元。"]}
    original_context = copy.deepcopy(context)
    result = await preview_work_creation(service, action, "判断是否开始试验", DOCUMENT, context)
    assert result["status"] == "candidate"
    assert result["read_only"] is True
    assert result["content"] == service.result
    assert result["document_hash"] == document_hash(DOCUMENT)
    assert service.original_document == DOCUMENT
    assert context == original_context
    assert len(service.calls) == 1
    assert [item[0] for item in calls] == ["source", "semantic"]
    assert "STYLE_FACT_SENTINEL" not in json.dumps(calls[0][2], ensure_ascii=False)
    assert calls[-1][3]["scope"] == "preview"
    ids = {item["id"] for item in calls[0][1]["acceptance"]}
    assert ("preview_decision_consistency" in ids) is (action == "summary")
    if action == "summary":
        assert "不重新评审" in service.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_summary_decision_reversal_is_a_revision_and_never_a_candidate(monkeypatch):
    calls = install_reviews(monkeypatch, source_status="revise")
    service = PreviewService(result="建议暂不推进试验。")
    result = await preview_work_creation(service, "summary", "忠实汇总原结论", DOCUMENT, CONTEXT)
    assert result["status"] == "revise"
    criterion = next(item["criterion"] for item in calls[0][1]["acceptance"]
                     if item["id"] == "preview_decision_consistency")
    assert "不得自行反转立场" in criterion
    assert "建议先试验" in calls[0][2]["input_base_document"]
    assert [item[0] for item in calls] == ["source"]
    assert result["content"] == service.result
    assert service.original_document == DOCUMENT


@pytest.mark.asyncio
async def test_invalid_source_review_keeps_preview_visible_and_unverified(monkeypatch):
    calls = install_reviews(monkeypatch)

    async def invalid_source(*args, **kwargs):
        raise OperationError("CREATION_DELIVERY_UNVERIFIED", "来源协议无效", retryable=True)

    monkeypatch.setattr(delivery_contract, "review_delivery", invalid_source)
    service = PreviewService()
    result = await preview_work_creation(service, "summary", "提取当前结论", DOCUMENT, CONTEXT)
    assert result["status"] == "unverified"
    assert result["content"] == service.result
    assert result["document_hash"] == document_hash(DOCUMENT)
    assert result["read_only"] is True
    assert calls == []
    assert result["review"]["issues"][0]["code"] == "preview_sources_unverified"


@pytest.mark.asyncio
async def test_explicit_review_preview_runs_without_generation_and_is_read_only(monkeypatch):
    calls = install_reviews(monkeypatch)
    service = PreviewService()
    result = await preview_work_creation(service, "review", "审阅当前正文", DOCUMENT, None)
    assert result["read_only"]
    assert result["content"] == ""
    assert service.calls == []
    assert [item[0] for item in calls] == ["semantic"]
    assert calls[0][2]["review_enabled"] is True
    assert result["document_hash"] == document_hash(DOCUMENT)


@pytest.mark.asyncio
async def test_failed_preview_sources_never_request_semantic_and_keep_candidate_separate(monkeypatch):
    calls = install_reviews(monkeypatch, source_status="revise")
    service = PreviewService()
    result = await preview_work_creation(service, "summary", "汇总当前结论", DOCUMENT, CONTEXT)
    assert result["status"] == "revise"
    assert result["content"] == service.result
    assert result["read_only"]
    assert [item[0] for item in calls] == ["source"]
    assert service.original_document == DOCUMENT


@pytest.mark.asyncio
async def test_blocked_preview_sources_are_explicitly_unverified(monkeypatch):
    calls = install_reviews(monkeypatch, source_status="blocked")
    service = PreviewService()
    result = await preview_work_creation(service, "summary", "汇总当前结论", DOCUMENT, CONTEXT)
    assert result["status"] == "unverified"
    assert result["review"]["status"] == "blocked"
    assert result["read_only"]
    assert result["content"] == service.result
    assert [item[0] for item in calls] == ["source"]


@pytest.mark.parametrize("action,document,instruction", [("summary", "字" * 24001, "摘要"),
    ("trial", DOCUMENT, "字" * 4001), ("review", "", "审阅"), ("summary", "", "摘要"),
    ("unsupported", DOCUMENT, "写作")])
@pytest.mark.asyncio
async def test_oversize_or_invalid_preview_does_not_send_any_model_request(monkeypatch, action, document, instruction):
    calls = install_reviews(monkeypatch)
    service = PreviewService()
    with pytest.raises(OperationError) as error:
        await preview_work_creation(service, action, instruction, document, CONTEXT)
    assert error.value.code == "CREATION_WORK_CONTEXT_INVALID"
    assert service.calls == []
    assert calls == []


@pytest.mark.asyncio
async def test_empty_or_incomplete_preview_cannot_be_published_as_candidate(monkeypatch):
    calls = install_reviews(monkeypatch)
    service = PreviewService(result="")
    with pytest.raises(OperationError) as error:
        await preview_work_creation(service, "trial", "试写", DOCUMENT, CONTEXT)
    assert error.value.code == "CREATION_DOCUMENT_INVALID"
    assert calls == []
    assert service.original_document == DOCUMENT


@pytest.mark.asyncio
async def test_preview_timeout_is_retryable_and_does_not_modify_body(monkeypatch):
    calls = install_reviews(monkeypatch)
    monkeypatch.setattr(work_integration, "PREVIEW_TIMEOUT_SECONDS", 0.005)
    service = PreviewService(delay=1)
    with pytest.raises(OperationError) as error:
        await preview_work_creation(service, "trial", "试写", DOCUMENT, CONTEXT)
    assert error.value.code == "CREATION_DELIVERY_UNVERIFIED"
    assert error.value.retryable
    assert service.original_document == DOCUMENT
    assert calls == []


@pytest.mark.asyncio
async def test_preview_cancel_is_propagated_and_does_not_continue_review(monkeypatch):
    calls = install_reviews(monkeypatch)
    service = PreviewService(delay=10)
    task = asyncio.create_task(preview_work_creation(service, "trial", "试写", DOCUMENT, CONTEXT))
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == []
    assert service.original_document == DOCUMENT


@pytest.mark.asyncio
async def test_source_cancellation_stops_integration_without_work_review_or_body_changes(monkeypatch):
    calls = install_reviews(monkeypatch)

    async def source(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(delivery_contract, "review_delivery", source)
    env = environment()
    with pytest.raises(asyncio.CancelledError):
        await review_work_delivery(object(), "写方案", DOCUMENT, bind_work_acceptance(BASE_CONTRACT, CONTEXT), env)
    assert calls == []
    assert env["document"] == DOCUMENT
