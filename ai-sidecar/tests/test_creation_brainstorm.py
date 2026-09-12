import json

import pytest

from creation.brainstorm import BrainstormCoordinator, BrainstormGenerationError


class StubCreationService:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        self.model_calls = []

    def analyze_requirement(self, query, options, **kwargs):
        return {}

    def retrieve_references(self, query, requirement, options):
        return []

    async def _stream_direct_completion(self, **kwargs):
        self.prompts.append(kwargs["user_prompt"])
        self.model_calls.append(kwargs)
        response = self.responses.pop(0)
        yield response


@pytest.mark.parametrize("root", [
    "我想设计一场小型读书交流会，先帮我梳理方向。",
    "我想设计一场小型读书交流会，先帮我梳理方向。这是创作功能验收样例，不检索个人资料。",
    "设计一张节日海报", "构思一个童话故事", "设计一个接口技术方案",
])
def test_default_coverage_is_deliverable_neutral_and_cannot_be_changed_by_request_keywords(root):
    coverage = BrainstormCoordinator._required_coverage(root, [])
    assert [item["id"] for item in coverage] == [
        "business_outcome", "users_workflow", "solution_architecture", "scope_boundary", "success_criteria"]
    assert [item["label"] for item in coverage] == [
        "目标与期望结果", "面向对象与使用情境", "内容结构与呈现方式", "范围与现实约束", "完成与成功标准"]


def test_skill_title_and_incidental_metadata_cannot_impose_professional_coverage():
    expected = BrainstormCoordinator._required_coverage("读书交流会", [])
    assert BrainstormCoordinator._required_coverage("读书交流会", [{
        "title": "技术能力架构方案", "summary": "业务流程、权限、技术性能、数据治理",
    }]) == expected


def test_selected_skill_adds_actual_decisions_with_stable_recoverable_ids():
    skill = {"id": "reading", "title": "读书交流会组织", "executionSteps": [
        {"id": "pace", "title": "讨论节奏安排", "objective": "比较自由发言与轮流分享的交流体验", "output": "交流节奏"},
        {"id": "write", "title": "撰写活动说明", "objective": "整理已确认的讨论安排"},
    ]}
    coverage = BrainstormCoordinator._required_coverage("组织读书交流会", [skill])
    extra = [item for item in coverage if item["id"].startswith("skill_decision_")]
    assert len(extra) == 1
    assert extra[0]["label"] == "讨论节奏安排"
    assert "自由发言与轮流分享" in extra[0]["question_goal"]
    assert "系统架构" not in extra[0]["question_goal"]
    renamed = {**skill, "executionSteps": [{**skill["executionSteps"][0], "title": "交流节奏选择"}]}
    revised = BrainstormCoordinator._required_coverage("组织读书交流会", [renamed])
    assert extra[0]["id"] in {item["id"] for item in revised}
    assert extra[0]["id"] in BrainstormCoordinator._covered_dimension_ids([
        {"dimension_id": extra[0]["id"], "answer": "轮流分享", "answer_source": "user"}])


@pytest.mark.parametrize("restriction", [
    "不检索个人资料", "不要查询我的历史记录", "禁止使用私人数据", "无需检索本地文档",
    "个人资料不要查询", "不需要检索", "只根据本次输入构思", "仅使用已提供的材料",
    "只用当前输入", "不能查询个人的资料", "不可以读取用户历史信息", "Do not search my personal records",
])
def test_explicit_source_limits_skip_before_query_analysis_and_retrieval(restriction):
    class NoAccessService:
        def analyze_requirement(self, *args, **kwargs):
            raise AssertionError("Source restriction must run before query analysis")
        def retrieve_references(self, *args, **kwargs):
            raise AssertionError("Private retrieval is forbidden")
    result = BrainstormCoordinator(NoAccessService())._retrieve_memory(
        "帮我构思读书交流会，" + restriction, [], "", "")
    assert result["status"] == "skipped"
    assert result["sources"] == []


