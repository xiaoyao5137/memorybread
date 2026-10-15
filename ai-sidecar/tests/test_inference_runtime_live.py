"""Opt-in lifecycle proof with an installed binary and isolated model storage."""
import json
import os
import shutil
import signal
import socket
import time
from pathlib import Path

import psutil
import pytest

from initialization_manager import InitializationManager
from inference_runtime_supervisor import InferenceRuntimeSupervisor, RecoveryPolicy


@pytest.mark.skipif(not os.environ.get("MEMORY_BREAD_TEST_RUNTIME_DIR"), reason="requires a verified local runtime")
def test_real_runtime_recovers_from_exit_and_unresponsive_process(tmp_path, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MEMORY_BREAD_OLLAMA_URL", "http://127.0.0.1:%s" % port)
    manager = InitializationManager(tmp_path / "app")
    source = Path(os.environ["MEMORY_BREAD_TEST_RUNTIME_DIR"])
    shutil.copytree(source, manager._runtime_root("normal") / source.name, copy_function=os.link)
    state = manager._new_state("normal")
    state.update(state="completed", progress=100)
    state["quality_gate"]["passed"] = True
    manager._save_state(state)
    supervisor = InferenceRuntimeSupervisor(manager, RecoveryPolicy(
        probe_interval=0, startup_grace=0, stable_health=0, retry_base=0, cooldown=0
    ))
    url = manager._ollama_base_url("normal")
    marker = manager._workspace_root("normal") / "processes" / "ollama.json"

    def ready():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if manager._ollama_healthy(url):
                return json.loads(marker.read_text())["pid"]
            time.sleep(0.1)
        pytest.fail("isolated managed runtime did not become healthy")

    try:
        supervisor.check()  # No initialization-status request.
        first = ready()
        assert manager._managed_ollama_process_owned("normal")
        psutil.Process(first).kill()
        manager._processes["ollama:normal"].wait(5)
        supervisor.check()
        second = ready()
        assert first != second
        supervisor.check()  # Clear failure budget after recovery.
        os.kill(second, signal.SIGSTOP)
        for _ in range(3):
            supervisor.check()
        third = ready()
        assert third != second
        assert manager._managed_ollama_process_owned("normal")
    finally:
        supervisor.stop()
        manager._stop_owned_ollama_for_repair("normal")
