"""离线回放脚本的门禁镜像测试。

脚本里的两套门禁是 Rust 侧 `bake_service.rs` 的 Python 镜像，用于在没有 Core 的环境里
做离线对照。这些用例把 Rust 测试已经验算过的判定结果固化下来：任一侧改了分值、权重或
reason_code 而另一侧没跟上，这里就会失败，避免离线评估结论与线上判定背离。
"""
from __future__ import annotations

import sqlite3

import pytest

from scripts.evaluate_knowledge_gate import (
    AUDIT_COLUMNS,
    CAPTURE_COLUMNS,
    LEGACY_RULE_VERSION,
    RULE_VERSION,
    TIMELINE_COLUMNS,
    _code_category_check,
    evaluate_sample,
    knowledge_dedup_key,
    legacy_decision,
    load_samples,
    normalize_semantic_key_part,
    open_readonly,
    reuse_decision,
)


def config(**overrides):
    base = {
        "enabled": True,
        "publish_score": 0.72,
        "shadow_score": 0.50,
        "dedup_window_days": 14,
        # 与 Rust KnowledgeGateConfig::default() 一一对应，两侧任一处失同步即应由本文件失败。
        "min_publish_audience": "team_or_stakeholders",
        "min_publish_horizon": "weeks",
        "public_reference_veto_enabled": True,
    }
    base.update(overrides)
    return base


def payload(**overrides):
    base = {
        "summary": "联盟切流共享集群根因",
        "future_question": "下次共享集群调用量异常时该先查什么",
        "decision_reason": "根因与解法可迁移到其他集群",
        "evidence_summary": "来源明确记录了版本回退前后调用量对比",
        "irreplaceability": "only_here",
        "irreplaceability_reason": "只在当时那次排查里得出，别处查不回",
        "reuse_audience": "team_or_stakeholders",
        "validity_horizon": "months_or_more",
        "subject_key": "联盟切流 共享集群",
        "predicate_key": "根因是老版本 SDK 放大调用量",
        "match_score": 0.9,
    }
    base.update(overrides)
    return base


def test_reuse_decision_mirrors_rust_score_table():
    """六组验算与 Rust 测试 test_knowledge_decision_scores_reuse_radius_dimensions 对齐。"""
    cases = [
        # (irreplaceability, audience, horizon, importance, 期望分数, 期望状态, 期望 reason_code)
        ("only_here", "anyone_same_domain", "months_or_more", 5, 1.0, "published", "publish_score_met"),
        ("only_here", "team_or_stakeholders", "months_or_more", 5, 0.9625, "published", "publish_score_met"),
        ("only_here", "team_or_stakeholders", "weeks", 4, 0.885, "published", "publish_score_met"),
        ("partially_recoverable", "team_or_stakeholders", "weeks", 5, 0.7425, "published", "publish_score_met"),
        ("partially_recoverable", "me_later", "days", 4, 0.5575, "shadow", "below_publish_score"),
        # importance 1 时跌到 0.445，低于 shadow 线，落 timeline_only 而非只写不读的 shadow。
        ("partially_recoverable", "me_later", "days", 1, 0.445, "timeline_only", "below_shadow_score"),
    ]
    for scope, audience, horizon, importance, expected_score, expected_state, expected_code in cases:
        decision = reuse_decision(
            payload(irreplaceability=scope, reuse_audience=audience, validity_horizon=horizon),
            importance,
            config(),
        )
        assert decision["state"] == expected_state, (scope, audience, horizon, importance)
        assert decision["reason_code"] == expected_code, (scope, audience, horizon, importance)
        assert decision["score"] == pytest.approx(expected_score, abs=1e-4)
        assert decision["rule_version"] == RULE_VERSION


