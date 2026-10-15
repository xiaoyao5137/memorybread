"""A repeated step ID must not transfer deterministic rows between Skills."""

import asyncio

from creation.agent_loop import CreationAgentLoop
from tests.test_brainstorm_frozen_skill_binding import ReviewService, state_for


def _source():
    return {
        "source_id": 17, "title": "项目金额", "can_use": True,
        "structured_data": {
            "tables": [[["项目", "金额"], ["甲", "2"], ["乙", "3"]]],
            "pagination": {"dataset_complete": True, "pages_captured": 1,
                           "total_rows": 2},
        },
    }


def _shared_step(skill_id):
    return {
        "id": "creation_main_agent", "action": "skill_step",
        "skill_id": skill_id, "skill_step_id": "metrics",
        "skill_step_title": "金额排行",
        "skill_step_objective": "按金额降序取前1行",
    }


def _two_skill_state(loop):
    state = state_for(loop, support=False)
    state.selected_skills.append({
        "id": "other", "executionSteps": [{"id": "metrics", "title": "另一指标"}],
    })
    state.plan.extend([_shared_step("weekly"), _shared_step("other")])
    state.environment["current_data_results"] = [_source()]
    return state


def test_ranked_queries_keep_two_skills_with_same_step_id_separate():
    loop = CreationAgentLoop(ReviewService())
    state = _two_skill_state(loop)
    first = _shared_step("weekly")
    second = _shared_step("other")

    loop._ensure_explicit_ranked_queries(state, first)
    loop._ensure_explicit_ranked_queries(state, second)
    loop._ensure_explicit_ranked_queries(state, first)

    results = state.environment["data_query_results"]
    plans = state.environment["data_query_plans"]
    assert len(results) == len(plans) == 2
    assert {item["skill_id"] for item in results} == {"weekly", "other"}
    assert {item["skill_id"] for item in plans} == {"weekly", "other"}
    assert set(state.environment["explicit_ranked_query_receipts"]) == {
        "weekly:metrics", "other:metrics"}
    assert len(loop._prompt_query_results(results, first, state)) == 1
    assert loop._prompt_query_results(results, first, state)[0]["skill_id"] == "weekly"
    assert len(loop._prompt_query_results(results, second, state)) == 1
    assert loop._prompt_query_results(results, second, state)[0]["skill_id"] == "other"


def test_legacy_step_only_query_is_accepted_only_with_unique_owner():
    loop = CreationAgentLoop(ReviewService())
    single = state_for(loop, support=False)
    legacy = {"skill_step_id": "metrics", "shape": "table", "rows": []}
    assert len(loop._prompt_query_results([legacy], _shared_step("weekly"), single)) == 1

    ambiguous = _two_skill_state(loop)
    assert loop._prompt_query_results([legacy], _shared_step("weekly"), ambiguous) == []
    assert loop._prompt_query_results([legacy], _shared_step("other"), ambiguous) == []


def test_provisional_rows_are_waived_only_for_the_matching_skill(monkeypatch):
    loop = CreationAgentLoop(ReviewService())
    state = _two_skill_state(loop)
    receipts = [
        {"skill_id": "weekly", "step_id": "metrics", "title": "周报数据",
         "status": "completed_with_gap", "missing_inputs": []},
        {"skill_id": "other", "step_id": "metrics", "title": "其他数据",
         "status": "completed", "missing_inputs": []},
    ]
    monkeypatch.setattr(loop, "_frozen_skill_delivery_receipts", lambda _state: receipts)
    state.environment["data_query_results"] = [
        {"skill_id": "weekly", "skill_step_id": "metrics", "shape": "table", "rows": []},
        {"skill_id": "other", "skill_step_id": "metrics", "shape": "table", "rows": []},
        {"skill_step_id": "metrics", "shape": "table", "rows": []},
    ]
    checked = []
    monkeypatch.setattr(loop, "_data_query_result_gaps", lambda _doc, rows:
                        checked.extend(rows) or [])

    asyncio.run(loop._review_creation_delivery(state, "# 报告\n\n待核实"))

    assert state.environment["provisional_query_rows_not_required"] == ["weekly:metrics"]
    assert len(checked) == 2
    assert checked[0]["skill_id"] == "other"
    assert "skill_id" not in checked[1]
