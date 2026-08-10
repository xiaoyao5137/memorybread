#!/usr/bin/env python3
"""为官网截图和录屏构建一套连贯的 MemoryBread 演示数据。

默认写入 ~/.memory-bread/memory-bread.db。脚本会先用 SQLite 在线备份 API
创建备份，再在一个事务中替换由本脚本生成的旧演示数据。不会删除用户真实数据。

Python 兼容基线：3.9。
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


DEMO_MARKER = "memorybread.website.demo.v1"
DEMO_CAPTURE_SOURCE = "website_demo_v1"
DEMO_CREATION_SESSION_PREFIX = "website-demo-v1-"
DEMO_DATA_KEY_PREFIX = "website-demo-v1-"
DEMO_TARGET_COUNT = 24
DEFAULT_DATABASE = Path.home() / ".memory-bread" / "memory-bread.db"
REQUIRED_TABLES = {
    "captures",
    "timelines",
    "bake_knowledge",
    "bake_documents",
    "bake_sops",
    "data_sources",
    "data_snapshots",
    "rag_sessions",
    "creation_history",
}


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def business_time(days_ago: int, hour: int, minute: int) -> int:
    """返回近期工作日风格的本地毫秒时间戳，避免生成未来时间。"""
    now = datetime.now()
    candidate = datetime.combine(now.date() - timedelta(days=days_ago), time(hour, minute))
    if candidate > now:
        candidate = now - timedelta(minutes=30)
    return int(candidate.timestamp() * 1000)


def iso_local(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="写入用于官网截图和录屏的四场景演示数据。"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DATABASE,
        help="MemoryBread SQLite 数据库路径（默认：~/.memory-bread/memory-bread.db）",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="跳过写入前的 SQLite 在线备份（仅建议用于临时测试库）",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="备份目录（默认：数据库同级 backups/）",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="只移除本脚本生成的演示数据，不重新写入",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="只检查演示数据是否完整，不修改数据库",
    )
    return parser.parse_args()


def connect_database(database_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(database_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def validate_schema(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = sorted(REQUIRED_TABLES - existing)
    if missing:
        raise RuntimeError("数据库缺少演示数据所需表：{}".format("、".join(missing)))


def backup_database(database_path: Path, backup_dir: Optional[Path]) -> Path:
    target_dir = backup_dir or database_path.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = target_dir / "memory-bread-before-demo-{}.db".format(timestamp)

    source = sqlite3.connect(str(database_path), timeout=30)
    target = sqlite3.connect(str(backup_path))
    try:
        source.execute("PRAGMA busy_timeout = 30000")
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def suspend_triggers(
    conn: sqlite3.Connection, trigger_names: Sequence[str]
) -> List[str]:
    """事务内临时移除已知会阻断定向清理的遗留触发器。"""
    definitions: List[str] = []
    for trigger_name in trigger_names:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger_name,),
        ).fetchone()
        if row is None or not row[0]:
            continue
        definitions.append(str(row[0]))
        conn.execute('DROP TRIGGER "{}"'.format(trigger_name.replace('"', '""')))
    return definitions


def restore_triggers(conn: sqlite3.Connection, definitions: Sequence[str]) -> None:
    for definition in definitions:
        conn.execute(definition)


def rebuild_search_indexes(conn: sqlite3.Connection) -> None:
    """同步外部内容 FTS5 索引，兼容缺少写入触发器的历史数据库。"""
    for table in (
        "captures_fts",
        "timelines_fts",
        "knowledge_fts",
        "bake_knowledge_fts",
        "bake_documents_fts",
        "bake_sops_fts",
        "data_snapshots_fts",
    ):
        if table_exists(conn, table):
            conn.execute(
                'INSERT INTO "{0}"("{0}") VALUES (\'rebuild\')'.format(table)
            )


def remove_demo_data(conn: sqlite3.Connection) -> Dict[str, int]:
    """只清理带稳定标记的官网演示数据。"""
    counts: Dict[str, int] = {}
    demo_timeline_ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT id FROM timelines WHERE content_origin = ?", (DEMO_MARKER,)
        )
    ]
    demo_capture_ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT id FROM captures WHERE screenshot_source = ?",
            (DEMO_CAPTURE_SOURCE,),
        )
    ]
    demo_document_ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT id FROM bake_documents WHERE generation_version = ?", (DEMO_MARKER,)
        )
    ]
    demo_sop_ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT id FROM bake_sops WHERE content LIKE ?",
            ("%{}%".format(DEMO_MARKER),),
        )
    ]
    demo_data_source_ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT id FROM data_sources WHERE canonical_key LIKE ?",
            (DEMO_DATA_KEY_PREFIX + "%",),
        )
    ]
    demo_knowledge_ids: List[int] = []
    if demo_timeline_ids:
        placeholders = ",".join("?" for _ in demo_timeline_ids)
        demo_knowledge_ids = [
            int(row[0])
            for row in conn.execute(
                "SELECT id FROM bake_knowledge WHERE timeline_id IN ({})".format(placeholders),
                demo_timeline_ids,
            )
        ]

    if (
        demo_document_ids
        and table_exists(conn, "artifact_vector_index")
        and table_exists(conn, "vector_deletion_queue")
    ):
        placeholders = ",".join("?" for _ in demo_document_ids)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO vector_deletion_queue ("
            "qdrant_point_id, source_type, reason, enqueued_at"
            ") SELECT qdrant_point_id, 'document', 'demo_data_replaced', ? "
            "FROM artifact_vector_index WHERE document_id IN ({})".format(placeholders),
            [int(datetime.now().timestamp() * 1000)] + demo_document_ids,
        )
        counts["vector_deletion_queue"] = cursor.rowcount

    if demo_document_ids and table_exists(conn, "bake_document_sections"):
        placeholders = ",".join("?" for _ in demo_document_ids)
        cursor = conn.execute(
            "DELETE FROM bake_document_sections WHERE document_id IN ({})".format(
                placeholders
            ),
            demo_document_ids,
        )
        counts["bake_document_sections"] = cursor.rowcount

    if demo_document_ids and table_exists(conn, "bake_document_source_fingerprints"):
        placeholders = ",".join("?" for _ in demo_document_ids)
        cursor = conn.execute(
            "DELETE FROM bake_document_source_fingerprints WHERE document_id IN ({})".format(
                placeholders
            ),
            demo_document_ids,
        )
        counts["bake_document_source_fingerprints"] = cursor.rowcount

    if demo_knowledge_ids and table_exists(conn, "bake_artifact_source_links"):
        placeholders = ",".join("?" for _ in demo_knowledge_ids)
        cursor = conn.execute(
            "DELETE FROM bake_artifact_source_links "
            "WHERE artifact_kind = 'knowledge' AND artifact_id IN ({})".format(placeholders),
            demo_knowledge_ids,
        )
        counts["bake_artifact_source_links"] = cursor.rowcount

    if demo_knowledge_ids and table_exists(conn, "bake_artifact_source_fingerprints"):
        placeholders = ",".join("?" for _ in demo_knowledge_ids)
        cursor = conn.execute(
            "DELETE FROM bake_artifact_source_fingerprints "
            "WHERE artifact_kind = 'knowledge' AND artifact_id IN ({})".format(
                placeholders
            ),
            demo_knowledge_ids,
        )
        counts["bake_artifact_source_fingerprints"] = cursor.rowcount

    if demo_sop_ids and table_exists(conn, "bake_artifact_source_links"):
        placeholders = ",".join("?" for _ in demo_sop_ids)
        conn.execute(
            "DELETE FROM bake_artifact_source_links "
            "WHERE artifact_kind = 'sop' AND artifact_id IN ({})".format(placeholders),
            demo_sop_ids,
        )
    if demo_sop_ids and table_exists(conn, "bake_artifact_source_fingerprints"):
        placeholders = ",".join("?" for _ in demo_sop_ids)
        conn.execute(
            "DELETE FROM bake_artifact_source_fingerprints "
            "WHERE artifact_kind = 'sop' AND artifact_id IN ({})".format(placeholders),
            demo_sop_ids,
        )

    if demo_data_source_ids:
        placeholders = ",".join("?" for _ in demo_data_source_ids)
        if table_exists(conn, "data_source_links"):
            conn.execute(
                "DELETE FROM data_source_links WHERE source_id IN ({})".format(placeholders),
                demo_data_source_ids,
            )
        conn.execute(
            "DELETE FROM data_snapshots WHERE source_id IN ({})".format(placeholders),
            demo_data_source_ids,
        )
        cursor = conn.execute(
            "DELETE FROM data_sources WHERE id IN ({})".format(placeholders),
            demo_data_source_ids,
        )
        counts["data_sources"] = cursor.rowcount
    else:
        counts["data_sources"] = 0

    if demo_timeline_ids and table_exists(conn, "timeline_data_facts"):
        placeholders = ",".join("?" for _ in demo_timeline_ids)
        cursor = conn.execute(
            "DELETE FROM timeline_data_facts WHERE timeline_id IN ({})".format(placeholders),
            demo_timeline_ids,
        )
        counts["timeline_data_facts"] = cursor.rowcount

    if demo_timeline_ids:
        placeholders = ",".join("?" for _ in demo_timeline_ids)
        cursor = conn.execute(
            "DELETE FROM bake_knowledge WHERE timeline_id IN ({})".format(placeholders),
            demo_timeline_ids,
        )
        counts["bake_knowledge"] = cursor.rowcount

        if table_exists(conn, "bake_sops"):
            cursor = conn.execute(
                "DELETE FROM bake_sops WHERE timeline_id IN ({})".format(placeholders),
                demo_timeline_ids,
            )
            counts["bake_sops"] = cursor.rowcount
    else:
        counts["bake_sops"] = 0

    cursor = conn.execute(
        "DELETE FROM bake_documents WHERE generation_version = ?", (DEMO_MARKER,)
    )
    counts["bake_documents"] = cursor.rowcount

    cursor = conn.execute("DELETE FROM rag_sessions WHERE scene_type = ?", (DEMO_MARKER,))
    counts["rag_sessions"] = cursor.rowcount

    cursor = conn.execute(
        "DELETE FROM creation_history WHERE session_id LIKE ?",
        (DEMO_CREATION_SESSION_PREFIX + "%",),
    )
    counts["creation_history"] = cursor.rowcount

    cursor = conn.execute("DELETE FROM timelines WHERE content_origin = ?", (DEMO_MARKER,))
    counts["timelines"] = cursor.rowcount

    if demo_capture_ids:
        placeholders = ",".join("?" for _ in demo_capture_ids)
        if table_exists(conn, "vector_index"):
            cursor = conn.execute(
                "DELETE FROM vector_index WHERE capture_id IN ({})".format(placeholders),
                demo_capture_ids,
            )
            counts["vector_index"] = cursor.rowcount
        # 一些升级自早期版本的数据库仍保留 knowledge_ad。该触发器引用了
        # knowledge_fts_backup 的旧隐藏列名，会让任何 captures 删除在编译级失败，
        # 即使没有关联的 legacy knowledge 行。事务内暂时移除并原样恢复即可。
        trigger_definitions = suspend_triggers(conn, ["knowledge_ad"])
        try:
            cursor = conn.execute(
                "DELETE FROM captures WHERE id IN ({})".format(placeholders),
                demo_capture_ids,
            )
        finally:
            restore_triggers(conn, trigger_definitions)
        counts["captures"] = cursor.rowcount
    else:
        counts["captures"] = 0

    return counts


def supplemental_topics() -> List[Dict[str, str]]:
    """补充一组彼此关联、适合列表和详情页截图的真实产品工作主题。"""
    return [
        {
            "key": "onboarding",
            "title": "复盘新手引导漏斗，确定首日激活优化方案",
            "overview": "将首次使用流程从五步收敛为三步，内测完成率由 62% 提升到 74%。",
            "category": "增长分析",
            "focus": "新手引导",
            "metric": "完成率提升 12 个百分点",
            "doc_type": "增长分析报告",
        },
        {
            "key": "beta_feedback",
            "title": "整理 37 条灰度反馈并完成问题分级",
            "overview": "将反馈归并为准确性、生成速度、分享体验和隐私提示四类，明确 5 项上线前修复。",
            "category": "用户反馈",
            "focus": "灰度反馈",
            "metric": "37 条反馈、5 项 P0 修复",
            "doc_type": "反馈分析报告",
        },
        {
            "key": "action_items",
            "title": "完成行动项识别专项评测与误差归因",
            "overview": "在 120 条行动项样本上达到 93.1% 识别准确率，主要误差来自隐含责任人。",
            "category": "质量评测",
            "focus": "行动项识别",
            "metric": "准确率 93.1%",
            "doc_type": "专项评测报告",
        },
        {
            "key": "share_flow",
            "title": "优化纪要分享漏斗并减少二次编辑流失",
            "overview": "合并确认与分享步骤后，分享前流失率从 18% 降至 7%。",
            "category": "体验优化",
            "focus": "分享链路",
            "metric": "流失率下降 11 个百分点",
            "doc_type": "体验优化方案",
        },
        {
            "key": "template_system",
            "title": "确定纪要模板体系与默认推荐规则",
            "overview": "首发提供项目例会、客户访谈、复盘会三类模板，并按会议上下文推荐默认模板。",
            "category": "功能设计",
            "focus": "纪要模板",
            "metric": "覆盖 3 类高频会议",
            "doc_type": "功能设计方案",
        },
        {
            "key": "multilingual",
            "title": "验证中英混合会议转写与摘要策略",
            "overview": "专有名词保留原文、结论统一中文表达，中英混合样本摘要可用率达到 92.4%。",
            "category": "能力验证",
            "focus": "中英混合会议",
            "metric": "摘要可用率 92.4%",
            "doc_type": "能力验证报告",
        },
        {
            "key": "privacy_review",
            "title": "完成本地优先与隐私提示专项评审",
            "overview": "明确原始转写、截图和推理结果默认本地保存，云端增强必须由用户主动开启。",
            "category": "隐私评审",
            "focus": "本地优先",
            "metric": "7 项隐私检查全部通过",
            "doc_type": "隐私设计说明",
        },
        {
            "key": "performance",
            "title": "完成长会议性能压测并收敛延迟瓶颈",
            "overview": "60 分钟会议的纪要生成 P95 从 176 秒降至 128 秒，峰值内存下降 18%。",
            "category": "性能优化",
            "focus": "长会议性能",
            "metric": "P95 降至 128 秒",
            "doc_type": "性能优化报告",
        },
        {
            "key": "accessibility",
            "title": "完成键盘操作与可访问性走查",
            "overview": "主流程支持完整键盘导航，修复 14 处焦点顺序、对比度和读屏标签问题。",
            "category": "可访问性",
            "focus": "键盘导航",
            "metric": "修复 14 项问题",
            "doc_type": "可访问性检查报告",
        },
        {
            "key": "analytics",
            "title": "统一灰度指标口径与埋点字典",
            "overview": "完成 28 个核心事件的命名、触发条件和去重规则，保证产品与数据看板口径一致。",
            "category": "数据分析",
            "focus": "埋点字典",
            "metric": "统一 28 个核心事件",
            "doc_type": "指标口径文档",
        },
        {
            "key": "customer_success",
            "title": "设计企业客户首周启用与陪跑流程",
            "overview": "将管理员配置、样板会议和团队复盘安排进五个工作日，首周激活目标设为 80%。",
            "category": "客户成功",
            "focus": "企业启用",
            "metric": "首周激活目标 80%",
            "doc_type": "客户成功方案",
        },
        {
            "key": "support_playbook",
            "title": "建立灰度期客服分流与升级机制",
            "overview": "按转写、纪要、行动项、分享和隐私五类问题分流，P0 问题 15 分钟内升级研发值班。",
            "category": "服务运营",
            "focus": "客服分流",
            "metric": "P0 升级时限 15 分钟",
            "doc_type": "服务运营手册",
        },
        {
            "key": "sales_enablement",
            "title": "完成企业版演示脚本与价值证明材料",
            "overview": "围绕节省整理时间、减少责任遗漏和本地隐私三项价值，形成 15 分钟标准演示路径。",
            "category": "销售赋能",
            "focus": "企业演示",
            "metric": "标准演示时长 15 分钟",
            "doc_type": "销售演示手册",
        },
        {
            "key": "pricing",
            "title": "完成定价访谈并确定套餐表达方式",
            "overview": "用户更容易理解按席位与会议时长组合的套餐，团队版意向转化率达到 12.8%。",
            "category": "商业化",
            "focus": "套餐设计",
            "metric": "团队版意向转化率 12.8%",
            "doc_type": "定价研究报告",
        },
        {
            "key": "mobile",
            "title": "评审移动端会后提醒与快速确认方案",
            "overview": "移动端只承载纪要完成提醒、行动项确认和快速分享，不复制桌面端完整编辑能力。",
            "category": "移动体验",
            "focus": "会后提醒",
            "metric": "通知打开率目标 55%",
            "doc_type": "移动端设计方案",
        },
        {
            "key": "integrations",
            "title": "确定日历与协作平台首批集成范围",
            "overview": "首批支持系统日历、飞书和钉钉，统一会议识别、纪要回写和失败重试协议。",
            "category": "生态集成",
            "focus": "协作平台集成",
            "metric": "首批覆盖 3 个平台",
            "doc_type": "集成设计方案",
        },
        {
            "key": "release_readiness",
            "title": "完成正式发布前的跨团队就绪检查",
            "overview": "产品、研发、测试、客服和市场共同核对 42 项检查点，当前完成度达到 92%。",
            "category": "发布管理",
            "focus": "发布就绪",
            "metric": "42 项检查、完成度 92%",
            "doc_type": "发布检查报告",
        },
        {
            "key": "retrospective",
            "title": "完成灰度首周复盘并确认下一阶段重点",
            "overview": "核心指标达到扩量门槛，下一阶段优先解决多人重叠发言和模板个性化问题。",
            "category": "项目复盘",
            "focus": "灰度复盘",
            "metric": "3 项核心指标全部达标",
            "doc_type": "项目复盘报告",
        },
    ]


def supplemental_timeline_specs() -> List[Dict[str, Any]]:
    app_pairs = [
        (("Chrome", "com.google.Chrome"), ("飞书", "com.bytedance.Feishu")),
        (("Numbers", "com.apple.Numbers"), ("飞书", "com.bytedance.Feishu")),
        (("Visual Studio Code", "com.microsoft.VSCode"), ("Chrome", "com.google.Chrome")),
        (("Figma", "com.figma.Desktop"), ("飞书会议", "com.bytedance.Feishu")),
    ]
    specs: List[Dict[str, Any]] = []
    for index, topic in enumerate(supplemental_topics()):
        first_app, second_app = app_pairs[index % len(app_pairs)]
        duration = 58 + (index % 5) * 7
        hour = (9, 10, 14, 15)[index % 4]
        minute = (10, 25, 5, 40)[index % 4]
        specs.append(
            {
                "key": topic["key"],
                "days_ago": 6 + index,
                "start": (hour, minute),
                "duration": duration,
                "summary": topic["title"],
                "overview": topic["overview"],
                "details": (
                    "围绕“{}”完成资料核对、方案评审和结果确认。关键证据为{}。"
                    "团队已经把结论转化为可执行动作，并明确负责人、验收口径与复盘时间。"
                ).format(topic["focus"], topic["metric"]),
                "entities": ["AI 会议助手 2.0", topic["focus"], topic["metric"]],
                "category": topic["category"],
                "importance": 7 + index % 4,
                "occurrence_count": 3 + index % 6,
                "activity_type": "demo_{}".format(topic["key"]),
                "evidence_strength": "high" if index % 3 else "medium",
                "captures": [
                    {
                        "offset": 0,
                        "app_name": first_app[0],
                        "bundle": first_app[1],
                        "title": "{}｜资料与数据".format(topic["focus"]),
                        "url": "https://demo.memorybread.local/work/{}".format(topic["key"]),
                        "event": "scroll",
                        "text": "{}。当前结果：{}。".format(topic["overview"], topic["metric"]),
                    },
                    {
                        "offset": duration // 2,
                        "app_name": second_app[0],
                        "bundle": second_app[1],
                        "title": "{}｜评审结论".format(topic["focus"]),
                        "url": "https://demo.memorybread.local/reviews/{}".format(topic["key"]),
                        "event": "key_pause",
                        "text": "评审结论：{}。后续按负责人、验收指标和复盘时间推进。".format(topic["overview"]),
                        "input": "已确认：{}。".format(topic["metric"]),
                    },
                ],
                "segments": [
                    (0, duration // 3, "核对{}的事实与指标".format(topic["focus"])),
                    (duration // 3 + 2, duration * 2 // 3, "评审方案与关键取舍"),
                    (duration * 2 // 3 + 2, duration, "确认责任人与验收口径"),
                ],
            }
        )
    return specs


def timeline_specs() -> List[Dict[str, Any]]:
    base = [
        {
            "key": "research",
            "days_ago": 5,
            "start": (9, 20),
            "duration": 74,
            "summary": "回看用户访谈，确认会后纪要速度是首要诉求",
            "overview": "整理 8 位高频会议用户的访谈记录，确认“会后立即可分享”比更多编辑功能更重要。",
            "details": (
                "上午集中回看用户访谈与客服反馈，归纳出三个高频痛点：会后整理耗时、责任人容易遗漏、"
                "跨团队转发前需要再次润色。产品侧将核心体验收敛为：会议结束后 3 分钟内生成结构化纪要，"
                "并能直接复制到群聊或项目文档。"
            ),
            "entities": ["AI 会议助手 2.0", "用户访谈", "结构化纪要", "P0 需求"],
            "category": "产品研究",
            "importance": 9,
            "occurrence_count": 8,
            "activity_type": "research_synthesis",
            "evidence_strength": "high",
            "captures": [
                {
                    "offset": 0,
                    "app_name": "Chrome",
                    "bundle": "com.google.Chrome",
                    "title": "会议纪要产品调研与用户访谈汇总",
                    "url": "https://demo.memorybread.local/research/interviews",
                    "event": "scroll",
                    "text": "用户原话：我最需要的不是更多编辑按钮，而是会一结束就有一份能直接发出去的纪要。8 位受访者中有 6 位把生成速度列为首要因素。",
                },
                {
                    "offset": 38,
                    "app_name": "飞书",
                    "bundle": "com.bytedance.Feishu",
                    "title": "用户访谈洞察整理",
                    "url": "https://demo.memorybread.local/docs/research-notes",
                    "event": "key_pause",
                    "text": "结论：将“会议结束后 3 分钟内生成结构化纪要”设为 P0 验收口径；默认输出结论、待办、风险和关键原话。",
                    "input": "首要价值不是转写更长，而是更快得到可直接分享的会议结论。",
                },
            ],
            "segments": [
                (0, 28, "阅读用户原话并标注高频痛点"),
                (31, 51, "对 8 份访谈记录进行主题归类"),
                (55, 74, "形成 P0 体验目标与验收口径"),
            ],
        },
        {
            "key": "competitive",
            "days_ago": 4,
            "start": (10, 5),
            "duration": 68,
            "summary": "完成会议助手竞品分析，找到三项差异化机会",
            "overview": "对比 5 款会议产品的转写、摘要、待办和分享链路，明确中文行动项识别与本地隐私是突破口。",
            "details": (
                "竞品普遍能生成摘要，但在中文口语中的责任人识别、截止时间归一化和跨应用分享上仍有明显断点。"
                "方案决定强化三点：行动项自动结构化、本地优先保存原始转写、分享前允许一键精简。"
            ),
            "entities": ["竞品分析", "行动项", "本地优先", "中文会议"],
            "category": "竞品分析",
            "importance": 8,
            "occurrence_count": 5,
            "activity_type": "competitive_analysis",
            "evidence_strength": "high",
            "captures": [
                {
                    "offset": 0,
                    "app_name": "Chrome",
                    "bundle": "com.google.Chrome",
                    "title": "会议产品能力对比表",
                    "url": "https://demo.memorybread.local/research/competitive-matrix",
                    "event": "scroll",
                    "text": "对比维度：转写准确率、说话人识别、摘要结构、行动项提取、分享链路、隐私模式。5 款产品中仅 1 款支持原始转写本地保存。",
                },
                {
                    "offset": 41,
                    "app_name": "Numbers",
                    "bundle": "com.apple.Numbers",
                    "title": "竞品能力评分与机会点",
                    "url": "https://demo.memorybread.local/data/competitive-score",
                    "event": "mouse_click",
                    "text": "差异化机会：中文行动项识别、本地优先、纪要精简后快速分享。优先级评分分别为 4.8、4.6、4.3。",
                },
            ],
            "segments": [
                (0, 25, "补齐 5 款产品的关键能力对比"),
                (28, 49, "按用户价值和实现成本进行评分"),
                (52, 68, "确定 2.0 的三项差异化方向"),
            ],
        },
        {
            "key": "scope",
            "days_ago": 3,
            "start": (14, 0),
            "duration": 96,
            "summary": "完成 AI 会议助手 2.0 范围评审",
            "overview": "产品、设计和研发对齐版本边界：首发聚焦快速纪要、行动项和可控分享，暂缓情绪分析与自动建群。",
            "details": (
                "范围评审采用用户价值、风险和交付成本三项评分。最终纳入 4 个 P0：实时转写、会后 3 分钟纪要、"
                "行动项责任人识别、分享前快速编辑；P1 保留自定义纪要模板和多语言翻译。"
            ),
            "entities": ["范围评审", "版本规划", "P0", "P1"],
            "category": "产品规划",
            "importance": 10,
            "occurrence_count": 6,
            "activity_type": "scope_review",
            "evidence_strength": "high",
            "captures": [
                {
                    "offset": 0,
                    "app_name": "飞书",
                    "bundle": "com.bytedance.Feishu",
                    "title": "AI 会议助手 2.0 需求池",
                    "url": "https://demo.memorybread.local/docs/requirements",
                    "event": "key_pause",
                    "text": "P0：实时转写、3 分钟内生成纪要、行动项责任人识别、分享前快速编辑。P1：自定义模板、多语言翻译。暂不做：情绪分析、自动建群。",
                    "input": "首发版本只承诺可衡量、可验证、与核心链路直接相关的能力。",
                },
                {
                    "offset": 58,
                    "app_name": "飞书会议",
                    "bundle": "com.bytedance.Feishu",
                    "title": "2.0 范围评审会议",
                    "url": "https://demo.memorybread.local/meetings/scope-review",
                    "event": "manual",
                    "text": "评审结论：所有 P0 均需在 30 分钟中文会议样本上通过验收；不以功能数量作为上线标准。",
                    "audio": "先把会后纪要这一条链路做透，情绪分析放到后续验证，不进入这次首发。",
                },
            ],
            "segments": [
                (0, 37, "逐项确认需求价值与证据"),
                (41, 70, "评估交付成本并划分 P0/P1"),
                (74, 96, "确认暂缓项与统一验收样本"),
            ],
        },
        {
            "key": "technical",
            "days_ago": 2,
            "start": (9, 35),
            "duration": 82,
            "summary": "评审实时转写与说话人识别技术方案",
            "overview": "确定端侧降噪、流式转写和云端可选增强的组合方案，弱网时优先保证文本不断流。",
            "details": (
                "技术评审确定双层策略：默认使用端侧 VAD 与降噪，流式转写按 2 秒窗口增量输出；说话人识别置信度"
                "低于 0.72 时不强行命名，而是标记为“待确认发言人”。云端增强必须由用户明确开启。"
            ),
            "entities": ["实时转写", "说话人识别", "端侧降噪", "弱网"],
            "category": "技术方案",
            "importance": 8,
            "occurrence_count": 4,
            "activity_type": "technical_review",
            "evidence_strength": "medium",
            "captures": [
                {
                    "offset": 0,
                    "app_name": "Visual Studio Code",
                    "bundle": "com.microsoft.VSCode",
                    "title": "streaming-transcription-design.md",
                    "url": "https://demo.memorybread.local/engineering/transcription-design",
                    "event": "key_pause",
                    "text": "流式窗口 2s，弱网缓存上限 30s。speaker confidence < 0.72 时输出待确认发言人，不进行错误归属。",
                    "input": "隐私默认值：端侧处理；云端增强需显式开启。",
                },
                {
                    "offset": 46,
                    "app_name": "飞书会议",
                    "bundle": "com.bytedance.Feishu",
                    "title": "实时转写技术评审",
                    "url": "https://demo.memorybread.local/meetings/technical-review",
                    "event": "manual",
                    "text": "降级顺序：保文本连续 > 保时间戳 > 保说话人标签。弱网恢复后仅补齐缺失片段，不覆盖用户已编辑内容。",
                    "audio": "说话人不确定时宁可让用户确认，也不要自信地分错人。",
                },
            ],
            "segments": [
                (0, 31, "确认端侧处理与流式窗口参数"),
                (35, 59, "评估弱网降级和恢复策略"),
                (63, 82, "确定说话人识别置信度阈值"),
            ],
        },
        {
            "key": "prototype",
            "days_ago": 1,
            "start": (14, 20),
            "duration": 105,
            "summary": "完成会后纪要高保真原型与交互评审",
            "overview": "打通“结束会议—生成纪要—确认待办—分享”主路径，并将首次可分享时间压缩到 3 分钟内。",
            "details": (
                "交互评审去掉了首屏复杂配置，将会后页面收敛为结论、待办、关键原话三个区块。用户可以直接确认"
                "责任人和截止日期，再选择复制、导出或分享到群聊；高级模板放入二级入口。"
            ),
            "entities": ["高保真原型", "会后纪要", "行动项", "分享链路"],
            "category": "交互设计",
            "importance": 9,
            "occurrence_count": 7,
            "activity_type": "prototype_review",
            "evidence_strength": "high",
            "captures": [
                {
                    "offset": 0,
                    "app_name": "Figma",
                    "bundle": "com.figma.Desktop",
                    "title": "AI 会议助手 2.0｜会后纪要原型",
                    "url": "https://demo.memorybread.local/design/post-meeting-flow",
                    "event": "mouse_click",
                    "text": "会后首页：一句话结论、行动项列表、关键原话。主按钮：确认并分享。次要入口：编辑全文、自定义模板。",
                },
                {
                    "offset": 63,
                    "app_name": "飞书",
                    "bundle": "com.bytedance.Feishu",
                    "title": "2.0 交互评审纪要",
                    "url": "https://demo.memorybread.local/docs/design-review",
                    "event": "key_pause",
                    "text": "评审通过：默认视图只呈现用户下一步需要做决定的信息；将“高级设置”从主路径移出。",
                    "input": "分享前必须允许快速修正责任人，避免错误行动项扩散。",
                },
            ],
            "segments": [
                (0, 36, "走查会后纪要信息层级"),
                (40, 72, "优化待办确认与快速修正交互"),
                (76, 105, "确认分享路径和二级设置入口"),
            ],
        },
        {
            "key": "metrics",
            "days_ago": 0,
            "start": (9, 30),
            "duration": 88,
            "summary": "敲定灰度发布指标与首周观察计划",
            "overview": "确定 10% 灰度范围和三项核心指标：纪要生成成功率、3 分钟可分享率、行动项确认率。",
            "details": (
                "首周面向 200 名高频会议用户进行 10% 灰度。上线门槛：纪要生成成功率 ≥ 98%，3 分钟内可分享率"
                "≥ 90%，行动项确认率 ≥ 55%。同时监控错误责任人反馈率，超过 3% 自动暂停扩量。"
            ),
            "entities": ["灰度发布", "成功指标", "埋点", "风险阈值"],
            "category": "发布计划",
            "importance": 10,
            "occurrence_count": 5,
            "activity_type": "launch_planning",
            "evidence_strength": "high",
            "captures": [
                {
                    "offset": 0,
                    "app_name": "飞书",
                    "bundle": "com.bytedance.Feishu",
                    "title": "AI 会议助手 2.0 灰度发布方案",
                    "url": "https://demo.memorybread.local/docs/launch-plan",
                    "event": "scroll",
                    "text": "灰度范围：200 名高频用户，10% 流量。核心指标：生成成功率≥98%，3 分钟可分享率≥90%，行动项确认率≥55%。",
                },
                {
                    "offset": 52,
                    "app_name": "Numbers",
                    "bundle": "com.apple.Numbers",
                    "title": "2.0 指标口径与看板",
                    "url": "https://demo.memorybread.local/data/launch-metrics",
                    "event": "mouse_click",
                    "text": "风险护栏：错误责任人反馈率 >3% 暂停扩量；P95 纪要生成时长 >180 秒持续 30 分钟触发告警。",
                },
            ],
            "segments": [
                (0, 27, "确认灰度人群与流量范围"),
                (31, 61, "统一三项核心指标计算口径"),
                (65, 88, "设置暂停扩量阈值与首周值班安排"),
            ],
        },
    ]
    return base + supplemental_timeline_specs()


def insert_capture(
    conn: sqlite3.Connection,
    timestamp_ms: int,
    capture: Dict[str, Any],
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO captures (
            ts, app_name, app_bundle_id, win_title, event_type,
            ax_text, ax_focused_role, ax_focused_id, ocr_text,
            screenshot_path, input_text, audio_text, is_sensitive, pii_scrubbed,
            screenshot_source, url, webpage_title
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?)
        """,
        (
            timestamp_ms,
            capture["app_name"],
            capture["bundle"],
            capture["title"],
            capture["event"],
            capture["text"],
            "AXWebArea" if capture["app_name"] == "Chrome" else "AXTextArea",
            "demo-content",
            None,
            None,
            capture.get("input"),
            capture.get("audio"),
            DEMO_CAPTURE_SOURCE,
            capture["url"],
            capture["title"],
        ),
    )
    return int(cursor.lastrowid)


