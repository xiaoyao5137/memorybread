"""
OcrWorker — IPC OCR 任务处理器

接收来自 Rust Core Engine 的 OcrRequest，
调用 OcrEngine 执行识别，将结果封装为 IpcResponse 返回。

设计要点：
- 所有异常在此处被捕获并转换为错误响应，不向上抛出
- 记录处理时间（latency_ms）用于性能监控
- 支持注入自定义 OcrEngine（测试用）
- 集成隐私过滤器，自动抹除敏感内容
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from memory_bread_ipc import IpcRequest, IpcResponse, OcrResult

from .engine import OcrEngine
from .privacy_filter import PrivacyFilter

logger = logging.getLogger(__name__)
_OCR_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-bread-ocr")


class OcrWorker:
    """
    处理 IPC OcrRequest 的异步 Worker。

    handle() 在 asyncio 事件循环中运行，但 OcrEngine.process()
    是同步阻塞调用，通过 run_in_executor 在线程池中执行，
    避免阻塞 asyncio 事件循环。
    """

    def __init__(self, engine: Optional[OcrEngine] = None, enable_privacy_filter: bool = False) -> None:
        self._engine = engine or OcrEngine.create_default()
        self._privacy_filter = PrivacyFilter() if enable_privacy_filter else None
        # Vision/Paddle 都是同步且资源密集的实现。独立的单线程执行器提供硬并发上限，
        # Semaphore 则避免多个协程提前向执行器内部继续排队。
        self._ocr_semaphore: Optional[asyncio.Semaphore] = None
        self._ocr_semaphore_loop: Optional[asyncio.AbstractEventLoop] = None
        self._executor = _OCR_EXECUTOR

    async def handle(self, req: IpcRequest) -> IpcResponse:
        """
        异步处理一个 OcrRequest。

        将同步的 engine.process() 包装到线程池中执行，
        不阻塞 asyncio 事件循环（兼容高并发场景）。
        """
        t0   = time.monotonic()
        task = req.task  # OcrRequest

        try:
            loop = asyncio.get_running_loop()
            if self._ocr_semaphore is None or self._ocr_semaphore_loop is not loop:
                self._ocr_semaphore = asyncio.Semaphore(1)
                self._ocr_semaphore_loop = loop
            ocr_semaphore = self._ocr_semaphore
            # OCR 全局串行执行，避免 Vision 在同一张图的超时重试下并发放大。
            async with ocr_semaphore:
                output = await loop.run_in_executor(
                    self._executor, self._engine.process, task.screenshot_path
                )

            # 隐私过滤
            text = output.text
            if self._privacy_filter and text:
                filter_result = self._privacy_filter.detect_and_redact(text, output.boxes)
                if filter_result.is_sensitive:
                    text = filter_result.sanitized_text
                    logger.info(
                        "OCR 敏感内容已过滤 capture_id=%d | 类型=%s",
                        task.capture_id, filter_result.detected_types
                    )

            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "OCR 完成 capture_id=%d | %d行 | 置信度=%.3f | %dms",
                task.capture_id, len(output.boxes), output.confidence, latency_ms,
            )

            result = OcrResult(
                text=text,
                confidence=round(output.confidence, 4),
                language=output.language,
            )
            return IpcResponse.make_ok(req.id, result, latency_ms)

        except FileNotFoundError as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("OCR 文件不存在 capture_id=%d: %s", task.capture_id, exc)
            return IpcResponse.make_error(
                req.id, "FILE_NOT_FOUND", str(exc), latency_ms
            )

        except RuntimeError as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.error("OCR 运行时失败 capture_id=%d: %s", task.capture_id, exc)
            return IpcResponse.make_error(
                req.id, "OCR_FAILED", str(exc), latency_ms
            )

        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.exception("OCR 未预期异常 capture_id=%d", task.capture_id)
            return IpcResponse.make_error(
                req.id, "INTERNAL_ERROR", str(exc), latency_ms
            )

    def close(self) -> None:
        """停止接收新的 OCR 线程任务；进程退出时不等待已运行的系统调用。"""
        self._executor.shutdown(wait=False, cancel_futures=True)
