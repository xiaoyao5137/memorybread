"""模型驱动的递归创作脑暴协调器。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from uuid import uuid4

from .service import CreationService

logger = logging.getLogger(__name__)


class BrainstormGenerationError(RuntimeError):
    """下一步脑暴问题无法安全生成。"""


class BrainstormCoordinator:
    """只负责生成下一步；权威会话状态仍由 Core 保存。"""

    MAX_CONTEXT_DECISIONS = 24
    MAX_BRIEF_CHARS = 12000
    MAX_SKILL_CONTEXT_CHARS = 16000
    MAX_GENERATION_ATTEMPTS = 3

    # 方案类创作先建立业务闭环，再讨论实现细节。代码维护覆盖门禁，模型只负责
    # 把当前 question_goal 写成自然、任务特定的问题，避免一次推荐后无限纵向下钻。
    SOLUTION_MARKERS = ("方案", "架构", "规划", "策略", "prd", "rfc")
    TECHNICAL_MARKERS = (
        "技术", "接口", "微服务", "系统", "平台", "数据", "api", "服务"
    )
    BUSINESS_COVERAGE = (
        {
            "id": "business_outcome",
            "label": "业务目标与预期决策",
            "question_goal": "明确要解决的业务问题、期望推动的动作和可感知价值；不要先询问性能参数或技术选型。",
        },
        {
            "id": "users_workflow",
            "label": "使用者与业务流程",
            "question_goal": "明确主要使用者、触发时机、所在业务环节，以及使用结果后要采取的动作。",
        },
        {
            "id": "problem_evidence",
            "label": "现状问题与证据",
            "question_goal": "明确当前症状、发生频率或已有证据，以及为什么现有机制不能解决；未知事实允许标为待调研。",
        },
        {
            "id": "scope_boundary",
            "label": "范围、对象与边界",
            "question_goal": "明确一期覆盖与不覆盖的对象、输入输出、系统边界和关键上下游。",
        },
        {
            "id": "ownership_delivery",
            "label": "责任、运营与落地闭环",
            "question_goal": "明确配置维护、审核发布、异常处理、反馈申诉、上线运营等责任和协作边界。",
        },
        {
            "id": "success_criteria",
            "label": "业务验收与成功标准",
            "question_goal": "明确可观察的业务验收结果、质量护栏和失败判定；没有依据时不要虚构精确指标。",
        },
    )
    # 方案类创作不能在“为什么做”清楚后就收敛。以下覆盖项对应会改变后续
    # 章节内容的设计决策，而不是要求用户提前代写章节。顺序有意把架构和
    # 核心机制放在背景三问之后，避免脑暴长期停留在第一章。
    DESIGN_COVERAGE = (
        {
            "id": "solution_architecture",
            "label": "总体方案与能力架构",
            "question_goal": "围绕原始任务确认总体解法、核心能力分层、关键组件职责与边界；给出同一层级的架构方向及取舍，不要只问目标或受众。",
        },
        {
            "id": "core_capability_mechanism",
            "label": "核心能力与实现机制",
            "question_goal": "识别原始任务最具差异化的核心能力，并确认它如何产生结果：关键输入、知识或规则来源、处理阶段、可控参数、人工介入与失败兜底。选项必须是任务特定的机制路线，而非宏观价值表述。",
        },
        {
            "id": "end_to_end_interaction",
            "label": "端到端使用链路与集成",
            "question_goal": "确认从用户发起、输入准备、过程反馈、结果修改到下游消费或回流的完整链路，以及产品内嵌、独立入口或 API 集成等方向取舍；不能只停留在首次点击或单个动作。",
        },
        {
            "id": "quality_evaluation",
            "label": "质量保障、评估与反馈闭环",
            "question_goal": "确认结果质量的判断标准、自动与人工评估方式、审核边界、用户反馈如何回流，以及不合格结果如何修正；未知指标保留为待实验定标。",
        },
        {
            "id": "delivery_rollout",
            "label": "实施路径、依赖与演进",
            "question_goal": "确认一期最小闭环、关键依赖、上线或试点方式、演进顺序和失败回退；选项应体现可交付路径的取舍，不虚构工期和资源数字。",
        },
    )
    TECHNICAL_COVERAGE = (
        {
            "id": "data_governance",
            "label": "数据、权限与治理",
            "question_goal": "明确数据来源和权威口径、访问权限、隐私审计、生命周期及错误数据处置。",
        },
        {
            "id": "technical_constraints",
            "label": "技术约束与质量属性",
            "question_goal": "在业务流程已明确后再确认性能、容量、一致性、可用性和集成约束；未知数值应给出压测定标方法，而不是推荐伪精确阈值。",
        },
    )
    DIMENSION_KEYWORDS = {
        "business_outcome": ("业务目标", "预期决策", "业务价值", "核心价值", "价值主张", "核心诉求", "要解决"),
        "users_workflow": ("使用者", "用户", "角色", "业务流程", "触发时机", "使用场景"),
        "problem_evidence": ("现状", "问题", "痛点", "证据", "发生频率", "根因"),
        "scope_boundary": ("范围", "非范围", "边界", "输入输出", "上下游", "诊断对象"),
        "ownership_delivery": ("责任", "运营", "组织", "raci", "发布", "反馈", "申诉", "维护"),
        "success_criteria": ("成功标准", "验收", "业务指标", "效果指标"),
        "data_governance": ("数据来源", "数据口径", "权限", "隐私", "审计", "生命周期", "治理"),
        "technical_constraints": ("性能", "容量", "一致性", "可用性", "延迟", "吞吐", "容错", "技术约束"),
        "solution_architecture": ("总体方案", "能力架构", "整体架构", "组件职责", "系统边界", "能力分层"),
        "core_capability_mechanism": ("核心能力", "核心机制", "生成机制", "实现机制", "处理机制", "机制路线"),
        "end_to_end_interaction": ("端到端使用", "完整使用链路", "下游集成", "结果回流", "集成闭环"),
        "quality_evaluation": ("质量保障", "质量评估", "评估机制", "审核边界", "反馈闭环"),
        "delivery_rollout": ("实施路径", "试点方式", "上线策略", "演进顺序", "交付路径", "灰度方案"),
    }

    # Skill 的步骤不逐条变成问题，否则一个九步模板会制造九道机械题。
    # 这里把决策性步骤归并到稳定章节决策面，并将步骤目标注入该问题。
    SKILL_STEP_GROUPS = {
        "solution_architecture": ("总体", "架构", "系统边界", "组件", "职责边界"),
        "core_capability_mechanism": ("核心能力", "详细环节", "详细设计", "功能设计", "生成", "机制"),
        "end_to_end_interaction": ("业务流程", "用户流程", "交互", "调用链路", "集成"),
        "quality_evaluation": ("质量", "评估", "验收", "测试", "审核", "反馈"),
        "delivery_rollout": ("实施", "排期", "上线", "迁移", "演进", "风险", "组织", "保障"),
        "data_governance": ("数据模型", "e-r", "数据所有权", "权限", "治理", "生命周期"),
        "technical_constraints": ("非功能", "性能", "容量", "容灾", "安全", "可观测"),
    }
    NON_DECISION_SKILL_STEP_MARKERS = (
        "收集",
        "检索",
        "调研",
        "撰写",
        "写作",
        "排版",
        "润色",
        "审校",
        "交付",
    )
    DESIGN_DIMENSION_IDS = {
        "solution_architecture",
        "core_capability_mechanism",
        "end_to_end_interaction",
        "quality_evaluation",
        "delivery_rollout",
    }

    def __init__(self, creation_service: CreationService) -> None:
        self.creation_service = creation_service

    async def next_step(
        self,
        *,
        root_request: str,
        decisions: list[dict[str, Any]],
        brief_markdown: str,
        selected_skills: Optional[list[dict[str, Any]]] = None,
        force_continue: bool = False,
        focus_hint: str = "",
        creation_model: Optional[str] = None,
        creation_api_key: Optional[str] = None,
        creation_base_url: Optional[str] = None,
    ) -> dict[str, Any]:
        skills = selected_skills or []
        required_coverage = self._required_coverage(root_request, skills)
        covered_ids = self._covered_dimension_ids(decisions)
        covered_ids.update(self._root_request_coverage_ids(root_request))
        next_goal = next(
            (item for item in required_coverage if item["id"] not in covered_ids),
            None,
        )
        prompt = self._build_prompt(
            root_request=root_request,
            decisions=decisions,
            brief_markdown=brief_markdown,
            selected_skills=skills,
            required_coverage=required_coverage,
            covered_ids=covered_ids,
            next_goal=next_goal,
            force_continue=force_continue,
            focus_hint=focus_hint,
        )
        last_error: Optional[Exception] = None
        for attempt in range(self.MAX_GENERATION_ATTEMPTS):
            raw = ""
            corrective = ""
            if attempt:
                corrective = (
                    "\n\n上一次输出未通过质量或结构校验："
                    f"{str(last_error)[:240]}。请修正后严格只输出一个 JSON 对象，"
                    "不要使用 Markdown 代码块或补充说明。若返回 question，type 只能是 "
                    "single_choice 或 multi_choice，options 必须包含 2 到 5 个完整对象；"
                    "每项必须有非空 id、label、description，且只能有一个 recommended=true。"
                )
            try:
                raw = await self._complete(
                    prompt + corrective,
                    creation_model=creation_model,
                    creation_api_key=creation_api_key,
                    creation_base_url=creation_base_url,
                )
                result = self._normalize_result(
                    raw,
                    force_continue=force_continue,
                    focus_hint=focus_hint,
                    expected_dimension_id=(next_goal or {}).get("id", ""),
                    root_request=root_request,
                    decisions=decisions,
                )
                if next_goal:
                    pending_labels = [
                        item["label"]
                        for item in required_coverage
                        if item["id"] not in covered_ids
                    ]
                    result["open_flags"] = self._merge_open_flags(
                        pending_labels,
                        result.get("open_flags", []),
                    )
                return result
            except (BrainstormGenerationError, json.JSONDecodeError) as exc:
                last_error = exc
                diagnostic = self._output_shape_diagnostic(raw)
                validation = str(exc)[:160]
                logger.warning(
                    "动态脑暴下一步生成失败 attempt=%s/%s error_type=%s "
                    "validation=%s status=%s question_type=%s option_count=%s",
                    attempt + 1,
                    self.MAX_GENERATION_ATTEMPTS,
                    type(exc).__name__,
                    validation,
                    diagnostic["status"],
                    diagnostic["question_type"],
                    diagnostic["option_count"],
                )
        raise BrainstormGenerationError(
            f"本地模型连续 {self.MAX_GENERATION_ATTEMPTS} 次未能生成合格的方向选项，请重试"
        ) from last_error

    async def _complete(
        self,
        user_prompt: str,
        *,
        creation_model: Optional[str],
        creation_api_key: Optional[str],
        creation_base_url: Optional[str],
    ) -> str:
        chunks: list[str] = []
        async for chunk in self.creation_service._stream_direct_completion(
            system_prompt=self._system_prompt(),
            user_prompt=user_prompt,
            creation_model=creation_model,
            creation_api_key=creation_api_key,
            creation_base_url=creation_base_url,
            num_predict=1600,
            temperature=0.15,
            disable_thinking=True,
            json_mode=True,
        ):
            chunks.append(chunk)
        result = "".join(chunks).strip()
        if not result:
            raise BrainstormGenerationError("模型未返回脑暴问题")
        return result

    @staticmethod
    def _system_prompt() -> str:
        return """你是创作前置脑暴协调器。你的任务不是写最终方案，而是帮助用户逐步想清楚需求。

