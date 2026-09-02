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
