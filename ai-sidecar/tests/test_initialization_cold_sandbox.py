from __future__ import annotations

import hashlib
import json
import os
import tarfile
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from initialization_manager import InitializationManager, SANDBOX_COLD_INSTALL_STAGES


FAKE_OLLAMA = r"""
#!/usr/bin/env python3
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

host, port = os.environ["OLLAMA_HOST"].rsplit(":", 1)
models_root = Path(os.environ["OLLAMA_MODELS"])
models_root.mkdir(parents=True, exist_ok=True)
state_path = models_root / "fake-models.json"

def read_models():
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return []

def write_models(models):
    state_path.write_text(json.dumps(models), encoding="utf-8")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def send_json(self, payload, status=200, content_type="application/json"):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self.send_json({"models": [{"name": name, "model": name} for name in read_models()]})
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/pull":
            models = read_models()
            name = str(payload["name"])
            if name not in models:
                models.append(name)
                write_models(models)
            self.send_json(
                {"status": "success", "total": 1, "completed": 1},
                content_type="application/x-ndjson",
            )
            return
        if self.path == "/api/generate":
            self.send_json({"response": "正常"})
            return
        if self.path in {"/api/embed", "/api/embeddings"}:
            self.send_json({"embeddings": [[0.1, 0.2, 0.3]]})
            return
        self.send_json({"error": "not found"}, 404)

ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
"""


FAKE_CORE = r"""
#!/usr/bin/env python3
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

home = Path(os.environ["HOME"])
db_path = home / ".memory-bread" / "memory-bread.db"
db_path.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(db_path) as conn:
    conn.executescript(
        '''
        CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            app_name TEXT,
            event_type TEXT,
            ax_text TEXT,
            is_sensitive INTEGER NOT NULL DEFAULT 0,
            pii_scrubbed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS timelines (id INTEGER PRIMARY KEY AUTOINCREMENT);
        CREATE TABLE IF NOT EXISTS creation_skills (id INTEGER PRIMARY KEY AUTOINCREMENT);
        '''
    )

host, port = os.environ["MEMORY_BREAD_CORE_BIND"].rsplit(":", 1)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_json({"status": "ok"})
            return
        if parsed.path == "/api/captures":
            raw_ids = parse_qs(parsed.query).get("ids", [])
            ids = [int(value) for raw in raw_ids for value in raw.split(",") if value]
            with sqlite3.connect(db_path) as conn:
                rows = []
                for capture_id in ids:
                    row = conn.execute("SELECT id FROM captures WHERE id = ?", (capture_id,)).fetchone()
                    if row:
                        rows.append({"id": row[0]})
            self.send_json({"captures": rows})
            return
        self.send_json({"error": "not found"}, 404)

ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
"""