核心规则：
1. 每轮只问一个当前最能改变方案方向的问题。
2. 问题和选项必须根据用户原始需求以及此前每一次选择动态生成，不能套用固定题库或固定维度顺序。
3. 先完成高影响维度的横向覆盖，再对已选方向纵向下钻。除非用户通过 focus_hint 主动要求，否则不要连续追问同一类性能、架构或实现细节。
4. 当上下文提供 next_question_goal 时，必须围绕它提问并返回完全相同的 dimension_id，不得返回 ready，也不得自行换题。
5. 不设固定层数。只有 required_coverage 全部 covered，且没有会推翻整体方向的高影响歧义时，才可返回 ready。
6. 是否继续追问以及何时收敛，由覆盖状态和当前上下文共同决定，不能把这个判断作为一道题交给用户。
7. 若 force_continue=true 且 focus_hint 非空，表示用户在收敛后主动选择了新方向；覆盖门禁仍优先，门禁完成后再围绕 focus_hint 提问。
8. question 的 type 只能是 single_choice 或 multi_choice。options 数组必须提供 2 到 5 个同一抽象层级的任务特定方向，绝不能只提供 0 或 1 个；推荐项排第一且只能有一个；每项必须有非空 id、label、description，description 必须说明依据、影响或代价。
9. 不得把业务场景仅按延迟、一致性等技术属性分类。不得在用户或资料没有给出依据时推荐具体 P95/P99、QPS、容量、成本或排期数值；应推荐“待调研/待压测定标”并说明定标方法。
10. 所有问题都必须先枚举真实可行的方向；用户界面会固定追加“自定义答案”，因此不得用 free_text、空 options 或“其他”选项逃避方向比较。难以穷举时，给出 2 到 5 个代表性路线，并在描述中说明枚举并不封闭。
11. 已选 Skill 是本轮覆盖和完成条件的依据，必须吸收其中的业务目标、角色、流程、数据、责任、风险和验收要求，但不要机械照抄章节目录。
12. 方案类任务必须覆盖背景决策和章节决策。章节决策聚焦总体架构、核心机制、端到端链路、质量反馈和实施演进；不得因目标、受众和痛点已明确就提前 ready。
13. 章节决策题必须提供能改变该章节方案走向的任务特定路线。例如核心机制要比较生成、规则、知识、人工协同等真正适用的路线及取舍，不能继续问宏观目标，也不能用空泛的“推荐/保守/激进”充当选项。
14. 返回 ready 时，同时推荐 2 到 4 个可选的继续脑暴方向。方向应拓展、挑战或补强当前简报；推荐项排第一且只能有一个。
15. 不输出思维链，只输出用户可理解的问题、简短原因、选项取舍和收敛摘要。

