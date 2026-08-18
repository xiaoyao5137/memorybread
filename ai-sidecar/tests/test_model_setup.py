from __future__ import annotations

import json
import threading
import time

import pytest

import model_manager as model_manager_module
from model_manager import (
    MIN_MACOS_MAJOR_FOR_OLLAMA,
    OLLAMA_MACOS_DOWNLOAD_URL,
    ModelManager,
)


def _manager(tmp_path) -> ModelManager:
    return ModelManager(config_path=tmp_path / "model_config.json")


def _mock_macos(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr(model_manager_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(model_manager_module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(model_manager_module.platform, "mac_ver", lambda: (version, ("", "", ""), ""))


def test_setup_status_guides_clean_mac_without_homebrew_to_official_download(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    _mock_macos(monkeypatch, "14.6.1")
    monkeypatch.setattr(manager, "_resolve_ollama_command", lambda: None)
    monkeypatch.setattr(manager, "_is_ollama_running", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(model_manager_module.shutil, "which", lambda _name: None)

    detail = manager.get_ollama_setup_status()

    assert detail["ollama_installed"] is False
    assert detail["ollama_running"] is False
    assert detail["can_auto_install"] is False
    assert detail["minimum_macos_major"] == MIN_MACOS_MAJOR_FOR_OLLAMA
    assert detail["official_download_url"] == OLLAMA_MACOS_DOWNLOAD_URL

    result = manager.install_ollama_auto()
    assert result["status"] == "error"
    assert result["stage"] == "manual_install"
    assert result["official_download_url"] == OLLAMA_MACOS_DOWNLOAD_URL


def test_setup_status_rejects_macos_older_than_current_ollama_requirement(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    # DMG 内嵌 Ollama 支持 macOS 12+，低于该版本才应被拒绝。
    _mock_macos(monkeypatch, "11.7.10")
    # 版本探测优先走真实 sw_vers，必须一并 mock 才能模拟旧系统。
    def _fake_sw_vers(*args, **kwargs):
        if args and args[0][:2] == ["sw_vers", "-productVersion"]:
            raise FileNotFoundError("sw_vers mocked away")
        raise FileNotFoundError("unexpected subprocess in test")

    monkeypatch.setattr(model_manager_module.subprocess, "run", _fake_sw_vers)
    monkeypatch.setattr(manager, "_parse_macos_major_version", lambda: 11)
    monkeypatch.setattr(manager, "_resolve_ollama_command", lambda: None)
    monkeypatch.setattr(manager, "_is_ollama_running", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(model_manager_module.shutil, "which", lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None)

    detail = manager.get_ollama_setup_status()

    assert MIN_MACOS_MAJOR_FOR_OLLAMA == 12
    assert detail["version_compatible"] is False
    assert detail["can_auto_install"] is False
    assert "12+" in detail["message"]


def test_running_ollama_api_is_ready_even_when_cli_is_not_on_path(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    _mock_macos(monkeypatch, "14.6.1")
    monkeypatch.setattr(manager, "_resolve_ollama_command", lambda: None)
    monkeypatch.setattr(manager, "_is_ollama_running", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(model_manager_module.shutil, "which", lambda _name: None)

    detail = manager.get_ollama_setup_status()

    assert detail["ollama_installed"] is True
    assert detail["ollama_running"] is True
    assert detail["can_auto_install"] is True


@pytest.mark.parametrize(
    ("public_model_id", "ollama_model_name"),
    [
        ("mbem-v1-local", "qwen3.5:4b"),
        ("bge-small-zh", "qllama/bge-small-zh-v1.5:q4_k_m"),
    ],
)
def test_text_and_vector_downloads_use_the_expected_internal_ollama_model(
    monkeypatch,
    tmp_path,
    public_model_id,
    ollama_model_name,
):
    manager = _manager(tmp_path)
    requested_names: list[str] = []
    completed = threading.Event()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter([b'{"status":"success"}\n'])

    def fake_urlopen(request, timeout):
        assert timeout == 3600
        requested_names.append(json.loads(request.data.decode("utf-8"))["name"])
        completed.set()
        return FakeResponse()

    monkeypatch.setattr(model_manager_module.urllib.request, "urlopen", fake_urlopen)

    result = manager.download_model(public_model_id)

    assert result["status"] == "downloading"
    assert completed.wait(timeout=1)
    assert requested_names == [ollama_model_name]


def test_download_transport_failure_becomes_terminal_error_status(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    failed = threading.Event()

    def failing_urlopen(_request, timeout):
        assert timeout == 3600
        failed.set()
        raise OSError("offline")

    monkeypatch.setattr(model_manager_module.urllib.request, "urlopen", failing_urlopen)
    monkeypatch.setattr(manager, "_is_installed", lambda *_args, **_kwargs: False)

    manager.download_model("mbem-v1-local")
    assert failed.wait(timeout=1)

    deadline = time.monotonic() + 1
    status = manager.get_all_status()["mbem-v1-local"]
    while status["status"] != "error" and time.monotonic() < deadline:
        time.sleep(0.01)
        status = manager.get_all_status()["mbem-v1-local"]

    assert status["status"] == "error"
    assert status["download_progress"] == 0
    assert "下载失败" in status["error"]
