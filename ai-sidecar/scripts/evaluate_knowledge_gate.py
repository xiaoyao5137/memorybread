#!/usr/bin/env python3
"""只读回放知识提炼门禁，对照第一期单分数口径与第二期复用半径口径。

默认 static 模式：只回放库里已存的 payload，不调用模型。存量行没有复用半径维度，
第二期门禁对它们不生效（存量不重算、不降级、不隐藏），因此 static 模式主要用于确认
存量边界、查看第一期口径的判定分布与 reason_code 落点。

replay 模式：用当前 `BAKE_BUNDLE_PROMPT` 对同一批时间线重跑一次 sidecar 提炼，拿到复用
半径维度后再算两套门禁。验收线（发布率区间、种子正误杀、类目发布率差异）以该模式为准。

脚本全程只读：连接串固定 `mode=ro`，不含任何 DDL/DML，也不写回用户数据库；评估结果只
落到 `--output` 指定的 JSON 文件。
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 与 core-engine/src/services/bake_service.rs 的门禁常量保持一一对应。
# 两侧任一改动都必须同步，否则离线回放结论与线上判定会背离。
LEGACY_PUBLISH_SCORE = 0.78
LEGACY_SHADOW_SCORE = 0.62
LEGACY_RULE_VERSION = "knowledge-open-semantic-v1"
PUBLISH_SCORE = 0.72
SHADOW_SCORE = 0.50
DEDUP_WINDOW_DAYS = 14
RULE_VERSION = "knowledge-reuse-radius-v3"

IRREPLACEABILITY_SCORES = {
    "only_here": 1.0,
    # 与第一期的 LEGACY_SHADOW_SCORE = 0.62 无关，勿混用。这一档同时容纳外部学术知识与
    # 已被 commit 承载的过程结论，二者的区分交给受众维度：领域公共知识取
    # anyone_same_domain 时 0.745 仍能发布，而过程型组合 partially + team + weeks + imp4
    # 只有 0.705；抬高本档分值会让后者升到 0.733、只比发布线高 0.013。
    "partially_recoverable": 0.55,
    "authoritative_elsewhere": 0.0,
}
AUDIENCE_LADDER = (
    ("only_me_this_session", 0.0),
    ("me_later", 0.5),
    ("team_or_stakeholders", 0.85),
    ("anyone_same_domain", 1.0),
)
HORIZON_LADDER = (
    ("hours", 0.15),
    ("days", 0.5),
    ("weeks", 0.8),
    ("months_or_more", 1.0),
)
# 分值表与档位都从同一张有序梯派生，与 Rust 侧 ladder_score / ladder_rank 对应。
AUDIENCE_SCORES = dict(AUDIENCE_LADDER)
HORIZON_SCORES = dict(HORIZON_LADDER)
AUDIENCE_RANKS = dict((name, rank) for rank, (name, _) in enumerate(AUDIENCE_LADDER))
HORIZON_RANKS = dict((name, rank) for rank, (name, _) in enumerate(HORIZON_LADDER))
# 发布除了算分够线还必须满足的最低复用半径（必要条件），与 Rust 侧 KNOWLEDGE_MIN_PUBLISH_* 对应。
MIN_PUBLISH_AUDIENCE = "team_or_stakeholders"
MIN_PUBLISH_HORIZON = "weeks"
# 出处性质：结论内容本身是否已写在公开教科书/论文/官方文档里。否决要求与
# irreplaceability != only_here 且 reuse_audience == anyone_same_domain 同时成立，
# 且只在模型明确声明时触发；缺失或非法值不否决（fail-open）。与 Rust 侧条件一致。
PUBLIC_REFERENCE_VETO_ENABLED = True
SEMANTIC_FIELDS = (
    "future_question",
    "decision_reason",
    "evidence_summary",
    "irreplaceability_reason",
    "subject_key",
    "predicate_key",
)

# 用户点名的种子标注集：block 是一次性过程细节，keep 是关键工作事实。
SEED_EXPECTATIONS = {
    4270: "block",
    4273: "block",
    4274: "block",
    4275: "block",
    3804: "keep",
    1206: "keep",
    4196: "keep",
}

TIMELINE_COLUMNS = (
    "id", "summary", "overview", "details", "entities", "category", "importance",
    "occurrence_count", "observed_at", "event_time_start", "event_time_end",
    "start_time", "end_time", "duration_minutes", "time_range_start", "time_range_end",
    "key_timestamps", "history_view", "content_origin", "activity_type",
    "evidence_strength", "work_item", "work_status", "work_progress", "capture_id",
)
CAPTURE_COLUMNS = (
    "id", "ts", "app_name", "win_title", "event_type", "ax_text", "ax_focused_role",
    "ax_focused_id", "ocr_text", "input_text", "audio_text", "url", "webpage_title",
)


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读 URI 打开数据库，任何写操作都会在 sqlite 层直接失败。"""
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _trimmed(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _finite_score(value: Any) -> Optional[float]:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score != score or score in (float("inf"), float("-inf")):
        return None
    return score


def legacy_decision(payload: Dict[str, Any]) -> Dict[str, Any]:
    """第一期单分数口径，等价于 Rust 侧 `legacy_knowledge_decision`。"""
    score = _finite_score(payload.get("match_score"))
    future_question = _trimmed(payload.get("future_question"))
    decision_reason = _trimmed(payload.get("decision_reason"))
    evidence = _trimmed(payload.get("evidence_summary"))

    if score is not None and score < LEGACY_SHADOW_SCORE:
        reason_code = "below_shadow_threshold"
        state = "timeline_only"
    elif score is None:
        reason_code, state = "quality_score_missing", "shadow"
    elif score < LEGACY_PUBLISH_SCORE:
        reason_code, state = "below_publish_threshold", "shadow"
    elif future_question is None or decision_reason is None or evidence is None:
        reason_code, state = "open_semantic_evidence_incomplete", "shadow"
    else:
        reason_code, state = "publish_threshold_met", "published"
    return {
        "state": state,
        "score": score,
        "reason_code": reason_code,
        "rule_version": LEGACY_RULE_VERSION,
    }


def reuse_decision(
    payload: Dict[str, Any],
    timeline_importance: int,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """第二期复用半径口径，等价于 Rust 侧 `resolve_knowledge_decision`。"""
    if not config.get("enabled", True):
        return legacy_decision(payload)

    irreplaceability = _trimmed(payload.get("irreplaceability"))
    reuse_audience = _trimmed(payload.get("reuse_audience"))
    validity_horizon = _trimmed(payload.get("validity_horizon"))
    source_publicity = _trimmed(payload.get("source_publicity"))

    def outcome(state: str, score: Optional[float], reason_code: str) -> Dict[str, Any]:
        return {
            "state": state,
            "score": score,
            "reason_code": reason_code,
            "rule_version": RULE_VERSION,
        }

    # 第一步 · 硬否决：四条都落 timeline_only，不再往只写不读的 shadow 池堆数据。
    if irreplaceability == "authoritative_elsewhere":
        return outcome("timeline_only", None, "redundant_with_authoritative_source")
    if reuse_audience == "only_me_this_session":
        return outcome("timeline_only", None, "session_local_reuse_only")
    if validity_horizon == "hours" and irreplaceability != "only_here":
        return outcome("timeline_only", None, "transient_process_detail")
    if (
        config.get("public_reference_veto_enabled", True)
        and source_publicity == "publicly_documented"
        and irreplaceability != "only_here"
        and reuse_audience == "anyone_same_domain"
    ):
        return outcome("timeline_only", None, "redundant_public_reference")

    # 第二步 · 语义完整性；维度未知值宽容降级为 shadow，不硬拒。
    if any(_trimmed(payload.get(field)) is None for field in SEMANTIC_FIELDS):
        return outcome("shadow", None, "reuse_semantics_incomplete")
    scope = IRREPLACEABILITY_SCORES.get(irreplaceability or "")
    audience = AUDIENCE_SCORES.get(reuse_audience or "")
    horizon = HORIZON_SCORES.get(validity_horizon or "")
    if scope is None or audience is None or horizon is None:
        return outcome("shadow", None, "reuse_dimension_missing")

    # 第三步 · 确定性算分。重要性取时间线值，不取 payload 自报值，避免同一次推理自我抬分。
    importance = min(5, max(1, int(timeline_importance or 3)))
    importance_score = (importance - 1) / 4.0
    quality = 0.40 * scope + 0.25 * audience + 0.20 * horizon + 0.15 * importance_score
    if quality >= config["publish_score"]:
        # 发布下限：算分够线但受众或时效低于下限时落 shadow，原因与 Rust 侧一致：
        # 该候选并未被证明冗余，留影子副本能让去重窗口挡住同一件事被反复提炼。
        min_audience_rank = AUDIENCE_RANKS.get(
            config.get("min_publish_audience") or MIN_PUBLISH_AUDIENCE, 0
        )
        min_horizon_rank = HORIZON_RANKS.get(
            config.get("min_publish_horizon") or MIN_PUBLISH_HORIZON, 0
        )
        if (
            AUDIENCE_RANKS[reuse_audience] < min_audience_rank
            or HORIZON_RANKS[validity_horizon] < min_horizon_rank
        ):
            return outcome("shadow", round(quality, 4), "reuse_radius_below_minimum")
        return outcome("published", round(quality, 4), "publish_score_met")
    if quality >= config["shadow_score"]:
        return outcome("shadow", round(quality, 4), "below_publish_score")
    return outcome("timeline_only", round(quality, 4), "below_shadow_score")


def normalize_semantic_key_part(raw: str) -> str:
    """归一化语义身份片段：小写、剔除标点与空白、丢弃版本号与纯数字 token。

    拼接时刻意不插入分隔符：中文对象名本身不带空格，"联盟切流 共享集群"与
    "联盟切流共享集群"必须收敛到同一 key。
    """
    tokens = re.split(r"[\s\W_]+", raw.lower())
    kept = []
    for token in tokens:
        if not token:
            continue
        digits = token[1:] if token.startswith("v") else token
        if digits and digits.isdigit():
            continue
        kept.append(token)
    return "".join(kept)


def knowledge_dedup_key(payload: Dict[str, Any]) -> Optional[str]:
    subject_raw = _trimmed(payload.get("subject_key"))
    predicate_raw = _trimmed(payload.get("predicate_key"))
    if subject_raw is None or predicate_raw is None:
        return None
    subject = normalize_semantic_key_part(subject_raw)
    predicate = normalize_semantic_key_part(predicate_raw)
    if not subject or not predicate:
        return None
    digest = hashlib.sha256(("%s|%s" % (subject, predicate)).encode("utf-8")).hexdigest()
    return digest[:32]


SELECT_COLUMNS = """
    SELECT k.id AS knowledge_id, k.timeline_id, k.title, k.summary, k.content,
           k.detailed_content, k.importance AS knowledge_importance,
           k.created_at_ms, k.source_capture_ids, k.dedup_key, k.quality_score,
           k.gate_rule_version
    FROM bake_knowledge k
"""

AUDIT_COLUMNS = (
    "decision_state", "quality_score", "decision_reason_code", "decision_reason_summary",
    "decision_rule_version", "shadow_payload_json", "model_accepted", "persist_status",
    "created_at_ms",
)


def load_audit(conn: sqlite3.Connection, timeline_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """取该时间线最新一条 knowledge 审计，它是当时门禁判定的权威记录。

    审计比 `bake_knowledge.content` 更适合回放对账：content 只落库了部分 payload 键
    （不含 future_question / decision_reason），而 shadow_payload_json 是完整 payload 快照。
    """
    if timeline_id is None:
        return None
    row = conn.execute(
        "SELECT %s FROM bake_artifact_audits"
        " WHERE artifact_kind = 'knowledge' AND timeline_id = ?"
        " ORDER BY created_at_ms DESC, id DESC LIMIT 1" % ", ".join(AUDIT_COLUMNS),
        (timeline_id,),
    ).fetchone()
    return dict(row) if row else None


def load_baseline(conn: sqlite3.Connection, days: int) -> Dict[str, Any]:
    """全量审计口径的当前门禁基线，用于对照验收线里的发布率区间与类目偏差。

    这里统计的是全部已判定审计而非抽样，因为「当前发布率 38%」这类基线数字必须
    来自完整总体，抽样只能用于逐条对照。
    """
    since_ms = int(time.time() * 1000) - days * 86_400_000
    rows = conn.execute(
        """
        SELECT COALESCE(t.category, 'unknown') AS category, a.decision_state AS state,
               a.decision_reason_code AS reason_code, COUNT(*) AS total
        FROM bake_artifact_audits a
        LEFT JOIN timelines t ON t.id = a.timeline_id
        WHERE a.artifact_kind = 'knowledge' AND a.decision_state IS NOT NULL
          AND a.created_at_ms >= ?
        GROUP BY 1, 2, 3
        """,
        (since_ms,),
    ).fetchall()

    states: Dict[str, int] = {}
    reason_codes: Dict[str, int] = {}
    rule_versions: Dict[str, int] = {}
    categories: Dict[str, Dict[str, int]] = {}
    total = 0
    published = 0
    for row in rows:
        count = int(row["total"])
        total += count
        states[row["state"]] = states.get(row["state"], 0) + count
        code = row["reason_code"] or "unset"
        reason_codes[code] = reason_codes.get(code, 0) + count
        bucket = categories.setdefault(row["category"], {"total": 0, "published": 0})
        bucket["total"] += count
        if row["state"] == "published":
            published += count
            bucket["published"] += count

    version_rows = conn.execute(
        "SELECT COALESCE(decision_rule_version, 'unset') AS version, COUNT(*) AS total"
        " FROM bake_artifact_audits"
        " WHERE artifact_kind = 'knowledge' AND decision_state IS NOT NULL AND created_at_ms >= ?"
        " GROUP BY 1",
        (since_ms,),
    ).fetchall()
    for row in version_rows:
        rule_versions[row["version"]] = int(row["total"])

    category_totals = _category_totals(categories)
    return {
        "window_days": days,
        "decided_total": total,
        "published_total": published,
        "publish_rate": _rate(published, total),
        "states": dict(sorted(states.items(), key=lambda pair: -pair[1])),
        "reason_codes": dict(sorted(reason_codes.items(), key=lambda pair: -pair[1])),
        "rule_versions": dict(sorted(rule_versions.items(), key=lambda pair: -pair[1])),
        "category_publish_rate": _category_rates(categories),
        "category_total": category_totals,
        "code_category_not_outlier": _code_category_check(categories),
    }


def load_samples(
    conn: sqlite3.Connection,
    days: int,
    limit: int,
    seed_ids: List[int],
    max_captures: int,
) -> List[Dict[str, Any]]:
    """以**审计表**为取样框抽取近 N 天的 knowledge 候选及其 timeline/capture 上下文。

    取样框必须是审计表而不是 `bake_knowledge`：后者只收录已发布的行，shadow 与
    timeline_only 候选根本不会建条目，用它当框测出来的是「旧门禁已发布内容的留存率」，
    而不是整体发布率，无法与 15.1 的 38.43% 基线对比（实测这一偏置会把发布率抬到 69%）。
    审计表对三种结局都留痕，才是无偏取样框，也与 `load_baseline` 的总体口径一致。

    种子标注集单独查询后再合并：它们大多是较早的行，与近 N 天共用一条
    `ORDER BY created_at_ms DESC LIMIT` 会被新行挤掉，而验收线正是要看这些样本。
    """
    since_ms = int(time.time() * 1000) - days * 86_400_000
    frames: List[Dict[str, Any]] = []
    seen_timelines = set()

    if seed_ids:
        for row in conn.execute(
            SELECT_COLUMNS + " WHERE k.id IN (%s) ORDER BY k.id"
            % ",".join("?" for _ in seed_ids),
            list(seed_ids),
        ).fetchall():
            frames.append({"knowledge_row": dict(row), "audit_ref": None})
            if row["timeline_id"] is not None:
                seen_timelines.add(row["timeline_id"])

    if limit > 0:
        for row in conn.execute(
            """
            SELECT a.timeline_id, a.artifact_id, a.created_at_ms
            FROM bake_artifact_audits a
            WHERE a.artifact_kind = 'knowledge' AND a.decision_state IS NOT NULL
              AND a.created_at_ms >= ?
            ORDER BY a.created_at_ms DESC, a.id DESC
            LIMIT ?
            """,
            (since_ms, limit),
        ).fetchall():
            timeline_id = row["timeline_id"]
            # 同一时间线可能有多轮审计，ORDER BY 已保证先遇到最新一轮。
            if timeline_id in seen_timelines:
                continue
            seen_timelines.add(timeline_id)
            knowledge_row = None
            if row["artifact_id"] is not None:
                found = conn.execute(
                    SELECT_COLUMNS + " WHERE k.id = ?", (row["artifact_id"],)
                ).fetchone()
                knowledge_row = dict(found) if found else None
            frames.append({"knowledge_row": knowledge_row, "audit_ref": dict(row)})

    samples = []
    for frame in frames:
        sample = _build_sample(conn, frame, max_captures)
        if sample is not None:
            samples.append(sample)
    return samples


def _fetch_captures(conn: sqlite3.Connection, capture_ids: List[int]) -> List[Dict[str, Any]]:
    captures = []
    for capture_id in capture_ids:
        row = conn.execute(
            "SELECT %s FROM captures WHERE id = ?" % ", ".join(CAPTURE_COLUMNS),
            (capture_id,),
        ).fetchone()
        if row:
            captures.append(dict(row))
    return captures


def _build_sample(
    conn: sqlite3.Connection,
    frame: Dict[str, Any],
    max_captures: int,
) -> Optional[Dict[str, Any]]:
    """把一个取样框行组装成可回放的样本，兼容「有知识条目」与「只有审计」两种来源。"""
    knowledge_row = frame.get("knowledge_row") or {}
    audit_ref = frame.get("audit_ref") or {}
    timeline_id = knowledge_row.get("timeline_id") or audit_ref.get("timeline_id")
    if timeline_id is None:
        return None

    timeline_row = conn.execute(
        "SELECT %s FROM timelines WHERE id = ?" % ", ".join(TIMELINE_COLUMNS),
        (timeline_id,),
    ).fetchone()
    timeline = dict(timeline_row) if timeline_row else None

    if knowledge_row.get("source_capture_ids") is not None:
        capture_ids = _parse_capture_ids(knowledge_row["source_capture_ids"])[:max_captures]
    else:
        # shadow / timeline_only 候选没有知识条目，capture 只能按时间线反查，
        # 这与烘焙流水线取上下文的方式一致。
        capture_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM captures WHERE timeline_id = ? ORDER BY ts LIMIT ?",
                (timeline_id, max_captures),
            ).fetchall()
        ]

    # 知识条目缺失时 knowledge_id 为 None，不得当成种子标注去查期望值。
    knowledge_id = knowledge_row.get("knowledge_id") or audit_ref.get("artifact_id")
    return {
        "knowledge_id": knowledge_id,
        "timeline_id": timeline_id,
        "title": knowledge_row.get("title") or (timeline or {}).get("summary"),
        "summary": knowledge_row.get("summary") or (timeline or {}).get("overview"),
        "content": knowledge_row.get("content"),
        "detailed_content": knowledge_row.get("detailed_content"),
        "knowledge_importance": knowledge_row.get("knowledge_importance"),
        "created_at_ms": knowledge_row.get("created_at_ms") or audit_ref.get("created_at_ms"),
        "stored_dedup_key": knowledge_row.get("dedup_key"),
        "stored_quality_score": knowledge_row.get("quality_score"),
        "stored_gate_rule_version": knowledge_row.get("gate_rule_version"),
        "category": (timeline or {}).get("category") or "unknown",
        "timeline": timeline,
        "captures": _fetch_captures(conn, capture_ids),
        "capture_ids": capture_ids,
        "audit": load_audit(conn, timeline_id),
        # 只有审计、没有知识条目的样本在 static 模式下只能靠 shadow_payload_json 回放。
        "has_knowledge_row": bool(knowledge_row),
    }


def _parse_capture_ids(raw: Any) -> List[int]:
    try:
        values = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        return []
    parsed = []
    for value in values if isinstance(values, list) else []:
        try:
            parsed.append(int(str(value).strip()))
        except (TypeError, ValueError):
            continue
    return parsed


def _stored_payload(sample: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """静态模式的 payload 来源，按保真度优先取审计快照。

    `shadow_payload_json` 是完整 payload；`content` JSON 只落库了部分键，缺
    future_question / decision_reason，用它回放会把语义完整性检查误判为不通过。
    """
    audit = sample.get("audit") or {}
    raw = audit.get("shadow_payload_json")
    source = "audit_shadow_payload"
    if not raw:
        raw = sample.get("content")
        source = "knowledge_content"
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return payload, source


def build_candidate(sample: Dict[str, Any]) -> Dict[str, Any]:
    """按 sidecar 契约拼装 bake 候选，字段名与 `_build_bake_candidate_text` 对齐。"""
    timeline = sample.get("timeline") or {}
    captures = sample.get("captures") or []
    primary = captures[0] if captures else {}
    key_timestamps = timeline.get("key_timestamps")
    if isinstance(key_timestamps, str):
        try:
            key_timestamps = json.loads(key_timestamps)
        except (TypeError, ValueError):
            pass
    entities = timeline.get("entities")
    if isinstance(entities, str):
        try:
            entities = json.loads(entities)
        except (TypeError, ValueError):
            entities = [entities]

    candidate = {
        "source_timeline_id": sample.get("timeline_id"),
        "source_capture_id": primary.get("id"),
        "source_capture_count": len(captures) or 1,
        "timeline_category": timeline.get("category"),
        "work_item": timeline.get("work_item"),
        "work_status": timeline.get("work_status"),
        "work_progress": timeline.get("work_progress"),
        "summary": timeline.get("summary") or sample.get("title"),
        "overview": timeline.get("overview"),
        "details": timeline.get("details"),
        "entities": entities if isinstance(entities, list) else [],
        "importance": timeline.get("importance") or sample.get("knowledge_importance") or 3,
        "occurrence_count": timeline.get("occurrence_count") or 1,
        "observed_at": timeline.get("observed_at"),
        "event_time_start": timeline.get("event_time_start"),
        "event_time_end": timeline.get("event_time_end"),
        "start_time": timeline.get("start_time"),
        "end_time": timeline.get("end_time"),
        "duration_minutes": timeline.get("duration_minutes"),
        "time_range_start": timeline.get("time_range_start"),
        "time_range_end": timeline.get("time_range_end"),
        "key_timestamps": key_timestamps,
        "history_view": bool(timeline.get("history_view")),
        "content_origin": timeline.get("content_origin"),
        "activity_type": timeline.get("activity_type"),
        "evidence_strength": timeline.get("evidence_strength"),
        "capture_ts": primary.get("ts"),
        "capture_app_name": primary.get("app_name"),
        "capture_win_title": primary.get("win_title"),
        "capture_url": primary.get("url"),
        "capture_webpage_title": primary.get("webpage_title"),
        "capture_ax_text": primary.get("ax_text"),
        "capture_ocr_text": primary.get("ocr_text"),
        "capture_input_text": primary.get("input_text"),
        "capture_audio_text": primary.get("audio_text"),
        "url_aggregated_text": "\n\n".join(
            str(item.get("ax_text") or item.get("ocr_text") or "")
            for item in captures[1:]
            if item.get("ax_text") or item.get("ocr_text")
        ),
        "url_aggregated_capture_count": max(0, len(captures) - 1),
        "action_trace": [
            {
                "capture_id": item.get("id"),
                "ts": item.get("ts"),
                "event_type": item.get("event_type"),
                "app_name": item.get("app_name"),
                "win_title": item.get("win_title"),
                "ax_focused_role": item.get("ax_focused_role"),
                "ax_focused_id": item.get("ax_focused_id"),
            }
            for item in captures
        ],
    }
    return candidate


def replay_payload(sample: Dict[str, Any], extractor: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """用当前 prompt 重跑一次 bundle 提炼，只取 knowledge 分支的 payload。"""
    try:
        result = extractor.extract_bake_bundle(build_candidate(sample))
    except Exception as exc:  # 单条失败不阻断整批回放
        return None, "%s: %s" % (type(exc).__name__, exc)
    knowledge = (result or {}).get("knowledge") or {}
    payload = knowledge.get("payload")
    if not isinstance(payload, dict):
        return None, "knowledge_payload_absent(accepted=%s)" % knowledge.get("accepted")
    return payload, None


def evaluate_sample(
    sample: Dict[str, Any],
    payload: Dict[str, Any],
    payload_source: str,
    config: Dict[str, Any],
    expectations: Dict[int, str],
    replay_error: Optional[str],
) -> Dict[str, Any]:
    timeline = sample.get("timeline") or {}
    timeline_importance = int(timeline.get("importance") or sample.get("knowledge_importance") or 3)
    legacy = legacy_decision(payload)
    reuse = reuse_decision(payload, timeline_importance, config)
    audit = sample.get("audit") or {}
    recorded_state = audit.get("decision_state")
    expectation = expectations.get(int(sample["knowledge_id"])) if sample.get("knowledge_id") is not None else None
    verdict = None
    if expectation:
        verdict = "pass" if (reuse["state"] == "published") == (expectation == "keep") else "fail"
    # 只知道「语义不齐」无法判断该修提示词还是修 schema，因此记下到底缺哪几个字段。
    missing_semantic_fields = [
        field for field in SEMANTIC_FIELDS if _trimmed(payload.get(field)) is None
    ]
    return {
        "knowledge_id": sample["knowledge_id"],
        "timeline_id": sample["timeline_id"],
        "category": sample["category"],
        "title": sample["title"],
        "summary": sample["summary"],
        "timeline_importance": timeline_importance,
        "payload_source": payload_source,
        # 语义字段不齐时，重算出的 shadow 只能说明回放输入不完整，不得当成门禁结论。
        "payload_semantics_complete": not missing_semantic_fields,
        "missing_semantic_fields": missing_semantic_fields,
        "recorded": {
            "decision_state": recorded_state,
            "quality_score": audit.get("quality_score"),
            "reason_code": audit.get("decision_reason_code"),
            "rule_version": audit.get("decision_rule_version"),
            "persist_status": audit.get("persist_status"),
        },
        # 存量行指未被第二期规则判定过的行；第二期门禁只在烘焙当时生效，不会回补重算。
        "is_stock_row": audit.get("decision_rule_version") != RULE_VERSION,
        "expectation": expectation,
        "verdict": verdict,
        "replay_error": replay_error,
        "dimensions": {
            "irreplaceability": payload.get("irreplaceability"),
            "irreplaceability_reason": payload.get("irreplaceability_reason"),
            "reuse_audience": payload.get("reuse_audience"),
            "validity_horizon": payload.get("validity_horizon"),
            "source_publicity": payload.get("source_publicity"),
            "subject_key": payload.get("subject_key"),
            "predicate_key": payload.get("predicate_key"),
        },
        "dedup_key": knowledge_dedup_key(payload),
        "legacy": legacy,
        "reuse": reuse,
    }


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def summarize(records: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    total = len(records)
    published = [item for item in records if item["reuse"]["state"] == "published"]
    legacy_published = [item for item in records if item["legacy"]["state"] == "published"]
    # 只有语义字段齐备的样本才能拿重算结果当结论，否则 shadow 只是回放输入不完整的产物。
    faithful = [item for item in records if item["payload_semantics_complete"]]

    shift: Dict[str, int] = {}
    for item in faithful:
        key = "%s->%s" % (item["legacy"]["state"], item["reuse"]["state"])
        shift[key] = shift.get(key, 0) + 1

    reason_codes: Dict[str, int] = {}
    for item in records:
        code = item["reuse"]["reason_code"]
        reason_codes[code] = reason_codes.get(code, 0) + 1

    categories: Dict[str, Dict[str, Any]] = {}
    for item in faithful:
        bucket = categories.setdefault(item["category"], {"total": 0, "published": 0})
        bucket["total"] += 1
        if item["reuse"]["state"] == "published":
            bucket["published"] += 1
    category_totals = _category_totals(categories)
    category_publish_rate = _category_rates(categories)

    dedup_groups: Dict[str, List[int]] = {}
    for item in records:
        if item["dedup_key"]:
            dedup_groups.setdefault(item["dedup_key"], []).append(item["knowledge_id"])

    annotated = [item for item in records if item["expectation"] and item["payload_semantics_complete"]]
    unannotatable = [
        item["knowledge_id"] for item in records
        if item["expectation"] and not item["payload_semantics_complete"]
    ]
    seeds = {
        "total": len(annotated),
        "pass": sum(1 for item in annotated if item["verdict"] == "pass"),
        "fail": [
            {
                "knowledge_id": item["knowledge_id"],
                "expectation": item["expectation"],
                "reuse_state": item["reuse"]["state"],
                "reason_code": item["reuse"]["reason_code"],
            }
            for item in annotated
            if item["verdict"] == "fail"
        ],
        "not_evaluable": unannotatable,
    }
    publish_rate = _rate(len(published), len(faithful)) if faithful else None
    acceptance = {
        "seed_annotations_all_pass": bool(annotated) and seeds["pass"] == len(annotated) and not unannotatable,
        "publish_rate": publish_rate,
        "publish_rate_in_target_band": publish_rate is not None and 0.15 <= publish_rate <= 0.25,
        "code_category_not_outlier": _code_category_check(categories),
        "evaluable_samples": len(faithful),
    }
    return {
        "mode": mode,
        "total": total,
        "faithful_total": len(faithful),
        "legacy_publish_rate": _rate(len(legacy_published), len(faithful)) if faithful else None,
        "reuse_publish_rate": publish_rate,
        "state_shift": dict(sorted(shift.items())),
        "reuse_reason_codes": dict(sorted(reason_codes.items(), key=lambda pair: -pair[1])),
        "category_publish_rate": category_publish_rate,
        "category_total": category_totals,
        "near_duplicate_groups": {
            key: ids for key, ids in sorted(dedup_groups.items()) if len(ids) > 1
        },
        "seed_verdicts": seeds,
        "replay_errors": sum(1 for item in records if item["replay_error"]),
        "acceptance": acceptance,
    }


def _category_totals(categories: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    return {
        name: bucket["total"]
        for name, bucket in sorted(categories.items(), key=lambda pair: -pair[1]["total"])
    }


def _category_rates(categories: Dict[str, Dict[str, int]]) -> Dict[str, Optional[float]]:
    return {
        name: _rate(bucket["published"], bucket["total"])
        for name, bucket in sorted(categories.items(), key=lambda pair: -pair[1]["total"])
    }


def _code_category_check(
    categories: Dict[str, Dict[str, int]],
    min_total: int = 20,
    max_gap: float = 0.15,
) -> Optional[bool]:
    """"代码"类发布率不再显著高于其他类目：加权差值超过 max_gap 视为仍然偏高。

    对比对象是非代码类目的**加权汇总**而不是其中最高的一个：取 max 会让某个小类目
    偶然的高发布率掩盖真实偏差。代码类目本身也先按 min_total 聚合，避开
    `代码|其他` 这类多值长尾带来的 1.0 噪声率。
    """
    code = {"published": 0, "total": 0}
    other = {"published": 0, "total": 0}
    for name, bucket in categories.items():
        target = code if "代码" in name else other
        target["published"] += bucket["published"]
        target["total"] += bucket["total"]
    if code["total"] < min_total or other["total"] < min_total:
        return None
    code_rate = _rate(code["published"], code["total"]) or 0.0
    other_rate = _rate(other["published"], other["total"]) or 0.0
    return code_rate - other_rate <= max_gap


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path(os.path.expanduser("~/.memory-bread/memory-bread.db")))
    parser.add_argument("--mode", choices=("static", "replay"), default="static")
    parser.add_argument("--days", type=int, default=30, help="抽取近 N 天的知识条目")
    parser.add_argument("--limit", type=int, default=40, help="样本上限，种子 ID 不计入")
    parser.add_argument("--max-captures", type=int, default=6, help="每条样本纳入的 capture 上限")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=None, help="replay 模式使用的 Ollama 模型名")
    parser.add_argument("--expect-keep", type=int, action="append", default=[], help="追加应保留的标注 ID")
    parser.add_argument("--expect-block", type=int, action="append", default=[], help="追加应拦下的标注 ID")
    parser.add_argument("--publish-score", type=float, default=PUBLISH_SCORE)
    parser.add_argument("--shadow-score", type=float, default=SHADOW_SCORE)
    parser.add_argument("--dedup-window-days", type=int, default=DEDUP_WINDOW_DAYS)
    parser.add_argument(
        "--min-publish-audience",
        choices=sorted(AUDIENCE_RANKS),
        default=MIN_PUBLISH_AUDIENCE,
        help="发布所需的最低复用受众档位（必要条件）；取最低档等于关闭本规则",
    )
    parser.add_argument(
        "--min-publish-horizon",
        choices=sorted(HORIZON_RANKS),
        default=MIN_PUBLISH_HORIZON,
        help="发布所需的最低时效量级（必要条件）；取 hours 等于关闭本规则",
    )
    parser.add_argument(
        "--no-public-reference-veto",
        action="store_true",
        help="关闭 source_publicity=publicly_documented 的硬否决",
    )
    parser.add_argument("--gate-disabled", action="store_true", help="模拟 bake.knowledge_gate_enabled=false 的应急回退")
    args = parser.parse_args()

    config = {
        "enabled": not args.gate_disabled,
        "publish_score": args.publish_score,
        # shadow 线不得高于 publish 线，否则会出现「够不上发布却又算不上 shadow」的空档。
        "shadow_score": min(args.shadow_score, args.publish_score),
        "dedup_window_days": args.dedup_window_days,
        "min_publish_audience": args.min_publish_audience,
        "min_publish_horizon": args.min_publish_horizon,
        "public_reference_veto_enabled": not args.no_public_reference_veto,
    }
    expectations = dict(SEED_EXPECTATIONS)
    for knowledge_id in args.expect_keep:
        expectations[knowledge_id] = "keep"
    for knowledge_id in args.expect_block:
        expectations[knowledge_id] = "block"

    if not args.db.exists():
        print("数据库不存在: %s" % args.db, file=sys.stderr)
        return 2

    started_at = time.monotonic()
    conn = open_readonly(args.db)
    try:
        baseline = load_baseline(conn, args.days)
        samples = load_samples(conn, args.days, args.limit, sorted(expectations), args.max_captures)
    finally:
        conn.close()

    extractor = None
    if args.mode == "replay":
        from knowledge.extractor_v2 import KnowledgeExtractorV2
        extractor = KnowledgeExtractorV2(model=args.model)

    records = []
    for sample in samples:
        replay_error = None
        payload = None
        payload_source = ""
        if args.mode == "replay":
            payload, replay_error = replay_payload(sample, extractor)
            payload_source = "llm_replay"
        if payload is None:
            payload, payload_source = _stored_payload(sample)
        records.append(evaluate_sample(
            sample, payload, payload_source, config, expectations, replay_error,
        ))
        print(json.dumps({
            "knowledge_id": records[-1]["knowledge_id"],
            "payload_source": payload_source,
            "recorded": records[-1]["recorded"]["decision_state"],
            "legacy": records[-1]["legacy"]["state"],
            "reuse": records[-1]["reuse"]["state"],
            "reason_code": records[-1]["reuse"]["reason_code"],
        }, ensure_ascii=False), flush=True)

    mode_notes = (
        "replay：payload 由当前 BAKE_BUNDLE_PROMPT 重新提炼，验收线以本模式为准"
        if args.mode == "replay"
        else "static：payload 优先取审计 shadow_payload_json（完整快照），否则取 bake_knowledge.content（只含部分键）。"
             "存量行没有复用半径维度，第二期门禁对其只会给出 reuse_semantics_incomplete，"
             "这正好验证「存量不重算、不降级、不隐藏」——线上判定发生在烘焙当时，不会回补。"
             "本模式下只有 baseline 是可信的当前发布率口径，summary 里的重算结果仅供参考"
    )
    report = {
        "generated_at_ms": int(time.time() * 1000),
        "mode": args.mode,
        "db": str(args.db),
        "scope": "只读回放，未写回用户数据库；连接串 mode=ro，脚本不含任何 DDL/DML",
        "mode_notes": mode_notes,
        "gate_config": config,
        "gate_rule_versions": {"legacy": LEGACY_RULE_VERSION, "reuse": RULE_VERSION},
        "score_table": {
            "irreplaceability": IRREPLACEABILITY_SCORES,
            "reuse_audience": AUDIENCE_SCORES,
            "validity_horizon": HORIZON_SCORES,
            "source_publicity": {
                "veto_value": "publicly_documented",
                "veto_requires": {
                    "irreplaceability": "!= only_here",
                    "reuse_audience": "anyone_same_domain",
                },
                "veto_enabled": config["public_reference_veto_enabled"],
            },
            "publish_minimum": {
                "reuse_audience": config["min_publish_audience"],
                "validity_horizon": config["min_publish_horizon"],
            },
            "weights": {"scope": 0.40, "audience": 0.25, "horizon": 0.20, "importance": 0.15},
        },
        "baseline": baseline,
        "summary": summarize(records, args.mode),
        "elapsed_seconds": round(time.monotonic() - started_at, 2),
        "samples": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "output": str(args.output),
        "baseline": report["baseline"],
        "summary": report["summary"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
