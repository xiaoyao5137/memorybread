"""The same user materials must survive assessment, writing and acceptance."""
import json

import pytest

from creation.agent_loop import CreationAgentLoop
from creation.delivery_contract import assess_inputs, review_delivery, with_source_scope_check
from creation.service import CreationOptions, CreationService
from creation.operations import OperationError
from tests.test_creation_delivery_contract import DeliveryService, StreamService, contract, review_for_model as review
from tests.test_creation_operations import run_args

SYSTEM_SCOPE_IDS = ["source_scope_grounding", "source_attributes_grounding", "source_obligations_grounding"]


@pytest.mark.asyncio
async def test_review_receives_prior_user_facts_with_roles_and_order():
    conversation = [
        {"role": "user", "content": "上周收入80万元，本周收入100万元。"},
        {"role": "assistant", "content": "建议把目标定为150万元。"},
        {"role": "user", "content": "更正：本周实际收入为96万元，150万元仅是建议。"},
    ]
    service = StreamService([json.dumps(review(document="line-1"))])
    await review_delivery(service, "按前面更正后的数据写小结", "收入96万元。", contract(), {
        "input_context": {"conversation": conversation, "creation_brief": ""},
    })
    materials = json.loads(service.calls[0]["user_prompt"])["provided_materials"]
    assert materials["conversation"] == conversation
    assert "助手" in service.calls[0]["system_prompt"]
    assert "更正" in service.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_assessment_can_bind_facts_from_confirmed_brainstorm_brief():
    value = contract(["business_data"])
    value["inputs"][0].update(state="provided", evidence="预算上限3000元。", query="")
    service = StreamService([json.dumps(value, ensure_ascii=False)])
    result = await assess_inputs(service, "按确认简报写方案", "", "generate", [],
                                 brief_context="已确认决策：\n预算上限3000元。")
    assert result["inputs"][0]["state"] == "provided"
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["supplied_lines"]["brief-2"] == "预算上限3000元。"


@pytest.mark.asyncio
async def test_agent_checkpoints_the_exact_context_used_by_assessment():
    class ContextService(DeliveryService):
        async def review_creation_delivery(self, instruction, document, conditions, environment):
            self.checked.append(environment)
            return {"status": "pass", "corrections": [], "checks": [
                {"id": check["id"], "passed": True, "reason": "上下文传递测试", "evidence": document}
                for check in conditions["acceptance"]]}

        async def assess_creation_inputs(self, *args, **kwargs):
            self.context_at_assessment = {
                "schema_version": "creation.input-context.v1",
                "conversation": args[3], "creation_brief": kwargs.get("brief_context", ""),
                "root_request": kwargs.get("root_request", ""),
                "user_options": kwargs.get("user_options", {}),
            }
            return await super().assess_creation_inputs(*args, **kwargs)

    service = ContextService()
    args = run_args("", "按已确认信息创作一份方案")
    args.update(conversation=[{"role": "user", "content": "所有参与者都是成年人。"}],
                creation_mode="brainstorm", creation_brief={"decisions": [
                    {"question_id": "budget", "dimension": "预算", "summary": "预算上限3000元。", "source": "user"}
                ]}, options=CreationOptions(audience="活动参与者"))
    events = [event async for event in CreationAgentLoop(service).run(**args)]
    assert events[-1]["type"] == "run.completed"
    snapshot = service.checked[0]["input_context"]
    assert {key: snapshot[key] for key in service.context_at_assessment} == service.context_at_assessment
    assert snapshot["brainstorm_context_version"] == 3
    assert snapshot["brainstorm_decisions"][0]["value"] == "预算上限3000元。"
    assert "3000" in snapshot["creation_brief"]
    assert snapshot["conversation"] == args["conversation"]
    assert snapshot["user_options"] == {"audience": "活动参与者"}


@pytest.mark.asyncio
async def test_long_conversation_keeps_initial_facts_and_recent_corrections():
    conversation = [{"role": "user", "content": "原始预算3000元。"}]
    conversation += [{"role": "assistant", "content": "第{}轮写作建议。".format(i)} for i in range(18)]
    conversation += [{"role": "user", "content": "最新更正：地点改为一楼。"}]
    service = StreamService([json.dumps(contract())])
    await assess_inputs(service, "继续成文", "", "generate", conversation, root_request="活动有20人参加。")
    payload = json.loads(service.calls[0]["user_prompt"])
    assert len(payload["conversation"]) == 6
    assert payload["conversation"][0] == conversation[0]
    assert payload["conversation"][-1] == conversation[-1]
    assert payload["supplied_lines"]["root-1"] == "活动有20人参加。"
    assert "原始预算3000元。" in payload["supplied_lines"].values()
    assert not any("写作建议" in value for value in payload["supplied_lines"].values())


@pytest.mark.asyncio
async def test_brief_source_line_ids_remain_resolvable_during_review():
    service = StreamService([json.dumps(review(document="line-1"))])
    brief = "已确认决策：\n预算上限3000元。"
    await review_delivery(service, "按简报成文", "预算上限3000元。", contract(), {
        "input_context": {"conversation": [], "creation_brief": brief},
    })
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["provided_materials"]["supplied_lines"]["brief-2"] == "预算上限3000元。"
    assert payload["candidate_document"] == "预算上限3000元。"


@pytest.mark.asyncio
async def test_general_pass_cannot_skip_unprovided_calendar_claim_review():
    service = StreamService([json.dumps(review(document="line-1"))] * 2)
    with pytest.raises(OperationError, match="核验最终"):
        await review_delivery(service, "本周收入96万元，只整理此事实。",
                              "本周（2026年第36周）收入96万元。", contract(), {})
    checks = json.loads(service.calls[0]["user_prompt"])["contract"]["acceptance"]
    assert checks[-1]["id"] == "calendar_grounding"
    assert "2026年第36周" in checks[-1]["criterion"]


@pytest.mark.asyncio
async def test_weekday_cannot_silently_gain_a_current_week_qualifier():
    service = StreamService([json.dumps(review(document="line-1"))] * 2)
    with pytest.raises(OperationError):
        await review_delivery(service, "活动在周三下午举行，只整理此事实。",
                              "活动在本周三下午举行。", contract(), {})
    checks = json.loads(service.calls[0]["user_prompt"])["contract"]["acceptance"]
    assert "本周三" in checks[-1]["criterion"]


@pytest.mark.asyncio
async def test_supplied_calendar_fact_needs_no_extra_calendar_check():
    document = "2026年9月8日举行活动。"
    service = StreamService([json.dumps(review(document="line-1"))])
    result = await review_delivery(service, document, document, contract(), {})
    assert result["status"] == "pass"
    assert len(json.loads(service.calls[0]["user_prompt"])["contract"]["acceptance"]) == 4