@pytest.mark.parametrize("root", [
    "不查询公开资料，参考本地记忆即可", "不要联网", "不需要图片", "不只检索个人资料，还要找公开资料",
    "不是不检索个人资料，而是不要联网", "请分析这句台词：‘不要检索个人资料’",
    '示例写着“禁止查询历史记录”，这只是引用', "请审阅这句代码：`不要检索个人资料`",
    '请按“简洁”风格写，分析这句台词：“不要检索个人资料”',
    '请遵守“每段简短”这个要求；示例写着“禁止查询历史记录”，这只是引用',
])
def test_source_limit_scope_and_quoted_content_do_not_disable_allowed_memory(root):
    assert BrainstormCoordinator._memory_allowed(root, []) is True


def test_current_user_answers_can_change_access_but_model_assumptions_cannot():
    root = "不检索个人资料"
    assert BrainstormCoordinator._memory_allowed(root, [{"answer_source": "agent_assumption", "answer": "可以检索个人资料"}]) is False
    assert BrainstormCoordinator._memory_allowed(root, [{"question_id": "q", "answer_source": "user", "answer": "现在可以检索个人资料"}], user_input_revisions={"root_request": 0, "q": 1}) is True
    assert BrainstormCoordinator._memory_allowed("构思读书交流会", [{"answer_source": "user", "answer": "不要查询历史记录"}]) is False


@pytest.mark.parametrize("answer", ["可以检索个人资料吗？", "是否可以检索个人资料", "尚未同意检索个人资料", "不是允许检索个人资料"])
def test_question_or_negated_permission_cannot_lift_an_existing_source_limit(answer):
    assert BrainstormCoordinator._memory_allowed("不检索个人资料", [{"answer_source": "user", "answer": answer}]) is False


@pytest.mark.parametrize("root", ["不得检索个人资料", "请勿检索个人资料", "不允许你检索个人资料", "请遵守“不检索个人资料”这个要求",
    "不允许检索或读取个人资料", "不要检索、使用我的个人资料", "请勿搜索和使用私人文档"])
def test_explicit_negative_scope_survives_modality_pronouns_and_emphasized_quotes(root):
    assert BrainstormCoordinator._memory_allowed(root, []) is False


@pytest.mark.parametrize("answer", ["我没说可以检索个人资料", "等我同意检索个人资料后再开始", "假如允许检索个人资料会怎样", "我只是转述：可以检索个人资料"])
def test_reported_or_conditional_permissions_never_lift_a_source_limit(answer):
    assert BrainstormCoordinator._memory_allowed("不检索个人资料", [{"answer_source": "user", "answer": answer}]) is False


def test_without_revision_evidence_current_root_limit_outranks_old_answer_permission():
    assert BrainstormCoordinator._memory_allowed("不检索个人资料", [
        {"question_id": "old", "answer_source": "user", "answer": "可以检索个人资料"}]) is False


@pytest.mark.parametrize("revisions, expected", [
    ({"root_request": 2, "q": 1}, False),
    ({"root_request": 2, "q": 3}, True),
    ({"root_request": 2, "q": 2}, False),
    ({"q": 3}, False),
    ({"root_request": 2}, False),
])
def test_source_permission_requires_proven_newer_user_revision(revisions, expected):
    assert BrainstormCoordinator._memory_allowed("旧目标", [
        {"question_id": "q", "answer_source": "user", "answer": "现在可以检索个人资料"}],
        {"root_request": "不检索个人资料"}, revisions) is expected


def test_edited_answer_and_open_flags_use_their_own_revision():
    decisions = [{"question_id": "q", "answer_source": "agent_assumption", "answer": "旧的推断"}]
    edits = {"q": "现在可以检索个人资料", "open_flags": "不要查询个人资料"}
    assert not BrainstormCoordinator._memory_allowed("读书会", decisions, edits, {"q": 3, "open_flags": 4})
    assert BrainstormCoordinator._memory_allowed("读书会", decisions, edits, {"q": 5, "open_flags": 4})
    assert BrainstormCoordinator._memory_allowed("现在可以检索个人资料", [
        {"question_id": "q", "answer_source": "user", "answer": "不检索个人资料"}],
        user_input_revisions={"root_request": 6, "q": 5})


