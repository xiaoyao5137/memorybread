import json
import os
import sqlite3
from pathlib import Path

import pytest

from background_processor import BackgroundProcessor
from knowledge.extractor_v2 import (
    DATA_FACT_CONTRACT_VERSION,
    DATA_FACT_PROMPT,
    KnowledgeExtractorV2,
    _data_fact_retry_needed,
    _fact_specific_statement,
    _validated_data_facts,
)


SOURCE_TEXT = "其中11%的场景（如生服模特库在电商AIGC中的复用）已成功合并，节省约6.28万成本"


def _valid_fact():
    return {
        "title": "生服模特库在电商AIGC中复用的成本节省金额",
        "subject": "生服模特库",
        "action": "复用",
        "target_context": "电商AIGC",
        "dimension": "",
        "metric": "成本节省金额",
        "value": "6.28",
        "unit": "万",
        "statement": "生服模特库在电商AIGC中的复用节省约6.28万成本。",
        "evidence_quote": SOURCE_TEXT,
        "confidence": "high",
    }


def test_validates_complete_model_fact_and_realigns_rewritten_evidence():
    accepted, rejected = _validated_data_facts([_valid_fact()], SOURCE_TEXT)
    assert accepted == [_valid_fact()]
    assert rejected == 0

    invalid = _valid_fact()
    invalid["evidence_quote"] = "不存在于原文的6.28万成本结论"
    accepted, rejected = _validated_data_facts([invalid], SOURCE_TEXT)
    assert rejected == 0
    assert accepted[0]["evidence_quote"] == "生服模特库在电商AIGC中的复用）已成功合并，节省约6.28万"


def test_accepts_paraphrased_title_when_subject_and_value_grounded_in_evidence():
    # title 是展示结构：模型概括式标题不含 subject/metric 原文时，
    # 只要 subject/value 能回证到 evidence 就应接受（防幻觉底线不变）。
    paraphrased = _valid_fact()
    paraphrased["title"] = "AIGC 成本"

    accepted, rejected = _validated_data_facts([paraphrased], SOURCE_TEXT)

    assert rejected == 0
    assert accepted[0]["title"] == "AIGC 成本"


def test_downgrades_invented_subject_to_a_grounded_semantic_anchor():
    invalid = _valid_fact()
    invalid["title"] = "商业体系 AI 建设资产复用方案 成本节省金额"
    invalid["subject"] = "商业体系 AI 建设资产复用方案"
    invalid["statement"] = "商业体系 AI 建设资产复用方案节省约6.28万成本。"

    accepted, rejected = _validated_data_facts([invalid], SOURCE_TEXT)

    assert rejected == 0
    assert accepted[0]["subject"] == "电商AIGC"
    assert accepted[0]["value"] == "6.28"


def test_accepts_value_with_format_drift_when_digit_tokens_hit_evidence():
    # value 带格式差异（如 "6.28万+" vs 原文 "6.28万"）时，
    # 数字 token 全部命中 evidence 即视为可靠。
    drifted = _valid_fact()
    drifted["value"] = "6.28+"

    accepted, rejected = _validated_data_facts([drifted], SOURCE_TEXT)

    assert rejected == 0
    assert accepted[0]["value"] == "6.28+"


def test_expands_short_exact_quote_to_include_nearest_named_subject():
    source = "Sync Standard $4 USD 每用户每月，按年计费 $5 USD 每用户每月，按月计费"
    fact = {
        "title": "Sync Standard 每用户月费",
        "subject": "Sync Standard",
        "action": "",
        "target_context": "",
        "dimension": "按年计费",
        "metric": "每用户月费",
        "value": "4",
        "unit": "USD",
        "statement": "Sync Standard 按年计费时每用户月费为4美元。",
        "evidence_quote": "$4 USD 每用户每月，按年计费",
        "confidence": "high",
    }

    accepted, rejected = _validated_data_facts([fact], source)

    assert rejected == 0
    assert accepted[0]["evidence_quote"] == "Sync Standard $4 USD 每用户每月，按年计费"


def test_prompt_requires_plan_dimensions_and_rejects_checklist_thresholds():
    assert DATA_FACT_CONTRACT_VERSION == "timeline-data-fact.v3"
    assert "切换前检查" in DATA_FACT_PROMPT
    assert "每用户每月 4 USD，按年计费" in DATA_FACT_PROMPT
    assert "不得使用浏览器窗口标题" in DATA_FACT_PROMPT
    assert "最多输出 24 条" in DATA_FACT_PROMPT
    assert "分页条数" in DATA_FACT_PROMPT
    assert "16分31秒" in DATA_FACT_PROMPT
    assert "目标场景" in DATA_FACT_PROMPT


