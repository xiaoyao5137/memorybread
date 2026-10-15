import copy
import json

import pytest

from creation.agent_loop import CreationAgentLoop
from creation.brainstorm_chapters import (
    chapter_context, chapter_decisions, chapter_delivery_scope, chapter_goal, chapter_prompt_context,
    chapter_routing_context, normalize_chapter_document, validate_chapter_document,
)
from creation.operations import OperationError, document_nodes
from tests.test_creation_delivery_contract import DeliveryService
from tests.test_creation_operations import run_args
from tests.test_creation_intent_restatements import Model, audit, piece


def brief(index=2):
    decisions = [{"question_id": "global", "dimension": "整体方向", "summary": "采用温暖视角", "source": "user"},
                 {"question_id": "first", "dimension": "第一章设计", "summary": "回家途中遇见旧友", "source": "user"},
                 {"question_id": "current", "dimension": "当前章设计", "summary": "用一句对白完成收束", "source": "user"}]
    return {"root_request": "构思三章短篇", "decisions": decisions,
            "brief_markdown": "不能把这份旧简报当作新章事实：未经选择的秘密候选",
            "chapter_context": {"id": "c{}".format(index), "title": "转折" if index == 2 else "初见",
                "goal": "展开人物行动" if index == 2 else "介绍人物", "index": index, "total": 3,
                "confirmed_decisions": [copy.deepcopy(decisions[-1])],
                "outline": [{"id": "c1", "title": "初见", "goal": "介绍人物"},
                            {"id": "c2", "title": "转折", "goal": "展开人物行动"},
                            {"id": "c3", "title": "重逢", "goal": "收束故事"}]}}


BASE = "# 归途\n\n## 初见\n\n灯亮起来的时候，小林走过巷口。"
DOCUMENT = BASE + "\n\n## 转折\n\n他停下来，轻声说：原来你还记得这条路。"


def single_brief(title="任意成文单元"):
    value = brief(1)
    value["root_request"] = "写完整故事，保留开端、过程和结尾"
    chapter = value["chapter_context"]
    chapter.update(title=title, goal="完整落实当前需求和全部有效确认设计", total=1)
    chapter["outline"] = [{key: chapter[key] for key in ("id", "title", "goal")}]
    return value


@pytest.mark.parametrize("title", ["创作正文", "故事", "任意成文单元"])
def test_single_unit_has_full_current_coverage_without_forcing_its_metadata_heading(title):
    from creation.brief_context import effective_brief_decisions
    snapshot = single_brief(title)
    before = copy.deepcopy(snapshot)
    assert chapter_decisions(snapshot) == effective_brief_decisions(snapshot)
    assert {item["question_id"] for item in chapter_decisions(snapshot)} == {"global", "first", "current"}
    text = chapter_prompt_context(snapshot)
    assert "本轮完成唯一成文单元的完整正文" in text
    assert "当前有效创作目标是本轮完整正文的实际成文范围" in text
    assert "不要求本轮完成整篇" not in text
    assert "章节标题文本为：" not in text
    assert "不把写作单元名称强制用作正文标题" in text
    assert "所选技能" in text and "内部分节" in text
    assert "回家途中遇见旧友" in text and "用一句对白完成收束" in text
    assert snapshot == before


def test_single_unit_current_edits_clears_and_unconfirmed_sources_keep_their_authority():
    snapshot = single_brief()
    snapshot["brief_edits"] = {"root_request": "当前有效全文需求", "global": "采用冷静视角", "first": ""}
    snapshot["decisions"].extend([
        {"question_id": "excluded", "summary": "旧资料", "source": "user_excluded"},
        {"question_id": "assumption", "summary": "待核验设想", "source": "agent_assumption"}])
    snapshot["chapter_context"]["confirmed_decisions"].append({
        "question_id": "forged", "summary": "嵌入的未确认候选", "source": "user"})
    current = chapter_decisions(snapshot)
    by_id = {item["question_id"]: item for item in current}
    assert by_id["global"]["value"] == "采用冷静视角"
    assert by_id["first"]["source"] == "user_cleared"
    assert by_id["excluded"]["source"] == "user_excluded"
    assert by_id["assumption"]["source"] == "agent_assumption"
    assert "forged" not in by_id
    text = chapter_prompt_context(snapshot)
    assert "当前有效全文需求" in text and "构思三章短篇" not in text
    assert "采用温暖视角" not in text and "回家途中遇见旧友" not in text


