import json

import pytest

from creation.brainstorm import BrainstormCoordinator, BrainstormGenerationError


class StubCreationService:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        self.model_calls = []

    async def _stream_direct_completion(self, **kwargs):
        self.prompts.append(kwargs["user_prompt"])
        self.model_calls.append(kwargs)
        response = self.responses.pop(0)
        yield response


@pytest.mark.asyncio
async def test_solution_brainstorm_fills_business_coverage_before_branch_detail():
    service = StubCreationService(
        [
            json.dumps(
                {
                    "status": "question",
                    "readiness_reason": "业务目标尚未明确",
                    "open_flags": ["业务目标与预期决策"],
                    "question": {
                        "dimension_id": "business_outcome",
                        "dimension": "业务目标与预期决策",
                        "type": "single_choice",
                        "prompt": "企业知识库首先要改善哪一种业务结果？",
                        "why_now": "业务结果会决定后续用户流程、范围和技术约束。",
                        "required": True,
                        "allow_custom": True,
                        "answer_template": "描述希望改善的业务结果。",
                        "options": [
                            {
                                "id": "find_knowledge",
                                "label": "缩短知识查找与复用时间",
                                "description": "优先改善员工任务效率，后续验证查找成功率和复用闭环。",
                                "recommended": True,
                            },
                            {
                                "id": "governance",
                                "label": "统一知识治理与口径",
                                "description": "优先解决内容权威性，但需要更强的审核和责任机制。",
                                "recommended": False,
                            },
                        ],
                    },
                },
                ensure_ascii=False,
            )
        ]
    )
    coordinator = BrainstormCoordinator(service)

    result = await coordinator.next_step(
        root_request="设计企业知识库方案",
        decisions=[
            {
                "dimension": "部署方式",
                "question": "采用哪种部署方式？",
                "answer": "私有化部署",
                "selected_options": ["私有化部署"],
            }
        ],
        brief_markdown="# 创作简报\n- 部署方式：私有化部署",
    )

    assert result["status"] == "question"
    assert result["question"]["dimension_id"] == "business_outcome"
    assert result["question"]["dimension"] == "业务目标与预期决策"
    assert result["question"]["options"][0]["recommended"] is True
    assert "使用者与业务流程" in result["open_flags"]
    assert result["question"]["id"].startswith("q_")
    assert "私有化部署" in service.prompts[0]
    assert "横向覆盖" in coordinator._system_prompt()
    assert '"id": "business_outcome"' in service.prompts[0]
    assert service.model_calls[0]["json_mode"] is True
    assert service.model_calls[0]["temperature"] == 0.15


@pytest.mark.asyncio
async def test_ready_has_no_fixed_depth_and_recommends_optional_brainstorm_directions():
    ready = json.dumps(
        {
            "status": "ready",
            "readiness_reason": "主方向和关键边界已经清晰",
            "open_flags": ["非关键页面细节"],
            "continuation_directions": [
                {
                    "id": "risk_challenge",
                    "label": "挑战关键假设",
                    "description": "检查当前主方向最可能失败的前提。",
                    "recommended": True,
                },
                {
                    "id": "delivery_detail",
                    "label": "补强落地路径",
                    "description": "继续细化交付节奏、责任和依赖。",
                    "recommended": False,
                },
            ],
        },
        ensure_ascii=False,
    )
    covered = [
        {"dimension_id": dimension_id, "answer": "已确认"}
        for dimension_id in (
            "business_outcome",
            "users_workflow",
            "problem_evidence",
            "solution_architecture",
            "core_capability_mechanism",
            "end_to_end_interaction",
            "quality_evaluation",
            "scope_boundary",
            "ownership_delivery",
            "delivery_rollout",
            "success_criteria",
        )
    ]
    service = StubCreationService([ready])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计产品方案",
        decisions=covered,
        brief_markdown="# 创作简报",
    )
    assert result["status"] == "ready"
    assert result["question"] is None
    assert [item["label"] for item in result["continuation_directions"]] == [
        "挑战关键假设",
        "补强落地路径",
    ]

    forced_service = StubCreationService([ready])
    continued = await BrainstormCoordinator(forced_service).next_step(
        root_request="设计产品方案",
        decisions=covered,
        brief_markdown="# 创作简报",
        force_continue=True,
        focus_hint="挑战关键假设",
    )
    assert continued["status"] == "question"
    assert continued["question"]["dimension_id"] == "continuation_focus"
    assert "挑战关键假设" in continued["question"]["prompt"]
    assert [item["id"] for item in continued["question"]["options"]] == [
        "risk_challenge",
        "delivery_detail",
    ]
    assert len(forced_service.prompts) == 1


