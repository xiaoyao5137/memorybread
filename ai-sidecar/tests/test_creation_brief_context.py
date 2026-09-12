"""Effective brainstorm decisions stay authoritative across legacy snapshots."""
import pytest

from creation.agent_loop import CreationAgentLoop


def decision(question_id, dimension, summary, source="user"):
    return {"question_id": question_id, "dimension": dimension,
            "summary": summary, "source": source}


@pytest.mark.parametrize("edited_root", [False, True])
def test_immediate_creation_after_source_limit_cannot_replay_cached_brief(edited_root):
    brief = {
        "root_request": "旧目标" if edited_root else "组织读书会，不使用个人资料",
        "brief_edits": {"root_request": "组织读书会，不使用个人资料"} if edited_root else {},
        "user_input_revisions": {"root_request": 3, "q-permission": 1},
        "decisions": [decision("q-permission", "PRIVATE-DIMENSION", "可以检索个人资料"),
                      decision("q-user", "PRIVATE-DIMENSION", "用户确认自由交流"),
                      decision("q-assumed", "PRIVATE-DIMENSION", "PRIVATE-ASSUMPTION", "agent_assumption")],
        "open_flags": ["PRIVATE-OPEN-FLAG"],
        "brief_markdown": "## 历史记忆参考\nPRIVATE-CACHED-EVIDENCE",
    }
    context = CreationAgentLoop._brainstorm_prompt_context(brief)
    assert "用户确认自由交流" in context
    assert "当前用户禁止使用个人或历史资料" in context
    assert "可以检索个人资料" not in context
    assert "PRIVATE-" not in context
    assert CreationAgentLoop._brainstorm_retrieval_context_terms(brief) == []


def test_source_permission_revision_survives_creation_handoff():
    brief = {"root_request": "不检索个人资料", "user_input_revisions": {"root_request": 0, "q": 2},
             "decisions": [decision("q", "资料范围", "现在可以检索个人资料")],
             "brief_markdown": "本轮已核对的历史参考"}
    assert "本轮已核对的历史参考" in CreationAgentLoop._brainstorm_prompt_context(brief)
    assert CreationAgentLoop._brainstorm_retrieval_context_terms(brief) == ["现在可以检索个人资料"]


def test_disabled_sources_preserve_explicit_clear_identity_and_override_old_root():
    brief = {"root_request": "组织活动，预算100万元，不检索个人资料",
             "decisions": [{**decision("q1", "预算上限", "旧摘要100万元"), "dimension_id": "resource_constraints"}],
             "brief_edits": {"q1": "", "open_flags": ""}}
    context = CreationAgentLoop._brainstorm_prompt_context(brief)
    assert '"question_id": "q1", "dimension": "预算上限"' in context
    assert "约束不再适用" in context
    assert "不代表已确认" in context and '"question_id": "open_flags"' in context
    assert "已确认（resource_constraints）" not in context
    assert "旧摘要100万元" not in context


def test_cleared_fields_remain_distinguishable_without_repeating_old_values():
    budget = {"decisions": [decision("q-budget", "预算上限", "100 万元")],
              "brief_edits": {"q-budget": ""}}
    deadline = {"decisions": [decision("q-date", "交付日期", "2026-10-01")],
                "brief_edits": {"q-date": ""}}
    budget_context = CreationAgentLoop._brainstorm_prompt_context(budget)
    deadline_context = CreationAgentLoop._brainstorm_prompt_context(deadline)
    assert budget_context != deadline_context
    assert "q-budget" in budget_context and "预算上限" in budget_context
    assert "q-date" in deadline_context and "交付日期" in deadline_context
    assert "100 万元" not in budget_context
    assert "2026-10-01" not in deadline_context


def test_old_manual_corrections_survive_the_recent_decision_window():
    brief = {
        "decisions": [decision("q-budget", "预算", "旧的预算"),
                      decision("q-owner", "负责人", "旧负责人")]
        + [decision("q-{}".format(i), "其他事项", "普通事实") for i in range(30)],
        "brief_edits": {"q-budget": "", "q-owner": "由当前项目组负责"},
    }
    context = CreationAgentLoop._brainstorm_prompt_context(brief)
    assert "q-budget" in context and "预算" in context
    assert "由当前项目组负责" in context
    assert "旧的预算" not in context and "旧负责人" not in context