def test_reuse_decision_publishes_external_science_knowledge():
    """含用户自己适配结论的外部知识仍须能发布；纯复述型公开知识已改走硬否决。

    本用例必须显式声明 `source_publicity="own_work_only"`：靠字段缺失走 fail-open 不是想要的
    契约。`partially_recoverable` 取 0.55：领域公共知识取 `anyone_same_domain` 时算分 0.745
    （imp 3）能过发布线，而 imp 2 只有 0.7075 恰好落选——「重要」这个限定词靠时间线重要性兑现。
    反之 `partially_recoverable + me_later` 是内部矛盾组合（提示词要求领域公共知识取
    `anyone_same_domain`），即使自报 importance 5 也只有 0.695，不得仅凭重要性发布。
    注：「看到的重要学术知识也要纳入」这条要求现在只在结论含有用户自己的取舍/适配/验证
    结果时成立，纯复述型学术知识归 `publicly_documented` 那一档（见下条否决用例）。
    """
    for audience, importance, expected in (
        ("anyone_same_domain", 3, "published"),
        ("anyone_same_domain", 2, "shadow"),
        ("me_later", 5, "shadow"),
        ("me_later", 3, "shadow"),
    ):
        decision = reuse_decision(
            payload(
                irreplaceability="partially_recoverable",
                irreplaceability_reason="原文虽在但需重新通读全文才能复原该结论",
                reuse_audience=audience,
                validity_horizon="months_or_more",
                source_publicity="own_work_only",
            ),
            importance,
            config(),
        )
        assert decision["state"] == expected, (audience, importance)


def test_publish_requires_minimum_reuse_radius():
    """缺陷 1 的镜像回归锁：算分过线但受众/时效低于下限时不得发布。

    加性公式下 `only_here` 一项就能拿 0.40，单独顶住发布线：线上真实组合
    `only_here + me_later + days + importance 4` 算得 0.7375、已过 0.72，而它按提示词
    自己的定义（只有我以后会用 + 几天就过期）正是该拦的一次性过程细节。
    """
    decision = reuse_decision(
        payload(reuse_audience="me_later", validity_horizon="days"), 4, config()
    )
    assert decision["score"] > 0.72, "用例前提：算分本身已过发布线"
    assert decision["state"] == "shadow"
    assert decision["reason_code"] == "reuse_radius_below_minimum"

    # 两条下限是任一不足即拦：只低受众、只低时效都要拦下
    for overrides in ({"reuse_audience": "me_later"}, {"validity_horizon": "days"}):
        got = reuse_decision(payload(**overrides), 5, config())
        assert got["reason_code"] == "reuse_radius_below_minimum", overrides

    # 下限可配置：放到最宽档等于关闭本规则
    relaxed = config(min_publish_audience="only_me_this_session", min_publish_horizon="hours")
    back = reuse_decision(
        payload(reuse_audience="me_later", validity_horizon="days"), 4, relaxed
    )
    assert back["state"] == "published"
    assert back["reason_code"] == "publish_score_met"


def public_reference_payload(**overrides):
    """出处否决的基准形状：三个自报必须同时成立，与 Rust 用例一一对应。"""
    base = {
        "irreplaceability": "partially_recoverable",
        "reuse_audience": "anyone_same_domain",
        "source_publicity": "publicly_documented",
    }
    base.update(overrides)
    return payload(**base)