def insert_timelines(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    inserted: Dict[str, Dict[str, Any]] = {}
    now_ms = int(datetime.now().timestamp() * 1000)
    for index, spec in enumerate(timeline_specs()):
        start_ms = business_time(spec["days_ago"], spec["start"][0], spec["start"][1])
        end_ms = start_ms + spec["duration"] * 60 * 1000
        updated_ms = now_ms - index * 1000
        capture_ids: List[int] = []
        for capture in spec["captures"]:
            capture_id = insert_capture(
                conn, start_ms + capture["offset"] * 60 * 1000, capture
            )
            capture_ids.append(capture_id)

        key_timestamps = [
            {
                "capture_ids": [
                    capture_id
                    for capture_id, capture in zip(capture_ids, spec["captures"])
                    if start_offset <= capture["offset"] <= end_offset
                ],
                "start_ts": start_ms + start_offset * 60 * 1000,
                "end_ts": start_ms + end_offset * 60 * 1000,
                "summary": summary,
            }
            for start_offset, end_offset, summary in spec["segments"]
        ]
        cursor = conn.execute(
            """
            INSERT INTO timelines (
                capture_id, summary, overview, details, entities, category,
                importance, occurrence_count, user_verified, user_edited,
                created_at, updated_at, capture_ids, start_time, end_time,
                duration_minutes, frag_app_name, frag_win_title, observed_at,
                event_time_start, event_time_end, history_view, content_origin,
                activity_type, is_self_generated, evidence_strength,
                created_at_ms, updated_at_ms, time_range_start, time_range_end,
                key_timestamps
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                capture_ids[0],
                spec["summary"],
                spec["overview"],
                spec["details"],
                json_text(spec["entities"]),
                spec["category"],
                spec["importance"],
                spec["occurrence_count"],
                iso_local(start_ms),
                iso_local(updated_ms),
                json_text(capture_ids),
                start_ms,
                end_ms,
                spec["duration"],
                spec["captures"][0]["app_name"],
                spec["captures"][0]["title"],
                end_ms,
                start_ms,
                end_ms,
                DEMO_MARKER,
                spec["activity_type"],
                spec["evidence_strength"],
                start_ms,
                updated_ms,
                start_ms,
                end_ms,
                json_text(key_timestamps),
            ),
        )
        timeline_id = int(cursor.lastrowid)
        conn.execute(
            "UPDATE captures SET timeline_id = ? WHERE id IN ({})".format(
                ",".join("?" for _ in capture_ids)
            ),
            [timeline_id] + capture_ids,
        )
        inserted[spec["key"]] = {
            "id": timeline_id,
            "capture_ids": capture_ids,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "summary": spec["summary"],
        }
    return inserted


def supplemental_content_topics() -> List[Dict[str, str]]:
    competitive = {
        "key": "competitive",
        "title": "会议助手竞品差异化机会已经收敛",
        "overview": "中文行动项识别、本地优先与分享前快速精简是最值得投入的三项机会。",
        "category": "竞品分析",
        "focus": "竞品差异化",
        "metric": "对比 5 款产品、确认 3 项机会",
        "doc_type": "竞品分析报告",
    }
    return [competitive] + supplemental_topics()


def supplemental_knowledge_specs() -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    for index, topic in enumerate(supplemental_content_topics()):
        specs.append(
            {
                "key": "insight_{}".format(topic["key"]),
                "timeline": topic["key"],
                "title": "{}：关键结论与复用原则".format(topic["focus"]),
                "summary": topic["overview"],
                "importance": 7 + index % 4,
                "entities": [topic["focus"], topic["metric"], "产品决策"],
                "overview": "由近期工作记录沉淀的可复用结论，适合后续咨询与创作引用。",
                "details": """## 结论

{}

## 关键证据

- {}；
- 来源包含原始资料、评审记录与量化结果；
- 结论已转化为明确的负责人和验收口径。

## 复用建议

后续遇到相似项目时，先核对适用范围和数据时效，再沿用本结论，避免把阶段性结果直接当作长期事实。""".format(
                    topic["overview"], topic["metric"]
                ),
            }
        )
    return specs


def knowledge_specs() -> List[Dict[str, Any]]:
    base = [
        {
            "key": "speed",
            "timeline": "research",
            "title": "会后 3 分钟可分享：核心体验与验收口径",
            "summary": "用户把“快速得到可直接分享的纪要”视为首要价值",
            "importance": 10,
            "entities": ["用户价值", "3 分钟", "结构化纪要", "验收标准"],
            "overview": "基于 8 位高频会议用户访谈形成的 P0 产品结论。",
            "details": """## 结论

AI 会议助手 2.0 的首要体验不是增加编辑功能，而是让用户在会议结束后 **3 分钟内** 拿到可直接分享的结构化纪要。

## 验收标准

- 30 分钟中文会议结束后，3 分钟内出现首版纪要；
- 默认包含一句话结论、行动项、风险与关键原话；
- 行动项尽可能识别责任人和截止时间；
- 用户可在分享前快速修正，不必进入完整编辑器。

## 证据

8 位受访者中有 6 位将“生成速度”列为首要因素。典型原话是：“我最需要的不是更多编辑按钮，而是会一结束就有一份能直接发出去的纪要。”""",
        },
        {
            "key": "scope",
            "timeline": "scope",
            "title": "AI 会议助手 2.0 版本范围与优先级",
            "summary": "首发聚焦 4 个 P0，情绪分析和自动建群暂缓",
            "importance": 10,
            "entities": ["版本范围", "P0", "P1", "产品决策"],
            "overview": "产品、设计与研发在范围评审中共同确认的版本边界。",
            "details": """## P0：首发必须交付

1. 实时转写；
2. 会后 3 分钟内生成结构化纪要；
3. 行动项责任人识别；
4. 分享前快速编辑。

## P1：验证后迭代

- 自定义纪要模板；
- 多语言翻译。

## 暂缓

情绪分析、自动建群。首发以核心链路通过可量化验收为准，不以功能数量为上线标准。""",
        },
        {
            "key": "speaker",
            "timeline": "technical",
            "title": "说话人识别采用“低置信度待确认”策略",
            "summary": "置信度低于 0.72 时不强行归属发言人",
            "importance": 8,
            "entities": ["说话人识别", "置信度", "端侧处理", "弱网降级"],
            "overview": "技术方案优先避免错误责任人扩散，而不是追求表面完整。",
            "details": """## 决策

- 默认使用端侧 VAD 与降噪；
- 流式转写按 2 秒窗口增量输出；
- 说话人识别置信度低于 **0.72** 时显示“待确认发言人”；
- 弱网降级顺序：保文本连续 > 保时间戳 > 保说话人标签；
- 云端增强能力必须由用户明确开启。

这个策略直接降低了错误行动项被分配给错误责任人的风险。""",
        },
        {
            "key": "interaction",
            "timeline": "prototype",
            "title": "会后纪要主路径的信息层级原则",
            "summary": "默认只呈现结论、行动项与关键原话",
            "importance": 9,
            "entities": ["信息架构", "会后页面", "行动项", "分享"],
            "overview": "高保真原型评审后形成的会后体验设计原则。",
            "details": """## 页面结构

会后首屏只呈现三类信息：

1. 一句话结论；
2. 可确认的行动项列表；
3. 支撑结论的关键原话。

用户确认责任人和截止日期后即可复制、导出或分享到群聊。高级模板和全文编辑保留，但不打断首次分享路径。""",
        },
        {
            "key": "metrics",
            "timeline": "metrics",
            "title": "AI 会议助手 2.0 灰度指标与风险护栏",
            "summary": "首周用三项核心指标判断是否扩量",
            "importance": 10,
            "entities": ["灰度发布", "成功率", "可分享率", "行动项确认率"],
            "overview": "面向 200 名高频会议用户的 10% 灰度观察口径。",
            "details": """## 核心指标

| 指标 | 首周门槛 |
| --- | ---: |
| 纪要生成成功率 | ≥ 98% |
| 3 分钟内可分享率 | ≥ 90% |
| 行动项确认率 | ≥ 55% |

## 风险护栏

错误责任人反馈率超过 **3%** 时自动暂停扩量；P95 纪要生成时长超过 180 秒并持续 30 分钟时触发告警。""",
        },
    ]
    return base + supplemental_knowledge_specs()


def insert_knowledge(
    conn: sqlite3.Connection, timelines: Dict[str, Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    inserted: Dict[str, Dict[str, Any]] = {}
    now_ms = int(datetime.now().timestamp() * 1000)
    for index, spec in enumerate(knowledge_specs()):
        source = timelines[spec["timeline"]]
        created_ms = source["end_ms"] + 12 * 60 * 1000
        metadata = {
            "status": "confirmed",
            "review_status": "confirmed",
            "source_timeline_id": source["id"],
            "source_capture_ids": [str(value) for value in source["capture_ids"]],
            "核心结论": spec["overview"],
            "match_score": round(0.94 - index * 0.01, 2),
            "match_level": "high",
        }
        cursor = conn.execute(
            """
            INSERT INTO bake_knowledge (
                title, summary, content, entities, importance, user_verified,
                user_edited, created_at, updated_at, created_at_ms, updated_at_ms,
                detailed_content, section_ids, source_timeline_ids, timeline_id,
                source_capture_ids
            ) VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, '[]', ?, ?, ?)
            """,
            (
                spec["title"],
                spec["summary"],
                json_text(metadata),
                json_text(spec["entities"]),
                spec["importance"],
                iso_local(created_ms),
                iso_local(now_ms),
                created_ms,
                now_ms - index * 1000,
                spec["details"],
                json_text([source["id"]]),
                source["id"],
                json_text(source["capture_ids"]),
            ),
        )
        inserted[spec["key"]] = {
            "id": int(cursor.lastrowid),
            "timeline_id": source["id"],
            "capture_ids": source["capture_ids"],
            "title": spec["title"],
            "summary": spec["summary"],
            "details": spec["details"],
        }
    return inserted


def supplemental_document_specs() -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    # 前五个通用主题改为真实会议纪要，既保持文档总数不变，也让创作页的
    # “记忆搜索”结果可以直接打开对应会议纪要查看来源。
    for topic in supplemental_content_topics()[5:]:
        specs.append(
            {
                "key": "brief_{}".format(topic["key"]),
                "title": "{}｜{}".format(topic["focus"], topic["doc_type"]),
                "doc_type": topic["doc_type"],
                "timeline_keys": [topic["key"]],
                "knowledge_keys": ["insight_{}".format(topic["key"])],
                "tags": ["AI 会议助手", topic["category"], topic["focus"]],
                "summary": topic["overview"],
                "sections": [
                    ("背景与目标", [topic["focus"], "目标"]),
                    ("事实与数据", [topic["metric"], "证据"]),
                    ("方案与取舍", ["方案", "边界"]),
                    ("执行与验收", ["责任人", "验收"]),
                ],
                "style_phrases": ["结论先行", "事实与判断分开", "指标必须可验证"],
                "prompt": "基于近期工作证据，输出{}，突出结论、数据、方案和验收标准。".format(
                    topic["doc_type"]
                ),
                "content": """# {}｜{}

## 一、背景与目标

本文件沉淀 AI 会议助手 2.0 在“{}”主题上的阶段性结论，用于团队评审、执行协同和后续复盘。

## 二、核心结论

{}

## 三、关键证据

- {}；
- 原始资料、讨论记录与最终评审结论已经相互核对；
- 结论具备明确的适用范围，不将阶段性观察扩大为长期事实。

## 四、执行方案

1. 以当前结论作为本阶段默认方案；
2. 在执行前再次核对数据时效和用户范围；
3. 将负责人、交付物和截止时间写入项目看板；
4. 达到验收口径后进入下一阶段，否则回到问题归因。

## 五、验收与复盘

验收以“{}”为核心证据，同时记录异常样本。项目组在下一里程碑完成后统一复盘，不以功能数量替代实际结果。""".format(
                    topic["focus"],
                    topic["doc_type"],
                    topic["focus"],
                    topic["overview"],
                    topic["metric"],
                    topic["metric"],
                ),
            }
        )

    meeting_minutes = [
        {
            "key": "minutes_scope_review",
            "title": "AI 会议助手 2.0 版本范围评审会议纪要",
            "timeline_keys": ["scope"],
            "knowledge_keys": ["scope"],
            "summary": "产品、设计与研发确认首发聚焦 4 个 P0，情绪分析和自动建群暂缓。",
            "decision": "首发只承诺实时转写、会后 3 分钟纪要、行动项责任人识别和分享前快速编辑。",
            "completed": "完成需求价值、风险与交付成本评分，形成 P0/P1 边界并通过三方评审。",
            "actions": "产品补齐验收用例；研发完成工作量拆分；测试准备 30 分钟中文会议回归集。",
        },
        {
            "key": "minutes_technical_review",
            "title": "说话人识别与弱网降级技术评审会议纪要",
            "timeline_keys": ["technical", "performance"],
            "knowledge_keys": ["speaker", "insight_performance"],
            "summary": "技术评审确认端侧降噪、低置信度待确认和弱网优先保文本连续的方案。",
            "decision": "说话人置信度低于 0.72 时不强行归属；弱网恢复不得覆盖用户已经编辑的内容。",
            "completed": "完成 60 分钟会议压测与 120 次断网注入，P95 生成时长降至 128 秒。",
            "actions": "补充多人重叠发言样本；接入 P95 告警；将错误归属率纳入每日回归。",
        },
        {
            "key": "minutes_experience_review",
            "title": "会后纪要主路径体验评审会议纪要",
            "timeline_keys": ["prototype", "template_system"],
            "knowledge_keys": ["interaction", "insight_template_system"],
            "summary": "设计、产品和用户研究共同确认首屏只突出结论、行动项与关键原话。",
            "decision": "确认责任人与截止日期后即可分享，高级模板和全文编辑不阻断首次分享。",
            "completed": "完成高保真原型评审，打通生成纪要、确认待办和分享的主路径。",
            "actions": "验证三类默认模板；继续压缩分享前操作；补齐键盘和读屏验收。",
        },
        {
            "key": "minutes_gray_review",
            "title": "灰度发布指标与风险护栏评审会议纪要",
            "timeline_keys": ["metrics", "release_readiness"],
            "knowledge_keys": ["metrics", "insight_release_readiness"],
            "summary": "跨团队确认 200 名用户、10% 流量的灰度方案和三项扩量指标。",
            "decision": "生成成功率不低于 98%、3 分钟可分享率不低于 90%、行动项确认率不低于 55%。",
            "completed": "完成指标口径、暂停扩量阈值和 42 项发布检查，当前就绪度达到 92%。",
            "actions": "每日 17:30 联合复盘；错误责任人反馈率超过 3% 时暂停扩量；P0 当日闭环。",
        },
        {
            "key": "minutes_weekly_review",
            "title": "AI 会议助手 2.0 本周项目复盘会议纪要",
            "timeline_keys": ["retrospective", "customer_success", "support_playbook"],
            "knowledge_keys": [
                "insight_retrospective",
                "insight_customer_success",
                "insight_support_playbook",
            ],
            "summary": "本周核心指标达到扩量门槛，下一阶段聚焦多人发言、模板个性化与企业启用。",
            "decision": "继续小步扩量，同时保持本地优先默认值和错误责任人 3% 风险护栏。",
            "completed": "完成企业客户首周陪跑流程、客服五类分流机制和首周灰度问题复盘。",
            "actions": "补齐高风险样本；跟进 2 项延期交付；对首批企业客户开展使用复盘。",
        },
    ]
    for minutes in meeting_minutes:
        specs.append(
            {
                "key": minutes["key"],
                "title": minutes["title"],
                "doc_type": "会议纪要",
                "timeline_keys": minutes["timeline_keys"],
                "knowledge_keys": minutes["knowledge_keys"],
                "tags": ["会议纪要", "本周复盘", "AI 会议助手"],
                "summary": minutes["summary"],
                "sections": [
                    ("会议结论", ["结论", "范围"]),
                    ("关键数据", ["指标", "证据"]),
                    ("本周完成", ["完成事项", "交付物"]),
                    ("后续行动", ["负责人", "截止时间"]),
                ],
                "style_phrases": ["结论先行", "保留指标口径", "行动项可追踪"],
                "prompt": "将本周评审讨论整理为会议纪要，保留决策、数据、完成事项和行动项。",
                "content": """# {}

## 会议结论

{}

## 关键决策

{}

## 本周已完成

{}

## 后续行动

{}

## 纪要确认

本纪要已经由产品、研发、设计、测试及相关协作方核对。数据结论均保留本周口径，后续如有刷新，以最新数据快照为准。""".format(
                    minutes["title"],
                    minutes["summary"],
                    minutes["decision"],
                    minutes["completed"],
                    minutes["actions"],
                ),
            }
        )

    specs.append(
        {
            "key": "quarter_roadmap",
            "title": "AI 会议助手下一季度产品路线图",
            "doc_type": "产品路线图",
            "timeline_keys": ["metrics", "release_readiness", "retrospective"],
            "knowledge_keys": ["metrics", "insight_release_readiness", "insight_retrospective"],
            "tags": ["路线图", "季度规划", "AI 会议助手"],
            "summary": "基于灰度指标与首周复盘形成的下一季度目标、里程碑和资源安排。",
            "sections": [
                ("季度目标", ["目标", "指标"]),
                ("重点方向", ["准确性", "模板", "集成"]),
                ("里程碑", ["月份", "交付物"]),
                ("风险与资源", ["风险", "协同"]),
            ],
            "style_phrases": ["目标可验收", "先讲优先级依据", "明确不做什么"],
            "prompt": "根据灰度结果生成下一季度路线图，包含目标、优先级、里程碑和风险。",
            "content": """# AI 会议助手下一季度产品路线图

## 季度目标

在保持会后 3 分钟可分享率不低于 90% 的前提下，进一步提升多人重叠发言场景的准确性，并完成模板个性化与首批协作平台集成。

## 三项重点

1. 准确性：多人重叠发言样本的行动项识别准确率达到 92%；
2. 个性化：上线团队模板与默认推荐，模板采用率达到 35%；
3. 集成：完成系统日历、飞书与钉钉的会议识别和纪要回写。

## 里程碑

- 第一个月：完成数据补齐、技术方案和交互评审；
- 第二个月：内部试用并关闭 P0 问题；
- 第三个月：分批灰度，依据指标决定是否扩量。

## 风险护栏

任何新增能力都不得牺牲本地优先默认值，也不得让核心链路的 P95 生成时长超过 180 秒。""",
        }
    )
    return specs


def document_specs() -> List[Dict[str, Any]]:
    base = [
        {
            "key": "product_design",
            "title": "AI 会议助手 2.0 产品设计方案",
            "doc_type": "产品设计方案",
            "timeline_keys": ["research", "scope", "technical", "prototype", "metrics"],
            "knowledge_keys": ["speed", "scope", "speaker", "interaction", "metrics"],
            "tags": ["AI 会议助手", "产品方案", "2.0", "评审版"],
            "summary": "从用户问题、版本目标到交互、技术边界和灰度指标的完整产品设计方案。",
            "sections": [
                ("背景与问题", ["用户痛点", "机会"]),
                ("产品目标", ["3 分钟", "可分享"]),
                ("核心方案", ["纪要", "行动项", "分享"]),
                ("技术与隐私边界", ["端侧", "置信度"]),
                ("灰度与验收", ["指标", "护栏"]),
            ],
            "style_phrases": ["结论先行", "以用户任务为主线", "所有指标可验证"],
            "prompt": "先讲清用户问题，再描述关键流程、版本边界、验收指标和风险护栏。",
            "content": """# AI 会议助手 2.0 产品设计方案

## 1. 背景与用户问题

高频会议用户真正缺少的不是更多编辑按钮，而是会议结束后快速得到一份可以直接分享、可以立即推动执行的纪要。现有链路通常需要二次整理责任人、截止时间和结论，导致会后 20—40 分钟仍停留在信息搬运。

## 2. 产品目标

围绕“会后 3 分钟可分享”建立单一主目标：30 分钟中文会议结束后，3 分钟内生成包含结论、行动项、风险与关键原话的首版纪要。

## 3. 核心体验

1. 会议中：稳定输出实时转写，弱网时优先保证文本连续；
2. 会议后：自动收敛为一句话结论、行动项和关键原话；
3. 分享前：允许快速修正责任人和截止日期；
4. 分享时：支持复制、导出或发送到协作群。

## 4. 版本范围

P0 包含实时转写、3 分钟纪要、行动项责任人识别和分享前快速编辑。自定义模板与多语言翻译进入 P1；情绪分析和自动建群暂缓。

## 5. 技术与隐私边界

默认使用端侧 VAD 与降噪，云端增强需用户明确开启。说话人识别置信度低于 0.72 时显示“待确认发言人”，不把不确定结果包装成确定事实。

## 6. 灰度与验收

首周向 200 名高频用户开放 10% 灰度。扩量门槛为：纪要生成成功率 ≥98%、3 分钟内可分享率 ≥90%、行动项确认率 ≥55%。错误责任人反馈率超过 3% 时暂停扩量。""",
        },
        {
            "key": "research_report",
            "title": "会议纪要产品调研与用户访谈汇总",
            "doc_type": "用户研究报告",
            "timeline_keys": ["research", "competitive"],
            "knowledge_keys": ["speed"],
            "tags": ["用户访谈", "竞品分析", "会议纪要"],
            "summary": "8 位用户访谈与 5 款竞品对比形成的机会洞察。",
            "sections": [
                ("研究设计", ["样本", "方法"]),
                ("关键发现", ["速度", "分享"]),
                ("竞品观察", ["差异化"]),
                ("产品启示", ["P0", "优先级"]),
            ],
            "style_phrases": ["证据与结论分开", "保留典型原话", "避免过度推断"],
            "prompt": "按研究问题、证据、洞察和产品启示组织内容，并保留用户原话。",
            "content": """# 会议纪要产品调研与用户访谈汇总

## 研究概况

本轮访谈覆盖 8 位每周参加 8 次以上会议的产品、研发和运营用户，同时对比 5 款会议产品的核心链路。

## 关键发现

- 6/8 的用户把生成速度列为首要因素；
- 用户对“责任人识别错误”的容忍度明显低于“摘要少一个细节”；
- 分享前快速修正比完整编辑器更高频；
- 原始转写的本地保存方式会直接影响用户是否愿意在敏感会议中使用。

> 典型原话：我最需要的不是更多编辑按钮，而是会一结束就有一份能直接发出去的纪要。

## 产品启示

首发版本应围绕“会后 3 分钟可分享”优化，不把功能数量作为完成度。行动项、隐私默认值和跨应用分享是最值得建立差异化的三个方向。""",
        },
        {
            "key": "launch_plan",
            "title": "AI 会议助手 2.0 灰度发布与指标方案",
            "doc_type": "发布方案",
            "timeline_keys": ["metrics"],
            "knowledge_keys": ["metrics"],
            "tags": ["灰度发布", "指标", "风险护栏"],
            "summary": "定义首周人群、扩量指标、暂停阈值和每日复盘机制。",
            "sections": [
                ("灰度范围", ["用户", "流量"]),
                ("成功指标", ["成功率", "可分享率"]),
                ("风险护栏", ["暂停扩量"]),
                ("观察节奏", ["日报", "复盘"]),
            ],
            "style_phrases": ["指标口径明确", "异常可回滚", "责任人到角色"],
            "prompt": "输出可执行的灰度范围、指标门槛、告警阈值、责任人与复盘节奏。",
            "content": """# AI 会议助手 2.0 灰度发布与指标方案

## 灰度范围

- 用户：200 名每周会议不少于 8 次的活跃用户；
- 流量：首日 10%，连续两天达到门槛后逐步扩至 30%；
- 场景：优先覆盖 15—60 分钟中文项目会议。

## 扩量门槛

| 指标 | 门槛 |
| --- | ---: |
| 纪要生成成功率 | ≥ 98% |
| 3 分钟内可分享率 | ≥ 90% |
| 行动项确认率 | ≥ 55% |

## 风险护栏

错误责任人反馈率超过 3% 立即暂停扩量；P95 纪要生成时长超过 180 秒并持续 30 分钟触发告警。每天 17:30 由产品、研发和客服共同复盘异常样本。""",
        },
        {
            "key": "weekly_template",
            "title": "产品团队周报：结论—进展—风险—下周计划",
            "doc_type": "周报模板",
            "timeline_keys": ["scope", "prototype", "metrics"],
            "knowledge_keys": ["scope", "interaction", "metrics"],
            "tags": ["周报", "产品团队", "模板"],
            "summary": "适合产品项目周报的结论先行模板，强调数据、决策与协同事项。",
            "sections": [
                ("本周结论", ["状态", "里程碑"]),
                ("关键进展", ["结果", "证据"]),
                ("用户反馈", ["洞察", "原话"]),
                ("风险与协同", ["风险", "责任人"]),
                ("下周计划", ["目标", "验收"]),
            ],
            "style_phrases": ["先给结论，再补证据", "用结果描述进展", "风险必须有动作"],
            "prompt": "生成面向项目组和管理者的产品周报，避免流水账，突出结果、指标、风险和下一步。",
            "content": """# 产品团队周报模板

## 本周结论

用 2—3 句话说明项目状态、最重要的完成项和是否存在阻断。

## 关键进展

| 事项 | 本周结果 | 证据/指标 | 状态 |
| --- | --- | --- | --- |
| 示例 | 描述已经发生的结果 | 数据或评审结论 | 已完成 |

## 用户反馈

只保留会影响产品判断的新证据，并注明样本范围。

## 风险与协同

每项风险写清影响、当前动作、需要谁在什么时间前提供什么支持。

## 下周计划

使用可验收的目标，不写“持续优化”“继续跟进”等无法判断完成度的表达。""",
        },
    ]
    return base + supplemental_document_specs()


def insert_documents(
    conn: sqlite3.Connection,
    timelines: Dict[str, Dict[str, Any]],
    knowledge: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    inserted: Dict[str, Dict[str, Any]] = {}
    now_ms = int(datetime.now().timestamp() * 1000)
    for index, spec in enumerate(document_specs()):
        source_timeline_ids = [str(timelines[key]["id"]) for key in spec["timeline_keys"]]
        source_capture_ids = [
            str(capture_id)
            for key in spec["timeline_keys"]
            for capture_id in timelines[key]["capture_ids"]
        ]
        linked_knowledge_ids = [str(knowledge[key]["id"]) for key in spec["knowledge_keys"]]
        sections = [
            {"title": title, "keywords": keywords, "notes": None}
            for title, keywords in spec["sections"]
        ]
        created_ms = now_ms - (index + 1) * 60 * 1000
        cursor = conn.execute(
            """
            INSERT INTO bake_documents (
                title, doc_type, status, tags, applicable_tasks,
                source_memory_ids, source_capture_ids, source_episode_ids,
                linked_knowledge_ids, sections_json, style_phrases,
                replacement_rules, summary, full_content, structured_content,
                prompt_hint, diagram_code, image_assets, source_app_name,
                source_win_title, source_url, content_hash, language, usage_count,
                match_score, match_level, creation_mode, review_status,
                evidence_summary, generation_version, deleted_at, created_at,
                updated_at, document_identity
            ) VALUES (?, ?, 'enabled', ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?, ?, ?, NULL, '[]', ?, ?, ?, ?, 'zh-CN', ?, ?, 'high', 'manual', 'confirmed', ?, ?, NULL, ?, ?, ?)
            """,
            (
                spec["title"],
                spec["doc_type"],
                json_text(spec["tags"]),
                json_text(["智能创作", "咨询检索", "官网演示"]),
                json_text(source_timeline_ids),
                json_text(source_capture_ids),
                json_text(linked_knowledge_ids),
                json_text(sections),
                json_text(spec["style_phrases"]),
                json_text([{"from": "持续优化", "to": "写明下一步动作与验收标准"}]),
                spec["summary"],
                spec["content"],
                json_text({"title": spec["title"], "sections": [title for title, _ in spec["sections"]]}),
                spec["prompt"],
                "MemoryBread",
                spec["title"],
                "https://demo.memorybread.local/documents/{}".format(spec["key"]),
                "{}:{}".format(DEMO_MARKER, spec["key"]),
                max(1, 12 - index % 10),
                round(0.97 - index * 0.02, 2),
                "由 {} 条时间线与 {} 条知识共同提炼。".format(
                    len(source_timeline_ids), len(linked_knowledge_ids)
                ),
                DEMO_MARKER,
                created_ms,
                now_ms - index * 1000,
                "demo:{}".format(spec["key"]),
            ),
        )
        document_id = int(cursor.lastrowid)
        inserted[spec["key"]] = {
            "id": document_id,
            "title": spec["title"],
            "doc_type": spec["doc_type"],
            "summary": spec["summary"],
            "content": spec["content"],
            "source_timeline_ids": source_timeline_ids,
            "source_capture_ids": source_capture_ids,
        }

    for document_key, knowledge_keys in {
        "product_design": ["scope", "speaker", "interaction"],
        "research_report": ["speed"],
        "launch_plan": ["metrics"],
        "weekly_template": [],
    }.items():
        for knowledge_key in knowledge_keys:
            conn.execute(
                "UPDATE bake_knowledge SET document_id = ? WHERE id = ?",
                (inserted[document_key]["id"], knowledge[knowledge_key]["id"]),
            )
    return inserted


def insert_sops(
    conn: sqlite3.Connection,
    timelines: Dict[str, Dict[str, Any]],
    knowledge: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    inserted: Dict[str, Dict[str, Any]] = {}
    knowledge_by_timeline = {
        int(item["timeline_id"]): item for item in knowledge.values()
    }
    now_ms = int(datetime.now().timestamp() * 1000)
    for index, spec in enumerate(timeline_specs()):
        source = timelines[spec["key"]]
        linked = knowledge_by_timeline.get(int(source["id"]))
        linked_ids = [str(linked["id"])] if linked else []
        steps = [
            "确认任务目标、适用范围和最终交付物",
            "回看来源记录，核对关键事实、指标与例外情况",
            "按既定模板完成处理，并记录重要取舍",
            "对照验收标准自检，补齐负责人和截止时间",
            "发布结果并安排下一次复盘，异常情况及时升级",
        ]
        problem = "如何稳定复现并推进：{}".format(spec["summary"])
        details = {
            "demo_marker": DEMO_MARKER,
            "source_timeline_id": source["id"],
            "source_memory_ids": [str(source["id"])],
            "source_capture_ids": [str(value) for value in source["capture_ids"]],
            "source_capture_id": str(source["capture_ids"][0]),
            "source_title": spec["summary"],
            "trigger_keywords": spec["entities"][:3],
            "confidence": "high" if spec["importance"] >= 9 else "medium",
            "extracted_problem": problem,
            "steps": steps,
            "linked_knowledge_ids": linked_ids,
            "status": "confirmed",
            "review_status": "confirmed",
            "creation_mode": "demo_seed",
            "generation_version": DEMO_MARKER,
        }
        detailed_content = """## 适用场景

当团队再次遇到“{}”相关任务时，可使用本操作手册快速复现已经验证过的方法。

## 操作步骤

{}

## 完成标准

- 结论有来源证据；
- 关键指标口径一致；
- 负责人、截止时间和复盘节点完整；
- 异常样本已记录且没有被平均值掩盖。""".format(
            spec["summary"],
            "\n".join("{}. {}".format(number, step) for number, step in enumerate(steps, 1)),
        )
        created_ms = source["end_ms"] + 18 * 60 * 1000
        cursor = conn.execute(
            """
            INSERT INTO bake_sops (
                timeline_id, title, summary, content, entities, importance,
                user_verified, user_edited, created_at, updated_at,
                created_at_ms, updated_at_ms, source_capture_ids, detailed_content
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                source["id"],
                "{}操作手册".format(spec["category"]),
                problem,
                json_text(details),
                json_text(spec["entities"]),
                spec["importance"],
                iso_local(created_ms),
                iso_local(now_ms),
                created_ms,
                now_ms - index * 1000,
                json_text(source["capture_ids"]),
                detailed_content,
            ),
        )
        sop_id = int(cursor.lastrowid)
        if table_exists(conn, "bake_artifact_source_links"):
            conn.execute(
                "INSERT OR IGNORE INTO bake_artifact_source_links "
                "(artifact_kind, artifact_id, source_timeline_id, created_at) "
                "VALUES ('sop', ?, ?, ?)",
                (sop_id, source["id"], created_ms),
            )
        inserted[spec["key"]] = {
            "id": sop_id,
            "title": "{}操作手册".format(spec["category"]),
            "summary": problem,
            "steps": steps,
            "timeline_id": source["id"],
            "capture_ids": source["capture_ids"],
            "created_ms": created_ms,
        }
    return inserted


