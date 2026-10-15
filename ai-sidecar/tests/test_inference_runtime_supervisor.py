import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from initialization_manager import InitializationManager
from inference_runtime_supervisor import InferenceRuntimeSupervisor, RecoveryPolicy
from runtime_lock import SidecarInstanceLock


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    manager = InitializationManager(tmp_path)
    state = manager._new_state("normal")
    state.update(state="completed", progress=100)
    state["quality_gate"]["passed"] = True
    manager._save_state(state)
    clock = [1000.0]
    engine = SimpleNamespace(healthy=False, owned=False, port=False, starts=[], stops=[])
    monkeypatch.setattr(manager, "_ollama_healthy", lambda _: engine.healthy)
    monkeypatch.setattr(manager, "_managed_ollama_process_owned", lambda _: engine.owned)
    monkeypatch.setattr(manager, "_port_in_use", lambda _: engine.port)
    monkeypatch.setattr(manager, "_managed_ollama_executable", lambda _: Path("/managed/ollama"))
    monkeypatch.setattr(manager, "_start_ollama", lambda *args: engine.starts.append(args))
    monkeypatch.setattr(manager, "_stop_owned_ollama_for_repair", lambda _: engine.stops.append(True) or True)
    supervisor = InferenceRuntimeSupervisor(manager, clock=lambda: clock[0])
    return manager, supervisor, engine, clock


def test_completed_initialization_recovers_without_status_poll(runtime):
    _, supervisor, engine, _ = runtime
    supervisor.check()
    assert len(engine.starts) == 1


def test_hung_owned_process_requires_consecutive_failures(runtime):
    _, supervisor, engine, clock = runtime
    engine.owned = True
    for index in range(3):
        clock[0] += 10
        supervisor.check()
        assert len(engine.starts) == (1 if index == 2 else 0)
    assert engine.stops == [True]


def test_recent_process_gets_startup_grace(runtime):
    manager, supervisor, engine, clock = runtime
    engine.owned = True
    marker = manager._workspace_root("normal") / "processes" / "ollama.json"
    manager._write_json_atomic(marker, {"create_time": clock[0]})
    for _ in range(8):
        supervisor.check()
        clock[0] += 10
    assert engine.starts == []
    clock[0] += 20
    supervisor.check()
    assert len(engine.starts) == 1


def test_restart_budget_persists_and_reopens_after_cooldown(runtime):
    manager, supervisor, engine, clock = runtime
    for offset in (0, 90, 180):
        clock[0] = 1000 + offset
        supervisor.check()
    assert len(engine.starts) == 3
    reloaded = InferenceRuntimeSupervisor(manager, clock=lambda: clock[0])
    for offset in range(190, 480, 10):
        clock[0] = 1000 + offset
        reloaded.check()
    assert len(engine.starts) == 3
    clock[0] = 1480
    reloaded.check()
    assert len(engine.starts) == 4
    assert json.loads(supervisor.state_path.read_text())["attempts"] == 1


def test_spawn_failure_is_budgeted(runtime, monkeypatch):
    manager, supervisor, engine, clock = runtime
    def fail(*_):
        engine.starts.append(True)
        raise OSError("spawn failed")
    monkeypatch.setattr(manager, "_start_ollama", fail)
    for _ in range(9):
        supervisor.check()
        clock[0] += 10
    assert len(engine.starts) == 1
    assert json.loads(supervisor.state_path.read_text())["status"] == "waiting"


@pytest.mark.parametrize("block", ["sandbox", "running", "external", "missing", "port", "identity", "lock"])
def test_never_races_initialization_or_kills_unmanaged_process(runtime, monkeypatch, block):
    manager, supervisor, engine, _ = runtime
    held = None
    if block == "sandbox":
        monkeypatch.setattr(manager, "_test_mode_enabled", lambda: True)
    elif block == "running":
        state = manager._load_state("normal")
        state["state"] = "running"
        manager._save_state(state)
    elif block == "external":
        monkeypatch.setattr(manager, "_external_backend_start_in_progress", lambda: True)
    elif block == "missing":
        monkeypatch.setattr(manager, "_managed_ollama_executable", lambda _: None)
    elif block == "port":
        engine.port = True
    elif block == "identity":
        engine.owned = True
        monkeypatch.setattr(manager, "_stop_owned_ollama_for_repair", lambda _: False)
    else:
        held = SidecarInstanceLock(supervisor.state_path.with_suffix(".lock"))
        held.acquire()
    try:
        for index in range(4):
            runtime[3][0] += 10
            supervisor.check()
        assert engine.starts == []
        assert engine.stops == []
    finally:
        if held:
            held.release()


def test_only_sustained_health_resets_attempts(runtime):
    _, supervisor, engine, clock = runtime
    supervisor.check()
    engine.healthy = True
    clock[0] += 10
    supervisor.check()
    assert json.loads(supervisor.state_path.read_text())["attempts"] == 1
    clock[0] += 60
    supervisor.check()
    assert json.loads(supervisor.state_path.read_text())["attempts"] == 0


def test_supervisor_start_is_idempotent_and_runs_without_frontend(runtime):
    _, supervisor, engine, _ = runtime
    entered = threading.Event()
    supervisor.check = entered.set
    supervisor.start()
    first = supervisor._thread
    supervisor.start()
    assert entered.wait(1)
    assert supervisor._thread is first
    supervisor.stop()
    first.join(1)
    assert not first.is_alive()


def test_frequent_status_refresh_does_not_starve_hung_process_recovery(runtime):
    _, supervisor, engine, clock = runtime
    engine.owned = True
    for second in range(31):
        clock[0] = 1000 + second
        supervisor.check(restart_unhealthy=False)
        if second % 10 == 0:
            supervisor.check()
    assert len(engine.starts) == 1
    assert engine.stops == [True]


def test_recovery_io_failure_does_not_break_status_refresh(runtime, monkeypatch):
    manager, supervisor, _, _ = runtime
    def fail(**_):
        raise OSError("recovery state unavailable")
    monkeypatch.setattr(supervisor, "check", fail)
    manager._runtime_supervisor = supervisor
    manager._try_restart_managed_ollama("normal")


def test_recovery_metadata_is_fresh_and_exposes_configured_budget(runtime):
    _, supervisor, _, clock = runtime
    supervisor.check()
    state = json.loads(supervisor.state_path.read_text())
    assert state['updated_at_ms'] == int(clock[0] * 1000)
    assert state['max_attempts'] == supervisor.policy.max_attempts
