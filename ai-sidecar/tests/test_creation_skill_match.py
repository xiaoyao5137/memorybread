"""创作提交后执行时 Skill 模型路由召回的单元测试。

输入过程中的逐键自动推荐已下线；这里只覆盖提交后执行时的召回链路，与创作
Agent Loop 的能力路由同构：Skill 自描述渐进式披露 + 模型决策 + 白名单校验
+ 失败降级为空召回。召回与否完全由模型依据 Skill 自述的用途决策，不做
枚举式意图门控，因此候选全量进入披露、由模型返回空数组拦住不匹配的请求。
"""

from __future__ import annotations

import pytest

from creation.service import CreationService


class SkillMatchService:
    """只绑定 Skill 召回相关方法，隔离数据库与模型依赖。"""

    model = "test-model"
    # classmethod 在类上访问后已是绑定方法，直接赋值即可，不能再包 classmethod。
    _skill_match_description = CreationService._skill_match_description
    build_skill_match_prompts = CreationService.build_skill_match_prompts
    parse_skill_match_decision = CreationService.parse_skill_match_decision
    route_creation_skills = CreationService.route_creation_skills

    def __init__(self):
        self.completion_text = '{"skill_ids": [], "reasoning": ""}'
        self.completion_error = None
        self.model_calls = []
        self.usage_logs = []

    def _log_creation_usage(self, **kwargs):
        self.usage_logs.append(kwargs)

    async def _stream_direct_completion(self, **kwargs):
        self.model_calls.append(kwargs)
        if self.completion_error is not None:
            raise self.completion_error
        yield self.completion_text


WEEKLY_SKILL = {
    "id": 27,
    "title": "GPU成本优化周报模板",
    "summary": "用于每周更新大模型性能成本优化周报。",
    "skill_description": {
        "purpose": "用于每周更新大模型性能成本优化周报。",
        "document_types": ["周报", "进度总结报告"],
        "problems": ["GPU指标数据分散，需要按统一口径整理为关键指标表"],
        "domains": ["算力运营", "成本优化"],
        "deliverables": ["包含本周进度总结与关键指标表的结构化周报"],
    },
}

PLAN_SKILL = {
    "id": 49,
    "title": "方案评审文档模板",
    "summary": "组织技术方案评审。",
    "skill_description": {
        "purpose": "把技术方案整理成可评审的文档。",
        "document_types": ["技术方案"],
        "problems": ["统一评审口径"],
        "domains": ["软件架构"],
        "deliverables": ["可评审的技术方案文档"],
    },
}

ARCH_SKILL = {
    "id": 52,
    "title": "架构方案模板",
    "summary": "用于撰写系统架构方案文档。",
    "skill_description": {
        "purpose": "把系统架构设计整理成可评审的方案文档。",
        "document_types": ["架构方案"],
        "problems": ["架构取舍需要结构化表达"],
        "domains": ["软件架构"],
        "deliverables": ["完整的架构方案文档"],
    },
}


def test_prompt_loads_skill_self_description():
    service = SkillMatchService()
    system, user = service.build_skill_match_prompts(
        "写一份本周的GPU成本优化周报", [WEEKLY_SKILL]
    )

    assert "id=27 GPU成本优化周报模板" in system
    assert "用于每周更新大模型性能成本优化周报" in system
    assert "GPU指标数据分散，需要按统一口径整理为关键指标表" in system
    assert "只做选择，不写正文" in system
    assert "用户输入：写一份本周的GPU成本优化周报" in user


def test_prompt_uses_generic_description_rule_without_enumeration():
    # 决策规则是通用的：依据每个 Skill 自述用途判断一致性，请求不在任何
    # Skill 声明用途内（如纯画图）返回空数组；不依赖枚举关键词，也不在
    # 披露中做 document_types 对齐。
    service = SkillMatchService()
    system, _ = service.build_skill_match_prompts(
        "画一张我们系统的架构图", [ARCH_SKILL]
    )

    assert "严格依据每个 Skill 自述的用途与解决的问题判断" in system
    assert "与该 Skill 声明的用途一致时才可选" in system
    assert "不在任何 Skill 描述声明的用途范围内时，返回空数组" in system
    # 模板自述用途完整披露，模型据此识别“画架构图”不在其声明用途内。
    assert "把系统架构设计整理成可评审的方案文档" in system
    assert "目标文档类型" not in system


