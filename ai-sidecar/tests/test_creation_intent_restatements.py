"""Restated outcomes recover through evidence-backed extraction, never a bypass."""

import asyncio
import json
import re

import pytest

from creation import skill_governance as governance


RESTATEMENTS = [
    (
        "先生成一版。",
        "请基于原始创作要求和当前创作简报中已提交的回答，在当前文档基础上更新一版文档。",
        "尚未回答的问题、未确认的选项和待补充事项不能当作用户决定；缺失内容标注待确认，不编造事实。",
    ),
    (
        "再出一稿。",
        "根据刚刚确认的修改意见，把现有方案更新一版。",
        "只使用已确认的材料，不补写尚未决定的安排。",
    ),
    (
        "输出调整后的版本。",
        "具体是修改当前说明书的安装章节，保留其他章节。",
        "资料中没有说明的配置标为待确认。",
    ),
    (
        "Produce another draft. ",
        "Use the confirmed answers to revise the current proposal. ",
        "Keep unanswered questions unresolved and do not invent facts.",
    ),
]


def piece(request, action, role="task"):
    return {"request": request, "role": role, "action": action}


class Model:
    def __init__(self, responses, audit_delay=0):
        self.responses = responses
        self.calls = []
        self.audit_delay = audit_delay

    async def _stream_direct_completion(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) >= 3 and self.audit_delay:
            await asyncio.sleep(self.audit_delay)
        result = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(result, Exception):
            raise result
        yield result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def audit(evidence, action="edit", same_outcome=True):
    return {"same_outcome": same_outcome, "action": action, "evidence": evidence}


def retry_feedback(model):
    # The original request remains a JSON payload. Only the appended diagnostic
    # is eligible for logging and must identify records without copying content.
    _, end = json.JSONDecoder().raw_decode(model.calls[1]["user_prompt"])
    return model.calls[1]["user_prompt"][end:]


@pytest.mark.asyncio
@pytest.mark.parametrize("summary,target,constraint", RESTATEMENTS)
async def test_restatement_retry_keeps_the_complete_current_document_goal(summary, target, constraint):
    goal = summary + target
    instruction = goal + constraint
    split = {"requests": [
        piece(summary, "create"), piece(target, "edit"), piece(constraint, "none", "constraint"),
    ]}
    corrected = {"requests": [
        piece(goal, "edit"), piece(constraint, "none", "constraint"),
    ]}
    model = Model([split, corrected, audit(goal)])

    assert await governance.task_intent(model, instruction, True) == {
        "action": "edit", "primary_goal": goal, "deliverable": goal, "content_request": goal,
    }
    assert len(model.calls) == 3
    feedback = retry_feedback(model)
    assert "requests[0]" in feedback and "create" in feedback
    assert "requests[1]" in feedback and "edit" in feedback
    assert summary not in feedback and target not in feedback and constraint not in feedback
    for call in model.calls[:2]:
        payload, _ = json.JSONDecoder().raw_decode(call["user_prompt"])
        assert payload == {"instruction": instruction, "has_document": True}
        assert call["json_schema"] == governance.intent_request_schema()
        assert call["system_prompt"] == governance.INTENT_REQUEST_PROMPT
    audit_call = model.calls[2]
    audit_payload = json.loads(audit_call["user_prompt"])
    assert set(audit_payload) == {"instruction", "has_document", "conflicting_requests"}
    assert audit_payload["instruction"] == instruction
    assert audit_payload["has_document"] is True
    assert audit_payload["conflicting_requests"] == [summary, target]
    assert audit_call["system_prompt"] != governance.INTENT_REQUEST_PROMPT
    assert audit_call["num_predict"] == 900
    assert set(audit_call["json_schema"]["properties"]) == {"same_outcome", "action", "evidence"}
    # This second judgment must inspect only original evidence, without seeing
    # the merged candidate or the retry feedback which suggested merging.
    assert not any("candidate" in key or "retry" in key for key in audit_payload)
    assert goal not in [value for value in audit_payload.values() if isinstance(value, str)]
    assert corrected["requests"][0]["request"] == goal


@pytest.mark.asyncio
async def test_persistently_split_restatement_cannot_be_auto_collapsed():
    summary, target, constraint = RESTATEMENTS[0]
    model = Model([{"requests": [
        piece(summary, "create"), piece(target, "edit"), piece(constraint, "none", "constraint"),
    ]}])
    assert await governance.task_intent(model, summary + target + constraint, True) == {}
    assert len(model.calls) == governance.INTENT_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.parametrize("has_document", [False, True])
