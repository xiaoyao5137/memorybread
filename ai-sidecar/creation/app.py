"""创作服务 FastAPI 应用"""

from __future__ import annotations

import asyncio
import concurrent.futures
from functools import wraps
import queue
import threading
import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Any, Literal, Optional
import logging

import httpx

from inference_queue import (
    LANE_P0_CREATION, LANE_P2_CREATION, InferencePreemptedError, Priority, get_global_queue,
    raise_if_preempted, register_current_preempt_callback,
)
from inference_transport import run_preemptible_async
from local_auth import install_fastapi_guard

from .agent_loop import CreationAgentLoop
from .operations import OperationError
from .brainstorm import (
    BrainstormCoordinator, BrainstormGenerationError, BrainstormGenerationTimeout,
)
from .inline_edit import (
    InlineEditValidationError,
    build_inline_edit_prompts,
    generate_local_replacement,
    validate_replacement,
)
from .service import CloudModelRequestError, CreationOptions, CreationService
from .tools import DEFAULT_CREATION_TOOL_IDS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Creation Service")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "tauri://localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
install_fastapi_guard(app)
creation_service = CreationService()
creation_agent_loop = CreationAgentLoop(creation_service)
CREATION_AGENT_STREAM_HEARTBEAT_SECONDS = 10.0
brainstorm_coordinator = BrainstormCoordinator(creation_service)