@pytest.mark.parametrize("environment", [
    {"tool_results": [{"tool_id": "memory_search", "query": "2026-09-08 收入",
                       "time_context": {"period_start": "2026-09-08"}}]},
    {"references": [{"title": "2026-09-08 报告", "observed_at": "2026-09-08",
                     "source_url": "https://example.test/2026-09-08", "content": "收入96万元。"}]},
    {"references": [{"content": "2026-09-08 收入96万元。",
                     "data_freshness": {"can_use": False}}]},
    {"references": [{"content": "2026-09-08 收入96万元。",
                     "data_use_policy": "current_values_unavailable"}]},
    {"data_results": [{"can_use": False, "title": "2026-09-08 报表",
                       "content_excerpt": "2026-09-08 收入96万元。"}]},
    {"data_results": [{"can_use": True, "collected_at": "2026-09-08",
                       "provenance": {"captured_at": "2026-09-08"}, "content_excerpt": "收入96万元。"}]},
    {"data_results": [{"can_use": True, "structured_data": {
        "metric": 96, "query": "2026-09-08 收入", "time_context": {"period_start": "2026-09-08"}}}]},
    {"web_results": [{"title": "2026-09-08 报告", "url": "https://example.test/2026-09-08",
                      "snippet": "收入96万元。"}]},
    {"web_results": [{"can_use": False, "snippet": "2026-09-08 收入96万元。"}]},
])
@pytest.mark.asyncio
async def test_retrieval_metadata_and_unavailable_sources_cannot_supply_calendar_facts(environment):
    service = StreamService([json.dumps(review(document="line-1"))] * 2)
    with pytest.raises(OperationError, match="核验最终"):
        await review_delivery(service, "根据材料写收入小结。", "2026-09-08 收入96万元。", contract(), environment)
    checks = json.loads(service.calls[0]["user_prompt"])["contract"]["acceptance"]
    assert checks[-1]["id"] == "calendar_grounding"
    assert "2026-09-08" in checks[-1]["criterion"]


@pytest.mark.parametrize("environment", [
    {"references": [{"content": "2026-09-08 收入96万元。"}]},
    {"references": [{"summary": "2026-09-08 收入96万元。"}]},
    {"data_results": [{"can_use": True, "content_excerpt": "2026-09-08 收入96万元。"}]},
    {"data_results": [{"can_use": True, "structured_data": {"verified_claims": [
        {"statement": "2026-09-08 收入96万元。"}]}}]},
    {"data_results": [{"can_use": True, "structured_data": {"2026-09-08": 96}}]},
    {"web_results": [{"snippet": "2026-09-08 收入96万元。"}]},
])
@pytest.mark.asyncio
async def test_usable_bounded_source_facts_can_supply_calendar_anchors(environment):
    service = StreamService([json.dumps(review(document="line-1"))])
    result = await review_delivery(service, "根据材料写收入小结。", "2026-09-08 收入96万元。", contract(), environment)
    assert result["status"] == "pass"
    assert len(json.loads(service.calls[0]["user_prompt"])["contract"]["acceptance"]) == 4


@pytest.mark.asyncio
async def test_calendar_anchor_outside_bounded_source_view_still_requires_review():
    service = StreamService([json.dumps(review(document="line-1"))] * 2)
    with pytest.raises(OperationError):
        await review_delivery(service, "根据材料写收入小结。", "2026-09-08 收入96万元。", contract(), {
            "references": [{"content": "其他材料。" * 500 + "2026-09-08 收入96万元。"}],
        })
    checks = json.loads(service.calls[0]["user_prompt"])["contract"]["acceptance"]
    assert checks[-1]["id"] == "calendar_grounding"


@pytest.mark.parametrize("additional_check", ["source_accuracy", "calendar_grounding"])
@pytest.mark.asyncio
async def test_review_retry_names_missing_independent_check_and_recovers_as_revise(additional_check):
    conditions = contract()
    document = "收入999万元。"
    environment = {"data_results": [{"can_use": False, "content_excerpt": "2026-09-08 收入999万元。"}]}
    if additional_check == "calendar_grounding":
        document = "2026-09-08 收入999万元。"
    else:
        conditions["acceptance"].append({"id": additional_check, "criterion": "数值只能引用可用来源"})
    first = review(status="revise")
    first["checks"][0]["reason"] = "数字与其他事实均缺少可用来源，需修改。"
    repaired = json.loads(json.dumps(first))
    repaired["checks"].append({"id": additional_check, "passed": False,
                               "reason": "此条件也独立核验失败。", "evidence": ""})
    service = StreamService([json.dumps(first), json.dumps(repaired)])
    result = await review_delivery(service, "据材料整理，不能新增事实。", document, conditions, environment)
    assert result["status"] == "revise"
    assert len(service.calls) == 2
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["required_check_ids"] == SYSTEM_SCOPE_IDS + ["a", additional_check]
    retry = service.calls[1]["user_prompt"]
    assert '"missing_ids": ["' + additional_check + '"]' in retry
    assert '"duplicate_ids": []' in retry
    assert '"unknown_ids": []' in retry
    assert "不能合并" in retry


@pytest.mark.parametrize("ids, missing, duplicates, unknown", [
    (["a", "a"], ["source_accuracy"], ["a"], []),
    (["a", "source_accuracy", "a"], [], ["a"], []),
    (["a", "source_accuracy", "extra"], [], [], ["extra"]),
])
@pytest.mark.asyncio
async def test_review_retry_reports_exact_coverage_errors_and_stops_after_two_calls(ids, missing, duplicates, unknown):
    conditions = contract()
    conditions["acceptance"].append({"id": "source_accuracy", "criterion": "来源正确"})
    invalid = review(document="line-1")
    invalid["checks"] = [{**invalid["checks"][0], "id": check_id} for check_id in ids]
    invalid["checks"].extend(review(document="line-1")["checks"][1:])
    service = StreamService([json.dumps(invalid)] * 2)
    with pytest.raises(OperationError, match="核验最终"):
        await review_delivery(service, "整理正文", "正文", conditions, {})
    assert len(service.calls) == 2
    retry = service.calls[1]["user_prompt"]
    for key, value in (("missing_ids", missing), ("duplicate_ids", duplicates), ("unknown_ids", unknown)):
        assert json.dumps(key) + ": " + json.dumps(value) in retry


