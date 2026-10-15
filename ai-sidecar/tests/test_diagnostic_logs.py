from pathlib import Path

from diagnostic_logs import MAX_LOG_BYTES, list_diagnostic_logs, read_diagnostic_log


def test_diagnostic_logs_use_fixed_whitelist_and_bounded_tail(tmp_path: Path) -> None:
    core_log = tmp_path / "core.log"
    core_log.write_text("prefix-secret\n" + "x" * MAX_LOG_BYTES, encoding="utf-8")
    (tmp_path / "not-allowed.log").write_text("private", encoding="utf-8")

    items = list_diagnostic_logs(tmp_path)
    keys = {str(item["key"]) for item in items}
    assert "core" in keys
    assert "not-allowed" not in keys

    content = read_diagnostic_log("core", tmp_path)
    assert content is not None
    assert content["truncated"] is True
    assert content["returned_bytes"] == MAX_LOG_BYTES
    assert "prefix-secret" not in str(content["content"])


def test_diagnostic_logs_reject_unknown_or_missing_files(tmp_path: Path) -> None:
    assert read_diagnostic_log("../../private", tmp_path) is None
    assert read_diagnostic_log("core", tmp_path) is None


def test_model_api_exposes_fallback_log_routes(monkeypatch) -> None:
    import model_api_server

    monkeypatch.setattr(
        model_api_server,
        "list_diagnostic_logs",
        lambda: [{"key": "core", "exists": True, "size_bytes": 7}],
    )
    monkeypatch.setattr(
        model_api_server,
        "read_diagnostic_log",
        lambda key: {
            "key": key,
            "content": "started",
            "truncated": False,
            "total_size_bytes": 7,
            "returned_bytes": 7,
        }
        if key == "core"
        else None,
    )

    client = model_api_server.app.test_client()
    listing = client.get("/api/debug/log-files")
    content = client.get("/api/debug/log-files/core")
    missing = client.get("/api/debug/log-files/not-allowed")

    assert listing.status_code == 200
    assert listing.get_json()["items"][0]["key"] == "core"
    assert content.status_code == 200
    assert content.get_json()["content"] == "started"
    assert missing.status_code == 404


def test_inference_logs_include_separate_directory_and_rotated_tails(tmp_path):
    app_dir = tmp_path / '.memory-bread'
    log_dir = app_dir / 'logs'
    engine_dir = app_dir / 'initialization' / 'logs'
    log_dir.mkdir(parents=True)
    engine_dir.mkdir(parents=True)
    expected = {
        'inference_engine': (engine_dir, 'local-ai-engine.log'),
        'inference_engine_previous_1': (engine_dir, 'local-ai-engine.log.1'),
        'inference_engine_previous_2': (engine_dir, 'local-ai-engine.log.2'),
        'inference_launcher': (log_dir, 'ollama.log'),
    }
    for key, (directory, name) in expected.items():
        (directory / name).write_text('old-prefix\n' + 'x' * MAX_LOG_BYTES)
    items = {item['key']: item for item in list_diagnostic_logs(log_dir)}
    for key in expected:
        assert items[key]['exists'] is True
        content = read_diagnostic_log(key, log_dir)
        assert content['returned_bytes'] == MAX_LOG_BYTES
        assert content['truncated'] is True
        assert 'old-prefix' not in content['content']


def test_engine_log_symlink_cannot_read_private_file(tmp_path):
    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    engine_dir = tmp_path / 'initialization' / 'logs'
    engine_dir.mkdir(parents=True)
    private = tmp_path / 'private.txt'
    private.write_text('private')
    (engine_dir / 'local-ai-engine.log').symlink_to(private)
    assert read_diagnostic_log('inference_engine', log_dir) is None
    assert next(item for item in list_diagnostic_logs(log_dir)
                if item['key'] == 'inference_engine')['exists'] is False


def test_model_api_reads_engine_logs_when_core_is_unavailable(tmp_path, monkeypatch):
    import diagnostic_logs
    import model_api_server
    log_dir = tmp_path / 'logs'
    engine_dir = tmp_path / 'initialization' / 'logs'
    engine_dir.mkdir(parents=True)
    (engine_dir / 'local-ai-engine.log').write_text('engine failed to bind\n')
    monkeypatch.setattr(diagnostic_logs, 'diagnostic_log_dir', lambda: log_dir)
    client = model_api_server.app.test_client()
    items = client.get('/api/debug/log-files').get_json()['items']
    assert next(item for item in items if item['key'] == 'inference_engine')['exists']
    response = client.get('/api/debug/log-files/inference_engine')
    assert response.status_code == 200
    assert response.get_json()['content'] == 'engine failed to bind\n'
