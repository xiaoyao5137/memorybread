from __future__ import annotations

import io
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from creation.agent_loop import CreationAgentLoop
from creation.service import (
    CreationOptions,
    CreationService,
    GithubSearchResult,
    ReferenceDocument,
)
from creation.tools import (
    ROUTABLE_AGENT_IDS,
    ROUTABLE_TOOL_IDS,
    ROUTING_CAPABILITIES,
    fallback_routing_decision,
    validate_routing_decision,
)


class FakeCreationService:
    def __init__(self):
        self.reference_queries = []
        self.data_queries = []
        self.data_results = []
        self.data_search_kwargs = {}
        self.reference_options = []
        self.scrape_outcome = None
        self.scrape_kwargs = {}
        self.routing_decision = None

    def analyze_requirement(
        self,
        message,
        options,
        entity_focus_text="",
        retrieval_context_terms=None,
    ):
        return {
            "topic": message,
            "doc_type": options.doc_type or "架构设计方案",
            "audience": options.audience or "研发团队",
            "keywords": ["架构", "Agent"],
            "style": "专业清晰",
            "needs_latest": False,
            "needs_images": False,
        }

    def retrieve_references(self, user_prompt, *_args):
        self.reference_queries.append(user_prompt)
        if _args:
            self.reference_options.append(_args[-1])
        return []

    async def refresh_recalled_documents(
        self,
        references,
        query,
        require_latest=False,
        browser_extension_enabled=True,
    ):
        return {"attempted": 0, "updated": 0, "no_change": 0, "skipped": 0, "failed": 0}

    async def collect_web_context(self, *_args):
        return []

    async def search_github_context(self, *_args):
        return [
            GithubSearchResult(
                full_name="example/agent-kit",
                url="https://github.com/example/agent-kit",
                description="Agent orchestration toolkit",
                stars=128,
                language="Python",
                updated_at="2026-07-01T00:00:00Z",
            )
        ]

    async def retrieve_data_context(self, *args, **_kwargs):
        if args:
            self.data_queries.append(str(args[0]))
        self.data_search_kwargs = dict(_kwargs)
        return list(self.data_results)

    async def scrape_data_context(self, data_results, *_args, **_kwargs):
        self.scrape_kwargs = dict(_kwargs)
        if self.scrape_outcome is not None:
            return self.scrape_outcome
        return {"scrapes": [], "refreshed_data": list(data_results)}

    async def run_specialist_agent(self, **kwargs):
        return f"{kwargs['agent_id']} 的分析结论"

    async def stream_specialist_agent(self, **kwargs):
        result = await self.run_specialist_agent(**kwargs)
        midpoint = max(1, len(result) // 2)
        yield result[:midpoint]
        yield result[midpoint:]

    def build_routing_prompts(
        self, query, requirement, selected_skills=(), enabled_tool_ids=None
    ):
        return ("路由决策系统提示", f"请为请求选择执行链路：{query}")

    def parse_routing_decision(self, text):
        parsed = json.loads(text)
        decision = validate_routing_decision(parsed)
        decision["reasoning"] = str(parsed.get("reasoning") or "")[:200]
        return decision

    async def route_capabilities(self, **kwargs):
        if self.routing_decision is not None:
            return dict(self.routing_decision)
        return fallback_routing_decision(kwargs["query"], kwargs["requirement"])

    async def stream_agent_document(self, **_kwargs):
        yield "# Agent 架构方案\n\n"
        yield "## 目标\n\n围绕目标动态编排能力。\n\n"
        yield "## 架构\n\n主 Agent 调用子 Agent、Tool 与 Skill。\n\n"
        yield "## 实施\n\n按契约、运行时、页面和测试分步实施。"

    @staticmethod
    def _clip(value, limit):
        return value[:limit]

    @staticmethod
    def _best_reference_content(reference):
        return reference.full_content


async def collect_events(iterator):
    return [event async for event in iterator]


def resolve_planned(loop, state, decision=None):
    """同步解析路由决策并重建计划，供单测直接断言计划结构。"""
    if decision is None:
        decision = fallback_routing_decision(
            str(state.environment.get("context_query") or state.user_message),
            state.environment.get("requirement", {}),
        )
    state.environment["routing_decision"] = dict(decision)
    state.plan = loop._compose_plan_from_decision(state, decision)
    return state.plan


def test_routing_decision_validator_filters_unknown_ids_and_dedupes():
    decision = validate_routing_decision(
        {
            "tools": ["data_search", "unknown_tool", "data_search"],
            "agents": ["solution_design_agent", "ghost_agent"],
        }
    )
    assert decision["tools"] == ["data_search"]
    assert decision["agents"] == ["solution_design_agent"]


def test_creation_tool_result_limits_have_compatible_defaults_and_bounds():
    defaults = CreationOptions()
    assert defaults.max_references == 10
    assert defaults.data_search_limit == 30

    bounded = CreationOptions(max_references=99, data_search_limit=0)
    assert bounded.max_references == 30
    assert bounded.data_search_limit == 1


def test_multi_target_quality_gate_requires_each_target_and_facet():
    contract = {
        "targets": ["场景甲", "场景乙", "场景丙"],
        "facets": ["用了哪些模型", "占比多少", "成本情况如何"],
    }
    incomplete = """# 盘点

## 场景甲
模型 A，占比 60%，成本 1 元。

## 场景乙
模型 B，成本 2 元。
"""
    complete = incomplete + """

## 场景丙
模型与占比、成本均为现有证据未覆盖。

## 场景乙补充
模型占比为现有证据未覆盖。
"""

    failed = CreationAgentLoop._multi_target_coverage_result(incomplete, contract)
    passed = CreationAgentLoop._multi_target_coverage_result(complete, contract)

    assert failed["passed"] is False
    assert failed["gaps"] == [
        {
            "target": "场景乙",
            "missing_facets": ["占比多少"],
            "reason": "facet_missing",
        },
        {
            "target": "场景丙",
            "missing_facets": contract["facets"],
            "reason": "target_missing",
        },
    ]
    assert passed["passed"] is True


def test_brainstorm_brief_is_preserved_as_structured_harness_context():
    loop = CreationAgentLoop(FakeCreationService())
    brief = {
        "revision": 4,
        "brief_markdown": "# 创作简报\n\n## 目标与决策\n- **已确认：** 推动批准或立项",
        "open_flags": ["补充目标指标基线"],
        "decisions": [
            {
                "dimension_id": "business_outcome",
                "dimension": "目标与决策",
                "summary": "推动批准或立项",
                "source": "user",
            },
            {
                "dimension_id": "success_criteria",
                "dimension": "成功标准",
                "summary": "指标基线待补，先给出定标方法",
                "source": "agent_assumption",
            },
        ],
        # 非契约内部字段即使被带入，也不得泄露到模型上下文。
        "creation_model": "internal-provider-model",
    }
    state = loop._new_state(
        user_message="设计数据治理平台建设方案",
        root_request="设计数据治理平台建设方案",
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(),
        model_mode="local",
        session_id="session-brainstorm",
        run_id="run-brainstorm",
        creation_mode="brainstorm",
        creation_brief=brief,
    )

    assert state.creation_mode == "brainstorm"
    assert state.environment["creation_mode"] == "brainstorm"
    assert state.environment["creation_brief"] == brief
    context_query = state.environment["context_query"]
    assert "脑暴创作上下文" in context_query
    assert "已确认决策" in context_query
    assert "推动批准或立项" in context_query
    assert "合理假设" in context_query
    assert "指标基线待补，先给出定标方法" in context_query
    assert "开放事项" in context_query
    assert "补充目标指标基线" in context_query
    assert "# 创作简报" in context_query
    assert "internal-provider-model" not in context_query
    # 生成 Agent 保留完整脑暴上下文，召回解析只看原始主题和
    # 用户已确认事实，避免开放问题或控制文字劫持实体。
    assert state.environment["retrieval_query"] == "设计数据治理平台建设方案"
    assert state.environment["requirement"]["topic"] == state.environment[
        "retrieval_query"
    ]
    assert state.environment["retrieval_context_terms"] == ["推动批准或立项"]
    assert "指标基线待补" not in json.dumps(
        state.environment["requirement"], ensure_ascii=False
    )

    prompt_environment = loop._prompt_environment(state)
    assert "推动批准或立项" in prompt_environment
    assert "指标基线待补，先给出定标方法" in prompt_environment
    assert "补充目标指标基线" in prompt_environment
    assert "# 创作简报" in prompt_environment
    assert "internal-provider-model" not in prompt_environment
    restored = type(state).restore(state.serializable())
    assert restored.creation_mode == "brainstorm"
    assert restored.environment["creation_brief"]["revision"] == 4


@pytest.mark.asyncio
async def test_brainstorm_brief_enters_external_routing_model_prompt():
    brief = {
        "brief_markdown": "# 创作简报\n\n用于管理层立项决策。",
        "open_flags": ["预算上限待确认"],
        "decisions": [
            {
                "dimension": "一期范围",
                "summary": "先覆盖三个核心数据域",
                "source": "user",
            }
        ],
    }
    events = await collect_events(
        CreationAgentLoop(FakeCreationService()).run(
            user_message="设计数据治理平台建设方案",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enable_rag=False),
            model_mode="external",
            creation_mode="brainstorm",
            creation_brief=brief,
        )
    )

    request = next(event for event in events if event["type"] == "model.request")
    messages = "\n".join(item["content"] for item in request["data"]["messages"])
    assert "先覆盖三个核心数据域" in messages
    assert "预算上限待确认" in messages
    assert "用于管理层立项决策" in messages


@pytest.mark.asyncio
async def test_agent_passes_configured_result_limits_to_memory_and_data_search():
    service = FakeCreationService()
    service.routing_decision = {"tools": ["data_search"], "agents": []}

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="查看本周经营数据并形成简报",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(max_references=12, data_search_limit=37),
        )
    )

    assert service.reference_options[0].max_references == 12
    assert service.data_search_kwargs["limit"] == 37
    completed = {
        event["actor"]["id"]: event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] in {"memory_search", "data_search"}
    }
    assert completed["memory_search"]["data"]["result_limit"] == 12
    assert completed["data_search"]["data"]["result_limit"] == 37


def test_routing_prompt_loads_every_capability_self_description():
    # 路由倾向不在系统提示词里硬编码，而是加载每个能力自己声明的描述。
    class PromptAssemblyService:
        build_routing_prompts = CreationService.build_routing_prompts
        _skill_description_lines = staticmethod(
            CreationService._skill_description_lines
        )

    service = PromptAssemblyService()
    skills = [
        {
            "id": "market-entry-skill",
            "title": "市场进入方案 Skill",
            "skillDescription": {
                "purpose": "帮助决策者形成可验证的市场进入方案。",
                "problems": ["判断机会、约束与进入路径"],
            },
        }
    ]
    system, user = service.build_routing_prompts(
        "查看看板实时数据",
        {"topic": "看板数据", "doc_type": "实时查询", "audience": ""},
        selected_skills=skills,
    )

    for capability in ROUTING_CAPABILITIES:
        assert capability["id"] in system
        assert capability["description"] in system
    assert "市场进入方案 Skill (Skill 上下文)" in system
    assert "判断机会、约束与进入路径" in system
    assert "用户请求：查看看板实时数据" in user
    # 白名单与能力自描述注册表保持同源。
    assert set(ROUTABLE_TOOL_IDS) == {
        item["id"] for item in ROUTING_CAPABILITIES if item["kind"] == "tool"
    }
    assert set(ROUTABLE_AGENT_IDS) == {
        item["id"] for item in ROUTING_CAPABILITIES if item["kind"] == "agent"
    }


def test_routing_prompt_only_discloses_enabled_optional_tools():
    # 契约：可选 Tool 只有启用后才向路由模型披露；未启用的工具对模型
    # 不可见，也就不可能被选择。
    class PromptAssemblyService:
        build_routing_prompts = CreationService.build_routing_prompts
        _skill_description_lines = staticmethod(
            CreationService._skill_description_lines
        )

    service = PromptAssemblyService()
    requirement = {"topic": "流程图", "doc_type": "", "audience": ""}

    system_without, _ = service.build_routing_prompts(
        "画一张流程图",
        requirement,
        selected_skills=[],
        enabled_tool_ids=(
            "internet_search",
            "memory_search",
            "data_search",
            "webpage_scrape",
        ),
    )
    assert "mermaid_diagram" not in system_without
    assert "plantuml_diagram" not in system_without
    # Agent 自描述不受工具启用状态影响。
    assert "solution_design_agent" in system_without

    system_with, _ = service.build_routing_prompts(
        "画一张流程图",
        requirement,
        selected_skills=[],
        enabled_tool_ids=(
            "internet_search",
            "memory_search",
            "data_search",
            "webpage_scrape",
            "mermaid_diagram",
        ),
    )
    assert "mermaid_diagram" in system_with
    assert "plantuml_diagram" not in system_with


def test_fallback_routing_decision_respects_enabled_tools():
    # 降级探针与模型路径同契约：只产出已启用的工具。
    disabled = fallback_routing_decision(
        "画一张系统流程图",
        {},
        enabled_tool_ids=(
            "internet_search",
            "memory_search",
            "data_search",
            "webpage_scrape",
        ),
    )
    assert "mermaid_diagram" not in disabled["tools"]
    assert "plantuml_diagram" not in disabled["tools"]

    enabled = fallback_routing_decision(
        "画一张系统流程图",
        {},
        enabled_tool_ids=(
            "internet_search",
            "memory_search",
            "data_search",
            "webpage_scrape",
            "mermaid_diagram",
        ),
    )
    assert "mermaid_diagram" in enabled["tools"]
    assert "plantuml_diagram" not in enabled["tools"]


def test_mermaid_is_default_but_explicit_empty_tool_list_keeps_it_disabled():
    assert "mermaid_diagram" in CreationOptions().enabled_tools
    assert "mermaid_diagram" not in CreationOptions(enabled_tools=()).enabled_tools


def test_model_routing_decision_overrides_keyword_heuristics():
    # 模型推理出的链路优先于降级关键词启发式：纯写作请求也可被选中数据检索。
    service = FakeCreationService()
    service.routing_decision = {
        "tools": ["data_search"],
        "agents": ["solution_design_agent"],
        "source": "model",
        "reasoning": "用户需要看板数据",
    }
    loop = CreationAgentLoop(service)
    state = loop._new_state(
        user_message="帮我写一首关于春天的短诗",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-model-routing",
        run_id="run-model-routing",
    )
    decision = {
        "tools": ["data_search"],
        "agents": ["solution_design_agent"],
        "source": "model",
    }
    plan = resolve_planned(loop, state, decision)
    step_ids = [step["id"] for step in plan]
    assert "data_search" in step_ids
    assert "solution_design_agent" in step_ids
    assert state.environment["routing_decision"]["source"] == "model"


@pytest.mark.asyncio
async def test_routing_falls_back_when_model_output_is_unparseable():
    class BrokenRoutingService(FakeCreationService):
        def parse_routing_decision(self, text):
            raise ValueError("无法解析路由决策")

    loop = CreationAgentLoop(BrokenRoutingService())
    state = loop._new_state(
        user_message="检索最新行业政策并写调研方案",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-route-broken",
        run_id="run-route-broken",
    )
    step = next(item for item in state.plan if item.get("action") == "route")
    events = [
        event
        async for event in loop._complete_model_step(state, step, "不是合法 JSON")
    ]
    # 路由决策公告后由 thinking.completed 收尾，展示层据此关闭思考卡片。
    assert events[-1]["type"] == "thinking.completed"
    assert any(item["type"] == "agent.completed" for item in events)
    assert state.environment["routing_decision"]["source"] == "fallback"
    plan_ids = [step_item["id"] for step_item in state.plan]
    assert "internet_search" in plan_ids
    assert "memory_search" in plan_ids


@pytest.mark.asyncio
async def test_local_route_failure_degrades_to_fallback_and_run_completes():
    class RaisingRouteService(FakeCreationService):
        async def route_capabilities(self, **_kwargs):
            return fallback_routing_decision(
                str(_kwargs["query"]),
                _kwargs["requirement"],
            )

    events = [
        event
        async for event in CreationAgentLoop(RaisingRouteService()).run(
            user_message="输出一份项目复盘方案",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(),
        )
    ]
    assert events[-1]["type"] == "run.completed"


@pytest.mark.asyncio
async def test_thinking_events_wrap_intent_routing_and_planning():
    # 深度思考事件必须成对包裹意图理解、链路决策与 Harness 反馈规划，
    # 页面据此展示呼吸灯思考状态和可展开的推理摘要。
    service = FakeCreationService()
    service.routing_decision = {
        "tools": ["data_search"],
        "agents": ["solution_design_agent"],
        "source": "model",
        "reasoning": "用户需要看板数据",
    }
    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="查看本周经营数据并形成简报",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(),
        )
    )

    types = [event["type"] for event in events]

    # 意图理解：thinking 对包裹 intent.interpreted，completed 携带推理摘要。
    intent_index = types.index("intent.interpreted")
    intent_started = [
        event
        for event in events[:intent_index]
        if event["type"] == "thinking.started"
        and event["data"]["stage"] == "intent"
    ]
    intent_completed = [
        event
        for event in events[intent_index + 1:]
        if event["type"] == "thinking.completed"
        and event["data"]["stage"] == "intent"
    ]
    assert intent_started
    assert intent_completed
    assert intent_completed[0]["data"]["reasoning"]

    # 链路决策：routing 思考对成对出现，completed 携带模型决策理由。
    routing_started = [
        event
        for event in events
        if event["type"] == "thinking.started"
        and event["data"]["stage"] == "routing"
    ]
    routing_completed = [
        event
        for event in events
        if event["type"] == "thinking.completed"
        and event["data"]["stage"] == "routing"
    ]
    assert len(routing_started) == len(routing_completed) == 1
    assert routing_completed[0]["data"]["reasoning"] == "用户需要看板数据"

    # route / plan 是主 Agent 的内部控制阶段，不应伪装成独立 Agent 启动步骤。
    main_agent_starts = [
        event
        for event in events
        if event["type"] == "agent.started"
        and event["actor"]["id"] == "creation_main_agent"
    ]
    assert main_agent_starts == []
    assert not any(event["summary"] == "创作 Agent 开始执行" for event in events)
    # 真正执行内容的子 Agent 仍保留可观察的 started 生命周期。
    assert any(
        event["type"] == "agent.started"
        and event["actor"]["id"] != "creation_main_agent"
        for event in events
    )

    # 内容生成：generation 思考对包裹大模型内容调用，completed 说明写回动作。
    generation_started = [
        event
        for event in events
        if event["type"] == "thinking.started"
        and event["data"]["stage"] == "generation"
    ]
    generation_completed = [
        event
        for event in events
        if event["type"] == "thinking.completed"
        and event["data"]["stage"] == "generation"
    ]
    assert generation_started
    assert len(generation_started) == len(generation_completed)
    assert "写回创作文档" in generation_completed[0]["data"]["reasoning"]
    # generation started 必须早于文档内容产出事件。
    document_index = types.index("document.replaced")
    assert any(
        event["type"] == "thinking.started"
        and event["data"]["stage"] == "generation"
        for event in events[:document_index]
    )

    # 反馈规划：每个 harness.decision 前后紧邻 planning 思考对。
    decisions = [
        index for index, event in enumerate(events)
        if event["type"] == "harness.decision"
    ]
    assert decisions
    for index in decisions:
        assert events[index - 1]["type"] == "thinking.started"
        assert events[index - 1]["data"]["stage"] == "planning"
        assert events[index + 1]["type"] == "thinking.completed"
        assert events[index + 1]["data"]["stage"] == "planning"
        assert events[index + 1]["data"]["reasoning"]

    # 无 Skill 流程：规划阶段先宏观总结接下来的步骤，每个计划步骤再成为顶层阶段。
    outline_completed = [
        event
        for event in events
        if event["type"] == "thinking.completed"
        and event["data"]["stage"] == "planning"
        and "接下来依次执行" in event["data"]["reasoning"]
    ]
    assert outline_completed
    assert "生成文档内容" in outline_completed[0]["data"]["reasoning"]
    plan_phases = [
        event
        for event in events
        if event["type"] == "phase.started"
        and event["data"]["phase_kind"] == "plan_step"
    ]
    assert plan_phases
    assert any(
        event["data"]["phase_title"] == "检索本地记忆资料"
        for event in plan_phases
    )
    assert any(
        event["data"]["phase_title"] == "生成文档内容" for event in plan_phases
    )


@pytest.mark.asyncio
async def test_external_route_thinking_pair_survives_pause_and_resume():
    # 外部模式下 thinking.started 在暂停前发出，thinking.completed 在恢复后
    # 由路由决策应用阶段补齐，两端事件属于同一 run。
    loop = CreationAgentLoop(FakeCreationService())
    first = await collect_events(
        loop.run(
            user_message="输出一份架构方案",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enable_rag=False, doc_type="架构设计方案"),
            model_mode="external",
        )
    )
    first_types = [event["type"] for event in first]
    assert first_types.count("thinking.started") == 2
    started_index = first_types.index("model.request")
    assert "thinking.started" in first_types[:started_index]
    # 路由思考在暂停前只发出 started，completed 要等恢复后补齐。
    assert not [
        event
        for event in first
        if event["type"] == "thinking.completed"
        and event["data"]["stage"] == "routing"
    ]

    second = await collect_events(
        loop.run(
            user_message="",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(),
            resume_state=first[-1]["data"]["continuation"],
            model_result='{"tools": [], "agents": ["solution_design_agent"], '
            '"reasoning": "架构方案需要先设计方案"}',
        )
    )
    routing_completed = [
        event
        for event in second
        if event["type"] == "thinking.completed"
        and event["data"]["stage"] == "routing"
    ]
    assert len(routing_completed) == 1
    assert routing_completed[0]["data"]["reasoning"] == "架构方案需要先设计方案"
    assert routing_completed[0]["run_id"] == first[0]["run_id"]


@pytest.mark.asyncio
async def test_external_route_step_pauses_for_routing_decision_then_resumes():
    loop = CreationAgentLoop(FakeCreationService())
    first = await collect_events(
        loop.run(
            user_message="输出一份架构方案",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enable_rag=False, doc_type="架构设计方案"),
            model_mode="external",
        )
    )
    assert first[-2]["type"] == "model.request"
    assert first[-1]["type"] == "run.paused"
    assert first[-2]["actor"]["id"] == "creation_main_agent"
    assert any(
        "执行链路" in message["content"]
        for message in first[-2]["data"]["messages"]
    )
    first_state = first[-1]["data"]["continuation"]

    second = await collect_events(
        loop.run(
            user_message="",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(),
            resume_state=first_state,
            model_result='{"tools": [], "agents": ["solution_design_agent"], '
            '"reasoning": "架构方案需要先设计方案"}',
        )
    )
    assert second[-2]["type"] == "model.request"
    assert second[-1]["type"] == "run.paused"
    assert second[-2]["actor"]["id"] == "solution_design_agent"


def test_tool_plan_enforces_required_tools_and_invokes_internet_by_intent():
    loop = CreationAgentLoop(FakeCreationService())
    regular = loop._new_state(
        user_message="写一份项目复盘总结",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-tools-regular",
        run_id="run-tools-regular",
    )
    researched = loop._new_state(
        user_message="检索最新行业政策并写调研方案",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-tools-research",
        run_id="run-tools-research",
    )

    regular_plan = resolve_planned(loop, regular)
    researched_plan = resolve_planned(loop, researched)
    assert "memory_search" in [step["id"] for step in regular_plan]
    assert "internet_search" not in [step["id"] for step in regular_plan]
    assert {"memory_search", "internet_search"} <= {
        step["id"] for step in researched_plan
    }


def test_weekly_report_starts_with_peer_evidence_probes_not_a_fixed_data_pipeline():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="生成本周项目周报，并分析核心指标变化",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-data-report",
        run_id="run-data-report",
    )

    step_ids = [step["id"] for step in resolve_planned(loop, state)]
    assert "memory_search" in step_ids
    assert "data_search" in step_ids
    assert "webpage_scrape" not in step_ids
    assert "data_analysis_agent" not in step_ids
    assert step_ids.index("data_search") < step_ids.index("document_writer_agent")


def test_metric_governance_uses_report_reference_as_a_data_probe():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="创作一份治理GPU利用率的方案文档",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-gpu-governance",
        run_id="run-gpu-governance",
    )

    assert "data_search" in [step["id"] for step in resolve_planned(loop, state)]
    state.environment["references"] = [
        {
            "title": "LangBridge 模型中心运营看板 - 示例 BI | 可视化",
            "source_url": "https://bi.example.com/dashboard?id=2119187",
            "summary": "历史摘要",
            "content": "历史 GPU 利用率 45%",
        }
    ]
    query = loop._step_context_query(state, {"id": "data_search"})
    assert query.startswith("LangBridge 模型中心运营看板")
    assert "https://bi.example.com/dashboard?id=2119187" in query

    loop._apply_data_freshness_to_references(
        state,
        [
            {
                "source_url": "https://bi.example.com/dashboard?id=2119187",
                "freshness_class": "missing",
                "collected_at": None,
                "refresh_required": True,
                "can_use": False,
            }
        ],
    )
    reference = state.environment["references"][0]
    assert reference["content"] == ""
    assert reference["data_use_policy"] == "current_values_unavailable"
    assert "不得写成当前数据" in reference["summary"]


def test_dashboard_lookup_intent_triggers_data_search_and_browser_refresh_chain():
    # 回归：查看看板实时数值的请求必须先进入 data_search，再由反馈进入
    # 浏览器抓取，而不是跳过检索直接生成文档。
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="基于LangBridge模型中心运营看板查看今天的电商token用量",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=(), doc_type="实时查询"),
        model_mode="local",
        session_id="session-dashboard-lookup",
        run_id="run-dashboard-lookup",
    )

    step_ids = [step["id"] for step in resolve_planned(loop, state)]
    assert "data_search" in step_ids
    assert step_ids.index("data_search") < step_ids.index("document_writer_agent")

    state.cursor = step_ids.index("data_search") + 1
    state.environment["data_results"] = [
        {
            "source_id": 9,
            "source_kind": "report_url",
            "source_url": "https://bi.example.com/dashboard?id=2119187",
            "refresh_required": True,
            "can_use": False,
            "content_excerpt": "历史 token 用量",
        }
    ]
    decision = loop._replan_after_feedback(
        state,
        {"id": "data_search"},
        status="completed",
    )
    assert decision["reason_code"] == "refresh_required"
    assert decision["scheduled"] == ["webpage_scrape"]
    assert state.plan[state.cursor]["id"] == "webpage_scrape"


def test_plain_writing_without_metric_object_does_not_trigger_data_search():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="帮我写一首关于春天的短诗",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-plain-writing",
        run_id="run-plain-writing",
    )
    assert "data_search" not in [step["id"] for step in resolve_planned(loop, state)]


def test_evidence_validation_requires_matching_metadata_value_and_label():
    payload = {
        "title": "GPU 实时看板",
        "url": "https://bi.example.com/dashboard/gpu",
        "collected_at": 1770000000000,
        "structured_data": {
            "tables": [["区域", "GPU 利用率"], ["国内", "42%"], ["海外", "47%"]]
        },
        "evidence": {
            "page_title": "GPU 实时看板",
            "source_url": "https://bi.example.com/dashboard/gpu",
            "captured_at": 1770000000000,
        },
    }

    matched = CreationService._compare_scrape_with_ocr(
        payload,
        "GPU 实时看板\n区域 GPU 利用率\n国内 42%\n海外 47%",
    )
    mismatched = CreationService._compare_scrape_with_ocr(
        payload,
        "GPU 实时看板\n国内 65%",
    )

    assert {claim["value"] for claim in matched["verified_claims"]} == {"42%", "47%"}
    assert mismatched["verified_claims"] == []


def test_requested_dashboard_metrics_allow_partial_success_and_filter_wrong_period():
    query = (
        "获取LangBridge模型中心运营看板里第二个tab下筛选本周的独立部署输入Tokens、"
        "独立部署输出Tokens、公共部署输入Tokens、公共部署输出tokens、"
        "商业模型输入Tokens、商业模型输出Tokens"
    )
    required = CreationService._extract_required_metrics(query)
    claims = [
        {
            "claim_type": "metric",
            "label": metric,
            "value": str(index + 1),
            "statement": f"{metric} {index + 1}",
            "statistical_period": "2026-08-10 至 2026-08-16",
        }
        for index, metric in enumerate(required)
    ]

    complete = CreationService._apply_required_metric_coverage(
        {"verified_claims": claims},
        required,
        {"start": "2026-08-10", "end": "2026-08-16"},
        fallback_policy="strict",
    )
    incomplete = CreationService._apply_required_metric_coverage(
        {"verified_claims": claims[:-1]},
        required,
        {"start": "2026-08-10", "end": "2026-08-16"},
        fallback_policy="strict",
    )
    wrong_period_claims = [
        {**claim, "statistical_period": "2026-08-03 至 2026-08-09"}
        for claim in claims
    ]
    wrong_period = CreationService._apply_required_metric_coverage(
        {"verified_claims": wrong_period_claims},
        required,
        {"start": "2026-08-10", "end": "2026-08-16"},
        fallback_policy="strict",
    )

    assert required == [
        "独立部署输入Token",
        "独立部署输出Token",
        "公共部署输入Token",
        "公共部署输出Token",
        "商业模型输入Token",
        "商业模型输出Token",
    ]
    assert complete["requirements_satisfied"] is True
    assert complete["required_metric_coverage"] == 1.0
    assert incomplete["requirements_satisfied"] is True
    assert incomplete["reason"] == "requested_metrics_partial"
    assert len(incomplete["verified_claims"]) == 5
    assert wrong_period["requirements_satisfied"] is False
    assert wrong_period["reason"] == "requested_metrics_period_mismatch"
    assert wrong_period["verified_claims"] == []

    # 截图 OCR 未补充有效指标时，再次覆盖校验仍应报告周期不符。
    rechecked = CreationService._apply_required_metric_coverage(
        wrong_period,
        required,
        {"start": "2026-08-10", "end": "2026-08-16"},
        fallback_policy="strict",
    )
    assert rechecked["reason"] == "requested_metrics_period_mismatch"
    assert rechecked["period_mismatch_metrics"] == required
    assert rechecked["missing_requested_metrics"] == []
    assert rechecked["requirements_satisfied"] is False
    assert rechecked["verified_claims"] == []

    corrected = CreationService._apply_required_metric_coverage(
        {**wrong_period, "verified_claims": claims[:1]},
        required,
        {"start": "2026-08-10", "end": "2026-08-16"},
        fallback_policy="strict",
    )
    assert corrected["reason"] == "requested_metrics_partial"
    assert corrected["period_mismatch_metrics"] == required[1:]
    assert corrected["verified_claims"][0]["value"] == claims[0]["value"]