def test_retries_for_applied_parameters_and_composite_duration_but_not_plain_ids():
    assert _data_fact_retry_needed(
        "MemoryBread 官网首屏背景视频已生成，时长15秒，画幅16:9"
    )
    assert _data_fact_retry_needed("官网首页优化任务总耗时约16分31秒")
    assert not _data_fact_retry_needed("用户打开了记录 ID 1710")


def test_focused_retry_recovers_scene_and_keeps_composite_duration():
    source = (
        "调研首页背景视频并设计生成方案。MemoryBread 官网首页视觉与文案优化已完成，"
        "任务总耗时约16分31秒。"
    )
    fact = {
        "title": "MemoryBread 官网首页视觉与文案优化任务耗时",
        "subject": "MemoryBread 官网首页视觉与文案优化",
        "action": "优化",
        "target_context": "官网首页",
        "dimension": "",
        "metric": "任务耗时",
        "value": "16分31秒",
        "unit": "",
        "statement": "MemoryBread 官网首页视觉与文案优化任务耗时为16分31秒。",
        "evidence_quote": "MemoryBread 官网首页视觉与文案优化已完成，任务总耗时约16分31秒",
        "confidence": "high",
    }
    extractor = object.__new__(KnowledgeExtractorV2)
    extractor._ollama_chat = lambda **_kwargs: {
        "message": {"content": json.dumps({"data_facts": [fact]}, ensure_ascii=False)}
    }

    facts, rejected = extractor._recover_missing_data_facts(
        source,
        {"overview": "用户完成了 MemoryBread 官网首页视觉与文案优化工作。"},
        "test",
    )

    assert rejected == 0
    assert facts[0]["title"] == fact["title"]
    assert facts[0]["value"] == "16分31秒"


def test_deduplicates_repeated_screenshot_facts_by_semantic_key():
    duplicate = _valid_fact()
    duplicate["statement"] = "复用生服模特库后节省约6.28万成本。"

    accepted, rejected = _validated_data_facts(
        [_valid_fact(), duplicate], SOURCE_TEXT
    )

    assert rejected == 0
    assert len(accepted) == 1


def test_rewrites_shared_batch_summary_into_fact_specific_statements():
    source = (
        "图生视频 badcase拦截率：72.3%，badcase漏审率8.93%，"
        "goodcase误杀率：35.77%"
    )
    shared_statement = (
        "图生视频中 high case 的误杀率为 35.77%，low case 漏审率为 8.93%。"
    )
    facts = []
    for metric, value, evidence in [
        ("badcase拦截率", "72.3", "图生视频 badcase拦截率：72.3%"),
        ("badcase漏审率", "8.93", "图生视频 badcase漏审率8.93%"),
        ("goodcase误杀率", "35.77", "图生视频 goodcase误杀率：35.77%"),
    ]:
        facts.append({
            "title": f"图生视频 {metric}",
            "subject": "图生视频",
            "action": "审核",
            "target_context": "视频评测",
            "dimension": "",
            "metric": metric,
            "value": value,
            "unit": "%",
            "statement": shared_statement,
            "evidence_quote": evidence,
            "confidence": "high",
        })

    accepted, rejected = _validated_data_facts(facts, source)

    assert rejected == 0
    assert len(accepted) == 3
    assert len({fact["statement"] for fact in accepted}) == 3
    assert all(fact["value"] in fact["statement"] for fact in accepted)
    assert all(fact["metric"] in fact["statement"] for fact in accepted)


def test_fact_specific_statement_keeps_metric_and_value_when_context_is_too_long():
    statement = _fact_specific_statement({
        "subject": "图生视频",
        "action": "审核",
        "target_context": "评测场景" * 200,
        "dimension": "当前批次" * 200,
        "metric": "badcase拦截率",
        "value": "72.3",
        "unit": "%",
    })

    assert len(statement) <= 500
    assert "badcase拦截率" in statement
    assert "72.3%" in statement


def test_generic_field_or_task_anchor_requires_a_specific_scene():
    source = "duration Kling 3.0 支持 3-15 秒。任务总耗时约16分31秒"
    generic_duration = {
        "title": "Kling 3.0 模型支持时长范围",
        "subject": "duration",
        "action": "",
        "target_context": "视频参数配置",
        "dimension": "",
        "metric": "支持时长范围",
        "value": "3-15",
        "unit": "秒",
        "statement": "Kling 3.0 支持生成视频时长3到15秒。",
        "evidence_quote": "duration Kling 3.0 支持 3-15 秒",
        "confidence": "high",
    }
    contextual_duration = dict(generic_duration)
    contextual_duration["target_context"] = "MemoryBread 官网首屏静音背景视频生成"

    rejected_facts, rejected = _validated_data_facts([generic_duration], source)
    accepted_facts, accepted_rejected = _validated_data_facts(
        [contextual_duration], source
    )

    assert rejected_facts == []
    assert rejected == 1
    assert accepted_rejected == 0
    assert accepted_facts[0]["target_context"] == contextual_duration["target_context"]


