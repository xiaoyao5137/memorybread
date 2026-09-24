"""Full-generation brief coverage cannot be replaced by a broad pass verdict."""
import copy
import json
import pytest
from creation.agent_loop import CreationAgentLoop, MAX_BRAINSTORM_CONTEXT_CHARS
from creation.brief_context import effective_brief_decisions, effective_brief_open_flags
from creation.delivery_contract import (review_delivery, with_brainstorm_coverage,
                                        with_brainstorm_open_flag_safety)
from creation.operations import OperationError
from tests.test_creation_delivery_contract import DeliveryService, StreamService, contract, review_for_model
from tests.test_creation_operations import run_args


def brief_fixture(count=27):
    brief = {"root_request": "写完整活动方案", "phase": "exploring", "decisions": [], "history": []}
    for i in range(count):
        qid = "q{}".format(i)
        value = "确认方向{}".format(i)
        brief["decisions"].append({"question_id": qid, "dimension": "维度{}".format(i),
                                  "source": "user", "summary": value, "user_inputs": [value]})
        brief["history"].append({"question": {"id": qid, "options": [
            {"id": "selected", "label": value, "description": "执行参数{}，每批{}份。".format(i, 100 + i)},
            {"id": "unselected", "label": "不采用的方向", "description": "UNSELECTED_SECRET"}]},
            "answer": {"source": "user", "selected_option_ids": ["selected"]}})
    return brief


def test_all_27_choices_and_full_selected_description_survive_without_markdown():
    brief = brief_fixture()
    brief["history"][0]["question"]["options"][0]["description"] += "完整说明" * 90 + "尾部参数782"
    before = copy.deepcopy(brief)
    prompt = CreationAgentLoop._brainstorm_prompt_context(brief)
    assert all("确认方向{}".format(i) in prompt and "执行参数{}，".format(i) in prompt for i in range(27))
    assert "尾部参数782" in prompt and "UNSELECTED_SECRET" not in prompt
    assert len(prompt) < MAX_BRAINSTORM_CONTEXT_CHARS and brief == before


def test_oversize_confirmed_values_fail_instead_of_silently_dropping_choices():
    brief = brief_fixture(1)
    brief["history"][0]["question"]["options"][0]["description"] = "已确认内容" * MAX_BRAINSTORM_CONTEXT_CHARS
    with pytest.raises(OperationError, match="上下文容量"):
        CreationAgentLoop._brainstorm_prompt_context(brief)


def test_generated_contract_is_complete_idempotent_and_ignores_non_user_entries():
    entries = effective_brief_decisions(brief_fixture())
    entries += [{**entries[0], "id": "excluded", "source": "user_excluded"},
                {**entries[1], "id": "assumption", "source": "agent_assumption"}]
    bound = with_brainstorm_coverage(contract(), entries, "generate", "完整活动目标")
    assert len(bound["acceptance"]) == 29
    assert {entry["id"] for entry in entries[:27]} <= {check["id"] for check in bound["acceptance"]}
    assert "执行参数0，每批100份。" in json.dumps(bound, ensure_ascii=False)
    assert bound == with_brainstorm_coverage(bound, entries, "generate", "完整活动目标")
    assert all(check["id"] not in {"excluded", "assumption"} for check in bound["acceptance"])


def test_open_flags_are_preserved_as_questions_not_turned_into_implicit_answers():
    original = contract()
    flags = ["预售目标是多少？", "- 可用预算是多少？", "预售目标是多少？"]
    bound = with_brainstorm_open_flag_safety(original, flags)
    assert original == contract()
    assert bound == with_brainstorm_open_flag_safety(bound, flags)
    assert all("不得因未代用户回答而判失败" in item["criterion"]
               for item in bound["acceptance"] if item["id"] != "brainstorm_open_flags")
    assert bound["acceptance"][-1] == {"id": "brainstorm_open_flags", "criterion":
        "以待确认状态保留这些未回答问题，不得写成用户已选方案或既定事实：预售目标是多少？；可用预算是多少？"}