def test_public_reference_is_hard_rejected_but_fails_open():
    """缺陷 2 的镜像回归锁：出处性质是硬条件，但只对显式声明生效。

    今天已发布行里最大一组（11/47）就是 `partially_recoverable + anyone_same_domain +
    months_or_more` 这种公共教科书内容，它靠算分永远拦不住，因为模型会把“需重新推导”
    一视同仁地写成 partially_recoverable。改为问一个事实性的出处问题。

    否决不得只看出处单一轴：实测 replay 里模型把用户点名的正例 3804 也判成了
    `publicly_documented`，单轴硬否决会直接误杀它；只有受众取同领域任何人时
    才构成公共参考知识。本用例同时钉住两个反面：矛盾组合不得被否决。
    """
    vetoed = reuse_decision(public_reference_payload(), 5, config())
    assert vetoed["state"] == "timeline_only"
    assert vetoed["reason_code"] == "redundant_public_reference"
    assert vetoed["score"] is None, "硬否决不算分，也不得往 shadow 堆数据"

    # 不误杀自报互相矛盾的组合（种子 3804 的实测形态）：受众只到自己的团队时，即使内容
    # 出自公开资料，“选哪一段、为何对本项目重要”仍然是用户自己的判断。该组算分 0.705
    # 在发布线下方，去向由分数决定，不得出现否决理由。
    seed = reuse_decision(
        public_reference_payload(reuse_audience="team_or_stakeholders", validity_horizon="weeks"),
        4,
        config(),
    )
    assert seed["reason_code"] == "below_publish_score"
    # 同一形状只把时效抬到 months_or_more：否决仍未介入，正常发布。
    broad_team_use = reuse_decision(
        public_reference_payload(reuse_audience="team_or_stakeholders"), 5, config()
    )
    assert broad_team_use["state"] == "published"
    # only_here + anyone_same_domain 同理豁免：结论只在现场存在时，公开出处这句话本身就可疑。
    exclusive = reuse_decision(public_reference_payload(irreplaceability="only_here"), 5, config())
    assert exclusive["reason_code"] != "redundant_public_reference"

    # fail-open：缺失/非法/空串都不否决。本地推理并不保证按 schema 输出，若缺失即否决，
    # 模型漏一个字段就会把整个知识页清空。改变出处这一轴时其余两轴保持否决形状。
    for publicity in (None, "", "public_knowledge"):
        kept = reuse_decision(public_reference_payload(source_publicity=publicity), 5, config())
        assert kept["state"] == "published", publicity

    # 可整体关闭：需要把复述型学术知识也纳入时靠开关，不改代码
    off = reuse_decision(
        public_reference_payload(), 5, config(public_reference_veto_enabled=False)
    )
    assert off["state"] == "published"
    assert off["score"] > 0.72, "证明拦住它的是否决而不是分数"


def test_reuse_decision_hard_rejects_without_shadow_payload():
    """四条硬否决直接落 timeline_only，不再往 shadow 池堆数据。"""
    rejects = [
        ({"irreplaceability": "authoritative_elsewhere"}, "redundant_with_authoritative_source"),
        ({"reuse_audience": "only_me_this_session"}, "session_local_reuse_only"),
        # 时效硬否决只对非独占事实生效；若不拦，这一组算分 0.6125 会落 shadow。
        (
            {"validity_horizon": "hours", "irreplaceability": "partially_recoverable"},
            "transient_process_detail",
        ),
        # 出处性质：三个自报一致才拦，且不进只写不读的 shadow 池
        (
            {
                "irreplaceability": "partially_recoverable",
                "reuse_audience": "anyone_same_domain",
                "source_publicity": "publicly_documented",
            },
            "redundant_public_reference",
        ),
    ]
    for overrides, reason_code in rejects:
        decision = reuse_decision(payload(**overrides), 5, config())
        assert decision["state"] == "timeline_only", reason_code
        assert decision["reason_code"] == reason_code
        assert decision["score"] is None

    # hours + only_here 是当场才能确认的独占事实，不应被时效维度误杀。
    kept = reuse_decision(payload(validity_horizon="hours", reuse_audience="me_later"), 5, config())
    assert kept["reason_code"] != "transient_process_detail"


def test_reuse_decision_tolerates_unknown_and_missing_semantics():
    """维度未知值与语义字段缺失都宽容降级为 shadow，不硬拒。"""
    unknown = reuse_decision(payload(reuse_audience="whole_company"), 5, config())
    assert unknown["state"] == "shadow"
    assert unknown["reason_code"] == "reuse_dimension_missing"

    for field in (
        "future_question",
        "decision_reason",
        "evidence_summary",
        "irreplaceability_reason",
        "subject_key",
        "predicate_key",
    ):
        incomplete = reuse_decision(payload(**{field: None}), 5, config())
        assert incomplete["state"] == "shadow", field
        assert incomplete["reason_code"] == "reuse_semantics_incomplete", field

    # 存量行 content JSON 没有维度字段，回放时只能落 shadow，不会被误判为发布。
    stock = reuse_decision({"match_score": 0.9, "summary": "旧知识"}, 5, config())
    assert stock["state"] == "shadow"
    assert stock["reason_code"] == "reuse_semantics_incomplete"