def qualified_metric_fixture():
    expected = {"start": "2026-08-17", "end": "2026-08-23"}
    claims = [
        {"claim_type": "metric", "label": label, "value": value,
         "statement": f"{label} 2026-08-26 至 2026-08-26 {value}",
         "statistical_period": "2026-08-26 至 2026-08-26"}
        for label, value in (("专有环境输入Token", "100亿"), ("共享环境输出Token", "0"))
    ]
    validation = CreationService._apply_required_metric_coverage(
        {"verified_claims": claims}, [claim["label"] for claim in claims], expected,
    )
    return expected, claims, validation


def test_period_fallback_retains_original_values_and_requires_risk_disclosure():
    expected, claims, validation = qualified_metric_fixture()
    assert CreationService._validation_is_verified(validation)
    assert validation["data_usage_status"] == "qualified"
    assert validation["exact_metric_coverage"] == 0
    assert validation["required_metric_coverage"] == 1
    assert validation["missing_requested_metrics"] == []
    assert [claim["value"] for claim in validation["verified_claims"]] == ["100亿", "0"]
    assert all(risk["kind"] == "period_mismatch" and risk["expected_period"] == expected for risk in validation["data_risks"])
    rechecked = CreationService._validation_with_ocr_output(
        validation, {}, SimpleNamespace(text="", confidence=0),
        [claim["label"] for claim in claims], expected, strategy="full_image",
    )
    assert rechecked["data_risks"] == validation["data_risks"]
    assert rechecked["reason"] == "requested_metrics_qualified"


def test_exact_period_candidate_wins_over_earlier_fallback_and_nearest_fallback_wins():
    expected, claims, _ = qualified_metric_fixture()
    exact = {**claims[0], "value": "80亿", "statistical_period": "2026-08-17 至 2026-08-23"}
    distant = {**claims[1], "value": "90", "statistical_period": "2025-01-01"}
    validation = CreationService._apply_required_metric_coverage(
        {"verified_claims": [*claims, distant, exact]}, [claim["label"] for claim in claims], expected,
    )
    assert [claim["value"] for claim in validation["verified_claims"]] == ["80亿", "0"]
    assert validation["exact_matched_requested_metrics"] == [claims[0]["label"]]
    assert [risk["label"] for risk in validation["data_risks"]] == [claims[1]["label"]]


def test_unknown_period_is_disclosed_and_strict_policy_can_disable_fallback(monkeypatch):
    expected, claims, _ = qualified_metric_fixture()
    claims[0]["statistical_period"] = ""
    validation = CreationService._apply_required_metric_coverage(
        {"verified_claims": claims}, [claim["label"] for claim in claims], expected,
    )
    assert validation["data_risks"][0]["kind"] == "period_unverified"
    monkeypatch.setenv("MEMORYBREAD_CREATION_DATA_FALLBACK_POLICY", "strict")
    strict = CreationService._apply_required_metric_coverage(
        {"verified_claims": claims}, [claim["label"] for claim in claims], expected,
    )
    assert not CreationService._validation_is_verified(strict)
    assert strict["verified_claims"] == []


def test_requested_card_after_large_detail_table_keeps_validation_budget():
    requested = "共享环境输出Token"
    payload = {
        "content_text": "\n".join([*(f"项目{i}成本\n2026-08-26\n{i + 100}" for i in range(180)), f"{requested}\n2026-08-26\n19亿"]),
        "structured_data": {"requested_metrics": [requested]},
    }
    validation = CreationService._apply_required_metric_coverage(
        CreationService._compare_scrape_programmatic_channels(payload), [requested],
        {"start": "2026-08-17", "end": "2026-08-23"},
    )
    assert validation["matched_requested_metrics"] == [requested]
    assert validation["verified_claims"][0]["value"] == "19亿"
    assert validation["data_usage_status"] == "qualified"


def test_fallback_does_not_bypass_unverified_view_or_invent_absent_values():
    _, _, validation = qualified_metric_fixture()
    rejected = CreationService._enforce_interaction_validation(
        validation, {"structured_data": {"interaction_result": {"view_status": "unverified"}}},
    )
    assert not CreationService._validation_is_verified(rejected)
    empty = CreationService._apply_required_metric_coverage(
        {"verified_claims": []}, ["输入Token"], {},
    )
    assert empty["data_usage_status"] == "unavailable"
    assert empty["data_risks"] == []


def test_qualified_data_survives_merge_prompt_and_final_risk_guard():
    _, _, validation = qualified_metric_fixture()
    evidence = {"validation_status": "verified", "validation": validation}
    payload = {"content_text": "原始页面", "structured_data": {}, "url": "https://bi.example/report", "title": "容量看板", "collected_at": 123}
    results = CreationService._merge_scrape_results(
        [{"source_id": 21, "source_kind": "report_url"}], {21: payload}, {21: evidence}, {21},
    )
    results[0]["target_section"] = "用量"
    CreationAgentLoop._enforce_report_evidence_policy(results)
    compact = CreationAgentLoop._prompt_data_results(results)
    assert compact[0]["can_use"] is True
    assert compact[0]["risk_disclosure_required"] is True
    assert len(compact[0]["structured_data"]["verified_claims"]) == 2
    assert "period_mismatch" in str(compact)
    reference = {"source_url": payload["url"], "content": "旧文档的当前数值 999"}
    CreationAgentLoop._apply_data_freshness_to_references(
        SimpleNamespace(environment={"references": [reference]}), results,
    )
    assert reference["data_use_policy"] == "qualified_snapshot_available"
    assert reference["content"] == ""
    # 模型即使省略了所有数值/备注，确定性输出也必须保留可供用户核验的值。
    original = "## 用量\n\n说明。\n\n## 后续\n\n行动。"
    rendered, audit = CreationAgentLoop._apply_data_risk_disclosures(original, results)
    assert "100亿" in rendered and "| 0 |" in rendered
    assert "2026-08-26" in rendered and "2026-08-17" in rendered
    assert "https://bi.example/report" in rendered
    assert rendered.index("数据风险说明") < rendered.index("## 后续")
    assert audit[0]["risk_count"] == 2
    again, _ = CreationAgentLoop._apply_data_risk_disclosures(rendered, results)
    assert again == rendered
    preserved, _ = CreationAgentLoop._apply_data_risk_disclosures(rendered, [])
    assert preserved == rendered
    results[0]["data_risks"] = []
    results[0]["creation_evidence"]["validation"] = {"data_risks": []}
    resolved, audit = CreationAgentLoop._apply_data_risk_disclosures(rendered, results)
    assert "数据风险说明" not in resolved
    assert audit[0]["status"] == "resolved"


def test_requested_metrics_strip_task_prefix_and_ignore_interaction_clause():
    query = (
        "获取某模型运营看板的Token数据：专有环境输入Tokens、"
        "共享环境输出Tokens；选择第二个tab用量统计，"
        "统计日期2026-08-10至2026-08-16，点击查询"
    )

    requested = CreationService._extract_requested_metrics(query)

    assert requested == ["专有环境输入Token", "共享环境输出Token"]


@pytest.mark.asyncio
async def test_full_creation_loop_discloses_qualified_values_even_when_writer_omits_them():
    _, _, validation = qualified_metric_fixture()
    evidence = {"validation_status": "verified", "validation": validation}
    payload = {"content_text": "来源事实", "structured_data": {}, "title": "容量看板", "url": "https://bi.example/report"}
    source = {"source_id": 21, "source_kind": "report_url", "title": "容量看板", "source_url": payload["url"], "refresh_required": True, "can_use": False}
    refreshed = CreationService._merge_scrape_results([source], {21: payload}, {21: evidence}, {21})
    service = FakeCreationService()
    service.routing_decision = {"tools": ["data_search"], "agents": []}
    service.data_results = [source]
    service.scrape_outcome = {"scrapes": [{"source_id": 21, "status": "completed", "evidence": evidence}], "refreshed_data": refreshed}
    events = await collect_events(CreationAgentLoop(service).run(
        user_message="生成本周容量看板用量章节", current_document="", conversation=[],
        selected_skills=[{"id": "usage-risk", "title": "用量", "executionSteps": [
            {"id": "usage", "title": "用量", "objective": "获取容量看板的专有环境输入Token、共享环境输出Token并写表格", "output": "指标表", "tools": ["data_search"], "agents": [], "skills": []},
        ]}], options=CreationOptions(doc_type="周报"),
    ))
    assert events[-1]["type"] == "run.completed"
    assert any(event["type"] == "document.data_risks.applied" for event in events)
    document = events[-1]["data"]["document"]
    assert "100亿" in document and "| 0 |" in document
    assert "数据风险说明" in document and "2026-08-26" in document


def test_generic_page_interaction_plan_normalizes_tab_period_and_collection():
    query = (
        "获取模型运营看板里第二个tab下的独立部署输入Tokens、"
        "公共部署输出Tokens"
    )
    requested = CreationService._extract_requested_metrics(query)

    plan = CreationService._build_page_interaction_plan(
        query,
        requested,
        {"start": "2026-08-17", "end": "2026-08-23"},
    )

    assert plan["schema_version"] == "memorybread.page-interaction-plan.v1"
    assert plan["safety_mode"] == "read_only"
    assert [step["action"] for step in plan["steps"]] == [
        "activate",
        "set_date_range",
        "scroll_collect",
        "collect",
    ]
    assert plan["steps"][0]["target"] == {
        "role_hints": ["tab", "navigation_item"],
        "labels": [],
        "ordinal": 2,
    }
    assert plan["steps"][0]["postconditions"] == [
        {"kind": "data_stable", "minimum_stable_passes": 2}
    ]
    assert plan["steps"][1]["value"] == {
        "start": "2026-08-17",
        "end": "2026-08-23",
    }


def test_generic_page_interaction_plan_supports_named_controls_without_site_rules():
    plan = CreationService._build_page_interaction_plan(
        "进入成本导航，在区域下拉框选择华东，展开高级筛选面板，点击查询按钮",
        ["调用量"],
        {},
    )

    actions = [step["action"] for step in plan["steps"]]
    assert actions == [
        "activate",
        "expand",
        "select_option",
        "activate",
        "scroll_collect",
        "collect",
    ]
    assert plan["steps"][0]["target"] == {
        "role_hints": ["navigation_item"],
        "labels": ["成本"],
    }
    assert plan["steps"][2]["target"] == {
        "role_hints": ["combobox"],
        "labels": ["区域"],
    }
    assert plan["steps"][2]["value"] == "华东"


def test_unverified_interaction_view_isolates_incidental_page_claims():
    validation = {
        "verified_claims": [
            {
                "claim_type": "metric",
                "label": "当前页汇总成本",
                "value": "100",
            }
        ],
        "requirements_satisfied": True,
    }
    payload = {
        "structured_data": {
            "interaction_result": {
                "status": "failed",
                "view_status": "unverified",
                "steps": [
                    {
                        "status": "failed",
                        "error_code": "INTERACTION_TARGET_NOT_FOUND",
                    }
                ],
            }
        }
    }

    guarded = CreationService._enforce_interaction_validation(validation, payload)

    assert guarded["verified_claims"] == []
    assert guarded["incidental_claims"][0]["value"] == "100"
    assert guarded["requirements_satisfied"] is False
    assert guarded["reason"] == "interaction_view_unverified"


def test_requested_metrics_ignore_root_skill_invocation_before_step_objective():
    query = (
        "请使用@GPU成本优化周报创作法 创作下本周的周报\n"
        "当前 Skill 步骤目标：用@数据检索 Tool 获取某看板里第二个tab下"
        "的独立部署输入Tokens、独立部署输出Tokens、公共部署输入Tokens、"
        "公共部署输出Tokens、商业模型输入Tokens、商业模型输出Tokens"
    )

    requested = CreationService._extract_requested_metrics(query)

    assert requested == [
        "独立部署输入Token",
        "独立部署输出Token",
        "公共部署输入Token",
        "公共部署输出Token",
        "商业模型输入Token",
        "商业模型输出Token",
    ]


def test_requested_metrics_ignore_document_and_step_titles_for_gpu_report():
    query = (
        "请生成下本周GPU成本优化的周报\n"
        "当前步骤：GPU算力数据\n"
        "用@数据检索 Tool 获取电商GPU信息平台的最新算力、利用率、收益数据，"
        "以及业务项目投入成本最高的10个项目的数据，添加到表格中"
    )

    requested = CreationService._extract_requested_metrics(query)

    assert requested == ["算力", "利用率", "收益", "业务项目投入成本"]


def test_unmatched_metric_preferences_retain_cross_checked_page_values():
    available = CreationService._apply_required_metric_coverage(
        {
            "verified_claims": [
                {
                    "claim_type": "metric",
                    "label": "在用项目数",
                    "value": "102",
                    "statement": "在用项目数 102",
                }
            ]
        },
        ["本周资源治理概览"],
        {"start": "2026-08-10", "end": "2026-08-16"},
    )

    assert available["requirements_satisfied"] is True
    assert available["requested_metric_policy"] == "preference"
    assert available["available_values_retained"] is True
    assert available["reason"] == "requested_metrics_qualified"
    assert available["data_risks"][0]["kind"] == "semantic_match_unverified"
    assert [claim["value"] for claim in available["verified_claims"]] == ["102"]
    assert available["unrequested_verified_claim_count"] == 1


def test_structured_table_headers_are_bound_to_values_and_periods():
    payload = {
        "content_text": "",
        "structured_data": {
            "tables": [
                [
                    ["日期", "维度", "容量", "峰值利用率", "日均利用率"],
                    ["2026-08-11", "总体", "2116", "61.67%", "29.39%"],
                ]
            ]
        },
    }

    claims = CreationService._scrape_claim_candidates(payload)

    assert {
        (claim["label"], claim["value"], claim["statistical_period"])
        for claim in claims
    } == {
        ("总体 容量", "2116", "2026-08-11"),
        ("总体 峰值利用率", "61.67%", "2026-08-11"),
        ("总体 日均利用率", "29.39%", "2026-08-11"),
    }


def test_metric_card_date_range_is_bound_to_its_own_value():
    payload = {
        "content_text": (
            "日期\n2026-08-04\n至\n2026-08-10\n"
            "独立部署输入Tokens\n2026-08-12至2026-08-12\n28,833.07亿"
        ),
        "structured_data": {},
    }

    claims = CreationService._scrape_claim_candidates(payload)
    claim = next(item for item in claims if item.get("value") == "28,833.07亿")

    assert claim["label"] == "独立部署输入Tokens"
    assert claim["statistical_period"] == "2026-08-12至2026-08-12"


def test_virtualized_metric_cards_are_parsed_from_structured_regions():
    payload = {
        "content_text": "LangBridge模型中心运营看板\n用量统计",
        "structured_data": {
            "evidence_regions": [
                {
                    "text": (
                        "独立部署输入Tokens 2026-08-15至2026-08-15 "
                        "19,762.89亿 亿 input_tokens(求和)"
                    )
                },
                {
                    "text": (
                        "公共部署输出tokens 2026-08-15至2026-08-15 "
                        "65.39亿 亿"
                    )
                },
            ]
        },
    }

    claims = CreationService._scrape_claim_candidates(payload)

    assert {
        (
            claim.get("label"),
            claim.get("value"),
            claim.get("statistical_period"),
        )
        for claim in claims
        if claim.get("claim_type") == "metric"
    } == {
        ("独立部署输入Tokens", "19,762.89亿", "2026-08-15至2026-08-15"),
        ("公共部署输出tokens", "65.39亿", "2026-08-15至2026-08-15"),
    }


def test_structured_metric_cards_precede_table_details_for_prompt_budget():
    payload = {
        "content_text": "",
        "structured_data": {
            "tables": [
                [
                    ["项目", "日期", "卡数"],
                    *[
                        [f"项目{index}", "2026-08-14", str(index)]
                        for index in range(1, 31)
                    ],
                ]
            ],
            "evidence_regions": [
                {"text": "总卡数（X40折算） 2026-08-14 1803.59"},
                {"text": "年化总成本 2026-08-14 12178.4万元"},
            ],
        },
    }

    claims = CreationService._scrape_claim_candidates(payload)

    assert [claim["label"] for claim in claims[:2]] == [
        "总卡数（X40折算）",
        "年化总成本",
    ]
    assert all(
        claim.get("evidence_origin") == "structured_region_metric"
        for claim in claims[:2]
    )


def test_adjacent_summary_cards_without_dates_precede_table_details():
    payload = {
        "content_text": (
            "项目 GPU 用量管理\n"
            "在用项目数\n102\n"
            "总卡数（X40折算）\n1803.59\n"
            "年化总成本（万元）\n12178.4万元\n"
            "平均 ROI\n39.86x"
        ),
        "structured_data": {
            "tables": [
                [
                    ["项目", "日期", "卡数"],
                    *[
                        [f"项目{index}", "2026-08-14", str(index)]
                        for index in range(1, 31)
                    ],
                ]
            ],
        },
    }

    claims = CreationService._scrape_claim_candidates(payload)

    assert [(claim["label"], claim["value"]) for claim in claims[:4]] == [
        ("在用项目数", "102"),
        ("总卡数（X40折算）", "1803.59"),
        ("年化总成本（万元）", "12178.4万元"),
        ("平均 ROI", "39.86x"),
    ]
    assert all(
        claim.get("evidence_origin") == "content_adjacent_metric"
        for claim in claims[:4]
    )


def test_adjacent_metric_parser_ignores_pagination_symbols():
    claims = CreationService._scrape_claim_candidates(
        {
            "content_text": "‹\n1\n…\n6\n总卡数\n1803.59",
            "structured_data": {},
        }
    )

    assert [(claim["label"], claim["value"]) for claim in claims] == [
        ("总卡数", "1803.59")
    ]


def test_prompt_data_results_prioritize_each_verified_report_with_bounded_claims():
    work_memories = [
        {
            "source_id": index,
            "source_kind": "work_memory",
            "can_use": True,
            "content_excerpt": "普通工作数据 " + ("说明" * 300),
        }
        for index in range(30)
    ]

    def verified_report(source_id, title, claim_count, validation):
        return {
            "source_id": source_id,
            "title": title,
            "source_kind": "report_url",
            "can_use": True,
            "creation_evidence": {"validation_status": "verified"},
            "structured_data": {
                "validation": validation,
                "verified_claims": [
                    {
                        "claim_type": "metric",
                        "label": f"指标{index}",
                        "value": str(index),
                        "statement": f"指标{index} {index}",
                    }
                    for index in range(claim_count)
                ],
            },
        }

    results = CreationAgentLoop._prompt_data_results(
        [
            *work_memories,
            verified_report(1584, "GPU报表", 120, "requested_metrics_unmatched"),
            verified_report(214, "Token报表", 6, "requested_metrics_verified"),
        ]
    )

    assert [item["source_id"] for item in results[:2]] == [1584, 214]
    assert len(results[0]["structured_data"]["verified_claims"]) == 4
    assert len(results[1]["structured_data"]["verified_claims"]) == 6


def test_verified_report_policy_preserves_generic_relations_and_coverage():
    results = [{
        "source_id": 41,
        "source_kind": "report_url",
        "can_use": True,
        "content_excerpt": "完整页面正文",
        "structured_data": {
            "tables": [
                [["对象", "度量"], ["甲", "10"], ["乙", "20"]],
            ],
            "pagination": {
                "dataset_complete": True,
                "captured_rows": 2,
                "total_rows": 2,
            },
            "completeness": {"status": "complete"},
        },
        "creation_evidence": {
            "validation_status": "verified",
            "validation": {
                "reason": "requested_metrics_partial",
                "primary_channel": "dom",
                "verified_claims": [{
                    "label": "度量",
                    "value": "20",
                    "statement": "乙 20",
                }],
            },
        },
    }]

    CreationAgentLoop._enforce_report_evidence_policy(results)

    structured = results[0]["structured_data"]
    assert structured["tables"][0][2] == ["乙", "20"]
    assert structured["pagination"]["dataset_complete"] is True
    assert structured["verified_claims"][0]["statement"] == "乙 20"


def test_query_quality_gate_requires_every_verified_result_row_in_document():
    result = {
        "shape": "table",
        "validation": {"status": "verified"},
        "provenance": {"relation_id": "source_41.table_0"},
        "rows": [
            {
                "row_id": "row-1",
                "cells": {
                    "name": {"raw": "甲项目", "normalized": "甲项目"},
                    "value": {"raw": "20.0万元", "normalized": "20.0"},
                },
            },
            {
                "row_id": "row-2",
                "cells": {
                    "name": {"raw": "乙项目", "normalized": "乙项目"},
                    "value": {"raw": "10.0万元", "normalized": "10.0"},
                },
            },
        ],
    }
    incomplete = "| 对象 | 度量 |\n| --- | --- |\n| 甲项目 | 20.0万元 |"
    complete = incomplete + "\n| 乙项目 | 10.0万元 |"

    gaps = CreationAgentLoop._data_query_result_gaps(incomplete, [result])
    assert gaps[0]["expected_rows"] == 2
    assert gaps[0]["missing_rows"] == 1
    assert CreationAgentLoop._data_query_result_gaps(complete, [result]) == []


def test_query_quality_gate_ignores_incomplete_coverage_results():
    result = {
        "shape": "table",
        "validation": {"status": "insufficient_coverage"},
        "rows": [{"row_id": "row-1", "cells": {"value": {"raw": "10"}}}],
    }

    assert CreationAgentLoop._data_query_result_gaps("", [result]) == []


def test_placeholder_guard_recognizes_bracketed_metric_placeholders():
    document = (
        "| 指标 | 数值 |\n"
        "| --- | --- |\n"
        "| 独立部署输入Tokens | [待补充具体数值] |"
    )

    assert CreationAgentLoop._placeholder_count(document) == 1
    cleaned, audit = CreationAgentLoop._guard_generated_placeholders(document, {})
    assert "待补充" not in cleaned
    assert audit == [{"kind": "unsupported_placeholder_removed", "count": 1}]


def test_langbridge_preview_source_wins_over_edit_source_for_same_dashboard():
    selected = CreationService._select_canonical_report_sources(
        [
            {
                "source_id": 260,
                "source_url": (
                    "https://bi.example.com/pc/dashboard/edit?"
                    "dashboardId=2119187&sheetId=285011"
                ),
            },
            {
                "source_id": 214,
                "source_url": (
                    "https://bi.example.com/pc/dashboard/preview?"
                    "dashboardId=2119187&sheetId=285011&tabIds=904433"
                ),
            },
        ]
    )

    assert [item["source_id"] for item in selected] == [214]


def test_refresh_selection_prefers_strong_source_identity_without_business_keywords():
    selected = CreationService._select_refreshable_report_sources(
        [
            {
                "source_id": 1,
                "source_kind": "report_url",
                "source_url": "https://metrics.example.com/product-a",
                "refresh_required": True,
                "refresh_policy": "on_demand",
                "identity_relevance_score": 0.72,
                "relevance_score": 0.72,
            },
            {
                "source_id": 2,
                "source_kind": "report_url",
                "source_url": "https://metrics.example.com/product-b",
                "refresh_required": True,
                "refresh_policy": "on_demand",
                "identity_relevance_score": 0.0,
                "relevance_score": 0.46,
            },
        ]
    )

    assert [item["source_id"] for item in selected] == [1]


def test_refresh_selection_prefers_longest_explicit_report_title_from_query():
    selected = CreationService._select_refreshable_report_sources(
        [
            {
                "source_id": 214,
                "title": "LangBridge模型中心运营看板 - KwaiBI | 可视化",
                "source_kind": "report_url",
                "source_url": "https://bi.example.com/dashboard/preview?id=214",
                "refresh_required": True,
                "refresh_policy": "on_demand",
                "identity_relevance_score": 0.84,
                "relevance_score": 0.84,
            },
            {
                "source_id": 15190,
                "title": "LangBridge",
                "source_kind": "report_url",
                "source_url": "https://models.example.com/model-market",
                "refresh_required": True,
                "refresh_policy": "on_demand",
                "identity_relevance_score": 0.84,
                "relevance_score": 0.84,
            },
        ],
        "获取LangBridge模型中心运营看板的本周Token数据",
    )

    assert [item["source_id"] for item in selected] == [214]


@pytest.mark.asyncio
async def test_screenshot_off_retries_silent_block_with_one_foreground_refresh(
    monkeypatch,
):
    requests = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.is_success = 200 <= status_code < 300

        def json(self):
            return self._payload

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, json):
            requests.append(dict(json))
            if len(requests) == 1:
                return FakeResponse(
                    412,
                    {
                        "error": "FOCUS_POLICY_BLOCKED",
                        "message": "静默取数没有已打开页面",
                    },
                )
            return FakeResponse(
                200,
                {
                    "collector": "browser_attach",
                    "browser": "chrome",
                    "interaction_mode": "temporary_foreground_window",
                    "focus_policy": "allow_once",
                    "focus_takeover_count": 1,
                    "collected_at": 1_770_000_000_000,
                    "title": "经营看板",
                    "url": "https://bi.example.com/report",
                    "content_text": "年化总成本\n1200万元",
                    "structured_data": {
                        "tables": [
                            [["指标", "数值"], ["年化总成本", "1200万元"]]
                        ],
                        "dom_content_text": "年化总成本\n1200万元",
                        "extraction": {"primary": "dom"},
                    },
                    "evidence": None,
                },
            )

    fake_client = FakeAsyncClient()
    monkeypatch.setattr(
        "creation.service.httpx.AsyncClient",
        lambda **_kwargs: fake_client,
    )
    service = CreationService(model="test", enable_vector_recall=False)

    outcome = await service.scrape_data_context(
        [
            {
                "source_id": 7,
                "source_kind": "report_url",
                "source_url": "https://bi.example.com/report",
                "title": "经营看板",
                "refresh_required": True,
                "refresh_policy": "on_demand",
                "can_use": False,
            }
        ],
        "获取最新年化总成本",
        {},
        run_id="run-1",
        session_id="session-1",
        retain_screenshot=False,
        browser_extension_enabled=False,
    )

    assert len(requests) == 2
    assert requests[0]["extension_preference"] == "disabled"
    assert requests[1]["extension_preference"] == "disabled"
    assert requests[0]["allow_foreground_refresh"] is False
    assert requests[0]["focus_policy"] == "never"
    assert requests[1]["allow_foreground_refresh"] is True
    assert requests[1]["focus_policy"] == "allow_once"
    assert requests[1]["capture_evidence"] is False
    assert requests[1]["retain_screenshot"] is False
    assert outcome["scrapes"][0]["status"] == "completed"
    assert outcome["scrapes"][0]["collection_attempt"] == "foreground_fallback"
    assert outcome["scrapes"][0]["focus_takeover_count"] == 1
    assert "preview_id" not in outcome["scrapes"][0]
    assert outcome["refreshed_data"][0]["can_use"] is True


