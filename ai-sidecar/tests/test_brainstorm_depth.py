"""Branch depth must advance actual questions without converting ready into a menu."""
import json

import pytest

from creation.brainstorm import BrainstormCoordinator, BrainstormGenerationError
from tests.test_creation_brainstorm import StubCreationService, concise_question_payload


def question(stage):
    value = concise_question_payload("怎样减少当前动作画面的失真？")
    value["question"].update({"dimension_id": "action_repair", "exploration_stage": stage})
    value["question"]["options"] = [
        {"id": "split", "label": "拆成单一动作镜头", "description": "分开生成再衔接，降低单段动作难度但增加剪辑工作。", "recommended": True},
        {"id": "reference", "label": "引入动作参考", "description": "用参考约束动作轨迹，但需要准备适配素材。", "recommended": False},
    ]
    return value


def ready():
    return {"status": "ready", "continuation_directions": [
        {"id": "a", "label": "讨论风险", "description": "继续检查风险。", "recommended": True},
        {"id": "b", "label": "讨论落地", "description": "继续细化安排。"},
    ]}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["solutions", "implementation", "validation"])
async def test_required_branch_stage_precedes_uncovered_background(stage):
    service = StubCreationService([json.dumps(question(stage), ensure_ascii=False)])
    result = await BrainstormCoordinator(service).next_step(
        root_request="构思短片，只用当前输入", decisions=[], brief_markdown="",
        exploration_stage=stage, force_continue=True,
        focus_hint="动作画面失真 → 拆分镜头", focus_hint_source="confirmed_selection")
    assert result["question"]["exploration_stage"] == stage
    assert result["question"]["dimension_id"] == "action_repair"
    assert '"next_question_goal": {}' in service.prompts[0]
    assert '"stage": "' + stage + '"' in service.prompts[0]
    assert "动作画面失真 → 拆分镜头" in service.prompts[0]
    assert BrainstormCoordinator.EXPLORATION_GOALS[stage] in service.prompts[0]
    system_prompt = service.model_calls[0]["system_prompt"]
    assert "严格服从 Core 指定的当前路径 focus_hint 和阶段 exploration_goal" in system_prompt
    assert "先横向讨论同层已选方向，再进入下一层" in system_prompt
    assert "优先纵向深入" not in system_prompt


@pytest.mark.asyncio
async def test_prefetch_does_not_assume_unanswered_sibling_decisions():
    service = StubCreationService([json.dumps(question("solutions"), ensure_ascii=False)])
    await BrainstormCoordinator(service).next_step(
        root_request="构思短片，只用当前输入", decisions=[], brief_markdown="",
        exploration_stage="solutions", prefetch=True,
        focus_hint="动作画面失真", focus_hint_source="confirmed_selection",
    )
    assert "候选仅基于目前共同已确认的信息" in service.prompts[0]
    assert "不得假定其他尚未回答的兄弟方向已做决定" in service.prompts[0]
    assert "使用条件化说明，不把未知条件写成已确认事实" in service.prompts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [ready(), question("explore"), question("validation")])
async def test_premature_ready_and_wrong_stage_regenerate_concrete_solution(invalid):
    service = StubCreationService([json.dumps(value, ensure_ascii=False) for value in [invalid, question("solutions")]])
    result = await BrainstormCoordinator(service).next_step(
        root_request="构思短片，只用当前输入", decisions=[], brief_markdown="",
        exploration_stage="solutions", force_continue=True,
        focus_hint="动作画面失真", focus_hint_source="confirmed_selection")
    assert len(service.prompts) == 2
    assert result["question"]["exploration_stage"] == "solutions"
    assert result["question"]["dimension_id"] != "continuation_focus"
    assert result["question"]["options"][0]["label"] == "拆成单一动作镜头"


@pytest.mark.asyncio
async def test_repeated_ready_fails_within_existing_retry_budget_instead_of_fake_depth():
    service = StubCreationService([json.dumps(ready())] * BrainstormCoordinator.MAX_GENERATION_ATTEMPTS)
    with pytest.raises(BrainstormGenerationError):
        await BrainstormCoordinator(service).next_step(
            root_request="构思短片", decisions=[], brief_markdown="",
            exploration_stage="solutions", force_continue=True, focus_hint="动作失真")
    assert len(service.prompts) == BrainstormCoordinator.MAX_GENERATION_ATTEMPTS