def test_reuse_decision_honours_config_overrides_and_disable_switch():
    strict = reuse_decision(payload(reuse_audience="me_later", validity_horizon="days"), 4, config(publish_score=0.9))
    assert strict["state"] == "shadow"

    # 只把 publish_score 调低不再等于能发布：受众/时效下限是独立必要条件，必须连下限一起放宽
    lowered = reuse_decision(
        payload(reuse_audience="me_later", validity_horizon="days"), 4, config(publish_score=0.5)
    )
    assert lowered["state"] == "shadow"
    assert lowered["reason_code"] == "reuse_radius_below_minimum"

    loose = reuse_decision(
        payload(reuse_audience="me_later", validity_horizon="days"),
        4,
        config(publish_score=0.5, min_publish_audience="me_later", min_publish_horizon="days"),
    )
    assert loose["state"] == "published"

    # 应急开关：gate_enabled=false 时回到第一期单分数口径，硬否决与维度全部不参与。
    fallback = reuse_decision(
        payload(irreplaceability="authoritative_elsewhere", match_score=0.91),
        5,
        config(enabled=False),
    )
    assert fallback["state"] == "published"
    assert fallback["reason_code"] == "publish_threshold_met"
    assert fallback["rule_version"] == LEGACY_RULE_VERSION


def test_legacy_decision_replays_first_phase_thresholds():
    assert legacy_decision(payload(match_score=0.91))["state"] == "published"
    assert legacy_decision(payload(match_score=0.77))["reason_code"] == "below_publish_threshold"
    assert legacy_decision(payload(match_score=0.61))["reason_code"] == "below_shadow_threshold"
    assert legacy_decision(payload(match_score=None))["reason_code"] == "quality_score_missing"
    incomplete = legacy_decision(payload(match_score=0.91, future_question=None))
    assert incomplete["reason_code"] == "open_semantic_evidence_incomplete"
    # content JSON 里 match_score 可能是字符串，非数值一律按缺失处理。
    assert legacy_decision(payload(match_score="high"))["reason_code"] == "quality_score_missing"


def test_dedup_key_is_stable_across_versions_dates_and_punctuation():
    base = payload()
    key = knowledge_dedup_key(base)
    assert key and len(key) == 32

    variants = [
        payload(subject_key="联盟切流共享集群"),
        payload(subject_key="联盟切流  共享集群"),
        payload(subject_key="联盟切流共享集群 v2"),
        payload(predicate_key="根因是老版本 SDK 放大调用量。"),
    ]
    for variant in variants:
        assert knowledge_dedup_key(variant) == key, variant["subject_key"]

    assert knowledge_dedup_key(payload(predicate_key="根因是网关限流阈值配置错误")) != key
    assert knowledge_dedup_key(payload(subject_key=None)) is None
    assert knowledge_dedup_key(payload(predicate_key="   ")) is None
    # 全是版本号与数字的对象名归一化后为空，不能退化成同一个 key。
    assert knowledge_dedup_key(payload(subject_key="v1 2026")) is None
    assert normalize_semantic_key_part("联盟切流 共享集群 v2.3") == "联盟切流共享集群"


def test_code_category_check_uses_weighted_gap():
    """对比非代码类目的加权汇总，避免小类目偶然高发布率掩盖真实偏差。"""
    outlier = {
        "代码": {"published": 336, "total": 516},
        "文档": {"published": 580, "total": 1793},
        "聊天": {"published": 49, "total": 177},
    }
    assert _code_category_check(outlier) is False

    balanced = {
        "代码": {"published": 170, "total": 516},
        "文档": {"published": 580, "total": 1793},
        "聊天": {"published": 49, "total": 177},
    }
    assert _code_category_check(balanced) is True

    # 样本量不足时不给结论，而不是返回一个不可信的布尔值。
    assert _code_category_check({"代码": {"published": 1, "total": 1}}) is None
    # 多值类目按“含代码”归入代码侧，长尾单条不会单独成为对比基准。
    assert _code_category_check({
        "代码|其他": {"published": 30, "total": 40},
        "代码": {"published": 140, "total": 480},
        "文档": {"published": 500, "total": 1800},
    }) is True