@pytest.mark.asyncio
async def test_qualified_values_survive_failed_attempt_to_get_exact_period(monkeypatch):
    _, _, validation = qualified_metric_fixture()
    requests = []
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, json):
            requests.append(json)
            if len(requests) == 1:
                return httpx.Response(200, json={"title": "容量看板", "url": url, "content_text": "100亿", "structured_data": {}, "collected_at": 123})
            return httpx.Response(503, json={"error": "SCRAPE_FAILED"})

    monkeypatch.setattr("creation.service.httpx.AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr("creation.service.REPORT_REFRESH_FOREGROUND_RETRY_DELAY_SECONDS", 0)
    service = CreationService(model="test", enable_vector_recall=False)
    async def validate(*_args, **_kwargs):
        return {"validation_status": "verified", "validation": validation}
    monkeypatch.setattr(service, "_validate_scrape_evidence", validate)
    outcome = await service.scrape_data_context(
        [{"source_id": 21, "source_kind": "report_url", "source_url": "https://bi.example/report", "refresh_required": True}],
        "获取本周专有环境输入Token与共享环境输出Token", {}, browser_extension_enabled=False,
    )
    assert len(requests) == 3
    assert outcome["scrapes"][0]["collection_attempt"] == "qualified_snapshot_fallback"
    assert outcome["refreshed_data"][0]["can_use"] is True
    assert outcome["refreshed_data"][0]["data_risks"] == validation["data_risks"]


@pytest.mark.asyncio
async def test_browser_extension_failure_never_escalates_to_foreground(monkeypatch):
    requests = []

    class FakeResponse:
        status_code = 503
        is_success = False

        @staticmethod
        def json():
            return {
                "error": "BROWSER_EXTENSION_UNAVAILABLE",
                "message": "Chrome 扩展当前未连接",
            }

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, json):
            requests.append(dict(json))
            return FakeResponse()

    monkeypatch.setattr(
        "creation.service.httpx.AsyncClient",
        lambda **_kwargs: FakeAsyncClient(),
    )
    service = CreationService(model="test", enable_vector_recall=False)
    outcome = await service.scrape_data_context(
        [
            {
                "source_id": 7,
                "source_kind": "report_url",
                "source_url": "https://bi.example.com/report",
                "title": "经营看板",
                "refresh_required": True,
                "refresh_policy": "on_demand",
                "can_use": False,
            }
        ],
        "获取最新年化总成本",
        {},
        browser_extension_enabled=True,
    )

    assert len(requests) == 1
    assert requests[0]["extension_preference"] == "auto"
    assert requests[0]["allow_foreground_refresh"] is False
    assert requests[0]["focus_policy"] == "never"
    assert outcome["scrapes"][0]["status"] == "failed"
    assert outcome["scrapes"][0]["collection_attempt"] == "extension_background"


@pytest.mark.asyncio
async def test_browser_extension_postcondition_failure_revalidates_current_view(
    monkeypatch,
):
    requests = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.is_success = 200 <= status_code < 300

        def json(self):
            return self._payload

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, json):
            requests.append(dict(json))
            if len(requests) == 1:
                return FakeResponse(
                    422,
                    {
                        "error": "INTERACTION_POSTCONDITION_FAILED",
                        "message": "日期控件未确认目标范围",
                    },
                )
            return FakeResponse(
                200,
                {
                    "collector": "chrome_attach",
                    "browser": "chrome",
                    "interaction_mode": "background_tab",
                    "focus_policy": "never",
                    "focus_takeover_count": 0,
                    "collected_at": 1_776_000_000_000,
                    "title": "Token 看板",
                    "url": "https://bi.example.com/token-report",
                    "content_text": "输入Token 100亿\n输出Token 20亿\n2026-08-10 至 2026-08-13",
                    "structured_data": {},
                },
            )

    monkeypatch.setattr(
        "creation.service.httpx.AsyncClient",
        lambda **_kwargs: FakeAsyncClient(),
    )
    service = CreationService(model="test", enable_vector_recall=False)

    async def validate(*_args, **kwargs):
        assert kwargs["expected_period"] == {
            "start": "2026-08-10",
            "end": "2026-08-13",
            "display": "2026-08-10 至 2026-08-13",
        }
        return {
            "validation_status": "verified",
            "validation": {
                "reason": "requested_metrics_verified",
                "verified_claims": [{"label": "输入Token", "value": "100亿"}],
            },
        }

    monkeypatch.setattr(service, "_validate_scrape_evidence", validate)
    outcome = await service.scrape_data_context(
        [
            {
                "source_id": 214,
                "source_kind": "report_url",
                "source_url": "https://bi.example.com/token-report",
                "title": "Token 看板",
                "refresh_required": True,
                "refresh_policy": "on_demand",
                "can_use": False,
            }
        ],
        "读取第二个 tab 的本周输入Token和输出Token",
        {
            "time_context": {
                "has_relative_time": True,
                "period_start": "2026-08-10",
                "period_end": "2026-08-16",
                "current_date": "2026-08-13",
                "display": "2026年第33周（2026-08-10 至 2026-08-16）",
            }
        },
        browser_extension_enabled=True,
    )

    assert len(requests) == 2
    assert requests[0]["expected_period_end"] == "2026-08-13"
    assert requests[0]["interaction_plan"] is not None
    assert requests[1]["expected_period_start"] is None
    assert requests[1]["expected_period_end"] is None
    assert requests[1]["interaction_plan"] is None
    assert requests[1]["allow_foreground_refresh"] is False
    assert outcome["scrapes"][0]["status"] == "completed"
    assert outcome["scrapes"][0]["collection_attempt"] == "extension_current_view"


@pytest.mark.asyncio
async def test_browser_extension_metric_rejection_revalidates_current_view(monkeypatch):
    requests = []

    class FakeResponse:
        status_code = 200
        is_success = True

        @staticmethod
        def json():
            return {
                "collector": "chrome_attach",
                "browser": "chrome",
                "interaction_mode": "background_tab",
                "focus_policy": "never",
                "focus_takeover_count": 0,
                "collected_at": 1_776_000_000_000,
                "title": "GPU 看板",
                "url": "https://bi.example.com/gpu-report",
                "content_text": "GPU 数据看板",
                "structured_data": {},
            }

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, json):
            requests.append(dict(json))
            return FakeResponse()

    monkeypatch.setattr(
        "creation.service.httpx.AsyncClient",
        lambda **_kwargs: FakeAsyncClient(),
    )
    service = CreationService(model="test", enable_vector_recall=False)
    validations = iter(
        [
            {
                "validation_status": "rejected",
                "validation": {
                    "reason": "requested_metrics_unavailable",
                    "verified_claims": [],
                },
            },
            {
                "validation_status": "verified",
                "validation": {
                    "reason": "requested_metrics_verified",
                    "verified_claims": [
                        {"label": "GPU 利用率", "value": "72%"}
                    ],
                },
            },
        ]
    )

    async def validate(*_args, **_kwargs):
        return next(validations)

    monkeypatch.setattr(service, "_validate_scrape_evidence", validate)
    outcome = await service.scrape_data_context(
        [
            {
                "source_id": 1582,
                "source_kind": "report_url",
                "source_url": "https://bi.example.com/gpu-report",
                "title": "GPU 看板",
                "refresh_required": True,
                "refresh_policy": "on_demand",
                "can_use": False,
            }
        ],
        "获取最新 GPU 利用率",
        {},
        browser_extension_enabled=True,
    )

    assert len(requests) == 2
    assert requests[0]["interaction_plan"] is not None
    assert requests[1]["interaction_plan"] is None
    assert outcome["scrapes"][0]["status"] == "completed"
    assert outcome["scrapes"][0]["collection_attempt"] == "extension_current_view"


def test_evidence_validation_also_supports_document_style_pages():
    payload = {
        "title": "容量治理说明",
        "url": "https://docs.example.com/capacity-governance",
        "collected_at": 1770000000000,
        "structured_data": {
            "text_blocks": ["容量治理应先识别长期闲置资源，再按业务优先级分批回收。"]
        },
        "evidence": {
            "page_title": "容量治理说明",
            "source_url": "https://docs.example.com/capacity-governance",
            "captured_at": 1770000000000,
        },
    }

    matched = CreationService._compare_scrape_with_ocr(
        payload,
        "容量治理说明\n容量治理应先识别长期闲置资源，再按业务优先级分批回收。",
    )

    assert matched["verified_claims"][0]["claim_type"] == "text"


def test_dashboard_metric_cards_are_extracted_even_when_cache_table_exists():
    payload = {
        "title": "LangBridge 模型中心运营看板",
        "url": "https://bi.example.com/dashboard/gpu",
        "collected_at": 1770000000000,
        "content_text": """日期
2026-07-24
至
2026-07-30
资源总览
年化总额度
2026-07-30
1,747
已交付额度(求和)
2026-07-30
1,736
未纳管
2026-07-30
387
资源管理进度
99.37%""",
        "structured_data": {
            "tables": [
                [["图表名称", "缓存开启情况", "缓存命中情况"], ["资源总览", "未开启缓存", "0%"]]
            ],
            "text_blocks": ["当前页面共有共计 71 个图表"],
        },
        "evidence": {
            "page_title": "LangBridge 模型中心运营看板",
            "source_url": "https://bi.example.com/dashboard/gpu",
            "captured_at": 1770000000000,
        },
    }

    matched = CreationService._compare_scrape_with_ocr(
        payload,
        "LangBridge 模型中心运营看板\n日期 2026-07-24 至 2026-07-30\n"
        "年化总额度 1,747\n已交付额度 1,736\n未纳管 387\n资源管理进度 99.37%",
    )

    metric_claims = [
        claim
        for claim in matched["verified_claims"]
        if str(claim.get("value") or "").strip()
    ]
    assert {claim["value"] for claim in metric_claims} >= {
        "1,747",
        "1,736",
        "387",
        "99.37%",
    }
    periods_by_value = {
        claim["value"]: claim.get("statistical_period")
        for claim in metric_claims
    }
    assert periods_by_value["1,747"] == "2026-07-30"
    assert periods_by_value["1,736"] == "2026-07-30"
    assert periods_by_value["387"] == "2026-07-30"
    assert periods_by_value["99.37%"] == "2026-07-24 至 2026-07-30"
    assert "0%" not in {claim["value"] for claim in metric_claims}


def test_dashboard_metric_cards_accept_compound_currency_and_multiplier_units():
    payload = {
        "title": "GPU 项目用量管理",
        "url": "https://bi.example.com/dashboard/gpu-project",
        "collected_at": 1770000000000,
        "content_text": """在用项目数
102
总卡数（X40折算）
1803.59
年化总成本（万元）
12178.4万元
平均 ROI
39.86x""",
        "structured_data": {},
        "evidence": {
            "page_title": "GPU 项目用量管理",
            "source_url": "https://bi.example.com/dashboard/gpu-project",
            "captured_at": 1770000000000,
        },
    }

    matched = CreationService._compare_scrape_with_ocr(
        payload,
        "在用项目数 102\n总卡数（X40折算） 1803.59\n"
        "年化总成本（万元） 12178.4万元\n平均 ROI 39.86x",
    )

    assert {claim["value"] for claim in matched["verified_claims"]} == {
        "102",
        "1803.59",
        "12178.4万元",
        "39.86x",
    }


def test_zero_metric_value_is_preserved_when_its_label_is_business_data():
    payload = {
        "content_text": "失败任务数\n2026-08-14\n0",
        "structured_data": {},
    }

    claims = CreationService._scrape_claim_candidates(payload)

    assert any(
        claim.get("label") == "失败任务数" and claim.get("value") == "0"
        for claim in claims
    )


def test_canvas_dashboard_values_use_ocr_only_when_dom_labels_match():
    payload = {
        "title": "LangBridge模型中心运营看板",
        "url": "https://bi.example.com/dashboard",
        "collected_at": 1770000000000,
        "content_text": (
            "Token总量\n公共部署模型Token计费成本总计\n"
            "独立部署GPU计费成本总计\n商业模型Token计费成本总计"
        ),
        "structured_data": {
            "dom_content_text": (
                "Token总量\n公共部署模型Token计费成本总计\n"
                "独立部署GPU计费成本总计\n商业模型Token计费成本总计"
            )
        },
        "evidence": {
            "page_title": "LangBridge模型中心运营看板",
            "source_url": "https://bi.example.com/dashboard",
            "captured_at": 1770000000000,
        },
    }
    ocr_text = """Token总量
2026-08-12至2026-08-
12
3,732.62亿
公共部署模型Token计
费成本总计
2026-08-12至2026-08-12
8,010.14
独立部署GPU计费
成本总计
2026-08-12至2026-08-
12
58,643
商业模型Token计费成本总计
2026-08-12至2026-08-12
6,404.98"""

    ocr_validation = CreationService._compare_scrape_with_ocr(payload, ocr_text)
    merged = CreationService._merge_canvas_ocr_validation(
        {"reason": "no_verified_metric", "verified_claims": []},
        ocr_validation,
        0.69,
    )

    assert ocr_validation["loading_marker_count"] == 0
    assert {claim["value"] for claim in merged["verified_claims"]} == {
        "3,732.62亿",
        "8,010.14",
        "58,643",
        "6,404.98",
    }
    assert {claim["label"] for claim in merged["verified_claims"]} == {
        "Token总量",
        "公共部署模型Token计费成本总计",
        "独立部署GPU计费成本总计",
        "商业模型Token计费成本总计",
    }
    assert merged["reason"] == "ocr_dom_label_matched"
    assert merged["primary_channel"] == "screenshot_ocr"
    assert all(
        claim["evidence_origin"] == "screenshot_ocr_dom_label"
        for claim in merged["verified_claims"]
    )


def test_canvas_requested_metrics_accept_repeated_ocr_units_and_exclude_other_cards():
    requested = [
        "专有环境输入Token量",
        "专有环境输出Token量",
        "共享环境输入Token量",
        "共享环境输出Token量",
        "商业类型输入Token量",
        "商业类型输出Token量",
    ]
    payload = {
        "title": "模型运营看板",
        "url": "https://bi.example.com/dashboard",
        "collected_at": 1770000000000,
        "content_text": (
            "Token总量\n共享环境Token计费成本总计\n"
            "专有环境GPU计费成本总计\n商业类型Token计费成本总计"
        ),
        "structured_data": {
            "dom_content_text": (
                "Token总量\n共享环境Token计费成本总计\n"
                "专有环境GPU计费成本总计\n商业类型Token计费成本总计"
            ),
            "requested_metrics": requested,
        },
        "evidence": {
            "page_title": "模型运营看板",
            "source_url": "https://bi.example.com/dashboard",
            "captured_at": 1770000000000,
        },
    }
    ocr_text = """Token计费趋势统计（模型维度）
8,000
专有环境输入Tokens
2026-08-13至2026-08-13
10,919.04亿 亿
专有环境输出Tokens
2026-08-13至2026-08-13
485.8亿 亿
共享环境输入Tokens
2026-08-13至2026-08-13
506.11亿
共享环境输出Tokens
2026-08-13至2026-08-13
32.07亿 亿
商业类型输入Tokens
2026-08-13至2026-08-13
39.11亿亿
商业类型输出Tokens
2026-08-13至2026-08-13
10亿 亿"""

    ocr_validation = CreationService._compare_scrape_with_ocr(payload, ocr_text)
    covered = CreationService._apply_required_metric_coverage(
        ocr_validation,
        requested,
        {"start": "2026-08-10", "end": "2026-08-16"},
    )

    assert covered["required_metric_coverage"] == 1.0
    assert covered["missing_requested_metrics"] == []
    assert [claim["value"] for claim in covered["verified_claims"]] == [
        "10,919.04亿",
        "485.8亿",
        "506.11亿",
        "32.07亿",
        "39.11亿",
        "10亿",
    ]
    assert all(
        claim["evidence_origin"] == "screenshot_ocr_requested_metric"
        for claim in covered["verified_claims"]
    )
    assert "8,000" not in {claim["value"] for claim in covered["verified_claims"]}


def test_canvas_falls_back_to_cross_checked_dom_cards_when_requested_series_absent():
    requested = ["独立部署输入Token", "公共部署输出Token"]
    payload = {
        "title": "模型运营看板",
        "url": "https://bi.example.com/dashboard",
        "collected_at": 1770000000000,
        "content_text": "Token总量\n公共部署模型Token计费成本总计",
        "structured_data": {
            "dom_content_text": "Token总量\n公共部署模型Token计费成本总计",
            "requested_metrics": requested,
        },
        "evidence": {
            "page_title": "模型运营看板",
            "source_url": "https://bi.example.com/dashboard",
            "captured_at": 1770000000000,
        },
    }

    validation = CreationService._compare_scrape_with_ocr(
        payload,
        "Token总量\n31,423.24亿\n公共部署模型Token计费成本总计\n171,538.18",
    )
    covered = CreationService._apply_required_metric_coverage(
        validation,
        requested,
        {"start": "2026-08-17", "end": "2026-08-23"},
    )

    assert covered["requirements_satisfied"] is True
    assert covered["available_values_retained"] is True
    assert covered["reason"] == "requested_metrics_qualified"
    assert covered["risk_disclosure_required"] is True
    assert {claim["value"] for claim in covered["verified_claims"]} == {
        "31,423.24亿",
        "171,538.18",
    }


def test_ocr_metric_value_cleanup_keeps_numeric_tail_safety():
    assert CreationService._normalize_ocr_metric_value_line("32.07亿 亿旦") == "32.07亿"
    assert CreationService._normalize_ocr_metric_value_line("39.11亿亿") == "39.11亿"
    assert (
        CreationService._normalize_ocr_metric_value_line("10,919.04亿1z")
        == "10,919.04亿1z"
    )


@pytest.mark.asyncio
async def test_long_screenshot_uses_adaptive_tiles_to_recover_requested_metrics():
    from PIL import Image
    from ocr.backends.base import OcrBox, OcrOutput

    image_buffer = io.BytesIO()
    Image.new("RGB", (1000, 3000), "white").save(image_buffer, format="JPEG")

    class ShapeAwareOcrEngine:
        def process(self, image_path):
            with Image.open(image_path) as image:
                if image.height >= 2500:
                    return OcrOutput(
                        boxes=[OcrBox(text="运营看板", confidence=0.3)]
                    )
            lines = [
                "私有环境输入Tokens",
                "2026-08-13至2026-08-13",
                "128.5亿 亿",
                "共享环境输出Tokens",
                "2026-08-13至2026-08-13",
                "0",
                "共享环境输出Tokens",
                "2026-08-13至2026-08-13",
                "12.6亿 亿回",
            ]
            return OcrOutput(
                boxes=[
                    OcrBox(
                        text=line,
                        confidence=0.9,
                        bbox=[
                            [0.1, index * 0.08],
                            [0.8, index * 0.08],
                            [0.8, index * 0.08 + 0.04],
                            [0.1, index * 0.08 + 0.04],
                        ],
                    )
                    for index, line in enumerate(lines)
                ]
            )

    class FakeResponse:
        def __init__(self, *, content=b"", payload=None, success=True):
            self.content = content
            self._payload = payload or {}
            self.is_success = success

        def json(self):
            return self._payload

    class FakeClient:
        async def get(self, _url):
            return FakeResponse(content=image_buffer.getvalue())

        async def post(self, _url, json):
            return FakeResponse(
                payload={
                    **payload["evidence"],
                    "validation_status": json["status"],
                    "validation": json["validation"],
                }
            )

    requested = ["私有环境输入Token", "共享环境输出Token"]
    payload = {
        "title": "通用运营看板",
        "url": "https://bi.example.com/dashboard",
        "collected_at": 1770000000000,
        "content_text": "",
        "structured_data": {},
        "evidence": {
            "id": "evidence-adaptive-ocr",
            "page_title": "通用运营看板",
            "source_url": "https://bi.example.com/dashboard",
            "captured_at": 1770000000000,
            "image_url": "/api/creation/evidence/evidence-adaptive-ocr/image",
            "width": 1000,
            "height": 3000,
        },
    }
    service = CreationService(model="test", enable_vector_recall=False)
    service._ocr_engine = ShapeAwareOcrEngine()

    result = await service._validate_scrape_evidence(
        FakeClient(),
        payload,
        require_metric=True,
        required_metrics=requested,
        expected_period={"start": "2026-08-10", "end": "2026-08-16"},
    )

    validation = result["validation"]
    assert result["validation_status"] == "verified"
    assert validation["ocr_strategy"] == "adaptive_tiles"
    assert validation["ocr_tile_count"] > 1
    assert validation["required_metric_coverage"] == 1.0
    assert [claim["value"] for claim in validation["verified_claims"]] == [
        "128.5亿",
        "12.6亿",
    ]


def test_canvas_dashboard_still_loading_is_not_promoted_by_ocr():
    validation = CreationService._merge_canvas_ocr_validation(
        {"reason": "no_verified_metric", "verified_claims": []},
        {
            "loading_marker_count": 2,
            "verified_claims": [
                {
                    "claim_type": "metric",
                    "label": "Token总量",
                    "value": "31,608.43亿",
                    "statement": "Token总量 31,608.43亿",
                }
            ],
        },
        0.68,
    )

    assert validation["verified_claims"] == []
    assert validation["reason"] == "page_still_loading"


def test_canvas_dashboard_accepts_only_locally_loaded_cards_on_partial_page():
    validation = CreationService._merge_canvas_ocr_validation(
        {"reason": "page_still_loading", "verified_claims": []},
        {
            "loading_marker_count": 3,
            "verified_claims": [
                {
                    "claim_type": "metric",
                    "label": "Token总量",
                    "value": "31,608.43亿",
                    "statement": "Token总量 31,608.43亿",
                    "loading_marker_nearby": False,
                },
                {
                    "claim_type": "metric",
                    "label": "公共部署模型Token计费成本总计",
                    "value": "222,111.32",
                    "statement": "公共部署模型Token计费成本总计 222,111.32",
                    "loading_marker_nearby": True,
                },
            ],
        },
        0.68,
    )

    assert validation["reason"] == "ocr_dom_label_matched_partial"
    assert [claim["value"] for claim in validation["verified_claims"]] == [
        "31,608.43亿"
    ]


def test_programmatic_validation_prefers_accessibility_and_matches_dom_claims():
    payload = {
        "title": "GPU 项目用量管理",
        "url": "https://bi.example.com/dashboard/gpu-project",
        "collected_at": 1770000000000,
        "content_text": "在用项目数\n102\n总卡数（X40折算）\n1803.59",
        "structured_data": {
            "dom_content_text": "在用项目数\n102\n总卡数（X40折算）\n1803.59",
            "extraction": {"primary": "accessibility", "fallback": "dom"},
        },
    }

    validation = CreationService._compare_scrape_programmatic_channels(payload)

    assert validation["reason"] == "ax_dom_matched"
    assert validation["primary_channel"] == "accessibility"
    assert {claim["value"] for claim in validation["verified_claims"]} == {
        "102",
        "1803.59",
    }


@pytest.mark.asyncio
async def test_structured_dom_data_is_usable_without_retained_screenshot():
    service = CreationService(model="test", enable_vector_recall=False)
    payload = {
        "title": "GPU 项目用量管理",
        "url": "https://bi.example.com/dashboard/gpu-project",
        "collected_at": 1770000000000,
        "content_text": "在用项目数\n102\n总卡数（X40折算）\n1803.59",
        "structured_data": {
            "dom_content_text": "在用项目数\n102\n总卡数（X40折算）\n1803.59",
            "extraction": {"primary": "dom", "fallback": "dom"},
        },
        "evidence": None,
    }

    result = await service._validate_scrape_evidence(
        object(), payload, require_metric=True
    )

    assert result["validation_status"] == "verified"
    assert result["evidence_kind"] == "structured_page"
    assert result["validation"]["reason"] == "dom_structured"
    assert "image_url" not in result


@pytest.mark.asyncio
async def test_transient_browser_preview_is_ocr_validated_without_retaining_evidence():
    class FakeResponse:
        is_success = True
        content = b"temporary-preview"

    class FakeClient:
        def __init__(self):
            self.requested_urls = []

        async def get(self, url):
            self.requested_urls.append(url)
            return FakeResponse()

        async def post(self, _url, json):
            raise AssertionError("transient preview must not persist evidence metadata")

    class FakeOcrOutput:
        text = "Token总量\n31,423.24亿"
        confidence = 0.96
        boxes = []

    class FakeOcrEngine:
        def process(self, _path):
            return FakeOcrOutput()

    service = CreationService(model="test", enable_vector_recall=False)
    service._ocr_engine = FakeOcrEngine()
    client = FakeClient()
    payload = {
        "title": "模型中心运营看板",
        "url": "https://bi.example.com/dashboard",
        "collected_at": 1770000000000,
        "content_text": "Token总量",
        "structured_data": {
            "dom_content_text": "Token总量",
            "extraction": {"primary": "dom", "fallback": "dom"},
        },
        "evidence": None,
        "transient_preview_url": "/api/browser-integration/jobs/job-1/preview",
    }

    result = await service._validate_scrape_evidence(
        client, payload, require_metric=True
    )

    assert result["validation_status"] == "verified"
    assert result["evidence_kind"] == "transient_preview_ocr"
    assert result["validation"]["verified_claims"][0]["value"] == "31,423.24亿"
    assert client.requested_urls == [
        f"{service.core_engine_base_url}/api/browser-integration/jobs/job-1/preview"
    ]


def test_refreshed_browser_payload_is_merged_without_second_top_k_search():
    evidence = {
        "id": "evidence-live",
        "validation_status": "verified",
        "validation": {
            "verified_claims": [
                {"label": "订单", "value": "1200", "statement": "订单 1200"}
            ]
        },
    }
    merged = CreationService._merge_scrape_results(
        [
            {
                "source_id": 1,
                "title": "经营看板",
                "source_kind": "report_url",
                "source_url": "https://bi.example.com/report",
                "refresh_required": True,
                "can_use": False,
            },
            {
                "source_id": 2,
                "title": "周会记录",
                "source_kind": "work_memory",
                "can_use": True,
                "content_excerpt": "历史结论",
            },
        ],
        {
            1: {
                "title": "经营看板",
                "url": "https://bi.example.com/report",
                "collector": "browser_attach",
                "browser": "chrome",
                "interaction_mode": "temporary_foreground_tab",
                "collected_at": 1770000000000,
                "content_text": "订单 1200",
                "structured_data": {"metric_labels": ["订单 1200"]},
            }
        },
        {1: evidence},
        {1},
    )

    assert [item["source_id"] for item in merged] == [1, 2]
    assert merged[0]["content_excerpt"] == "订单 1200"
    assert merged[0]["creation_evidence"] == evidence
    assert merged[0]["can_use"] is True
    assert merged[0]["refresh_required"] is False
    assert merged[1]["content_excerpt"] == "历史结论"


def test_rejected_live_report_blocks_same_url_historical_values():
    report_url = "https://bi.example.com/report"
    merged = CreationService._merge_scrape_results(
        [
            {
                "source_id": 1,
                "title": "经营看板",
                "source_kind": "report_url",
                "source_url": report_url,
                "refresh_required": True,
                "can_use": False,
            },
            {
                "source_id": 2,
                "title": "经营看板历史指标",
                "source_kind": "work_memory",
                "source_url": report_url,
                "can_use": True,
                "content_excerpt": "旧订单 900",
            },
            {
                "source_id": 3,
                "title": "独立会议记录",
                "source_kind": "work_memory",
                "source_url": None,
                "can_use": True,
                "content_excerpt": "本周计划",
            },
        ],
        {
            1: {
                "title": "经营看板",
                "url": report_url,
                "collector": "browser_attach",
                "browser": "chrome",
                "interaction_mode": "background_browser_window",
                "collected_at": 1770000000000,
                "content_text": "最新订单 1200",
                "structured_data": {"metric_labels": ["订单 1200"]},
            }
        },
        {
            1: {
                "validation_status": "rejected",
                "validation": {
                    "reason": "no_verified_metric",
                    "verified_claims": [],
                },
            }
        },
        {1},
    )

    assert merged[0]["can_use"] is False
    assert merged[0]["refresh_required"] is True
    assert merged[0]["freshness_class"] == "unverified"
    assert merged[0]["evidence_status"] == "rejected"
    assert merged[0]["evidence_reason"] == "no_verified_metric"
    assert merged[1]["can_use"] is False
    assert merged[1]["content_excerpt"] is None
    assert merged[1]["unavailable_reason"] == "superseded_by_live_report"
    assert merged[1]["superseded_by_source_id"] == 1
    assert merged[2]["can_use"] is True
    assert merged[2]["content_excerpt"] == "本周计划"


def test_refresh_attempt_decision_only_continues_on_transient_errors():
    decision = CreationService._should_continue_refresh_attempts
    # 静默读取被焦点门禁拦截时照常升级到前台，瞬态错误也进入前台。
    assert decision("silent", "FOCUS_POLICY_BLOCKED") is True
    assert decision("silent", "SCRAPE_HTTP_502") is True
    assert decision("silent", "SCRAPE_PAGE_ERROR") is True
    assert decision("silent", "SCRAPE_NOT_FOUND") is False
    # 前台降级遇瞬态错误再给一次机会，持久性错误不重试。
    assert decision("foreground_fallback", "SCRAPE_OUTPUT_UNPARSEABLE") is True
    assert decision("foreground_fallback", "SCRAPE_TIMEOUT") is True
    assert decision("foreground_fallback", "SCRAPE_AUTH_REQUIRED") is False
    assert decision(
        "extension_background", "INTERACTION_POSTCONDITION_FAILED"
    ) is True
    assert decision("extension_background", "SCRAPE_PAGE_ERROR") is True
    assert decision("extension_background", "BROWSER_EXTENSION_UNAVAILABLE") is False
    # 最后一次前台重试是终点，任何错误都不再续命。
    assert decision("foreground_retry", "SCRAPE_FAILED") is False
    assert decision("evidence_capture", "SCRAPE_FAILED") is False


def _snapshot_db(
    tmp_path,
    source_id,
    collected_at,
    period_start=None,
    period_end=None,
    content_text="在用项目数 102",
    structured_data=None,
):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE data_snapshots ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER,"
        " collected_at INTEGER, observed_at INTEGER,"
        " period_start_at INTEGER, period_end_at INTEGER,"
        " content_text TEXT, structured_data TEXT, collector TEXT)"
    )
    conn.execute(
        "INSERT INTO data_snapshots (source_id, collected_at, observed_at,"
        " period_start_at, period_end_at, content_text, structured_data, collector)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_id,
            collected_at,
            collected_at,
            period_start,
            period_end,
            content_text,
            json.dumps(structured_data or {"metric_rows": []}),
            "browser_attach",
        ),
    )
    conn.commit()
    conn.close()
    return str(db_path)


