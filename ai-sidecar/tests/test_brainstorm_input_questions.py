"""Input clarification questions are proposed from user scope and local evidence."""

import copy

import pytest

from creation.brainstorm_input_preflight import build_input_needs
from creation.brainstorm_input_questions import plan_input_clarification_questions


SKILL = {"id": "creation-skill-gpu-cost-weekly-report", "title": "GPU成本优化周报模板",
         "skillDescription": {"documentTypes": ["周报"]},
         "executionSteps": [
             {"id": "collect-inputs", "title": "本周大模型性能成本优化周会会议纪要",
              "objective": "用@记忆搜索 Tool 获取本周大模型性能成本优化周会会议纪要", "tools": ["memory_search"]},
             {"id": "summarize-progress", "title": "AIGC进度总结",
              "objective": "用@记忆搜索 Tool 获取本周AIGC共建项目的进展", "tools": ["memory_search"]},
             {"id": "build-metrics-table", "title": "GPU算力数据",
              "objective": "用@数据检索 Tool 获取电商GPU信息平台的数据：① GPU使用报表先切换日期；② 项目用量报表按年化成本排序",
              "tools": ["data_search"]},
             {"id": "custom-step-4", "title": "Token数据",
              "objective": "用@数据检索 Tool 获取LangBridge模型中心运营看板中的Token数据", "tools": ["data_search"]},
         ]}
NEED_IDS = [item["id"] for item in build_input_needs([SKILL])]
assert len(NEED_IDS) == 5


def plan(root="请生成GPU成本优化的周报", skills=None, decisions=None, preflight=None,
         today="2026-09-28"):
    return plan_input_clarification_questions(root_request=root,
        selected_skills=[SKILL] if skills is None else skills,
        confirmed_decisions=[] if decisions is None else decisions,
        preflight=preflight, as_of_date=today)


def needs(*statuses):
    assert len(statuses) == len(NEED_IDS)
    return [{"id": need_id, "label": "资料" + str(index), "status": status}
            for index, (need_id, status) in enumerate(zip(NEED_IDS, statuses), 1)]


def confirmed_period():
    return {"status": "confirmed", "start": "2026-09-21", "end": "2026-09-27",
            "source": "user", "candidate_ranges": []}


def test_weekly_skill_gets_period_before_layout_and_preserves_seventeen_answers():
    decisions = [{"question_id": "existing_" + str(index), "answer_source": "user",
                  "answer": "已确认"} for index in range(17)]
    original = copy.deepcopy(decisions)
    result = plan(decisions=decisions, preflight={"period": {"status": "unconfirmed"}})
    assert decisions == original
    assert result["status"] == "question"
    assert result["period"]["status"] == "unconfirmed"
    assert len(result["questions"]) == 1
    question = result["questions"][0]
    assert question["input_clarification"] == {"kind": "report_period", "need_ids": NEED_IDS}
    assert question["required"] is True and question["allow_custom"] is True
    assert [item["input_value"] for item in question["options"]] == [
        {"start": "2026-09-21", "end": "2026-09-27", "timezone": "Asia/Shanghai"},
        {"start": "2026-09-28", "end": "2026-10-04", "timezone": "Asia/Shanghai"},
    ]
    assert [item["label"] for item in question["options"]] == [
        "最近完整一周（2026-09-21 至 2026-09-27）",
        "本周（2026-09-28 至 2026-10-04）",
    ]
    assert all(not item["recommended"] for item in question["options"])
    assert result == plan(decisions=decisions, preflight={"period": {"status": "unconfirmed"}})


def test_history_188_actual_aigc_subneeds_are_both_bound_to_period():
    skill = copy.deepcopy(SKILL)
    skill["executionSteps"][1]["objective"] = (
        "用@记忆搜索 Tool 工具获取本周AIGC共建项目的进展，以及AIGC 共建项目周报里关于"
        "推理性能优化相关的进展，并以列表形式展示，最多15行文字")
    expected = build_input_needs([skill])
    result = plan(skills=[skill])
    assert len(expected) == 6
    assert result["questions"][0]["input_clarification"]["need_ids"] == [
        item["id"] for item in expected]
    assert len([item for item in expected if item["skill_step_id"] == "summarize-progress"]) == 2