def test_combined_selected_direction_and_custom_permission_keep_original_user_inputs():
    combined = {"question_id": "continue", "answer_source": "user",
        "answer": "交流节奏；现在可以检索个人资料",
        "user_inputs": ["交流节奏", "现在可以检索个人资料"]}
    assert BrainstormCoordinator._memory_allowed("不检索个人资料", [combined],
        user_input_revisions={"root_request": 0, "continue": 1})
    assert not BrainstormCoordinator._memory_allowed("不检索个人资料", [combined],
        user_input_revisions={"root_request": 2, "continue": 1})
    decisions, brief = BrainstormCoordinator._user_only_context("不检索个人资料", [combined])
    assert decisions[0]["answer"] == "交流节奏"
    assert "可以检索个人资料" not in brief
    # A manual replacement clears the original grant as well as its summary.
    assert not BrainstormCoordinator._memory_allowed("不检索个人资料", [combined],
        {"continue": "交流保持轻松"}, {"root_request": 0, "continue": 3})


def test_explicit_local_grant_can_keep_an_independent_external_restriction():
    assert BrainstormCoordinator._memory_allowed("不检索个人资料", [
        {"question_id": "q", "answer_source": "user", "answer": "现在可以检索个人资料，但不要联网"}],
        user_input_revisions={"root_request": 0, "q": 1})


def test_disabled_sources_keep_only_user_excluded_topic_without_old_question_facts():
    decisions, brief = BrainstormCoordinator._user_only_context("不使用个人资料", [{
        "question_id": "q", "dimension_id": "skill_decision_procurement", "dimension": "采购预算",
        "answer_source": "user_excluded", "answer": "用户排除：PRIVATE-QUESTION",
        "question": "PRIVATE-QUESTION", "selected_options": [{"tradeoff": "PRIVATE-OPTION"}],
    }])
    assert decisions[0]["excluded_topic"] == "采购预算"
    assert "采购预算" in brief and "只是排除范围" in brief
    assert "PRIVATE-" not in json.dumps(decisions, ensure_ascii=False) + brief


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["user", "confirmed_selection"])
async def test_disabled_sources_preserve_current_explicit_focus_and_manual_user_fields(origin):
    service = StubCreationService([json.dumps(concise_question_payload("希望讨论怎样的交流体验？"), ensure_ascii=False)])
    await BrainstormCoordinator(service).next_step(
        root_request="不检索个人资料", decisions=[
            {"question_id": "q", "dimension_id": "users_workflow", "answer_source": "agent_assumption", "answer": "旧私人猜测"}],
        brief_edits={"q": "用户确认轮流分享", "open_flags": "注意照顾新成员"},
        brief_markdown="PRIVATE-CACHED-EVIDENCE", focus_hint="用户选择轻松讨论", focus_hint_source=origin)
    prompt = service.prompts[0]
    assert all(text in prompt for text in ("用户确认轮流分享", "注意照顾新成员", "用户选择轻松讨论"))
    assert "PRIVATE-CACHED-EVIDENCE" not in prompt and "旧私人猜测" not in prompt


@pytest.mark.asyncio
async def test_disabled_sources_do_not_survive_inside_old_question_option_or_focus_context():
    value = concise_question_payload("你希望读书交流会带来怎样的体验？")
    service = StubCreationService([json.dumps(value, ensure_ascii=False)])
    result = await BrainstormCoordinator(service).next_step(
        root_request="组织读书会，不使用个人资料",
        decisions=[{"question_id": "old", "dimension_id": "users_workflow", "answer_source": "user",
                    "answer": "自由发言", "question": "PRIVATE-CACHED-EVIDENCE-QUESTION",
                    "selected_options": [{"label": "自由发言", "tradeoff": "记忆依据：PRIVATE-CACHED-EVIDENCE-OPTION"}]}],
        brief_markdown="# 创作简报\n## 已确认问题\nPRIVATE-CACHED-EVIDENCE-BRIEF",
        focus_hint="旧分支说明：PRIVATE-CACHED-EVIDENCE-FOCUS")
    assert "未检索历史记忆" in result["memory_brief"]
    assert "自由发言" in service.prompts[0]
    assert "PRIVATE-CACHED-EVIDENCE" not in service.prompts[0]