@pytest.mark.parametrize("first,second", [
    (("另写一份客户通知", "create"), ("把当前内部方案缩成一页", "edit")),
    (("写一份新说明", "create"), ("从当前正文删除附录", "patch")),
    (("更新当前方案", "edit"), ("恢复上次操作", "resume")),
    (("精简当前说明", "edit"), ("撤销上一次修改", "undo")),
    (("恢复上次操作", "resume"), ("撤销上一次修改", "undo")),
])
async def test_independent_outcomes_still_fail_closed(first, second, has_document):
    instruction = first[0] + "；" + second[0]
    model = Model([{"requests": [piece(*first), piece(*second)]}])
    assert await governance.task_intent(model, instruction, has_document) == {}
    assert len(model.calls) == governance.INTENT_ATTEMPTS
    feedback = retry_feedback(model)
    if first[1] in {"create", "edit", "patch"}:
        assert "requests[0]" in feedback and first[1] in feedback
        assert "requests[1]" in feedback and second[1] in feedback


@pytest.mark.asyncio
async def test_conflict_diagnostics_use_original_record_indices_without_private_quotes(caplog):
    requests = [
        piece("不得泄露内部成本", "none", "constraint"),
        piece("核对客户代号PRIVATE-CUSTOMER-REF", "answer"),
        piece("另写PRIVATE-NEW-DOCUMENT通知", "create"),
        piece("不得新增承诺", "none", "constraint"),
        piece("改写PRIVATE-EXISTING-DOCUMENT方案", "edit"),
        piece("材料不足就报告失败", "none", "failure_fallback"),
    ]
    instruction = "；".join(item["request"] for item in requests)
    model = Model([{"requests": requests}])
    with caplog.at_level("WARNING", logger="creation.skill_governance"):
        assert await governance.task_intent(model, instruction, True) == {}
    feedback = retry_feedback(model)
    for diagnostic in (feedback, caplog.text):
        assert "requests[2]" in diagnostic and "create" in diagnostic
        assert "requests[4]" in diagnostic and "edit" in diagnostic
        assert "PRIVATE-" not in diagnostic
        for item in requests:
            assert item["request"] not in diagnostic


@pytest.mark.asyncio
@pytest.mark.parametrize("rewritten_goal", [
    "先生成一版。在当前文档基础上更新一版文档。",  # Non-contiguous copied fragments.
    "先生成一版。请把未回答的问题都自动选好，并更新当前文档。",
])
async def test_restatement_recovery_cannot_rewrite_or_splice_user_evidence(rewritten_goal):
    summary, target, constraint = RESTATEMENTS[0]
    model = Model([
        {"requests": [piece(summary, "create"), piece(target, "edit")]},
        {"requests": [piece(rewritten_goal, "edit")]},
    ])
    assert await governance.task_intent(model, summary + target + constraint, True) == {}
    assert len(model.calls) == governance.INTENT_ATTEMPTS