def data_metric_specs() -> List[Dict[str, Any]]:
    raw = [
        ("meeting_quality", "纪要生成质量看板", "纪要成功率与生成时长均达到灰度门槛", "纪要生成成功率", "98.6%", "高于 98% 门槛", "P95 生成时长", "128 秒", "低于 180 秒护栏"),
        ("share_funnel", "会后分享转化漏斗", "大多数用户在三分钟内完成确认并分享", "3 分钟内可分享率", "91.8%", "环比提升 4.2 个百分点", "分享完成率", "84.2%", "主要流失发生在责任人确认"),
        ("action_quality", "行动项识别质量周报", "显式责任人的识别已稳定，隐含责任人仍需优化", "行动项识别准确率", "93.1%", "120 条标注样本", "责任人确认率", "62.4%", "超过 55% 灰度门槛"),
        ("speaker_quality", "说话人识别评测", "低置信度待确认策略显著降低错误归属", "说话人识别准确率", "94.1%", "多人会议样本", "错误归属率", "2.3%", "低于 3% 风险阈值"),
        ("beta_activation", "灰度用户激活看板", "新手引导收敛后首日激活明显改善", "首日激活率", "74.0%", "优化前为 62.0%", "引导完成时长", "2 分 18 秒", "较优化前缩短 41 秒"),
        ("launch_guardrail", "灰度扩量风险护栏", "三项核心指标达标，错误责任人反馈保持可控", "错误责任人反馈率", "2.1%", "暂停阈值为 3%", "严重故障数", "0", "本周未触发回滚"),
        ("feedback_mix", "灰度反馈分布", "准确性和分享体验占全部反馈的六成以上", "有效反馈", "37 条", "已全部完成分级", "P0 待修复", "5 项", "上线前必须关闭"),
        ("weak_network", "弱网恢复专项数据", "短时断网后文本连续性达到预期", "恢复成功率", "99.2%", "覆盖 120 次断网注入", "平均补齐时长", "4.6 秒", "不覆盖用户已编辑内容"),
        ("template_usage", "纪要模板使用分布", "项目例会模板使用最高，客户访谈模板增长最快", "模板采用率", "38.0%", "灰度用户口径", "推荐命中率", "82.5%", "按会议上下文推荐"),
        ("multilingual_quality", "中英混合会议质量", "专有名词保留策略提升了摘要可用性", "摘要可用率", "92.4%", "40 场中英混合会议", "专有名词保留率", "96.8%", "人工抽检口径"),
        ("privacy_choice", "隐私模式选择分布", "大多数用户保持本地优先默认值", "本地优先使用率", "87.0%", "主动开启云端增强为 13%", "隐私提示理解率", "91.0%", "完成设置后抽样回访"),
        ("performance", "长会议性能基线", "60 分钟会议的延迟和内存均完成优化", "P95 生成时长", "128 秒", "优化前 176 秒", "峰值内存", "620 MB", "较优化前下降 18%"),
        ("accessibility", "可访问性修复进度", "主流程已经支持完整键盘操作", "已修复问题", "14 项", "焦点、对比度与读屏标签", "键盘可达率", "100%", "覆盖核心流程"),
        ("event_dictionary", "产品埋点字典覆盖", "核心漏斗事件已完成统一命名与去重", "核心事件", "28 个", "产品与数据口径一致", "校验通过率", "100%", "自动化检查"),
        ("customer_activation", "企业客户首周启用", "陪跑流程显著改善团队激活", "首周激活率", "81.3%", "目标为 80%", "样板会议完成率", "88.0%", "覆盖 25 个试用团队"),
        ("support_sla", "灰度期客服响应", "P0 问题全部在目标时限内完成升级", "P0 升级达标率", "100%", "目标 15 分钟内", "首次响应时长", "6.4 分钟", "中位数"),
        ("sales_demo", "企业版演示转化", "标准演示脚本提升了客户对核心价值的理解", "演示完成率", "96.0%", "15 分钟标准路径", "下一步意向率", "68.0%", "演示后预约技术交流"),
        ("pricing_intent", "团队版定价意向", "按席位与会议时长组合的表达最易理解", "团队版意向转化率", "12.8%", "定价页访谈样本", "价格理解率", "84.0%", "无需销售额外解释"),
        ("mobile_notice", "移动端会后提醒", "纪要完成提醒带来了稳定的快速确认", "通知打开率", "54.7%", "接近 55% 目标", "行动项确认率", "61.2%", "移动端入口"),
        ("integration_health", "协作平台集成健康度", "首批三个平台的纪要回写总体稳定", "回写成功率", "99.6%", "系统日历、飞书、钉钉", "平均重试次数", "0.08 次", "失败自动重试"),
        ("release_readiness", "正式发布就绪度", "绝大多数跨团队检查项已经关闭", "检查完成度", "92.0%", "42 项检查点", "未关闭 P0", "0 项", "剩余均为 P1"),
        ("weekly_delivery", "本周交付完成情况", "核心交付按计划推进，没有阻断性风险", "计划交付", "20 项", "本周承诺口径", "按期完成", "18 项", "其余 2 项已重新排期"),
        ("retention", "灰度用户次周留存", "高频会议用户保持较高留存", "次周留存率", "68.9%", "高频用户为 76.4%", "周均处理会议", "9.6 场", "活跃用户口径"),
        ("quarter_progress", "季度路线图进度", "准确性、模板和集成三条主线均按计划推进", "里程碑完成率", "75.0%", "已完成 6/8 个里程碑", "高风险事项", "1 项", "多人重叠发言样本不足"),
    ]
    result: List[Dict[str, Any]] = []
    for row in raw:
        result.append(
            {
                "key": row[0],
                "title": row[1],
                "summary": row[2],
                "metric_rows": [
                    {"dimension": "当前", "metric": row[3], "value": row[4], "note": row[5]},
                    {"dimension": "当前", "metric": row[6], "value": row[7], "note": row[8]},
                ],
            }
        )
    return result


