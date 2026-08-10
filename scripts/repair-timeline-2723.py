#!/usr/bin/env python3
"""一次性数据修复：把误并入 timeline 2723 的低价值 capture 24059 剔除。

背景：旧代码中低价值 capture（Amphetamine 菜单截图）与真实工作 capture 24065
同组提炼时，被一起写入时间线 2723 的 capture_ids 与 key_timestamps。
根治修复后（extract_merged 分段丢弃过滤 + 隐藏回收时间线），此路径已堵住；
本脚本清理存量脏数据：
  1. 2723.capture_ids 去掉 24059；key_timestamps 去掉 [24059] 分段；
  2. 24059 挂到隐藏的"低价值采集回收"时间线（与新代码
     background_processor._get_low_value_sink_timeline_id 的约定一致），
     保持已处理状态，避免重新进入提炼队列。

幂等：重复运行不会重复创建回收时间线，也不会在已修复时报错。
"""

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path.home() / ".memory-bread" / "memory-bread.db"
TIMELINE_ID = 2723
BAD_CAPTURE_ID = 24059
GOOD_CAPTURE_ID = 24065
SINK_SUMMARY = "低价值采集回收"


def main() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(
            "SELECT capture_ids, key_timestamps FROM timelines WHERE id = ?",
            (TIMELINE_ID,),
        ).fetchone()
        if row is None:
            print(f"timeline {TIMELINE_ID} 不存在，跳过")
            return

        capture_ids = json.loads(row[0] or "[]")
        if BAD_CAPTURE_ID not in capture_ids:
            print(f"timeline {TIMELINE_ID} 已不含 capture {BAD_CAPTURE_ID}，无需修复")
            return

        # 1. 获取/创建隐藏回收时间线（约定与新代码一致）
        sink = conn.execute(
            "SELECT id FROM timelines WHERE summary = ? AND is_self_generated = 1 "
            "ORDER BY id LIMIT 1",
            (SINK_SUMMARY,),
        ).fetchone()
        if sink:
            sink_id = sink[0]
        else:
            now_ms = int(time.time() * 1000)
            cursor = conn.execute(
                """
                INSERT INTO timelines (
                    capture_id, summary, overview, details, entities, category,
                    importance, occurrence_count, observed_at, history_view,
                    content_origin, activity_type, is_self_generated,
                    evidence_strength, created_at_ms, updated_at_ms
                )
                VALUES (?, ?, NULL, ?, '[]', '其他', 0, 0, ?, 1, 'system',
                        'background_task', 1, 'low', ?, ?)
                """,
                (
                    BAD_CAPTURE_ID,
                    SINK_SUMMARY,
                    "确定性丢弃的低价值采集挂此隐藏时间线回收，不对外展示。",
                    now_ms,
                    now_ms,
                    now_ms,
                ),
            )
            sink_id = cursor.lastrowid

        # 2. 从 2723 剔除 24059
        new_capture_ids = [cid for cid in capture_ids if cid != BAD_CAPTURE_ID]
        key_timestamps = json.loads(row[1] or "[]")
        new_key_timestamps = [
            seg for seg in key_timestamps
            if BAD_CAPTURE_ID not in (seg.get("capture_ids") or [])
        ]
        conn.execute(
            "UPDATE timelines SET capture_ids = ?, key_timestamps = ?, updated_at_ms = ? "
            "WHERE id = ?",
            (
                json.dumps(new_capture_ids),
                json.dumps(new_key_timestamps, ensure_ascii=False),
                int(time.time() * 1000),
                TIMELINE_ID,
            ),
        )

        # 3. 24059 挂到回收时间线
        conn.execute(
            "UPDATE captures SET timeline_id = ? WHERE id = ?",
            (sink_id, BAD_CAPTURE_ID),
        )
        conn.commit()
        print(
            f"修复完成: timeline {TIMELINE_ID} capture_ids={new_capture_ids}; "
            f"capture {BAD_CAPTURE_ID} → 回收时间线 {sink_id}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