@pytest.mark.asyncio
async def test_skipped_memory_has_honest_notice_and_does_not_reuse_old_brief_sources():
    value = concise_question_payload("你希望这次交流会带来怎样的体验？")
    value["question"]["dimension"] = "目标与期望结果"
    service = StubCreationService([json.dumps(value, ensure_ascii=False)])
    def forbidden(*args, **kwargs):
        raise AssertionError("No private query is allowed")
    service.analyze_requirement = forbidden
    service.retrieve_references = forbidden
    result = await BrainstormCoordinator(service).next_step(
        root_request="组织读书交流会，不检索个人资料", decisions=[],
        brief_edits={"open_flags": "活动轻松即可"},
        brief_markdown="# 创作简报\n## 历史记忆参考\n旧私人材料标记\n## 当前决定\n活动轻松即可")
    assert "按你的资料范围要求未检索" in result["memory_brief"]
    assert "本轮已检索" not in result["readiness_reason"]
    assert "旧私人材料标记" not in service.prompts[0]
    assert "活动轻松即可" in service.prompts[0]
    assert len(service.model_calls) == 1
    assert '"label": "目标与期望结果"' in service.prompts[0]
    assert "总体方案与能力架构" not in result["open_flags"]


@pytest.mark.asyncio
async def test_brainstorm_fills_missing_background_when_no_branch_is_selected():
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
    assert "面向对象与使用情境" in result["open_flags"]
    assert result["question"]["id"].startswith("q_")
    assert "私有化部署" in service.prompts[0]
    assert '"next_question_goal": {"id": "business_outcome"' in service.prompts[0]
    assert '"id": "business_outcome"' in service.prompts[0]
    assert service.model_calls[0]["json_mode"] is True
    assert service.model_calls[0]["temperature"] == 0.15


