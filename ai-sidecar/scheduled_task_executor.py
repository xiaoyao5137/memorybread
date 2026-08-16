"""
定时任务执行器

负责：
1. 从数据库查询 knowledge 条目（已是精炼的工作片段）
2. 根据 token 预算决定是否压缩上下文
3. 调用 LLM 按用户指令生成报告
4. 将结果写入 task_executions 表
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Optional

from energy_policy import EnergyPolicy

logger = logging.getLogger(__name__)

# 内置场景模板（UI 展示用，执行时直接用 user_instruction）
BUILTIN_TEMPLATES = [
    # ── 工作总结类 ──────────────────────────────────────────────────────────
    {
        "id": "daily_journal",
        "name": "生成昨日工作日记",
        "cron": "0 9 * * *",
        "category": "工作总结",
        "user_instruction": (
            "请根据昨天的工作记录，生成高度浓缩的工作日记。要求：\n"
            "默认使用简体中文；即使原始记录主要为英文，也使用中文叙述，产品名和代码标识符可保留原文。\n"
            "1. 【今日产出】最多 4 条，只写真正完成的成果、修复、决策或交付，每条不超过 45 个中文字符。\n"
            "2. 【问题与解决】最多 2 条，仅记录已解决问题或明确结论；没有则写「无」。\n"
            "3. 不要生成明日计划、后续计划、待办或建议，只记录当天已经发生并有依据的事实。\n"
            "过滤掉：浏览、阅读、搜索、应用切换、会议过程、配置环境、失败尝试等流水账。"
        ),
    },
    {
        "id": "weekly_report",
        "name": "生成上周工作周记",
        "cron": "0 9 * * 1",
        "category": "工作总结",
        "user_instruction": (
            "请根据上周时间线记录，生成一份工作周记。要求：\n"
            "1. 【本周核心产出】按重要性排列，每条说明：做了什么（结果）、为什么重要（价值/影响），有量化数据的必须写出。\n"
            "2. 【项目进展】当前各项目的阶段状态，用「已完成 / 进行中 / 待启动」标注。\n"
            "3. 【下周计划】每条是具体可交付目标，不写「继续推进」「调研」等模糊描述。\n"
            "4. 【风险/阻塞】（如有）描述具体问题和影响范围。\n"
            "过滤掉：阅读文档、安装依赖、无结论的调研等活动流水账。"
        ),
    },
    {
        "id": "monthly_summary",
        "name": "生成上月工作月记",
        "cron": "0 9 1 * *",
        "category": "工作总结",
        "user_instruction": (
            "请根据上月时间线记录，生成工作月记。要求：\n"
            "1. 【主要成果】列出上月最重要的 3-5 项交付物，每项说明其业务价值或影响，有数据的写数据。\n"
            "2. 【时间分配】按项目/类别分析时间投入占比，指出是否与优先级匹配。\n"
            "3. 【效率亮点与问题】各一条，基于事实而非感受。\n"
            "4. 【下月目标】具体、可验收的目标，不写方向性描述。\n"
            "过滤掉：活动流水、工具配置、无结论的探索等低价值记录。"
        ),
    },
    {
        "id": "project_weekly_report",
        "name": "生成项目周报",
        "cron": "0 18 * * 5",
        "category": "工作总结",
        "user_instruction": (
            "请根据本周项目相关工作记录，生成项目周报。要求：\n"
            "1. 【本周核心产出】按项目/专项列出完成结果与业务价值，优先写可验证数字。\n"
            "2. 【项目进展】按「已完成 / 进行中 / 待启动」标注状态，并说明关键里程碑。\n"
            "3. 若涉及 OKR/KPI/专项，必须提取并呈现可验证的量化进展。\n"
            "4. 【下周计划】仅写可验收交付项。\n"
            "5. 【风险/阻塞】写明影响范围与依赖。"
        ),
    },
    # ── 学习成长类 ──────────────────────────────────────────────────────────
    {
        "id": "daily_learning",
        "name": "每日学习笔记",
        "cron": "0 21 * * *",
        "category": "学习成长",
        "user_instruction": "请整理今天浏览的技术文档、代码、文章，提取关键知识点，生成学习笔记。重点记录新学到的概念、技术细节和待深入研究的方向。",
    },
    {
        "id": "tech_weekly",
        "name": "个人技术周刊",
        "cron": "0 10 * * 0",
        "category": "学习成长",
        "user_instruction": "请汇总本周接触的新技术、工具、最佳实践，生成个人技术周刊。包括：技术动态、学习收获、值得分享的内容。",
    },
    # ── 文档管理类 ──────────────────────────────────────────────────────────
    {
        "id": "doc_update_reminder",
        "name": "文档更新提醒",
        "cron": "0 9 * * 1",
        "category": "文档管理",
        "user_instruction": "请检查上周修改过的项目文件和代码，列出需要同步更新文档的地方，生成文档待办清单。",
    },
    {
        "id": "code_review_summary",
        "name": "每日代码审查摘要",
        "cron": "0 17 * * 1-5",
        "category": "文档管理",
        "user_instruction": "请总结今天编写和修改的代码，分析代码质量、潜在问题和改进点，生成代码审查报告。",
    },
    # ── 效率分析类 ──────────────────────────────────────────────────────────
    {
        "id": "time_analysis",
        "name": "每周时间使用分析",
        "cron": "0 20 * * 0",
        "category": "效率分析",
        "user_instruction": "请分析本周在各个应用和任务上的时间分配，识别时间浪费点和高效时段，提供时间管理优化建议。",
    },
    {
        "id": "focus_report",
        "name": "每日专注力报告",
        "cron": "0 19 * * 1-5",
        "category": "效率分析",
        "user_instruction": "请分析今天的工作模式，识别高效时段和分心时段，统计深度工作时间，生成专注力报告。",
    },
    # ── 目标跟踪类 ──────────────────────────────────────────────────────────
    {
        "id": "okr_tracking",
        "name": "OKR 进度跟踪",
        "cron": "0 12 * * 3",
        "category": "目标跟踪",
        "user_instruction": "请根据本周工作记录，评估各项目标的推进情况，识别风险和阻碍，生成 OKR 进度报告。",
    },
    # ── 协作沟通类 ──────────────────────────────────────────────────────────
    {
        "id": "weekly_qa",
        "name": "每周答疑汇总",
        "cron": "0 17 * * 5",
        "category": "协作沟通",
        "user_instruction": "请整理本周在各个沟通工具中回答的问题，按主题分类汇总，生成 FAQ 文档，方便后续复用。",
    },
    {
        "id": "meeting_minutes",
        "name": "每日会议纪要",
        "cron": "0 18 * * 1-5",
        "category": "协作沟通",
        "user_instruction": "请根据今天的会议记录和讨论内容，生成会议纪要。包括：决策事项、待办任务、责任人和截止时间。",
    },
    # ── 运维值班类 ──────────────────────────────────────────────────────────
    {
        "id": "oncall_summary",
        "name": "On-call 值班总结",
        "cron": "0 9 * * 1",
        "category": "运维值班",
        "user_instruction": "请总结值班期间处理的告警、事故、用户问题，分析根因，记录解决方案，生成值班交接报告。",
    },
    {
        "id": "system_health",
        "name": "系统健康周报",
        "cron": "0 9 * * 1",
        "category": "运维值班",
        "user_instruction": "请分析上周的系统日志、错误信息、性能指标，识别潜在风险和异常趋势，生成系统健康报告。",
    },
    # ── 邮件文档类 ──────────────────────────────────────────────────────────
    {
        "id": "email_todo",
        "name": "邮件待办提取",
        "cron": "0 9 * * 1-5",
        "category": "邮件文档",
        "user_instruction": "请从昨天的邮件往来中提取需要跟进的事项、待回复的问题，生成今日邮件待办清单，按优先级排序。",
    },
    {
        "id": "doc_changelog",
        "name": "文档变更日志",
        "cron": "0 16 * * 5",
        "category": "邮件文档",
        "user_instruction": "请追踪本周修改的所有文档，生成变更日志，包括修改内容摘要和版本说明。",
    },
]

# 每条 knowledge 的平均 token 估算（overview + details）
AVG_TOKENS_PER_KNOWLEDGE = 300
# 直接全量使用的 token 上限
FULL_CONTEXT_TOKEN_LIMIT = 24000
# 只用 overview 的 token 上限
OVERVIEW_ONLY_TOKEN_LIMIT = 60000
# 定时 daily_journal 每次只生成昨天，历史断档交给充电空闲时的一天一补 worker。
DAILY_DIARY_CATCHUP_DAYS = 1
# 推理队列空闲时，自动从最近历史日期中补齐缺失的 daily 日记。一次只处理一天，避免后台长时间占用 LLM。
IDLE_DIARY_BACKFILL_LOOKBACK_DAYS = int(os.getenv("DIARY_BACKFILL_LOOKBACK_DAYS", "30"))
DAILY_DIARY_CONTEXT_MAX_ITEMS = int(os.getenv("DAILY_DIARY_CONTEXT_MAX_ITEMS", "24"))
DAILY_DIARY_TIMELINE_MAX_ITEMS = int(os.getenv("DAILY_DIARY_TIMELINE_MAX_ITEMS", "12"))
DIARY_ITEM_SUMMARY_MAX_CHARS = 120
DIARY_DEFAULT_LANGUAGE = "zh-CN"
DIARY_LANGUAGE_LABELS = {
    "zh-CN": "简体中文",
    "zh-TW": "繁体中文",
    "en": "英文",
}

# 创作 Agent / 工具的 @ 提及标签，与 desktop-ui CREATION_SKILL_*_OPTIONS 保持一致，
# 用于解析任务指令中的提及，避免把工具/Agent 名误判为技能标题。
CREATION_MENTION_AGENT_LABELS = [
    "行业调研 Agent",
    "数据分析 Agent",
    "方案设计 Agent",
    "章节设计 Agent",
    "文档撰写 Agent",
    "去 AI 味 Agent",
    "细节润色 Agent",
    "表格润色 Agent",
    "字体润色 Agent",
    "图片润色 Agent",
    "质量审校 Agent",
]
CREATION_MENTION_TOOL_LABELS = [
    "记忆搜索",
    "互联网检索",
    "数据检索",
    "网页爬取",
    "GitHub 检索",
    "PlantUML 画图",
]
MENTION_TOKEN_PATTERN = re.compile(r"@([A-Za-z0-9_\u4e00-\u9fa5-]{2,40})")
MENTION_NAME_CHAR_PATTERN = re.compile(r"[A-Za-z0-9_\u4e00-\u9fa5-]")


class NotificationDeliveryRejected(Exception):
    """消息渠道返回了 HTTP 成功，但业务状态明确拒绝投递。"""


class TaskExecutor:
    """定时任务执行器"""

    def __init__(
        self,
        db_path: str,
        *,
        energy_policy: Optional[EnergyPolicy] = None,
    ):
        self.db_path = db_path
        self.energy_policy = energy_policy or EnergyPolicy(db_path)
        self._llm_client = None

    def _get_llm_client(self):
        if self._llm_client is None:
            from ollama import Client
            # Ollama 是固定的本机服务。macOS 系统代理可能被 httpx 自动继承，
            # 导致 127.0.0.1 请求错误地走代理并出现 Connection refused。
            self._llm_client = Client(host="http://127.0.0.1:11434", trust_env=False)
        return self._llm_client

    # ─────────────────────────────────────────────────────────────────────────
    # 核心执行入口
    # ─────────────────────────────────────────────────────────────────────────

    def execute_task(self, task_id: int) -> dict:
        """
        执行一个定时任务，返回执行结果。
        由 Rust 调度器通过 HTTP 调用触发。
        """
        started_at = int(time.time() * 1000)
        conn = sqlite3.connect(self.db_path)

        # 1. 读取任务定义
        task = self._get_task(conn, task_id)
        if not task:
            conn.close()
            return {"status": "failed", "error": f"任务 {task_id} 不存在"}

        diary_period = self._detect_diary_period(task)
        if diary_period:
            profile = self.energy_policy.current_profile()
            if not profile.allow_diary:
                conn.close()
                logger.info(
                    "日记任务延迟到充电模式: task_id=%s mode=%s battery=%s",
                    task_id,
                    profile.mode,
                    profile.battery_percent,
                )
                return {
                    "status": "deferred",
                    "reason": "waiting_for_external_power",
                    "energy_mode": profile.mode,
                    "battery_percent": profile.battery_percent,
                }

        # 2. 创建执行记录（running 状态）
        exec_id = self._create_execution(conn, task_id, started_at)

        try:
            creation_history_id = None
            if diary_period:
                diary_result = self._execute_diary_task(conn, task, diary_period)
                knowledge_count = diary_result["source_count"]
                token_estimate = diary_result["token_estimate"]
                result_text = diary_result["result_text"]
            else:
                result_text = None
                knowledge_count = 0
                token_estimate = 0
                fallback_note = None
                if (task.get("executor_kind") or "consult") == "creation":
                    try:
                        creation_result = self._execute_creation_task(task, exec_id)
                        result_text = creation_result["result_text"]
                        creation_history_id = creation_result.get("creation_history_id")
                    except Exception as creation_error:
                        # 创作智能体连接失败或执行失败时，回退现有 knowledge+LLM 咨询路径。
                        logger.warning(
                            "任务 %s 创作智能体执行失败，回退咨询智能体: %s",
                            task_id,
                            creation_error,
                        )
                        fallback_note = (
                            f"[创作智能体执行失败（{creation_error}），已回退咨询智能体生成结果]"
                        )

                if result_text is None:
                    # 3. 查询 knowledge 上下文
                    knowledge_list = self._query_knowledge(conn, task['user_instruction'])
                    knowledge_count = len(knowledge_list)
                    is_weekly_report = self._is_weekly_report_instruction(task['user_instruction'])
                    kpi_mode = is_weekly_report and self._is_kpi_mode_instruction(task['user_instruction'])

                    # 4. 构建上下文（根据 token 预算决定压缩策略）
                    context_text, token_estimate = self._build_context(
                        knowledge_list,
                        user_instruction=task['user_instruction'],
                    )

                    # 5. 调用 LLM 生成报告
                    result_text = self._llm_generate(
                        user_instruction=task['user_instruction'],
                        context=context_text,
                        task_id=task_id,
                        is_weekly_report=is_weekly_report,
                        kpi_mode=kpi_mode,
                    )
                    if fallback_note:
                        result_text = f"{fallback_note}\n\n{result_text}"

            # 6. 更新执行记录为成功
            completed_at = int(time.time() * 1000)
            success_fields = {
                "status": "success",
                "completed_at": completed_at,
                "result_text": result_text,
                "knowledge_count": knowledge_count,
                "token_used": token_estimate,
                "latency_ms": completed_at - started_at,
            }
            if creation_history_id is not None:
                success_fields["creation_history_id"] = creation_history_id
            self._update_execution(conn, exec_id, success_fields)

            # 7. 更新任务统计
            self._update_task_stats(conn, task_id, "success", completed_at)

            # 8. 将成功结果投递到用户本地配置的消息渠道。投递失败不改变任务结果。
            notification_deliveries = self._deliver_task_result(
                conn,
                task,
                exec_id,
                result_text,
                completed_at,
            )

            conn.close()
            logger.info(f"✅ 任务 {task_id} 执行成功，耗时 {completed_at - started_at}ms")
            return {
                "status": "success",
                "exec_id": exec_id,
                "result": result_text,
                "notification_deliveries": notification_deliveries,
            }

        except Exception as e:
            completed_at = int(time.time() * 1000)
            self._update_execution(conn, exec_id, {
                "status": "failed",
                "completed_at": completed_at,
                "error_message": str(e),
                "latency_ms": completed_at - started_at,
            })
            self._update_task_stats(conn, task_id, "failed", completed_at)
            conn.close()
            logger.error(f"❌ 任务 {task_id} 执行失败: {e}")
            return {"status": "failed", "error": str(e)}

    # ─────────────────────────────────────────────────────────────────────────
    # Knowledge 查询与上下文构建
    # ─────────────────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────────────────
    # 日记生成
    # ─────────────────────────────────────────────────────────────────────────

    def _detect_diary_period(self, task: dict) -> Optional[str]:
        template_id = (task.get("template_id") or "").strip()
        text = " ".join(
            str(task.get(k) or "") for k in ("name", "user_instruction", "template_id")
        ).lower()

        if template_id == "daily_journal" or "工作日记" in text or "daily journal" in text:
            return "daily"
        if template_id == "weekly_report" or "周记" in text or "工作周报" in text:
            return "weekly"
        if template_id == "monthly_summary" or "月记" in text or "月度工作总结" in text:
            return "monthly"
        return None

    def _execute_diary_task(self, conn: sqlite3.Connection, task: dict, period_type: str) -> dict:
        self._ensure_diaries_table(conn)
        if period_type == "daily":
            return self._execute_daily_diary_catchup(conn, task)

        output_language = self._resolve_diary_output_language(task.get("user_instruction") or "")
        period_start, period_end, diary_date = self._resolve_diary_period(period_type)
        source_items = self._query_timelines_for_period(conn, period_start, period_end)
        context_text, token_estimate = self._build_daily_diary_context(
            source_items,
            output_language=output_language,
        )
        source_timeline_ids = [
            int(item["id"]) for item in source_items if item.get("id") is not None
        ]
        source_diary_ids: list[int] = []

        result_text = self._llm_generate(
            user_instruction=self._diary_instruction(
                period_type,
                task["user_instruction"],
                output_language=output_language,
            ),
            context=context_text or "无可用工作记录。",
            task_id=task["id"],
            is_weekly_report=(period_type == "weekly"),
            kpi_mode=False,
            output_language=output_language,
            concise=True,
        )

        content = self._build_diary_content(
            period_type=period_type,
            period_start=period_start,
            period_end=period_end,
            diary_date=diary_date,
            markdown=result_text,
            source_items=source_items,
            output_language=output_language,
        )
        self._upsert_diary(
            conn=conn,
            period_type=period_type,
            period_start=period_start,
            period_end=period_end,
            diary_date=diary_date,
            content=content,
            source_timeline_ids=source_timeline_ids,
            source_diary_ids=source_diary_ids,
        )

        return {
            "result_text": result_text,
            "source_count": len(source_items),
            "token_estimate": token_estimate,
        }

    def _execute_daily_diary_catchup(self, conn: sqlite3.Connection, task: dict) -> dict:
        target_dates = self._resolve_recent_daily_dates(days=DAILY_DIARY_CATCHUP_DAYS)[-1:]
        newest_target = target_dates[-1] if target_dates else None
        result_blocks: list[str] = []
        skipped_dates: list[str] = []
        total_source_count = 0
        total_token_estimate = 0

        for diary_date in target_dates:
            existing = self._get_diary_meta(conn, "daily", diary_date)
            if existing and not existing["is_system_generated"]:
                skipped_dates.append(f"{diary_date}: 用户已编辑")
                continue

            source_items = self._query_timelines_for_date(conn, diary_date)
            if not source_items and existing is None and diary_date != newest_target:
                skipped_dates.append(f"{diary_date}: 无可用工作记录")
                continue

            result = self._execute_single_daily_diary(conn, task, diary_date, source_items)
            total_source_count += result["source_count"]
            total_token_estimate += result["token_estimate"]
            result_blocks.append(f"### {diary_date}\n{result['result_text']}")

        if result_blocks:
            result_text = "\n\n".join(result_blocks)
            if skipped_dates:
                result_text += "\n\n已跳过：\n" + "\n".join(f"- {item}" for item in skipped_dates)
        else:
            result_text = "最近工作日记无需更新。"
            if skipped_dates:
                result_text += "\n" + "\n".join(f"- {item}" for item in skipped_dates)

        return {
            "result_text": result_text,
            "source_count": total_source_count,
            "token_estimate": total_token_estimate,
        }

    def _execute_single_daily_diary(
        self,
        conn: sqlite3.Connection,
        task: dict,
        diary_date: str,
        source_items: Optional[list[dict]] = None,
    ) -> dict:
        source_items = source_items if source_items is not None else self._query_timelines_for_date(conn, diary_date)
        output_language = self._resolve_diary_output_language(task.get("user_instruction") or "")
        context_text, token_estimate = self._build_daily_diary_context(
            source_items,
            output_language=output_language,
        )
        source_timeline_ids = [int(item["id"]) for item in source_items if item.get("id") is not None]
        result_text = self._llm_generate(
            user_instruction=self._daily_diary_instruction(
                diary_date,
                output_language=output_language,
            ),
            context=context_text or "无可用工作记录。",
            task_id=task["id"],
            is_weekly_report=False,
            kpi_mode=False,
            output_language=output_language,
            concise=True,
        )
        result_text = self._normalize_daily_diary_markdown(
            result_text,
            output_language=output_language,
        )

        content = self._build_diary_content(
            period_type="daily",
            period_start=diary_date,
            period_end=diary_date,
            diary_date=diary_date,
            markdown=result_text,
            source_items=source_items,
            output_language=output_language,
        )
        self._upsert_diary(
            conn=conn,
            period_type="daily",
            period_start=diary_date,
            period_end=diary_date,
            diary_date=diary_date,
            content=content,
            source_timeline_ids=source_timeline_ids,
            source_diary_ids=[],
        )

        return {
            "result_text": result_text,
            "source_count": len(source_items),
            "token_estimate": token_estimate,
        }

    @staticmethod
    def _resolve_recent_daily_dates(
        today: Optional[date] = None,
        days: int = DAILY_DIARY_CATCHUP_DAYS,
    ) -> list[str]:
        today = today or date.today()
        day_count = max(1, int(days))
        return [
            (today - timedelta(days=offset)).isoformat()
            for offset in range(day_count, 0, -1)
        ]

    def execute_idle_diary_backfill_once(
        self,
        lookback_days: int = IDLE_DIARY_BACKFILL_LOOKBACK_DAYS,
        today: Optional[date] = None,
    ) -> dict:
        """推理队列空闲时调用：补齐一个缺失的历史 daily 日记。"""
        profile = self.energy_policy.current_profile()
        if not profile.allow_diary:
            return {
                "status": "deferred",
                "reason": "waiting_for_external_power",
                "energy_mode": profile.mode,
                "battery_percent": profile.battery_percent,
            }

        started_at = int(time.time() * 1000)
        conn = sqlite3.connect(self.db_path)
        self._ensure_diaries_table(conn)

        task = self._get_daily_diary_task(conn)
        if not task:
            conn.close()
            return {"status": "idle", "reason": "daily_diary_task_not_configured"}

        candidate = self._find_idle_diary_backfill_candidate(
            conn,
            lookback_days=lookback_days,
            today=today,
        )
        if not candidate:
            conn.close()
            return {"status": "idle", "reason": "no_missing_historical_daily_diary"}

        diary_date = candidate["diary_date"]
        source_items = candidate["source_items"]
        exec_id = self._create_execution(conn, task["id"], started_at) if task.get("id") else None

        try:
            result = self._execute_single_daily_diary(conn, task, diary_date, source_items)
            result_text = f"### {diary_date}\n{result['result_text']}"
            completed_at = int(time.time() * 1000)
            if exec_id is not None:
                self._update_execution(conn, exec_id, {
                    "status": "success",
                    "completed_at": completed_at,
                    "result_text": result_text,
                    "knowledge_count": result["source_count"],
                    "token_used": result["token_estimate"],
                    "latency_ms": completed_at - started_at,
                })
                self._update_task_stats(conn, task["id"], "success", completed_at)
            conn.close()
            logger.info(
                "✅ 闲时补齐历史日记成功: date=%s source_count=%s",
                diary_date,
                result["source_count"],
            )
            return {
                "status": "success",
                "diary_date": diary_date,
                "source_count": result["source_count"],
                "token_estimate": result["token_estimate"],
                "exec_id": exec_id,
            }
        except Exception as e:
            completed_at = int(time.time() * 1000)
            if exec_id is not None:
                self._update_execution(conn, exec_id, {
                    "status": "failed",
                    "completed_at": completed_at,
                    "error_message": str(e),
                    "latency_ms": completed_at - started_at,
                })
                self._update_task_stats(conn, task["id"], "failed", completed_at)
            conn.close()
            logger.error("❌ 闲时补齐历史日记失败: date=%s error=%s", diary_date, e)
            return {"status": "failed", "diary_date": diary_date, "error": str(e)}

    def _get_daily_diary_task(self, conn: sqlite3.Connection) -> Optional[dict]:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT id, name, user_instruction, cron_expression, template_id
                FROM scheduled_tasks
                WHERE enabled = 1
                  AND (
                    template_id = 'daily_journal'
                    OR name LIKE '%工作日记%'
                    OR user_instruction LIKE '%工作日记%'
                    OR lower(user_instruction) LIKE '%daily journal%'
                  )
                ORDER BY
                  CASE WHEN template_id = 'daily_journal' THEN 0 ELSE 1 END,
                  id ASC
                LIMIT 1
                """
            )
        except sqlite3.OperationalError as e:
            logger.warning("读取 daily_journal 任务失败: %s", e)
            return None
        row = cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "name": row[1],
                "user_instruction": row[2],
                "cron_expression": row[3],
                "template_id": row[4],
            }
        return self._create_default_daily_diary_task(conn)

    def _create_default_daily_diary_task(self, conn: sqlite3.Connection) -> Optional[dict]:
        template = self._builtin_template("daily_journal")
        if not template:
            return None

        now_ms = int(time.time() * 1000)
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO scheduled_tasks (
                        name, user_instruction, cron_expression, template_id, is_builtin,
                        enabled, run_count, next_run_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 1, 1, 0, 0, ?, ?)
                    """,
                    (
                        template["name"],
                        template["user_instruction"],
                        template["cron"],
                        template["id"],
                        now_ms,
                        now_ms,
                    ),
                )
            except sqlite3.OperationalError:
                cursor.execute(
                    """
                    INSERT INTO scheduled_tasks (
                        name, user_instruction, cron_expression, template_id,
                        enabled, run_count, next_run_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 1, 0, 0, ?, ?)
                    """,
                    (
                        template["name"],
                        template["user_instruction"],
                        template["cron"],
                        template["id"],
                        now_ms,
                        now_ms,
                    ),
                )
            conn.commit()
        except sqlite3.OperationalError as e:
            logger.warning("创建默认 daily_journal 任务失败: %s", e)
            return None

        task_id = cursor.lastrowid
        logger.info("已创建默认 daily_journal 任务用于日记补齐: id=%s", task_id)
        return {
            "id": task_id,
            "name": template["name"],
            "user_instruction": template["user_instruction"],
            "cron_expression": template["cron"],
            "template_id": template["id"],
        }

    @staticmethod
    def _builtin_template(template_id: str) -> Optional[dict]:
        for template in BUILTIN_TEMPLATES:
            if template.get("id") == template_id:
                return template
        return None

    def _find_idle_diary_backfill_candidate(
        self,
        conn: sqlite3.Connection,
        lookback_days: int = IDLE_DIARY_BACKFILL_LOOKBACK_DAYS,
        today: Optional[date] = None,
    ) -> Optional[dict]:
        """返回最近一个有时间线但缺 daily 日记的历史日期。"""
        today = today or date.today()
        day_count = max(1, int(lookback_days))
        start_day = today - timedelta(days=day_count)
        start_ms, _ = self._date_range_ms(start_day.isoformat())
        end_ms, _ = self._date_range_ms(today.isoformat())

        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                WITH timeline_days AS (
                    SELECT
                        date(
                            COALESCE(start_time, event_time_start, observed_at, created_at_ms, 0) / 1000,
                            'unixepoch',
                            'localtime'
                        ) AS diary_date,
                        COUNT(*) AS source_count
                    FROM timelines
                    WHERE COALESCE(start_time, event_time_start, observed_at, created_at_ms, 0) >= ?
                      AND COALESCE(start_time, event_time_start, observed_at, created_at_ms, 0) < ?
                      AND COALESCE(is_self_generated, 0) = 0
                      AND (
                          COALESCE(overview, '') != ''
                          OR COALESCE(details, '') != ''
                      )
                    GROUP BY diary_date
                )
                SELECT td.diary_date, td.source_count
                FROM timeline_days td
                LEFT JOIN diaries d
                  ON d.period_type = 'daily'
                 AND d.diary_date = td.diary_date
                WHERE d.id IS NULL
                ORDER BY td.diary_date DESC
                LIMIT 1
                """,
                (start_ms, end_ms),
            )
        except sqlite3.OperationalError as e:
            logger.warning("查找历史日记缺口失败: %s", e)
            return None

        row = cursor.fetchone()
        if not row:
            return None

        diary_date = row[0]
        source_items = self._query_timelines_for_date(conn, diary_date)
        if not source_items:
            return None

        return {
            "diary_date": diary_date,
            "source_count": row[1],
            "source_items": source_items,
        }

    @staticmethod
    def _resolve_diary_period(period_type: str, today: Optional[date] = None) -> tuple[str, str, str]:
        today = today or date.today()
        if period_type == "daily":
            target = today - timedelta(days=1)
            iso = target.isoformat()
            return iso, iso, iso

        if period_type == "weekly":
            end = today - timedelta(days=today.weekday() + 1)
            start = end - timedelta(days=6)
            return start.isoformat(), end.isoformat(), end.isoformat()

        if period_type == "monthly":
            first_this_month = today.replace(day=1)
            end = first_this_month - timedelta(days=1)
            start = end.replace(day=1)
            return start.isoformat(), end.isoformat(), end.isoformat()

        raise ValueError(f"不支持的日记周期: {period_type}")

    @staticmethod
    def _date_range_ms(day: str) -> tuple[int, int]:
        d = date.fromisoformat(day)
        start_dt = datetime.combine(d, datetime_time.min)
        end_dt = start_dt + timedelta(days=1)
        return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)

    def _query_timelines_for_date(self, conn: sqlite3.Connection, diary_date: str) -> list[dict]:
        start_ms, end_ms = self._date_range_ms(diary_date)
        return self._query_timelines_between_ms(conn, start_ms, end_ms)

    def _query_timelines_for_period(
        self,
        conn: sqlite3.Connection,
        period_start: str,
        period_end: str,
    ) -> list[dict]:
        start_ms, _ = self._date_range_ms(period_start)
        _, end_ms = self._date_range_ms(period_end)
        return self._query_timelines_between_ms(conn, start_ms, end_ms)

    def _query_timelines_between_ms(
        self,
        conn: sqlite3.Connection,
        start_ms: int,
        end_ms: int,
    ) -> list[dict]:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, capture_id, overview, details, category, importance,
                   start_time, end_time, duration_minutes,
                   frag_app_name, entities,
                   user_verified, observed_at, event_time_start, event_time_end,
                   history_view, content_origin, activity_type, is_self_generated,
                   evidence_strength, created_at_ms
            FROM timelines
            WHERE COALESCE(start_time, event_time_start, observed_at, created_at_ms, 0) >= ?
              AND COALESCE(start_time, event_time_start, observed_at, created_at_ms, 0) < ?
              AND COALESCE(is_self_generated, 0) = 0
            ORDER BY COALESCE(start_time, event_time_start, observed_at, created_at_ms, 0) ASC, id ASC
            LIMIT 1000
            """,
            (start_ms, end_ms),
        )

        rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "capture_id": r[1],
                "overview": r[2] or "",
                "details": r[3] or "",
                "category": r[4] or "其他",
                "importance": r[5] or 3,
                "start_time": r[6],
                "end_time": r[7],
                "duration_minutes": r[8],
                "app_name": r[9],
                "entities": json.loads(r[10]) if r[10] else [],
                "user_verified": bool(r[11]) if r[11] is not None else False,
                "observed_at": r[12],
                "event_time_start": r[13],
                "event_time_end": r[14],
                "history_view": bool(r[15]) if r[15] is not None else False,
                "content_origin": r[16],
                "activity_type": r[17],
                "is_self_generated": bool(r[18]) if r[18] is not None else False,
                "evidence_strength": r[19],
                "created_at": r[20],
            }
            for r in rows
        ]

    def _query_daily_diaries(
        self, conn: sqlite3.Connection, period_start: str, period_end: str
    ) -> list[dict]:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, diary_date, content
            FROM diaries
            WHERE period_type = 'daily'
              AND diary_date >= ?
              AND diary_date <= ?
            ORDER BY diary_date ASC, id ASC
            """,
            (period_start, period_end),
        )
        rows = cursor.fetchall()
        result = []
        for row in rows:
            content = self._parse_json_object(row[2])
            result.append({"id": row[0], "diary_date": row[1], "content": content})
        return result

    def _build_diary_rollup_context(self, diaries: list[dict]) -> str:
        blocks = []
        for diary in diaries:
            content = diary.get("content") or {}
            lines = [f"[{diary['diary_date']}] {content.get('title') or '工作日记'}"]
            for label, key in (
                ("产出", "work_outputs"),
                ("问题与解决", "problems_solved"),
            ):
                values = content.get(key) or []
                if values:
                    lines.append(f"{label}:")
                    lines.extend(f"- {item}" for item in values if item)
            markdown = content.get("markdown")
            if markdown and len(lines) == 1:
                lines.append(str(markdown)[:2000])
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _diary_instruction(
        self,
        period_type: str,
        user_instruction: str,
        output_language: str = DIARY_DEFAULT_LANGUAGE,
    ) -> str:
        language_label = self._diary_language_label(output_language)
        if period_type == "daily":
            return self._daily_diary_instruction(None, output_language=output_language)
        if period_type == "weekly":
            return (
                "请仅基于输入的时间线记录汇总周记，不要引入时间线之外的事实。\n"
                f"必须使用{language_label}输出，即使来源中包含大量英文。\n"
                "输出 Markdown，包含：## 本周核心产出、## 项目进展、## 下周计划、## 风险/阻塞。\n"
                "每个章节最多 5 条，每条只写一个结论，不复述每日过程。"
            )
        if period_type == "monthly":
            return (
                "请仅基于输入的时间线记录汇总月记，不要引入时间线之外的事实。\n"
                f"必须使用{language_label}输出，即使来源中包含大量英文。\n"
                "输出 Markdown，包含：## 主要成果、## 时间分配、## 效率亮点与问题、## 下月目标。\n"
                "每个章节最多 5 条，合并重复事项，不逐日复述。"
            )
        return user_instruction

    @classmethod
    def _daily_diary_instruction(
        cls,
        diary_date: Optional[str],
        output_language: str = DIARY_DEFAULT_LANGUAGE,
    ) -> str:
        date_label = f"{diary_date} " if diary_date else ""
        language_label = cls._diary_language_label(output_language)
        section_titles = [rule[0] for rule in cls._daily_diary_section_rules(output_language)]
        return (
            f"请基于{date_label}的时间线内容生成高度浓缩的工作日记，只保留当天最重要的产出、决策、修复和明确结论。\n"
            f"必须使用{language_label}输出；即使时间线、应用名或技术术语主要为英文，也要用{language_label}叙述，产品名和代码标识符可保留原文。\n"
            f"输出 Markdown，只包含：{'、'.join(f'## {title}' for title in section_titles)}。\n"
            "写作约束：\n"
            f"- 【{section_titles[0]}】最多 4 条，每条不超过 45 个字符，必须是结果，不写过程。\n"
            f"- 【{section_titles[1]}】最多 2 条，只写已解决问题或明确结论；没有则写「- 无」。\n"
            "- 不要生成明日计划、后续计划、待办或建议，不要推测未来事项。\n"
            "- 删除浏览、阅读、搜索、应用切换、会议过程、配置环境、失败尝试等流水账。"
        )

    def _build_diary_content(
        self,
        period_type: str,
        period_start: str,
        period_end: str,
        diary_date: str,
        markdown: str,
        source_items: list[dict],
        output_language: str = DIARY_DEFAULT_LANGUAGE,
    ) -> dict:
        title_suffix = {"daily": "工作日记", "weekly": "工作周记", "monthly": "工作月记"}.get(
            period_type, "工作日记"
        )
        title = f"{diary_date} {title_suffix}" if period_start == period_end else f"{period_start} 至 {period_end} {title_suffix}"

        content = {
            "schema_version": "diary.v1",
            "period_type": period_type,
            "title": title,
            "period_start": period_start,
            "period_end": period_end,
            "language": output_language,
            "summary": self._first_meaningful_line(markdown),
            "markdown": markdown,
            "work_outputs": self._extract_markdown_section_items(markdown, ("今日产出", "工作产出", "核心产出", "主要成果", "Today's Outcomes", "Work Outputs")),
            "problems_solved": self._extract_markdown_section_items(markdown, ("问题与解决", "风险/阻塞", "效率亮点与问题", "Problems & Resolutions", "Problems and Resolutions")),
            "timeline": [],
            "source_count": len(source_items),
        }

        if period_type != "daily":
            content["next_plan"] = self._extract_markdown_section_items(
                markdown,
                ("明日计划", "下周计划", "下月目标", "Next-day Plan", "Next Day Plan"),
            )

        if period_type == "daily":
            content["work_environment"] = self._infer_work_environment(source_items)
            content["timeline"] = [
                {
                    "timeline_id": item.get("id"),
                    "time": self._format_time(item.get("start_time")),
                    "duration_minutes": item.get("duration_minutes"),
                    "summary": self._compact_diary_item_text(item),
                    "category": item.get("category") or "其他",
                }
                for item in self._select_daily_diary_items(
                    source_items,
                    limit=DAILY_DIARY_TIMELINE_MAX_ITEMS,
                )
            ]
        else:
            content["source_dates"] = [item.get("diary_date") for item in source_items if item.get("diary_date")]

        return content

    def _build_daily_diary_context(
        self,
        source_items: list[dict],
        output_language: str = DIARY_DEFAULT_LANGUAGE,
    ) -> tuple[str, int]:
        if not source_items:
            return "", 0

        picked = self._select_daily_diary_items(source_items, limit=DAILY_DIARY_CONTEXT_MAX_ITEMS)
        blocks: list[str] = [self._build_work_environment_context(source_items, output_language)]
        for item in picked:
            ts = self._format_time(item.get("start_time"))
            duration = f"（{item['duration_minutes']}分钟）" if item.get("duration_minutes") else ""
            category = item.get("category") or "其他"
            importance = item.get("importance") or 0
            text = self._compact_diary_item_text(item)
            if text:
                blocks.append(f"[{ts}{duration}][{category}][重要性{importance}] {text}")

        if len(source_items) > len(picked):
            blocks.append(f"已从 {len(source_items)} 条时间线中筛选 {len(picked)} 条高价值线索，其余低价值流水不展开。")

        context = "\n".join(blocks)
        return context, max(1, len(context) // 4)

    @classmethod
    def _resolve_diary_output_language(cls, user_instruction: str = "") -> str:
        instruction = (user_instruction or "").lower()
        explicit_languages = (
            (r"(?<!默认)(?:使用|用|以|输出为?|写成)\s*(?:简体)?中文|simplified chinese|in chinese", "zh-CN"),
            (r"(?<!默认)(?:使用|用|以|输出为?|写成)\s*繁体中文|traditional chinese", "zh-TW"),
            (r"(?<!默认)(?:使用|用|以|输出为?|写成)\s*英文|in english|english output", "en"),
        )
        matches: list[tuple[int, str]] = []
        for pattern, language in explicit_languages:
            matches.extend(
                (match.start(), language)
                for match in re.finditer(pattern, instruction, flags=re.IGNORECASE)
            )
        if matches:
            return max(matches, key=lambda item: item[0])[1]

        configured = cls._normalize_diary_language(os.getenv("MEMORY_BREAD_DIARY_LANGUAGE", ""))
        return configured or DIARY_DEFAULT_LANGUAGE

    @staticmethod
    def _normalize_diary_language(value: str) -> Optional[str]:
        normalized = (value or "").strip().lower().replace("_", "-")
        if not normalized:
            return None
        if normalized.startswith("zh-hant") or normalized.startswith(("zh-tw", "zh-hk")):
            return "zh-TW"
        if normalized.startswith("zh") or normalized in {"chinese", "中文", "简体中文"}:
            return "zh-CN"
        aliases = {
            "english": "en",
            "英文": "en",
        }
        primary = normalized.split("-", 1)[0]
        return aliases.get(normalized) or (primary if primary == "en" else None)

    @staticmethod
    def _diary_language_label(language: str) -> str:
        return DIARY_LANGUAGE_LABELS.get(language, language or "简体中文")

    @staticmethod
    def _infer_work_environment(source_items: list[dict]) -> dict:
        apps: Counter[str] = Counter()
        categories: Counter[str] = Counter()
        entities: Counter[str] = Counter()

        for item in source_items:
            app = re.sub(r"\s+", " ", str(item.get("app_name") or "")).strip()
            category = re.sub(r"\s+", " ", str(item.get("category") or "")).strip()
            if app and app.lower() not in {"unknown", "未知", "其他"}:
                apps[app[:48]] += 1
            if category and category.lower() not in {"unknown", "未知", "其他"}:
                categories[category[:32]] += 1
            entity_values = item.get("entities") or []
            if isinstance(entity_values, str):
                try:
                    parsed_entities = json.loads(entity_values)
                    entity_values = parsed_entities if isinstance(parsed_entities, list) else [entity_values]
                except Exception:
                    entity_values = [entity_values]
            for entity in entity_values:
                text = re.sub(r"\s+", " ", str(entity or "")).strip()
                if text:
                    entities[text[:48]] += 1

        return {
            "apps": [name for name, _ in apps.most_common(5)],
            "categories": [name for name, _ in categories.most_common(4)],
            "entities": [name for name, _ in entities.most_common(6)],
        }

    @classmethod
    def _build_work_environment_context(
        cls,
        source_items: list[dict],
        output_language: str,
    ) -> str:
        environment = cls._infer_work_environment(source_items)
        lines = [
            "【工作环境摘要】",
            f"- 输出语言：{cls._diary_language_label(output_language)}",
        ]
        if environment["apps"]:
            lines.append(f"- 主要应用/工具：{'、'.join(environment['apps'])}")
        if environment["categories"]:
            lines.append(f"- 主要工作类型：{'、'.join(environment['categories'])}")
        if environment["entities"]:
            lines.append(f"- 项目/主题线索：{'、'.join(environment['entities'])}")
        lines.append("以上信息只用于识别用户的真实工作场景；不要把应用切换或工具使用本身写成产出。")
        return "\n".join(lines)

    @classmethod
    def _daily_diary_section_rules(cls, output_language: str) -> tuple:
        if output_language == "en":
            return (
                ("Today's Outcomes", ("Today's Outcomes", "Work Outputs", "今日产出", "工作产出"), 4, 45),
                ("Problems & Resolutions", ("Problems & Resolutions", "Problems and Resolutions", "问题与解决"), 2, 45),
            )
        return (
            ("今日产出", ("今日产出", "工作产出", "核心产出", "主要成果"), 4, 45),
            ("问题与解决", ("问题与解决", "风险/阻塞", "效率亮点与问题"), 2, 45),
        )

    @classmethod
    def _normalize_daily_diary_markdown(
        cls,
        markdown: str,
        output_language: str = DIARY_DEFAULT_LANGUAGE,
    ) -> str:
        section_rules = cls._daily_diary_section_rules(output_language)
        blocks: list[str] = []
        for title, headings, item_limit, char_limit in section_rules:
            items = cls._extract_markdown_section_items(markdown, headings)
            compacted: list[str] = []
            seen: set[str] = set()
            for item in items:
                text = re.sub(r"\s+", " ", item).strip()
                if not text or text in seen:
                    continue
                if len(text) > char_limit:
                    text = text[: char_limit - 1].rstrip("，,。；; ") + "…"
                seen.add(text)
                compacted.append(text)
                if len(compacted) >= item_limit:
                    break
            blocks.append(f"## {title}\n" + "\n".join(f"- {item}" for item in (compacted or ["无"])))
        return "\n\n".join(blocks)

    def _select_daily_diary_items(self, source_items: list[dict], limit: int) -> list[dict]:
        valuable = [
            item for item in source_items
            if self._compact_diary_item_text(item) and not self._is_low_value_diary_item(item)
        ]
        candidates = valuable or [
            item for item in source_items if self._compact_diary_item_text(item)
        ]

        ranked = sorted(
            candidates,
            key=lambda item: (
                int(item.get("importance") or 0),
                int(item.get("duration_minutes") or 0),
                int(item.get("start_time") or item.get("created_at") or 0),
            ),
            reverse=True,
        )
        picked = ranked[: max(1, int(limit))]
        picked.sort(key=lambda item: int(item.get("start_time") or item.get("created_at") or 0))
        return picked

    @staticmethod
    def _compact_diary_item_text(item: dict) -> str:
        text = " ".join(str(part or "").strip() for part in (item.get("overview"), item.get("details")) if part)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > DIARY_ITEM_SUMMARY_MAX_CHARS:
            return text[:DIARY_ITEM_SUMMARY_MAX_CHARS].rstrip() + "..."
        return text

    @staticmethod
    def _is_low_value_diary_item(item: dict) -> bool:
        text = TaskExecutor._compact_diary_item_text(item)
        lowered = text.lower()
        category = str(item.get("category") or "").lower()
        activity = str(item.get("activity_type") or "").lower()

        result_tokens = (
            "完成", "修复", "确定", "交付", "上线", "发布", "实现", "新增", "优化", "解决",
            "通过", "验证", "沉淀", "设计", "决策", "整理出", "形成了", "关闭", "delivered",
            "fixed", "implemented", "released",
        )
        if any(token in text or token in lowered for token in result_tokens):
            return False
        if int(item.get("importance") or 0) >= 4:
            return False

        low_value_tokens = (
            "浏览", "查看", "阅读", "搜索", "打开", "切换", "停留", "尝试", "调试环境",
            "安装", "配置", "无明显", "idle", "browsing", "reading",
        )
        if any(token in text or token in lowered for token in low_value_tokens):
            return True
        return category in {"浏览", "阅读", "其他", "idle", "browsing"} or activity in {"idle", "browsing"}

    @staticmethod
    def _first_meaningful_line(markdown: str) -> str:
        for raw_line in (markdown or "").splitlines():
            line = raw_line.strip().strip("#-* ")
            if line:
                return line[:160]
        return ""

    @staticmethod
    def _extract_markdown_section_items(markdown: str, headings: tuple[str, ...]) -> list[str]:
        items: list[str] = []
        in_section = False
        for raw_line in (markdown or "").splitlines():
            line = raw_line.strip()
            heading_text = line.strip("# ").strip("【】")
            if any(token in heading_text for token in headings):
                in_section = True
                continue
            if in_section and (line.startswith("#") or re.fullmatch(r"【.+】", line)):
                break
            if in_section and re.match(r"^(?:[-*]|\d+[.)、])\s*", line):
                item = re.sub(r"^(?:[-*]|\d+[.)、])\s*", "", line).strip()
                if item:
                    items.append(item[:240])
        return items[:12]

    @staticmethod
    def _parse_json_object(raw: str) -> dict:
        try:
            parsed = json.loads(raw or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {"markdown": raw or ""}

    def _get_diary_meta(
        self,
        conn: sqlite3.Connection,
        period_type: str,
        diary_date: str,
    ) -> Optional[dict]:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, is_system_generated, generation_status, updated_at
            FROM diaries
            WHERE period_type = ?
              AND diary_date = ?
            """,
            (period_type, diary_date),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "is_system_generated": bool(row[1]),
            "generation_status": row[2],
            "updated_at": row[3],
        }

    def _ensure_diaries_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS diaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_type TEXT NOT NULL CHECK(period_type IN ('daily', 'weekly', 'monthly', 'yearly')),
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                diary_date TEXT NOT NULL,
                content TEXT NOT NULL,
                source_timeline_ids TEXT NOT NULL DEFAULT '[]',
                source_diary_ids TEXT NOT NULL DEFAULT '[]',
                generation_status TEXT NOT NULL DEFAULT 'ready',
                is_system_generated INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(period_type, diary_date)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_diaries_type_date ON diaries(period_type, diary_date DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_diaries_period ON diaries(period_start, period_end)")
        conn.commit()

    def _upsert_diary(
        self,
        conn: sqlite3.Connection,
        period_type: str,
        period_start: str,
        period_end: str,
        diary_date: str,
        content: dict,
        source_timeline_ids: list[int],
        source_diary_ids: list[int],
    ) -> None:
        conn.execute(
            """
            INSERT INTO diaries (
                period_type, period_start, period_end, diary_date, content,
                source_timeline_ids, source_diary_ids, generation_status, is_system_generated
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', 1)
            ON CONFLICT(period_type, diary_date) DO UPDATE SET
                period_start = excluded.period_start,
                period_end = excluded.period_end,
                content = excluded.content,
                source_timeline_ids = excluded.source_timeline_ids,
                source_diary_ids = excluded.source_diary_ids,
                generation_status = 'ready',
                is_system_generated = 1,
                updated_at = datetime('now')
            """,
            (
                period_type,
                period_start,
                period_end,
                diary_date,
                json.dumps(content, ensure_ascii=False),
                json.dumps(source_timeline_ids, ensure_ascii=False),
                json.dumps(source_diary_ids, ensure_ascii=False),
            ),
        )
        conn.commit()

    def _query_knowledge(self, conn: sqlite3.Connection, user_instruction: str) -> list[dict]:
        """
        查询 knowledge 表。
        完全由 LLM 根据用户指令决定时间范围，这里默认取最近 30 天、
        重要性 >= 2 的条目，按时间倒序，最多 500 条。
        LLM 会在生成时自行判断哪些内容相关。
        """
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, capture_id, overview, details, category, importance,
                   start_time, end_time, duration_minutes,
                   frag_app_name, entities,
                   user_verified, observed_at, event_time_start, event_time_end,
                   history_view, content_origin, activity_type, is_self_generated,
                   evidence_strength, created_at
            FROM timelines
            WHERE importance >= 2
              AND (start_time IS NULL OR start_time >= ?)
            ORDER BY COALESCE(start_time, created_at) DESC
            LIMIT 500
        """, (int(time.time() * 1000) - 30 * 24 * 3600 * 1000,))

        rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "capture_id": r[1],
                "overview": r[2] or "",
                "details": r[3] or "",
                "category": r[4] or "其他",
                "importance": r[5] or 3,
                "start_time": r[6],
                "end_time": r[7],
                "duration_minutes": r[8],
                "app_name": r[9],
                "entities": json.loads(r[10]) if r[10] else [],
                "user_verified": bool(r[11]) if r[11] is not None else False,
                "observed_at": r[12],
                "event_time_start": r[13],
                "event_time_end": r[14],
                "history_view": bool(r[15]) if r[15] is not None else False,
                "content_origin": r[16],
                "activity_type": r[17],
                "is_self_generated": bool(r[18]) if r[18] is not None else False,
                "evidence_strength": r[19],
                "created_at": r[20],
            }
            for r in rows
        ]

    def _build_context(
        self,
        knowledge_list: list[dict],
        user_instruction: str = "",
    ) -> tuple[str, int]:
        """
        根据 token 预算构建上下文文本。

        策略：
        - 预估 token 数 <= FULL_CONTEXT_TOKEN_LIMIT：overview + details 全用
        - 预估 token 数 <= OVERVIEW_ONLY_TOKEN_LIMIT：只用 overview
        - 超出：按重要性截断到 OVERVIEW_ONLY_TOKEN_LIMIT
        """
        estimated_tokens = len(knowledge_list) * AVG_TOKENS_PER_KNOWLEDGE
        is_weekly_report = self._is_weekly_report_instruction(user_instruction)
        kpi_mode = is_weekly_report and self._is_kpi_mode_instruction(user_instruction)

        if estimated_tokens <= FULL_CONTEXT_TOKEN_LIMIT:
            # 全量：overview + details
            blocks = []
            for k in knowledge_list:
                ts = self._format_time(k.get('start_time'))
                duration = f"（{k['duration_minutes']}分钟）" if k.get('duration_minutes') else ""
                work_item = k.get('work_item') or k['category']
                block = f"[{ts}{duration}][{work_item}] {k['overview']}"
                if k.get('work_progress'):
                    block += f"（{k['work_progress']}）"
                if k.get('details'):
                    block += f"\n详情：{k['details']}"
                blocks.append(block)
            context = "\n\n".join(blocks)
            logger.info(f"上下文策略：全量，{len(knowledge_list)} 条，预估 {estimated_tokens} tokens")

        elif estimated_tokens <= OVERVIEW_ONLY_TOKEN_LIMIT:
            # 只用 overview
            blocks = []
            for k in knowledge_list:
                ts = self._format_time(k.get('start_time'))
                duration = f"（{k['duration_minutes']}分钟）" if k.get('duration_minutes') else ""
                work_item = k.get('work_item') or k['category']
                line = f"[{ts}{duration}][{work_item}] {k['overview']}"
                if k.get('work_progress'):
                    line += f"（{k['work_progress']}）"
                blocks.append(line)
            context = "\n".join(blocks)
            estimated_tokens = len(knowledge_list) * 80
            logger.info(f"上下文策略：仅概述，{len(knowledge_list)} 条，预估 {estimated_tokens} tokens")

        else:
            # 按重要性截断
            sorted_k = sorted(knowledge_list, key=lambda x: x['importance'], reverse=True)
            max_count = OVERVIEW_ONLY_TOKEN_LIMIT // 80
            truncated = sorted_k[:max_count]
            # 截断后按时间重新排序
            truncated.sort(key=lambda x: x.get('start_time') or 0)
            blocks = []
            for k in truncated:
                ts = self._format_time(k.get('start_time'))
                work_item = k.get('work_item') or k['category']
                line = f"[{ts}][{work_item}] {k['overview']}"
                if k.get('work_progress'):
                    line += f"（{k['work_progress']}）"
                blocks.append(line)
            context = "\n".join(blocks)
            estimated_tokens = len(truncated) * 80
            logger.info(f"上下文策略：截断，{len(truncated)}/{len(knowledge_list)} 条，预估 {estimated_tokens} tokens")

        if is_weekly_report and knowledge_list:
            quant_block = self._build_quant_evidence_block(
                knowledge_list,
                kpi_mode=kpi_mode,
                top_n=10 if kpi_mode else 6,
            )
            if quant_block:
                context = f"{context}\n\n{quant_block}" if context else quant_block
                estimated_tokens += max(40, len(quant_block) // 4)

        return context, estimated_tokens


    @staticmethod
    def _is_weekly_report_instruction(user_instruction: str) -> bool:
        lowered = (user_instruction or "").lower()
        return any(token in lowered for token in (
            "周报",
            "weekly report",
            "weekly",
        ))

    @staticmethod
    def _is_kpi_mode_instruction(user_instruction: str) -> bool:
        lowered = (user_instruction or "").lower()
        return any(token in lowered for token in (
            "okr",
            "kpi",
            "专项",
            "关键结果",
            "指标",
            "里程碑",
            "达成率",
            "完成率",
        ))

    def _build_quant_evidence_block(
        self,
        knowledge_list: list[dict],
        kpi_mode: bool = False,
        top_n: int = 6,
    ) -> str:
        if not knowledge_list:
            return ""

        candidates: list[tuple[str, str, float]] = []
        for item in knowledge_list:
            text_parts = [item.get("overview") or ""]
            if item.get("details"):
                text_parts.append(item["details"])
            fact_lines = self._extract_quant_fact_lines("\n".join(text_parts), kpi_mode=kpi_mode)
            if not fact_lines:
                continue

            evidence_ref = self._format_evidence_ref(item)
            evidence_score = self._score_evidence(item)
            for fact in fact_lines:
                candidates.append((fact, evidence_ref, evidence_score))

        if not candidates:
            return ""

        dedup: dict[str, tuple[str, str, float]] = {}
        for fact, ref, score in candidates:
            key = self._normalize_fact_key(fact)
            prev = dedup.get(key)
            if prev is None or score > prev[2]:
                dedup[key] = (fact, ref, score)

        ranked = sorted(dedup.values(), key=lambda item: (-item[2], len(item[0])))
        picked = ranked[: max(1, top_n)]

        lines = ["【量化证据】（仅可引用以下证据中的数字结论）"]
        for idx, (fact, ref, _) in enumerate(picked, 1):
            lines.append(f"- [{idx}] {fact}（证据：{ref}）")
        return "\n".join(lines)

    @staticmethod
    def _extract_quant_fact_lines(text: str, kpi_mode: bool = False) -> list[str]:
        if not text:
            return []

        progress_keywords = (
            "完成", "达成", "推进", "上线", "交付", "修复", "关闭", "处理", "新增", "减少", "降低", "提升", "优化",
            "通过率", "成功率", "失败率", "耗时", "时延", "里程碑", "okr", "kpi", "专项", "progress", "improve", "fixed", "delivered",
        )
        number_pattern = re.compile(
            r"(\d+(?:\.\d+)?\s*%|\d+\s*/\s*\d+|\d+(?:\.\d+)?\s*(?:个|项|次|处|条|页|分钟|小时|天|周|月|年|ms|s|秒|模块|接口|问题|bug|任务|需求|pr|PR|commit|人天|台|条告警))"
        )

        candidates: list[str] = []
        segments = re.split(r"[\n。；;！？!?]+", text)
        for seg in segments:
            line = " ".join(seg.strip().split())
            if len(line) < 6:
                continue
            if not number_pattern.search(line):
                continue

            lowered = line.lower()
            if not any((kw in line) or (kw in lowered) for kw in progress_keywords):
                continue
            if TaskExecutor._looks_like_noise_numeric_line(line):
                continue
            if kpi_mode and not any(token in lowered for token in ("okr", "kpi", "专项", "达成", "完成", "提升", "降低", "上线", "交付", "通过率")):
                continue

            candidates.append(line[:120])

        return candidates[:8]

    @staticmethod
    def _looks_like_noise_numeric_line(line: str) -> bool:
        if re.fullmatch(r"[\d\s\-/:年月日.]+", line):
            return True

        has_progress_word = any(
            token in line for token in ("完成", "达成", "提升", "下降", "减少", "增加", "修复", "关闭", "交付", "上线", "通过率", "耗时")
        )
        if re.search(r"\b20\d{2}[-/年]\d{1,2}(?:[-/月]\d{1,2})?", line) and not has_progress_word:
            return True
        if re.search(r"\bv?\d+\.\d+\.\d+\b", line) and not has_progress_word:
            return True

        return False

    @staticmethod
    def _normalize_fact_key(fact: str) -> str:
        normalized = fact.lower()
        normalized = re.sub(r"\s+", "", normalized)
        normalized = re.sub(r"[，,。；;：:（）()\[\]【】'\"]", "", normalized)
        return normalized

    @staticmethod
    def _format_evidence_ref(item: dict) -> str:
        knowledge_ref = None
        capture_ref = None

        try:
            if item.get("id") is not None:
                knowledge_ref = f"K#{int(item['id'])}"
        except Exception:
            knowledge_ref = None

        try:
            if item.get("capture_id") is not None:
                capture_ref = f"C#{int(item['capture_id'])}"
        except Exception:
            capture_ref = None

        if knowledge_ref and capture_ref:
            return f"{knowledge_ref}/{capture_ref}"
        if knowledge_ref:
            return knowledge_ref
        if capture_ref:
            return capture_ref
        return "未知证据"

    @staticmethod
    def _score_evidence(item: dict) -> float:
        evidence_strength = str(item.get("evidence_strength") or "").lower()
        strength_score = {"high": 1.6, "medium": 1.0, "low": 0.2}.get(evidence_strength, 0.5)

        try:
            importance = float(item.get("importance") or 3)
        except Exception:
            importance = 3.0
        importance_score = max(0.0, min(importance, 5.0)) * 0.25

        user_verified_score = 2.0 if item.get("user_verified") else 0.0

        ts_value = item.get("observed_at") or item.get("event_time_end") or item.get("end_time") or item.get("start_time")
        recency_score = 0.0
        try:
            ts_int = int(ts_value)
            age_days = (int(time.time() * 1000) - ts_int) / (24 * 60 * 60 * 1000)
            recency_score = max(0.0, 1.2 - age_days / 14)
        except Exception:
            recency_score = 0.0

        return user_verified_score + strength_score + importance_score + recency_score

    def _llm_generate(
        self,
        user_instruction: str,
        context: str,
        task_id: int = None,
        is_weekly_report: bool = False,
        kpi_mode: bool = False,
        output_language: Optional[str] = None,
        concise: bool = False,
    ) -> str:
        """调用 LLM 生成报告"""
        from monitor.llm_tracker import LLMCallTracker, estimate_tokens

        weekly_rules = ""
        if is_weekly_report:
            if kpi_mode:
                weekly_rules = (
                    "\n5. 你必须输出“## 本周量化进展（OKR/KPI/专项）”章节，且每条量化结论都必须附证据编号（证据：K#xx/C#yy）。"
                    "\n6. 未出现在“量化证据”区块中的数字禁止输出；缺少证据时改写为定性描述。"
                )
            else:
                weekly_rules = (
                    "\n5. 若上下文包含“量化证据”区块，优先引用其中数字并附证据编号（证据：K#xx/C#yy）。"
                    "\n6. 无证据支撑时不得编造数字。"
                )

        language_rules = ""
        if output_language:
            language_label = self._diary_language_label(output_language)
            language_rules = (
                f"\n8. 【输出语言】必须使用{language_label}。即使工作记录大部分为英文，也必须用{language_label}叙述；"
                "产品名、应用名和代码标识符可以保留原文。"
            )
        if concise:
            language_rules += (
                "\n9. 【精简要求】合并同类事项，只保留成果、结论和可验证计划；禁止逐时段复述，禁止补写背景铺垫或总结性套话。"
            )

        system_prompt = (
            "你是用户的个人工作助手。以下是用户近期的工作记录摘要（按时间顺序）。"
            "每条记录的格式为：[时间][工作项] 概述（进度）\n\n"
            "请严格按照用户的指令，基于这些工作记录生成相应的报告或总结。"
            "输出使用 Markdown 格式，语言简洁专业。\n\n"
            "【重要】生成报告时必须遵守以下规则：\n"
            "1. 每条产出必须明确说明是哪个项目/工作项的成果，格式：【工作项】完成了…\n"
            "2. 项目进展章节必须按工作项分组，每个工作项说明当前状态（待启动/进行中/已完成/阻塞）和具体进度\n"
            "3. 以「产出」为中心，而非「活动」。每一条内容必须体现可见的价值或结果。\n"
            "4. 以下类型的活动禁止直接写入报告：纯阅读/查看文档、安装配置环境、无结论的调研、中间态的失败尝试。"
            "若这些活动产生了明确结论或结果，则以结论为主语来描述。\n"
            "5. 凡有可量化数据（测试通过率、性能指标、完成模块数等），必须写出具体数字。\n"
            "6. 每条工作项须能回答「这件事带来了什么价值？」，否则删除该条。\n"
            "7. 【格式规范】所有章节标题必须使用 ## 二级标题格式（如 ## 开发、## 阅读、## 今日小结），章节内的列表项必须统一使用无序列表格式（以 - 或 * 开头，无额外缩进）。"
            f"{weekly_rules}{language_rules}"
        )
        user_prompt = f"## 工作记录\n\n{context}\n\n---\n\n## 用户指令\n\n{user_instruction}"
        # 使用全局统一的 Ollama 模型名，避免与 RAG 查询使用不同模型导致 Ollama swap
        from model_registry_global import get_active_ollama_model
        model = get_active_ollama_model()

        def _chat() -> dict:
            client = self._get_llm_client()
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            import inference_queue as inference_queue_module

            current_task_preempt_requested = getattr(
                inference_queue_module,
                "current_task_preempt_requested",
                lambda: False,
            )
            raise_if_preempted = getattr(
                inference_queue_module,
                "raise_if_preempted",
                lambda: None,
            )
            register_current_preempt_callback = getattr(
                inference_queue_module,
                "register_current_preempt_callback",
                lambda _callback: (lambda: None),
            )

            def preemptible_chat(chat_messages: list[dict], options: dict) -> dict:
                raise_if_preempted()
                stream = client.chat(
                    model=model,
                    messages=chat_messages,
                    stream=True,
                    # Qwen 等模型会把内部推理放在 thinking 字段。日记只需要最终正文，
                    # 显式关闭思考输出，避免英文推理过程被误当成日记保存。
                    think=False,
                    options=options,
                )
                chunks = [stream] if isinstance(stream, dict) else stream
                close_stream = getattr(chunks, "close", lambda: None)
                unregister = register_current_preempt_callback(close_stream)
                content_parts: list[str] = []
                thinking_parts: list[str] = []
                final: dict = {}
                try:
                    for raw_chunk in chunks:
                        raise_if_preempted()
                        chunk = (
                            raw_chunk.model_dump()
                            if hasattr(raw_chunk, "model_dump")
                            else dict(raw_chunk)
                        )
                        final.update(chunk)
                        message = chunk.get("message") or {}
                        if message.get("content"):
                            content_parts.append(str(message["content"]))
                        if message.get("thinking"):
                            thinking_parts.append(str(message["thinking"]))
                except Exception:
                    if current_task_preempt_requested():
                        raise_if_preempted()
                    raise
                finally:
                    unregister()
                raise_if_preempted()
                final["message"] = {
                    **(final.get("message") or {}),
                    "content": "".join(content_parts),
                    "thinking": "".join(thinking_parts),
                }
                return final

            response = preemptible_chat(
                messages,
                {"temperature": 0.2 if concise else 0.5, "num_predict": 768 if concise else 2048},
            )
            content = self._llm_message_content(response)
            if self._requires_chinese_output(output_language) and not self._is_chinese_diary_output(content):
                logger.warning("日记首次生成未遵守中文输出要求，正在执行一次纠偏重写")
                response = preemptible_chat(
                    messages + [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "请将上面的内容严格改写为简体中文，不得新增事实。"
                                "保留规定章节，删除过程性流水账和套话，每条只保留一个结果或结论。"
                            ),
                        },
                    ],
                    {"temperature": 0.1, "num_predict": 768 if concise else 2048},
                )
                content = self._llm_message_content(response)
                if not self._is_chinese_diary_output(content):
                    raise RuntimeError("模型连续两次未按要求输出中文日记，已拒绝写入")
            return response

        with LLMCallTracker(
            caller="task",
            model_name=model,
            caller_id=str(task_id) if task_id else None,
            db_path=self.db_path,
        ) as tracker:
            try:
                from inference_queue import LANE_P2_DIARY, Priority, get_global_queue

                response = get_global_queue().submit_sync(
                    Priority.P2,
                    _chat,
                    timeout=900.0,
                    lane=LANE_P2_DIARY,
                )
            except ImportError:
                response = _chat()
            tracker.set_response(response)
            # 如果 Ollama 没返回 token 信息，用估算补充
            content = self._llm_message_content(response)
            if tracker._prompt_tokens == 0:
                tracker.set_tokens(
                    prompt=estimate_tokens(system_prompt + user_prompt),
                    completion=estimate_tokens(content),
                )
        return content

    @staticmethod
    def _llm_message_content(response: dict) -> str:
        msg = response.get("message") or {}
        return msg.get("content", "") or msg.get("thinking", "")

    @staticmethod
    def _requires_chinese_output(output_language: Optional[str]) -> bool:
        return bool(output_language and output_language.startswith("zh"))

    @staticmethod
    def _is_chinese_diary_output(content: str) -> bool:
        lowered = (content or "").lower()
        reasoning_markers = (
            "thinking process",
            "constraints (from",
            "user's latest prompt",
            "simplified chinese only",
            "no new facts",
        )
        if any(marker in lowered for marker in reasoning_markers):
            return False

        body = re.sub(
            r"(?m)^#+\s*(?:今日产出|问题与解决|明日计划|本周核心产出|项目进展|下周计划|风险/阻塞|主要成果|时间分配|效率亮点与问题|下月目标)\s*$",
            "",
            content or "",
        )
        chinese_chars = len(re.findall(r"[\u3400-\u9fff]", body))
        latin_words = len(re.findall(r"\b[A-Za-z]{2,}\b", body))
        if chinese_chars < 2 or chinese_chars < latin_words:
            return False

        bullet_items = [
            re.sub(r"^(?:[-*]|\d+[.)、])\s*", "", line.strip())
            for line in (content or "").splitlines()
            if re.match(r"^(?:[-*]|\d+[.)、])\s*", line.strip())
        ]
        for item in bullet_items:
            item_chinese_chars = len(re.findall(r"[\u3400-\u9fff]", item))
            item_latin_words = len(re.findall(r"\b[A-Za-z]{2,}\b", item))
            if item_chinese_chars == 0 or item_latin_words > max(4, item_chinese_chars):
                return False
        return True


    # ─────────────────────────────────────────────────────────────────────────
    # 创作智能体执行路径
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _core_engine_url() -> str:
        return os.getenv("CORE_ENGINE_URL", "http://127.0.0.1:7070").rstrip("/")

    def _execute_creation_task(self, task: dict, exec_id: int) -> dict:
        """
        复用创作 Agent 链路执行任务指令。
        抛出任何异常都表示创作路径不可用，调用方回退咨询智能体路径。
        """
        core_url = self._core_engine_url()
        instruction = str(task.get("user_instruction") or "")
        session_id = "session-task-{}-{}".format(task["id"], exec_id)
        started = time.time()

        installed_skills = self._fetch_installed_creation_skills(core_url)
        selected_skills = self._build_task_selected_skills(instruction, installed_skills)
        payload = {
            "user_prompt": instruction,
            "root_request": instruction,
            "session_id": session_id,
            "current_document": "",
            "conversation": [],
            "selected_skills": selected_skills,
            "model_mode": "local",
            "confirmed": True,
        }
        events, document = self._run_creation_agent_stream(core_url, payload)
        if not document.strip():
            raise RuntimeError("创作智能体未返回文档")
        latency_ms = int((time.time() - started) * 1000)
        history_id = self._save_task_creation_history(
            core_url, task, session_id, instruction, document, events, latency_ms
        )
        return {"result_text": document, "creation_history_id": history_id}

    @staticmethod
    def _fetch_installed_creation_skills(core_url: str) -> list:
        import httpx

        try:
            response = httpx.get(
                f"{core_url}/api/creation/skills?installed=true", timeout=30
            )
            response.raise_for_status()
            skills = response.json()
        except Exception as exc:
            raise RuntimeError(f"拉取已安装创作技能失败: {exc}") from exc
        return skills if isinstance(skills, list) else []

    def _build_task_selected_skills(self, instruction: str, installed_skills: list) -> list:
        titles = [
            str(skill.get("title")).strip()
            for skill in installed_skills
            if isinstance(skill, dict) and str(skill.get("title") or "").strip()
        ]
        mentioned_titles = self._parse_task_mentioned_skills(instruction, titles)
        skills_by_title = {}
        for skill in installed_skills:
            if not isinstance(skill, dict):
                continue
            title = str(skill.get("title") or "").strip()
            if title and title not in skills_by_title:
                skills_by_title[title] = skill
        selected = []
        for title in mentioned_titles:
            skill = skills_by_title.get(title)
            if not skill:
                # 未安装的同名提及不携带技能体，仅保留在指令文本里。
                continue
            selected.append(
                {
                    "id": skill.get("client_skill_key") or str(skill.get("id") or title),
                    "title": title,
                    "summary": skill.get("summary") or "",
                    "workflowRole": "primary",
                    "skillDescription": skill.get("skill_description"),
                    "executionSteps": skill.get("execution_steps") or [],
                    "skillInstructions": self._skill_markdown_text(skill),
                    "strictStructure": True,
                }
            )
        return selected

    @staticmethod
    def _skill_markdown_text(skill: dict) -> str:
        for package_file in skill.get("package_files") or []:
            if not isinstance(package_file, dict):
                continue
            if package_file.get("path") != "SKILL.md":
                continue
            content_base64 = package_file.get("content_base64") or ""
            try:
                return base64.b64decode(content_base64).decode("utf-8")
            except Exception:
                return ""
        return ""

    def _parse_task_mentioned_skills(self, text: str, installed_titles: list) -> list:
        """
        解析任务指令中被 @ 的技能，与桌面端
        CreationSkillEditor.parseMentionedResources 的边界规则保持一致：
        最长名称优先消费，提及后紧跟提及字符则不算命中。
        """
        remaining = text or ""

        def find_mention_index(source: str, token: str) -> int:
            position = source.find(token)
            while position >= 0:
                end = position + len(token)
                next_char = source[end] if end < len(source) else ""
                if not next_char or not MENTION_NAME_CHAR_PATTERN.match(next_char):
                    return position
                position = source.find(token, position + 1)
            return -1

        def consume(token: str) -> bool:
            nonlocal remaining
            if find_mention_index(remaining, token) < 0:
                return False
            remaining = remaining.replace(token, " ")
            return True

        # 先消费 Agent / 工具提及，避免它们的名称被后续兼容正则误判为技能。
        for label in sorted(
            CREATION_MENTION_AGENT_LABELS + CREATION_MENTION_TOOL_LABELS,
            key=len,
            reverse=True,
        ):
            consume(f"@{label}")

        skills: list = []
        for title in sorted(dict.fromkeys(installed_titles), key=len, reverse=True):
            if consume(f"@{title}") and title not in skills:
                skills.append(title)

        for match in MENTION_TOKEN_PATTERN.finditer(remaining):
            name = match.group(1)
            if name not in skills:
                skills.append(name)
        return skills

    def _run_creation_agent_stream(self, core_url: str, payload: dict):
        """流式消费创作 Agent SSE，直到 run.completed / run.failed。"""
        import httpx

        events: list = []
        document = ""
        completed = False
        timeout = httpx.Timeout(1800.0, connect=30.0)
        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    "POST", f"{core_url}/api/creation/agent/run", json=payload
                ) as response:
                    if response.status_code != 200:
                        body = response.read()[:2000].decode("utf-8", "replace")
                        raise RuntimeError(
                            f"创作智能体接口返回 {response.status_code}: {body}"
                        )
                    for raw_line in response.iter_lines():
                        line = raw_line.strip()
                        if not line.startswith("data:"):
                            continue
                        data_text = line[len("data:"):].strip()
                        if not data_text:
                            continue
                        try:
                            event = json.loads(data_text)
                        except json.JSONDecodeError as exc:
                            # 解析失败时把完整原文落日志，便于诊断。
                            logger.error(
                                "创作智能体 SSE 事件解析失败: %s 原文=%r", exc, data_text
                            )
                            continue
                        if not isinstance(event, dict):
                            continue
                        events.append(event)
                        event_type = event.get("type")
                        if event_type == "document.delta":
                            document += str((event.get("data") or {}).get("content") or "")
                        elif event_type == "run.completed":
                            completed_document = str(
                                (event.get("data") or {}).get("document") or ""
                            )
                            if completed_document.strip():
                                document = completed_document
                            completed = True
                            break
                        elif event_type == "run.failed":
                            raise RuntimeError(
                                str(event.get("summary") or "创作智能体执行失败")
                            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"连接创作智能体失败: {exc}") from exc

        if not completed:
            raise RuntimeError("创作智能体事件流结束但未收到执行完成事件")
        return events, document

    @staticmethod
    def _save_task_creation_history(
        core_url: str,
        task: dict,
        session_id: str,
        instruction: str,
        document: str,
        events: list,
        latency_ms: int,
    ) -> Optional[int]:
        import httpx

        payload = {
            "prompt": instruction,
            "generated_content": document,
            "reference_count": 0,
            "references": [],
            "session_id": session_id,
            "conversation": [{"role": "user", "content": instruction}],
            "agent_trace": events,
            "root_request": instruction,
            "latency_ms": latency_ms,
            "source_kind": "scheduled_task",
            "source_ref_id": task["id"],
        }
        try:
            response = httpx.post(
                f"{core_url}/api/creation/history", json=payload, timeout=30
            )
            response.raise_for_status()
            saved = response.json()
        except Exception as exc:
            logger.warning("任务 %s 创作记录写入失败: %s", task["id"], exc)
            return None
        if isinstance(saved, dict) and isinstance(saved.get("id"), int):
            return saved["id"]
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # 数据库操作
    # ─────────────────────────────────────────────────────────────────────────

    def _get_task(self, conn: sqlite3.Connection, task_id: int) -> Optional[dict]:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT id, name, user_instruction, cron_expression, template_id,
                          notification_channel_ids, executor_kind
                   FROM scheduled_tasks WHERE id = ?""",
                (task_id,),
            )
        except sqlite3.OperationalError:
            try:
                cursor.execute(
                    """SELECT id, name, user_instruction, cron_expression, template_id,
                              notification_channel_ids
                       FROM scheduled_tasks WHERE id = ?""",
                    (task_id,),
                )
            except sqlite3.OperationalError:
                # 兼容仍在使用旧测试夹具或尚未迁移的本地数据库。
                cursor.execute(
                    """SELECT id, name, user_instruction, cron_expression, template_id
                       FROM scheduled_tasks WHERE id = ?""",
                    (task_id,),
                )
        row = cursor.fetchone()
        if not row:
            return None
        channel_ids = []
        if len(row) > 5:
            try:
                decoded_ids = json.loads(row[5] or "[]")
                if isinstance(decoded_ids, list):
                    channel_ids = [
                        int(channel_id)
                        for channel_id in decoded_ids
                        if isinstance(channel_id, int) and channel_id > 0
                    ]
            except (TypeError, ValueError, json.JSONDecodeError):
                channel_ids = []
        executor_kind = "consult"
        if len(row) > 6 and row[6]:
            executor_kind = str(row[6])
        return {
            "id": row[0],
            "name": row[1],
            "user_instruction": row[2],
            "cron_expression": row[3],
            "template_id": row[4],
            "notification_channel_ids": channel_ids,
            "executor_kind": executor_kind,
        }

    def _deliver_task_result(
        self,
        conn: sqlite3.Connection,
        task: dict,
        execution_id: int,
        result_text: str,
        completed_at: int,
    ) -> list[dict]:
        channel_ids = sorted(set(task.get("notification_channel_ids") or []))
        if not channel_ids:
            return []

        placeholders = ",".join("?" for _ in channel_ids)
        try:
            rows = conn.execute(
                f"""SELECT id, name, channel_type, webhook_url
                    FROM notification_channels
                    WHERE enabled = 1 AND id IN ({placeholders})
                    ORDER BY id""",
                channel_ids,
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning(
                "任务结果消息渠道读取失败: task_id=%s error_type=%s",
                task["id"],
                type(exc).__name__,
            )
            return []

        deliveries = []
        for channel_id, channel_name, channel_type, webhook_url in rows:
            created_at = int(time.time() * 1000)
            try:
                conn.execute(
                    """INSERT INTO task_notification_deliveries
                         (execution_id, channel_id, status, created_at)
                       VALUES (?, ?, 'pending', ?)
                       ON CONFLICT(execution_id, channel_id) DO UPDATE SET
                         status = 'pending',
                         error_message = NULL,
                         delivered_at = NULL""",
                    (execution_id, channel_id, created_at),
                )
                conn.commit()

                payload = self._notification_payload(
                    channel_type=channel_type,
                    task=task,
                    execution_id=execution_id,
                    result_text=result_text,
                    completed_at=completed_at,
                )
                self._post_notification(webhook_url, payload, channel_type)
                delivered_at = int(time.time() * 1000)
                conn.execute(
                    """UPDATE task_notification_deliveries
                       SET status = 'success', error_message = NULL, delivered_at = ?
                       WHERE execution_id = ? AND channel_id = ?""",
                    (delivered_at, execution_id, channel_id),
                )
                conn.commit()
                deliveries.append(
                    {
                        "channel_id": channel_id,
                        "channel_name": channel_name,
                        "status": "success",
                    }
                )
            except Exception as exc:
                error_message = self._safe_delivery_error(exc)
                try:
                    conn.execute(
                        """UPDATE task_notification_deliveries
                           SET status = 'failed', error_message = ?, delivered_at = NULL
                           WHERE execution_id = ? AND channel_id = ?""",
                        (error_message, execution_id, channel_id),
                    )
                    conn.commit()
                except sqlite3.Error:
                    pass
                logger.warning(
                    "任务结果消息投递失败: task_id=%s channel_id=%s error=%s",
                    task["id"],
                    channel_id,
                    error_message,
                )
                deliveries.append(
                    {
                        "channel_id": channel_id,
                        "channel_name": channel_name,
                        "status": "failed",
                        "error_message": error_message,
                    }
                )
        return deliveries

    @staticmethod
    def _notification_payload(
        *,
        channel_type: str,
        task: dict,
        execution_id: int,
        result_text: str,
        completed_at: int,
    ) -> dict:
        safe_result = (result_text or "")[:12000]
        content = f"MemoryBread · {task['name']}\n\n{safe_result}"
        if channel_type == "feishu":
            return {"msg_type": "text", "content": {"text": content}}
        if channel_type in {"dingtalk", "wecom"}:
            return {"msgtype": "text", "text": {"content": content}}
        return {
            "event": "memorybread.task.completed",
            "task": {"id": task["id"], "name": task["name"]},
            "execution_id": execution_id,
            "result": safe_result,
            "completed_at": completed_at,
        }

    @staticmethod
    def _post_notification(webhook_url: str, payload: dict, channel_type: str) -> None:
        request = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "MemoryBread/TaskNotification",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            response_body = response.read(16_384)
        if channel_type == "webhook" or not response_body:
            return
        try:
            response_payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(response_payload, dict):
            return
        if channel_type == "feishu":
            status_code = response_payload.get(
                "code",
                response_payload.get("StatusCode", 0),
            )
        else:
            status_code = response_payload.get("errcode", 0)
        if status_code not in (0, "0", None):
            raise NotificationDeliveryRejected()

    @staticmethod
    def _safe_delivery_error(exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            return f"http_{exc.code}"
        if isinstance(exc, urllib.error.URLError):
            return "connection_error"
        if isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, sqlite3.Error):
            return "storage_error"
        if isinstance(exc, NotificationDeliveryRejected):
            return "provider_rejected"
        return type(exc).__name__

    def _create_execution(self, conn: sqlite3.Connection, task_id: int, started_at: int) -> int:
        self._ensure_task_executions_creation_history_column(conn)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO task_executions (task_id, started_at, status) VALUES (?, ?, 'running')",
            (task_id, started_at)
        )
        conn.commit()
        return cursor.lastrowid

    @staticmethod
    def _ensure_task_executions_creation_history_column(conn: sqlite3.Connection) -> None:
        # 列存在性防御：旧本地库可能尚未应用 086 迁移。
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(task_executions)")}
        except sqlite3.Error:
            return
        if "creation_history_id" not in columns:
            try:
                conn.execute(
                    "ALTER TABLE task_executions ADD COLUMN creation_history_id INTEGER"
                )
                conn.commit()
            except sqlite3.Error:
                pass

    def _update_execution(self, conn: sqlite3.Connection, exec_id: int, data: dict):
        fields = ", ".join(f"{k} = ?" for k in data if k != "exec_id")
        values = [v for k, v in data.items() if k != "exec_id"]
        conn.execute(
            f"UPDATE task_executions SET {fields} WHERE id = ?",
            values + [exec_id]
        )
        conn.commit()

    def _update_task_stats(
        self, conn: sqlite3.Connection, task_id: int, status: str, completed_at: int
    ):
        conn.execute(
            """UPDATE scheduled_tasks
               SET run_count = run_count + 1,
                   last_run_at = ?,
                   last_run_status = ?,
                   updated_at = ?
               WHERE id = ?""",
            (completed_at, status, completed_at, task_id)
        )
        conn.commit()

    def _format_time(self, ts_ms: Optional[int]) -> str:
        if not ts_ms:
            return "未知时间"
        return datetime.fromtimestamp(ts_ms / 1000).strftime('%m-%d %H:%M')
