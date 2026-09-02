"""
IPC 传输层（Python 服务端）

AI Sidecar 以此模块作为 Socket 服务端，接收来自 Rust Core Engine 的任务请求，
将请求分派给各 Worker，并将结果以帧格式写回。

用法：
    from memory_bread_ipc import IpcServer

    server = IpcServer(dispatch_fn=my_dispatcher)
    asyncio.run(server.serve())
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import struct
import time
from typing import Awaitable, Callable, Optional

from pydantic import TypeAdapter

from .message import IpcRequest, IpcResponse, TaskRequest

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────────────────────

MAX_MESSAGE_BYTES = 16 * 1024 * 1024  # 16 MB
DEFAULT_UNIX_SOCKET_PATH = "/tmp/memory-bread-sidecar.sock"
UNIX_SOCKET_PATH = os.environ.get(
    "MEMORY_BREAD_IPC_SOCKET", DEFAULT_UNIX_SOCKET_PATH
)
TCP_HOST          = "127.0.0.1"
TCP_PORT          = 17071

# 请求解析适配器（Pydantic v2 推荐方式）
_task_adapter = TypeAdapter(TaskRequest)


# ─────────────────────────────────────────────────────────────────────────────
# 帧编解码
# ─────────────────────────────────────────────────────────────────────────────

class FrameCodec:
    """帧格式：[4字节大端 uint32 长度] + [N字节 UTF-8 JSON]"""

    @staticmethod
    async def read_frame(reader: asyncio.StreamReader) -> bytes:
        """从 StreamReader 读取一个完整帧，返回 JSON payload bytes"""
        # 读取 4 字节 length header
        header = await reader.readexactly(4)
        msg_len = struct.unpack(">I", header)[0]

        if msg_len > MAX_MESSAGE_BYTES:
            raise ValueError(
                f"消息体超过最大限制: {msg_len} > {MAX_MESSAGE_BYTES} bytes"
            )

        payload = await reader.readexactly(msg_len)
        return payload

    @staticmethod
    async def write_frame(writer: asyncio.StreamWriter, resp: IpcResponse) -> None:
        """将 IpcResponse 编码为帧并写入 StreamWriter"""
        frame = resp.to_frame()
        writer.write(frame)
        await writer.drain()

    @staticmethod
    def parse_request(payload: bytes) -> IpcRequest:
        """将 JSON bytes 解析为 IpcRequest（含 task 字段的 discriminated union）"""
        import json
        raw = json.loads(payload)
        # task 字段需要单独用 TypeAdapter 解析
        task = _task_adapter.validate_python(raw["task"])
        return IpcRequest(id=raw["id"], ts=raw["ts"], task=task)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# IPC 服务端
# ─────────────────────────────────────────────────────────────────────────────

# 分派函数类型：接收 IpcRequest，返回 IpcResponse
DispatchFn = Callable[[IpcRequest], Awaitable[IpcResponse]]


class IpcServer:
    """
    AI Sidecar 的 IPC 服务端。

    - macOS/Linux：监听 Unix Domain Socket
    - Windows    ：监听 TCP Loopback

    每个连接在独立 asyncio task 中处理，支持并发请求。
    """

    def __init__(self, dispatch_fn: DispatchFn) -> None:
        self._dispatch = dispatch_fn
        self._server: Optional[asyncio.Server] = None

    # ── 启动/停止 ─────────────────────────────────────────────────────────────

    async def serve(self) -> None:
        """启动服务端，阻塞直到进程退出"""
        if platform.system() == "Windows":
            await self._serve_tcp()
        else:
            await self._serve_unix()

    async def _serve_unix(self) -> None:
        # 只有无法连接时才把路径视为崩溃遗留。旧实现无条件 unlink，允许第二个
        # Sidecar 在同一路径重新 bind，旧进程则继续持有一个不可达 socket。
        if os.path.exists(UNIX_SOCKET_PATH):
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(UNIX_SOCKET_PATH),
                    timeout=0.25,
                )
            except ConnectionRefusedError:
                try:
                    os.remove(UNIX_SOCKET_PATH)
                except FileNotFoundError:
                    pass
            except FileNotFoundError:
                pass
            except (OSError, asyncio.TimeoutError) as exc:
                # 无法确认所有者已死亡时宁可拒绝启动，也不能删除可能仍在使用的
                # Socket；否则会再次制造一个不可达的旧 Sidecar。
                raise RuntimeError(
                    f"无法确认 IPC Socket 是否空闲: {UNIX_SOCKET_PATH}"
                ) from exc
            else:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                raise RuntimeError(
                    f"IPC Socket 已由另一个 Sidecar 使用: {UNIX_SOCKET_PATH}"
                )

        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=UNIX_SOCKET_PATH,
        )
        socket_stat = os.stat(UNIX_SOCKET_PATH)
        socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
        logger.info("IPC 服务端已启动（Unix Socket: %s）", UNIX_SOCKET_PATH)
        try:
            async with self._server:
                try:
                    await self._server.serve_forever()
                except asyncio.CancelledError:
                    if self._server.is_serving():
                        raise
        finally:
            self._server.close()
            await self._server.wait_closed()
            try:
                current_stat = os.stat(UNIX_SOCKET_PATH)
                if (current_stat.st_dev, current_stat.st_ino) == socket_identity:
                    os.remove(UNIX_SOCKET_PATH)
            except FileNotFoundError:
                pass

    async def _serve_tcp(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=TCP_HOST,
            port=TCP_PORT,
        )
        logger.info("IPC 服务端已启动（TCP: %s:%d）", TCP_HOST, TCP_PORT)
        async with self._server:
            await self._server.serve_forever()

    def stop(self) -> None:
        if self._server:
            self._server.close()

    # ── 连接处理 ──────────────────────────────────────────────────────────────

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """每个连接的处理循环，持续读取帧直到连接关闭"""
        peer = writer.get_extra_info("peername", "<unix>")
        logger.debug("新连接: %s", peer)

        try:
            while True:
                try:
                    payload = await FrameCodec.read_frame(reader)
                except asyncio.IncompleteReadError:
                    # 连接正常关闭
                    logger.debug("连接关闭: %s", peer)
                    break
                except ValueError as e:
                    logger.warning("帧解析错误: %s", e)
                    break

                # 解析请求
                try:
                    req = FrameCodec.parse_request(payload)
                except Exception as e:
                    logger.warning("请求解析失败: %s", e)
                    # 无法解析就无法知道 req.id，只能断开连接
                    break

                # 客户端在等待结果期间可能因本地超时而断开。旧实现仍会把已经
                # 没有调用方的 OCR 任务留在单线程队列中执行，自动扫描连续超时
                # 后会形成分钟级积压。并行监听连接 EOF，断开时立即取消仍在等待
                # 的业务协程；已经进入系统 OCR 调用的线程无法强停，但不会再让
                # 后续尚未开始的请求继续堆积。
                dispatch_task = asyncio.create_task(self._safe_dispatch(req))
                disconnect_task = asyncio.create_task(reader.read(1))
                done, _pending = await asyncio.wait(
                    {dispatch_task, disconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if dispatch_task not in done:
                    extra_data = disconnect_task.result()
                    dispatch_task.cancel()
                    await asyncio.gather(dispatch_task, return_exceptions=True)
                    if extra_data:
                        logger.warning("请求完成前收到额外 IPC 数据，关闭连接: %s", peer)
                    else:
                        logger.debug("客户端断开，已取消未完成请求 %s", req.id)
                    break

                disconnect_task.cancel()
                await asyncio.gather(disconnect_task, return_exceptions=True)
                resp = dispatch_task.result()
                try:
                    await FrameCodec.write_frame(writer, resp)
                except (ConnectionResetError, BrokenPipeError) as e:
                    logger.debug("连接在响应写回前已断开: %s (%s)", peer, e)
                    break

        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _safe_dispatch(self, req: IpcRequest) -> IpcResponse:
        """调用分派函数，捕获所有异常转换为错误响应"""
        t0 = time.monotonic()
        try:
            resp = await self._dispatch(req)
            return resp
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.exception("处理请求 %s 时发生内部错误", req.id)
            return IpcResponse.make_error(
                req_id=req.id,
                code="INTERNAL_ERROR",
                message=str(e),
                latency_ms=latency_ms,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 默认分派器（Sidecar 入口处替换为真实实现）
# ─────────────────────────────────────────────────────────────────────────────

async def default_dispatch(req: IpcRequest) -> IpcResponse:
    """
    占位分派器，实际由 ai-sidecar/main.py 中的真实分派器覆盖。
    此处仅处理 ping，其余任务返回 NOT_IMPLEMENTED。
    """
    from .message import PingResult, TaskRequest
    import time

    t0 = time.monotonic()

    task = req.task
    if hasattr(task, "type") and task.type == "ping":
        latency_ms = int((time.monotonic() - t0) * 1000)
        return IpcResponse.make_ok(
            req_id=req.id,
            result=PingResult(sidecar_version="0.1.0"),
            latency_ms=latency_ms,
        )

    latency_ms = int((time.monotonic() - t0) * 1000)
    return IpcResponse.make_error(
        req_id=req.id,
        code="NOT_IMPLEMENTED",
        message=f"Task type '{getattr(task, 'type', '?')}' not implemented in default dispatcher",
        latency_ms=latency_ms,
    )
