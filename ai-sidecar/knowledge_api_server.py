#!/usr/bin/env python3

from __future__ import annotations

"""
临时知识库 API 服务器
在 Core Engine 端口 7070 上提供知识库 API
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import sqlite3
import json
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DB_PATH = str(Path.home() / ".memory-bread" / "memory-bread.db")

def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({"status": "ok", "version": "0.1.0"})

@app.route('/api/captures', methods=['GET'])
def list_captures():
    """获取采集记录列表"""
    try:
        limit = int(request.args.get('limit', 20))

        conn = get_db()
        cursor = conn.cursor()

        # 检查 captures 表是否存在
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='captures'")
        if not cursor.fetchone():
            conn.close()
            return jsonify({'captures': [], 'total': 0})

        cursor.execute("SELECT COUNT(*) FROM captures")
        total = cursor.fetchone()[0]

        # 查询采集记录，并关联知识库
        cursor.execute(f"""
            SELECT
                c.*,
                k.id as knowledge_id,
                k.summary as knowledge_summary,
                k.category as knowledge_category,
                k.importance as knowledge_importance
            FROM captures c
            LEFT JOIN timelines k ON c.id = k.capture_id
            ORDER BY c.ts DESC
            LIMIT {limit}
        """)
        rows = cursor.fetchall()

        captures = []
        for row in rows:
            capture = dict(row)
            # 如果有关联的知识，添加到 capture 对象中
            if capture.get('knowledge_id'):
                capture['knowledge'] = {
                    'id': capture['knowledge_id'],
                    'summary': capture['knowledge_summary'],
                    'category': capture['knowledge_category'],
                    'importance': capture['knowledge_importance']
                }
                # 删除临时字段
                del capture['knowledge_id']
                del capture['knowledge_summary']
                del capture['knowledge_category']
                del capture['knowledge_importance']
            else:
                capture['knowledge'] = None
                # 删除 None 字段
                for key in ['knowledge_id', 'knowledge_summary', 'knowledge_category', 'knowledge_importance']:
                    if key in capture:
                        del capture[key]

            captures.append(capture)

        conn.close()

        return jsonify({'captures': captures, 'total': total})

    except Exception as e:
        logger.error(f"获取采集记录失败: {e}")
        return jsonify({'captures': [], 'total': 0})

@app.route('/api/vector/status', methods=['GET'])
def vector_status():
    """获取向量化状态"""
    try:
        conn = get_db()
        cursor = conn.cursor()

        # 检查表是否存在
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vector_status'")
        if not cursor.fetchone():
            conn.close()
            return jsonify({'items': []})

        cursor.execute("SELECT * FROM vector_status LIMIT 100")
        rows = cursor.fetchall()

        items = [dict(row) for row in rows]
        conn.close()

        return jsonify({'items': items})

    except Exception as e:
        logger.error(f"获取向量状态失败: {e}")
        return jsonify({'items': []})

@app.route('/api/stats', methods=['GET'])
def system_stats():
    """获取系统统计信息"""
    try:
        conn = get_db()
        cursor = conn.cursor()

        stats = {
            'total_captures': 0,
            'total_vectorized': 0,
            'db_size_mb': 0,
            'last_capture_ts': None
        }

        # 检查 captures 表
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='captures'")
        if cursor.fetchone():
            cursor.execute("SELECT COUNT(*) FROM captures")
            stats['total_captures'] = cursor.fetchone()[0]

            cursor.execute("SELECT MAX(ts) FROM captures")
            stats['last_capture_ts'] = cursor.fetchone()[0]

        # 检查 vector_status 表
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vector_status'")
        if cursor.fetchone():
            cursor.execute("SELECT COUNT(*) FROM vector_status WHERE vectorized = 1")
            stats['total_vectorized'] = cursor.fetchone()[0]

        # 数据库大小
        import os
        if os.path.exists(DB_PATH):
            stats['db_size_mb'] = round(os.path.getsize(DB_PATH) / 1024 / 1024, 2)

        conn.close()
        return jsonify(stats)

    except Exception as e:
        logger.error(f"获取统计信息失败: {e}")
        return jsonify({
            'total_captures': 0,
            'total_vectorized': 0,
            'db_size_mb': 0,
            'last_capture_ts': None
        })

@app.route('/api/knowledge', methods=['GET'])
def list_knowledge():
    """获取知识列表"""
    try:
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))
        category = request.args.get('category')
        verified_only = request.args.get('verified_only', 'false').lower() == 'true'

        conn = get_db()
        cursor = conn.cursor()

        # 构建查询
        query = "SELECT * FROM timelines WHERE 1=1"
        count_query = "SELECT COUNT(*) FROM timelines WHERE 1=1"
        params = []

        if category:
            query += " AND category = ?"
            count_query += " AND category = ?"
            params.append(category)

        if verified_only:
            query += " AND user_verified = 1"
            count_query += " AND user_verified = 1"

        # 查询总数
        cursor.execute(count_query, params)
        total = cursor.fetchone()[0]

        # 查询条目
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        cursor.execute(query, params + [limit, offset])
        rows = cursor.fetchall()

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

        conn.close()

        return jsonify({
            'entries': entries,
            'total': total,
            'limit': limit,
            'offset': offset
        })

    except Exception as e:
        logger.error(f"获取知识列表失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/knowledge/<int:entry_id>', methods=['GET'])
def get_knowledge(entry_id):
    """获取单个知识条目"""
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM timelines WHERE id = ?", (entry_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return jsonify({'error': '知识条目不存在'}), 404

        entry = dict(row)
        if entry['entities']:
            try:
                entry['entities'] = json.loads(entry['entities'])
            except:
                entry['entities'] = []
        else:
            entry['entities'] = []

        return jsonify(entry)

    except Exception as e:
        logger.error(f"获取知识条目失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/knowledge/<int:entry_id>', methods=['DELETE'])
def delete_knowledge(entry_id):
    """删除知识条目"""
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("DELETE FROM timelines WHERE id = ?", (entry_id,))
        conn.commit()

        if cursor.rowcount == 0:
            conn.close()
            return jsonify({'error': '知识条目不存在'}), 404

        conn.close()
        return jsonify({'success': True, 'message': '删除成功'})

    except Exception as e:
        logger.error(f"删除知识条目失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/knowledge/<int:entry_id>/verify', methods=['POST'])
def verify_knowledge(entry_id):
    """验证知识条目"""
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            "UPDATE timelines SET user_verified = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (entry_id,)
        )
        conn.commit()

        if cursor.rowcount == 0:
            conn.close()
            return jsonify({'error': '知识条目不存在'}), 404

        conn.close()
        return jsonify({'success': True, 'message': '验证成功'})

    except Exception as e:
        logger.error(f"验证知识条目失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/knowledge/search', methods=['GET'])
def search_knowledge():
    """搜索知识条目"""
    try:
        query = request.args.get('q', '')
        limit = int(request.args.get('limit', 20))

        if not query:
            return jsonify({'results': [], 'query': query, 'total': 0})

        conn = get_db()
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

        results = []
        for row in rows:
            entry = dict(row)
            if entry['entities']:
                try:
                    entry['entities'] = json.loads(entry['entities'])
                except:
                    entry['entities'] = []
            else:
                entry['entities'] = []
            results.append(entry)

        return jsonify({
            'results': results,
            'query': query,
            'total': len(results)
        })

    except Exception as e:
        logger.error(f"搜索知识条目失败: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    from runtime_endpoints import service_bind
    bind_host, bind_port = service_bind("model_api")
    logger.info(f"启动知识库 API 服务器，数据库: {DB_PATH}")
    logger.info("监听地址: http://%s:%s", bind_host, bind_port)
    app.run(host=bind_host, port=bind_port, debug=False)