def _failed_refresh_report_item(source_id):
    return {
        "source_id": source_id,
        "title": "AI大模型度量",
        "source_kind": "report_url",
        "source_url": "https://bi.example.com/report",
        "refresh_required": True,
        "can_use": True,
        "content_excerpt": "在用项目数 102",
    }


def test_failed_refresh_falls_back_to_period_matched_snapshot(tmp_path):
    snapshot_tables = {
        "tables": [[
            ["项目", "年内成本", "收益"],
            ["项目甲", "806.4万元", "0万元"],
        ]]
    }
    db_path = _snapshot_db(
        tmp_path,
        7,
        1787155000000,
        1787100000000,
        1787700000000,
        content_text="历史快照正文：在用项目数 102",
        structured_data=snapshot_tables,
    )
    merged = CreationService._merge_scrape_results(
        [_failed_refresh_report_item(7)],
        {},
        {},
        {7},
        db_path=db_path,
        time_context={
            "has_relative_time": True,
            "period_start_ms": 1787000000000,
            "period_end_ms": 1787600000000,
        },
    )
    assert merged[0]["can_use"] is True
    assert merged[0]["freshness_class"] == "stale"
    assert (
        merged[0]["stale_fallback"]["reason"]
        == "refresh_failed_period_matched_snapshot"
    )
    assert merged[0]["content_excerpt"] == "历史快照正文：在用项目数 102"
    assert merged[0]["structured_data"] == snapshot_tables
    assert merged[0]["provenance"]["fallback"] is True


def test_failed_refresh_skips_poisoned_error_snapshot(tmp_path):
    db_path = _snapshot_db(
        tmp_path,
        7,
        1787155000000,
        1787100000000,
        1787700000000,
        content_text="在用项目数 102\n总卡数 1803.59",
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO data_snapshots (source_id, collected_at, observed_at,"
        " period_start_at, period_end_at, content_text, structured_data, collector)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            7,
            1787156000000,
            1787156000000,
            1787100000000,
            1787700000000,
            "GPU 项目用量管理\n网络错误",
            json.dumps({"page_state": {"terminal_error_marker_count": 1}}),
            "chrome_attach",
        ),
    )
    conn.commit()
    conn.close()

    merged = CreationService._merge_scrape_results(
        [_failed_refresh_report_item(7)],
        {},
        {},
        {7},
        db_path=db_path,
        time_context={
            "has_relative_time": True,
            "period_start_ms": 1787000000000,
            "period_end_ms": 1787600000000,
        },
    )

    assert merged[0]["can_use"] is True
    assert merged[0]["content_excerpt"] == "在用项目数 102\n总卡数 1803.59"
    assert merged[0]["stale_fallback"]["snapshot_collected_at"] == 1787155000000


def test_failed_refresh_rejects_period_mismatched_snapshot(tmp_path):
    # 快照周期与要求周期不重叠时绝不降级，避免旧周期数据冒充本周数据。
    db_path = _snapshot_db(tmp_path, 7, 1786000000000, 1785900000000, 1786500000000)
    merged = CreationService._merge_scrape_results(
        [_failed_refresh_report_item(7)],
        {},
        {},
        {7},
        db_path=db_path,
        time_context={
            "has_relative_time": True,
            "period_start_ms": 1787000000000,
            "period_end_ms": 1787600000000,
        },
    )
    assert merged[0]["can_use"] is False
    assert merged[0]["unavailable_reason"] == "refresh_failed"


def test_failed_refresh_rejects_snapshot_with_out_of_period_metric_facts(tmp_path):
    # 快照周标签即使与目标周重叠，指标自身明确标注为上周时仍不得降级。
    db_path = _snapshot_db(
        tmp_path,
        7,
        1787155000000,
        1787100000000,
        1787700000000,
        content_text="2026-08-15日输入Tokens为822.21亿",
        structured_data={
            "metric_rows": [
                {
                    "metric": "输入Tokens",
                    "value": "822.21亿",
                    "statement": "2026-08-15日输入Tokens为822.21亿。",
                }
            ]
        },
    )
    merged = CreationService._merge_scrape_results(
        [_failed_refresh_report_item(7)],
        {},
        {},
        {7},
        db_path=db_path,
        time_context={
            "has_relative_time": True,
            "period_start_ms": int(datetime(2026, 8, 17).timestamp() * 1000),
            "period_end_ms": int(datetime(2026, 8, 23, 23, 59).timestamp() * 1000),
        },
    )
    assert merged[0]["can_use"] is False
    assert merged[0]["unavailable_reason"] == "refresh_failed"


def test_requested_period_policy_removes_out_of_period_work_memory_values():
    start_ms = int(datetime(2026, 8, 17).timestamp() * 1000)
    end_ms = int(datetime(2026, 8, 23, 23, 59).timestamp() * 1000)
    results = [
        {
            "source_id": 6333,
            "source_kind": "work_memory",
            "can_use": True,
            "content_excerpt": "2026-08-15日输入Tokens为822.21亿。",
            "structured_data": {
                "period": {"start_at": start_ms, "end_at": end_ms},
                "metric_rows": [
                    {
                        "metric": "输入Tokens",
                        "value": "822.21亿",
                        "statement": "2026-08-15日输入Tokens为822.21亿。",
                    }
                ],
            },
            "provenance": {"period": {"start_at": start_ms, "end_at": end_ms}},
        }
    ]

    filtered = CreationService._apply_requested_period_policy(
        results,
        {
            "has_relative_time": True,
            "period_start_ms": start_ms,
            "period_end_ms": end_ms,
        },
    )

    assert filtered[0]["can_use"] is False
    assert filtered[0]["period_match"] is False
    assert filtered[0]["unavailable_reason"] == "metric_period_mismatch"
    assert filtered[0]["content_excerpt"] is None
    assert filtered[0]["structured_data"] is None


def test_requested_period_policy_keeps_only_in_period_metric_facts():
    start_ms = int(datetime(2026, 8, 17).timestamp() * 1000)
    end_ms = int(datetime(2026, 8, 23, 23, 59).timestamp() * 1000)
    results = [
        {
            "source_id": 8,
            "source_kind": "work_memory",
            "can_use": True,
            "content_excerpt": "旧值 10，新值 20",
            "structured_data": {
                "period": {"start_at": start_ms, "end_at": end_ms},
                "metric_rows": [
                    {"value": "10", "statement": "2026-08-15日指标为10。"},
                    {"value": "20", "statement": "2026-08-20日指标为20。"},
                ],
            },
        }
    ]

    filtered = CreationService._apply_requested_period_policy(
        results,
        {
            "has_relative_time": True,
            "period_start_ms": start_ms,
            "period_end_ms": end_ms,
        },
    )

    assert filtered[0]["can_use"] is True
    assert filtered[0]["content_excerpt"] == "2026-08-20日指标为20。"
    assert [
        row["value"] for row in filtered[0]["structured_data"]["metric_rows"]
    ] == ["20"]


def test_failed_refresh_falls_back_to_recent_snapshot_without_time_requirement(tmp_path):
    now_ms = int(time.time() * 1000)
    db_path = _snapshot_db(tmp_path, 7, now_ms - 3600 * 1000)
    merged = CreationService._merge_scrape_results(
        [_failed_refresh_report_item(7)],
        {},
        {},
        {7},
        db_path=db_path,
        time_context={},
    )
    assert merged[0]["can_use"] is True
    assert merged[0]["stale_fallback"]["reason"] == "refresh_failed_recent_snapshot"


def test_data_citation_guard_replaces_guessed_source_with_supporting_memory():
    document = """# GPU 治理方案

| 维度 | GPUTL | SMACT |
|---|---:|---:|
| 国内 | 42% | 25% |
| 海外 | 47% | 25% |
| 充分利用阈值 | - | 80% |

*数据来源：LangBridge 模型中心运营看板，采集时间 2026-07-30*
"""
    data_results = [
        {
            "source_id": 55,
            "title": "阅读并分析了容器云 GPU 指标采集项目的技术文档",
            "source_kind": "work_memory",
            "source_url": None,
            "can_use": True,
            "collected_at": 1770000000000,
            "observed_at": 1770000000000,
            "content_excerpt": (
                "容器云 GPU 指标采集项目：国内日均 42%，海外 47%，"
                "SMACT 约 25%，英伟达认为达到 80% 才算充分利用。"
            ),
            "structured_data": {},
        }
    ]

    updated, audit = CreationAgentLoop._guard_data_citations(document, data_results)

    assert audit[0]["status"] == "corrected"
    assert audit[0]["source_id"] == 55
    assert "LangBridge 模型中心运营看板" not in updated
    assert "容器云 GPU 指标采集项目" in updated
    assert "工作记忆采集时间" in updated


def test_data_citation_guard_removes_unsupported_source_without_meta_notice():
    document = """# 周报

本周 GPU 利用率达到 65%。

*数据来源：某实时看板，采集时间 2026-07-30*
"""
    updated, audit = CreationAgentLoop._guard_data_citations(
        document,
        [
            {
                "source_id": 1,
                "title": "GPU 看板",
                "source_kind": "report_url",
                "can_use": False,
                "content_excerpt": None,
            }
        ],
    )

    assert audit[0]["status"] == "unsupported"
    assert "某实时看板" not in updated
    assert "数据状态" not in updated
    assert "待核验" not in updated


def test_verified_evidence_card_is_inserted_below_the_claim_block():
    document = "# GPU 治理方案\n\n国内 GPU 利用率为 42%，需要优先治理。\n\n## 后续动作\n\n按周复盘。"
    evidence = {
        "id": "evidence-1",
        "source_url": "https://bi.example.com/dashboard/gpu",
        "page_title": "GPU 实时看板",
        "captured_at": 1770000000000,
        "image_url": "/api/creation/evidence/evidence-1/image",
        "display_image_url": "/api/creation/evidence/evidence-1/image?crop=10,20,600,280",
        "validation_status": "verified",
        "validation": {
            "verified_claims": [
                {"label": "国内 GPU 利用率", "value": "42%", "statement": "国内 GPU 利用率 42%"}
            ]
        },
    }

    updated, applied = CreationAgentLoop._apply_creation_evidence_cards(document, [evidence])

    assert len(applied) == 1
    assert updated.index("国内 GPU 利用率为 42%") < updated.index("![证据截图")
    assert updated.index("![证据截图") < updated.index("## 后续动作")
    assert "?crop=10,20,600,280" in updated
    assert "查看原始全图" in updated


def test_step_scoped_evidence_cannot_attach_to_an_earlier_section():
    document = """# GPU成本优化周报

## 本周大模型性能成本优化周会会议纪要

- 2026年8月13日推进 GPU 成本优化专项。

## GPU算力数据

| 项目 | 卡数(X40) |
| --- | ---: |
| 快点大模型治理平台 | 119.42 |

## Token数据

本周 Token 成本保持稳定。"""
    evidence = {
        "id": "gpu-info-evidence",
        "source_url": "https://gpu.example.com/info",
        "page_title": "电商GPU信息平台 - GPU使用情况一览",
        "captured_at": 1_786_812_464_123,
        "image_url": "/api/creation/evidence/gpu-info-evidence/image",
        "validation_status": "verified",
        "skill_step_title": "GPU算力数据",
        "target_section": "GPU算力数据",
        "validation": {
            "verified_claims": [
                {
                    "claim_type": "metric",
                    "label": "UNKNOWN GPU卡数",
                    "value": "2",
                    "statement": "2026-08-13 UNKNOWN 2",
                }
            ]
        },
    }

    updated, applied = CreationAgentLoop._apply_creation_evidence_cards(
        document, [evidence]
    )

    screenshot_index = updated.index("![证据截图")
    assert len(applied) == 1
    assert screenshot_index > updated.index("## GPU算力数据")
    assert screenshot_index < updated.index("## Token数据")


def test_unscoped_evidence_ignores_single_character_metric_values():
    document = "# 周报\n\n2026年8月13日推进 GPU 成本优化专项。"
    evidence = {
        "id": "weak-evidence",
        "image_url": "/api/creation/evidence/weak-evidence/image",
        "validation_status": "verified",
        "validation": {
            "verified_claims": [
                {"claim_type": "metric", "label": "GPU卡数", "value": "2"}
            ]
        },
    }

    updated, applied = CreationAgentLoop._apply_creation_evidence_cards(
        document, [evidence]
    )

    assert updated == document
    assert applied == []


def test_creation_evidence_keeps_its_skill_step_section_scope():
    evidence = CreationAgentLoop._scope_creation_evidence(
        {"id": "gpu-evidence", "validation_status": "verified"},
        {
            "skill_id": "gpu-weekly",
            "skill_step_id": "gpu-metrics",
            "skill_step_title": "GPU算力数据",
        },
    )

    assert evidence["skill_id"] == "gpu-weekly"
    assert evidence["skill_step_id"] == "gpu-metrics"
    assert evidence["skill_step_title"] == "GPU算力数据"
    assert evidence["target_section"] == "GPU算力数据"


def test_gpu_and_token_evidence_are_accumulated_across_skill_steps():
    gpu = {
        "id": "gpu-evidence",
        "page_title": "GPU 算力看板",
        "validation_status": "verified",
    }
    token = {
        "id": "token-evidence",
        "page_title": "LangBridge 模型中心运营看板",
        "validation_status": "verified",
    }

    merged = CreationAgentLoop._merge_evidence_items([gpu], [token])

    assert [item["id"] for item in merged] == ["gpu-evidence", "token-evidence"]


def test_evidence_display_crop_focuses_on_verified_claim_region():
    crop = CreationService._derive_evidence_display_crop(
        {"structured_data": {}},
        {"width": 1200, "height": 800},
        {
            "verified_claims": [
                {
                    "claim_type": "metric",
                    "label": "国内 GPU 利用率",
                    "value": "42%",
                }
            ]
        },
        [
            SimpleNamespace(
                text="国内 GPU 利用率 42%",
                bbox=[[200, 300], [700, 300], [700, 400], [200, 400]],
            )
        ],
    )

    assert crop is not None
    assert crop["x"] < 200
    assert crop["y"] < 300
    assert crop["width"] < 1200
    assert crop["height"] < 800


def test_evidence_display_crop_uses_final_ocr_coordinates_for_stitched_page():
    crop = CreationService._derive_evidence_display_crop(
        {"structured_data": {"scroll_capture": {"aggregated": True}}},
        {"width": 1200, "height": 5000},
        {
            "verified_claims": [
                {
                    "claim_type": "metric",
                    "label": "输入 Token 用量",
                    "value": "128亿",
                }
            ]
        },
        [
            SimpleNamespace(
                text="输入 Token 用量 128亿",
                bbox=[[180, 2100], [760, 2100], [760, 2200], [180, 2200]],
            )
        ],
    )

    assert crop is not None
    assert crop["y"] < 2100
    assert crop["height"] < 5000


def test_evidence_display_crop_ignores_short_label_fragments_elsewhere():
    crop = CreationService._derive_evidence_display_crop(
        {"structured_data": {"scroll_capture": {"aggregated": True}}},
        {"width": 1200, "height": 5000},
        {
            "verified_claims": [
                {
                    "claim_type": "metric",
                    "label": "共享环境输出Tokens",
                    "value": "12.6亿",
                }
            ]
        },
        [
            SimpleNamespace(
                text="共享环境输出Tokens",
                bbox=[[0.2, 0.3], [0.5, 0.3], [0.5, 0.33], [0.2, 0.33]],
            ),
            SimpleNamespace(
                text="12.6亿",
                bbox=[[0.2, 0.34], [0.4, 0.34], [0.4, 0.38], [0.2, 0.38]],
            ),
            SimpleNamespace(
                text="Token",
                bbox=[[0.2, 0.9], [0.4, 0.9], [0.4, 0.93], [0.2, 0.93]],
            ),
        ],
    )

    assert crop is not None
    assert crop["y"] < 1500
    assert crop["height"] < 1000


def test_evidence_display_crop_excludes_distant_duplicate_value_from_chart():
    claims = [
        {"claim_type": "metric", "label": "类别甲输入量", "value": "109.04亿"},
        {"claim_type": "metric", "label": "类别甲输出量", "value": "10亿"},
        {"claim_type": "metric", "label": "类别乙输入量", "value": "39.11亿"},
    ]
    crop = CreationService._derive_evidence_display_crop(
        {"structured_data": {"scroll_capture": {"aggregated": True}}},
        {"width": 2400, "height": 8740},
        {"verified_claims": claims},
        [
            # 趋势图纵轴碰巧与指标值相同，不应拉高最终裁剪框。
            SimpleNamespace(text="10亿", bbox=[[1326, 1765], [1382, 1793]]),
            SimpleNamespace(text="类别甲输入量", bbox=[[94, 2817], [356, 2848]]),
            SimpleNamespace(text="109.04亿", bbox=[[94, 2897], [387, 2954]]),
            SimpleNamespace(text="类别甲输出量", bbox=[[509, 2848], [771, 2876]]),
            SimpleNamespace(text="10亿", bbox=[[2093, 2933], [2246, 2985]]),
            SimpleNamespace(text="类别乙输入量", bbox=[[1678, 2817], [1943, 2848]]),
            SimpleNamespace(text="39.11亿", bbox=[[1678, 2898], [1887, 2950]]),
        ],
    )

    assert crop is not None
    assert crop["y"] > 2600
    assert crop["height"] < 500


def test_evidence_display_crop_uses_dense_absolute_dom_region_for_long_page():
    crop = CreationService._derive_evidence_display_crop(
        {
            "structured_data": {
                "scroll_capture": {"aggregated": True},
                "page_state": {
                    "outer_width": 1200,
                    "inner_width": 1200,
                    "outer_height": 740,
                    "inner_height": 619,
                    "scroll_height": 2680,
                },
                "evidence_regions": [
                    {
                        "x": 252,
                        "document_y": 101,
                        "width": 916,
                        "height": 244,
                        "text": (
                            "在用项目数 102 总容量 1803.59 "
                            "年化成本 12178.4万元 平均 ROI 39.86x"
                        ),
                    },
                    {
                        "x": 252,
                        "document_y": 900,
                        "width": 916,
                        "height": 180,
                        "text": "项目甲 119.42 806.4万元 0.00x",
                    },
                ],
            }
        },
        {"width": 2400, "height": 5602},
        {
            "verified_claims": [
                {"label": "在用项目数", "value": "102"},
                {"label": "总容量", "value": "1803.59"},
                {"label": "年化成本", "value": "12178.4万元"},
                {"label": "平均 ROI", "value": "39.86x"},
                {"label": "项目甲", "value": "119.42"},
                {"label": "项目甲", "value": "806.4万元"},
                {"label": "项目甲", "value": "0.00x"},
            ],
            "matched_requested_metrics": [],
        },
        [],
    )

    assert crop is not None
    assert crop["y"] < 600
    assert crop["height"] < 800
    assert crop["width"] > 1600


def test_generated_week_placeholder_is_corrected_and_metric_placeholder_removed():
    document = (
        "# 周报\n\n本周（2025年第X周）完成两项优化。\n\n"
        "| 指标 | 数值 |\n| --- | --- |\n| 独立部署输入Token | 数据未明确区分 |\n"
        "| 商业模型输出Token | 数据未获取 |\n"
    )
    requirement = {
        "time_context": CreationService._relative_time_context(
            "本周",
            now=datetime.fromisoformat("2026-08-13T20:20:00+08:00"),
        )
    }

    updated, audit = CreationAgentLoop._guard_generated_placeholders(
        document,
        requirement,
    )

    assert "2026年第33周（2026-08-10 至 2026-08-16）" in updated
    assert "2025年第X周" not in updated
    assert "数据未明确区分" not in updated
    assert "数据未获取" not in updated
    assert {item["kind"] for item in audit} == {
        "relative_time_corrected",
        "unsupported_placeholder_removed",
    }


def test_quality_gate_decision_uses_user_facing_summary():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="写一份架构方案",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-quality-summary",
        run_id="run-quality-summary",
    )

    event = loop._harness_decision_event(
        state,
        {
            "trigger": "quality_review_agent",
            "trigger_status": "completed",
            "reason_code": "quality_gate_passed",
            "scheduled": [],
            "activated_skills": [],
        },
    )

    assert event["summary"] == "质量检查通过"
    assert "Harness" not in event["summary"]


def test_harness_uses_data_search_feedback_to_choose_the_next_capability():
    loop = CreationAgentLoop(FakeCreationService())

    def state_with(results):
        state = loop._new_state(
            user_message="生成本周项目周报，并分析核心指标变化",
            root_request=None,
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enabled_tools=()),
            model_mode="local",
            session_id="session-data-feedback",
            run_id="run-data-feedback",
        )
        resolve_planned(loop, state)
        state.cursor = next(
            index + 1 for index, step in enumerate(state.plan) if step["id"] == "data_search"
        )
        state.environment["data_results"] = results
        return state

    stale_report = state_with(
        [
            {
                "source_id": 1,
                "source_kind": "report_url",
                "source_url": "https://bi.example.com/report",
                "refresh_required": True,
                "can_use": False,
                "content_excerpt": "上次采集的指标",
            }
        ]
    )
    decision = loop._replan_after_feedback(
        stale_report,
        {"id": "data_search"},
        status="completed",
    )
    assert decision["scheduled"] == ["webpage_scrape"]
    assert stale_report.plan[stale_report.cursor]["id"] == "webpage_scrape"

    fresh_snapshot = state_with(
        [
            {
                "source_id": 2,
                "source_kind": "report_url",
                "source_url": "https://bi.example.com/report",
                "refresh_required": False,
                "can_use": True,
                "content_excerpt": "本周订单 1200",
            }
        ]
    )
    decision = loop._replan_after_feedback(
        fresh_snapshot,
        {"id": "data_search"},
        status="completed",
    )
    assert decision["scheduled"] == []
    assert decision["reason_code"] == "source_metadata_only"

    structured_snapshot = state_with(
        [
            {
                "source_id": 3,
                "source_kind": "report_url",
                "source_url": "https://bi.example.com/report",
                "refresh_required": False,
                "can_use": True,
                "structured_data": {
                    "tables": [[['维度', '指标'], ['A', '10']]],
                    "pagination": {"dataset_complete": True},
                },
            }
        ]
    )
    decision = loop._replan_after_feedback(
        structured_snapshot,
        {"id": "data_search"},
        status="completed",
    )
    assert decision["scheduled"] == ["data_query_planner"]
    assert decision["reason_code"] == "structured_data_ready"

    structured_snapshot.cursor += 1
    structured_snapshot.environment["data_query_plans"] = [
        {"mode": "narrative", "operations": []}
    ]
    decision = loop._replan_after_feedback(
        structured_snapshot,
        {"id": "data_query_planner"},
        status="completed",
    )
    assert decision["scheduled"] == ["data_analysis_agent"]
    assert decision["reason_code"] == "narrative_analysis_required"

    no_data = state_with([])
    decision = loop._replan_after_feedback(
        no_data,
        {"id": "data_search"},
        status="completed",
    )
    assert decision["scheduled"] == []
    assert decision["reason_code"] == "no_matching_data"


def test_harness_does_not_analyze_unverified_report_after_refresh_failure():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="生成本周项目周报，并分析核心指标变化",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-data-refresh-failed",
        run_id="run-data-refresh-failed",
    )
    resolve_planned(loop, state)
    state.cursor = next(
        index + 1 for index, step in enumerate(state.plan) if step["id"] == "data_search"
    )
    state.environment["data_results"] = [
        {
            "source_id": 1,
            "source_kind": "report_url",
            "source_url": "https://bi.example.com/report",
            "refresh_required": True,
            "can_use": False,
            "content_excerpt": "历史订单 900",
        }
    ]
    loop._replan_after_feedback(state, {"id": "data_search"}, status="completed")
    state.cursor += 1

    decision = loop._replan_after_feedback(
        state,
        {"id": "webpage_scrape"},
        status="failed",
        error_code="SCRAPE_AUTH_REQUIRED",
    )

    assert decision["scheduled"] == []
    assert decision["reason_code"] == "refresh_failed_without_snapshot"


def test_quality_feedback_routes_specialists_and_dependencies_in_stable_order():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="写一份带流程图和对比表的架构方案",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=("plantuml_diagram",)),
        model_mode="local",
        session_id="session-quality-routing",
        run_id="run-quality-routing",
    )
    resolve_planned(loop, state)
    state.cursor = len(state.plan)
    state.environment["quality_issues"] = [
        {"code": "ai_style_signals", "severity": "soft", "agent_id": "anti_ai_style_agent", "required_capabilities": []},
        {"code": "detail_incomplete", "severity": "soft", "agent_id": "detail_polish_agent", "required_capabilities": []},
        {"code": "table_needs_polish", "severity": "soft", "agent_id": "table_polish_agent", "required_capabilities": []},
        {"code": "visual_needs_polish", "severity": "soft", "agent_id": "image_polish_agent", "required_capabilities": ["plantuml_diagram"]},
        {"code": "emphasis_needs_polish", "severity": "soft", "agent_id": "typography_polish_agent", "required_capabilities": []},
    ]

    decision = loop._replan_after_feedback(
        state,
        {"id": "quality_review_agent"},
        status="completed",
    )

    assert decision["reason_code"] == "quality_issues_detected"
    assert decision["quality_cycle"] == 1
    assert decision["activated_skills"] == []
    assert decision["scheduled"] == [
        "plantuml_diagram",
        "detail_polish_agent",
        "table_polish_agent",
        "image_polish_agent",
        "anti_ai_style_agent",
        "typography_polish_agent",
        "quality_review_agent",
    ]

    contract_path = (
        Path(__file__).resolve().parents[2]
        / "shared"
        / "creation-tools"
        / "creation-tools.schema.json"
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    decision_branches = contract["$defs"]["harness_decision_event"]["properties"][
        "data"
    ]["oneOf"]
    quality_branch = next(
        branch
        for branch in decision_branches
        if branch.get("properties", {}).get("trigger", {}).get("const")
        == "quality_review_agent"
    )
    assert set(quality_branch["required"]) <= set(decision)
    assert decision["trigger"] == quality_branch["properties"]["trigger"]["const"]
    allowed_scheduled = set(
        quality_branch["properties"]["scheduled"]["items"]["enum"]
    )
    assert set(decision["scheduled"]) <= allowed_scheduled


def test_placeholder_markers_only_count_standalone_items_not_inline_mentions():
    loop = CreationAgentLoop(FakeCreationService())

    # 正常句子里提到“待补充”不是占位符（事故现场：确认事项列表）
    assert loop._placeholder_count(
        "- 确认事项: 8 月 6 日是否有具体论文摘要待补充;如有,预计更新时间和内容范围"
    ) == 0
    assert loop._placeholder_count("后续完善的方向由负责人跟进。") == 0

    # 独占一行、列表项或表格单元格的标记才算占位符
    assert loop._placeholder_count("待补充") == 1
    assert loop._placeholder_count("- 待补充\n- TODO\n1. TBD") == 3
    assert loop._placeholder_count("| 指标 | 待补充 |\n| 来源 | TBD |") == 2


def test_inline_placeholder_mention_does_not_flag_detail_incomplete():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="展示下 8 月 6 号的 Agent 架构前沿论文报告的内容",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-placeholder-inline",
        run_id="run-placeholder-inline",
    )
    body = (
        "本报告汇总了当日公开的 Agent 架构相关论文，按主题归类并给出核心结论与阅读建议，"
        "所有条目均保留原始链接供核验。" * 4
    )
    document = (
        "# Agent 架构前沿论文报告\n\n"
        f"## 概览\n\n{body}\n\n"
        f"## 论文汇总时间线\n\n{body}\n\n"
        "## 确认事项\n\n"
        "- 确认事项: 8 月 6 日是否有具体论文摘要待补充;如有,预计更新时间和内容范围，"
        "并同步核对各条目的原始链接、发布时间与作者信息是否完整可验证。\n"
    )

    criteria, issues = loop._inspect_document_quality(state, document)

    assert criteria["detail_complete"] is True
    assert not any(item["code"] == "detail_incomplete" for item in issues)


def test_action_structure_requirements_detect_missing_and_short_subsections():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="生成技术架构设计文档",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-component-detail-missing",
        run_id="run-component-detail-missing",
    )
    state.environment["skill_structure_requirements"] = {
        "minimum_subsections": 3,
        "minimum_subsection_chars": 80,
        "source_text": "至少 3 个子章节，每个子章节正文不少于 80 字。",
    }
    document = """# 技术架构方案

## 方案设计

### 整体架构

```plantuml
@startuml
[接入网关] as Gateway
[路由控制器] as Router
[执行引擎] as Engine
[状态存储] as Store
Gateway --> Router
Router --> Engine
Engine --> Store
@enduml
```

仅展示总体关系。

## 实施计划

先完成接口联调，再进行小流量验证和回退演练。实施过程记录版本、负责人和验收结果，确保变更可以复核。
"""

    criteria, issues = loop._inspect_document_quality(state, document)

    assert criteria["subsection_requirements_satisfied"] is False
    issue = next(
        item
        for item in issues
        if item["code"] == "subsection_requirements_incomplete"
    )
    assert issue["agent_id"] == "detail_polish_agent"
    assert issue["evidence"]["subsection_count"] == 1
    assert issue["evidence"]["qualified_subsection_count"] == 0


