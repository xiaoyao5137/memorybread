"""The content-only gap fallback requires saved, exact user decisions."""

from copy import deepcopy

import pytest

from creation.brainstorm_gap_delivery import retract_content_gap_preview


PERIOD = {
    "start": "2026-09-21",
    "end": "2026-09-27",
    "timezone": "Asia/Shanghai",
}
NEEDS = [
    {"id": "need-bill", "label": "GPU账单费用可核验来源", "status": "candidate"},
    {"id": "need-usage", "label": "GPU卡时可核验来源", "status": "candidate"},
    {"id": "need-utilization", "label": "GPU利用率与回收记录可核验来源", "status": "candidate"},
]
UNSAFE = (
    "# GPU成本优化周报\n\n"
    "电商项目的账单表明本周成本下降 15%。9 月 30 日采集的资料已确认覆盖本期。"
    "内部模型名 provider-model-secret。\n"
)


def make_brief():
    period = dict(PERIOD, source="user")
    stages = [
        {"id": "direction", "preparation_contract_version": 17},
        {
            "id": "detail",
            "preparation_contract_version": 17,
            "confirmed_period": period,
            "input_preflight": {
                "period": dict(PERIOD, source="user", status="confirmed"),
                "timezone": PERIOD["timezone"],
                "needs": deepcopy(NEEDS),
            },
        },
        {"id": "writing", "preparation_contract_version": 17},
    ]
    history = []
    for need in NEEDS:
        history.append({
            "question": {
                "input_clarification": {
                    "kind": "source_confirmation", "need_ids": [need["id"]],
                },
                "options": [
                    {
                        "id": "draft_with_gap",
                        "input_value": {
                            "action": "draft_with_gap",
                            "need_id": need["id"],
                            "target_period": dict(PERIOD),
                        },
                    },
                    {
                        "id": "confirm_candidate",
                        "input_value": {
                            "action": "confirm_candidate",
                            "need_id": need["id"],
                            "target_period": dict(PERIOD),
                        },
                    },
                ],
            },
            "answer": {"source": "user", "selected_option_ids": ["draft_with_gap"]},
        })
    return {"stage_workflow": {"version": 2, "stages": stages}, "history": history}


def test_all_gap_retracts_only_new_body_and_keeps_exact_needs():
    brief = make_brief()
    protected = "# Earlier paid chapter\n\nConfirmed historic text.\n"
    revised, edits, audit = retract_content_gap_preview(
        protected + UNSAFE, brief, [], protected_base=protected,
    )
    assert revised.startswith(protected)
    assert revised[:len(protected)] == protected
    assert "2026-09-21 至 2026-09-27" in revised
    for need in NEEDS:
        assert need["label"] in revised
        assert need["id"] in audit["need_ids"]
    assert "本期数据、变化及结果：待核实" in revised
    assert "成本下降 15%" not in revised
    assert "电商项目" not in revised
    assert "已确认覆盖本期" not in revised
    assert "provider-model-secret" not in revised
    assert len(edits) == 1
    assert audit["status"] == "converted"
    assert audit["protected_base_preserved"] is True


def test_authorization_probe_and_idempotence():
    brief = make_brief()
    preview, edits, audit = retract_content_gap_preview("", brief, [])
    assert audit["status"] == "converted"
    assert edits
    assert preview.startswith("# 报告草稿（待核实）")
    same, second_edits, second_audit = retract_content_gap_preview(preview, brief, [])
    assert same == preview
    assert second_edits == []
    assert second_audit["status"] == "converted"
    assert second_audit["already_safe"] is True


def test_first_chapter_gap_has_current_heading_in_multi_chapter_report():
    brief = make_brief()
    brief["chapter_context"] = {
        "id": "cost", "title": "账单与用量", "goal": "核对账单",
        "index": 1, "total": 2, "confirmed_decisions": [],
        "outline": [
            {"id": "cost", "title": "账单与用量", "goal": "核对账单"},
            {"id": "action", "title": "优化动作", "goal": "核对动作"},
        ],
    }
    revised, _, audit = retract_content_gap_preview("", brief, [])
    assert audit["status"] == "converted"
    assert revised.startswith("# 报告草稿（待核实）\n\n## 账单与用量\n\n")
    from creation.brainstorm_chapters import chapter_context, validate_chapter_document
    validate_chapter_document(revised, "", chapter_context(brief), completed=True)


