"""生成阶段 A 的合成时间证据冻结语料。Python 3.9 兼容。"""

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List

SCHEMA_VERSION = "memorybread.temporal-evidence-stage-a.v1"


def candidate(
    claim_text: str,
    evidence_quote: str,
    claim_kind: str,
    modality: str,
    status: str,
    event_date: Any,
    date_role: str,
    time_basis: str,
    has_conflict: bool = False,
) -> Dict[str, Any]:
    return {
        "claim_text": claim_text,
        "evidence_quote": evidence_quote,
        "claim_kind": claim_kind,
        "modality": modality,
        "status": status,
        "event_date": event_date,
        "date_role": date_role,
        "time_basis": time_basis,
        "has_conflict": has_conflict,
    }


def family_cases(project: str, metric_value: int) -> List[Dict[str, Any]]:
    """每个项目生成 30 个覆盖不同时间/状态语义的原子事项。"""
    return [
        {
            "family": "explicit_completed_in_period",
            "purpose": "period_progress",
            "source": "2026-08-20，%s 已完成接口验收。" % project,
            "candidate": candidate(
                "%s 完成接口验收" % project,
                "2026-08-20，%s 已完成接口验收。" % project,
                "progress_event",
                "actual",
                "completed",
                "2026-08-20",
                "completion",
                "explicit_text",
            ),
            "decision": "eligible",
        },
        {
            "family": "explicit_completed_outside_period",
            "purpose": "period_progress",
            "source": "2026-02-06，%s 已完成接口验收。" % project,
            "candidate": candidate(
                "%s 完成接口验收" % project,
                "2026-02-06，%s 已完成接口验收。" % project,
                "progress_event",
                "actual",
                "completed",
                "2026-02-06",
                "completion",
                "explicit_text",
            ),
            "decision": "background_only",
        },
        {
            "family": "planned_deadline_in_period",
            "purpose": "period_progress",
            "source": "%s 计划于 2026-08-22 完成接口验收，目前尚未开始。" % project,
            "candidate": candidate(
                "%s 计划完成接口验收" % project,
                "%s 计划于 2026-08-22 完成接口验收，目前尚未开始。" % project,
                "goal",
                "planned",
                "planned",
                None,
                "deadline",
                "explicit_text",
            ),
            "decision": "background_only",
        },
        {
            "family": "target_horizon_in_period",
            "purpose": "period_progress",
            "source": "%s 的目标是在 2026-08-23 前把处理耗时降低 20%%。" % project,
            "candidate": candidate(
                "%s 目标降低处理耗时" % project,
                "%s 的目标是在 2026-08-23 前把处理耗时降低 20%%。" % project,
                "goal",
                "target",
                "planned",
                None,
                "goal_horizon",
                "explicit_text",
            ),
            "decision": "background_only",
        },
        {
            "family": "observation_time_only",
            "purpose": "period_progress",
            "source": "%s 仍处于建设中，正文没有提供启动或完成日期。" % project,
            "candidate": candidate(
                "%s 仍在建设" % project,
                "%s 仍处于建设中，正文没有提供启动或完成日期。" % project,
                "current_status",
                "actual",
                "in_progress",
                None,
                "observation",
                "observation_time",
            ),
            "decision": "background_only",
        },
        {
            "family": "revision_date_distracts_historical_event",
            "purpose": "period_progress",
            "source": (
                "文档修订日期：2026-08-20。历史回顾：2026-02-06，%s 已完成接口验收。"
                % project
            ),
            "candidate": candidate(
                "%s 完成接口验收" % project,
                "历史回顾：2026-02-06，%s 已完成接口验收。" % project,
                "progress_event",
                "actual",
                "completed",
                "2026-02-06",
                "completion",
                "explicit_text",
            ),
            "decision": "background_only",
        },
        {
            "family": "meeting_date_with_prior_completion",
            "purpose": "period_progress",
            "source": (
                "会议时间：2026-08-20。会上回顾，%s 已于 2026-02-06 完成接口验收。"
                % project
            ),
            "candidate": candidate(
                "%s 完成接口验收" % project,
                "会上回顾，%s 已于 2026-02-06 完成接口验收。" % project,
                "progress_event",
                "actual",
                "completed",
                "2026-02-06",
                "completion",
                "explicit_text",
            ),
            "decision": "background_only",
        },
        {
            "family": "meeting_context_inherited_completion",
            "purpose": "period_progress",
            "source": "会议时间：2026-08-20。\n本周进展\n- %s 已完成接口验收。"
            % project,
            "candidate": candidate(
                "%s 完成接口验收" % project,
                "%s 已完成接口验收。" % project,
                "progress_event",
                "actual",
                "completed",
                "2026-08-20",
                "completion",
                "structural_context",
            ),
            "decision": "eligible",
        },
        {
            "family": "mixed_document_target_is_historical",
            "purpose": "period_progress",
            "source": (
                "2026-08-19，另一事项完成评审。2025-12-01，%s 已完成接口验收。"
                % project
            ),
            "candidate": candidate(
                "%s 完成接口验收" % project,
                "2025-12-01，%s 已完成接口验收。" % project,
                "progress_event",
                "actual",
                "completed",
                "2025-12-01",
                "completion",
                "explicit_text",
            ),
            "decision": "background_only",
        },
        {
            "family": "old_document_contains_new_event",
            "purpose": "period_progress",
            "source": (
                "文档创建于 2025-10-30。新增记录：2026-08-21，%s 已完成接口验收。"
                % project
            ),
            "candidate": candidate(
                "%s 完成接口验收" % project,
                "新增记录：2026-08-21，%s 已完成接口验收。" % project,
                "progress_event",
                "actual",
                "completed",
                "2026-08-21",
                "completion",
                "explicit_text",
            ),
            "decision": "eligible",
        },
        {
            "family": "annual_forecast_without_event",
            "purpose": "period_progress",
            "source": "%s 预计全年可节省 %d 万元，当前为测算目标。"
            % (project, metric_value),
            "candidate": candidate(
                "%s 全年节省预测" % project,
                "%s 预计全年可节省 %d 万元，当前为测算目标。" % (project, metric_value),
                "forecast",
                "forecast",
                "planned",
                None,
                "goal_horizon",
                "unknown",
            ),
            "decision": "background_only",
        },
        {
            "family": "current_completed_status_without_change_date",
            "purpose": "period_progress",
            "source": "%s 当前状态为已完成，但资料没有说明完成日期。" % project,
            "candidate": candidate(
                "%s 当前已完成" % project,
                "%s 当前状态为已完成，但资料没有说明完成日期。" % project,
                "current_status",
                "actual",
                "completed",
                None,
                "unknown",
                "unknown",
            ),
            "decision": "background_only",
        },
        {
            "family": "relative_this_week_with_meeting_anchor",
            "purpose": "period_progress",
            "source": "会议时间：2026-08-20。本周 %s 已完成接口验收。" % project,
            "candidate": candidate(
                "%s 本周完成接口验收" % project,
                "本周 %s 已完成接口验收。" % project,
                "progress_event",
                "actual",
                "completed",
                "2026-08-20",
                "completion",
                "relative_to_source_anchor",
            ),
            "decision": "eligible",
        },
        {
            "family": "relative_previous_month_with_anchor",
            "purpose": "period_progress",
            "source": "会议时间：2026-08-20。上月 %s 已完成接口验收。" % project,
            "candidate": candidate(
                "%s 上月完成接口验收" % project,
                "上月 %s 已完成接口验收。" % project,
                "progress_event",
                "actual",
                "completed",
                "2026-07-31",
                "completion",
                "relative_to_source_anchor",
            ),
            "decision": "background_only",
        },
        {
            "family": "conflicting_event_dates",
            "purpose": "period_progress",
            "source": (
                "记录甲称 2026-08-20 %s 完成验收；记录乙称该事项在 2026-08-25 才完成。"
                % project
            ),
            "candidate": candidate(
                "%s 完成验收日期冲突" % project,
                "记录甲称 2026-08-20 %s 完成验收；记录乙称该事项在 2026-08-25 才完成。"
                % project,
                "progress_event",
                "actual",
                "completed",
                None,
                "unknown",
                "conflict",
                True,
            ),
            "decision": "conflict",
        },
        {
            "family": "cumulative_metric_not_period_progress",
            "purpose": "period_progress",
            "source": "截至 2026-08-20，%s 累计节省 %d 万元。"
            % (project, metric_value),
            "candidate": candidate(
                "%s 累计节省金额" % project,
                "截至 2026-08-20，%s 累计节省 %d 万元。" % (project, metric_value),
                "metric",
                "actual",
                "unknown",
                "2026-08-20",
                "statistical_period",
                "explicit_text",
            ),
            "decision": "background_only",
        },
        {
            "family": "actual_metric_in_period_for_metric_section",
            "purpose": "metric",
            "source": "统计周期 2026-08-20，%s 当日处理量为 %d 万次。"
            % (project, metric_value),
            "candidate": candidate(
                "%s 当日处理量" % project,
                "统计周期 2026-08-20，%s 当日处理量为 %d 万次。"
                % (project, metric_value),
                "metric",
                "actual",
                "unknown",
                "2026-08-20",
                "statistical_period",
                "explicit_text",
            ),
            "decision": "eligible",
        },
        {
            "family": "revision_date_only_for_target",
            "purpose": "period_progress",
            "source": "修订日期：2026-08-20。%s 的年度目标是降低处理耗时。" % project,
            "candidate": candidate(
                "%s 年度目标" % project,
                "%s 的年度目标是降低处理耗时。" % project,
                "goal",
                "target",
                "planned",
                None,
                "revision",
                "structural_context",
            ),
            "decision": "background_only",
        },
        {
            "family": "deadline_in_period_still_pending",
            "purpose": "period_progress",
            "source": "%s 截止日期为 2026-08-21，目前仍在建设中。" % project,
            "candidate": candidate(
                "%s 尚未完成" % project,
                "%s 截止日期为 2026-08-21，目前仍在建设中。" % project,
                "current_status",
                "actual",
                "in_progress",
                None,
                "deadline",
                "explicit_text",
            ),
            "decision": "background_only",
        },
        {
            "family": "cancelled_status_change_in_period",
            "purpose": "period_progress",
            "source": "2026-08-22，团队决定取消 %s。" % project,
            "candidate": candidate(
                "%s 被取消" % project,
                "2026-08-22，团队决定取消 %s。" % project,
                "status_change",
                "actual",
                "cancelled",
                "2026-08-22",
                "status_change",
                "explicit_text",
            ),
            "decision": "eligible",
        },
        {
            "family": "blocked_status_change_in_period",
            "purpose": "period_progress",
            "source": "2026-08-21，%s 因外部依赖进入阻塞状态。" % project,
            "candidate": candidate(
                "%s 进入阻塞" % project,
                "2026-08-21，%s 因外部依赖进入阻塞状态。" % project,
                "status_change",
                "actual",
                "blocked",
                "2026-08-21",
                "status_change",
                "explicit_text",
            ),
            "decision": "eligible",
        },
        {
            "family": "relative_time_without_anchor",
            "purpose": "period_progress",
            "source": "上周 %s 已完成接口验收，但资料没有发布日期或会议日期。"
            % project,
            "candidate": candidate(
                "%s 上周完成接口验收" % project,
                "上周 %s 已完成接口验收，但资料没有发布日期或会议日期。" % project,
                "progress_event",
                "actual",
                "completed",
                None,
                "unknown",
                "unknown",
            ),
            "decision": "unresolved",
        },
        {
            "family": "hallucinated_quote",
            "purpose": "period_progress",
            "source": "2026-08-20，%s 仍在建设中。" % project,
            "candidate": candidate(
                "%s 完成接口验收" % project,
                "2026-08-20，%s 已完成接口验收。" % project,
                "progress_event",
                "actual",
                "completed",
                "2026-08-20",
                "completion",
                "explicit_text",
            ),
            "decision": "unresolved",
            "live_expected": {
                "decision": "background_only",
                "claim_kind": "current_status",
                "modality": "actual",
                "status": "in_progress",
                "event_date": None,
                "date_role": "observation",
            },
        },
        {
            "family": "status_semantic_enhancement",
            "purpose": "period_progress",
            "source": "2026-08-20，%s 正在推进接口设计。" % project,
            "candidate": candidate(
                "%s 完成接口设计" % project,
                "2026-08-20，%s 正在推进接口设计。" % project,
                "progress_event",
                "actual",
                "completed",
                "2026-08-20",
                "completion",
                "explicit_text",
            ),
            "decision": "unresolved",
            "live_expected": {
                "decision": "eligible",
                "claim_kind": "progress_event",
                "modality": "actual",
                "status": "in_progress",
                "event_date": "2026-08-20",
                "date_role": "event",
            },
        },
        {
            "family": "historical_claim_for_background_task",
            "purpose": "background",
            "source": "2025-12-01，%s 完成了首版架构设计。" % project,
            "candidate": candidate(
                "%s 完成首版架构设计" % project,
                "2025-12-01，%s 完成了首版架构设计。" % project,
                "progress_event",
                "actual",
                "completed",
                "2025-12-01",
                "completion",
                "explicit_text",
            ),
            "decision": "eligible",
        },
        {
            "family": "target_metric_for_metric_section",
            "purpose": "metric",
            "source": "%s 的年度目标处理量为 %d 万次。" % (project, metric_value),
            "candidate": candidate(
                "%s 年度目标处理量" % project,
                "%s 的年度目标处理量为 %d 万次。" % (project, metric_value),
                "goal",
                "target",
                "planned",
                None,
                "goal_horizon",
                "unknown",
            ),
            "decision": "background_only",
        },
        {
            "family": "actual_metric_outside_period",
            "purpose": "metric",
            "source": "统计周期 2026-02-06，%s 当日处理量为 %d 万次。"
            % (project, metric_value),
            "candidate": candidate(
                "%s 当日处理量" % project,
                "统计周期 2026-02-06，%s 当日处理量为 %d 万次。"
                % (project, metric_value),
                "metric",
                "actual",
                "unknown",
                "2026-02-06",
                "statistical_period",
                "explicit_text",
            ),
            "decision": "background_only",
        },
        {
            "family": "publication_date_not_event_date",
            "purpose": "period_progress",
            "source": "发布日期：2026-08-20。%s 当前处于建设中。" % project,
            "candidate": candidate(
                "%s 当前处于建设中" % project,
                "%s 当前处于建设中。" % project,
                "current_status",
                "actual",
                "in_progress",
                None,
                "publication",
                "structural_context",
            ),
            "decision": "background_only",
        },
        {
            "family": "decision_made_in_period",
            "purpose": "period_progress",
            "source": "2026-08-19，团队决定 %s 采用新的缓存策略。" % project,
            "candidate": candidate(
                "%s 决定采用新的缓存策略" % project,
                "2026-08-19，团队决定 %s 采用新的缓存策略。" % project,
                "decision",
                "actual",
                "completed",
                "2026-08-19",
                "event",
                "explicit_text",
            ),
            "decision": "eligible",
        },
        {
            "family": "historical_decision_outside_period",
            "purpose": "period_progress",
            "source": "2026-02-05，团队决定 %s 采用新的缓存策略。" % project,
            "candidate": candidate(
                "%s 决定采用新的缓存策略" % project,
                "2026-02-05，团队决定 %s 采用新的缓存策略。" % project,
                "decision",
                "actual",
                "completed",
                "2026-02-05",
                "event",
                "explicit_text",
            ),
            "decision": "background_only",
        },
    ]