def test_action_structure_requirements_accept_qualified_subsections():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="生成技术架构设计文档",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-component-detail-covered",
        run_id="run-component-detail-covered",
    )
    state.environment["skill_structure_requirements"] = {
        "minimum_subsections": 2,
        "minimum_subsection_chars": 30,
        "source_text": "至少 2 个子章节，每个子章节正文不少于 30 字。",
    }
    document = """# 技术架构方案

## 方案设计

### 整体架构

```mermaid
flowchart LR
  A[接入网关] --> B[路由控制器]
  B --> C[执行引擎]
  C --> D[状态存储]
```

总体链路按接入、决策、执行和持久化四类职责组织，各层只通过稳定接口交互。

### 接入与路由控制

接入网关负责认证、限流和请求规范化，输出稳定请求信封。路由控制器只消费规范化字段，根据能力、负载和健康状态选择执行目标；路由失败时回到默认目标，并记录决策原因用于验证。

### 执行与状态管理

执行引擎负责运行受控任务并输出状态事件，不直接修改路由规则。状态存储按请求标识保存幂等结果、阶段状态和审计依据；执行超时后可以依据最后一个稳定状态恢复或回退，并通过成功率和恢复时长验收。

## 实施计划

按接口、灰度、观测和回退四个阶段实施，每个阶段完成后核对责任人、验收证据及继续扩量条件。
"""

    criteria, issues = loop._inspect_document_quality(state, document)

    assert criteria["subsection_requirements_satisfied"] is True
    assert not any(
        item["code"] == "subsection_requirements_incomplete" for item in issues
    )

    state.environment["skill_structure_requirements"]["minimum_subsections"] = 4
    stricter_criteria, _ = loop._inspect_document_quality(state, document)
    assert stricter_criteria["subsection_requirements_satisfied"] is False


def test_quality_replan_does_not_rerun_polisher_for_same_unresolved_issue():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="写一份架构方案文档",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-quality-dedupe",
        run_id="run-quality-dedupe",
    )
    resolve_planned(loop, state)
    state.cursor = len(state.plan)
    state.environment["quality_issues"] = [
        {
            "code": "detail_incomplete",
            "severity": "soft",
            "agent_id": "detail_polish_agent",
            "required_capabilities": [],
        }
    ]

    decision = loop._replan_after_feedback(
        state,
        {"id": "quality_review_agent"},
        status="completed",
    )
    assert decision["reason_code"] == "quality_issues_detected"
    assert "detail_polish_agent" in decision["scheduled"]

    # 模拟细节润色 Agent 执行过一次并记录了修复动作，但质检仍报同一问题
    resolve_planned(loop, state)
    state.cursor = len(state.plan)
    state.environment.setdefault("quality_mutations", []).append(
        {
            "agent_id": "detail_polish_agent",
            "quality_cycle": 1,
            "issue_codes": ["detail_incomplete"],
            "document_hash": "hash-1",
        }
    )

    decision = loop._replan_after_feedback(
        state,
        {"id": "quality_review_agent"},
        status="completed",
    )
    assert decision["reason_code"] == "quality_issues_deferred"
    assert decision["scheduled"] == []
    assert decision["deferred_issue_codes"] == ["detail_incomplete"]
    assert decision["quality_cycle"] == 1
    assert not any(
        step.get("id") == "detail_polish_agent"
        for step in state.plan[state.cursor :]
    )


@pytest.mark.asyncio
async def test_unresolvable_detail_issue_converges_after_one_polish_attempt():
    class StubbornPlaceholderService(FakeCreationService):
        def analyze_requirement(
            self,
            message,
            options,
            entity_focus_text="",
            retrieval_context_terms=None,
        ):
            requirement = super().analyze_requirement(
                message, options, entity_focus_text
            )
            # 避免默认 doc_type 里的“架构”触发图示质检，只验证占位符问题收敛
            requirement["doc_type"] = "项目复盘"
            return requirement

        async def stream_agent_document(self, **kwargs):
            document = (
                "# 项目复盘\n\n"
                "## 发生了什么\n\n"
                "团队在两周内完成了需求拆分、接口联调和灰度发布。联调阶段暴露出三个边界条件，"
                "负责人当天补充了用例，第二天完成回归。这个过程没有改变发布目标，"
                "但让验收口径从口头约定变成了可重复检查的清单，后续复盘可以直接引用。"
                "灰度期间共收集到十二条用户反馈，其中九条在当周闭环，剩余三条列入下一轮迭代跟踪。"
                "发布前的全量回归用时比上一轮缩短了四小时，主要得益于用例去重和失败重试策略的调整。"
                "监控侧同步新增了灰度期间的错误率看板，值班同学可以在五分钟内定位到异常接口。"
                "联调阶段发现的三个边界条件也都写进了回归用例，避免同类问题在后续版本重复出现。\n\n"
                "## 我们怎么判断\n\n"
                "复盘以工单、测试记录和发布日志为依据。**关键判断是先修正进入条件，再增加提醒。**"
                "原因很直接：同一问题连续出现时，提醒只能让人更忙，明确的进入条件才能减少返工。"
                "对照上一轮复盘的三条行动项，两条已经闭环，一条因为依赖外部接口延期，需要在下一轮重新排期。"
                "所有判断都记录了工单编号和日志截取位置，方便后续新成员直接按图核验。"
                "对于延期的一项，团队约定在外部接口恢复后一周内补做灰度验证，并把结果写回同一份复盘记录。\n\n"
                "## 下一步怎么做\n\n"
                "- 待补充\n"
            )
            # 润色 Agent 也无法消除这个占位符，返回相同内容
            yield document

    events = await collect_events(
        CreationAgentLoop(StubbornPlaceholderService()).run(
            user_message="请写一份完整的项目复盘文档",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enable_rag=False),
        )
    )

    assert events[-1]["type"] == "run.completed"
    # 细节润色 Agent 只应被调度一次，不能因为问题未解决而反复重写全文
    assert sum(
        event["type"] == "agent.completed"
        and event["actor"]["id"] == "detail_polish_agent"
        for event in events
    ) == 1
    deferred = [
        event
        for event in events
        if event["type"] == "harness.decision"
        and event["data"].get("reason_code") == "quality_issues_deferred"
    ]
    assert deferred
    assert deferred[0]["data"]["deferred_issue_codes"] == ["detail_incomplete"]
    assert "待补充" in events[-1]["data"]["document"]


@pytest.mark.asyncio
async def test_polish_step_streams_progress_instead_of_full_document():
    class StubbornPlaceholderService(FakeCreationService):
        def analyze_requirement(
            self,
            message,
            options,
            entity_focus_text="",
            retrieval_context_terms=None,
        ):
            requirement = super().analyze_requirement(
                message, options, entity_focus_text
            )
            requirement["doc_type"] = "项目复盘"
            return requirement

        async def stream_agent_document(self, **kwargs):
            document = (
                "# 项目复盘\n\n"
                "## 发生了什么\n\n"
                "团队在两周内完成了需求拆分、接口联调和灰度发布。联调阶段暴露出三个边界条件，"
                "负责人当天补充了用例，第二天完成回归。这个过程没有改变发布目标，"
                "但让验收口径从口头约定变成了可重复检查的清单，后续复盘可以直接引用。"
                "灰度期间共收集到十二条用户反馈，其中九条在当周闭环，剩余三条列入下一轮迭代跟踪。"
                "发布前的全量回归用时比上一轮缩短了四小时，主要得益于用例去重和失败重试策略的调整。"
                "监控侧同步新增了灰度期间的错误率看板，值班同学可以在五分钟内定位到异常接口。"
                "联调阶段发现的三个边界条件也都写进了回归用例，避免同类问题在后续版本重复出现。\n\n"
                "## 我们怎么判断\n\n"
                "复盘以工单、测试记录和发布日志为依据。**关键判断是先修正进入条件，再增加提醒。**"
                "原因很直接：同一问题连续出现时，提醒只能让人更忙，明确的进入条件才能减少返工。"
                "对照上一轮复盘的三条行动项，两条已经闭环，一条因为依赖外部接口延期，需要在下一轮重新排期。"
                "所有判断都记录了工单编号和日志截取位置，方便后续新成员直接按图核验。"
                "对于延期的一项，团队约定在外部接口恢复后一周内补做灰度验证，并把结果写回同一份复盘记录。\n\n"
                "## 下一步怎么做\n\n"
                "- 待补充\n"
            )
            yield document

    events = await collect_events(
        CreationAgentLoop(StubbornPlaceholderService()).run(
            user_message="请写一份完整的项目复盘文档",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enable_rag=False),
        )
    )

    assert events[-1]["type"] == "run.completed"
    # 润色期间只推节流进度事件，不能把全文 chunk 流式推给页面
    polish_deltas = [
        event
        for event in events
        if event["type"] == "document.patch.delta"
        and event["actor"]["id"] == "detail_polish_agent"
    ]
    assert polish_deltas
    for event in polish_deltas:
        assert not event["data"].get("content")
        assert event["data"].get("progress_chars", 0) > 0
    # 全文只在 document.patch.applied 中一次性局部生效
    applied = [
        event
        for event in events
        if event["type"] == "document.patch.applied"
        and event["actor"]["id"] == "detail_polish_agent"
    ]
    assert applied
    assert applied[0]["data"].get("patch", {}).get("summary")


@pytest.mark.asyncio
async def test_selected_skill_quality_gate_does_not_activate_unrelated_agents():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="把复盘写得自然一些",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[
            {
                "id": "natural-retrospective",
                "title": "自然复盘表达 Skill",
                "writingGuidelines": ["用团队日常语言陈述判断", "避免模板化转折"],
                "executionSteps": [],
            }
        ],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-quality-skill",
        run_id="run-quality-skill",
    )
    resolve_planned(loop, state)
    state.environment["applied_skills"] = loop._match_skills(state)
    state.cursor = len(state.plan)
    state.environment["quality_issues"] = [
        {
            "code": "ai_style_signals",
            "severity": "soft",
            "agent_id": "anti_ai_style_agent",
            "required_capabilities": ["skill:voice_style"],
        }
    ]

    decision = loop._replan_after_feedback(
        state,
        {"id": "quality_review_agent"},
        status="completed",
    )

    assert decision["reason_code"] == "quality_gate_passed"
    assert [step["id"] for step in state.plan].count("quality_review_agent") == 1
    assert "anti_ai_style_agent" not in [step["id"] for step in state.plan]


def test_selected_skill_quality_gate_routes_only_declared_generic_checks():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="使用已选 Skill 整理文档",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[{
            "id": "selected-format-skill",
            "title": "已选格式 Skill",
            "executionSteps": [],
        }],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-selected-emphasis-quality",
        run_id="run-selected-emphasis-quality",
    )
    resolve_planned(loop, state)
    state.cursor = len(state.plan)
    state.environment["quality_issues"] = [
        {
            "code": "ai_style_signals",
            "severity": "soft",
            "agent_id": "anti_ai_style_agent",
            "required_capabilities": [],
        },
        {
            "code": "emphasis_needs_polish",
            "severity": "soft",
            "agent_id": "typography_polish_agent",
            "required_capabilities": [],
        },
    ]

    decision = loop._replan_after_feedback(
        state,
        {"id": "quality_review_agent"},
        status="completed",
    )

    assert decision["scheduled"] == [
        "typography_polish_agent",
        "quality_review_agent",
    ]
    assert state.plan[-1]["quality_issue_codes"] == [
        "data_query_result_incomplete",
        "emphasis_needs_polish",
        "unsupported_page_absence_claim",
        "subsection_requirements_incomplete",
    ]
    assert "anti_ai_style_agent" not in [step["id"] for step in state.plan]


def test_emphasis_quality_allows_plain_text_and_rejects_over_formatting():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="整理一份通用说明文档",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-emphasis-quality",
        run_id="run-emphasis-quality",
    )
    plain_document = "# 说明\n\n## 背景\n\n" + ("这是一段无需额外强调的完整说明。" * 45)
    criteria, issues = loop._inspect_document_quality(state, plain_document)
    assert criteria["emphasis_selective"] is True
    assert "emphasis_needs_polish" not in {item["code"] for item in issues}

    heavy_document = (
        "# 说明\n\n## 背景\n\n"
        + ("**这是一段被过度加粗的完整说明内容**，其余内容保持普通文本。" * 35)
    )
    criteria, issues = loop._inspect_document_quality(state, heavy_document)
    emphasis_issue = next(
        item for item in issues if item["code"] == "emphasis_needs_polish"
    )
    assert criteria["emphasis_selective"] is False
    assert emphasis_issue["agent_id"] == "typography_polish_agent"
    assert emphasis_issue["evidence"]["bold_character_ratio"] > 0.12


def test_quality_gate_rejects_page_absence_claim_after_interaction_failure():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="生成数据周报",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-page-absence-quality",
        run_id="run-page-absence-quality",
    )
    state.environment["webpage_scrapes"] = [
        {
            "source_id": 9,
            "status": "failed",
            "error_code": "INTERACTION_TARGET_NOT_FOUND",
        }
    ]
    for absence_claim in (
        "看板未展示输入输出 Token 字段。",
        "报表页面未显示 GPU 利用率。",
        "报表页面未提供项目成本字段。",
        "报表无法提供目标周期的数据。",
    ):
        document = (
            "# 周报\n\n## 数据\n\n"
            + absence_claim * 20
            + "\n\n## 结论\n\n本周数据统计完成。"
        )

        criteria, issues = loop._inspect_document_quality(state, document)

        assert criteria["page_absence_claim_supported"] is False
        assert any(
            item["code"] == "unsupported_page_absence_claim" for item in issues
        )


def test_emphasis_quality_requires_short_labels_for_narrative_fragments():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="整理一份通用进展文档",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-fragment-label-quality",
        run_id="run-fragment-label-quality",
    )
    context = "\n\n" + ("补充背景说明，确保质量检查适用于完整文档。" * 35)
    unlabeled = (
        "# 进展\n\n## 当前事项\n\n"
        "- 第一项工作已经完成方案评审，下一步进入小范围验证。\n"
        "- 第二项工作已经完成性能测试，下一步补充观测指标。"
        + context
    )
    criteria, issues = loop._inspect_document_quality(state, unlabeled)
    emphasis_issue = next(
        item for item in issues if item["code"] == "emphasis_needs_polish"
    )
    assert criteria["emphasis_selective"] is False
    assert emphasis_issue["evidence"]["narrative_fragment_count"] == 2
    assert emphasis_issue["evidence"]["missing_label_count"] == 2

    labeled = (
        "# 进展\n\n## 当前事项\n\n"
        "- **方案评审：** 已完成方案评审，下一步进入小范围验证。\n"
        "- **性能测试：** 已完成性能测试，下一步补充观测指标。"
        + context
    )
    criteria, issues = loop._inspect_document_quality(state, labeled)
    assert criteria["emphasis_selective"] is True
    assert "emphasis_needs_polish" not in {item["code"] for item in issues}


def test_document_unify_restores_strict_skill_section_headings_by_order():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="生成文档",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-heading-restore",
        run_id="run-heading-restore",
    )
    state.environment["strict_skill_ids"] = ["skill-weekly"]
    state.environment["applied_skills"] = [
        {
            "id": "skill-weekly",
            "execution_steps": [
                {"title": "本周进展"},
                {"title": "后续计划"},
            ],
        }
    ]
    restored, count = loop._restore_strict_skill_section_headings(
        state,
        "# 周报\n\n## 进展概览\n\n正文\n\n## 下一步\n\n正文",
    )
    assert count == 2
    assert "## 本周进展" in restored
    assert "## 后续计划" in restored
    assert "## 进展概览" not in restored


def test_quality_review_detects_observable_ai_style_signals():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="写一份运营方案",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=()),
        model_mode="local",
        session_id="session-ai-style",
        run_id="run-ai-style",
    )
    repeated = (
        "首先，值得注意的是，我们需要关注“核心能力”。其次，不难发现，"
        "“协同机制”具有重要价值。此外，我们还要建设“增长飞轮”。"
    )
    document = (
        "# 运营方案\n\n## 背景\n\n"
        + repeated * 3
        + "\n\n## 执行\n\n"
        + repeated * 3
        + "\n\n## 验证\n\n通过真实结果复核每项动作。"
    )

    criteria, issues = loop._inspect_document_quality(state, document)

    assert criteria["natural_expression"] is False
    style_issue = next(item for item in issues if item["code"] == "ai_style_signals")
    assert style_issue["agent_id"] == "anti_ai_style_agent"
    assert style_issue["evidence"]["decorative_quote_pairs"] >= 5


@pytest.mark.asyncio
async def test_quality_loop_runs_anti_ai_polisher_and_rechecks_the_result():
    class NaturalRewriteService(FakeCreationService):
        async def stream_agent_document(self, **kwargs):
            if "去 AI 味 Agent" in kwargs["system_prompt"]:
                yield """# 项目复盘

## 发生了什么

团队在两周内完成了需求拆分、接口联调和灰度发布。联调阶段暴露出三个边界条件，负责人当天补充了用例，第二天完成回归。这个过程没有改变发布目标，但让验收口径从口头约定变成了可重复检查的清单。

## 我们怎么判断

复盘以工单、测试记录和发布日志为依据。**关键判断是先修正进入条件，再增加提醒。** 原因很直接：同一问题连续出现时，提醒只能让人更忙，明确的进入条件才能减少返工。没有证据支持的猜测继续留在核验清单中。

## 下一步怎么做

产品负责人本周补齐异常路径，研发负责人把边界用例加入回归集，发布负责人在下一次灰度前核对清单。下轮复盘只看三个结果：异常是否提前暴露、返工是否减少、责任人能否根据记录直接接手。
"""
                return
            repeated = (
                "首先，值得注意的是，我们需要关注“核心能力”。其次，不难发现，"
                "“协同机制”具有重要价值。此外，我们还要建设“增长飞轮”。"
            )
            yield (
                "# 项目复盘\n\n## 发生了什么\n\n"
                + repeated * 3
                + "\n\n## 我们怎么判断\n\n"
                + repeated * 3
                + "\n\n## 下一步怎么做\n\n"
                + repeated * 2
                + " **下一步由负责人复核结果。**"
            )

    events = await collect_events(
        CreationAgentLoop(NaturalRewriteService()).run(
            user_message="请写一份完整的项目复盘",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enable_rag=False),
        )
    )

    assert events[-1]["type"] == "run.completed"
    assert any(
        event["type"] == "agent.completed"
        and event["actor"]["id"] == "anti_ai_style_agent"
        for event in events
    )
    assert sum(
        event["type"] == "agent.completed"
        and event["actor"]["id"] == "quality_review_agent"
        for event in events
    ) == 2
    final_document = events[-1]["data"]["document"]
    assert "值得注意的是" not in final_document
    assert "关键判断" in final_document


@pytest.mark.asyncio
async def test_harness_replans_during_the_run_from_fresh_data_feedback():
    service = FakeCreationService()
    service.data_results = [
        {
            "source_id": 2,
            "source_kind": "work_memory",
            "source_url": None,
            "refresh_required": False,
            "can_use": True,
            "content_excerpt": "本周完成需求 12 项",
            "structured_data": {"metric_statements": ["本周完成需求 12 项"]},
        }
    ]

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="生成本周项目周报，并分析核心指标变化",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enabled_tools=()),
        )
    )

    decisions = [event for event in events if event["type"] == "harness.decision"]
    assert decisions[0]["data"]["scheduled"] == ["data_analysis_agent"]
    assert not any(
        (event.get("actor") or {}).get("id") == "webpage_scrape" for event in events
    )
    assert any(
        (event.get("actor") or {}).get("id") == "data_analysis_agent"
        and event["type"] == "agent.completed"
        for event in events
    )
    assert events[-1]["type"] == "run.completed"


@pytest.mark.asyncio
async def test_webpage_scrape_streams_on_demand_screenshot_preview_metadata():
    service = FakeCreationService()
    evidence = {
        "id": "evidence-preview",
        "image_url": "/api/creation/evidence/evidence-preview/image",
        "validation_status": "verified",
        "captured_at": 1_720_000_000_000,
        "source_url": "https://bi.example.com/report",
        "validation": {
            "verified_claims": [
                {"statement": "本周订单 1200", "label": "订单", "value": "1200"}
            ]
        },
    }
    report = {
        "source_id": 7,
        "source_kind": "report_url",
        "source_url": "https://bi.example.com/report",
        "title": "经营看板",
        "refresh_required": True,
        "can_use": False,
    }
    service.data_results = [report]
    service.scrape_outcome = {
        "scrapes": [
            {
                "source_id": 7,
                "status": "completed",
                "title": "经营看板",
                "browser": "chrome",
                "interaction_mode": "temporary_foreground_window",
                "focus_policy": "allow_once",
                "focus_takeover_count": 1,
                "evidence": evidence,
            }
        ],
        "refreshed_data": [
            {
                **report,
                "refresh_required": False,
                "can_use": True,
                "creation_evidence": evidence,
            }
        ],
    }

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="生成本周经营周报，并分析核心指标变化",
            current_document="",
            conversation=[],
            selected_skills=[
                {
                    "id": "weekly-report",
                    "title": "经营周报 Skill",
                    "summary": "生成经营周报",
                    "executionSteps": [
                        {
                            "id": "refresh-report",
                            "title": "刷新经营数据",
                            "objective": "取得本周实时经营指标",
                            "output": "核验后的经营指标",
                            "agents": [],
                            "skills": [],
                            "tools": ["data_search", "webpage_scrape"],
                            "retainWebpageScreenshot": True,
                        }
                    ],
                }
            ],
            options=CreationOptions(enabled_tools=()),
        )
    )

    started = next(event for event in events if event["type"] == "browser.preview.started")
    completed = next(
        event for event in events if event["type"] == "browser.preview.completed"
    )
    preview = started["data"]["previews"][0]
    assert preview["source_id"] == 7
    assert preview["title"] == "经营看板"
    assert preview["image_url"].endswith(f"/{preview['id']}/image")
    assert "source_url" not in preview
    assert service.scrape_kwargs["preview_ids"] == {7: preview["id"]}
    assert service.scrape_kwargs["retain_screenshot"] is True
    assert completed["data"]["previews"][0]["image_url"] == evidence["image_url"]
    assert completed["data"]["previews"][0]["interaction_mode"] == "temporary_foreground_window"
    assert completed["data"]["previews"][0]["focus_takeover_count"] == 1


@pytest.mark.asyncio
async def test_webpage_scrape_defaults_to_silent_structured_collection():
    service = FakeCreationService()
    report = {
        "source_id": 9,
        "source_kind": "report_url",
        "source_url": "https://bi.example.com/report",
        "title": "经营看板",
        "refresh_required": True,
        "can_use": False,
    }
    structured_evidence = {
        "validation_status": "verified",
        "evidence_kind": "structured_page",
        "validation": {
            "verified_claims": [
                {"statement": "本周订单 1200", "label": "订单", "value": "1200"}
            ]
        },
    }
    service.data_results = [report]
    service.scrape_outcome = {
        "scrapes": [
            {
                "source_id": 9,
                "status": "completed",
                "title": "经营看板",
                "browser": "chrome",
                "interaction_mode": "background_tab",
                "focus_policy": "never",
                "focus_takeover_count": 0,
                "evidence": structured_evidence,
            }
        ],
        "refreshed_data": [
            {
                **report,
                "refresh_required": False,
                "can_use": True,
                "creation_evidence": structured_evidence,
            }
        ],
    }

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="生成本周经营周报，并分析核心指标变化",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enabled_tools=()),
        )
    )

    assert not any(event["type"].startswith("browser.preview.") for event in events)
    assert service.scrape_kwargs["retain_screenshot"] is False
    assert service.scrape_kwargs["preview_ids"] == {}
    completed = next(
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "webpage_scrape"
    )
    assert "全程在后台读取" not in completed["summary"]
    assert completed["data"]["sources"][0]["focus_takeover_count"] == 0


@pytest.mark.asyncio
async def test_webpage_scrape_reports_foreground_fallback_without_screenshot():
    service = FakeCreationService()
    report = {
        "source_id": 10,
        "source_kind": "report_url",
        "source_url": "https://bi.example.com/report",
        "title": "经营看板",
        "refresh_required": True,
        "can_use": False,
    }
    structured_evidence = {
        "validation_status": "verified",
        "evidence_kind": "structured_page",
        "validation": {
            "verified_claims": [
                {"statement": "年化总成本 1200万元", "label": "年化总成本", "value": "1200万元"}
            ]
        },
    }
    service.data_results = [report]
    service.scrape_outcome = {
        "scrapes": [
            {
                "source_id": 10,
                "status": "completed",
                "title": "经营看板",
                "browser": "chrome",
                "interaction_mode": "temporary_foreground_window",
                "focus_policy": "allow_once",
                "focus_takeover_count": 1,
                "collection_attempt": "foreground_fallback",
                "evidence": structured_evidence,
            }
        ],
        "refreshed_data": [
            {
                **report,
                "refresh_required": False,
                "can_use": True,
                "creation_evidence": structured_evidence,
            }
        ],
    }

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="生成本周经营周报，并分析最新成本",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enabled_tools=()),
        )
    )

    assert not any(event["type"].startswith("browser.preview.") for event in events)
    completed = next(
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "webpage_scrape"
    )
    assert "一次性浏览器会话完成即时取数" in completed["summary"]
    assert "未保留截图" in completed["summary"]
    assert completed["data"]["sources"][0]["collection_attempt"] == "foreground_fallback"


@pytest.mark.asyncio
async def test_webpage_scrape_reports_focus_gate_instead_of_structure_rejection():
    service = FakeCreationService()
    report = {
        "source_id": 11,
        "source_kind": "report_url",
        "source_url": "https://bi.example.com/report",
        "title": "经营看板",
        "refresh_required": True,
        "can_use": False,
    }
    service.data_results = [report]
    service.scrape_outcome = {
        "scrapes": [
            {
                "source_id": 11,
                "status": "failed",
                "error_code": "FOCUS_POLICY_BLOCKED",
                "collection_attempt": "foreground_fallback",
            }
        ],
        "refreshed_data": [
            {
                **report,
                "freshness_class": "unverified",
                "evidence_status": "failed",
                "unavailable_reason": "refresh_failed",
            }
        ],
    }

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="生成本周经营周报，并分析最新成本",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enabled_tools=()),
        )
    )

    completed = next(
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "webpage_scrape"
    )
    assert completed["status"] == "warning"
    assert "需要前台操作" in completed["summary"]
    assert "结构校验" not in completed["summary"]


@pytest.mark.asyncio
async def test_webpage_scrape_without_verified_metrics_emits_warning_status():
    service = FakeCreationService()
    report = {
        "source_id": 12,
        "source_kind": "report_url",
        "source_url": "https://bi.example.com/token-report",
        "title": "Token 数据报表",
        "refresh_required": True,
        "can_use": False,
    }
    service.data_results = [report]
    service.scrape_outcome = {
        "scrapes": [
            {
                "source_id": 12,
                "status": "rejected",
                "title": "Token 数据报表",
                "validation_reason": "no_verified_metric",
                "verified_claim_count": 0,
            }
        ],
        "refreshed_data": [
            {
                **report,
                "freshness_class": "unverified",
                "evidence_status": "rejected",
                "unavailable_reason": "validation_failed",
            }
        ],
    }

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="生成本周 Token 数据摘要",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enabled_tools=()),
        )
    )

    completed = next(
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "webpage_scrape"
    )
    assert completed["status"] == "warning"
    assert "暂未取得与任务指标一致的即时数据" in completed["summary"]
    assert "页面结构校验" not in completed["summary"]
    assert completed["data"]["rejected_sources"] == [
        {
            "source_id": 12,
            "title": "Token 数据报表",
            "url": "https://bi.example.com/token-report",
        }
    ]


@pytest.mark.asyncio
async def test_webpage_scrape_partial_success_lists_rejected_source_details():
    service = FakeCreationService()
    reports = [
        {
            "source_id": source_id,
            "source_kind": "report_url",
            "source_url": f"https://bi.example.com/token-report/{source_id}",
            "title": title,
            "refresh_required": True,
            "can_use": False,
        }
        for source_id, title in ((31, "Token 周报"), (32, "Token 月报"))
    ]
    service.data_results = reports
    service.scrape_outcome = {
        "scrapes": [
            {"source_id": 31, "status": "completed", "title": "Token 周报"},
            {
                "source_id": 32,
                "status": "rejected",
                "title": "Token 月报",
                "validation_reason": "no_verified_metric",
            },
        ],
        "refreshed_data": [
            {**reports[0], "can_use": True, "refresh_required": False},
            {
                **reports[1],
                "freshness_class": "unverified",
                "evidence_status": "rejected",
                "unavailable_reason": "validation_failed",
            },
        ],
    }

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="生成本周 Token 数据摘要",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enabled_tools=()),
        )
    )

    completed = next(
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "webpage_scrape"
    )
    assert "1 个来源暂未取得目标指标，未采用" in completed["summary"]
    assert "全程在后台读取" not in completed["summary"]
    assert completed["data"]["rejected_sources"] == [
        {
            "source_id": 32,
            "title": "Token 月报",
            "url": "https://bi.example.com/token-report/32",
        }
    ]


