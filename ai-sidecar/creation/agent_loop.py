"""目标驱动的创作 Agent Loop。

创作 Agent 只负责维护目标、环境和下一步计划。子 Agent、Tool、Skill 的每次
执行都会先产生可观察事件，再把结果写回环境，随后重新评估剩余步骤。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

import httpx

from .query_engine import (
    QueryPlanError,
    build_query_planner_prompts,
    execute_query_plan,
    parse_query_plan,
    relation_catalog,
    validate_query_plan,
)
from .service import (
    CloudModelRequestError,
    CreationOptions,
    CreationService,
    ReferenceDocument,
    _is_retryable_model_transport,
)
from .tools import (
    CreationToolExecutionError,
    DATA_SEARCH_TOOL_ID,
    GITHUB_SEARCH_TOOL_ID,
    INTERNET_SEARCH_TOOL_ID,
    MEMORY_SEARCH_TOOL_ID,
    MERMAID_DIAGRAM_TOOL_ID,
    PLANTUML_DIAGRAM_TOOL_ID,
    WEBPAGE_SCRAPE_TOOL_ID,
    build_mermaid_context,
    build_plantuml_context,
    fallback_routing_decision,
    normalize_creation_tool_ids,
    validate_routing_decision,
)
from .visual_plan import parse_chapter_design_result

SCHEMA_VERSION = "creation.agent.v1"
logger = logging.getLogger(__name__)
MAX_LOOP_STEPS = 64
MAX_QUALITY_CYCLES = 3
# 节点级容错熔断阈值：单个节点失败只标记并跳过，连续失败超过该阈值才中止整轮。
MAX_CONSECUTIVE_STEP_FAILURES = 3
MAX_SKILL_STEP_RESOURCES = 4
MAX_PROMPT_ENVIRONMENT_CHARS = 56000
MAX_PROMPT_DATA_RESULTS_CHARS = 22000
MAX_PROMPT_REFERENCE_CHARS = 16000
MAX_PROMPT_SKILL_CHARS = 18000
MAX_PROMPT_COMPLETED_STEPS_CHARS = 9000
MAX_PROMPT_SCRAPE_CHARS = 5000
MAX_SKILL_INSTRUCTION_CHARS = 12000
MAX_BRAINSTORM_CONTEXT_CHARS = 16000
MAX_BRAINSTORM_CONTEXT_DECISIONS = 24
MAX_BRAINSTORM_DECISION_DIMENSION_CHARS = 80
MAX_BRAINSTORM_DECISION_SUMMARY_CHARS = 320
MAX_BRAINSTORM_OPEN_FLAG_CHARS = 300


def _step_failure_details(exc: BaseException) -> tuple[str, str]:
    """把节点执行失败收敛为稳定错误码与用户可读原因，不泄露供应商细节。"""
    if isinstance(exc, httpx.TransportError):
        return "MODEL_TRANSPORT_UNAVAILABLE", "模型服务连接中断"
    if isinstance(exc, CloudModelRequestError):
        return "MODEL_REQUEST_FAILED", f"模型请求失败（状态码 {exc.status_code}）"
    reason = str(exc).strip() or type(exc).__name__
    return "STEP_GENERATION_FAILED", reason[:120]


KNOWN_SECTION_TITLES = (
    "行业调研",
    "市场调研",
    "市场分析",
    "竞品分析",
    "用户调研",
    "需求分析",
    "背景与目标",
    "总体架构",
    "功能设计",
    "交互设计",
    "数据分析",
    "实施计划",
    "风险与验证",
    "验收标准",
    "后续核验与补充清单",
)
APPEND_MARKERS = ("补充", "新增", "增加", "添加", "加上", "完善", "扩展", "补上")
DELETE_MARKERS = ("删除", "删掉", "移除", "去掉")
REPLACE_MARKERS = ("修改", "调整", "改成", "改为", "替换", "重写")
GLOBAL_REWRITE_MARKERS = (
    "全文",
    "整篇",
    "整体重写",
    "全部重写",
    "重新生成",
    "推倒重来",
    "统一改写",
)
SECTION_ORDER_RULES = (
    (10, ("背景", "目标", "现状", "概述", "原则")),
    (20, ("行业", "市场", "竞品", "用户调研", "用户与场景")),
    (30, ("需求", "约束", "数据", "指标", "统计", "分析")),
    (40, ("总体", "架构", "方案", "策略", "设计", "机制", "决策")),
    (50, ("功能", "组件", "模块", "流程", "接口", "数据流")),
    (60, ("实施", "落地", "执行", "路线", "里程碑", "演进")),
    (70, ("运营", "治理", "保障")),
    (80, ("风险", "安全", "合规")),
    (90, ("验证", "验收", "评估")),
    (100, ("参考", "核验", "补充清单", "结语", "总结")),
)

QUALITY_AGENT_ORDER = (
    "document_writer_agent",
    "detail_polish_agent",
    "table_polish_agent",
    "image_polish_agent",
    "anti_ai_style_agent",
    "typography_polish_agent",
)
EMPHASIS_QUALITY_ISSUE_CODE = "emphasis_needs_polish"
EMPHASIS_QUALITY_CRITERION = "emphasis_selective"
DATA_QUERY_QUALITY_ISSUE_CODE = "data_query_result_incomplete"
DATA_QUERY_QUALITY_CRITERION = "data_query_results_complete"
PAGE_ABSENCE_QUALITY_ISSUE_CODE = "unsupported_page_absence_claim"
PAGE_ABSENCE_QUALITY_CRITERION = "page_absence_claim_supported"
VISUAL_PLAN_QUALITY_ISSUE_CODE = "planned_diagram_missing"
VISUAL_PLAN_QUALITY_CRITERION = "planned_diagrams_covered"
SUBSECTION_REQUIREMENTS_QUALITY_ISSUE_CODE = "subsection_requirements_incomplete"
SUBSECTION_REQUIREMENTS_QUALITY_CRITERION = "subsection_requirements_satisfied"
MULTI_TARGET_COVERAGE_ISSUE_CODE = "multi_target_coverage_incomplete"
MULTI_TARGET_COVERAGE_CRITERION = "multi_target_coverage_satisfied"
MAX_EMPHASIS_CHARACTER_RATIO = 0.18
MAX_EMPHASIS_SPAN_CHARS = 32
MIN_NARRATIVE_FRAGMENT_CHARS = 18
STRICT_SKILL_QUALITY_ISSUE_CODES = (
    DATA_QUERY_QUALITY_ISSUE_CODE,
    EMPHASIS_QUALITY_ISSUE_CODE,
    PAGE_ABSENCE_QUALITY_ISSUE_CODE,
    SUBSECTION_REQUIREMENTS_QUALITY_ISSUE_CODE,
)
THINKING_STAGE_LABELS = {
    "intent": "理解本轮要求",
    "routing": "决定执行链路",
    "generation": "生成文档内容",
    "planning": "规划下一步",
}
HARNESS_REASON_TEXTS = {
    "data_search_failed": "数据检索未完成，继续使用其他可用资料",
    "refresh_required": "发现需要即时刷新的报表，安排后台采集",
    "snapshot_ready": "已有可分析的数据快照，安排数据分析",
    "structured_data_ready": "发现结构化关系数据，安排通用查询规划",
    "source_metadata_only": "只找到来源信息，暂时没有可用数据，保留当前计划",
    "no_matching_data": "没有找到匹配的数据来源，保留当前计划",
    "refresh_failed_stale_snapshot_available": "即时刷新失败，保留历史快照并继续分析",
    "refresh_feedback_ready": "即时采集完成，可以继续分析最新数据",
    "refresh_failed_without_snapshot": "即时刷新失败，也没有可用历史快照，继续基于其他资料创作",
    "refresh_returned_no_analyzable_data": "页面已刷新，但没有提取到可分析数据，保留当前计划",
    "visual_plan_ready": "章节蓝图识别出适合图示表达的关系，按章节准备 Mermaid 约束",
    "visual_plan_empty": "章节内容不需要额外图示，继续完成正文",
    "explicit_visual_request": "用户明确要求 Mermaid 图示，按原始请求准备画图约束",
    "visual_tool_disabled": "章节存在图示建议，但 Mermaid Tool 当前未启用",
    "chapter_design_failed": "章节设计未完成，不追加自动配图步骤",
    "quality_review_failed": "质量检查未完成，暂不追加优化动作",
    "quality_gate_passed": "质量要求已满足，可以结束本轮创作",
    "quality_cycle_budget_exhausted": "已达到自动优化上限，剩余问题保留给用户复核",
    "quality_issues_detected": "发现可继续优化的问题，安排对应的优化能力",
    "quality_issues_deferred": "已尝试自动修复但仍有遗留，保留当前版本不再重写",
    "hard_failure_retry_exhausted": "完整文档重试后仍有阻断问题，结束本轮优化",
}
QUALITY_SKILL_CAPABILITY_FIELDS = {
    "skill:voice_style": ("voice_style", "guidelines"),
    "skill:writing_design": ("writing_design", "skill_description", "field_examples"),
    "skill:table_style": ("writing_design", "field_examples"),
    "skill:typography_style": ("title_design_style", "voice_style"),
    "skill:image_style": ("image_generation",),
}
AI_STYLE_BOILERPLATE = (
    "在当今",
    "随着时代的发展",
    "在这个快速发展的时代",
    "值得注意的是",
    "不难发现",
    "不可否认",
    "毋庸置疑",
    "综上所述",
    "总而言之",
    "由此可见",
    "这不仅",
    "更是",
    "赋能",
)
AI_STYLE_TRANSITIONS = (
    "首先",
    "其次",
    "再次",
    "此外",
    "同时",
    "最后",
    "一方面",
    "另一方面",
)


@dataclass
class GoalState:
    objective: str
    status: str = "active"
    revision: int = 0
    acceptance_criteria: list[str] = field(default_factory=list)
    remaining_steps: list[str] = field(default_factory=list)
    outcome: str = ""


@dataclass(frozen=True)
class EditIntent:
    """面向用户展示且可执行的意图摘要，不包含模型私有思维过程。"""

    mode: str
    operation: str
    target_sections: tuple[str, ...] = ()
    preserve_untouched: bool = True
    summary: str = ""
    reasoning_summary: str = ""


@dataclass
class LoopState:
    session_id: str
    run_id: str
    mode: str
    model_mode: str
    creation_mode: str
    user_message: str
    root_request: str
    current_document: str
    conversation: list[dict[str, str]]
    options: dict[str, Any]
    selected_skills: list[dict[str, Any]]
    goal: GoalState
    environment: dict[str, Any] = field(default_factory=dict)
    plan: list[dict[str, Any]] = field(default_factory=list)
    cursor: int = 0
    sequence: int = 0
    pending_model_step: Optional[dict[str, Any]] = None
    writer_revisions: int = 0
    quality_cycles: int = 0

    def serializable(self) -> dict[str, Any]:
        value = asdict(self)
        value["goal"] = asdict(self.goal)
        return value

    @classmethod
    def restore(cls, value: dict[str, Any]) -> "LoopState":
        data = dict(value)
        data.setdefault("root_request", data.get("user_message", ""))
        data.setdefault("quality_cycles", 0)
        data.setdefault("creation_mode", "direct")
        data["goal"] = GoalState(**data["goal"])
        return cls(**data)


class CreationAgentLoop:
    """创作 Agent 的可暂停、可恢复状态机。"""

    def __init__(self, service: CreationService):
        self.service = service

    async def run(
        self,
        *,
        user_message: str,
        root_request: Optional[str] = None,
        current_document: str,
        conversation: list[dict[str, str]],
        selected_skills: list[dict[str, Any]],
        options: CreationOptions,
        model_mode: str = "local",
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        confirmed: bool = False,
        resume_state: Optional[dict[str, Any]] = None,
        model_result: Optional[str] = None,
        creation_model: Optional[str] = None,
        creation_api_key: Optional[str] = None,
        creation_base_url: Optional[str] = None,
        creation_mode: str = "direct",
        creation_brief: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        if resume_state:
            state = LoopState.restore(resume_state)
            yield self._event(state, "run.resumed", "创作 Agent 已恢复创作循环")
            if not state.pending_model_step or model_result is None:
                yield self._event(
                    state,
                    "run.failed",
                    "恢复创作循环时缺少待处理的模型结果",
                    status="failed",
                )
                return
            completed_model_step = dict(state.pending_model_step.get("step") or {})
            async for event in self._apply_model_result(state, model_result):
                yield event
            decision = self._replan_after_feedback(
                state,
                completed_model_step,
                status="completed",
            )
            if decision:
                async for event in self._yield_harness_decision(state, decision):
                    yield event
        else:
            state = self._new_state(
                user_message=user_message,
                root_request=root_request,
                current_document=current_document,
                conversation=conversation,
                selected_skills=selected_skills,
                options=options,
                model_mode=model_mode,
                session_id=session_id,
                run_id=run_id,
                creation_mode=creation_mode,
                creation_brief=creation_brief,
            )
            yield self._event(state, "run.started", "创作 Agent 已接管目标")
            yield self._event(
                state,
                "goal.updated",
                "已建立创作目标与验收条件",
                environment_patch={
                    "mode": state.mode,
                    "root_request": state.root_request,
                },
            )
            intent = state.environment["edit_intent"]
            yield self._thinking_started(state, "intent")
            yield self._event(
                state,
                "intent.interpreted",
                str(intent["summary"]),
                status="completed",
                actor=self._actor("agent", "creation_main_agent", "创作 Agent"),
                data={
                    "operation": intent["operation"],
                    "target_sections": intent["target_sections"],
                    "preserve_untouched": intent["preserve_untouched"],
                    "reasoning_summary": intent["reasoning_summary"],
                    "root_request": state.root_request,
                    "current_instruction": state.user_message,
                },
            )
            yield self._thinking_completed(
                state,
                "intent",
                str(intent.get("reasoning_summary") or ""),
            )
            if self._needs_confirmation(state) and not confirmed:
                state.goal.status = "waiting_user"
                yield self._event(
                    state,
                    "confirmation.required",
                    "需要确认后才能继续",
                    status="waiting",
                    actor=self._actor("agent", "creation_main_agent", "创作 Agent"),
                    data={
                        "question": "当前要求较简略。是否按现有信息继续，由 Agent 补全合理假设？",
                        "confirm_label": "按当前信息继续",
                        "request_id": f"confirm-{uuid4()}",
                    },
                )
                yield self._event(
                    state,
                    "run.paused",
                    "创作循环正在等待用户确认",
                    status="waiting",
                    data={"reason": "user_confirmation"},
                )
                return

        loop_count = 0
        consecutive_failures = 0
        while state.cursor < len(state.plan) and loop_count < MAX_LOOP_STEPS:
            loop_count += 1
            step = state.plan[state.cursor]
            state.cursor += 1
            state.goal.remaining_steps = [item["name"] for item in state.plan[state.cursor:]]
            step_status = "completed"
            error_code: Optional[str] = None
            # 顶层阶段边界：让页面能按 Skill 步骤/宏观计划分层展示执行过程。
            async for phase_event in self._switch_phase(
                state, self._phase_of_step(step)
            ):
                yield phase_event
            try:
                async for event in self._execute_step(
                    state,
                    step,
                    creation_model=creation_model,
                    creation_api_key=creation_api_key,
                    creation_base_url=creation_base_url,
                ):
                    yield event
            except Exception as exc:
                if step.get("kind") != "tool":
                    # 节点级容错：模型节点失败只在该节点标记失败并跳过，
                    # 仅当连续失败超过熔断阈值时才中止整轮创作。
                    consecutive_failures += 1
                    if consecutive_failures > MAX_CONSECUTIVE_STEP_FAILURES:
                        logger.error(
                            "创作节点已连续失败 %s 次，超过容错阈值，中止本轮创作: %s",
                            consecutive_failures,
                            exc,
                        )
                        raise
                    error_code, failure_reason = _step_failure_details(exc)
                    logger.warning(
                        "节点 %s 执行失败（%s），跳过该节点继续执行: %s",
                        step.get("id"),
                        error_code,
                        exc,
                    )
                    state.environment.setdefault("failed_steps", []).append(
                        {
                            "step_id": str(step.get("id") or ""),
                            "name": str(step.get("name") or ""),
                            "action": str(step.get("action") or ""),
                            "skill_step_id": step.get("skill_step_id"),
                            "error_code": error_code,
                            "reason": failure_reason,
                        }
                    )
                    if (
                        step.get("action") in {"skill_step", "writer"}
                        and step.get("skill_step_id")
                        and state.environment.get("strict_skill_workflow")
                    ):
                        self._record_failed_skill_step(state, step, failure_reason)
                        # 重组不含失败步骤的文档，替换页面上断流前残留的部分预览。
                        assembled = self._assemble_strict_skill_document(state)
                        state.environment["document"] = assembled
                        state.current_document = assembled
                        yield self._event(
                            state,
                            "document.replaced",
                            "节点失败后文档已更新为最新可用版本",
                            status="completed",
                            actor=self._actor(
                                "agent",
                                str(step.get("id") or ""),
                                str(step.get("name") or "创作 Agent"),
                            ),
                            data={
                                "content": assembled,
                                "operation": "failed_step_assembly",
                            },
                        )
                    self._update_goal(state)
                    # thinking.started 已在 _execute_step 内发出，失败时也要配对关闭思考块。
                    yield self._thinking_completed(
                        state,
                        "generation",
                        f"节点执行失败：{failure_reason}",
                    )
                    step_title = (
                        self._step_content_title(step)
                        or str(step.get("skill_step_title") or "")
                        or str(step.get("name") or "当前节点")
                    )
                    yield self._event(
                        state,
                        "agent.failed",
                        f"「{step_title}」生成失败：{failure_reason}，已跳过该节点继续执行",
                        status="failed",
                        actor=self._actor(
                            "agent",
                            str(step.get("id") or ""),
                            str(step.get("name") or "创作 Agent"),
                        ),
                        data={
                            "error_code": error_code,
                            "error_reason": failure_reason,
                            "skill_step_id": step.get("skill_step_id"),
                        },
                    )
                    step_status = "failed"
                else:
                    tool_id = str(step.get("id") or "")
                    error_code = (
                        exc.error_code
                        if isinstance(exc, CreationToolExecutionError)
                        else "TOOL_EXECUTION_FAILED"
                    )
                    state.environment.setdefault("tool_results", []).append(
                        {
                            "tool_id": tool_id,
                            "status": "failed",
                            "error_code": error_code,
                            "skill_step_id": step.get("skill_step_id"),
                        }
                    )
                    self._update_goal(state)
                    yield self._event(
                        state,
                        "tool.failed",
                        f"{step.get('name', 'Tool')} 暂时不可用，Agent 将基于已有上下文继续",
                        status="failed",
                        actor=self._actor(
                            "tool",
                            tool_id,
                            str(step.get("name") or "Tool"),
                        ),
                        data={"error_code": error_code},
                    )
                    step_status = "failed"
            if step_status == "completed":
                consecutive_failures = 0
            if not state.pending_model_step:
                decision = self._replan_after_feedback(
                    state,
                    step,
                    status=step_status,
                    error_code=error_code,
                )
                if decision:
                    async for event in self._yield_harness_decision(state, decision):
                        yield event
            if state.pending_model_step:
                yield self._event(
                    state,
                    "run.paused",
                    "等待品牌模型返回当前子 Agent 的结果",
                    status="waiting",
                    data={
                        "reason": "external_model",
                        "continuation": state.serializable(),
                    },
                )
                return

        if loop_count >= MAX_LOOP_STEPS and state.cursor < len(state.plan):
            state.goal.status = "failed"
            state.goal.outcome = "Agent Loop 超过最大步数"
            async for event in self._close_phase(state):
                yield event
            yield self._event(state, "run.failed", state.goal.outcome, status="failed")
            return

        async for event in self._close_phase(state):
            yield event

        hard_failures = [
            str(item)
            for item in state.environment.get("quality_hard_failures", [])
        ]
        soft_warnings = [
            str(item)
            for item in state.environment.get("quality_soft_warnings", [])
        ]
        quality_warnings = [*hard_failures, *soft_warnings]
        document = str(state.environment.get("document") or state.current_document)
        if state.environment.get("strict_skill_workflow") and not state.environment.get(
            "strict_skill_document_polished"
        ):
            strict_document = self._assemble_strict_skill_document(state)
            if strict_document and self._document_hash(strict_document) != self._document_hash(document):
                document = strict_document
                state.environment["document"] = document
                state.current_document = document
                yield self._event(
                    state,
                    "document.skill_structure.enforced",
                    "已按 Skill 步骤白名单恢复章节顺序并移除未声明栏目",
                    status="completed",
                    data={"content": document},
                )
        document, placeholder_audit = self._guard_generated_placeholders(
            document,
            state.environment.get("requirement", {}),
        )
        if placeholder_audit:
            state.environment["document"] = document
            state.current_document = document
            state.environment["placeholder_audit"] = placeholder_audit
            yield self._event(
                state,
                "document.placeholders.validated",
                f"已校正或移除 {len(placeholder_audit)} 处错误时间/无数据占位内容",
                status="completed",
                data={"content": document, "audit": placeholder_audit},
            )
        document, citation_audit = self._guard_data_citations(
            document,
            [
                item
                for item in state.environment.get("data_results", [])
                if isinstance(item, dict)
            ],
        )
        if citation_audit:
            state.environment["document"] = document
            state.current_document = document
            state.environment["data_citation_audit"] = citation_audit
            unsupported_count = sum(
                1 for item in citation_audit if item.get("status") == "unsupported"
            )
            if unsupported_count:
                quality_warnings.append(
                    f"{unsupported_count} 处数据引用没有逐项匹配到可用证据，已移除来源归属"
                )
            yield self._event(
                state,
                "document.citations.validated",
                (
                    f"已校正 {len(citation_audit) - unsupported_count} 处数据来源，"
                    f"{unsupported_count} 处无证据引用已移除来源归属"
                ),
                status="completed",
                data={"content": document, "audit": citation_audit},
            )
        document, applied_evidence = self._apply_creation_evidence_cards(
            document,
            [
                item
                for item in state.environment.get("creation_evidence", [])
                if isinstance(item, dict)
            ],
        )
        if applied_evidence:
            state.environment["document"] = document
            state.current_document = document
            state.environment["creation_evidence"] = applied_evidence
            yield self._event(
                state,
                "document.evidence.applied",
                f"已把 {len(applied_evidence)} 张校验通过的即时截图放到对应数据引用下方",
                status="completed",
                data={"content": document, "evidence": applied_evidence},
            )
        document, data_risk_audit = self._apply_data_risk_disclosures(
            document, list(state.environment.get("data_results") or [])
        )
        if data_risk_audit:
            state.environment["document"] = document
            state.current_document = document
            state.environment["data_risk_audit"] = data_risk_audit
            if any(item.get("risk_count") for item in data_risk_audit):
                quality_warnings.append("文档使用了带风险标注的参考数据，请核对实际周期和口径")
            yield self._event(
                state, "document.data_risks.applied",
                "已在数据下方保留参考值、实际周期、来源与风险说明",
                status="completed", data={"content": document, "audit": data_risk_audit},
            )
        state.goal.status = "complete"
        state.goal.remaining_steps = []
        state.goal.outcome = (
            "已生成可用文档，并在执行记录中保留质量风险"
            if quality_warnings
            else "已生成满足当前验收条件的文档"
        )
        yield self._event(
            state,
            "goal.updated",
            state.goal.outcome,
            environment_patch={
                "document_ready": True,
                "quality_warnings": quality_warnings,
            },
        )
        failed_steps = [
            item
            for item in state.environment.get("failed_steps", [])
            if isinstance(item, dict)
        ]
        completed_summary = (
            f"本轮创作完成，其中 {len(failed_steps)} 个节点失败已跳过，可继续对话补充"
            if failed_steps
            else "本轮创作完成，可以继续对话优化文档"
        )
        yield self._event(
            state,
            "run.completed",
            completed_summary,
            status="completed",
            data={
                "document": state.environment.get("document", state.current_document),
                "references": state.environment.get("reference_summaries", []),
                "skills": state.environment.get("applied_skills", []),
                "tools": state.environment.get("tool_results", []),
                "edit_intent": state.environment.get("edit_intent", {}),
                "document_patch": state.environment.get("last_document_patch"),
                "evidence": state.environment.get("creation_evidence", []),
                "failed_steps": failed_steps,
                "goal": asdict(state.goal),
            },
        )

    def _new_state(
        self,
        *,
        user_message: str,
        root_request: Optional[str],
        current_document: str,
        conversation: list[dict[str, str]],
        selected_skills: list[dict[str, Any]],
        options: CreationOptions,
        model_mode: str,
        session_id: Optional[str],
        run_id: Optional[str],
        creation_mode: str = "direct",
        creation_brief: Optional[dict[str, Any]] = None,
    ) -> LoopState:
        message = user_message.strip()
        normalized_conversation = self._normalize_conversation(conversation)
        resolved_root_request = self._resolve_root_request(
            root_request,
            normalized_conversation,
            message,
        )
        mode = "revision" if current_document.strip() else "initial"
        intent = self._interpret_edit_intent(
            message,
            current_document=current_document,
            mode=mode,
        )
        objective = (
            (
                f"以原始需求“{resolved_root_request}”为基线，"
                f"按本轮要求优化现有文档（冲突处以本轮为准）：{message}"
            )
            if mode == "revision"
            else f"生成一份可直接使用的文档：{resolved_root_request}"
        )
        goal = GoalState(
            objective=objective,
            acceptance_criteria=[
                f"保留原始需求中未被本轮替换的约束：{resolved_root_request}",
                "完整回应用户本轮要求",
                "事实与参考资料可追溯，不编造具体数据",
                "结构清晰，输出为可继续编辑的 Markdown 文档",
                "保留现有文档中未被要求删除的有效内容",
            ],
        )
        state = LoopState(
            session_id=session_id or f"session-{uuid4()}",
            run_id=run_id or f"run-{uuid4()}",
            mode=mode,
            model_mode=model_mode,
            creation_mode=creation_mode,
            user_message=message,
            root_request=resolved_root_request,
            current_document=current_document,
            conversation=normalized_conversation,
            options=asdict(options),
            selected_skills=selected_skills[:8],
            goal=goal,
        )
        context_query = (
            f"{resolved_root_request}\n本轮补充：{message}"
            if mode == "revision" and resolved_root_request != message
            else message
        )
        retrieval_query = context_query
        retrieval_context_terms: list[str] = []
        if creation_mode == "brainstorm" and creation_brief:
            state.environment["creation_brief"] = creation_brief
            state.environment["creation_mode"] = "brainstorm"
            retrieval_context_terms = self._brainstorm_retrieval_context_terms(
                creation_brief
            )
            creation_brief_context = self._brainstorm_prompt_context(creation_brief)
            if creation_brief_context:
                state.environment["creation_brief_context"] = creation_brief_context
                context_query = "\n\n".join((context_query, creation_brief_context))
        requirement = self.service.analyze_requirement(
            retrieval_query,
            options,
            retrieval_context_terms=retrieval_context_terms,
        )
        state.environment["requirement"] = requirement
        state.environment["context_query"] = context_query
        state.environment["retrieval_query"] = retrieval_query
        state.environment["retrieval_context_terms"] = retrieval_context_terms
        edit_intent = asdict(intent)
        edit_intent["target_sections"] = list(intent.target_sections)
        state.environment["edit_intent"] = edit_intent
        if mode == "revision":
            state.environment["revision_base_document"] = current_document
        state.plan = self._build_plan(state)
        state.goal.remaining_steps = [item["name"] for item in state.plan]
        return state

    @staticmethod
    def _brainstorm_prompt_context(creation_brief: Any) -> str:
        """把 Core 保存的脑暴状态收敛为可直接给 Agent 消费的有界上下文。

        这里只白名单透传已确认决策、合理假设、开放事项和简报；
        session_id、模型或其他内部字段不得进入模型提示。
        """
        if not isinstance(creation_brief, dict):
            return ""

        confirmed_decisions: list[str] = []
        assumptions: list[str] = []
        raw_decisions = creation_brief.get("decisions")
        if isinstance(raw_decisions, list):
            for raw in raw_decisions[-MAX_BRAINSTORM_CONTEXT_DECISIONS:]:
                if not isinstance(raw, dict):
                    continue
                dimension = re.sub(
                    r"\s+", " ", str(raw.get("dimension") or "").strip()
                )[:MAX_BRAINSTORM_DECISION_DIMENSION_CHARS]
                summary = re.sub(
                    r"\s+", " ", str(raw.get("summary") or "").strip()
                )[:MAX_BRAINSTORM_DECISION_SUMMARY_CHARS]
                if not summary:
                    continue
                line = f"- {dimension}：{summary}" if dimension else f"- {summary}"
                if str(raw.get("source") or "").strip() == "agent_assumption":
                    assumptions.append(line)
                else:
                    confirmed_decisions.append(line)

        open_flags = []
        raw_open_flags = creation_brief.get("open_flags")
        if isinstance(raw_open_flags, list):
            for raw in raw_open_flags[:8]:
                value = re.sub(r"\s+", " ", str(raw or "").strip())[
                    :MAX_BRAINSTORM_OPEN_FLAG_CHARS
                ]
                if value:
                    open_flags.append(f"- {value}")

        blocks = [
            "脑暴创作上下文：已确认决策必须遵守；合理假设不得改写为用户已确认事实；"
            "开放事项不得擅自定论，必要时在文档中明确标注待补充。"
        ]
        if confirmed_decisions:
            blocks.append("已确认决策：\n" + "\n".join(confirmed_decisions))
        if assumptions:
            blocks.append("合理假设：\n" + "\n".join(assumptions))
        if open_flags:
            blocks.append("开放事项：\n" + "\n".join(open_flags))

        brief_markdown = str(creation_brief.get("brief_markdown") or "").strip()
        if brief_markdown:
            prefix = "\n\n".join(blocks)
            remaining = MAX_BRAINSTORM_CONTEXT_CHARS - len(prefix) - len(
                "\n\n当前创作简报：\n"
            )
            if remaining > 0:
                blocks.append("当前创作简报：\n" + brief_markdown[:remaining])

        if len(blocks) == 1:
            return ""
        return "\n\n".join(blocks)[:MAX_BRAINSTORM_CONTEXT_CHARS]

    @staticmethod
    def _brainstorm_retrieval_context_terms(creation_brief: Any) -> list[str]:
        """只提取用户已确认的业务事实，作为低权重检索辅助词。

        推理假设、开放问题、简报模板和“必须遵守”等控制文字
        不得进入召回规划，避免它们被误识别为业务实体。
        """
        if not isinstance(creation_brief, dict):
            return []
        selected: list[str] = []
        raw_decisions = creation_brief.get("decisions")
        if not isinstance(raw_decisions, list):
            return selected
        for raw in raw_decisions[-MAX_BRAINSTORM_CONTEXT_DECISIONS:]:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("source") or "").strip() == "agent_assumption":
                continue
            summary = re.sub(
                r"\s+", " ", str(raw.get("summary") or "").strip()
            )[:MAX_BRAINSTORM_DECISION_SUMMARY_CHARS]
            if summary and summary not in selected:
                selected.append(summary)
            if len(selected) >= 8:
                break
        return selected

    @staticmethod
    def _resolve_root_request(
        root_request: Optional[str],
        conversation: list[dict[str, str]],
        user_message: str,
    ) -> str:
        explicit = (root_request or "").strip()
        if explicit:
            return explicit[:12000]
        for item in conversation:
            if item.get("role") == "user" and str(item.get("content") or "").strip():
                return str(item["content"]).strip()[:12000]
        return user_message[:12000]

    def _interpret_edit_intent(
        self,
        user_message: str,
        *,
        current_document: str,
        mode: str,
    ) -> EditIntent:
        if mode == "initial":
            return EditIntent(
                mode=mode,
                operation="create_document",
                preserve_untouched=False,
                summary="理解为新建文档，将按完整需求生成首版内容",
                reasoning_summary="当前没有可编辑的既有文档，因此需要生成首个完整版本。",
            )

        message = user_message.strip()
        existing_titles = self._markdown_section_titles(current_document)
        targets = self._find_target_sections(message, existing_titles)

        if any(marker in message for marker in GLOBAL_REWRITE_MARKERS):
            return EditIntent(
                mode=mode,
                operation="rewrite_document",
                preserve_untouched=False,
                summary="理解为整篇改写，将重新生成完整文档",
                reasoning_summary="本轮指令明确作用于全文，无法安全限定到单一章节。",
            )

        target_text = "、".join(f"“{target}”" for target in targets)
        if targets:
            summary = f"理解为围绕{target_text}联动修订完整文档"
            reasoning = (
                "目标章节仅作为改动线索；创作 Agent 会结合全文判断实际影响范围，"
                "同步更新目录、摘要、编号、交叉引用及其他受影响章节。"
            )
        else:
            summary = "理解为结合本轮要求修订完整文档"
            reasoning = (
                "本轮要求可能影响多个位置；创作 Agent 会在完整上下文中判断变更范围，"
                "并保留未受影响的有效内容。"
            )
        return EditIntent(
            mode=mode,
            operation="revise_document",
            target_sections=tuple(targets),
            preserve_untouched=True,
            summary=summary,
            reasoning_summary=reasoning,
        )

    @staticmethod
    def _markdown_section_titles(document: str) -> list[str]:
        titles: list[str] = []
        for match in re.finditer(r"(?m)^#{2,6}\s+(.+?)\s*$", document):
            title = re.sub(r"\s+#+\s*$", "", match.group(1)).strip()
            if title and title not in titles:
                titles.append(title)
        return titles

    def _find_target_section(
        self,
        message: str,
        existing_titles: list[str],
    ) -> Optional[str]:
        targets = self._find_target_sections(message, existing_titles)
        return targets[0] if targets else None

    def _find_target_sections(
        self,
        message: str,
        existing_titles: list[str],
    ) -> list[str]:
        compact_message = self._normalize_section_name(message)
        candidates = sorted(
            [*KNOWN_SECTION_TITLES, *existing_titles],
            key=len,
            reverse=True,
        )
        targets: list[str] = []
        for title in candidates:
            normalized = self._normalize_section_name(title)
            if normalized and normalized in compact_message:
                matched = self._match_existing_section(title, existing_titles)
                target = matched or title
                if target not in targets:
                    targets.append(target)

        for quoted in re.finditer(r"[《“\"']([^》”\"']{2,40})[》”\"']", message):
            target = quoted.group(1).strip()
            matched = self._match_existing_section(target, existing_titles)
            target = matched or target
            if target not in targets:
                targets.append(target)

        for marker in (*APPEND_MARKERS, *DELETE_MARKERS, *REPLACE_MARKERS):
            if marker not in message:
                continue
            tail = message.split(marker, 1)[1]
            tail = re.sub(r"^(?:一下|下|一下子|关于|对|把|将|文档中|文档里的)*", "", tail)
            tail = re.split(r"[，。；：,;:\n]", tail, maxsplit=1)[0]
            tail = re.sub(r"(?:章节|部分|内容|这一节|这部分).*$", "", tail).strip()
            for item in re.split(r"(?:以及|并且|同时|和|与|及|、)", tail):
                target = item.strip()
                if not 2 <= len(target) <= 24:
                    continue
                matched = self._match_existing_section(target, existing_titles)
                target = matched or target
                if target not in targets:
                    targets.append(target)
        return targets[:8]

    @classmethod
    def _match_existing_section(
        cls,
        target: str,
        existing_titles: list[str],
    ) -> Optional[str]:
        normalized_target = cls._normalize_section_name(target)
        for title in existing_titles:
            normalized_title = cls._normalize_section_name(title)
            if (
                normalized_title == normalized_target
                or normalized_title in normalized_target
                or normalized_target in normalized_title
            ):
                return title
        return None

    @staticmethod
    def _normalize_section_name(value: str) -> str:
        return re.sub(r"[\s：:、，,。.!！?？（）()《》“”\"'`#_-]+", "", value).lower()

    def _build_plan(self, state: LoopState) -> list[dict[str, Any]]:
        """执行链路先由模型路由决策；未决策前计划中只有路由步骤。"""
        decision = state.environment.get("routing_decision")
        if isinstance(decision, dict):
            return self._compose_plan_from_decision(state, decision)
        return [
            {
                "kind": "agent",
                "id": "creation_main_agent",
                "name": "创作 Agent",
                "action": "route",
            }
        ]

    def _compose_plan_from_decision(
        self,
        state: LoopState,
        decision: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """推理后的代码只做校验与结构兜底：白名单过滤、必备步骤补齐。"""
        enabled_tools = set(
            normalize_creation_tool_ids(state.options.get("enabled_tools"))
        )
        validated = validate_routing_decision(decision)
        routed_tools = [
            item for item in validated["tools"] if item in enabled_tools
        ]
        routed_agents = list(validated["agents"])
        matched_skills = self._match_skills(state)
        plan: list[dict[str, Any]] = [
            {
                "kind": "agent",
                "id": "creation_main_agent",
                "name": "创作 Agent",
                "action": "plan",
            }
        ]
        for skill in matched_skills:
            plan.append(
                {
                    "kind": "skill",
                    "id": str(skill["id"]),
                    "name": str(skill["name"]),
                    "action": "apply_skill",
                    "skill": skill,
                }
            )

        explicit_skills = [
            skill
            for skill in matched_skills
            if skill.get("source") == "installed"
            and skill.get("workflow_role") != "support"
        ]
        strict_skill_workflow = bool(explicit_skills)
        state.environment["strict_skill_workflow"] = strict_skill_workflow
        if strict_skill_workflow:
            state.environment["strict_skill_ids"] = [
                str(skill["id"]) for skill in explicit_skills
            ]
            # 严格 Skill 工作流不注入其他路由工具，但路由决策选中且已启用
            # 的画图工具要补位：用户明确要图时不能因为 Skill 步骤没声明
            # 画图工具就静默丢弃该决策。画图步骤放在写作步骤之前，与
            # 非严格路径“工具先于 Agent”一致，保证撰写时能拿到画图约束；
            # Skill 步骤已声明的画图工具由工作流自己调度，不重复补位。
            declared_diagram_tools: set[str] = set()
            for skill in explicit_skills:
                for raw_step in skill.get("execution_steps", []) or []:
                    if not isinstance(raw_step, dict):
                        continue
                    for raw_tool in raw_step.get("tools", []) or []:
                        declared_tool = str(raw_tool)
                        if declared_tool in (
                            PLANTUML_DIAGRAM_TOOL_ID,
                            MERMAID_DIAGRAM_TOOL_ID,
                        ):
                            declared_diagram_tools.add(declared_tool)
            scheduled_step_ids = {str(item.get("id") or "") for item in plan}
            for diagram_tool_id in (
                PLANTUML_DIAGRAM_TOOL_ID,
                MERMAID_DIAGRAM_TOOL_ID,
            ):
                if (
                    diagram_tool_id in routed_tools
                    and diagram_tool_id in enabled_tools
                    and diagram_tool_id not in scheduled_step_ids
                    and diagram_tool_id not in declared_diagram_tools
                ):
                    diagram_step = self._tool_plan_step(diagram_tool_id)
                    if diagram_step:
                        plan.append(diagram_step)
                        scheduled_step_ids.add(diagram_tool_id)
            for skill in explicit_skills:
                workflow = skill.get("execution_steps", [])
                if not workflow:
                    description = skill.get("skill_description") or {}
                    purpose = (
                        str(description.get("purpose") or "").strip()
                        if isinstance(description, dict)
                        else ""
                    )
                    deliverables = (
                        description.get("deliverables") or []
                        if isinstance(description, dict)
                        else []
                    )
                    workflow = [
                        {
                            "id": "execute-skill",
                            "title": "完成创作",
                            "objective": purpose
                            or str(skill.get("summary") or "严格遵循 Skill 内部规则完成创作"),
                            "output": "；".join(
                                str(item) for item in deliverables if str(item).strip()
                            )
                            or "完整创作结果",
                            "agents": [],
                            "skills": [],
                            "tools": [],
                        }
                    ]
                plan.extend(
                    self._plan_skill_workflow(
                        workflow,
                        skill,
                        enabled_tools,
                    )
                )
            # 每个 Skill 步骤独立推理出产物并按白名单组装后，最后追加一次
            # 有边界的全文整合润色，统一术语与衔接，不改变章节结构。
            # 只有存在至少两个独立推理产物需要合并时才调度，避免单步骤
            # Skill 多一次无意义的大模型调用。
            total_skill_steps = sum(
                max(len(skill.get("execution_steps") or []), 1)
                for skill in explicit_skills
            )
            if total_skill_steps >= 2:
                plan.append(
                    {
                        "kind": "agent",
                        "id": "document_unify_polisher",
                        "name": "全文整合润色 Agent",
                        "action": "polisher",
                        "schedule_key": "skill_polish:final",
                    }
                )
            # Skill 仍然独占业务结构和内容规则；最后只追加与具体任务无关的
            # Markdown 强调检查，避免通用渲染问题绕过质量门禁。
            quality_review_step = self._agent_plan_step("quality_review_agent")
            if quality_review_step:
                plan.append(
                    {
                        **quality_review_step,
                        "quality_issue_codes": list(
                            STRICT_SKILL_QUALITY_ISSUE_CODES
                        ),
                        "schedule_key": "strict_skill:emphasis_review",
                    }
                )
        else:
            if MEMORY_SEARCH_TOOL_ID in enabled_tools:
                plan.append(self._tool_plan_step(MEMORY_SEARCH_TOOL_ID))
            for tool_id in routed_tools:
                if tool_id == DATA_SEARCH_TOOL_ID:
                    # data_search 统一插入到 memory_search 之后，见下方。
                    continue
                if (
                    state.mode == "initial"
                    and tool_id == MERMAID_DIAGRAM_TOOL_ID
                ):
                    # 初稿的 Mermaid 图示要等章节设计产出 Visual Plan 后再按
                    # 章节调度；修订模式没有章节设计步骤，继续沿用即时路由。
                    continue
                tool_step = self._tool_plan_step(tool_id)
                if tool_step:
                    plan.append(tool_step)
            for agent_id in routed_agents:
                agent_step = self._agent_plan_step(agent_id)
                if agent_step:
                    plan.append(agent_step)

        if DATA_SEARCH_TOOL_ID in routed_tools and not strict_skill_workflow:
            # 数据检索与记忆/互联网检索同属证据探针。网页刷新和数据分析是
            # 依赖反馈的动作，不在初始计划中预置固定流水线。
            normalized_plan: list[dict[str, Any]] = []
            data_search_step: Optional[dict[str, Any]] = None
            for item in plan:
                step_id = str(item.get("id") or "")
                if step_id in {WEBPAGE_SCRAPE_TOOL_ID, "data_analysis_agent"}:
                    continue
                if step_id == DATA_SEARCH_TOOL_ID:
                    if data_search_step is None:
                        data_search_step = item
                    continue
                normalized_plan.append(item)
            plan = normalized_plan
            insert_at = 1
            while (
                insert_at < len(plan)
                and plan[insert_at].get("kind") == "skill"
                and plan[insert_at].get("action") == "apply_skill"
            ):
                insert_at += 1
            memory_positions = [
                index
                for index, item in enumerate(plan)
                if str(item.get("id") or "") == MEMORY_SEARCH_TOOL_ID
            ]
            if memory_positions:
                insert_at = memory_positions[0] + 1
            plan.insert(
                insert_at,
                data_search_step or self._tool_plan_step(DATA_SEARCH_TOOL_ID),
            )

        # 明确选择 Skill 时，execution_steps 是唯一的业务执行契约。除通用的
        # Markdown 强调质量门禁外，只有步骤声明的 Agent/Tool 能进入初始计划；
        # data_search 命中实时报表后所需的受控网页采集依赖由反馈阶段补齐。
        if strict_skill_workflow:
            return plan

        scheduled_actions = {str(item.get("id")) for item in plan}
        if "document_writer_agent" not in scheduled_actions:
            plan.append(self._agent_plan_step("document_writer_agent"))
        if state.mode == "initial":
            # 未明确选择 Skill 时，章节设计是通用初稿链路的显式前置产物。
            plan = [
                item
                for item in plan
                if str(item.get("id") or "") != "chapter_design_agent"
            ]
            writer_index = next(
                index
                for index, item in enumerate(plan)
                if str(item.get("id") or "") == "document_writer_agent"
            )
            plan.insert(
                writer_index,
                self._agent_plan_step("chapter_design_agent"),
            )
        scheduled_actions = {str(item.get("id")) for item in plan}
        if "quality_review_agent" not in scheduled_actions:
            plan.append(self._agent_plan_step("quality_review_agent"))
        return plan

    async def _apply_routing_decision(
        self,
        state: LoopState,
        step: dict[str, Any],
        decision: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """应用路由决策：只做校验与重建计划，不改变决策内容。"""
        record = validate_routing_decision(decision)
        record["source"] = (
            str(decision.get("source") or "model")
            if isinstance(decision, dict)
            else "model"
        )
        reasoning = ""
        if isinstance(decision, dict):
            reasoning = str(decision.get("reasoning") or "").strip()
        if reasoning:
            record["reasoning"] = reasoning[:200]
        state.environment["routing_decision"] = record
        state.plan = self._compose_plan_from_decision(state, record)
        state.cursor = 0
        self._update_goal(state)
        selected = [str(item.get("name") or item.get("id")) for item in state.plan[1:]]
        summary = (
            "已由模型决定执行链路：" + "、".join(selected)
            if selected
            else "已由模型决定执行链路，无需额外检索能力"
        )
        yield self._event(
            state,
            "agent.completed",
            summary,
            status="completed",
            actor=self._actor(
                "agent",
                str(step.get("id") or "creation_main_agent"),
                str(step.get("name") or "创作 Agent"),
            ),
            environment_patch={"routing_decision": record},
            data={"routing_decision": record},
        )
        yield self._thinking_completed(
            state,
            "routing",
            str(record.get("reasoning") or ""),
        )

    def _replan_after_feedback(
        self,
        state: LoopState,
        step: dict[str, Any],
        *,
        status: str,
        error_code: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Harness 只依据可观察 Tool 反馈追加下一步，不预演固定链路。"""
        step_id = str(step.get("id") or "")
        step_key = self._step_schedule_key(step)
        completed_ids = state.environment.setdefault("harness_completed_step_ids", [])
        if status == "completed" and step_key and step_key not in completed_ids:
            completed_ids.append(step_key)
        strict_skill_workflow = bool(
            state.environment.get("strict_skill_workflow")
        )
        # 明确 Skill 不追加会改变业务内容的通用写作或分析能力；但允许通用的
        # Markdown 强调检查，以及 data_search 命中报表后的受控采集依赖。
        if strict_skill_workflow and step_id not in {
            DATA_SEARCH_TOOL_ID,
            WEBPAGE_SCRAPE_TOOL_ID,
            "data_query_planner",
            "quality_review_agent",
        }:
            return None
        if step_id == "chapter_design_agent":
            visual_plan = state.environment.get("visual_plan")
            diagrams = (
                visual_plan.get("diagrams", [])
                if isinstance(visual_plan, dict)
                else []
            )
            enabled_tools = set(
                normalize_creation_tool_ids(state.options.get("enabled_tools"))
            )
            routed_tools = set(
                (state.environment.get("routing_decision") or {}).get("tools", [])
            )
            explicit_mermaid_request = (
                MERMAID_DIAGRAM_TOOL_ID in routed_tools and not diagrams
            )
            candidates: list[Optional[dict[str, Any]]] = []
            if status == "completed" and MERMAID_DIAGRAM_TOOL_ID in enabled_tools:
                for spec in diagrams:
                    if not isinstance(spec, dict):
                        continue
                    diagram_step = self._tool_plan_step(MERMAID_DIAGRAM_TOOL_ID)
                    if not diagram_step:
                        continue
                    diagram_id = str(spec.get("id") or len(candidates) + 1)
                    section_title = str(spec.get("section_title") or "").strip()
                    candidates.append(
                        {
                            **diagram_step,
                            "name": (
                                f"{section_title} · Mermaid 画图 Tool"
                                if section_title
                                else diagram_step["name"]
                            ),
                            "diagram_spec": spec,
                            "schedule_key": f"mermaid_visual_plan:{diagram_id}",
                        }
                    )
                if explicit_mermaid_request:
                    fallback_step = self._tool_plan_step(MERMAID_DIAGRAM_TOOL_ID)
                    if fallback_step:
                        candidates.append(
                            {
                                **fallback_step,
                                "schedule_key": "mermaid_visual_plan:explicit_request",
                            }
                        )
            inserted = self._insert_harness_steps(state, candidates)
            if status != "completed":
                reason_code = "chapter_design_failed"
            elif (
                (diagrams or explicit_mermaid_request)
                and MERMAID_DIAGRAM_TOOL_ID not in enabled_tools
            ):
                reason_code = "visual_tool_disabled"
            elif diagrams:
                reason_code = "visual_plan_ready"
            elif explicit_mermaid_request:
                reason_code = "explicit_visual_request"
            else:
                reason_code = "visual_plan_empty"
            decision = {
                "trigger": step_id,
                "trigger_status": status,
                "reason_code": reason_code,
                "diagram_count": len(diagrams) or int(explicit_mermaid_request),
                "scheduled": [str(item.get("id") or "") for item in inserted],
                "error_code": error_code,
            }
            state.environment.setdefault("harness_decisions", []).append(decision)
            self._update_goal(state)
            return decision
        if step_id == "quality_review_agent":
            return self._replan_quality_issues(state, status=status)
        if step_id == "data_query_planner":
            query_plans = [
                item
                for item in state.environment.get("data_query_plans", [])
                if isinstance(item, dict)
            ]
            latest_plan = query_plans[-1] if query_plans else {}
            scheduled_steps: list[dict[str, Any]] = []
            if latest_plan.get("mode") == "narrative" and not strict_skill_workflow:
                scheduled_steps.append(self._agent_plan_step("data_analysis_agent"))
            inserted = self._insert_harness_steps(state, scheduled_steps)
            decision = {
                "trigger": step_id,
                "trigger_status": status,
                "reason_code": (
                    "narrative_analysis_required"
                    if scheduled_steps
                    else "query_execution_complete"
                ),
                "result_count": len(
                    state.environment.get("current_data_results") or []
                ),
                "refreshable_count": 0,
                "analyzable_count": 0,
                "scheduled": [item["id"] for item in inserted],
                "error_code": error_code,
            }
            state.environment.setdefault("harness_decisions", []).append(decision)
            self._update_goal(state)
            return decision
        if step_id not in {DATA_SEARCH_TOOL_ID, WEBPAGE_SCRAPE_TOOL_ID}:
            return None

        results = [
            item
            for item in (
                state.environment.get("current_data_results")
                or state.environment.get("data_results", [])
            )
            if isinstance(item, dict)
        ]
        refreshable_count = len(
            CreationService._select_refreshable_report_sources(results)
        )
        analyzable_count = sum(1 for item in results if self._has_analyzable_data(item))
        scheduled_steps: list[dict[str, Any]] = []
        reason_code: str

        if step_id == DATA_SEARCH_TOOL_ID:
            if status != "completed":
                reason_code = "data_search_failed"
            elif refreshable_count > 0:
                scheduled_steps.append(self._tool_plan_step(WEBPAGE_SCRAPE_TOOL_ID))
                reason_code = "refresh_required"
            elif relation_catalog(results):
                scheduled_steps.append(self._agent_plan_step("data_query_planner"))
                reason_code = "structured_data_ready"
            elif analyzable_count > 0:
                if not strict_skill_workflow:
                    scheduled_steps.append(self._agent_plan_step("data_analysis_agent"))
                reason_code = "snapshot_ready"
            elif results:
                reason_code = "source_metadata_only"
            else:
                reason_code = "no_matching_data"
        elif relation_catalog(results):
            scheduled_steps.append(self._agent_plan_step("data_query_planner"))
            reason_code = "structured_data_ready"
        elif analyzable_count > 0:
            if not strict_skill_workflow:
                scheduled_steps.append(self._agent_plan_step("data_analysis_agent"))
            reason_code = (
                "refresh_failed_stale_snapshot_available"
                if status != "completed"
                else "refresh_feedback_ready"
            )
        else:
            reason_code = (
                "refresh_failed_without_snapshot"
                if status != "completed"
                else "refresh_returned_no_analyzable_data"
            )

        if step.get("skill_step_id"):
            metadata = {
                key: value
                for key, value in step.items()
                if key.startswith("skill_step_") or key == "skill_id"
            }
            scoped_steps: list[Optional[dict[str, Any]]] = []
            for candidate in scheduled_steps:
                if not candidate:
                    scoped_steps.append(candidate)
                    continue
                scoped = {**candidate, **metadata}
                skill_id = str(step.get("skill_id") or "")
                skill_step_id = str(step.get("skill_step_id") or "")
                candidate_id = str(candidate.get("id") or "")
                candidate_kind = str(candidate.get("kind") or "")
                schedule_kind = "tool" if candidate_kind == "tool" else "agent"
                scoped["schedule_key"] = (
                    f"skill_{schedule_kind}:{skill_id}:"
                    f"{skill_step_id}:{candidate_id}"
                )
                step_title = str(step.get("skill_step_title") or "").strip()
                if step_title:
                    scoped["name"] = f"{step_title} · {candidate.get('name')}"
                scoped_steps.append(scoped)
            scheduled_steps = scoped_steps

        inserted = self._insert_harness_steps(state, scheduled_steps)
        decision = {
            "trigger": step_id,
            "trigger_status": status,
            "reason_code": reason_code,
            "result_count": len(results),
            "refreshable_count": refreshable_count,
            "analyzable_count": analyzable_count,
            "scheduled": [item["id"] for item in inserted],
            "error_code": error_code,
        }
        state.environment.setdefault("harness_decisions", []).append(decision)
        self._update_goal(state)
        return decision

    def _replan_quality_issues(
        self,
        state: LoopState,
        *,
        status: str,
    ) -> dict[str, Any]:
        issues = [
            item
            for item in state.environment.get("quality_issues", [])
            if isinstance(item, dict)
        ]
        if state.environment.get("strict_skill_workflow"):
            allowed_codes = set(STRICT_SKILL_QUALITY_ISSUE_CODES)
            issues = [
                item
                for item in issues
                if str(item.get("code") or "") in allowed_codes
            ]
        # 对应 Agent 已经针对同一问题做过一轮修复、质检仍然报同样问题时，
        # 说明它不在自动修复的可控范围内，不能再次调度同一 Agent 重写全文，
        # 否则质检循环会一直反复更新文档。
        attempted_issue_keys = {
            (str(mutation.get("agent_id") or ""), str(code))
            for mutation in state.environment.get("quality_mutations", [])
            if isinstance(mutation, dict)
            for code in (mutation.get("issue_codes") or [])
        }
        actionable_issues = [
            item
            for item in issues
            if (
                str(item.get("agent_id") or ""),
                str(item.get("code") or ""),
            )
            not in attempted_issue_keys
        ]
        deferred_issue_codes = [
            str(item.get("code") or "")
            for item in issues
            if item not in actionable_issues
        ]
        if status != "completed":
            reason_code = "quality_review_failed"
            candidates: list[Optional[dict[str, Any]]] = []
        elif not issues:
            reason_code = "quality_gate_passed"
            candidates = []
        elif state.quality_cycles >= MAX_QUALITY_CYCLES:
            reason_code = "quality_cycle_budget_exhausted"
            candidates = []
        else:
            # 先用“下一轮”编号构造候选步骤；只有真正插入修复步骤时才提交轮次，
            # 避免在没有可执行动作时空耗质检预算。
            cycle = state.quality_cycles + 1
            candidates = []
            if not actionable_issues:
                reason_code = "quality_issues_deferred"
            else:
                reason_code = "quality_issues_detected"
            hard_issues = [
                item
                for item in actionable_issues
                if item.get("severity") == "hard"
            ]
            if hard_issues:
                if state.writer_revisions < 1:
                    state.writer_revisions += 1
                    candidates.append(
                        self._quality_cycle_step("document_writer_agent", cycle)
                    )
                else:
                    reason_code = "hard_failure_retry_exhausted"
            elif actionable_issues:
                enabled_tools = set(
                    normalize_creation_tool_ids(state.options.get("enabled_tools"))
                )
                requested_agents = {
                    str(item.get("agent_id") or "") for item in actionable_issues
                }
                required_capabilities = {
                    str(capability)
                    for item in actionable_issues
                    for capability in item.get("required_capabilities", [])
                    if str(capability)
                }
                if (
                    DATA_SEARCH_TOOL_ID in required_capabilities
                    and not state.environment.get("data_results")
                ):
                    candidates.append(self._tool_plan_step(DATA_SEARCH_TOOL_ID))
                if (
                    "data_analysis_agent" in required_capabilities
                    and not state.environment.get("data_analysis")
                    and state.environment.get("data_results")
                ):
                    candidates.append(
                        self._quality_cycle_step("data_analysis_agent", cycle)
                    )
                if (
                    PLANTUML_DIAGRAM_TOOL_ID in required_capabilities
                    and PLANTUML_DIAGRAM_TOOL_ID in enabled_tools
                    and not state.environment.get("plantuml_diagram")
                ):
                    candidates.append(self._tool_plan_step(PLANTUML_DIAGRAM_TOOL_ID))
                if (
                    MERMAID_DIAGRAM_TOOL_ID in required_capabilities
                    and MERMAID_DIAGRAM_TOOL_ID in enabled_tools
                    and not state.environment.get("mermaid_diagram")
                ):
                    candidates.append(self._tool_plan_step(MERMAID_DIAGRAM_TOOL_ID))
                candidates.extend(
                    self._quality_skill_steps(state, actionable_issues, cycle)
                )
                for agent_id in QUALITY_AGENT_ORDER:
                    if agent_id not in requested_agents:
                        continue
                    candidates.append(self._quality_cycle_step(agent_id, cycle))
            if candidates:
                state.quality_cycles = cycle
                state.environment["quality_cycle"] = cycle
                review = self._quality_cycle_step("quality_review_agent", cycle)
                if review:
                    if state.environment.get("strict_skill_workflow"):
                        review["quality_issue_codes"] = list(
                            STRICT_SKILL_QUALITY_ISSUE_CODES
                        )
                    candidates.append(review)
            elif reason_code == "quality_issues_detected":
                # 所有可调度对象都已完成过或不可调度，本轮没有新增步骤，
                # 退回“已尝试修复但仍有遗留”的收敛状态，避免空转。
                reason_code = "quality_issues_deferred"

        inserted = self._insert_harness_steps(state, candidates)
        activated_skills = [
            str(item.get("skill_id") or "")
            for item in inserted
            if item.get("kind") == "skill"
        ]
        decision = {
            "trigger": "quality_review_agent",
            "trigger_status": status,
            "reason_code": reason_code,
            "quality_cycle": state.quality_cycles,
            "issue_count": len(issues),
            "issue_codes": [str(item.get("code") or "") for item in issues],
            "deferred_issue_codes": deferred_issue_codes,
            "scheduled": [
                str(item.get("id") or "")
                for item in inserted
                if item.get("kind") != "skill"
            ],
            "activated_skills": activated_skills,
            "error_code": None,
        }
        state.environment.setdefault("harness_decisions", []).append(decision)
        self._update_goal(state)
        return decision

    def _quality_skill_steps(
        self,
        state: LoopState,
        issues: list[dict[str, Any]],
        cycle: int,
    ) -> list[dict[str, Any]]:
        """把质检声明的 Skill 能力解析成受控、可观察的上下文激活步骤。"""
        requested = {
            str(capability)
            for issue in issues
            for capability in issue.get("required_capabilities", [])
            if str(capability).startswith("skill:")
        }
        if not requested:
            return []

        remaining = set(requested)
        steps: list[dict[str, Any]] = []
        for skill in state.environment.get("applied_skills", []):
            if not isinstance(skill, dict):
                continue
            matched = [
                capability
                for capability in sorted(remaining)
                if any(
                    bool(skill.get(field))
                    for field in QUALITY_SKILL_CAPABILITY_FIELDS.get(capability, ())
                )
            ]
            if not matched:
                continue
            skill_id = str(skill.get("id") or "")
            if not skill_id:
                continue
            issue_codes = [
                str(issue.get("code") or "")
                for issue in issues
                if any(
                    str(capability) in matched
                    for capability in issue.get("required_capabilities", [])
                )
            ]
            steps.append(
                {
                    "kind": "skill",
                    "id": f"skill:{skill_id}",
                    "skill_id": skill_id,
                    "name": f"{skill.get('name') or skill_id} · 质检复用",
                    "action": "activate_quality_skill",
                    "skill": skill,
                    "quality_cycle": cycle,
                    "quality_issue_codes": issue_codes,
                    "matched_capabilities": matched,
                    "schedule_key": f"skill:{skill_id}:quality:{cycle}",
                }
            )
            remaining.difference_update(matched)
            if not remaining or len(steps) >= 2:
                break
        return steps

    def _quality_cycle_step(
        self,
        agent_id: str,
        cycle: int,
    ) -> Optional[dict[str, Any]]:
        step = self._agent_plan_step(agent_id)
        if not step:
            return None
        return {
            **step,
            "quality_cycle": cycle,
            "schedule_key": f"{agent_id}:quality:{cycle}",
        }

    @staticmethod
    def _has_analyzable_data(item: dict[str, Any]) -> bool:
        if item.get("source_kind") == "report_url":
            evidence = item.get("creation_evidence")
            if not isinstance(evidence, dict) or evidence.get("validation_status") != "verified":
                return False
            if item.get("can_use") is not True:
                return False
        return bool(item.get("content_excerpt")) or item.get("structured_data") is not None

    @staticmethod
    def _step_schedule_key(step: dict[str, Any]) -> str:
        return str(step.get("schedule_key") or step.get("id") or "")

    @staticmethod
    def _insert_harness_steps(
        state: LoopState,
        candidates: list[Optional[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        completed = set(state.environment.get("harness_completed_step_ids", []))
        future_ids = {
            CreationAgentLoop._step_schedule_key(item)
            for item in state.plan[state.cursor :]
        }
        inserted: list[dict[str, Any]] = []
        for candidate in candidates:
            if not candidate:
                continue
            step_id = CreationAgentLoop._step_schedule_key(candidate)
            if step_id in completed or step_id in future_ids:
                continue
            inserted.append(candidate)
            future_ids.add(step_id)
        if inserted:
            state.plan[state.cursor : state.cursor] = inserted
        return inserted

    def _thinking_started(self, state: LoopState, stage: str) -> dict[str, Any]:
        """发出深度思考开始事件，供页面展示呼吸灯式思考状态。"""
        label = THINKING_STAGE_LABELS.get(stage, "")
        summary = f"深度思考中：{label}" if label else "深度思考中"
        return self._event(
            state,
            "thinking.started",
            summary,
            actor=self._actor("agent", "creation_main_agent", "创作 Agent"),
            data={"stage": stage},
        )

    # 无 Skill 流程时，按计划步骤给出的宏观阶段标题。
    FRIENDLY_PHASE_TITLES = {
        "memory_search": "检索本地记忆资料",
        "internet_search": "检索外部资料",
        "data_search": "检索数据来源",
        "webpage_scrape": "刷新网页实时数据",
        "github_search": "检索 GitHub 线索",
        "plantuml_diagram": "准备图表绘制",
        "mermaid_diagram": "准备 Mermaid 图示",
        "document_writer_agent": "生成文档内容",
        "chapter_design_agent": "设计章节结构",
        "quality_review_agent": "质量审校",
        "anti_ai_style_agent": "润色行文风格",
        "document_unify_polisher": "全文整合润色",
        "data_analysis_agent": "分析数据快照",
        "data_query_plan": "编译并执行数据查询",
        "data_query_planner": "编译并执行数据查询",
    }

    def _step_purpose(self, step: dict[str, Any]) -> str:
        """步骤目的：优先 Skill 步骤标题，其次步骤名中的“标题 · 能力”前缀。"""
        title = str(step.get("skill_step_title") or "").strip()
        if title:
            return title
        name = str(step.get("name") or "")
        if " · " in name:
            return name.split(" · ")[0].strip()
        return ""

    @classmethod
    def _friendly_phase_title(cls, step: dict[str, Any]) -> str:
        title = cls.FRIENDLY_PHASE_TITLES.get(str(step.get("action") or ""))
        if not title:
            title = cls.FRIENDLY_PHASE_TITLES.get(str(step.get("id") or ""))
        return title or str(step.get("name") or "执行当前步骤")

    @classmethod
    def _phase_of_step(cls, step: dict[str, Any]) -> Optional[tuple]:
        """步骤所属的顶层执行阶段 (phase_id, phase_title, phase_kind)。

        Skill 流程里同一个 Skill 步骤的 Tool/Agent/Writer 归入同一阶段；
        无 Skill 时每个计划步骤自成一个宏观阶段。准备类步骤不入阶段。
        """
        skill_step_id = str(step.get("skill_step_id") or "").strip()
        if skill_step_id:
            title = str(step.get("skill_step_title") or "Skill 步骤").strip()
            return (f"skill_step:{skill_step_id}", title, "skill_step")
        action = str(step.get("action") or "")
        if action in {"plan", "route", "apply_skill", "activate_quality_skill"}:
            return None
        step_id = str(step.get("id") or "")
        return (
            f"step:{step_id or action}",
            cls._friendly_phase_title(step),
            "plan_step",
        )

    async def _switch_phase(
        self, state: LoopState, phase: Optional[tuple]
    ) -> AsyncIterator[dict[str, Any]]:
        """在顶层阶段边界发出 phase.completed / phase.started 事件。"""
        current = state.environment.get("current_phase")
        if phase is None:
            return
        phase_id, title, kind = phase
        if isinstance(current, dict) and current.get("id") == phase_id:
            return
        if isinstance(current, dict) and current.get("id"):
            yield self._event(
                state,
                "phase.completed",
                str(current.get("title") or ""),
                status="completed",
                actor=self._actor("agent", "creation_main_agent", "创作 Agent"),
                data={
                    "phase_id": str(current.get("id")),
                    "phase_title": str(current.get("title") or ""),
                    "phase_kind": str(current.get("kind") or "plan_step"),
                },
            )
        state.environment["current_phase"] = {
            "id": phase_id,
            "title": title,
            "kind": kind,
        }
        yield self._event(
            state,
            "phase.started",
            title,
            actor=self._actor("agent", "creation_main_agent", "创作 Agent"),
            data={
                "phase_id": phase_id,
                "phase_title": title,
                "phase_kind": kind,
            },
        )

    async def _close_phase(self, state: LoopState) -> AsyncIterator[dict[str, Any]]:
        """收尾时关闭仍在进行的顶层阶段。"""
        current = state.environment.get("current_phase")
        if not isinstance(current, dict) or not current.get("id"):
            return
        state.environment["current_phase"] = None
        yield self._event(
            state,
            "phase.completed",
            str(current.get("title") or ""),
            status="completed",
            actor=self._actor("agent", "creation_main_agent", "创作 Agent"),
            data={
                "phase_id": str(current.get("id")),
                "phase_title": str(current.get("title") or ""),
                "phase_kind": str(current.get("kind") or "plan_step"),
            },
        )

    async def _yield_plan_outline(self, state: LoopState) -> AsyncIterator[dict[str, Any]]:
        """无 Skill 流程时，在规划阶段宏观总结接下来要执行的步骤。"""
        titles: list = []
        for item in state.plan[1:]:
            phase = self._phase_of_step(item)
            if phase and phase[1] not in titles:
                titles.append(phase[1])
        if not titles:
            return
        outline = "、".join(
            f"{index}. {title}" for index, title in enumerate(titles, start=1)
        )
        yield self._thinking_started(state, "planning")
        yield self._thinking_completed(
            state,
            "planning",
            f"围绕当前目标，接下来依次执行：{outline}；每一步再决定具体调用的 Tool 与 Agent。",
        )

    def _thinking_completed(
        self,
        state: LoopState,
        stage: str,
        reasoning: str,
    ) -> dict[str, Any]:
        """发出深度思考完成事件，reasoning 为该阶段面向用户的推理摘要。"""
        label = THINKING_STAGE_LABELS.get(stage, "")
        summary = f"深度思考完成：{label}" if label else "深度思考完成"
        return self._event(
            state,
            "thinking.completed",
            summary,
            status="completed",
            actor=self._actor("agent", "creation_main_agent", "创作 Agent"),
            data={"stage": stage, "reasoning": (reasoning or "").strip()[:400]},
        )

    def _step_content_title(self, step: dict[str, Any]) -> Optional[str]:
        """内容生成步骤的具体内容标题；通用能力步骤返回 None。

        主创作 Agent 承接具体步骤时名称为“创作 Agent · 标题”，
        动作行标题只保留标题部分，避免“创作 Agent”反复出现。
        """
        name = str(step.get("name") or "")
        prefix = "创作 Agent · "
        if step.get("id") == "creation_main_agent" and name.startswith(prefix):
            return name[len(prefix):]
        return None

    def _generation_reasoning(self, state: LoopState, step: dict[str, Any]) -> str:
        """内容生成类思考的推理摘要：强调大模型产出与写回动作。"""
        objective = str(state.goal.objective or "").strip()
        prefix = f"围绕「{objective}」，" if objective else ""
        title = self._step_content_title(step)
        if title:
            return f"{prefix}调用大模型生成「{title}」内容，并把结果写回创作文档"
        return f"{prefix}{step.get('name') or '创作能力'}调用大模型生成内容，并把结果写回创作文档"

    async def _yield_harness_decision(
        self,
        state: LoopState,
        decision: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """用深度思考事件包裹 Harness 反馈决策，让规划过程可观察。"""
        yield self._thinking_started(state, "planning")
        yield self._harness_decision_event(state, decision)
        reason_code = str(decision.get("reason_code") or "")
        reasoning = HARNESS_REASON_TEXTS.get(reason_code, "已根据本次反馈完成判断")
        yield self._thinking_completed(state, "planning", reasoning)

    def _harness_decision_event(
        self,
        state: LoopState,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        scheduled = decision.get("scheduled") or []
        activated_skills = decision.get("activated_skills") or []
        if scheduled or activated_skills:
            additions = [*activated_skills, *scheduled]
            summary = f"已根据反馈补充 {len(additions)} 项后续处理"
        elif decision.get("reason_code") == "quality_gate_passed":
            summary = "质量检查通过"
        elif decision.get("reason_code") == "quality_cycle_budget_exhausted":
            summary = "已达到自动优化上限，剩余问题需要人工复核"
        elif decision.get("reason_code") == "quality_issues_deferred":
            summary = "已尝试自动修复但仍有遗留，保留当前版本不再重写"
        else:
            summary = "已根据反馈保留当前处理计划"
        return self._event(
            state,
            "harness.decision",
            summary,
            status="completed",
            actor=self._actor("agent", "creation_main_agent", "创作 Agent"),
            environment_patch={"harness_decision": decision},
            data=decision,
        )

    @staticmethod
    def _structure_requirements_from_action(action: Any) -> dict[str, Any]:
        """从执行动作的自然语言中提取可确定校验的章节数量与字数要求。"""
        text = str(action or "").strip()
        count_patterns = (
            r"(?:至少|最少|不少于)(?:形成|包含|设置|展开|设计|输出)?\s*"
            r"(\d+)\s*个?\s*(?:(?:三级|四级|三级或四级|三四级)\s*)?"
            r"(?:子章节|细节章节|详细章节)",
            r"(?:子章节|细节章节|详细章节)(?:数量)?\s*(?:至少|最少|不少于)\s*"
            r"(\d+)\s*个?",
        )
        length_patterns = (
            r"每(?:个|一)?\s*(?:(?:三级|四级|三级或四级|三四级)\s*)?"
            r"(?:子章节|细节章节|详细章节|章节|节)(?:的)?(?:正文|内容)?\s*"
            r"(?:不少于|至少|最少)\s*(\d+)\s*(?:字|字符)",
            r"每(?:节|章)(?:正文|内容)?\s*(?:不少于|至少|最少)\s*"
            r"(\d+)\s*(?:字|字符)",
        )

        def largest(patterns: tuple[str, ...], upper: int) -> Optional[int]:
            values: list[int] = []
            for pattern in patterns:
                values.extend(int(item) for item in re.findall(pattern, text))
            values = [item for item in values if 1 <= item <= upper]
            return max(values) if values else None

        return {
            "minimum_subsections": largest(count_patterns, 30),
            "minimum_subsection_chars": largest(length_patterns, 5000),
            "source_text": text[:500],
        }

    @classmethod
    def _skill_structure_requirements(cls, skill: Any) -> dict[str, Any]:
        merged: dict[str, Any] = {
            "minimum_subsections": None,
            "minimum_subsection_chars": None,
            "source_text": "",
        }
        if not isinstance(skill, dict):
            return merged
        sources: list[str] = []
        for raw_step in skill.get("execution_steps", []) or []:
            if not isinstance(raw_step, dict):
                continue
            current = cls._structure_requirements_from_action(
                raw_step.get("objective")
            )
            for key in ("minimum_subsections", "minimum_subsection_chars"):
                value = current.get(key)
                if value is not None:
                    merged[key] = max(int(merged[key] or 0), int(value))
            if current["minimum_subsections"] or current["minimum_subsection_chars"]:
                sources.append(current["source_text"])
        merged["source_text"] = "\n".join(sources)[:1500]
        return merged

    def _plan_skill_workflow(
        self,
        workflow: list[dict[str, Any]],
        skill: dict[str, Any],
        enabled_tools: set[str],
    ) -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        for raw_step in workflow[:12]:
            if not isinstance(raw_step, dict):
                continue
            step_id = str(raw_step.get("id") or "")
            step_title = str(raw_step.get("title") or step_id or "技能步骤")
            step_skills = [
                str(item)
                for item in raw_step.get("skills", [])
                if str(item or "").strip()
            ][:8]
            metadata = {
                "skill_id": str(skill["id"]),
                "skill_step_id": step_id,
                "skill_step_title": step_title,
                "skill_step_objective": str(raw_step.get("objective") or ""),
                "skill_step_output": str(raw_step.get("output") or ""),
                "skill_step_skills": step_skills,
                "skill_step_structure_requirements": (
                    self._structure_requirements_from_action(
                        raw_step.get("objective")
                    )
                ),
                # 旧 Skill 没有该字段时默认静默取数。截图必须由 Skill
                # 明确开启，不能把历史缺省值解释为前台操作授权。
                "skill_step_retain_webpage_screenshot": bool(
                    raw_step.get(
                        "retainWebpageScreenshot",
                        raw_step.get("retain_webpage_screenshot", False),
                    )
                ),
            }
            scheduled_in_step: set[str] = set()
            resource_count = 0
            for tool_id in raw_step.get("tools", []):
                tool_id = str(tool_id)
                if (
                    tool_id not in enabled_tools
                    or tool_id in scheduled_in_step
                    or resource_count >= MAX_SKILL_STEP_RESOURCES
                ):
                    continue
                tool_step = self._tool_plan_step(tool_id)
                if tool_step:
                    plan.append(
                        {
                            **tool_step,
                            **metadata,
                            "name": f"{step_title} · {tool_step['name']}",
                            "schedule_key": (
                                f"skill_tool:{skill['id']}:"
                                f"{step_id or len(plan) + 1}:{tool_id}"
                            ),
                        }
                    )
                    scheduled_in_step.add(tool_id)
                    resource_count += 1
            for agent_id in raw_step.get("agents", []):
                agent_id = str(agent_id)
                if (
                    agent_id in scheduled_in_step
                    or resource_count >= MAX_SKILL_STEP_RESOURCES
                ):
                    continue
                agent_step = self._agent_plan_step(agent_id)
                if agent_step:
                    plan.append(
                        {
                            **agent_step,
                            **metadata,
                            "name": f"{step_title} · {agent_step['name']}",
                            "schedule_key": (
                                f"skill_agent:{skill['id']}:"
                                f"{step_id or len(plan) + 1}:{agent_id}"
                            ),
                        }
                    )
                    scheduled_in_step.add(agent_id)
                    resource_count += 1
            has_document_agent = bool(
                scheduled_in_step
                & {
                    "document_writer_agent",
                    "quality_review_agent",
                    *QUALITY_AGENT_ORDER,
                }
            )
            if has_document_agent:
                plan.append(
                    {
                        "kind": "skill",
                        "id": f"{skill['id']}:{step_id or len(plan) + 1}",
                        "name": f"{skill['name']} · {step_title}",
                        "action": "activate_skill_step",
                        **metadata,
                    }
                )
            else:
                # Tool 与显式子 Agent 只提供本步骤所需资源；未声明 Writer 时，
                # 由主创作 Agent 自己完成步骤目标，不能暗中创建“步骤整理”子 Agent。
                plan.append(
                    {
                        "kind": "agent",
                        "id": "creation_main_agent",
                        "name": f"创作 Agent · {step_title}",
                        "action": "skill_step",
                        "schedule_key": (
                            f"skill_step:{skill['id']}:"
                            f"{step_id or len(plan) + 1}"
                        ),
                        **metadata,
                    }
                )
        return plan

    @staticmethod
    def _tool_plan_step(tool_id: str) -> Optional[dict[str, Any]]:
        definitions = {
            MEMORY_SEARCH_TOOL_ID: ("记忆搜索 Tool", "memory_search"),
            INTERNET_SEARCH_TOOL_ID: ("互联网检索 Tool", "internet_search"),
            DATA_SEARCH_TOOL_ID: ("数据检索 Tool", DATA_SEARCH_TOOL_ID),
            WEBPAGE_SCRAPE_TOOL_ID: ("网页爬取 Tool", WEBPAGE_SCRAPE_TOOL_ID),
            GITHUB_SEARCH_TOOL_ID: ("GitHub 检索 Tool", GITHUB_SEARCH_TOOL_ID),
            PLANTUML_DIAGRAM_TOOL_ID: (
                "PlantUML 画图 Tool",
                PLANTUML_DIAGRAM_TOOL_ID,
            ),
            MERMAID_DIAGRAM_TOOL_ID: (
                "Mermaid 画图 Tool",
                MERMAID_DIAGRAM_TOOL_ID,
            ),
        }
        definition = definitions.get(tool_id)
        if not definition:
            return None
        name, action = definition
        return {
            "kind": "tool",
            "id": tool_id,
            "name": name,
            "action": action,
        }

    @staticmethod
    def _agent_plan_step(agent_id: str) -> Optional[dict[str, Any]]:
        definitions = {
            "industry_research_agent": (
                "行业调研 Agent",
                "specialist",
                "industry_research",
            ),
            "data_analysis_agent": (
                "数据分析 Agent",
                "specialist",
                "data_analysis",
            ),
            "data_query_planner": (
                "数据查询规划 Agent",
                "data_query_plan",
                "data_query_plan",
            ),
            "solution_design_agent": (
                "方案设计 Agent",
                "specialist",
                "solution_design",
            ),
            "chapter_design_agent": (
                "章节设计 Agent",
                "specialist",
                "chapter_design",
            ),
            "document_writer_agent": ("文档撰写 Agent", "writer", None),
            "anti_ai_style_agent": (
                "去 AI 味 Agent",
                "polisher",
                None,
            ),
            "detail_polish_agent": (
                "细节润色 Agent",
                "polisher",
                None,
            ),
            "table_polish_agent": (
                "表格润色 Agent",
                "polisher",
                None,
            ),
            "typography_polish_agent": (
                "字体润色 Agent",
                "polisher",
                None,
            ),
            "image_polish_agent": (
                "图片润色 Agent",
                "polisher",
                None,
            ),
            "quality_review_agent": ("质量审校 Agent", "review", None),
        }
        definition = definitions.get(agent_id)
        if not definition:
            return None
        name, action, output_key = definition
        step = {
            "kind": "agent",
            "id": agent_id,
            "name": name,
            "action": action,
        }
        if output_key:
            step["output_key"] = output_key
        return step

    def _match_skills(self, state: LoopState) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for item in state.selected_skills:
            title = str(item.get("title") or item.get("name") or "已安装技能")
            raw_field_examples = (
                item.get("fieldExamples") or item.get("field_examples") or {}
            )
            if not isinstance(raw_field_examples, dict):
                raw_field_examples = {}
            matches.append(
                {
                    "id": item.get("id") or item.get("clientSkillKey") or title,
                    "name": title,
                    "summary": item.get("summary") or "",
                    "workflow_role": (
                        "support"
                        if str(
                            item.get("workflowRole")
                            or item.get("workflow_role")
                            or "primary"
                        ).lower()
                        == "support"
                        else "primary"
                    ),
                    "workflow_role_declared": (
                        "workflowRole" in item or "workflow_role" in item
                    ),
                    "skill_instructions": str(
                        item.get("skillInstructions")
                        or item.get("skill_instructions")
                        or ""
                    )[:MAX_SKILL_INSTRUCTION_CHARS],
                    "strict_structure": bool(
                        item.get("strictStructure", item.get("strict_structure", True))
                    ),
                    "skill_description": (
                        item.get("skillDescription")
                        or item.get("skill_description")
                        or {}
                    ),
                    "execution_steps": (
                        item.get("executionSteps")
                        or item.get("execution_steps")
                        or []
                    ),
                    "title_design_style": (
                        item.get("titleDesignStyle")
                        or item.get("title_design_style")
                        or []
                    ),
                    "writing_design": (
                        item.get("writingDesign")
                        or item.get("writing_design")
                        or ""
                    ),
                    "image_generation": (
                        item.get("imageGeneration")
                        or item.get("image_generation")
                        or ""
                    ),
                    "voice_style": (
                        item.get("voiceStyle")
                        or item.get("voice_style")
                        or item.get("writingGuidelines")
                        or item.get("writing_guidelines")
                        or []
                    ),
                    # 保留旧键供恢复态兼容；新撰写提示以 voice_style 为准。
                    "guidelines": (
                        item.get("voiceStyle")
                        or item.get("voice_style")
                        or item.get("writingGuidelines")
                        or item.get("writing_guidelines")
                        or []
                    ),
                    "field_examples": {
                        key: value
                        for key, value in raw_field_examples.items()
                        if key not in {"structurePattern", "structure_pattern"}
                    },
                    # 完整示例可能包含为演示写法而虚构的主题。它只属于 Skill
                    # 编辑/预览层，绝不能进入运行时事实环境或形成证据义务。
                    "example_document_available": bool(
                        item.get("exampleDocument") or item.get("example_document")
                    ),
                    "source": "installed",
                }
            )

        # 用户通过 @ 明确选择主 Skill 时，只保留被提及的 Skill 及其步骤中
        # 显式引用的依赖。旧客户端可能把名称相近的自动匹配模板一起发来；
        # 若全部当作主 Skill，会把多套 execution_steps 串成超长并行主流程。
        if matches:
            request_text = f"{state.root_request}\n{state.user_message}"
            mentioned = [
                skill
                for skill in matches
                if f"@{str(skill.get('name') or '').strip()}" in request_text
            ]
            if mentioned:
                allowed: list[dict[str, Any]] = []
                allowed_ids: set[str] = set()
                pending_references: set[str] = set()

                def append_allowed(skill: dict[str, Any], role: str) -> None:
                    skill_id = str(skill.get("id") or "")
                    if not skill_id or skill_id in allowed_ids or len(allowed) >= 4:
                        return
                    skill["workflow_role"] = role
                    allowed.append(skill)
                    allowed_ids.add(skill_id)
                    for raw_step in skill.get("execution_steps", []):
                        if not isinstance(raw_step, dict):
                            continue
                        pending_references.update(
                            str(reference).strip().lower()
                            for reference in raw_step.get("skills", [])
                            if str(reference).strip()
                        )

                for skill in mentioned:
                    append_allowed(skill, "primary")
                changed = True
                while changed and len(allowed) < 4:
                    changed = False
                    for skill in matches:
                        keys = {
                            str(skill.get("id") or "").strip().lower(),
                            str(skill.get("name") or "").strip().lower(),
                        }
                        if pending_references & {key for key in keys if key}:
                            before = len(allowed)
                            append_allowed(skill, "support")
                            changed = changed or len(allowed) > before
                return allowed[:4]

            # 新客户端会显式标记 primary/support。若是没有角色字段的旧客户端，
            # 自动匹配结果只能取最高分的第一个，避免静默执行多套完整模板。
            if any(skill.get("workflow_role_declared") for skill in matches):
                return matches[:4]
            matches[0]["workflow_role"] = "primary"
            return matches[:1]

        # 没有客户端选中/召回的 Skill 时不再做内置模板关键词兜底：
        # 枚举触发词会把“画一张架构图”这类请求套上方案模板。文档结构
        # 完全由章节设计 Agent 生成，模板能力只经由披露+模型决策的
        # 召回链路（已安装 Skill）引入。
        return matches

    async def _execute_step(
        self,
        state: LoopState,
        step: dict[str, Any],
        *,
        creation_model: Optional[str],
        creation_api_key: Optional[str],
        creation_base_url: Optional[str],
    ) -> AsyncIterator[dict[str, Any]]:
        actor = self._actor(step["kind"], step["id"], step["name"])
        action = step["action"]
        # route / plan 是主 Agent 的内部控制阶段，已有 thinking 与结果事件表达进度。
        # 不再把它们包装成普通 Agent 启动步骤，避免用户看到“创作 Agent 开始执行”
        # 这类没有独立动作含义的生命周期空壳。
        if action not in {"route", "plan"}:
            content_title = self._step_content_title(step)
            started_summary = (
                f"正在生成「{content_title}」内容"
                if content_title
                else f"{step['name']} 开始执行"
            )
            yield self._event(
                state,
                f"{step['kind']}.started",
                started_summary,
                actor=actor,
            )

        if action == "plan":
            state.environment["plan_summary"] = [item["name"] for item in state.plan[1:]]
            self._update_goal(state)
            yield self._event(
                state,
                "agent.completed",
                f"已根据目标规划 {len(state.plan) - 1} 个执行步骤",
                status="completed",
                actor=actor,
                environment_patch={"plan": state.environment["plan_summary"]},
            )
            if not state.environment.get("strict_skill_workflow"):
                # 无 Skill 流程时先宏观总结执行计划，再由每一步选择具体能力。
                async for outline_event in self._yield_plan_outline(state):
                    yield outline_event
            return

        if action == "route":
            requirement = state.environment["requirement"]
            query = str(state.environment.get("context_query") or state.user_message)
            # 契约：可选 Tool 只有启用后才向路由模型披露，未启用不可见、不可选。
            enabled_tool_ids = set(
                normalize_creation_tool_ids(state.options.get("enabled_tools"))
            )
            yield self._thinking_started(state, "routing")
            if state.model_mode == "external":
                system_prompt, user_prompt = self.service.build_routing_prompts(
                    query,
                    requirement,
                    state.selected_skills,
                    enabled_tool_ids,
                )
                state.pending_model_step = {
                    "step": step,
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                }
                yield self._event(
                    state,
                    "model.request",
                    f"{step['name']} 请求品牌模型决定执行链路",
                    status="waiting",
                    actor=actor,
                    data={
                        "request_id": f"model-{uuid4()}",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
                return
            decision = await self.service.route_capabilities(
                query=query,
                requirement=requirement,
                selected_skills=state.selected_skills,
                enabled_tool_ids=enabled_tool_ids,
                creation_model=creation_model,
                creation_api_key=creation_api_key,
                creation_base_url=creation_base_url,
            )
            async for event in self._apply_routing_decision(state, step, decision):
                yield event
            return

        if action == "memory_search":
            options = CreationOptions(**state.options)
            query = self._step_context_query(state, step)
            # Skill 步骤目标必须重新进入需求解析；根请求画像只适合路由，不能
            # 继续支配“AIGC 共建项目”等步骤级检索对象。实体识别进一步只看
            # 步骤自身主题，避免“整体创作背景”里其他章节的实体（如 GPU）
            # 被误当作本步骤核心实体。
            focus_text = self._step_focus_query(step) or query
            requirement = self.service.analyze_requirement(
                query,
                options,
                entity_focus_text=focus_text,
                retrieval_context_terms=list(
                    state.environment.get("retrieval_context_terms") or []
                ),
            )
            references = self.service.retrieve_references(
                query,
                requirement,
                options,
            )
            requested_time_context = requirement.get("time_context", {})
            # 创作消费召回结果前先对命中文档做浏览器即时刷新，把最新正文
            # 回写进召回对象；任何失败都静默降级，不中断创作主链路。
            document_refresh_stats = await self.service.refresh_recalled_documents(
                references,
                query,
                require_latest=bool(requirement.get("needs_latest")),
                browser_extension_enabled=bool(
                    state.options.get("browser_extension_enabled", True)
                ),
            )
            batch_references = [
                {
                    **self._reference_to_state(
                        item,
                        period_evidence=CreationService.reference_period_evidence(
                            item,
                            requested_time_context,
                        ),
                    ),
                    "retrieval_query": query,
                    "skill_step_id": step.get("skill_step_id"),
                    "skill_step_title": step.get("skill_step_title"),
                }
                for item in references
            ]
            state.environment["references"] = self._merge_reference_states(
                list(state.environment.get("references") or []),
                batch_references,
                limit=30,
            )
            batch_summaries = [
                {
                    "id": item.id,
                    "title": item.title,
                    "doc_type": item.doc_type,
                    "source_type": item.source_type,
                    "source_id": item.source_id,
                    "reason": item.reason,
                    "final_weight": round(item.final_weight, 4),
                    "relevance_score": round(item.relevance_score, 4),
                    "quality_score": round(item.quality_score, 4),
                    "completeness_score": round(item.completeness_score, 4),
                    "usage_score": round(item.usage_score, 4),
                    "format_score": round(item.format_score, 4),
                    "freshness_score": round(item.freshness_score, 4),
                    "usage_count": item.usage_count,
                    "retrieval_tier": item.retrieval_tier,
                    "retrieval_paths": list(item.retrieval_paths),
                    "matched_keywords": list(item.matched_keywords),
                    "matched_entities": list(item.matched_entities),
                    "lexical_score": round(item.lexical_score, 4),
                    "semantic_score": round(item.semantic_score, 4),
                    "entity_score": round(item.entity_score, 4),
                    "retrieval_mode": item.retrieval_mode,
                    "primary_target": item.primary_target,
                    "matched_components": list(item.matched_components),
                    "matched_relations": list(item.matched_relations),
                    "relation_score": round(item.relation_score, 4),
                    "selection_reasons": list(item.selection_reasons),
                    "summary": self.service._clip(item.summary, 600),
                    "source_url": item.source_url,
                    "observed_at": item.observed_at,
                    "period_evidence": CreationService.reference_period_evidence(
                        item,
                        requested_time_context,
                    ),
                    "refresh_status": item.refresh_status,
                    "refresh_completeness": item.refresh_completeness,
                    "refresh_collected_at": item.refresh_collected_at,
                    "refresh_truncated": item.refresh_truncated,
                    "skill_step_id": step.get("skill_step_id"),
                    "skill_step_title": step.get("skill_step_title"),
                }
                for item in references
            ]
            state.environment["reference_summaries"] = self._merge_reference_states(
                list(state.environment.get("reference_summaries") or []),
                batch_summaries,
                limit=30,
            )
            source_counts: dict[str, int] = {}
            for item in references:
                source_counts[item.source_type] = source_counts.get(item.source_type, 0) + 1
            # 事后排查“某条采集的知识为何没被采用”时，需要能直接从落库
            # 事件里看到本次实际召回的记忆域与 ID，而不是重放检索。
            reference_ids = [
                f"{item.source_type}:{item.source_id}" for item in references
            ]
            state.environment.setdefault("tool_results", []).append(
                {
                    "tool_id": MEMORY_SEARCH_TOOL_ID,
                    "status": "completed",
                    "result_count": len(references),
                    "result_limit": options.max_references,
                    "source_counts": source_counts,
                    "reference_ids": reference_ids,
                    "query": query,
                    "keywords": requirement.get("keywords", []),
                    "entity_context": requirement.get("entity_context", {}),
                    "retrieval_plan": requirement.get("retrieval_plan", {}),
                    "retrieval_diagnostics": requirement.get(
                        "retrieval_diagnostics", {}
                    ),
                    "time_context": requirement.get("time_context", {}),
                    "document_refresh": document_refresh_stats,
                }
            )
            self._update_goal(state)
            purpose = self._step_purpose(step)
            memory_summary = (
                f"检索「{purpose}」相关资料，召回 {len(references)} 条本地资料"
                if purpose
                else f"记忆搜索完成，召回 {len(references)} 条本地资料"
            )
            yield self._event(
                state,
                "tool.completed",
                memory_summary,
                status="completed",
                actor=actor,
                environment_patch={"references": batch_summaries},
                data={
                    "result_count": len(references),
                    "result_limit": options.max_references,
                    "source_counts": source_counts,
                    "reference_ids": reference_ids,
                    "query": query,
                    "keywords": requirement.get("keywords", []),
                    "entity_context": requirement.get("entity_context", {}),
                    "retrieval_plan": requirement.get("retrieval_plan", {}),
                    "retrieval_diagnostics": requirement.get(
                        "retrieval_diagnostics", {}
                    ),
                    "document_refresh": document_refresh_stats,
                    "skill_step_id": step.get("skill_step_id"),
                    "skill_step_title": step.get("skill_step_title"),
                },
            )
            return

        if action == "internet_search":
            results = await self.service.collect_web_context(
                self._step_context_query(state, step),
                state.environment["requirement"],
            )
            state.environment["web_results"] = [asdict(item) for item in results]
            state.environment.setdefault("tool_results", []).append(
                {
                    "tool_id": INTERNET_SEARCH_TOOL_ID,
                    "status": "completed",
                    "result_count": len(results),
                    "skill_step_id": step.get("skill_step_id"),
                }
            )
            self._update_goal(state)
            purpose = self._step_purpose(step)
            web_summary = (
                f"检索「{purpose}」相关外部资料，获得 {len(results)} 条外部资料"
                if purpose
                else f"互联网检索完成，获得 {len(results)} 条外部资料"
            )
            yield self._event(
                state,
                "tool.completed",
                web_summary,
                status="completed",
                actor=actor,
                environment_patch={
                    "web_results": [
                        {"title": item.title, "url": item.url} for item in results
                    ]
                },
                data={"result_count": len(results)},
            )
            return

        if action == DATA_SEARCH_TOOL_ID:
            query = self._step_context_query(state, step)
            step_requirement = self.service.analyze_requirement(
                query,
                CreationOptions(**state.options),
            )
            results = await self.service.retrieve_data_context(
                query,
                step_requirement,
                limit=int(state.options.get("data_search_limit") or 30),
            )
            self._enforce_report_evidence_policy(results)
            state.environment["current_data_results"] = results
            state.environment["data_results"] = self._merge_data_results(
                list(state.environment.get("data_results") or []),
                results,
            )
            self._apply_data_freshness_to_references(state, results)
            refresh_count = sum(
                1 for item in results if item.get("refresh_required") is True
            )
            state.environment.setdefault("tool_results", []).append(
                {
                    "tool_id": DATA_SEARCH_TOOL_ID,
                    "status": "completed",
                    "result_count": len(results),
                    "result_limit": int(
                        state.options.get("data_search_limit") or 30
                    ),
                    "refresh_required_count": refresh_count,
                    "query": query,
                    "time_context": step_requirement.get("time_context", {}),
                    "skill_step_id": step.get("skill_step_id"),
                }
            )
            self._update_goal(state)
            purpose = self._step_purpose(step)
            data_summary = (
                f"检索「{purpose}」相关数据来源，召回 {len(results)} 个来源，"
                f"其中 {refresh_count} 个需要刷新"
                if purpose
                else f"数据检索完成，召回 {len(results)} 个来源，其中 {refresh_count} 个需要刷新"
            )
            yield self._event(
                state,
                "tool.completed",
                data_summary,
                status="completed",
                actor=actor,
                environment_patch={
                    "data_sources": [
                        {
                            "source_id": item.get("source_id"),
                            "title": item.get("title"),
                            "source_kind": item.get("source_kind"),
                            "freshness_class": item.get("freshness_class"),
                            "refresh_required": item.get("refresh_required"),
                            "can_use": item.get("can_use"),
                            **(
                                {"evidence_status": item.get("evidence_status")}
                                if item.get("evidence_status")
                                else {}
                            ),
                            **(
                                {"evidence_reason": item.get("evidence_reason")}
                                if item.get("evidence_reason")
                                else {}
                            ),
                            **(
                                {"unavailable_reason": item.get("unavailable_reason")}
                                if item.get("unavailable_reason")
                                else {}
                            ),
                        }
                        for item in results
                    ]
                },
                data={
                    "result_count": len(results),
                    "result_limit": int(
                        state.options.get("data_search_limit") or 30
                    ),
                    "refresh_required_count": refresh_count,
                    "skill_id": step.get("skill_id"),
                    "skill_step_id": step.get("skill_step_id"),
                    "skill_step_title": step.get("skill_step_title"),
                },
            )
            return

        if action == WEBPAGE_SCRAPE_TOOL_ID:
            retain_screenshot = bool(
                step.get("skill_step_retain_webpage_screenshot", False)
            )
            preview_sources = CreationService._select_refreshable_report_sources(
                list(
                    state.environment.get("current_data_results")
                    or state.environment.get("data_results")
                    or []
                )
            )[:5]
            previews = [
                {
                    "id": str(uuid4()),
                    "source_id": int(item["source_id"]),
                    "title": str(item.get("title") or "实时数据页面")[:160],
                }
                for item in preview_sources
            ] if retain_screenshot else []
            for preview in previews:
                preview["image_url"] = (
                    f"/api/creation/browser-previews/{preview['id']}/image"
                )
            if previews:
                yield self._event(
                    state,
                    "browser.preview.started",
                    f"将依次读取 {len(previews)} 个数据页面；每个页面只在截图阶段临时切换一次浏览器",
                    actor=actor,
                    data={
                        "previews": previews,
                        "focus_policy": "allow_once",
                    },
                )
            outcome = await self.service.scrape_data_context(
                list(
                    state.environment.get("current_data_results")
                    or state.environment.get("data_results")
                    or []
                ),
                self._step_context_query(state, step),
                self.service.analyze_requirement(
                    self._step_context_query(state, step),
                    CreationOptions(**state.options),
                ),
                run_id=state.run_id,
                session_id=state.session_id,
                preview_ids={
                    int(preview["source_id"]): str(preview["id"])
                    for preview in previews
                },
                retain_screenshot=retain_screenshot,
                browser_extension_enabled=bool(
                    state.options.get("browser_extension_enabled", True)
                ),
            )
            scrapes = list(outcome.get("scrapes") or [])
            refreshed = list(outcome.get("refreshed_data") or [])
            for item in refreshed:
                if step.get("skill_step_title"):
                    item["target_section"] = step["skill_step_title"]
            self._enforce_report_evidence_policy(refreshed)
            state.environment["webpage_scrapes"] = [
                *list(state.environment.get("webpage_scrapes") or []),
                *scrapes,
            ]
            state.environment["current_data_results"] = refreshed
            state.environment["data_results"] = self._merge_data_results(
                list(state.environment.get("data_results") or []),
                refreshed,
            )
            new_evidence = [
                self._scope_creation_evidence(item["evidence"], step)
                for item in scrapes
                if item.get("status") == "completed"
                and isinstance(item.get("evidence"), dict)
                and item["evidence"].get("validation_status") == "verified"
                and item["evidence"].get("image_url")
            ]
            state.environment["creation_evidence"] = self._merge_evidence_items(
                list(state.environment.get("creation_evidence") or []),
                new_evidence,
            )
            self._apply_data_freshness_to_references(state, refreshed)
            completed_count = sum(
                1 for item in scrapes if item.get("status") == "completed"
            )
            failed_count = sum(
                1 for item in scrapes if item.get("status") in {"failed", "rejected"}
            )
            loading_timeout_count = sum(
                1
                for item in scrapes
                if item.get("validation_reason") == "page_still_loading"
            )
            foreground_refresh_count = sum(
                1
                for item in scrapes
                if item.get("collection_attempt") == "foreground_fallback"
                and item.get("status") == "completed"
            )
            focus_blocked_count = sum(
                1
                for item in scrapes
                if item.get("error_code") == "FOCUS_POLICY_BLOCKED"
            )
            empty_scrape_count = sum(
                1
                for item in scrapes
                if item.get("error_code") == "SCRAPE_EMPTY"
            )
            extension_timeout_count = sum(
                1
                for item in scrapes
                if item.get("error_code") == "BROWSER_EXTENSION_TIMEOUT"
            )
            extension_unresponsive_count = sum(
                1
                for item in scrapes
                if item.get("error_code") == "BROWSER_EXTENSION_UNRESPONSIVE"
            )
            collection_failure_count = sum(
                1 for item in scrapes if item.get("status") == "failed"
            )
            period_mismatch_count = sum(
                1
                for item in scrapes
                if item.get("error_code") == "SCRAPE_PERIOD_MISMATCH"
                or item.get("validation_reason")
                == "requested_metrics_period_mismatch"
            )
            validation_rejected_count = sum(
                1
                for item in scrapes
                if item.get("status") == "rejected"
                and item.get("error_code") != "SCRAPE_PERIOD_MISMATCH"
                and item.get("validation_reason")
                != "requested_metrics_period_mismatch"
            )
            stale_fallback_count = sum(
                1
                for item in refreshed
                if isinstance(item, dict)
                and isinstance(item.get("stale_fallback"), dict)
                and item.get("can_use") is True
            )
            scrape_summaries = [
                {
                    "source_id": item.get("source_id"),
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "status": item.get("status"),
                    "collector": item.get("collector"),
                    "collected_at": item.get("collected_at"),
                    "error_code": item.get("error_code"),
                    "validation_reason": item.get("validation_reason"),
                    "verified_claim_count": item.get("verified_claim_count", 0),
                    "interaction_mode": item.get("interaction_mode"),
                    "focus_policy": item.get("focus_policy"),
                    "focus_takeover_count": item.get("focus_takeover_count", 0),
                    "collection_attempt": item.get("collection_attempt"),
                }
                for item in scrapes
                if isinstance(item, dict)
            ]
            refreshed_by_source_id = {
                item.get("source_id"): item
                for item in refreshed
                if isinstance(item, dict) and item.get("source_id") is not None
            }
            rejected_sources = []
            for item in scrapes:
                if (
                    not isinstance(item, dict)
                    or item.get("status") != "rejected"
                    or item.get("error_code") == "SCRAPE_PERIOD_MISMATCH"
                    or item.get("validation_reason")
                    == "requested_metrics_period_mismatch"
                ):
                    continue
                source_id = item.get("source_id")
                refreshed_item = refreshed_by_source_id.get(source_id, {})
                rejected_sources.append(
                    {
                        "source_id": source_id,
                        "title": item.get("title")
                        or refreshed_item.get("title")
                        or f"数据来源 #{source_id}",
                        "url": item.get("url")
                        or refreshed_item.get("source_url")
                        or "",
                    }
                )
            state.environment.setdefault("tool_results", []).append(
                {
                    "tool_id": WEBPAGE_SCRAPE_TOOL_ID,
                    "status": "completed",
                    "result_count": completed_count,
                    "failed_count": failed_count,
                    "attempted_count": len(scrapes),
                    "skill_step_id": step.get("skill_step_id"),
                }
            )
            self._update_goal(state)
            if completed_count:
                if retain_screenshot:
                    evidence_suffix = "，并保留证据截图"
                elif foreground_refresh_count:
                    evidence_suffix = (
                        f"，其中 {foreground_refresh_count} 个来源在静默读取不足后"
                        "使用一次性浏览器会话完成即时取数，未保留截图"
                    )
                else:
                    evidence_suffix = ""
                details = []
                qualified_count = sum(1 for item in refreshed if item.get("can_use") is True and item.get("risk_disclosure_required"))
                if qualified_count:
                    details.append(f"{qualified_count} 个来源使用参考数据，文档将标注实际周期与使用风险")
                if period_mismatch_count:
                    details.append(
                        f"{period_mismatch_count} 个来源展示周期与任务周期不一致，未采用"
                    )
                if validation_rejected_count:
                    details.append(
                        f"{validation_rejected_count} 个来源暂未取得目标指标，未采用"
                    )
                other_failure_count = max(
                    0,
                    collection_failure_count - period_mismatch_count,
                )
                if other_failure_count:
                    details.append(
                        f"{other_failure_count} 个来源本次未完成刷新，保留原有数据状态"
                    )
                detail_suffix = f"；{'；'.join(details)}" if details else ""
                summary = (
                    f"已读取 {len(scrapes)} 个报表，采用其中 {completed_count} 个来源"
                    f"{detail_suffix}{evidence_suffix}"
                )
            elif stale_fallback_count:
                failure_details = []
                if empty_scrape_count:
                    failure_details.append(
                        f"{empty_scrape_count} 个后台页面未提取到正文"
                    )
                if extension_timeout_count:
                    failure_details.append(
                        f"{extension_timeout_count} 个来源在等待时间内未完成读取"
                    )
                detail = "、".join(failure_details) or "后台即时读取未完成"
                summary = (
                    f"即时刷新 {len(scrapes)} 个报表未完成（{detail}）；"
                    f"已使用 {stale_fallback_count} 个与目标周期匹配的历史快照，"
                    "并保留采集时间标记"
                )
            elif focus_blocked_count:
                summary = (
                    f"本次读取的 {len(scrapes)} 个报表中，{focus_blocked_count} 个来源"
                    "需要前台操作；为避免打断当前操作，本轮未采用这些页面数值"
                )
            elif period_mismatch_count:
                summary = (
                    f"已读取 {len(scrapes)} 个报表，其中 {period_mismatch_count} 个来源"
                    "展示周期与任务周期不一致；本轮未采用这些页面数值"
                )
            elif loading_timeout_count:
                summary = (
                    f"已读取 {len(scrapes)} 个报表，其中 {loading_timeout_count} 个来源"
                    "在等待时间内尚未加载完成；本轮未采用这些页面数值"
                )
            elif empty_scrape_count:
                summary = (
                    f"已读取 {len(scrapes)} 个报表，其中 {empty_scrape_count} 个来源"
                    "暂未展示可读取的数据；本轮未采用这些页面数值"
                )
            elif extension_timeout_count:
                summary = (
                    f"已读取 {len(scrapes)} 个报表，其中 {extension_timeout_count} 个来源"
                    "在等待时间内未完成读取；本轮保留原有数据状态"
                )
            elif extension_unresponsive_count:
                summary = (
                    f"已读取 {len(scrapes)} 个报表，其中 {extension_unresponsive_count} 个来源"
                    "暂未开始后台读取；本轮保留原有数据状态"
                )
            elif collection_failure_count:
                summary = (
                    f"已读取 {len(scrapes)} 个报表，其中 {collection_failure_count} 个来源"
                    "本次未完成刷新；本轮保留原有数据状态"
                )
            elif scrapes:
                summary = (
                    f"已读取 {len(scrapes)} 个报表，但暂未取得与任务指标一致的即时数据；"
                    "本轮未采用这些页面数值"
                )
            else:
                summary = "没有可实时刷新的报表 URL，保留可用工作记忆及其采集时间"
            scrape_purpose = self._step_purpose(step)
            if scrape_purpose and completed_count:
                summary = f"刷新「{scrape_purpose}」的过期数据：{summary}"
            yield self._event(
                state,
                "tool.completed",
                summary,
                status=(
                    "warning"
                    if scrapes and (completed_count == 0 or failed_count > 0)
                    else "completed"
                ),
                actor=actor,
                environment_patch={
                    "attempted_source_count": len(scrapes),
                    "scraped_source_count": completed_count,
                    "failed_source_count": failed_count,
                    "sources": scrape_summaries,
                    "rejected_sources": rejected_sources,
                    "data_sources": [
                        {
                            "source_id": item.get("source_id"),
                            "title": item.get("title"),
                            "source_kind": item.get("source_kind"),
                            "freshness_class": item.get("freshness_class"),
                            "refresh_required": item.get("refresh_required"),
                            "can_use": item.get("can_use"),
                            **(
                                {"evidence_status": item.get("evidence_status")}
                                if item.get("evidence_status")
                                else {}
                            ),
                            **(
                                {"evidence_reason": item.get("evidence_reason")}
                                if item.get("evidence_reason")
                                else {}
                            ),
                            **(
                                {"unavailable_reason": item.get("unavailable_reason")}
                                if item.get("unavailable_reason")
                                else {}
                            ),
                        }
                        for item in refreshed
                    ],
                },
                data={
                    "attempted_count": len(scrapes),
                    "result_count": completed_count,
                    "failed_count": failed_count,
                    "sources": scrape_summaries,
                    "rejected_sources": rejected_sources,
                },
            )
            if previews:
                scrape_by_source = {
                    int(item["source_id"]): item
                    for item in scrapes
                    if item.get("source_id") is not None
                }
                completed_previews = []
                for preview in previews:
                    scrape = scrape_by_source.get(int(preview["source_id"])) or {}
                    evidence = scrape.get("evidence")
                    completed_previews.append(
                        {
                            **preview,
                            "title": str(scrape.get("title") or preview["title"])[:160],
                            "status": str(scrape.get("status") or "failed"),
                            "browser": scrape.get("browser"),
                            "interaction_mode": scrape.get("interaction_mode"),
                            "focus_policy": scrape.get("focus_policy"),
                            "focus_takeover_count": scrape.get(
                                "focus_takeover_count", 0
                            ),
                            "image_url": (
                                evidence.get("image_url")
                                if isinstance(evidence, dict)
                                and evidence.get("image_url")
                                else preview["image_url"]
                            ),
                        }
                    )
                yield self._event(
                    state,
                    "browser.preview.completed",
                    "按需网页截图已结束，缩略预览保留在执行记录中",
                    status="completed",
                    actor=actor,
                    data={
                        "previews": completed_previews,
                        "focus_policy": "allow_once",
                    },
                )
            return

        if action == GITHUB_SEARCH_TOOL_ID:
            results = await self.service.search_github_context(
                self._step_context_query(state, step),
                state.environment["requirement"],
            )
            state.environment["github_results"] = [asdict(item) for item in results]
            state.environment.setdefault("tool_results", []).append(
                {
                    "tool_id": GITHUB_SEARCH_TOOL_ID,
                    "status": "completed",
                    "result_count": len(results),
                    "skill_step_id": step.get("skill_step_id"),
                }
            )
            self._update_goal(state)
            yield self._event(
                state,
                "tool.completed",
                f"GitHub 检索完成，获得 {len(results)} 个公开仓库线索",
                status="completed",
                actor=actor,
                environment_patch={
                    "github_results": [
                        {
                            "full_name": item.full_name,
                            "url": item.url,
                            "stars": item.stars,
                        }
                        for item in results
                    ]
                },
                data={"result_count": len(results)},
            )
            return

        if action == PLANTUML_DIAGRAM_TOOL_ID:
            diagram_context = build_plantuml_context(
                self._step_context_query(state, step)
            )
            state.environment["plantuml_diagram"] = diagram_context
            state.environment.setdefault("tool_results", []).append(
                {
                    "tool_id": PLANTUML_DIAGRAM_TOOL_ID,
                    "status": "completed",
                    "diagram_type": diagram_context["diagram_type"],
                    "skill_step_id": step.get("skill_step_id"),
                }
            )
            self._update_goal(state)
            yield self._event(
                state,
                "tool.completed",
                f"PlantUML 画图准备完成，将生成 {diagram_context['diagram_type']} 图",
                status="completed",
                actor=actor,
                environment_patch={
                    "plantuml_diagram": {
                        "diagram_type": diagram_context["diagram_type"],
                        "language": diagram_context["language"],
                    }
                },
                data={"diagram_type": diagram_context["diagram_type"]},
            )
            return

        if action == MERMAID_DIAGRAM_TOOL_ID:
            diagram_context = build_mermaid_context(
                self._step_context_query(state, step),
                step.get("diagram_spec"),
            )
            state.environment["mermaid_diagram"] = diagram_context
            prepared_diagrams = state.environment.setdefault(
                "mermaid_diagrams", []
            )
            diagram_id = str(diagram_context.get("diagram_id") or "").strip()
            prepared_diagrams[:] = [
                item
                for item in prepared_diagrams
                if not (
                    isinstance(item, dict)
                    and diagram_id
                    and str(item.get("diagram_id") or "") == diagram_id
                )
            ]
            prepared_diagrams.append(diagram_context)
            state.environment.setdefault("tool_results", []).append(
                {
                    "tool_id": MERMAID_DIAGRAM_TOOL_ID,
                    "status": "completed",
                    "diagram_type": diagram_context["diagram_type"],
                    "diagram_id": diagram_context.get("diagram_id"),
                    "section_title": diagram_context.get("section_title"),
                    "skill_step_id": step.get("skill_step_id"),
                }
            )
            self._update_goal(state)
            yield self._event(
                state,
                "tool.completed",
                f"Mermaid 画图准备完成，将生成 {diagram_context['diagram_type']} 图",
                status="completed",
                actor=actor,
                environment_patch={
                    "mermaid_diagram": {
                        "diagram_type": diagram_context["diagram_type"],
                        "language": diagram_context["language"],
                        "diagram_id": diagram_context.get("diagram_id"),
                        "section_title": diagram_context.get("section_title"),
                    }
                },
                data={
                    "diagram_type": diagram_context["diagram_type"],
                    "diagram_id": diagram_context.get("diagram_id"),
                    "section_title": diagram_context.get("section_title"),
                },
            )
            return

        if action == "apply_skill":
            skill = step["skill"]
            state.environment.setdefault("applied_skills", []).append(skill)
            structure_requirements = self._skill_structure_requirements(skill)
            if (
                structure_requirements.get("minimum_subsections")
                or structure_requirements.get("minimum_subsection_chars")
            ):
                existing = state.environment.get("skill_structure_requirements")
                if not isinstance(existing, dict):
                    existing = {}
                state.environment["skill_structure_requirements"] = {
                    "minimum_subsections": max(
                        int(existing.get("minimum_subsections") or 0),
                        int(structure_requirements.get("minimum_subsections") or 0),
                    ) or None,
                    "minimum_subsection_chars": max(
                        int(existing.get("minimum_subsection_chars") or 0),
                        int(
                            structure_requirements.get(
                                "minimum_subsection_chars"
                            )
                            or 0
                        ),
                    ) or None,
                    "source_text": "\n".join(
                        item
                        for item in (
                            str(existing.get("source_text") or "").strip(),
                            str(
                                structure_requirements.get("source_text") or ""
                            ).strip(),
                        )
                        if item
                    )[:1500],
                }
            self._update_goal(state)
            yield self._event(
                state,
                "skill.completed",
                f"已应用 {step['name']}",
                status="completed",
                actor=actor,
                environment_patch={
                    "skill": {
                        "id": skill["id"],
                        "name": skill["name"],
                        "source": skill.get("source"),
                    }
                },
            )
            return

        if action == "activate_skill_step":
            step_result = {
                "skill_id": step.get("skill_id"),
                "step_id": step.get("skill_step_id"),
                "title": step.get("skill_step_title"),
                "objective": step.get("skill_step_objective"),
                "output": step.get("skill_step_output"),
                "skills": step.get("skill_step_skills", []),
            }
            state.environment.setdefault("completed_skill_steps", []).append(step_result)
            self._update_goal(state)
            yield self._event(
                state,
                "skill.completed",
                f"已激活工作流步骤：{step.get('skill_step_title') or step['name']}",
                status="completed",
                actor=actor,
                environment_patch={"skill_step": step_result},
            )
            return

        if action == "activate_quality_skill":
            skill = step.get("skill") or {}
            activation = {
                "skill_id": step.get("skill_id"),
                "name": skill.get("name") or step.get("name"),
                "quality_cycle": step.get("quality_cycle"),
                "issue_codes": step.get("quality_issue_codes", []),
                "capabilities": step.get("matched_capabilities", []),
                "structure": skill.get("structure", []),
                "voice_style": skill.get("voice_style") or skill.get("guidelines", []),
                "writing_design": skill.get("writing_design", ""),
                "image_generation": skill.get("image_generation", ""),
                "field_examples": skill.get("field_examples", {}),
            }
            state.environment.setdefault("activated_quality_skills", []).append(
                activation
            )
            self._update_goal(state)
            yield self._event(
                state,
                "skill.completed",
                f"已按质检问题激活 {activation['name']} 的相关规则",
                status="completed",
                actor=actor,
                environment_patch={"quality_skill_activation": activation},
                data={
                    "quality_cycle": activation["quality_cycle"],
                    "issue_codes": activation["issue_codes"],
                    "capabilities": activation["capabilities"],
                },
            )
            return

        if action in {
            "specialist",
            "writer",
            "polisher",
            "skill_step",
            "data_query_plan",
        }:
            intent = state.environment.get("edit_intent", {})
            is_revision = action == "writer" and state.mode == "revision"
            is_document_mutation = action in {"writer", "polisher"}
            if is_revision or action == "polisher":
                targets = [str(item) for item in intent.get("target_sections", [])]
                if action == "polisher":
                    if step.get("id") == "document_unify_polisher":
                        planned_summary = "正在统一全文结构与表达，保留既有章节和事实"
                    else:
                        planned_summary = (
                            f"{step['name']}将按质检问题局部润色相关细节，"
                            "未涉及章节保持原样"
                        )
                elif targets:
                    planned_summary = (
                        f"{step['name']}将以{'、'.join(targets)}为线索检查全文联动"
                    )
                else:
                    planned_summary = f"{step['name']}将根据质检问题检查完整文档"
                yield self._event(
                    state,
                    "document.patch.planned",
                    planned_summary,
                    status="completed",
                    actor=actor,
                    data={
                        "operation": intent.get("operation"),
                        "target_sections": targets,
                        "preserve_untouched": intent.get("preserve_untouched", True),
                        "reasoning_summary": intent.get("reasoning_summary"),
                    },
                )

            if action == "data_query_plan":
                current_results = [
                    item
                    for item in (
                        state.environment.get("current_data_results")
                        or state.environment.get("data_results")
                        or []
                    )
                    if isinstance(item, dict)
                ]
                system_prompt, user_prompt = build_query_planner_prompts(
                    self._step_context_query(state, step),
                    relation_catalog(current_results),
                )
            else:
                system_prompt, user_prompt = self._model_prompts(state, step)
            # 内容生成是真正的深度思考点：用思考事件包裹大模型调用。
            yield self._thinking_started(state, "generation")
            if state.model_mode == "external":
                state.pending_model_step = {
                    "step": step,
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                }
                yield self._event(
                    state,
                    "model.request",
                    f"{step['name']} 请求品牌模型推理",
                    status="waiting",
                    actor=actor,
                    data={
                        "request_id": f"model-{uuid4()}",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
                return

            if (
                action == "skill_step"
                and state.environment.get("strict_skill_workflow")
                and hasattr(self.service, "stream_specialist_agent")
            ):
                # 流中途断连属于可重试故障：预览按 document_parts 原子重组，
                # 整步重试时清空重新生成不会把两次输出拼接为一份结果。
                max_stream_attempts = 2
                for stream_attempt in range(1, max_stream_attempts + 1):
                    document_parts = []
                    last_preview_ts = 0.0
                    try:
                        async for chunk in self.service.stream_specialist_agent(
                            agent_id=step["id"],
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            creation_model=creation_model,
                            creation_api_key=creation_api_key,
                            creation_base_url=creation_base_url,
                        ):
                            document_parts.append(chunk)
                            now_ts = time.monotonic()
                            if now_ts - last_preview_ts < 0.15:
                                continue
                            last_preview_ts = now_ts
                            preview, preview_audit = self._assemble_strict_skill_document(
                                state,
                                pending_step=step,
                                pending_content="".join(document_parts),
                                include_audit=True,
                            )
                            if preview:
                                yield self._event(
                                    state,
                                    "document.preview",
                                    f"正在生成「{self._step_content_title(step)}」内容",
                                    actor=actor,
                                    data={
                                        "content": preview,
                                        "section_title": self._step_content_title(step),
                                        "progress_chars": sum(len(item) for item in document_parts),
                                        "assembly_audit": preview_audit,
                                    },
                                )
                        result = "".join(document_parts)
                        break
                    except Exception as exc:
                        if (
                            stream_attempt >= max_stream_attempts
                            or not _is_retryable_model_transport(exc)
                        ):
                            raise
                        logger.warning(
                            "Skill 步骤 %s 流式输出中途断连（%s），重新生成整步: attempt=%s",
                            step["id"],
                            type(exc).__name__,
                            stream_attempt,
                        )
                        yield self._event(
                            state,
                            "agent.started",
                            f"模型连接中断，正在重试生成「{self._step_content_title(step)}」内容",
                            actor=actor,
                        )
            elif is_document_mutation:
                is_local_polish = action == "polisher"
                # 本地润色只推进度事件不推正文，断流可安全整步重试；
                # document.delta 路径 UI 侧增量追加，重试会重复内容，不重试。
                max_stream_attempts = 2 if is_local_polish else 1
                for stream_attempt in range(1, max_stream_attempts + 1):
                    document_parts: list[str] = []
                    polish_received_chars = 0
                    last_polish_progress_ts = 0.0
                    try:
                        async for chunk in self.service.stream_agent_document(
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            creation_model=creation_model,
                            creation_api_key=creation_api_key,
                            creation_base_url=creation_base_url,
                        ):
                            document_parts.append(chunk)
                            if is_local_polish:
                                # 润色只是局部重写相关细节：不把全文流式推给页面，
                                # 避免用户误以为整篇文档在重新生成；只同步节流进度。
                                polish_received_chars += len(chunk)
                                now_ts = time.monotonic()
                                if now_ts - last_polish_progress_ts >= 1.5:
                                    last_polish_progress_ts = now_ts
                                    yield self._event(
                                        state,
                                        "document.patch.delta",
                                        f"{step['name']}正在局部润色相关细节，其余章节保持原样",
                                        actor=actor,
                                        data={"progress_chars": polish_received_chars},
                                    )
                                continue
                            yield self._event(
                                state,
                                (
                                    "document.patch.delta"
                                    if is_revision
                                    else "document.delta"
                                ),
                                (
                                    f"{step['name']}正在联动修订全文"
                                    if is_revision
                                    else f"{step['name']}正在更新文档"
                                ),
                                actor=actor,
                                data={"content": chunk},
                            )
                        result = "".join(document_parts)
                        break
                    except Exception as exc:
                        if (
                            stream_attempt >= max_stream_attempts
                            or not _is_retryable_model_transport(exc)
                        ):
                            raise
                        logger.warning(
                            "%s 流式输出中途断连（%s），重新生成整步: attempt=%s",
                            step["name"],
                            type(exc).__name__,
                            stream_attempt,
                        )
                        yield self._event(
                            state,
                            "agent.started",
                            f"模型连接中断，正在重试{step['name']}",
                            actor=actor,
                        )
            else:
                result = await self.service.run_specialist_agent(
                    agent_id=step["id"],
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    creation_model=creation_model,
                    creation_api_key=creation_api_key,
                    creation_base_url=creation_base_url,
                )
            async for event in self._complete_model_step(state, step, result):
                yield event
            return

        if action == "review":
            document = str(state.environment.get("document") or "")
            criteria, issues = self._inspect_document_quality(state, document)
            requested_issue_codes = {
                str(item)
                for item in (step.get("quality_issue_codes") or [])
                if str(item)
            }
            if requested_issue_codes:
                issues = [
                    item
                    for item in issues
                    if str(item.get("code") or "") in requested_issue_codes
                ]
                requested_criteria = {
                    criterion
                    for code, criterion in (
                        (
                            DATA_QUERY_QUALITY_ISSUE_CODE,
                            DATA_QUERY_QUALITY_CRITERION,
                        ),
                        (
                            EMPHASIS_QUALITY_ISSUE_CODE,
                            EMPHASIS_QUALITY_CRITERION,
                        ),
                        (
                            SUBSECTION_REQUIREMENTS_QUALITY_ISSUE_CODE,
                            SUBSECTION_REQUIREMENTS_QUALITY_CRITERION,
                        ),
                    )
                    if code in requested_issue_codes
                }
                criteria = {
                    key: value
                    for key, value in criteria.items()
                    if key in requested_criteria
                }
            report = {
                **criteria,
                "passed": not issues and all(criteria.values()),
                "cycle": state.quality_cycles,
                "issues": issues,
            }
            hard_failures = [
                str(item.get("code") or "")
                for item in issues
                if item.get("severity") == "hard"
            ]
            soft_warnings = [
                str(item.get("code") or "")
                for item in issues
                if item.get("severity") != "hard"
            ]
            soft_warnings.extend(
                key
                for key, value in criteria.items()
                if key not in {"has_document", "has_structure", "revision_changed"}
                and not value
                and key not in soft_warnings
            )
            state.environment["quality_review"] = report
            state.environment["quality_issues"] = issues
            state.environment["quality_hard_failures"] = hard_failures
            state.environment["quality_soft_warnings"] = soft_warnings
            summary = (
                f"质检发现 {len(issues)} 个可执行问题，正在安排后续优化"
                if issues
                else (
                    "质量检查完成，已记录非阻断警告；保留当前完整版本"
                    if soft_warnings
                    else "质量检查通过"
                )
            )
            self._update_goal(state)
            yield self._event(
                state,
                "agent.completed",
                summary,
                status="completed",
                actor=actor,
                environment_patch={"quality_review": report},
                data={
                    "issue_count": len(issues),
                    "issue_codes": [
                        str(item.get("code") or "") for item in issues
                    ],
                    "quality_cycle": state.quality_cycles,
                },
            )

    @classmethod
    def _subsection_requirement_result(
        cls,
        document: str,
        requirements: dict[str, Any],
    ) -> dict[str, Any]:
        minimum_count = int(requirements.get("minimum_subsections") or 0)
        minimum_chars = int(requirements.get("minimum_subsection_chars") or 0)
        headings = list(re.finditer(r"(?m)^(#{3,6})\s+(.+?)\s*$", document))
        sections: list[dict[str, Any]] = []
        for index, heading in enumerate(headings):
            end = (
                headings[index + 1].start()
                if index + 1 < len(headings)
                else len(document)
            )
            body = document[heading.end():end]
            prose = cls._prose_for_quality(body).strip()
            char_count = len(re.sub(r"\s+", "", prose))
            sections.append(
                {
                    "title": re.sub(r"\s+#+\s*$", "", heading.group(2)).strip(),
                    "char_count": char_count,
                }
            )
        qualified = [
            item
            for item in sections
            if not minimum_chars or int(item["char_count"]) >= minimum_chars
        ]
        count_satisfied = not minimum_count or len(qualified) >= minimum_count
        length_satisfied = (
            not minimum_chars
            or (bool(sections) and len(qualified) == len(sections))
        )
        passed = count_satisfied and length_satisfied
        return {
            "passed": passed,
            "subsection_count": len(sections),
            "qualified_subsection_count": len(qualified),
            "minimum_subsections": minimum_count,
            "minimum_subsection_chars": minimum_chars,
            "short_subsections": [
                item for item in sections
                if minimum_chars and int(item["char_count"]) < minimum_chars
            ][:12],
            "subsections": [item["title"] for item in sections[:20]],
            "source_text": str(requirements.get("source_text") or "")[:500],
        }

    def _inspect_document_quality(
        self,
        state: LoopState,
        document: str,
    ) -> tuple[dict[str, bool], list[dict[str, Any]]]:
        """把主观质检拆成可观察指标和可路由的问题。"""
        headings = sum(
            1
            for line in document.splitlines()
            if re.match(r"^#{1,6}\s+\S", line.lstrip())
        )
        criteria: dict[str, bool] = {
            "has_document": len(document.strip()) >= 180,
            "has_structure": headings >= 3,
            "addresses_goal": bool(state.user_message.strip()),
        }
        issues: list[dict[str, Any]] = []

        coverage_contract = state.environment.get("requirement", {}).get(
            "coverage_contract"
        )
        if isinstance(coverage_contract, dict):
            coverage_result = self._multi_target_coverage_result(
                document,
                coverage_contract,
            )
            criteria[MULTI_TARGET_COVERAGE_CRITERION] = bool(
                coverage_result["passed"]
            )
            if not coverage_result["passed"]:
                issues.append(
                    self._quality_issue(
                        code=MULTI_TARGET_COVERAGE_ISSUE_CODE,
                        severity="hard",
                        agent_id="document_writer_agent",
                        summary="多目标请求没有逐对象、逐维度完整回答",
                        evidence=coverage_result,
                    )
                )

        structure_requirements = state.environment.get(
            "skill_structure_requirements"
        )
        if isinstance(structure_requirements, dict) and (
            structure_requirements.get("minimum_subsections")
            or structure_requirements.get("minimum_subsection_chars")
        ):
            subsection_result = self._subsection_requirement_result(
                document,
                structure_requirements,
            )
            criteria[SUBSECTION_REQUIREMENTS_QUALITY_CRITERION] = bool(
                subsection_result["passed"]
            )
            if not subsection_result["passed"]:
                issues.append(
                    self._quality_issue(
                        code=SUBSECTION_REQUIREMENTS_QUALITY_ISSUE_CODE,
                        severity="soft",
                        agent_id="detail_polish_agent",
                        summary=(
                            "正文没有满足执行动作声明的最少子章节数或每节最少字数"
                        ),
                        evidence=subsection_result,
                        required_capabilities=["skill:writing_design"],
                    )
                )

        if state.mode == "revision":
            base_document = str(
                state.environment.get("revision_base_document")
                or state.current_document
            )
            intent = state.environment.get("edit_intent", {})
            criteria.update(
                {
                    "revision_changed": self._document_hash(base_document)
                    != self._document_hash(document),
                    "preserves_structure": (
                        not bool(intent.get("preserve_untouched", True))
                        or self._revision_preserves_structure(
                            base_document,
                            document,
                        )
                    ),
                    "target_position_logical": self._target_positions_are_logical(
                        document,
                        [
                            str(item)
                            for item in intent.get("target_sections", [])
                        ],
                        allow_missing=any(
                            marker in state.user_message
                            for marker in DELETE_MARKERS
                        ),
                    ),
                }
            )

        hard_checks = {
            "has_document": "文档正文不足，不能形成可交付版本",
            "has_structure": "文档缺少可导航的章节结构",
            "revision_changed": "修订结果与原文没有可观察差异",
        }
        for code, summary in hard_checks.items():
            if code in criteria and not criteria[code]:
                issues.append(
                    self._quality_issue(
                        code=code,
                        severity="hard",
                        agent_id="document_writer_agent",
                        summary=summary,
                    )
                )

        prose = self._prose_for_quality(document)
        ai_style_signals = self._ai_style_signals(prose)
        criteria["natural_expression"] = not ai_style_signals
        if ai_style_signals:
            issues.append(
                self._quality_issue(
                    code="ai_style_signals",
                    severity="soft",
                    agent_id="anti_ai_style_agent",
                    summary="表达存在模板词、机械衔接、装饰性引号或长句堆叠",
                    evidence=ai_style_signals,
                    required_capabilities=["skill:voice_style"],
                )
            )

        short_sections = self._short_detail_sections(document)
        placeholder_count = self._placeholder_count(document)
        detail_incomplete = (
            len(document.strip()) >= 500
            and (bool(short_sections) or placeholder_count > 0)
        )
        criteria["detail_complete"] = not detail_incomplete
        if detail_incomplete:
            required_capabilities: list[str] = ["skill:writing_design"]
            routing_decision = state.environment.get("routing_decision") or {}
            data_routed = DATA_SEARCH_TOOL_ID in list(
                routing_decision.get("tools") or []
            )
            if data_routed and not state.environment.get(
                "data_analysis"
            ):
                if state.environment.get("data_results"):
                    if any(
                        self._has_analyzable_data(item)
                        for item in state.environment.get("data_results", [])
                        if isinstance(item, dict)
                    ):
                        required_capabilities.append("data_analysis_agent")
                else:
                    required_capabilities.append(DATA_SEARCH_TOOL_ID)
            issues.append(
                self._quality_issue(
                    code="detail_incomplete",
                    severity="soft",
                    agent_id="detail_polish_agent",
                    summary="部分章节只有观点或结论，缺少边界、动作、依据或例子",
                    evidence={
                        "short_sections": short_sections[:8],
                        "placeholder_count": placeholder_count,
                    },
                    required_capabilities=required_capabilities,
                )
            )

        context = "\n".join(
            (
                state.root_request,
                state.user_message,
                str(state.environment.get("requirement", {}).get("doc_type") or ""),
            )
        )
        has_table, malformed_tables = self._markdown_table_quality(document)
        table_expected = any(
            marker in context
            for marker in ("表格", "对比", "矩阵", "清单", "指标", "参数", "排期")
        )
        table_needs_polish = malformed_tables > 0 or (
            len(document.strip()) >= 500 and table_expected and not has_table
        )
        criteria["table_readable"] = not table_needs_polish
        if table_needs_polish:
            issues.append(
                self._quality_issue(
                    code="table_needs_polish",
                    severity="soft",
                    agent_id="table_polish_agent",
                    summary="需要补充或修复结构化表格，确保列口径和 Markdown 结构一致",
                    evidence={
                        "has_table": has_table,
                        "malformed_table_count": malformed_tables,
                    },
                    required_capabilities=["skill:table_style"],
                )
            )

        query_result_gaps = self._data_query_result_gaps(
            document,
            state.environment.get("data_query_results", []),
        )
        criteria[DATA_QUERY_QUALITY_CRITERION] = not query_result_gaps
        if query_result_gaps:
            issues.append(
                self._quality_issue(
                    code=DATA_QUERY_QUALITY_ISSUE_CODE,
                    severity="soft",
                    agent_id="table_polish_agent",
                    summary=(
                        "确定性数据查询已返回完整行结果，但成稿没有逐行保留"
                    ),
                    evidence={"results": query_result_gaps[:8]},
                    required_capabilities=["skill:table_style"],
                )
            )

        incomplete_scrapes = [
            item
            for item in state.environment.get("webpage_scrapes", [])
            if isinstance(item, dict)
            and str(item.get("status") or "") in {"failed", "rejected"}
        ]
        page_absence_claims = re.findall(
            r"[^。！？\n]{0,80}(?:看板|报表|页面)[^。！？\n]{0,40}"
            r"(?:未展示|未显示|未提供|没有展示|没有显示|没有提供|"
            r"无法提供|不存在|不包含)[^。！？\n]{0,80}",
            document,
            re.IGNORECASE,
        )
        unsupported_page_absence = bool(
            incomplete_scrapes and page_absence_claims
        )
        criteria[PAGE_ABSENCE_QUALITY_CRITERION] = not unsupported_page_absence
        if unsupported_page_absence:
            issues.append(
                self._quality_issue(
                    code=PAGE_ABSENCE_QUALITY_ISSUE_CODE,
                    severity="hard",
                    agent_id="detail_polish_agent",
                    summary=(
                        "页面交互或采集未完成，不能据此断言看板不存在相关字段"
                    ),
                    evidence={
                        "claims": page_absence_claims[:4],
                        "failed_source_count": len(incomplete_scrapes),
                    },
                    required_capabilities=[],
                )
            )

        bold_spans = re.findall(r"\*\*([^*\n]{1,120})\*\*", document)
        bold_chars = sum(len(item) for item in bold_spans)
        prose_chars = max(1, len(re.sub(r"\s+", "", prose)))
        emphasis_ratio = bold_chars / prose_chars
        fragment_metrics = self._narrative_fragment_label_metrics(document)
        selective_emphasis_ratio = max(
            0,
            bold_chars - fragment_metrics["labeled_fragment_bold_chars"],
        ) / prose_chars
        overlong_bold_spans = [
            item for item in bold_spans if len(item) > MAX_EMPHASIS_SPAN_CHARS
        ]
        emphasis_needs_polish = (
            len(document.strip()) >= 600
            and (
                selective_emphasis_ratio > MAX_EMPHASIS_CHARACTER_RATIO
                or bool(overlong_bold_spans)
                or fragment_metrics["missing_label_count"] > 0
            )
        )
        criteria["emphasis_selective"] = not emphasis_needs_polish
        if emphasis_needs_polish:
            issues.append(
                self._quality_issue(
                    code=EMPHASIS_QUALITY_ISSUE_CODE,
                    severity="soft",
                    agent_id="typography_polish_agent",
                    summary="重点过多、过长，或并列叙事片段缺少简短的小标题",
                    evidence={
                        "bold_span_count": len(bold_spans),
                        "bold_character_ratio": round(emphasis_ratio, 4),
                        "selective_emphasis_character_ratio": round(
                            selective_emphasis_ratio, 4
                        ),
                        "overlong_bold_span_count": len(overlong_bold_spans),
                        **fragment_metrics,
                    },
                    required_capabilities=["skill:typography_style"],
                )
            )

        visual_plan = state.environment.get("visual_plan")
        planned_diagram_gaps = self._planned_diagram_gaps(document, visual_plan)
        planned_diagrams = (
            visual_plan.get("diagrams", [])
            if isinstance(visual_plan, dict)
            else []
        )
        if planned_diagrams:
            criteria[VISUAL_PLAN_QUALITY_CRITERION] = not planned_diagram_gaps
            criteria["visual_explains_relationships"] = not planned_diagram_gaps
        else:
            has_diagram = bool(
                re.search(
                    r"```\s*(?:plantuml|mermaid)\b",
                    document,
                    re.IGNORECASE,
                )
            )
            visual_expected = bool(
                state.environment.get("requirement", {}).get("needs_images")
            ) or any(
                marker in context
                for marker in (
                    "架构",
                    "流程",
                    "时序",
                    "链路",
                    "模块关系",
                    "状态机",
                )
            )
            visual_needs_polish = (
                len(document.strip()) >= 500
                and visual_expected
                and not has_diagram
            )
            criteria["visual_explains_relationships"] = not visual_needs_polish
        if planned_diagram_gaps:
            issues.append(
                self._quality_issue(
                    code=VISUAL_PLAN_QUALITY_ISSUE_CODE,
                    severity="soft",
                    agent_id="image_polish_agent",
                    summary="章节 Visual Plan 中的图示缺失、位置错误或类型不一致",
                    evidence={"missing_diagrams": planned_diagram_gaps[:8]},
                    required_capabilities=[
                        MERMAID_DIAGRAM_TOOL_ID,
                        "skill:image_style",
                    ],
                )
            )
        elif not planned_diagrams and visual_needs_polish:
            issues.append(
                self._quality_issue(
                    code="visual_needs_polish",
                    severity="soft",
                    agent_id="image_polish_agent",
                    summary="关键关系或流程仅靠连续文字表达，需要可编辑代码图示",
                    evidence={"has_diagram": has_diagram},
                    required_capabilities=[
                        PLANTUML_DIAGRAM_TOOL_ID,
                        MERMAID_DIAGRAM_TOOL_ID,
                        "skill:image_style",
                    ],
                )
            )

        return criteria, issues

    @staticmethod
    def _coverage_terms(value: object) -> list[str]:
        text = re.sub(r"\s+", "", str(value or ""))
        text = re.sub(
            r"(?:分别|使用了?|用了?|哪些|什么|多少|情况|如何|是否|有何)",
            "",
            text,
        )
        terms = [
            item
            for item in re.split(r"[/／、，,；;和及与]", text)
            if len(item) >= 2
        ]
        return list(dict.fromkeys([text, *terms])) if text else []

    @classmethod
    def _multi_target_coverage_result(
        cls,
        document: str,
        contract: dict[str, Any],
    ) -> dict[str, Any]:
        """确定性检查每个枚举目标附近是否覆盖了每个提问维度。"""
        targets = [
            str(item).strip()
            for item in contract.get("targets", [])
            if str(item).strip()
        ]
        facets = [
            str(item).strip()
            for item in contract.get("facets", [])
            if str(item).strip()
        ]
        heading_matches = list(re.finditer(r"(?m)^#{2,3}\s+.+$", document))
        sections = []
        for index, heading in enumerate(heading_matches):
            end = (
                heading_matches[index + 1].start()
                if index + 1 < len(heading_matches)
                else len(document)
            )
            sections.append(document[heading.start():end])
        if not sections:
            sections = [document]

        gaps = []
        for target in targets:
            target_terms = cls._coverage_terms(target)
            matched_sections = [
                section
                for section in sections
                if any(
                    term in re.sub(r"\s+", "", section)
                    for term in target_terms
                )
            ]
            if not matched_sections:
                gaps.append(
                    {
                        "target": target,
                        "missing_facets": facets,
                        "reason": "target_missing",
                    }
                )
                continue
            scope = "\n".join(matched_sections)
            missing_facets = []
            for facet in facets:
                facet_terms = cls._coverage_terms(facet)
                if facet_terms and not any(term in scope for term in facet_terms):
                    missing_facets.append(facet)
            if missing_facets:
                gaps.append(
                    {
                        "target": target,
                        "missing_facets": missing_facets,
                        "reason": "facet_missing",
                    }
                )
        return {
            "passed": not gaps,
            "target_count": len(targets),
            "facet_count": len(facets),
            "gaps": gaps[:16],
        }

    @classmethod
    def _data_query_result_gaps(
        cls,
        document: str,
        query_results: Any,
    ) -> list[dict[str, Any]]:
        """检查确定性表格结果是否以完整行进入成稿。

        这里只消费 QueryPlan/QueryResult 契约，不识别报表、字段或维度名称。
        非完整覆盖结果不能被当作全局集合，因此不要求 Writer 强行写入。
        """
        normalized_document = cls._normalize_query_quality_text(document)
        gaps: list[dict[str, Any]] = []
        for result in list(query_results or []):
            if not isinstance(result, dict) or result.get("shape") != "table":
                continue
            validation = result.get("validation") or {}
            if validation.get("status") != "verified":
                continue
            rows = [
                row
                for row in (result.get("rows") or [])
                if isinstance(row, dict)
            ]
            if not rows:
                continue
            missing_row_ids: list[str] = []
            for row in rows:
                cells = row.get("cells") or {}
                cell_variants: list[list[str]] = []
                for cell in cells.values() if isinstance(cells, dict) else []:
                    if not isinstance(cell, dict):
                        continue
                    variants: list[str] = []
                    for value in (cell.get("raw"), cell.get("normalized")):
                        normalized = cls._normalize_query_quality_text(value)
                        if len(normalized) >= 2 and normalized not in variants:
                            variants.append(normalized)
                    if variants:
                        cell_variants.append(variants)
                # 一个稳定身份值和一个度量值同时出现，才能证明整行而不是
                # 偶然重复的单元格进入了文档；只有一列时则退化为该列命中。
                required_hits = 1 if len(cell_variants) <= 1 else 2
                hit_count = sum(
                    1
                    for variants in cell_variants
                    if any(value in normalized_document for value in variants)
                )
                if hit_count < required_hits:
                    missing_row_ids.append(str(row.get("row_id") or ""))
            if missing_row_ids:
                gaps.append(
                    {
                        "skill_step_id": result.get("skill_step_id"),
                        "relation_id": (result.get("provenance") or {}).get(
                            "relation_id"
                        ),
                        "expected_rows": len(rows),
                        "missing_rows": len(missing_row_ids),
                        "missing_row_ids": missing_row_ids[:20],
                    }
                )
        return gaps

    @staticmethod
    def _normalize_query_quality_text(value: Any) -> str:
        return re.sub(
            r"[^0-9a-z\u3400-\u9fff]+",
            "",
            str(value or "").casefold(),
        )

    @staticmethod
    def _placeholder_count(document: str) -> int:
        """统计真正的占位符。

        英文 TODO/TBD 与中文“待补充、此处补充、后续完善”只有在独占一行、
        列表项或表格单元格时才算占位符；写在正常句子里（例如确认事项中的
        “是否有摘要待补充”）不能误判为未完成，否则质检会和润色 Agent
        反复拉扯、不断重写全文。
        """
        marker = re.compile(
            r"[\[【(（]?"
            r"(?:待补充(?:具体)?(?:数值|数据|指标)?|此处补充|后续完善|(?i:TODO|TBD))"
            r"[\]】)）]?[。：:，,；;\s]*"
        )
        count = 0
        for line in document.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("|") and stripped.endswith("|"):
                cells = [
                    cell.strip()
                    for cell in stripped.strip("|").split("|")
                ]
            else:
                cells = [re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", stripped)]
            for cell in cells:
                if cell and marker.fullmatch(cell):
                    count += 1
        return count

    @staticmethod
    def _guard_generated_placeholders(
        document: str,
        requirement: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]]]:
        """阻止模型把错误周次或无数据占位文案写进最终交付物。"""
        if not document.strip():
            return document, []
        audit: list[dict[str, Any]] = []
        result = document
        time_context = (
            requirement.get("time_context", {})
            if isinstance(requirement, dict)
            else {}
        )
        if time_context.get("period_kind") in {"current_week", "previous_week"}:
            display = str(time_context.get("display") or "").strip()
            iso_year = int(time_context.get("iso_year") or 0)
            iso_week = int(time_context.get("iso_week") or 0)
            if display and iso_year > 0 and iso_week > 0:
                patterns = (
                    r"本周[（(]\s*20\d{2}\s*年第\s*(?:[Xx?？]+|\d+)\s*周\s*[）)]",
                    r"20\d{2}\s*年第\s*[Xx?？]+\s*周",
                )
                replacement_values = (f"本周（{display}）", display)
                for pattern, replacement in zip(patterns, replacement_values):
                    updated, count = re.subn(pattern, replacement, result)
                    if count:
                        audit.append(
                            {
                                "kind": "relative_time_corrected",
                                "count": count,
                                "replacement": replacement,
                            }
                        )
                        result = updated

        removed_lines: list[str] = []
        kept_lines: list[str] = []
        for line in result.splitlines():
            normalized = "".join(line.split())
            is_metric_placeholder = (
                "数据未明确区分" in normalized
                or "数据未明确" in normalized
                or "数据未获取" in normalized
                or "未获取到数据" in normalized
                or "指标未获取" in normalized
                or "待补充具体数值" in normalized
            )
            is_empty_progress_placeholder = bool(
                re.search(
                    r"(?:本周暂无(?:相关|明确)?(?:会议记录|进展记录|进展)|"
                    r"未检索到本周.*(?:会议纪要|进展))",
                    normalized,
                )
            )
            if is_metric_placeholder or is_empty_progress_placeholder:
                removed_lines.append(line)
                continue
            kept_lines.append(line)
        if removed_lines:
            audit.append(
                {
                    "kind": "unsupported_placeholder_removed",
                    "count": len(removed_lines),
                }
            )
            result = "\n".join(kept_lines)
            result = re.sub(r"\n{3,}", "\n\n", result).strip() + "\n"
        return result, audit

    @staticmethod
    def _quality_issue(
        *,
        code: str,
        severity: str,
        agent_id: str,
        summary: str,
        evidence: Optional[Any] = None,
        required_capabilities: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        return {
            "code": code,
            "severity": severity,
            "agent_id": agent_id,
            "summary": summary,
            "evidence": evidence if evidence is not None else {},
            "required_capabilities": required_capabilities or [],
        }

    @staticmethod
    def _prose_for_quality(document: str) -> str:
        prose = re.sub(r"```.*?```", "", document, flags=re.DOTALL)
        prose = re.sub(r"(?m)^\s*\|.*\|\s*$", "", prose)
        prose = re.sub(r"(?m)^#{1,6}\s+", "", prose)
        return prose

    @staticmethod
    def _narrative_fragment_label_metrics(document: str) -> dict[str, int]:
        """Check structure, not business vocabulary, for scannable list fragments."""
        bullet_pattern = re.compile(r"^(\s{0,3})[-+*]\s+(.+?)\s*$")
        label_pattern = re.compile(
            r"^(?:\[[ xX]\]\s+)?\*\*([^*\n]{1,32})\*\*(.*)$"
        )
        groups: list[list[str]] = []
        current: list[str] = []
        for line in document.splitlines():
            match = bullet_pattern.match(line)
            if match:
                current.append(match.group(2).strip())
                continue
            if not line.strip() and current:
                continue
            if current:
                groups.append(current)
                current = []
        if current:
            groups.append(current)

        eligible_items = [
            item
            for group in groups
            if len(group) >= 2
            for item in group
            if len(re.sub(r"[*_`\[\]]", "", item)) >= MIN_NARRATIVE_FRAGMENT_CHARS
        ]
        labeled_count = 0
        labeled_bold_chars = 0
        for item in eligible_items:
            match = label_pattern.match(item)
            if not match:
                continue
            label = match.group(1).strip()
            remainder = match.group(2).lstrip()
            if label.endswith(("：", ":")) or remainder.startswith(("：", ":")):
                labeled_count += 1
                labeled_bold_chars += len(label)
        return {
            "narrative_fragment_count": len(eligible_items),
            "labeled_fragment_count": labeled_count,
            "labeled_fragment_bold_chars": labeled_bold_chars,
            "missing_label_count": len(eligible_items) - labeled_count,
        }

    @staticmethod
    def _restore_strict_skill_section_headings(
        state: LoopState,
        document: str,
    ) -> tuple[str, int]:
        """Restore a strict Skill's section contract without changing its content."""
        strict_ids = {
            str(item) for item in state.environment.get("strict_skill_ids", [])
        }
        expected = [
            str(step.get("title") or "").strip()
            for skill in state.environment.get("applied_skills", [])
            if isinstance(skill, dict)
            and (not strict_ids or str(skill.get("id") or "") in strict_ids)
            for step in skill.get("execution_steps", []) or []
            if isinstance(step, dict) and str(step.get("title") or "").strip()
        ]
        matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", document))
        if not expected or len(matches) != len(expected):
            return document, 0
        replacements = 0
        chunks: list[str] = []
        cursor = 0
        for match, title in zip(matches, expected):
            chunks.append(document[cursor:match.start()])
            chunks.append(f"## {title}")
            cursor = match.end()
            if match.group(1).strip() != title:
                replacements += 1
        chunks.append(document[cursor:])
        return "".join(chunks), replacements

    @staticmethod
    def _ai_style_signals(prose: str) -> dict[str, Any]:
        compact = re.sub(r"\s+", "", prose)
        if len(compact) < 260:
            return {}
        quote_pairs = len(re.findall(r"“[^”\n]{1,80}”", prose))
        boilerplate = {
            phrase: prose.count(phrase)
            for phrase in AI_STYLE_BOILERPLATE
            if prose.count(phrase)
        }
        transitions = {
            phrase: prose.count(phrase)
            for phrase in AI_STYLE_TRANSITIONS
            if prose.count(phrase)
        }
        sentences = [
            sentence.strip()
            for sentence in re.split(r"[。！？!?；;\n]+", prose)
            if len(sentence.strip()) >= 8
        ]
        overlong_count = sum(1 for sentence in sentences if len(sentence) > 90)
        signals: dict[str, Any] = {}
        if quote_pairs >= max(5, len(compact) // 360):
            signals["decorative_quote_pairs"] = quote_pairs
        if sum(boilerplate.values()) >= 3:
            signals["boilerplate_phrases"] = boilerplate
        if sum(transitions.values()) >= 8 or any(
            count >= 3 for count in transitions.values()
        ):
            signals["mechanical_transitions"] = transitions
        if overlong_count >= 2 and overlong_count / max(1, len(sentences)) >= 0.18:
            signals["overlong_sentences"] = overlong_count
        return signals

    @classmethod
    def _short_detail_sections(cls, document: str) -> list[str]:
        result: list[str] = []
        ignored_markers = ("参考", "附录", "核验", "结语", "总结")
        for span in cls._markdown_section_spans(document):
            if int(span.get("level") or 0) != 2:
                continue
            title = str(span.get("title") or "")
            if any(marker in title for marker in ignored_markers):
                continue
            block = document[int(span["start"]) : int(span["end"])]
            body = re.sub(r"(?m)^#{1,6}\s+.*$", "", block)
            body = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
            body = re.sub(r"(?m)^\s*[-*+]\s+", "", body)
            body = re.sub(r"\s+", "", body)
            if 0 < len(body) < 60:
                result.append(title)
        return result

    @staticmethod
    def _strict_skill_document_title(state: LoopState) -> str:
        strict_ids = {
            str(item) for item in state.environment.get("strict_skill_ids", [])
        }
        applied = [
            item
            for item in state.environment.get("applied_skills", [])
            if isinstance(item, dict)
            and (not strict_ids or str(item.get("id")) in strict_ids)
        ]
        name = str((applied[0] if applied else {}).get("name") or "创作结果").strip()
        name = re.sub(r"(?:创作|写作)(?:方法|法|技能)$", "", name).strip()
        name = re.sub(r"\s*Skill$", "", name, flags=re.IGNORECASE).strip()
        return name or "创作结果"

    @classmethod
    def _strict_skill_step_content(
        cls,
        item: dict[str, Any],
        workflow_titles: list[str],
    ) -> tuple[str, dict[str, Any]]:
        """把单个 Skill 步骤结果收敛到当前章节，不误删章节内部结构。"""
        content = str(item.get("content") or "").strip()
        title = str(item.get("title") or item.get("step_id") or "执行结果").strip()
        declared_heading_scope = cls._normalize_section_name(
            " ".join(
                (
                    title,
                    str(item.get("objective") or ""),
                    str(item.get("output") or ""),
                )
            )
        )
        other_titles = {
            cls._normalize_section_name(value)
            for value in workflow_titles
            if cls._normalize_section_name(value)
            != cls._normalize_section_name(title)
        }
        forbidden_top_level_markers = (
            "结论",
            "重点进展",
            "风险",
            "阻塞",
            "下周计划",
            "后续计划",
        )
        normalized_lines: list[str] = []
        skipped_headings: list[str] = []
        preserved_subheadings: list[str] = []
        fallback_lines: list[str] = []
        skip_undeclared_block = False
        skip_reason = ""
        for line in content.splitlines():
            heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", line)
            if not heading:
                if not skip_undeclared_block:
                    normalized_lines.append(line)
                elif skip_reason == "forbidden_expansion":
                    fallback_lines.append(line)
                continue
            level = len(heading.group(1))
            heading_title = heading.group(2).strip()
            normalized_heading = cls._normalize_section_name(heading_title)
            if (
                not any(value.strip() for value in normalized_lines)
                and normalized_heading == cls._normalize_section_name(title)
            ):
                skip_undeclared_block = False
                skip_reason = ""
                continue

            is_other_workflow_step = normalized_heading in other_titles
            is_forbidden_expansion = (
                level <= 2
                and normalized_heading not in declared_heading_scope
                and any(
                    marker in heading_title
                    for marker in forbidden_top_level_markers
                )
            )
            if is_other_workflow_step or is_forbidden_expansion:
                skip_undeclared_block = True
                skip_reason = (
                    "other_workflow_step"
                    if is_other_workflow_step
                    else "forbidden_expansion"
                )
                skipped_headings.append(heading_title)
                continue

            # 当前步骤的子标题属于章节内部表达。即使模型用了 H1/H2，也降为
            # H3 后保留，避免合法的列表、指标表被连同标题整块静默删除。
            skip_undeclared_block = False
            skip_reason = ""
            normalized_level = min(6, max(3, level))
            normalized_lines.append(f"{'#' * normalized_level} {heading_title}")
            preserved_subheadings.append(heading_title)

        normalized = "\n".join(normalized_lines).strip()
        recovered_from_empty = False
        if content and not normalized and any(line.strip() for line in fallback_lines):
            # 最后一道防丢失保护：如果模型把所有有效文字都包在一个未声明的
            # 通用顶层标题下，移除标题但保留正文。其它工作流步骤仍不会恢复。
            normalized = "\n".join(fallback_lines).strip()
            recovered_from_empty = bool(normalized)
        audit = {
            "step_id": str(item.get("step_id") or ""),
            "source_chars": len(content),
            "retained_chars": len(normalized),
            "skipped_heading_count": len(skipped_headings),
            "preserved_subheading_count": len(preserved_subheadings),
            "recovered_from_empty": recovered_from_empty,
        }
        return normalized, audit

    def _assemble_strict_skill_document(
        self,
        state: LoopState,
        *,
        pending_step: Optional[dict[str, Any]] = None,
        pending_content: str = "",
        include_audit: bool = False,
    ) -> Any:
        strict_ids = {
            str(item) for item in state.environment.get("strict_skill_ids", [])
        }
        workflow_titles = [
            str(raw_step.get("title") or "")
            for skill in state.environment.get("applied_skills", [])
            if isinstance(skill, dict)
            and (not strict_ids or str(skill.get("id") or "") in strict_ids)
            for raw_step in skill.get("execution_steps", []) or []
            if isinstance(raw_step, dict)
        ]
        completed_items = list(state.environment.get("completed_skill_steps", []))
        if pending_step is not None and pending_content.strip():
            completed_items = [
                item
                for item in completed_items
                if not (
                    isinstance(item, dict)
                    and item.get("skill_id") == pending_step.get("skill_id")
                    and item.get("step_id") == pending_step.get("skill_step_id")
                )
            ]
            completed_items.append(
                {
                    "skill_id": pending_step.get("skill_id"),
                    "step_id": pending_step.get("skill_step_id"),
                    "title": pending_step.get("skill_step_title"),
                    "objective": pending_step.get("skill_step_objective"),
                    "output": pending_step.get("skill_step_output"),
                    "content": pending_content,
                }
            )
        sections: list[str] = []
        audits: list[dict[str, Any]] = []
        seen_steps: set[tuple[str, str]] = set()
        for item in completed_items:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or "")
            step_id = str(item.get("step_id") or "")
            if strict_ids and skill_id not in strict_ids:
                continue
            key = (skill_id, step_id)
            content = str(item.get("content") or "").strip()
            title = str(item.get("title") or step_id or "执行结果").strip()
            if not content or key in seen_steps:
                continue
            seen_steps.add(key)
            normalized, audit = self._strict_skill_step_content(
                item,
                workflow_titles,
            )
            audits.append(audit)
            sections.append(f"## {title}\n\n{normalized}")
        if not sections:
            return ("", audits) if include_audit else ""
        document = (
            f"# {self._strict_skill_document_title(state)}\n\n"
            + "\n\n".join(sections)
        ).strip()
        return (document, audits) if include_audit else document

    @staticmethod
    def _record_completed_skill_step(
        state: LoopState,
        step: dict[str, Any],
        content: str,
    ) -> dict[str, Any]:
        step_result = {
            "skill_id": step.get("skill_id"),
            "step_id": step.get("skill_step_id"),
            "title": step.get("skill_step_title"),
            "objective": step.get("skill_step_objective"),
            "output": step.get("skill_step_output"),
            "skills": step.get("skill_step_skills", []),
            "content": content,
        }
        completed_steps = state.environment.setdefault("completed_skill_steps", [])
        completed_steps[:] = [
            item
            for item in completed_steps
            if not (
                isinstance(item, dict)
                and item.get("skill_id") == step_result["skill_id"]
                and item.get("step_id") == step_result["step_id"]
            )
        ]
        completed_steps.append(step_result)
        return step_result

    @staticmethod
    def _record_failed_skill_step(
        state: LoopState,
        step: dict[str, Any],
        reason: str,
    ) -> None:
        """记录失败的 Skill 步骤；失败步骤不写入文档，只在执行轨迹中展示原因。"""
        failed_record = {
            "skill_id": step.get("skill_id"),
            "step_id": step.get("skill_step_id"),
            "title": step.get("skill_step_title"),
            "reason": reason,
        }
        failed_steps = state.environment.setdefault("failed_skill_steps", [])
        failed_steps[:] = [
            item
            for item in failed_steps
            if not (
                isinstance(item, dict)
                and item.get("skill_id") == failed_record["skill_id"]
                and item.get("step_id") == failed_record["step_id"]
            )
        ]
        failed_steps.append(failed_record)

    @staticmethod
    def _markdown_table_quality(document: str) -> tuple[bool, int]:
        lines = document.splitlines()
        has_table = False
        malformed = 0
        for index in range(len(lines) - 1):
            header = lines[index].strip()
            separator = lines[index + 1].strip()
            if not (header.startswith("|") and header.endswith("|")):
                continue
            if not re.match(
                r"^\|\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|$",
                separator,
            ):
                continue
            has_table = True
            expected = max(0, header.count("|") - 1)
            row_index = index + 2
            while row_index < len(lines):
                row = lines[row_index].strip()
                if not (row.startswith("|") and row.endswith("|")):
                    break
                if max(0, row.count("|") - 1) != expected:
                    malformed += 1
                row_index += 1
        return has_table, malformed

    @classmethod
    def _planned_diagram_gaps(
        cls,
        document: str,
        visual_plan: Any,
    ) -> list[dict[str, Any]]:
        """检查 Visual Plan 中的图是否落在对应章节且图类型一致。"""
        if not isinstance(visual_plan, dict):
            return []
        diagrams = visual_plan.get("diagrams")
        if not isinstance(diagrams, list) or not diagrams:
            return []
        spans = cls._markdown_section_spans(document)
        expected_prefixes = {
            "flowchart": ("flowchart", "graph"),
            "flowchart_lr": ("flowchart", "graph"),
            "sequence": ("sequencediagram",),
            "state": ("statediagram",),
            "class": ("classdiagram",),
            "er": ("erdiagram",),
            "journey": ("journey",),
            "gantt": ("gantt",),
            "mindmap": ("mindmap",),
        }
        gaps: list[dict[str, Any]] = []
        for spec in diagrams:
            if not isinstance(spec, dict):
                continue
            section_title = str(spec.get("section_title") or "").strip()
            matched = cls._find_section_span(section_title, spans)
            if not matched:
                gaps.append(
                    {
                        "diagram_id": spec.get("id"),
                        "section_title": section_title,
                        "expected_type": spec.get("diagram_type"),
                        "reason": "section_missing",
                    }
                )
                continue
            section = document[int(matched["start"]) : int(matched["end"])]
            blocks = re.findall(
                r"```\s*mermaid\s*\n([\s\S]*?)```",
                section,
                re.IGNORECASE,
            )
            if not blocks:
                gaps.append(
                    {
                        "diagram_id": spec.get("id"),
                        "section_title": section_title,
                        "expected_type": spec.get("diagram_type"),
                        "reason": "diagram_missing",
                    }
                )
                continue
            expected_type = str(spec.get("diagram_type") or "flowchart")
            prefixes = expected_prefixes.get(expected_type, ())
            normalized_first_lines = [
                re.sub(r"\s+", "", block.strip().splitlines()[0]).lower()
                for block in blocks
                if block.strip()
            ]
            if prefixes and not any(
                any(line.startswith(prefix) for prefix in prefixes)
                for line in normalized_first_lines
            ):
                gaps.append(
                    {
                        "diagram_id": spec.get("id"),
                        "section_title": section_title,
                        "expected_type": expected_type,
                        "reason": "diagram_type_mismatch",
                    }
                )
        return gaps

    async def _apply_model_result(
        self, state: LoopState, model_result: str
    ) -> AsyncIterator[dict[str, Any]]:
        pending = state.pending_model_step or {}
        step = pending.get("step")
        state.pending_model_step = None
        if not step:
            return
        async for event in self._complete_model_step(state, step, model_result):
            yield event

    async def _complete_model_step(
        self, state: LoopState, step: dict[str, Any], result: str
    ) -> AsyncIterator[dict[str, Any]]:
        actor = self._actor("agent", step["id"], step["name"])
        cleaned = result.strip()
        if step.get("action") == "route":
            requirement = state.environment.get("requirement") or {}
            query = str(state.environment.get("context_query") or state.user_message)
            try:
                decision = self.service.parse_routing_decision(cleaned)
                decision["source"] = "model"
            except Exception:
                # 校验失败时降级为保守路由，不阻断创作链路；降级探针同样
                # 只产出已启用工具，与模型路径的披露契约保持一致。
                decision = fallback_routing_decision(
                    query,
                    requirement,
                    set(
                        normalize_creation_tool_ids(
                            state.options.get("enabled_tools")
                        )
                    ),
                )
            async for event in self._apply_routing_decision(state, step, decision):
                yield event
            return
        if step.get("action") == "data_query_plan":
            current_results = [
                item
                for item in (
                    state.environment.get("current_data_results")
                    or state.environment.get("data_results")
                    or []
                )
                if isinstance(item, dict)
            ]
            catalog = relation_catalog(current_results)
            try:
                plan = validate_query_plan(parse_query_plan(cleaned), catalog)
                query_result = execute_query_plan(plan, current_results)
                planner_status = "completed"
                planner_error = None
            except QueryPlanError as exc:
                # 规划失败只关闭确定性关系执行，不阻断既有的叙述总结、分析和
                # 数据渲染路径；不能用猜测性代码替模型补造字段绑定。
                plan = {
                    "schema_version": "memorybread.data-query-plan.v1",
                    "mode": "narrative",
                    "operations": [],
                    "reason": "planner_validation_failed",
                }
                query_result = execute_query_plan(plan, current_results)
                planner_status = "fallback"
                planner_error = exc.code
            scoped_plan = {
                **plan,
                "skill_step_id": step.get("skill_step_id"),
                "skill_step_title": step.get("skill_step_title"),
            }
            scoped_result = {
                **query_result,
                "skill_step_id": step.get("skill_step_id"),
                "skill_step_title": step.get("skill_step_title"),
            }
            state.environment.setdefault("data_query_plans", []).append(scoped_plan)
            state.environment.setdefault("data_query_results", []).append(scoped_result)
            state.environment.setdefault("tool_results", []).append(
                {
                    "tool_id": "data_query_executor",
                    "status": planner_status,
                    "mode": plan.get("mode"),
                    "result_shape": query_result.get("shape"),
                    "result_row_count": len(query_result.get("rows") or []),
                    "validation_status": (
                        query_result.get("validation") or {}
                    ).get("status"),
                    "error_code": planner_error,
                    "skill_step_id": step.get("skill_step_id"),
                }
            )
            self._update_goal(state)
            yield self._event(
                state,
                "agent.completed",
                (
                    "已完成通用数据查询规划与确定性执行"
                    if plan.get("mode") == "relational"
                    else "当前目标保留叙述型数据分析路径"
                ),
                status="completed",
                actor=actor,
                environment_patch={
                    "data_query_plan": scoped_plan,
                    "data_query_result": {
                        "shape": scoped_result.get("shape"),
                        "row_count": len(scoped_result.get("rows") or []),
                        "validation": scoped_result.get("validation"),
                    },
                },
                data={
                    "mode": plan.get("mode"),
                    "result_shape": query_result.get("shape"),
                    "result_row_count": len(query_result.get("rows") or []),
                    "validation": query_result.get("validation"),
                    "error_code": planner_error,
                },
            )
            yield self._thinking_completed(
                state,
                "generation",
                "已把自然语言目标编译为受控数据计划，并由程序执行可验证算子",
            )
            return
        if (
            step.get("action") == "writer"
            and step.get("skill_step_id")
            and state.environment.get("strict_skill_workflow")
        ):
            if not cleaned:
                raise RuntimeError(f"{step['name']} 未返回步骤产出")
            step_result = self._record_completed_skill_step(state, step, cleaned)
            assembled, assembly_audits = self._assemble_strict_skill_document(
                state,
                include_audit=True,
            )
            assembly_audit = assembly_audits[-1] if assembly_audits else {}
            state.environment.setdefault("strict_skill_assembly_audits", []).append(
                assembly_audit
            )
            if assembled:
                state.environment["document"] = assembled
                state.current_document = assembled
                yield self._event(
                    state,
                    "document.replaced",
                    "文档撰写 Agent 已按当前 Skill 步骤更新文档",
                    status="completed",
                    actor=actor,
                    data={
                        "content": assembled,
                        "operation": "strict_skill_workflow_assembly",
                        "assembly_audit": assembly_audit,
                    },
                )
            self._update_goal(state)
            completed_summary = (
                f"已生成「{self._step_content_title(step)}」内容，并把结果写回创作文档"
                if self._step_content_title(step)
                else f"{step['name']} 已完成当前 Skill 步骤"
            )
            yield self._event(
                state,
                "agent.completed",
                completed_summary,
                status="completed",
                actor=actor,
                environment_patch={
                    "skill_step": {
                        **step_result,
                        "content": self.service._clip(cleaned, 1200),
                    }
                },
            )
            yield self._thinking_completed(
                state, "generation", self._generation_reasoning(state, step)
            )
            return
        if step["action"] == "polisher":
            if not cleaned:
                raise RuntimeError(f"{step['name']} 未返回润色后的完整文档")
            if (
                step["id"] == "document_unify_polisher"
                and state.environment.get("strict_skill_workflow")
            ):
                cleaned, restored_heading_count = (
                    self._restore_strict_skill_section_headings(state, cleaned)
                )
                if restored_heading_count:
                    state.environment["strict_skill_heading_restore"] = {
                        "restored_heading_count": restored_heading_count,
                    }
            base_document = state.current_document
            relevant_issues = [
                item
                for item in state.environment.get("quality_issues", [])
                if isinstance(item, dict) and item.get("agent_id") == step["id"]
            ]
            document_patch = self._build_document_revision_patch(
                base_document,
                cleaned,
                operation=f"quality_polish:{step['id']}",
                requested_sections=[
                    str(section)
                    for item in relevant_issues
                    for section in (
                        (item.get("evidence") or {}).get("short_sections", [])
                        if isinstance(item.get("evidence"), dict)
                        else []
                    )
                ],
                preserved_untouched=True,
            )
            state.environment["document"] = cleaned
            state.environment["last_document_patch"] = document_patch
            state.current_document = cleaned
            state.environment.setdefault("quality_mutations", []).append(
                {
                    "agent_id": step["id"],
                    "quality_cycle": step.get("quality_cycle"),
                    "issue_codes": [
                        str(item.get("code") or "") for item in relevant_issues
                    ],
                    "document_hash": self._document_hash(cleaned),
                }
            )
            patch = {
                "document_length": len(cleaned),
                "document_patch": document_patch,
            }
            yield self._event(
                state,
                "document.patch.applied",
                f"{step['name']}已应用：{document_patch['summary']}",
                status="completed",
                actor=actor,
                environment_patch={"document_patch": document_patch},
                data={"content": cleaned, "patch": document_patch},
            )
            if step.get("skill_step_id"):
                state.environment["strict_skill_document_owned_by_agent"] = step["id"]
            if step["id"] == "document_unify_polisher":
                # 全文整合润色后的文档是最终交付物；run 结束时的白名单
                # 重组不得再用未润色的步骤原文覆盖它。
                state.environment["strict_skill_document_polished"] = True
        elif step["action"] == "writer":
            intent = state.environment.get("edit_intent", {})
            operation = str(intent.get("operation") or "")
            if state.mode == "revision":
                if not cleaned:
                    raise RuntimeError("文档撰写 Agent 未返回修订后的完整文档")
                base_document = str(
                    state.environment.get("revision_base_document")
                    or state.current_document
                )
                document_patch = self._build_document_revision_patch(
                    base_document,
                    cleaned,
                    operation=operation or "revise_document",
                    requested_sections=[
                        str(item) for item in intent.get("target_sections", [])
                    ],
                    preserved_untouched=bool(
                        intent.get("preserve_untouched", True)
                    ),
                )
                state.environment["document"] = cleaned
                state.environment["last_document_patch"] = document_patch
                state.current_document = cleaned
                patch = {
                    "document_length": len(cleaned),
                    "document_patch": document_patch,
                }
                yield self._event(
                    state,
                    "document.patch.applied",
                    str(document_patch["summary"]),
                    status="completed",
                    actor=actor,
                    environment_patch={"document_patch": document_patch},
                    data={"content": cleaned, "patch": document_patch},
                )
            elif operation in {
                "append_section",
                "replace_section",
                "delete_section",
            }:
                updated, document_patch = self._apply_document_patch(
                    state.current_document,
                    cleaned,
                    operation=operation,
                    target_sections=[
                        str(item) for item in intent.get("target_sections", [])
                    ],
                )
                state.environment["document"] = updated
                state.environment["last_document_patch"] = document_patch
                state.current_document = updated
                patch = {
                    "document_length": len(updated),
                    "document_patch": document_patch,
                }
                yield self._event(
                    state,
                    "document.patch.applied",
                    str(document_patch["summary"]),
                    status="completed",
                    actor=actor,
                    environment_patch={"document_patch": document_patch},
                    data={"content": updated, "patch": document_patch},
                )
            else:
                if not cleaned:
                    raise RuntimeError("文档撰写 Agent 未返回文档内容")
                state.environment["document"] = cleaned
                state.current_document = cleaned
                patch = {
                    "document_length": len(cleaned),
                    "operation": operation or "rewrite_document",
                }
                yield self._event(
                    state,
                    "document.replaced",
                    "文档撰写 Agent 已提交完整文档版本",
                    status="completed",
                    actor=actor,
                    data={
                        "content": cleaned,
                        "operation": operation or "rewrite_document",
                    },
                )
            if step.get("skill_step_id"):
                state.environment["strict_skill_document_owned_by_agent"] = step["id"]
        elif step["action"] == "skill_step":
            if not cleaned:
                raise RuntimeError(f"{step['name']} 未返回步骤产出")
            step_result = self._record_completed_skill_step(state, step, cleaned)
            patch = {
                "skill_step": {
                    **step_result,
                    "content": self.service._clip(cleaned, 1200),
                }
            }
            if not state.environment.get("strict_skill_document_owned_by_agent"):
                assembled, assembly_audits = self._assemble_strict_skill_document(
                    state,
                    include_audit=True,
                )
                assembly_audit = assembly_audits[-1] if assembly_audits else {}
                state.environment.setdefault(
                    "strict_skill_assembly_audits", []
                ).append(assembly_audit)
                if assembled:
                    state.environment["document"] = assembled
                    state.current_document = assembled
                    yield self._event(
                        state,
                        "document.replaced",
                        "创作 Agent 已按 Skill 步骤顺序组装当前文档",
                        status="completed",
                        actor=actor,
                        data={
                            "content": assembled,
                            "operation": "strict_skill_workflow_assembly",
                            "assembly_audit": assembly_audit,
                        },
                    )
        elif step.get("id") == "chapter_design_agent":
            blueprint, visual_plan = parse_chapter_design_result(cleaned)
            state.environment["chapter_design"] = blueprint
            state.environment["visual_plan"] = visual_plan
            patch = {
                "chapter_design": self.service._clip(blueprint, 1200),
                "visual_plan": visual_plan,
            }
        else:
            output_key = step.get("output_key") or step["id"]
            state.environment[output_key] = cleaned
            patch = {output_key: self.service._clip(cleaned, 600)}
        self._update_goal(state)
        content_title = self._step_content_title(step)
        completed_summary = (
            f"已生成「{content_title}」内容，并把结果写回创作文档"
            if content_title
            else f"{step['name']} 已完成，并把结果写回创作环境"
        )
        yield self._event(
            state,
            "agent.completed",
            completed_summary,
            status="completed",
            actor=actor,
            environment_patch=patch,
        )
        yield self._thinking_completed(
            state, "generation", self._generation_reasoning(state, step)
        )

    def _apply_document_patch(
        self,
        document: str,
        generated_fragment: str,
        *,
        operation: str,
        target_sections: list[str],
    ) -> tuple[str, dict[str, Any]]:
        target = (target_sections[0] if target_sections else "").strip()
        if not document.strip():
            raise RuntimeError("局部修订缺少现有文档")
        if not target:
            raise RuntimeError("局部修订缺少目标章节")

        before_hash = self._document_hash(document)
        spans = self._markdown_section_spans(document)
        matched = self._find_section_span(target, spans)
        effective_operation = operation

        if operation == "delete_section":
            if not matched:
                raise RuntimeError(f"未在现有文档中找到要删除的“{target}”章节")
            updated = self._replace_span(document, matched["start"], matched["end"], "")
        else:
            fragment = self._extract_target_fragment(generated_fragment, target)
            if not fragment:
                raise RuntimeError(f"文档撰写 Agent 未返回“{target}”章节内容")
            if matched:
                effective_operation = "replace_section"
                updated = self._replace_span(
                    document,
                    matched["start"],
                    matched["end"],
                    fragment,
                )
            else:
                effective_operation = "append_section"
                updated = self._insert_section(document, fragment, spans)

        updated = updated.strip() + "\n"
        after_hash = self._document_hash(updated)
        if before_hash == after_hash:
            raise RuntimeError(f"“{target}”章节局部修订没有产生有效变更")

        action_label = {
            "append_section": "新增",
            "replace_section": "更新",
            "delete_section": "删除",
        }.get(effective_operation, "修改")
        patch = {
            "operation": effective_operation,
            "target_sections": [target],
            "base_hash": before_hash,
            "result_hash": after_hash,
            "preserved_untouched": True,
            "summary": f"已局部{action_label}“{target}”章节，其余内容保持不变",
        }
        return updated, patch

    @staticmethod
    def _document_hash(document: str) -> str:
        return hashlib.sha256(document.encode("utf-8")).hexdigest()[:16]

    def _build_document_revision_patch(
        self,
        base_document: str,
        updated_document: str,
        *,
        operation: str,
        requested_sections: list[str],
        preserved_untouched: bool,
    ) -> dict[str, Any]:
        changes = self._document_changes(base_document, updated_document)
        changed_sections: list[str] = []
        for change in changes:
            section = str(change.get("section_title") or "").strip()
            if section and section not in changed_sections:
                changed_sections.append(section)
        change_count = len(changes)
        section_preview = "、".join(changed_sections[:4])
        if change_count:
            summary = f"已按本轮指令完成 {change_count} 处调整"
            if section_preview:
                summary += f"，涉及{section_preview}"
        else:
            summary = "本轮修订未检测到正文差异"
        return {
            "operation": operation or "revise_document",
            "target_sections": changed_sections,
            "requested_sections": self._dedupe_strings(requested_sections),
            "changes": changes,
            "change_count": change_count,
            "base_hash": self._document_hash(base_document),
            "result_hash": self._document_hash(updated_document),
            "preserved_untouched": preserved_untouched,
            "summary": summary,
        }

    @classmethod
    def _document_changes(
        cls,
        base_document: str,
        updated_document: str,
    ) -> list[dict[str, Any]]:
        before_lines = base_document.splitlines()
        after_lines = updated_document.splitlines()
        before_sections = cls._line_section_titles(before_lines)
        after_sections = cls._line_section_titles(after_lines)
        before_section_names = {
            cls._normalize_section_name(section) for section in before_sections
        }
        after_section_names = {
            cls._normalize_section_name(section) for section in after_sections
        }
        matcher = SequenceMatcher(
            None,
            before_lines,
            after_lines,
            autojunk=False,
        )
        changes: list[dict[str, Any]] = []
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag == "equal":
                continue
            if new_start < new_end:
                for segment in cls._segment_line_range(
                    after_sections,
                    new_start,
                    new_end,
                ):
                    section_title = str(segment["section_title"])
                    change_type = (
                        "added"
                        if tag == "insert"
                        or cls._normalize_section_name(section_title)
                        not in before_section_names
                        else "modified"
                    )
                    changes.append(
                        {
                            "change_type": change_type,
                            "section_title": section_title,
                            "start_line": segment["start_line"],
                            "end_line": segment["end_line"],
                            "summary": cls._change_summary(
                                change_type,
                                str(segment["section_title"]),
                            ),
                        }
                    )
            if old_start < old_end and (
                new_start == new_end or tag == "replace"
            ):
                for segment in cls._segment_line_range(
                    before_sections,
                    old_start,
                    old_end,
                ):
                    section_title = str(segment["section_title"])
                    if (
                        tag == "replace"
                        and cls._normalize_section_name(section_title)
                        in after_section_names
                    ):
                        continue
                    changes.append(
                        {
                            "change_type": "deleted",
                            "section_title": section_title,
                            "start_line": None,
                            "end_line": None,
                            "base_start_line": segment["start_line"],
                            "base_end_line": segment["end_line"],
                            "summary": cls._change_summary(
                                "deleted",
                                str(segment["section_title"]),
                            ),
                        }
                    )
        return cls._merge_adjacent_changes(changes)

    @staticmethod
    def _line_section_titles(lines: list[str]) -> list[str]:
        current = "标题与导语"
        result: list[str] = []
        for line in lines:
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if match and len(match.group(1)) == 2:
                current = re.sub(r"\s+#+\s*$", "", match.group(2)).strip()
            result.append(current)
        return result

    @staticmethod
    def _segment_line_range(
        section_titles: list[str],
        start: int,
        end: int,
    ) -> list[dict[str, Any]]:
        if start >= end:
            return []
        segments: list[dict[str, Any]] = []
        segment_start = start
        current = section_titles[start] if start < len(section_titles) else "标题与导语"
        for index in range(start + 1, end):
            section = (
                section_titles[index]
                if index < len(section_titles)
                else current
            )
            if section == current:
                continue
            segments.append(
                {
                    "section_title": current,
                    "start_line": segment_start + 1,
                    "end_line": index,
                }
            )
            current = section
            segment_start = index
        segments.append(
            {
                "section_title": current,
                "start_line": segment_start + 1,
                "end_line": end,
            }
        )
        return segments

    @classmethod
    def _merge_adjacent_changes(
        cls,
        changes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for change in changes:
            previous = merged[-1] if merged else None
            if (
                previous
                and previous["change_type"] == change["change_type"]
                and previous["section_title"] == change["section_title"]
                and isinstance(previous.get("end_line"), int)
                and isinstance(change.get("start_line"), int)
                and int(change["start_line"]) <= int(previous["end_line"]) + 1
            ):
                previous["end_line"] = change["end_line"]
                continue
            merged.append(dict(change))
        return merged

    @staticmethod
    def _change_summary(change_type: str, section_title: str) -> str:
        action = {
            "added": "新增",
            "modified": "修改",
            "deleted": "删除",
        }.get(change_type, "调整")
        return f"{action}“{section_title}”中的内容"

    @staticmethod
    def _dedupe_strings(values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            text = value.strip()
            if text and text not in result:
                result.append(text)
        return result

    @classmethod
    def _revision_preserves_structure(
        cls,
        base_document: str,
        updated_document: str,
    ) -> bool:
        before = {
            cls._normalize_section_name(title)
            for title in cls._markdown_section_titles(base_document)
        }
        after = {
            cls._normalize_section_name(title)
            for title in cls._markdown_section_titles(updated_document)
        }
        if not before:
            return True
        return len(before & after) / len(before) >= 0.65

    @classmethod
    def _target_positions_are_logical(
        cls,
        document: str,
        target_sections: list[str],
        *,
        allow_missing: bool = False,
    ) -> bool:
        headings = [
            str(span["title"])
            for span in cls._markdown_section_spans(document)
            if int(span["level"]) == 2
        ]
        for target in target_sections:
            target_rank = cls._section_order_rank(target)
            if target_rank is None:
                continue
            matched = cls._match_existing_section(target, headings)
            if not matched:
                if allow_missing:
                    continue
                return False
            target_index = headings.index(matched)
            for index, heading in enumerate(headings):
                rank = cls._section_order_rank(heading)
                if rank is None or index == target_index:
                    continue
                if index < target_index and rank > target_rank:
                    return False
                if index > target_index and rank < target_rank:
                    return False
        return True

    @classmethod
    def _section_order_rank(cls, title: str) -> Optional[int]:
        normalized = cls._normalize_section_name(title)
        for rank, markers in SECTION_ORDER_RULES:
            if any(
                cls._normalize_section_name(marker) in normalized
                for marker in markers
            ):
                return rank
        return None

    @classmethod
    def _markdown_section_spans(cls, document: str) -> list[dict[str, Any]]:
        matches = list(re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*$", document))
        spans: list[dict[str, Any]] = []
        for index, match in enumerate(matches):
            level = len(match.group(1))
            end = len(document)
            for following in matches[index + 1 :]:
                if len(following.group(1)) <= level:
                    end = following.start()
                    break
            spans.append(
                {
                    "title": re.sub(r"\s+#+\s*$", "", match.group(2)).strip(),
                    "level": level,
                    "start": match.start(),
                    "end": end,
                }
            )
        return spans

    @classmethod
    def _find_section_span(
        cls,
        target: str,
        spans: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        normalized_target = cls._normalize_section_name(target)
        exact = [
            span
            for span in spans
            if cls._normalize_section_name(str(span["title"])) == normalized_target
        ]
        if exact:
            return exact[0]
        fuzzy = [
            span
            for span in spans
            if normalized_target in cls._normalize_section_name(str(span["title"]))
            or cls._normalize_section_name(str(span["title"])) in normalized_target
        ]
        return fuzzy[0] if fuzzy else None

    @classmethod
    def _extract_target_fragment(cls, result: str, target: str) -> str:
        text = result.strip()
        if not text:
            return ""
        fenced = re.fullmatch(r"```(?:markdown|md)?\s*\n([\s\S]*?)\n```", text)
        if fenced:
            text = fenced.group(1).strip()

        spans = cls._markdown_section_spans(text)
        matched = cls._find_section_span(target, spans)
        if matched:
            fragment = text[matched["start"] : matched["end"]].strip()
            heading_match = re.match(r"^(#{1,6})\s+", fragment)
            if heading_match and len(heading_match.group(1)) == 1:
                fragment = re.sub(r"^#\s+", "## ", fragment, count=1)
            return fragment

        body = text
        if body.startswith("{") and body.endswith("}"):
            raise RuntimeError("局部修订返回了无法识别的结构化内容")
        return f"## {target}\n\n{body}".strip()

    @classmethod
    def _insert_section(
        cls,
        document: str,
        fragment: str,
        spans: list[dict[str, Any]],
    ) -> str:
        fragment_spans = cls._markdown_section_spans(fragment)
        target_heading = next(
            (
                str(span["title"])
                for span in fragment_spans
                if int(span["level"]) == 2
            ),
            "",
        )
        target_rank = cls._section_order_rank(target_heading)
        if target_rank is not None:
            for span in spans:
                if int(span["level"]) != 2:
                    continue
                existing_rank = cls._section_order_rank(str(span["title"]))
                if existing_rank is not None and existing_rank > target_rank:
                    insertion = int(span["start"])
                    return (
                        f"{document[:insertion].rstrip()}\n\n"
                        f"{fragment}\n\n"
                        f"{document[insertion:].lstrip()}"
                    )

        trailing_markers = (
            "实施计划",
            "风险",
            "验收",
            "后续核验",
            "参考资料",
            "结语",
            "总结",
        )
        insertion = None
        for span in spans:
            if span["level"] != 2:
                continue
            normalized = cls._normalize_section_name(str(span["title"]))
            if any(
                cls._normalize_section_name(marker) in normalized
                for marker in trailing_markers
            ):
                insertion = int(span["start"])
                break
        if insertion is None:
            return f"{document.rstrip()}\n\n{fragment}\n"
        return (
            f"{document[:insertion].rstrip()}\n\n"
            f"{fragment}\n\n"
            f"{document[insertion:].lstrip()}"
        )

    @staticmethod
    def _replace_span(document: str, start: int, end: int, replacement: str) -> str:
        before = document[:start].rstrip()
        after = document[end:].lstrip()
        parts = [part for part in (before, replacement.strip(), after) if part]
        return "\n\n".join(parts)

    def _model_prompts(
        self, state: LoopState, step: dict[str, Any]
    ) -> tuple[str, str]:
        environment = self._prompt_environment(state, step)
        agent_id = step["id"]
        if step["action"] == "writer":
            if (
                step.get("skill_step_id")
                and state.environment.get("strict_skill_workflow")
            ):
                system = """你是 MemoryBread 的文档撰写 Agent，当前只负责已安装 Skill 明确声明的一个步骤。
execution_steps 是唯一流程和章节白名单。只生成当前步骤 title/objective/output 所要求的内容，不得输出整篇文档、总标题或其它步骤内容，不得把不同步骤的数据或表格合并。
workflow_role=support 的 Skill 只提供当前步骤所引用的能力，其 execution_steps 不是本轮流程，也不得据此增加章节。
除非当前步骤逐字要求，否则不得自行添加“结论”“重点进展”“风险/阻塞”“下周计划”等通用模板栏目。只使用当前步骤环境中的 Tool 结果、上一步产出和用户材料；缺少信息时直接省略，不得补造事实或占位说明。
章节结构和字体格式也是当前步骤的交付质量：外层二级标题会由 Harness 使用步骤 title 统一生成，不要重复输出。objective/output 明确包含多个子主题时，可使用与这些子主题语义一致的三级标题；不得新增未声明的通用栏目。
要求列表展示时，不要把多个长句无差别平铺。同一组含两个以上较长、彼此独立的无序列表片段时，每项开头必须提炼一个能区分对象或主题的最短名词短语，写成 `- **短标签：** 事实、动作或结果`；只加粗短标签，不能把整句前半段当标签。短小枚举不强制添加标签。当至少四项内容存在由当前证据直接支持的稳定分类时，使用最多两级的父子列表；无法确认分类时保留同级列表，不得为了版式虚构父子关系。步骤、优先级或时间顺序才使用有序列表。标题本身不重复加粗，不使用内联 HTML、字号或颜色。
环境存在“确定性数据查询结果”时，筛选、排序、分组、聚合、去重和行数限制必须服从该结果；不得从原始表格重新计算或跨行拼接。只有 validation.status=verified 的结果可以写成完整集合或全局排名。plan.presentation 只决定使用表格、图表、正文或指标卡表达，不得改变执行结果或强制把所有数据写成表格。
输出可直接放入当前步骤对应章节的 Markdown 正文，不输出 JSON、修改说明或思考过程。"""
            else:
                system = """你是 MemoryBread 的文档撰写 Agent。请依据目标、子 Agent 结论、Tool 证据和 Skill 规则，输出完整 Markdown 文档。
环境中存在“已激活的 Skill 步骤”时，必须按记录顺序消费每一步的 content，并把这些中间产物拼接成完整文档；不得跳过步骤、调换步骤，或只依据最后一次 Tool 结果重写全部内容。
章节设计 Agent 已给出章节蓝图时，以蓝图作为初稿骨架；信息缺乏支持时省略无法确认的内容，不能用套话把章节撑满。
任务画像含 `coverage_contract` 时，它是硬性覆盖合同：必须逐个覆盖 targets，并在每个目标内逐项回答 facets；不得只围绕其中一个高频目标展开，也不得用其他对象、上位业务或邻近口径的指标替代当前 facet。证据未覆盖某个单元格时，在对应目标下简洁写明“现有证据未覆盖”，不得把缺失项扩写成旁支业务章节。除必要的开头结论和结尾建议外，不新增与 targets 平级的其他业务场景。
环境中的“已准备的章节 Mermaid 图示”是章节级交付合同：逐项在 section_title 对应章节按 placement 插入一个 ```mermaid 代码块，图前用一句正文说明阅读方式，图后补充必要边界。节点、动作、状态和连线只能来自 source_points、正文或已有证据；starter 仅是语法骨架，不得把示例对象写入成稿。没有准备图示的章节不要为了版式自行配图。
对于已安装的技能，优先复刻 title_design_style 中的子标题句式、writing_design 中的行文推进、voice_style 中的惯用话术和 image_generation 中的代码生图方式；field_examples 只用于学习写法，不得照抄主题或事实。示例文档不会进入运行时事实环境。不要把这些鲜明特征稀释成通用公文。
除非用户要求或当前 Skill execution_steps 的目标/产出明确要求分析证据状态，否则不要输出“证据不足”“证据缺口”“证据完备”“待核验说明”等元说明。
环境存在“确定性数据查询结果”时，筛选、排序、分组、聚合、去重和行数限制必须逐字服从该结果；不得从原始表格重新计算或跨行拼接。只有 validation.status=verified 的结果可以表述为完整集合或全局排名，insufficient_coverage 只能描述已捕获范围。plan.presentation 只控制最终表达形式；auto 时根据当前文档语境选择正文、表格、图表或指标卡。
参考文档的 `refresh_status=fresh_complete` 表示本轮已校验当前原文；`fresh_recent` 表示节流窗口内复用近期完整校验，可继续支持当前事实；`fresh_partial` / `fresh_recent_partial` 只能支持已读取段落，不得声称已通读全文；`historical_only` 只能作历史背景，不得用来证明“当前/最新”事实。
要求：保留可验证事实；不编造政策编号、指标或来源；对外部信息给出链接；数据、文档、知识、操作和互联网线索是平权证据，不因所属模块获得额外优先级，按相关性、可靠性、时效和口径适配度取舍；“本周/今日”等相对时间只能使用环境给出的确定日期、年份和周次，禁止输出“第X周”等占位符；使用数据时写明统计周期和采集时间，`can_use=false` 或陈旧快照不得写成当前结论；数据来源名称、URL 与采集时间只能逐字取自同一条可用数据结果，不能根据相邻参考资料猜测或拼接，页面筛选日期是请求范围，指标实际统计周期以来源证据为准，不能冒充浏览器采集时间；无法确认归属时省略相关事实与“数据来源”行；缺失指标有 qualified 参考值时必须保留数值和风险标注；完全无来源数字时不编造，不得写“数据未明确区分”等占位值；页面交互、滚动或分页未验证完成时，只能说明“本次未完成采集”，不得改写为“看板未展示、不包含或不存在该字段”；环境包含 PlantUML 画图约束时必须输出对应的 ```plantuml 代码块，否则技术关系优先使用 Mermaid；只输出文档正文。"""
            if state.mode == "revision" and not step.get("skill_step_id"):
                intent = state.environment.get("edit_intent", {})
                targets = [str(item) for item in intent.get("target_sections", [])]
                target_hint = "、".join(targets) if targets else "由本轮要求推断的相关位置"
                system = f"""你是 MemoryBread 的文档修订 Agent。请基于现有完整文档输出修订后的完整 Markdown，不能只输出新增片段。
环境中存在“已激活的 Skill 步骤”时，必须按记录顺序消费每一步的 content，并把这些中间产物用于对应章节；不得跳过步骤、调换步骤，或只依据最后一次 Tool 结果覆盖已有有效内容。
对于已安装的技能，优先复刻 title_design_style 中的子标题句式、writing_design 中的行文推进、voice_style 中的惯用话术和 image_generation 中的代码生图方式；field_examples 只用于学习写法，不得照抄主题或事实。示例文档不会进入运行时事实环境。
除非用户要求或当前 Skill execution_steps 的目标/产出明确要求分析证据状态，否则不要新增“证据不足”“证据缺口”“证据完备”“待核验说明”等元说明。
参考文档中 `refresh_status=fresh_complete` 表示本轮完整校验，`fresh_recent` 表示节流窗口内复用近期完整校验，两者均可支持当前事实；`fresh_partial` / `fresh_recent_partial` 不得支撑全文结论；`historical_only` 不得用来证明“当前/最新”事实。
本轮已识别的改动线索：{target_hint}。这些只是线索，不是唯一可修改范围。
先判断新要求在全文中的合理位置和全部影响面，再执行修订：
1. 新内容必须放在语义与叙事顺序最合理的位置，不得机械追加到文末；
2. 若目录、摘要、章节编号、交叉引用、方案设计、实施计划、风险或验收条件受影响，必须联动更新；
3. 一轮可以新增、修改或删除多个章节；不要为了“局部更新”而忽略必要的跨章节修改；
4. 保留未受影响且仍有效的内容，避免无意义改写；
5. 本轮明确修改优先于冲突的原始约束，其余原始约束继续生效；
6. 保留可验证事实，不编造政策编号、指标或来源；外部结论保留链接；数据、文档、知识、操作和互联网线索按相关性、可靠性、时效和口径适配度平权取舍；数据结论写明统计周期和采集时间，`can_use=false` 或陈旧快照不得写成当前结论；来源名称、URL 与采集时间必须来自支持该数字的同一条数据结果，筛选日期不是采集时间，无法逐项匹配时省略相关事实与“数据来源”行。
只输出最终完整文档正文，不要输出 JSON 或修订说明；不要用代码围栏包裹整篇文档，但 Tool 要求的 PlantUML 或 Mermaid 图示代码块必须保留。"""
        elif step["action"] == "polisher":
            if agent_id == "document_unify_polisher":
                common = """请基于当前完整文档完成一次全文整合，并输出润色后的完整 Markdown 文档。
只处理全文结构、术语和表达一致性，保留全部事实、来源 URL、数据口径、代码块和用户明确要求。不得编造数字、案例、政策编号或来源；缺少支持的信息直接省略，除非用户或 Skill 明确要求，否则不要新增证据状态或待核验说明。不要输出 JSON、修改说明或思考过程，也不要用代码围栏包住整篇文档。"""
            else:
                common = """请基于当前完整文档做一次有边界的二次编辑，并输出润色后的完整 Markdown 文档。
只处理质检分派给你的问题，保留未受影响的章节、事实、来源 URL、数据口径、代码块和用户明确要求。不得编造数字、案例、政策编号或来源；缺少支持的信息直接省略，除非用户或 Skill 明确要求，否则不要新增证据状态或待核验说明。不要输出 JSON、修改说明或思考过程，也不要用代码围栏包住整篇文档。"""
            role_instructions = {
                "anti_ai_style_agent": """目标是提高中文表达的自然度和作者感，不以规避 AIGC 检测为目标。
删除空泛开场、重复小结、机械的“首先/其次/最后”和无增量的转折；普通概念不要为了强调而滥用引号，真实引语、字段名、代码和专有名词除外。把过长复句拆成自然短句，长短句交替；补出明确主语和动作，能用直接动词就不用“进行、实现、赋能”等名词化套话。优先贴合已安装 Skill 的 voice_style、用户历史表达和当前文档语域，但不得模仿特定在世作者。保持原意和事实强度，不把严谨内容改成网络口头禅。""",
                "detail_polish_agent": """逐章检查观点是否有完整的“对象/边界—依据—动作或机制—结果/验证”。只在已有用户材料、Tool 证据、数据分析和专业 Agent 结论支持的范围内补充细节；需要数据但当前环境没有可用结果时省略对应细节，不得补造数字或主动添加待核验说明。优先深挖质检列出的短章节、跳步推论和只写口号的段落，避免为了变长而重复同义句。""",
                "table_polish_agent": """修复不合法的 Markdown 表格；对确实需要逐项比较、职责映射、参数口径或验收矩阵的内容使用表格。表头要短而明确，同一列保持同一口径，单元格避免堆整段正文；复杂解释仍放在表格前后。只输出标准 Markdown 表格，不写内联 HTML/CSS。创作页面会自动为合法表头应用品牌背景色、边框、对齐和斑马纹。""",
                "typography_polish_agent": """Markdown `**重点**` 只表达语义上的强强调，不承担下划线或交互提示。普通自然段没有必要时可以完全不使用；同一组含两个以上较长、彼此独立的无序列表片段时，每项开头必须提炼一个能区分对象或主题的最短名词短语，写成 `- **短标签：** 事实、动作或结果`。只加粗短标签，以及读者必须先看到的关键判断、数字、风险或行动中的最短完整词组；不得加粗整句、整段、标题或列表正文，也不要用内联 HTML。""",
                "image_polish_agent": """只在组件关系、状态变化、跨角色流程或时间交互用文字难以准确理解时补充代码图示。环境存在章节 Visual Plan 时，只修复质检指出的缺失章节，并逐项服从 section_title、diagram_type、source_points、placement 和 max_nodes；不得把图统一追加到文末。没有 Visual Plan 时，环境有 PlantUML 约束则输出 `plantuml`，有 Mermaid 约束则输出 `mermaid`，均不存在时默认使用 `mermaid`。图中对象、连线和标签必须来自正文，先用一段正文说明阅读方式，图后补充异常或边界；不插入装饰图、占位图片或无法编辑的外链图片。""",
                "document_unify_polisher": """当前文档由多个 Skill 步骤独立推理的产物拼接而成。你的任务只做全文整合润色：统一术语、称谓、时态与数字口径；删除章节之间重复的过渡句、重复背景与相互矛盾的表述；保持 Skill 声明的二级章节标题、数量与顺序不变，不新增也不删除二级章节。
同时统一章节内部的 Markdown 表达：步骤目标中已经声明多个子主题时，用语义一致的三级标题分隔。同一组含两个以上较长、彼此独立的无序列表片段时，每项开头必须提炼一个能区分对象或主题的最短名词短语，写成 `- **短标签：** 事实、动作或结果`；只加粗短标签，不能把整句前半段当标签。短小枚举、连续解释因果或取舍的自然段不强制添加标签。同一章节有至少四项内容且现有事实能够直接支持稳定分类时，改成最多两级的父子列表；没有可靠分类时保留同级列表，不得虚构归属。除短标签外，只对关键判断、关键数字、风险和行动项的最短完整词组加粗，不加粗整句、整段或标题。
逐字保留事实、数字、统计周期、来源链接、表格和代码块；环境中 can_use=true 且 validation/verified_claims 已通过校验的指标必须写入对应章节，禁止将它们改成“待补充”“见原文”或任何占位内容；不得补造新事实，也不得删除任何实质性信息。""",
            }
            system = (
                f"你是 MemoryBread 的{step['name']}。\n"
                f"{common}\n{role_instructions.get(agent_id, '')}"
            )
        elif step["action"] == "skill_step":
            system = """你是 MemoryBread 的主创作 Agent，当前正在执行 Skill 明确声明的一个步骤。请严格完成当前步骤，不要调用或假设存在未声明的子 Agent，也不要提前撰写整篇文档。
已安装 Skill 的 execution_steps 是唯一流程和章节白名单。只生成当前步骤 title/objective/output 要求的内容，不得把其它步骤的数据、结论或表格合并进来；不得输出总标题或整篇文档。
workflow_role=support 的 Skill 只提供当前步骤所引用的能力，其 execution_steps 不是本轮流程，也不得据此增加章节。
除非当前步骤逐字要求，否则不得自行添加“结论”“重点进展”“风险/阻塞”“下周计划”等通用模板栏目，也不得用文档类型常见结构补齐 Skill 没有声明的内容。
章节结构和字体格式也是当前步骤的交付质量：外层二级标题会由 Harness 使用步骤 title 统一生成，不要重复输出。objective/output 明确包含多个子主题时，可使用与这些子主题语义一致的三级标题；不得新增未声明的通用栏目。
要求列表展示时，不要把多个长句无差别平铺。同一组含两个以上较长、彼此独立的无序列表片段时，每项开头必须提炼一个能区分对象或主题的最短名词短语，写成 `- **短标签：** 事实、动作或结果`；只加粗短标签，不能把整句前半段当标签。短小枚举不强制添加标签。当至少四项内容存在由当前证据直接支持的稳定分类时，使用最多两级的父子列表；无法确认分类时保留同级列表，不得为了版式虚构父子关系。步骤、优先级或时间顺序才使用有序列表。标题本身不重复加粗，不使用内联 HTML、字号或颜色。
当前步骤声明的 Tool 已由 Harness 在你开始处理前执行。objective 中“用 @某 Tool 获取”表示直接消费当前环境中的“Tool 执行回执”及对应结果，不是要求你再次调用 Tool；不得声称工具列表缺少接口、自己无法调用 Tool，或要求后续再调用已经执行完成的 Tool。
只使用当前环境中已有的 Tool 结果、上一步产出和用户材料，按照当前步骤的 objective 形成明确中间产物；预期产出为空时，根据步骤标题和目标给出最适合后续拼接的结构。
环境存在“确定性数据查询结果”时，它是筛选、排序、分组、聚合、去重和行数限制的唯一依据：validation.status=verified 才能把结果写成完整确定结论；不得绕过该结果重新从原始表格计算。insufficient_coverage 只能支持已捕获范围内的观察，不得写成全局排名或完整集合。plan.presentation 只控制最终表达形式；没有表格要求时可正常输出正文、图表或指标卡，不得为了使用查询结果强制生成表格。
结果必须可直接交给下一个 Skill 步骤或最终文档撰写 Agent：保留有依据的事实、数字、来源和时间口径，不得把不同来源的名称、时间与数值混拼，不得补造信息。
本地参考中的 `period_evidence` 只依据正文逐字出现的完整日期：`match_status=matched` 表示正文事件日期明确落在请求周期内，可以用于该周期；`observed_at` 和 `refresh_collected_at` 只是记录/刷新时间，不得用它们否定正文日期。`match_status=unknown` 仅表示未提取到完整日期，不等于正文不属于该周期。
“本周/今日”等相对时间必须逐字服从环境中的当前确定时间；禁止输出“第X周”等占位符。缺失的指标或进展直接省略，不得写“数据未明确区分”“暂无明确进展”等占位内容。
除非用户要求或当前 Skill 步骤的 objective/output 明确要求分析证据状态，否则不要输出“证据不足”“证据缺口”“证据完备”“待核验说明”等元说明；结果无法支持某项事实时，直接省略该事实，只保留有依据的内容。
只输出本步骤产出正文，不输出思考过程、JSON、完整成稿或与本步骤无关的章节。"""
        else:
            role_instructions = {
                "data_analysis_agent": "优先使用网页实时采集后且已通过 AX 或 DOM 结构化校验的数据；截图与 OCR 只用于补充留证，不得作为结构化网页数据可用性的唯一门槛。其次使用数据检索中 can_use=true 的工作记忆。任务画像含 coverage_contract 时，只保留能够归属于某个 target 且直接回答某个 facet 的事实，按目标与维度组织；其他对象、上位业务或邻近口径的指标不得替代当前 facet。目标列出多个指标时逐项消费已校验成功的值：可用几项就展示几项，不因其他指标缺失拒绝整个来源，也不为缺失项生成占位行。需要趋势、环比或历史比较时，必须读取同一结果的 history，并按 period_key/period_start_at/period_end_at 对齐阶段；同一自然周内的数据视为一个阶段，不同阶段不得覆盖或混写。每个数字都要与同一结果中的 source_id、title、source_url、collected_at/observed_at 绑定；页面筛选日期是请求范围，指标实际统计周期以来源证据为准，不是采集时间。不同来源、周期或口径不得擅自拼接。工作记忆只能按 observed_at 加权，陈旧数据必须标注。禁止编造数字或来源，只输出有支持的‘结论—指标—统计阶段—采集时间—来源’，qualified 参考值必须附带实际周期、目标周期与风险说明，不能省略披露。",
                "industry_research_agent": "综合互联网检索结果，只提炼有来源支持的行业现状、趋势与约束，每条外部结论保留来源 URL；省略无法确认的事实，qualified 参考值必须附带实际周期、目标周期与风险说明，不能省略披露。",
                "solution_design_agent": "围绕目标、约束和证据设计可落地方案，明确边界、关键决策、组件关系、实施步骤、风险和验证方式。",
                "chapter_design_agent": """先设计章节，再交给文档撰写 Agent。结合目标、读者、文档类型、证据和 Skill，输出有顺序的章节蓝图；每章写明目的、要回答的问题、可用证据、建议表达形式和完成标准。章节必须互斥且共同覆盖目标，不写正文，不补造事实。任务画像含 coverage_contract 时，按 targets 建立主体章节，并在每个目标内逐项覆盖 facets；不得把邻近指标或其他业务场景提升为平级主体章节。
同时对每章做通用的关系表达判断：只有当已有信息包含多个对象之间的依赖、步骤与分支、跨角色时间交互、状态变化或实体关系，并且图比连续文字更容易准确理解时，才加入 Visual Plan；背景、目标、原则、孤立清单和证据不足的章节不配图。判断依据是内容结构，不是文档名称或行业关键词。
只输出一个 JSON 对象，格式为 {"blueprint_markdown":"章节蓝图 Markdown","visual_plan":{"schema_version":"creation.visual-plan.v1","policy":"auto","max_diagrams":4,"diagrams":[{"id":"稳定英文或数字标识","section_title":"与蓝图完全一致的章节标题","purpose":"图要帮助读者理解什么","diagram_type":"flowchart|flowchart_lr|sequence|state|class|er|journey|gantt|mindmap","required":true,"reason":"为什么文字不足以表达","source_points":["允许画入的对象、动作、状态或关系短句"],"placement":"after_intro|before_details|after_details","max_nodes":12}]}}。diagrams 可为空；通常一章最多一图，最多八图。""",
            }
            system = f"你是 MemoryBread 的{step['name']}。{role_instructions.get(agent_id, '完成当前专业分析。')}"
        structure_requirements = step.get("skill_step_structure_requirements")
        if isinstance(structure_requirements, dict) and (
            structure_requirements.get("minimum_subsections")
            or structure_requirements.get("minimum_subsection_chars")
        ):
            minimum_count = structure_requirements.get("minimum_subsections")
            minimum_chars = structure_requirements.get("minimum_subsection_chars")
            constraints: list[str] = []
            if minimum_count:
                constraints.append(f"至少 {minimum_count} 个三级或更深子章节")
            if minimum_chars:
                constraints.append(f"每个子章节正文不少于 {minimum_chars} 字")
            system += (
                "\n当前执行动作包含可量化的章节要求："
                f"{'、'.join(constraints)}。这些要求是当前动作正文的一部分，必须直接落实；"
                "子章节标题应对应动作要求的真实对象或主题，不得用无关通用栏目凑数。"
            )
        workflow_context = ""
        if step.get("skill_step_id"):
            workflow_context = f"""【当前 Skill 执行步骤】
步骤：{step.get("skill_step_title", "")}
目标：{step.get("skill_step_objective", "")}
预期产出：{step.get("skill_step_output", "")}
可协同 Skill：{"、".join(step.get("skill_step_skills", [])) or "无"}
从执行动作提取的结构要求：{structure_requirements}

"""
        user = f"""{workflow_context}【目标】
{state.goal.objective}

【原始需求（基线；与本轮明确修改冲突时以本轮为准）】
{state.root_request}

【用户本轮要求】
{state.user_message}

【当前环境】
{environment}
"""
        system += "\n数据风险披露规则优先于省略缺失项或禁止元说明的通用写作规则：当 can_use=true 且 data_usage_status=qualified 时，必须使用来源提供的原始参考值补齐对应指标，并在数据下方标明实际统计周期、请求周期、来源与 data_risks 风险；不能把参考值写成目标周期实绩。编辑、整合与润色必须保留数值及风险标注。指标语义未匹配时只保留原名供参考，不能擅自改名；完全无来源数值、鉴权失败或视图未验证时不得编造或绕过校验。"
        return system, user

    @staticmethod
    def _step_focus_query(step: dict[str, Any]) -> str:
        """步骤自身主题文本，不含根请求背景。

        核心实体识别必须只用这段文本：根请求可能包含其他章节的主题
        （如 GPU 成本章节），若参与实体识别会通过“整体创作背景”劫持
        当前步骤的层级排序。
        """
        objective = str(step.get("skill_step_objective") or "").strip()
        output = str(step.get("skill_step_output") or "").strip()
        step_title = str(step.get("skill_step_title") or "").strip()
        skills = [
            str(item).strip()
            for item in step.get("skill_step_skills", [])
            if str(item).strip()
        ]
        return "\n".join(
            item
            for item in (
                f"当前步骤：{step_title}" if step_title else "",
                objective,
                f"需要产出：{output}" if output else "",
                f"协同 Skill：{'、'.join(skills)}" if skills else "",
            )
            if item
        )

    @classmethod
    def _step_context_query(cls, state: LoopState, step: dict[str, Any]) -> str:
        step_id = str(step.get("id") or "")
        query_key = (
            "retrieval_query" if step_id == MEMORY_SEARCH_TOOL_ID else "context_query"
        )
        context_query = str(
            state.environment.get(query_key) or state.user_message
        ).strip()
        step_specific_query = cls._step_focus_query(step)
        if step_id == "data_query_planner" and step_specific_query:
            return step_specific_query
        if (
            step.get("skill_step_id")
            and step_id in {MEMORY_SEARCH_TOOL_ID, DATA_SEARCH_TOOL_ID}
            and step_specific_query
        ):
            # Tool 的首要检索对象来自 execution_steps，而非“使用 @某 Skill”
            # 这类根请求包装。记忆检索可把根请求放到末尾补充语境；数据检索
            # 必须完全隔离其他步骤主题，避免报表 Top-K 再次被周报名称稀释。
            context_query = (
                "\n".join(
                    [step_specific_query, f"整体创作背景：{context_query}"]
                )
                if step_id == MEMORY_SEARCH_TOOL_ID
                else step_specific_query
            )
        if step_id == DATA_SEARCH_TOOL_ID:
            # Skill 内的数据检索必须服从当前步骤自己的目标。若把整篇创作请求
            # 混在检索词最前面，周报名称和其他步骤主题会稀释明确的数据对象，
            # 使 Skill 指定的数据反而掉出 Top-K。
            # data_search 位于 memory_search 之后时，优先带上已经命中的报表标题。
            # 这样“GPU 利用率治理”既能召回旧资料，也能把其中引用的运营看板
            # 解析成需要即时刷新的数据源。
            report_titles = []
            for reference in state.environment.get("references", []):
                if not isinstance(reference, dict):
                    continue
                title = str(reference.get("title") or "").strip()
                url = str(reference.get("source_url") or "").strip()
                evidence = f"{title}\n{url}".lower()
                if not any(
                    marker in evidence
                    for marker in (
                        "看板",
                        "报表",
                        "dashboard",
                        "report",
                        "analytics",
                        "grafana",
                        "tableau",
                        "powerbi",
                    )
                ):
                    continue
                if title:
                    report_titles.append(title)
                if url:
                    report_titles.append(url)
                if len(report_titles) >= 4:
                    break
            if report_titles:
                context_query = "\n".join([*report_titles, context_query])
            if step.get("skill_step_id"):
                return context_query
        if step_id == MEMORY_SEARCH_TOOL_ID and step.get("skill_step_id"):
            return context_query
        if not step_specific_query:
            return context_query
        return "\n".join(
            item
            for item in (context_query, step_specific_query)
            if item
        )

    @staticmethod
    def _apply_data_freshness_to_references(
        state: LoopState,
        data_results: list[dict[str, Any]],
    ) -> None:
        """阻止报表型旧文档在刷新失败后继续冒充当前数据。"""
        by_url = {
            str(item.get("source_url") or "").strip(): item
            for item in data_results
            if isinstance(item, dict) and str(item.get("source_url") or "").strip()
        }
        if not by_url:
            return
        for reference in state.environment.get("references", []):
            if not isinstance(reference, dict):
                continue
            source_url = str(reference.get("source_url") or "").strip()
            result = by_url.get(source_url)
            if not result:
                continue
            reference["data_freshness"] = {
                "freshness_class": result.get("freshness_class"),
                "collected_at": result.get("collected_at"),
                "refresh_required": result.get("refresh_required"),
                "can_use": result.get("can_use"),
                "evidence_verified": (
                    isinstance(result.get("creation_evidence"), dict)
                    and result["creation_evidence"].get("validation_status") == "verified"
                ),
            }
            evidence_verified = (
                isinstance(result.get("creation_evidence"), dict)
                and result["creation_evidence"].get("validation_status") == "verified"
            )
            if result.get("can_use") is True and evidence_verified:
                if result.get("risk_disclosure_required"):
                    reference["data_use_policy"] = "qualified_snapshot_available"
                    reference["data_freshness"]["risk_disclosure_required"] = True
                    reference["content"] = ""
                    reference["summary"] = "该来源已取得带风险标注的参考数据；只能使用当前数据结果中的数值、实际周期与风险说明，不从旧文档补充当前数值。"
                else:
                    reference["data_use_policy"] = "current_snapshot_available"
                continue
            reference["content"] = ""
            reference["summary"] = (
                "该引用指向需要即时刷新的报表；刷新成功前只能作为来源线索，"
                "其中的历史数值不得写成当前数据。"
            )
            reference["data_use_policy"] = "current_values_unavailable"

    @staticmethod
    def _enforce_report_evidence_policy(data_results: list[dict[str, Any]]) -> None:
        """未通过本轮 AX/DOM 结构校验的报表不向写作 Agent 暴露数值。"""
        for result in data_results:
            if not isinstance(result, dict) or result.get("source_kind") != "report_url":
                continue
            evidence = result.get("creation_evidence")
            verified = (
                isinstance(evidence, dict)
                and evidence.get("validation_status") == "verified"
            )
            if verified:
                validation = evidence.get("validation") or {}
                original_structured = result.get("structured_data")
                claims = [
                    claim
                    for claim in validation.get("verified_claims", [])
                    if isinstance(claim, dict)
                ]
                result["content_excerpt"] = "\n".join(
                    str(claim.get("statement") or "").strip()
                    for claim in claims
                    if str(claim.get("statement") or "").strip()
                )
                verified_structured = {
                    "validation": validation.get("reason") or "programmatic_verified",
                    "primary_channel": validation.get("primary_channel"),
                    "verified_claims": claims,
                    "data_usage_status": validation.get("data_usage_status", "verified"),
                    "risk_disclosure_required": bool(validation.get("risk_disclosure_required")),
                }
                result["data_usage_status"] = validation.get("data_usage_status", "verified")
                result["risk_disclosure_required"] = bool(validation.get("risk_disclosure_required"))
                result["data_risks"] = validation.get("data_risks", [])
                # 页面证据已通过校验时，保留同一次采集产生的关系表及覆盖率
                # 元数据。QueryPlan 需要完整行边界执行排序、分组和 Top-N；
                # 不能把表格压扁成若干独立 claim。这里只按通用结构键保留，
                # 不识别任何报表、字段名或业务维度。
                if isinstance(original_structured, dict):
                    for key in (
                        "tables",
                        "pagination",
                        "completeness",
                        "summary_metrics",
                    ):
                        value = original_structured.get(key)
                        if value is not None:
                            verified_structured[key] = value
                result["structured_data"] = verified_structured
                result["provenance"] = {
                    "creation_evidence_id": evidence.get("id"),
                    "captured_at": evidence.get("captured_at") or result.get("collected_at"),
                    "source_url": evidence.get("source_url") or result.get("source_url"),
                    "evidence_kind": evidence.get("evidence_kind") or "webpage_screenshot",
                }
                continue
            result["can_use"] = False
            result["content_excerpt"] = None
            result["structured_data"] = None
            result["provenance"] = None

    @classmethod
    def _apply_data_risk_disclosures(
        cls, document: str, data_results: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """在模型整合/润色后确定性补齐参考数据和风险，不依赖模型自行披露。"""
        if not document.strip() or not data_results:
            return document, []
        def source_key(result: dict) -> str:
            evidence = result.get("creation_evidence") or {}
            identity = repr((result.get("source_id"), result.get("source_url") or evidence.get("source_url")))
            return hashlib.sha256(identity.encode()).hexdigest()[:16]

        current_keys = {source_key(result) for result in data_results if isinstance(result, dict) and result.get("can_use") is True}
        removed = set()
        def remove_current(match: re.Match) -> str:
            if match.group(1) not in current_keys:
                return match.group(0)
            removed.add(match.group(1))
            return ""
        # 重复渲染或修订时重建本程序的说明，不触碰用户自行写的备注。
        document = re.sub(
            r"\n*<!-- memorybread:data-risks:([a-f0-9]+) -->.*?<!-- /memorybread:data-risks -->",
            remove_current, document, flags=re.DOTALL,
        )

        def cell(value: Any) -> str:
            return " ".join(str(value or "").split()).replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")

        audit = []
        insertions: dict[int, list[str]] = {}
        seen = set()
        spans = cls._markdown_section_spans(document)
        for result in data_results:
            if not isinstance(result, dict) or result.get("can_use") is not True:
                continue
            evidence = result.get("creation_evidence") or {}
            validation = evidence.get("validation") or {}
            risks = result.get("data_risks") or validation.get("data_risks") or []
            risks = [risk for risk in risks if isinstance(risk, dict) and str(risk.get("value") or "").strip()]
            if not risks:
                continue
            # 概念偏好没有字段命中时，仅披露正文实际采用的原名事实；不把
            # 任意相关数字强行补入请求字段。同指标跨周期的参考值则完整列出。
            risks = [risk for risk in risks if risk.get("kind") != "semantic_match_unverified"
                     or (str(risk.get("label") or "") in document and str(risk.get("value")) in document)]
            if not risks:
                continue
            source_url = str(result.get("source_url") or evidence.get("source_url") or "")
            source_title = cell(result.get("title") or evidence.get("page_title") or "数据来源")
            key = source_key(result)
            if key in seen:
                continue
            seen.add(key)
            rows = []
            for risk in risks:
                expected = risk.get("expected_period") or {}
                requested_period = " 至 ".join(str(expected.get(field) or "") for field in ("start", "end")).strip(" 至") or "未指定"
                rows.append("| " + " | ".join(cell(value) for value in (
                    risk.get("label"), risk.get("value"), risk.get("actual_period") or "未明确",
                    requested_period, risk.get("note"),
                )) + " |")
            source = source_title
            if source_url.startswith(("https://", "http://")):
                safe_url = source_url.replace("(", "%28").replace(")", "%29").replace(" ", "%20").replace("\n", "").replace("\r", "")
                source = f"[{source_title.replace('[', '').replace(']', '')}]({safe_url})"
            block = "\n\n".join((
                f"<!-- memorybread:data-risks:{key} -->",
                "**数据风险说明（参考值）**",
                "以下为有来源支持、但周期或口径尚不完全符合本次请求的参考数值；不代表目标周期实绩，请核验后使用。",
                "\n".join(["| 来源指标 | 参考值 | 实际统计周期 | 请求周期 | 风险说明 |", "| --- | --- | --- | --- | --- |", *rows]),
                f"来源：{source}",
                "<!-- /memorybread:data-risks -->",
            ))
            target = str(result.get("target_section") or "")
            span = cls._find_section_span(target, spans) if target else None
            if span is None and spans:
                ranked = []
                for candidate in spans:
                    section = document[int(candidate["start"]):int(candidate["end"])]
                    score = sum(str(risk.get("label") or "") in section for risk in risks)
                    ranked.append((score, -len(section), candidate))
                score, _, best = max(ranked, key=lambda entry: entry[:2])
                if score:
                    span = best
            offset = int(span["end"]) if span else len(document)
            insertions.setdefault(offset, []).append(block)
            audit.append({"source_id": result.get("source_id"), "risk_count": len(risks), "data_risks": risks})
        for offset in sorted(insertions, reverse=True):
            document = document[:offset].rstrip() + "\n\n" + "\n\n".join(insertions[offset]) + "\n\n" + document[offset:].lstrip()
        audit.extend({"source_key": key, "risk_count": 0, "status": "resolved"} for key in removed - seen)
        return document.rstrip(), audit

    @classmethod
    def _guard_data_citations(
        cls,
        document: str,
        data_results: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """用可用证据重建数据引用，阻止 Writer 自由拼接来源与采集时间。"""
        if not document.strip() or not data_results:
            return document, []
        sources = cls._citation_sources(data_results)
        citation_line = re.compile(
            r"(?m)^\s*(?:\*|_)?\s*数据来源\s*[:：].*?(?:\*|_)?\s*$"
        )
        blocks = re.split(r"(\n\s*\n)", document)
        audit: list[dict[str, Any]] = []
        for index in range(0, len(blocks), 2):
            block = blocks[index]
            matches = list(citation_line.finditer(block))
            if not matches:
                continue
            local_context = citation_line.sub("", block).strip()
            if not local_context:
                previous_index = index - 2
                while previous_index >= 0 and not blocks[previous_index].strip():
                    previous_index -= 2
                local_context = blocks[previous_index] if previous_index >= 0 else ""
                heading_index = previous_index - 2
                while heading_index >= 0 and previous_index - heading_index <= 6:
                    heading = blocks[heading_index].strip()
                    if heading.startswith("#"):
                        local_context = f"{heading}\n{local_context}"
                        break
                    heading_index -= 2
            claim_values = cls._extract_numeric_claim_values(local_context)
            if not claim_values:
                continue
            ranked = sorted(
                (
                    (cls._citation_source_score(local_context, claim_values, source), source)
                    for source in sources
                ),
                key=lambda item: item[0],
                reverse=True,
            )
            best_score, best_source = ranked[0] if ranked else (0.0, None)
            original_line = matches[0].group(0).strip()
            if best_source is not None and best_score >= 0.82:
                replacement = cls._format_data_citation(best_source)
                status = "corrected"
                source_id = best_source.get("source_id")
            else:
                replacement = ""
                status = "unsupported"
                source_id = None
            blocks[index] = citation_line.sub(replacement, block)
            audit.append(
                {
                    "status": status,
                    "source_id": source_id,
                    "claim_values": sorted(claim_values),
                    "original": original_line,
                    "replacement": replacement,
                }
            )
        return "".join(blocks), audit

    @classmethod
    def _citation_sources(
        cls,
        data_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for result in data_results:
            if result.get("can_use") is not True:
                continue
            source_kind = str(result.get("source_kind") or "")
            evidence = result.get("creation_evidence")
            if source_kind == "report_url":
                if (
                    not isinstance(evidence, dict)
                    or evidence.get("validation_status") != "verified"
                ):
                    continue
                validation = evidence.get("validation") or {}
                claims = [
                    claim
                    for claim in validation.get("verified_claims", [])
                    if isinstance(claim, dict)
                ]
                support_text = "\n".join(
                    str(claim.get("statement") or "") for claim in claims
                )
                periods = sorted(
                    {
                        str(claim.get("statistical_period") or "").strip()
                        for claim in claims
                        if str(claim.get("statistical_period") or "").strip()
                    }
                )
                collected_at = evidence.get("captured_at") or result.get("collected_at")
                title = evidence.get("page_title") or result.get("title")
                source_url = evidence.get("source_url") or result.get("source_url")
                evidence_verified = True
            else:
                support_text = "\n".join(
                    (
                        str(result.get("content_excerpt") or ""),
                        str(result.get("structured_data") or ""),
                    )
                )
                periods = []
                collected_at = result.get("collected_at") or result.get("observed_at")
                title = result.get("title")
                source_url = result.get("source_url")
                evidence_verified = False
            values = cls._extract_numeric_claim_values(support_text)
            if not values:
                continue
            sources.append(
                {
                    "source_id": result.get("source_id"),
                    "source_kind": source_kind,
                    "title": str(title or "数据来源").strip(),
                    "source_url": str(source_url or "").strip(),
                    "collected_at": int(collected_at or 0),
                    "statistical_periods": periods,
                    "support_text": support_text,
                    "values": values,
                    "evidence_verified": evidence_verified,
                }
            )
        return sources

    @staticmethod
    def _extract_numeric_claim_values(value: str) -> set[str]:
        # Markdown 标题编号属于文档结构，不是被引用的数据值。
        value = "\n".join(
            line for line in value.splitlines() if not line.lstrip().startswith("#")
        )
        without_dates = re.sub(
            r"\b20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?\b",
            " ",
            value,
        )
        values: set[str] = set()
        for match in re.findall(
            r"(?<![\w])([+-]?\d[\d,]*(?:\.\d+)?(?:%|亿|万|千|百|卡|个|次|元|秒|ms|s)?)",
            without_dates,
            flags=re.IGNORECASE,
        ):
            normalized = match.replace(",", "").lower().strip()
            if normalized in {"0", "0.0", "0%", "0.0%"}:
                continue
            values.add(normalized)
        return values

    @classmethod
    def _citation_source_score(
        cls,
        context: str,
        claim_values: set[str],
        source: dict[str, Any],
    ) -> float:
        source_values = set(source.get("values") or set())
        coverage = len(claim_values & source_values) / max(1, len(claim_values))
        if coverage < 0.8:
            return coverage
        context_tokens = set(cls._evidence_match_tokens(context))
        title_tokens = set(cls._evidence_match_tokens(str(source.get("title") or "")))
        title_matches = bool(context_tokens & title_tokens)
        evidence_bonus = 0.10 if source.get("evidence_verified") else 0.0
        title_bonus = 0.10 if title_matches else 0.0
        indirect_penalty = 0.20 if not source.get("evidence_verified") and not title_matches else 0.0
        return min(
            1.0,
            coverage + title_bonus + evidence_bonus - indirect_penalty,
        )

    @staticmethod
    def _format_data_citation(source: dict[str, Any]) -> str:
        title = str(source.get("title") or "数据来源").replace("|", "｜").replace("]", "）")
        source_url = str(source.get("source_url") or "").strip()
        source_label = f"[{title}](<{source_url}>)" if source_url else title
        parts = [f"数据来源：{source_label}"]
        periods = [
            str(item).strip()
            for item in source.get("statistical_periods", [])
            if str(item).strip()
        ]
        if periods:
            parts.append(f"统计周期：{'、'.join(periods[:3])}")
        collected_at = int(source.get("collected_at") or 0)
        if collected_at > 0:
            collected_label = datetime.fromtimestamp(collected_at / 1000).astimezone().strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            )
            time_name = (
                "浏览器采集时间"
                if source.get("source_kind") == "report_url"
                else "工作记忆采集时间"
            )
            parts.append(f"{time_name}：{collected_label}")
        return f"*{'；'.join(parts)}*"

    @classmethod
    def _apply_creation_evidence_cards(
        cls,
        document: str,
        evidence_items: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        if not document.strip() or not evidence_items:
            return document, []
        blocks = re.split(r"(\n\s*\n)", document)
        applied: list[dict[str, Any]] = []
        for evidence in evidence_items:
            if evidence.get("validation_status") != "verified":
                continue
            original_image_url = str(evidence.get("image_url") or "").strip()
            image_url = str(
                evidence.get("display_image_url") or original_image_url
            ).strip()
            if not image_url or image_url in document or original_image_url in document:
                continue
            validation = evidence.get("validation") or {}
            claims = validation.get("verified_claims") or []
            current_document = "".join(blocks)
            target_section = str(
                evidence.get("target_section")
                or evidence.get("skill_step_title")
                or ""
            ).strip()
            candidate_indices = list(range(0, len(blocks), 2))
            if target_section:
                section_spans = [
                    span
                    for span in cls._markdown_section_spans(current_document)
                    if int(span.get("level") or 0) == 2
                ]
                target_span = cls._find_section_span(target_section, section_spans)
                if not target_span:
                    # 带步骤归属的证据不能退化为全篇匹配，否则会再次被同名词或
                    # 短数字吸附到其它章节。
                    continue
                block_starts: list[int] = []
                cursor = 0
                for block in blocks:
                    block_starts.append(cursor)
                    cursor += len(block)
                target_start = int(target_span["start"])
                target_end = int(target_span["end"])
                candidate_indices = [
                    index
                    for index in candidate_indices
                    if block_starts[index] < target_end
                    and block_starts[index] + len(blocks[index]) > target_start
                ]
            matched_index: Optional[int] = None
            for index in candidate_indices:
                block = blocks[index]
                normalized_block = cls._normalize_evidence_match_text(block)
                for claim in claims:
                    if not isinstance(claim, dict):
                        continue
                    if claim.get("claim_type") == "text":
                        statement = str(claim.get("statement") or "")
                        normalized_statement = cls._normalize_evidence_match_text(statement)
                        tokens = cls._evidence_match_tokens(statement)
                        matched_tokens = [
                            token
                            for token in tokens
                            if cls._normalize_evidence_match_text(token) in normalized_block
                        ]
                        if (
                            normalized_statement
                            and normalized_statement in normalized_block
                        ) or (
                            len(matched_tokens) >= 2
                            and len(matched_tokens) / max(1, len(tokens)) >= 0.6
                        ):
                            matched_index = index
                            break
                        continue
                    value = cls._normalize_evidence_match_text(str(claim.get("value") or ""))
                    if not target_section and len(value) < 2:
                        # 无章节归属的旧证据仍可使用正文兜底，但单字符值（例如
                        # “2”）没有足够区分度，极易命中年份、版本号或列表序号。
                        continue
                    labels = cls._evidence_match_tokens(str(claim.get("label") or ""))
                    label_match = any(
                        cls._normalize_evidence_match_text(label) in normalized_block
                        for label in labels
                    )
                    if value and value in normalized_block and label_match:
                        matched_index = index
                        break
                if matched_index is not None:
                    break
            if matched_index is None and target_section and candidate_indices:
                # 步骤归属比 OCR 文本相似度更可靠。若 Writer 没有逐字采用截图
                # 中的指标，仍把已验证截图留在所属章节末尾，而不是跨章节猜测。
                matched_index = candidate_indices[-1]
            if matched_index is None:
                continue
            title = str(evidence.get("page_title") or "即时数据页面").replace("]", "）")
            source_url = str(evidence.get("source_url") or "").strip()
            captured_at = int(evidence.get("captured_at") or 0)
            captured_label = (
                datetime.fromtimestamp(captured_at / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                if captured_at > 0
                else "创作时"
            )
            source_markdown = f"[{title}](<{source_url}>)" if source_url else title
            original_link = (
                f" · [查看原始全图](<{original_image_url}>)"
                if original_image_url and image_url != original_image_url
                else ""
            )
            card = (
                f"\n\n![证据截图：{title}]({image_url})\n\n"
                f"> 证据截图 · 来源：{source_markdown} · 采集于 {captured_label} · "
                f"页面数据与截图文字已通过一致性校验{original_link}"
            )
            blocks[matched_index] = f"{blocks[matched_index].rstrip()}{card}"
            applied.append(evidence)
        return "".join(blocks), applied

    @staticmethod
    def _scope_creation_evidence(
        evidence: dict[str, Any],
        step: dict[str, Any],
    ) -> dict[str, Any]:
        scoped = dict(evidence)
        step_title = str(step.get("skill_step_title") or "").strip()
        if step.get("skill_id"):
            scoped["skill_id"] = step.get("skill_id")
        if step.get("skill_step_id"):
            scoped["skill_step_id"] = step.get("skill_step_id")
        if step_title:
            scoped["skill_step_title"] = step_title
            scoped["target_section"] = step_title
        return scoped

    @staticmethod
    def _normalize_evidence_match_text(value: str) -> str:
        return re.sub(r"[\s,，:：;；|｜]", "", value).lower()

    @staticmethod
    def _evidence_match_tokens(value: str) -> list[str]:
        tokens: list[str] = []
        for english in re.findall(r"[a-zA-Z]{2,}", value):
            lowered = english.lower()
            if lowered not in tokens:
                tokens.append(lowered)
        for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", value):
            chars = list(sequence)
            for index in range(max(1, len(chars) - 1)):
                token = "".join(chars[index : index + 2])
                if len(token) == 2 and token not in tokens:
                    tokens.append(token)
        return tokens[:24]

    @classmethod
    def _compact_prompt_value(cls, value: Any, depth: int = 0) -> Any:
        """把运行时完整对象转为有界的模型事实视图。

        完整 DOM、截图区域、滚动与交互调试状态保留在环境/数据库中，
        但不属于 Agent 需要消费的事实。这里只按通用数据形态裁剪，
        不感知看板名、业务字段或具体指标。
        """
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            text = " ".join(value.split()).strip()
            return text if len(text) <= 600 else text[:600].rstrip() + "..."
        if isinstance(value, dict):
            if depth >= 4:
                return "[nested object omitted]"
            omitted_keys = {
                "dom_content_text",
                "raw_html",
                "html",
                "evidence_regions",
                "scroll_capture",
                "interaction",
                "page_state",
                "browser_script",
                "screenshot",
            }
            compacted: dict[str, Any] = {}
            for key, item in list(value.items())[:32]:
                key_text = str(key)
                if key_text.lower() in omitted_keys:
                    continue
                compacted[key_text] = cls._compact_prompt_value(item, depth + 1)
            return compacted
        if isinstance(value, (list, tuple)):
            if depth >= 4:
                return ["[nested items omitted]"]
            return [
                cls._compact_prompt_value(item, depth + 1)
                for item in list(value)[:24]
            ]
        return cls._compact_prompt_value(str(value), depth + 1)

    @classmethod
    def _prompt_data_results(cls, results: Any) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        used_chars = 2
        indexed_results = [
            (index, raw)
            for index, raw in enumerate(list(results or []))
            if isinstance(raw, dict)
        ]

        def result_rank(entry: tuple[int, dict[str, Any]]) -> tuple[int, int]:
            index, raw = entry
            evidence = raw.get("creation_evidence")
            verified_report = (
                raw.get("source_kind") == "report_url"
                and raw.get("can_use") is True
                and isinstance(evidence, dict)
                and evidence.get("validation_status") == "verified"
            )
            # 全文整合会同时消费多个数据步骤。若继续沿用第一次检索的
            # 原始顺序，前一张报表及其工作记忆会占满 Prompt，后一张已经
            # 验证成功的报表只能被模型写成“待补充”。
            return (0 if verified_report else 1, index)

        for _, raw in sorted(indexed_results, key=result_rank)[:30]:
            item = {
                key: raw.get(key)
                for key in (
                    "source_id",
                    "title",
                    "source_kind",
                    "source_url",
                    "observed_at",
                    "collected_at",
                    "freshness_class",
                    "freshness_score",
                    "refresh_required",
                    "can_use",
                    "evidence_status",
                    "evidence_reason",
                    "unavailable_reason",
                    "data_usage_status",
                    "risk_disclosure_required",
                )
                if raw.get(key) is not None
            }
            # 不可用的报表只向 Agent 暴露来源身份与动作状态。
            # 可用结果才带有界的事实、来源和少量历史阶段。
            if raw.get("can_use") is True:
                excerpt = str(raw.get("content_excerpt") or "").strip()
                if excerpt:
                    item["content_excerpt"] = cls._compact_prompt_value(excerpt)
                if raw.get("structured_data") is not None:
                    structured_data = raw.get("structured_data")
                    compact_structured = cls._compact_prompt_value(structured_data)
                    if (
                        raw.get("source_kind") == "report_url"
                        and isinstance(structured_data, dict)
                        and isinstance(compact_structured, dict)
                        and isinstance(structured_data.get("verified_claims"), list)
                    ):
                        # 为每个实时来源保留公平预算。KPI 已由采集层排序，
                        # 定向字段匹配时保留完整请求集；只有概念偏好而没有
                        # 字段命中时只暴露前四个汇总 KPI，避免 Writer 把项目
                        # 明细二次推导成未经页面支持的新指标。
                        validation_reason = str(
                            structured_data.get("validation") or ""
                        )
                        claim_limit = (
                            12
                            if validation_reason
                            in {"requested_metrics_verified", "requested_metrics_partial", "requested_metrics_qualified"}
                            else 4
                        )
                        compact_structured["verified_claims"] = [
                            cls._compact_prompt_value(claim)
                            for claim in structured_data["verified_claims"][:claim_limit]
                            if isinstance(claim, dict)
                        ]
                    item["structured_data"] = compact_structured
                if raw.get("provenance") is not None:
                    item["provenance"] = cls._compact_prompt_value(
                        raw.get("provenance")
                    )
                history = raw.get("history")
                if isinstance(history, list) and history:
                    item["history"] = cls._compact_prompt_value(history[:3])
            candidate_size = len(str(item))
            if compacted and used_chars + candidate_size > MAX_PROMPT_DATA_RESULTS_CHARS:
                break
            compacted.append(item)
            used_chars += candidate_size
        return compacted

    @staticmethod
    def _reference_identity(raw: dict[str, Any]) -> str:
        source_id = raw.get("source_id")
        if source_id is None:
            source_id = raw.get("id")
        return f"{raw.get('source_type') or 'document'}:{source_id}"

    @classmethod
    def _scope_references_for_step(
        cls,
        references: Any,
        step: Optional[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set]:
        """当前 Skill 步骤自己召回的参考必须排在写作 Prompt 最前面。

        合并列表按首次出现顺序保留，上一步的结果会占据前位；若不按
        matched_skill_steps 重排，本步召回的证据会被预算截断在 Prompt 外。
        """
        items = [
            item for item in list(references or []) if isinstance(item, dict)
        ]
        step_id = (
            str(step.get("skill_step_id") or "").strip()
            if isinstance(step, dict)
            else ""
        )
        if not step_id:
            return items, set()
        matched: list[dict[str, Any]] = []
        rest: list[dict[str, Any]] = []
        for item in items:
            matched_steps = [
                str(value) for value in (item.get("matched_skill_steps") or [])
            ]
            if step_id in matched_steps:
                matched.append(item)
            else:
                rest.append(item)
        matched.sort(
            key=lambda item: float(item.get("final_weight") or 0),
            reverse=True,
        )
        matched_keys = {cls._reference_identity(item) for item in matched}
        return matched + rest, matched_keys

    @classmethod
    def _compact_reference_items(
        cls,
        ordered: list[dict[str, Any]],
        *,
        content_limit: int,
    ) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        used_chars = 2
        for raw in ordered[:16]:
            if not isinstance(raw, dict):
                continue
            item = {
                key: cls._compact_prompt_value(raw.get(key))
                for key in (
                    "id",
                    "source_id",
                    "source_type",
                    "title",
                    "summary",
                    "reason",
                    "final_weight",
                    "source_url",
                    "observed_at",
                    "period_evidence",
                    "data_use_policy",
                    "data_freshness",
                    "refresh_status",
                    "refresh_completeness",
                    "refresh_collected_at",
                    "refresh_truncated",
                )
                if raw.get(key) is not None
            }
            # 正文单独按 content_limit 截断（不走通用 600 字压缩），
            # 预算紧张时的压缩重试才有实际可回收空间。
            raw_content = raw.get("content")
            if raw_content is not None:
                text = " ".join(str(raw_content).split()).strip()
                if text:
                    refresh_status = str(raw.get("refresh_status") or "")
                    status_limit = 1600
                    if refresh_status in {"fresh_complete", "fresh_recent"}:
                        status_limit = 6000
                    elif refresh_status in {"fresh_partial", "fresh_recent_partial"}:
                        status_limit = 3000
                    effective_limit = min(content_limit, status_limit)
                    item["content"] = (
                        text
                        if len(text) <= effective_limit
                        else text[:effective_limit].rstrip() + "…"
                    )
            candidate_size = len(str(item))
            if compacted and used_chars + candidate_size > MAX_PROMPT_REFERENCE_CHARS:
                break
            compacted.append(item)
            used_chars += candidate_size
        return compacted

    @classmethod
    def _prompt_references(
        cls,
        references: Any,
        step: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        ordered, matched_keys = cls._scope_references_for_step(references, step)
        compacted = cls._compact_reference_items(ordered, content_limit=6000)
        if matched_keys:
            matched_in = sum(
                1
                for item in compacted
                if cls._reference_identity(item) in matched_keys
            )
            if matched_in < len(matched_keys):
                # 预算装不下本步召回的全部证据时，先压缩单条正文再重试，
                # 而不是直接丢弃当前步骤自己的检索结果。
                retry = cls._compact_reference_items(ordered, content_limit=800)
                retry_matched = sum(
                    1
                    for item in retry
                    if cls._reference_identity(item) in matched_keys
                )
                if retry_matched > matched_in:
                    compacted = retry
        return compacted

    @classmethod
    def _prompt_bounded_items(
        cls,
        values: Any,
        *,
        char_budget: int,
        item_limit: int,
    ) -> list[Any]:
        compacted: list[Any] = []
        used_chars = 2
        for raw in list(values or [])[:item_limit]:
            item = cls._compact_prompt_value(raw)
            candidate_size = len(str(item))
            if compacted and used_chars + candidate_size > char_budget:
                break
            compacted.append(item)
            used_chars += candidate_size
        return compacted

    @classmethod
    def _prompt_query_results(
        cls,
        values: Any,
        step: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """QueryResult 是一个原子执行产物，按结果而不是零散 claim 裁剪。"""
        step_id = str((step or {}).get("skill_step_id") or "")
        compacted: list[dict[str, Any]] = []
        used_chars = 2
        for raw in list(values or []):
            if not isinstance(raw, dict):
                continue
            if step_id and str(raw.get("skill_step_id") or "") != step_id:
                continue
            item = {
                key: raw.get(key)
                for key in (
                    "schema_version",
                    "shape",
                    "plan",
                    "schema",
                    "rows",
                    "coverage",
                    "provenance",
                    "validation",
                    "skill_step_id",
                    "skill_step_title",
                )
                if raw.get(key) is not None
            }
            # 行是确定性执行结果，必须保持行边界；只在整个结果超过环境预算
            # 时截取可容纳的完整前缀，不把单元格拆成独立事实。
            rows = item.get("rows")
            if isinstance(rows, list):
                item["rows"] = rows[:100]
            candidate_size = len(str(item))
            if compacted and used_chars + candidate_size > 18000:
                break
            if candidate_size > 18000 and isinstance(item.get("rows"), list):
                bounded_rows = []
                base_size = len(str({**item, "rows": []}))
                for row in item["rows"]:
                    if base_size + len(str(bounded_rows)) + len(str(row)) > 18000:
                        break
                    bounded_rows.append(row)
                item["rows"] = bounded_rows
            compacted.append(item)
            used_chars += len(str(item))
        return compacted

    @classmethod
    def _prompt_webpage_scrapes(
        cls,
        scrapes: Any,
        source_ids: set[Any],
    ) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        used_chars = 2
        for raw in list(scrapes or []):
            if not isinstance(raw, dict):
                continue
            if source_ids and raw.get("source_id") not in source_ids:
                continue
            evidence = raw.get("evidence") or {}
            validation = (
                evidence.get("validation")
                if isinstance(evidence, dict)
                and isinstance(evidence.get("validation"), dict)
                else {}
            )
            item = {
                key: raw.get(key)
                for key in (
                    "source_id",
                    "status",
                    "title",
                    "url",
                    "collector",
                    "collected_at",
                    "validation_reason",
                    "verified_claim_count",
                )
                if raw.get(key) is not None
            }
            if isinstance(evidence, dict):
                item["evidence"] = {
                    key: evidence.get(key)
                    for key in (
                        "id",
                        "validation_status",
                        "page_title",
                        "source_url",
                        "captured_at",
                        "display_image_url",
                        "image_url",
                    )
                    if evidence.get(key) is not None
                }
                item["evidence"]["validation"] = {
                    key: validation.get(key)
                    for key in (
                        "reason",
                        "requirements_satisfied",
                        "screenshot_status",
                        "matched_requested_metrics",
                        "missing_requested_metrics",
                    )
                    if validation.get(key) is not None
                }
            candidate_size = len(str(item))
            if compacted and used_chars + candidate_size > MAX_PROMPT_SCRAPE_CHARS:
                break
            compacted.append(item)
            used_chars += candidate_size
        return compacted[-8:]

    def _prompt_environment(
        self,
        state: LoopState,
        step: Optional[dict[str, Any]] = None,
    ) -> str:
        requirement = state.environment.get("requirement", {})
        time_context = (
            requirement.get("time_context", {})
            if isinstance(requirement, dict)
            else {}
        )
        all_data_results = list(state.environment.get("data_results") or [])
        current_data_results = list(
            state.environment.get("current_data_results") or []
        )
        # Skill 内显式声明的 Writer/专项 Agent 也只能看到当前步骤的数据视图。
        # 否则第二个指标步骤可能同时拿到第一个步骤的数据并合并成一张表。
        is_scoped_skill_step = bool(step and step.get("skill_step_id"))
        # 严格 Skill 流程中每个步骤必须独立推理：看不到其他步骤的产物全文、
        # 已拼接的文档和它们的 Tool 回执，避免步骤之间互相污染；
        # 各步骤产物先按白名单组装，最后由全文整合润色统一衔接。
        strict_isolated = is_scoped_skill_step and bool(
            state.environment.get("strict_skill_workflow")
        )
        prompt_data_results = (
            current_data_results
            if is_scoped_skill_step and current_data_results
            else all_data_results
        )
        current_source_ids = {
            item.get("source_id")
            for item in prompt_data_results
            if isinstance(item, dict) and item.get("source_id") is not None
        }
        compact_data_results = self._prompt_data_results(prompt_data_results)
        compact_query_results = self._prompt_query_results(
            state.environment.get("data_query_results", []),
            step,
        )
        compact_scrapes = self._prompt_webpage_scrapes(
            state.environment.get("webpage_scrapes", []),
            current_source_ids if is_scoped_skill_step else set(),
        )
        compact_references = self._prompt_references(
            state.environment.get("references", []),
            step,
        )
        compact_skills = self._prompt_bounded_items(
            state.environment.get("applied_skills", []),
            char_budget=MAX_PROMPT_SKILL_CHARS,
            item_limit=8,
        )
        completed_steps_source = list(
            state.environment.get("completed_skill_steps") or []
        )
        completed_steps_label = "已激活的 Skill 步骤"
        if strict_isolated:
            # 独立推理：只告知哪些步骤已完成，不提供它们的正文，
            # 否则当前步骤会把其他步骤的结论掺进自己的章节。
            current_step_id = str(step.get("skill_step_id") or "")
            completed_steps_source = [
                {
                    "step_id": item.get("step_id"),
                    "title": item.get("title"),
                    "status": "completed",
                }
                for item in completed_steps_source
                if isinstance(item, dict)
                and str(item.get("step_id") or "") != current_step_id
            ]
            completed_steps_label = "已完成的 Skill 步骤（仅提供标题，步骤间独立推理）"
            tool_results_source = [
                item
                for item in (state.environment.get("tool_results") or [])
                if isinstance(item, dict)
                and str(item.get("skill_step_id") or "") == current_step_id
            ]
        else:
            tool_results_source = list(state.environment.get("tool_results") or [])
        compact_completed_steps = self._prompt_bounded_items(
            completed_steps_source,
            char_budget=MAX_PROMPT_COMPLETED_STEPS_CHARS,
            item_limit=16,
        )
        brainstorm_context = str(
            state.environment.get("creation_brief_context") or ""
        ).strip()
        if (
            not brainstorm_context
            and state.creation_mode == "brainstorm"
            and state.environment.get("creation_brief")
        ):
            # 兼容修复前已持久化、尚未带紧凑上下文的 continuation。
            brainstorm_context = self._brainstorm_prompt_context(
                state.environment.get("creation_brief")
            )
        blocks = [
            f"当前确定时间（本机时区，禁止自行猜测）：{time_context}",
            f"原始需求：{state.root_request}",
            f"本轮编辑意图：{state.environment.get('edit_intent', {})}",
            f"任务画像：{state.environment.get('requirement', {})}",
            f"确定性数据查询结果：{compact_query_results}",
            f"当前步骤数据事实：{compact_data_results}",
            "数据风险使用规则：优先使用目标周期且口径匹配的数据；can_use=true 且 data_usage_status=qualified 的来源事实允许用于补齐缺失指标，必须写出原始值并标记‘参考值’，在表格或段落下方披露 data_risks 中的实际周期、请求周期、来源及风险。verified_claims 仅表示来源事实已核验，不代表符合目标周期；不得将参考值写成本周实绩，不得混合周期推算环比或增长率。整合、编辑、润色时必须保留这些数值和风险说明。没有来源数字时不得编造。",
            f"当前步骤网页采集回执：{compact_scrapes}",
            (
                "Tool 执行回执（Tool 已由 Harness 调用，Agent 直接消费结果）："
                f"{self._prompt_bounded_items(tool_results_source, char_budget=5000, item_limit=20)}"
            ),
            f"{completed_steps_label}：{compact_completed_steps}",
            f"本地参考：{compact_references}",
            f"已应用 Skill：{compact_skills}",
            f"本轮质检动态激活 Skill：{self._compact_prompt_value(state.environment.get('activated_quality_skills', []))}",
            f"互联网资料：{self._compact_prompt_value(state.environment.get('web_results', []))}",
            f"GitHub 公开仓库：{self._compact_prompt_value(state.environment.get('github_results', []))}",
            f"PlantUML 画图约束：{self._compact_prompt_value(state.environment.get('plantuml_diagram', {}))}",
            f"Mermaid 画图约束：{self._compact_prompt_value(state.environment.get('mermaid_diagram', {}))}",
            f"章节 Visual Plan：{self._compact_prompt_value(state.environment.get('visual_plan', {}))}",
            f"已准备的章节 Mermaid 图示：{self._compact_prompt_value(state.environment.get('mermaid_diagrams', []))}",
            f"数据分析：{state.environment.get('data_analysis', '')}",
            f"行业调研：{state.environment.get('industry_research', '')}",
            f"方案设计：{state.environment.get('solution_design', '')}",
            f"章节设计：{state.environment.get('chapter_design', '')}",
            f"上一轮质量审校：{state.environment.get('quality_review', {})}",
            f"当前质检问题：{state.environment.get('quality_issues', [])}",
        ]
        if brainstorm_context:
            # 提前放置，保证环境达到总长度上限时仍保留用户已选决策。
            blocks.insert(2, brainstorm_context)
        if state.current_document and not strict_isolated:
            outline = "\n".join(
                f"{'#' * int(span['level'])} {span['title']}"
                for span in self._markdown_section_spans(state.current_document)
            )
            blocks.append(f"现有文档目录：\n{outline}")
            blocks.append(
                f"现有完整文档：\n{self.service._clip(state.current_document, 64000)}"
            )
        if state.conversation:
            blocks.append(f"关键对话：{self._conversation_for_prompt(state.conversation)}")
        environment = "\n\n".join(blocks)
        return self.service._clip(environment, MAX_PROMPT_ENVIRONMENT_CHARS)

    def _document_patch_context(
        self,
        document: str,
        target_sections: list[str],
    ) -> str:
        spans = self._markdown_section_spans(document)
        outline = "\n".join(
            f"{'#' * int(span['level'])} {span['title']}" for span in spans
        )
        target = target_sections[0] if target_sections else ""
        matched = self._find_section_span(target, spans) if target else None
        if matched:
            section = document[int(matched["start"]) : int(matched["end"])].strip()
            section_context = self.service._clip(section, 12000)
        else:
            first_h2 = next(
                (int(span["start"]) for span in spans if int(span["level"]) == 2),
                min(len(document), 3000),
            )
            preface = document[:first_h2].strip()
            section_context = (
                f"目标章节“{target}”当前不存在，需要新增。\n"
                f"文档标题和导语：\n{self.service._clip(preface, 3000)}"
            )
        return f"文档目录：\n{outline}\n\n目标章节上下文：\n{section_context}"

    def _reference_to_state(
        self,
        item: ReferenceDocument,
        period_evidence: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        content_limit = 8000 if item.refresh_status.startswith("fresh_") else 1600
        return {
            "id": item.id,
            "source_id": item.source_id,
            "source_type": item.source_type,
            "title": item.title,
            "doc_type": item.doc_type,
            "summary": self.service._clip(item.summary, 600),
            "content": self.service._clip(
                self.service._best_reference_content(item), content_limit
            ),
            "reason": item.reason,
            "final_weight": round(item.final_weight, 4),
            "retrieval_tier": item.retrieval_tier,
            "retrieval_paths": list(item.retrieval_paths),
            "matched_keywords": list(item.matched_keywords),
            "matched_entities": list(item.matched_entities),
            "lexical_score": round(item.lexical_score, 4),
            "semantic_score": round(item.semantic_score, 4),
            "entity_score": round(item.entity_score, 4),
            "retrieval_mode": item.retrieval_mode,
            "primary_target": item.primary_target,
            "matched_components": list(item.matched_components),
            "matched_relations": list(item.matched_relations),
            "relation_score": round(item.relation_score, 4),
            "selection_reasons": list(item.selection_reasons),
            "source_url": item.source_url,
            "observed_at": item.observed_at,
            "period_evidence": period_evidence or {},
            "refresh_status": item.refresh_status,
            "refresh_completeness": item.refresh_completeness,
            "refresh_collected_at": item.refresh_collected_at,
            "refresh_truncated": item.refresh_truncated,
        }

    @staticmethod
    def _merge_reference_states(
        existing: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        positions: dict[str, int] = {}
        for raw in [*existing, *incoming]:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            source_type = str(item.get("source_type") or "document")
            source_id = item.get("source_id")
            if source_id is None:
                source_id = item.get("id")
            key = f"{source_type}:{source_id}"
            position = positions.get(key)
            if position is None:
                positions[key] = len(merged)
                item["matched_skill_steps"] = [
                    str(item.get("skill_step_id"))
                ] if item.get("skill_step_id") else []
                merged.append(item)
                continue
            current = merged[position]
            step_id = str(item.get("skill_step_id") or "").strip()
            matched_steps = list(current.get("matched_skill_steps") or [])
            if step_id and step_id not in matched_steps:
                matched_steps.append(step_id)
            current["matched_skill_steps"] = matched_steps
            if float(item.get("final_weight") or 0) > float(
                current.get("final_weight") or 0
            ):
                preserved_steps = current["matched_skill_steps"]
                merged[position] = {**item, "matched_skill_steps": preserved_steps}
        return merged[: max(1, limit)]

    @staticmethod
    def _merge_data_results(
        existing: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        positions: dict[str, int] = {}
        for raw in [*existing, *incoming]:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            source_id = item.get("source_id")
            key = (
                f"source:{source_id}"
                if source_id is not None
                else f"url:{str(item.get('source_url') or '').strip()}"
            )
            position = positions.get(key)
            if position is None:
                positions[key] = len(merged)
                merged.append(item)
            else:
                merged[position] = {**merged[position], **item}
        return merged[:100]

    @staticmethod
    def _merge_evidence_items(
        existing: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        positions: dict[str, int] = {}
        for raw in [*existing, *incoming]:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            evidence_id = str(item.get("id") or "").strip()
            if not evidence_id:
                continue
            position = positions.get(evidence_id)
            if position is None:
                positions[evidence_id] = len(merged)
                merged.append(item)
            else:
                merged[position] = {**merged[position], **item}
        return merged[:50]

    def _needs_confirmation(self, state: LoopState) -> bool:
        compact = "".join(state.user_message.split())
        return state.mode == "initial" and len(compact) < 8

    def _update_goal(self, state: LoopState) -> None:
        state.goal.revision += 1
        state.goal.status = "active"
        state.goal.remaining_steps = [item["name"] for item in state.plan[state.cursor:]]

    def _event(
        self,
        state: LoopState,
        event_type: str,
        summary: str,
        *,
        status: str = "running",
        actor: Optional[dict[str, str]] = None,
        environment_patch: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        state.sequence += 1
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"event-{uuid4()}",
            "session_id": state.session_id,
            "run_id": state.run_id,
            "sequence": state.sequence,
            "timestamp": int(time.time() * 1000),
            "type": event_type,
            "status": status,
            "actor": actor
            or self._actor("agent", "creation_main_agent", "创作 Agent"),
            "summary": summary,
            "goal": {
                "objective": state.goal.objective,
                "status": state.goal.status,
                "revision": state.goal.revision,
                "remaining_steps": state.goal.remaining_steps,
                "outcome": state.goal.outcome,
            },
            "environment_patch": environment_patch or {},
            "data": data or {},
        }

    @staticmethod
    def _actor(kind: str, actor_id: str, name: str) -> dict[str, str]:
        return {"kind": kind, "id": actor_id, "name": name}

    @staticmethod
    def _normalize_conversation(
        conversation: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for item in conversation:
            role = str(item.get("role") or "")
            content = str(item.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                result.append({"role": role, "content": content[:12000]})
        if len(result) <= 40:
            return result
        # 根需求和最初约束永远保留；中间轮次可由当前文档承载，尾部保留近期修改。
        return [*result[:4], *result[-36:]]

    @staticmethod
    def _conversation_for_prompt(
        conversation: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        if len(conversation) <= 16:
            return conversation
        return [*conversation[:4], *conversation[-12:]]