def test_rejects_tool_or_model_execution_shell_as_target_scene():
    source = "15秒 vedio-aigc系统生成参数配置 Kling 3.0模型生成控制"
    fact = {
        "title": "vedio-aigc系统请求参数时长",
        "subject": "15秒",
        "action": "配置",
        "target_context": "Kling 3.0模型生成控制",
        "dimension": "",
        "metric": "请求参数时长",
        "value": "15",
        "unit": "秒",
        "statement": "vedio-aigc系统请求参数时长为15秒。",
        "evidence_quote": source,
        "confidence": "high",
    }

    accepted, rejected = _validated_data_facts([fact], source)

    assert accepted == []
    assert rejected == 1


def test_accepts_concrete_business_scene_even_when_target_ends_with_parameter_config():
    source = "MemoryBread 官网首屏视频已配置为15秒、画幅16：9。"
    facts = [
        {
            "title": "MemoryBread 官网首屏视频生成时长",
            "subject": "MemoryBread 官网首屏视频",
            "action": "配置",
            "target_context": "MemoryBread 官网首屏视频参数配置",
            "dimension": "",
            "metric": "生成时长",
            "value": "15",
            "unit": "秒",
            "statement": "MemoryBread 官网首屏视频生成时长为15秒。",
            "evidence_quote": source,
            "confidence": "high",
        },
        {
            "title": "MemoryBread 官网首屏视频画幅比例",
            "subject": "MemoryBread 官网首屏视频",
            "action": "配置",
            "target_context": "MemoryBread 官网首屏视频参数配置",
            "dimension": "",
            "metric": "画幅比例",
            "value": "16:9",
            "unit": "",
            "statement": "MemoryBread 官网首屏视频画幅比例为16:9。",
            "evidence_quote": source,
            "confidence": "high",
        },
    ]

    accepted, rejected = _validated_data_facts(facts, source)

    assert rejected == 0
    assert [fact["value"] for fact in accepted] == ["15", "16:9"]


def test_accepts_dashboard_kpi_fact_as_business_data():
    source = "GPU 使用一览 在用项目数 102 总卡数（X40折算） 1803.59 年化总成本（万元） 12178.4万元 平均 ROI 39.86x"
    fact = {
        "title": "GPU 使用一览在用项目数",
        "subject": "GPU 使用一览",
        "action": "",
        "target_context": "",
        "dimension": "当前",
        "metric": "在用项目数",
        "value": "102",
        "unit": "",
        "statement": "GPU 使用一览当前在用项目数为 102。",
        "evidence_quote": "GPU 使用一览 在用项目数 102",
        "confidence": "high",
    }

    accepted, rejected = _validated_data_facts([fact], source)

    assert rejected == 0
    assert accepted[0]["metric"] == "在用项目数"


def test_realigns_evidence_for_an_arbitrary_report_metric_without_domain_rules():
    source = "服务质量概览 当前活跃节点 102 平均处理延迟 18.6 ms 错误率 0.3%"
    fact = {
        "title": "服务质量概览平均处理延迟",
        "subject": "服务质量概览",
        "action": "",
        "target_context": "",
        "dimension": "当前",
        "metric": "平均处理延迟",
        "value": "18.6",
        "unit": "ms",
        "statement": "服务质量概览当前平均处理延迟为 18.6 ms。",
        "evidence_quote": "服务质量概览：平均处理延迟为18.6ms。",
        "confidence": "high",
    }

    accepted, rejected = _validated_data_facts([fact], source)

    assert rejected == 0
    assert accepted[0]["evidence_quote"] == "服务质量概览 当前活跃节点 102 平均处理延迟 18.6 ms"


def test_keeps_flattened_ax_table_fact_when_model_rewrites_noncritical_fields():
    source = (
        "GPU使用一览在用项目数102总卡数（X40折算）1803.59"
        "年化总成本（万元）12178.4平均ROI39.86x"
    )
    fact = {
        "title": "电商 GPU 资源平均 ROI",
        "subject": "电商 GPU 资源看板",
        "action": "",
        "target_context": "GPU使用一览",
        "dimension": "当前阶段",
        "metric": "平均ROI",
        "value": "39.86",
        "unit": "倍",
        "statement": "电商 GPU 资源的平均 ROI 为 39.86 倍。",
        "evidence_quote": "GPU 资源看板的平均 ROI 是 39.86 倍",
        "confidence": "high",
    }

    accepted, rejected = _validated_data_facts([fact], source)

    assert rejected == 0
    assert accepted[0]["subject"] == "GPU使用一览"
    assert accepted[0]["evidence_quote"] == source[:-1]
    assert accepted[0]["unit"] == ""
    assert accepted[0]["dimension"] == ""