@pytest.mark.asyncio
async def test_complete_review_needs_only_one_model_call():
    service = StreamService([json.dumps(review(document="line-1"))])
    result = await review_delivery(service, "整理正文", "正文", contract(), {})
    assert result["status"] == "pass"
    assert len(service.calls) == 1
    assert json.loads(service.calls[0]["user_prompt"])["required_check_ids"] == SYSTEM_SCOPE_IDS + ["a"]


@pytest.mark.parametrize("kind", ["current_week", "previous_week"])
def test_calendar_placeholder_guard_never_rewrites_numeric_source_annotations(kind):
    document = "本周（2026年第36周）收入96万元，上周（2026年第35周）收入80万元。"
    result, audit = CreationAgentLoop._guard_generated_placeholders(document, {"time_context": {
        "period_kind": kind, "display": "2026年第37周", "iso_year": 2026, "iso_week": 37,
    }})
    assert result == document
    assert audit == []


def test_self_contained_writer_does_not_receive_execution_clock_as_source_fact():
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args("", "基于给定材料写收入小结"), model_mode="local")
    state.environment.update(input_contract=contract(), requirement={"time_context": {
        "current_date": "2026-09-08", "period_kind": "previous_week", "iso_week": 36,
        "display": "2026年第36周", "period_start": "2026-08-31",
    }})
    prompt = loop._prompt_environment(state)
    assert "2026-09-08" not in prompt
    assert "2026年第36周" not in prompt


def test_middle_user_cancellation_survives_assistant_context_compaction():
    from creation.delivery_contract import bounded_input_conversation
    conversation = [{"role": "user", "content": "预算上限100万元。"}]
    conversation += [{"role": "assistant", "content": "建议"}] * 4
    conversation += [{"role": "user", "content": "取消此前预算上限。"}]
    conversation += [{"role": "assistant", "content": "其他说明"}] * 14
    compact = bounded_input_conversation(conversation)
    assert [item["content"] for item in compact if item["role"] == "user"] == ["预算上限100万元。", "取消此前预算上限。"]


@pytest.mark.parametrize("assistant_tail", [36, 80])
def test_new_state_keeps_user_cancellation_before_large_assistant_tail(assistant_tail):
    conversation = [{"role": "user", "content": "预算上限100万元。"}]
    conversation += [{"role": "assistant", "content": "建议"}] * 4
    conversation += [{"role": "user", "content": "取消此前预算上限。"}]
    conversation += [{"role": "assistant", "content": "其他说明"}] * assistant_tail
    args = run_args("", "按已确认条件继续创作")
    args.update(conversation=conversation)
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args, model_mode="local")
    context = loop._input_context(state)
    assert [item["content"] for item in context["conversation"] if item["role"] == "user"] == [
        "预算上限100万元。", "取消此前预算上限。"]
    assert len(state.conversation) <= 40
    assert len(context["conversation"]) == 6


def test_shared_context_compaction_bounds_users_and_is_idempotent():
    from creation.delivery_contract import bounded_input_conversation
    conversation = []
    for index in range(60):
        conversation.extend([{"role": "user", "content": "用户更正{}".format(index)},
                             {"role": "assistant", "content": "助手说明{}".format(index)}])
    compact = bounded_input_conversation(conversation)
    assert len(compact) == 40
    assert compact[0] == conversation[0]
    assert compact[-1] == conversation[-1]
    assert [item["content"] for item in compact if item["role"] == "user"] == [
        "用户更正0"] + ["用户更正{}".format(i) for i in range(25, 60)]
    assert bounded_input_conversation(compact) == compact
    assert CreationAgentLoop._normalize_conversation(conversation) == compact


def test_legacy_checkpoint_rebuilds_stale_brief_cache_before_writing_and_review():
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args("", "按简报成文"), model_mode="local", creation_mode="brainstorm",
        creation_brief={"decisions": [{"question_id": "budget", "dimension": "预算", "summary": "预算100万元。", "source": "user"}],
                        "brief_edits": {"budget": ""}})
    state.environment["creation_brief_context"] = "旧简报：预算100万元。"
    state.environment["input_context"] = {"creation_brief": "旧简报：预算100万元。"}
    context = loop._input_context(state)
    assert "100万元" not in context["creation_brief"]
    assert "预算" in context["creation_brief"]
    assert "用户已清空的决定" in context["creation_brief"]
    assert state.environment["creation_brief_context"] == context["creation_brief"]


@pytest.mark.asyncio
async def test_review_uses_measured_length_and_distinguishes_process_notes_from_content():
    from creation.delivery_contract import candidate_text_metrics
    document = "# 标题\n\n小蜗牛做面包。Hello world!\n"
    service = StreamService([json.dumps(review(document="line-3"))])
    await review_delivery(service, "写简短故事", document, contract(), {})
    metrics = json.loads(service.calls[0]["user_prompt"])["candidate_metrics"]
    assert metrics == candidate_text_metrics(document)
    assert metrics["body_cjk_characters"] == 6
    assert metrics["body_latin_words"] == 2
    assert "不得用行数猜测" in service.calls[0]["system_prompt"]
    assert "撰写过程注释" in service.calls[0]["system_prompt"]


@pytest.mark.parametrize("instruction", [
    "会议定在周三下午，地点二楼会议室。写一段通知。",
    "将客户投诉和销售数据整理为表格。",
    "介绍政府、领导与企业的关系，不指定读者身份。",
])
def test_request_topics_do_not_invent_an_audience(instruction):
    service = CreationService(db_path=":memory:", enable_vector_recall=False)
    requirement = service.analyze_requirement(instruction, CreationOptions())
    assert requirement["audience"] == ""
    _, prompt = service.build_routing_prompts(instruction, requirement)
    assert "目标读者：" not in prompt
    assert instruction in prompt


@pytest.mark.parametrize("audience", ["研发团队", "家长", "跨组织项目参加者"])
def test_explicit_audience_option_survives_without_inferred_expansion(audience):
    service = CreationService(db_path=":memory:", enable_vector_recall=False)
    requirement = service.analyze_requirement("按材料写通知", CreationOptions(audience=audience))
    assert requirement["audience"] == audience
    _, prompt = service.build_routing_prompts("按材料写通知", requirement)
    assert "目标读者：" + audience in prompt


@pytest.mark.parametrize("instruction", [
    "请先告诉我会议时间，再据此写一段可以直接发出的通知。",
    "给各位同事写通知，会议在周三下午举行。",
    "写一篇虚构故事，可以创造人物及彼此关系。",
    "先删除原文最后一句，再加一句邀请语，其余逐字保留。",
])
@pytest.mark.asyncio
async def test_generated_contract_draft_cannot_replace_the_actual_request(instruction):
    generated = contract()
    generated["deliverable"] = "全体员工：明天在总部开会。"
    service = StreamService([json.dumps(generated, ensure_ascii=False)])
    result = await assess_inputs(service, instruction, "原文。", "generate", [])
    assert result["deliverable"] == instruction
    assert "总部" not in json.dumps(result, ensure_ascii=False)
    assert result["acceptance"] == generated["acceptance"]
    assert len(service.calls) == 1
    assert json.loads(service.calls[0]["user_prompt"])["supplied_lines"]["instruction-1"] == instruction


