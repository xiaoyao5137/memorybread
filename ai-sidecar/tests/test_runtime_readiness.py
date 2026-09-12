import threading
import time
from runtime_readiness import CapabilityWarmup


def test_warmup_single_flight_and_retries_after_failure():
    started, release = threading.Event(), threading.Event()
    calls = []
    def loader():
        calls.append(True)
        started.set()
        release.wait(1)
        raise RuntimeError('temporary failure')
    warmup = CapabilityWarmup(loader, retry_seconds=0)
    assert warmup.request()
    assert started.wait(1)
    assert not warmup.request()
    release.set()
    deadline = time.monotonic() + 1
    while warmup.lock.locked() and time.monotonic() < deadline:
        time.sleep(.001)
    assert warmup.request()
    deadline = time.monotonic() + 1
    while warmup.lock.locked() and time.monotonic() < deadline:
        time.sleep(.001)
    assert len(calls) == 2


def test_warmup_respects_backoff():
    warmup = CapabilityWarmup(lambda: None)
    warmup.next_attempt = time.monotonic() + 60
    assert not warmup.request()
    assert not warmup.lock.locked()