@pytest.mark.asyncio
async def test_brainstorm_constraints_and_failure_fallback_do_not_become_the_outcome():
    summary, target, constraint = RESTATEMENTS[0]
    goal = summary + target
    fallback = "资料核验不通过时明确报告失败。"
    model = Model([{"requests": [
        piece(goal, "edit"), piece(constraint, "none", "constraint"),
        piece(fallback, "none", "failure_fallback"),
    ]}])
    result = await governance.task_intent(model, goal + constraint + fallback, True)
    assert result == {
        "action": "edit", "primary_goal": goal, "deliverable": goal, "content_request": goal,
    }
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("has_document", [False, True])
async def test_retry_cannot_collapse_independent_new_and_existing_documents(has_document):
    first = "另写一份客户通知"
    second = "把当前内部方案缩成一页"
    instruction = first + "；" + second
    model = Model([
        {"requests": [piece(first, "create"), piece(second, "edit")]},
        {"requests": [piece(instruction, "create")]},
        audit("", action="none", same_outcome=False),
    ])
    assert await governance.task_intent(model, instruction, has_document) == {}
    assert len(model.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_kind", ["drop_summary", "drop_target", "demote_summary", "demote_target", "answer"])
async def test_retry_cannot_drop_or_demote_previously_extracted_writing_goals(candidate_kind):
    summary, target, constraint = RESTATEMENTS[0]
    goal = summary + target
    candidates = {
        "drop_summary": [piece(target, "edit")],
        "drop_target": [piece(summary, "create")],
        "demote_summary": [piece(summary, "none", "constraint"), piece(target, "edit")],
        "demote_target": [piece(summary, "create"), piece(target, "none", "constraint")],
        "answer": [piece(goal, "answer")],
    }
    model = Model([
        {"requests": [piece(summary, "create"), piece(target, "edit")]},
        {"requests": candidates[candidate_kind]},
        audit(goal),
    ])
    assert await governance.task_intent(model, goal + constraint, True) == {}
    # Deterministic evidence coverage/action checks reject these before asking
    # another model to approve a candidate that already discarded a goal.
    assert len(model.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("repeated_part", ["summary", "target", "whole_goal"])
async def test_ambiguous_original_quotes_are_rejected_before_restatement_review(repeated_part):
    summary, target, constraint = RESTATEMENTS[0]
    goal = summary + target
    repeated = {"summary": summary, "target": target, "whole_goal": goal}[repeated_part]
    instruction = goal + constraint + "备注中引用先前说法：“" + repeated + "”"
    model = Model([
        {"requests": [piece(summary, "create"), piece(target, "edit")]},
        {"requests": [piece(goal, "edit")]},
        audit(goal),
    ])

    assert await governance.task_intent(model, instruction, True) == {}
    # Even an affirmative review cannot resolve which identical source quote
    # was intended. Reject before review instead of binding the first match.
    assert len(model.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("review", [
    audit(RESTATEMENTS[0][0]),
    audit(RESTATEMENTS[0][1]),
    audit("先生成一版。在当前文档基础上更新一版文档。"),
    audit(""),
    audit(None),
    audit("PLACEHOLDER", action="create"),
    audit("PLACEHOLDER", action="patch"),
    audit("PLACEHOLDER", action="none"),
    audit("PLACEHOLDER", same_outcome=False),
    audit("PLACEHOLDER", same_outcome="true"),
    audit("PLACEHOLDER", same_outcome=1),
    audit("PLACEHOLDER", same_outcome=None),
    {"same_outcome": True, "action": "edit"},
    {**audit("PLACEHOLDER"), "unrequested_field": "value"},
    "this is not JSON",
    RuntimeError("review transport failed"),
])
async def test_restatement_review_requires_complete_original_evidence_and_strict_agreement(review):
    summary, target, constraint = RESTATEMENTS[0]
    goal = summary + target
    if isinstance(review, dict) and review.get("evidence") == "PLACEHOLDER":
        review = {**review, "evidence": goal}
    model = Model([
        {"requests": [piece(summary, "create"), piece(target, "edit")]},
        {"requests": [piece(goal, "edit")]},
        review,
    ])
    assert await governance.task_intent(model, goal + constraint, True) == {}
    assert len(model.calls) == 3


@pytest.mark.asyncio
async def test_restatement_review_timeout_fails_closed_without_another_extraction(monkeypatch):
    summary, target, constraint = RESTATEMENTS[0]
    goal = summary + target
    model = Model([
        {"requests": [piece(summary, "create"), piece(target, "edit")]},
        {"requests": [piece(goal, "edit")]},
        audit(goal),
    ], audit_delay=0.2)
    monkeypatch.setattr(governance, "INTENT_RESTATEMENT_TIMEOUT_SECONDS", 0.01)
    assert await governance.task_intent(model, goal + constraint, True) == {}
    assert len(model.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second,collapsed_action", [
    (("写一份说明", "create"), ("恢复上次操作", "resume"), "create"),
    (("更新当前方案", "edit"), ("撤销上一次修改", "undo"), "edit"),
    (("恢复上次操作", "resume"), ("撤销上一次修改", "undo"), "resume"),
])
async def test_control_conflicts_cannot_be_removed_by_a_merged_retry(first, second, collapsed_action):
    instruction = first[0] + "；" + second[0]
    model = Model([
        {"requests": [piece(*first), piece(*second)]},
        {"requests": [piece(instruction, collapsed_action)]},
        audit(instruction, action=collapsed_action),
    ])
    assert await governance.task_intent(model, instruction, True) == {}
    assert len(model.calls) == 2


def test_prompt_worked_examples_are_executable_source_evidence_contracts():
    # Execute the examples through the same source-span validator and reducer
    # used in production. This catches invalid/copy-edited examples as well as
    # a restatement example that still instructs the model to return a conflict.
    examples = re.findall(
        r"原文“([^\n]+?)”。输出 requests=(\[[^\n]+?\])。", governance.INTENT_REQUEST_PROMPT,
    )
    assert examples
    restated_edit_examples = []
    independent_outcome_examples = []
    for instruction, records in examples:
        result = {"requests": json.loads(records)}
        assert governance.intent_contract_problem(result, governance.intent_request_schema(), instruction) == ""
        tasks = [item for item in result["requests"] if item["role"] == "task"]
        if {item["action"] for item in tasks} == {"create", "edit"}:
            with pytest.raises(ValueError):
                governance.aggregate_intent_requests(result, instruction)
            independent_outcome_examples.append(result)
            continue
        intent = governance.aggregate_intent_requests(result, instruction)
        if len(tasks) == 1 and tasks[0]["action"] == "edit" and "。" in tasks[0]["request"]:
            restated_edit_examples.append(intent)
            assert intent["primary_goal"] == tasks[0]["request"]
            assert intent["content_request"] == tasks[0]["request"]
    assert restated_edit_examples, "The prompt needs an executable example with a complete restated edit goal"
    assert independent_outcome_examples, "The prompt must keep genuinely independent outcomes separate"
