import copy
import json

import pytest

from creation.brief_context import effective_brief_decisions


def brief_fixture():
    return {
        "root_request": "组织试验方案",
        "decisions": [{"question_id": "q1", "dimension": "试验安排", "source": "user",
                       "summary": "大样本统计；固定布光；自定义要求",
                       "user_inputs": ["大样本统计", "固定布光", "自定义要求"]}],
        "history": [{"question": {"id": "q1", "prompt": "旧问题背景不进入事实",
                    "details": "历史记忆不可直接作为决定",
                    "options": [
                        {"id": "large", "label": "大样本统计", "description": "生成1000条视频，分层覆盖各类目。", "details": "不应自动采用的选项依据"},
                        {"id": "light", "label": "固定布光", "description": "指定45度侧光与5600K色温。"},
                        {"id": "small", "label": "小样本", "description": "未选择的50条方案"}]},
                    "answer": {"selected_option_ids": ["large", "light"], "custom_text": "自定义要求", "source": "user"}}],
        "current_question": {"id": "next", "options": [{"label": "未回答选项"}]},
    }


def test_expands_selected_option_semantics_and_custom_without_unselected_evidence():
    brief = brief_fixture()
    baseline = copy.deepcopy(brief)
    entries = effective_brief_decisions(brief)
    assert [(item["value"], item["description"]) for item in entries] == [
        ("大样本统计", "生成1000条视频，分层覆盖各类目。"),
        ("固定布光", "指定45度侧光与5600K色温。"), ("自定义要求", "")]
    assert all(item["source"] == "user" for item in entries)
    rendered = json.dumps(entries, ensure_ascii=False)
    for forbidden in ("旧问题背景", "历史记忆", "选项依据", "50条", "未回答选项"):
        assert forbidden not in rendered
    assert brief == baseline


def test_all_decisions_and_full_values_survive_without_a_recent_items_limit():
    value = "完整约束" * 150
    brief = {"decisions": [{"question_id": "q{}".format(index), "source": "user", "summary": value + str(index)}
                           for index in range(50)]}
    entries = effective_brief_decisions(brief)
    assert len(entries) == 50
    assert entries[0]["value"] == value + "0"
    assert entries[-1]["value"] == value + "49"
    assert len({item["id"] for item in entries}) == 50


@pytest.mark.parametrize("edit,source", [("只生成200条", "user"), ("", "user_cleared")])
def test_edits_and_clears_override_old_selection_and_summary(edit, source):
    brief = brief_fixture()
    brief["brief_edits"] = {"q1": edit}
    entries = effective_brief_decisions(brief)
    assert len(entries) == 1
    assert entries[0]["source"] == source
    assert entries[0]["value"] == edit
    assert entries[0]["description"] == ""


@pytest.mark.parametrize("source", ["agent_assumption", "user_excluded"])
def test_non_user_decision_does_not_adopt_historical_option_details(source):
    brief = brief_fixture()
    brief["decisions"][0]["source"] = source
    if source == "user_excluded":
        brief["brief_edits"] = {"q1": "旧编辑不能恢复排除项"}
    entries = effective_brief_decisions(brief)
    assert len(entries) == 1
    assert entries[0]["source"] == source
    assert entries[0]["description"] == ""
    assert "旧编辑" not in entries[0]["value"]


def test_memory_restriction_keeps_only_direct_user_inputs_and_cleared_sources():
    brief = brief_fixture()
    brief["brief_edits"] = {"root_request": "组织试验方案，不检索个人资料"}
    brief["decisions"][0]["summary"] += "；历史汇总中的额外事实"
    brief["decisions"][0]["user_inputs"].append("现在可以检索个人资料")
    brief["decisions"].append({"question_id": "assumed", "summary": "历史假设", "source": "agent_assumption"})
    entries = effective_brief_decisions(brief)
    assert [item["value"] for item in entries] == ["大样本统计", "固定布光", "自定义要求"]
    assert all(item["description"] == "" for item in entries)
    assert "历史" not in json.dumps(entries, ensure_ascii=False)


def test_later_explicit_permission_restores_current_selected_descriptions():
    brief = brief_fixture()
    brief["root_request"] = "不检索个人资料"
    brief["decisions"].append({"question_id": "permission", "source": "user", "summary": "现在可以检索个人资料"})
    brief["user_input_revisions"] = {"root_request": 1, "permission": 2}
    entries = effective_brief_decisions(brief)
    assert entries[0]["description"] == "生成1000条视频，分层覆盖各类目。"


def test_effective_list_and_invalidations_bound_history_scope():
    brief = brief_fixture()
    brief["history"].append({"question": {"id": "obsolete", "options": [{"id": "o", "label": "陈旧方向", "description": "旧说明"}]},
                             "answer": {"selected_option_ids": ["o"]}})
    assert "陈旧" not in json.dumps(effective_brief_decisions(brief), ensure_ascii=False)
    brief["invalidated_question_ids"] = ["q1"]
    assert effective_brief_decisions(brief) == []


def test_option_identity_survives_reordering_and_duplicate_selected_ids():
    brief = brief_fixture()
    first = {item["value"]: item["id"] for item in effective_brief_decisions(brief)}
    brief["session_id"] = "do-not-expose-session"
    brief["history"][0]["answer"]["selected_option_ids"] = ["light", "large", "light", "missing"]
    brief["history"][0]["question"]["options"].reverse()
    entries = effective_brief_decisions(brief)
    assert {item["value"]: item["id"] for item in entries} == first
    assert len(entries) == 3
    assert "do-not-expose-session" not in json.dumps(entries)