@pytest.mark.parametrize("document", [
    "# 用户要求的作品名\n\n## 开端\n\n人物踏上归途。\n\n## 结尾\n\n人物回到家中。",
    "亲爱的朋友：\n\n归途中的问候，愿你一路平安。",
])
def test_single_unit_accepts_actual_title_and_sections_or_titleless_form(document):
    chapter = chapter_context(single_brief())
    validate_chapter_document(document, "", chapter, completed=True)
    assert normalize_chapter_document(document, "", chapter) == document


@pytest.mark.parametrize("document", ["", "# 用户要求的作品名", "\n## 开端\n\n"])
def test_single_unit_still_requires_actual_body_to_complete(document):
    with pytest.raises(OperationError):
        validate_chapter_document(document, "", chapter_context(single_brief()), completed=True)


def test_single_unit_preserves_existing_body_and_uses_full_coverage_in_delivery_context():
    snapshot = single_brief()
    chapter = chapter_context(snapshot)
    candidate = BASE + "\n\n## 后续正文\n\n新正文完整保留已定设计。"
    validate_chapter_document(candidate, BASE, chapter, completed=True)
    with pytest.raises(OperationError):
        validate_chapter_document(candidate.replace("灯亮起来", "天黑之后"), BASE, chapter, completed=True)
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args(BASE, snapshot), model_mode="local")
    context = loop._input_context(state)
    assert context["root_request"] == chapter_goal(chapter)
    assert {item["question_id"] for item in context["brainstorm_decisions"]} == {"global", "first", "current"}
    assert context["chapter_delivery_scope"]["unwritten_chapters"] == []
    assert "完整正文与内部分节" in context["chapter_delivery_scope"]["scope_rule"]
    assert context["chapter_delivery_scope"]["protected_previous_body_characters"] == len(BASE)


def test_multi_unit_scope_and_required_heading_are_unchanged():
    snapshot = brief()
    chapter = chapter_context(snapshot)
    assert "本轮仅追加第 2/3 章《转折》" in chapter_goal(chapter)
    assert [item["question_id"] for item in chapter_decisions(snapshot)] == ["current"]
    assert "当前有效创作目标只提供全篇背景，不要求本轮完成整篇" in chapter_prompt_context(snapshot)
    assert "章节标题文本为：转折" in chapter_prompt_context(snapshot)
    with pytest.raises(OperationError):
        validate_chapter_document(BASE + "\n\n无本章标题的新正文。", BASE, chapter, completed=True)


def args(doc=BASE, snapshot=None):
    return {**run_args(doc, "落实刚刚确认的本章设计，只在文末追加当前章节并保留全部原文"),
            "root_request": "构思三章短篇", "creation_mode": "brainstorm", "creation_brief": snapshot or brief()}


def test_current_chapter_scope_keeps_prior_decisions_as_context_only():
    snapshot = brief()
    text = chapter_prompt_context(snapshot)
    assert "温暖视角" in text and "用一句对白完成收束" in text
    assert "未经选择的秘密候选" not in text
    assert "此前有效决定仅作为" in text
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args(), model_mode="local")
    context = loop._input_context(state)
    assert "仅追加第 2/3 章《转折》" in context["root_request"]
    assert [item["question_id"] for item in context["brainstorm_decisions"]] == ["current"]
    assert "构思三章短篇" == state.root_request
    assert not loop._uses_brief_sections(state, {"action": "writer"})