def insert_data_sources(
    conn: sqlite3.Connection,
    timelines: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    inserted: Dict[str, Dict[str, Any]] = {}
    timeline_values = list(timelines.values())
    now_ms = int(datetime.now().timestamp() * 1000)
    for index, spec in enumerate(data_metric_specs()):
        source_timeline = timeline_values[index % len(timeline_values)]
        collected_at = now_ms - index * 45 * 60 * 1000
        # 官网演示指标来自本地工作记录，不伪装成可实时刷新的网页报表。
        # demo.memorybread.local 是叙事占位地址，若标成 report_url，创作 Harness
        # 会正确尝试实时采集，但该虚拟页面永远无法通过 AX/DOM 证据校验。
        source_kind = "work_memory"
        source_url = None
        canonical_key = DEMO_DATA_KEY_PREFIX + spec["key"]
        cursor = conn.execute(
            """
            INSERT INTO data_sources (
                canonical_key, title, source_kind, source_url, access_mode,
                refresh_policy, realtime_level, source_app_name,
                source_window_title, tags, first_seen_at, last_seen_at,
                last_collected_at, last_success_at, last_error_code, status,
                created_at, updated_at, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, ?, NULL)
            """,
            (
                canonical_key,
                spec["title"],
                source_kind,
                source_url,
                "memory_only",
                "never",
                "observed",
                "MemoryBread",
                spec["title"],
                json_text(["官网演示", "AI 会议助手", spec["metric_rows"][0]["metric"]]),
                collected_at - 2 * 60 * 60 * 1000,
                collected_at,
                collected_at,
                collected_at,
                collected_at - 2 * 60 * 60 * 1000,
                collected_at,
            ),
        )
        source_id = int(cursor.lastrowid)
        semantic_summary = "{}：{}；{}。{}。".format(
            spec["title"],
            "{} {}".format(
                spec["metric_rows"][0]["metric"],
                spec["metric_rows"][0]["value"],
            ),
            "{} {}".format(
                spec["metric_rows"][1]["metric"],
                spec["metric_rows"][1]["value"],
            ),
            spec["summary"],
        )
        semantic_rows = []
        for row in spec["metric_rows"]:
            semantic_rows.append(
                {
                    "dimension": row["dimension"],
                    "metric": row["metric"],
                    "value": row["value"],
                    "note": row["note"],
                    "statement": "{} {}（{}）".format(
                        row["metric"], row["value"], row["note"]
                    ),
                    "observed_at": collected_at,
                }
            )
        structured = {
            "extraction_version": "data-memory.v15",
            "semantic_origin": "model_structured_fact",
            "title": spec["title"],
            "summary": semantic_summary,
            "semantic_subject": spec["title"],
            "semantic_identity": canonical_key,
            "metric_rows": semantic_rows,
            "metric_statements": [
                {
                    "statement": "{} {}（{}）".format(
                        item["metric"], item["value"], item["note"]
                    ),
                    "observed_at": collected_at,
                }
                for item in semantic_rows
            ],
        }
        content_text = "{}。{}。{}".format(
            spec["title"],
            spec["summary"],
            "；".join(
                "{} {}（{}）".format(row["metric"], row["value"], row["note"])
                for row in spec["metric_rows"]
            ),
        )
        cursor = conn.execute(
            """
            INSERT INTO data_snapshots (
                source_id, collected_at, observed_at, collector, content_text,
                structured_data, content_hash, freshness_ttl_seconds, provenance,
                source_capture_ids, source_timeline_ids, status, created_at,
                period_granularity, period_key, period_start_at, period_end_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'success', ?, 'week', ?, ?, ?)
            """,
            (
                source_id,
                collected_at,
                collected_at,
                "memory_extract",
                content_text,
                json_text(structured),
                "{}:data:{}".format(DEMO_MARKER, spec["key"]),
                0,
                json_text({"demo_marker": DEMO_MARKER, "source": "website_demo"}),
                json_text(source_timeline["capture_ids"]),
                json_text([source_timeline["id"]]),
                collected_at,
                "demo-{}".format(spec["key"]),
                collected_at - 7 * 24 * 60 * 60 * 1000,
                collected_at,
            ),
        )
        snapshot_id = int(cursor.lastrowid)
        if table_exists(conn, "data_source_links"):
            conn.execute(
                """
                INSERT INTO data_source_links (
                    source_id, source_ref_key, capture_id, timeline_id,
                    link_kind, observed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    "{}:{}".format(canonical_key, source_timeline["capture_ids"][0]),
                    source_timeline["capture_ids"][0],
                    source_timeline["id"],
                    "work_memory",
                    collected_at,
                    collected_at,
                ),
            )
        inserted[spec["key"]] = {
            "id": source_id,
            "snapshot_id": snapshot_id,
            "title": spec["title"],
            "source_kind": source_kind,
            "summary": spec["summary"],
            "metric_rows": spec["metric_rows"],
            "timeline_id": source_timeline["id"],
            "capture_id": source_timeline["capture_ids"][0],
            "collected_at": collected_at,
        }
    return inserted


def rag_context_from_document(
    document: Dict[str, Any], capture_id: int, score: float, timestamp_ms: int
) -> Dict[str, Any]:
    return {
        "capture_id": capture_id,
        "doc_key": "document:{}".format(document["id"]),
        "text": document["summary"],
        "score": score,
        "source": "merged",
        "source_type": "document",
        "artifact_id": document["id"],
        "document_id": document["id"],
        "app_name": "MemoryBread",
        "win_title": document["title"],
        "source_url": "https://demo.memorybread.local/documents/{}".format(document["id"]),
        "title": document["title"],
        "doc_type": document["doc_type"],
        "time": timestamp_ms,
        "summary": document["summary"],
        "evidence_strength": "高可信",
        "source_timeline_ids": document["source_timeline_ids"],
    }


def rag_context_from_knowledge(
    item: Dict[str, Any], score: float, timestamp_ms: int
) -> Dict[str, Any]:
    return {
        "capture_id": item["capture_ids"][0],
        "doc_key": "bake_knowledge:{}".format(item["id"]),
        "text": item["summary"],
        "score": score,
        "source": "merged",
        "source_type": "bake_knowledge",
        "artifact_id": item["id"],
        "app_name": "MemoryBread",
        "win_title": item["title"],
        "title": item["title"],
        "doc_type": "知识",
        "time": timestamp_ms,
        "summary": item["summary"],
        "evidence_strength": "高可信",
        "source_timeline_ids": [str(item["timeline_id"])],
    }


def rag_context_from_timeline(
    item: Dict[str, Any], text: str, score: float
) -> Dict[str, Any]:
    return {
        "capture_id": item["capture_ids"][0],
        "doc_key": "timeline:{}".format(item["id"]),
        "text": text,
        "score": score,
        "source": "merged",
        "source_type": "knowledge",
        "knowledge_id": item["id"],
        "app_name": "MemoryBread",
        "win_title": item["summary"],
        "title": item["summary"],
        "time": item["end_ms"],
        "summary": item["summary"],
        "activity_type": "工作时间线",
        "content_origin": "本地工作记录",
        "evidence_strength": "高可信",
        "importance": 10,
    }


def rag_context_from_sop(item: Dict[str, Any], score: float) -> Dict[str, Any]:
    return {
        "capture_id": item["capture_ids"][0],
        "doc_key": "operation:{}".format(item["id"]),
        "text": item["summary"],
        "score": score,
        "source": "merged",
        "source_type": "operation",
        "artifact_id": item["id"],
        "app_name": "MemoryBread",
        "win_title": item["title"],
        "title": item["title"],
        "doc_type": "操作",
        "time": item["created_ms"],
        "summary": item["summary"],
        "source_timeline_ids": [str(item["timeline_id"])],
    }


def rag_context_from_data(item: Dict[str, Any], score: float) -> Dict[str, Any]:
    return {
        "capture_id": item["capture_id"],
        "doc_key": "data:{}".format(item["id"]),
        "text": item["summary"],
        "score": score,
        "source": "merged",
        "source_type": "data",
        "artifact_id": item["id"],
        "app_name": "MemoryBread",
        "win_title": item["title"],
        "title": item["title"],
        "doc_type": "数据",
        "time": item["collected_at"],
        "summary": item["summary"],
        "source_timeline_ids": [str(item["timeline_id"])],
    }


def insert_rag_sessions(
    conn: sqlite3.Connection,
    timelines: Dict[str, Dict[str, Any]],
    knowledge: Dict[str, Dict[str, Any]],
    documents: Dict[str, Dict[str, Any]],
    sops: Dict[str, Dict[str, Any]],
    data_sources: Dict[str, Dict[str, Any]],
) -> None:
    sessions: List[Tuple[str, str, Sequence[Dict[str, Any]], int]] = [
        (
            "我记得用户访谈里提到“会后很快拿到能直接发出去的纪要”，具体在哪份资料？当时定的验收标准是什么？",
            """找到了。最直接的出处是《会议纪要产品调研与用户访谈汇总》，对应的原始工作记录是“回看用户访谈，确认会后纪要速度是首要诉求”。

## 当时看到的原话

> 我最需要的不是更多编辑按钮，而是会一结束就有一份能直接发出去的纪要。

这轮访谈覆盖 8 位高频会议用户，其中 6 位把生成速度列为首要因素。

## 最终沉淀的验收标准

1. 30 分钟中文会议结束后，**3 分钟内**出现首版纪要；
2. 默认包含一句话结论、行动项、风险和关键原话；
3. 行动项尽可能识别责任人与截止时间；
4. 分享前可以快速修正，不必进入完整编辑器。

因此，2.0 的 P0 不是“提供更多编辑能力”，而是“会后 3 分钟得到一份可直接分享的结构化纪要”。""",
            [
                rag_context_from_document(
                    documents["research_report"],
                    timelines["research"]["capture_ids"][0],
                    0.96,
                    timelines["research"]["end_ms"],
                ),
                rag_context_from_knowledge(
                    knowledge["speed"], 0.94, timelines["research"]["end_ms"]
                ),
                rag_context_from_timeline(
                    timelines["research"],
                    "8 位受访者中有 6 位把生成速度列为首要因素，并形成会后 3 分钟可分享的验收口径。",
                    0.91,
                ),
            ],
            business_time(0, 16, 10),
        ),
        (
            "说话人识别最后采用了什么策略？为什么没有低置信度也自动归属？",
            """最终采用“**低置信度待确认**”策略：说话人识别置信度低于 **0.72** 时，显示“待确认发言人”，不强行分配姓名。

原因有两点：

- 错误的责任人会沿着行动项和分享链路扩散，业务风险高于少一个说话人标签；
- 弱网时系统优先保证文本连续，其次是时间戳，最后才是说话人标签。

同时，默认使用端侧 VAD 与降噪；云端增强只有在用户明确开启后才使用。""",
            [
                rag_context_from_knowledge(
                    knowledge["speaker"], 0.97, timelines["technical"]["end_ms"]
                ),
                rag_context_from_document(
                    documents["product_design"],
                    timelines["technical"]["capture_ids"][0],
                    0.88,
                    timelines["technical"]["end_ms"],
                ),
            ],
            business_time(0, 14, 40),
        ),
        (
            "灰度发布要看哪三个核心指标？什么情况下暂停扩量？",
            """灰度首周看三项核心指标：

| 指标 | 扩量门槛 |
| --- | ---: |
| 纪要生成成功率 | ≥ 98% |
| 3 分钟内可分享率 | ≥ 90% |
| 行动项确认率 | ≥ 55% |

风险护栏是：**错误责任人反馈率超过 3% 时自动暂停扩量**。此外，P95 纪要生成时长超过 180 秒并持续 30 分钟会触发告警。""",
            [
                rag_context_from_document(
                    documents["launch_plan"],
                    timelines["metrics"]["capture_ids"][0],
                    0.98,
                    timelines["metrics"]["end_ms"],
                ),
                rag_context_from_knowledge(
                    knowledge["metrics"], 0.95, timelines["metrics"]["end_ms"]
                ),
            ],
            business_time(0, 11, 35),
        ),
    ]

    document_values = list(documents.values())
    sop_values = list(sops.values())
    data_values = list(data_sources.values())
    for index in range(DEMO_TARGET_COUNT - len(sessions)):
        document = document_values[index % len(document_values)]
        sop = sop_values[index % len(sop_values)]
        data_item = data_values[index % len(data_values)]
        primary_metric = data_item["metric_rows"][0]
        query_templates = [
            "我之前看过关于“{}”的资料，帮我找回当时的核心结论和依据。",
            "哪份文档提到“{}”？请把关键数据和执行建议一起找出来。",
            "回忆一下我们对“{}”最后是怎么决定的，有没有可直接复用的操作步骤？",
        ]
        query = query_templates[index % len(query_templates)].format(document["title"])
        answer = """找到了，与这个问题最相关的是《{}》。

## 核心结论

{}

## 可核对的数据

- {}：{}；
- 数据来源：《{}》；
- 当前记录带有原始时间线与采集证据，可以继续回看上下文。

## 可直接复用的操作

《{}》已经把处理过程整理为标准步骤。建议先确认适用范围，再核对数据时效，最后按验收口径完成复盘。""".format(
            document["title"],
            document["summary"],
            primary_metric["metric"],
            primary_metric["value"],
            data_item["title"],
            sop["title"],
        )
        sessions.append(
            (
                query,
                answer,
                [
                    rag_context_from_document(
                        document,
                        int(document["source_capture_ids"][0]),
                        0.95,
                        data_item["collected_at"],
                    ),
                    rag_context_from_data(data_item, 0.92),
                    rag_context_from_sop(sop, 0.88),
                ],
                business_time(index // 4, 9 + index % 7, (index * 7) % 60),
            )
        )

    for index, (query, answer, contexts, timestamp_ms) in enumerate(sessions):
        conn.execute(
            """
            INSERT INTO rag_sessions (
                ts, scene_type, user_query, retrieved_ids, prompt_used,
                llm_response, user_feedback, latency_ms, model
            ) VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?)
            """,
            (
                timestamp_ms - index * 1000,
                DEMO_MARKER,
                query,
                json_text(list(contexts)),
                answer,
                1860 + index * 240,
                "mbem-v1-local",
            ),
        )


def creation_references(
    document_keys: Sequence[str], documents: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    result = []
    for index, key in enumerate(document_keys):
        document = documents[key]
        result.append(
            {
                "id": document["id"],
                "title": document["title"],
                "doc_type": document["doc_type"],
                "final_weight": round(0.94 - index * 0.04, 2),
                "relevance_score": round(0.97 - index * 0.03, 2),
                "quality_score": 0.92,
                "completeness_score": round(0.95 - index * 0.02, 2),
                "usage_score": 0.86,
                "format_score": 0.91,
                "freshness_score": 0.98,
                "usage_count": 8 - index,
                "reason": "包含本次写作需要的{}，且与最新项目结论一致。".format(
                    "结构和事实" if index == 0 else "补充证据"
                ),
                "summary": document["summary"],
                "source_url": "https://demo.memorybread.local/documents/{}".format(
                    document["id"]
                ),
            }
        )
    return result


def creation_data_references(
    data_keys: Sequence[str], data_sources: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    result = []
    for key in data_keys:
        item = data_sources[key]
        result.append(
            {
                "source_id": item["id"],
                "title": item["title"],
                "source_kind": item["source_kind"],
                "freshness_class": "fresh",
                "refresh_required": False,
                "can_use": True,
                "evidence_status": "verified",
                "evidence_reason": "本周快照完整，指标口径与会议纪要一致。",
            }
        )
    return result


def build_weekly_report_content(
    title: str,
    status: str,
    meeting_keys: Sequence[str],
    data_keys: Sequence[str],
    completed_items: Sequence[str],
    analysis_items: Sequence[str],
    risk_items: Sequence[str],
    next_items: Sequence[str],
    documents: Dict[str, Dict[str, Any]],
    data_sources: Dict[str, Dict[str, Any]],
) -> str:
    metric_lines = []
    for data_key in data_keys:
        data_item = data_sources[data_key]
        for row in data_item["metric_rows"]:
            metric_lines.append(
                "| {} | {} | {} | 《{}》 |".format(
                    row["metric"], row["value"], row["note"], data_item["title"]
                )
            )
    meeting_lines = [
        "- **{}**：{}".format(documents[key]["title"], documents[key]["summary"])
        for key in meeting_keys
    ]
    completed_lines = [
        "{}. {}".format(index, item) for index, item in enumerate(completed_items, 1)
    ]
    analysis_lines = ["- {}".format(item) for item in analysis_items]
    risk_lines = ["- {}".format(item) for item in risk_items]
    next_lines = [
        "{}. {}".format(index, item) for index, item in enumerate(next_items, 1)
    ]
    return """# {}

> 周期：本周 · 项目状态：{}

## 一、本周结论

本周工作已从分散推进转入“数据验证—会议决策—执行闭环”的稳定节奏。结构化数据表明核心指标总体达到当前阶段门槛；会议纪要进一步明确了范围、技术取舍和后续负责人。以下结论均来自本周数据快照与已确认会议纪要，不扩大阶段性观察的适用范围。

## 二、本周核心数据

| 指标 | 本周结果 | 口径或判断 | 数据来源 |
| --- | ---: | --- | --- |
{}

## 三、数据分析

{}

## 四、会议决策回顾

{}

## 五、本周已完成事项

{}

## 六、风险与待协同事项

{}

## 七、下周计划

{}

## 八、口径说明

本周数据由数据检索 Tool 读取最新本地快照，会议结论由记忆搜索 Tool 召回已确认纪要。数据分析 Agent 完成趋势与门槛判断，章节扩写 Agent 补全上下文，文字润色 Agent 统一表达，内容质检 Agent 已检查事实依据、数字一致性、章节完整性和行动可执行性。""".format(
        title,
        status,
        "\n".join(metric_lines),
        "\n".join(analysis_lines),
        "\n".join(meeting_lines),
        "\n".join(completed_lines),
        "\n".join(risk_lines),
        "\n".join(next_lines),
    )


def weekly_creation_events(
    session_id: str,
    run_id: str,
    objective: str,
    base_ms: int,
    references: Sequence[Dict[str, Any]],
    data_references: Sequence[Dict[str, Any]],
    data_analysis: str,
    content: str,
) -> List[Dict[str, Any]]:
    main_actor = {"kind": "agent", "id": "creation_main_agent", "name": "创作 Agent"}
    events: List[Dict[str, Any]] = []

    def add_event(
        event_type: str,
        summary: str,
        actor: Dict[str, str],
        data: Optional[Dict[str, Any]] = None,
        environment_patch: Optional[Dict[str, Any]] = None,
    ) -> None:
        sequence = len(events) + 1
        event: Dict[str, Any] = {
            "schema_version": "creation.agent.v1",
            "event_id": "{}-event-{}".format(session_id, sequence),
            "session_id": session_id,
            "run_id": run_id,
            "sequence": sequence,
            "timestamp": base_ms + sequence * 720,
            "type": event_type,
            "status": "completed" if not event_type.endswith("started") else "running",
            "actor": actor,
            "summary": summary,
            "goal": {
                "objective": objective,
                "status": "completed" if event_type == "run.completed" else "running",
                "revision": 1,
                "remaining_steps": [],
                "outcome": "周报已生成并通过质检"
                if event_type == "run.completed"
                else None,
            },
            "data": data or {},
        }
        if environment_patch is not None:
            event["environment_patch"] = environment_patch
        events.append(event)

    memory_actor = {"kind": "tool", "id": "memory_search", "name": "记忆搜索 Tool"}
    data_actor = {"kind": "tool", "id": "data_search", "name": "数据检索 Tool"}
    analysis_actor = {
        "kind": "agent",
        "id": "data_analysis_agent",
        "name": "数据分析 Agent",
    }
    chapter_actor = {
        "kind": "agent",
        "id": "document_writer_agent",
        "name": "章节扩写 Agent",
    }
    polish_actor = {
        "kind": "agent",
        "id": "detail_polish_agent",
        "name": "文字润色 Agent",
    }
    quality_actor = {
        "kind": "agent",
        "id": "quality_review_agent",
        "name": "内容质检 Agent",
    }

    add_event("run.started", "创作 Agent 已接管本周周报目标", main_actor)
    add_event(
        "intent.interpreted",
        "已拆解为数据取数、纪要召回、分析、扩写、润色和质检六个阶段",
        main_actor,
        {
            "operation": "create_document",
            "root_request": objective,
            "current_instruction": objective,
            "reasoning_summary": "周报必须同时使用本周数据快照与已确认会议纪要，数字和结论需要交叉核对。",
        },
    )
    # 两个 Tool 先后启动、再先后完成，在执行轨迹中明确表达并行取数。
    add_event("tool.started", "数据检索与记忆搜索并行启动：正在读取本周指标", data_actor)
    add_event("tool.started", "数据检索与记忆搜索并行启动：正在召回会议纪要", memory_actor)
    add_event(
        "tool.completed",
        "数据检索完成，召回 {} 个来源".format(len(data_references)),
        data_actor,
        {"result_count": len(data_references), "refresh_required_count": 0},
        {"data_sources": list(data_references)},
    )
    add_event(
        "tool.completed",
        "记忆搜索完成，召回 {} 条本地资料，其中包含已确认会议纪要".format(
            len(references)
        ),
        memory_actor,
        {"result_count": len(references)},
        {"references": list(references)},
    )
    add_event(
        "harness.decision",
        "检索证据完整，开始数据分析与正文生成",
        main_actor,
        {
            "trigger": "data_search",
            "trigger_status": "completed",
            "reason_code": "matching_data_ready",
            "result_count": len(data_references),
            "refreshable_count": 0,
            "analyzable_count": len(data_references),
            "scheduled": [
                "data_analysis_agent",
                "document_writer_agent",
                "detail_polish_agent",
                "quality_review_agent",
            ],
        },
    )
    add_event("agent.started", "数据分析 Agent 开始核对指标口径与阶段门槛", analysis_actor)
    add_event(
        "agent.completed",
        "数据分析完成，已形成趋势、门槛与风险判断",
        analysis_actor,
        environment_patch={"data_analysis": data_analysis},
    )
    add_event("agent.started", "章节扩写 Agent 开始组织周报章节与证据", chapter_actor)
    add_event(
        "agent.completed",
        "章节扩写完成，已补齐数据分析、完成事项、风险和下周计划",
        chapter_actor,
        environment_patch={
            "plan": ["本周结论", "核心数据", "数据分析", "完成事项", "风险", "下周计划"]
        },
    )
    add_event("agent.started", "文字润色 Agent 开始统一措辞与管理层阅读节奏", polish_actor)
    add_event(
        "agent.completed",
        "文字润色完成，已减少重复表达并统一指标口径",
        polish_actor,
    )
    add_event("agent.started", "内容质检 Agent 开始检查事实、数字、结构和行动项", quality_actor)
    add_event(
        "agent.completed",
        "内容质检通过：数据有来源、会议结论一致、完成事项和后续动作完整",
        quality_actor,
        environment_patch={
            "quality_review": {
                "facts_grounded": True,
                "numbers_consistent": True,
                "sections_complete": True,
                "actions_executable": True,
            }
        },
    )
    add_event(
        "harness.decision",
        "质量要求已满足，结束本轮优化循环",
        main_actor,
        {
            "trigger": "quality_review_agent",
            "trigger_status": "completed",
            "reason_code": "quality_gate_passed",
            "quality_cycle": 1,
            "issue_count": 0,
            "issue_codes": [],
            "scheduled": [],
        },
    )
    add_event(
        "run.completed",
        "多 Agent 周报已生成并通过内容质检",
        main_actor,
        {"document": content},
    )
    return events


def creation_events(
    session_id: str,
    run_id: str,
    objective: str,
    base_ms: int,
    reference_count: int,
) -> List[Dict[str, Any]]:
    event_specs = [
        ("intent.interpreted", "agent", "creation_main_agent", "创作 Agent", "已理解文档目标与读者", "completed"),
        ("tool.completed", "tool", "memory_search", "记忆搜索", "已召回 {} 份高相关资料".format(reference_count), "completed"),
        ("agent.completed", "agent", "outline_designer", "章节设计 Agent", "已完成章节结构与证据映射", "completed"),
        ("agent.completed", "agent", "document_writer", "文档撰写 Agent", "已生成完整文档并完成一致性检查", "completed"),
    ]
    events = []
    for sequence, (event_type, actor_kind, actor_id, actor_name, summary, status) in enumerate(
        event_specs, start=1
    ):
        data: Dict[str, Any] = {}
        if actor_id == "memory_search":
            data = {"result_count": reference_count}
        if event_type == "intent.interpreted":
            data = {"operation": "create_document"}
        events.append(
            {
                "schema_version": "creation.agent.v1",
                "event_id": "{}-event-{}".format(session_id, sequence),
                "session_id": session_id,
                "run_id": run_id,
                "sequence": sequence,
                "timestamp": base_ms + sequence * 900,
                "type": event_type,
                "status": status,
                "actor": {"kind": actor_kind, "id": actor_id, "name": actor_name},
                "summary": summary,
                "goal": {
                    "objective": objective,
                    "status": "completed" if sequence == len(event_specs) else "running",
                    "revision": 1,
                    "remaining_steps": [],
                    "outcome": "文档已生成" if sequence == len(event_specs) else None,
                },
                "data": data,
            }
        )
    return events


def insert_creation_history(
    conn: sqlite3.Connection,
    documents: Dict[str, Dict[str, Any]],
    data_sources: Dict[str, Dict[str, Any]],
) -> None:
    now_ms = int(datetime.now().timestamp() * 1000)
    design_content = documents["product_design"]["content"] + """

## 7. 关键流程验收用例

| 场景 | 前置条件 | 期望结果 |
| --- | --- | --- |
| 正常中文会议 | 30 分钟、3—6 人 | 3 分钟内生成结构化纪要 |
| 说话人低置信度 | 置信度 < 0.72 | 标记待确认，不错误归属 |
| 弱网恢复 | 断网不超过 30 秒 | 文本连续，不覆盖用户编辑 |
| 分享前修正 | 行动项责任人有误 | 两步内完成修正并分享 |

## 8. 后续迭代

灰度通过后再验证自定义纪要模板和多语言翻译。所有新增能力继续以“是否缩短从会议结束到推动执行的时间”为优先级判断依据。"""

    weekly_specs = [
        {
            "suffix": "weekly-product-rd",
            "title": "AI 会议助手 2.0｜产品研发周报",
            "prompt": "同时检索本周产品数据和范围、技术、体验评审会议纪要，调用数据分析、章节扩写、文字润色和内容质检 Agent，生成产品研发周报。",
            "audience": "项目组与管理层",
            "status": "按计划推进，可进入下一阶段灰度",
            "meeting_keys": [
                "minutes_scope_review",
                "minutes_technical_review",
                "minutes_experience_review",
                "minutes_gray_review",
            ],
            "data_keys": [
                "meeting_quality",
                "share_funnel",
                "action_quality",
                "speaker_quality",
            ],
            "completed_items": [
                "完成 2.0 版本范围评审，确认实时转写、3 分钟纪要、行动项责任人识别和分享前快速编辑 4 个 P0。",
                "完成会后主路径高保真评审，打通生成纪要、确认待办和分享流程。",
                "完成说话人低置信度待确认方案，阈值确定为 0.72。",
                "完成 200 名用户、10% 流量的灰度指标与暂停扩量规则。",
            ],
            "analysis_items": [
                "纪要生成成功率 98.6%，高于 98% 门槛，主链路稳定性达到扩量前提。",
                "3 分钟内可分享率 91.8%，高于 90% 门槛，但责任人确认仍是主要流失点。",
                "行动项识别准确率达到 93.1%，错误主要集中在隐含责任人和多人重叠发言。",
                "说话人错误归属率为 2.3%，低于 3% 风险阈值，低置信度待确认策略有效。",
            ],
            "risk_items": [
                "多人重叠发言样本仍不足，现有准确率不能直接外推到所有会议类型。",
                "分享完成率仍受责任人确认步骤影响，需要继续缩短修正路径。",
            ],
            "next_items": [
                "完成 20 场内部回归并重点补充多人重叠发言样本。",
                "继续优化责任人确认与分享前快速修正流程。",
                "每日复盘生成成功率、可分享率和错误责任人反馈率。",
            ],
        },
        {
            "suffix": "weekly-beta-growth",
            "title": "AI 会议助手 2.0｜灰度增长与用户反馈周报",
            "prompt": "检索灰度激活、反馈、企业启用和留存数据，同时召回灰度复盘会议纪要，完成数据分析后生成增长与用户反馈周报。",
            "audience": "产品、增长、运营与客户成功团队",
            "status": "核心增长指标向好，反馈进入闭环阶段",
            "meeting_keys": [
                "minutes_weekly_review",
                "minutes_gray_review",
                "minutes_experience_review",
            ],
            "data_keys": [
                "beta_activation",
                "feedback_mix",
                "customer_activation",
                "retention",
            ],
            "completed_items": [
                "将首次使用流程从五步收敛为三步，并完成新手引导灰度验证。",
                "完成 37 条灰度反馈分级，5 项上线前问题均已明确负责人。",
                "完成企业客户五日陪跑流程和样板会议模板。",
                "建立客服、产品和研发每日联合复盘机制。",
            ],
            "analysis_items": [
                "首日激活率达到 74.0%，较优化前提升 12 个百分点，新手引导收敛方向有效。",
                "企业客户首周激活率为 81.3%，超过 80% 目标，样板会议是关键促进因素。",
                "次周留存率为 68.9%，高频会议用户达到 76.4%，使用频次与留存呈正相关。",
                "当前反馈重点已经从能否生成转向责任人准确性和分享效率，产品进入体验优化阶段。",
            ],
            "risk_items": [
                "5 项 P0 反馈需要在下一次扩量前全部关闭。",
                "低频会议用户的次周留存仍需单独观察，不能用高频用户均值替代。",
            ],
            "next_items": [
                "完成 5 项 P0 反馈回归并同步用户验证结果。",
                "对低频会议用户补充流失访谈和触达实验。",
                "跟踪首批企业客户样板会议后的团队扩散情况。",
            ],
        },
        {
            "suffix": "weekly-quality-performance",
            "title": "AI 会议助手 2.0｜质量与性能专项周报",
            "prompt": "使用数据检索获取说话人、弱网、长会议和平台集成指标，使用记忆搜索召回技术评审纪要，经分析、扩写、润色和质检后生成专项周报。",
            "audience": "研发、测试、产品与技术负责人",
            "status": "性能与稳定性达标，高风险样本继续补齐",
            "meeting_keys": [
                "minutes_technical_review",
                "minutes_gray_review",
                "minutes_weekly_review",
            ],
            "data_keys": [
                "speaker_quality",
                "weak_network",
                "performance",
                "integration_health",
            ],
            "completed_items": [
                "完成说话人识别多人会议专项评测和错误归属复盘。",
                "完成 120 次断网注入，验证恢复过程中不覆盖用户编辑。",
                "完成 60 分钟会议性能压测与内存优化。",
                "完成系统日历、飞书和钉钉的首批回写健康检查。",
            ],
            "analysis_items": [
                "说话人识别准确率为 94.1%，错误归属率 2.3%，当前风险可控。",
                "弱网恢复成功率达到 99.2%，平均补齐时长 4.6 秒，文本连续性策略稳定。",
                "P95 生成时长由 176 秒降至 128 秒，距离 180 秒护栏保留 52 秒余量。",
                "协作平台回写成功率为 99.6%，平均重试 0.08 次，集成链路总体健康。",
            ],
            "risk_items": [
                "多人同时发言、专有名词密集和长时间弱网组合场景覆盖不足。",
                "平台接口限流与用户授权过期仍可能造成短时回写失败。",
            ],
            "next_items": [
                "补充 5 场多人重叠发言与弱网组合回归。",
                "接入 P95 时长和回写失败率自动告警。",
                "验证授权过期后的恢复提示和重试边界。",
            ],
        },
        {
            "suffix": "weekly-commercial-success",
            "title": "AI 会议助手 2.0｜商业化与客户成功周报",
            "prompt": "同时检索企业演示、定价意向、客户激活和客服响应数据，并召回项目复盘及体验会议纪要，生成商业化与客户成功周报。",
            "audience": "管理层、销售、客户成功与产品团队",
            "status": "标准演示路径稳定，客户启用达到阶段目标",
            "meeting_keys": [
                "minutes_weekly_review",
                "minutes_experience_review",
                "minutes_gray_review",
            ],
            "data_keys": [
                "sales_demo",
                "pricing_intent",
                "customer_activation",
                "support_sla",
            ],
            "completed_items": [
                "完成围绕节省整理时间、减少责任遗漏和本地隐私的 15 分钟标准演示脚本。",
                "完成团队版按席位与会议时长组合的套餐表达验证。",
                "完成企业客户首周启用、样板会议和团队复盘流程。",
                "完成灰度期客服五类问题分流和 P0 升级机制。",
            ],
            "analysis_items": [
                "标准演示完成率达到 96.0%，下一步意向率 68.0%，价值表达能够支撑后续技术交流。",
                "团队版意向转化率为 12.8%，价格理解率 84.0%，套餐表达仍有简化空间。",
                "企业客户首周激活率达到 81.3%，样板会议完成率 88.0%，陪跑流程有效。",
                "P0 升级达标率为 100%，首次响应中位数 6.4 分钟，当前服务能力可覆盖小规模扩量。",
            ],
            "risk_items": [
                "意向率尚未等同于付费转化，需要跟踪技术交流后的真实推进结果。",
                "客户规模扩大后，当前人工陪跑和客服响应能力可能成为瓶颈。",
            ],
            "next_items": [
                "跟进完成演示客户的技术交流和试用转化。",
                "继续验证套餐表达并记录价格异议类型。",
                "沉淀企业启用自助材料，降低人工陪跑成本。",
            ],
        },
        {
            "suffix": "weekly-management-summary",
            "title": "AI 会议助手 2.0｜管理层项目周报",
            "prompt": "基于本周核心经营数据和四份关键会议纪要，调用数据分析、章节扩写、文字润色和内容质检 Agent，输出管理层项目周报。",
            "audience": "管理层与跨团队负责人",
            "status": "整体健康，达到阶段门槛但仍有一项高风险事项",
            "meeting_keys": [
                "minutes_scope_review",
                "minutes_technical_review",
                "minutes_gray_review",
                "minutes_weekly_review",
            ],
            "data_keys": [
                "meeting_quality",
                "release_readiness",
                "weekly_delivery",
                "quarter_progress",
            ],
            "completed_items": [
                "完成 4 个 P0 范围锁定及跨职能资源确认。",
                "完成核心链路、技术降级和风险护栏评审。",
                "本周 20 项计划交付中按期完成 18 项，其余 2 项已重新排期。",
                "完成 42 项发布检查中的绝大多数事项，未关闭 P0 为 0。",
            ],
            "analysis_items": [
                "纪要生成成功率为 98.6%，核心产品质量达到当前扩量门槛。",
                "发布检查完成度为 92.0%，未关闭 P0 为 0，发布准备总体可控。",
                "本周按期完成 18/20 项，交付兑现率为 90%，延期项已有明确排期。",
                "季度里程碑完成率为 75.0%，当前唯一高风险事项是多人重叠发言样本不足。",
            ],
            "risk_items": [
                "多人重叠发言样本不足可能影响扩大用户范围后的责任人准确性。",
                "两项延期工作需要避免与下一轮灰度准备形成资源冲突。",
            ],
            "next_items": [
                "关闭剩余发布检查项并完成最终回归。",
                "专项补齐多人重叠发言样本和质量报告。",
                "复盘两项延期原因并调整下一周资源安排。",
            ],
        },
    ]

    records: List[Dict[str, Any]] = []
    for index, spec in enumerate(weekly_specs):
        content = build_weekly_report_content(
            spec["title"],
            spec["status"],
            spec["meeting_keys"],
            spec["data_keys"],
            spec["completed_items"],
            spec["analysis_items"],
            spec["risk_items"],
            spec["next_items"],
            documents,
            data_sources,
        )
        records.append(
            {
                "suffix": spec["suffix"],
                "prompt": spec["prompt"],
                "content": content,
                "doc_type": "工作周报",
                "audience": spec["audience"],
                "document_keys": spec["meeting_keys"],
                "data_keys": spec["data_keys"],
                "data_analysis": "；".join(spec["analysis_items"]),
                "timestamp": now_ms - (index + 1) * 60 * 1000,
                "multi_agent_weekly": True,
            }
        )

    records.append(
        {
            "suffix": "product-design",
            "prompt": "基于访谈、范围评审、技术方案和交互原型，撰写《AI 会议助手 2.0 产品设计方案》，需要包含产品目标、核心流程、版本范围、验收指标和风险护栏。",
            "content": design_content,
            "doc_type": "产品设计方案",
            "audience": "产品、设计、研发与测试",
            "document_keys": ["product_design", "research_report", "launch_plan"],
            "timestamp": now_ms - 6 * 60 * 1000,
            "multi_agent_weekly": False,
        }
    )

    generated_document_keys = [
        key for key in documents if key not in {"weekly_template", "product_design"}
    ]
    remaining_count = DEMO_TARGET_COUNT - len(records)
    for index, document_key in enumerate(generated_document_keys[:remaining_count]):
        document = documents[document_key]
        records.append(
            {
                "suffix": "document-{}".format(document_key),
                "prompt": "请结合本地工作记录，把《{}》整理成一份可直接评审的{}，保留关键数据、决策依据和验收标准。".format(
                    document["title"], document["doc_type"]
                ),
                "content": """# {}｜创作稿

> 已基于本地时间线、知识、文档和数据记录生成。

{}

## 创作说明

- 关键结论均能回溯到本地来源；
- 指标保留原始口径与适用范围；
- 执行动作包含负责人、交付物和验收标准；
- 发布前仍需由项目负责人确认最新状态。""".format(
                    document["title"], document["content"]
                ),
                "doc_type": document["doc_type"],
                "audience": "项目负责人、协作团队与评审人",
                "document_keys": [document_key, "product_design"],
                "timestamp": now_ms - (index + 7) * 2 * 60 * 1000,
                "multi_agent_weekly": False,
            }
        )

    if len(records) != DEMO_TARGET_COUNT:
        raise RuntimeError(
            "创作演示记录应为 {} 条，实际为 {} 条".format(
                DEMO_TARGET_COUNT, len(records)
            )
        )

    for index, record in enumerate(records):
        session_id = DEMO_CREATION_SESSION_PREFIX + record["suffix"]
        run_id = session_id + "-run-1"
        references = creation_references(record["document_keys"], documents)
        data_references = creation_data_references(
            record.get("data_keys", []), data_sources
        )
        conversation = [
            {
                "id": session_id + "-user-1",
                "role": "user",
                "content": record["prompt"],
                "createdAt": record["timestamp"],
                "runId": run_id,
            },
            {
                "id": session_id + "-assistant-1",
                "role": "assistant",
                "content": (
                    "已并行完成数据检索和会议纪要记忆搜索，并依次完成数据分析、章节扩写、文字润色和内容质检。周报中的指标、分析结论与完成事项均可回看来源。"
                    if record["multi_agent_weekly"]
                    else "已结合本地时间线、知识和文档完成初稿。内容已按结论先行组织，并将关键指标与风险护栏写入正文。"
                ),
                "createdAt": record["timestamp"]
                + (13800 if record["multi_agent_weekly"] else 5200),
                "runId": run_id,
            },
        ]
        if record["multi_agent_weekly"]:
            events = weekly_creation_events(
                session_id,
                run_id,
                record["prompt"],
                record["timestamp"],
                references,
                data_references,
                record["data_analysis"],
                record["content"],
            )
        else:
            events = creation_events(
                session_id,
                run_id,
                record["prompt"],
                record["timestamp"],
                len(references),
            )
        goal = {
            "objective": record["prompt"],
            "status": "completed",
            "revision": 1,
            "remaining_steps": [],
            "outcome": "已生成{}".format(record["doc_type"]),
        }
        patch = {
            "summary": (
                "已基于数据检索、会议纪要和多 Agent 协作完成周报"
                if record["multi_agent_weekly"]
                else "已完成{}首版".format(record["doc_type"])
            ),
            "target_sections": [],
            "changes": [
                {
                    "change_type": "added",
                    "section_title": "完整文档",
                    "start_line": 1,
                    "end_line": len(record["content"].splitlines()),
                    "summary": (
                        "数据分析、章节扩写、文字润色与内容质检均已完成"
                        if record["multi_agent_weekly"]
                        else "基于本地记忆生成完整初稿"
                    ),
                }
            ],
        }
        conn.execute(
            """
            INSERT INTO creation_history (
                prompt, generated_content, doc_type, audience, reference_count,
                created_at, updated_at, model, latency_ms, references_json,
                session_id, conversation_json, agent_trace_json, goal_json,
                root_request, parent_history_id, revision_no, edit_operation,
                document_patch_json, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, 'create_document', ?, '[]')
            """,
            (
                record["prompt"],
                record["content"],
                record["doc_type"],
                record["audience"],
                len(references),
                record["timestamp"] - index * 1000,
                record["timestamp"]
                + (14500 if record["multi_agent_weekly"] else 6200)
                - index * 1000,
                "mbem-v1-local",
                (14500 if record["multi_agent_weekly"] else 6200) + index * 780,
                json_text(references),
                session_id,
                json_text(conversation),
                json_text(events),
                json_text(goal),
                record["prompt"],
                json_text(patch),
            ),
        )


def seed_demo_data(conn: sqlite3.Connection) -> Dict[str, int]:
    timelines = insert_timelines(conn)
    knowledge = insert_knowledge(conn, timelines)
    documents = insert_documents(conn, timelines, knowledge)
    sops = insert_sops(conn, timelines, knowledge)
    data_sources = insert_data_sources(conn, timelines)
    insert_rag_sessions(
        conn, timelines, knowledge, documents, sops, data_sources
    )
    insert_creation_history(conn, documents, data_sources)
    return {
        "captures": sum(len(value["capture_ids"]) for value in timelines.values()),
        "timelines": len(timelines),
        "bake_knowledge": len(knowledge),
        "bake_documents": len(documents),
        "bake_sops": len(sops),
        "data_sources": len(data_sources),
        "rag_sessions": DEMO_TARGET_COUNT,
        "creation_history": DEMO_TARGET_COUNT,
    }


def verification_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    demo_timeline_ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT id FROM timelines WHERE content_origin = ?", (DEMO_MARKER,)
        )
    ]
    counts = {
        "captures": int(
            conn.execute(
                "SELECT COUNT(*) FROM captures WHERE screenshot_source = ?",
                (DEMO_CAPTURE_SOURCE,),
            ).fetchone()[0]
        ),
        "timelines": len(demo_timeline_ids),
        "bake_documents": int(
            conn.execute(
                "SELECT COUNT(*) FROM bake_documents WHERE generation_version = ?",
                (DEMO_MARKER,),
            ).fetchone()[0]
        ),
        "bake_sops": int(
            conn.execute(
                "SELECT COUNT(*) FROM bake_sops WHERE content LIKE ?",
                ("%{}%".format(DEMO_MARKER),),
            ).fetchone()[0]
        ),
        "data_sources": int(
            conn.execute(
                "SELECT COUNT(*) FROM data_sources WHERE canonical_key LIKE ?",
                (DEMO_DATA_KEY_PREFIX + "%",),
            ).fetchone()[0]
        ),
        "rag_sessions": int(
            conn.execute(
                "SELECT COUNT(*) FROM rag_sessions WHERE scene_type = ?", (DEMO_MARKER,)
            ).fetchone()[0]
        ),
        "creation_history": int(
            conn.execute(
                "SELECT COUNT(*) FROM creation_history WHERE session_id LIKE ?",
                (DEMO_CREATION_SESSION_PREFIX + "%",),
            ).fetchone()[0]
        ),
        "bake_knowledge": 0,
    }
    if demo_timeline_ids:
        placeholders = ",".join("?" for _ in demo_timeline_ids)
        counts["bake_knowledge"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM bake_knowledge WHERE timeline_id IN ({})".format(
                    placeholders
                ),
                demo_timeline_ids,
            ).fetchone()[0]
        )
    return counts


def verify_demo_data(conn: sqlite3.Connection) -> Dict[str, int]:
    expected = {
        "captures": DEMO_TARGET_COUNT * 2,
        "timelines": DEMO_TARGET_COUNT,
        "bake_knowledge": DEMO_TARGET_COUNT,
        "bake_documents": DEMO_TARGET_COUNT,
        "bake_sops": DEMO_TARGET_COUNT,
        "data_sources": DEMO_TARGET_COUNT,
        "rag_sessions": DEMO_TARGET_COUNT,
        "creation_history": DEMO_TARGET_COUNT,
    }
    actual = verification_counts(conn)
    mismatches = [
        "{}：期望 {}，实际 {}".format(key, expected[key], actual.get(key, 0))
        for key in expected
        if actual.get(key) != expected[key]
    ]
    if mismatches:
        raise RuntimeError("演示数据校验失败：{}".format("；".join(mismatches)))

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError("SQLite 完整性检查失败：{}".format(integrity))

    data_payloads = conn.execute(
        "SELECT source.title, snapshot.structured_data "
        "FROM data_sources source "
        "JOIN data_snapshots snapshot ON snapshot.source_id = source.id "
        "WHERE source.canonical_key LIKE ?",
        (DEMO_DATA_KEY_PREFIX + "%",),
    ).fetchall()
    for source_title, structured_text in data_payloads:
        structured = json.loads(structured_text)
        rows = structured.get("metric_rows", [])
        summary = structured.get("summary", "")
        if structured.get("extraction_version") != "data-memory.v15":
            raise RuntimeError("演示数据缺少当前语义版本：{}".format(source_title))
        if structured.get("semantic_subject") != source_title:
            raise RuntimeError("演示数据主题与来源不一致：{}".format(source_title))
        if len(rows) < 2:
            raise RuntimeError("演示数据指标不足 2 项：{}".format(source_title))
        first_metric = str(rows[0].get("metric", ""))
        first_value = str(rows[0].get("value", ""))
        if first_metric not in summary or first_value not in summary:
            raise RuntimeError("演示数据摘要不能独立说明首项指标：{}".format(source_title))

    demo_report_url_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM data_sources "
            "WHERE canonical_key LIKE ? AND (source_kind <> 'work_memory' OR source_url IS NOT NULL)",
            (DEMO_DATA_KEY_PREFIX + "%",),
        ).fetchone()[0]
    )
    if demo_report_url_count != 0:
        raise RuntimeError(
            "演示指标不应触发网页实时采集：发现 {} 个网页报表来源".format(
                demo_report_url_count
            )
        )

    meeting_minutes_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM bake_documents "
            "WHERE generation_version = ? AND doc_type = '会议纪要'",
            (DEMO_MARKER,),
        ).fetchone()[0]
    )
    if meeting_minutes_count != 5:
        raise RuntimeError(
            "演示会议纪要数量错误：期望 5，实际 {}".format(meeting_minutes_count)
        )

    weekly_records = conn.execute(
        "SELECT session_id, generated_content, references_json, agent_trace_json "
        "FROM creation_history WHERE session_id LIKE ? ORDER BY updated_at DESC",
        (DEMO_CREATION_SESSION_PREFIX + "weekly-%",),
    ).fetchall()
    if len(weekly_records) != 5:
        raise RuntimeError(
            "多 Agent 周报记录数量错误：期望 5，实际 {}".format(
                len(weekly_records)
            )
        )
    required_actor_ids = {
        "data_search",
        "memory_search",
        "data_analysis_agent",
        "document_writer_agent",
        "detail_polish_agent",
        "quality_review_agent",
    }
    required_sections = [
        "## 二、本周核心数据",
        "## 三、数据分析",
        "## 四、会议决策回顾",
        "## 五、本周已完成事项",
        "## 六、风险与待协同事项",
        "## 七、下周计划",
    ]
    for session_id, content, references_text, trace_text in weekly_records:
        references = json.loads(references_text)
        events = json.loads(trace_text)
        actor_ids = {
            str(event.get("actor", {}).get("id", "")) for event in events
        }
        if not required_actor_ids.issubset(actor_ids):
            raise RuntimeError(
                "多 Agent 周报执行链不完整：{}".format(session_id)
            )
        if len(references) < 3 or any(
            reference.get("doc_type") != "会议纪要" for reference in references
        ):
            raise RuntimeError(
                "周报记忆搜索没有完整引用会议纪要：{}".format(session_id)
            )
        data_events = [
            event
            for event in events
            if event.get("type") == "tool.completed"
            and event.get("actor", {}).get("id") == "data_search"
        ]
        memory_events = [
            event
            for event in events
            if event.get("type") == "tool.completed"
            and event.get("actor", {}).get("id") == "memory_search"
        ]
        if len(data_events) != 1 or len(memory_events) != 1:
            raise RuntimeError("周报检索 Tool 轨迹不完整：{}".format(session_id))
        tool_start_sequences = [
            int(event.get("sequence", 0))
            for event in events
            if event.get("type") == "tool.started"
            and event.get("actor", {}).get("id") in {"data_search", "memory_search"}
        ]
        tool_complete_sequences = [
            int(data_events[0].get("sequence", 0)),
            int(memory_events[0].get("sequence", 0)),
        ]
        if (
            len(tool_start_sequences) != 2
            or max(tool_start_sequences) >= min(tool_complete_sequences)
        ):
            raise RuntimeError("周报双 Tool 未按并行轨迹记录：{}".format(session_id))
        data_references = data_events[0].get("environment_patch", {}).get(
            "data_sources", []
        )
        memory_references = memory_events[0].get("environment_patch", {}).get(
            "references", []
        )
        if len(data_references) != 4 or len(memory_references) != len(references):
            raise RuntimeError("周报检索来源数量错误：{}".format(session_id))
        quality_events = [
            event
            for event in events
            if event.get("type") == "agent.completed"
            and event.get("actor", {}).get("id") == "quality_review_agent"
        ]
        quality_review = (
            quality_events[0].get("environment_patch", {}).get("quality_review", {})
            if quality_events
            else {}
        )
        if not quality_review or not all(quality_review.values()):
            raise RuntimeError("周报内容质检未全部通过：{}".format(session_id))
        if len(events) < 17 or any(section not in content for section in required_sections):
            raise RuntimeError("周报正文或执行轨迹不完整：{}".format(session_id))

    if table_exists(conn, "bake_documents_fts"):
        document_hits = int(
            conn.execute(
                "SELECT COUNT(*) FROM bake_documents_fts "
                "WHERE bake_documents_fts MATCH 'AI' "
                "AND rowid IN (SELECT id FROM bake_documents WHERE generation_version = ?)",
                (DEMO_MARKER,),
            ).fetchone()[0]
        )
        if document_hits < 2:
            raise RuntimeError("演示文档全文索引不完整：命中 {} 条".format(document_hits))

    if table_exists(conn, "bake_knowledge_fts"):
        knowledge_hits = int(
            conn.execute(
                "SELECT COUNT(*) FROM bake_knowledge_fts "
                "WHERE bake_knowledge_fts MATCH 'AI' "
                "AND rowid IN ("
                "  SELECT id FROM bake_knowledge WHERE timeline_id IN ("
                "    SELECT id FROM timelines WHERE content_origin = ?"
                "  )"
                ")",
                (DEMO_MARKER,),
            ).fetchone()[0]
        )
        if knowledge_hits < 1:
            raise RuntimeError("演示知识全文索引不完整：命中 {} 条".format(knowledge_hits))

    index_checks: List[Tuple[str, str, Sequence[Any], int]] = [
        (
            "captures_fts",
            "SELECT COUNT(*) FROM captures_fts WHERE rowid IN "
            "(SELECT id FROM captures WHERE screenshot_source = ?)",
            (DEMO_CAPTURE_SOURCE,),
            DEMO_TARGET_COUNT * 2,
        ),
        (
            "knowledge_fts",
            "SELECT COUNT(*) FROM knowledge_fts WHERE rowid IN "
            "(SELECT id FROM timelines WHERE content_origin = ?)",
            (DEMO_MARKER,),
            DEMO_TARGET_COUNT,
        ),
        (
            "bake_knowledge_fts",
            "SELECT COUNT(*) FROM bake_knowledge_fts WHERE rowid IN "
            "(SELECT id FROM bake_knowledge WHERE timeline_id IN "
            "(SELECT id FROM timelines WHERE content_origin = ?))",
            (DEMO_MARKER,),
            DEMO_TARGET_COUNT,
        ),
        (
            "bake_documents_fts",
            "SELECT COUNT(*) FROM bake_documents_fts WHERE rowid IN "
            "(SELECT id FROM bake_documents WHERE generation_version = ?)",
            (DEMO_MARKER,),
            DEMO_TARGET_COUNT,
        ),
        (
            "bake_sops_fts",
            "SELECT COUNT(*) FROM bake_sops_fts WHERE rowid IN "
            "(SELECT id FROM bake_sops WHERE content LIKE ?)",
            ("%{}%".format(DEMO_MARKER),),
            DEMO_TARGET_COUNT,
        ),
        (
            "data_snapshots_fts",
            "SELECT COUNT(*) FROM data_snapshots_fts WHERE rowid IN "
            "(SELECT snapshot.id FROM data_snapshots snapshot "
            "JOIN data_sources source ON source.id = snapshot.source_id "
            "WHERE source.canonical_key LIKE ?)",
            (DEMO_DATA_KEY_PREFIX + "%",),
            DEMO_TARGET_COUNT,
        ),
    ]
    for table, query, params, expected_count in index_checks:
        if not table_exists(conn, table):
            continue
        indexed_count = int(conn.execute(query, params).fetchone()[0])
        if indexed_count != expected_count:
            raise RuntimeError(
                "{} 索引不完整：期望 {}，实际 {}".format(
                    table, expected_count, indexed_count
                )
            )
    return actual


def print_counts(title: str, counts: Dict[str, int]) -> None:
    print(title)
    labels = {
        "captures": "采集记录",
        "timelines": "时间线",
        "bake_knowledge": "知识",
        "bake_documents": "文档",
        "bake_sops": "操作",
        "data_sources": "数据",
        "rag_sessions": "咨询记录",
        "creation_history": "创作记录",
    }
    for key in (
        "captures",
        "timelines",
        "bake_knowledge",
        "bake_documents",
        "bake_sops",
        "data_sources",
        "rag_sessions",
        "creation_history",
    ):
        print("  - {}：{}".format(labels[key], counts.get(key, 0)))


def main() -> int:
    args = parse_args()
    database_path = args.db.expanduser().resolve()
    if not database_path.is_file():
        print("错误：数据库不存在：{}".format(database_path), file=sys.stderr)
        return 2

    conn = connect_database(database_path)
    try:
        validate_schema(conn)
        if args.verify:
            counts = verify_demo_data(conn)
            print_counts("演示数据完整，SQLite 完整性检查通过：", counts)
            return 0

        backup_path = None
        if not args.no_backup:
            backup_path = backup_database(database_path, args.backup_dir)

        with conn:
            removed = remove_demo_data(conn)
            if args.remove:
                rebuild_search_indexes(conn)
                counts = verification_counts(conn)
            else:
                seed_demo_data(conn)
                rebuild_search_indexes(conn)
                counts = verify_demo_data(conn)

        if args.remove:
            print_counts("已移除官网演示数据，当前演示记录数：", counts)
        else:
            print_counts("官网演示数据已写入并校验通过：", counts)
        removed_total = sum(max(0, value) for value in removed.values())
        print("  - 本次替换的旧演示关联记录：{}".format(removed_total))
        if backup_path is not None:
            print("  - 写入前备份：{}".format(backup_path))
        print("  - 数据库：{}".format(database_path))
        return 0
    except (RuntimeError, sqlite3.Error, OSError) as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