def test_ready_rejects_missing_model_recommended_brainstorm_directions():
    with pytest.raises(BrainstormGenerationError):
        BrainstormCoordinator._normalize_result(
            json.dumps(
                {
                    "status": "ready",
                    "readiness_reason": "已经收敛",
                    "open_flags": [],
                    "continuation_directions": [],
                },
                ensure_ascii=False,
            ),
            force_continue=False,
        )

    with pytest.raises(BrainstormGenerationError, match="必答业务维度"):
        BrainstormCoordinator._normalize_result(
            json.dumps(
                {
                    "status": "ready",
                    "readiness_reason": "模型误判为已收敛",
                    "open_flags": [],
                    "continuation_directions": [
                        {
                            "id": "risk",
                            "label": "风险",
                            "description": "继续检查风险。",
                            "recommended": True,
                        },
                        {
                            "id": "delivery",
                            "label": "落地",
                            "description": "继续检查落地。",
                            "recommended": False,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            force_continue=False,
            expected_dimension_id="business_outcome",
        )


def test_technical_solution_coverage_includes_business_data_delivery_and_quality():
    coverage = BrainstormCoordinator._required_coverage(
        "设计广告诊断接口方案",
        [
            {
                "title": "微服务模块技术方案文档",
                "summary": "覆盖业务流程、数据所有权、组织保障和验收。",
            }
        ],
    )

    assert [item["id"] for item in coverage] == [
        "business_outcome",
        "users_workflow",
        "problem_evidence",
        "solution_architecture",
        "core_capability_mechanism",
        "end_to_end_interaction",
        "data_governance",
        "quality_evaluation",
        "scope_boundary",
        "ownership_delivery",
        "delivery_rollout",
        "success_criteria",
        "technical_constraints",
    ]
    assert BrainstormCoordinator._required_coverage("设计一张节日海报", []) == []


def test_legacy_decisions_are_mapped_to_stable_coverage_dimensions():
    covered = BrainstormCoordinator._covered_dimension_ids(
        [
            {"dimension": "主要使用者与业务流程", "answer": "投放运营在复盘时使用"},
            {"dimension": "数据权限与审计", "answer": "按广告账户授权"},
        ]
    )

    assert covered == {"users_workflow", "data_governance"}


def test_explicit_root_request_facts_are_not_asked_again():
    covered = BrainstormCoordinator._root_request_coverage_ids(
        "目标是缩短排障时间，面向投放运营，在广告上线后复盘环节使用；"
        "一期只覆盖账户级诊断，数据来源需经过权限和审计，验收标准是人工排障时长下降。"
    )

    assert {
        "business_outcome",
        "users_workflow",
        "scope_boundary",
        "data_governance",
        "success_criteria",
    }.issubset(covered)


def test_performance_only_ad_diagnosis_history_cannot_satisfy_business_gate():
    decisions = [
        {"dimension": "诊断场景的核心特征", "answer": "实时竞价辅助决策"},
        {"dimension": "性能约束与容错设计", "answer": "P99 低延迟优先"},
        {"dimension": "性能指标量化", "answer": "P95 延迟目标"},
        {"dimension": "资源保护与容错设计", "answer": "主动熔断"},
        {"dimension": "低延迟计算模型", "answer": "单实例计算与本地缓存"},
    ]
    required = BrainstormCoordinator._required_coverage(
        "设计广告诊断接口方案",
        [{"title": "微服务模块技术方案文档"}],
    )
    covered = BrainstormCoordinator._covered_dimension_ids(decisions)
    next_goal = next(item for item in required if item["id"] not in covered)

    assert "technical_constraints" in covered
    assert next_goal["id"] == "business_outcome"


def test_latest_original_script_history_advances_to_architecture_not_ready():
    decisions = [
        {
            "dimension_id": "d_1_target_audience_and_value_proposition",
            "dimension": "目标用户与核心价值主张",
            "question": "原创剧本能力首先面向谁，核心价值是什么？",
            "answer": "面向短视频创作者，快速获得可拍摄的原创剧情。",
        },
        {
            "dimension_id": "d_2_user_scenario_and_pain_point",
            "dimension": "用户场景与痛点",
            "question": "用户在什么场景触发，当前痛点是什么？",
            "answer": "选题枯竭时使用，解决从想法到剧本的困难。",
        },
        {
            "dimension_id": "d_3_user_behavior_and_interaction_flow",
            "dimension": "用户行为与交互流程",
            "question": "进入独立站后的第一个动作是什么？",
            "answer": "输入一句话剧情想法。",
        },
    ]
    required = BrainstormCoordinator._required_coverage(
        "设计下原创剧本如何在快手灵机独立站使用",
        [],
    )
    covered = BrainstormCoordinator._covered_dimension_ids(decisions)
    next_goal = next(item for item in required if item["id"] not in covered)

    assert {"business_outcome", "users_workflow", "problem_evidence"}.issubset(covered)
    assert next_goal["id"] == "solution_architecture"
    assert "能力架构" in next_goal["label"]


def test_skill_steps_enrich_stable_chapter_decisions_without_becoming_one_question_each():
    coverage = BrainstormCoordinator._required_coverage(
        "设计原创剧本生成方案",
        [
            {
                "title": "产品技术方案模板",
                "executionSteps": [
                    {
                        "id": "architecture",
                        "title": "总体方案与系统边界",
                        "objective": "确定组件职责、同步异步边界和演进路径。",
                        "output": "架构总览",
                    },
                    {
                        "id": "feature",
                        "title": "原创剧本生成机制",
                        "objective": "比较专家知识、规则约束和模型生成的组合方式。",
                        "output": "生成机制方案",
                    },
                    {
                        "id": "write",
                        "title": "撰写全文",
                        "objective": "按章节生成完整文档。",
                        "output": "文档",
                    },
                ],
            }
        ],
    )
    by_id = {item["id"]: item for item in coverage}

    assert len(coverage) == len({item["id"] for item in coverage})
    assert by_id["solution_architecture"]["source_steps"][0]["step"] == "总体方案与系统边界"
    assert by_id["core_capability_mechanism"]["source_steps"][0]["step"] == "原创剧本生成机制"
    assert "专家知识、规则约束和模型生成" in by_id["core_capability_mechanism"]["question_goal"]
    assert all("撰写全文" not in str(item.get("source_steps", [])) for item in coverage)


def test_question_rejects_unsupported_exact_performance_target():
    raw = json.dumps(
        {
            "status": "question",
            "readiness_reason": "仍需确认",
            "open_flags": [],
            "question": {
                "dimension_id": "technical_constraints",
                "dimension": "技术约束与质量属性",
                "type": "single_choice",
                "prompt": "接口的延迟目标选哪一项？",
                "why_now": "这会影响实现。",
                "required": True,
                "allow_custom": True,
                "answer_template": "补充依据。",
                "options": [
                    {
                        "id": "fast",
                        "label": "P95 < 10ms",
                        "description": "采用该精确目标。",
                        "recommended": True,
                    },
                    {
                        "id": "calibrate",
                        "label": "压测定标",
                        "description": "先依据业务链路测量。",
                        "recommended": False,
                    },
                ],
            },
        },
        ensure_ascii=False,
    )

    with pytest.raises(BrainstormGenerationError, match="缺乏.*依据"):
        BrainstormCoordinator._normalize_result(
            raw,
            force_continue=False,
            expected_dimension_id="technical_constraints",
            root_request="设计广告诊断接口方案",
            decisions=[],
        )


def test_chapter_decision_rejects_macro_placeholder_options():
    raw = json.dumps(
        {
            "status": "question",
            "readiness_reason": "仍需确认架构",
            "open_flags": [],
            "question": {
                "dimension_id": "solution_architecture",
                "dimension": "总体方案与能力架构",
                "type": "single_choice",
                "prompt": "整体架构选择哪个方向？",
                "why_now": "架构会影响后续章节。",
                "required": True,
                "allow_custom": True,
                "answer_template": "补充实际架构方向。",
                "options": [
                    {
                        "id": "recommended",
                        "label": "推荐方向",
                        "description": "采用推荐的总体方向。",
                        "recommended": True,
                    },
                    {
                        "id": "alternative",
                        "label": "备选方向",
                        "description": "采用另一个总体方向。",
                        "recommended": False,
                    },
                ],
            },
        },
        ensure_ascii=False,
    )

    with pytest.raises(BrainstormGenerationError, match="过于宏观"):
        BrainstormCoordinator._normalize_result(
            raw,
            force_continue=False,
            expected_dimension_id="solution_architecture",
            root_request="设计原创剧本如何在灵机独立站使用",
            decisions=[],
        )


@pytest.mark.asyncio
async def test_selected_skill_rules_are_included_in_brainstorm_prompt():
    response = json.dumps(
        {
            "status": "question",
            "readiness_reason": "业务目标尚未确认",
            "open_flags": ["业务目标"],
            "question": {
                "dimension_id": "business_outcome",
                "dimension": "业务目标与预期决策",
                "type": "single_choice",
                "prompt": "广告诊断首先要推动什么业务动作？",
                "why_now": "该动作决定流程、责任和技术约束。",
                "required": True,
                "allow_custom": True,
                "answer_template": "描述业务动作和价值。",
                "options": [
                    {
                        "id": "reduce_diagnosis_time",
                        "label": "缩短异常定位时间",
                        "description": "优先让运营更快找到问题，后续方案聚焦诊断效率。",
                        "recommended": True,
                    },
                    {
                        "id": "improve_action_quality",
                        "label": "提高处置建议质量",
                        "description": "优先改善下一步动作，但需要更强的证据与反馈闭环。",
                        "recommended": False,
                    },
                ],
            },
        },
        ensure_ascii=False,
    )
    service = StubCreationService([response])

    await BrainstormCoordinator(service).next_step(
        root_request="设计广告诊断接口方案",
        decisions=[],
        brief_markdown="# 创作简报",
        selected_skills=[
            {
                "title": "微服务模块技术方案文档",
                "summary": "必须覆盖业务流程、数据所有权、RACI 和上线验收。",
                "executionSteps": [{"title": "组织与人员保障"}],
            }
        ],
    )

    assert "微服务模块技术方案文档" in service.prompts[0]
    assert "数据所有权" in service.prompts[0]
    assert "组织与人员保障" in service.prompts[0]


@pytest.mark.asyncio
async def test_invalid_first_output_is_retried_with_json_correction():
    service = StubCreationService(
        [
            "不是 JSON",
            json.dumps(
                {
                    "status": "question",
                    "readiness_reason": "仍需确认",
                    "open_flags": [],
                    "question": {
                        "dimension_id": "business_outcome",
                        "dimension": "业务目标与预期决策",
                        "type": "single_choice",
                        "prompt": "这份运营方案首先要改善什么业务结果？",
                        "why_now": "业务结果决定后续范围与验收标准。",
                        "required": True,
                        "allow_custom": True,
                        "answer_template": "描述目标、业务动作和价值。",
                        "options": [
                            {
                                "id": "retention",
                                "label": "提升用户留存",
                                "description": "围绕持续使用设计运营闭环。",
                                "recommended": True,
                            },
                            {
                                "id": "conversion",
                                "label": "提升关键转化",
                                "description": "围绕单次关键动作优化路径。",
                                "recommended": False,
                            },
                        ],
                    },
                },
                ensure_ascii=False,
            ),
        ]
    )
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计运营方案",
        decisions=[],
        brief_markdown="# 创作简报",
    )
    assert result["question"]["type"] == "single_choice"
    assert len(service.prompts) == 2
    assert "上一次输出未通过质量或结构校验" in service.prompts[1]


@pytest.mark.asyncio
async def test_answered_question_cannot_be_reused_under_a_new_dimension_id():
    repeated_prompt = "剧本生成结果应通过何种机制配置维护、审核发布及异常处理？"
    repeated = {
        "status": "question",
        "readiness_reason": "仍需确认实施路径",
        "open_flags": [],
        "question": {
            "dimension_id": "delivery_rollout",
            "dimension": "实施路径、依赖与演进",
            "type": "single_choice",
            "prompt": repeated_prompt,
            "why_now": "实施路径尚未确认。",
            "options": [
                {
                    "id": "auto_review",
                    "label": "自动审核后发布",
                    "description": "上线较快，但需要异常兜底。",
                    "recommended": True,
                },
                {
                    "id": "manual_review",
                    "label": "人工审核后发布",
                    "description": "合规更稳，但运营成本更高。",
                    "recommended": False,
                },
            ],
        },
    }
    corrected = {
        "status": "question",
        "readiness_reason": "仍需确认实施路径",
        "open_flags": [],
        "question": {
            "dimension_id": "delivery_rollout",
            "dimension": "实施路径、依赖与演进",
            "type": "single_choice",
            "prompt": "一期应如何试点上线，并在效果不达标时回退？",
            "why_now": "试点与回退策略决定可交付路径。",
            "options": [
                {
                    "id": "small_cohort",
                    "label": "小范围创作者灰度",
                    "description": "先验证核心闭环，失败时可快速关闭入口。",
                    "recommended": True,
                },
                {
                    "id": "internal_pilot",
                    "label": "内部运营试用",
                    "description": "风险更低，但真实用户反馈较少。",
                    "recommended": False,
                },
            ],
        },
    }
    service = StubCreationService(
        [json.dumps(repeated, ensure_ascii=False), json.dumps(corrected, ensure_ascii=False)]
    )

    result = await BrainstormCoordinator(service).next_step(
        root_request="设计下原创剧本如何在快手灵机独立站使用",
        decisions=[
            {
                "dimension_id": dimension_id,
                "question": repeated_prompt if dimension_id == "ownership_delivery" else dimension_id,
                "answer": "已确认",
            }
            for dimension_id in (
                "business_outcome",
                "users_workflow",
                "problem_evidence",
                "solution_architecture",
                "core_capability_mechanism",
                "end_to_end_interaction",
                "quality_evaluation",
                "scope_boundary",
                "ownership_delivery",
            )
        ],
        brief_markdown="# 创作简报",
    )

    assert result["question"]["dimension_id"] == "delivery_rollout"
    assert result["question"]["prompt"] == "一期应如何试点上线，并在效果不达标时回退？"
    assert len(service.prompts) == 2
    assert "问题重复了已经回答过的脑暴问题" in service.prompts[1]


@pytest.mark.asyncio
async def test_choice_with_too_few_options_retries_to_limit_then_fails():
    one_option = json.dumps(
        {
            "status": "question",
            "readiness_reason": "仍需确认使用流程",
            "open_flags": [],
            "question": {
                "dimension_id": "users_workflow",
                "dimension": "使用者与业务流程",
                "type": "single_choice",
                "prompt": "原创剧本能力应嵌入用户的哪个核心创作环节？",
                "why_now": "使用环节会决定后续交互链路和能力边界。",
                "required": True,
                "allow_custom": True,
                "answer_template": "描述主要使用者、触发时机和使用流程。",
                "options": [
                    {
                        "id": "before_creation",
                        "label": "创作前辅助生成",
                        "description": "在动笔前提供灵感和结构，缩短从想法到初稿的路径。",
                        "recommended": True,
                    }
                ],
            },
        },
        ensure_ascii=False,
    )
    service = StubCreationService(
        [one_option] * BrainstormCoordinator.MAX_GENERATION_ATTEMPTS
    )

    with pytest.raises(BrainstormGenerationError, match="连续 3 次"):
        await BrainstormCoordinator(service).next_step(
            root_request="设计下原创剧本如何在快手灵机独立站使用",
            decisions=[
                {
                    "dimension_id": "business_outcome",
                    "dimension": "业务目标与预期决策",
                    "answer": "提升用户留存与活跃度",
                }
            ],
            brief_markdown="# 创作简报\n- 业务目标：提升用户留存与活跃度",
        )

    assert len(service.prompts) == BrainstormCoordinator.MAX_GENERATION_ATTEMPTS
    assert "动态选择题需要 2 到 5 个选项" in service.prompts[1]
    assert "options 必须包含 2 到 5 个完整对象" in service.prompts[2]


def test_free_text_question_type_is_rejected():
    with pytest.raises(BrainstormGenerationError, match="只能返回"):
        BrainstormCoordinator._normalize_result(
            json.dumps(
                {
                    "status": "question",
                    "readiness_reason": "仍需确认",
                    "open_flags": [],
                    "question": {
                        "dimension_id": "business_outcome",
                        "dimension": "业务目标与预期决策",
                        "type": "free_text",
                        "prompt": "首先改善什么业务结果？",
                        "why_now": "该结果决定后续方向。",
                        "required": True,
                        "allow_custom": True,
                        "answer_template": "补充其他方向。",
                        "options": [],
                    },
                },
                ensure_ascii=False,
            ),
            force_continue=False,
        )


def test_duplicate_option_id_is_rejected():
    options = [
        {
            "id": "route_a",
            "label": "路线 A",
            "description": "优先速度，但个性化较弱。",
            "recommended": True,
        },
        {
            "id": "route_b",
            "label": "路线 B",
            "description": "优先个性化，但实现更复杂。",
            "recommended": False,
        },
    ]
    options[1]["id"] = "route_a"
    raw = json.dumps(
        {
            "status": "question",
            "readiness_reason": "仍需确认",
            "open_flags": [],
            "question": {
                "dimension_id": "business_outcome",
                "dimension": "业务目标与预期决策",
                "type": "single_choice",
                "prompt": "首先采用哪条方向？",
                "why_now": "该方向决定后续方案。",
                "required": True,
                "allow_custom": True,
                "answer_template": "补充其他方向。",
                "options": options,
            },
        },
        ensure_ascii=False,
    )

    with pytest.raises(BrainstormGenerationError, match="id 重复"):
        BrainstormCoordinator._normalize_result(raw, force_continue=False)


def test_question_recovers_missing_or_aliased_option_descriptions():
    question = {
        "dimension_id": "business_outcome",
        "dimension": "业务目标与预期决策",
        "type": "single_choice",
        "prompt": "这项能力首先要推动哪一种业务结果？",
        "why_now": "业务结果会改变后续方案的设计重点。",
        "options": [
            {
                "id": "efficiency",
                "label": "优先改善工作效率",
                "description": "",
                "recommended": True,
            },
            {
                "id": "quality",
                "label": "优先改善结果质量",
                "tradeoff": "需要增加质量评估与人工反馈闭环。",
                "recommended": False,
            },
        ],
    }

    normalized = BrainstormCoordinator._normalize_question(question)

    assert normalized["options"][0]["description"] == (
        "选择“优先改善工作效率”会作为后续方案的方向依据；"
        "具体收益、约束与代价仍需结合后续回答校验。"
    )
    assert normalized["options"][1]["description"] == (
        "需要增加质量评估与人工反馈闭环。"
    )


@pytest.mark.parametrize(
    "recommendations, expected_recommended_id",
    [
        ([False, False], "route_a"),
        ([True, True], "route_a"),
        (["false", "true"], "route_b"),
    ],
)
def test_question_recommendation_metadata_is_normalized(
    recommendations, expected_recommended_id
):
    options = [
        {
            "id": "route_a",
            "label": "路线 A",
            "description": "优先速度，但个性化较弱。",
            "recommended": recommendations[0],
        },
        {
            "id": "route_b",
            "label": "路线 B",
            "description": "优先个性化，但实现更复杂。",
            "recommended": recommendations[1],
        },
    ]
    raw = json.dumps(
        {
            "status": "question",
            "readiness_reason": "仍需确认",
            "open_flags": [],
            "question": {
                "dimension_id": "business_outcome",
                "dimension": "业务目标与预期决策",
                "type": "single_choice",
                "prompt": "首先采用哪条方向？",
                "why_now": "该方向决定后续方案。",
                "required": True,
                "allow_custom": True,
                "answer_template": "补充其他方向。",
                "options": options,
            },
        },
        ensure_ascii=False,
    )

    result = BrainstormCoordinator._normalize_result(raw, force_continue=False)
    normalized = result["question"]["options"]

    assert normalized[0]["id"] == expected_recommended_id
    assert [item["recommended"] for item in normalized] == [True, False]


def test_output_shape_diagnostic_keeps_only_non_content_metadata():
    diagnostic = BrainstormCoordinator._output_shape_diagnostic(
        json.dumps(
            {
                "status": "question",
                "question": {
                    "type": "single_choice",
                    "prompt": "不应进入日志的业务问题",
                    "options": [{"label": "不应进入日志的方向"}],
                },
            },
            ensure_ascii=False,
        )
    )

    assert diagnostic == {
        "status": "question",
        "question_type": "single_choice",
        "option_count": 1,
    }
    assert "业务问题" not in json.dumps(diagnostic, ensure_ascii=False)


def test_system_prompt_choice_example_satisfies_option_count_contract():
    prompt = BrainstormCoordinator._system_prompt()

    assert "绝不能只提供 0 或 1 个" in prompt
    assert "不得用 free_text" in prompt
    assert '"id":"recommended_option"' in prompt
    assert '"id":"alternative_option"' in prompt
