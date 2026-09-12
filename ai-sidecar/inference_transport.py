"""Cancellable model HTTP transport for synchronous inference queue workers.

The async socket belongs to the worker running the model task. Preemption cancels
that task before connect/headers as well as during reads, and unwinds the HTTP
contexts before returning the shared inference slot. Closing a synchronous
Response from another thread cannot provide this guarantee.
"""

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar
from urllib.parse import urlsplit

import httpx


_Result = TypeVar("_Result")


def run_preemptible_async(operation: Callable[[], Awaitable[_Result]]) -> _Result:
    """Run in a sync inference worker, preserving its queue/task thread identity."""
    from inference_queue import raise_if_preempted, register_current_preempt_callback

    async def execute() -> _Result:
        raise_if_preempted()
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        def cancel() -> None:
            # A watcher can have copied the callback just before unregister().
            # A late notification must not raise against a closed event loop.
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

        unregister = register_current_preempt_callback(cancel)
        try:
            raise_if_preempted()
            result = await operation()
            raise_if_preempted()
            return result
        except asyncio.CancelledError:
            # The HTTP coroutine's async contexts have already closed by now.
            # Keep the queue's recoverable error rather than leaking CancelledError.
            raise_if_preempted()
            raise
        finally:
            unregister()

    return asyncio.run(execute())


def stream_inference_json(
    url: str,
    payload: Dict[str, Any],
    *,
    timeout: float,
    on_chunk: Callable[[Dict[str, Any]], None],
    trust_env: Optional[bool] = None,
) -> None:
    """Consume an NDJSON model stream; callback errors also close the connection."""
    from inference_queue import raise_if_preempted

    if trust_env is None:
        trust_env = urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}

    async def consume() -> None:
        async with httpx.AsyncClient(timeout=timeout, trust_env=trust_env) as client:
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code >= 400:
                    await response.aread()
                response.raise_for_status()
                async for line in response.aiter_lines():
                    raise_if_preempted()
                    if not line.strip():
                        continue
                    on_chunk(json.loads(line))

    run_preemptible_async(consume)


class CancellableOllamaClient:
    """Small synchronous chat facade for queued background diary inference."""

    def __init__(self, host: str, trust_env: bool = False, timeout: float = 900.0):
        self.host = host.rstrip("/")
        self.trust_env = trust_env
        self.timeout = timeout

    def chat(self, **payload: Any) -> Dict[str, Any]:
        from ollama import ResponseError

        payload["stream"] = True
        result: Dict[str, Any] = {}
        content = []
        thinking = []

        def collect(chunk: Dict[str, Any]) -> None:
            # Keep the SDK's HTTP-200 error-event semantics. Do not expose the
            # upstream error body in task records or logs, which may contain data.
            if chunk.get("error"):
                raise ResponseError("本地模型返回错误，生成未完成")
            result.update(chunk)
            message = chunk.get("message") or {}
            content.append(str(message.get("content") or ""))
            thinking.append(str(message.get("thinking") or ""))

        try:
            stream_inference_json(
                self.host + "/api/chat", payload, timeout=self.timeout,
                on_chunk=collect, trust_env=self.trust_env,
            )
        except httpx.HTTPStatusError as exc:
            raise ResponseError("本地模型请求失败", exc.response.status_code) from None
        # The SDK's iterator accepts plain EOF; a task that persists and delivers
        # complete reports must enforce completion in addition to transport EOF.
        if result.get("done") is not True:
            raise ResponseError("本地模型连接提前结束，生成未完成")
        result["message"] = {
            **(result.get("message") or {}),
            "content": "".join(content),
            "thinking": "".join(thinking),
        }
        return result
