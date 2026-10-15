"""Keep deferred sidecar activation retryable for the lifetime of the service."""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def monitor_runtime_upgrade(check, activate, needed, interval=30.0, sleep=asyncio.sleep):
    checks = 0
    logger.info("自动升级监控已启动：等待本地推理服务恢复")
    while needed():
        await sleep(interval)
        if not needed():
            return
        checks += 1
        try:
            result = await asyncio.to_thread(check)
            if result.get("critical_passed"):
                await activate(result)
                logger.info("自动升级成功：后台提炼已启动")
                return
            if checks % 10 == 0:
                logger.warning("本地推理服务仍不可用（第 %s 次），继续等待后台恢复", checks)
        except Exception:
            logger.warning("完整能力启动失败，稍后重试（第 %s 次）", checks, exc_info=True)