@pytest.mark.parametrize("root,expected", [
    ("请写2026年9月21日至2026年9月27日的周报", ("2026-09-21", "2026-09-27")),
    ("请写2026年9月21日至9月27日的周报", ("2026-09-21", "2026-09-27")),
    ("请写2026年第39周周报", ("2026-09-21", "2026-09-27")),
    ("请写本周周报", ("2026-09-28", "2026-10-04")),
    ("请写上周周报", ("2026-09-21", "2026-09-27")),
])
def test_explicit_user_week_is_resolved_without_reasking(root, expected):
    result = plan(root=root)
    assert result["status"] == "awaiting_preflight"
    assert result["questions"] == []
    assert (result["period"]["range"]["start"], result["period"]["range"]["end"]) == expected
    assert result["period"]["source"] == "root_request"


def test_skill_template_this_week_does_not_select_a_week_for_user():
    result = plan(root="请生成GPU成本优化的周报")
    assert result["status"] == "question"
    assert result["period"]["status"] == "unconfirmed"


def test_unrelated_task_does_not_get_weekly_period_question():
    result = plan(root="请写产品接入说明", skills=[])
    assert result == {"status": "not_applicable", "period": {"status": "not_applicable"},
                      "questions": []}


def test_custom_answer_requires_full_unambiguous_range():
    marker = {"kind": "report_period", "need_ids": NEED_IDS}
    result = plan(decisions=[{"input_clarification": marker, "answer_source": "user",
                              "custom_text": "最近一周吧"}])
    assert result["status"] == "question"
    result = plan(decisions=[{"input_clarification": marker, "answer_source": "user",
                              "custom_text": "2026-09-21 至 2026-09-27"}])
    assert result["status"] == "awaiting_preflight"
    assert result["period"]["range"]["start"] == "2026-09-21"


def test_period_selected_option_is_structured_user_answer_not_recommendation():
    option = plan()["questions"][0]["options"][1]
    result = plan(decisions=[{"input_clarification": {"kind": "report_period"},
                              "answer_source": "user", "selected_options": [option]}])
    assert result["period"] == {"status": "confirmed", "range": option["input_value"],
                                "source": "user_answer"}


def test_matched_local_sources_do_not_prompt_for_links_or_screenshots():
    result = plan(preflight={"period": confirmed_period(),
                             "needs": needs("matched", "matched", "matched", "matched", "matched")})
    assert result["status"] == "ready"
    assert result["questions"] == []


def test_only_actual_unresolved_sources_get_questions_and_keep_receipt_dates():
    items = needs("matched", "partial", "candidate", "missing", "matched")
    items[2]["receipts"] = [{"source_id": 925, "title": "AIGC周会记录",
                             "source_period": {"start": "2026-09-22", "end": "2026-09-22"},
                             "period_status": "matched", "observed_at": "2026-09-28"}]
    original = copy.deepcopy(items)
    result = plan(preflight={"period": confirmed_period(), "needs": items})
    assert items == original
    assert result["status"] == "question"
    assert [question["input_clarification"]["need_ids"] for question in result["questions"]] == [
        [NEED_IDS[1]], [NEED_IDS[2]], [NEED_IDS[3]]]
    candidate = result["questions"][1]
    assert "2026-09-22" in candidate["context_details"]
    assert "2026-09-28" in candidate["context_details"]
    assert {key: candidate["options"][0]["input_value"][key]
            for key in ("action", "need_id", "source_id")} == {
        "action": "confirm_candidate", "need_id": NEED_IDS[2], "source_id": 925}
    assert candidate["options"][0]["input_value"]["target_period"]["start"] == "2026-09-21"
    assert all(not option["recommended"] for question in result["questions"]
               for option in question["options"])


@pytest.mark.parametrize("status,phrase", [("failed", "检索失败"),
                                             ("skipped", "读取已跳过")])
def test_failed_or_skipped_retrieval_is_not_described_as_missing(status, phrase):
    result = plan(preflight={"period": confirmed_period(),
                             "needs": needs("matched", status, "missing", "matched", "matched")})
    assert result["status"] == "question"
    question = result["questions"][0]
    assert phrase in question["context_details"]
    assert "未找到" not in question["context_details"]
    assert {item["input_value"]["action"] for item in question["options"]} == {
        "retry_local_recall", "draft_with_gap"}


