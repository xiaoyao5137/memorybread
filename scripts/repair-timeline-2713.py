"""修复 timeline 2713 脏数据：拆分被相似度去重误合并的采集记录。

背景：2026-08-06 02:40 与 02:52，相似度去重（阈值 0.72）先后把"排查时间线 2148
+ 创作润色死循环"的两个片段（captures 24019-24040、24041-24046）误并入主题
完全不同的 timeline 2713（"排查 ID1230 低价值数据"）。本脚本：
1. 把这两段共 27 条采集拆到一条新时间线（内容取自 2713 details 的两段"补充"）；
2. 还原 2713 的成员、时间范围、occurrence_count 与 details；
3. 把初始分组里混入的 4 条无关采集（Word/Kim/云集）解除关联，交回提炼管线。
"""

import json
import sqlite3
import time

DB = "/Users/xianjiaqi/.memory-bread/memory-bread.db"
MARKER_1 = "\n\n--- 补充 (2026-08-06 02:40) ---\n"
MARKER_2 = "\n\n--- 补充 (2026-08-06 02:52) ---\n"
MERGED_IDS_BATCH_1 = [24019, 24020, 24021, 24022, 24024, 24025, 24026, 24027,
                      24028, 24029, 24030, 24031, 24032, 24033, 24034, 24035,
                      24036, 24037, 24038, 24039, 24040]
MERGED_IDS_BATCH_2 = [24041, 24042, 24043, 24044, 24045, 24046]
MERGED_IDS = MERGED_IDS_BATCH_1 + MERGED_IDS_BATCH_2
KEEP_IDS = [23984, 23985, 23986, 23987, 23988, 23989]
UNRELATED_INITIAL_IDS = [23983, 23990, 23991, 23992]
ALL_ORIGINAL_IDS = KEEP_IDS + UNRELATED_INITIAL_IDS

NEW_OVERVIEW = (
    "用户针对 MemoryBread 项目中时间线采集记录异常（ID 2148 时间线混入大量不相关采集）"
    "及创作记录细节润色环节死循环问题进行排查。通过执行 SQL 查询本地数据库、检索 "
    "group_captures 与润色相关代码定位根因：跨批合并依赖启发式判断、相似度去重阈值"
    "因实体加分变相降低且对比锚点陈旧。随后针对润色 Agent 流式输出问题制定优化方案"
    "并开始修改服务端 agent_loop.py。"
)
NEW_ENTITIES = ["MemoryBread", "2148", "group_captures", "agent_loop.py", "相似度去重"]


def main() -> None:
    conn = sqlite3.connect(DB, timeout=30)
    try:
        row = conn.execute(
            "SELECT details, occurrence_count, capture_ids FROM timelines WHERE id = 2713"
        ).fetchone()
        if not row:
            raise SystemExit("timeline 2713 不存在")
        details, occurrence_count, capture_ids_raw = row
        if MARKER_1 not in (details or "") or MARKER_2 not in (details or ""):
            raise SystemExit("未找到两次误合并的补充段落，疑似已修复或数据结构变化")
        if occurrence_count != 3:
            raise SystemExit(f"occurrence_count={occurrence_count}，与预期 3 不符，中止")
        current_ids = json.loads(capture_ids_raw or "[]")
        if sorted(current_ids) != sorted(MERGED_IDS + ALL_ORIGINAL_IDS):
            raise SystemExit(f"capture_ids 与预期不符: {current_ids}")

        original_details, rest = details.split(MARKER_1, 1)
        supplement_1, supplement_2 = rest.split(MARKER_2, 1)
        supplement = MARKER_1.strip("\n") + "\n" + supplement_1.strip() + "\n\n" + \
            MARKER_2.strip("\n") + "\n" + supplement_2.strip()

        ts_start = conn.execute(
            "SELECT MIN(ts) FROM captures WHERE id IN (%s)" % ",".join("?" * len(MERGED_IDS)),
            MERGED_IDS,
        ).fetchone()[0]
        ts_end = conn.execute(
            "SELECT MAX(ts) FROM captures WHERE id IN (%s)" % ",".join("?" * len(MERGED_IDS)),
            MERGED_IDS,
        ).fetchone()[0]
        keep_start = conn.execute(
            "SELECT MIN(ts) FROM captures WHERE id IN (%s)" % ",".join("?" * len(KEEP_IDS)),
            KEEP_IDS,
        ).fetchone()[0]
        keep_end = conn.execute(
            "SELECT MAX(ts) FROM captures WHERE id IN (%s)" % ",".join("?" * len(KEEP_IDS)),
            KEEP_IDS,
        ).fetchone()[0]

        now_ms = int(time.time() * 1000)
        cursor = conn.execute(
            """
            INSERT INTO timelines (
                capture_id, summary, overview, details, entities, category, importance,
                occurrence_count, capture_ids, start_time, end_time, time_range_start,
                time_range_end, observed_at, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, '代码', 4, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                MERGED_IDS[0],
                NEW_OVERVIEW[:50],
                NEW_OVERVIEW,
                supplement,
                json.dumps(NEW_ENTITIES, ensure_ascii=False),
                json.dumps(MERGED_IDS, ensure_ascii=False),
                ts_start, ts_end, ts_start, ts_end, ts_end, now_ms, now_ms,
            ),
        )
        new_timeline_id = cursor.lastrowid

        conn.execute(
            "UPDATE captures SET timeline_id = ? WHERE id IN (%s)"
            % ",".join("?" * len(MERGED_IDS)),
            [new_timeline_id] + MERGED_IDS,
        )
        conn.execute(
            "UPDATE captures SET timeline_id = NULL WHERE id IN (%s)"
            % ",".join("?" * len(UNRELATED_INITIAL_IDS)),
            UNRELATED_INITIAL_IDS,
        )
        conn.execute(
            """
            UPDATE timelines SET
                occurrence_count = 1,
                details = ?,
                capture_ids = ?,
                start_time = ?,
                end_time = ?,
                time_range_start = ?,
                time_range_end = ?,
                observed_at = ?,
                updated_at_ms = ?
            WHERE id = 2713
            """,
            (
                original_details,
                json.dumps(KEEP_IDS, ensure_ascii=False),
                keep_start, keep_end, keep_start, keep_end, keep_end, now_ms,
            ),
        )
        conn.execute(
            """
            INSERT INTO timeline_data_fact_runs (
                timeline_id, contract_version, accepted_count, rejected_count,
                created_at, updated_at
            ) VALUES (?, 'timeline-data-fact.v2', 0, 0, ?, ?)
            """,
            (new_timeline_id, now_ms, now_ms),
        )
        conn.commit()

        member_count = conn.execute(
            "SELECT COUNT(*) FROM captures WHERE timeline_id = 2713"
        ).fetchone()[0]
        new_member_count = conn.execute(
            "SELECT COUNT(*) FROM captures WHERE timeline_id = ?",
            (new_timeline_id,),
        ).fetchone()[0]
        print(f"OK 新时间线 id={new_timeline_id} 成员={new_member_count}")
        print(f"OK timeline 2713 剩余成员={member_count} (预期 {len(KEEP_IDS)})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