def test_cleared_or_unconfirmed_embedded_answers_cannot_be_resurrected():
    snapshot = brief()
    snapshot["brief_edits"] = {"current": ""}
    snapshot["chapter_context"]["confirmed_decisions"].append({
        "question_id": "not-confirmed", "summary": "偷加的候选", "source": "user"})
    selected = chapter_decisions(snapshot)
    assert len(selected) == 1 and selected[0]["source"] == "user_cleared"
    assert "偷加的候选" not in chapter_prompt_context(snapshot)
    assert "用一句对白完成收束" not in chapter_prompt_context(snapshot)


@pytest.mark.parametrize("mutation", [
    lambda value: value["chapter_context"].update(index=0),
    lambda value: value["chapter_context"].update(total=5),
    lambda value: value["chapter_context"].update(id="c1"),
    lambda value: value["chapter_context"].update(confirmed_decisions="candidate"),
    lambda value: value["chapter_context"]["outline"][0].update(id="c2"),
    lambda value: value["chapter_context"]["outline"][2].update(title="## # 第三章 《转折》"),
])
def test_invalid_chapter_binding_fails_closed(mutation):
    snapshot = brief()
    mutation(snapshot)
    with pytest.raises(OperationError):
        chapter_context(snapshot)


@pytest.mark.parametrize("candidate", [
    DOCUMENT.replace("灯亮起来", "天黑下去"),
    DOCUMENT + "\n\n## 重逢\n尚未确认的未来章节正文。",
    DOCUMENT + "\n\n## 三、重逢\n尚未确认的未来章节正文。",
    BASE,
    BASE + "\n\n## 转折",
])
def test_no_earlier_body_mutation_future_chapter_or_empty_completion(candidate):
    with pytest.raises(OperationError):
        validate_chapter_document(candidate, BASE, chapter_context(brief()), completed=True)
    validate_chapter_document(DOCUMENT, BASE, chapter_context(brief()), completed=True)


def test_invalid_published_mutation_rolls_back_checkpoint_body():
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args(), model_mode="local")
    corrupted = DOCUMENT.replace("灯亮起来", "丢失原文")
    state.current_document = corrupted
    state.environment["document"] = corrupted
    with pytest.raises(OperationError):
        loop._event(state, "document.patch.applied", "修改", data={"content": corrupted})
    assert state.current_document == state.environment["document"] == BASE


class ChapterService(DeliveryService):
    async def stream_agent_document(self, **kwargs):
        self.writes += 1
        yield DOCUMENT


@pytest.mark.asyncio
async def test_chapter_runs_normal_generation_delivery_and_preserves_prior_body():
    service = ChapterService()
    events = [event async for event in CreationAgentLoop(service).run(**args())]
    assert events[-1]["type"] == "run.completed"
    assert events[-1]["data"]["document"] == DOCUMENT
    assert events[-1]["data"]["delivery_review"]["status"] == "pass"
    assert any(event["type"] == "delivery.checked" and event["data"]["review"]["status"] == "pass"
               for event in events)
    assert service.writes == 1 and service.assessed == 1 and len(service.checked) == 1
    checks = service.checked[0]["input_contract"]["acceptance"]
    checks_text = str(checks)
    assert "用一句对白完成收束" in checks_text
    assert "回家途中遇见旧友" not in checks_text
    assert "围绕原始创作目标完整成文" in checks_text
    assert "本轮仅追加第 2/3 章" in checks_text


@pytest.mark.asyncio
async def test_chapter_missing_required_material_remains_blocked_before_writing():
    service = ChapterService(["work_context"])
    def fail(*unused):
        raise RuntimeError("source unavailable")
    service.retrieve_references = fail
    events = []
    with pytest.raises(OperationError, match="资料检索失败"):
        async for event in CreationAgentLoop(service).run(**args()):
            events.append(event)
    assert service.writes == 0
    assert not any(event["type"] == "run.completed" for event in events)


@pytest.mark.asyncio
async def test_chapter_failed_review_does_not_complete_or_advance():
    service = ChapterService(reports=["blocked"])
    events = []
    with pytest.raises(OperationError):
        async for event in CreationAgentLoop(service).run(**args()):
            events.append(event)
    assert not any(event["type"] == "run.completed" for event in events)
    assert len(service.checked) == 1
    assert not any(event["type"] == "delivery.checked" and event["data"]["review"]["status"] == "pass"
                   for event in events)


