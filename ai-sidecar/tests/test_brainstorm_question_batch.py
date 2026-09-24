"""Confirmed branches plan independent same-stage questions without guessing answers."""

import json

import pytest

from creation.brainstorm import BrainstormCoordinator, BrainstormGenerationError
from tests.test_brainstorm_depth import question, ready
from tests.test_creation_brainstorm import StubCreationService


def branch_request(**overrides):
    return {
        "root_request": "构思短片，只用当前输入",
        "decisions": [], "brief_markdown": "",
        "exploration_stage": "solutions", "force_continue": True,
        "focus_hint": "动作画面失真", "focus_hint_source": "confirmed_selection",
        "question_batch_limit": 3,
        **overrides,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("goals", [[], ["怎样保持连续镜头的角色特征？"], [
    "怎样保持连续镜头的角色特征？", "如何处理动作与对白的相互干扰？",
]])
async def test_one_completion_can_plan_a_variable_number_of_independent_questions(goals):
    value = question("solutions")
    value["sibling_question_goals"] = goals
    service = StubCreationService([json.dumps(value, ensure_ascii=False)])
    request = branch_request()
    result = await BrainstormCoordinator(service).next_step(**request)
    assert result["sibling_question_goals"] == goals
    assert len(service.model_calls) == 1
    assert request["decisions"] == [] and request["brief_markdown"] == ""
    assert all(goal not in result["memory_brief"] for goal in goals)
    assert "单题足够时计划只含一项" in service.prompts[0]
    assert "不得依赖当前题的未来答案" in service.prompts[0]
    assert "同父选项、同探索阶段" in service.prompts[0]
    assert "首题必须锁定一个明确决定，不能笼统询问整个方向或全流程" in service.prompts[0]
    assert "不要只留在open_flags" in service.prompts[0]
    assert "或其余问题依赖尚未给出的答案时使用单项计划" in service.prompts[0]
    field = service.model_calls[0]["json_schema"]["properties"]["question_plan"]
    # Shared grammar deliberately omits larger collection bounds to avoid
    # llama.cpp grammar expansion; the normalizer enforces the actual limit.
    assert field["type"] == "array" and field["items"]["type"] == "string"
    assert '"sibling_goal_limit": 2' in service.prompts[0]
    fields = list(service.model_calls[0]["json_schema"]["properties"])
    assert fields.index("question_plan") < fields.index("question")
    assert "sibling_question_goals" not in fields
    option_fields = list(service.model_calls[0]["json_schema"]["properties"]["question"]["properties"]["options"]["items"]["properties"])
    assert all(option_fields.index("memory_mode") < option_fields.index(field)
               for field in ["label", "description", "memory_ids"])


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"question_batch_limit": 1},
    {"focus_hint_source": "legacy"},
    {"focus_hint_source": "user"},
    {"focus_hint": ""},
])
async def test_unconfirmed_or_single_question_requests_do_not_plan_siblings(overrides):
    service = StubCreationService([json.dumps(question("solutions"))])
    result = await BrainstormCoordinator(service).next_step(**branch_request(**overrides))
    assert result["sibling_question_goals"] == []
    assert service.model_calls[0]["json_schema"]["properties"]["sibling_question_goals"]["maxItems"] == 0


@pytest.mark.parametrize("goals", [
    "不是数组", [None], ["预算"], ["确认" * 41],
    ["第一待问主题", "第二待问主题", "超出批次上限"],
])
def test_malformed_plans_fail_before_publication(goals):
    value = question("solutions")
    value["sibling_question_goals"] = goals
    with pytest.raises(BrainstormGenerationError):
        BrainstormCoordinator._normalize_result(
            json.dumps(value, ensure_ascii=False), force_continue=True,
            expected_exploration_stage="solutions", sibling_goal_limit=2,
            decisions=[{"question": "怎样处理旧问题!", "answer_source": "user", "answer": "已回答"}],
        )


