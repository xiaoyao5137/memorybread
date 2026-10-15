import inspect
import json
import sys
import threading

import main
import packaged_entry


def test_packaged_sidecar_removes_outer_service_argument(monkeypatch):
    observed_argv = None

    async def fake_main():
        nonlocal observed_argv
        observed_argv = list(sys.argv)

    monkeypatch.setattr(main, "_main", fake_main)
    monkeypatch.setattr(sys, "argv", ["memory-bread-ai", "sidecar", "--log-level", "DEBUG"])

    packaged_entry.run_sidecar()

    assert observed_argv == ["memory-bread-ai", "--log-level", "DEBUG"]


def test_runtime_status_heartbeat_is_independent_from_asyncio():
    assert not inspect.iscoroutinefunction(main._runtime_status_heartbeat)


def test_runtime_status_writes_remain_atomic_across_threads(tmp_path, monkeypatch):
    status_file = tmp_path / "state" / "sidecar_runtime_status.json"
    monkeypatch.setattr(main, "_STATE_DIR", status_file.parent)
    monkeypatch.setattr(main, "_RUNTIME_STATUS_FILE", status_file)
    monkeypatch.setattr(main, "_RUNTIME_STATUS_CACHE", None)

    def write_status(index: int) -> None:
        main._write_runtime_status(
            mode=f"test-{index}",
            full_dispatch_ready=True,
            background_processor_running=True,
            critical_checks_passed=True,
            embedding_ok=True,
        )

    threads = [threading.Thread(target=write_status, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert payload["mode"].startswith("test-")
    assert payload["critical_checks_passed"] is True
    assert isinstance(payload["updated_at_ms"], int)
    assert not status_file.with_suffix(".json.tmp").exists()


def test_packaged_model_api_starts_supervisor_before_serving(monkeypatch):
    from types import SimpleNamespace
    events = []
    server = SimpleNamespace(
        initialization_manager=SimpleNamespace(start_runtime_supervisor=lambda: events.append("supervisor")),
        _idle_diary_backfill_worker=SimpleNamespace(start=lambda: None),
        get_rag_pipeline=lambda: None,
        logger=SimpleNamespace(info=lambda *_: None),
        app=SimpleNamespace(run=lambda **_: events.append("serve")),
    )
    monkeypatch.setitem(sys.modules, "model_api_server", server)
    monkeypatch.setattr(packaged_entry.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    packaged_entry.run_model_api()
    assert events == ["supervisor", "serve"]