@pytest.mark.parametrize("instruction", [
    "按材料写会议通知。",
    "给各位同事写通知，周三下午在二楼会议室召开会议。",
    "写虚构故事，可以创造人物及组织关系。",
])
def test_writer_uses_sources_instead_of_invented_or_stale_assessment_text(instruction):
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args("", instruction), model_mode="local")
    conditions = contract(["work_context"])
    conditions["deliverable"] = "全体员工周五上午在总部开会。"
    conditions["self_contained_reason"] = "预检推测：没有负责人资料，不能生成正文。"
    conditions["inputs"][0]["reason"] = "预检推测：所有参会者都是公司员工。"
    state.environment.update(input_contract=conditions, input_receipts={"memory_search": "completed"},
        references=[{"source_type": "document", "source_id": 61001, "title": "会议材料",
                     "content": "会议在周三下午于二楼会议室举行，负责人林舟。"}])
    _, prompt = loop._model_prompts(state, {"action": "answer_writer"})
    assert instruction in prompt
    assert "负责人林舟" in prompt
    assert "周五上午" not in prompt
    assert "总部" not in prompt
    assert "预检推测" not in prompt
    attached = json.loads(prompt.split("本轮交付条件与真实检索状态：\n", 1)[1])
    assert attached["contract"] == with_source_scope_check({"deliverable": instruction, "acceptance": conditions["acceptance"]})
    assert attached["receipts"] == {"memory_search": "completed"}
    assert "受众身份" in prompt
    assert "用户明确允许虚构" in prompt


@pytest.mark.parametrize("instruction, supplied", [
    ("请写通知，不补充事实。", "周三下午在二楼会议室开会。"),
    ("给各位同事写通知，不补充事实。", "周三下午在二楼会议室开会。"),
    ("写一篇虚构故事，可以自行设定人物关系。", "蜗牛在月亮上开面包店。"),
])
@pytest.mark.asyncio
async def test_review_keeps_original_facts_and_does_not_treat_contract_drafts_as_sources(instruction, supplied):
    conditions = contract()
    conditions["deliverable"] = "全体员工明天在总部开会。"
    service = StreamService([json.dumps(review(document="line-1"))])
    await review_delivery(service, instruction, supplied, conditions, {"input_context": {
        "conversation": [{"role": "user", "content": supplied}]}})
    payload = json.loads(service.calls[0]["user_prompt"])
    materials = json.dumps(payload["provided_materials"], ensure_ascii=False)
    assert instruction in materials and supplied in materials
    assert "总部" not in materials
    assert "受众身份" in service.calls[0]["system_prompt"]
    assert "用户明确允许虚构" in service.calls[0]["system_prompt"]


@pytest.mark.parametrize("audience", ["研发团队", "家长", ""])
@pytest.mark.asyncio
async def test_explicit_option_has_the_same_provenance_in_assessment_writer_and_review(audience):
    instruction = "按材料写一段会议通知。"
    args = run_args("", instruction)
    args["options"] = CreationOptions(audience=audience)
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args, model_mode="local")
    # A guessed/legacy profile must not establish user provenance.
    state.environment["requirement"]["audience"] = "未授权的业务群体"
    context = loop._input_context(state)
    expected = {"audience": audience} if audience else {}
    assert context["user_options"] == expected
    service = StreamService([json.dumps(contract())])
    conditions = await CreationService.assess_creation_inputs(service, instruction, "", "generate", [],
                                                              user_options=context["user_options"])
    assessment = json.loads(service.calls[0]["user_prompt"])
    assert assessment["user_options"] == expected
    assert (assessment["supplied_lines"].get("user-options-audience-1") or "") == audience
    assert "未授权的业务群体" not in json.dumps(assessment, ensure_ascii=False)
    state.environment["input_contract"] = conditions
    _, prompt = loop._model_prompts(state, {"action": "answer_writer"})
    writing = json.loads(prompt.split("本轮交付条件与真实检索状态：\n", 1)[1])
    assert writing["user_options"] == expected
    reviewer = StreamService([json.dumps(review(document="line-1"))])
    await review_delivery(reviewer, instruction, "会议通知。", conditions, state.environment)
    materials = json.loads(reviewer.calls[0]["user_prompt"])["provided_materials"]
    assert materials["user_options"] == expected
    assert (materials["supplied_lines"].get("user-options-audience-1") or "") == audience
    assert "未授权的业务群体" not in json.dumps(materials, ensure_ascii=False)


def test_cached_context_recovers_explicit_options_without_rewriting_frozen_materials():
    args = run_args("", "继续成文")
    args["options"] = CreationOptions(audience="跨组织项目参加者")
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args, model_mode="local")
    original = {"schema_version": "creation.input-context.v1", "root_request": "原请求",
                "conversation": [{"role": "user", "content": "原材料"}], "creation_brief": "已确认简报"}
    state.environment["input_context"] = dict(original)
    restored = loop._input_context(state)
    assert {key: restored[key] for key in original} == original
    assert restored["user_options"] == {"audience": "跨组织项目参加者"}
    state.options["audience"] = ""
    assert loop._input_context(state)["user_options"] == {}


@pytest.mark.asyncio
async def test_broad_pass_cannot_omit_the_independent_identity_and_scope_check():
    from tests.test_creation_delivery_contract import review as incomplete_review
    service = StreamService([json.dumps(incomplete_review(document="line-1"))] * 2)
    with pytest.raises(OperationError, match="核验最终"):
        await review_delivery(service, "只整理给定材料", "各位同事请参加。", contract(), {})
    assert len(service.calls) == 2
    assert '"missing_ids": ' + json.dumps(SYSTEM_SCOPE_IDS) in service.calls[1]["user_prompt"]


