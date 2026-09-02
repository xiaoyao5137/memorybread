"""从冻结评测 JSON 生成阶段 A 验收报告。Python 3.9 兼容。"""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def pct(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return "%.2f%%" % (value * 100)


def gate(name: str, actual: Any, passed: bool, requirement: str) -> Dict[str, Any]:
    return {
        "name": name,
        "actual": actual,
        "passed": passed,
        "requirement": requirement,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deterministic", required=True, type=Path)
    parser.add_argument("--live", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    deterministic = json.loads(args.deterministic.read_text(encoding="utf-8"))
    live = json.loads(args.live.read_text(encoding="utf-8"))
    corpus = deterministic["corpus"]
    prototype = deterministic["prototype"]
    baseline = deterministic["baseline"]
    live_metrics = live["metrics"]
    extraction = live["extraction_metrics"]

    gates = [
        gate("语料规模", corpus["total"], corpus["total"] >= 300, ">= 300"),
        gate(
            "来源覆盖",
            len(corpus["source_counts"]),
            len(corpus["source_counts"]) == 6
            and min(corpus["source_counts"].values()) >= 1,
            "六类来源均覆盖",
        ),
        gate(
            "确定性门禁误纳率",
            pct(prototype["false_admission_rate"]),
            prototype["false_admission_rate"] <= 0.01,
            "<= 1%",
        ),
        gate(
            "确定性门禁保留率",
            pct(prototype["retention_rate"]),
            prototype["retention_rate"] >= 0.90,
            ">= 90%",
        ),
        gate(
            "端到端误纳率",
            pct(live_metrics["false_admission_rate"]),
            live_metrics["false_admission_rate"] <= 0.01,
            "<= 1%",
        ),
        gate(
            "端到端保留率",
            pct(live_metrics["retention_rate"]),
            live_metrics["retention_rate"] >= 0.90,
            ">= 90%",
        ),
        gate(
            "引用回查率",
            pct(extraction["grounded_quote_rate"]),
            extraction["grounded_quote_rate"] == 1.0,
            "100%",
        ),
        gate(
            "日期角色准确率",
            pct(extraction["date_role_exact_rate"]),
            extraction["date_role_exact_rate"] >= 0.98,
            ">= 98%",
        ),
        gate(
            "状态不增强率",
            pct(extraction["status_non_enhancement_rate"]),
            extraction["status_non_enhancement_rate"] >= 0.99,
            ">= 99%",
        ),
        gate(
            "模型传输/协议错误",
            len(live["transport_errors"]),
            len(live["transport_errors"]) == 0,
            "0",
        ),
    ]
    automated_passed = all(item["passed"] for item in gates)
    human_gate = {
        "name": "独立人工双标",
        "actual": "未执行",
        "passed": False,
        "requirement": "关键集双人独立标注，Cohen's kappa >= 0.80",
    }

    family_failures = Counter(
        item["scenario_family"] for item in live_metrics.get("failures") or []
    )
    usage = live.get("usage") or {}
    duration_seconds = float(usage.get("total_duration_ns") or 0) / 1_000_000_000
    count = int(live.get("selected_count") or 0)
    avg_seconds = duration_seconds / count if count else 0
    prompt_tokens = int(usage.get("prompt_eval_count") or 0)
    output_tokens = int(usage.get("eval_count") or 0)
    avg_prompt_tokens = prompt_tokens / count if count else 0
    avg_output_tokens = output_tokens / count if count else 0

    lines: List[str] = [
        "# 时间证据长期建设：阶段 A 验收报告",
        "",
        "> 后续决策：方案已于 2026-08-29 放弃；不启动阶段 A.1 或阶段 B-E。本报告仅作为历史验收证据保留。",
        "",
        "生成时间：%s。阶段 A 不接入产品运行链路、不迁移用户数据库。"
        % datetime.now(timezone.utc).isoformat(),
        "",
        "## 1. 验收结论",
        "",
    ]
    if automated_passed:
        lines.extend(
            [
                "自动化技术门槛全部通过，但独立人工双标尚未执行，因此阶段 A 为 **有条件通过**，不得直接进入阶段 B。完成或经决策人明确豁免人工门后再复核。",
            ]
        )
    else:
        lines.extend(
            [
                "阶段 A **未通过进入阶段 B 的质量门**。失败项见下表；不得以确定性门禁满分替代端到端模型识别结果。独立人工双标也尚未执行。",
            ]
        )
    lines.extend(
        [
            "",
            "## 2. 自动化质量门",
            "",
            "| 质量门 | 实际 | 要求 | 结果 |",
            "| --- | ---: | ---: | --- |",
        ]
    )
    for item in gates:
        lines.append(
            "| %s | %s | %s | %s |"
            % (
                item["name"],
                item["actual"],
                item["requirement"],
                "通过" if item["passed"] else "未通过",
            )
        )
    lines.extend(
        [
            "",
            "## 3. 人工标注门",
            "",
            "| 质量门 | 实际 | 要求 | 结果 |",
            "| --- | --- | --- | --- |",
            "| %s | %s | %s | 未通过 |"
            % (human_gate["name"], human_gate["actual"], human_gate["requirement"]),
            "",
            "仓库内 360 条语料是合成契约语料。自动模板标签和模型输出不能冒充两名独立人工标注者；私人真实样本也未写入仓库。",
            "",
            "## 4. 基线与原型",
            "",
            "- 语料：%d 条；开发集 %d，冻结集 %d；六类来源各 %d 条。"
            % (
                corpus["total"],
                corpus["split_counts"]["development"],
                corpus["split_counts"]["holdout"],
                min(corpus["source_counts"].values()),
            ),
            "- 文档级日期基线：准确率 %s，误纳 %d 条，保留率 %s。"
            % (
                pct(baseline["accuracy"]),
                baseline["false_admissions"],
                pct(baseline["retention_rate"]),
            ),
            "- 事项级确定性门禁（冻结候选）：准确率 %s，误纳 %d 条，保留率 %s。"
            % (
                pct(prototype["accuracy"]),
                prototype["false_admissions"],
                pct(prototype["retention_rate"]),
            ),
            "",
            "确定性门禁结果证明契约和程序规则能表达预期边界，不证明模型能正确生成候选；端到端结果必须单独看。",
            "",
            "## 5. 本地模型端到端结果",
            "",
            "- 样本：%d 条；总耗时 %.1f 秒，平均 %.2f 秒/条。"
            % (count, duration_seconds, avg_seconds),
            "- 输入 token：%d（平均 %.1f/条）；输出 token：%d（平均 %.1f/条）。"
            % (prompt_tokens, avg_prompt_tokens, output_tokens, avg_output_tokens),
            "- 成功模型调用 %d 次；失败后自适应拆分的调用 %d 次。"
            % (
                int(usage.get("successful_calls") or 0),
                int(usage.get("failed_calls") or 0),
            ),
            "- 决策准确率 %s；误纳率 %s；本期保留率 %s；弃权率 %s。"
            % (
                pct(live_metrics["accuracy"]),
                pct(live_metrics["false_admission_rate"]),
                pct(live_metrics["retention_rate"]),
                pct(live_metrics["abstention_rate"]),
            ),
            "- 引用回查率 %s；事件日期完全一致率 %s；日期角色一致率 %s；语态一致率 %s；状态不增强率 %s。"
            % (
                pct(extraction["grounded_quote_rate"]),
                pct(extraction["event_date_exact_rate"]),
                pct(extraction["date_role_exact_rate"]),
                pct(extraction["modality_exact_rate"]),
                pct(extraction["status_non_enhancement_rate"]),
            ),
            "",
            "这组数据是每条样本都进入模型的端到端压力测量，不是线上成本预测。线上设计先用来源元数据、内容哈希缓存和确定性规则，仅把事项时间不明确或证据冲突的增量内容送入模型；阶段 B 仍需测量实际触发率后才能给出容量和费用预算。",
            "",
            "### 失败分布",
            "",
        ]
    )
    if family_failures:
        lines.extend(["| 场景族 | 失败数 |", "| --- | ---: |"])
        for name, failure_count in sorted(family_failures.items()):
            lines.append("| `%s` | %d |" % (name, failure_count))
    else:
        lines.append("无决策失败。")
    lines.extend(
        [
            "",
            "## 6. 已完成交付物",
            "",
            "- 时间/事项规范性契约和 JSON Schema；",
            "- 标注指南、隐私规则和冻结策略；",
            "- 360 条合成原子事项及生成器；",
            "- 文档级基线、事项级门禁和真实本地模型评测器；",
            "- 数据库、索引、内部 API、迁移、开关和回滚详细设计；",
            "- Python 3.9 离线测试及可复现命令。",
            "",
            "## 7. 阶段 B 建议",
            "",
        ]
    )
    if automated_passed:
        lines.append(
            "暂不启动阶段 B。先完成独立人工双标；通过后可申请阶段 B 的影子双写，仍不得直接启用创作读取。"
        )
    else:
        lines.append(
            "不建议进入阶段 B。先在 development 集修复未通过的模型提取能力，冻结新版本后使用未见过的新 holdout 复测；禁止根据本报告失败样本继续调整后又复用同一 holdout 宣称通过。"
        )
    lines.extend(
        [
            "",
            "## 8. 验证边界",
            "",
            "- 本报告是合成语料上的阶段 A 证据，不代表任意私人文档绝不出错。",
            "- 未运行产品迁移、运行时集成、桌面 UI、备份恢复或最终文档复验；这些属于后续阶段。",
            "- `evaluation-live-model.json` 使用通用标签保存模型名称，不向客户端暴露供应商信息或成本。",
            "- 测试语料不包含本次问题中的真实项目、金额、人员、组织或 URL。",
            "",
        ]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(
                {
                    "schema_version": "memorybread.temporal-stage-a-acceptance.v1",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "automated_gates": gates,
                    "automated_gates_passed": automated_passed,
                    "human_double_annotation": human_gate,
                    "stage_b_entry_approved": automated_passed and human_gate["passed"],
                    "post_acceptance_decision": "abandoned",
                    "further_execution_authorized": False,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    print("wrote %s" % args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