@pytest.mark.asyncio
@pytest.mark.parametrize("open_flags", [
    [],
    ["后续上线前必须确认第三方接口授权范围"],
    ["后续可细化具体执行步骤或技术选型，但不影响当前方案主体方向"],
])
async def test_ready_has_no_fixed_depth_and_recommends_optional_brainstorm_directions(open_flags):
    ready = json.dumps(
        {
            "status": "ready",
            "readiness_reason": "主方向和关键边界已经清晰",
            "open_flags": open_flags,
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
    # Optional directions do not manufacture assumptions; concrete risks and
    # legacy notes are retained without guessing their meaning from keywords.
    assert result["open_flags"] == open_flags
    assert [item["label"] for item in result["continuation_directions"]] == [
        "挑战关键假设",
        "补强落地路径",
    ]

    concrete = concise_question_payload("怎样检验当前方案的关键假设？")
    concrete["question"]["dimension_id"] = "assumption_test"
    forced_service = StubCreationService([ready, json.dumps(concrete, ensure_ascii=False)])
    continued = await BrainstormCoordinator(forced_service).next_step(
        root_request="设计产品方案",
        decisions=covered,
        brief_markdown="# 创作简报",
        force_continue=True,
        focus_hint="挑战关键假设",
    )
    assert continued["status"] == "question"
    assert continued["question"]["dimension_id"] == "assumption_test"
    assert continued["question"]["prompt"] == concrete["question"]["prompt"]
    assert len(forced_service.prompts) == 2
    assert "已选分支尚待深入" in forced_service.prompts[1]


def test_model_contract_separates_unresolved_items_from_optional_refinement():
    system_prompt = BrainstormCoordinator._system_prompt()
    ready_example = next(
        json.loads(line) for line in system_prompt.splitlines()
        if line.startswith('{"status":"ready"')
    )
    assert ready_example["open_flags"] == []
    assert len(ready_example["continuation_directions"]) == 2
    assert "每项必须说清什么尚未确定" in system_prompt
    assert "已由用户回答、人工修订或明确排除的事项不得重复列入" in system_prompt
    assert "这些建议只放在 continuation_directions" in system_prompt
    assert "不授权代替用户作决定或自动采用假设" in system_prompt


@pytest.mark.asyncio
async def test_suggest_directions_interrupts_current_question_and_returns_alternatives():
    ready = json.dumps(
        {
            "status": "ready",
            "readiness_reason": "已根据当前简报整理新的探索方向",
            "open_flags": ["原问题尚未确认"],
            "continuation_directions": [
                {
                    "id": "user_journey",
                    "label": "转向用户链路",
                    "description": "从实际使用流程重新检查方案。",
                    "recommended": True,
                },
                {
                    "id": "risk_boundary",
                    "label": "转向风险边界",
                    "description": "优先挑战当前方案的失败前提。",
                    "recommended": False,
                },
            ],
        },
        ensure_ascii=False,
    )
    service = StubCreationService([ready])

    result = await BrainstormCoordinator(service).next_step(
        root_request="设计产品方案",
        decisions=[],
        brief_markdown="# 创作简报\n\n当前仍在确认业务目标",
        suggest_directions=True,
    )

    assert result["status"] == "ready"
    assert result["question"] is None
    assert [item["id"] for item in result["continuation_directions"]] == [
        "user_journey",
        "risk_boundary",
    ]
    assert '"suggest_directions": true' in service.prompts[0]
    assert "只返回 ready" in service.prompts[0]


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

    with pytest.raises(BrainstormGenerationError, match="必答创作维度"):
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


def test_explicit_technical_skill_steps_add_data_delivery_and_quality_coverage():
    coverage = BrainstormCoordinator._required_coverage(
        "设计广告诊断接口方案",
        [
            {
                "title": "微服务模块技术方案文档",
                "summary": "覆盖业务流程、数据所有权、组织保障和验收。",
                "executionSteps": [
                    {"id": "flow", "title": "完整调用链路"},
                    {"id": "data", "title": "数据所有权与权限"},
                    {"id": "quality", "title": "质量评估"},
                    {"id": "rollout", "title": "实施路径"},
                    {"id": "constraints", "title": "性能与容量"},
                ],
            }
        ],
    )

    assert [item["id"] for item in coverage] == [
        "business_outcome",
        "users_workflow",
        "solution_architecture",
        "end_to_end_interaction",
        "data_governance",
        "quality_evaluation",
        "delivery_rollout",
        "technical_constraints",
        "scope_boundary",
        "success_criteria",
    ]
    assert all(item.get("source_steps") for item in coverage[3:-2])


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


def test_known_goal_and_audience_still_require_substantive_content_decisions():
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
    assert next_goal["label"] == "内容结构与呈现方式"
    assert "主体内容" in next_goal["question_goal"]


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
    assert result["question"]["type"] == "multi_choice"
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
        selected_skills=[{"title": "产品实施方案", "executionSteps": [{"id": "rollout", "title": "实施路径"}]}],
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


def test_duplicate_option_id_is_repaired_without_changing_choices():
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

    result = BrainstormCoordinator._normalize_result(raw, force_continue=False)
    normalized = result["question"]["options"]
    assert len({item["id"] for item in normalized}) == len(options)
    assert [(item["label"], item["description"]) for item in normalized] == [
        (item["label"], item["description"]) for item in options
    ]


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
        "选择“优先改善工作效率”会作为后续创作的方向依据；"
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


def concise_question_payload(prompt):
    return {
        "status": "question",
        "question": {
            "dimension_id": "business_outcome",
            "prompt": prompt,
            "why_now": "确认优先级以决定方案重点。",
            "answer_template": "可补充真实案例、频率与影响。",
            "options": [
                {"id": "editing", "label": "人工修改成本高且耗时",
                 "description": "优先改善修改流程。", "recommended": True},
                {"id": "feedback", "label": "缺乏数据反馈无法优化模型效果",
                 "description": "优先建立反馈闭环。", "recommended": False},
            ],
        },
    }


@pytest.mark.parametrize("prompt", [
    "主要痛点是什么？1）人工修改成本高且耗时；2）缺乏数据反馈无法优化模型效果。",
    "主要痛点是人工修改成本高且耗时，还是缺乏数据反馈无法优化模型效果？",
    "主要痛点是人工修改成本高且耗时，还是缺乏数据反馈 无法优化模型效果？",
])
def test_question_rejects_repeated_choices_in_title(prompt):
    with pytest.raises(BrainstormGenerationError, match="题干重复列举多个选项"):
        BrainstormCoordinator._normalize_question(concise_question_payload(prompt)["question"])


@pytest.mark.parametrize("prompt", [
    "品牌方市场人员生成视频剧本时，主要痛点是什么？",
    "针对人工修改成本高且耗时的问题，下一步应优先改善什么？",
])
def test_question_preserves_necessary_context_and_answer_guidance(prompt):
    question = concise_question_payload(prompt)["question"]
    result = BrainstormCoordinator._normalize_question(question)
    assert result["prompt"] == prompt
    assert result["options"] == question["options"]
    assert result["answer_template"] == question["answer_template"]


@pytest.mark.asyncio
async def test_question_repairs_duplicate_title_without_losing_choices():
    verbose = concise_question_payload(
        "主要痛点是人工修改成本高且耗时，还是缺乏数据反馈无法优化模型效果？"
    )
    concise = concise_question_payload("当前生成视频剧本的主要痛点是什么？")
    service = StubCreationService([json.dumps(verbose), json.dumps(concise)])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计视频剧本生成方案", decisions=[], brief_markdown="",
    )
    assert result["question"]["prompt"] == concise["question"]["prompt"]
    for actual, expected in zip(result["question"]["options"], concise["question"]["options"]):
        assert actual["id"] == expected["id"]
        assert actual["label"] == expected["label"]
        assert actual["description"].startswith(expected["description"])
        assert "未附历史记忆引用" in actual["details"]
    assert "题干重复列举多个选项" in service.prompts[1]
    system_prompt = service.model_calls[0]["system_prompt"]
    assert "不得在题干中重复列举、编号或改写" in system_prompt
    assert "在 options 中枚举真实可行的方向" in system_prompt


def test_question_defaults_to_multi_choice_unless_exclusivity_is_explained():
    question = {
        "dimension": "方向", "prompt": "你希望如何推进方案？", "why_now": "明确可并行的方向",
        "options": [
            {"id": "a", "label": "路径 A", "description": "方向一", "recommended": True},
            {"id": "b", "label": "路径 B", "description": "方向二"},
        ],
    }
    assert BrainstormCoordinator._normalize_question(question)["type"] == "multi_choice"
    question["type"] = "single_choice"
    assert BrainstormCoordinator._normalize_question(question)["type"] == "multi_choice"
    question["single_choice_reason"] = "本次交付只能选定一个唯一文件格式"
    assert BrainstormCoordinator._normalize_question(question)["type"] == "single_choice"


@pytest.mark.asyncio
async def test_selected_branch_focus_is_drilled_before_remaining_coverage():
    service = StubCreationService([json.dumps({
        "status": "question", "question": {
            "dimension_id": "selected_branch", "dimension": "已选思路下钻", "type": "multi_choice",
            "prompt": "如何落实已选的协同决策思路？", "why_now": "逐项展开而非忽略其他选择",
            "options": [
                {"id": "a", "label": "审核流程", "description": "明确审核责任", "recommended": True},
                {"id": "b", "label": "反馈机制", "description": "持续积累经验"},
            ],
        },
    }, ensure_ascii=False)])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计企业知识库方案", decisions=[], brief_markdown="已有多选决定",
        force_continue=True, focus_hint="逐项下钻：协同决策",
    )
    assert result["question"]["dimension_id"] == "selected_branch"
    assert '"next_question_goal": {}' in service.prompts[0]
    assert "逐项下钻：协同决策" in service.prompts[0]


