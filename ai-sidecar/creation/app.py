"""创作服务 FastAPI 应用"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Any, Optional
import logging

import httpx

from inference_queue import LANE_P0_CREATION, Priority, get_global_queue

from .agent_loop import CreationAgentLoop
from .service import CloudModelRequestError, CreationOptions, CreationService
from .tools import REQUIRED_CREATION_TOOL_IDS

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
creation_service = CreationService()
creation_agent_loop = CreationAgentLoop(creation_service)


def _creation_failure_details(exc: Exception) -> tuple[str, str, bool]:
    """把内部异常收敛为不泄露供应商信息的稳定错误契约。"""
    if isinstance(exc, httpx.TransportError):
        return (
            "MODEL_TRANSPORT_UNAVAILABLE",
            "模型服务连接暂时中断，自动重试后仍未恢复，请稍后重试",
            True,
        )
    if isinstance(exc, CloudModelRequestError):
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
    enabled_tools: list[str] = Field(
        default_factory=lambda: list(REQUIRED_CREATION_TOOL_IDS)
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


class AgentRunRequest(GenerateRequest):
    """创作 Agent Loop 的启动或恢复请求。"""

    session_id: Optional[str] = None
    run_id: Optional[str] = None
    root_request: Optional[str] = None
    current_document: str = ""
    conversation: list[dict[str, str]] = Field(default_factory=list)
    selected_skills: list[dict[str, Any]] = Field(default_factory=list)
    model_mode: str = "local"
    confirmed: bool = False
    resume_state: Optional[dict[str, Any]] = None
    model_result: Optional[str] = None


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
    options = _options_from_request(request)
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

            try:
                asyncio.run(produce())
            except Exception as exc:
                logger.exception("Creation agent loop failed")
                error_code, failure_summary, retryable = _creation_failure_details(exc)
                event_queue.put(
                    {
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
                )
            finally:
                event_queue.put(finished)

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
                "data": {"lane": "interactive_creation"},
            }
            yield f"data: {json.dumps(queued_event, ensure_ascii=False)}\n\n"

        future = get_global_queue().submit(
            Priority.P0,
            run_loop,
            lane=LANE_P0_CREATION,
        )
        try:
            while True:
                item = await asyncio.to_thread(event_queue.get)
                if item is finished:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            await asyncio.to_thread(future.result)
        finally:
            cancelled.set()
            future.cancel()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/creation/references")
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
                    "reason": ref.reason,
                    "summary": ref.summary,
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


@app.get("/health")
async def health():
    return {"status": "ok"}


class TestModelRequest(BaseModel):
    model: str
    api_key: str
    base_url: Optional[str] = None


@app.post("/creation/test_model")
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
            async for chunk in creation_service._chat_cloud(
                request.messages, request.model, request.api_key, request.base_url or ""
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
        enabled_tools=tuple(
            getattr(request, "enabled_tools", REQUIRED_CREATION_TOOL_IDS)
            or REQUIRED_CREATION_TOOL_IDS
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