@pytest.mark.asyncio
async def test_system_scope_check_preserves_user_conditions_and_handles_id_collision():
    conditions = contract()
    conditions["acceptance"].append({"id": "source_scope_grounding", "criterion": "用户自定义条件"})
    original = json.loads(json.dumps(conditions))
    response = review(document="line-1")
    response["checks"].append({**response["checks"][1], "id": "source_scope_grounding_"})
    service = StreamService([json.dumps(response)])
    result = await review_delivery(service, "整理正文", "正文", conditions, {})
    assert result["status"] == "pass" and len(service.calls) == 1
    assert conditions == original
    acceptance = json.loads(service.calls[0]["user_prompt"])["contract"]["acceptance"]
    assert acceptance[3:] == conditions["acceptance"]
    assert acceptance[0]["id"] == "source_scope_grounding_"
    checks_schema = service.calls[0]["json_schema"]["properties"]["checks"]
    assert checks_schema['required'] == [item['id'] for item in acceptance]
    assert [item['id'] for item in result['checks']] == checks_schema['required']
    assert all(list(branch['properties']) == ["evidence", "reason", "passed"]
        for item in checks_schema['properties'].values() for branch in item['oneOf'])


@pytest.mark.parametrize("overall", ["pass", "revise"])
@pytest.mark.asyncio
async def test_failed_identity_check_cannot_be_overruled_by_other_passed_conditions(overall):
    response = review(document="line-1")
    response["status"] = overall
    response["checks"][1].update(passed=False, reason="新增受众身份没有输入依据")
    response["corrections"] = []
    service = StreamService([json.dumps(response)] * 2)
    if overall == "pass":
        with pytest.raises(OperationError):
            await review_delivery(service, "整理材料", "各位同事请参加。", contract(), {})
        assert len(service.calls) == 2
    else:
        result = await review_delivery(service, "整理材料", "各位同事请参加。", contract(), {})
        assert result["status"] == "revise"
        assert next(item for item in result['checks'] if item['id'] == 'a')['passed'] is True
        assert next(item for item in result['checks'] if item['id'] == 'source_scope_grounding')['passed'] is False
        assert "新增受众身份没有输入依据" in "\n".join(result["corrections"])
        assert len(service.calls) == 1


def test_real_identity_controls_only_change_explicit_authorization_for_same_notice():
    from scripts.evaluate_creation_delivery import IDENTITY_REVIEW_CASES
    unknown, explicit, fiction = IDENTITY_REVIEW_CASES[:3]
    assert unknown["document"] == explicit["document"]
    assert unknown["instruction"] == explicit["instruction"]
    assert unknown["contract"] == explicit["contract"]
    assert unknown["environment"]["input_context"]["conversation"] == explicit["environment"]["input_context"]["conversation"]
    assert unknown["environment"]["input_context"]["user_options"] == {}
    assert explicit["environment"]["input_context"]["user_options"] == {"audience": "同事"}
    assert unknown["expected"] == ["revise"] and explicit["expected"] == ["pass"]
    assert "虚构" in fiction["instruction"] and fiction["expected"] == ["pass"]
    assert all(item["id"] != "source_scope_grounding" for item in unknown["contract"]["acceptance"])


def test_real_process_controls_keep_candidate_and_only_add_source_authorization():
    from scripts.evaluate_creation_delivery import IDENTITY_REVIEW_CASES
    unknown, explicit = IDENTITY_REVIEW_CASES[3:5]
    assert unknown["document"] == explicit["document"]
    assert unknown["instruction"] == explicit["instruction"]
    assert unknown["contract"] == explicit["contract"]
    assert unknown["expected"] == ["revise"] and explicit["expected"] == ["pass"]
    assert unknown["required_failed_checks"] == ["source_attributes_grounding", "source_obligations_grounding"]
    unknown_sources = unknown["environment"]["input_context"]["conversation"]
    explicit_sources = explicit["environment"]["input_context"]["conversation"]
    assert explicit_sources[:-1] == unknown_sources
    assert "例会" in explicit_sources[-1]["content"]
    assert "提前联系相关负责人报备" in explicit_sources[-1]["content"]


def test_neutral_invitation_control_only_removes_unprovided_identity():
    from scripts.evaluate_creation_delivery import IDENTITY_REVIEW_CASES
    unknown = IDENTITY_REVIEW_CASES[0]
    neutral = next(item for item in IDENTITY_REVIEW_CASES if item["id"] == "accept-neutral-meeting-invitation")
    assert neutral["document"] == unknown["document"].replace("各位同事，", "", 1)
    assert "请准时参加。" in neutral["document"]
    assert all(neutral[key] == unknown[key] for key in ("instruction", "environment", "contract"))
    assert neutral["expected"] == ["pass"] and unknown["expected"] == ["revise"]


@pytest.mark.asyncio
async def test_delivery_repair_uses_review_source_snapshot_and_excludes_environment_analysis():
    original = "# 标题\n\n需保留的旧正文。"
    args = run_args(original, "修正现有材料。")
    args.update(root_request="用户根要求", conversation=[
        {"role": "user", "content": "实际材料为42项。"},
        {"role": "assistant", "content": "仅为建议99项。"}],
        options=CreationOptions(audience="研发团队"))
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args, model_mode="local")
    state.current_document = "错误候选声称999项。"
    state.environment.update(input_contract=contract(), input_base_document=original,
        requirement={"entity_context": "统计噪声", "retrieval_plan": "检索画像噪声"},
        chapter_visual_plan="空图表噪声", references=[
            {"content": "可用检索原文42项。", "can_use": True},
            {"content": "不可用数字1000项。", "can_use": False}],
        delivery_review={"status": "revise", "corrections": ["删除无依据的999项"],
            "checks": [{"id": "a", "passed": False, "reason": "数量错误", "evidence": "999项"}]})
    system, prompt = loop._model_prompts(state, {"action": "polisher", "delivery_repair": True})
    payload = json.loads(prompt)
    service = StreamService([json.dumps(review(document="line-1"))])
    await review_delivery(service, state.user_message, state.current_document, contract(), state.environment)
    reviewed = json.loads(service.calls[0]["user_prompt"])
    assert payload["provided_materials"] == reviewed["provided_materials"]
    assert payload["provided_materials"]["original_document"] == original
    assert payload["provided_materials"]["conversation"] == args["conversation"]
    assert payload["provided_materials"]["user_options"] == {"audience": "研发团队"}
    assert payload["candidate_document"] == state.current_document
    assert "999项" not in json.dumps(payload["provided_materials"], ensure_ascii=False)
    assert "可用检索原文42项" in prompt and "不可用数字1000项" not in prompt
    assert all(noise not in prompt for noise in ("统计噪声", "检索画像噪声", "空图表噪声"))
    assert "不能作为新增事实来源" in system and "创作授权" in system
    assert "删除无依据的999项" in json.dumps(payload["failed_review"], ensure_ascii=False)