@pytest.mark.asyncio
async def test_bad_plan_reuses_existing_generation_repair_budget():
    invalid = question("solutions")
    invalid["sibling_question_goals"] = [None]
    valid = question("solutions")
    valid["sibling_question_goals"] = ["如何处理动作与对白的相互干扰？"]
    service = StubCreationService([json.dumps(item, ensure_ascii=False) for item in [invalid, valid]])
    result = await BrainstormCoordinator(service).next_step(**branch_request())
    assert len(service.model_calls) == 2
    assert result["sibling_question_goals"] == valid["sibling_question_goals"]
    assert "rejected_sibling_question_goals" in service.prompts[1]
    assert "不是用户决定" in service.prompts[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("root", ["构思短片", "构思短片，只用当前输入"])
async def test_extension_uses_its_exact_goal_and_cannot_recursively_plan_more(root):
    goal = "如何处理动作与对白的相互干扰？"
    value = question("solutions")
    target = BrainstormCoordinator._extension_generation_goal(goal)
    value["question"].update(dimension_id=target["id"], dimension=goal)
    service = StubCreationService([json.dumps(value, ensure_ascii=False)])
    result = await BrainstormCoordinator(service).next_step(**branch_request(
        root_request=root, extension_goal=goal, prefetch=True,
    ))
    assert result["sibling_question_goals"] == []
    prompt = service.prompts[0]
    assert '"extension_goal": "' + goal + '"' in prompt
    assert "本题只围绕 extension_goal" in prompt
    assert "不是用户答案" in prompt
    assert '"next_question_goal": ' + json.dumps(target, ensure_ascii=False) in prompt
    assert "本次只为此目标生成真实问题和候选做法" in prompt
    fields = service.model_calls[0]["json_schema"]["properties"]["question"]["properties"]
    assert fields["dimension_id"]["enum"] == [target["id"]]
    assert fields["dimension"]["enum"] == [goal]
    assert "目标与期望结果" not in result["open_flags"]
    assert service.model_calls[0]["json_schema"]["properties"]["sibling_question_goals"]["maxItems"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"question_batch_limit": 0}, {"question_batch_limit": 9},
    {"question_batch_limit": True}, {"question_batch_limit": 2.5},
    {"extension_goal": "长" * 81},
    {"extension_goal": "待确认的同层主题", "focus_hint_source": "legacy"},
    {"extension_goal": "待确认的同层主题", "suggest_directions": True},
])
async def test_invalid_batch_requests_fail_before_retrieval_or_model(overrides):
    service = StubCreationService([])
    coordinator = BrainstormCoordinator(service)
    result = []
    coordinator._retrieve_memory = lambda *args: result.append(True)
    with pytest.raises(BrainstormGenerationError):
        await coordinator.next_step(**branch_request(**overrides))
    assert result == [] and service.model_calls == []


def test_ready_or_extension_responses_cannot_add_branch_plans():
    value = ready()
    value["sibling_question_goals"] = ["旧方向的待确认主题"]
    with pytest.raises(BrainstormGenerationError):
        BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=False,
                                                suggest_directions=True)
    value = question("solutions")
    value["sibling_question_goals"] = ["不应递归的待确认主题"]
    with pytest.raises(BrainstormGenerationError):
        BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=True,
                                                sibling_goal_limit=0)


def test_reworded_question_reusing_confirmed_option_is_rejected():
    value = question("solutions")
    value["question"].update(
        prompt="新号内容分发应设定何种自动化程度？",
        options=[
            {"id": "renamed", "label": "全量自动分发",
             "description": "由平台自动完成选品、脚本、制作与发布，商家仅需审核，效率最高。",
             "details": "当前输入", "recommended": True},
            {"id": "other", "label": "半自动辅助分发",
             "description": "商家参与部分流程以平衡效率与控制。",
             "details": "当前输入", "recommended": False},
        ],
    )
    decisions = [{
        "question": "新号应如何运营以最大化爆款产出？", "answer_source": "user",
        "selected_options": [{"id": "old", "label": "全量自动分发",
                              "tradeoff": "由平台自动完成选品、脚本、制作与发布，商家仅需审核，效率最高。"}],
    }]
    with pytest.raises(BrainstormGenerationError, match="重复了已经回答"):
        BrainstormCoordinator._normalize_result(
            json.dumps(value, ensure_ascii=False), force_continue=True,
            expected_exploration_stage="solutions", decisions=decisions,
        )


@pytest.mark.asyncio
async def test_legacy_question_without_plan_field_remains_compatible():
    service = StubCreationService([json.dumps(question("solutions"))])
    request = branch_request()
    del request["question_batch_limit"]
    result = await BrainstormCoordinator(service).next_step(**request)
    assert result["sibling_question_goals"] == []


def overlapping_live_payload():
    """Minimal synthetic reproduction of the observed combined-head failure."""
    value = question("solutions")
    value["question"].update(
        prompt="如何设计破冰与分享方法促进表达？", dimension="破冰与分享方法设计",
    )
    value["sibling_question_goals"] = ["破冰方法", "分享方法"]
    return value


