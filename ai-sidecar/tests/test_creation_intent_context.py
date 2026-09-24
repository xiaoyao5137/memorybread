"""Routing classifies the current instruction while retaining separate brief constraints."""

import json

import pytest

from creation.agent_loop import CreationAgentLoop, LoopState
from creation.operations import OperationError
from creation.service import CreationOptions, CreationService


INSTRUCTION = "请根据简报写一份活动说明。"
BRIEF = {
    "revision": 2,
    "root_request": INSTRUCTION,
    "decisions": [{"question_id": "format", "dimension": "呈现方式",
                   "summary": "使用简明列表", "source": "user"}],
    "brief_markdown": "# 创作简报\n旧问题提到删除正文；这些是背景，不是本轮动作。",
}
ROUTE = {"kind": "agent", "id": "route", "name": "解释本轮操作", "action": "route"}
RESPONSE = {"tools": [], "agents": [], "reasoning": "确认当前要求",
            "operation": {"kind": "respond", "response": "已理解"}}


def make_service():
    service = CreationService.__new__(CreationService)
    service.model = "test-model"
    service.calls = []
    service._log_creation_usage = lambda **kwargs: None

    async def stream(**kwargs):
        service.calls.append(kwargs)
        yield json.dumps(RESPONSE, ensure_ascii=False)

    service._stream_direct_completion = stream
    return service


def make_state(loop, model_mode="local", document=""):
    state = loop._new_state(
        user_message=INSTRUCTION, root_request=INSTRUCTION, current_document=document,
        conversation=[], selected_skills=[], options=CreationOptions(),
        model_mode=model_mode, session_id="intent-context", run_id="intent-context-run",
        creation_mode="brainstorm", creation_brief=BRIEF,
    )
    state.environment["requirement"]["operation_context"] = {"current_document": document}
    return state


def install_intent(monkeypatch, action="create"):
    calls = []

    async def intent(service, instruction, has_document):
        calls.append((instruction, has_document))
        return {"action": action, "primary_goal": instruction,
                "deliverable": instruction, "content_request": instruction}

    monkeypatch.setattr("creation.skill_governance.task_intent", intent)
    return calls


def capture_decision(loop):
    applied = []

    async def apply(state, step, decision):
        applied.append(decision)
        if False:
            yield {}

    loop._apply_routing_decision = apply
    return applied