def test_chapter_does_not_discard_real_saved_open_flags():
    snapshot = brief()
    snapshot["open_flags"] = ["当前章引用的访谈原文尚缺"]
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args(snapshot=snapshot), model_mode="local")
    assert loop._brainstorm_open_flags(state) == snapshot["open_flags"]
    assert loop._brief_writing_scope(state)["open_flags"] == snapshot["open_flags"]


@pytest.mark.asyncio
async def test_chapter_cannot_bypass_delivery_through_literal_patch_route():
    service = ChapterService(operation={"kind": "patch", "patches": [{"action": "insert",
        "target": {"text": BASE}, "position": "after", "content": "## 转折\n跳过审核的候选正文。"}]})
    events = []
    with pytest.raises(OperationError, match="必须经过成文与交付核验"):
        async for event in CreationAgentLoop(service).run(**args()):
            events.append(event)
    assert not any(event["type"] in {"document.patch.applied", "run.completed"} for event in events)


def test_current_chapter_completion_requires_review_of_exact_published_body():
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args(), model_mode="local")
    state.environment["delivery_review"] = {"status": "pass"}
    state.environment["delivery_checked_hash"] = loop._document_hash(BASE)
    with pytest.raises(OperationError, match="最终正文交付核验"):
        loop._event(state, "run.completed", "完成", data={"document": DOCUMENT})


