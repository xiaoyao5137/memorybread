"""
RAG Worker - 处理 RAG 查询任务

注意：Embed 任务由 embedding/worker.py 处理
这个 Worker 主要负责查询（未来扩展）
"""

from __future__ import annotations

import logging
import time
from memory_bread_ipc import IpcRequest, IpcResponse

logger = logging.getLogger(__name__)


class RagWorker:
    """RAG 任务处理器"""
    
    def __init__(self):
        logger.info("RagWorker 初始化")
    
    async def handle(self, req: IpcRequest) -> IpcResponse:
        """
        处理 RAG 相关任务
        
        目前 RAG 查询通过 HTTP API (端口 7071) 提供
        IPC 层面暂不处理 RAG 任务
        """
        t0 = time.monotonic()
        latency_ms = int((time.monotonic() - t0) * 1000)
        
        logger.warning("RAG 任务应通过 HTTP API (端口 7071) 调用，而非 IPC")
        return IpcResponse.make_error(
            req.id,
            "NOT_IMPLEMENTED",
            "RAG 查询请使用本机模型 API，而非 IPC",
            latency_ms,
        )