def generate() -> List[Dict[str, Any]]:
    records = []
    source_types = (
        "document",
        "meeting",
        "chat",
        "timeline",
        "work_memory",
        "data_snapshot",
    )
    for project_index in range(12):
        project = "示例项目%02d" % (project_index + 1)
        for family_index, raw in enumerate(family_cases(project, 120 + project_index)):
            case_index = project_index * 30 + family_index
            case_id = "%s_%02d" % (raw["family"], project_index + 1)
            records.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": case_id,
                    "split": "holdout" if case_index % 3 == 2 else "development",
                    "scenario_family": raw["family"],
                    "source_type": source_types[case_index % len(source_types)],
                    "source_text": raw["source"],
                    "metadata": {
                        "observed_at": "2026-08-27T10:00:00+08:00",
                        "source_created_at": None,
                        "source_modified_at": None,
                    },
                    "task": {
                        "period_start": "2026-08-17",
                        "period_end": "2026-08-23",
                        "timezone": "Asia/Shanghai",
                        "purpose": raw["purpose"],
                    },
                    "candidate": raw["candidate"],
                    "expected": {
                        "decision": raw["decision"],
                        "claim_kind": raw["candidate"]["claim_kind"],
                        "modality": raw["candidate"]["modality"],
                        "status": raw["candidate"]["status"],
                        "event_date": raw["candidate"]["event_date"],
                        "date_role": raw["candidate"]["date_role"],
                    },
                    "live_expected": raw.get("live_expected")
                    or {
                        "decision": raw["decision"],
                        "claim_kind": raw["candidate"]["claim_kind"],
                        "modality": raw["candidate"]["modality"],
                        "status": raw["candidate"]["status"],
                        "event_date": raw["candidate"]["event_date"],
                        "date_role": raw["candidate"]["date_role"],
                    },
                    "gold_provenance": "synthetic_contract_template_v1",
                }
            )
    return records


def write_review_sample(path: Path, records: List[Dict[str, Any]]) -> None:
    """输出不含答案的双标样本，每个场景族两条。"""
    family_counts = {}
    selected = []
    for record in records:
        family = record["scenario_family"]
        count = family_counts.get(family, 0)
        if count >= 2:
            continue
        family_counts[family] = count + 1
        selected.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "id",
                "source_type",
                "source_text",
                "period_start",
                "period_end",
                "timezone",
                "purpose",
                "evidence_quote",
                "claim_kind",
                "modality",
                "status",
                "event_date",
                "date_role",
                "time_basis",
                "decision",
                "annotator",
                "notes",
            ],
        )
        writer.writeheader()
        for record in selected:
            task = record["task"]
            writer.writerow(
                {
                    "id": record["id"],
                    "source_type": record["source_type"],
                    "source_text": record["source_text"],
                    "period_start": task["period_start"],
                    "period_end": task["period_end"],
                    "timezone": task["timezone"],
                    "purpose": task["purpose"],
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--review-output", type=Path)
    args = parser.parse_args()
    records = generate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    if args.review_output:
        write_review_sample(args.review_output, records)
    print("wrote %d cases to %s" % (len(records), args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
