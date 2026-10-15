from creation.skill_period_recovery import plan_skill_period_recovery


def _step(step_id="meeting", objective="整理本周会议事实"):
    return {
        "action": "skill_step",
        "skill_id": "weekly-skill",
        "skill_step_id": step_id,
        "skill_step_objective": objective,
    }


def _output(content, step_id="meeting", objective="整理本周会议事实"):
    return {
        "skill_id": "weekly-skill",
        "step_id": step_id,
        "objective": objective,
        "content": content,
    }


def _plan(outputs, *, steps=None, confirmed=True, base="", persisted="", revision="none"):
    return plan_skill_period_recovery(
        {"period_start": "2026-09-21", "period_end": "2026-09-27"},
        steps if steps is not None else [_step()],
        outputs,
        target_confirmed=confirmed,
        chapter_base_document=base,
        persisted_document=persisted,
        user_revision_status=revision,
    )


def test_supersedes_unqualified_old_fact_in_current_week_section():
    plan = _plan([
        _output("## 本周会议结论\n- 会议于2026年8月17日召开，完成了方案评审。")
    ])

    assert plan["status"] == "rewrite"
    assert plan["supersede_step_ids"] == ["meeting"]
    assert plan["rewrite_step_ids"] == ["meeting"]
    assert plan["diagnostics"][0]["evidence"] == [{
        "line_number": 2,
        "fact_date": "2026-08-17",
        "reason": "unqualified_out_of_period_fact_in_current_section",
    }]


def test_qualified_historical_comparison_is_reviewed_without_supersession():
    plan = _plan([
        _output("## 本周进展\n- 历史对照：2026-08-17 的指标仅作参考。\n"
                "- 本周 2026-09-24 完成评审。")
    ])

    assert plan["status"] == "diagnostic"
    assert plan["supersede_step_ids"] == []
    assert plan["review_step_ids"] == ["meeting"]
    assert plan["diagnostics"][0]["reason"] == "historical_reference_requires_review"


def test_user_body_or_revision_prevents_automatic_rewrite():
    output = _output("## 本周进展\n- 2026-08-17 完成评审。")
    for protected in (
        {"base": "用户确认的前章"},
        {"persisted": "已提交的正文"},
        {"revision": "present"},
        {"revision": "unknown"},
    ):
        plan = _plan([output], **protected)
        assert plan["supersede_step_ids"] == []
        assert plan["diagnostics"][0]["reason"] in {
            "protected_document_present", "user_revision_present_or_unknown"
        }


def test_execution_day_default_is_not_a_confirmed_target_period():
    plan = _plan([
        _output("## 本周结果\n- 2026-08-17 已完成。")
    ], confirmed=False)

    assert plan["supersede_step_ids"] == []
    assert plan["diagnostics"] == [{"reason": "target_period_not_confirmed"}]


def test_only_frozen_current_period_steps_can_be_superseded():
    outputs = [
        _output("## 本周结果\n- 2026-08-17 已完成。", "meeting"),
        _output("## 历史脉络\n- 2026-08-17 已完成。", "background", "整理历史脉络"),
        _output("## 本周结果\n- 2026-08-17 已完成。", "unfrozen"),
    ]
    plan = _plan(outputs, steps=[_step(), _step("background", "整理历史脉络")])

    assert plan["supersede_step_ids"] == ["meeting"]
    assert {item.get("reason") for item in plan["diagnostics"]} == {
        "stale_current_fact", "frozen_step_not_found"
    }


def test_ambiguous_relative_date_does_not_trigger_rewrite():
    plan = _plan([_output("## 本周结果\n- 8月17日已完成，但年份未给出。")])

    assert plan["status"] == "diagnostic"
    assert plan["supersede_step_ids"] == []
    assert plan["diagnostics"][0]["reason"] == "period_not_proven_by_step_output"


def test_invalid_period_fails_closed():
    plan = plan_skill_period_recovery(
        {"period_start": "2026-09-27", "period_end": "2026-09-21"},
        [_step()],
        [_output("## 本周结果\n- 2026-08-17 已完成。")],
        target_confirmed=True,
        chapter_base_document="",
        persisted_document="",
        user_revision_status="none",
    )

    assert plan["supersede_step_ids"] == []
    assert plan["diagnostics"] == [{"reason": "target_period_invalid"}]
