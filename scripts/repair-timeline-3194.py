"""重置 timeline 3194 的混组数据，让修复后的流水线重新提炼。

该时间线把 ChatGPT、杭州天气、Kim 消息流和 GPU 页面错误合并，并进一步
生成了只属于 timeline:3194 的污染 work_memory 数据源。本脚本会：

1. 使用 SQLite backup API 创建一致性数据库备份；
2. 将 timeline 3194 的成员 captures 重新置为待提炼；
3. 删除指向 timeline 3194 的数据来源链接（包括外键置空后键名仍残留的链接）；
4. 删除仅由该混组时间线生成的数据源及其级联快照/链接；
5. 删除 timeline 3194，让新分组与一致性门禁重新生成正确时间线。

脚本幂等：timeline 已不存在时不会重复修改。默认数据库为本机开发数据。
"""

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple


TARGET_TIMELINE_ID = 3194
DEFAULT_DB_PATH = Path.home() / ".memory-bread" / "memory-bread.db"


def _capture_ids(raw_value: object) -> List[int]:
    try:
        values = json.loads(str(raw_value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(values, list):
        return []
    result: List[int] = []
    for value in values:
        try:
            capture_id = int(value)
        except (TypeError, ValueError):
            continue
        if capture_id not in result:
            result.append(capture_id)
    return result


def _contaminated_source_ids(
    conn: sqlite3.Connection,
    timeline_id: int,
) -> List[int]:
    semantic_scope = "timeline:{0}".format(timeline_id)
    rows = conn.execute(
        """
        SELECT DISTINCT ds.id
        FROM data_sources ds
        JOIN data_snapshots snapshot ON snapshot.source_id = ds.id
        WHERE json_extract(snapshot.provenance, '$.semantic_scope') = ?
          AND NOT EXISTS (
              SELECT 1
              FROM data_source_links link
              WHERE link.source_id = ds.id
                AND COALESCE(link.timeline_id, -1) <> ?
          )
          AND NOT EXISTS (
              SELECT 1
              FROM data_snapshots other_snapshot
              WHERE other_snapshot.source_id = ds.id
                AND COALESCE(
                    json_extract(other_snapshot.provenance, '$.semantic_scope'),
                    ''
                ) <> ?
          )
        ORDER BY ds.id
        """,
        (semantic_scope, timeline_id, semantic_scope),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _stale_source_link_ids(
    conn: sqlite3.Connection,
    timeline_id: int,
) -> List[int]:
    encoded_timeline = "%:discovered:{0}:%".format(timeline_id)
    rows = conn.execute(
        """
        SELECT id
        FROM data_source_links
        WHERE timeline_id = ?
           OR source_ref_key LIKE ?
        ORDER BY id
        """,
        (timeline_id, encoded_timeline),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _backup_database(db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / (
        "memory-bread-before-timeline-{0}-repair-{1}.db".format(
            TARGET_TIMELINE_ID,
            timestamp,
        )
    )
    source = sqlite3.connect(str(db_path))
    target = sqlite3.connect(str(backup_path))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


def repair(
    db_path: Path,
    dry_run: bool = False,
    create_backup: bool = True,
) -> Tuple[List[int], List[int], Optional[Path]]:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        row = conn.execute(
            "SELECT capture_ids FROM timelines WHERE id = ?",
            (TARGET_TIMELINE_ID,),
        ).fetchone()
        member_ids = _capture_ids(row[0]) if row is not None else []
        contaminated_source_ids = _contaminated_source_ids(
            conn,
            TARGET_TIMELINE_ID,
        )
        stale_source_link_ids = _stale_source_link_ids(
            conn,
            TARGET_TIMELINE_ID,
        )
        if row is None and not contaminated_source_ids and not stale_source_link_ids:
            print("timeline 3194 及其残留引用已不存在，无需修复")
            return [], [], None
        print(
            "待重提炼 captures={0}; 待清理污染 data_sources={1}; "
            "待清理残留 source_links={2}".format(
                member_ids,
                contaminated_source_ids,
                stale_source_link_ids,
            )
        )
        if dry_run:
            return member_ids, contaminated_source_ids, None
    finally:
        conn.close()

    backup_path = _backup_database(db_path) if create_backup else None
    if backup_path is not None:
        print("数据库备份：{0}".format(backup_path))

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if member_ids:
            placeholders = ",".join("?" for _ in member_ids)
            conn.execute(
                "UPDATE captures SET timeline_id = NULL "
                "WHERE timeline_id = ? AND id IN ({0})".format(placeholders),
                [TARGET_TIMELINE_ID] + member_ids,
            )
        if stale_source_link_ids:
            placeholders = ",".join("?" for _ in stale_source_link_ids)
            conn.execute(
                "DELETE FROM data_source_links WHERE id IN ({0})".format(
                    placeholders
                ),
                stale_source_link_ids,
            )
        if contaminated_source_ids:
            placeholders = ",".join("?" for _ in contaminated_source_ids)
            conn.execute(
                "DELETE FROM data_sources WHERE id IN ({0})".format(placeholders),
                contaminated_source_ids,
            )
        conn.execute(
            "DELETE FROM timelines WHERE id = ?",
            (TARGET_TIMELINE_ID,),
        )

        remaining_timeline = conn.execute(
            "SELECT COUNT(*) FROM timelines WHERE id = ?",
            (TARGET_TIMELINE_ID,),
        ).fetchone()[0]
        remaining_links = conn.execute(
            "SELECT COUNT(*) FROM captures WHERE timeline_id = ?",
            (TARGET_TIMELINE_ID,),
        ).fetchone()[0]
        remaining_sources = 0
        if contaminated_source_ids:
            placeholders = ",".join("?" for _ in contaminated_source_ids)
            remaining_sources = conn.execute(
                "SELECT COUNT(*) FROM data_sources WHERE id IN ({0})".format(
                    placeholders
                ),
                contaminated_source_ids,
            ).fetchone()[0]
        remaining_source_links = conn.execute(
            """
            SELECT COUNT(*)
            FROM data_source_links
            WHERE timeline_id = ?
               OR source_ref_key LIKE ?
            """,
            (
                TARGET_TIMELINE_ID,
                "%:discovered:{0}:%".format(TARGET_TIMELINE_ID),
            ),
        ).fetchone()[0]
        if (
            remaining_timeline
            or remaining_links
            or remaining_sources
            or remaining_source_links
        ):
            raise RuntimeError(
                "修复后校验失败: timeline={0} capture_links={1} "
                "sources={2} source_links={3}".format(
                    remaining_timeline,
                    remaining_links,
                    remaining_sources,
                    remaining_source_links,
                )
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(
        "修复完成：timeline 3194 已删除，{0} 条 captures 已等待新流水线重提炼".format(
            len(member_ids)
        )
    )
    return member_ids, contaminated_source_ids, backup_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="仅供数据库副本测试；真实数据不要关闭备份",
    )
    args = parser.parse_args()
    if not args.db.is_file():
        raise SystemExit("数据库不存在：{0}".format(args.db))
    repair(
        args.db,
        dry_run=args.dry_run,
        create_backup=not args.no_backup,
    )


if __name__ == "__main__":
    main()