def test_resolved_open_flags_remove_their_obsolete_acceptance_contract():
    stale = with_brainstorm_open_flag_safety(contract(), ["自动化边界选哪一种？"])
    assert stale["acceptance"][-1]["id"] == "brainstorm_open_flags"
    rebound = with_brainstorm_open_flag_safety(stale, [])
    assert all(item["id"] != "brainstorm_open_flags" for item in rebound["acceptance"])
    assert "open_flags" not in json.dumps(rebound["acceptance"], ensure_ascii=False)


def test_generic_continue_does_not_turn_open_flags_into_data_dependencies():
    original = contract()
    original["inputs"].append({"id": "business_data", "source": "business_data",
        "need": "预算", "reason": "开放项推导", "state": "missing", "evidence": "", "query": "查询预算"})
    bound = with_brainstorm_open_flag_safety(original, ["预算是多少？"], "transform", "继续生成")
    assert bound["inputs"][0]["state"] == "not_needed" and not bound["inputs"][0]["query"]
    explicit = with_brainstorm_open_flag_safety(original, ["预算是多少？"], "transform", "继续生成并查询预算")
    assert explicit["inputs"][0]["state"] == "missing" and explicit["inputs"][0]["query"] == "查询预算"


@pytest.mark.parametrize("operation", ["transform", "answer", "patch", "execute_skill"])
def test_partial_operations_do_not_acquire_full_brief_rewrite_requirements(operation):
    original = contract()
    assert with_brainstorm_coverage(original, effective_brief_decisions(brief_fixture()), operation) == original


@pytest.mark.asyncio
async def test_broad_pass_cannot_replace_per_selection_review():
    brief = brief_fixture()
    bound = with_brainstorm_coverage(contract(), effective_brief_decisions(brief), "generate")
    response = json.dumps(review_for_model(document="line-1"))
    service = StreamService([response, response])
    with pytest.raises(OperationError, match="核验最终"):
        await review_delivery(service, "按简报完整成文", "仅提及一个方向", bound, {
            "input_context": {"creation_brief": CreationAgentLoop._brainstorm_prompt_context(brief)}})
    payload = json.loads(service.calls[0]["user_prompt"])
    assert all(entry["id"] in payload["required_check_ids"] for entry in effective_brief_decisions(brief))
    assert len(service.calls) == 2


@pytest.mark.parametrize("context_version", [None, 2])
def test_legacy_checkpoint_upgrades_brief_contract_and_invalidates_old_pass(context_version):
    loop = CreationAgentLoop(DeliveryService())
    brief = brief_fixture()
    state = loop._new_state(**run_args("", "按简报生成"), model_mode="local", creation_mode="brainstorm", creation_brief=brief)
    state.environment.update(input_context={"schema_version": "creation.input-context.v1", "root_request": "旧上下文根目标",
        "conversation": [], "creation_brief": "只有最近的标签", "brainstorm_context_version": context_version}, input_contract=contract(),
        operation={"kind": "generate"}, delivery_checked_hash="stale-pass")
    context = loop._input_context(state)
    assert context["root_request"] == "旧上下文根目标"
    assert "执行参数0，每批100份。" in context["creation_brief"]
    assert len(context["brainstorm_decisions"]) == 27
    assert context["brainstorm_context_version"] == 4
    assert "delivery_checked_hash" not in state.environment
    bound = copy.deepcopy(state.environment["input_contract"])
    loop._input_context(state)
    assert state.environment["input_contract"] == bound
    _, prompt = loop._model_prompts(state, {"kind": "agent", "id": "document_writer_agent", "action": "writer"})
    assert all(entry["id"] in prompt for entry in context["brainstorm_decisions"])
    state.root_request = "超长根需求" * 14000
    prompt = loop._prompt_environment(state)
    assert "执行参数0，每批100份。" in prompt and "执行参数26，每批126份。" in prompt