def test_legacy_summaries_and_additional_user_inputs_have_no_invented_details():
    brief = {"decisions": [
        {"question_id": "legacy", "source": "user", "summary": "旧版完整回答"},
        {"question_id": "new", "source": "user", "summary": "精简摘要", "user_inputs": ["用户甲约束", "用户乙约束"]}]}
    entries = effective_brief_decisions(brief)
    assert [item["value"] for item in entries] == ["旧版完整回答", "用户甲约束", "用户乙约束"]
    assert all(item["description"] == "" for item in entries)


def test_stale_history_cannot_restore_choices_missing_from_current_user_inputs():
    brief = brief_fixture()
    brief["decisions"][0]["user_inputs"] = ["当前新方向"]
    brief["decisions"][0]["summary"] = "当前新方向"
    entries = effective_brief_decisions(brief)
    assert [(item["value"], item["description"]) for item in entries] == [("当前新方向", "")]


def test_partial_legacy_history_preserves_the_whole_saved_answer():
    brief = brief_fixture()
    del brief["decisions"][0]["user_inputs"]
    brief["history"][0]["question"]["options"] = brief["history"][0]["question"]["options"][:1]
    entries = effective_brief_decisions(brief)
    assert len(entries) == 1
    assert entries[0]["value"] == brief["decisions"][0]["summary"]
    assert entries[0]["description"] == ""


@pytest.mark.parametrize("brief", [None, [], {}, {"decisions": None}, {"decisions": [None, "invalid"]}])
def test_malformed_or_missing_decisions_are_not_mined_from_generated_prose(brief):
    assert effective_brief_decisions(brief) == []


def skipped_then_confirmed_fixture():
    placeholder = "暂未确定，生成时由创作 Agent 补充并保留为待核验假设"
    return {
        "root_request": "制定自动运营方案",
        "decisions": [
            {"question_id": "skipped", "dimension_id": "delivery_shape", "dimension": "运营产出",
             "source": "agent_assumption", "summary": placeholder},
            {"question_id": "confirmed", "dimension_id": "delivery_shape", "dimension": "运营产出",
             "source": "user", "summary": "自动选品与发布", "user_inputs": ["自动选品与发布"]}],
        "history": [
            {"question": {"id": "skipped", "parent_question_id": "root", "parent_option_id": "automation"},
             "answer": {"source": "agent_assumption", "selected_option_ids": [], "custom_text": placeholder}},
            {"question": {"id": "confirmed", "parent_question_id": "root", "parent_option_id": "efficiency",
                "options": [{"id": "automatic", "label": "自动选品与发布",
                             "description": "自动完成选品、脚本生成、视频发布全流程，以发布量和GMV贡献为度量。"}]},
             "answer": {"source": "user", "selected_option_ids": ["automatic"], "custom_text": ""}}],
        "brief_markdown": "旧渲染简报：运营产出属于合理假设。" + placeholder,
    }


def test_later_confirmation_retires_same_dimension_skip_placeholder_without_mutation():
    brief = skipped_then_confirmed_fixture()
    original = copy.deepcopy(brief)
    entries = effective_brief_decisions(brief)
    assert entries[0]["source"] == "superseded_assumption"
    assert entries[0]["superseded_by"] == "confirmed"
    assert entries[0]["value"] == original["decisions"][0]["summary"]
    assert entries[1]["source"] == "user"
    assert "发布量和GMV贡献" in entries[1]["description"]
    assert brief == original


@pytest.mark.parametrize("change", ["dimension", "parent", "stage", "missing_parent", "specific_assumption", "later_assumption"])
def test_confirmation_does_not_retire_a_different_or_uncertain_assumption_scope(change):
    brief = skipped_then_confirmed_fixture()
    if change == "dimension":
        brief["decisions"][0]["dimension_id"] = "separate_delivery_constraint"
    elif change == "parent":
        brief["history"][0]["question"]["parent_question_id"] = "detail_branch"
    elif change == "stage":
        brief["history"][0]["question"]["exploration_stage"] = "validation"
    elif change == "missing_parent":
        del brief["history"][0]["question"]["parent_question_id"]
    elif change == "specific_assumption":
        brief["decisions"][0]["summary"] = "额外预算暂按100元估算"
    elif change == "later_assumption":
        brief["decisions"].reverse()
    entries = effective_brief_decisions(brief)
    assert next(item for item in entries if item["question_id"] == "skipped")["source"] == "agent_assumption"


@pytest.mark.parametrize("source", ["agent_assumption", "user_excluded", "cleared"])
def test_unconfirmed_or_cleared_later_answer_does_not_resolve_a_placeholder(source):
    brief = skipped_then_confirmed_fixture()
    if source == "cleared":
        brief["brief_edits"] = {"confirmed": ""}
    else:
        brief["decisions"][1]["source"] = source
    assert effective_brief_decisions(brief)[0]["source"] == "agent_assumption"


def test_manual_confirmation_has_precedence_but_manual_old_value_is_not_retired():
    brief = skipped_then_confirmed_fixture()
    brief["decisions"][1]["source"] = "agent_assumption"
    brief["brief_edits"] = {"confirmed": "用户明确的完整安排"}
    assert effective_brief_decisions(brief)[0]["source"] == "superseded_assumption"
    brief["brief_edits"]["skipped"] = "另一个明确约束"
    assert effective_brief_decisions(brief)[0]["source"] == "user"


def test_renderer_does_not_reintroduce_retired_placeholder_from_old_markdown():
    from creation.agent_loop import CreationAgentLoop

    brief = skipped_then_confirmed_fixture()
    context = CreationAgentLoop._brainstorm_prompt_context(brief)
    assert "暂未确定，生成时由创作 Agent 补充" not in context
    assert "旧渲染简报" not in context
    assert "自动选品与发布" in context
    assert "发布量和GMV贡献" in context