def test_live_combined_head_cannot_be_split_back_into_repeated_sibling_topics():
    result = BrainstormCoordinator._normalize_result(json.dumps(overlapping_live_payload()),
        force_continue=True, expected_exploration_stage="solutions", sibling_goal_limit=2)
    assert result["sibling_question_goals"] == []


@pytest.mark.parametrize("goals", [["破冰方法"], ["如何设计破冰方式？", "分享规则"]])
def test_live_short_topic_cannot_reask_the_head_with_generic_suffixes(goals):
    value = question("solutions")
    value["question"].update(prompt="小组交流中如何破冰让新人开口？", dimension="破冰与表达促进")
    value["sibling_question_goals"] = goals
    result = BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=True,
        expected_exploration_stage="solutions", sibling_goal_limit=2)
    assert all("破冰" not in goal for goal in result["sibling_question_goals"])


@pytest.mark.parametrize("prompt", [
    "小组交流中，如何设计破冰让新人愿意开口？",
    "如何设计破冰让新人愿意开口？",
])
def test_live_extension_cannot_repeat_unanswered_head_with_weak_wording_changes(prompt):
    value = question("solutions")
    value["question"].update(prompt=prompt, dimension="促进交流")
    with pytest.raises(BrainstormGenerationError, match="其他同层待问主题"):
        BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=True,
            expected_exploration_stage="solutions",
            sibling_question_context=["小组交流中如何破冰让新人开口？", "材料呈现方式"])


def test_generic_topic_normalization_preserves_distinct_decisions_and_short_shared_words():
    for first, second in [("破冰方法", "分享规则"), ("讨论材料", "组织材料领取"),
                          ("角色塑造方式", "情节推进方式"), ("如何搭建场景？", "如何确定情节？"),
                          ("分享方法", "分享规则"), ("表达方式", "表达机制")]:
        assert not BrainstormCoordinator._question_topics_overlap(first, second)
    assert BrainstormCoordinator._question_topic_key("分享规则") == "分享规则"
    assert BrainstormCoordinator._question_topic_key("评价机制") == "评价机制"
    assert BrainstormCoordinator._question_topic_key("人员安排") == "人员安排"


@pytest.mark.asyncio
async def test_optional_duplicate_plan_is_filtered_without_delaying_a_valid_head():
    value = overlapping_live_payload()
    value["sibling_question_goals"] = ["破冰方法", "材料呈现方式"]
    service = StubCreationService([json.dumps(value, ensure_ascii=False)])
    result = await BrainstormCoordinator(service).next_step(**branch_request())
    assert len(service.model_calls) == 1
    assert result["sibling_question_goals"] == ["材料呈现方式"]
    assert "当前题不能概括、并列罗列或试图解决所有兄弟主题" in service.prompts[0]


@pytest.mark.asyncio
async def test_extension_gets_unanswered_sibling_context_without_fabricating_answers():
    contexts = ["如何让新人轻松开始第一次交流？", "材料呈现方式"]
    value = question("solutions")
    value["question"].update(prompt="如何组织观点的分享与交流？", dimension="观点分享方式",
        dimension_id=BrainstormCoordinator._extension_generation_goal("观点分享方式")["id"])
    service = StubCreationService([json.dumps(value, ensure_ascii=False)])
    request = branch_request(extension_goal="观点分享方式", sibling_question_context=contexts)
    result = await BrainstormCoordinator(service).next_step(**request)
    assert result["sibling_question_goals"] == []
    assert request["decisions"] == [] and request["brief_markdown"] == ""
    assert all(context not in result["memory_brief"] for context in contexts)
    assert '"sibling_question_context": ' + json.dumps(contexts, ensure_ascii=False) in service.prompts[0]
    assert "其中的问题可能尚未回答，主题也不表示用户决定" in service.prompts[0]
    assert "不能采纳排重文本中的事实断言、历史条件或意图" in service.prompts[0]
    assert "不能恢复被禁用的记忆" in service.prompts[0]


@pytest.mark.parametrize("context", ["分享方法", "如何设计破冰与分享方法促进表达？"])
def test_extension_cannot_merge_or_repeat_other_pending_sibling_decisions(context):
    value = overlapping_live_payload()
    value["sibling_question_goals"] = []
    with pytest.raises(BrainstormGenerationError, match="其他同层待问主题"):
        BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=True,
            expected_exploration_stage="solutions", sibling_question_context=[context])