def test_excluded_directions_survive_compact_decision_window():
    decisions = [{"dimension": "采购预算", "question": "是否讨论采购？", "answer_source": "user_excluded"}]
    decisions += [{"dimension": "其他", "answer": "已确认"}] * (BrainstormCoordinator.MAX_CONTEXT_DECISIONS + 1)
    prompt = BrainstormCoordinator._build_prompt(
        root_request="方案", decisions=decisions, brief_markdown="", selected_skills=[],
        required_coverage=[], covered_ids=set(), next_goal=None,
        force_continue=False, suggest_directions=False, focus_hint="",
    )
    assert '"excluded_directions": [{"dimension": "采购预算", "question": "是否讨论采购？"}]' in prompt
    assert "不要再次提问、展开、推荐" in prompt



def test_exclusions_do_not_become_memory_retrieval_queries():
    class Service:
        def __init__(self):
            self.queries = []
        def analyze_requirement(self, query, options, **kwargs):
            self.queries.append(query)
            return {}
        def retrieve_references(self, query, requirement, options):
            return []
    service = Service()
    BrainstormCoordinator(service)._retrieve_memory(
        "方案", [{"answer_source": "user_excluded", "answer": "不要展开采购预算"}],
        "# 简报\n- **排除约束（不得写入正文）：** 采购预算", "审核流程"
    )
    assert all("采购预算" not in query for query in service.queries)