只输出以下 JSON 之一：
{"status":"question","readiness_reason":"为什么还要继续","open_flags":["仍待确认的高影响事项"],"question":{"dimension_id":"稳定维度ID","dimension":"本轮主题","type":"single_choice","prompt":"问题","why_now":"为何现在要问","required":true,"allow_custom":true,"answer_template":"自定义答案提示","options":[{"id":"recommended_option","label":"推荐选项","description":"推荐依据、影响或代价","recommended":true},{"id":"alternative_option","label":"备选选项","description":"备选方向的影响或代价","recommended":false}]}}
或
{"status":"ready","readiness_reason":"为什么已经足以生成","open_flags":["仍可后续细化但不会推翻主方向的事项"],"continuation_directions":[{"id":"recommended_direction","label":"推荐的继续脑暴方向","description":"这个方向能补强什么","recommended":true},{"id":"alternative_direction","label":"另一个继续脑暴方向","description":"这个方向能挑战什么","recommended":false}]}
"""

    @classmethod
    def _build_prompt(
        cls,
        *,
        root_request: str,
        decisions: list[dict[str, Any]],
        brief_markdown: str,
        selected_skills: list[dict[str, Any]],
        required_coverage: list[dict[str, Any]],
        covered_ids: set[str],
        next_goal: Optional[dict[str, str]],
        force_continue: bool,
        focus_hint: str,
    ) -> str:
        compact_decisions = decisions[-cls.MAX_CONTEXT_DECISIONS :]
        context = {
            "original_request": root_request.strip(),
            "decision_path": compact_decisions,
            "current_brief": brief_markdown[-cls.MAX_BRIEF_CHARS :],
            "force_continue": force_continue,
            "focus_hint": focus_hint.strip(),
            "answered_depth": len(decisions),
            "selected_skills": cls._compact_skill_context(selected_skills),
            "required_coverage": [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "status": "covered" if item["id"] in covered_ids else "pending",
                    "source_steps": item.get("source_steps", []),
                }
                for item in required_coverage
            ],
            "next_question_goal": next_goal or {},
        }
        return (
            "请判断当前是否已经收敛；若未收敛，生成唯一的下一步动态问题。"
            "有 next_question_goal 时必须先补齐该横向维度；没有时才可沿当前分支细化。\n\n"
            + json.dumps(context, ensure_ascii=False)
        )

    @classmethod
    def _normalize_result(
        cls,
        raw: str,
        *,
        force_continue: bool,
        focus_hint: str = "",
        expected_dimension_id: str = "",
        root_request: str = "",
        decisions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        parsed = cls._parse_json_object(raw)
        status = str(parsed.get("status") or "").strip().lower()
        if expected_dimension_id and status == "ready":
            raise BrainstormGenerationError("必答业务维度尚未覆盖，不能收敛")
        open_flags = cls._clean_string_list(parsed.get("open_flags"), limit=8)
        readiness_reason = str(parsed.get("readiness_reason") or "").strip()[:500]
        if status == "ready":
            directions = cls._normalize_directions(
                parsed.get("continuation_directions")
            )
            if force_continue:
                normalized_focus = str(focus_hint or "").strip()[:160]
                dimension = normalized_focus or "继续脑暴"
                question = cls._normalize_question(
                    {
                        "dimension_id": "continuation_focus",
                        "dimension": dimension,
                        "type": "single_choice",
                        "prompt": (
                            f"围绕“{normalized_focus}”，下一步优先深入哪个方向？"
                            if normalized_focus
                            else "接下来优先沿哪个方向继续深入？"
                        ),
                        "why_now": (
                            "模型判断现有简报已经收敛；以下选项复用它给出的拓展方向，"
                            "用于把你选择的继续脑暴意图落实为下一步问题。"
                        ),
                        "required": True,
                        "allow_custom": True,
                        "answer_template": "补充你希望继续深入的具体角度。",
                        "options": directions,
                    }
                )
                return {
                    "status": "question",
                    "readiness_reason": readiness_reason or "已按所选方向继续展开",
                    "open_flags": open_flags,
                    "continuation_directions": [],
                    "question": question,
                }
            return {
                "status": "ready",
                "readiness_reason": readiness_reason or "关键方向已经足以支撑创作",
                "open_flags": open_flags,
                "continuation_directions": directions,
                "question": None,
            }
        if status != "question" or not isinstance(parsed.get("question"), dict):
            raise BrainstormGenerationError("模型返回了无效的脑暴状态")
        question = cls._normalize_question(parsed["question"])
        if expected_dimension_id and question["dimension_id"] != expected_dimension_id:
            raise BrainstormGenerationError(
                "问题没有回答指定的覆盖目标 " + expected_dimension_id
            )
        if cls._repeats_answered_question(question, decisions or []):
            raise BrainstormGenerationError("问题重复了已经回答过的脑暴问题")
        if cls._has_unsupported_performance_target(
            question,
            root_request=root_request,
            decisions=decisions or [],
        ):
            raise BrainstormGenerationError("问题包含缺乏用户或资料依据的精确性能指标")
        if (
            expected_dimension_id in cls.DESIGN_DIMENSION_IDS
            and cls._has_only_generic_design_options(question)
        ):
            raise BrainstormGenerationError("章节决策选项过于宏观，缺少任务特定机制或取舍")
        return {
            "status": "question",
            "readiness_reason": readiness_reason or "仍有会影响方案方向的事项需要确认",
            "open_flags": open_flags,
            "continuation_directions": [],
            "question": question,
        }

    @classmethod
    def _required_coverage(
        cls,
        root_request: str,
        selected_skills: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        skill_text = json.dumps(selected_skills, ensure_ascii=False).lower()
        task_text = (root_request + "\n" + skill_text).lower()
        solution_like = any(marker in task_text for marker in cls.SOLUTION_MARKERS) or (
            "设计" in task_text
            and any(
                marker in task_text
                for marker in cls.TECHNICAL_MARKERS
                + ("产品", "流程", "组织", "业务", "治理", "机制", "功能", "能力", "使用", "agent")
            )
        )
        if not solution_like:
            return []
        business_by_id = {item["id"]: dict(item) for item in cls.BUSINESS_COVERAGE}
        design_by_id = {item["id"]: dict(item) for item in cls.DESIGN_COVERAGE}
        # 背景三问完成后立即进入会改变正文主体的架构与机制决策；范围、责任
        # 和验收仍会补齐，但不再挡在所有设计章节之前。
        order = (
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
        required = [
            dict(business_by_id.get(item_id) or design_by_id[item_id])
            for item_id in order
        ]
        if any(marker in task_text for marker in cls.TECHNICAL_MARKERS):
            # 数据治理放在核心机制之后，技术约束始终最后确认，确保性能指标
            # 从业务、架构和机制推导，而不是主导整个脑暴。
            required.insert(6, dict(cls.TECHNICAL_COVERAGE[0]))
            required.append(dict(cls.TECHNICAL_COVERAGE[1]))
        return cls._enrich_coverage_from_skills(required, selected_skills)

    @classmethod
    def _enrich_coverage_from_skills(
        cls,
        required: list[dict[str, Any]],
        selected_skills: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_id = {item["id"]: item for item in required}
        for skill in selected_skills[:4]:
            if not isinstance(skill, dict):
                continue
            skill_title = str(skill.get("title") or "已选 Skill").strip()
            steps = skill.get("executionSteps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, dict):
                    continue
                title = str(step.get("title") or "").strip()
                objective = str(step.get("objective") or "").strip()
                output = str(step.get("output") or "").strip()
                if any(marker in title for marker in cls.NON_DECISION_SKILL_STEP_MARKERS):
                    continue
                haystack = " ".join((title, objective, output)).lower()
                for dimension_id, keywords in cls.SKILL_STEP_GROUPS.items():
                    target = by_id.get(dimension_id)
                    if target is None or not any(
                        keyword.lower() in haystack for keyword in keywords
                    ):
                        continue
                    sources = target.setdefault("source_steps", [])
                    source = {
                        "skill": skill_title[:120],
                        "step": title[:120],
                        "objective": objective[:500],
                        "output": output[:200],
                    }
                    if source not in sources:
                        sources.append(source)
        for item in required:
            sources = item.get("source_steps") or []
            if sources:
                step_summary = "；".join(
                    f"{source['skill']} / {source['step']}：{source['objective']}"
                    for source in sources[:3]
                )
                item["question_goal"] += (
                    " 本题还必须吸收已选 Skill 的对应步骤，但只询问需要用户拍板的方向："
                    + step_summary
                )
        return required

    @classmethod
    def _covered_dimension_ids(cls, decisions: list[dict[str, Any]]) -> set[str]:
        covered: set[str] = set()
        for decision in decisions:
            explicit = str(decision.get("dimension_id") or "").strip()
            if explicit in cls.DIMENSION_KEYWORDS:
                covered.add(explicit)
                continue
            question_text = " ".join(
                str(decision.get(key) or "")
                for key in ("dimension", "question")
            ).lower()
            matched = False
            for dimension_id, keywords in cls.DIMENSION_KEYWORDS.items():
                if any(keyword.lower() in question_text for keyword in keywords):
                    covered.add(dimension_id)
                    matched = True
            if matched:
                continue
            answer_text = str(decision.get("answer") or "").lower()
            for dimension_id, keywords in cls.DIMENSION_KEYWORDS.items():
                if any(keyword.lower() in answer_text for keyword in keywords):
                    covered.add(dimension_id)
        return covered

    @staticmethod
    def _root_request_coverage_ids(root_request: str) -> set[str]:
        text = root_request.lower()
        patterns = {
            "business_outcome": (r"目标是", r"为了", r"希望(?:实现|改善|解决|推动)", r"要解决"),
            "users_workflow": (r"面向.{1,20}(?:用户|人员|团队|运营|商家|客户)", r"由.{1,20}使用", r"在.{1,24}(?:环节|阶段|流程)"),
            "problem_evidence": (r"当前(?:存在|经常|已经)", r"现状", r"痛点", r"已有(?:数据|证据|案例)"),
            "scope_boundary": (r"一期", r"不(?:包含|覆盖|负责)", r"范围(?:是|包括)", r"输入(?:是|包括)", r"输出(?:是|包括)"),
            "ownership_delivery": (r"由.{1,20}(?:负责|维护|审核|运营)", r"责任人", r"raci", r"上线后"),
            "success_criteria": (r"验收(?:标准|指标)", r"成功标准", r"以.{1,24}为准", r"业务指标"),
            "data_governance": (r"数据来源", r"权威口径", r"权限", r"隐私", r"审计"),
            "technical_constraints": (r"p(?:90|95|99)", r"qps", r"tps", r"一致性", r"可用性", r"延迟", r"吞吐"),
        }
        return {
            dimension_id
            for dimension_id, expressions in patterns.items()
            if any(re.search(expression, text, re.IGNORECASE) for expression in expressions)
        }

    @staticmethod
    def _merge_open_flags(primary: Any, required: list[str]) -> list[str]:
        merged: list[str] = []
        for item in list(primary or []) + required:
            value = str(item).strip()[:300]
            if value and value not in merged:
                merged.append(value)
        return merged[:8]

    @classmethod
    def _compact_skill_context(
        cls,
        selected_skills: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        remaining = cls.MAX_SKILL_CONTEXT_CHARS
        for skill in selected_skills[:4]:
            if not isinstance(skill, dict) or remaining <= 0:
                continue
            item = {
                key: skill.get(key)
                for key in (
                    "id",
                    "title",
                    "summary",
                    "workflowRole",
                    "skillDescription",
                    "executionSteps",
                    "writingDesign",
                    "voiceStyle",
                )
                if skill.get(key) not in (None, "", [], {})
            }
            encoded = json.dumps(item, ensure_ascii=False)
            if len(encoded) > remaining:
                item = {
                    "id": item.get("id", ""),
                    "title": item.get("title", ""),
                    "summary": str(item.get("summary") or "")[: max(0, remaining - 300)],
                }
                encoded = json.dumps(item, ensure_ascii=False)
            compact.append(item)
            remaining -= len(encoded)
        return compact

    @staticmethod
    def _has_unsupported_performance_target(
        question: dict[str, Any],
        *,
        root_request: str,
        decisions: list[dict[str, Any]],
    ) -> bool:
        target_pattern = re.compile(
            r"(?:p(?:90|95|99)|qps|tps)[^\n，。;；]{0,24}\d+|"
            r"\d+(?:\.\d+)?\s*(?:ms|毫秒)[^\n，。;；]{0,16}(?:以内|以下|小于|<)",
            re.IGNORECASE,
        )
        question_text = json.dumps(question, ensure_ascii=False)
        if not target_pattern.search(question_text):
            return False
        evidence_text = root_request + "\n" + json.dumps(decisions, ensure_ascii=False)
        return target_pattern.search(evidence_text) is None

    @staticmethod
    def _has_only_generic_design_options(question: dict[str, Any]) -> bool:
        options = question.get("options")
        if not isinstance(options, list) or not options:
            return False
        generic = re.compile(
            r"^(?:推荐|备选|默认|标准|常规|保守|平衡|激进|轻量|完整|全面|"
            r"方向一|方向二|方案一|方案二)(?:方案|方向|模式|路线)?$"
        )
        labels = [str(item.get("label") or "").strip() for item in options if isinstance(item, dict)]
        return bool(labels) and all(generic.fullmatch(label) for label in labels)

    @staticmethod
    def _question_fingerprint(value: Any) -> str:
        """忽略标点与空白比较问题正文，阻止模型只换维度 ID 后重复提问。"""
        return "".join(
            character.lower()
            for character in str(value or "")
            if character.isalnum()
        )

    @classmethod
    def _repeats_answered_question(
        cls,
        question: dict[str, Any],
        decisions: list[dict[str, Any]],
    ) -> bool:
        fingerprint = cls._question_fingerprint(question.get("prompt"))
        if not fingerprint:
            return False
        return any(
            cls._question_fingerprint(decision.get("question")) == fingerprint
            for decision in decisions
            if isinstance(decision, dict)
        )

    @classmethod
    def _normalize_directions(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not 2 <= len(value) <= 4:
            raise BrainstormGenerationError("模型需要推荐 2 到 4 个继续脑暴方向")
        directions: list[dict[str, Any]] = []
        recommended_seen = False
        used_ids: set[str] = set()
        for index, raw_direction in enumerate(value):
            if not isinstance(raw_direction, dict):
                raise BrainstormGenerationError("继续脑暴方向格式无效")
            label = str(raw_direction.get("label") or "").strip()[:120]
            description = str(raw_direction.get("description") or "").strip()[:300]
            if not label or not description:
                raise BrainstormGenerationError("继续脑暴方向缺少说明")
            raw_id = str(raw_direction.get("id") or f"direction_{index + 1}")
            direction_id = cls._safe_id(raw_id, fallback=f"direction_{index + 1}")
            while direction_id in used_ids:
                direction_id = f"{direction_id}_{index + 1}"
            used_ids.add(direction_id)
            recommended = cls._coerce_bool(raw_direction.get("recommended")) and not recommended_seen
            if recommended:
                recommended_seen = True
            directions.append(
                {
                    "id": direction_id,
                    "label": label,
                    "description": description,
                    "recommended": recommended,
                }
            )
        if recommended_seen:
            directions.sort(key=lambda item: not item["recommended"])
        else:
            directions[0]["recommended"] = True
        return directions

    @classmethod
    def _normalize_question(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        question_type = str(value.get("type") or "single_choice").strip()
        if question_type not in {"single_choice", "multi_choice"}:
            raise BrainstormGenerationError("模型只能返回 single_choice 或 multi_choice 题型")
        prompt = str(value.get("prompt") or "").strip()
        dimension = str(value.get("dimension") or "继续脑暴").strip()
        dimension_id = cls._safe_id(
            str(value.get("dimension_id") or dimension),
            fallback="dynamic_dimension",
        )
        why_now = str(value.get("why_now") or "").strip()
        if len(prompt) < 4 or len(prompt) > 500 or not why_now:
            raise BrainstormGenerationError("模型返回的问题不完整")
        options: list[dict[str, Any]] = []
        raw_options = value.get("options") if isinstance(value.get("options"), list) else []
        if not 2 <= len(raw_options) <= 5:
            raise BrainstormGenerationError("动态选择题需要 2 到 5 个选项")
        recommended_seen = False
        used_ids: set[str] = set()
        for index, raw_option in enumerate(raw_options):
            if not isinstance(raw_option, dict):
                raise BrainstormGenerationError("动态选项格式无效")
            label = str(raw_option.get("label") or "").strip()[:120]
            if not label:
                raise BrainstormGenerationError("动态选项缺少标签")
            description = cls._normalize_option_description(raw_option, label)
            raw_id = str(raw_option.get("id") or "").strip()
            if not raw_id:
                raise BrainstormGenerationError("动态选项缺少 id")
            option_id = cls._safe_id(raw_id, fallback=f"option_{index + 1}")
            if option_id in used_ids:
                raise BrainstormGenerationError("动态选项 id 重复")
            used_ids.add(option_id)
            # Recommendation is presentation metadata rather than substantive
            # question content. Local models occasionally omit it, mark several
            # options, or serialize JSON booleans as strings. Keep the otherwise
            # valid choices and deterministically retain the first recommendation;
            # when none is present, the prompt contract makes the first option the
            # intended default.
            recommended = cls._coerce_bool(raw_option.get("recommended")) and not recommended_seen
            if recommended:
                recommended_seen = True
            options.append(
                {
                    "id": option_id,
                    "label": label,
                    "description": description,
                    "recommended": recommended,
                }
            )
        if recommended_seen:
            options.sort(key=lambda item: not item["recommended"])
        else:
            options[0]["recommended"] = True
        question_id = f"q_{uuid4().hex[:12]}"
        return {
            "id": question_id,
            "dimension_id": dimension_id,
            "dimension": dimension[:100],
            "type": question_type,
            "prompt": prompt,
            "why_now": why_now[:500],
            "required": bool(value.get("required", True)),
            # 用户必须始终能跳出模型给出的候选集合，避免动态选项变成新的固定限制。
            "allow_custom": True,
            "options": options,
            "answer_template": str(value.get("answer_template") or "补充你的具体考虑。")[:300],
        }

    @staticmethod
    def _normalize_option_description(
        raw_option: dict[str, Any],
        label: str,
    ) -> str:
        """兼容小模型的说明字段漂移，并为缺失的展示文案提供安全降级。"""
        for key in ("description", "tradeoff", "reason", "impact", "rationale"):
            description = str(raw_option.get(key) or "").strip()
            if description:
                return description[:300]
        return (
            f"选择“{label}”会作为后续方案的方向依据；"
            "具体收益、约束与代价仍需结合后续回答校验。"
        )[:300]

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        text = raw.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise BrainstormGenerationError("模型返回内容不是 JSON 对象")
        return parsed

    @classmethod
    def _output_shape_diagnostic(cls, raw: str) -> dict[str, Any]:
        """只记录结构元数据，不把脑暴问题或选项正文写入日志。"""
        diagnostic: dict[str, Any] = {
            "status": "unparseable",
            "question_type": "unknown",
            "option_count": -1,
        }
        if not raw.strip():
            return diagnostic
        try:
            parsed = cls._parse_json_object(raw)
        except (BrainstormGenerationError, json.JSONDecodeError):
            return diagnostic
        diagnostic["status"] = str(parsed.get("status") or "missing")[:32]
        question = parsed.get("question")
        if not isinstance(question, dict):
            return diagnostic
        diagnostic["question_type"] = str(question.get("type") or "missing")[:32]
        options = question.get("options")
        diagnostic["option_count"] = len(options) if isinstance(options, list) else -1
        return diagnostic

    @staticmethod
    def _safe_id(value: str, *, fallback: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip()).strip("_")
        return normalized[:64] or fallback

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes"}
        if isinstance(value, (int, float)):
            return value == 1
        return False

    @staticmethod
    def _clean_string_list(value: Any, *, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:300] for item in value if str(item).strip()][:limit]