@pytest.mark.asyncio
async def test_webpage_scrape_partial_success_explains_period_mismatch_readably():
    service = FakeCreationService()
    reports = [
        {
            "source_id": source_id,
            "source_kind": "report_url",
            "source_url": f"https://bi.example.com/report/{source_id}",
            "title": f"报表 {source_id}",
            "refresh_required": True,
            "can_use": False,
        }
        for source_id in (21, 22)
    ]
    service.data_results = reports
    service.scrape_outcome = {
        "scrapes": [
            {"source_id": 21, "status": "completed", "title": "报表 21"},
            {
                "source_id": 22,
                "status": "failed",
                "title": "报表 22",
                "error_code": "SCRAPE_PERIOD_MISMATCH",
            },
        ],
        "refreshed_data": [
            {**reports[0], "can_use": True, "refresh_required": False},
            {**reports[1], "can_use": False, "unavailable_reason": "refresh_failed"},
        ],
    }

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="生成上周经营周报",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enabled_tools=()),
        )
    )

    completed = next(
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "webpage_scrape"
    )
    assert completed["status"] == "warning"
    assert "采用其中 1 个来源" in completed["summary"]
    assert "展示周期与任务周期不一致" in completed["summary"]
    assert "页面结构校验" not in completed["summary"]


@pytest.mark.asyncio
async def test_webpage_scrape_empty_body_reports_collection_failure_not_validation():
    service = FakeCreationService()
    report = {
        "source_id": 13,
        "source_kind": "report_url",
        "source_url": "https://bi.example.com/gpu-report",
        "title": "GPU 数据报表",
        "refresh_required": True,
        "can_use": False,
    }
    service.data_results = [report]
    service.scrape_outcome = {
        "scrapes": [{
            "source_id": 13,
            "status": "failed",
            "error_code": "SCRAPE_EMPTY",
        }],
        "refreshed_data": [{
            **report,
            "can_use": False,
            "unavailable_reason": "refresh_failed",
        }],
    }

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="生成本周 GPU 成本周报",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enabled_tools=()),
        )
    )

    completed = next(
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "webpage_scrape"
    )
    assert completed["status"] == "warning"
    assert "暂未展示可读取的数据" in completed["summary"]
    assert "结构校验" not in completed["summary"]


@pytest.mark.asyncio
async def test_webpage_scrape_storage_failure_reports_collection_failure_not_validation():
    service = FakeCreationService()
    report = {
        "source_id": 14,
        "source_kind": "report_url",
        "source_url": "https://bi.example.com/gpu-report",
        "title": "GPU 数据报表",
        "refresh_required": True,
        "can_use": False,
    }
    service.data_results = [report]
    service.scrape_outcome = {
        "scrapes": [{
            "source_id": 14,
            "status": "failed",
            "error_code": "SCRAPE_FAILED",
            "collection_attempt": "extension_background",
        }],
        "refreshed_data": [{
            **report,
            "can_use": False,
            "unavailable_reason": "refresh_failed",
        }],
    }

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="生成本周 GPU 成本周报",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enabled_tools=()),
        )
    )

    completed = next(
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "webpage_scrape"
    )
    assert completed["status"] == "warning"
    assert "本次未完成刷新" in completed["summary"]
    assert "结构校验" not in completed["summary"]


@pytest.mark.asyncio
async def test_webpage_scrape_unresponsive_extension_reports_task_claim_failure():
    service = FakeCreationService()
    report = {
        "source_id": 15,
        "source_kind": "report_url",
        "source_url": "https://bi.example.com/gpu-report",
        "title": "GPU 数据报表",
        "refresh_required": True,
        "can_use": False,
    }
    service.data_results = [report]
    service.scrape_outcome = {
        "scrapes": [{
            "source_id": 15,
            "status": "failed",
            "error_code": "BROWSER_EXTENSION_UNRESPONSIVE",
            "collection_attempt": "extension_background",
        }],
        "refreshed_data": [{
            **report,
            "can_use": False,
            "unavailable_reason": "refresh_failed",
        }],
    }

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="生成本周 GPU 成本周报",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enabled_tools=()),
        )
    )

    completed = next(
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "webpage_scrape"
    )
    assert completed["status"] == "warning"
    assert "暂未开始后台读取" in completed["summary"]
    assert "后台页面读取超时" not in completed["summary"]


def test_primary_skill_workflow_drives_agent_tool_order_and_step_context():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="为管理层写一份市场进入方案",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[
            {
                "id": "market-entry-skill",
                "title": "市场进入方案 Skill",
                "summary": "把行业事实、数据判断和方案取舍组织成决策材料。",
                "skillDescription": {
                    "purpose": "帮助决策者形成可验证的市场进入方案。",
                    "documentTypes": ["市场进入方案"],
                    "problems": ["判断机会、约束与进入路径"],
                    "domains": ["市场战略"],
                    "deliverables": ["可评审的市场进入方案"],
                },
                "executionSteps": [
                    {
                        "id": "research-market",
                        "title": "调研市场",
                        "objective": "核对行业现状与趋势。",
                        "output": "带来源的行业事实",
                        "agents": ["industry_research_agent"],
                        "skills": ["evidence-brief"],
                        "tools": ["internet_search", "github_search"],
                    },
                    {
                        "id": "analyze-evidence",
                        "title": "分析证据",
                        "objective": "形成数据判断并标记证据缺口。",
                        "output": "数据结论和待核验项",
                        "agents": ["data_analysis_agent"],
                        "skills": [],
                        "tools": [],
                    },
                    {
                        "id": "design-entry",
                        "title": "设计进入方案",
                        "objective": (
                            "把约束与结论转成进入路径，至少形成 3 个子章节，"
                            "每个子章节正文不少于 80 字。"
                        ),
                        "output": "方案结构与关键取舍",
                        "agents": [
                            "solution_design_agent",
                            "document_writer_agent",
                            "quality_review_agent",
                        ],
                        "skills": [],
                        "tools": [],
                    },
                ],
            }
        ],
        options=CreationOptions(enabled_tools=("internet_search",)),
        model_mode="local",
        session_id="session-skill-workflow",
        run_id="run-skill-workflow",
    )

    skill_plan = resolve_planned(loop, state)
    assert [step["id"] for step in skill_plan] == [
        "creation_main_agent",
        "market-entry-skill",
        "internet_search",
        "industry_research_agent",
        "creation_main_agent",
        "data_analysis_agent",
        "creation_main_agent",
        "solution_design_agent",
        "document_writer_agent",
        "quality_review_agent",
        "market-entry-skill:design-entry",
        "document_unify_polisher",
        "quality_review_agent",
    ]
    assert skill_plan[-1]["quality_issue_codes"] == [
        "data_query_result_incomplete",
        "emphasis_needs_polish",
        "unsupported_page_absence_claim",
        "subsection_requirements_incomplete",
    ]
    assert "chapter_design_agent" not in [step["id"] for step in skill_plan]
    research_step = next(
        step for step in skill_plan if step["id"] == "industry_research_agent"
    )
    assert research_step["skill_step_id"] == "research-market"
    assert research_step["skill_step_skills"] == ["evidence-brief"]
    _, prompt = loop._model_prompts(state, research_step)
    assert "【当前 Skill 执行步骤】" in prompt
    assert "核对行业现状与趋势" in prompt
    assert "带来源的行业事实" in prompt
    assert "evidence-brief" in prompt
    assert "github_search" not in [step["id"] for step in skill_plan]
    design_step = next(
        step for step in skill_plan if step["id"] == "solution_design_agent"
    )
    assert design_step["skill_step_structure_requirements"] == {
        "minimum_subsections": 3,
        "minimum_subsection_chars": 80,
        "source_text": "把约束与结论转成进入路径，至少形成 3 个子章节，每个子章节正文不少于 80 字。",
    }
    system, prompt = loop._model_prompts(state, design_step)
    assert "至少 3 个三级或更深子章节" in system
    assert "每个子章节正文不少于 80 字" in system
    assert "'minimum_subsections': 3" in prompt


def test_skill_step_prompt_consumes_harness_tool_result_without_meta_evidence():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="请使用@GPU周报 Skill 生成本周周报",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[
            {
                "id": "gpu-weekly",
                "title": "GPU周报 Skill",
                "executionSteps": [
                    {
                        "id": "collect-meeting",
                        "title": "本周会议纪要",
                        "objective": "用@记忆搜索 Tool 获取本周会议纪要并总结。",
                        "output": "会议纪要列表",
                        "agents": [],
                        "skills": [],
                        "tools": ["memory_search"],
                    }
                ],
            }
        ],
        options=CreationOptions(enabled_tools=("memory_search",)),
        model_mode="local",
        session_id="session-tool-receipt",
        run_id="run-tool-receipt",
    )
    state.environment["tool_results"] = [
        {
            "tool_id": "memory_search",
            "status": "completed",
            "result_count": 3,
            "skill_step_id": "collect-meeting",
        }
    ]
    state.environment["references"] = [
        {"id": 1, "title": "本周会议纪要", "content": "已完成成本优化评审。"}
    ]
    plan = resolve_planned(loop, state)
    skill_step = next(step for step in plan if step["action"] == "skill_step")

    system, user = loop._model_prompts(state, skill_step)

    assert "Tool 已由 Harness 在你开始处理前执行" in system
    assert "不得声称工具列表缺少接口" in system
    assert "证据不足" in system
    assert "证据完备" in system
    assert "否则不要输出" in system
    assert "章节结构和字体格式也是当前步骤的交付质量" in system
    assert "- **短标签：** 事实、动作或结果" in system
    assert "最多两级的父子列表" in system
    assert "不得为了版式虚构父子关系" in system
    assert "Tool 执行回执" in user
    assert "'tool_id': 'memory_search'" in user
    assert "'status': 'completed'" in user
    assert "本周会议纪要" in user


def test_strict_skill_step_prompt_isolates_other_step_artifacts_and_receipts():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="请使用@GPU周报 Skill 生成本周周报",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[
            {
                "id": "gpu-weekly",
                "title": "GPU周报 Skill",
                "executionSteps": [
                    {
                        "id": "meeting",
                        "title": "本周会议纪要",
                        "objective": "获取会议纪要。",
                        "output": "会议纪要列表",
                        "agents": [],
                        "skills": [],
                        "tools": ["memory_search"],
                    },
                    {
                        "id": "aigc",
                        "title": "AIGC进度总结",
                        "objective": "获取 AIGC 项目进度。",
                        "output": "项目进度列表",
                        "agents": [],
                        "skills": [],
                        "tools": ["memory_search"],
                    },
                ],
            }
        ],
        options=CreationOptions(enabled_tools=("memory_search",)),
        model_mode="local",
        session_id="session-isolated-steps",
        run_id="run-isolated-steps",
    )
    plan = resolve_planned(loop, state)
    current_step = next(
        step
        for step in plan
        if step["action"] == "skill_step" and step.get("skill_step_id") == "aigc"
    )
    state.environment["completed_skill_steps"] = [
        {
            "skill_id": "gpu-weekly",
            "step_id": "meeting",
            "title": "本周会议纪要",
            "objective": "获取会议纪要。",
            "output": "会议纪要列表",
            "content": "会议纪要独立产物正文：已完成成本优化评审。",
        }
    ]
    state.environment["tool_results"] = [
        {
            "tool_id": "memory_search",
            "status": "completed",
            "result_count": 2,
            "skill_step_id": "meeting",
        },
        {
            "tool_id": "memory_search",
            "status": "completed",
            "result_count": 5,
            "skill_step_id": "aigc",
        },
    ]
    state.current_document = (
        "# GPU周报\n\n## 本周会议纪要\n\n会议纪要独立产物正文：已完成成本优化评审。"
    )

    _, user = loop._model_prompts(state, current_step)

    # 独立推理：其他步骤的产物正文与已拼接文档不得进入当前步骤环境。
    assert "会议纪要独立产物正文" not in user
    assert "现有完整文档" not in user
    assert "现有文档目录" not in user
    # 已完成步骤只保留标题，并明示步骤间独立推理。
    assert "已完成的 Skill 步骤（仅提供标题，步骤间独立推理）" in user
    assert "本周会议纪要" in user
    # Tool 回执按步骤过滤：只见当前步骤回执，不见其他步骤回执。
    assert "'result_count': 5" in user
    assert "'result_count': 2" not in user


def test_single_structured_step_skill_does_not_schedule_document_unify_polisher():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="请使用@单步 Skill 生成周报",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[
            {
                "id": "single-step",
                "title": "单步 Skill",
                "executionSteps": [
                    {
                        "id": "only",
                        "title": "唯一步骤",
                        "objective": "形成周报。",
                        "output": "周报正文",
                        "agents": [],
                        "skills": [],
                        "tools": ["memory_search"],
                    }
                ],
            }
        ],
        options=CreationOptions(enabled_tools=("memory_search",)),
        model_mode="local",
        session_id="session-single-step-skill",
        run_id="run-single-step-skill",
    )

    plan = resolve_planned(loop, state)

    # 只有一个独立推理产物时不需要合并润色，避免多余的大模型调用。
    assert "document_unify_polisher" not in {step["id"] for step in plan}


def test_strict_skill_document_keeps_gpu_and_token_steps_separate():
    loop = CreationAgentLoop(FakeCreationService())
    state = SimpleNamespace(
        environment={
            "strict_skill_ids": ["gpu-cost-weekly"],
            "applied_skills": [
                {"id": "gpu-cost-weekly", "name": "GPU成本优化周报创作法"}
            ],
            "completed_skill_steps": [
                {
                    "skill_id": "gpu-cost-weekly",
                    "step_id": "gpu-metrics",
                    "title": "GPU算力数据",
                    "objective": "获取 GPU 算力数据并形成独立表格。",
                    "output": "GPU算力数据表",
                    "content": (
                        "| GPU 指标 | 数值 |\n"
                        "| --- | ---: |\n"
                        "| 总卡数 | 1803.59 |\n\n"
                        "算力利用率为 76%。\n\n"
                        "## 本周结论\n\n这是未声明的总结。\n\n"
                        "## 风险阻塞\n\n这是未声明的风险分析。"
                    ),
                },
                {
                    "skill_id": "gpu-cost-weekly",
                    "step_id": "token-metrics",
                    "title": "Token用量数据",
                    "objective": "获取 Token 用量数据并形成独立表格。",
                    "output": "Token用量数据表",
                    "content": (
                        "| Token 指标 | 数值 |\n"
                        "| --- | ---: |\n"
                        "| 输入 Token | 12.4 亿 |\n\n"
                        "输出 Token 成本为 31 万元。\n\n"
                        "## 重点进展\n\n这是未声明的进展总结。\n\n"
                        "## 下周计划\n\n这是未声明的计划。"
                    ),
                },
            ],
        }
    )

    document = loop._assemble_strict_skill_document(state)

    assert document.startswith("# GPU成本优化周报")
    assert [
        line.removeprefix("## ")
        for line in document.splitlines()
        if line.startswith("## ")
    ] == ["GPU算力数据", "Token用量数据"]
    assert "本周结论" not in document
    assert "重点进展" not in document
    assert "风险阻塞" not in document
    assert "下周计划" not in document
    assert "未声明" not in document
    assert document.index("| GPU 指标 | 数值 |") < document.index("## Token用量数据")
    assert document.index("## Token用量数据") < document.index("| Token 指标 | 数值 |")


def test_strict_skill_document_preserves_internal_subheadings_without_expanding_workflow():
    loop = CreationAgentLoop(FakeCreationService())
    state = SimpleNamespace(
        environment={
            "strict_skill_ids": ["gpu-cost-weekly"],
            "applied_skills": [
                {
                    "id": "gpu-cost-weekly",
                    "name": "GPU成本优化周报创作法",
                    "execution_steps": [
                        {"id": "aigc", "title": "AIGC进度总结"},
                        {"id": "gpu", "title": "GPU算力数据"},
                    ],
                }
            ],
            "completed_skill_steps": [
                {
                    "skill_id": "gpu-cost-weekly",
                    "step_id": "aigc",
                    "title": "AIGC进度总结",
                    "objective": "总结本周 AIGC 项目进度并以列表展示。",
                    "output": "项目进度列表",
                    "content": (
                        "## AIGC进度总结\n\n"
                        "### 项目进展概览\n\n"
                        "- 推理性能优化已完成首轮验证。\n\n"
                        "## 下周计划\n\n这是未声明的扩展章节。\n\n"
                        "## GPU算力数据\n\n这是其它工作流步骤的内容。"
                    ),
                }
            ],
        }
    )

    document, audits = loop._assemble_strict_skill_document(
        state,
        include_audit=True,
    )

    assert "### 项目进展概览" in document
    assert "推理性能优化已完成首轮验证" in document
    assert "下周计划" not in document
    assert "其它工作流步骤的内容" not in document
    assert audits[0]["step_id"] == "aigc"
    assert audits[0]["source_chars"] > audits[0]["retained_chars"] > 0
    assert audits[0]["skipped_heading_count"] == 2
    assert audits[0]["preserved_subheading_count"] == 1
    assert audits[0]["recovered_from_empty"] is False


def test_strict_skill_document_recovers_text_when_generic_heading_would_empty_step():
    loop = CreationAgentLoop(FakeCreationService())
    state = SimpleNamespace(
        environment={
            "strict_skill_ids": ["weekly"],
            "applied_skills": [{"id": "weekly", "name": "周报技能"}],
            "completed_skill_steps": [
                {
                    "skill_id": "weekly",
                    "step_id": "aigc",
                    "title": "AIGC进度总结",
                    "objective": "总结项目状态。",
                    "output": "项目列表",
                    "content": "## 重点进展\n\n- 推理性能优化已完成首轮验证。",
                }
            ],
        }
    )

    document, audits = loop._assemble_strict_skill_document(
        state,
        include_audit=True,
    )

    assert "## 重点进展" not in document
    assert "推理性能优化已完成首轮验证" in document
    assert audits[0]["recovered_from_empty"] is True


def test_scoped_skill_writer_prompt_uses_current_step_data_and_forbids_expansion():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="请使用@GPU成本优化周报创作法生成周报",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[
            {
                "id": "gpu-cost-weekly",
                "title": "GPU成本优化周报创作法",
                "skillInstructions": "# GPU 成本周报\n\n只执行声明的步骤，不增加其它章节。",
                "executionSteps": [
                    {
                        "id": "gpu-metrics",
                        "title": "GPU算力数据",
                        "objective": "生成独立 GPU 表格。",
                        "output": "GPU算力数据表",
                        "agents": ["document_writer_agent"],
                        "skills": [],
                        "tools": [],
                    }
                ],
            }
        ],
        options=CreationOptions(),
        model_mode="local",
        session_id="session-strict-writer",
        run_id="run-strict-writer",
    )
    state.environment["data_results"] = [{"source_id": 1, "title": "历史 Token 表"}]
    state.environment["current_data_results"] = [
        {"source_id": 2, "title": "当前 GPU 表"}
    ]
    plan = resolve_planned(loop, state)
    apply_step = next(step for step in plan if step["action"] == "apply_skill")
    state.environment.setdefault("applied_skills", []).append(apply_step["skill"])
    writer_step = next(step for step in plan if step["id"] == "document_writer_agent")

    system, user = loop._model_prompts(state, writer_step)

    assert "execution_steps 是唯一流程和章节白名单" in system
    assert "不得把不同步骤的数据或表格合并" in system
    assert "不得自行添加“结论”“重点进展”“风险/阻塞”“下周计划”" in system
    assert "# GPU 成本周报" in user
    assert "当前 GPU 表" in user
    assert "历史 Token 表" not in user


def test_skill_step_prompt_uses_scoped_bounded_fact_view_for_large_dashboard_payloads():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="请按已安装 Skill 生成本周报表摘要",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(),
        model_mode="local",
        session_id="session-bounded-dashboard",
        run_id="run-bounded-dashboard",
    )
    previous_result = {
        "source_id": 1,
        "title": "上一步骤的大型报表",
        "source_kind": "report_url",
        "can_use": True,
        "structured_data": {"dom_content_text": "x" * 600000},
    }
    current_result = {
        "source_id": 2,
        "title": "当前步骤的实时报表",
        "source_kind": "report_url",
        "source_url": "https://bi.example.com/current",
        "can_use": True,
        "refresh_required": False,
        "structured_data": {
            "validation": "requested_metrics_verified",
            "verified_claims": [
                {
                    "claim_type": "metric",
                    "label": "当前请求指标",
                    "value": "123.45亿",
                    "statement": "当前请求指标 123.45亿",
                }
            ],
            "dom_content_text": "y" * 600000,
            "evidence_regions": [{"text": "z" * 10000}],
        },
        "history": [
            {"content_text": "历史阶段 100亿", "structured_data": {}}
        ],
    }
    state.environment["data_results"] = [previous_result, current_result]
    state.environment["current_data_results"] = [current_result]
    state.environment["webpage_scrapes"] = [
        {
            "source_id": 2,
            "status": "completed",
            "title": "当前步骤的实时报表",
            "evidence": {
                "id": "evidence-current",
                "validation_status": "verified",
                "validation": {
                    "reason": "requested_metrics_verified",
                    "verified_claims": [
                        {"statement": "duplicate" * 100000}
                    ],
                },
            },
        }
    ]
    state.environment["references"] = [
        {
            "id": index,
            "title": f"参考 {index}",
            "content": "r" * 5000,
        }
        for index in range(30)
    ]
    step = {
        "id": "creation_main_agent:metrics",
        "name": "创作 Agent · 实时指标",
        "action": "skill_step",
        "skill_id": "weekly-report",
        "skill_step_id": "metrics",
        "skill_step_title": "实时指标",
        "skill_step_objective": "使用当前页面中已校验的指标",
        "skill_step_output": "指标表格",
        "skill_step_skills": [],
    }

    _, user = loop._model_prompts(state, step)

    assert len(user) < 65000
    assert "当前步骤的实时报表" in user
    assert "当前请求指标" in user
    assert "123.45亿" in user
    assert "上一步骤的大型报表" not in user
    assert "x" * 1000 not in user
    assert "y" * 1000 not in user
    assert "duplicate" * 100 not in user
    assert len(state.environment["data_results"][0]["structured_data"]["dom_content_text"]) == 600000


def test_skill_workflow_reuses_resources_and_executes_logic_only_step():
    loop = CreationAgentLoop(FakeCreationService())
    skill = {
        "id": "research-review-skill",
        "title": "调研复核 Skill",
        "executionSteps": [
            {
                "id": "initial-research",
                "title": "开展初查",
                "objective": "收集第一轮行业事实。",
                "output": "初查事实清单",
                "agents": ["industry_research_agent"],
                "skills": ["evidence-brief"],
                "tools": ["internet_search"],
            },
            {
                "id": "source-review",
                "title": "复核来源",
                "objective": "用第二轮检索交叉核验关键结论。",
                "output": "复核后的事实与冲突项",
                "agents": ["industry_research_agent"],
                "skills": ["evidence-brief"],
                "tools": ["internet_search"],
            },
            {
                "id": "set-boundary",
                "title": "确认边界",
                "objective": "明确哪些结论可以进入成稿。",
                "output": "可写事实边界",
                "agents": [],
                "skills": [],
                "tools": [],
            },
        ],
    }
    state = loop._new_state(
        user_message="写一份行业研究材料",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[skill],
        options=CreationOptions(enabled_tools=("internet_search",)),
        model_mode="local",
        session_id="session-repeat-resources",
        run_id="run-repeat-resources",
    )

    repeat_plan = resolve_planned(loop, state)
    assert [
        step["skill_step_id"]
        for step in repeat_plan
        if step["id"] == "internet_search"
    ] == ["initial-research", "source-review"]
    assert [
        step["skill_step_id"]
        for step in repeat_plan
        if step["id"] == "industry_research_agent"
    ] == ["initial-research", "source-review"]
    logic_step = next(
        step for step in repeat_plan if step.get("skill_step_id") == "set-boundary"
    )
    assert logic_step["action"] == "skill_step"
    query = loop._step_context_query(
        state,
        next(
            step
            for step in repeat_plan
            if step["id"] == "internet_search"
            and step["skill_step_id"] == "source-review"
        ),
    )
    assert "交叉核验关键结论" in query
    assert "evidence-brief" in query


def test_selected_skill_without_structured_steps_still_never_adds_hidden_agents():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="使用明确选中的 Skill 完成创作",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[{
            "id": "imported-skill",
            "title": "导入 Skill",
            "summary": "遵循导入 Skill 自身规则完成创作。",
            "executionSteps": [],
        }],
        options=CreationOptions(enabled_tools=("memory_search", "data_search")),
        model_mode="local",
        session_id="session-empty-skill-workflow",
        run_id="run-empty-skill-workflow",
    )

    plan = resolve_planned(
        loop,
        state,
        {"tools": ["memory_search", "data_search"], "agents": ["solution_design_agent"]},
    )

    assert [step["id"] for step in plan] == [
        "creation_main_agent",
        "imported-skill",
        "creation_main_agent",
        "quality_review_agent",
    ]
    assert plan[-1]["quality_issue_codes"] == [
        "data_query_result_incomplete",
        "emphasis_needs_polish",
        "unsupported_page_absence_claim",
        "subsection_requirements_incomplete",
    ]
    assert plan[-2]["action"] == "skill_step"
    assert plan[-2]["skill_step_id"] == "execute-skill"


def test_explicit_skill_mention_drops_legacy_automatic_template_expansion():
    loop = CreationAgentLoop(FakeCreationService())
    primary_steps = [
        {
            "id": "meeting",
            "title": "本周大模型性能成本优化周会会议纪要",
            "objective": "获取会议纪要。",
            "output": "会议纪要列表",
            "agents": [],
            "skills": [],
            "tools": ["memory_search"],
        },
        {
            "id": "aigc",
            "title": "AIGC进度总结",
            "objective": "获取 AIGC 进度。",
            "output": "进度列表",
            "agents": [],
            "skills": [],
            "tools": ["memory_search"],
        },
        {
            "id": "gpu",
            "title": "GPU算力数据",
            "objective": "生成 GPU 数据表。",
            "output": "GPU 数据表",
            "agents": [],
            "skills": [],
            "tools": ["data_search"],
        },
        {
            "id": "token",
            "title": "Token数据",
            "objective": "生成 Token 数据表。",
            "output": "Token 数据表",
            "agents": [],
            "skills": [],
            "tools": ["data_search"],
        },
    ]
    state = loop._new_state(
        user_message="请使用@GPU成本优化周报模板 创作下本周的周报",
        root_request=None,
        current_document="",
        conversation=[],
        # 模拟旧客户端错误地把两个自动匹配模板与 @ Skill 一起发给 Agent。
        selected_skills=[
            {
                "id": "gpu-weekly",
                "title": "GPU成本优化周报模板",
                "executionSteps": primary_steps,
            },
            {
                "id": "generic-weekly",
                "title": "通用工作周报模板",
                "executionSteps": [
                    {
                        "id": "risks",
                        "title": "形成风险与下周计划",
                        "objective": "补充风险和计划。",
                        "agents": ["solution_design_agent"],
                        "skills": [],
                        "tools": [],
                    }
                ],
            },
            {
                "id": "stage-update",
                "title": "项目阶段汇报模板",
                "executionSteps": [
                    {
                        "id": "stage",
                        "title": "撰写阶段汇报",
                        "objective": "撰写完整阶段汇报。",
                        "agents": ["document_writer_agent"],
                        "skills": [],
                        "tools": [],
                    }
                ],
            },
        ],
        options=CreationOptions(enabled_tools=("memory_search", "data_search")),
        model_mode="local",
        session_id="session-explicit-skill-only",
        run_id="run-explicit-skill-only",
    )

    plan = resolve_planned(loop, state)

    assert [step["id"] for step in plan if step["action"] == "apply_skill"] == [
        "gpu-weekly"
    ]
    assert [
        step.get("skill_step_id")
        for step in plan
        if step.get("action") == "skill_step"
    ] == ["meeting", "aigc", "gpu", "token"]
    # 四个独立推理步骤后先整合全文，再执行通用数据完整性与强调检查。
    assert len(plan) == 12
    assert plan[-2]["id"] == "document_unify_polisher"
    assert plan[-1]["id"] == "quality_review_agent"
    assert plan[-1]["quality_issue_codes"] == [
        "data_query_result_incomplete",
        "emphasis_needs_polish",
        "unsupported_page_absence_claim",
        "subsection_requirements_incomplete",
    ]
    unify_system, _ = loop._model_prompts(state, plan[-2])
    assert "只处理全文结构、术语和表达一致性" in unify_system
    assert "只处理质检分派给你的问题" not in unify_system
    assert "保持 Skill 声明的二级章节标题、数量与顺序不变" in unify_system
    assert "最多两级的父子列表" in unify_system
    assert "只对关键判断、关键数字、风险和行动项的最短完整词组加粗" in unify_system
    assert "不得虚构归属" in unify_system
    assert {step["id"] for step in plan}.isdisjoint(
        {
            "generic-weekly",
            "stage-update",
            "solution_design_agent",
            "document_writer_agent",
        }
    )


