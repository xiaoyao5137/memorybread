"""Recover an installed managed inference engine independently of UI polling."""

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from runtime_lock import SidecarAlreadyRunningError, SidecarInstanceLock
from initialization_diagnostics import exception_evidence, write_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryPolicy:
    probe_interval: float = 10.0
    failure_threshold: int = 3
    startup_grace: float = 90.0
    stable_health: float = 60.0
    retry_base: float = 30.0
    max_attempts: int = 3
    cooldown: float = 300.0


class InferenceRuntimeSupervisor:
    def __init__(self, manager, policy: Optional[RecoveryPolicy] = None, clock=time.time):
        self.manager = manager
        self.policy = policy or RecoveryPolicy()
        self.clock = clock
        self._stop = threading.Event()
        self._thread = None
        self._thread_lock = threading.Lock()
        self.state_path = manager.base_dir / "initialization" / "runtime-recovery.json"

    def start(self):
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="inference-runtime-supervisor", daemon=True
            )
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:
                logger.exception("local inference supervision failed; will retry")
            self._stop.wait(self.policy.probe_interval)

    def check(self, restart_unhealthy=True):
        manager = self.manager
        with manager._lock:
            if manager._test_mode_enabled() or manager._external_backend_start_in_progress():
                return
            state = manager._load_state("normal")
            if state.get("state") != "completed" and not (
                state.get("state") == "interrupted"
                and state.get("error_code") == "INITIALIZATION_COMPONENT_MISSING"
            ):
                return
            if manager._thread is not None and manager._thread.is_alive():
                return
            lock = SidecarInstanceLock(self.state_path.with_suffix(".lock"))
            try:
                lock.acquire()
            except SidecarAlreadyRunningError:
                return
            try:
                self._check_locked(restart_unhealthy)
            finally:
                lock.release()

    def _check_locked(self, restart_unhealthy):
        manager, policy, now = self.manager, self.policy, self.clock()
        # Read-only UI refreshes must not consume the worker's probe slot for a
        # hung owned engine; otherwise frequent polling can starve recovery.
        if not restart_unhealthy and manager._managed_ollama_process_owned("normal"):
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        # Persist budgets across helper restarts and share them with status refreshes.
        if now < state.get("next_probe", 0):
            return
        previous_status = state.get("status")
        recovery_error = None
        recovery_evidence = {}
        state["updated_at_ms"] = int(now * 1000)
        state["max_attempts"] = policy.max_attempts
        state["next_probe"] = now + policy.probe_interval
        try:
            base_url = manager._ollama_base_url("normal")
            healthy = manager._ollama_healthy(base_url)
            recovery_evidence.update(manager._runtime_probes.get(base_url, {}))
            recovery_evidence["port"] = manager._ollama_port("normal")
            if healthy:
                state["failures"] = 0
                state.setdefault("healthy_since", now)
                state["status"] = "healthy"
                if now - state["healthy_since"] >= policy.stable_health:
                    state.update(attempts=0, next_attempt=0, startup_until=0)
                return
            state.pop("healthy_since", None)
            state["failures"] = state.get("failures", 0) + 1
            state["status"] = "unavailable"
            if now < max(state.get("next_attempt", 0), state.get("startup_until", 0)):
                state["status"] = "waiting"
                return
            executable = manager._managed_ollama_executable("normal")
            if executable is None:
                state["status"] = "component_missing"
                return  # Recovery never downloads or repeats initialization.
            owned = manager._managed_ollama_process_owned("normal")
            if owned:
                try:
                    marker_path = manager._workspace_root("normal") / "processes" / "ollama.json"
                    created = json.loads(marker_path.read_text(encoding="utf-8")).get("create_time")
                    if created is not None and now - float(created) < policy.startup_grace:
                        state["status"] = "warming_up"
                        return
                except (OSError, ValueError, TypeError):
                    pass
            if owned and (not restart_unhealthy or state["failures"] < policy.failure_threshold):
                return
            if not owned and manager._port_in_use(manager._ollama_port("normal")):
                state["status"] = "port_conflict"
                return  # Never terminate a third-party listener.
            attempts = state.get("attempts", 0)
            if attempts >= policy.max_attempts:
                # A completed cooldown opens a new bounded recovery window.
                attempts = 0
            state.update(attempts=attempts + 1, failures=0, status="restarting")
            delay = policy.retry_base * (2 ** attempts)
            if state["attempts"] >= policy.max_attempts:
                delay = max(delay, policy.cooldown)
            state["next_attempt"] = now + delay
            state["startup_until"] = now + policy.startup_grace
            # Commit before process mutation, so crashes cannot reset the budget.
            manager._write_json_atomic(self.state_path, state)
            try:
                if owned and not manager._stop_owned_ollama_for_repair("normal"):
                    recovery_evidence["reason"] = "identity_unverified"
                    raise RuntimeError("managed process identity could not be verified for restart")
                manager._start_ollama("normal", executable)
                logger.warning("local inference recovery started (attempt %s/%s)",
                               state["attempts"], policy.max_attempts)
            except Exception as exc:
                recovery_error = exc
                recovery_evidence.update(getattr(exc, "evidence", {}))
                recovery_evidence.update(exception_evidence(exc))
                state.update(status="restart_failed", last_error=str(exc))
                logger.warning("local inference recovery failed; retry scheduled: %s", exc)
        finally:
            if state.get("status") != previous_status:
                recovery_evidence.setdefault("reason", state.get("status"))
                write_event(manager.base_dir, "runtime_" + state.get("status", "unknown"),
                            manager._load_state("normal"), evidence=recovery_evidence,
                            error=recovery_error, attempt=state.get("attempts", 0))
            if state.get("status") != previous_status and state.get("status") in {
                "component_missing", "port_conflict"
            }:
                logger.warning("local inference recovery blocked: %s", state["status"])
            manager._write_json_atomic(self.state_path, state)