class _InteractiveCreationCancellation:
    """Bridge request cancellation to the actual task owning the model socket."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled = False
        self._loop = None
        self._task = None

    def cancel(self):
        with self._lock:
            # Repeated cancellation must not interrupt HTTP cleanup after the
            # first cancellation has already unwound the model coroutine.
            if self._cancelled:
                return
            self._cancelled = True
            loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # Completion can clear/close the loop just after we copied it.
                pass

    def run(self, operation, *, preemptible=False):
        async def execute():
            with self._lock:
                self._loop = asyncio.get_running_loop()
                self._task = asyncio.current_task()
                cancelled = self._cancelled
            unregister = (
                register_current_preempt_callback(self.cancel)
                if preemptible else lambda: None
            )
            try:
                if preemptible:
                    raise_if_preempted()
                # Also covers cancellation after admission but before the
                # worker's event loop registered the actual operation task.
                if cancelled:
                    raise asyncio.CancelledError()
                result = await operation()
                if preemptible:
                    raise_if_preempted()
                return result
            except asyncio.CancelledError as exc:
                # Queue workers catch Exception, whereas asyncio cancellation
                # is a BaseException. Settle the Future only after cleanup.
                if preemptible:
                    raise_if_preempted()
                raise concurrent.futures.CancelledError("创作请求已取消") from exc
            finally:
                unregister()
                with self._lock:
                    self._loop = None
                    self._task = None

        return asyncio.run(execute())


def _interactive_creation_endpoint(handler):
    """Assign online priority at the HTTP boundary; shared services stay neutral."""
    @wraps(handler)
    async def scheduled(*args, **kwargs):
        cancellation = _InteractiveCreationCancellation()
        future = get_global_queue().submit(
            Priority.P0,
            lambda: cancellation.run(lambda: handler(*args, **kwargs)),
            lane=LANE_P0_CREATION,
        )
        try:
            return await asyncio.wrap_future(future)
        finally:
            cancellation.cancel()
            # A disconnected request must not start later if it is still queued.
            future.cancel()
    return scheduled


async def _interactive_creation_chunks(stream_factory):
    """Keep the model iterator in the P0 worker for its entire lifetime."""
    chunks: queue.Queue = queue.Queue()
    finished = object()
    cancelled = threading.Event()
    cancellation = _InteractiveCreationCancellation()

    def run_stream():
        async def produce():
            stream = stream_factory()
            try:
                async for chunk in stream:
                    if cancelled.is_set():
                        break
                    chunks.put(chunk)
            finally:
                await stream.aclose()
        return cancellation.run(produce)

    future = get_global_queue().submit(
        Priority.P0, run_stream, lane=LANE_P0_CREATION,
    )
    # Eviction/cancellation before admission must also wake the HTTP consumer.
    future.add_done_callback(lambda _future: chunks.put(finished))

    def cancel_model(_request_task=None):
        cancelled.set()
        cancellation.cancel()
        future.cancel()

    # The response task can be cancelled while sending a chunk, when this
    # iterator is suspended at yield and its finally has not been entered.
    request_task = asyncio.current_task()
    if request_task is not None:
        request_task.add_done_callback(cancel_model)
    try:
        while True:
            item = await asyncio.to_thread(chunks.get)
            if item is finished:
                break
            yield item
        await asyncio.wrap_future(future)
    finally:
        if request_task is not None:
            request_task.remove_done_callback(cancel_model)
        cancel_model()


def _creation_failure_details(exc: Exception) -> tuple[str, str, bool]:
    """把内部异常收敛为不泄露供应商信息的稳定错误契约。"""
    if isinstance(exc, InferencePreemptedError):
        return "INFERENCE_PREEMPTED", "后台创作已让出模型资源，稍后自动重试", True
    if isinstance(exc, OperationError):
        return exc.code, str(exc), bool(getattr(exc, "retryable", False))
    if isinstance(exc, httpx.TransportError):
        return (
            "MODEL_TRANSPORT_UNAVAILABLE",
            "模型服务连接中断，已重试仍未恢复，可稍后重试",
            True,
        )
    if isinstance(exc, CloudModelRequestError):
        if exc.status_code in {401, 403}:
            return (
                "MODEL_ACCESS_DENIED",
                "当前模型访问未获授权，请切换可用模型或检查模型权限后重试",
                False,
            )
        if exc.status_code == 429:
            return (
                "MODEL_RATE_LIMITED",
                "模型服务当前繁忙，自动重试后仍未恢复，请稍后重试",
                True,
            )
        if exc.status_code >= 500:
            return (
                "MODEL_SERVICE_UNAVAILABLE",
                "模型服务暂时不可用，自动重试后仍未恢复，请稍后重试",
                True,
            )
        return (
            "MODEL_REQUEST_FAILED",
            "模型服务未能接受请求，请检查模型配置后重试",
            False,
        )
    message = str(exc).strip()
    return (
        "CREATION_AGENT_FAILED",
        message or "创作执行失败，服务未返回具体原因",
        False,
    )


class GenerateRequest(BaseModel):
    user_prompt: str
    design_templates: list[dict]
    timeline_context: Optional[str] = None
    capture_context: Optional[str] = None
    doc_type: str = ""
    audience: str = ""
    output_format: str = "markdown"
    inherit_format: bool = True
    enable_rag: bool = True
    enable_web_search: bool = False
    enable_image_generation: bool = False
    browser_extension_enabled: bool = True
    enabled_tools: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CREATION_TOOL_IDS)
    )
    content_weight: float = 0.45
    quality_weight: float = 0.15
    completeness_weight: float = 0.15
    usage_weight: float = 0.10
    format_weight: float = 0.10
    freshness_weight: float = 0.05
    max_references: int = Field(default=10, ge=1, le=30)
    data_search_limit: int = Field(default=30, ge=1, le=50)
    creation_model: Optional[str] = None
    creation_api_key: Optional[str] = None
    creation_base_url: Optional[str] = None


class ReferenceRequest(BaseModel):
    user_prompt: str
    doc_type: str = ""
    audience: str = ""
    inherit_format: bool = True
    enable_rag: bool = True
    content_weight: float = 0.45
    quality_weight: float = 0.15
    completeness_weight: float = 0.15
    usage_weight: float = 0.10
    format_weight: float = 0.10
    freshness_weight: float = 0.05
    max_references: int = 6


class AnalyzeCreationSkillRequest(BaseModel):
    document_title: str
    document_content: str
    doc_type: str = ""


class MatchCreationSkillsRequest(BaseModel):
    prompt: str
    skills: list[dict[str, Any]] = Field(default_factory=list)


class AgentRunRequest(GenerateRequest):
    """创作 Agent Loop 的启动或恢复请求。"""

    execution_origin: Literal["interactive", "scheduled_task"] = "interactive"
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    root_request: Optional[str] = None
    current_document: str = ""
    conversation: list[dict[str, str]] = Field(default_factory=list)
    selected_skills: list[dict[str, Any]] = Field(default_factory=list)
    available_skills: Optional[list[dict[str, Any]]] = None
    explicit_skill_ids: list[str] = Field(default_factory=list)
    model_mode: str = "local"
    confirmed: bool = False
    resume_state: Optional[dict[str, Any]] = None
    model_result: Optional[str] = None
    creation_mode: str = "direct"
    creation_brief: Optional[dict[str, Any]] = None
    operation_context: Optional[dict[str, Any]] = None
    resume_checkpoint: Optional[dict[str, Any]] = None


class InlineEditConstraints(BaseModel):
    schema_version: str = "creation.inline-edit.constraints.v1"
    allowed_facts: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    skill_invariants: list[str] = Field(default_factory=list)


class InlineEditRequest(BaseModel):
    schema_version: str
    request_id: str
    action: str
    selected_markdown: str
    section_context: str = ""
    custom_prompt: str = ""
    model_mode: str = "local"
    context_constraints: InlineEditConstraints = Field(
        default_factory=InlineEditConstraints
    )
    resume_state: Optional[dict[str, Any]] = None
    model_result: Optional[str] = None


class BrainstormNextRequest(BaseModel):
    root_request: str
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    brief_markdown: str = ""
    selected_skills: list[dict[str, Any]] = Field(default_factory=list)
    force_continue: bool = False
    prefetch: bool = False
    suggest_directions: bool = False
    focus_hint: str = ""
    focus_hint_source: str = "legacy"
    exploration_stage: str = ""
    question_batch_limit: int = Field(default=1, ge=1, le=8)
    extension_goal: str = Field(default="", max_length=80)
    sibling_question_context: list[str] = Field(default_factory=list, max_length=32)
    brief_edits: dict[str, str] = Field(default_factory=dict)
    user_input_revisions: dict[str, int] = Field(default_factory=dict)
    creation_model: Optional[str] = None
    creation_api_key: Optional[str] = None
    creation_base_url: Optional[str] = None


@app.post("/creation/brainstorm/next")
async def next_brainstorm_step(request: BrainstormNextRequest, http_request: Request = None):
    """生成当前题及按需规划的同层待问主题，或判断当前已收敛。"""
    if not request.root_request.strip():
        raise HTTPException(status_code=400, detail="root_request 不能为空")

    # 排队和模型执行共享同一预算；晚获准的任务不能重新获得完整 150 秒。
    deadline = time.monotonic() + brainstorm_coordinator.MAX_TURN_SECONDS
    timeout_message = "本轮脑暴生成等待超时，已保留当前输入和已确认进度，请稍后重试"
    cancellation = _InteractiveCreationCancellation()

    async def run_with_deadline() -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrainstormGenerationTimeout(timeout_message)
        try:
            # Future.cancel() 无法终止已运行的线程；由任务自己的事件循环
            # 取消模型请求，退出流上下文并释放推理槽。
            return await asyncio.wait_for(
                brainstorm_coordinator.next_step(
                    root_request=request.root_request,
                    decisions=request.decisions,
                    brief_markdown=request.brief_markdown,
                    selected_skills=request.selected_skills,
                    force_continue=request.force_continue,
                    prefetch=request.prefetch,
                    suggest_directions=request.suggest_directions,
                    focus_hint=request.focus_hint,
                    focus_hint_source=request.focus_hint_source,
                    exploration_stage=request.exploration_stage,
                    question_batch_limit=request.question_batch_limit,
                    extension_goal=request.extension_goal,
                    sibling_question_context=request.sibling_question_context,
                    brief_edits=request.brief_edits,
                    user_input_revisions=request.user_input_revisions,
                    creation_model=request.creation_model,
                    creation_api_key=request.creation_api_key,
                    creation_base_url=request.creation_base_url,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            raise BrainstormGenerationTimeout(timeout_message) from exc

    def run_brainstorm() -> dict[str, Any]:
        if time.monotonic() >= deadline:
            raise BrainstormGenerationTimeout(timeout_message)
        try:
            return cancellation.run(run_with_deadline, preemptible=request.prefetch)
        except concurrent.futures.CancelledError as exc:
            if time.monotonic() >= deadline:
                raise BrainstormGenerationTimeout(timeout_message) from exc
            raise

    future = get_global_queue().submit(
        Priority.P2 if request.prefetch else Priority.P0,
        run_brainstorm,
        lane=LANE_P2_CREATION if request.prefetch else LANE_P0_CREATION,
    )

    async def await_result():
        wrapped = asyncio.wrap_future(future)
        if http_request is None:
            return await wrapped
        stop_watching = asyncio.Event()

        async def wait_disconnect():
            # A non-streaming ASGI handler is not cancelled automatically when
            # Core drops its HTTP request after a branch becomes obsolete.
            while not stop_watching.is_set():
                if await http_request.is_disconnected():
                    raise asyncio.CancelledError()
                await asyncio.sleep(0.05)

        disconnected = asyncio.create_task(wait_disconnect())
        try:
            done, _ = await asyncio.wait(
                (wrapped, disconnected), return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnected in done:
                await disconnected
            return await wrapped
        finally:
            stop_watching.set()
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            wrapped.cancel()

    try:
        try:
            result = await asyncio.wait_for(
                await_result(),
                timeout=max(0.0, deadline - time.monotonic()),
            )
            # This gate is computed from validated option labels, never from a
            # model-authored promise that a future answer cannot alter sources.
            result["prefetch_safe_option_ids"] = [
                option["id"] for option in (result.get("question") or {}).get("options", [])
                if isinstance(option.get("id"), str) and option["id"]
                and brainstorm_coordinator._source_directive(str(option.get("label") or "")) == 0
            ]
            return result
        except asyncio.TimeoutError as exc:
            # 尚未运行的任务必须撤销，不能在请求已返回后又启动生成。
            # 已运行的任务由 run_with_deadline 使用同一时限取消。
            future.cancel()
            raise BrainstormGenerationTimeout(timeout_message) from exc
    except InferencePreemptedError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "INFERENCE_PREEMPTED",
            "message": "后台脑暴预生成已让出模型资源，稍后自动重试",
            "retryable": True,
        }) from exc
    except BrainstormGenerationError as exc:
        raise HTTPException(
            status_code=504 if exc.code == "BRAINSTORM_MODEL_TIMEOUT" else 502,
            detail={
                "code": exc.code,
                "message": str(exc),
                "retryable": exc.code == "BRAINSTORM_MODEL_TIMEOUT",
            },
        ) from exc
    except Exception as exc:
        # These failures can contain provider URLs, credentials and response
        # bodies. Keep the exhausted-retry boundary specific to brainstorm;
        # neither exception text nor a traceback belongs in its public contract.
        error_code = "BRAINSTORM_MODEL_OUTPUT_INVALID"
        upstream_status = None
        if isinstance(exc, httpx.HTTPStatusError):
            upstream_status = exc.response.status_code
        elif isinstance(exc, CloudModelRequestError):
            upstream_status = exc.status_code
        if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
            error_code = "BRAINSTORM_MODEL_TIMEOUT"
        elif upstream_status is not None:
            if upstream_status in {408, 504}:
                error_code = "BRAINSTORM_MODEL_TIMEOUT"
            elif upstream_status == 429:
                error_code = "MODEL_RATE_LIMITED"
            elif upstream_status in {401, 403}:
                error_code = "MODEL_ACCESS_DENIED"
            elif upstream_status >= 500:
                error_code = "MODEL_SERVICE_UNAVAILABLE"
            else:
                error_code = "MODEL_REQUEST_FAILED"
        elif isinstance(exc, httpx.TransportError):
            error_code = "MODEL_SERVICE_UNAVAILABLE"
        elif isinstance(exc, OperationError):
            error_code = {
                "MODEL_TIMEOUT": "BRAINSTORM_MODEL_TIMEOUT",
                "BRAINSTORM_MODEL_TIMEOUT": "BRAINSTORM_MODEL_TIMEOUT",
                "MODEL_RATE_LIMITED": "MODEL_RATE_LIMITED",
                "MODEL_UNAVAILABLE": "MODEL_SERVICE_UNAVAILABLE",
                "MODEL_SERVICE_UNAVAILABLE": "MODEL_SERVICE_UNAVAILABLE",
                "MODEL_TRANSPORT_UNAVAILABLE": "MODEL_SERVICE_UNAVAILABLE",
                "MODEL_ACCESS_DENIED": "MODEL_ACCESS_DENIED",
                "MODEL_REQUEST_FAILED": "MODEL_REQUEST_FAILED",
            }.get(exc.code, error_code)
        status_code, message, retryable = {
            "BRAINSTORM_MODEL_TIMEOUT": (
                504, "脑暴问题生成超时，已保留当前输入，请稍后重试", True,
            ),
            "MODEL_RATE_LIMITED": (
                429, "模型服务当前繁忙，已保留当前输入，请稍后重试", True,
            ),
            "MODEL_SERVICE_UNAVAILABLE": (
                503, "脑暴问题生成服务暂时不可用，已保留当前输入，请稍后重试", True,
            ),
            "MODEL_ACCESS_DENIED": (
                403, "当前模型访问未获授权，请切换可用模型或检查模型权限后重试", False,
            ),
            "MODEL_REQUEST_FAILED": (
                502, "模型未能接受脑暴请求，请检查模型配置后重试", False,
            ),
            "BRAINSTORM_MODEL_OUTPUT_INVALID": (
                502, "脑暴问题未生成有效结果，已保留当前输入，请重试", False,
            ),
        }[error_code]
        logger.warning(
            "Dynamic brainstorm generation failed: type=%s code=%s",
            type(exc).__name__, error_code,
        )
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": error_code,
                "message": message,
                "retryable": retryable,
            },
        ) from exc
    finally:
        cancellation.cancel()
        future.cancel()


@app.post("/creation/generate")
async def generate_document(request: GenerateRequest):
    """流式生成文档"""
    try:
        options = _options_from_request(request)

        async def event_stream():
            import json
            yield f"data: {json.dumps({'status': 'started'})}\n\n"
            chunk_queue: queue.Queue = queue.Queue()
            finished = object()
            cancelled = threading.Event()

            def run_creation() -> None:
                async def produce() -> None:
                    if cancelled.is_set():
                        return
                    async for chunk in creation_service.generate_document(
                        user_prompt=request.user_prompt,
                        design_templates=request.design_templates,
                        timeline_context=request.timeline_context,
                        capture_context=request.capture_context,
                        options=options,
                        creation_model=request.creation_model,
                        creation_api_key=request.creation_api_key,
                        creation_base_url=request.creation_base_url,
                    ):
                        if cancelled.is_set():
                            break
                        chunk_queue.put(chunk)

                try:
                    asyncio.run(produce())
                finally:
                    chunk_queue.put(finished)

            future = get_global_queue().submit(
                Priority.P0,
                run_creation,
                lane=LANE_P0_CREATION,
            )
            try:
                while True:
                    item = await asyncio.to_thread(chunk_queue.get)
                    if item is finished:
                        break
                    yield f"data: {json.dumps({'content': item})}\n\n"
                await asyncio.to_thread(future.result)
                yield f"data: {json.dumps({'done': True})}\n\n"
            except Exception as e:
                logger.error(f"Streaming generation error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            finally:
                cancelled.set()
                future.cancel()

        return StreamingResponse(event_stream(), media_type="text/event-stream")
    except Exception as e:
        logger.error(f"Generation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/creation/agent/run")
async def run_creation_agent(request: AgentRunRequest):
    """执行可观察、可暂停和可恢复的目标驱动创作循环。"""
    if request.model_mode not in {"local", "external"}:
        raise HTTPException(status_code=400, detail="model_mode 只支持 local 或 external")
    if request.creation_mode not in {"direct", "brainstorm"}:
        raise HTTPException(
            status_code=400,
            detail="creation_mode 只支持 direct 或 brainstorm",
        )
    options = _options_from_request(request)
    background = request.execution_origin == "scheduled_task"
    inference_priority = Priority.P2 if background else Priority.P0
    inference_lane = LANE_P2_CREATION if background else LANE_P0_CREATION
    resume_state = request.resume_state or {}
    resolved_session_id = (
        request.session_id
        or resume_state.get("session_id")
        or f"session-{uuid4()}"
    )
    resolved_run_id = request.run_id or resume_state.get("run_id") or f"run-{uuid4()}"

    async def event_stream():
        import json

        event_queue: queue.Queue = queue.Queue()
        finished = object()
        cancelled = threading.Event()
        runtime_identity = {
            "session_id": resolved_session_id,
            "run_id": resolved_run_id,
            "sequence": 0,
        }

        def run_loop() -> None:
            async def produce() -> None:
                async for event in creation_agent_loop.run(
                    user_message=request.user_prompt,
                    root_request=request.root_request,
                    current_document=request.current_document,
                    conversation=request.conversation,
                    selected_skills=request.selected_skills,
                    governance_required=True,
                    available_skills=request.available_skills,
                    explicit_skill_ids=request.explicit_skill_ids,
                    options=options,
                    model_mode=request.model_mode,
                    session_id=resolved_session_id,
                    run_id=resolved_run_id,
                    confirmed=request.confirmed,
                    resume_state=request.resume_state,
                    model_result=request.model_result,
                    creation_model=request.creation_model,
                    creation_api_key=request.creation_api_key,
                    creation_base_url=request.creation_base_url,
                    creation_mode=request.creation_mode,
                    creation_brief=request.creation_brief,
                    operation_context=request.operation_context,
                    resume_checkpoint=request.resume_checkpoint,
                ):
                    if cancelled.is_set():
                        break
                    runtime_identity["session_id"] = event.get(
                        "session_id", runtime_identity["session_id"]
                    )
                    runtime_identity["run_id"] = event.get(
                        "run_id", runtime_identity["run_id"]
                    )
                    runtime_identity["sequence"] = event.get(
                        "sequence", runtime_identity["sequence"]
                    )
                    event_queue.put(event)

            run_preemptible_async(produce)

        if not request.resume_state:
            queued_event = {
                "schema_version": "creation.agent.v1",
                "event_id": f"event-{uuid4()}",
                "session_id": resolved_session_id,
                "run_id": resolved_run_id,
                "sequence": 0,
                "timestamp": int(time.time() * 1000),
                "type": "run.queued",
                "status": "waiting",
                "actor": {
                    "kind": "agent",
                    "id": "creation_main_agent",
                    "name": "创作主 Agent",
                },
                "summary": "已接收本轮指令，正在准备执行",
                "goal": {
                    "status": "active",
                    "revision": 0,
                    "remaining_steps": [],
                    "outcome": "",
                },
                "environment_patch": {},
                "data": {"lane": "background_creation" if background else "interactive_creation"},
            }
            yield f"data: {json.dumps(queued_event, ensure_ascii=False)}\n\n"

        future = get_global_queue().submit(
            inference_priority,
            run_loop,
            lane=inference_lane,
        )
        future.add_done_callback(lambda _future: event_queue.put(finished))
        try:
            while True:
                try:
                    item = await asyncio.to_thread(
                        event_queue.get,
                        True,
                        CREATION_AGENT_STREAM_HEARTBEAT_SECONDS,
                    )
                except queue.Empty:
                    # A memory search can spend several minutes in local model
                    # scoring without producing a domain event.  Keep the
                    # sidecar -> Core hop active as well as Core -> WebView;
                    # otherwise a transport can disappear while the durable
                    # checkpoint still says that the tool is in flight.
                    yield ": keep-alive\n\n"
                    continue
                if item is finished:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            await asyncio.to_thread(future.result)
        except Exception as exc:
            logger.error("Creation agent loop failed: %s", type(exc).__name__)
            error_code, failure_summary, retryable = _creation_failure_details(exc)
            failure_event = {
                "schema_version": "creation.agent.v1",
                "event_id": f"event-{uuid4()}",
                "session_id": runtime_identity["session_id"],
                "run_id": runtime_identity["run_id"],
                "sequence": runtime_identity["sequence"] + 1,
                "timestamp": int(time.time() * 1000),
                "type": "run.failed",
                "status": "failed",
                "actor": {
                    "kind": "agent",
                    "id": "creation_main_agent",
                    "name": "创作主 Agent",
                },
                "summary": failure_summary,
                "goal": {
                    "status": "failed",
                    "revision": 0,
                    "remaining_steps": [],
                    "outcome": failure_summary,
                },
                "environment_patch": {},
                "data": {
                    "error_code": error_code,
                    "retryable": retryable,
                },
            }
            yield f"data: {json.dumps(failure_event, ensure_ascii=False)}\n\n"
        finally:
            cancelled.set()
            future.cancel()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/creation/inline-edit/run")
async def run_creation_inline_edit(request: InlineEditRequest):
    """只生成选区 replacement；完整文档拼接和持久化由 Core 负责。"""
    if request.schema_version != "creation.inline-edit.v1":
        raise HTTPException(status_code=400, detail="不支持的选区编辑协议版本")
    if request.action not in {"brainstorm", "polish", "expand", "elaborate"}:
        raise HTTPException(status_code=400, detail="不支持的选区编辑动作")
    if request.model_mode not in {"local", "external"}:
        raise HTTPException(status_code=400, detail="model_mode 只支持 local 或 external")
    if request.action not in {"brainstorm", "polish"} and request.custom_prompt.strip():
        raise HTTPException(status_code=400, detail="自定义要求只支持脑暴写回或润色动作")

    constraints = request.context_constraints.model_dump()
    system_prompt, user_prompt = build_inline_edit_prompts(
        action=request.action,
        selected_markdown=request.selected_markdown,
        section_context=request.section_context,
        custom_prompt=request.custom_prompt,
        context_constraints=constraints,
    )

    if request.model_mode == "external" and request.model_result is None:
        return {
            "schema_version": "creation.inline-edit.v1",
            "request_id": request.request_id,
            "status": "paused",
            "model_request": {
                "request_id": f"inline-model-{request.request_id}",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            "resume_state": {
                "schema_version": "creation.inline-edit.v1",
                "request_id": request.request_id,
                "action": request.action,
                "selected_markdown": request.selected_markdown,
            },
        }

    if request.model_mode == "external":
        resume_state = request.resume_state or {}
        if (
            resume_state.get("schema_version") != "creation.inline-edit.v1"
            or resume_state.get("request_id") != request.request_id
            or resume_state.get("action") != request.action
            or resume_state.get("selected_markdown") != request.selected_markdown
        ):
            raise HTTPException(status_code=409, detail="选区编辑恢复状态不匹配")
        try:
            allowed_facts = list(constraints.get("allowed_facts") or [])
            if request.action == "brainstorm" and request.custom_prompt.strip():
                allowed_facts.append(request.custom_prompt.strip())
            replacement = validate_replacement(
                request.action,
                request.selected_markdown,
                request.model_result or "",
                allowed_facts,
            )
        except InlineEditValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        def run_local() -> str:
            return asyncio.run(
                generate_local_replacement(
                    creation_service,
                    action=request.action,
                    selected_markdown=request.selected_markdown,
                    section_context=request.section_context,
                    custom_prompt=request.custom_prompt,
                    context_constraints=constraints,
                )
            )

        future = get_global_queue().submit(
            Priority.P0,
            run_local,
            lane=LANE_P0_CREATION,
        )
        try:
            replacement = await asyncio.to_thread(future.result)
        except InlineEditValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Creation inline edit failed")
            error_code, message, retryable = _creation_failure_details(exc)
            raise HTTPException(
                status_code=503 if retryable else 500,
                detail={
                    "code": error_code,
                    "message": message,
                    "retryable": retryable,
                },
            ) from exc
        finally:
            future.cancel()

    return {
        "schema_version": "creation.inline-edit.v1",
        "request_id": request.request_id,
        "status": "candidate",
        "replacement_markdown": replacement,
    }


@app.get("/creation/inline-edit/capabilities")
async def creation_inline_edit_capabilities():
    return {
        "schema_version": "creation.inline-edit.v1",
        "enabled": True,
        "actions": ["brainstorm", "polish", "expand", "elaborate"],
        "max_selection_bytes": 12000,
        "max_custom_prompt_bytes": 2000,
        "supported_node_kinds": [
            "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"
        ],
    }


@app.post("/creation/references")
@_interactive_creation_endpoint
async def preview_references(request: ReferenceRequest):
    """预览本次创作会优先使用的参考资料及权重。"""
    try:
        options = _options_from_request(request)
        parsed = creation_service.analyze_requirement(request.user_prompt, options)
        references = (
            creation_service.retrieve_references(request.user_prompt, parsed, options)
            if options.enable_rag
            else []
        )
        return {
            "requirement": parsed,
            "references": [
                {
                    "id": ref.id,
                    "source_id": ref.source_id,
                    "source_type": ref.source_type,
                    "title": ref.title,
                    "doc_type": ref.doc_type,
                    "final_weight": round(ref.final_weight, 4),
                    "relevance_score": round(ref.relevance_score, 4),
                    "quality_score": round(ref.quality_score, 4),
                    "completeness_score": round(ref.completeness_score, 4),
                    "usage_score": round(ref.usage_score, 4),
                    "format_score": round(ref.format_score, 4),
                    "freshness_score": round(ref.freshness_score, 4),
                    "usage_count": ref.usage_count,
                    "retrieval_tier": ref.retrieval_tier,
                    "retrieval_paths": list(ref.retrieval_paths),
                    "matched_keywords": list(ref.matched_keywords),
                    "matched_entities": list(ref.matched_entities),
                    "lexical_score": round(ref.lexical_score, 4),
                    "semantic_score": round(ref.semantic_score, 4),
                    "entity_score": round(ref.entity_score, 4),
                    "retrieval_mode": ref.retrieval_mode,
                    "primary_target": ref.primary_target,
                    "matched_components": list(ref.matched_components),
                    "matched_relations": list(ref.matched_relations),
                    "relation_score": round(ref.relation_score, 4),
                    "selection_reasons": list(ref.selection_reasons),
                    "reason": ref.reason,
                    "summary": ref.summary,
                    "source_snapshot_id": ref.source_snapshot_id,
                    "source_body_hash": ref.source_body_hash,
                    "source_url": ref.source_url,
                    "observed_at": ref.observed_at,
                }
                for ref in references
            ],
        }
    except Exception as e:
        logger.error(f"Reference preview error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/creation/skills/analyze")
@_interactive_creation_endpoint
async def analyze_creation_skill(request: AnalyzeCreationSkillRequest):
    """在本地从既有文档提炼可编辑的技能。"""
    try:
        return await creation_service.analyze_creation_skill(
            document_title=request.document_title,
            document_content=request.document_content,
            doc_type=request.doc_type,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Creation skill analysis error: %s", e)
        raise HTTPException(status_code=500, detail="本地技能分析失败")


@app.post("/creation/skills/review")
@_interactive_creation_endpoint
async def review_creation_skill(request: dict[str, Any]):
    from .skill_governance import review_skill
    return await review_skill(creation_service, request)


@app.post("/creation/skills/match")
@_interactive_creation_endpoint
async def match_creation_skills(request: MatchCreationSkillsRequest):
    """创作提交后由模型路由决定执行时引入哪个 Skill；失败时返回空召回。"""
    try:
        return await creation_service.route_creation_skills(
            prompt=request.prompt,
            skills=request.skills,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Creation skill match error: %s", e)
        raise HTTPException(status_code=500, detail="技能召回路由失败")


@app.get("/health")
async def health():
    return {"status": "ok"}


class TestModelRequest(BaseModel):
    model: str
    api_key: str
    base_url: Optional[str] = None


@app.post("/creation/test_model")
@_interactive_creation_endpoint
async def test_creation_model(request: TestModelRequest):
    """验证创作模型连通性"""
    try:
        chunks = []
        async for chunk in creation_service._generate_cloud(
            "You are a helpful assistant.", "Reply with just 'OK'.",
            request.model, request.api_key, request.base_url or "",
        ):
            chunks.append(chunk)
            if len("".join(chunks)) >= 20:
                break
        return {"status": "ok", "message": "".join(chunks)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class ChatRequest(BaseModel):
    model: str
    api_key: str
    base_url: Optional[str] = None
    messages: list


@app.post("/creation/chat")
async def chat_with_model(request: ChatRequest):
    """与创作模型流式对话"""
    import json as _json
    async def event_stream():
        try:
            async for chunk in _interactive_creation_chunks(
                lambda: creation_service._chat_cloud(
                    request.messages, request.model, request.api_key, request.base_url or ""
                )
            ):
                yield f"data: {_json.dumps({'content': chunk})}\n\n"
            yield f"data: {_json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'error': str(e)})}\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _options_from_request(request) -> CreationOptions:
    return CreationOptions(
        doc_type=getattr(request, "doc_type", "") or "",
        audience=getattr(request, "audience", "") or "",
        output_format=getattr(request, "output_format", "markdown") or "markdown",
        inherit_format=bool(getattr(request, "inherit_format", True)),
        enable_rag=bool(getattr(request, "enable_rag", True)),
        enable_web_search=bool(getattr(request, "enable_web_search", False)),
        enable_image_generation=bool(getattr(request, "enable_image_generation", False)),
        browser_extension_enabled=bool(
            getattr(request, "browser_extension_enabled", True)
        ),
        enabled_tools=tuple(
            DEFAULT_CREATION_TOOL_IDS
            if getattr(request, "enabled_tools", None) is None
            else getattr(request, "enabled_tools")
        ),
        content_weight=float(getattr(request, "content_weight", 0.45)),
        quality_weight=float(getattr(request, "quality_weight", 0.15)),
        completeness_weight=float(getattr(request, "completeness_weight", 0.15)),
        usage_weight=float(getattr(request, "usage_weight", 0.10)),
        format_weight=float(getattr(request, "format_weight", 0.10)),
        freshness_weight=float(getattr(request, "freshness_weight", 0.05)),
        max_references=int(getattr(request, "max_references", 10)),
        data_search_limit=int(getattr(request, "data_search_limit", 30)),
    )
