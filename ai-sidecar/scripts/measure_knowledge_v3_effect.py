#!/usr/bin/env python3
"""只读量算：把今天已发布的知识行按 v3 规则重判，量出缺陷 1/2 的实际拦截面。

不写库、不改任何行；结果用于方案文档 §15.12 的验收记录。用法：

    python3 ai-sidecar/scripts/measure_knowledge_v3_effect.py

已发布行的 content 快照不含 future_question / decision_reason（它们落在 detailed_content
的分节里），因此这里不走完整门禁复用判定，而是只重算「算分 + 发布下限」这两步，另外统计否决取值形状的命中面：
行既已发布，说明语义完整性在烘焙当时已经通过。

发布线一律拿 Core 当时算出并存下的 quality_score 判，不用本地重算值：重算需要时间线
重要性，而行被后续合并时重要性会变（实测 58 行里有 2 行对不上）。下限只看存下的
三个维度取值，与分数无关。
"""
import collections
import datetime
import json
import os
import sqlite3

from evaluate_knowledge_gate import (  # noqa: E402
    AUDIENCE_RANKS,
    AUDIENCE_SCORES,
    HORIZON_RANKS,
    HORIZON_SCORES,
    IRREPLACEABILITY_SCORES,
    MIN_PUBLISH_AUDIENCE,
    MIN_PUBLISH_HORIZON,
    RULE_VERSION,
)

PUBLISH_SCORE = 0.72
# 出处否决的取值形状：publicly_documented + 非 only_here + anyone_same_domain。
VETO_ELIGIBLE_SCOPES = {"partially_recoverable", "authoritative_elsewhere"}


def recompute(payload, importance):
    """返回 (quality, 新判定, 原因)。维度缺失或取值未知时不猜，标 not_evaluable。"""
    scope = IRREPLACEABILITY_SCORES.get((payload.get("irreplaceability") or "").strip())
    audience = AUDIENCE_SCORES.get((payload.get("reuse_audience") or "").strip())
    horizon = HORIZON_SCORES.get((payload.get("validity_horizon") or "").strip())
    if scope is None or audience is None or horizon is None:
        return None, "not_evaluable", "reuse_dimension_missing"
    quality = (
        0.40 * scope
        + 0.25 * audience
        + 0.20 * horizon
        + 0.15 * ((min(5, max(1, int(importance or 3))) - 1) / 4.0)
    )
    if quality < PUBLISH_SCORE:
        return quality, "shadow", "below_publish_score"
    if (
        AUDIENCE_RANKS[payload["reuse_audience"].strip()]
        < AUDIENCE_RANKS[MIN_PUBLISH_AUDIENCE]
        or HORIZON_RANKS[payload["validity_horizon"].strip()] < HORIZON_RANKS[MIN_PUBLISH_HORIZON]
    ):
        return quality, "shadow", "reuse_radius_below_minimum"
    return quality, "published", "publish_score_met"


def main():
    db = os.path.expanduser("~/.memory-bread/memory-bread.db")
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    conn.row_factory = sqlite3.Row
    local = datetime.datetime.now().astimezone()
    midnight = int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    rows = conn.execute(
        "SELECT k.id, k.timeline_id, k.importance AS k_importance, k.content,"
        " k.gate_rule_version, k.quality_score AS row_quality, t.importance AS t_importance"
        " FROM bake_knowledge k LEFT JOIN timelines t ON t.id = k.timeline_id"
        " WHERE k.created_at_ms >= ? ORDER BY k.id",
        (midnight,),
    ).fetchall()

    print("rule_version:", RULE_VERSION, " publish_line:", PUBLISH_SCORE)
    print(
        "today_rows:",
        len(rows),
        "by_version:",
        dict(collections.Counter(r["gate_rule_version"] for r in rows)),
    )

    reasons = collections.Counter()
    combos = collections.Counter()
    blocked = []
    veto_eligible = []
    stored_delta = []
    for row in rows:
        try:
            payload = json.loads(row["content"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not payload.get("irreplaceability"):
            reasons["no_dimensions_in_snapshot"] += 1
            continue
        combo = (
            payload.get("irreplaceability"),
            payload.get("reuse_audience"),
            payload.get("validity_horizon"),
            payload.get("source_publicity") or "(absent)",
        )
        combos[combo] += 1
        importance = row["t_importance"] or row["k_importance"] or 3
        recomputed, _, _ = recompute(payload, importance)
        if recomputed is None:
            reasons["reuse_dimension_missing"] += 1
            continue
        quality = row["row_quality"] if row["row_quality"] is not None else recomputed
        if abs(recomputed - float(quality)) > 1e-6:
            stored_delta.append((row["id"], round(recomputed, 4), float(quality)))
        if quality < PUBLISH_SCORE:
            reasons["below_publish_score_unexpected"] += 1
        elif (
            AUDIENCE_RANKS[payload["reuse_audience"].strip()] < AUDIENCE_RANKS[MIN_PUBLISH_AUDIENCE]
            or HORIZON_RANKS[payload["validity_horizon"].strip()]
            < HORIZON_RANKS[MIN_PUBLISH_HORIZON]
        ):
            reasons["reuse_radius_below_minimum"] += 1
            blocked.append((row["id"], round(quality, 4), combo))
        else:
            reasons["publish_score_met"] += 1
        if (
            payload.get("irreplaceability") in VETO_ELIGIBLE_SCOPES
            and payload.get("reuse_audience") == "anyone_same_domain"
        ):
            veto_eligible.append(row["id"])

    print("rejudged_reason_codes:", dict(reasons))
    print(
        "stored_vs_recomputed_quality_mismatches:",
        len(stored_delta),
        "(本地重算重要性取的是时间线当前值，行被后续合并后会变)",
    )
    for item in stored_delta:
        print("   id=%s recomputed=%s stored=%s" % item)
    print("blocked_by_publish_minimum:", len(blocked))
    for row in blocked:
        print("   id=%s score=%s dims=%s" % (row[0], row[1], row[2]))
    print("veto_eligible_if_publicity_declared:", len(veto_eligible))
    print("dimension_combos:")
    for key, value in combos.most_common():
        print("   %2d  %s" % (value, key))


if __name__ == "__main__":
    main()