@pytest.mark.asyncio
async def test_append_transform_keeps_current_chapter_brief_and_exact_decision_coverage():
    class InspectingService(ChapterService):
        async def assess_creation_inputs(self, *positional, **keywords):
            self.assessment_context = keywords
            return await super().assess_creation_inputs(*positional, **keywords)
    service = InspectingService()
    loop = CreationAgentLoop(service)
    state = loop._new_state(**args(), model_mode="local")
    state.user_message = "根据已确认的创作简报，只创作第2章《转折》，追加本章完整内容，不要提前写后续章节。"
    record = {"tools": [], "agents": [], "operation": {
        "kind": "transform", "targets": [document_nodes(BASE)[-1]["id"]]}, "source": "model"}
    _ = [event async for event in loop._apply_routing_decision(state, state.plan[0], record)]
    assert "用一句对白完成收束" in service.assessment_context["brief_context"]
    acceptance = str(state.environment["input_contract"]["acceptance"])
    assert "用一句对白完成收束" in acceptance
    assert "回家途中遇见旧友" not in acceptance
    assert any(step["action"] == "delivery_check" for step in state.plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("document,action", [("", "create"), (BASE, "edit")])
async def test_legacy_chapter_instruction_is_reclassified_with_trusted_workflow_context(document, action):
    snapshot = brief(1 if not document else 2)
    chapter = snapshot["chapter_context"]
    instruction = ("根据已确认的创作简报，只创作第 {} 章《{}》。本章目标：{}。"
        "保留已有正文和已完成章节，追加本章完整内容；不要提前写后续章节。"
        "通过通用 Agent Loop 完成计划、创作、检查与交付。".format(
            chapter["index"], chapter["title"], chapter["goal"]))
    service = Model([{"requests": [piece(instruction, action)]}])
    loop = CreationAgentLoop(service)
    state = loop._new_state(**{**args(document, snapshot), "user_message": instruction}, model_mode="local")
    # A pre-upgrade restored intent bound to the identical durable instruction
    # must be rechecked because it did not account for the chapter contract.
    state.environment["requirement"]["task_intent"] = {"action": "respond"}
    state.environment["requirement"]["task_intent_context"] = {
        "schema_version": "creation.current-turn-intent.v3",
        "instruction_hash": loop._document_hash(instruction), "has_document": bool(document)}
    requirement = await loop._routing_requirement(state)
    assert len(service.calls) == 1
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["instruction"] == state.user_message == instruction
    assert payload["workflow_context"] == chapter_routing_context(snapshot, bool(document))
    assert payload["workflow_context"]["workflow_steps_are_separate_deliverables"] is False
    assert requirement["task_intent"]["action"] == action
    assert requirement["task_intent"]["primary_goal"] == instruction
    assert "保留已有正文" in service.calls[0]["system_prompt"]
    assert "没有正文时" in service.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_trusted_chapter_scope_cannot_bypass_invalid_model_intent():
    instruction = "撰写当前章节，保留原文。"
    service = Model([{"requests": [piece(instruction, "create", "constraint")]}])
    loop = CreationAgentLoop(service)
    state = loop._new_state(**{**args("", brief(1)), "user_message": instruction}, model_mode="local")
    with pytest.raises(OperationError, match="未能核验本轮动作"):
        await loop._routing_requirement(state)
    assert len(service.calls) == 2
    assert state.user_message == instruction


@pytest.mark.asyncio
async def test_workflow_context_preserves_independent_goal_audit_after_retry():
    from creation.skill_governance import task_intent
    first, second = "另写一份客户通知", "修改当前活动介绍"
    instruction = first + "；" + second
    service = Model([{"requests": [piece(first, "create"), piece(second, "edit")]},
                     {"requests": [piece(instruction, "edit")]},
                     audit("", action="none", same_outcome=False)])
    workflow = chapter_routing_context(brief(), True)
    assert await task_intent(service, instruction, True, workflow_context=workflow) == {}
    assert len(service.calls) == 3
    independent_payload = json.loads(service.calls[2]["user_prompt"])
    assert independent_payload["instruction"] == instruction
    assert independent_payload["workflow_context"] == workflow
    assert independent_payload["conflicting_requests"] == [first, second]


@pytest.mark.parametrize("heading", ["《重逢》", "第三章 《重逢》", "“重逢”", "# 第三章 重逢", "# # 第三章 《重逢》"])
def test_future_chapter_quoted_heading_even_without_prose_is_forbidden(heading):
    with pytest.raises(OperationError, match="未确认章节"):
        validate_chapter_document(DOCUMENT + "\n\n## " + heading, BASE, chapter_context(brief()), completed=True)


@pytest.mark.parametrize("duplicate", ["## 《初见》", "## # 第一章 初见"])
def test_duplicate_heading_packaging_normalizes_without_altering_prior_prose(duplicate):
    chapter = chapter_context(brief(1))
    raw = "# 初见\n\n" + duplicate + "\n\n小林走过巷口。\n"
    normalized = normalize_chapter_document(raw, "", chapter)
    assert normalized.count("初见") == 1 and normalized.endswith("小林走过巷口。\n")
    validate_chapter_document(normalized, "", chapter, completed=True)
    raw_append = BASE + "\n\n## 转折\n\n### 《转折》\n\n他停下来。"
    normalized = normalize_chapter_document(raw_append, BASE, chapter_context(brief()))
    assert normalized.startswith(BASE) and normalized.count("转折") == 1
    assert normalized.endswith("他停下来。")


@pytest.mark.parametrize("base,index", [("", 1), (BASE, 2)])
def test_actual_prior_body_scope_is_shared_by_assessment_writer_and_review(base, index):
    from creation.delivery_contract import delivery_source_materials
    snapshot = brief(index)
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args(base, snapshot), model_mode="local")
    scope = chapter_delivery_scope(snapshot, base)
    assert scope["protected_previous_body_is_empty"] == (not base)
    assert scope["protected_previous_body_characters"] == len(base)
    assert scope["unwritten_chapters"][0]["id"] == "c{}".format(index + 1)
    assert loop._input_context(state)["chapter_delivery_scope"] == scope
    assert "chapter_delivery_scope" in loop._acceptance_brief_context(state)
    assert "chapter_delivery_scope" in loop._prompt_environment(state)
    materials = delivery_source_materials(state.user_message, state.environment)
    assert materials["original_document"] == base
    assert materials["chapter_delivery_scope"] == scope
    assert "无历史章节需要补回" in chapter_delivery_scope(brief(1), "")["preservation_rule"]