def test_support_skill_is_loaded_without_expanding_its_workflow():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="请使用@主周报 Skill 生成周报",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[
            {
                "id": "primary-weekly",
                "title": "主周报 Skill",
                "workflowRole": "primary",
                "executionSteps": [
                    {
                        "id": "facts",
                        "title": "事实列表",
                        "objective": "整理事实。",
                        "agents": [],
                        "skills": ["evidence-support"],
                        "tools": [],
                    }
                ],
            },
            {
                "id": "evidence-support",
                "title": "证据辅助 Skill",
                "workflowRole": "support",
                "executionSteps": [
                    {
                        "id": "forbidden-risk-plan",
                        "title": "风险与下周计划",
                        "objective": "生成另一套完整周报。",
                        "agents": ["document_writer_agent"],
                        "skills": [],
                        "tools": [],
                    }
                ],
            },
        ],
        options=CreationOptions(),
        model_mode="local",
        session_id="session-support-skill",
        run_id="run-support-skill",
    )

    plan = resolve_planned(loop, state)

    assert [step["id"] for step in plan if step["action"] == "apply_skill"] == [
        "primary-weekly",
        "evidence-support",
    ]
    assert [
        step.get("skill_step_id")
        for step in plan
        if step.get("action") == "skill_step"
    ] == ["facts"]
    assert "document_writer_agent" not in {step["id"] for step in plan}
    assert state.environment["strict_skill_ids"] == ["primary-weekly"]


@pytest.mark.asyncio
async def test_reference_a889436d_four_step_workflow_order_and_output_structure():
    class ReferenceOutputService(FakeCreationService):
        async def stream_agent_document(self, **kwargs):
            system_prompt = str(kwargs.get("system_prompt") or "")
            if "全文整合润色" in system_prompt:
                # 全文整合润色只做有边界的统一；测试中以透传组装稿验证
                # “独立产物组装 -> 最终润色 -> 不被重组覆盖”的链路。
                prompt = str(kwargs.get("user_prompt") or "")
                marker = "现有完整文档：\n"
                if marker in prompt:
                    yield prompt.split(marker, 1)[1].strip()
                    return
            async for chunk in super().stream_agent_document(**kwargs):
                yield chunk

        async def run_specialist_agent(self, **kwargs):
            prompt = str(kwargs.get("user_prompt") or "")
            current_step = ""
            if "【当前 Skill 执行步骤】" in prompt and "步骤：" in prompt:
                current_step = prompt.split("步骤：", 1)[1].splitlines()[0].strip()
                if current_step == "本周大模型性能成本优化周会会议纪要":
                    return "\n".join(
                        f"- **会议事项 {index}：** 已核对本周成本优化动作、业务影响、责任边界与验证结果。"
                        for index in range(1, 6)
                    )
                if current_step == "AIGC进度总结":
                    return "\n".join(
                        f"- **AIGC 项目 {index}：** 本周完成阶段性交付、性能验证与应用场景复核，并记录下一里程碑。"
                        for index in range(1, 11)
                    )
            if current_step == "GPU算力数据":
                return (
                    "| 项目 | 业务线 | 卡数(X40) | 年化收益(万元) | 年化成本(万元) | ROI |\n"
                    "| --- | --- | ---: | ---: | ---: | ---: |\n"
                    + "\n".join(
                        f"| GPU项目{index} | AI基座 | {index * 12:.2f} | {index * 210:.1f} | {index * 80:.1f} | {index * 1.2:.2f}x |"
                        for index in range(1, 8)
                    )
                )
            if current_step == "Token数据":
                return (
                    "| 指标 | 统计周期 | 数值 |\n"
                    "| --- | --- | ---: |\n"
                    "| 独立部署输入Tokens | 2026-08-10 至 2026-08-16 | 10919.04亿 |\n"
                    "| 独立部署输出Tokens | 2026-08-10 至 2026-08-16 | 689.96亿 |\n"
                    "| 公共部署输入Tokens | 2026-08-10 至 2026-08-16 | 675.90亿 |\n"
                    "| 公共部署输出Tokens | 2026-08-10 至 2026-08-16 | 46.45亿 |\n"
                    "| 商业模型输入Tokens | 2026-08-10 至 2026-08-16 | 57.76亿 |\n"
                    "| 商业模型输出Tokens | 2026-08-10 至 2026-08-16 | 13.58亿 |"
                )
            return await super().run_specialist_agent(**kwargs)

    service = ReferenceOutputService()
    service.routing_decision = {
        "tools": ["memory_search", "data_search"],
        "agents": [],
    }
    service.data_results = [
        {
            "source_id": 1617,
            "title": "电商GPU信息平台总卡数（X40折算）",
            "source_kind": "work_memory",
            "source_url": "https://gpu.example.com/projects/usage",
            "refresh_required": False,
            "can_use": True,
            "content_excerpt": "总卡数 1803.59（按 X40 折算）",
        },
        {
            "source_id": 1584,
            "title": "电商GPU信息平台 - GPU项目用量管理",
            "source_kind": "report_url",
            "source_url": "https://gpu.example.com/projects/usage",
            "refresh_required": True,
            "can_use": False,
            "content_excerpt": None,
        },
    ]
    service.scrape_outcome = {
        "scrapes": [
            {
                "source_id": 1584,
                "status": "completed",
                "collector": "browser_attach",
                "collected_at": 1786271177838,
                "title": "电商GPU信息平台 - GPU项目用量管理",
                "url": "https://gpu.example.com/projects/usage",
                "verified_claim_count": 1,
                "evidence": {
                    "validation_status": "verified",
                    "image_url": "/api/creation/browser-previews/test/image",
                },
            }
        ],
        "refreshed_data": [
            service.data_results[0],
            {
                **service.data_results[1],
                "refresh_required": False,
                "can_use": True,
                "content_excerpt": "在用项目 102 个，总卡数 1803.59",
                "creation_evidence": {
                    "validation_status": "verified",
                    "image_url": "/api/creation/browser-previews/test/image",
                },
            },
        ],
    }
    skill = {
        "id": "creation-skill-gpu-cost-weekly-report",
        "title": "GPU成本优化周报创作法",
        "executionSteps": [
            {
                "id": "collect-inputs",
                "title": "本周大模型性能成本优化周会会议纪要",
                "objective": "获取本周会议纪要，并总结为列表。",
                "output": "会议纪要列表",
                "agents": [],
                "skills": [],
                "tools": ["memory_search"],
            },
            {
                "id": "summarize-progress",
                "title": "AIGC进度总结",
                "objective": "获取本周 AIGC 项目进度，并以列表展示。",
                "output": "项目进度列表",
                "agents": [],
                "skills": [],
                "tools": ["memory_search"],
            },
            {
                "id": "build-metrics-table",
                "title": "GPU算力数据",
                "objective": "获取电商GPU信息平台的最新算力、利用率和收益数据并形成表格。",
                "output": "GPU算力数据表",
                "agents": [],
                "skills": [],
                "tools": ["data_search"],
            },
            {
                "id": "token-metrics-table",
                "title": "Token数据",
                "objective": "获取 LangBridge 模型中心本周 Token 数据并形成独立表格。",
                "output": "Token数据表",
                "agents": [],
                "skills": [],
                "tools": ["data_search"],
            },
        ],
    }
    loop = CreationAgentLoop(service)
    state = loop._new_state(
        user_message="请使用@GPU成本优化周报创作法 创作下本周的周报",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[skill],
        options=CreationOptions(enabled_tools=("memory_search", "data_search")),
        model_mode="local",
        session_id="session-gpu-weekly",
        run_id="run-gpu-weekly",
    )

    plan = resolve_planned(loop, state, service.routing_decision)
    workflow_order = [
        (step.get("skill_step_id"), step["id"])
        for step in plan
        if step.get("skill_step_id")
    ]
    assert workflow_order == [
        ("collect-inputs", "memory_search"),
        ("collect-inputs", "creation_main_agent"),
        ("summarize-progress", "memory_search"),
        ("summarize-progress", "creation_main_agent"),
        ("build-metrics-table", "data_search"),
        ("build-metrics-table", "creation_main_agent"),
        ("token-metrics-table", "data_search"),
        ("token-metrics-table", "creation_main_agent"),
    ]
    assert {
        step["id"] for step in plan
    }.isdisjoint({
        "chapter_design_agent",
        "document_writer_agent",
        "data_analysis_agent",
        "webpage_scrape",
    })
    assert plan[-1]["id"] == "quality_review_agent"
    assert plan[-1]["quality_issue_codes"] == [
        "data_query_result_incomplete",
        "emphasis_needs_polish",
        "unsupported_page_absence_claim",
        "subsection_requirements_incomplete",
    ]
    data_tool_step = next(step for step in plan if step["id"] == "data_search")
    assert data_tool_step["skill_step_id"] == "build-metrics-table"
    assert data_tool_step["name"] == "GPU算力数据 · 数据检索 Tool"

    events = await collect_events(
        loop.run(
            user_message="请使用@GPU成本优化周报创作法 创作下本周的周报",
            current_document="",
            conversation=[],
            selected_skills=[skill],
            options=CreationOptions(enabled_tools=("memory_search", "data_search")),
        )
    )
    # a889436d 的真实记录为 45 个可见事件（旧记录无思考事件）；引入深度思考
    # 事件（意图 + 每次内容生成大模型调用各一对）与顶层阶段事件（四个 Skill
    # 步骤各一对 phase 事件）后同一流程约 71 个。保留少量实现波动空间，
    # 但不能再回到错误版本的 119 个事件和三套模板长链。
    assert len(events) <= 90
    # 顶层阶段：四个 Skill 步骤按顺序形成四个阶段，phase 事件成对包裹；
    # 最后追加全文整合润色，以及不改变业务结构的强调质量检查。
    phase_started = [event for event in events if event["type"] == "phase.started"]
    phase_completed = [
        event for event in events if event["type"] == "phase.completed"
    ]
    assert [event["data"]["phase_title"] for event in phase_started] == [
        "本周大模型性能成本优化周会会议纪要",
        "AIGC进度总结",
        "GPU算力数据",
        "Token数据",
        "全文整合润色",
        "质量审校",
    ]
    assert all(
        event["data"]["phase_kind"] == "skill_step"
        for event in phase_started[:-2]
    )
    assert all(
        event["data"]["phase_kind"] == "plan_step"
        for event in phase_started[-2:]
    )
    assert len(phase_started) == len(phase_completed) == 6
    # 阶段内的工具摘要要表达调用目的，而不只是结果数量。
    memory_summaries = [
        event["summary"]
        for event in events
        if event["type"] == "tool.completed"
        and event.get("actor", {}).get("id") == "memory_search"
    ]
    assert memory_summaries
    assert all(summary.startswith("检索「") for summary in memory_summaries)
    assert all("召回" in summary for summary in memory_summaries)
    completed_steps = [
        event["environment_patch"]["skill_step"]
        for event in events
        if isinstance(event.get("environment_patch", {}).get("skill_step"), dict)
        and "content" in event["environment_patch"]["skill_step"]
    ]
    assert [item["step_id"] for item in completed_steps] == [
        "collect-inputs",
        "summarize-progress",
        "build-metrics-table",
        "token-metrics-table",
    ]
    previews = [event for event in events if event["type"] == "document.preview"]
    assert previews
    assert {
        event["data"]["section_title"] for event in previews
    } == {
        "本周大模型性能成本优化周会会议纪要",
        "AIGC进度总结",
        "GPU算力数据",
        "Token数据",
    }
    assert any(
        "## AIGC进度总结" in event["data"]["content"]
        and "AIGC 项目" in event["data"]["content"]
        for event in previews
    )
    assembly_events = [
        event
        for event in events
        if event["type"] == "document.replaced"
        and event["data"].get("operation") == "strict_skill_workflow_assembly"
    ]
    assert len(assembly_events) == 4
    assert all(
        event["data"]["assembly_audit"]["retained_chars"] > 0
        for event in assembly_events
    )
    assert len(service.data_queries) == 2
    assert service.data_queries[0].startswith("当前步骤：GPU算力数据")
    assert service.data_queries[1].startswith("当前步骤：Token数据")
    assert service.reference_queries[1].startswith("当前步骤：AIGC进度总结")
    assert "AIGC 项目进度" in service.reference_queries[1]
    assert service.reference_queries[1].endswith(
        "整体创作背景：请使用@GPU成本优化周报创作法 创作下本周的周报"
    )
    assert "电商GPU信息平台" in service.data_queries[0]
    assert "LangBridge" in service.data_queries[1]
    assert "创作下本周的周报" not in service.data_queries[0]

    data_event = next(
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "data_search"
    )
    assert data_event["actor"]["name"] == "GPU算力数据 · 数据检索 Tool"
    assert data_event["data"]["skill_step_id"] == "build-metrics-table"
    assert {
        item["source_id"]
        for item in data_event["environment_patch"]["data_sources"]
    } == {1584, 1617}

    event_actor_ids = [event["actor"]["id"] for event in events]
    assert "webpage_scrape" in event_actor_ids
    assert "data_analysis_agent" not in event_actor_ids
    data_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "data_search"
    )
    scrape_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "tool.started"
        and event["actor"]["id"] == "webpage_scrape"
    )
    step_writer_index = next(
        index
        for index, event in enumerate(events)
        if index > scrape_index
        and event["type"] == "agent.started"
        and event["actor"]["id"] == "creation_main_agent"
        and event["actor"]["name"] == "创作 Agent · GPU算力数据"
    )
    assert data_index < scrape_index < step_writer_index
    scrape_event = next(
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "webpage_scrape"
    )
    assert next(
        item
        for item in scrape_event["environment_patch"]["data_sources"]
        if item["source_id"] == 1584
    ) == {
        "source_id": 1584,
        "title": "电商GPU信息平台 - GPU项目用量管理",
        "source_kind": "report_url",
        "freshness_class": None,
        "refresh_required": False,
        "can_use": True,
    }
    refresh_decision = next(
        event
        for event in events
        if event["type"] == "harness.decision"
        and event["data"]["trigger"] == "data_search"
    )
    assert refresh_decision["data"]["reason_code"] == "refresh_required"
    assert refresh_decision["data"]["scheduled"] == ["webpage_scrape"]
    final_document = next(
        event["data"]["document"]
        for event in reversed(events)
        if event["type"] == "run.completed"
    )
    assert final_document.startswith("# GPU成本优化周报")
    assert [
        line.removeprefix("## ")
        for line in final_document.splitlines()
        if line.startswith("## ")
    ] == [
        "本周大模型性能成本优化周会会议纪要",
        "AIGC进度总结",
        "GPU算力数据",
        "Token数据",
    ]
    assert 1_500 <= len(final_document) <= 3_500
    assert "## 本周结论" not in final_document
    assert "## 重点进展" not in final_document
    assert "## 风险与阻塞" not in final_document
    assert "## 下周计划" not in final_document
    assert "辅助明细" not in final_document


@pytest.mark.asyncio
async def test_optional_tools_are_called_only_when_enabled_and_matched():
    events = await collect_events(
        CreationAgentLoop(FakeCreationService()).run(
            user_message="检索 GitHub 开源仓库并用 PlantUML 画一张时序图，形成技术方案",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(
                enabled_tools=(
                    "internet_search",
                    "memory_search",
                    "github_search",
                    "plantuml_diagram",
                )
            ),
        )
    )

    tool_events = [
        event
        for event in events
        if event["type"] == "tool.completed"
    ]
    assert {"github_search", "plantuml_diagram"} <= {
        event["actor"]["id"] for event in tool_events
    }
    github_event = next(
        event for event in tool_events if event["actor"]["id"] == "github_search"
    )
    assert github_event["data"]["result_count"] == 1
    plantuml_event = next(
        event for event in tool_events if event["actor"]["id"] == "plantuml_diagram"
    )
    assert plantuml_event["data"]["diagram_type"] == "sequence"
    assert events[-1]["type"] == "run.completed"


@pytest.mark.asyncio
async def test_mermaid_diagram_tool_is_called_when_enabled_and_matched():
    events = await collect_events(
        CreationAgentLoop(FakeCreationService()).run(
            user_message="用 Mermaid 画一张状态图，说明任务从创建到归档的状态流转",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enabled_tools=("mermaid_diagram",)),
        )
    )

    tool_events = [
        event
        for event in events
        if event["type"] == "tool.completed"
    ]
    assert "mermaid_diagram" in {
        event["actor"]["id"] for event in tool_events
    }
    mermaid_event = next(
        event for event in tool_events if event["actor"]["id"] == "mermaid_diagram"
    )
    assert mermaid_event["data"]["diagram_type"] == "state"
    assert events[-1]["type"] == "run.completed"


@pytest.mark.asyncio
async def test_tool_failure_uses_stable_error_code_and_creation_continues():
    class FailingMemoryService(FakeCreationService):
        def retrieve_references(self, *_args):
            raise RuntimeError("本地索引暂时不可用")

    events = await collect_events(
        CreationAgentLoop(FailingMemoryService()).run(
            user_message="输出一份项目复盘方案",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(),
        )
    )

    failed = next(event for event in events if event["type"] == "tool.failed")
    assert failed["actor"]["id"] == "memory_search"
    assert failed["data"]["error_code"] == "TOOL_EXECUTION_FAILED"
    assert events[-1]["type"] == "run.completed"


def test_no_selected_skills_means_no_template_without_builtin_fallback():
    # 内置模板关键词兜底已下线：未召回任何已安装 Skill 时不再套模板，
    # 含“架构”等主题词的画图请求也不会被套上方案模板。
    loop = CreationAgentLoop(FakeCreationService())
    state = SimpleNamespace(
        selected_skills=[],
        root_request="画一张快手推理引擎的架构图",
        user_message="画一张快手推理引擎的架构图",
        environment={"requirement": {"doc_type": "技术文档"}},
    )

    assert loop._match_skills(state) == []


def test_no_builtin_template_even_with_explicit_solution_intent():
    # 模板能力只经由披露+模型决策的召回链路（已安装 Skill）引入，
    # 即使输入含明确的方案意图也不再由 Loop 内置兜底。
    loop = CreationAgentLoop(FakeCreationService())
    state = SimpleNamespace(
        selected_skills=[],
        root_request="为周报生成系统编写接口设计技术方案",
        user_message="为周报生成系统编写接口设计技术方案",
        environment={"requirement": {"doc_type": "技术方案"}},
    )

    assert loop._match_skills(state) == []


def test_installed_skill_keeps_style_but_excludes_fictional_example_facts():
    loop = CreationAgentLoop(FakeCreationService())
    state = SimpleNamespace(
        selected_skills=[
            {
                "id": "skill-style",
                "title": "架构方案风格",
                "summary": "复刻源文档风格",
                "titleDesignStyle": ["子标题使用问句结构"],
                "writingDesign": "先解释原因，再展开方案。",
                "imageGeneration": "推荐工具：PlantUML。",
                "structurePattern": ["问题与原因", "方案设计"],
                "voiceStyle": ["习惯用“基于此”承接方案。"],
                "fieldExamples": {"titleDesignStyle": ["方案如何落地"]},
                "exampleDocument": (
                    "# 示例\n\n## 国产卡切换\n\n"
                    "潮汐调度与推理引擎优化仅用于演示写法。"
                ),
            }
        ],
        root_request="输出架构方案",
        user_message="输出架构方案",
        environment={"requirement": {"doc_type": "架构方案"}},
    )

    matched = loop._match_skills(state)

    assert matched[0]["title_design_style"] == ["子标题使用问句结构"]
    assert matched[0]["writing_design"] == "先解释原因，再展开方案。"
    assert matched[0]["image_generation"] == "推荐工具：PlantUML。"
    assert matched[0]["voice_style"] == ["习惯用“基于此”承接方案。"]
    assert matched[0]["field_examples"]["titleDesignStyle"] == ["方案如何落地"]
    assert matched[0]["example_document_available"] is True
    assert "example_document" not in matched[0]
    runtime_environment = json.dumps(matched, ensure_ascii=False)
    assert "国产卡切换" not in runtime_environment
    assert "潮汐调度" not in runtime_environment
    assert "推理引擎优化" not in runtime_environment
    assert "structure" not in matched[0]
    assert "structurePattern" not in matched[0]


def test_skill_step_defaults_to_silent_collection_without_disabling_data_tool():
    loop = CreationAgentLoop(FakeCreationService())
    skill = {"id": "gpu-weekly", "name": "GPU 周报", "source": "installed"}
    disabled = loop._plan_skill_workflow(
        [
            {
                "id": "gpu-data",
                "title": "GPU 算力数据",
                "objective": "读取最新指标",
                "output": "实时指标",
                "tools": ["data_search"],
                "retain_webpage_screenshot": False,
            }
        ],
        skill,
        {"data_search", "webpage_scrape"},
    )
    defaulted = loop._plan_skill_workflow(
        [
            {
                "id": "gpu-data",
                "title": "GPU 算力数据",
                "objective": "读取最新指标",
                "output": "实时指标",
                "tools": ["data_search"],
            }
        ],
        skill,
        {"data_search", "webpage_scrape"},
    )

    assert disabled[0]["id"] == "data_search"
    assert disabled[0]["skill_step_retain_webpage_screenshot"] is False
    assert defaulted[0]["skill_step_retain_webpage_screenshot"] is False


@pytest.mark.asyncio
async def test_loop_updates_goal_after_agent_tool_and_skill_results():
    loop = CreationAgentLoop(FakeCreationService())
    events = await collect_events(
        loop.run(
            user_message="设计创作功能的 Agent Loop 架构方案",
            current_document="",
            conversation=[],
            selected_skills=[
                {
                    "id": "arch-plan",
                    "title": "架构方案模板",
                    "summary": "把系统架构设计整理成可评审的方案文档。",
                    "workflow_role": "primary",
                    "workflow_role_declared": True,
                    "execution_steps": [
                        {
                            "id": "arch-research",
                            "title": "检索本地记忆",
                            "objective": "收集创作功能相关的历史资料",
                            "output": "记忆检索结果",
                            "agents": [],
                            "skills": [],
                            "tools": ["memory_search"],
                        },
                        {
                            "id": "arch-write",
                            "title": "撰写架构方案",
                            "objective": "输出总体架构与关键决策",
                            "output": "架构方案文档",
                            "agents": ["solution_design_agent", "document_writer_agent"],
                            "skills": [],
                            "tools": [],
                        }
                    ],
                }
            ],
            options=CreationOptions(enable_rag=True, doc_type="架构设计方案"),
        )
    )

    event_types = [event["type"] for event in events]
    assert event_types[0] == "run.started"
    assert "tool.completed" in event_types
    assert "skill.completed" in event_types
    assert "document.delta" in event_types
    assert event_types[-1] == "run.completed"

    skill_completed = next(
        event for event in events if event["type"] == "skill.completed"
    )
    assert skill_completed["summary"] == "已应用 架构方案模板"
    assert "写入环境" not in skill_completed["summary"]

    unify_planned = next(
        event
        for event in events
        if event["type"] == "document.patch.planned"
        and event["actor"]["id"] == "document_unify_polisher"
    )
    assert unify_planned["summary"] == "正在统一全文结构与表达，保留既有章节和事实"

    completed = [
        event
        for event in events
        if event["type"] in {"agent.completed", "tool.completed", "skill.completed"}
    ]
    revisions = [event["goal"]["revision"] for event in completed]
    assert revisions == sorted(revisions)
    assert len(set(revisions)) == len(revisions)
    assert any(
        event["actor"]["id"] == "solution_design_agent" for event in completed
    )
    assert any(
        event["actor"]["id"] == "document_writer_agent" for event in completed
    )


@pytest.mark.asyncio
async def test_external_model_can_pause_and_resume_each_dynamic_agent_step():
    loop = CreationAgentLoop(FakeCreationService())
    first = await collect_events(
        loop.run(
            user_message="输出一份架构方案",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enable_rag=False, doc_type="架构设计方案"),
            model_mode="external",
        )
    )
    assert first[-2]["type"] == "model.request"
    assert first[-1]["type"] == "run.paused"
    first_state = first[-1]["data"]["continuation"]

    second = await collect_events(
        loop.run(
            user_message="",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(),
            resume_state=first_state,
            model_result='{"tools": [], "agents": ["solution_design_agent"], '
            '"reasoning": "方案文档"}',
        )
    )
    assert second[-2]["type"] == "model.request"
    assert second[-1]["type"] == "run.paused"
    second_state = second[-1]["data"]["continuation"]

    third = await collect_events(
        loop.run(
            user_message="",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(),
            resume_state=second_state,
            model_result="已明确组件边界、数据流和验证方式。",
        )
    )
    assert third[-2]["type"] == "model.request"
    assert third[-1]["type"] == "run.paused"
    third_state = third[-1]["data"]["continuation"]

    fourth = await collect_events(
        loop.run(
            user_message="",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(),
            resume_state=third_state,
            model_result="章节蓝图：目标、总体架构、实施与验证。",
        )
    )
    assert fourth[-2]["type"] == "model.request"
    assert fourth[-1]["type"] == "run.paused"
    fourth_state = fourth[-1]["data"]["continuation"]

    document = """# Agent 架构方案

## 目标

建立目标驱动的动态编排运行时。

## 总体架构

主 Agent 根据环境选择子 Agent、Tool 和 Skill。

## 实施与验证

用契约测试、运行时测试和页面测试验证完整链路。

先固化事件协议，再实现可暂停和恢复的运行时。每个 Agent、Tool 和 Skill 完成后都更新环境快照与目标修订号，主 Agent 依据最新状态选择下一步。验收覆盖初次生成、多轮修订、外部模型恢复、用户确认和失败重试。
"""
    final = await collect_events(
        loop.run(
            user_message="",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(),
            resume_state=fourth_state,
            model_result=document,
        )
    )
    assert final[-1]["type"] == "run.completed"
    assert final[-1]["data"]["document"] == document.strip()
    assert final[-1]["goal"]["status"] == "complete"


@pytest.mark.asyncio
async def test_short_initial_goal_requests_user_confirmation():
    loop = CreationAgentLoop(FakeCreationService())
    events = await collect_events(
        loop.run(
            user_message="写方案",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(enable_rag=False),
        )
    )
    assert events[-2]["type"] == "confirmation.required"
    assert events[-2]["goal"]["status"] == "waiting_user"
    assert events[-1]["type"] == "run.paused"


@pytest.mark.asyncio
async def test_follow_up_revises_full_document_and_places_new_section_logically():
    class IndustryResearchService(FakeCreationService):
        async def stream_agent_document(self, **_kwargs):
            yield """# 新能源数据平台建设方案

## 背景与目标

本方案面向集团管理层，建设新能源数据平台，统一数据口径并支持经营分析。原始范围包括数据治理、指标服务和权限审计，所有结论必须基于可核验资料。

## 行业调研

行业需求持续增长，具体规模和政策口径需结合检索来源核验。

- 趋势：从单点工具走向平台化协同。
- 约束：合规、数据质量与组织协作仍是主要挑战。

## 总体架构

平台由采集、治理、指标和应用四层组成，沿用既有安全边界与部署约束。

## 实施计划

第一阶段完成行业口径核验与数据梳理，第二阶段建设平台，第三阶段进行验收和推广。

## 风险与验证

通过数据质量抽检、权限审计、行业口径复核和业务验收验证方案有效性。
"""

    original = """# 新能源数据平台建设方案

## 背景与目标

本方案面向集团管理层，建设新能源数据平台，统一数据口径并支持经营分析。原始范围包括数据治理、指标服务和权限审计，所有结论必须基于可核验资料。

## 总体架构

平台由采集、治理、指标和应用四层组成，沿用既有安全边界与部署约束。

## 实施计划

第一阶段完成口径梳理，第二阶段建设平台，第三阶段进行验收和推广。

## 风险与验证

通过数据质量抽检、权限审计和业务验收验证方案有效性。
"""
    service = IndustryResearchService()
    loop = CreationAgentLoop(service)
    events = await collect_events(
        loop.run(
            user_message="补充下行业调研",
            root_request="为集团管理层生成新能源数据平台建设方案，保留安全边界",
            current_document=original,
            conversation=[
                {
                    "role": "user",
                    "content": "为集团管理层生成新能源数据平台建设方案，保留安全边界",
                },
                {"role": "assistant", "content": "已生成首版"},
                {"role": "user", "content": "补充下行业调研"},
            ],
            selected_skills=[],
            options=CreationOptions(enable_rag=True, enable_web_search=False),
        )
    )

    intent = next(event for event in events if event["type"] == "intent.interpreted")
    assert intent["data"]["operation"] == "revise_document"
    assert intent["data"]["target_sections"] == ["行业调研"]
    assert intent["data"]["root_request"].startswith("为集团管理层")

    patch_event = next(
        event for event in events if event["type"] == "document.patch.applied"
    )
    updated = patch_event["data"]["content"]
    assert "## 行业调研" in updated
    assert "## 总体架构\n\n平台由采集、治理、指标和应用四层组成" in updated
    assert updated.index("## 行业调研") < updated.index("## 总体架构")
    assert patch_event["data"]["patch"]["preserved_untouched"] is True
    assert patch_event["data"]["patch"]["operation"] == "revise_document"
    assert patch_event["data"]["patch"]["change_count"] >= 3
    assert {
        change["section_title"]
        for change in patch_event["data"]["patch"]["changes"]
    } >= {"行业调研", "实施计划", "风险与验证"}
    assert not any(event["type"] == "document.replaced" for event in events)
    assert events[-1]["data"]["document"] == updated
    assert "新能源数据平台建设方案" in service.reference_queries[0]
    assert "补充下行业调研" in service.reference_queries[0]


