from pathlib import Path

from flask import Flask

from local_auth import TOKEN_HEADER, browser_request_authorized, install_flask_guard


def test_internal_request_is_allowed_without_browser_origin(monkeypatch, tmp_path: Path):
    token_file = tmp_path / "token"
    token_file.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("MEMORY_BREAD_LOCAL_AUTH_TOKEN_FILE", str(token_file))
    assert browser_request_authorized(None, None)


def test_browser_request_requires_matching_instance_token(monkeypatch, tmp_path: Path):
    token_file = tmp_path / "token"
    token_file.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("MEMORY_BREAD_LOCAL_AUTH_TOKEN_FILE", str(token_file))
    assert not browser_request_authorized("tauri://localhost", None)
    assert not browser_request_authorized("tauri://localhost", "old-secret")
    assert browser_request_authorized("tauri://localhost", "secret")


def test_flask_guard_rejects_missing_and_accepts_current_token(monkeypatch, tmp_path: Path):
    token_file = tmp_path / "token"
    token_file.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("MEMORY_BREAD_LOCAL_AUTH_TOKEN_FILE", str(token_file))
    app = Flask(__name__)
    install_flask_guard(app)
    app.get("/health")(lambda: {"status": "ok"})
    client = app.test_client()
    assert client.get("/health", headers={"Origin": "tauri://localhost"}).status_code == 401
    response = client.get(
        "/health",
        headers={"Origin": "tauri://localhost", TOKEN_HEADER: "secret"},
    )
    assert response.status_code == 200