@pytest.mark.asyncio
async def test_review_receives_trusted_empty_base_and_form_aware_coverage():
    from creation.delivery_contract import review_delivery
    from tests.test_creation_delivery_contract import StreamService, contract, review_for_model
    service = StreamService([json.dumps(review_for_model(document="line-3"))])
    document = "# 初见\n\n大家围坐聊书，安静倾听也很自在。"
    await review_delivery(service, "写150字活动介绍", document, contract(), {
        "creation_brief": brief(1), "chapter_base_document": "", "input_base_document": ""})
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["provided_materials"]["original_document"] == ""
    assert payload["provided_materials"]["chapter_delivery_scope"]["protected_previous_body_is_empty"] is True
    system = service.calls[0]["system_prompt"]
    assert "不能证明另有已完成正文" in system
    assert "不增加原需求和该选择之外的验收义务" in system


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_kind", ["quoted", "nested_markdown", "valid"])
@pytest.mark.parametrize("old_acceptance_version", [7, 8])
async def test_same_checkpoint_upgrade_preserves_user_criteria_and_reenters_normal_gate(candidate_kind, old_acceptance_version):
    first = [event async for event in CreationAgentLoop(ChapterService()).run(**args())]
    checkpoint = copy.deepcopy([event["data"]["checkpoint"] for event in first
                                if event["type"] == "operation.checkpoint"][-1])
    original_instruction = checkpoint["user_message"]
    environment = checkpoint["environment"]
    invalid_candidate = candidate_kind != "valid"
    candidate = DOCUMENT
    if candidate_kind == "quoted":
        candidate += "\n\n## 《重逢》"
    elif candidate_kind == "nested_markdown":
        candidate = DOCUMENT.replace("## 转折", "## # 第二章 转折") + "\n\n## # 第三章 重逢\n1. 不该提前创作的步骤\n2. 更不该提前确认的材料"
    checkpoint["current_document"] = environment["document"] = candidate
    environment["brainstorm_acceptance_version"] = old_acceptance_version
    environment["input_context"]["brainstorm_context_version"] = 4
    environment["operation_context"] = {"operation_id": "original-operation"}
    authored = {"id": "user-length", "criterion": "正文保持150字左右，不增加未经提供的资料"}
    environment["input_contract"]["acceptance"].append(authored)
    environment["delivery_repair_count"] = 2
    environment["delivery_review"] = {"status": "revise", "checks": [], "corrections": ["氛围选择须有输入产物验证流程"]}
    environment["delivery_pending_review"] = {"checks": [{"id": "obsolete", "reason": "旧失败"}]}
    checkpoint["plan"].append({"kind": "agent", "id": "delivery_repair", "name": "旧修正", "action": "polisher",
                               "delivery_repair": True, "reason": "氛围选择须有输入产物验证流程"})
    service = ChapterService()
    resumed = [event async for event in CreationAgentLoop(service).run(**args(), resume_checkpoint=checkpoint)]
    assert resumed[-1]["type"] == "run.completed"
    assert resumed[-1]["data"]["document"] == DOCUMENT
    assert service.writes == int(invalid_candidate) and service.assessed == 0
    checked = service.checked[-1]
    assert authored in checked["input_contract"]["acceptance"]
    assert checked["input_base_document"] == checked["chapter_base_document"] == BASE
    assert checked["operation_context"] == {"operation_id": "original-operation"}
    assert checked["brainstorm_acceptance_version"] == 9 and checked["delivery_repair_count"] == 0
    assert not checked.get("delivery_pending_review") and not checked.get("delivery_last_revise")
    assert "按用户要求的文体、用途、范围和篇幅" in str(checked["input_contract"]["acceptance"])
    if invalid_candidate:
        assert checked["chapter_rejected_candidates"][-1]["document"] == candidate
    checkpoints = [event["data"]["checkpoint"] for event in resumed if event["type"] == "operation.checkpoint"]
    assert checkpoints[-1]["user_message"] == original_instruction
    assert not any(step.get("reason") == "氛围选择须有输入产物验证流程"
                   for step in checkpoints[0]["plan"][checkpoints[0]["cursor"]:])