@pytest.mark.parametrize("stage", ["solutions", "implementation", "validation"])
def test_deep_questions_cannot_use_only_generic_options(stage):
    value = question(stage)
    for option, label in zip(value["question"]["options"], ["保守方案", "激进方案"]):
        option["label"] = label
    with pytest.raises(BrainstormGenerationError, match="缺少任务特定"):
        BrainstormCoordinator._normalize_result(json.dumps(value, ensure_ascii=False),
            force_continue=True, expected_exploration_stage=stage)


def test_new_stage_does_not_invent_progress_for_legacy_question():
    result = BrainstormCoordinator._normalize_question(concise_question_payload("希望改善什么体验？")["question"])
    assert result["exploration_stage"] == "explore"


def test_deep_question_requires_substantive_option_explanations():
    value = question("solutions")["question"]
    del value["options"][0]["description"]
    with pytest.raises(BrainstormGenerationError, match="不能用通用说明补齐"):
        BrainstormCoordinator._normalize_question(value)


def test_missing_or_unknown_stage_is_rejected_for_required_branch():
    value = question("solutions")
    del value["question"]["exploration_stage"]
    with pytest.raises(BrainstormGenerationError, match="指定探索阶段"):
        BrainstormCoordinator._normalize_result(json.dumps(value), force_continue=True,
            expected_exploration_stage="solutions")
    value["question"]["exploration_stage"] = "finished"
    with pytest.raises(BrainstormGenerationError, match="无效的脑暴探索阶段"):
        BrainstormCoordinator._normalize_question(value["question"])


def test_source_restrictions_preserve_stage_and_ancestry_but_not_old_evidence():
    decisions, brief = BrainstormCoordinator._user_only_context("只用当前输入", [{
        "question_id": "q2", "dimension_id": "actions", "answer_source": "user", "answer": "拆分镜头",
        "exploration_stage": "solutions", "parent_question_id": "q1", "parent_option_id": "distortion",
        "question": "PRIVATE-EVIDENCE", "selected_options": [{"description": "PRIVATE-EVIDENCE"}],
    }])
    assert decisions[0]["exploration_stage"] == "solutions"
    assert decisions[0]["parent_question_id"] == "q1"
    assert decisions[0]["parent_option_id"] == "distortion"
    assert "PRIVATE-EVIDENCE" not in json.dumps(decisions) + brief


@pytest.mark.asyncio
async def test_changing_direction_can_leave_unfinished_branch():
    service = StubCreationService([json.dumps(ready())])
    result = await BrainstormCoordinator(service).next_step(
        root_request="构思短片", decisions=[], brief_markdown="",
        suggest_directions=True, exploration_stage="implementation")
    assert result["status"] == "ready"
    assert '"exploration_goal": {}' in service.prompts[0]


@pytest.mark.asyncio
async def test_invalid_request_stage_fails_before_any_model_or_retrieval():
    service = StubCreationService([])
    with pytest.raises(BrainstormGenerationError):
        await BrainstormCoordinator(service).next_step(root_request="构思短片", decisions=[],
            brief_markdown="", exploration_stage="unknown")
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_selected_stage_uses_sources_without_inheriting_unrelated_background(monkeypatch):
    from tests.test_brainstorm_memory import MemoryService, QUOTE

    value = question("solutions")
    value["question"]["options"][0]["memory_ids"] = ["m1"]
    service = MemoryService([value])
    coordinator = BrainstormCoordinator(service)

    async def no_background_assessment(**kwargs):
        raise AssertionError("Background inheritance cannot advance the selected branch")

    monkeypatch.setattr(coordinator, "_assess_memory", no_background_assessment)
    result = await coordinator.next_step(root_request="构思短片", decisions=[], brief_markdown="",
        exploration_stage="solutions", force_continue=True, focus_hint="动作画面失真")
    assert service.queries
    assert QUOTE in result["question"]["options"][0]["details"]
    assert "沿用历史结论" not in result["memory_brief"]