def test_parse_decision_filters_ids_by_whitelist_and_keeps_one():
    service = SkillMatchService()

    decision = service.parse_skill_match_decision(
        '```json\n{"skill_ids": [999, 27, 27, 49], "reasoning": "输入要写周报"}\n```',
        {27, 49},
    )

    assert decision["skill_ids"] == [27]
    assert decision["reasoning"] == "输入要写周报"
    assert decision["parse_status"] == "complete"

    with pytest.raises(ValueError):
        service.parse_skill_match_decision("没有 JSON 的回复", {27})


def test_parse_decision_recovers_skill_ids_when_reasoning_json_is_malformed():
    service = SkillMatchService()

    decision = service.parse_skill_match_decision(
        '{"skill_ids": [49], "reasoning": "用户需要设计可落地的方案。”}',
        {27, 49},
    )

    assert decision == {
        "skill_ids": [49],
        "reasoning": "",
        "parse_status": "recovered",
    }


def test_parse_decision_does_not_guess_when_skill_ids_are_unrecoverable():
    service = SkillMatchService()

    with pytest.raises(ValueError):
        service.parse_skill_match_decision(
            '{"reasoning": "输出损坏。”}',
            {27, 49},
        )


@pytest.mark.asyncio
async def test_route_recovers_model_selected_plan_from_malformed_reasoning():
    service = SkillMatchService()
    service.completion_text = (
        '{"skill_ids": [49], "reasoning": '
        '"用户需求是设计方案，与方案技能用途一致。”}'
    )

    result = await service.route_creation_skills(
        prompt="设计下GPU性能优化的方案",
        skills=[WEEKLY_SKILL, PLAN_SKILL],
    )

    assert result["skill_ids"] == [49]
    assert result["source"] == "model"
    assert result["parse_status"] == "recovered"


@pytest.mark.asyncio
async def test_all_skills_are_disclosed_for_model_decision():
    # 不做枚举式意图过滤：任何输入下全部已安装 Skill 都进入披露，
    # 由模型依据自描述决策；模型判定用途不一致时以空数组拦住。
    service = SkillMatchService()
    service.completion_text = '{"skill_ids": [], "reasoning": "画图不在模板声明用途内"}'

    result = await service.route_creation_skills(
        prompt="用 mermaid 画一张我们系统的架构图",
        skills=[ARCH_SKILL, WEEKLY_SKILL],
    )

    assert result["skill_ids"] == []
    assert result["source"] == "model"
    assert len(service.model_calls) == 1
    disclosed = service.model_calls[0]["system_prompt"]
    assert "id=52 架构方案模板" in disclosed
    assert "id=27 GPU成本优化周报模板" in disclosed


@pytest.mark.asyncio
async def test_model_decision_is_returned_when_intent_present():
    service = SkillMatchService()
    service.completion_text = '{"skill_ids": [27], "reasoning": "明确要写本周周报"}'

    result = await service.route_creation_skills(
        prompt="写一份本周的GPU成本优化周报",
        skills=[WEEKLY_SKILL, PLAN_SKILL],
    )

    assert result["skill_ids"] == [27]
    assert result["source"] == "model"
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_model_hallucinated_id_is_filtered_by_whitelist():
    service = SkillMatchService()
    service.completion_text = '{"skill_ids": [9999], "reasoning": "编造的ID"}'

    result = await service.route_creation_skills(
        prompt="写一份本周的GPU成本优化周报",
        skills=[WEEKLY_SKILL],
    )

    assert result["skill_ids"] == []
    assert result["source"] == "model"


@pytest.mark.asyncio
async def test_model_failure_degrades_to_empty_recall():
    service = SkillMatchService()
    service.completion_error = RuntimeError("模型不可用")

    result = await service.route_creation_skills(
        prompt="帮我写一份方案评审文档",
        skills=[PLAN_SKILL],
    )

    assert result == {"skill_ids": [], "source": "fallback", "reasoning": ""}
    assert service.usage_logs and service.usage_logs[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_plan_template_is_recalled_when_purpose_aligns():
    # 用户目标与模板自述用途一致时，模型正常选中，回归保护。
    service = SkillMatchService()
    service.completion_text = '{"skill_ids": [52], "reasoning": "明确要写架构方案"}'

    result = await service.route_creation_skills(
        prompt="帮我写一份推理服务的架构方案",
        skills=[ARCH_SKILL, WEEKLY_SKILL],
    )

    assert result["skill_ids"] == [52]
    assert result["source"] == "model"
    assert len(service.model_calls) == 1
