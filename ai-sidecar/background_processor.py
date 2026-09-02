"""
后台任务处理器 - 自动处理向量化和时间线提炼

定期扫描数据库中未处理的采集记录，执行：
1. 向量化（Embedding）
2. 时间线提炼（Timeline Extraction）
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib import error as urllib_error, request as urllib_request

from energy_policy import EnergyPolicy
from idle_compute.model_manager import _log_model_event
from knowledge.fragment_grouper import FragmentGrouper

logger = logging.getLogger(__name__)

_RAG_LOCK_FILE = "/tmp/memory-bread-rag.lock"
_PROCESS_LOCK_FILE = "/tmp/memory-bread-knowledge-extract.lock"
_DEFAULT_CORE_ENGINE_URL = "http://127.0.0.1:7070"
_DEFAULT_MODEL_API_URL = "http://127.0.0.1:7071"
_BAKE_RUN_ENDPOINT = "/api/bake/run"
_BAKE_QUEUE_STATUS_ENDPOINT = "/api/bake/queue-status"
_DATA_EXTRACTION_ENDPOINT = "/api/data/sources/extract"
_DATA_SOURCE_DISCOVERED_ENDPOINT = "/api/data/sources/discovered"
_INFERENCE_QUEUE_STATUS_ENDPOINT = "/api/inference/queue-status"
_CHARGING_CATCHUP_MAX_BATCH_SIZE = 100
_CHARGING_CATCHUP_SLEEP_SECS = 1
# 充电且已有可执行 bake 积压时，Core run 完成后快速续批。这里只缩短只读
# queue-status / run-in-progress 检查，不增加候选数量或模型推理次数。
_CHARGING_BACKLOG_BAKE_POLL_SECS = 2
# 单模型槽下不能只按“批次数”做公平调度：100 条 capture 可能拆成十几个模型
# 请求，一轮就独占十几分钟。双队列高压时把两侧都切成有界工作片，保证轮转。
_BAKE_BACKLOG_BURST_THRESHOLD = 100
# 双队列轮转同样使用正常批量 20 作为 bake 侧低水位；100 只保留为单边
# bake burst 的启动阈值，避免从 100 降到 99 就重新出现 capture 长批。
_BAKE_BACKLOG_PRESSURE_THRESHOLD = 20
# 20 是充电档正常单批上限。高压模式必须持续到 backlog 回到正常单批以内，
# 不能在 100 处形成阈值悬崖，否则一个大语义组就会让下一轮重新变成长批。
_CAPTURE_BACKLOG_PRESSURE_THRESHOLD = 20
_BAKE_BACKLOG_MAX_CONSECUTIVE_RUNS = 3
_DUAL_BACKLOG_MAX_CONSECUTIVE_BAKE_RUNS = 1
# 双队列高压时每轮轮到 6 组 capture / 6 条 bake：保持轮转公平，但把
# 消费速度翻倍，避免 quantum 过小导致创纪录流入日两侧都排不空。
_DUAL_BACKLOG_CAPTURE_GROUP_QUANTUM = 6
_DUAL_BACKLOG_BAKE_LIMIT = 6
# capture 优先规则：capture 严重积压且 bake 队列很小时，周期性 bake 让位，
# 把模型槽全部还给提炼（bake 候选本身来自 timeline 产出）；
# pending 降回恢复阈值以下后自动恢复 bake 周期触发。
_CAPTURE_PRIORITY_PENDING_THRESHOLD = 500
_CAPTURE_PRIORITY_BAKE_CEILING = 50
_CAPTURE_PRIORITY_RESUME_THRESHOLD = 100
# queue-status 连续报无进展时，继续触发 run 只会空转并抢占 capture 模型槽：
# 达到阈值后跳过触发、指数退避，且 no_progress run 无权让位 capture。
_NO_PROGRESS_BAKE_SKIP_THRESHOLD = 3
_NO_PROGRESS_BAKE_BACKOFF_BASE_SECS = 15
_NO_PROGRESS_BAKE_MAX_BACKOFF_SECS = 15 * 60
# 失败片段不能永久占据 ORDER BY ts 的队首。重试状态按 capture 持久化，
# 同一批次连续失败后改为确定性单条处理，不冒险混写时间线。
_TIMELINE_GROUP_SPLIT_AFTER_FAILURES = 2
_TIMELINE_RETRY_BASE_SECS = 60
_TIMELINE_RETRY_MAX_SECS = 15 * 60
# 电池模式仍保持单模型槽；双队列高压时只增大有界工作片并缩短
# 续批间隔，减少模型空闲窗口，不提高物理并发度。
_BATTERY_BACKLOG_BAKE_INTERVAL_SECS = 30
_BATTERY_BACKLOG_BAKE_LIMIT = 3
_SUBSTANTIVE_DOCUMENT_MIN_CHARS = 200
_ARTIFACT_VECTOR_CHECK_INTERVAL_SECS = 5 * 60
_VECTOR_CONSISTENCY_AUDIT_INTERVAL_SECS = 24 * 60 * 60
_DATA_EXTRACTION_INTERVAL_SECS = 5 * 60
# 推理运行时进程守卫巡检周期：保证全局最多 1 个 llama-server，孤儿及时回收。
_RUNTIME_GUARD_INTERVAL_SECS = 15 * 60

# 时间线增长上限：防止单条时间线无限吞噬不相关的采集记录（过度合并）。
# 历史上出现过单条时间线被反复合并到跨 5 天的脏数据（如 ID 2148），
# 根因是跨批合并与相似度去重均无规模上限，导致宽泛主题的时间线成为"垃圾桶"。
# 注意：短时高频采集（如同一 2 小时会话内上百条截图/采集）是正常时间线，
# 上限必须足够宽松，避免把同一时段的密集采集拆散到多条时间线浪费空间；
# 真正的异常特征是"跨天合并"，由跨度上限兜底。
_TIMELINE_MAX_MEMBER_COUNT = 500      # 成员 capture 数上限
_TIMELINE_MAX_OCCURRENCE_COUNT = 200  # 合并次数上限（初始=1，每次合并+1）
# 确定性丢弃（SKIP/无价值/质量不足）的 captures 挂到这条隐藏时间线上标记已处理，
# 避免 timeline_id 一直为 NULL 被 `_get_unprocessed_captures` 反复重提炼。
_LOW_VALUE_SINK_SUMMARY = "低价值采集回收"
_TIMELINE_MAX_SPAN_HOURS = 24.0       # 时间跨度上限（小时）

# 全局 embedding 信号量，限制并发数
_embedding_semaphore = asyncio.Semaphore(2)

_SELF_GENERATED_APP_KEYWORDS = (
    "memory-bread",
    "记忆面包",
)

_SELF_GENERATED_WINDOW_KEYWORDS = (
    "memory-bread",
    "memorybread preview",
    "记忆面包",
    "KnowledgePanel",
    "MonitorPanel",
    "RagPanel",
)


def _check_preempt_signal() -> bool:
    """检查是否收到抢占信号"""
    import os
    return os.path.exists("/tmp/memory-bread-preempt.signal")


def _is_self_generated_capture(app_name: Optional[str], window_title: Optional[str]) -> bool:
    app_lower = (app_name or "").lower()
    title_lower = (window_title or "").lower()
    return any(keyword in app_lower for keyword in _SELF_GENERATED_APP_KEYWORDS) or any(
        keyword.lower() in title_lower for keyword in _SELF_GENERATED_WINDOW_KEYWORDS
    )


class _LegacyKnowledgeExtractorAdapter:
    """兼容旧版提炼器的片段提炼适配器"""

    def __init__(self, extractor):
        self._extractor = extractor

    def _find_similar_knowledge(self, overview, db_conn, **kwargs):
        return None

    def extract_merged(self, captures: list[dict]) -> Optional[dict]:
        if not captures:
            return None

        merged_text_parts = []
        for capture in captures:
            text = (capture.get('ocr_text') or capture.get('ax_text') or '').strip()
            if text:
                merged_text_parts.append(text)

        if not merged_text_parts:
            return None

        first_capture = captures[0]
        last_capture = captures[-1]
        merged_capture = {
            'id': first_capture['id'],
            'app_name': last_capture.get('app_name') or first_capture.get('app_name') or '',
            'window_title': last_capture.get('window_title') or first_capture.get('window_title') or '',
            'timestamp': datetime.fromtimestamp(last_capture['ts'] / 1000).isoformat(),
            'ocr_text': '\n\n'.join(merged_text_parts),
        }

        extracted = self._extractor.extract_sync(merged_capture)
        if not extracted:
            return None

        start_time = first_capture['ts']
        end_time = last_capture['ts']
        duration_minutes = max(0, int((end_time - start_time) / 60000))

        segments = self._generate_segments(captures)

        return {
            'capture_ids': json.dumps([capture['id'] for capture in captures]),
            'summary': extracted.get('summary', ''),
            'overview': extracted.get('summary', ''),
            'details': '',
            'entities': extracted.get('entities', '[]'),
            'category': extracted.get('category', '其他'),
            'importance': extracted.get('importance', 2),
            'occurrence_count': 1,
            'start_time': start_time,
            'end_time': end_time,
            'duration_minutes': duration_minutes,
            'time_range_start': start_time,
            'time_range_end': end_time,
            'key_timestamps': json.dumps(segments),
            'frag_app_name': last_capture.get('app_name') or first_capture.get('app_name'),
            'frag_win_title': last_capture.get('window_title') or first_capture.get('window_title'),
            'observed_at': end_time,
            'event_time_start': None,
            'event_time_end': None,
            'history_view': False,
            'content_origin': 'other',
            'activity_type': 'other',
            'is_self_generated': False,
            'evidence_strength': 'low',
        }

    def _generate_segments(self, captures: list[dict]) -> list[dict]:
        """生成语义分段"""
        segments_map = {}
        for cap in captures:
            key = f"{cap.get('app_name')}|{cap.get('window_title', '')}"
            if key not in segments_map:
                segments_map[key] = {
                    'capture_ids': [],
                    'start_ts': cap['ts'],
                    'end_ts': cap['ts'],
                    'app_name': cap.get('app_name', ''),
                    'window_title': cap.get('window_title', ''),
                    'texts': []
                }
            seg = segments_map[key]
            seg['capture_ids'].append(cap['id'])
            seg['end_ts'] = cap['ts']
            text = (cap.get('ocr_text') or cap.get('ax_text') or '').strip()
            if text:
                seg['texts'].append(text[:100])

        segments = []
        for seg in segments_map.values():
            summary = ' '.join(seg['texts'])[:60]
            if not summary:
                summary = f"{seg['app_name']}活动"
            segments.append({
                'capture_ids': seg['capture_ids'],
                'start_ts': seg['start_ts'],
                'end_ts': seg['end_ts'],
                'summary': summary
            })
        return segments


class BackgroundProcessor:
    """后台任务处理器"""

    def __init__(
        self,
        db_path: str,
        interval: int = 30,  # 扫描间隔（秒）
        batch_size: int = 10,  # 每次处理的记录数
        energy_policy: Optional[EnergyPolicy] = None,
    ):
        self.db_path = db_path
        self.interval = interval
        self.batch_size = batch_size
        self.energy_policy = energy_policy or EnergyPolicy(db_path)
        self.running = False
        # Python 3.9 会在 Lock 构造时绑定当前事件循环；HTTP 路由会先同步构造
        # processor，再通过 asyncio.run() 执行，因此必须延迟到协程内创建。
        self._run_lock: Optional[asyncio.Lock] = None
        self._run_lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_energy_mode: Optional[str] = None
        self._last_data_extraction_at: float = 0.0
        self._consecutive_backlog_bake_runs = 0
        self._latest_bake_actionable_count = 0
        self._latest_pending_capture_count = 0
        # no_op 活锁防护：上一次触发的 run 若被 queue-status 证实零进展则累加，
        # 达到阈值后暂停周期性触发并指数退避。
        self._consecutive_no_progress_bake_runs = 0
        self._last_bake_no_progress_count = 0
        self._bake_trigger_watch = False
        self._bake_yield_to_capture = False
        self._timeline_retry_state_path = (
            Path(self.db_path).parent / "state" / "timeline-extraction-retries.json"
        )
        self._timeline_retry_state = self._load_timeline_retry_state()

        # 懒加载 workers
        self._embed_worker = None
        self._knowledge_extractor = None

        # 提炼状态：写入本地 JSON 文件供 core-engine 直读，避免 sidecar 重负载时 Flask 被 GIL 卡住误报 stalled
        self._extracting_lock = threading.Lock()
        self._extracting_groups: dict[int, dict] = {}  # group_id -> {captures, started_at}
        self._next_group_id = 0
        self._last_extraction_at_ms: Optional[int] = None
        self._status_file = Path.home() / ".memory-bread" / "state" / "extraction_status.json"
        self._status_file.parent.mkdir(parents=True, exist_ok=True)
        # 初始化时立即写一次，保证 core-engine 拿得到 running=true 信号
        self._touch_status_file()

    def _capture_and_extraction_enabled(self) -> bool:
        """读取与 Core Engine 共用的持久化运行开关；缺失或读取失败时保持默认开启。"""
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT value FROM user_preferences WHERE key = ? LIMIT 1",
                    ("runtime.capture_enabled",),
                ).fetchone()
                return row is None or str(row[0]).lower() != "false"
            finally:
                conn.close()
        except Exception as error:
            logger.warning("读取采集与提炼运行开关失败，保持默认开启: %s", error)
            return True

    def _build_status_snapshot(self) -> dict:
        """构造写入文件 / HTTP 响应共用的状态快照。调用前需持有 _extracting_lock。"""
        extracting_captures: list[dict] = []
        extracting_groups: list[dict] = []
        for gid, entry in self._extracting_groups.items():
            extracting_captures.extend(entry["captures"])
            extracting_groups.append({
                "group_id": gid,
                "started_at_ms": entry["started_at"],
                "captures": entry["captures"],
            })
        return {
            "running": True,
            "extracting_captures": extracting_captures,
            "extracting_groups": extracting_groups,
            "extracting_group_count": len(self._extracting_groups),
            "last_extraction_at_ms": self._last_extraction_at_ms,
            "updated_at_ms": int(time.time() * 1000),
        }

    def _write_status_file(self) -> None:
        """原子写入状态文件（写 .tmp 后 rename）。调用前需持有 _extracting_lock。"""
        try:
            snapshot = self._build_status_snapshot()
            tmp = self._status_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False))
            tmp.replace(self._status_file)
        except Exception as e:
            logger.warning("写入 extraction_status.json 失败: %s", e)

    def _touch_status_file(self) -> None:
        """刷新提炼状态心跳，避免空转等待时被监控页误判为未启动。"""
        with self._extracting_lock:
            self._write_status_file()

    def _mark_group_extracting(self, group: list[dict]) -> int:
        """登记一组 captures 进入提炼，返回 group_id 用于结束时移除。"""
        with self._extracting_lock:
            gid = self._next_group_id
            self._next_group_id += 1
            self._extracting_groups[gid] = {
                "captures": [
                    {
                        "id": int(c.get("id") or 0),
                        "ts": int(c.get("ts") or 0),
                        "app_name": c.get("app_name") or "",
                        "win_title": c.get("window_title") or c.get("win_title") or "",
                    }
                    for c in group
                ],
                "started_at": int(time.time() * 1000),
            }
            self._write_status_file()
            return gid

    def _unmark_group_extracting(self, group_id: int, succeeded: bool) -> None:
        with self._extracting_lock:
            self._extracting_groups.pop(group_id, None)
            if succeeded:
                self._last_extraction_at_ms = int(time.time() * 1000)
            self._write_status_file()

    def get_extraction_status(self) -> dict:
        """供外部 HTTP 端点读取的实时提炼状态快照（保留 HTTP 接口作为回退）。"""
        with self._extracting_lock:
            return self._build_status_snapshot()

    def _get_embed_worker(self):
        """懒加载 EmbedWorker，使用全局共享 EmbeddingModel"""
        if self._embed_worker is None:
            from embedding.worker import EmbedWorker
            from model_registry_global import get_shared_embedding
            start_ms = int(time.time() * 1000)
            _log_model_event("load_start", "embedding", "Sidecar Embedding · Shared", memory_mb=650)
            # 使用全局共享 EmbeddingModel，避免与 RAG pipeline 重复加载
            model = get_shared_embedding()
            self._embed_worker = EmbedWorker(model=model)
            _log_model_event(
                "load_done",
                "embedding",
                "Sidecar Embedding · Shared",
                duration_ms=int(time.time() * 1000) - start_ms,
                memory_mb=650,
            )
            logger.info("EmbedWorker 已初始化（后台任务，使用共享 Embedding）")
        return self._embed_worker

    def _read_user_identity(self) -> str:
        """从 user_preferences 表读取用户身份关键词"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT value FROM user_preferences WHERE key = 'user.identity_keywords' LIMIT 1"
            )
            row = cursor.fetchone()
            conn.close()
            return (row[0] or "").strip() if row else ""
        except Exception as e:
            logger.warning("读取用户身份偏好失败: %s", e)
            return ""

    def _get_knowledge_extractor(self):
        """懒加载 KnowledgeExtractor，仅允许 V2 模型提炼"""
        current_identity = self._read_user_identity()
        if self._knowledge_extractor is None or getattr(self, '_cached_identity', None) != current_identity:
            logger.info("开始初始化 KnowledgeExtractor（后台任务，identity=%r）", current_identity)
            try:
                from knowledge.extractor_v2 import KnowledgeExtractorV2
                from model_registry_global import get_active_ollama_model

                try:
                    embed_worker = self._get_embed_worker()
                    embed_model = embed_worker._model if embed_worker else None
                except Exception as embed_exc:
                    embed_model = None
                    logger.warning(
                        "向量模型未就绪，时间线提炼将降级运行（禁用语义去重/向量化增强）: %s",
                        embed_exc,
                    )

                # 使用全局统一的 Ollama 模型名，确保与 RAG 查询使用同一模型
                ollama_model = get_active_ollama_model()

                self._knowledge_extractor = KnowledgeExtractorV2(
                    model=ollama_model,
                    embedding_model=embed_model,
                    user_identity=current_identity,
                )
                logger.info(
                    "KnowledgeExtractor V2 已初始化（后台任务，模型提炼模式，model=%s, embedding=%s）",
                    ollama_model,
                    "enabled" if embed_model else "disabled",
                )
            except Exception as exc:
                logger.error("KnowledgeExtractor V2 初始化失败: %s", exc)
                raise RuntimeError(f"KnowledgeExtractor V2 初始化失败: {exc}") from exc

            self._cached_identity = current_identity
        return self._knowledge_extractor

    def _load_timeline_retry_state(self) -> dict:
        try:
            payload = json.loads(self._timeline_retry_state_path.read_text())
            if not isinstance(payload, dict):
                return {}
            return {
                int(capture_id): state
                for capture_id, state in payload.items()
                if isinstance(state, dict)
            }
        except (OSError, ValueError, TypeError):
            return {}

    def _save_timeline_retry_state(self) -> None:
        try:
            self._timeline_retry_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._timeline_retry_state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._timeline_retry_state, ensure_ascii=False))
            tmp.replace(self._timeline_retry_state_path)
        except OSError as exc:
            logger.warning("保存时间线提炼重试状态失败: %s", exc)

    def _record_timeline_extraction_failure(self, capture_ids: list[int]) -> int:
        now_ms = int(time.time() * 1000)
        max_failures = 0
        for capture_id in capture_ids:
            state = self._timeline_retry_state.get(int(capture_id), {})
            failure_count = int(state.get("failure_count") or 0) + 1
            retry_secs = min(
                _TIMELINE_RETRY_BASE_SECS * (2 ** max(0, failure_count - 1)),
                _TIMELINE_RETRY_MAX_SECS,
            )
            self._timeline_retry_state[int(capture_id)] = {
                "failure_count": failure_count,
                "next_retry_at_ms": now_ms + retry_secs * 1000,
                "updated_at_ms": now_ms,
            }
            max_failures = max(max_failures, failure_count)
        self._save_timeline_retry_state()
        return max_failures

    def _clear_timeline_extraction_failures(self, capture_ids: list[int]) -> None:
        changed = False
        for capture_id in capture_ids:
            if self._timeline_retry_state.pop(int(capture_id), None) is not None:
                changed = True
        if changed:
            self._save_timeline_retry_state()

    def _get_unprocessed_captures(self, conn: sqlite3.Connection, limit: int):
        """获取未处理的采集记录（按时间升序，用于分组）"""
        cursor = conn.cursor()
        # timeline_id IS NULL 表示尚未被合并进任何时间线片段
        # 自生成 app/窗口在 SQL 层直接过滤，避免 LIMIT 被自生成记录占满导致真实内容取不到
        app_kws = tuple(k.lower() for k in _SELF_GENERATED_APP_KEYWORDS)
        win_kws = tuple(k.lower() for k in _SELF_GENERATED_WINDOW_KEYWORDS)
        app_not_like = " AND ".join(
            f"LOWER(COALESCE(c.app_name, '')) NOT LIKE '%{k}%'" for k in app_kws
        )
        win_not_like = " AND ".join(
            f"LOWER(COALESCE(c.win_title, '')) NOT LIKE '%{k}%'" for k in win_kws
        )
        now_ms = int(time.time() * 1000)
        cooling_ids = [
            int(capture_id)
            for capture_id, state in self._timeline_retry_state.items()
            if int(state.get("next_retry_at_ms") or 0) > now_ms
        ]
        cooldown_sql = ""
        params = []
        if cooling_ids:
            placeholders = ",".join("?" for _ in cooling_ids)
            cooldown_sql = f" AND c.id NOT IN ({placeholders})"
            params.extend(cooling_ids)
        params.append(limit)
        cursor.execute(f"""
            SELECT c.id, c.ts, c.app_name, c.win_title,
                   c.ocr_text, c.ax_text, c.input_text, c.audio_text, c.url,
                   c.webpage_title
            FROM captures c
            WHERE (COALESCE(c.ocr_text, '') != ''
               OR COALESCE(c.ax_text, '') != ''
               OR COALESCE(c.input_text, '') != ''
               OR COALESCE(c.audio_text, '') != '')
              AND c.timeline_id IS NULL
              AND c.is_sensitive = 0
              AND ({app_not_like})
              AND ({win_not_like})
              {cooldown_sql}
            ORDER BY c.ts ASC
            LIMIT ?
        """, params)
        rows = cursor.fetchall()
        return [
            {
                'id': r[0], 'ts': r[1], 'app_name': r[2],
                'window_title': r[3], 'ocr_text': r[4], 'ax_text': r[5],
                'input_text': r[6], 'audio_text': r[7], 'url': r[8],
                'webpage_title': r[9],
            }
            for r in rows
        ]

    def _count_unprocessed_captures(self) -> int:
        """统计完整待提炼 backlog；限速只改变消费速度，不改变 capture 状态。"""
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                app_kws = tuple(k.lower() for k in _SELF_GENERATED_APP_KEYWORDS)
                win_kws = tuple(k.lower() for k in _SELF_GENERATED_WINDOW_KEYWORDS)
                app_not_like = " AND ".join(
                    f"LOWER(COALESCE(c.app_name, '')) NOT LIKE '%{k}%'" for k in app_kws
                )
                win_not_like = " AND ".join(
                    f"LOWER(COALESCE(c.win_title, '')) NOT LIKE '%{k}%'" for k in win_kws
                )
                row = conn.execute(f"""
                    SELECT COUNT(*)
                    FROM captures c
                    WHERE (COALESCE(c.ocr_text, '') != ''
                       OR COALESCE(c.ax_text, '') != ''
                       OR COALESCE(c.input_text, '') != ''
                       OR COALESCE(c.audio_text, '') != '')
                      AND c.timeline_id IS NULL
                      AND c.is_sensitive = 0
                      AND ({app_not_like})
                      AND ({win_not_like})
                """).fetchone()
                return int(row[0] or 0) if row else 0
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("统计待提炼 capture backlog 失败: %s", exc)
            return 0

    @staticmethod
    def _timeline_batch_limit(profile, pending_count: int) -> int:
        base_limit = max(1, int(profile.timeline_batch_size))
        if profile.mode not in {"charging", "unrestricted"}:
            return base_limit
        if pending_count <= base_limit:
            return base_limit
        return min(max(base_limit, int(pending_count)), _CHARGING_CATCHUP_MAX_BATCH_SIZE)

    @staticmethod
    def _is_dual_backlog_pressure(
        pending_capture_count: int,
        actionable_bake_count: int,
    ) -> bool:
        return (
            int(pending_capture_count) >= _CAPTURE_BACKLOG_PRESSURE_THRESHOLD
            and int(actionable_bake_count) >= _BAKE_BACKLOG_PRESSURE_THRESHOLD
        )

    @classmethod
    def _capture_group_quantum(
        cls,
        pending_capture_count: int,
        actionable_bake_count: int,
    ) -> Optional[int]:
        if cls._is_dual_backlog_pressure(
            pending_capture_count,
            actionable_bake_count,
        ):
            return _DUAL_BACKLOG_CAPTURE_GROUP_QUANTUM
        return None

    @staticmethod
    def _scheduled_bake_limit(profile, pending_capture_count: int) -> int:
        limit = max(1, int(profile.bake_limit))
        if int(pending_capture_count) >= _CAPTURE_BACKLOG_PRESSURE_THRESHOLD:
            if profile.mode == "battery":
                return min(_BATTERY_BACKLOG_BAKE_LIMIT, _DUAL_BACKLOG_BAKE_LIMIT)
            return min(limit, _DUAL_BACKLOG_BAKE_LIMIT)
        return limit

    @classmethod
    def _bake_burst_run_limit(
        cls,
        pending_capture_count: int,
        actionable_bake_count: int,
    ) -> int:
        if cls._is_dual_backlog_pressure(
            pending_capture_count,
            actionable_bake_count,
        ):
            return _DUAL_BACKLOG_MAX_CONSECUTIVE_BAKE_RUNS
        return _BAKE_BACKLOG_MAX_CONSECUTIVE_RUNS

    @classmethod
    def _scheduled_bake_interval_secs(
        cls,
        profile,
        pending_capture_count: int,
        actionable_bake_count: int,
    ) -> float:
        interval_secs = max(1.0, float(profile.bake_interval_secs))
        if (
            profile.mode in {"charging", "unrestricted"}
            and int(actionable_bake_count) > 0
        ):
            return min(interval_secs, float(_CHARGING_BACKLOG_BAKE_POLL_SECS))
        if (
            profile.mode == "battery"
            and cls._is_dual_backlog_pressure(
                pending_capture_count,
                actionable_bake_count,
            )
        ):
            return min(interval_secs, float(_BATTERY_BACKLOG_BAKE_INTERVAL_SECS))
        return interval_secs

    @staticmethod
    def _should_continue_charging_catchup(
        profile,
        pending_before: int,
        pending_after: int,
        batch_result: dict,
    ) -> bool:
        """仅在本轮确实消费了片段时加速追赶，避免等待静默期间每秒空转。"""
        return (
            profile.mode in {"charging", "unrestricted"}
            and pending_before > profile.timeline_batch_size
            and pending_after > profile.timeline_batch_size
            and int(batch_result.get("processed_count", 0)) > 0
        )

    # 跨批上下文回溯条数：用于判断新 batch 开头是否与前一批末尾属于同一件事
    _CROSS_BATCH_CONTEXT_N = 5

    def _get_recent_processed_captures(self, conn: sqlite3.Connection, before_ts: int) -> list[dict]:
        """查出 before_ts 之前最近已处理的 N 条 captures，用于跨批语义判断。
        每条附带所属 timeline_id，供合并时使用。"""
        app_kws = tuple(k.lower() for k in _SELF_GENERATED_APP_KEYWORDS)
        win_kws = tuple(k.lower() for k in _SELF_GENERATED_WINDOW_KEYWORDS)
        app_not_like = " AND ".join(f"LOWER(COALESCE(c.app_name, '')) NOT LIKE '%{k}%'" for k in app_kws)
        win_not_like = " AND ".join(f"LOWER(COALESCE(c.win_title, '')) NOT LIKE '%{k}%'" for k in win_kws)
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT c.id, c.ts, c.app_name, c.win_title,
                   c.ocr_text, c.ax_text, c.input_text, c.audio_text,
                   c.timeline_id, c.url, c.webpage_title
            FROM captures c
            WHERE c.timeline_id IS NOT NULL
              AND c.ts < ?
              AND c.is_sensitive = 0
              AND ({app_not_like})
              AND ({win_not_like})
            ORDER BY c.ts DESC
            LIMIT ?
        """, (before_ts, self._CROSS_BATCH_CONTEXT_N))
        rows = cursor.fetchall()
        # 返回时间升序（DESC 取出后反转）
        return [
            {
                'id': r[0], 'ts': r[1], 'app_name': r[2],
                'window_title': r[3], 'ocr_text': r[4], 'ax_text': r[5],
                'input_text': r[6], 'audio_text': r[7],
                'timeline_id': r[8], 'url': r[9], 'webpage_title': r[10],
                '_is_context': True,  # 标记为前缀上下文
            }
            for r in reversed(rows)
        ]

    def _get_fragment_grouper(self):
        """懒加载 FragmentGrouper"""
        if not hasattr(self, '_fragment_grouper'):
            from knowledge.fragment_grouper import FragmentGrouper
            # 复用已有的 embedding model（如果已初始化）
            embed_model = self._embed_worker._model if self._embed_worker else None
            self._fragment_grouper = FragmentGrouper(embedding_model=embed_model)
            logger.info("FragmentGrouper 已初始化")
        return self._fragment_grouper

    def _acquire_rag_priority_lock(self):
        fd = open(_RAG_LOCK_FILE, "w")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _acquire_process_file_lock(self):
        fd = open(_PROCESS_LOCK_FILE, "w")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    @staticmethod
    def _release_rag_priority_lock(fd) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()

    @staticmethod
    def _release_process_file_lock(fd) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()

    def _build_batch_summary(self, fetched_count: int, processed: int) -> dict:
        return {
            "fetched_count": fetched_count,
            "processed_count": processed,
            "remaining_estimate": max(fetched_count - processed, 0),
        }

    def _build_skipped_summary(self, fetched_count: int, reason: str) -> dict:
        return {
            "fetched_count": fetched_count,
            "processed_count": 0,
            "remaining_estimate": fetched_count,
            "reason": reason,
        }

    def _build_idle_summary(self) -> dict:
        return {
            "fetched_count": 0,
            "processed_count": 0,
            "remaining_estimate": 0,
            "reason": "no_unprocessed_captures",
        }

    @staticmethod
    def _get_core_engine_url() -> str:
        return os.getenv("CORE_ENGINE_URL") or os.getenv("MEMORY_BREAD_CORE_URL") or _DEFAULT_CORE_ENGINE_URL

    @staticmethod
    def _get_model_api_url() -> str:
        return os.getenv("MODEL_API_URL") or _DEFAULT_MODEL_API_URL

    def _all_inference_queues_idle(self) -> bool:
        """同时确认本进程 P1 队列和 7071 实际模型队列均为空。

        BackgroundProcessor 通常运行在 main.py，而 P0/P2 运行在独立的
        model_api_server.py 进程。只检查进程内单例会把远端正在推理误判为空闲。
        状态接口不可用时 fail-closed，留到下一轮再试。
        """
        from inference_queue import get_global_queue

        if not get_global_queue().is_idle():
            return False

        url = (
            f"{self._get_model_api_url().rstrip('/')}"
            f"{_INFERENCE_QUEUE_STATUS_ENDPOINT}"
        )
        try:
            request = urllib_request.Request(url, method="GET")
            with urllib_request.urlopen(request, timeout=3) as response:
                body = response.read().decode("utf-8") if response else ""
                data = json.loads(body) if body else {}
            return data.get("status") == "ok" and data.get("idle") is True
        except Exception as exc:
            logger.debug("读取跨进程推理队列状态失败，按忙碌处理: %s", exc)
            return False

    def _get_bake_queue_status(self) -> Optional[dict]:
        """读取 Core 统一计算的 bake 队列快照；不可用时 fail-closed。"""
        url = f"{self._get_core_engine_url().rstrip('/')}{_BAKE_QUEUE_STATUS_ENDPOINT}"
        try:
            request = urllib_request.Request(url, method="GET")
            with urllib_request.urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8") if response else ""
                data = json.loads(body) if body else {}
            if not isinstance(data, dict):
                return None
            if not data.get("capture_enabled"):
                return data
            if "actionable_count" not in data:
                return None
            return data
        except Exception as exc:
            logger.debug("读取 Core bake 队列状态失败，按不可调度处理: %s", exc)
            return None

    async def _trigger_unified_bake_pipeline(
        self,
        processed_count: int,
        force: bool = False,
        *,
        limit_override: Optional[int] = None,
        max_concurrency: int = 3,
    ) -> dict:
        if processed_count <= 0 and not force:
            return {
                "triggered": False,
                "reason": "no_new_knowledge",
            }
        queue_status = await asyncio.to_thread(self._get_bake_queue_status)
        if queue_status is None:
            return {
                "triggered": False,
                "reason": "queue_status_unavailable",
                "retry_after_ms": 30_000,
            }
        if not queue_status.get("capture_enabled"):
            return {
                "triggered": False,
                "reason": "capture_disabled",
                "retry_after_ms": int(queue_status.get("recommended_retry_after_ms") or 300_000),
                "actionable_count": int(queue_status.get("actionable_count") or 0),
            }
        if int(queue_status.get("actionable_count") or 0) <= 0:
            return {
                "triggered": False,
                "reason": "no_actionable_bake_candidate",
                "retry_after_ms": int(queue_status.get("recommended_retry_after_ms") or 300_000),
                "actionable_count": 0,
                "queue": queue_status,
            }
        # recommended_retry_after_ms 只描述“当前没有可执行候选”时的下一次检查时间。
        # recent_no_progress_count 也会让 Core 给出较长建议值，但只要 actionable_count
        # 大于 0，就必须实际发起一次 run；否则每次检查都会再次读到相同建议值，形成
        # 永久自我延后，监控只能持续报 scheduler_mismatch。
        # core 同时只允许一个 bake run；已有 run 在跑时不再发起 HTTP 触发，
        # 避免高频空转请求。陈旧 running 行由监控轮询收敛，不会永久阻塞。
        if await asyncio.to_thread(self._has_active_bake_run):
            return {
                "triggered": False,
                "reason": "run_in_progress",
                "actionable_count": int(queue_status.get("actionable_count") or 0),
            }
        if not await asyncio.to_thread(self._all_inference_queues_idle):
            return {
                "triggered": False,
                "reason": "inference_busy",
                "actionable_count": int(queue_status.get("actionable_count") or 0),
            }

        url = f"{self._get_core_engine_url().rstrip('/')}{_BAKE_RUN_ENDPOINT}"
        limit = (
            max(1, int(limit_override))
            if limit_override is not None
            else max(processed_count, 20)
        )
        payload = json.dumps({
            "trigger_reason": "knowledge_background",
            "limit": limit,
            "max_concurrency": max(1, min(int(max_concurrency), 3)),
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        request = urllib_request.Request(url, data=payload, headers=headers, method="POST")

        def _send() -> dict:
            try:
                with urllib_request.urlopen(request, timeout=15) as response:
                    body = response.read().decode("utf-8") if response else ""
                    data = json.loads(body) if body else {}
                    status = data.get("status")
                    run_id = data.get("id")
                    accepted = status == "accepted" and run_id is not None
                    return {
                        "triggered": accepted,
                        "status": status,
                        "run_id": run_id,
                        "auto_created_count": data.get("auto_created_count"),
                        "candidate_count": data.get("candidate_count"),
                        "discarded_count": data.get("discarded_count"),
                        "retry_after_ms": data.get("retry_after_ms"),
                        "reason": None if accepted else (data.get("reason") or f"status={status}"),
                    }
            except urllib_error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
                logger.warning("统一 bake pipeline 触发失败: status=%s body=%s", exc.code, detail)
            except Exception as exc:
                logger.warning("统一 bake pipeline 触发异常: %s", exc)
            return {
                "triggered": False,
                "reason": "request_failed",
            }

        result = await asyncio.to_thread(_send)
        result["actionable_count"] = int(queue_status.get("actionable_count") or 0)
        result["recent_no_progress_count"] = int(
            queue_status.get("recent_no_progress_count") or 0
        )
        # accepted 分支 Core 不回退避建议；用触发前读到的 queue 快照补齐，
        # 让周期检查能按 no_progress 指数退避，不再固定短间隔重复触发。
        if not result.get("retry_after_ms"):
            result["retry_after_ms"] = int(
                queue_status.get("recommended_retry_after_ms") or 0
            )
        if result.get("triggered"):
            logger.info(
                "统一 bake pipeline 已触发: run_id=%s status=%s auto=%s candidate=%s discarded=%s",
                result.get("run_id"),
                result.get("status"),
                result.get("auto_created_count"),
                result.get("candidate_count"),
                result.get("discarded_count"),
            )
        return result

    async def _trigger_data_extraction(self, limit: int = 200) -> dict:
        """让 Core 以 timeline 为入口识别数据源和工作数据记忆。

        Core 会在本机 SQLite 中读取 timeline 及其关联 captures；这里只传数量上限，
        不传正文或 URL，原始数据始终留在共享本机数据库。尝试开始即进入冷却，
        避免 Core 超时或已有任务运行时在后台循环中立即重试形成请求风暴。
        """
        self._last_data_extraction_at = time.monotonic()
        url = f"{self._get_core_engine_url().rstrip('/')}{_DATA_EXTRACTION_ENDPOINT}"
        payload = json.dumps({"limit": max(1, min(int(limit), 5000))}).encode("utf-8")
        request = urllib_request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _send() -> dict:
            try:
                with urllib_request.urlopen(request, timeout=15) as response:
                    body = response.read().decode("utf-8") if response else ""
                    data = json.loads(body) if body else {}
                return {
                    "triggered": True,
                    "scanned_count": int(data.get("scanned_count") or 0),
                    "source_created_count": int(data.get("source_created_count") or 0),
                    "source_updated_count": int(data.get("source_updated_count") or 0),
                    "snapshot_created_count": int(data.get("snapshot_created_count") or 0),
                }
            except Exception as exc:
                logger.warning("数据记忆识别触发失败: %s", type(exc).__name__)
                return {"triggered": False, "reason": "request_failed"}

        result = await asyncio.to_thread(_send)
        if result.get("triggered"):
            if result.get("source_created_count") or result.get("snapshot_created_count"):
                logger.info(
                    "数据记忆识别完成: scanned=%s sources=%s snapshots=%s",
                    result.get("scanned_count"),
                    result.get("source_created_count"),
                    result.get("snapshot_created_count"),
                )
        return result

    def _process_batch_sync(self, limit_override: Optional[int] = None, force_finalize_tail: bool = False) -> dict:
        """同步执行一轮批处理，便于后台循环与手动触发复用。"""
        conn = sqlite3.connect(self.db_path)
        try:
            limit = limit_override or self.batch_size
            captures = self._get_unprocessed_captures(conn, limit)
            # 跨批上下文：取当前 batch 第一条之前最近已处理的若干条，用于语义合并判断
            context_prefix = (
                self._get_recent_processed_captures(conn, captures[0]['ts'])
                if captures else []
            )
        finally:
            conn.close()

        if not captures:
            return self._build_idle_summary()

        now_ms = int(time.time() * 1000)
        first_capture = captures[0]
        last_capture = captures[-1]

        logger.info(
            "📦 发现 %s 条待处理 captures，开始语义分组 (first_id=%s, last_id=%s, "
            "context_prefix=%s force_finalize_tail=%s)",
            len(captures), first_capture['id'], last_capture['id'],
            len(context_prefix), force_finalize_tail,
        )

        if len(captures) < FragmentGrouper.MIN_GROUP_WAIT:
            should_finalize, reason = self._should_finalize_last_group(captures, now_ms, len(captures))
            if force_finalize_tail and captures:
                should_finalize = True
                reason = 'force_finalize_tail'
            idle_minutes = self._group_idle_minutes(captures, now_ms)
            logger.info(
                "片段候选不足最小数量: count=%s idle=%.1fmin finalize=%s reason=%s",
                len(captures), idle_minutes, should_finalize, reason,
            )
            if not should_finalize:
                return self._build_skipped_summary(len(captures), reason)
            groups = [captures]
            # 跨批上下文合并信号（小 batch 情况）
            merge_first_group_into: Optional[int] = self._detect_cross_batch_merge(
                context_prefix, captures
            )
        else:
            grouper = self._get_fragment_grouper()
            # 把前缀上下文拼在 batch 前面，让 grouper 感知跨批连续性
            combined = context_prefix + captures
            combined_groups = grouper.group_captures(combined)
            # 分离：若第一组包含了前缀 captures，则其中的新 captures 应合并到前缀的 timeline
            groups, merge_first_group_into = self._split_context_from_groups(
                combined_groups, context_prefix, captures
            )

        groups_to_process = groups[:-1] if len(groups) > 1 else []
        last_group = groups[-1] if groups else []
        finalize_last_group = False
        finalize_reason = 'no_groups'
        last_group_idle = self._group_idle_minutes(last_group, now_ms) if last_group else 0.0

        if last_group:
            finalize_last_group, finalize_reason = self._should_finalize_last_group(
                last_group, now_ms, len(captures)
            )
            if force_finalize_tail:
                finalize_last_group = True
                finalize_reason = 'force_finalize_tail'
            if finalize_last_group and (not groups_to_process or groups_to_process[-1] is not last_group):
                groups_to_process.append(last_group)

        logger.info(
            "分组结果: captures=%s groups=%s process_now=%s last_group_size=%s "
            "last_group_idle=%.1fmin finalize_last=%s reason=%s merge_into_timeline=%s",
            len(captures), len(groups), len(groups_to_process), len(last_group),
            last_group_idle, finalize_last_group, finalize_reason, merge_first_group_into,
        )

        if not groups_to_process:
            logger.info("跳过本轮: captures=%s reason=%s last_group_idle=%.1fmin", len(captures), finalize_reason, last_group_idle)
            return self._build_skipped_summary(len(captures), finalize_reason)

        return {
            "captures": captures,
            "groups_to_process": groups_to_process,
            "fetched_count": len(captures),
            "finalize_reason": finalize_reason,
            # 若第一组需要合并到已有 timeline，传递 timeline_id
            "merge_first_group_into": merge_first_group_into,
        }

    def _detect_cross_batch_merge(
        self, context_prefix: list[dict], new_captures: list[dict]
    ) -> Optional[int]:
        """小 batch 情况下，判断新 captures 是否与前缀属于同一件事。
        返回需要合并进的 timeline_id，或 None。"""
        if not context_prefix or not new_captures:
            return None
        grouper = self._get_fragment_grouper()
        combined = context_prefix + new_captures
        groups = grouper.group_captures(combined)
        if not groups:
            return None
        first_group = groups[0]
        first_ids = {c['id'] for c in first_group}
        has_prefix = any(c['id'] in first_ids for c in context_prefix)
        has_new = any(c['id'] in first_ids for c in new_captures)
        if has_prefix and has_new:
            # 取前缀中的最后一个 timeline_id 作为合并目标
            for c in reversed(context_prefix):
                if c.get('timeline_id') and c['id'] in first_ids:
                    return c['timeline_id']
        return None

    def _split_context_from_groups(
        self,
        combined_groups: list[list[dict]],
        context_prefix: list[dict],
        new_captures: list[dict],
    ) -> tuple[list[list[dict]], Optional[int]]:
        """把 grouper 对 (context_prefix + new_captures) 的分组结果拆开：
        - 返回只含新 captures 的分组列表
        - 若第一组混入了前缀 captures，提取 merge_into timeline_id
        """
        if not combined_groups:
            return [new_captures], None

        prefix_ids = {c['id'] for c in context_prefix}
        new_ids = {c['id'] for c in new_captures}
        merge_into: Optional[int] = None
        result_groups: list[list[dict]] = []

        for i, group in enumerate(combined_groups):
            group_ids = {c['id'] for c in group}
            has_prefix = bool(group_ids & prefix_ids)
            has_new = bool(group_ids & new_ids)

            if has_prefix and has_new and i == 0:
                # 第一组跨越了前缀和新 captures：新 captures 部分需合并到前缀 timeline
                new_part = [c for c in group if c['id'] in new_ids]
                if new_part:
                    # 找前缀中对应的 timeline_id
                    for c in reversed(context_prefix):
                        if c.get('timeline_id') and c['id'] in group_ids:
                            merge_into = c['timeline_id']
                            break
                    result_groups.append(new_part)
            elif has_new:
                result_groups.append([c for c in group if c['id'] in new_ids])
            # 纯前缀的组直接丢弃

        # 若分组结果为空（所有新 captures 都被归入前缀组且已提取），保留原始 new_captures
        if not result_groups and new_captures:
            result_groups = [new_captures]

        return result_groups, merge_into

    async def _run_batch(
        self,
        limit_override: Optional[int] = None,
        force_finalize_tail: bool = False,
        *,
        max_groups: Optional[int] = None,
        trigger_bake: bool = True,
        bake_limit: Optional[int] = None,
        bake_concurrency: int = 3,
    ) -> dict:
        batch = await asyncio.to_thread(self._process_batch_sync, limit_override, force_finalize_tail)
        groups_to_process = batch.get('groups_to_process')
        if not groups_to_process:
            return batch
        if max_groups is not None and len(groups_to_process) > max(1, int(max_groups)):
            scheduled_count = max(1, int(max_groups))
            logger.info(
                "双队列高压工作片: 本轮处理 %s/%s 个 capture 语义组，其余留待轮转",
                scheduled_count,
                len(groups_to_process),
            )
            groups_to_process = groups_to_process[:scheduled_count]

        # 内存压力检查：内存不足时跳过提炼，避免系统卡死
        from model_registry_global import check_memory_pressure
        pressure = check_memory_pressure()
        if pressure == "critical":
            logger.warning("内存压力 Critical，跳过本轮时间线提炼，避免系统卡死")
            return self._build_skipped_summary(
                batch.get('fetched_count', 0),
                'memory_pressure_critical',
            )

        # 只持有 process_lock（防止多实例并发提炼），不持有 rag_priority_lock。
        # rag_priority_lock 是给 model_api_server（RAG 查询）与 background_processor
        # 互相谦让用的：model_api_server 持锁时 extractor 探测到后跳过本轮；
        # 但如果 background_processor 自己也持有 rag_lock 再调用 extractor，
        # macOS flock 同进程不可重入（errno=35），extractor 内的 _rag_is_active()
        # 会永远返回 True，导致提炼永远被跳过。
        #
        # 现在的并发保护机制：
        # 1. InferenceQueue 统一调度所有 LLM 推理（P0 RAG > P1 提炼 > P2 bake）
        # 2. P0 任务执行时自动持有 RAG 文件锁，extractor_v2._rag_is_active() 可检测
        # 3. 本模块不再需要手动操作 RAG 锁
        process_lock_fd = await asyncio.to_thread(self._acquire_process_file_lock)
        try:
            processed = 0
            merge_into = batch.get('merge_first_group_into')
            for i, group in enumerate(groups_to_process):
                handled = False
                # 第一组且有跨批合并信号时，直接追加到已有 timeline
                # —— 但需先过文档边界守卫：不能把不同文档/非文档内容并进文档 timeline，
                #    也不能把文档内容并进非文档 timeline，否则"一份文档独占 timeline"被破坏。
                if i == 0 and merge_into and not self._doc_compatible_with_timeline(group, merge_into):
                    logger.info(
                        "🚫 跨批合并被文档边界拦截: group 与 timeline_id=%d 文档不一致，改为独立提炼",
                        merge_into,
                    )
                    merge_into = None
                if i == 0 and merge_into:
                    if await self._process_capture_group(
                        group,
                        merge_timeline_id=merge_into,
                    ):
                        handled = True
                        processed += 1
                        logger.info(
                            "🔀 跨批提炼并合并: %d 条 captures → timeline_id=%d",
                            len(group),
                            merge_into,
                        )
                    else:
                        # 指定时间线可能已被删除；退化为正常提炼。
                        if await self._process_capture_group(group):
                            handled = True
                            processed += 1
                else:
                    if await self._process_capture_group(group):
                        handled = True
                        processed += 1
                if handled:
                    asyncio.create_task(self._process_vectorization_batch(group))
                await asyncio.sleep(0.5)
        finally:
            await asyncio.to_thread(self._release_process_file_lock, process_lock_fd)

        fetched_count = int(batch.get('fetched_count', 0))
        logger.info("批处理完成: processed=%s fetched=%s", processed, fetched_count)
        summary = self._build_batch_summary(fetched_count, processed)
        if trigger_bake:
            summary['bake_trigger'] = await self._trigger_unified_bake_pipeline(
                processed,
                limit_override=bake_limit,
                max_concurrency=bake_concurrency,
            )
        else:
            summary['bake_trigger'] = {
                "triggered": False,
                "reason": "scheduled_separately",
            }
        return summary

    async def run_once(
        self,
        limit_override: Optional[int] = None,
        force_finalize_tail: bool = False,
        *,
        max_groups: Optional[int] = None,
        trigger_bake: bool = True,
        bake_limit: Optional[int] = None,
        bake_concurrency: int = 3,
    ) -> dict:
        current_loop = asyncio.get_running_loop()
        if self._run_lock is None or self._run_lock_loop is not current_loop:
            self._run_lock = asyncio.Lock()
            self._run_lock_loop = current_loop
        run_lock = self._run_lock
        async with run_lock:
            return await self._run_batch(
                limit_override,
                force_finalize_tail,
                max_groups=max_groups,
                trigger_bake=trigger_bake,
                bake_limit=bake_limit,
                bake_concurrency=bake_concurrency,
            )

    @staticmethod
    def _save_timeline_data_facts(
        conn: sqlite3.Connection,
        timeline_id: int,
        knowledge: dict,
        capture_ids: list[int],
    ) -> None:
        """持久化模型一次提炼出的结构化事实；不在此处重做语义提炼。"""
        contract_version = str(knowledge.get('data_fact_contract') or '').strip()
        if not contract_version:
            return

        raw_facts = knowledge.get('data_facts')
        facts = raw_facts if isinstance(raw_facts, list) else []
        try:
            rejected_count = max(0, int(knowledge.get('data_fact_rejected_count') or 0))
        except (TypeError, ValueError):
            rejected_count = 0
        now_ms = int(time.time() * 1000)
        observed_at = knowledge.get('observed_at') or knowledge.get('end_time') or knowledge.get('start_time')
        period_observed_at = int(observed_at or now_ms)
        week_ms = 7 * 24 * 60 * 60 * 1000
        first_monday_ms = 4 * 24 * 60 * 60 * 1000
        period_start_at = (
            (period_observed_at - first_monday_ms) // week_ms
        ) * week_ms + first_monday_ms
        period_end_at = period_start_at + week_ms - 1
        period_key = f"week:{period_start_at}"
        capture_ids_json = json.dumps(capture_ids, ensure_ascii=False)

        try:
            conn.execute(
                """
                INSERT INTO timeline_data_fact_runs (
                    timeline_id, contract_version, accepted_count, rejected_count, created_at, updated_at
                ) VALUES (?, ?, 0, ?, ?, ?)
                ON CONFLICT(timeline_id) DO UPDATE SET
                    contract_version = excluded.contract_version,
                    rejected_count = timeline_data_fact_runs.rejected_count + excluded.rejected_count,
                    updated_at = excluded.updated_at
                """,
                (timeline_id, contract_version, rejected_count, now_ms, now_ms),
            )
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                identity = "\x1f".join(
                    str(fact.get(field) or '').strip().casefold()
                    for field in ('subject', 'action', 'target_context', 'metric')
                )
                fact_key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
                conn.execute(
                    """
                    INSERT INTO timeline_data_facts (
                        timeline_id, fact_key, title, subject, action, target_context,
                        dimension, metric, value, unit, statement, evidence_quote,
                        confidence, observed_at, period_granularity, period_key,
                        period_start_at, period_end_at, source_capture_ids,
                        semantic_relation, future_question, decision_reason,
                        decision_state, decision_rule_version, created_at, updated_at
                    ) VALUES (
                        ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12,
                        ?13, ?14, 'week', ?15, ?16, ?17, ?18, ?19, ?20, ?21,
                        ?22, ?23, ?24, ?25
                    )
                    ON CONFLICT(timeline_id, fact_key, dimension, value, unit) DO UPDATE SET
                        title = excluded.title,
                        statement = excluded.statement,
                        evidence_quote = excluded.evidence_quote,
                        confidence = excluded.confidence,
                        observed_at = COALESCE(excluded.observed_at, timeline_data_facts.observed_at),
                        source_capture_ids = excluded.source_capture_ids,
                        semantic_relation = excluded.semantic_relation,
                        future_question = excluded.future_question,
                        decision_reason = excluded.decision_reason,
                        decision_state = excluded.decision_state,
                        decision_rule_version = excluded.decision_rule_version,
                        updated_at = excluded.updated_at
                    """,
                    (
                        timeline_id,
                        fact_key,
                        fact.get('title', ''),
                        fact.get('subject', ''),
                        fact.get('action', ''),
                        fact.get('target_context', ''),
                        fact.get('dimension', ''),
                        fact.get('metric', ''),
                        fact.get('value', ''),
                        fact.get('unit', ''),
                        fact.get('statement', ''),
                        fact.get('evidence_quote', ''),
                        fact.get('confidence', 'medium'),
                        observed_at,
                        period_key,
                        period_start_at,
                        period_end_at,
                        capture_ids_json,
                        fact.get('semantic_relation', ''),
                        fact.get('future_question', ''),
                        fact.get('decision_reason', ''),
                        fact.get('decision_state', 'shadow'),
                        fact.get('decision_rule_version', 'data-open-semantic-v4'),
                        now_ms,
                        now_ms,
                    ),
                )
            accepted_count = conn.execute(
                "SELECT COUNT(*) FROM timeline_data_facts WHERE timeline_id = ?",
                (timeline_id,),
            ).fetchone()[0]
            conn.execute(
                """
                UPDATE timeline_data_fact_runs
                SET accepted_count = ?, updated_at = ?
                WHERE timeline_id = ?
                """,
                (accepted_count, now_ms, timeline_id),
            )
        except sqlite3.OperationalError as exc:
            # Sidecar 可能比 Core Engine 更早启动；迁移完成前保留时间线主流程。
            logger.warning("结构化数据事实表尚不可用，暂不持久化: %s", exc)

    # 模型判定为可刷新报表源的页面类型；data_content/none 不注册
    _REGISTERABLE_PAGE_KINDS = {"data_report", "data_platform"}

    def _register_timeline_data_pages(
        self,
        timeline_id: int,
        knowledge: dict,
        captures: list,
    ) -> None:
        """注册时间线推理中模型分类出的数据报表/平台页面为可刷新报表源。

        与 data_facts 同车产出、同姿态容错：仅接受规范化后仍存在于本组
        capture URL 集合的条目（防模型幻觉），接口失败仅告警，不打断
        时间线主流程。data_content 的数值已由 data_facts 通道落库，此处不注册。
        """
        contract_version = str(knowledge.get('data_page_contract') or '').strip()
        pages = knowledge.get('data_pages')
        if not contract_version or not isinstance(pages, list) or not pages:
            return

        url_to_capture = {}
        for capture in captures or []:
            capture_id = capture.get('id')
            url = str(capture.get('url') or '').strip().rstrip('/')
            if capture_id is not None and url and url not in url_to_capture:
                url_to_capture[url] = int(capture_id)
        if not url_to_capture:
            return

        candidates = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            kind = str(page.get('page_kind') or '').strip()
            if kind not in self._REGISTERABLE_PAGE_KINDS:
                continue
            url = str(page.get('url') or '').strip().rstrip('/')
            capture_id = url_to_capture.get(url)
            if capture_id is None:
                logger.info("data_pages 条目 URL 不在本组采集中，跳过注册: %s", url[:200])
                continue
            candidates.append((url, kind, capture_id, str(page.get('title') or '').strip()))
        if not candidates:
            return

        observed_at = (
            knowledge.get('observed_at')
            or knowledge.get('end_time')
            or knowledge.get('start_time')
        )
        endpoint = f"{self._get_core_engine_url().rstrip('/')}{_DATA_SOURCE_DISCOVERED_ENDPOINT}"
        for url, kind, capture_id, title in candidates:
            payload = json.dumps({
                "url": url,
                "title": title or None,
                "capture_id": capture_id,
                "timeline_id": timeline_id,
                "observed_at": observed_at,
                "page_kind": kind,
            }).encode("utf-8")
            request = urllib_request.Request(
                endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib_request.urlopen(request, timeout=5) as response:
                    body = response.read().decode("utf-8") if response else ""
                    data = json.loads(body) if body else {}
                if data.get("status") == "registered":
                    logger.info(
                        "模型分类数据页面已注册为报表源: url=%s source_id=%s created=%s",
                        url[:120], data.get("source_id"), data.get("created"),
                    )
                else:
                    logger.info(
                        "数据页面注册被服务端拒绝: reason=%s url=%s",
                        data.get("reason_code"), url[:120],
                    )
            except Exception as exc:
                logger.warning(
                    "数据页面注册失败（不影响时间线主流程）: %s url=%s",
                    type(exc).__name__, url[:120],
                )

    @staticmethod
    def _ensure_timeline_work_columns(conn: sqlite3.Connection) -> None:
        """Defensively bridge startup/test databases not yet opened by Core."""
        existing = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(timelines)").fetchall()
        }
        for name in ("work_item", "work_status", "work_progress"):
            if name not in existing:
                conn.execute(f"ALTER TABLE timelines ADD COLUMN {name} TEXT")

    def _save_knowledge(self, conn: sqlite3.Connection, knowledge: dict) -> int:
        """保存 knowledge 条目，返回新插入的 id"""
        self._ensure_timeline_work_columns(conn)
        capture_ids_raw = knowledge.get('capture_ids', '[]')
        try:
            capture_ids = json.loads(capture_ids_raw) if capture_ids_raw else []
        except json.JSONDecodeError:
            capture_ids = []

        primary_capture_id = capture_ids[0] if capture_ids else knowledge.get('capture_id')
        if primary_capture_id is None:
            raise ValueError('knowledge 缺少 capture_id/capture_ids，无法保存')

        overview = " ".join(str(knowledge.get('overview') or knowledge.get('summary', '')).split())
        summary = " ".join(str(knowledge.get('summary') or '').split())
        if not summary:
            summary = overview[:42].rstrip() + ('…' if len(overview) > 42 else '')
        cursor = conn.cursor()
        current_time_ms = int(time.time() * 1000)
        cursor.execute("""
            INSERT INTO timelines
            (
                capture_id,
                summary,
                overview,
                details,
                entities,
                category,
                importance,
                occurrence_count,
                capture_ids,
                start_time,
                end_time,
                duration_minutes,
                time_range_start,
                time_range_end,
                key_timestamps,
                frag_app_name,
                frag_win_title,
                observed_at,
                event_time_start,
                event_time_end,
                history_view,
                content_origin,
                activity_type,
                is_self_generated,
                evidence_strength,
                work_item,
                work_status,
                work_progress,
                created_at_ms,
                updated_at_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            primary_capture_id,
            summary,
            overview,
            knowledge.get('details', ''),
            knowledge.get('entities', '[]'),
            knowledge.get('category', '其他'),
            knowledge.get('importance', 3),
            knowledge.get('occurrence_count', 1),
            capture_ids_raw,
            knowledge.get('start_time'),
            knowledge.get('end_time'),
            knowledge.get('duration_minutes'),
            knowledge.get('time_range_start'),
            knowledge.get('time_range_end'),
            knowledge.get('key_timestamps'),
            knowledge.get('frag_app_name'),
            knowledge.get('frag_win_title'),
            knowledge.get('observed_at') or knowledge.get('end_time') or knowledge.get('start_time'),
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
            current_time_ms,
            current_time_ms,
        ))
        timeline_id = cursor.lastrowid
        self._save_timeline_data_facts(conn, timeline_id, knowledge, capture_ids)
        conn.commit()
        return timeline_id

    @staticmethod
    def _apply_document_metadata_defaults(knowledge: dict, captures: list[dict]) -> bool:
        """对“文档 URL + 实质正文”应用确定性元数据。

        importance 仍保留模型对业务重要程度的判断；文档是否进入 bake 不再依赖
        importance，也不依赖模型是否完整输出 activity/origin/evidence 三个字段。
        """
        from knowledge.fragment_grouper import _document_identity, _content_document_identity

        substantive_chars = 0
        has_document_url = False
        # 内容文档（无 URL）：办公应用/IM 里的密集长正文同样是文档，
        # 但必须占本组实质正文的主体，避免混合组被误标为文档（timeline 2008 教训）。
        content_doc_chars = 0
        for capture in captures:
            if _document_identity(capture.get('url')):
                has_document_url = True
            visible_text = " ".join(
                str(capture.get(key) or "")
                for key in ("ax_text", "ocr_text", "input_text", "audio_text")
            )
            chars = len("".join(visible_text.split()))
            substantive_chars += chars
            if _content_document_identity(capture):
                content_doc_chars += chars

        if substantive_chars < _SUBSTANTIVE_DOCUMENT_MIN_CHARS:
            return False
        if not has_document_url and content_doc_chars * 2 < substantive_chars:
            return False

        knowledge['category'] = '文档'
        knowledge['activity_type'] = 'reading'
        knowledge['content_origin'] = 'document_reference'
        knowledge['evidence_strength'] = 'medium'
        return True

    def _mark_captures_processed(
        self, conn: sqlite3.Connection, capture_ids: list[int], timeline_id: int
    ):
        """标记 captures 已被合并进 timeline"""
        placeholders = ','.join('?' * len(capture_ids))
        conn.execute(
            f"UPDATE captures SET timeline_id = ? WHERE id IN ({placeholders})",
            [timeline_id] + capture_ids,
        )
        conn.commit()

    def _get_low_value_sink_timeline_id(
        self, conn: sqlite3.Connection, primary_capture_id: int
    ) -> int:
        """获取/创建隐藏的"低价值回收"时间线，承接确定性丢弃的 captures。

        该时间线 is_self_generated=1（UI 与 bake 的查询都带 is_self_generated=0
        过滤，不会展示），overview 保持 NULL，避免被
        `_find_similar_knowledge` 的相似度合并命中。
        """
        row = conn.execute(
            "SELECT id FROM timelines WHERE summary = ? AND is_self_generated = 1 "
            "ORDER BY id LIMIT 1",
            (_LOW_VALUE_SINK_SUMMARY,),
        ).fetchone()
        if row:
            return int(row[0])
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
                primary_capture_id,
                _LOW_VALUE_SINK_SUMMARY,
                "确定性丢弃的低价值采集挂此隐藏时间线回收，不对外展示。",
                now_ms,
                now_ms,
                now_ms,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)

    def _consume_discarded_captures(
        self, capture_ids: list[int], reason: str
    ) -> bool:
        """将确定性丢弃的 captures 挂到隐藏回收时间线并标记已处理。"""
        sink_id = None
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                sink_id = self._get_low_value_sink_timeline_id(
                    conn, int(capture_ids[0])
                )
                self._mark_captures_processed(conn, capture_ids, sink_id)
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning("确定性丢弃 captures 落库失败: %s", e)
            return False
        logger.info(
            "🗑 片段确定性丢弃 (reason=%s): %d 条 captures 挂到低价值回收时间线 %d，不再重提炼",
            reason,
            len(capture_ids),
            sink_id,
        )
        return True

    def _timeline_member_capture_ids(
        self, conn: sqlite3.Connection, timeline_id: int
    ) -> list[int]:
        """返回 timeline 的成员 capture ids，兼容旧 capture_ids 字段与 captures.timeline_id 反向链接。"""

        member_ids: list[int] = []

        def add_id(value) -> None:
            try:
                cid = int(value)
            except (TypeError, ValueError):
                return
            if cid not in member_ids:
                member_ids.append(cid)

        row = conn.execute(
            "SELECT capture_id, capture_ids FROM timelines WHERE id = ?",
            (timeline_id,),
        ).fetchone()
        if row:
            add_id(row[0])
            try:
                for cid in json.loads(row[1] or "[]"):
                    add_id(cid)
            except (TypeError, json.JSONDecodeError):
                pass

        try:
            linked_rows = conn.execute(
                "SELECT id FROM captures WHERE timeline_id = ? ORDER BY ts ASC, id ASC",
                (timeline_id,),
            ).fetchall()
            for linked_row in linked_rows:
                add_id(linked_row[0])
        except sqlite3.OperationalError:
            # 兼容极旧测试库或迁移中间态，无法反查时保留旧字段结果。
            pass

        return member_ids

    def _document_state_for_capture_ids(
        self, conn: sqlite3.Connection, capture_ids: list[int]
    ) -> Optional[tuple[set[str], bool]]:
        """读取 capture 的文档 URL identity 与文档特征；查询失败时返回 None。"""
        if not capture_ids:
            return set(), False
        from knowledge.fragment_grouper import _document_identity, _looks_like_document_capture

        placeholders = ','.join('?' * len(capture_ids))
        try:
            rows = conn.execute(
                f"""
                SELECT url, win_title, webpage_title
                FROM captures
                WHERE id IN ({placeholders})
                """,
                capture_ids,
            ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("文档边界守卫查询 capture 来源失败: %s，拒绝合并", e)
            return None

        identities: set[str] = set()
        has_document_hint = False
        for url, win_title, webpage_title in rows:
            identity = _document_identity(url)
            if identity:
                identities.add(identity)
            if _looks_like_document_capture({
                'url': url,
                'win_title': win_title,
                'webpage_title': webpage_title,
            }):
                has_document_hint = True
        return identities, has_document_hint

    def _append_key_timestamp_segment(
        self,
        conn: sqlite3.Connection,
        timeline_id: int,
        added_capture_ids: list[int],
        group: list[dict],
        summary: str = '',
    ) -> Optional[str]:
        """合并追加 capture 时同步补一段 key_timestamps，返回更新后的 JSON。

        历史上跨批合并只更新 capture_ids 不维护 key_timestamps，导致列表页
        关联采集数（capture_ids 长度）与详情页片段覆盖的采集数不一致。
        新增 capture 已被既有片段覆盖或无可用时间戳时返回 None，不改动原值。
        """
        if not added_capture_ids:
            return None
        row = conn.execute(
            "SELECT key_timestamps FROM timelines WHERE id = ?",
            (timeline_id,),
        ).fetchone()
        if not row:
            return None
        segments: list[dict] = []
        try:
            loaded = json.loads(row[0] or '[]')
            if isinstance(loaded, list):
                segments = [seg for seg in loaded if isinstance(seg, dict)]
        except (TypeError, json.JSONDecodeError):
            segments = []
        covered: set[int] = set()
        for seg in segments:
            raw_ids = seg.get('capture_ids')
            if isinstance(raw_ids, list):
                for cid in raw_ids:
                    try:
                        covered.add(int(cid))
                    except (TypeError, ValueError):
                        pass
        added = [cid for cid in added_capture_ids if cid not in covered]
        if not added:
            return None
        ts_map: dict[int, int] = {}
        for capture in group:
            try:
                ts_map[int(capture.get('id'))] = int(capture.get('ts'))
            except (TypeError, ValueError):
                pass
        missing = [cid for cid in added if cid not in ts_map]
        if missing:
            placeholders = ','.join('?' * len(missing))
            try:
                for cid, ts in conn.execute(
                    f"SELECT id, ts FROM captures WHERE id IN ({placeholders})",
                    missing,
                ).fetchall():
                    if ts is not None:
                        ts_map[int(cid)] = int(ts)
            except sqlite3.OperationalError:
                pass
        times = [ts_map[cid] for cid in added if cid in ts_map]
        if not times:
            return None
        segments.append({
            'capture_ids': added,
            'start_ts': min(times),
            'end_ts': max(times),
            'summary': summary or '',
        })
        return json.dumps(segments, ensure_ascii=False)

    def _merge_group_into_existing_timeline(
        self,
        conn: sqlite3.Connection,
        timeline_id: int,
        capture_ids: list[int],
        group: list[dict],
        merged_details: str,
        work_item: Optional[str] = None,
        work_status: Optional[str] = None,
        work_progress: Optional[str] = None,
    ) -> None:
        """语义去重合并：同步 timeline 成员、时间范围和 details。"""
        self._ensure_timeline_work_columns(conn)
        row = conn.execute(
            """
            SELECT start_time, end_time, time_range_start, time_range_end, observed_at
            FROM timelines WHERE id = ?
            """,
            (timeline_id,),
        ).fetchone()
        existing_ids = self._timeline_member_capture_ids(conn, timeline_id)
        merged_ids = existing_ids + [cid for cid in capture_ids if cid not in existing_ids]
        added_ids = [cid for cid in capture_ids if cid not in existing_ids]
        key_timestamps_json = self._append_key_timestamp_segment(
            conn, timeline_id, added_ids, group
        )

        group_times = []
        for capture in group:
            try:
                group_times.append(int(capture.get('ts')))
            except (TypeError, ValueError):
                pass

        existing_start = row[0] if row else None
        existing_end = row[1] if row else None
        existing_range_start = row[2] if row else None
        existing_range_end = row[3] if row else None
        existing_observed = row[4] if row else None

        start_candidates = [v for v in (existing_start, existing_range_start, *group_times) if v]
        end_candidates = [v for v in (existing_end, existing_range_end, existing_observed, *group_times) if v]
        next_start = min(start_candidates) if start_candidates else None
        next_end = max(end_candidates) if end_candidates else None

        conn.execute(
            """
            UPDATE timelines SET
                occurrence_count = occurrence_count + 1,
                details = ?,
                capture_ids = ?,
                key_timestamps = COALESCE(?, key_timestamps),
                start_time = COALESCE(?, start_time),
                end_time = COALESCE(?, end_time),
                time_range_start = COALESCE(?, time_range_start),
                time_range_end = COALESCE(?, time_range_end),
                observed_at = COALESCE(?, observed_at),
                work_item = COALESCE(NULLIF(?, ''), work_item),
                work_status = COALESCE(NULLIF(?, ''), work_status),
                work_progress = COALESCE(NULLIF(?, ''), work_progress),
                updated_at_ms = ?
            WHERE id = ?
            """,
            (
                merged_details,
                json.dumps(merged_ids, ensure_ascii=False),
                key_timestamps_json,
                next_start,
                next_end,
                next_start,
                next_end,
                next_end,
                work_item,
                work_status,
                work_progress,
                int(time.time() * 1000),
                timeline_id,
            ),
        )

    def _timeline_accepts_merge(
        self,
        conn: sqlite3.Connection,
        timeline_id: int,
        additional_count: int = 0,
    ) -> bool:
        """增长上限守卫：时间线成员数/合并次数/时间跨度达到上限时拒绝继续合并。

        这是过度合并的根治闸：无论跨批合并还是相似度去重合并，只要目标时间线
        已经足够大，就强制新建时间线，避免单条时间线无限吞噬不相关采集记录。
        返回 True 表示允许合并，False 表示应新建时间线。
        """
        try:
            row = conn.execute(
                "SELECT occurrence_count, start_time, end_time FROM timelines WHERE id = ?",
                (timeline_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            # 旧库缺列时不拦截，保持向后兼容
            return True
        if not row:
            return False

        occurrence_count = int(row[0] or 0)
        if occurrence_count >= _TIMELINE_MAX_OCCURRENCE_COUNT:
            logger.info(
                "⛔ 时间线 %d 合并次数已达上限 %d，拒绝继续合并，改为新建",
                timeline_id, occurrence_count,
            )
            return False

        member_count = len(self._timeline_member_capture_ids(conn, timeline_id))
        if member_count + max(0, additional_count) > _TIMELINE_MAX_MEMBER_COUNT:
            logger.info(
                "⛔ 时间线 %d 成员数将达上限 %d（当前 %d + 新增 %d），拒绝合并",
                timeline_id, _TIMELINE_MAX_MEMBER_COUNT, member_count, additional_count,
            )
            return False

        start_time, end_time = row[1], row[2]
        if start_time and end_time and end_time > start_time:
            span_hours = (int(end_time) - int(start_time)) / 3600000.0
            if span_hours > _TIMELINE_MAX_SPAN_HOURS:
                logger.info(
                    "⛔ 时间线 %d 跨度 %.1f 小时已超上限 %.1f，拒绝合并",
                    timeline_id, span_hours, _TIMELINE_MAX_SPAN_HOURS,
                )
                return False

        return True

    def _doc_compatible_with_timeline(self, group: list[dict], timeline_id: int) -> bool:
        """文档边界守卫：判断 group 是否可跨批合并进 timeline_id。

        规则与 FragmentGrouper 一致：
        - 文档 timeline 只接受每条 capture 都具有相同非空文档 URL identity 的 group；
        - 不同 URL、混入普通 capture、URL 为空，一律拒绝合并；
        - URL 为空但标题/类型呈现文档特征的 timeline 也不接受跨批合并；
        - 普通非文档 timeline 仍按原语义规则合并。
        数据库查询失败时也拒绝合并，避免在无法证明同 URL 时污染 timeline。
        """
        from knowledge.fragment_grouper import _document_identity, _looks_like_document_capture
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                member_ids = self._timeline_member_capture_ids(conn, timeline_id)
                if not member_ids:
                    return False
                timeline_state = self._document_state_for_capture_ids(conn, member_ids)
                if timeline_state is None:
                    return False
                tl_docs, tl_has_document_hint = timeline_state
                timeline_row = conn.execute(
                    """
                    SELECT category, content_origin, frag_win_title
                    FROM timelines
                    WHERE id = ?
                    """,
                    (timeline_id,),
                ).fetchone()
                if timeline_row:
                    category, content_origin, frag_win_title = timeline_row
                    tl_has_document_hint = tl_has_document_hint or (
                        '文档' in str(category or '')
                        or str(content_origin or '') == 'document_reference'
                        or _looks_like_document_capture({
                            'url': None,
                            'win_title': frag_win_title,
                        })
                    )
            finally:
                conn.close()
        except Exception as e:
            logger.warning("文档边界守卫查询失败 timeline_id=%d: %s，拒绝合并", timeline_id, e)
            return False

        group_identities = [_document_identity(c.get('url')) for c in group]
        grp_has_document_hint = any(_looks_like_document_capture(c) for c in group)

        if tl_docs:
            # 历史上已经混入多个文档的 timeline 不再接受任何追加，避免继续污染。
            if len(tl_docs) != 1 or not group:
                return False
            target_doc = next(iter(tl_docs))
            return all(identity == target_doc for identity in group_identities)

        # timeline 看起来是文档但自身 URL 为空，无法证明后续 capture 同源，禁止追加。
        if tl_has_document_hint:
            return False

        # 普通 timeline 不接收已知文档或 URL 为空的文档型 capture。
        return not grp_has_document_hint

    def _entities_compatible_with_timeline(self, knowledge: dict, timeline_id: int) -> bool:
        """实体一致性守卫：相似度合并前确认新旧知识不是"同项目不同任务"。

        只在新旧知识都带实体且交集为空时拦截；任一侧实体缺失时不拦截，
        交由相似度阈值判断。数据库查询失败时不拦截，保持与旧行为兼容。
        """
        def _parse_entities(raw) -> set:
            if raw is None:
                return set()
            if isinstance(raw, list):
                items = raw
            else:
                try:
                    items = json.loads(raw or '[]')
                except (TypeError, json.JSONDecodeError):
                    return set()
            return {str(e).strip().lower() for e in items if str(e).strip()}

        new_entities = _parse_entities(knowledge.get('entities'))
        if not new_entities:
            return True
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT entities FROM timelines WHERE id = ?",
                    (timeline_id,),
                ).fetchone()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("实体一致性守卫查询失败 timeline_id=%d: %s，不拦截", timeline_id, e)
            return True
        if not row:
            return False
        existing_entities = _parse_entities(row[0])
        if not existing_entities:
            return True
        return bool(new_entities & existing_entities)

    def _density_compatible_with_timeline(self, group: list[dict], timeline_id: int) -> bool:
        """内容密度差守卫：新片段与目标时间线成员的正文密度差异过大时拒绝合并。

        针对 timeline 2008 类问题：密集汇报正文被并进以日历/杂项为主的
        稀薄时间线后细节被稀释。双向拦截：密集片段不进稀薄时间线，
        稀薄片段也不进密集时间线。阈值取 0.3；无法计算时不拦截，
        保持与旧行为兼容。
        """
        from knowledge.fragment_grouper import text_density_score

        def _capture_density(capture: dict) -> Optional[float]:
            text = str(
                capture.get('ax_text')
                or capture.get('ocr_text')
                or capture.get('input_text')
                or capture.get('audio_text')
                or ''
            )
            if len(' '.join(text.split())) < 40:
                return None
            return text_density_score(text)

        new_densities = [d for d in (_capture_density(c) for c in group) if d is not None]
        if not new_densities:
            return True
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                member_ids = self._timeline_member_capture_ids(conn, timeline_id)
                if not member_ids:
                    return True
                placeholders = ','.join('?' for _ in member_ids)
                rows = conn.execute(
                    f"SELECT ocr_text, ax_text, input_text, audio_text FROM captures WHERE id IN ({placeholders})",
                    member_ids,
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("密度差守卫查询失败 timeline_id=%d: %s，不拦截", timeline_id, e)
            return True
        member_densities = []
        for row in rows:
            text = str(row[1] or row[0] or row[2] or row[3] or '')
            if len(' '.join(text.split())) < 40:
                continue
            member_densities.append(text_density_score(text))
        if not member_densities:
            return True
        new_avg = sum(new_densities) / len(new_densities)
        member_avg = sum(member_densities) / len(member_densities)
        return abs(new_avg - member_avg) < 0.3

    async def _append_captures_to_timeline(self, group: list[dict], timeline_id: int) -> bool:
        """跨批合并：把 group 里的新 captures 追加到已有 timeline（occurrence_count+1，更新 end_time）。
        不调 LLM，直接标记 captures 已处理并更新 timeline 的时间范围。"""
        try:
            capture_ids = [c['id'] for c in group]
            conn = sqlite3.connect(self.db_path)
            try:
                # 检查 timeline 仍存在
                row = conn.execute(
                    "SELECT id, capture_ids, end_time FROM timelines WHERE id = ?", (timeline_id,)
                ).fetchone()
                if not row:
                    return False
                # 合并 capture_ids JSON，并同步补 key_timestamps 段，
                # 避免详情页片段只覆盖初始采集、与列表页关联采集数不一致。
                existing_ids = json.loads(row[1] or '[]') if row[1] else []
                merged_ids = existing_ids + [c for c in capture_ids if c not in existing_ids]
                added_ids = [c for c in capture_ids if c not in existing_ids]
                key_timestamps_json = self._append_key_timestamp_segment(
                    conn, timeline_id, added_ids, group
                )
                new_end_time = max((c['ts'] for c in group), default=row[2] or 0)
                conn.execute(
                    """UPDATE timelines SET
                         capture_ids = ?,
                         key_timestamps = COALESCE(?, key_timestamps),
                         end_time = MAX(COALESCE(end_time, 0), ?),
                         occurrence_count = occurrence_count + 1,
                         updated_at_ms = ?
                       WHERE id = ?""",
                    (
                        json.dumps(merged_ids),
                        key_timestamps_json,
                        new_end_time,
                        int(time.time() * 1000),
                        timeline_id,
                    ),
                )
                conn.commit()
                self._mark_captures_processed(conn, capture_ids, timeline_id)
            finally:
                conn.close()
            return True
        except Exception as e:
            logger.warning("跨批追加 timeline 失败 timeline_id=%d: %s", timeline_id, e)
            return False

    def _group_idle_minutes(self, group: list[dict], now_ms: int) -> float:
        """计算片段距当前时间的静默分钟数"""
        if not group:
            return 0.0
        return max(0.0, (now_ms - group[-1]['ts']) / 60000)

    def _should_finalize_last_group(
        self,
        group: list[dict],
        now_ms: int,
        fetched_count: int,
    ) -> tuple[bool, str]:
        """判断最后一组是否已经足够成熟，可以落成 knowledge"""
        if not group:
            return False, 'empty_group'

        group_size = len(group)
        idle_minutes = self._group_idle_minutes(group, now_ms)
        soft_window = FragmentGrouper.SOFT_SPLIT_MINUTES
        hard_window = FragmentGrouper.HARD_SPLIT_MINUTES
        min_group_wait = FragmentGrouper.MIN_GROUP_WAIT

        if idle_minutes >= hard_window:
            return True, 'hard_timeout'

        # 最老的 capture 已经等超过 hard_window，无论最新一条多新都应落库
        oldest_age_minutes = max(0.0, (now_ms - group[0]['ts']) / 60000)
        if oldest_age_minutes >= hard_window:
            return True, 'oldest_capture_timeout'

        if group_size >= min_group_wait and idle_minutes >= soft_window:
            return True, 'idle_window_reached'

        if fetched_count < self.batch_size and idle_minutes >= soft_window:
            return True, 'tail_batch_idle'

        if group_size < min_group_wait:
            return False, 'group_too_small'

        return False, 'idle_not_enough'

    async def _process_capture_group(
        self,
        group: list[dict],
        merge_timeline_id: Optional[int] = None,
    ):
        """将一组 captures 合并提炼为一个 knowledge 条目"""
        try:
            capture_ids = [c['id'] for c in group]
            logger.info(
                "开始片段提炼: size=%s first_id=%s last_id=%s",
                len(group),
                capture_ids[0] if capture_ids else None,
                capture_ids[-1] if capture_ids else None,
            )
            extractor = self._get_knowledge_extractor()
            logger.info("KnowledgeExtractor 已就绪，提交 InferenceQueue 执行 extract_merged")
            from inference_queue import (
                LANE_P1_CAPTURE,
                Priority,
                QueueEvictedError,
                current_task_preempt_requested,
                get_global_queue,
            )
            def _run_extract_merged():
                group_id = self._mark_group_extracting(group)
                succeeded = False
                try:
                    knowledge_result = extractor.extract_merged(
                        captures=group,
                        preempt_check=current_task_preempt_requested,
                    )
                    succeeded = bool(knowledge_result)
                    return knowledge_result
                finally:
                    self._unmark_group_extracting(group_id, succeeded)

            try:
                knowledge = get_global_queue().submit_sync(
                    Priority.P1,
                    _run_extract_merged,
                    timeout=600.0,
                    lane=LANE_P1_CAPTURE,
                )
            except QueueEvictedError as ee:
                logger.warning(f"extract_merged 被队列淘汰: {ee}")
                self._record_timeline_extraction_failure(capture_ids)
                return False

            if not knowledge:
                logger.warning(f"片段提炼未产出 knowledge ({len(group)} 条 captures)")
                failure_count = self._record_timeline_extraction_failure(capture_ids)
                if (
                    len(group) > 1
                    and failure_count >= _TIMELINE_GROUP_SPLIT_AFTER_FAILURES
                ):
                    logger.warning(
                        "片段连续失败 %s 次，确定性拆成单条重试: ids=%s",
                        failure_count,
                        capture_ids,
                    )
                    handled_all = True
                    for capture in group:
                        if not await self._process_capture_group([capture]):
                            handled_all = False
                    return handled_all
                return False

            if knowledge.get('_split_required'):
                # LLM 在落库前确认上游片段实际包含多个独立任务。严格按已经
                # 校验过的 capture ID 分区逐组重提炼，绝不先写入一条混合时间线。
                # 原 group 若带跨批合并目标也不向子组透传，避免所有子组被强制
                # 追加到同一旧时间线；各子组重新走正常的相似度与实体门禁。
                capture_by_id = {int(c['id']): c for c in group}
                subgroup_ids = knowledge.get('capture_groups') or []
                subgroups = [
                    [capture_by_id[int(capture_id)] for capture_id in ids]
                    for ids in subgroup_ids
                ]
                logger.info(
                    "任务一致性门禁拆分片段: original_ids=%s groups=%s reason=%s",
                    capture_ids,
                    subgroup_ids,
                    knowledge.get('split_reason') or '',
                )
                handled_all = True
                for subgroup in subgroups:
                    if not await self._process_capture_group(subgroup):
                        handled_all = False
                if handled_all:
                    self._clear_timeline_extraction_failures(capture_ids)
                return handled_all

            if knowledge.get('_discarded'):
                # 确定性丢弃（SKIP/无价值/质量不足）：标记已处理，终止重提炼循环。
                # 临时失败（抢占/RAG 活跃/解析失败）仍返回 None 走上面的重试路径。
                self._consume_discarded_captures(
                    capture_ids, knowledge.get('discard_reason', 'unknown')
                )
                self._clear_timeline_extraction_failures(capture_ids)
                return True

            discarded_ids = knowledge.get('_discarded_capture_ids') or []
            if discarded_ids:
                # 混入同组的低价值 captures：挂隐藏回收时间线标记已处理，
                # 并从本提炼组剔除，不写入时间线成员（防无关采集混入）。
                self._consume_discarded_captures(
                    [int(i) for i in discarded_ids], 'merged_group_low_value'
                )
                self._clear_timeline_extraction_failures(
                    [int(i) for i in discarded_ids]
                )
                discarded_set = set(int(i) for i in discarded_ids)
                group = [c for c in group if c['id'] not in discarded_set]
                capture_ids = [c['id'] for c in group]
                if not group:
                    return True

            if self._apply_document_metadata_defaults(knowledge, group):
                logger.info(
                    "文档元数据确定性兜底已应用: captures=%s activity=reading origin=document_reference evidence=medium",
                    capture_ids,
                )

            conn = sqlite3.connect(self.db_path)

            # 跨批次去重：若新 knowledge 与已有条目高度相似，则合并而非插入
            overview = knowledge.get('overview') or knowledge.get('summary', '')
            similar_id = merge_timeline_id
            if similar_id is not None:
                timeline_exists = conn.execute(
                    "SELECT COUNT(*) > 0 FROM timelines WHERE id = ?",
                    (similar_id,),
                ).fetchone()[0]
                if not timeline_exists:
                    conn.close()
                    return False
            else:
                similar_id = extractor._find_similar_knowledge(
                    overview,
                    conn,
                    entities=json.loads(knowledge.get('entities') or '[]') if knowledge.get('entities') else None,
                    start_time=knowledge.get('start_time'),
                    end_time=knowledge.get('end_time'),
                ) if overview else None

            if similar_id:
                if not self._doc_compatible_with_timeline(group, similar_id):
                    logger.info(
                        "🚫 相似时间线合并被文档边界拦截: group 与 timeline_id=%d 文档不一致，改为新建时间线",
                        similar_id,
                    )
                    similar_id = None

            if similar_id and not self._entities_compatible_with_timeline(knowledge, similar_id):
                logger.info(
                    "🚫 相似时间线合并被实体一致性拦截: 新知识实体与 timeline_id=%d 无任何交集，改为新建时间线",
                    similar_id,
                )
                similar_id = None

            if similar_id and not self._density_compatible_with_timeline(group, similar_id):
                logger.info(
                    "🚫 相似时间线合并被内容密度差拦截: 新片段与 timeline_id=%d 成员正文密度差异过大，改为新建时间线",
                    similar_id,
                )
                similar_id = None

            if similar_id and not self._timeline_accepts_merge(conn, similar_id, len(capture_ids)):
                logger.info(
                    "🚫 时间线 %d 已达增长上限，拒绝继续合并，改为新建时间线（过度合并防护）",
                    similar_id,
                )
                similar_id = None

            if similar_id:
                # 合并：occurrence_count+1，追加 details（去重保留新信息）
                existing = conn.execute(
                    "SELECT details FROM timelines WHERE id = ?", (similar_id,)
                ).fetchone()
                existing_details = (existing[0] or "") if existing else ""
                new_details = knowledge.get('details', '')
                if new_details and new_details not in existing_details:
                    from datetime import datetime as _dt
                    merged_details = existing_details + f"\n\n--- 补充 ({_dt.now().strftime('%Y-%m-%d %H:%M')}) ---\n{new_details}"
                else:
                    merged_details = existing_details
                self._merge_group_into_existing_timeline(
                    conn,
                    similar_id,
                    capture_ids,
                    group,
                    merged_details,
                    knowledge.get('work_item'),
                    knowledge.get('work_status'),
                    knowledge.get('work_progress'),
                )
                self._save_timeline_data_facts(
                    conn,
                    similar_id,
                    knowledge,
                    self._timeline_member_capture_ids(conn, similar_id),
                )
                self._register_timeline_data_pages(similar_id, knowledge, group)
                conn.commit()
                self._mark_captures_processed(conn, capture_ids, similar_id)
                conn.close()
                logger.info(
                    f"🔀 片段已合并到已有时间线: {len(group)} captures → timeline_id={similar_id} (重复)"
                )
                self._clear_timeline_extraction_failures(capture_ids)
                return True

            timeline_id = self._save_knowledge(conn, knowledge)
            self._register_timeline_data_pages(timeline_id, knowledge, group)
            self._mark_captures_processed(conn, capture_ids, timeline_id)
            conn.close()

            asyncio.create_task(self._process_knowledge_vectorization(group, timeline_id, knowledge))

            logger.info(
                f"✅ 片段提炼完成: {len(group)} captures → timeline_id={timeline_id}, "
                f"时长={knowledge.get('duration_minutes')}分钟"
            )
            self._clear_timeline_extraction_failures(capture_ids)
            return True

        except Exception as e:
            logger.error(f"片段提炼异常: {e}")
            if 'capture_ids' in locals():
                self._record_timeline_extraction_failure(capture_ids)
            return False

    def _current_embedding_model_name(self) -> Optional[str]:
        """当前嵌入模型名，用于识别旧后端索引的过期向量。

        嵌入后端切换后（如 Ollama 量化 -> sentence-transformers）同名模型
        的向量空间不兼容，需要用当前模型重建；获取失败返回 None，表示跳过
        模型版本校验，保持原有行为。
        """
        try:
            from model_registry_global import get_shared_embedding

            return get_shared_embedding().model_name
        except Exception as exc:
            logger.debug("未能获取当前嵌入模型名，跳过向量模型版本校验: %s", exc)
            return None

    async def _process_vectorization_batch(self, group: list[dict]):
        """对一组 captures 批量向量化。

        文档 URL 使用完整正文分块并写入 ``document`` 检索域；普通活动记录
        继续保留短摘要向量，避免把聊天/应用 AX 树无限扩张进索引。
        """
        from embedding.document_chunks import build_document_snapshot
        from embedding.vector_storage import get_vector_storage

        storage = get_vector_storage()
        document_snapshots: dict[str, object] = {}
        regular_texts: list[str] = []
        regular_captures: list[dict] = []

        for capture in group:
            snapshot = build_document_snapshot(capture)
            if snapshot is not None:
                existing = document_snapshots.get(snapshot.doc_key)
                if existing is None or len(snapshot.body) > len(existing.body):
                    document_snapshots[snapshot.doc_key] = snapshot
                continue
            text = self._build_capture_embedding_text(capture)
            if text:
                regular_texts.append(text)
                regular_captures.append(capture)

        pending_snapshots = [
            snapshot
            for snapshot in document_snapshots.values()
            if not storage.document_version_exists(
                snapshot.doc_key,
                snapshot.content_hash,
                len(snapshot.chunks),
                self._current_embedding_model_name(),
            )
        ]
        document_texts = [
            chunk
            for snapshot in pending_snapshots
            for chunk in snapshot.chunks
        ]
        texts = [*regular_texts, *document_texts]
        if not texts:
            return

        async with _embedding_semaphore:
            try:
                from model_registry_global import get_shared_embedding

                # 使用全局共享 EmbeddingModel，避免重复加载
                model = get_shared_embedding()
                vectors = []
                for offset in range(0, len(texts), 32):
                    batch_vectors = await asyncio.to_thread(
                        model.encode,
                        texts[offset : offset + 32],
                    )
                    vectors.extend(batch_vectors)

                regular_vectors = vectors[: len(regular_texts)]
                for capture, vec_obj in zip(regular_captures, regular_vectors):
                    if vec_obj and vec_obj.vector:
                        try:
                            storage.store_vector(
                                capture_id=capture['id'],
                                text=vec_obj.text,
                                vector=vec_obj.vector,
                                metadata={
                                    "doc_key": f"capture:{capture['id']}",
                                    "source_type": "capture",
                                    "ts": capture.get('ts'),
                                    "app_name": capture.get('app_name'),
                                }
                            )
                        except Exception:
                            logger.warning(f"⚠️ 向量存储失败，继续时间线提炼: capture_id={capture['id']}")

                vector_offset = len(regular_texts)
                for snapshot in pending_snapshots:
                    chunk_count = len(snapshot.chunks)
                    chunk_vectors = [
                        item.vector
                        for item in vectors[vector_offset : vector_offset + chunk_count]
                    ]
                    vector_offset += chunk_count
                    if len(chunk_vectors) != chunk_count or any(not item for item in chunk_vectors):
                        logger.warning(
                            "文档分块向量结果不完整，跳过写入: doc_key=%s",
                            snapshot.doc_key,
                        )
                        continue
                    capture = next(
                        (
                            item
                            for item in group
                            if int(item.get("id") or 0) == snapshot.capture_id
                        ),
                        {},
                    )
                    storage.store_document_vectors(
                        capture_id=snapshot.capture_id,
                        chunks=snapshot.chunks,
                        vectors=chunk_vectors,
                        metadata={
                            "doc_key": snapshot.doc_key,
                            "source_type": "document",
                            "content_hash": snapshot.content_hash,
                            "url": snapshot.canonical_url,
                            "title": snapshot.title,
                            "ts": capture.get("ts"),
                            "app_name": capture.get("app_name"),
                            "win_title": capture.get("window_title"),
                            "model_name": model.model_name,
                        },
                    )
            except Exception as e:
                logger.error(f"批量向量化失败: {e}")

    async def backfill_document_vectors(self, limit: int = 2000) -> dict:
        """启动后补齐历史文档 capture 的分块向量。

        每个 URL 只选择正文最完整的一次快照；稳定 point id 会让未变化文档
        直接跳过，因此该任务可安全地在每次启动时执行。
        """
        try:
            captures = await asyncio.to_thread(self._load_document_backfill_captures, limit)
            if not captures:
                return {"candidate_count": 0, "processed_count": 0}

            from embedding.document_chunks import build_document_snapshot

            best_by_url: dict[str, dict] = {}
            best_lengths: dict[str, int] = {}
            for capture in captures:
                snapshot = build_document_snapshot(capture)
                if snapshot is None:
                    continue
                body_length = len(snapshot.body)
                if body_length > best_lengths.get(snapshot.doc_key, -1):
                    best_by_url[snapshot.doc_key] = capture
                    best_lengths[snapshot.doc_key] = body_length

            selected = list(best_by_url.values())
            processed = 0
            for offset in range(0, len(selected), 8):
                batch = selected[offset : offset + 8]
                await self._process_vectorization_batch(batch)
                processed += len(batch)
                await asyncio.sleep(0)
            logger.info(
                "历史文档分块向量补齐完成: captures=%s urls=%s",
                len(captures),
                processed,
            )
            return {
                "candidate_count": len(captures),
                "processed_count": processed,
            }
        except Exception as exc:
            logger.error("历史文档分块向量补齐失败: %s", exc, exc_info=True)
            return {
                "candidate_count": 0,
                "processed_count": 0,
                "error": str(exc),
            }

    async def backfill_bake_document_vectors(self, limit: int = 128) -> dict:
        """Backfill vectors from durable bake documents, independent of captures."""
        try:
            expected_model_name = await asyncio.to_thread(
                self._current_embedding_model_name
            )
            documents = await asyncio.to_thread(
                self._load_pending_bake_documents,
                limit,
                expected_model_name,
            )
            if not documents:
                return {"candidate_count": 0, "processed_count": 0}

            processed = 0
            for offset in range(0, len(documents), 4):
                processed += await self._process_bake_document_vector_batch(
                    documents[offset : offset + 4]
                )
                await asyncio.sleep(0)
            logger.info(
                "持久 bake 文档向量补齐完成: candidates=%s processed=%s",
                len(documents),
                processed,
            )
            return {
                "candidate_count": len(documents),
                "processed_count": processed,
            }
        except Exception as exc:
            logger.error("持久 bake 文档向量补齐失败: %s", exc, exc_info=True)
            return {
                "candidate_count": 0,
                "processed_count": 0,
                "error": str(exc),
            }

    async def _process_bake_document_vector_batch(
        self,
        documents: list[dict],
    ) -> int:
        from embedding.document_chunks import build_bake_document_snapshot
        from embedding.vector_storage import get_vector_storage

        storage = get_vector_storage()
        availability_check = getattr(storage, "is_qdrant_available", None)
        if callable(availability_check) and not availability_check():
            logger.debug("Qdrant 暂不可写，延后持久 bake 文档向量补齐")
            return 0
        expected_model_name = self._current_embedding_model_name()
        snapshots = [
            snapshot
            for document in documents
            if (snapshot := build_bake_document_snapshot(document)) is not None
            and not storage.artifact_document_version_exists(
                snapshot.document_id,
                snapshot.doc_key,
                snapshot.content_hash,
                len(snapshot.chunks),
                expected_model_name,
            )
        ]
        if not snapshots:
            return 0

        texts = [chunk for snapshot in snapshots for chunk in snapshot.chunks]
        async with _embedding_semaphore:
            from model_registry_global import get_shared_embedding

            model = get_shared_embedding()
            vectors = []
            for offset in range(0, len(texts), 32):
                batch_vectors = await asyncio.to_thread(
                    model.encode,
                    texts[offset : offset + 32],
                )
                vectors.extend(batch_vectors)

        processed = 0
        vector_offset = 0
        documents_by_id = {
            int(document.get("id") or 0): document for document in documents
        }
        for snapshot in snapshots:
            chunk_count = len(snapshot.chunks)
            chunk_vectors = [
                item.vector
                for item in vectors[vector_offset : vector_offset + chunk_count]
            ]
            vector_offset += chunk_count
            if len(chunk_vectors) != chunk_count or any(not item for item in chunk_vectors):
                logger.warning(
                    "持久文档 embedding 结果不完整: document_id=%s",
                    snapshot.document_id,
                )
                continue
            document = documents_by_id.get(snapshot.document_id, {})
            if storage.store_artifact_document_vectors(
                document_id=snapshot.document_id,
                chunks=snapshot.chunks,
                vectors=chunk_vectors,
                metadata={
                    "doc_key": snapshot.doc_key,
                    "content_hash": snapshot.content_hash,
                    "url": snapshot.canonical_url,
                    "title": snapshot.title,
                    "doc_type": document.get("doc_type"),
                    "updated_at": document.get("updated_at"),
                    "model_name": model.model_name,
                },
            ):
                processed += 1
        return processed

    def _load_pending_bake_documents(
        self,
        limit: int,
        expected_model_name: Optional[str] = None,
    ) -> list[dict]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'table'
                          AND name IN ('bake_documents', 'artifact_vector_index')
                        """
                    )
                }
                if tables != {"bake_documents", "artifact_vector_index"}:
                    return []
                # 嵌入后端切换后，旧模型索引的向量也视为待重建。
                stale_model_clause = ""
                if expected_model_name:
                    stale_model_clause = (
                        "OR SUM(CASE WHEN COALESCE(v.model_name, '') != ? "
                        "THEN 1 ELSE 0 END) > 0"
                    )
                rows = conn.execute(
                    f"""
                    SELECT
                        d.id,
                        d.title,
                        d.doc_type,
                        d.summary,
                        d.full_content,
                        d.sections_json,
                        d.source_url,
                        d.updated_at
                    FROM bake_documents d
                    LEFT JOIN artifact_vector_index v
                      ON v.document_id = d.id
                    WHERE d.deleted_at IS NULL
                      AND (
                          LENGTH(COALESCE(d.full_content, '')) >= ?
                          OR LENGTH(COALESCE(d.sections_json, '')) >= ?
                      )
                    GROUP BY d.id
                    HAVING COUNT(v.id) = 0
                        OR MAX(v.indexed_at) < d.updated_at
                        {stale_model_clause}
                    ORDER BY d.updated_at DESC, d.id DESC
                    LIMIT ?
                    """,
                    (
                        _SUBSTANTIVE_DOCUMENT_MIN_CHARS,
                        _SUBSTANTIVE_DOCUMENT_MIN_CHARS,
                        *(
                            [expected_model_name]
                            if expected_model_name
                            else []
                        ),
                        max(1, int(limit)),
                    ),
                ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("读取待补齐持久文档失败: %s", exc)
            return []
        return [
            {
                "id": row[0],
                "title": row[1],
                "doc_type": row[2],
                "summary": row[3],
                "full_content": row[4],
                "sections_json": row[5],
                "source_url": row[6],
                "updated_at": row[7],
            }
            for row in rows
        ]

    def _drain_vector_deletion_queue(self) -> dict:
        from embedding.vector_storage import get_vector_storage

        return get_vector_storage().drain_deletion_queue()

    def _audit_vector_consistency(self) -> dict:
        from embedding.vector_storage import get_vector_storage

        try:
            return get_vector_storage().audit_qdrant_consistency(
                mark_missing_artifacts=True,
            )
        except Exception as exc:
            logger.warning("向量一致性审计失败: %s", exc)
            return {"available": False, "error": str(exc)}

    def _load_document_backfill_captures(self, limit: int) -> list[dict]:
        url_markers = (
            "/docs/",
            "docs.google",
            "/document/",
            "yuque.com",
            "feishu.cn/docx",
            "feishu.cn/wiki",
            "notion.so",
            "confluence",
            "/wiki/",
            "shimo.im",
            "/d/home/",
            "/s/home/",
            "/k/home/",
        )
        marker_sql = " OR ".join(
            "LOWER(COALESCE(c.url, '')) LIKE ?" for _ in url_markers
        )
        params = [f"%{marker}%" for marker in url_markers]
        params.append(max(1, int(limit)))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT c.id, c.ts, c.app_name, c.win_title, c.webpage_title,
                       c.ocr_text, c.ax_text, c.url
                FROM captures c
                WHERE c.is_sensitive = 0
                  AND ({marker_sql})
                  AND LENGTH(
                        REPLACE(
                            REPLACE(COALESCE(c.ax_text, '') || COALESCE(c.ocr_text, ''), ' ', ''),
                            char(10),
                            ''
                        )
                      ) >= ?
                ORDER BY c.ts DESC
                LIMIT ?
                """,
                [*params[:-1], _SUBSTANTIVE_DOCUMENT_MIN_CHARS, params[-1]],
            ).fetchall()
        return [
            {
                "id": row[0],
                "ts": row[1],
                "app_name": row[2],
                "window_title": row[3],
                "webpage_title": row[4],
                "ocr_text": row[5],
                "ax_text": row[6],
                "url": row[7],
            }
            for row in rows
        ]

    @staticmethod
    def _build_capture_embedding_text(capture: dict) -> str:
        parts: list[str] = []
        if capture.get('app_name'):
            parts.append(f"应用：{capture['app_name']}")
        if capture.get('window_title'):
            parts.append(f"窗口：{capture['window_title']}")
        if capture.get('ocr_text'):
            parts.append(f"OCR：{capture['ocr_text'][:500]}")  # 截断 OCR
        if capture.get('ax_text'):
            parts.append(f"AX：{capture['ax_text'][:500]}")  # 截断 AX
        return "\n".join(part for part in parts if part)[:1000]  # 总长度限制

    @staticmethod
    def _build_knowledge_embedding_text(group: list[dict], knowledge: dict) -> str:
        try:
            entities_raw = knowledge.get('entities') or '[]'
            if isinstance(entities_raw, str):
                entities = json.loads(entities_raw) if entities_raw else []
            else:
                entities = entities_raw
        except Exception:
            entities = []

        parts: list[str] = []
        overview = knowledge.get('overview') or knowledge.get('summary')
        if overview:
            parts.append(f"概述：{overview}")
        if knowledge.get('details'):
            parts.append(f"详情：{knowledge['details']}")
        if entities:
            parts.append(f"实体：{'、'.join(str(entity) for entity in entities if entity)}")
        if knowledge.get('frag_app_name'):
            parts.append(f"应用：{knowledge['frag_app_name']}")
        if knowledge.get('frag_win_title'):
            parts.append(f"窗口：{knowledge['frag_win_title']}")
        if group:
            evidence = [capture.get('window_title') or capture.get('app_name') or '' for capture in group[:3]]
            evidence = [item for item in evidence if item]
            if evidence:
                parts.append(f"证据：{' | '.join(evidence)}")
        return "\n".join(parts)

    async def _process_vectorization(self, capture: dict, text: str):
        """处理单条记录的向量化"""
        capture_id = capture['id']
        try:
            worker = self._get_embed_worker()
            if not worker or not worker._model:
                logger.error("❌ 向量模型未就绪，跳过向量化: capture_id=%s", capture_id)
                return

            # 创建 IPC 请求格式
            from memory_bread_ipc import IpcRequest, EmbedRequest

            embed_req = EmbedRequest(
                capture_id=capture_id,
                texts=[text]  # 注意：texts 是列表
            )

            req = IpcRequest(
                id=f"bg_{capture_id}",
                ts=int(time.time() * 1000),
                task=embed_req
            )
            response = await worker.handle(req)

            if response.status == "ok":
                from embedding.vector_storage import get_vector_storage

                vectors = response.result.vectors
                if vectors and len(vectors) > 0:
                    storage = get_vector_storage()
                    success = storage.store_vector(
                        capture_id=capture_id,
                        text=text,
                        vector=vectors[0],
                        metadata={
                            "doc_key": f"capture:{capture_id}",
                            "source_type": "capture",
                            "ts": capture.get('ts') or req.ts,
                            "timestamp": req.ts,
                            "app_name": capture.get('app_name'),
                            "win_title": capture.get('window_title'),
                        }
                    )

                    if success:
                        logger.info(f"✅ 向量化+存储完成: capture_id={capture_id}")
                        return True
                    else:
                        logger.warning(f"⚠️ 向量存储失败，继续时间线提炼: capture_id={capture_id}")
                        return False
                else:
                    logger.warning(f"⚠️  向量化返回空结果: capture_id={capture_id}")
                    return False
            else:
                logger.error(f"❌ 向量化失败: capture_id={capture_id}, error={response.error}")
                return False

        except Exception as e:
            logger.error(f"❌ 向量化异常: capture_id={capture_id}, error={e}")
            return False

    async def _process_knowledge_vectorization(self, group: list[dict], knowledge_id: int, knowledge: dict) -> bool:
        """对知识条目执行向量化并写入向量索引。"""
        try:
            text = self._build_knowledge_embedding_text(group, knowledge)
            if not text:
                return False

            worker = self._get_embed_worker()
            if not worker or not worker._model:
                logger.error("❌ 向量模型未就绪，跳过知识向量化: knowledge_id=%s", knowledge_id)
                return False

            from memory_bread_ipc import IpcRequest, EmbedRequest
            from embedding.vector_storage import get_vector_storage

            primary_capture_id = group[0]['id'] if group else int(knowledge.get('capture_id') or 0)
            embed_req = EmbedRequest(
                capture_id=primary_capture_id,
                texts=[text],
            )
            req = IpcRequest(
                id=f"bg_knowledge_{knowledge_id}",
                ts=int(time.time() * 1000),
                task=embed_req,
            )
            response = await worker.handle(req)
            if response.status != "ok" or not response.result.vectors:
                logger.warning("知识向量化失败: knowledge_id=%s error=%s", knowledge_id, response.error)
                return False

            success = get_vector_storage().store_vector(
                capture_id=primary_capture_id,
                text=text,
                vector=response.result.vectors[0],
                metadata={
                    "doc_key": f"knowledge:{knowledge_id}",
                    "source_type": "knowledge",
                    "knowledge_id": knowledge_id,
                    "start_time": knowledge.get('start_time'),
                    "end_time": knowledge.get('end_time'),
                    "observed_at": knowledge.get('observed_at') or knowledge.get('end_time') or knowledge.get('start_time'),
                    "event_time_start": knowledge.get('event_time_start'),
                    "event_time_end": knowledge.get('event_time_end'),
                    "history_view": knowledge.get('history_view', False),
                    "content_origin": knowledge.get('content_origin'),
                    "activity_type": knowledge.get('activity_type'),
                    "is_self_generated": knowledge.get('is_self_generated', False),
                    "evidence_strength": knowledge.get('evidence_strength'),
                    "app_name": knowledge.get('frag_app_name') or (group[0].get('app_name') if group else None),
                    "win_title": knowledge.get('frag_win_title') or (group[0].get('window_title') if group else None),
                    "category": knowledge.get('category', '其他'),
                    "user_verified": False,
                },
            )
            if success:
                logger.info("✅ 知识向量化完成: knowledge_id=%s", knowledge_id)
            return success
        except Exception as exc:
            logger.warning("知识向量化异常: knowledge_id=%s error=%s", knowledge_id, exc)
            return False

    async def _process_knowledge_extraction(self, capture_data: dict):
        """处理单条记录的时间线提炼"""
        try:
            extractor = self._get_knowledge_extractor()

            # 打开数据库连接用于去重
            conn = sqlite3.connect(self.db_path)

            # 使用同步方法提炼（V2 版本）—— 走 InferenceQueue 统一调度
            from inference_queue import LANE_P1_CAPTURE, Priority, get_global_queue, QueueEvictedError
            try:
                knowledge = get_global_queue().submit_sync(
                    Priority.P1,
                    lambda: extractor.extract_sync(capture_data, db_conn=conn),
                    timeout=600.0,
                    lane=LANE_P1_CAPTURE,
                )
            except QueueEvictedError as ee:
                logger.warning(f"extract_sync 被队列淘汰: {ee}")
                conn.close()
                return False

            if knowledge:
                self._apply_document_metadata_defaults(knowledge, [capture_data])
                merged_timeline_id = knowledge.get('_merged_timeline_id')
                if merged_timeline_id:
                    merged_capture_ids = self._timeline_member_capture_ids(
                        conn,
                        int(merged_timeline_id),
                    )
                    if capture_data['id'] not in merged_capture_ids:
                        merged_capture_ids.append(capture_data['id'])
                    self._save_timeline_data_facts(
                        conn,
                        int(merged_timeline_id),
                        knowledge,
                        merged_capture_ids,
                    )
                    self._register_timeline_data_pages(
                        int(merged_timeline_id), knowledge, [capture_data]
                    )
                    conn.commit()
                    self._mark_captures_processed(
                        conn,
                        [capture_data['id']],
                        int(merged_timeline_id),
                    )
                    conn.close()
                    logger.info(
                        "✅ 单条采集已合并并保存结构化事实: capture_id=%s timeline_id=%s",
                        capture_data['id'],
                        merged_timeline_id,
                    )
                    return True
                # 保存到数据库
                cursor = conn.cursor()
                self._ensure_timeline_work_columns(conn)

                # 支持新旧两种格式
                overview = knowledge.get('overview') or knowledge.get('summary', '')
                details = knowledge.get('details', '')
                current_time_ms = int(time.time() * 1000)

                cursor.execute("""
                    INSERT INTO timelines
                    (
                        capture_id, summary, overview, details, entities, category, importance, occurrence_count,
                        observed_at, event_time_start, event_time_end, history_view, content_origin,
                        activity_type, is_self_generated, evidence_strength,
                        work_item, work_status, work_progress, created_at_ms, updated_at_ms
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    capture_data['id'],
                    overview,  # 保持向后兼容
                    overview,
                    details,
                    knowledge.get('entities', '[]'),
                    knowledge.get('category', '其他'),
                    knowledge.get('importance', 3),
                    knowledge.get('occurrence_count', 1),
                    knowledge.get('observed_at') or capture_data.get('ts'),
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
                    current_time_ms,
                    current_time_ms,
                ))

                timeline_id = cursor.lastrowid
                self._save_timeline_data_facts(
                    conn,
                    timeline_id,
                    knowledge,
                    [capture_data['id']],
                )
                self._register_timeline_data_pages(timeline_id, knowledge, [capture_data])
                conn.commit()
                self._mark_captures_processed(
                    conn,
                    [capture_data['id']],
                    timeline_id,
                )
                conn.close()

                logger.info(f"✅ 时间线提炼完成: capture_id={capture_data['id']}, category={knowledge.get('category')}")
                return True
            else:
                conn.close()
                logger.debug(f"⏭️  跳过无价值或重复内容: capture_id={capture_data['id']}")
                return False

        except Exception as e:
            logger.error(f"❌ 时间线提炼异常: capture_id={capture_data['id']}, error={e}")
            return False

    async def _process_batch(
        self,
        *,
        limit: int,
        max_groups: Optional[int],
        trigger_bake: bool,
        bake_limit: int,
        bake_concurrency: int,
    ) -> dict:
        """处理一批未处理的记录（基于语义分组）"""
        try:
            return await self.run_once(
                limit_override=limit,
                max_groups=max_groups,
                trigger_bake=trigger_bake,
                bake_limit=bake_limit,
                bake_concurrency=bake_concurrency,
            )
        except Exception as e:
            logger.error(f"批处理异常: {e}")
            return {
                "fetched_count": 0,
                "processed_count": 0,
                "remaining_estimate": 0,
                "reason": "batch_error",
            }

    def _has_active_bake_run(self) -> bool:
        """检查是否有未完成的 bake run，避免重复触发空转请求。

        查询失败或表不存在时返回 False，退回原有触发路径。
        """
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM bake_runs WHERE status = 'running'"
                ).fetchone()
            finally:
                conn.close()
            return bool(row and row[0] > 0)
        except (sqlite3.Error, OSError):
            return False

    def _has_pending_bake_timelines(self) -> bool:
        """兼容旧调用；候选口径以 Core queue-status 为唯一真相源。"""
        status = self._get_bake_queue_status()
        return bool(status and int(status.get("actionable_count") or 0) > 0)

    async def _maybe_trigger_periodic_bake(
        self,
        *,
        limit: int,
        max_concurrency: int,
    ) -> dict:
        """周期性主动触发 bake，处理因没有新 capture 而积压的 pending timeline。"""
        logger.info("🔁 检测到积压的 pending timeline，主动触发 bake pipeline")
        result = await self._trigger_unified_bake_pipeline(
            processed_count=limit,
            force=True,
            limit_override=limit,
            max_concurrency=max_concurrency,
        )
        if result.get("triggered"):
            logger.info(
                "周期性 bake 已触发: run_id=%s",
                result.get("run_id"),
            )
        else:
            logger.warning("周期性 bake 触发失败: %s", result.get("reason"))
        return result

    async def _run_periodic_bake_check(
        self,
        profile,
        next_check_ts: float,
        *,
        now: Optional[float] = None,
        pending_capture_count: int = 0,
    ) -> tuple[float, bool]:
        """尝试触发 bake，返回（下次检查时间，本轮是否应让出 capture 槽位）。

        这个检查既要放在 capture 批处理前，也要保留在批处理后。
        充电追赶模式一批最多会提炼 100 条 capture；若只在批末检查，
        长批次会让已到期的 bake 积压继续等待十几分钟。
        """
        check_ts = time.monotonic() if now is None else float(now)
        if check_ts < next_check_ts:
            return next_check_ts, False

        # capture 优先：capture 严重积压且 bake 队列很小时，周期性 bake 让位，
        # 把模型槽全部还给提炼；pending 降回恢复阈值以下后自动恢复。
        if (
            pending_capture_count >= _CAPTURE_PRIORITY_PENDING_THRESHOLD
            and self._latest_bake_actionable_count < _CAPTURE_PRIORITY_BAKE_CEILING
        ):
            if not self._bake_yield_to_capture:
                logger.info(
                    "🔁 capture 优先：pending_capture=%s 已达阈值且 actionable_bake=%s 低于 %s，"
                    "暂停周期性 bake 触发，模型槽让给提炼",
                    pending_capture_count,
                    self._latest_bake_actionable_count,
                    _CAPTURE_PRIORITY_BAKE_CEILING,
                )
            self._bake_yield_to_capture = True
        elif pending_capture_count < _CAPTURE_PRIORITY_RESUME_THRESHOLD:
            if self._bake_yield_to_capture:
                logger.info(
                    "🔁 capture 积压已降到 %s 以下，恢复周期性 bake 触发",
                    _CAPTURE_PRIORITY_RESUME_THRESHOLD,
                )
            self._bake_yield_to_capture = False
        if self._bake_yield_to_capture:
            return (
                check_ts
                + self._scheduled_bake_interval_secs(
                    profile,
                    pending_capture_count,
                    self._latest_bake_actionable_count,
                ),
                False,
            )

        # 连续无进展时，前一轮已经把 next_check_ts 推到指数退避之后。
        # 到达这里说明退避已到期，必须允许一次半开探测；若继续直接跳过，
        # 计数永远不会被新进展清零，重启前都会永久锁死。
        if self._consecutive_no_progress_bake_runs >= _NO_PROGRESS_BAKE_SKIP_THRESHOLD:
            backoff_secs = min(
                _NO_PROGRESS_BAKE_BACKOFF_BASE_SECS
                * (2 ** self._consecutive_no_progress_bake_runs),
                _NO_PROGRESS_BAKE_MAX_BACKOFF_SECS,
            )
            logger.info(
                "🔎 连续 %s 次 bake 触发零进展，%.0fs 退避已到期，执行一次半开探测",
                self._consecutive_no_progress_bake_runs,
                backoff_secs,
            )

        bake_limit = self._scheduled_bake_limit(profile, pending_capture_count)
        bake_result = await self._maybe_trigger_periodic_bake(
            limit=bake_limit,
            max_concurrency=profile.bake_concurrency,
        )
        actionable_count = int(bake_result.get("actionable_count") or 0)
        if "actionable_count" in bake_result:
            self._latest_bake_actionable_count = actionable_count
        no_progress_count = int(bake_result.get("recent_no_progress_count") or 0)
        # 上一次触发的 run 被本轮 queue-status 证实零进展（recent_no_progress
        # 增长）则累加；队列恢复进展后清零，避免永久锁死触发。
        if (
            self._bake_trigger_watch
            and no_progress_count > self._last_bake_no_progress_count
        ):
            self._consecutive_no_progress_bake_runs += 1
        elif no_progress_count == 0:
            self._consecutive_no_progress_bake_runs = 0
        self._last_bake_no_progress_count = no_progress_count
        scheduled_interval_secs = self._scheduled_bake_interval_secs(
            profile,
            pending_capture_count,
            actionable_count,
        )
        retry_after_secs = max(
            scheduled_interval_secs,
            int(bake_result.get("retry_after_ms") or 0) / 1000.0,
        )
        if no_progress_count >= 2:
            # 队列持续报无进展时指数退避（15s×2^n，上限 15 分钟），
            # 覆盖 accepted/no_op 两种场景，不再固定短间隔重复触发。
            retry_after_secs = max(
                retry_after_secs,
                min(
                    _NO_PROGRESS_BAKE_BACKOFF_BASE_SECS * (2 ** no_progress_count),
                    _NO_PROGRESS_BAKE_MAX_BACKOFF_SECS,
                ),
            )
        triggered = bool(bake_result.get("triggered"))
        self._bake_trigger_watch = triggered
        reason = bake_result.get("reason")
        backlog_burst = (
            actionable_count >= _BAKE_BACKLOG_BURST_THRESHOLD
            or self._is_dual_backlog_pressure(
                pending_capture_count,
                actionable_count,
            )
        )
        burst_run_limit = self._bake_burst_run_limit(
            pending_capture_count,
            actionable_count,
        )

        if triggered:
            if backlog_burst:
                self._consecutive_backlog_bake_runs += 1
            else:
                self._consecutive_backlog_bake_runs = 0

        # 单边 bake 积压时保留原来的 bounded burst；双边积压时一次小 bake run
        # 就轮到一个有界 capture 工作片，避免两侧任一批次长期独占模型。
        hold_capture = triggered
        if (
            triggered
            and backlog_burst
            and self._consecutive_backlog_bake_runs
            >= burst_run_limit
        ):
            hold_capture = False
        if reason == "run_in_progress" and backlog_burst:
            hold_capture = (
                self._consecutive_backlog_bake_runs
                < burst_run_limit
            )
        if no_progress_count >= 2:
            # 无进展的 run 无权抢占 capture 提炼的模型槽。
            hold_capture = False
        return check_ts + retry_after_secs, hold_capture

    def _enforce_runtime_guard(self, reason: str) -> None:
        """巡检托管推理运行时，保证全局最多 1 个 llama-server（孤儿一律回收）。"""
        try:
            from runtime_process_guard import enforce_runtime_guards
            summary = enforce_runtime_guards(reason=reason)
            if summary.get("killed_runners") or summary.get("killed_serves"):
                logger.warning(
                    "进程守卫收敛: runners=%s serves=%s（%s）",
                    summary.get("killed_runners"),
                    summary.get("killed_serves"),
                    reason,
                )
        except Exception as exc:
            logger.warning("进程守卫巡检失败（忽略）: %s", exc)

    async def run(self):
        """运行后台处理循环"""
        self.running = True
        logger.info(f"🚀 后台处理器启动 (间隔={self.interval}s, 批量={self.batch_size})")

        _next_periodic_bake_check_ts: float = 0.0
        # 启动后的首要任务是消费采集/bake 积压。向量补齐和一致性审计属于
        # 可延后维护，不能在启动阶段先占用数分钟，让 LLM 有工作却空闲。
        _maintenance_started_at = time.monotonic()
        _last_artifact_vector_check_ts: float = _maintenance_started_at
        _last_vector_consistency_audit_ts: float = _maintenance_started_at
        _last_runtime_guard_ts: float = 0.0

        # 启动即清扫一次：上次退出可能遗留孤儿 llama-server（宿主被杀后 reparent 到 launchd）。
        await asyncio.to_thread(self._enforce_runtime_guard, "startup sweep")
        _last_runtime_guard_ts = time.monotonic()

        while self.running:
            sleep_secs = self.interval
            try:
                self._touch_status_file()
                await asyncio.to_thread(self._drain_vector_deletion_queue)

                now = time.monotonic()
                has_known_extraction_backlog = (
                    self._latest_pending_capture_count > 0
                    or self._latest_bake_actionable_count > 0
                )
                if (
                    not has_known_extraction_backlog
                    and now - _last_vector_consistency_audit_ts
                    >= _VECTOR_CONSISTENCY_AUDIT_INTERVAL_SECS
                ):
                    audit = await asyncio.to_thread(self._audit_vector_consistency)
                    if audit.get("available"):
                        _last_vector_consistency_audit_ts = now
                        if audit.get("missing_count") or audit.get("orphan_count"):
                            logger.warning(
                                "向量一致性审计: missing=%s repaired_artifacts=%s "
                                "orphans=%s（孤儿仅报告，不自动删除）",
                                audit.get("missing_count"),
                                audit.get("missing_artifacts_marked_for_rebuild"),
                                audit.get("orphan_count"),
                            )
                if (
                    not has_known_extraction_backlog
                    and now - _last_artifact_vector_check_ts
                    >= _ARTIFACT_VECTOR_CHECK_INTERVAL_SECS
                ):
                    await self.backfill_bake_document_vectors(limit=64)
                    _last_artifact_vector_check_ts = now

                if now - _last_runtime_guard_ts >= _RUNTIME_GUARD_INTERVAL_SECS:
                    await asyncio.to_thread(self._enforce_runtime_guard, "periodic sweep")
                    _last_runtime_guard_ts = now

                if not self._capture_and_extraction_enabled():
                    logger.debug("采集与自动提炼已暂停，跳过本轮后台处理")
                    await asyncio.sleep(self.interval)
                    continue

                profile = self.energy_policy.current_profile(
                    base_timeline_interval_secs=self.interval,
                    base_timeline_batch_size=self.batch_size,
                )
                sleep_secs = profile.timeline_interval_secs
                if profile.mode != self._last_energy_mode:
                    logger.info(
                        "后台节能档位切换: mode=%s saving=%s plugged=%s battery=%s "
                        "timeline_interval=%ss timeline_batch=%s bake_interval=%ss "
                        "bake_limit=%s bake_concurrency=%s",
                        profile.mode,
                        profile.saving_enabled,
                        profile.on_external_power,
                        profile.battery_percent,
                        profile.timeline_interval_secs,
                        profile.timeline_batch_size,
                        profile.bake_interval_secs,
                        profile.bake_limit,
                        profile.bake_concurrency,
                    )
                    self._last_energy_mode = profile.mode

                if not profile.allow_background_extraction:
                    logger.info(
                        "低电量节能档位暂停后台时间线与 bake 提炼: battery=%s%%",
                        profile.battery_percent,
                    )
                    await asyncio.sleep(sleep_secs)
                    continue

                pending_before = await asyncio.to_thread(
                    self._count_unprocessed_captures
                )
                self._latest_pending_capture_count = pending_before

                # 先给已到期的 bake 一次调度机会，再进入 capture 工作片。
                (
                    _next_periodic_bake_check_ts,
                    preflight_bake_holds_capture,
                ) = await self._run_periodic_bake_check(
                    profile,
                    _next_periodic_bake_check_ts,
                    pending_capture_count=pending_before,
                )
                if preflight_bake_holds_capture:
                    # Core 是异步 accepted：此时 P2 请求可能尚未拿到跨进程
                    # 单模型槽。本轮若立即提交 P1 capture，会在优先级上
                    # 反超 bake，造成“触发成功但水位仍不推进”。
                    logger.info(
                        "周期性 bake 已接受或仍在运行，本轮让出 capture 批处理的模型槽"
                    )
                    wait_until_check = max(
                        1.0,
                        _next_periodic_bake_check_ts - time.monotonic(),
                    )
                    await asyncio.sleep(min(sleep_secs, wait_until_check))
                    continue

                burst_run_limit = self._bake_burst_run_limit(
                    pending_before,
                    self._latest_bake_actionable_count,
                )
                if (
                    self._consecutive_backlog_bake_runs
                    >= burst_run_limit
                ):
                    logger.info(
                        "bake 工作片已运行 %s 批，本轮恢复 capture 公平配额",
                        self._consecutive_backlog_bake_runs,
                    )
                    self._consecutive_backlog_bake_runs = 0

                maximum_throughput = profile.mode in {"charging", "unrestricted"}
                timeline_batch_limit = self._timeline_batch_limit(profile, pending_before)
                capture_group_quantum = self._capture_group_quantum(
                    pending_before,
                    self._latest_bake_actionable_count,
                )
                scheduled_bake_limit = self._scheduled_bake_limit(
                    profile,
                    pending_before,
                )
                batch_result = await self._process_batch(
                    limit=timeline_batch_limit,
                    max_groups=capture_group_quantum,
                    trigger_bake=maximum_throughput,
                    bake_limit=scheduled_bake_limit,
                    bake_concurrency=profile.bake_concurrency,
                )
                processed = int(batch_result.get('processed_count', 0))
                self._touch_status_file()

                data_extraction_due = (
                    processed > 0
                    or time.monotonic() - self._last_data_extraction_at
                    >= _DATA_EXTRACTION_INTERVAL_SECS
                )
                if data_extraction_due:
                    batch_result['data_extraction'] = await self._trigger_data_extraction(
                        limit=max(100, min(200, timeline_batch_limit * 4)),
                    )

                if processed > 0:
                    logger.info(f"✅ 本轮处理完成: {processed} 条记录")
                bake_trigger = batch_result.get('bake_trigger') or {}
                if "actionable_count" in bake_trigger:
                    self._latest_bake_actionable_count = int(
                        bake_trigger.get("actionable_count") or 0
                    )
                if bake_trigger.get('triggered'):
                    scheduled_interval_secs = self._scheduled_bake_interval_secs(
                        profile,
                        pending_before,
                        self._latest_bake_actionable_count,
                    )
                    _next_periodic_bake_check_ts = (
                        time.monotonic() + scheduled_interval_secs
                    )
                pending_after = await asyncio.to_thread(
                    self._count_unprocessed_captures
                )
                self._latest_pending_capture_count = pending_after
                if maximum_throughput and pending_before > profile.timeline_batch_size:
                    if self._should_continue_charging_catchup(
                        profile,
                        pending_before,
                        pending_after,
                        batch_result,
                    ):
                        sleep_secs = _CHARGING_CATCHUP_SLEEP_SECS

                dual_backlog_pressure = self._is_dual_backlog_pressure(
                    pending_after,
                    self._latest_bake_actionable_count,
                )
                if dual_backlog_pressure:
                    # capture 工作片结束后强制重新对账 bake 状态，不能因为充电
                    # catch-up 的 1 秒间隔连续提交下一批 P1 请求。
                    _next_periodic_bake_check_ts = 0.0

                # 长批次结束后再检查一次，方便在本轮刚产出新
                # timeline 或前一个 run 刚完成时立即续跑。
                (
                    _next_periodic_bake_check_ts,
                    postflight_bake_holds_capture,
                ) = (
                    await self._run_periodic_bake_check(
                        profile,
                        _next_periodic_bake_check_ts,
                        pending_capture_count=pending_after,
                    )
                )
                if postflight_bake_holds_capture:
                    wait_until_check = max(
                        1.0,
                        _next_periodic_bake_check_ts - time.monotonic(),
                    )
                    sleep_secs = min(
                        sleep_secs,
                        wait_until_check,
                    )

                # 等待下一轮
                await asyncio.sleep(sleep_secs)

            except Exception as e:
                logger.error(f"后台处理循环异常: {e}")
                self._touch_status_file()
                await asyncio.sleep(sleep_secs)

    def stop(self):
        """停止后台处理器"""
        logger.info("⏹️  停止后台处理器")
        self.running = False
