import asyncio
import threading

import pytest
from memory_bread_ipc import IpcRequest
from memory_bread_ipc.message import InteractiveOcrActivityRequest
from ocr.backends.base import OcrBox, OcrOutput
from ocr.control import current_control
from ocr.worker import OcrWorker


def request(name, priority='background'):
    return IpcRequest(id=name, ts=1, task={'type': 'ocr', 'capture_id': 0,
                                         'screenshot_path': name, 'priority': priority})


def activity(worker, name, active):
    worker.activity(InteractiveOcrActivityRequest(activity_id=name, active=active))


def stub_interactive_demand(monkeypatch, callback):
    # test_engine deliberately evicts ocr.worker from sys.modules after test
    # collection. Patch the module globals owned by this collected class so the
    # full suite and isolated runs exercise the same dependency.
    monkeypatch.setitem(
        OcrWorker._interactive_active.__globals__,
        'interactive_demand_active',
        callback,
    )


@pytest.mark.asyncio
async def test_foreground_cancels_background_and_resumes_same_image_after_all_sessions(monkeypatch):
    stub_interactive_demand(monkeypatch, lambda: False)
    started = threading.Event()
    cancelled = threading.Event()
    calls = []
    class Engine:
        def process(self, path):
            calls.append(path)
            if path == 'background' and calls.count(path) == 1:
                with current_control().native_request(cancelled.set):
                    started.set()
                    assert cancelled.wait(2), 'native request was not cancelled'
            return OcrOutput([OcrBox(path, 1)])
    worker = OcrWorker(engine=Engine())
    background = asyncio.create_task(worker.handle(request('background')))
    assert await asyncio.to_thread(started.wait, 1)
    # Both UI sessions hold background even after their individual OCR returns.
    activity(worker, 'consultation', True)
    activity(worker, 'creation', True)
    user = await worker.handle(request('user', 'foreground'))
    assert user.result.text == 'user'
    assert cancelled.is_set()
    assert 'OCR_DEFERRED' in (await background).error
    assert (await worker.handle(request('background'))).result is None
    activity(worker, 'consultation', False)
    assert 'OCR_DEFERRED' in (await worker.handle(request('background'))).error
    activity(worker, 'creation', False)
    assert (await worker.handle(request('background'))).result.text == 'background'
    assert calls == ['background', 'user', 'background']


@pytest.mark.asyncio
async def test_foreground_request_itself_preempts_without_activity_lease(monkeypatch):
    stub_interactive_demand(monkeypatch, lambda: False)
    started, cancelled = threading.Event(), threading.Event()
    class Engine:
        def process(self, path):
            if path == 'background':
                with current_control().native_request(cancelled.set):
                    started.set()
                    assert cancelled.wait(2)
            return OcrOutput([OcrBox(path, 1)])
    worker = OcrWorker(engine=Engine())
    background = asyncio.create_task(worker.handle(request('background')))
    assert await asyncio.to_thread(started.wait, 1)
    foreground = await worker.handle(request('user', 'foreground'))
    assert foreground.result.text == 'user'
    assert 'OCR_DEFERRED' in (await background).error


@pytest.mark.asyncio
async def test_expired_lease_and_existing_p0_demand(monkeypatch):
    demand = [False]
    stub_interactive_demand(monkeypatch, lambda: demand[0])
    class Engine:
        def process(self, path):
            return OcrOutput([OcrBox(path, 1)])
    worker = OcrWorker(engine=Engine())
    activity(worker, 'abandoned', True)
    worker._activities['abandoned'] = 0
    assert (await worker.handle(request('background'))).result.text == 'background'
    demand[0] = True
    assert 'OCR_DEFERRED' in (await worker.handle(request('background'))).error
    assert (await worker.handle(request('user', 'foreground'))).result.text == 'user'
    demand[0] = False
    assert (await worker.handle(request('background'))).result.text == 'background'


@pytest.mark.asyncio
async def test_cancelled_client_cancels_native_call_and_frees_foreground_count(monkeypatch):
    stub_interactive_demand(monkeypatch, lambda: False)
    started, cancelled = threading.Event(), threading.Event()
    class Engine:
        def process(self, path):
            with current_control().native_request(cancelled.set):
                started.set()
                assert cancelled.wait(2)
    worker = OcrWorker(engine=Engine())
    task = asyncio.create_task(worker.handle(request('user', 'foreground')))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert worker._foreground_pending == 0
    assert not worker._ocr_semaphore.locked()


def test_legacy_request_defaults_to_background():
    req = IpcRequest(id='legacy', ts=1, task={'type':'ocr', 'capture_id':0, 'screenshot_path':'image'})
    assert req.task.priority == 'background'


def test_native_cancel_failure_still_discards_result():
    from ocr.control import OcrControl, OcrDeferred
    control = OcrControl()
    def unavailable():
        raise RuntimeError('not cancellable')
    with pytest.raises(OcrDeferred):
        with control.native_request(unavailable):
            control.cancel()
            control.cancel()