def test_gap_draft_does_not_copy_unverified_user_decision_summary_as_fact():
    brief = make_brief()
    brief["decisions"] = [
        {"source": "user", "dimension": "GPU 成本事实核查",
         "summary": "若有本期与可比期账单及同口径用量，才拆解费用变化；缺项列待核"},
        {"source": "user", "dimension": "GPU 成本事实核查",
         "summary": "若有本期与可比期账单及同口径用量，才拆解费用变化；缺项列待核"},
        {"source": "user", "dimension": "资料核对", "summary": "标明缺口先成稿"},
        {"source": "agent_assumption", "dimension": "错误事实",
         "summary": "本周节省 15%"},
    ]
    revised, _, audit = retract_content_gap_preview("", brief, [])
    assert audit["status"] == "converted"
    assert "若有本期与可比期账单" not in revised
    assert "可比口径一致时才计算变化" in revised
    assert "本周节省 15%" not in revised


def test_core_private_gap_contract_works_without_public_stage_workflow():
    brief = make_brief()
    detail = brief["stage_workflow"]["stages"][1]
    brief["confirmed_report_period"] = detail["confirmed_period"]
    brief["content_source_gap"] = {
        "schema_version": "brainstorm.content-source-gap.v1",
        "preparation_contract_version": 17,
        "confirmed_period": detail["confirmed_period"],
        "input_preflight": detail["input_preflight"],
    }
    del brief["stage_workflow"]

    revised, edits, audit = retract_content_gap_preview(UNSAFE, brief, [])

    assert audit["status"] == "converted"
    assert len(audit["need_ids"]) == 3
    assert "成本下降 15%" not in revised
    assert edits


def test_latest_answer_for_same_need_supersedes_saved_retry():
    brief = make_brief()
    old = deepcopy(brief["history"][0])
    old["question"]["id"] = "earlier-retry"
    old["question"]["options"] = [{"id": "retry", "input_value": {
        "action": "retry_local_recall", "need_id": NEEDS[0]["id"],
        "target_period": dict(PERIOD)}}]
    old["answer"]["selected_option_ids"] = ["retry"]
    brief["history"].insert(0, old)
    assert retract_content_gap_preview(UNSAFE, brief, [])[2]["status"] == "converted"
    brief["history"].append(deepcopy(brief["history"][1]))
    assert retract_content_gap_preview(UNSAFE, brief, [])[2]["status"] == "converted"

    later = deepcopy(old)
    later["question"]["id"] = "later-retry"
    brief["history"].append(later)
    assert retract_content_gap_preview(UNSAFE, brief, [])[2] == {
        "status": "invalid", "reason": "source_answer_not_gap"}


@pytest.mark.parametrize("mutate,reason", [
    (lambda b: b["history"][0]["answer"].update(source="agent_assumption"), "source_answer_not_user"),
    (lambda b: b["history"][0]["answer"].update(selected_option_ids=["confirm_candidate"]), "source_answer_not_gap"),
    (lambda b: b["history"][0]["question"]["options"][0]["input_value"].update(need_id="need-other"), "source_answer_not_gap"),
    (lambda b: b["history"][0]["question"]["options"][0]["input_value"]["target_period"].update(end="2026-09-28"), "source_answer_not_gap"),
    (lambda b: b["history"].pop(), "missing_source_answers"),
    (lambda b: b["history"][0]["question"]["input_clarification"].update(need_ids=["need-other"]), "missing_source_answers"),
    (lambda b: b["stage_workflow"]["stages"][1]["input_preflight"]["needs"].append({"id": "need-new", "label": "新缺口"}), "missing_source_answers"),
    (lambda b: b["stage_workflow"]["stages"][1]["input_preflight"]["period"].update(end="2026-09-28"), "preflight_period_mismatch"),
    (lambda b: b["stage_workflow"]["stages"][1]["confirmed_period"].update(end="2026-09-28"), "preflight_period_mismatch"),
    (lambda b: b.update(confirmed_report_period=dict(PERIOD, source="agent_assumption")), "brief_period_mismatch"),
])
def test_contract_tampering_does_not_transform(mutate, reason):
    brief = make_brief()
    mutate(brief)
    revised, edits, audit = retract_content_gap_preview(UNSAFE, brief, [])
    assert revised == UNSAFE
    assert edits == []
    assert audit == {"status": "invalid", "reason": reason}


def test_protected_base_mismatch_is_invalid_and_unchanged():
    revised, edits, audit = retract_content_gap_preview(
        UNSAFE, make_brief(), [], protected_base="# Another chapter\n",
    )
    assert revised == UNSAFE
    assert edits == []
    assert audit == {"status": "invalid", "reason": "protected_base_mismatch"}


@pytest.mark.parametrize("change,skills", [
    (lambda b: b["stage_workflow"].update(version=1), []),
    (lambda b: b["stage_workflow"]["stages"][0].update(preparation_contract_version=16), []),
    (lambda b: None, [{"id": "selected-skill"}]),
])
def test_other_contracts_and_skill_sessions_are_not_changed(change, skills):
    brief = make_brief()
    change(brief)
    revised, edits, audit = retract_content_gap_preview(UNSAFE, brief, skills)
    assert revised == UNSAFE
    assert edits == []
    assert audit == {"status": "not_applicable"}
