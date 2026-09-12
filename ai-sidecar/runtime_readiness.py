"""Single-flight warmup after runtime recovery, without blocking status requests."""
import logging
import threading
import time
from typing import Callable


class CapabilityWarmup:
    def __init__(self, loader: Callable[[], object], retry_seconds: float = 15):
        self.loader = loader
        self.retry_seconds = retry_seconds
        self.lock = threading.Lock()
        self.next_attempt = 0.0

    def request(self) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        if time.monotonic() < self.next_attempt:
            self.lock.release()
            return False

        def run():
            try:
                self.loader()
            except Exception:
                logging.getLogger(__name__).warning('consultation warmup failed; will retry')
            finally:
                self.next_attempt = time.monotonic() + self.retry_seconds
                self.lock.release()
        try:
            threading.Thread(target=run, daemon=True, name='consultation-readiness-warmup').start()
        except Exception:
            self.lock.release()
            raise
        return True
