"""Cooperative cancellation for one native OCR invocation, without partial results."""
import threading
import logging
from contextlib import contextmanager


class OcrDeferred(Exception):
    pass


class OcrControl:
    def __init__(self):
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._callback = None

    def cancel(self):
        if self.cancelled.is_set():
            return
        self.cancelled.set()
        with self._lock:
            if self._callback:
                try:
                    self._callback()
                except Exception:
                    # Still discard the cancelled result and yield once the native call returns.
                    logging.getLogger(__name__).warning('Native OCR cancellation unavailable; waiting for safe boundary')

    def check(self):
        if self.cancelled.is_set():
            raise OcrDeferred('OCR_DEFERRED')

    @contextmanager
    def native_request(self, cancel):
        with self._lock:
            self.check()
            self._callback = cancel
        try:
            yield
            self.check()
        finally:
            with self._lock:
                self._callback = None


_local = threading.local()


def current_control():
    return getattr(_local, 'control', None)


@contextmanager
def use_control(control):
    previous = current_control()
    _local.control = control
    try:
        control.check()
        yield
        control.check()
    finally:
        _local.control = previous
