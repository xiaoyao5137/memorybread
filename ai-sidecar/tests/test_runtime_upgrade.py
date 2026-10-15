import asyncio

from runtime_upgrade import monitor_runtime_upgrade


def test_recovers_after_old_thirty_minute_limit():
    probes, activations = [], []
    def check():
        probes.append(True)
        return {"critical_passed": len(probes) > 60, "embedding_ok": False}
    async def activate(result):
        activations.append(result)
    async def sleep(_):
        pass
    asyncio.run(monitor_runtime_upgrade(check, activate, lambda: True, sleep=sleep))
    assert len(probes) == 61
    assert len(activations) == 1


def test_activation_error_retries_instead_of_losing_upgrade():
    calls = []
    async def activate(_):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("temporary dispatcher error")
    async def sleep(_):
        pass
    asyncio.run(monitor_runtime_upgrade(lambda: {"critical_passed": True}, activate,
                                       lambda: True, sleep=sleep))
    assert len(calls) == 2


def test_shutdown_cancels_monitor():
    async def run():
        task = asyncio.create_task(monitor_runtime_upgrade(lambda: {}, None, lambda: True))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        raise AssertionError("monitor ignored cancellation")
    asyncio.run(run())