def test_edited_brief_rebuilds_from_fields_without_legacy_markdown_overrides():
    brief = {
        "root_request": "旧的创作目标", "brief_edits": {
            "root_request": "用户新的创作目标", "q-budget": "", "q-owner": "新负责人",
            "open_flags": "", "q-old-assumption": "已确认的新机制",
        },
        "decisions": [decision("q-budget", "预算", "旧预算100"),
                      decision("q-owner", "负责人", "旧负责人"),
                      decision("q-old-assumption", "机制", "旧推测", "agent_assumption"),
                      decision("q-assumption", "规模", "需要核验的规模", "agent_assumption")],
        "open_flags": ["旧开放问题"],
        "brief_markdown": "# 创作简报\n旧的创作目标\n旧预算100\n旧负责人\n旧推测\n旧开放问题",
    }
    context = CreationAgentLoop._brainstorm_prompt_context(brief)
    for stale in ("旧的创作目标", "旧预算100", "旧负责人", "旧推测", "旧开放问题"):
        assert stale not in context
    assert "用户新的创作目标" in context
    assert "新负责人" in context and "已确认的新机制" in context
    assert "合理假设：\n- 规模：需要核验的规模" in context
    assert "q-budget" in context


def test_cleared_root_and_open_flags_are_explicit():
    context = CreationAgentLoop._brainstorm_prompt_context({
        "root_request": "旧目标", "brief_edits": {"root_request": "", "open_flags": ""},
        "open_flags": ["旧问题"], "brief_markdown": "旧目标\n旧问题",
    })
    assert "root_request" in context and "open_flags" in context
    assert "已清空" in context
    assert "旧目标" not in context and "旧问题" not in context


def test_retrieval_uses_manual_values_but_never_unconfirmed_assumptions_or_exclusions():
    brief = {
        "decisions": [decision("q-old", "方向", "过期方向"),
                      decision("q-empty", "范围", "已取消范围"),
                      decision("q-assumption", "目标", "未核验目标", "agent_assumption"),
                      decision("q-confirmed", "机制", "旧机制假设", "agent_assumption"),
                      decision("q-excluded", "成本", "排除范围", "user_excluded")],
        "brief_edits": {"q-old": "当前方向", "q-empty": "", "q-confirmed": "人工确认机制",
                        "q-excluded": "旧编辑不能覆盖排除状态"},
    }
    terms = CreationAgentLoop._brainstorm_retrieval_context_terms(brief)
    assert terms == ["当前方向", "人工确认机制"]
    context = CreationAgentLoop._brainstorm_prompt_context(brief)
    assert "旧编辑不能覆盖排除状态" not in context
    assert "用户明确排除的范围" in context and "排除范围" in context
    assert "合理假设：\n- 目标：未核验目标" in context


@pytest.mark.parametrize("brief", [None, [], "", {}])
def test_empty_or_non_object_brief_is_compatible(brief):
    assert CreationAgentLoop._brainstorm_prompt_context(brief) == ""
    assert CreationAgentLoop._brainstorm_retrieval_context_terms(brief) == []


def test_markdown_only_legacy_brief_is_retained_when_no_manual_override_exists():
    brief = {"brief_markdown": "## 历史记忆参考\n《项目会议》原始摘录。", "decisions": []}
    assert "《项目会议》原始摘录。" in CreationAgentLoop._brainstorm_prompt_context(brief)


def test_exploring_draft_uses_saved_answers_without_adopting_unanswered_options():
    brief = {
        "phase": "exploring", "revision": 3,
        "root_request": "为跨团队分享会写一版方案",
        "decisions": [decision("q-format", "活动形式", "圆桌讨论"),
                      decision("q-time", "活动时长", "预计一小时", "agent_assumption"),
                      decision("q-gifts", "礼品", "礼品采购", "user_excluded")],
        "current_question": {"id": "q-budget", "prompt": "活动预算是多少？", "required": True,
                             "options": [{"id": "high", "label": "尚未确认的十万元预算", "recommended": True}]},
        "open_flags": ["预算待确认"],
        "brief_markdown": "# 创作简报\n## 待决定\n- 活动预算是多少？",
    }
    context = CreationAgentLoop._brainstorm_prompt_context(brief)
    assert "已确认决策：\n- 活动形式：圆桌讨论" in context
    assert "合理假设：\n- 活动时长：预计一小时" in context
    assert "用户明确排除的范围" in context and "礼品采购" in context
    assert "开放事项：\n- 预算待确认" in context
    assert "开放事项不得擅自定论" in context
    assert "尚未确认的十万元预算" not in context
    assert CreationAgentLoop._brainstorm_retrieval_context_terms(brief) == ["圆桌讨论"]
    assert brief["phase"] == "exploring" and brief["revision"] == 3
    assert brief["current_question"]["id"] == "q-budget"
    assert len(brief["decisions"]) == 3