def test_context_window_keeps_original_request_and_recent_turns():
    service = FakeCreationService()
    loop = CreationAgentLoop(service)
    conversation = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": (
                "最初的完整行业方案需求"
                if index == 0
                else f"第 {index} 轮补充"
            ),
        }
        for index in range(70)
    ]

    state = loop._new_state(
        user_message="再补充行业调研",
        root_request=None,
        current_document="# 方案\n\n## 背景\n\n已有内容",
        conversation=conversation,
        selected_skills=[],
        options=CreationOptions(enable_rag=True),
        model_mode="local",
        session_id="session-context",
        run_id="run-context",
    )

    assert state.root_request == "最初的完整行业方案需求"
    assert state.conversation[0]["content"] == "最初的完整行业方案需求"
    assert state.conversation[-1]["content"] == "第 69 轮补充"
    assert "最初的完整行业方案需求" in state.environment["context_query"]
    assert "再补充行业调研" in state.goal.objective


@pytest.mark.asyncio
async def test_soft_quality_warning_does_not_regenerate_an_already_complete_revision():
    class CompleteRevisionService(FakeCreationService):
        def __init__(self):
            super().__init__()
            self.writer_calls = 0

        async def stream_agent_document(self, **_kwargs):
            self.writer_calls += 1
            yield """# 周年员工礼物自主领取指南

## 领取原则

参考成熟互联网公司的员工关怀做法，周年礼物采用自主选择、统一额度、按时领取的方式。礼物方案覆盖实用、纪念与体验三类，并明确适用人群、领取窗口和异常处理。

## 礼物分档

员工可在标准额度内选择办公用品、生活家电或纪念礼盒。每项礼物展示规格、库存和预计到货时间，缺货时提供同档替代选项，避免默认升级或隐性加价。

## 执行安排

人力团队提前确认名单，采购团队维护礼物池，员工在周年日前完成选择。系统保留选择记录，逾期、退换和地址变更进入人工处理，并由负责人按周核验完成情况。
"""

    original = """# 周年礼物指南

## 背景

说明周年礼物的员工关怀目标。

## 选品

列出可选择的礼物。

## 领取流程

说明领取步骤。

## 预算

说明预算约束。

## 风险

说明缺货和延期处理。
"""
    service = CompleteRevisionService()
    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="参考示例公司员工周年礼物方案",
            root_request="写一份周年员工的礼物指南",
            current_document=original,
            conversation=[
                {"role": "user", "content": "写一份周年员工的礼物指南"},
                {"role": "assistant", "content": "文档已更新"},
                {"role": "user", "content": "参考示例公司员工周年礼物方案"},
            ],
            selected_skills=[],
            options=CreationOptions(enable_rag=False),
        )
    )

    quality_event = next(
        event
        for event in events
        if event["type"] == "agent.completed"
        and event["actor"]["id"] == "quality_review_agent"
    )
    assert service.writer_calls == 1
    assert sum(event["type"] == "document.patch.applied" for event in events) == 1
    assert quality_event["environment_patch"]["quality_review"]["preserves_structure"] is False
    assert "保留当前完整版本" in quality_event["summary"]
    assert events[-1]["type"] == "run.completed"
    assert "质量风险" in events[-1]["goal"]["outcome"]


def test_replace_and_delete_section_patches_preserve_other_sections():
    loop = CreationAgentLoop(FakeCreationService())
    original = """# 平台方案

## 背景

背景保持不变。

## 行业调研

旧调研内容。

## 实施计划

实施计划保持不变。
"""
    replaced, replace_patch = loop._apply_document_patch(
        original,
        "## 行业调研\n\n新调研内容，并保留可核验口径。",
        operation="replace_section",
        target_sections=["行业调研"],
    )
    assert "旧调研内容" not in replaced
    assert "背景保持不变" in replaced
    assert "实施计划保持不变" in replaced
    assert replace_patch["operation"] == "replace_section"
    assert replace_patch["base_hash"] != replace_patch["result_hash"]

    deleted, delete_patch = loop._apply_document_patch(
        replaced,
        "",
        operation="delete_section",
        target_sections=["行业调研"],
    )
    assert "## 行业调研" not in deleted
    assert "背景保持不变" in deleted
    assert "实施计划保持不变" in deleted
    assert delete_patch["operation"] == "delete_section"

    inserted, _ = loop._apply_document_patch(
        original.replace(
            "## 行业调研\n\n旧调研内容。\n\n",
            "",
        ),
        "## 行业调研\n\n新增调研内容。",
        operation="append_section",
        target_sections=["行业调研"],
    )
    assert inserted.index("## 行业调研") < inserted.index("## 实施计划")
    assert loop._target_positions_are_logical(inserted, ["行业调研"]) is True
    appended_too_late = (
        original.replace(
            "## 行业调研\n\n旧调研内容。\n\n",
            "",
        ).rstrip()
        + "\n\n## 行业调研\n\n新增调研内容。\n"
    )
    assert loop._target_positions_are_logical(
        appended_too_late,
        ["行业调研"],
    ) is False

    global_change = loop._interpret_edit_intent(
        "把目标读者改为董事会成员",
        current_document=original,
        mode="revision",
    )
    assert global_change.operation == "revise_document"


def test_revision_patch_tracks_added_modified_and_deleted_ranges():
    loop = CreationAgentLoop(FakeCreationService())
    before = """# 平台方案

## 背景与目标

服务研发团队。

## 总体架构

沿用旧架构。

## 实施计划

分两阶段实施。
"""
    after = """# 平台方案

## 背景与目标

服务董事会和经营团队。

## 行业调研

行业正在从单点工具向平台化协同演进。

## 总体架构

采用数据、服务和应用三层架构。
"""
    patch = loop._build_document_revision_patch(
        before,
        after,
        operation="revise_document",
        requested_sections=["背景与目标", "行业调研", "总体架构"],
        preserved_untouched=True,
    )

    assert patch["operation"] == "revise_document"
    assert patch["change_count"] >= 4
    assert {change["change_type"] for change in patch["changes"]} == {
        "added",
        "modified",
        "deleted",
    }
    visible_changes = [
        change
        for change in patch["changes"]
        if change["change_type"] != "deleted"
    ]
    assert all(change["start_line"] <= change["end_line"] for change in visible_changes)
    assert "行业调研" in patch["target_sections"]


def _reference_state_item(
    source_type,
    source_id,
    *,
    content_chars=0,
    final_weight=0.5,
    skill_step_id=None,
):
    item = {
        "id": source_id,
        "source_id": source_id,
        "source_type": source_type,
        "title": f"{source_type}-{source_id}",
        "summary": "参考摘要",
        "content": "甲" * content_chars,
        "reason": "与步骤主题相关",
        "final_weight": final_weight,
    }
    if skill_step_id:
        item["skill_step_id"] = skill_step_id
        item["skill_step_title"] = f"步骤{skill_step_id}"
    return item


def test_prompt_references_prioritizes_current_step_matches_over_merge_order():
    # 上一步（会议纪要）先召回占满前位，本步（AIGC进度总结）召回的知识排在合并列表尾部
    batch_minutes = [
        _reference_state_item(
            "document",
            700 + index,
            content_chars=1600,
            final_weight=0.8 - index * 0.01,
            skill_step_id="collect-minutes",
        )
        for index in range(10)
    ]
    batch_progress = [
        _reference_state_item("knowledge", 2275, content_chars=1500, final_weight=0.68, skill_step_id="summarize-progress"),
        _reference_state_item("knowledge", 2274, content_chars=1200, final_weight=0.66, skill_step_id="summarize-progress"),
        _reference_state_item("document", 582, content_chars=1000, final_weight=0.60, skill_step_id="summarize-progress"),
    ]
    merged = CreationAgentLoop._merge_reference_states([], batch_minutes, limit=30)
    merged = CreationAgentLoop._merge_reference_states(merged, batch_progress, limit=30)
    progress_ids = {"knowledge:2275", "knowledge:2274", "document:582"}

    baseline = CreationAgentLoop._prompt_references(merged)
    baseline_ids = {
        f"{item.get('source_type') or 'document'}:{item.get('source_id')}"
        for item in baseline
    }
    # 不带步骤上下文时，尾部召回的本步证据会被预算截断（修复前的故障形态）
    assert not progress_ids <= baseline_ids

    scoped = CreationAgentLoop._prompt_references(
        merged,
        step={"skill_step_id": "summarize-progress"},
    )
    scoped_ids = [
        f"{item.get('source_type') or 'document'}:{item.get('source_id')}"
        for item in scoped
    ]
    # 本步命中的参考按 final_weight 降序排到最前且全部进入写作 Prompt
    assert scoped_ids[:3] == ["knowledge:2275", "knowledge:2274", "document:582"]
    # 非匹配参考保持原合并顺序，不被错误提升
    merged_order = [
        f"{item.get('source_type') or 'document'}:{item.get('source_id')}"
        for item in merged
        if f"{item.get('source_type') or 'document'}:{item.get('source_id')}"
        not in progress_ids
    ]
    assert scoped_ids[3:] == [
        identity for identity in merged_order if identity in set(scoped_ids[3:])
    ]


def test_prompt_references_retries_compaction_when_step_matches_exceed_budget():
    matched = [
        _reference_state_item(
            "knowledge",
            3000 + index,
            content_chars=1600,
            final_weight=0.9 - index * 0.01,
            skill_step_id="summarize-progress",
        )
        for index in range(12)
    ]
    fillers = [
        _reference_state_item(
            "document",
            4000 + index,
            content_chars=1600,
            final_weight=0.3,
            skill_step_id="collect-minutes",
        )
        for index in range(4)
    ]
    merged = CreationAgentLoop._merge_reference_states([], matched + fillers, limit=30)

    result = CreationAgentLoop._prompt_references(
        merged,
        step={"skill_step_id": "summarize-progress"},
    )

    result_ids = [
        f"{item.get('source_type') or 'document'}:{item.get('source_id')}"
        for item in result
    ]
    matched_ids = [f"knowledge:{3000 + index}" for index in range(12)]
    # 预算装不下全部匹配项时压缩正文重试，本步证据全部进入 Prompt
    assert matched_ids == result_ids[:12]
    for item in result[:12]:
        assert len(str(item.get("content") or "")) <= 801


def test_prompt_references_keeps_source_identity_for_history_trace():
    merged = CreationAgentLoop._merge_reference_states(
        [],
        [
            _reference_state_item(
                "knowledge",
                2275,
                content_chars=100,
                final_weight=0.7,
                skill_step_id="summarize-progress",
            )
        ],
        limit=30,
    )

    result = CreationAgentLoop._prompt_references(
        merged,
        step={"skill_step_id": "summarize-progress"},
    )

    # references_json 落库依赖 source_type/source_id 追溯记忆域与来源记录
    assert result[0]["source_type"] == "knowledge"
    assert result[0]["source_id"] == 2275


def _make_reference_document(source_type, source_id, *, final_weight=0.8):
    return ReferenceDocument(
        id=source_id,
        title=f"{source_type}资料{source_id}",
        doc_type="周报",
        summary="本周进展摘要",
        full_content="本周 AIGC 共建项目完成推理性能优化。",
        sections_json="[]",
        style_phrases="",
        prompt_hint="",
        usage_count=1,
        review_status="approved",
        updated_at=1720000000000,
        source_url=None,
        relevance_score=0.9,
        quality_score=0.8,
        completeness_score=0.8,
        usage_score=0.5,
        format_score=0.7,
        freshness_score=0.9,
        final_weight=final_weight,
        reason="与本周进展相关",
        source_type=source_type,
        source_id=source_id,
        retrieval_mode="relational",
        primary_target="AIGC 共建项目",
        matched_components=("AIGC 共建项目", "Agent"),
        matched_relations=("结合",),
        relation_score=1.0,
    )


@pytest.mark.asyncio
async def test_memory_search_event_keeps_recall_trace_fields():
    service = FakeCreationService()
    service.routing_decision = {"tools": ["memory_search"], "agents": []}
    retrieval_plan = {
        "mode": "relational",
        "primary_target": "AIGC 共建项目",
        "components": ["AIGC 共建项目", "Agent"],
        "relations": ["结合"],
        "hard_entity_gate": False,
    }
    service.analyze_requirement = lambda *args, **kwargs: {
        "topic": "AIGC 共建项目与 Agent 结合方案",
        "doc_type": "周报",
        "audience": "研发团队",
        "keywords": ["AIGC 共建项目", "Agent"],
        "style": "专业清晰",
        "entity_context": {"primary_entities": []},
        "retrieval_plan": retrieval_plan,
        "needs_latest": False,
        "needs_images": False,
    }
    recalled = [
        _make_reference_document("knowledge", 2275),
        _make_reference_document("document", 582, final_weight=0.6),
    ]
    service.retrieve_references = lambda *args, **kwargs: list(recalled)

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="写一份本周 AIGC 共建项目周报",
            current_document="",
            conversation=[],
            selected_skills=[],
            options=CreationOptions(),
        )
    )

    memory_events = [
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["actor"]["id"] == "memory_search"
    ]
    assert memory_events
    data = memory_events[0]["data"]
    # trace 落库必须保留召回 id 列表与查询，才能事后取证"某条知识为何没进成稿"
    assert data["reference_ids"] == ["knowledge:2275", "document:582"]
    assert data["result_count"] == 2
    assert data["query"]
    assert isinstance(data["keywords"], list)
    assert data["retrieval_plan"] == retrieval_plan
    assert data["entity_context"] == {"primary_entities": []}
    assert data["retrieval_diagnostics"] == {}
    assert "skill_step_id" in data and "skill_step_title" in data

    patch_references = memory_events[0]["environment_patch"]["references"]
    assert [ref["source_id"] for ref in patch_references] == [2275, 582]
    assert [ref["source_type"] for ref in patch_references] == [
        "knowledge",
        "document",
    ]
    assert all("skill_step_title" in ref for ref in patch_references)
    assert all(ref["retrieval_mode"] == "relational" for ref in patch_references)
    assert all(ref["primary_target"] == "AIGC 共建项目" for ref in patch_references)
    assert all(ref["matched_components"] == ["AIGC 共建项目", "Agent"] for ref in patch_references)
    assert all(ref["matched_relations"] == ["结合"] for ref in patch_references)
    assert all(ref["relation_score"] == 1.0 for ref in patch_references)
    assert all(ref["selection_reasons"] == [] for ref in patch_references)


def _strict_diagram_state(loop, step_tools):
    return loop._new_state(
        user_message="用架构方案模板 Skill 生成方案并画架构图",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[
            {
                "id": "arch-plan",
                "title": "架构方案模板",
                "executionSteps": [
                    {
                        "id": "write",
                        "title": "撰写方案",
                        "objective": "撰写完整架构方案。",
                        "agents": ["document_writer_agent"],
                        "skills": [],
                        "tools": list(step_tools),
                    }
                ],
            }
        ],
        options=CreationOptions(
            enabled_tools=("memory_search", "data_search", "mermaid_diagram")
        ),
        model_mode="local",
        session_id="session-strict-diagram",
        run_id="run-strict-diagram",
    )


def _generic_visual_plan():
    return {
        "schema_version": "creation.visual-plan.v1",
        "policy": "auto",
        "max_diagrams": 4,
        "diagrams": [
            {
                "id": "relationship-overview",
                "section_title": "关系总览",
                "purpose": "解释对象之间的调用关系",
                "diagram_type": "flowchart_lr",
                "required": True,
                "reason": "多个对象和方向用连续文字不易理解",
                "source_points": ["对象甲调用对象乙", "对象乙返回结果"],
                "placement": "after_intro",
                "max_nodes": 12,
            },
            {
                "id": "lifecycle",
                "section_title": "生命周期",
                "purpose": "解释状态变化和回退",
                "diagram_type": "state",
                "required": True,
                "reason": "存在状态变化和异常回退",
                "source_points": ["待处理进入处理中", "处理中可以完成或失败"],
                "placement": "after_intro",
                "max_nodes": 10,
            },
        ],
    }


def test_chapter_visual_plan_schedules_section_scoped_mermaid_steps():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="解释多个对象之间的关系、状态变化和异常回退",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=("mermaid_diagram",)),
        model_mode="local",
        session_id="session-generic-visual-plan",
        run_id="run-generic-visual-plan",
    )
    resolve_planned(loop, state, decision={"tools": [], "agents": []})
    state.environment["visual_plan"] = _generic_visual_plan()
    chapter_index = next(
        index
        for index, step in enumerate(state.plan)
        if step["id"] == "chapter_design_agent"
    )
    state.cursor = chapter_index + 1

    decision = loop._replan_after_feedback(
        state,
        {"id": "chapter_design_agent"},
        status="completed",
    )

    assert decision["reason_code"] == "visual_plan_ready"
    assert decision["scheduled"] == ["mermaid_diagram", "mermaid_diagram"]
    inserted = state.plan[state.cursor : state.cursor + 2]
    assert [item["diagram_spec"]["section_title"] for item in inserted] == [
        "关系总览",
        "生命周期",
    ]
    assert inserted[0]["schedule_key"] != inserted[1]["schedule_key"]


def test_visual_plan_quality_gate_checks_each_target_section_and_type():
    loop = CreationAgentLoop(FakeCreationService())
    state = loop._new_state(
        user_message="解释多个对象之间的关系和状态变化",
        root_request=None,
        current_document="",
        conversation=[],
        selected_skills=[],
        options=CreationOptions(enabled_tools=("mermaid_diagram",)),
        model_mode="local",
        session_id="session-generic-visual-quality",
        run_id="run-generic-visual-quality",
    )
    state.environment["visual_plan"] = _generic_visual_plan()
    document = """# 通用说明

## 关系总览

对象之间存在方向明确的调用关系。

```mermaid
flowchart LR
    A[对象甲] --> B[对象乙]
```

## 生命周期

对象会在多个状态之间变化。
"""

    criteria, issues = loop._inspect_document_quality(state, document)

    visual_issue = next(
        item for item in issues if item["code"] == "planned_diagram_missing"
    )
    assert criteria["planned_diagrams_covered"] is False
    assert visual_issue["required_capabilities"] == [
        "mermaid_diagram",
        "skill:image_style",
    ]
    assert visual_issue["evidence"]["missing_diagrams"] == [
        {
            "diagram_id": "lifecycle",
            "section_title": "生命周期",
            "expected_type": "state",
            "reason": "diagram_missing",
        }
    ]


def test_strict_skill_workflow_appends_enabled_routed_diagram_tool():
    # 路由决策选中且已启用的画图工具不得因 Skill 步骤未声明而被丢弃；
    # 画图步骤在写作步骤之前，保证撰写时能拿到画图约束。
    loop = CreationAgentLoop(FakeCreationService())
    state = _strict_diagram_state(loop, step_tools=[])

    plan = resolve_planned(
        loop,
        state,
        decision={
            "tools": ["mermaid_diagram"],
            "agents": [],
            "source": "model",
            "reasoning": "用户明确要架构图",
        },
    )

    plan_ids = [step["id"] for step in plan]
    assert plan_ids.count("mermaid_diagram") == 1
    assert state.environment["strict_skill_ids"] == ["arch-plan"]
    mermaid_index = plan_ids.index("mermaid_diagram")
    first_skill_step_index = next(
        index
        for index, step in enumerate(plan)
        if step.get("action") in {"skill_step", "activate_skill_step"}
    )
    assert mermaid_index < first_skill_step_index


def test_strict_skill_workflow_does_not_duplicate_declared_diagram_tool():
    # Skill 步骤已声明 mermaid 时由工作流自己调度，不再重复补位。
    loop = CreationAgentLoop(FakeCreationService())
    state = _strict_diagram_state(loop, step_tools=["mermaid_diagram"])

    plan = resolve_planned(
        loop,
        state,
        decision={
            "tools": ["mermaid_diagram"],
            "agents": [],
            "source": "model",
            "reasoning": "用户明确要架构图",
        },
    )

    assert [step["id"] for step in plan].count("mermaid_diagram") == 1


def test_strict_skill_workflow_ignores_unenabled_diagram_decision():
    # 未启用的画图工具即使出现在决策里也不得进入计划（与披露契约一致）。
    loop = CreationAgentLoop(FakeCreationService())
    state = _strict_diagram_state(loop, step_tools=[])
    state.options = dict(state.options)
    state.options["enabled_tools"] = (
        "internet_search",
        "memory_search",
        "data_search",
        "webpage_scrape",
    )

    plan = resolve_planned(
        loop,
        state,
        decision={
            "tools": ["mermaid_diagram"],
            "agents": [],
            "source": "model",
            "reasoning": "用户明确要架构图",
        },
    )

    assert "mermaid_diagram" not in [step["id"] for step in plan]


def _flaky_stream_skill():
    return {
        "id": "creation-skill-flaky-stream",
        "title": "断流重试创作法",
        "executionSteps": [
            {
                "id": "write-section",
                "title": "章节生成",
                "objective": "生成一个完整章节。",
                "output": "章节内容",
                "agents": [],
                "skills": [],
                "tools": [],
            }
        ],
    }


@pytest.mark.asyncio
async def test_strict_skill_step_retries_midstream_transport_drop_and_keeps_single_copy():
    # 流中途断连属于可重试故障：整步重试一次后继续完成创作；
    # 预览按 document_parts 原子重组，成稿不得拼接两次输出。
    class FlakyStreamService(FakeCreationService):
        def __init__(self):
            super().__init__()
            self.stream_calls = 0

        async def run_specialist_agent(self, **kwargs):
            return "这是模型断流恢复后生成的完整章节。"

        async def stream_specialist_agent(self, **kwargs):
            self.stream_calls += 1
            result = await self.run_specialist_agent(**kwargs)
            midpoint = max(1, len(result) // 2)
            yield result[:midpoint]
            if self.stream_calls == 1:
                raise httpx.RemoteProtocolError(
                    "peer closed connection without sending complete message body"
                )
            yield result[midpoint:]

    service = FlakyStreamService()
    service.routing_decision = {"tools": [], "agents": []}

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="使用断流重试创作法生成章节",
            current_document="",
            conversation=[],
            selected_skills=[_flaky_stream_skill()],
            options=CreationOptions(),
        )
    )

    assert service.stream_calls == 2
    retry_events = [
        event
        for event in events
        if event["type"] == "agent.started" and "连接中断" in event["summary"]
    ]
    assert len(retry_events) == 1
    assert any(event["type"] == "run.completed" for event in events)
    final_documents = [
        event["data"]["content"]
        for event in events
        if event["type"] == "document.replaced"
    ]
    assert final_documents
    assert final_documents[-1].count("模型断流恢复后生成的完整章节") == 1


@pytest.mark.asyncio
async def test_strict_skill_step_skips_node_after_retry_budget_exhausted():
    # 持续断流时最多重试一次；重试耗尽后不再上抛中止整轮，
    # 而是把该节点标记为失败并继续收尾。
    class AlwaysDroppingStreamService(FakeCreationService):
        def __init__(self):
            super().__init__()
            self.stream_calls = 0

        async def stream_specialist_agent(self, **kwargs):
            self.stream_calls += 1
            yield "部分输出"
            raise httpx.RemoteProtocolError("peer closed connection")

    service = AlwaysDroppingStreamService()
    service.routing_decision = {"tools": [], "agents": []}

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="使用断流重试创作法生成章节",
            current_document="",
            conversation=[],
            selected_skills=[_flaky_stream_skill()],
            options=CreationOptions(),
        )
    )

    assert service.stream_calls == 2
    failed_events = [event for event in events if event["type"] == "agent.failed"]
    assert len(failed_events) == 1
    assert failed_events[0]["status"] == "failed"
    assert failed_events[0]["data"]["error_code"] == "MODEL_TRANSPORT_UNAVAILABLE"
    assert not any(event["type"] == "run.failed" for event in events)
    completed = [event for event in events if event["type"] == "run.completed"]
    assert completed
    assert "1 个节点失败已跳过" in completed[0]["summary"]


@pytest.mark.asyncio
async def test_run_specialist_agent_retries_midstream_transport_drop(tmp_path):
    # 非流式消费调用：流中途断流且已有部分缓冲时同样在限界内重试一次，
    # 丢弃半截缓冲是安全的。
    calls = {"count": 0}

    class MidStreamDropService(CreationService):
        async def _stream_direct_completion(self, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                yield "部分输出，即将断流"
                raise httpx.RemoteProtocolError("peer closed connection")
            yield "完整的结构化结论"

    service = MidStreamDropService(
        db_path=str(tmp_path / "creation-usage.db"),
        model="qwen3.5:4b",
        enable_vector_recall=False,
    )
    result = await service.run_specialist_agent(
        agent_id="chapter_design_agent",
        system_prompt="系统提示",
        user_prompt="用户提示",
    )
    assert result == "完整的结构化结论"
    assert calls["count"] == 2


def _fault_tolerance_skill(step_titles):
    return {
        "id": "creation-skill-fault-tolerance",
        "title": "节点容错创作法",
        "executionSteps": [
            {
                "id": f"write-{index}",
                "title": title,
                "objective": f"生成{title}章节。",
                "output": "章节内容",
                "agents": [],
                "skills": [],
                "tools": [],
            }
            for index, title in enumerate(step_titles, start=1)
        ],
    }


class FaultToleranceStreamService(FakeCreationService):
    """按步骤标题注入流式断流故障，其余步骤正常产出；全文润色透传组装稿。"""

    def __init__(self, failing_titles):
        super().__init__()
        self.failing_titles = set(failing_titles)
        self.stream_calls_by_title = {}

    @staticmethod
    def _step_title(kwargs):
        prompt = str(kwargs.get("user_prompt") or "")
        if "步骤：" not in prompt:
            return ""
        return prompt.split("步骤：", 1)[1].splitlines()[0].strip()

    async def stream_specialist_agent(self, **kwargs):
        title = self._step_title(kwargs)
        self.stream_calls_by_title[title] = (
            self.stream_calls_by_title.get(title, 0) + 1
        )
        if title in self.failing_titles:
            yield "部分输出"
            raise httpx.RemoteProtocolError("peer closed connection")
        yield f"{title}的完整章节内容。"

    async def stream_agent_document(self, **kwargs):
        system_prompt = str(kwargs.get("system_prompt") or "")
        if "全文整合润色" in system_prompt:
            prompt = str(kwargs.get("user_prompt") or "")
            marker = "现有完整文档：\n"
            if marker in prompt:
                yield prompt.split(marker, 1)[1].strip()
                return
        async for chunk in super().stream_agent_document(**kwargs):
            yield chunk


@pytest.mark.asyncio
async def test_model_step_failure_skips_node_and_continues():
    # 单节点模型推理失败只在该节点标记失败并跳过，后续节点继续执行，整轮不中断。
    service = FaultToleranceStreamService({"背景梳理"})
    service.routing_decision = {"tools": [], "agents": []}

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="使用节点容错创作法生成文档",
            current_document="",
            conversation=[],
            selected_skills=[
                _fault_tolerance_skill(["背景梳理", "数据分析", "结论建议"])
            ],
            options=CreationOptions(),
        )
    )

    failed_events = [event for event in events if event["type"] == "agent.failed"]
    assert len(failed_events) == 1
    assert failed_events[0]["status"] == "failed"
    assert failed_events[0]["data"]["error_code"] == "MODEL_TRANSPORT_UNAVAILABLE"
    assert "背景梳理" in failed_events[0]["summary"]
    assert "已跳过该节点继续执行" in failed_events[0]["summary"]
    assert not any(event["type"] == "run.failed" for event in events)
    completed = [event for event in events if event["type"] == "run.completed"]
    assert completed
    assert "1 个节点失败已跳过" in completed[0]["summary"]
    document = str(completed[0]["data"]["document"])
    assert "数据分析的完整章节内容。" in document
    assert "结论建议的完整章节内容。" in document
    assert "背景梳理的完整章节内容" not in document
    # 失败步骤重试耗尽（2 次调用），成功步骤只调用一次，没有多余重试。
    assert service.stream_calls_by_title["背景梳理"] == 2
    assert service.stream_calls_by_title["数据分析"] == 1


@pytest.mark.asyncio
async def test_consecutive_failures_beyond_budget_abort_run():
    # 连续失败超过熔断阈值时中止整轮：异常上抛由上层转 run.failed。
    titles = ["步骤一", "步骤二", "步骤三", "步骤四"]
    service = FaultToleranceStreamService(set(titles))
    service.routing_decision = {"tools": [], "agents": []}

    with pytest.raises(httpx.RemoteProtocolError):
        await collect_events(
            CreationAgentLoop(service).run(
                user_message="使用节点容错创作法生成文档",
                current_document="",
                conversation=[],
                selected_skills=[_fault_tolerance_skill(titles)],
                options=CreationOptions(),
            )
        )

    # 前三个失败节点被跳过，第四个连续失败触发熔断，每步各重试一次。
    assert service.stream_calls_by_title["步骤四"] == 2


@pytest.mark.asyncio
async def test_failed_step_recovery_resets_failure_budget():
    # 成功节点重置连续失败计数：失败-成功-失败-失败不触发熔断，整轮正常收尾。
    titles = ["步骤一", "步骤二", "步骤三", "步骤四"]
    service = FaultToleranceStreamService({"步骤一", "步骤三", "步骤四"})
    service.routing_decision = {"tools": [], "agents": []}

    events = await collect_events(
        CreationAgentLoop(service).run(
            user_message="使用节点容错创作法生成文档",
            current_document="",
            conversation=[],
            selected_skills=[_fault_tolerance_skill(titles)],
            options=CreationOptions(),
        )
    )

    failed_events = [event for event in events if event["type"] == "agent.failed"]
    assert len(failed_events) == 3
    completed = [event for event in events if event["type"] == "run.completed"]
    assert completed
    assert "3 个节点失败已跳过" in completed[0]["summary"]
    document = str(completed[0]["data"]["document"])
    assert "步骤二的完整章节内容。" in document