def _write_executable(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def _fake_runtime_archive(fixtures: Path) -> tuple[Path, str]:
    executable = fixtures / "ollama"
    _write_executable(executable, FAKE_OLLAMA)
    archive = fixtures / "ollama-darwin.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(executable, arcname="ollama")
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def _wait_for_terminal(manager: InitializationManager, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    state = manager.get_status()
    while state["state"] not in {"completed", "failed"} and time.monotonic() < deadline:
        time.sleep(0.05)
        state = manager.get_status()
    return state


@pytest.mark.initialization_cold
def test_cold_sandbox_reinstalls_every_component_without_touching_normal_environment(
    monkeypatch,
    tmp_path,
):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    archive, checksum = _fake_runtime_archive(fixtures)
    fake_core = fixtures / "memory-bread-core"
    _write_executable(fake_core, FAKE_CORE)

    base_dir = tmp_path / "normal-home"
    manager = InitializationManager(base_dir=base_dir)
    monkeypatch.setenv("MEMORY_BREAD_OLLAMA_DOWNLOAD_URL", archive.as_uri())
    monkeypatch.setenv("MEMORY_BREAD_OLLAMA_SHA256", checksum)
    monkeypatch.setenv("MEMORY_BREAD_CORE_EXECUTABLE", str(fake_core))
    monkeypatch.setenv("MEMORY_BREAD_INITIALIZATION_ALLOW_UNSUPPORTED", "1")
    monkeypatch.setattr(
        manager,
        "_environment_snapshot",
        lambda: {
            "os": "darwin",
            "os_version": "test",
            "architecture": "arm64",
            "memory_gb": 16.0,
            "disk_free_gb": 100.0,
        },
    )
    def install_fake_capture_model(mode):
        manager._http_json(
            manager._ollama_base_url(mode) + "/api/pull",
            json.dumps({"name": "qwen3.5:4b"}).encode("utf-8"),
        )

    def download_fake_embedding(target):
        target.mkdir(parents=True, exist_ok=True)
        (target / "verified.marker").write_text("verified", encoding="utf-8")
        return []

    monkeypatch.setattr(manager, "_install_pinned_capture_model", install_fake_capture_model)
    monkeypatch.setattr(
        manager,
        "_embedding_ready",
        lambda mode: (manager._embedding_model_dir(mode) / "verified.marker").is_file(),
    )
    monkeypatch.setattr(manager, "_probe_local_embedding", lambda _mode: None)
    monkeypatch.setattr(
        "embedding.model_sources.download_embedding_model",
        download_fake_embedding,
    )

    normal_runtime = base_dir / "initialization" / "runtime" / "ollama" / "normal.sentinel"
    normal_models = base_dir / "initialization" / "models" / "normal.sentinel"
    normal_database = base_dir / "memory-bread.db"
    normal_runtime.parent.mkdir(parents=True, exist_ok=True)
    normal_models.parent.mkdir(parents=True, exist_ok=True)
    normal_runtime.write_text("keep-runtime", encoding="utf-8")
    normal_models.write_text("keep-models", encoding="utf-8")
    normal_database.write_bytes(b"keep-database")

    for _cycle in range(2):
        enabled = manager.enable_test_mode("ENABLE_INITIALIZATION_TEST_MODE")
        assert enabled["mode"] == "sandbox"
        assert enabled["sandbox_isolation"] == {
            "enforced": True,
            "cold_start": True,
            "normal_runtime_hidden": True,
            "normal_models_hidden": True,
            "normal_database_hidden": True,
        }
        config = json.loads(manager.mode_path.read_text(encoding="utf-8"))
        assert config["ollama_port"] != 11434
        assert config["core_port"] != 7070

        manager.start("sandbox")
        finished = _wait_for_terminal(manager)
        assert finished["state"] == "completed", finished

        stages = {stage["id"]: stage for stage in finished["stages"]}
        for stage_id in SANDBOX_COLD_INSTALL_STAGES:
            assert stages[stage_id]["status"] == "succeeded", stages[stage_id]

        sandbox_models = json.loads(
            (manager._models_root("sandbox") / "fake-models.json").read_text(encoding="utf-8")
        )
        assert sandbox_models == ["qwen3.5:4b"]
        assert manager._managed_ollama_executable("sandbox") is not None
        assert manager._database_path("sandbox").is_file()
        assert (manager.sandbox_root / "skills-tools.json").is_file()
        assert manager._sandbox_process_owned("ollama")
        assert manager._sandbox_process_owned("core")

        marker_pids = [
            json.loads(path.read_text(encoding="utf-8"))["pid"]
            for path in (manager.sandbox_root / "processes").glob("*.json")
        ]
        manager.disable_test_mode("DISABLE_INITIALIZATION_TEST_MODE")
        assert not manager.sandbox_root.exists()
        assert all(not psutil.pid_exists(pid) for pid in marker_pids)

        assert normal_runtime.read_text(encoding="utf-8") == "keep-runtime"
        assert normal_models.read_text(encoding="utf-8") == "keep-models"
        assert normal_database.read_bytes() == b"keep-database"