def test_chapter_boundary_recovery_is_bounded_and_retains_rejected_candidates():
    from tests.test_creation_delivery_contract import contract
    loop = CreationAgentLoop(ChapterService())
    state = loop._new_state(**args(), model_mode="local")
    state.environment["input_contract"] = contract()
    for attempt in range(2):
        state.current_document = DOCUMENT + "\n\n## 《重逢》"
        assert loop._recover_invalid_chapter_candidate(state)
        assert state.current_document == BASE
    state.current_document = DOCUMENT + "\n\n## 《重逢》"
    with pytest.raises(OperationError, match="连续未满足"):
        loop._recover_invalid_chapter_candidate(state)
    assert len(state.environment["chapter_rejected_candidates"]) == 2


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_fenced_markdown_examples_do_not_count_as_chapter_headings(fence):
    candidate = DOCUMENT + "\n\n下面是标题语法示例：\n" + fence + "markdown\n## 转折\n## # 第三章 《重逢》\n" + fence
    validate_chapter_document(candidate, BASE, chapter_context(brief()), completed=True)
    with pytest.raises(OperationError, match="未确认章节"):
        validate_chapter_document(candidate + "\n\n## # 第三章 《重逢》", BASE, chapter_context(brief()), completed=True)


@pytest.mark.parametrize("dimension,value,description", [
    ("氛围", "轻松随意，自由交流", "采用亲切语气，以短介绍呈现"),
    ("验证方案", "固定同一批样本比较耗时", "输入为200份已授权样本，产物是耗时记录，按相同设备复测"),
])
def test_choice_coverage_preserves_actual_promises_without_invented_proposal_template(dimension, value, description):
    from creation.delivery_contract import with_brainstorm_coverage
    from tests.test_creation_delivery_contract import contract
    original = contract()
    authored = {"id": "user-method", "criterion": "按用户指定方法验收，不能省略已要求的步骤与验证"}
    original["acceptance"].append(authored)
    bound = with_brainstorm_coverage(original, [{"id": "choice", "source": "user",
        "dimension": dimension, "value": value, "description": description}], "generate")
    selection = next(item["criterion"] for item in bound["acceptance"] if item["id"] == "choice")
    assert value in selection and description in selection
    assert "按用户要求的文体、用途、范围和篇幅" in selection
    assert "实际承诺的内容" in selection
    assert "所需输入、产物与验证方法" not in selection
    assert authored in bound["acceptance"]


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [1, 2])
async def test_review_chapter_length_excludes_previous_body_and_current_heading(index):
    from creation.delivery_contract import review_delivery, candidate_text_metrics
    from tests.test_creation_delivery_contract import StreamService, contract, review_for_model
    snapshot = brief(index)
    previous = "# 全篇\n\n## 初见\n\n" + "甲" * 150 if index == 2 else ""
    chapter_title = snapshot["chapter_context"]["title"]
    prefix = previous + "\n\n" if previous else "# 全篇\n\n"
    document = prefix + "## " + chapter_title + "\n\n" + "乙" * 150
    line_id = "line-{}".format(len(document.splitlines()))
    service = StreamService([json.dumps(review_for_model(document=line_id))])
    await review_delivery(service, "每章约150字", document, contract(), {
        "creation_brief": snapshot, "chapter_base_document": previous, "input_base_document": previous})
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["candidate_metrics"] == candidate_text_metrics(document)
    assert payload["candidate_metrics_scope"] == "complete_document"
    assert payload["chapter_candidate_metrics"]["scope"] == "current_chapter_addition_excluding_title"
    assert payload["chapter_candidate_metrics"]["body_cjk_characters"] == 150
    assert payload["chapter_candidate_metrics"]["body_non_whitespace_characters_including_markup"] == 150
    assert payload["candidate_metrics"]["body_cjk_characters"] >= index * 150
    assert "字数条件必须使用 chapter_candidate_metrics" in service.calls[0]["system_prompt"]


