"""Full-generation brief coverage cannot be replaced by a broad pass verdict."""
import copy
import json
import pytest
from creation.agent_loop import CreationAgentLoop, MAX_BRAINSTORM_CONTEXT_CHARS
from creation.brief_context import effective_brief_decisions
from creation.delivery_contract import review_delivery, with_brainstorm_coverage
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
    assert context["brainstorm_context_version"] == 3
    assert "delivery_checked_hash" not in state.environment
    bound = copy.deepcopy(state.environment["input_contract"])
    loop._input_context(state)
    assert state.environment["input_contract"] == bound
    _, prompt = loop._model_prompts(state, {"kind": "agent", "id": "document_writer_agent", "action": "writer"})
    assert all(entry["id"] in prompt for entry in context["brainstorm_decisions"])
    state.root_request = "超长根需求" * 14000
    prompt = loop._prompt_environment(state)
    assert "执行参数0，每批100份。" in prompt and "执行参数26，每批126份。" in prompt


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