@pytest.mark.asyncio
async def test_option_id_collisions_do_not_retry_the_model_or_steal_later_ids():
    value = concise_question_payload("下一步优先改善什么？")
    choices = value["question"]["options"]
    choices[0]["id"] = "same!"
    choices[1]["id"] = "same?"
    choices.append({"id": "option_2", "label": "新机制路线", "description": "先验证机制再推广。", "recommended": False})
    service = StubCreationService([json.dumps(value, ensure_ascii=False)])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown=""
    )
    assert len(service.model_calls) == 1
    options = result["question"]["options"]
    assert len({item["id"] for item in options}) == 3
    assert options[2]["id"] == "option_2"
    assert [(item["label"], item["recommended"]) for item in options] == [
        (item["label"], item["recommended"]) for item in choices
    ]


def test_duplicate_option_content_still_requires_regeneration():
    value = concise_question_payload("下一步优先改善什么？")
    value["question"]["options"][1]["label"] = value["question"]["options"][0]["label"]
    with pytest.raises(BrainstormGenerationError, match="内容重复"):
        BrainstormCoordinator._normalize_result(json.dumps(value, ensure_ascii=False), force_continue=False)


@pytest.mark.asyncio
async def test_change_direction_repairs_question_response_before_returning():
    ready = {
        "status": "ready", "continuation_directions": [
            {"id": "risk", "label": "风险边界", "description": "检查失败条件"},
            {"id": "delivery", "label": "交付路径", "description": "细化执行方案"},
        ],
    }
    service = StubCreationService([
        json.dumps(concise_question_payload("下一步优先改善什么？"), ensure_ascii=False),
        json.dumps(ready, ensure_ascii=False),
    ])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计产品方案", decisions=[], brief_markdown="",
        suggest_directions=True,
    )
    assert result["status"] == "ready"
    assert result["question"] is None
    assert len(service.prompts) == 2


@pytest.mark.parametrize("answer", ["", " \n\t"])
def test_manually_cleared_decision_is_pending_and_can_be_asked_again(answer):
    question = concise_question_payload("当前生成视频剧本的主要痛点是什么？")["question"]
    decision = {
        "dimension_id": "business_outcome", "question": question["prompt"],
        "answer": answer, "manually_edited": True, "answer_source": "user",
    }
    assert "business_outcome" not in BrainstormCoordinator._covered_dimension_ids([decision])
    assert not BrainstormCoordinator._repeats_answered_question(question, [decision])
    decision["answer_source"] = "user_excluded"
    assert "business_outcome" in BrainstormCoordinator._covered_dimension_ids([decision])


@pytest.mark.parametrize("required, expected", [("false", False), ("true", True), (False, False)])
def test_question_required_uses_boolean_value(required, expected):
    question = concise_question_payload("下一步优先改善什么？")["question"]
    question["required"] = required
    assert BrainstormCoordinator._normalize_question(question)["required"] is expected


@pytest.mark.asyncio
async def test_cleared_decision_overrides_root_coverage_and_cannot_be_restored_from_memory():
    question = concise_question_payload("新的业务目标应该是什么？")
    service = StubCreationService([json.dumps(question, ensure_ascii=False)])

    class InspectingCoordinator(BrainstormCoordinator):
        async def _assess_memory(self, **kwargs):
            assert "business_outcome" not in {item["id"] for item in kwargs["coverage"]}
            return []

    result = await InspectingCoordinator(service).next_step(
        root_request="设计产品方案，目标是缩短旧流程耗时。",
        decisions=[{"dimension_id": "business_outcome", "answer": "",
                    "question": question["question"]["prompt"], "manually_edited": True}],
        brief_markdown="# 创作简报\n\n业务目标：",
    )
    assert result["question"]["dimension_id"] == "business_outcome"
    assert '"id": "business_outcome", "label": "目标与期望结果", "status": "pending"' in service.prompts[0]
