"""Work settings survive request boundaries; preview is queued and read-only."""
import asyncio
import concurrent.futures
import importlib
import threading

import pytest
from inference_queue import LANE_P0_CREATION, Priority
from creation.operations import OperationError


creation_app = importlib.import_module("creation.app")
integration = importlib.import_module("creation.work_integration")


def test_request_options_preserve_legacy_defaults_and_restore_history_context():
    legacy = creation_app.GenerateRequest(user_prompt="写一份说明", design_templates=[])
    assert creation_app._options_from_request(legacy).work_context is None
    context = {"purpose": "方案评审", "decision_goal": "是否批准试点", "review_enabled": True,
               "style_samples": ["建议先验证入口，再扩展后续流程。"]}
    request = creation_app.AgentRunRequest(user_prompt="继续", design_templates=[],
        creation_brief={"decisions": [{"summary": "保留流程"}], "work_context": context})
    normalized = creation_app._options_from_request(request).work_context
    assert normalized["decision_goal"] == context["decision_goal"]
    assert normalized["style_samples"] == context["style_samples"]
    assert request.creation_brief["decisions"] == [{"summary": "保留流程"}]
    request.work_context = {"purpose": "不同用途", "review_enabled": False}
    assert creation_app._options_from_request(request).work_context["purpose"] == "不同用途"


@pytest.mark.parametrize("invalid", [
    {"style_samples": ["a", "b", "c", "d"]}, {"constraints": "字" * 1501},
    {"review_enabled": "true"}, {"schema_version": "unsupported"},
])
def test_options_reject_invalid_contract_without_truncation(invalid):
    request = creation_app.GenerateRequest(user_prompt="继续", design_templates=[], work_context=invalid)
    with pytest.raises(OperationError) as error:
        creation_app._options_from_request(request)
    assert error.value.code == "CREATION_WORK_CONTEXT_INVALID"


@pytest.mark.asyncio
async def test_preview_uses_interactive_queue_and_returns_independent_result(monkeypatch):
    admissions, seen = [], []

    class ThreadQueue:
        def submit(self, priority, operation, lane=None):
            admissions.append((priority, lane))
            future = concurrent.futures.Future()
            def run():
                try:
                    result = operation()
                    if not future.cancelled():
                        future.set_result(result)
                except Exception as error:
                    if not future.cancelled():
                        future.set_exception(error)
            threading.Thread(target=run, daemon=True).start()
            return future

    async def preview(service, action, instruction, document, context):
        seen.append((action, instruction, document, context))
        return {"status": "candidate", "content": "独立候选", "read_only": True}

    monkeypatch.setattr(creation_app, "get_global_queue", lambda: ThreadQueue())
    monkeypatch.setattr(integration, "preview_work_creation", preview)
    request = creation_app.WorkCreationPreviewRequest(action="trial", instruction="设计试点", current_document="原正文")
    result = await creation_app.preview_work_document(request)
    assert result["content"] == "独立候选"
    assert request.current_document == "原正文"
    assert seen[0][:3] == ("trial", "设计试点", "原正文")
    assert admissions == [(Priority.P0, LANE_P0_CREATION)]


@pytest.mark.asyncio
async def test_cancelled_preview_cancels_pending_queue_work(monkeypatch):
    future = concurrent.futures.Future()
    class PendingQueue:
        def submit(self, _priority, _operation, lane=None):
            return future
    monkeypatch.setattr(creation_app, "get_global_queue", lambda: PendingQueue())
    task = asyncio.create_task(creation_app.preview_work_document(
        creation_app.WorkCreationPreviewRequest(action="review", current_document="原正文")))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert future.cancelled()