def test_persists_fact_contract_run_and_normalized_fact(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "facts.db"))
    conn.execute("CREATE TABLE timelines (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO timelines (id) VALUES (1416)")
    conn.execute(
        "CREATE TABLE data_snapshots ("
        "id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, "
        "collected_at INTEGER NOT NULL, observed_at INTEGER)"
    )
    migration = (
        Path(__file__).parents[2]
        / "shared"
        / "db-schema"
        / "migrations"
        / "068_timeline_data_facts.sql"
    )
    conn.executescript(migration.read_text(encoding="utf-8"))
    period_migration = (
        Path(__file__).parents[2]
        / "shared"
        / "db-schema"
        / "migrations"
        / "074_data_snapshot_period_history.sql"
    )
    conn.executescript(period_migration.read_text(encoding="utf-8"))
    fact_period_migration = (
        Path(__file__).parents[2]
        / "shared"
        / "db-schema"
        / "migrations"
        / "075_timeline_data_fact_period_history.sql"
    )
    conn.executescript(fact_period_migration.read_text(encoding="utf-8"))
    knowledge = {
        "data_fact_contract": DATA_FACT_CONTRACT_VERSION,
        "data_facts": [_valid_fact()],
        "data_fact_rejected_count": 2,
        "observed_at": 1_785_943_846_089,
    }

    BackgroundProcessor._save_timeline_data_facts(conn, 1416, knowledge, [20859])

    run = conn.execute(
        "SELECT contract_version, accepted_count, rejected_count FROM timeline_data_fact_runs WHERE timeline_id = 1416"
    ).fetchone()
    fact = conn.execute(
        "SELECT title, subject, target_context, metric, value, unit, source_capture_ids FROM timeline_data_facts WHERE timeline_id = 1416"
    ).fetchone()
    conn.close()

    assert run == (DATA_FACT_CONTRACT_VERSION, 1, 2)
    assert fact == (
        "生服模特库在电商AIGC中复用的成本节省金额",
        "生服模特库",
        "电商AIGC",
        "成本节省金额",
        "6.28",
        "万",
        "[20859]",
    )


@pytest.mark.live_data_facts
@pytest.mark.skipif(
    os.environ.get("MEMORY_BREAD_RUN_LIVE_DATA_FACT_EVAL") != "1",
    reason="真实模型金标回归仅在显式开启时执行",
)
def test_live_model_matches_timeline_data_fact_golden_corpus():
    corpus_path = Path(__file__).parent / "fixtures" / "timeline_data_fact_golden.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    extractor = KnowledgeExtractorV2(
        model=os.environ.get("MEMORY_BREAD_DATA_FACT_EVAL_MODEL", "qwen3.5:4b")
    )

    failures = []
    for case in corpus:
        last_failure = ""
        # 本地生成模型存在少量非确定空响应；正例允许一次重试，
        # 但计划/阈值反例必须首次就被门禁拦截。
        max_attempts = 2 if case["expected"] else 1
        for _attempt in range(max_attempts):
            facts, _rejected = extractor._recover_missing_data_facts(
                case["source_text"],
                case["timeline_context"],
                "golden:%s" % case["id"],
            )
            if len(facts) != len(case["expected"]):
                last_failure = "%s: expected %d facts, got %d: %r" % (
                    case["id"],
                    len(case["expected"]),
                    len(facts),
                    facts,
                )
                continue

            unmatched = []
            for expected in case["expected"]:
                accepted_values = expected.get("accepted_values") or [expected["value"]]
                accepted_values = [value.replace("：", ":") for value in accepted_values]
                matches = []
                for fact in facts:
                    actual_value = str(fact.get("value") or "").replace("：", ":")
                    semantic_text = " ".join(
                        str(fact.get(field) or "")
                        for field in ("title", "subject", "target_context", "metric")
                    )
                    if actual_value in accepted_values and all(
                        token in semantic_text for token in expected["semantic_tokens"]
                    ):
                        matches.append(fact)
                if not matches:
                    unmatched.append(expected)
            if not unmatched:
                last_failure = ""
                break
            last_failure = "%s: no facts matched %r in %r" % (
                case["id"],
                unmatched,
                facts,
            )

        if last_failure:
            failures.append(last_failure)

    assert failures == []
