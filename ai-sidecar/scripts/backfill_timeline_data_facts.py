#!/usr/bin/env python3
"""历史时间线 data_facts 回填脚本。

背景：timeline_data_facts 管道（当前 timeline-data-fact.v3 契约）上线晚于大量
历史时间线的创建时间（如 timeline 2008，其中包含完整的项目进度汇报但
0 条数据事实），且主流程没有回填机制。本脚本扫描"无 data_facts 但成员
采集含密集数值正文"的历史时间线，只补跑 data_facts 契约提取并落库，
不改动时间线本身的 summary/details。

用法：
    python3 scripts/backfill_timeline_data_facts.py --timeline-id 2008 --dry-run
    python3 scripts/backfill_timeline_data_facts.py --limit 20
    python3 scripts/backfill_timeline_data_facts.py --db /path/to/memory-bread.db

Python 3.9 兼容（不使用 X | Y 联合类型与 match/case）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_data_facts")

OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_DB_PATH = Path.home() / ".memory-bread" / "memory-bread.db"

# 数值指标特征：除百分比/小数外，覆盖配置参数、复合时长、数量和宽高比。
# 这里只筛选值得补提炼的候选；事实状态和场景完整性仍由模型契约与校验层判断。
_NUMERIC_METRIC_RE = re.compile(
    r"(?:\d+(?:[.,]\d+)?\s*(?:%|％|毫秒|秒|分(?:钟)?|小时|天|元|万|亿|"
    r"usd|gb|tb|mb|kb|qps|倍|张|条|个|次|人|份|项))|"
    r"(?:\d+\s*[:：×xX]\s*\d+)",
    re.IGNORECASE,
)
# 强单位/比例/宽高比信号命中一条即可进入补提炼，最终仍 fail-closed 校验。
_MIN_NUMERIC_HITS = 1

_QUOTA_DENSE = 3000
_QUOTA_DEFAULT = 800
# 离线回填无实时提炼约束，预算高于主流程，尽量保留成员正文完整性
_TOTAL_MAX_CHARS = 12000

SYSTEM_PROMPT_PREFIX = (
    "你是一个数据事实提取助手。你的唯一任务是从输入的采集文本中提取结构化数据事实，"
    "只输出 JSON，不要输出任何其他内容。\n"
)


def _build_candidate_text(captures: List[Dict[str, Any]]) -> str:
    """按密度感知配额拼接成员采集文本（与主流程截断策略一致）。"""
    from knowledge.extractor_v2 import (
        _sanitize_capture_text,
        _strip_pressure_noise_lines,
        _truncate_preserving_metrics,
    )
    from knowledge.fragment_grouper import text_density_score, DENSE_TEXT_THRESHOLD

    bodies: List[Tuple[float, str]] = []
    seen_bodies = set()
    for cap in captures:
        text = str(
            cap.get("ax_text")
            or cap.get("ocr_text")
            or cap.get("input_text")
            or cap.get("audio_text")
            or ""
        )
        sanitized = _sanitize_capture_text(text)
        if not sanitized.strip():
            continue
        if sanitized in seen_bodies:
            continue
        seen_bodies.add(sanitized)
        density = text_density_score(sanitized)
        quota = _QUOTA_DENSE if density >= DENSE_TEXT_THRESHOLD else _QUOTA_DEFAULT
        bodies.append((density, _truncate_preserving_metrics(sanitized, quota)))

    if not bodies:
        return ""

    def joined() -> str:
        return "\n\n---\n\n".join(body for _, body in bodies)

    merged = joined()
    if len(merged) > _TOTAL_MAX_CHARS:
        bodies = [(d, _strip_pressure_noise_lines(b)) for d, b in bodies]
        merged = joined()
    if len(merged) > _TOTAL_MAX_CHARS:
        # 阶段3：按密度加权等比缩减（密集正文保留更多，保底 250 字）
        body_budget = _TOTAL_MAX_CHARS - 12 * max(0, len(bodies) - 1)
        total_body_len = sum(len(b) for _, b in bodies)
        if body_budget > 0 and total_body_len > body_budget:
            weights = [max(d, 0.05) for d, _ in bodies]
            weight_sum = sum(weights) or 1.0
            new_bodies: List[Tuple[float, str]] = []
            for (density, body), w in zip(bodies, weights):
                share = int(body_budget * w / weight_sum)
                cap = max(min(len(body), share), 250)
                if len(body) > cap:
                    body = _truncate_preserving_metrics(body, cap)
                new_bodies.append((density, body))
            bodies = new_bodies
            merged = joined()
    if len(merged) > _TOTAL_MAX_CHARS:
        merged = merged[:_TOTAL_MAX_CHARS]
    return merged


def _timeline_member_captures(conn: sqlite3.Connection, timeline_id: int) -> List[Dict[str, Any]]:
    member_ids: List[int] = []

    row = conn.execute(
        "SELECT capture_id, capture_ids FROM timelines WHERE id = ?", (timeline_id,)
    ).fetchone()
    if not row:
        return []
    if row[0]:
        member_ids.append(int(row[0]))
    try:
        for cid in json.loads(row[1] or "[]"):
            cid_int = int(cid)
            if cid_int not in member_ids:
                member_ids.append(cid_int)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    for linked in conn.execute(
        "SELECT id FROM captures WHERE timeline_id = ? ORDER BY ts ASC, id ASC",
        (timeline_id,),
    ).fetchall():
        if linked[0] not in member_ids:
            member_ids.append(linked[0])

    if not member_ids:
        return []
    placeholders = ",".join("?" for _ in member_ids)
    rows = conn.execute(
        f"""
        SELECT id, ts, app_name, win_title, ocr_text, ax_text, input_text, audio_text
        FROM captures WHERE id IN ({placeholders}) ORDER BY ts ASC
        """,
        member_ids,
    ).fetchall()
    return [
        {
            "id": r[0],
            "ts": r[1],
            "app_name": r[2],
            "window_title": r[3],
            "ocr_text": r[4],
            "ax_text": r[5],
            "input_text": r[6],
            "audio_text": r[7],
        }
        for r in rows
    ]


def _timeline_context_hint(conn: sqlite3.Connection, timeline_id: int) -> str:
    """读取已落库的工作语境；仅辅助标题/场景，不作为逐字证据。"""
    row = conn.execute(
        "SELECT summary, overview, details FROM timelines WHERE id = ?",
        (timeline_id,),
    ).fetchone()
    if not row:
        return ""
    parts: List[str] = []
    for raw in row:
        value = " ".join(str(raw or "").split())
        if value and value not in parts:
            parts.append(value)
    return "\n".join(parts)[:1200]


def _select_candidates(conn: sqlite3.Connection, limit: int) -> List[int]:
    rows = conn.execute(
        """
        SELECT t.id FROM timelines t
        WHERE NOT EXISTS (
            SELECT 1 FROM timeline_data_facts f WHERE f.timeline_id = t.id
        )
        ORDER BY t.id DESC
        LIMIT ?
        """,
        (max(limit * 10, 200),),
    ).fetchall()
    candidates: List[int] = []
    for (timeline_id,) in rows:
        captures = _timeline_member_captures(conn, timeline_id)
        text = _build_candidate_text(captures)
        if not text:
            continue
        if len(_NUMERIC_METRIC_RE.findall(text)) >= _MIN_NUMERIC_HITS:
            candidates.append(timeline_id)
        if len(candidates) >= limit:
            break
    return candidates


def _resolve_model(model: Optional[str]) -> str:
    if model:
        return model
    try:
        from model_registry_global import get_active_ollama_model
        return get_active_ollama_model()
    except Exception as e:
        logger.warning("无法获取全局模型名，回退默认值: %s", e)
        return "qwen3:4b"


def _call_llm(model: str, system_prompt: str, user_prompt: str, timeout: int = 1200) -> str:
    import requests

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": True,
        "think": False,
        "keep_alive": "10m",
        "format": "json",
        # 与主流程 data_facts 提炼对齐，避免默认 num_predict 截断 JSON
        "options": {
            # 低温保证逐字引用稳定；小模型高温下易改写原文导致回证失败
            "temperature": 0.1,
            "num_ctx": 32768,
            "num_predict": 8192,
            # 防重复循环：小模型在长输出下易陷入同一事实复读，吃光 token 预算
            "repeat_penalty": 1.15,
        },
    }
    parts: List[str] = []
    accumulated_len = 0
    last_repetition_check = 0
    with requests.post(
        f"{OLLAMA_BASE_URL}/api/chat",
        json=payload,
        timeout=timeout,
        stream=True,
    ) as response:
        response.raise_for_status()
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            try:
                chunk = json.loads(raw_line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            message = chunk.get("message") or {}
            if message.get("content"):
                piece = str(message["content"])
                parts.append(piece)
                accumulated_len += len(piece)
                if (
                    accumulated_len >= 3000
                    and accumulated_len - last_repetition_check >= 1024
                ):
                    last_repetition_check = accumulated_len
                    joined = "".join(parts)
                    window = joined[-160:]
                    if window.strip() and joined.count(window) >= 4:
                        logger.warning("回填模型输出陷入重复退化，提前停止并抢救完整事实")
                        break
    return "".join(parts)


def _persist_facts(
    conn: sqlite3.Connection,
    timeline_id: int,
    facts: List[Dict[str, Any]],
    rejected_count: int,
    capture_ids: List[int],
) -> int:
    """与 BackgroundProcessor._save_timeline_data_facts 相同的落库逻辑。"""
    from knowledge.extractor_v2 import DATA_FACT_CONTRACT_VERSION

    now_ms = int(time.time() * 1000)
    week_ms = 7 * 24 * 60 * 60 * 1000
    first_monday_ms = 4 * 24 * 60 * 60 * 1000
    capture_ids_json = json.dumps(capture_ids, ensure_ascii=False)
    inserted = 0

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
        (timeline_id, DATA_FACT_CONTRACT_VERSION, rejected_count, now_ms, now_ms),
    )
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        identity = "\x1f".join(
            str(fact.get(field) or "").strip().casefold()
            for field in ("subject", "action", "target_context", "metric")
        )
        fact_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        observed_at = fact.get("observed_at")
        period_observed_at = int(observed_at or now_ms)
        period_start_at = (
            (period_observed_at - first_monday_ms) // week_ms
        ) * week_ms + first_monday_ms
        period_end_at = period_start_at + week_ms - 1
        period_key = f"week:{period_start_at}"
        cursor = conn.execute(
            """
            INSERT INTO timeline_data_facts (
                timeline_id, fact_key, title, subject, action, target_context,
                dimension, metric, value, unit, statement, evidence_quote,
                confidence, observed_at, period_granularity, period_key,
                period_start_at, period_end_at, source_capture_ids, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'week', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(timeline_id, fact_key, dimension, value, unit) DO UPDATE SET
                title = excluded.title,
                statement = excluded.statement,
                evidence_quote = excluded.evidence_quote,
                confidence = excluded.confidence,
                observed_at = COALESCE(excluded.observed_at, timeline_data_facts.observed_at),
                source_capture_ids = excluded.source_capture_ids,
                updated_at = excluded.updated_at
            """,
            (
                timeline_id,
                fact_key,
                str(fact.get("title") or ""),
                str(fact.get("subject") or ""),
                str(fact.get("action") or ""),
                str(fact.get("target_context") or ""),
                str(fact.get("dimension") or ""),
                str(fact.get("metric") or ""),
                str(fact.get("value") or ""),
                str(fact.get("unit") or ""),
                str(fact.get("statement") or ""),
                str(fact.get("evidence_quote") or ""),
                str(fact.get("confidence") or ""),
                observed_at,
                period_key,
                period_start_at,
                period_end_at,
                capture_ids_json,
                now_ms,
                now_ms,
            ),
        )
        if cursor.rowcount:
            inserted += 1
    conn.execute(
        """
        UPDATE timeline_data_fact_runs
        SET accepted_count = accepted_count + ?, updated_at = ?
        WHERE timeline_id = ?
        """,
        (len(facts), now_ms, timeline_id),
    )
    conn.commit()
    return inserted


def _salvage_truncated_facts(content: str) -> Optional[Dict[str, Any]]:
    """输出被 num_predict 截断时，抢救 data_facts 数组中已闭合的完整事实对象。"""
    start = content.find('"data_facts"')
    if start == -1:
        return None
    arr_start = content.find('[', start)
    if arr_start == -1:
        return None
    from knowledge.extractor_v2 import _extract_json_object

    facts: List[Dict[str, Any]] = []
    i = arr_start + 1
    n = len(content)
    while i < n:
        while i < n and content[i] in ' \t\r\n,':
            i += 1
        if i >= n or content[i] != '{':
            break
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            ch = content[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if j >= n or depth != 0:
            break  # 末尾对象不完整，丢弃
        obj = _extract_json_object(content[i:j + 1])
        if isinstance(obj, dict):
            facts.append(obj)
        i = j + 1
    if not facts:
        return None
    return {"data_facts": facts}


def _parse_llm_facts(content: str) -> Optional[Dict[str, Any]]:
    """解析 LLM 输出；完整 JSON 失败时抢救截断数组中的完整对象。"""
    from knowledge.extractor_v2 import _extract_json_object

    parsed = _extract_json_object(content)
    if parsed:
        return parsed
    return _salvage_truncated_facts(content)


_CHUNK_MAX_MEMBERS = 3
_CHUNK_MAX_ATTEMPTS = 2  # 小模型逐字引用不稳定，失败块重试并合并结果


def _dedupe_captures_by_body(captures: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """正文完全相同的成员只保留首次出现（连续同屏重复采集）。"""
    from knowledge.extractor_v2 import _sanitize_capture_text

    deduped: List[Dict[str, Any]] = []
    seen = set()
    for cap in captures:
        text = str(
            cap.get("ax_text")
            or cap.get("ocr_text")
            or cap.get("input_text")
            or cap.get("audio_text")
            or ""
        )
        sanitized = _sanitize_capture_text(text)
        if not sanitized.strip() or sanitized in seen:
            continue
        seen.add(sanitized)
        deduped.append(cap)
    return deduped


def _backfill_one(
    conn: sqlite3.Connection,
    timeline_id: int,
    model: str,
    dry_run: bool,
) -> Dict[str, Any]:
    from knowledge.extractor_v2 import (
        DATA_FACT_PROMPT,
        JSON_OUTPUT_RULES,
        _validated_data_facts,
    )

    captures = _timeline_member_captures(conn, timeline_id)
    text = _build_candidate_text(captures)
    context_hint = _timeline_context_hint(conn, timeline_id)
    result: Dict[str, Any] = {
        "timeline_id": timeline_id,
        "captures": len(captures),
        "text_len": len(text),
        "facts": 0,
        "rejected": 0,
        "status": "skipped",
    }
    if not text or len(_NUMERIC_METRIC_RE.findall(text)) < _MIN_NUMERIC_HITS:
        result["status"] = "no_numeric_content"
        return result

    if dry_run:
        result["status"] = "dry_run"
        logger.info(
            "[dry-run] timeline=%d captures=%d text_len=%d numeric_hits=%d",
            timeline_id,
            len(captures),
            len(text),
            len(_NUMERIC_METRIC_RE.findall(text)),
        )
        return result

    system_prompt = (
        SYSTEM_PROMPT_PREFIX
        + DATA_FACT_PROMPT.replace("（与上述时间线提炼在同一次输出中完成）", "")
        + JSON_OUTPUT_RULES
    )

    # 分块提炼：小模型对长输入容易只看开头、长输出陷入复读，
    # 按 _CHUNK_MAX_MEMBERS 个成员一块分别调用，合并事实后去重落库。
    members = _dedupe_captures_by_body(captures)
    chunks = [
        members[i:i + _CHUNK_MAX_MEMBERS]
        for i in range(0, len(members), _CHUNK_MAX_MEMBERS)
    ]
    all_facts: List[Dict[str, Any]] = []
    total_rejected = 0
    chunk_errors = 0
    for chunk_idx, chunk in enumerate(chunks):
        chunk_text = _build_candidate_text(chunk)
        if not chunk_text or not _NUMERIC_METRIC_RE.search(chunk_text):
            continue
        user_prompt = (
            "以下是历史采集文本，请只提取结构化数据事实，输出 JSON："
            '{"data_facts": [...]}，没有可靠事实时输出 {"data_facts": []}。\n'
            "要求：先提取总体/汇总指标，再按决策价值和证据完整度选择代表性明细，"
            "单次最多输出 24 条；evidence_quote 从原文复制连续片段且不超过"
            "120 字，严禁改写；statement 不超过 60 字；每条事实只输出一次。"
            "工作语境只用于恢复具体任务、产品、动作和目标场景，不能作为 evidence_quote；"
            "模型名、系统名、参数配置、生成控制和过程监控只是执行环境，target_context "
            "必须写参数最终服务的具体交付物或业务用途；复合时长必须完整保留，"
            "同义句与重复截图只输出一条。\n\n"
            f"工作语境：\n{context_hint or '未提供'}\n\n"
            "原始采集证据：\n"
            + chunk_text
        )
        chunk_facts: List[Dict[str, Any]] = []
        chunk_ok = False
        for attempt in range(_CHUNK_MAX_ATTEMPTS):
            try:
                content = _call_llm(model, system_prompt, user_prompt)
            except Exception as e:
                logger.error(
                    "timeline=%d chunk=%d attempt=%d LLM 调用失败: %s",
                    timeline_id, chunk_idx, attempt + 1, e,
                )
                continue
            parsed = _parse_llm_facts(content)
            if not parsed:
                logger.warning(
                    "timeline=%d chunk=%d attempt=%d 输出无法解析（len=%d）",
                    timeline_id, chunk_idx, attempt + 1, len(content),
                )
                continue
            chunk_ok = True
            facts, rejected = _validated_data_facts(
                parsed.get("data_facts"), chunk_text, relaxed_subject=True
            )
            total_rejected += rejected
            chunk_facts.extend(facts)
            logger.info(
                "timeline=%d chunk=%d/%d attempt=%d accepted=%d rejected=%d",
                timeline_id, chunk_idx + 1, len(chunks), attempt + 1, len(facts), rejected,
            )
            # 一次调用已有可接受事实即结束该块；拒绝项已 fail-closed，重复调用
            # 容易让小模型围绕同一长截图继续复读。只有 0 事实时才重试一次。
            if chunk_facts:
                break
        if not chunk_ok:
            chunk_errors += 1
        all_facts.extend(chunk_facts)

    # 同一事实跨块重复输出去重
    deduped_facts: List[Dict[str, Any]] = []
    seen_keys = set()
    for fact in all_facts:
        key = (
            fact.get("subject"), fact.get("action"), fact.get("target_context"),
            fact.get("metric"), fact.get("dimension"), fact.get("value"), fact.get("unit"),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped_facts.append(fact)
    facts = deduped_facts[:24]
    result["facts"] = len(facts)
    result["rejected"] = total_rejected
    if not facts:
        result["status"] = "llm_error" if chunk_errors and chunk_errors == len(chunks) else "no_facts"
        return result

    capture_ids = [int(c["id"]) for c in captures]
    inserted = _persist_facts(conn, timeline_id, facts, total_rejected, capture_ids)
    result["status"] = "ok"
    result["inserted"] = inserted
    logger.info(
        "timeline=%d 回填成功: facts=%d rejected=%d inserted=%d",
        timeline_id,
        len(facts),
        total_rejected,
        inserted,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="历史时间线 data_facts 回填")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="memory-bread.db 路径")
    parser.add_argument("--timeline-id", type=int, action="append", default=None,
                        help="指定时间线 ID（可多次传入）；不传则自动扫描候选")
    parser.add_argument("--limit", type=int, default=10, help="自动扫描模式下最多回填条数")
    parser.add_argument("--model", default=None, help="Ollama 模型名，默认取全局活跃模型")
    parser.add_argument("--dry-run", action="store_true", help="只评估候选，不调用 LLM 不写库")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        logger.error("数据库不存在: %s", db_path)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        if args.timeline_id:
            timeline_ids = list(dict.fromkeys(args.timeline_id))
        else:
            timeline_ids = _select_candidates(conn, args.limit)
            logger.info("自动扫描到 %d 个候选时间线: %s", len(timeline_ids), timeline_ids)

        if not timeline_ids:
            logger.info("无候选时间线")
            return 0

        model = _resolve_model(args.model)
        if not args.dry_run:
            logger.info("使用模型: %s", model)

        summary = []
        for timeline_id in timeline_ids:
            summary.append(_backfill_one(conn, timeline_id, model, args.dry_run))

        ok = [r for r in summary if r["status"] == "ok"]
        logger.info(
            "回填完成: 成功 %d / 总计 %d，新增事实 %d 条",
            len(ok),
            len(summary),
            sum(int(r.get("inserted") or 0) for r in ok),
        )
        for r in summary:
            print(json.dumps(r, ensure_ascii=False))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
