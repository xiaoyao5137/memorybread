from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

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
    monkeypatch.setattr(manager, "_resolve_brew_command", lambda: None)
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


def test_text_download_uses_the_expected_internal_ollama_model(
    monkeypatch,
    tmp_path,
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

    result = manager.download_model("mbem-v1-local")

    assert result["status"] == "downloading"
    assert completed.wait(timeout=1)
    assert requested_names == ["qwen3.5:4b"]


def test_vector_download_uses_verified_local_artifacts_not_ollama(monkeypatch, tmp_path):
    from embedding import model_sources
    from model_manager import ModelStatus

    manager = _manager(tmp_path)
    completed = threading.Event()
    targets = []
    monkeypatch.setattr(model_sources, "app_model_dir", lambda: tmp_path / "embedding")
    monkeypatch.setattr(
        model_sources,
        "download_embedding_model",
        lambda target: targets.append(target) or completed.set() or [],
    )
    monkeypatch.setattr(manager, "_check_model_status", lambda _info: ModelStatus.INSTALLED)
    monkeypatch.setattr(
        model_manager_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not call Ollama")),
    )

    result = manager.download_model("bge-small-zh")

    assert result["status"] == "downloading"
    assert completed.wait(timeout=1)
    assert targets == [tmp_path / "embedding"]


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


def test_brew_formula_command_uses_reported_custom_prefix(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    formula_prefix = tmp_path / "custom-brew" / "opt" / "ollama"
    ollama_command = formula_prefix / "bin" / "ollama"
    ollama_command.parent.mkdir(parents=True)
    ollama_command.write_text("#!/bin/sh\n", encoding="utf-8")
    ollama_command.chmod(0o755)

    monkeypatch.setattr(
        model_manager_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=str(formula_prefix) + "\n",
            stderr="",
        ),
    )

    resolved = manager._resolve_brew_formula_command("/custom/bin/brew", "ollama")

    assert resolved == str(ollama_command)


def test_resolve_ollama_command_supports_user_applications(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    ollama_command = tmp_path / "Applications" / "Ollama.app" / "Contents" / "Resources" / "ollama"
    ollama_command.parent.mkdir(parents=True)
    ollama_command.write_text("#!/bin/sh\n", encoding="utf-8")
    ollama_command.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path / "packaged-runtime"))
    monkeypatch.setenv("MEMORY_BREAD_USER_HOME", str(tmp_path))
    monkeypatch.setattr(model_manager_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(manager, "_resolve_brew_command", lambda: None)
    monkeypatch.setattr(manager, "_ollama_app_roots", lambda: [tmp_path / "Applications" / "Ollama.app"])

    assert manager._resolve_ollama_command() == str(ollama_command)


def test_ollama_app_roots_include_real_user_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "packaged-runtime"))
    monkeypatch.setenv("MEMORY_BREAD_USER_HOME", str(tmp_path / "real-user"))

    roots = ModelManager._ollama_app_roots()

    assert tmp_path / "real-user" / "Applications" / "Ollama.app" in roots
    assert tmp_path / "packaged-runtime" / "Applications" / "Ollama.app" not in roots


def test_upgrade_starts_ollama_from_homebrew_reported_prefix(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    formula_prefix = tmp_path / "custom-brew" / "opt" / "ollama"
    ollama_command = formula_prefix / "bin" / "ollama"
    ollama_command.parent.mkdir(parents=True)
    ollama_command.write_text("#!/bin/sh\n", encoding="utf-8")
    ollama_command.chmod(0o755)
    run_calls = []
    popen_calls = []

    def fake_run(command, **kwargs):
        run_calls.append(command)
        if command[1:] == ["--prefix", "ollama"]:
            return SimpleNamespace(returncode=0, stdout=str(formula_prefix) + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(model_manager_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        model_manager_module.subprocess,
        "Popen",
        lambda command, **kwargs: popen_calls.append(command),
    )
    monkeypatch.setattr(model_manager_module, "shutdown_all_managed_serves", lambda **kwargs: [])
    monkeypatch.setattr(model_manager_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        manager,
        "get_ollama_setup_status",
        lambda force=False: {"ollama_version": "test"},
    )

    manager._upgrade_ollama_task({"brew_path": "/custom/bin/brew"})

    assert ["/custom/bin/brew", "install", "ollama"] in run_calls
    assert all("unlink" not in command and "link" not in command for command in run_calls)
    assert popen_calls == [[str(ollama_command), "serve"]]
    assert manager.get_upgrade_status()["status"] == "success"