def test_delivery_repair_keeps_patch_baseline_targets_and_explicit_skill_contract():
    from creation.operations import document_nodes
    base = "# 标题\n\n## 可编辑\n原词。\n\n## 保留\n原封不动。\n"
    args = run_args(base, "只改可编辑段。")
    args["selected_skills"] = [{"id": "explicit-skill", "title": "用户选定规则", "skillInstructions": "保留代码围栏和来源链接。"}]
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args, model_mode="local")
    targets = [document_nodes(base)[1]["id"]]
    state.current_document = base.replace("原词", "错误新词")
    state.environment.update(input_contract=contract(), input_base_document=base,
        operation={"kind": "transform", "targets": targets})
    system, prompt = loop._model_prompts(state, {"action": "patch_writer", "delivery_repair": True, "patch_base_document": base})
    payload = json.loads(prompt)
    assert payload["document"] == base and payload["candidate_document"] == state.current_document
    assert payload["allowed_targets"] == targets and payload["nodes"] == document_nodes(base)
    assert "保留代码围栏和来源链接" in json.dumps(payload["skill_constraints"], ensure_ascii=False)
    assert "只输出 JSON 对象" in system and "只输出修正后的完整 Markdown" not in system


def test_delivery_repair_preserves_explicit_fiction_authorization_and_answer_output_mode():
    instruction = "继续这个虚构故事，可自由设定角色和情节。"
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args("", instruction), model_mode="local")
    state.environment["input_contract"] = contract()
    system, prompt = loop._model_prompts(state, {"action": "answer_writer", "delivery_repair": True})
    assert json.loads(prompt)["provided_materials"]["user_instruction_and_supplied_facts"] == instruction
    assert "用户授权虚构或方案设计" in system
    assert "本轮只输出修正后的问题回答，不改写用户文档" in system


def test_answer_repair_targets_the_reviewed_response_and_preserves_user_document():
    original = "用户原文必须不变。"
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args(original, "只回答时间，不改文档。"), model_mode="local")
    state.environment.update(input_contract=contract(), input_base_document=original,
        operation={"kind": "answer", "response": "错误回答：周五下午。"})
    _, prompt = loop._model_prompts(state, {"action": "answer_writer", "delivery_repair": True})
    payload = json.loads(prompt)
    assert payload["candidate_document"] == "错误回答：周五下午。"
    assert payload["provided_materials"]["original_document"] == original
    assert state.current_document == original


@pytest.mark.parametrize("field,value,normalized", [
    ("writingGuidelines", ["每段仅一句。"], "voice_style"),
    ("writingDesign", "先结论再证据。", "writing_design"),
    ("titleDesignStyle", ["标题仅两层。"], "title_design_style"),
    ("imageGeneration", "仅使用用户给定的图。", "image_generation"),
])
def test_delivery_repair_keeps_existing_normalized_skill_design_rules(field, value, normalized):
    args = run_args("正文", "按已选规则修正。")
    args["selected_skills"] = [{"id": "selected", "title": "明确规则", field: value,
                                "exampleDocument": "虚构示例不能作为依据"}]
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args, model_mode="local")
    _, prompt = loop._model_prompts(state, {"action": "polisher", "delivery_repair": True})
    assert json.loads(prompt)["skill_constraints"][0][normalized] == value
    assert "虚构示例不能作为依据" not in prompt


@pytest.mark.parametrize("selection", [
    {"applied_skills": []},
    {"applied_skills": [], "routed_skill_ids": ["candidate"]},
    {"routed_skill_ids": []},
    {"skill_admission": []},
    {"skill_admission": [{"skill_id": "candidate", "admitted": False}]},
    {"routing_decision": {"operation": {"kind": "generate"}}},
    {"candidate_routing_decision": {"operation": {"kind": "generate"}}},
    {"explicit_skill_ids": []},
    {"routed_skill_ids": [], "skill_admission": [], "explicit_skill_ids": []},
])
def test_delivery_repair_and_regeneration_do_not_apply_unselected_candidate_catalog(selection):
    loop = CreationAgentLoop(DeliveryService())
    args = run_args("# 文档\n\n需要修正的正文。", "按本轮需求修正。")
    args["selected_skills"] = [{"id": "candidate", "title": "无关候选模板",
                                "skillInstructions": "候选目录里的规则不得进入修复提示"}]
    state = loop._new_state(**args, model_mode="local")
    state.environment.update(selection)
    state.environment["input_contract"] = contract()
    _, prompt = loop._model_prompts(state, {"action": "polisher", "delivery_repair": True})
    assert "skill_constraints" not in json.loads(prompt)
    assert "候选目录里的规则" not in prompt
    regenerated = loop._delivery_regeneration_input(state)
    assert "skill_constraints" not in regenerated
    assert "候选目录里的规则" not in json.dumps(regenerated, ensure_ascii=False)


@pytest.mark.parametrize("selection", [
    {"routed_skill_ids": ["adopted"]},
    {"skill_admission": [{"skill_id": "adopted", "admitted": True},
                          {"skill_id": "candidate", "admitted": False}]},
    {"routing_decision": {"operation": {"kind": "generate", "constraint_skill_ids": ["adopted"]}}},
    {"routing_decision": {"operation": {"kind": "execute_skill", "skill_ids": ["adopted"]}}},
    {"explicit_skill_ids": ["adopted"]},
])
def test_delivery_repair_loads_only_recorded_selected_ids_and_normalizes_their_rules(selection):
    loop = CreationAgentLoop(DeliveryService())
    args = run_args("正文。", "修正已采用规则。")
    args["selected_skills"] = [
        {"id": "adopted", "title": "已采用规则", "writingDesign": "每段先交代事实依据。",
         "exampleDocument": "样例故事不能成为事实"},
        {"id": "candidate", "title": "候选目录", "writingDesign": "无关候选约束"}]
    state = loop._new_state(**args, model_mode="local")
    state.environment.update(selection)
    constraints = loop._repair_skill_constraints(state)
    assert [item["id"] for item in constraints] == ["adopted"]
    assert constraints[0]["writing_design"] == "每段先交代事实依据。"
    assert "样例故事" not in json.dumps(constraints, ensure_ascii=False)
    assert "无关候选" not in json.dumps(constraints, ensure_ascii=False)


def test_applied_skill_receipt_is_normalized_without_falling_back_to_the_catalog():
    loop = CreationAgentLoop(DeliveryService())
    args = run_args("正文。", "修正已采用规则。")
    args["selected_skills"] = [{"id": "candidate", "title": "无关候选", "writingDesign": "不得自动应用"}]
    state = loop._new_state(**args, model_mode="local")
    state.environment.update(applied_skills=[{
        "id": "adopted", "name": "已有应用记录", "writingGuidelines": ["每段一句"],
        "strict_structure": False, "workflow_role": "support", "exampleDocument": "示例不进入正文",
    }], routed_skill_ids=[])
    constraints = loop._repair_skill_constraints(state)
    assert [item["id"] for item in constraints] == ["adopted"]
    assert constraints[0]["voice_style"] == ["每段一句"]
    assert constraints[0]["strict_structure"] is False
    assert constraints[0]["workflow_role"] == "support"
    assert "示例" not in json.dumps(constraints, ensure_ascii=False)