def test_open_readonly_rejects_any_write(tmp_path):
    """脚本的“不写回用户数据库”承诺由连接层兜底，而不只是靠自觉。"""
    db_path = tmp_path / "sample.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("CREATE TABLE bake_knowledge (id INTEGER PRIMARY KEY, title TEXT)")
    writer.execute("INSERT INTO bake_knowledge (id, title) VALUES (1, '原始标题')")
    writer.commit()
    writer.close()

    conn = open_readonly(db_path)
    try:
        assert conn.execute("SELECT title FROM bake_knowledge WHERE id = 1").fetchone()[0] == "原始标题"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE bake_knowledge SET title = '被改写' WHERE id = 1")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE injected (id INTEGER)")
    finally:
        conn.close()

    checker = sqlite3.connect(str(db_path))
    assert checker.execute("SELECT title FROM bake_knowledge WHERE id = 1").fetchone()[0] == "原始标题"
    checker.close()


def sample_row(**overrides):
    base = {
        "knowledge_id": 4273,
        "timeline_id": 10243,
        "category": "聊天",
        "title": "工作流分支条件继承",
        "summary": "修复后测试通过",
        "timeline": {"importance": 4},
        "audit": {},
    }
    base.update(overrides)
    return base


def test_evaluate_sample_reports_which_semantic_fields_are_missing():
    """只报「语义不齐」无法定位该修提示词还是修 schema，必须点名缺的字段。

    实测回放里 4273 / 4275 正是靠 `reuse_semantics_incomplete` 被拦下的，如果记录里
    看不出缺哪个字段，就无法区分“模型没填”与“回放输入本来就不完整”。
    """
    complete = evaluate_sample(sample_row(), payload(), "llm_replay", config(), {}, None)
    assert complete["payload_semantics_complete"] is True
    assert complete["missing_semantic_fields"] == []

    # future_question 丢失时，三个维度齐备也仍应降级 shadow，且缺的字段被点名。
    incomplete = evaluate_sample(
        sample_row(knowledge_id=4275),
        payload(future_question="   "),
        "llm_replay",
        config(),
        {4275: "block"},
        None,
    )
    assert incomplete["payload_semantics_complete"] is False
    assert incomplete["missing_semantic_fields"] == ["future_question"]
    assert incomplete["reuse"]["state"] == "shadow"
    assert incomplete["reuse"]["reason_code"] == "reuse_semantics_incomplete"
    assert incomplete["verdict"] == "pass"

    # 未被种子标注集覆盖的样本不给 verdict，避免把无标注行当成验收结果。
    assert evaluate_sample(sample_row(knowledge_id=1), payload(), "llm_replay", config(), {}, None)[
        "verdict"
    ] is None


def _untyped_ddl(table, columns):
    """不声明列类型（BLOB 亲和）：sqlite 不会把整数转成文本，created_at_ms 比较才准确。"""
    return "CREATE TABLE %s (%s)" % (table, ", ".join(columns))


