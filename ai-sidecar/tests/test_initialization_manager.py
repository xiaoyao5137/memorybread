from __future__ import annotations

import json
import threading
import time

import pytest

from initialization_manager import InitializationFailure, InitializationManager, STAGES


def _wait_for_terminal(manager: InitializationManager, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    state = manager.get_status()
    while state["state"] not in {"completed", "failed"} and time.monotonic() < deadline:
        time.sleep(0.01)
        state = manager.get_status()
    return state


def _stub_successful_stages(monkeypatch: pytest.MonkeyPatch, manager: InitializationManager) -> None:
    monkeypatch.setattr(manager, "_completed_state_still_valid", lambda _mode: True)
    monkeypatch.setattr(manager, "_notify_os_completion", lambda: None)
    for stage_id, _label, _start, _end in STAGES:
        monkeypatch.setattr(
            manager,
            f"_stage_{stage_id}",
            lambda _mode, _state, current=stage_id: (current != "feature_smoke_tests", f"{current} ok"),
        )


def test_one_click_initialization_runs_all_stages_and_persists_completion(monkeypatch, tmp_path):
    manager = InitializationManager(base_dir=tmp_path)
    _stub_successful_stages(monkeypatch, manager)

    started = manager.start("normal")
    finished = _wait_for_terminal(manager)

    assert started["run_id"]
    assert finished["run_id"] == started["run_id"]
    assert finished["state"] == "completed"
    assert finished["progress"] == 100
    assert finished["quality_gate"]["passed"] is True
    assert all(stage["status"] in {"skipped", "succeeded"} for stage in finished["stages"])

    monkeypatch.setattr(InitializationManager, "_completed_state_still_valid", lambda _self, _mode: True)
    reloaded = InitializationManager(base_dir=tmp_path)
    assert reloaded.get_status()["state"] == "completed"


def test_duplicate_start_returns_same_running_task(monkeypatch, tmp_path):
    manager = InitializationManager(base_dir=tmp_path)
    release = threading.Event()

    def blocked_preflight(_mode, _state):
        release.wait(timeout=1)
        return False, "preflight ok"

    _stub_successful_stages(monkeypatch, manager)
    monkeypatch.setattr(manager, "_stage_preflight", blocked_preflight)

    first = manager.start("normal")
    second = manager.start("normal")
    release.set()

    assert second["run_id"] == first["run_id"]
    assert _wait_for_terminal(manager)["state"] == "completed"


def test_failure_exposes_stable_code_and_privacy_safe_report(monkeypatch, tmp_path):
    manager = InitializationManager(base_dir=tmp_path)

    def fail(_mode, _state):
        raise InitializationFailure(
            "MODEL_DOWNLOAD_FAILED",
            f"Ollama {_CAPTURE_INTERNAL_NAME_FOR_TEST()} failed at {tmp_path}",
        )

    monkeypatch.setattr(manager, "_stage_preflight", fail)
    state = manager.start("normal")
    assert state["state"] == "running"

    failed = _wait_for_terminal(manager)
    report = manager.get_report_bundle()
    serialized = json.dumps(report, ensure_ascii=False)

    assert failed["state"] == "failed"
    assert failed["error_code"] == "MODEL_DOWNLOAD_FAILED"
    assert failed["can_retry"] is True
    assert report["error_code"] == "MODEL_DOWNLOAD_FAILED"
    assert "qwen" not in serialized.lower()
    assert "ollama" not in serialized.lower()
    assert str(tmp_path) not in serialized
    assert "prompt" not in report


def _CAPTURE_INTERNAL_NAME_FOR_TEST() -> str:
    # 测试只确认端侧脱敏，不把内部名称写入断言输出。
    return "qwen3.5:4b"


def test_test_mode_is_isolated_and_restores_normal_state(monkeypatch, tmp_path):
    manager = InitializationManager(base_dir=tmp_path)
    monkeypatch.setattr(manager, "_completed_state_still_valid", lambda _mode: True)
    normal_state = manager._new_state("normal")
    normal_state.update({"state": "completed", "progress": 100})
    normal_state["quality_gate"]["passed"] = True
    manager._save_state(normal_state)

    enabled = manager.enable_test_mode("ENABLE_INITIALIZATION_TEST_MODE")
    assert enabled["mode"] == "sandbox"
    assert enabled["test_mode_enabled"] is True
    assert manager.sandbox_root.exists()

    marker = manager.sandbox_root / "sandbox-only.txt"
    marker.write_text("temporary", encoding="utf-8")
    restored = manager.disable_test_mode("DISABLE_INITIALIZATION_TEST_MODE")

    assert restored["mode"] == "normal"
    assert restored["state"] == "completed"
    assert restored["test_mode_enabled"] is False
    assert not manager.sandbox_root.exists()


def test_test_mode_requires_explicit_confirmation(tmp_path):
    manager = InitializationManager(base_dir=tmp_path)

    with pytest.raises(InitializationFailure) as exc:
        manager.enable_test_mode("yes")

    assert exc.value.code == "CONFIRMATION_REQUIRED"


def test_completed_state_is_gated_again_when_a_required_component_disappears(monkeypatch, tmp_path):
    manager = InitializationManager(base_dir=tmp_path)
    completed = manager._new_state("normal")
    completed.update({"state": "completed", "progress": 100})
    completed["quality_gate"]["passed"] = True
    manager._save_state(completed)
    monkeypatch.setattr(manager, "_completed_state_still_valid", lambda _mode: False)

    state = manager.get_status()

    assert state["state"] == "interrupted"
    assert state["progress"] == 0
    assert state["can_retry"] is True
    assert state["can_report"] is False
    assert state["error_code"] == "INITIALIZATION_COMPONENT_MISSING"


def test_transient_engine_outage_keeps_completed_state_within_grace_window(monkeypatch, tmp_path):
    manager = InitializationManager(base_dir=tmp_path)
    completed = manager._new_state("normal")
    completed.update({"state": "completed", "progress": 100})
    completed["quality_gate"]["passed"] = True
    manager._save_state(completed)
    monkeypatch.setattr(manager, "_completed_state_still_valid", lambda _mode: False)
    monkeypatch.setattr(manager, "_components_genuinely_missing", lambda _mode: False)

    state = manager.get_status()

    assert state["state"] == "completed"
    assert state["error_code"] is None


def test_persistent_engine_outage_demotes_after_grace_window(monkeypatch, tmp_path):
    manager = InitializationManager(base_dir=tmp_path)
    completed = manager._new_state("normal")
    completed.update({"state": "completed", "progress": 100})
    completed["quality_gate"]["passed"] = True
    manager._save_state(completed)
    monkeypatch.setattr(manager, "_completed_state_still_valid", lambda _mode: False)
    monkeypatch.setattr(manager, "_components_genuinely_missing", lambda _mode: False)

    manager.get_status()
    manager._invalid_since["normal"] = time.monotonic() - 1000

    state = manager.get_status()

    assert state["state"] == "interrupted"
    assert state["error_code"] == "INITIALIZATION_COMPONENT_MISSING"


def test_interrupted_state_recovers_when_components_become_valid_again(monkeypatch, tmp_path):
    manager = InitializationManager(base_dir=tmp_path)
    interrupted = manager._empty_state("normal")
    interrupted.update(
        {
            "state": "interrupted",
            "error_code": "INITIALIZATION_COMPONENT_MISSING",
            "can_retry": True,
        }
    )
    manager._save_state(interrupted)
    monkeypatch.setattr(manager, "_completed_state_still_valid", lambda _mode: True)

    state = manager.get_status()

    assert state["state"] == "completed"
    assert state["progress"] == 100
    assert state["error_code"] is None
    assert state["quality_gate"]["passed"] is True
    persisted = json.loads(manager.normal_state_path.read_text(encoding="utf-8"))
    assert persisted["state"] == "completed"


def test_normal_initialization_migrates_gui_runtime_to_managed_cli(monkeypatch, tmp_path):
    manager = InitializationManager(base_dir=tmp_path)
    running = {"value": True}
    calls: list[str] = []
    executable = tmp_path / "initialization" / "runtime" / "ollama" / "v-test" / "runtime" / "ollama"

    monkeypatch.setattr(manager, "_ollama_healthy", lambda _url: running["value"])
    monkeypatch.setattr(manager, "_ollama_gui_running", lambda: True)
    monkeypatch.setattr(manager, "_managed_ollama_process_owned", lambda _mode: False)
    monkeypatch.setattr(manager, "_managed_ollama_executable", lambda _mode: executable)
    monkeypatch.setattr(manager, "_port_in_use", lambda _port: running["value"])

    def stop_gui():
        calls.append("stop_gui")
        running["value"] = False

    def start_managed(_mode, selected_executable):
        calls.append("start_managed")
        assert selected_executable == executable
        running["value"] = True

    monkeypatch.setattr(manager, "_stop_ollama_gui", stop_gui)
    monkeypatch.setattr(manager, "_start_ollama", start_managed)

    state = manager._new_state("normal")
    skipped, detail = manager._stage_inference_engine("normal", state)

    assert skipped is False
    assert calls == ["stop_gui", "start_managed"]
    assert state["legacy_runtime_migrated"] is True
    assert "无界面后台运行" in detail


def test_completed_state_reopens_gate_when_gui_runtime_returns(monkeypatch, tmp_path):
    manager = InitializationManager(base_dir=tmp_path)
    monkeypatch.setattr(manager, "_ollama_healthy", lambda _url: True)
    monkeypatch.setattr(manager, "_ollama_gui_running", lambda: True)

    assert manager._completed_state_still_valid("normal") is False


def test_smoke_generation_disables_long_thinking_and_bounds_output(monkeypatch, tmp_path):
    manager = InitializationManager(base_dir=tmp_path)
    captured: dict = {}

    def fake_http_json(_url, payload=None, timeout=30):
        captured["payload"] = json.loads(payload.decode("utf-8"))
        captured["timeout"] = timeout
        return {"response": "正常"}

    monkeypatch.setattr(manager, "_http_json", fake_http_json)

    assert manager._ollama_generate("normal", "固定质检问题") == "正常"
    assert captured["payload"]["think"] is False
    assert captured["payload"]["options"] == {
        "num_ctx": 2048,
        "num_predict": 64,
        "temperature": 0,
    }
    assert captured["timeout"] == 180