def test_open_flag_contract_upgrade_discards_failures_from_obsolete_acceptance():
    loop = CreationAgentLoop(DeliveryService())
    brief = brief_fixture(1)
    brief["open_flags"] = ["预算是多少？"]
    state = loop._new_state(**run_args("已有正文", "继续生成"), model_mode="local",
                            creation_mode="brainstorm", creation_brief=brief)
    state.environment.update(input_contract=contract(), operation={"kind": "transform"},
        brainstorm_acceptance_version=3, delivery_repair_count=2,
        delivery_review={"status": "revise"}, delivery_pending_review={"checks": []},
        delivery_last_revise={"corrections": ["旧规则要求代答"]}, delivery_checked_hash="stale")
    loop._input_context(state)
    assert state.environment["brainstorm_acceptance_version"] == 7
    assert state.environment["delivery_repair_count"] == 0
    assert "delivery_review" not in state.environment
    assert "delivery_pending_review" not in state.environment
    assert "delivery_last_revise" not in state.environment
    assert "delivery_checked_hash" not in state.environment
    assert state.environment["input_contract"]["acceptance"][-1]["id"] == "brainstorm_open_flags"


def test_repeated_current_question_cannot_restore_flags_over_confirmed_choice():
    brief = brief_fixture(1)
    selected = brief["history"][0]["question"]["options"][0]
    brief["current_question"] = {
        "id": "stale-repeat", "prompt": "换种说法再次确认同一选择？",
        "options": [
            {"id": "renamed", "label": selected["label"], "description": selected["description"]},
            {"id": "other", "label": "另一个方向", "description": "这是另一个足够具体的候选方向说明。"},
        ],
    }
    brief["open_flags"] = ["同一选择是否仍待确认", "同一模式之间的影响差异"]
    assert effective_brief_open_flags(brief) == []
    prompt = CreationAgentLoop._brainstorm_prompt_context(brief)
    assert "开放事项：\n" not in prompt and "同一选择是否仍待确认" not in prompt

    # A manual edit is explicit user input and must win over stale-state cleanup.
    brief["brief_edits"] = {"open_flags": "用户明确保留的问题"}
    assert effective_brief_open_flags(brief) == ["用户明确保留的问题"]


@pytest.mark.asyncio
async def test_large_choice_review_batches_keep_tail_failures_and_audit_sources_once(monkeypatch):
    import creation.delivery_contract as module
    entries = effective_brief_decisions(brief_fixture())
    conditions = with_brainstorm_coverage(contract(), entries, "generate", "根目标")
    calls = []
    async def batch(service, instruction, document, conditions, environment, audit_sources=True, failure_scope=None):
        calls.append((conditions, document, audit_sources, failure_scope))
        checks = [{"id": item["id"], "passed": item["id"] != entries[-1]["id"],
                   "reason": "尾部遗漏" if item["id"] == entries[-1]["id"] else "已覆盖", "evidence": document}
                  for item in conditions["acceptance"]]
        failed = any(not check["passed"] for check in checks)
        return {"status": "revise" if failed else "pass", "checks": checks,
                "corrections": ["补全尾部选择"] if failed else []}
    monkeypatch.setattr(module, "_review_delivery_batch", batch)
    env = {"input_context": {"brainstorm_decisions": entries}}
    result = await module.review_delivery(None, "生成", "同一份原文", conditions, env)
    assert len(calls) == 5 and [call[2] for call in calls] == [True, False, False, False, False]
    assert [len(call[0]["acceptance"]) for call in calls[1:]] == [8, 8, 8, 3]
    assert all(call[1] == "同一份原文" and call[3] == conditions for call in calls)
    assert result["status"] == "revise" and result["checks"][-1]["id"] == entries[-1]["id"]
    assert len(result["checks"]) == len(conditions["acceptance"])
    assert env["delivery_pending_review"]["checks"][-1]["id"] == entries[-1]["id"]