def _seed_frame_db(tmp_path, now_ms=1_800_000_000_000):
    """造一个同时含已发布与 shadow 候选的最小库，用于验证取样框无偏。"""
    db_path = tmp_path / "frame.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute(_untyped_ddl("bake_knowledge", [
        "id", "timeline_id", "title", "summary", "content", "detailed_content",
        "importance", "created_at_ms", "source_capture_ids", "dedup_key",
        "quality_score", "gate_rule_version",
    ]))
    writer.execute(_untyped_ddl("timelines", TIMELINE_COLUMNS))
    writer.execute(_untyped_ddl("captures", list(CAPTURE_COLUMNS) + ["timeline_id"]))
    writer.execute(_untyped_ddl(
        "bake_artifact_audits",
        ["id", "timeline_id", "artifact_kind", "artifact_id"] + list(AUDIT_COLUMNS),
    ))

    # timeline 501 已发布（有知识条目 7），timeline 502 被降级 shadow（无条目），
    # timeline 502 还有第二轮审计，用于验证按时间线去重。
    writer.execute(
        "INSERT INTO timelines (id, summary, overview, category, importance)"
        " VALUES (501, '已发布时间线', '概览', '代码', 4)"
    )
    writer.execute(
        "INSERT INTO timelines (id, summary, overview, category, importance)"
        " VALUES (502, '影子时间线', '概览', '会议', 3)"
    )
    writer.execute(
        "INSERT INTO bake_knowledge (id, timeline_id, title, summary, importance,"
        " created_at_ms, source_capture_ids) VALUES (7, 501, '已发布知识', '摘要', 4, ?, '[9]')",
        (now_ms,),
    )
    writer.execute(
        "INSERT INTO captures (id, ts, timeline_id) VALUES (9, ?, 501)", (now_ms,)
    )
    writer.execute(
        "INSERT INTO captures (id, ts, timeline_id) VALUES (11, ?, 502)", (now_ms,)
    )
    audit_columns = ["id", "timeline_id", "artifact_kind", "artifact_id"] + list(AUDIT_COLUMNS)
    writer.execute(
        "INSERT INTO bake_artifact_audits (%s) VALUES (1, 501, 'knowledge', 7,"
        " 'published', 0.9, 'publish_threshold_met', NULL, ?, NULL, 1, 'created', ?)"
        % ", ".join(audit_columns),
        (LEGACY_RULE_VERSION, now_ms),
    )
    writer.execute(
        "INSERT INTO bake_artifact_audits (%s) VALUES (2, 502, 'knowledge', NULL,"
        " 'shadow', NULL, 'below_publish_threshold', NULL, ?, '{\"summary\":\"x\"}', 1,"
        " 'shadow', ?)"
        % ", ".join(audit_columns),
        (LEGACY_RULE_VERSION, now_ms - 1000),
    )
    writer.execute(
        "INSERT INTO bake_artifact_audits (%s) VALUES (3, 502, 'knowledge', NULL,"
        " 'shadow', NULL, 'below_publish_threshold', NULL, ?, NULL, 1, 'shadow', ?)"
        % ", ".join(audit_columns),
        (LEGACY_RULE_VERSION, now_ms),
    )
    writer.commit()
    writer.close()
    return db_path


def test_load_samples_uses_audit_frame_so_shadow_candidates_are_in_scope(tmp_path):
    """取样框必须是审计表：`bake_knowledge` 只有已发布的行，用它当框会把 shadow
    候选整体排除，测出来的是留存率而不是发布率（实测偏置会把数字抬到 69%）。"""
    db_path = _seed_frame_db(tmp_path)
    conn = open_readonly(db_path)
    try:
        samples = load_samples(conn, days=60, limit=10, seed_ids=[], max_captures=5)
    finally:
        conn.close()

    # 两条时间线各一个样本：同一时间线的两轮审计按 timeline_id 去重。
    assert [s["timeline_id"] for s in samples] == [502, 501]

    published = next(s for s in samples if s["timeline_id"] == 501)
    assert published["knowledge_id"] == 7
    assert published["has_knowledge_row"] is True
    assert published["category"] == "代码"

    # shadow 候选没有知识条目，knowledge_id 为 None，capture 按时间线反查。
    shadowed = next(s for s in samples if s["timeline_id"] == 502)
    assert shadowed["knowledge_id"] is None
    assert shadowed["has_knowledge_row"] is False
    assert shadowed["capture_ids"] == [11]
    assert shadowed["category"] == "会议"
    assert (shadowed["audit"] or {}).get("decision_state") == "shadow"


def test_load_samples_merges_seeds_without_letting_recent_rows_squeeze_them_out(tmp_path):
    """种子标注集必须单独查询后合并：共用一条 `ORDER BY ... DESC LIMIT` 会被新行挤掉。"""
    db_path = _seed_frame_db(tmp_path)
    conn = open_readonly(db_path)
    try:
        seeds_only = load_samples(conn, days=60, limit=0, seed_ids=[7], max_captures=5)
        assert [s["knowledge_id"] for s in seeds_only] == [7]

        merged = load_samples(conn, days=60, limit=10, seed_ids=[7], max_captures=5)
    finally:
        conn.close()

    # 种子排在前面，且不会因为 timeline 501 已在审计框里而重复出现。
    assert [s["timeline_id"] for s in merged] == [501, 502]
    assert merged[0]["knowledge_id"] == 7