def test_incomplete_preflight_cannot_say_ready():
    result = plan(preflight={"period": confirmed_period(),
                             "needs": needs("matched", "matched", "matched", "matched", "pending")})
    assert result["status"] == "awaiting_preflight"


def test_retrieval_attempt_changes_source_question_id_after_a_user_retry():
    prior = {"period": confirmed_period(), "needs": needs("missing", "matched", "matched", "matched", "matched"),
             "attempt": 0}
    later = copy.deepcopy(prior)
    later["attempt"] = 1
    a = plan(preflight=prior)["questions"][0]
    b = plan(preflight=later)["questions"][0]
    assert a["id"] != b["id"]
    assert a["input_clarification"] == b["input_clarification"]


def test_retry_keeps_other_confirmed_need_but_old_retry_is_not_replayed():
    prior = {"period": confirmed_period(), "needs": needs("missing", "missing", "matched", "matched", "matched"),
             "attempt": 0}
    first, second = plan(preflight=prior)["questions"]
    decisions = [
        {"question_id": first["id"], "input_clarification": first["input_clarification"],
         "answer_source": "user", "selected_options": [first["options"][-1]]},
        {"question_id": second["id"], "input_clarification": second["input_clarification"],
         "answer_source": "user", "selected_options": [second["options"][-2]]},
    ]
    assert plan(preflight=prior, decisions=decisions)["status"] == "retry_preflight"
    later = copy.deepcopy(prior)
    later["attempt"] = 1
    replanned = plan(preflight=later, decisions=decisions)
    assert replanned["status"] == "question"
    assert [item["input_clarification"]["need_ids"] for item in replanned["questions"]] == [
        [NEED_IDS[1]]]


def test_mismatched_source_date_cannot_be_confirmed_as_target_period():
    items = needs("matched", "matched", "candidate", "matched", "matched")
    items[2]["receipts"] = [{"source_id": 925, "title": "旧周会记录",
                             "source_period": {"start": "2026-09-22", "end": "2026-09-22"},
                             "period_status": "mismatch", "observed_at": "2026-09-28"}]
    result = plan(preflight={"period": {"status": "confirmed", "start": "2026-09-28",
                                            "end": "2026-10-04", "source": "user"}, "needs": items})
    assert [item["input_value"]["action"] for item in result["questions"][0]["options"]] == [
        "retry_local_recall", "draft_with_gap"]


def test_title_date_conflict_blocks_quick_confirmation_without_claiming_a_source_period():
    items = needs("candidate", "matched", "matched", "matched", "matched")
    items[0]["receipts"] = [{"source_id": 925, "title": "模型数据 - 周会会议纪要 -6 月",
                            "source_period": None, "period_status": "unknown"}]
    result = plan(preflight={"period": confirmed_period(), "needs": items})
    question = result["questions"][0]
    assert [item["input_value"]["action"] for item in question["options"]] == [
        "retry_local_recall", "draft_with_gap"]
    assert "题名日期与目标周期不符" in question["context_details"]
    assert "统计期未标明" in question["context_details"]
    assert items[0]["receipts"][0]["source_period"] is None


def test_title_conflict_filters_before_three_candidate_limit_and_rechecks_new_period():
    items = needs("candidate", "matched", "matched", "matched", "matched")
    items[0]["receipts"] = [
        {"source_id": index, "title": "2026年6月周会纪要", "period_status": "unknown"}
        for index in range(3)] + [
        {"source_id": 4, "title": "近期周会纪要", "period_status": "unknown"}]
    current = plan(preflight={"period": confirmed_period(), "needs": items})
    options = [item for item in current["questions"][0]["options"]
               if item["input_value"]["action"] == "confirm_candidate"]
    assert [item["input_value"]["source_id"] for item in options] == [4]
    june = copy.deepcopy(items)
    period = {"status": "confirmed", "start": "2026-06-01", "end": "2026-06-30",
              "source": "user", "candidate_ranges": []}
    refreshed = plan(preflight={"period": period, "needs": june})
    options = [item for item in refreshed["questions"][0]["options"]
               if item["input_value"]["action"] == "confirm_candidate"]
    assert [item["input_value"]["source_id"] for item in options] == [0, 1, 2]


