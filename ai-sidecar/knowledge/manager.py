"""
知识库管理模块 - 负责知识条目的 CRUD 操作
"""

from __future__ import annotations

import sqlite3
import json
import logging
from typing import Any, Dict, List, Any, Optional
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class KnowledgeManager:
    """知识库管理器"""

    def __init__(self, db_path: str = None):
        """
        初始化知识库管理器

        Args:
            db_path: SQLite 数据库路径
        """
        if db_path is None:
            db_path = str(Path.home() / ".memory-bread" / "memory-bread.db")

        self.db_path = db_path
        logger.info(f"初始化知识库管理器，数据库: {db_path}")
        self._init_tables()

    def _init_tables(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 创建知识条目表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS timelines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                capture_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                overview TEXT,
                details TEXT,
                entities TEXT,
                category TEXT,
                importance INTEGER DEFAULT 3,
                occurrence_count INTEGER DEFAULT 1,
                user_verified BOOLEAN DEFAULT 0,
                user_edited BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (capture_id) REFERENCES captures(id)
            )
        """)

        self._ensure_fragment_columns(cursor)
        self._ensure_semantic_columns(cursor)

        # 创建全文搜索索引
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                summary,
                entities,
                content='timelines',
                content_rowid='id'
            )
        """)

        # 创建触发器
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON timelines BEGIN
                INSERT INTO knowledge_fts(rowid, summary, entities)
                VALUES (new.id, new.summary, new.entities);
            END
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON timelines BEGIN
                UPDATE knowledge_fts SET summary = new.summary, entities = new.entities
                WHERE rowid = new.id;
            END
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON timelines BEGIN
                DELETE FROM knowledge_fts WHERE rowid = old.id;
            END
        """)

        conn.commit()
        conn.close()
        logger.info("知识库表初始化完成")

    @staticmethod
    def _ensure_semantic_columns(cursor: sqlite3.Cursor) -> None:
        """兼容旧数据库：补齐知识语义字段。"""
        cursor.execute("PRAGMA table_info(timelines)")
        existing = {row[1] for row in cursor.fetchall()}

        expected_columns = {
            'observed_at': 'INTEGER',
            'event_time_start': 'INTEGER',
            'event_time_end': 'INTEGER',
            'history_view': 'INTEGER NOT NULL DEFAULT 0',
            'content_origin': 'TEXT',
            'activity_type': 'TEXT',
            'is_self_generated': 'INTEGER NOT NULL DEFAULT 0',
            'evidence_strength': 'TEXT',
            'work_item': 'TEXT',
            'work_status': 'TEXT',
            'work_progress': 'TEXT',
        }

        for name, col_type in expected_columns.items():
            if name not in existing:
                logger.info("为 timelines 自动补列: %s", name)
                cursor.execute(f"ALTER TABLE timelines ADD COLUMN {name} {col_type}")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_observed_at ON timelines(observed_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_event_time ON timelines(event_time_start, event_time_end)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_activity_type ON timelines(activity_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_history_view ON timelines(history_view)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_self_generated ON timelines(is_self_generated)")

    @staticmethod
    def _ensure_fragment_columns(cursor: sqlite3.Cursor) -> None:
        """兼容旧数据库：补齐片段知识相关列与索引。"""
        cursor.execute("PRAGMA table_info(timelines)")
        existing = {row[1] for row in cursor.fetchall()}

        expected_columns = {
            'capture_ids': 'TEXT',
            'start_time': 'INTEGER',
            'end_time': 'INTEGER',
            'duration_minutes': 'INTEGER',
            'frag_app_name': 'TEXT',
            'frag_win_title': 'TEXT',
        }

        for name, col_type in expected_columns.items():
            if name not in existing:
                logger.info("为 timelines 自动补列: %s", name)
                cursor.execute(f"ALTER TABLE timelines ADD COLUMN {name} {col_type}")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_time ON timelines(start_time, end_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_app ON timelines(frag_app_name)")

    def add_entry(self, knowledge: Dict[str, Any]) -> int:
        """
        添加知识条目

        Args:
            knowledge: 知识字典

        Returns:
            新增条目的 ID
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 支持新旧两种格式
        overview = knowledge.get('overview') or knowledge.get('summary', '')
        details = knowledge.get('details', '')

        cursor.execute("""
            INSERT INTO timelines (
                capture_id, summary, overview, details, entities, category,
                importance, occurrence_count, observed_at, event_time_start,
                event_time_end, history_view, content_origin, activity_type,
                is_self_generated, evidence_strength, work_item, work_status,
                work_progress
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            knowledge['capture_id'],
            overview,  # 保持向后兼容
            overview,
            details,
            knowledge.get('entities', '[]'),
            knowledge.get('category', '其他'),
            knowledge.get('importance', 3),
            knowledge.get('occurrence_count', 1),
            knowledge.get('observed_at'),
            knowledge.get('event_time_start'),
            knowledge.get('event_time_end'),
            int(bool(knowledge.get('history_view', False))),
            knowledge.get('content_origin'),
            knowledge.get('activity_type'),
            int(bool(knowledge.get('is_self_generated', False))),
            knowledge.get('evidence_strength'),
            knowledge.get('work_item'),
            knowledge.get('work_status'),
            knowledge.get('work_progress'),
        ))

        entry_id = cursor.lastrowid
        conn.commit()
        conn.close()

        logger.info(f"添加知识条目 {entry_id}: {overview[:50]}...")
        return entry_id

    def get_entries(
        self,
        limit: int = 50,
        offset: int = 0,
        category: Optional[str] = None,
        verified_only: bool = False
    ) -> List[Dict[str, Any]]:
        """
        获取知识条目列表

        Args:
            limit: 返回数量限制
            offset: 偏移量
            category: 分类筛选
            verified_only: 只返回已验证的条目

        Returns:
            知识条目列表
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = "SELECT * FROM timelines WHERE 1=1"
        params = []

        if category:
            query += " AND category = ?"
            params.append(category)

        if verified_only:
            query += " AND user_verified = 1"

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        entries = []
        for row in rows:
            entry = dict(row)
            # 解析 JSON 字段
            if entry['entities']:
                try:
                    entry['entities'] = json.loads(entry['entities'])
                except:
                    entry['entities'] = []
            else:
                entry['entities'] = []
            entries.append(entry)

        return entries

    def get_entry(self, entry_id: int) -> Optional[Dict[str, Any]]:
        """获取单个知识条目"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM timelines WHERE id = ?", (entry_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        entry = dict(row)
        if entry['entities']:
            try:
                entry['entities'] = json.loads(entry['entities'])
            except:
                entry['entities'] = []
        else:
            entry['entities'] = []

        return entry

    def update_entry(self, entry_id: int, updates: Dict[str, Any]) -> bool:
        """
        更新知识条目

        Args:
            entry_id: 条目 ID
            updates: 更新字段字典

        Returns:
            是否更新成功
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 构建更新语句
        set_clauses = []
        params = []

        allowed_fields = {
            'summary', 'entities', 'category', 'importance', 'user_verified', 'user_edited',
            'overview', 'details', 'occurrence_count', 'observed_at', 'event_time_start',
            'event_time_end', 'history_view', 'content_origin', 'activity_type',
            'is_self_generated', 'evidence_strength',
        }

        for key, value in updates.items():
            if key in allowed_fields:
                set_clauses.append(f"{key} = ?")
                if key == 'entities' and isinstance(value, list):
                    params.append(json.dumps(value, ensure_ascii=False))
                elif key in {'history_view', 'is_self_generated'}:
                    params.append(int(bool(value)))
                else:
                    params.append(value)

        if not set_clauses:
            conn.close()
            return False

        set_clauses.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(entry_id)

        query = f"UPDATE timelines SET {', '.join(set_clauses)} WHERE id = ?"
        cursor.execute(query, params)

        success = cursor.rowcount > 0
        conn.commit()
        conn.close()

        if success:
            logger.info(f"更新知识条目 {entry_id}")

        return success

    def delete_entry(self, entry_id: int) -> bool:
        """删除知识条目"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("DELETE FROM timelines WHERE id = ?", (entry_id,))
        success = cursor.rowcount > 0

        conn.commit()
        conn.close()

        if success:
            logger.info(f"删除知识条目 {entry_id}")

        return success

    def search_entries(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        全文搜索知识条目

        Args:
            query: 搜索关键词
            limit: 返回数量限制

        Returns:
            匹配的知识条目列表
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT ke.* FROM timelines ke
            JOIN knowledge_fts kf ON ke.id = kf.rowid
            WHERE knowledge_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit))

        rows = cursor.fetchall()
        conn.close()

        entries = []
        for row in rows:
            entry = dict(row)
            if entry['entities']:
                try:
                    entry['entities'] = json.loads(entry['entities'])
                except:
                    entry['entities'] = []
            else:
                entry['entities'] = []
            entries.append(entry)

        return entries

    def count_entries(
        self,
        category: Optional[str] = None,
        verified_only: bool = False
    ) -> int:
        """
        统计知识条目数量

        Args:
            category: 分类筛选
            verified_only: 只统计已验证的条目

        Returns:
            条目数量
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        query = "SELECT COUNT(*) FROM timelines WHERE 1=1"
        params = []

        if category:
            query += " AND category = ?"
            params.append(category)

        if verified_only:
            query += " AND user_verified = 1"

        cursor.execute(query, params)
        count = cursor.fetchone()[0]
        conn.close()

        return count

    def get_stats(self) -> Dict[str, Any]:
        """获取知识库统计信息"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 总条目数
        cursor.execute("SELECT COUNT(*) FROM timelines")
        total = cursor.fetchone()[0]

        # 已验证条目数
        cursor.execute("SELECT COUNT(*) FROM timelines WHERE user_verified = 1")
        verified = cursor.fetchone()[0]

        # 分类统计
        cursor.execute("""
            SELECT category, COUNT(*) as count
            FROM timelines
            GROUP BY category
        """)
        categories = {row[0]: row[1] for row in cursor.fetchall()}

        conn.close()

        return {
            'total': total,
            'verified': verified,
            'unverified': total - verified,
            'categories': categories
        }


# 测试代码
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    manager = KnowledgeManager()

    # 测试添加
    test_knowledge = {
        'capture_id': 1,
        'summary': '产品评审会确定 Q1 路线图，优先开发 OCR 采集功能',
        'entities': json.dumps(['张三', '李四', '王五', 'Q1', 'OCR'], ensure_ascii=False),
        'category': '会议',
        'importance': 5
    }

    entry_id = manager.add_entry(test_knowledge)
    print(f"添加条目 ID: {entry_id}")

    # 测试查询
    entries = manager.get_entries(limit=10)
    print(f"\n查询到 {len(entries)} 条记录")
    for entry in entries:
        print(f"- {entry['summary'][:50]}...")

    # 测试统计
    stats = manager.get_stats()
    print(f"\n统计信息: {json.dumps(stats, indent=2, ensure_ascii=False)}")