def test_chapter_metrics_scope_upgrade_discards_obsolete_cumulative_length_failure():
    from tests.test_creation_delivery_contract import contract
    loop = CreationAgentLoop(ChapterService())
    state = loop._new_state(**args(), model_mode="local")
    state.plan = [loop._agent_plan_step("document_writer_agent")]
    state.cursor = 1
    state.current_document = DOCUMENT
    state.environment.update(document=DOCUMENT, input_contract=contract(),
        operation={"kind": "generate"}, brainstorm_acceptance_version=9)
    context = loop._input_context(state)
    context.pop("chapter_metrics_scope_version")
    state.environment.pop("brainstorm_contract_rebound", None)
    state.environment.update(delivery_review={"status": "revise", "corrections": ["累计300字超过本章150字"]},
                             delivery_repair_count=2, delivery_checked_hash="old-check")
    loop._input_context(state)
    assert context["chapter_metrics_scope_version"] == 1
    assert state.environment["delivery_repair_count"] == 0
    assert "delivery_review" not in state.environment and "delivery_checked_hash" not in state.environment
    assert loop._recover_invalid_chapter_candidate(state)
    assert state.current_document == DOCUMENT
    assert [step["action"] for step in state.plan[state.cursor:]] == ["delivery_check"]


@pytest.mark.asyncio
async def test_appended_chapter_repair_uses_current_addition_and_completes_normal_review():
    original_sentence = "他停下来，轻声说：原来你还记得这条路。"
    repaired_sentence = "他停下来，轻声说：原来你也一直记着回家的路。"
    class RepairingAppendService(DeliveryService):
        def __init__(self):
            super().__init__(reports=["revise", "pass"], operation={"kind": "transform",
                "targets": [document_nodes(BASE)[0]["id"]]})
            self.repairs = []
        async def run_specialist_agent(self, **kwargs):
            if kwargs["agent_id"] == "document_transform":
                return json.dumps({"patches": [{"action": "insert", "target": {"text": BASE},
                    "position": "after", "content": DOCUMENT[len(BASE):]}]})
            if kwargs["agent_id"] == "delivery_repair":
                payload = json.loads(kwargs["user_prompt"])
                self.repairs.append(payload)
                assert payload["document"] == DOCUMENT
                assert payload["allowed_targets"] == [{"text": DOCUMENT[len(BASE):], "occurrence": 1}]
                assert "原始基线重新生成" not in kwargs["system_prompt"]
                return json.dumps({"patches": [{"action": "replace", "target": {"text": original_sentence},
                    "content": repaired_sentence}]})
            return await super().run_specialist_agent(**kwargs)
    service = RepairingAppendService()
    events = [event async for event in CreationAgentLoop(service).run(**args())]
    assert events[-1]["type"] == "run.completed"
    assert events[-1]["data"]["delivery_review"]["status"] == "pass"
    assert events[-1]["data"]["document"] == DOCUMENT.replace(original_sentence, repaired_sentence)
    assert events[-1]["data"]["document"].startswith(BASE)
    assert len(service.repairs) == 1 and len(service.checked) == 2
    assert any(event["type"] == "document.mutation.salvaged" for event in events)


@pytest.mark.asyncio
async def test_chapter_repair_cannot_use_old_root_target_to_modify_protected_chapter():
    loop = CreationAgentLoop(ChapterService())
    state = loop._new_state(**args(), model_mode="local")
    state.current_document = DOCUMENT
    state.environment.update(document=DOCUMENT, operation={"kind": "transform",
        "targets": [document_nodes(BASE)[0]["id"]]})
    original_operation = copy.deepcopy(state.environment["operation"])
    step = {"kind": "agent", "id": "delivery_repair", "name": "修正本章", "action": "patch_writer",
            "delivery_repair": True, "patch_base_document": BASE}
    patches = {"patches": [{"action": "replace", "target": {"text": "灯亮起来的时候，小林走过巷口。"},
                             "content": "不能改动的第一章被重写。"}]}
    with pytest.raises(OperationError, match="超出了本轮选定范围"):
        _ = [event async for event in loop._complete_model_step(state, step, json.dumps(patches))]
    assert state.current_document == state.environment["document"] == DOCUMENT
    assert state.environment["operation"] == original_operation
