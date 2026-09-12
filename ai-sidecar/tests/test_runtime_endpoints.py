import json

import pytest

from runtime_endpoints import service_base_url, service_bind, service_port


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("core", "http://127.0.0.1:7070"),
        ("model_api", "http://127.0.0.1:7071"),
        ("creation", "http://127.0.0.1:8001"),
        ("vector_search", "http://127.0.0.1:7072"),
        ("ollama", "http://127.0.0.1:11434"),
    ],
)
def test_legacy_defaults(monkeypatch, name, expected):
    for variable in (
        "CORE_ENGINE_URL",
        "MEMORY_BREAD_CORE_URL",
        "MEMORY_BREAD_MODEL_API_URL",
        "MODEL_API_URL",
        "CREATION_SIDECAR_URL",
        "MEMORY_BREAD_VECTOR_SEARCH_URL",
        "MEMORY_BREAD_OLLAMA_URL",
        "OLLAMA_HOST",
        "MEMORY_BREAD_ENDPOINT_REGISTRY",
    ):
        monkeypatch.delenv(variable, raising=False)
    assert service_base_url(name) == expected


def test_explicit_environment_wins_and_is_normalized(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "localhost:49180")
    assert service_base_url("ollama") == "http://127.0.0.1:49180"
    assert service_port("ollama") == 49180


def test_registry_resolves_all_services(monkeypatch, tmp_path):
    path = tmp_path / "local-services.v1.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "local-services.v1",
                "services": {
                    "core": {
                        "scheme": "http",
                        "host": "127.0.0.1",
                        "port": 49200,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("CORE_ENGINE_URL", raising=False)
    monkeypatch.delenv("MEMORY_BREAD_CORE_URL", raising=False)
    monkeypatch.setenv("MEMORY_BREAD_ENDPOINT_REGISTRY", str(path))
    assert service_base_url("core") == "http://127.0.0.1:49200"
    assert service_bind("core") == ("127.0.0.1", 49200)


def test_non_loopback_environment_is_rejected(monkeypatch):
    monkeypatch.setenv("CORE_ENGINE_URL", "http://0.0.0.0:49200")
    monkeypatch.delenv("MEMORY_BREAD_ENDPOINT_REGISTRY", raising=False)
    assert service_base_url("core") == "http://127.0.0.1:7070"