async def execute_route(loop, state):
    return [event async for event in loop._execute_step(
        state, ROUTE, creation_model=None, creation_api_key=None, creation_base_url=None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_mode", ["local", "external"])
@pytest.mark.parametrize("document", ["", "# 已有正文\n保留此段。"])
@pytest.mark.parametrize("restored", [False, True])
async def test_route_keeps_current_instruction_separate_from_brief(
        monkeypatch, model_mode, document, restored):
    service = make_service()
    loop = CreationAgentLoop(service)
    calls = install_intent(monkeypatch)
    capture_decision(loop)
    state = make_state(loop, model_mode, document)
    if restored:
        # Old checkpoints contain a classifier result for the combined context.
        state.environment["requirement"]["task_intent"] = {"action": "edit"}
        state.environment["creation_brief_context"] = "过时的简报缓存"
        state = LoopState.restore(state.serializable())
    full_context = state.environment["context_query"]
    events = await execute_route(loop, state)

    assert calls == [(INSTRUCTION, bool(document))]
    assert state.environment["requirement"]["task_intent"]["action"] == "create"
    if model_mode == "local":
        prompt = service.calls[0]["user_prompt"]
    else:
        request = next(event for event in events if event["type"] == "model.request")
        prompt = request["data"]["messages"][1]["content"]
        assert not service.calls
    assert "脑暴创作约束背景（不是本轮指令" in prompt
    assert "使用简明列表" in prompt
    assert "过时的简报缓存" not in prompt
    marker = "本轮用户原始指令（执行目标与位置以此为准）："
    assert prompt.rsplit(marker, 1)[1] == INSTRUCTION
    assert state.environment["context_query"] == full_context
    assert "# 创作简报" in full_context
    assert state.environment["retrieval_query"] == INSTRUCTION


@pytest.mark.asyncio
async def test_old_external_continuation_rechecks_intent_before_applying_result(monkeypatch):
    service = make_service()
    loop = CreationAgentLoop(service)
    state = make_state(loop, "external", "# 正文\n不可修改。")
    state.environment["requirement"]["task_intent"] = {"action": "respond"}
    state.pending_model_step = {"step": ROUTE}
    state = LoopState.restore(state.serializable())
    calls = install_intent(monkeypatch, "answer")
    original = state.current_document

    with pytest.raises(OperationError, match="操作与本轮主目标不一致"):
        [event async for event in loop._apply_model_result(state, json.dumps(RESPONSE))]
    assert calls == [(INSTRUCTION, True)]
    assert state.current_document == original


@pytest.mark.asyncio
async def test_current_external_continuation_reuses_only_bound_intent(monkeypatch):
    service = make_service()
    loop = CreationAgentLoop(service)
    state = make_state(loop, "external")
    calls = install_intent(monkeypatch, "respond")
    applied = capture_decision(loop)
    await execute_route(loop, state)
    restored = LoopState.restore(state.serializable())
    [event async for event in loop._apply_model_result(restored, json.dumps(RESPONSE))]
    assert calls == [(INSTRUCTION, False)]
    assert applied[0]["operation"]["kind"] == "respond"


@pytest.mark.asyncio
async def test_old_policy_binding_is_rechecked_even_when_instruction_and_document_match(monkeypatch):
    service = make_service()
    loop = CreationAgentLoop(service)
    state = make_state(loop, "external")
    calls = install_intent(monkeypatch, "respond")
    await execute_route(loop, state)
    state.environment["requirement"]["task_intent_context"]["schema_version"] = "creation.current-turn-intent.v1"
    restored = LoopState.restore(state.serializable())

    await execute_route(loop, restored)

    assert calls == [(INSTRUCTION, False), (INSTRUCTION, False)]
    assert restored.environment["requirement"]["task_intent_context"]["schema_version"] == "creation.current-turn-intent.v3"


@pytest.mark.asyncio
async def test_bound_intent_is_rechecked_when_current_instruction_changes(monkeypatch):
    service = make_service()
    loop = CreationAgentLoop(service)
    state = make_state(loop, "external")
    calls = install_intent(monkeypatch, "respond")
    await execute_route(loop, state)
    original_binding = dict(state.environment["requirement"]["task_intent_context"])

    state.user_message = "只告诉我活动说明是否已完成。"
    await execute_route(loop, state)

    assert calls == [(INSTRUCTION, False), (state.user_message, False)]
    requirement = state.environment["requirement"]
    assert requirement["task_intent"]["primary_goal"] == state.user_message
    assert requirement["task_intent_context"] != original_binding


@pytest.mark.asyncio
async def test_bound_intent_is_rechecked_when_document_becomes_present(monkeypatch):
    service = make_service()
    loop = CreationAgentLoop(service)
    state = make_state(loop, "external")
    calls = install_intent(monkeypatch, "respond")
    await execute_route(loop, state)

    state.current_document = "# 活动说明\n已有正文。"
    state.environment["requirement"]["operation_context"]["current_document"] = state.current_document
    await execute_route(loop, state)

    assert calls == [(INSTRUCTION, False), (INSTRUCTION, True)]
    assert state.environment["requirement"]["task_intent_context"]["has_document"] is True


@pytest.mark.asyncio
async def test_brief_change_refreshes_routing_background_without_reclassifying(monkeypatch):
    service = make_service()
    loop = CreationAgentLoop(service)
    state = make_state(loop, "external")
    calls = install_intent(monkeypatch, "respond")
    await execute_route(loop, state)
    original_binding = dict(state.environment["requirement"]["task_intent_context"])

    state.environment["creation_brief"] = {
        **BRIEF, "revision": 3, "brief_edits": {"format": "使用整段叙述"}}
    events = await execute_route(loop, state)

    assert calls == [(INSTRUCTION, False)]
    assert state.environment["requirement"]["task_intent_context"] == original_binding
    request = next(event for event in events if event["type"] == "model.request")
    prompt = request["data"]["messages"][1]["content"]
    assert "使用整段叙述" in prompt
    assert "使用简明列表" not in prompt
    assert prompt.endswith(INSTRUCTION)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_mode", ["local", "external"])
async def test_unverified_current_intent_still_stops_before_routing(monkeypatch, model_mode):
    service = make_service()
    loop = CreationAgentLoop(service)
    state = make_state(loop, model_mode)
    state.environment["requirement"]["task_intent"] = {"action": "create"}

    async def unavailable(*args):
        return {}

    monkeypatch.setattr("creation.skill_governance.task_intent", unavailable)
    with pytest.raises(OperationError, match="未能核验本轮动作"):
        await execute_route(loop, state)
    assert not service.calls