def test_ambiguous_multi_date_title_does_not_create_false_conflict():
    items = needs("candidate", "matched", "matched", "matched", "matched")
    items[0]["receipts"] = [{"source_id": 5, "title": "2026年6月与9月纪要汇总",
                            "period_status": "unknown"}]
    result = plan(preflight={"period": confirmed_period(), "needs": items})
    assert result["questions"][0]["options"][0]["input_value"]["action"] == "confirm_candidate"


def test_trusted_body_period_overrides_stale_title_hint():
    items = needs("candidate", "matched", "matched", "matched", "matched")
    items[0]["receipts"] = [{"source_id": 6, "title": "2026年6月迭代记录",
                            "source_period": {"start": "2026-09-21", "end": "2026-09-27"},
                            "period_status": "matched"}]
    result = plan(preflight={"period": confirmed_period(), "needs": items})
    assert result["questions"][0]["options"][0]["input_value"]["action"] == "confirm_candidate"


def test_source_answer_only_applies_to_same_need_period_and_receipt():
    items = needs("matched", "matched", "candidate", "matched", "matched")
    items[2]["receipts"] = [{"source_id": 925, "title": "AIGC周会记录"}]
    preflight = {"period": confirmed_period(), "needs": items}
    question = plan(preflight=preflight)["questions"][0]
    answer = {"question_id": question["id"], "input_clarification": question["input_clarification"],
              "answer_source": "user", "selected_options": [question["options"][0]]}
    assert plan(preflight=preflight, decisions=[answer])["status"] == "ready_with_gaps_or_attestation"
    changed = copy.deepcopy(preflight)
    changed["period"].update(start="2026-09-28", end="2026-10-04")
    assert plan(preflight=changed, decisions=[answer])["status"] == "question"
    replaced = copy.deepcopy(preflight)
    replaced["attempt"] = 1
    replaced["needs"][2]["receipts"][0]["source_snapshot_id"] = 999
    assert plan(preflight=replaced, decisions=[answer])["status"] == "question"


def test_explicit_gap_answer_survives_recall_attempt_without_reclassifying_missing():
    preflight = {"period": confirmed_period(),
                 "needs": needs("matched", "matched", "matched", "missing", "matched"),
                 "attempt": 0}
    question = plan(preflight=preflight)["questions"][0]
    selected = question["options"][-1]
    answer = {"question_id": question["id"], "input_clarification": question["input_clarification"],
              "answer_source": "user", "selected_options": [selected]}
    later = copy.deepcopy(preflight)
    later["attempt"] = 1
    result = plan(preflight=later, decisions=[answer])
    assert result["status"] == "ready_with_gaps_or_attestation"
    assert result["source_handlings"][NEED_IDS[3]] == "confirmed"
    assert later["needs"][3]["status"] == "missing"


def test_nonweekly_task_with_explicit_source_need_still_asks_after_local_preflight():
    result = plan(root="请写项目方案", skills=[], preflight={"needs": [
        {"id": "meeting", "label": "项目纪要", "status": "missing"}],
        "period": {"status": "confirmed", "start": "2026-09-01", "end": "2026-09-28",
                   "source": "user"}})
    assert result["status"] == "question"
    assert result["questions"][0]["input_clarification"] == {
        "kind": "source_confirmation", "need_ids": ["meeting"]}


def test_nonweekly_source_gap_without_period_never_mentions_target_week():
    result = plan(root="请写项目方案", skills=[], preflight={"period": {"status": "not_required"},
        "needs": [{"id": "meeting", "label": "项目纪要", "status": "missing"}]})
    assert result["status"] == "question"
    question = result["questions"][0]
    assert "目标周" not in question["prompt"] + question["context_details"]
    assert question["input_clarification"]["need_ids"] == ["meeting"]


def test_monthly_preflight_candidates_use_its_frozen_calendar_ranges():
    result = plan(root="请写月报", skills=[], preflight={
        "period": {"status": "unconfirmed", "cadence": "monthly", "candidate_ranges": [
            {"kind": "latest_completed_month", "start": "2026-08-01", "end": "2026-08-31"},
            {"kind": "current_month", "start": "2026-09-01", "end": "2026-09-30"}]},
        "needs": [{"id": "meeting", "status": "candidate"}],
        "timezone": "Asia/Shanghai"})
    assert result["status"] == "question"
    assert "报告" in result["questions"][0]["prompt"]
    assert [item["input_value"]["end"] for item in result["questions"][0]["options"]] == [
        "2026-08-31", "2026-09-30"]