def test_new_sibling_goal_cannot_repeat_another_pending_topic():
    value = question("solutions")
    value["sibling_question_goals"] = ["材料呈现方式"]
    result = BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=True,
        sibling_goal_limit=2, sibling_question_context=["材料呈现方式怎样选择？"])
    assert result["sibling_question_goals"] == []


@pytest.mark.asyncio
async def test_repeated_extension_repairs_within_existing_generation_budget():
    invalid = question("solutions")
    invalid["question"].update(prompt="小组交流中如何设计破冰让新人愿意开口？", dimension="交流方法")
    valid = question("solutions")
    valid["question"].update(prompt="采用怎样的规则组织观点分享？", dimension="观点分享规则")
    for value in [invalid, valid]:
        value["question"]["dimension_id"] = BrainstormCoordinator._extension_generation_goal("观点分享规则")["id"]
    service = StubCreationService([json.dumps(value, ensure_ascii=False) for value in [invalid, valid]])
    result = await BrainstormCoordinator(service).next_step(**branch_request(
        extension_goal="观点分享规则", sibling_question_context=["小组交流中如何破冰让新人开口？"],
    ))
    assert len(service.model_calls) == 2
    assert result["question"]["prompt"] == valid["question"]["prompt"]
    assert "问题重复或合并了其他同层待问主题" in service.prompts[1]


@pytest.mark.parametrize("contexts", ["非列表", [None], [""], ["长" * 81], ["合成待问主题"] * 33])
@pytest.mark.asyncio
async def test_invalid_sibling_context_is_rejected_before_model(contexts):
    service = StubCreationService([])
    with pytest.raises(BrainstormGenerationError):
        await BrainstormCoordinator(service).next_step(**branch_request(sibling_question_context=contexts))
    assert service.model_calls == []


def planned_payload(plan):
    value = question("solutions")
    value["question_plan"] = plan
    if isinstance(plan, list) and plan and isinstance(plan[0], str):
        value["question"]["dimension"] = plan[0]
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", [
    ["动作修复方式"],
    ["动作修复方式", "对白节奏安排"],
    ["动作修复方式", "对白节奏安排", "角色特征保持"],
])
async def test_whole_plan_first_item_drives_the_head_and_only_the_rest_become_siblings(plan):
    service = StubCreationService([json.dumps(planned_payload(plan), ensure_ascii=False)])
    result = await BrainstormCoordinator(service).next_step(**branch_request())
    assert len(service.model_calls) == 1
    assert result["question"]["dimension"] == plan[0]
    assert result["sibling_question_goals"] == plan[1:]
    assert "question_plan" not in result
    assert "question_plan[0] 是本轮立即展示的当前题目标" in service.prompts[0]


@pytest.mark.parametrize("plan", [None, "非列表", [], ["短"], [None], ["长" * 81],
    ["动作修复方式", "对白节奏安排", "角色特征保持", "超出批次上限"]])
def test_malformed_whole_plan_cannot_publish_a_partial_batch(plan):
    value = planned_payload(["动作修复方式"])
    value["question_plan"] = plan
    with pytest.raises(BrainstormGenerationError):
        BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=True, sibling_goal_limit=2)


def test_plan_wording_and_short_question_dimension_need_not_be_identical():
    value = planned_payload(["动作修复方式", "对白节奏安排"])
    value["question"]["dimension"] = "动作质量"
    result = BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=True, sibling_goal_limit=2)
    assert result["question"]["dimension"] == "动作质量"
    assert result["sibling_question_goals"] == ["对白节奏安排"]


def test_whole_plan_deduplicates_optional_tail_without_repeating_its_head():
    value = planned_payload(["动作修复方式", "动作修复方式", "对白节奏安排"])
    result = BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=True, sibling_goal_limit=2)
    assert result["sibling_question_goals"] == ["对白节奏安排"]


def test_broad_dimension_does_not_filter_a_complementary_sibling_decision():
    goal = "协作过程遇到争议时如何决策？"
    result = BrainstormCoordinator._normalize_sibling_question_goals(
        [goal], limit=2,
        question={"prompt": "如何安排跨部门协作会议？", "dimension": "协作方式"},
        decisions=[], sibling_question_context=[],
    )
    assert result == [goal]


def test_broad_dimension_does_not_reject_a_complementary_extension_question():
    value = question("solutions")
    value["question"].update(
        prompt="协作过程遇到争议时如何决策？", dimension="协作方式",
    )
    result = BrainstormCoordinator._normalize_result(
        json.dumps(value), force_continue=True, expected_exploration_stage="solutions",
        sibling_question_context=["如何安排跨部门协作会议？"],
    )
    assert result["question"]["prompt"] == value["question"]["prompt"]