@pytest.mark.asyncio
async def test_repair_chain_keeps_prior_problems_until_whole_review_passes():
    from creation.delivery_contract import prior_delivery_failures
    instruction, document = "只按材料写通知。", "各位同事，请提前报备。"
    first = review(document="line-1")
    first["status"] = "revise"
    for check in first["checks"][1:]:
        check.update(passed=check["id"] != "source_scope_grounding")
    first["checks"][1]["reason"] = "同事称谓未获用户提供"
    second = review(document="line-1")
    second["status"] = "revise"
    second["checks"][3].update(passed=False, reason="缺席报备步骤未获提供")
    service = StreamService([json.dumps(first), json.dumps(second), json.dumps(review(document="line-1"))])
    environment = {"input_base_document": ""}
    await review_delivery(service, instruction, document, contract(), environment)
    environment["delivery_repair_count"] = 1
    latest = await review_delivery(service, instruction, document, contract(), environment)
    prior = prior_delivery_failures(instruction, environment, contract())
    assert "同事称谓未获用户提供" in json.dumps(prior, ensure_ascii=False)
    assert "缺席报备步骤未获提供" in json.dumps(prior, ensure_ascii=False)
    payload = json.loads(service.calls[1]["user_prompt"])
    assert "同事称谓未获用户提供" not in payload["contract"]["acceptance"][0]["criterion"]
    assert "同事称谓未获用户提供" not in json.dumps(payload["prior_failed_checks"], ensure_ascii=False)
    assert any(item["check_id"] == "source_scope_grounding" for item in payload["prior_failed_checks"]["checks"])
    assert "同事称谓未获用户提供" not in json.dumps(payload["source_catalog"], ensure_ascii=False)
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args(document, instruction), model_mode="local")
    state.environment.update(environment, input_contract=contract(), delivery_review=latest)
    _, prompt = loop._model_prompts(state, {"action": "polisher", "delivery_repair": True})
    assert [item["previous_quote"] for item in json.loads(prompt)["prior_failed_checks"]["checks"]] == [item["evidence"] for item in prior["checks"]]
    # New, explicit source can correct an old judgment even with the same text.
    environment["input_context"] = {"user_options": {"audience": "同事"}, "conversation": [
        {"role": "user", "content": "明确安排：缺席需要提前报备。"}]}
    environment["delivery_repair_count"] = 2
    accepted = await review_delivery(service, instruction, document, contract(), environment)
    assert accepted["status"] == "pass" and "delivery_pending_review" not in environment
    assert json.loads(service.calls[2]["user_prompt"])["prior_failed_checks"]["checks"]


@pytest.mark.asyncio
async def test_same_condition_keeps_pre_audit_findings_and_all_corrections_in_pending_review():
    from tests.test_creation_delivery_contract import audited_report
    from creation.delivery_contract import prior_delivery_failures
    document, instruction = "同事请联系负责人。", "按给定材料写通知。"
    value = audited_report(document, "unsupported", "identity")
    value["status"] = "revise"
    for check in value["checks"]:
        check["evidence"] = "line-1"
    value["checks"][1].update(passed=False, reason="同事身份无原文支持")
    next(iter(value["source_audit"].values()))["text"] = "负责人"
    service = StreamService([json.dumps(value, ensure_ascii=False)])
    environment = {}
    await review_delivery(service, instruction, document, contract(), environment)
    environment["delivery_repair_count"] = 1
    prior = prior_delivery_failures(instruction, environment, contract())
    reasons = [item["reason"] for item in prior["checks"] if item["id"] == "source_scope_grounding"]
    assert len(reasons) >= 2
    assert "同事身份无原文支持" in reasons and any("负责人" in reason for reason in reasons)
    assert any("负责人" in item for item in prior["corrections"])


@pytest.mark.parametrize("change", ["new_request", "new_base", "new_contract", "new_chain"])
@pytest.mark.asyncio
async def test_prior_findings_do_not_leak_into_another_request_or_repair_chain(change):
    from creation.delivery_contract import prior_delivery_failures
    instruction = "本轮任务"
    conditions = contract()
    environment = {"input_base_document": "原文"}
    service = StreamService([json.dumps(review("revise", document="line-1"))])
    await review_delivery(service, instruction, "候选", conditions, environment)
    environment["delivery_repair_count"] = 1
    assert prior_delivery_failures(instruction, environment, conditions)["checks"]
    if change == "new_request": instruction = "另一轮任务"
    if change == "new_base": environment["input_base_document"] = "另一份原文"
    if change == "new_contract": conditions["acceptance"][0]["criterion"] = "新要求"
    if change == "new_chain": environment["delivery_repair_count"] = 0
    assert prior_delivery_failures(instruction, environment, conditions) == {"checks": [], "corrections": []}


@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.asyncio
async def test_writer_repair_and_review_share_the_exact_mandatory_source_boundary(repair):
    from creation.delivery_contract import SOURCE_SCOPE_CHECKS
    instruction = "写一段会议通知，不补充新事实。"
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args("", instruction), model_mode="local")
    conditions = contract()
    state.environment.update(input_contract=conditions, delivery_review={"status": "revise",
        "corrections": ["修改称谓"], "checks": [{"id": "source_scope_grounding", "passed": False,
        "reason": "称谓无依据", "evidence": "各位同事"}]})
    _, prompt = loop._model_prompts(state, {"action": "answer_writer", "delivery_repair": repair})
    writing = json.loads(prompt if repair else prompt.split("本轮交付条件与真实检索状态：\n", 1)[1])
    assert writing["contract"]["acceptance"][:3] == list(SOURCE_SCOPE_CHECKS)
    scope_text = "\n".join(item["criterion"] for item in SOURCE_SCOPE_CHECKS)
    assert "办理流程" in scope_text and "前置条件" in scope_text
    assert "角色是否存在" in scope_text and "事件性质" in scope_text
    assert "方案设计" in scope_text
    service = StreamService([json.dumps(review(document="line-1"))])
    await review_delivery(service, instruction, "会议通知", conditions, state.environment)
    acceptance = json.loads(service.calls[0]["user_prompt"])["contract"]["acceptance"]
    assert acceptance == writing["contract"]["acceptance"]
    if repair:
        assert "修改称谓" in prompt
        assert instruction in prompt


def test_historical_evidence_exposes_change_without_reclassifying_current_facts():
    import copy
    from creation.delivery_contract import delivery_failure_context
    prior = {"checks": [
        {"id": "calendar_grounding", "reason": "原来增加了本周", "evidence": "本周三下午"},
        {"id": "source_scope_grounding", "reason": "原称谓未提供", "evidence": "各位同事"},
        {"id": "a", "reason": "旧引用也可能是误报", "evidence": "周三下午"}],
        "corrections": ["旧修改说明"]}
    original = copy.deepcopy(prior)
    result = delivery_failure_context(prior, "各位同事，周三下午在二楼会议室开会。")
    assert [item["location_state"] for item in result["checks"]] == ["changed_or_removed", "exact_match", "exact_match"]
    assert all("passed" not in item for item in result["checks"])
    assert [item["previous_quote"] for item in result["checks"]] == [item["evidence"] for item in prior["checks"]]
    assert all("reason" not in item for item in result["checks"])
    assert "corrections" not in result and prior == original


def test_historical_location_whitelist_keeps_all_current_matches_and_no_old_arguments():
    from creation.delivery_contract import delivery_failure_context
    prior = {'checks': [
        {'id': 'a', 'reason': 'OLD_REASON_MUST_NOT_APPEAR', 'evidence': '同事'},
        {'id': 'a', 'reason': '另一个无定位问题', 'evidence': ''},
        {'id': 'a', 'reason': '旧跨段引文', 'evidence': '首行\n\n第二行'}], 'corrections': ['OLD_CORRECTION']}
    result = delivery_failure_context(prior, '同事在首行\n\n第二行同事\n第三行同事')
    assert result['checks'][0]['current_line_ids'] == ['line-1', 'line-3', 'line-4']
    assert result['checks'][1]['location_state'] == 'unavailable' and not result['checks'][1]['current_line_ids']
    assert result['checks'][2]['current_line_ids'] == ['line-1', 'line-3']
    assert all(set(item) == {'issue_id', 'check_id', 'previous_quote', 'location_state', 'current_line_ids'} for item in result['checks'])
    assert 'OLD_' not in json.dumps(result)
    assert len({item['issue_id'] for item in result['checks']}) == 3


@pytest.mark.asyncio
async def test_independent_review_does_not_consume_author_analysis_as_evidence():
    environment = {'author_analysis': {'claimed_source': 'WRITER_SELF_CLAIM'},
                   'delivery_repair_result': {'step_id': 'old', 'recheck_unchanged': True}}
    service = StreamService([json.dumps(review(document='line-1'))])
    await review_delivery(service, '写通知', '会议通知', contract(), environment)
    payload = json.loads(service.calls[0]['user_prompt'])
    assert 'WRITER_SELF_CLAIM' not in json.dumps(payload)
    assert 'author_analysis' not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize('candidate,source', [
    ('2026年9月8日举行活动。', '2026/09/08举行活动。'),
    ('星期三下午举行活动。', '周三下午举行活动。'),
    ('本周三下午举行活动。', '这周三下午举行活动。'),
])
async def test_explicit_calendar_equivalence_uses_selected_source_without_extra_condition(candidate, source):
    from tests.test_creation_delivery_contract import audited_report
    value = audited_report(candidate, 'source_supported', 'attribute', 'conversation-user-0')
    for check in value['checks']:
        check['evidence'] = 'line-1'
    service = StreamService([json.dumps(value)])
    result = await review_delivery(service, '整理已给的活动时间。', candidate, contract(), {
        'input_context': {'conversation': [{'role': 'user', 'content': source}]}})
    assert result['status'] == 'pass'
    assert json.loads(service.calls[0]['user_prompt'])['required_check_ids'] == SYSTEM_SCOPE_IDS + ['a']


def test_actual_v21_calendar_audits_keep_same_materials_and_revise_then_pass():
    from pathlib import Path
    from creation.delivery_contract import _apply_source_audit, _decode_review_checks, source_audit_segments
    records = json.loads((Path(__file__).parent / 'fixtures' / 'creation_calendar_review_v21.json').read_text())
    assert records[0]['provided_materials'] == records[1]['provided_materials']
    assert records[0]['provided_materials']['conversation'][0] == {'role': 'user',
        'content': '会议定在周三下午，地点二楼会议室。只使用这些材料，不检索、不补充新事实。'}
    statuses = []
    for record in records:
        document, conditions = record['candidate_document'], record['contract']
        raw = record['raw_review']
        _decode_review_checks(raw, [item['id'] for item in conditions['acceptance']])
        lines = {'line-{}'.format(index): line for index, line in enumerate(document.splitlines(), 1) if line.strip()}
        for check in raw['checks']:
            check['evidence'] = lines.get(check['evidence'], '')
        report = _apply_source_audit(raw, source_audit_segments(document), record['provided_materials'], conditions, document)
        assert report['status'] == record['expected_status']
        statuses.append(report['status'])
    assert statuses == ['revise', 'pass']
    assert records[1]['candidate_document'] == '# 会议通知\n\n会议时间定于周三下午，地点为二楼会议室。特此通知。'


@pytest.mark.asyncio
@pytest.mark.parametrize('raises', [False, True])
async def test_delivery_evaluation_records_initial_fixture_without_runtime_mutation(tmp_path, monkeypatch, raises):
    import copy
    from types import SimpleNamespace
    from scripts import evaluate_creation_delivery as evaluation
    before = copy.deepcopy(evaluation.IDENTITY_REVIEW_CASES)
    identity = 'accept-explicit-audience-option'
    original = next(case for case in before if case['id'] == identity)
    monkeypatch.setattr(evaluation, 'RecordingService', lambda **kwargs: SimpleNamespace(replies=[], calls=[]))
    async def mutate_runtime(service, instruction, document, conditions, environment):
        assert environment == original['environment'] and conditions == original['contract']
        environment['delivery_pending_review'] = {'runtime': 'only'}
        environment['input_context']['conversation'][0]['content'] = 'runtime must not alter fixture'
        conditions['acceptance'].clear()
        if raises:
            raise OperationError('CREATION_DELIVERY_UNVERIFIED', 'stub failure')
        return {'status': 'pass', 'checks': [], 'corrections': []}
    monkeypatch.setattr(evaluation, 'review_delivery', mutate_runtime)
    output = tmp_path / 'evaluation.json'
    if raises:
        with pytest.raises(SystemExit):
            await evaluation.evaluate(output, review_only=True, selected_cases=[identity])
    else:
        await evaluation.evaluate(output, review_only=True, selected_cases=[identity])
    assert evaluation.IDENTITY_REVIEW_CASES == before
    assert json.loads(output.read_text())['cases'][0]['fixture'] == original