@pytest.mark.parametrize("legacy", [["对白节奏安排"], [], ["其他待问主题"]])
def test_dual_plan_protocol_must_agree_without_merging_or_losing_topics(legacy):
    value = planned_payload(["动作修复方式", "对白节奏安排"])
    value["sibling_question_goals"] = legacy
    if legacy == ["对白节奏安排"]:
        result = BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=True, sibling_goal_limit=2)
        assert result["sibling_question_goals"] == legacy
    else:
        with pytest.raises(BrainstormGenerationError, match="相冲突"):
            BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=True, sibling_goal_limit=2)


def test_extension_and_default_single_question_requests_cannot_create_a_new_plan():
    value = planned_payload(["动作修复方式"])
    with pytest.raises(BrainstormGenerationError):
        BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=True, sibling_goal_limit=0)


@pytest.mark.asyncio
async def test_whole_plan_mismatch_repairs_in_the_existing_single_generation_budget():
    invalid = planned_payload(["动作修复方式", "对白节奏安排"])
    invalid["sibling_question_goals"] = ["与整体计划冲突的其他主题"]
    valid = planned_payload(["动作修复方式", "对白节奏安排"])
    service = StubCreationService([json.dumps(value, ensure_ascii=False) for value in [invalid, valid]])
    result = await BrainstormCoordinator(service).next_step(**branch_request())
    assert len(service.model_calls) == 2
    assert result["sibling_question_goals"] == ["对白节奏安排"]
    assert "rejected_question_plan" in service.prompts[1]


@pytest.mark.asyncio
async def test_live_full_question_plan_keeps_independent_material_topic_without_rewriting_head():
    # The real first candidate was useful; rejecting its short dimension caused
    # unnecessary retries which then exceeded the requested planning limit.
    value = question("solutions")
    value["question_plan"] = ["如何设计破冰环节促进新朋友开口？",
        "如何设计分享环节鼓励不同看法？", "如何安排材料支持低门槛表达？"]
    value["question"].update(prompt="小组交流中，如何设计破冰与分享机制促进表达？", dimension="破冰与分享机制设计")
    value["question"]["options"] = [
        {"id": "pair", "label": "随机搭档发言", "description": "随机配对降低防备，但需额外组织时间。", "recommended": True},
        {"id": "cards", "label": "主题卡片讨论", "description": "提供预设话题降低开口难度，依赖材料准备。", "recommended": True},
        {"id": "anonymous", "label": "匿名纸条收集", "description": "匿名形式消除顾虑，但无法即时互动。", "recommended": True},
    ]
    service = StubCreationService([json.dumps(value, ensure_ascii=False)])
    result = await BrainstormCoordinator(service).next_step(**branch_request())
    assert len(service.model_calls) == 1
    assert result["question"]["prompt"] == value["question"]["prompt"]
    assert result["question"]["dimension"] == value["question"]["dimension"]
    assert result["sibling_question_goals"] == ["如何安排材料支持低门槛表达？"]


@pytest.mark.asyncio
async def test_specific_extension_target_repairs_repeated_broad_head_without_changing_coverage():
    goal = "如何安排材料支持低门槛表达？"
    target = BrainstormCoordinator._extension_generation_goal(goal)
    duplicate = question("solutions")
    duplicate["question"].update(prompt="小组交流中，如何设计破冰与分享机制让新朋友愿意表达？",
        dimension_id=target["id"], dimension=goal)
    valid = question("solutions")
    valid["question"].update(prompt="采用什么材料形式帮助参与者表达？", dimension_id=target["id"], dimension=goal)
    valid["open_flags"] = ["具体材料内容仍待确定"]
    service = StubCreationService([json.dumps(value, ensure_ascii=False) for value in [duplicate, duplicate, valid]])
    result = await BrainstormCoordinator(service).next_step(**branch_request(extension_goal=goal,
        sibling_question_context=[duplicate["question"]["prompt"]]))
    assert len(service.model_calls) == 3
    assert result["question"]["prompt"] == valid["question"]["prompt"]
    assert result["open_flags"] == ["具体材料内容仍待确定"]
    assert all('"next_question_goal": ' + json.dumps(target, ensure_ascii=False) in prompt for prompt in service.prompts)
    assert all("exploration_goal仅决定讨论深度" in prompt for prompt in service.prompts)
    assert all(prompt.rfind(goal) > prompt.rfind("sibling_question_context\":") for prompt in service.prompts)
