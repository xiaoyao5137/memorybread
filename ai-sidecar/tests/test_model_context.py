import json

import httpx

import model_context
import user_models


def test_codex_context_uses_effective_model_and_workspace_override(tmp_path, monkeypatch):
    monkeypatch.setattr(user_models, "codex_home", lambda: tmp_path)
    (tmp_path / "config.toml").write_text('model = "default-model"\n')
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "default-model", "context_window": 128000, "effective_context_window_percent": 95},
        {"slug": "alternate", "context_window": 64000, "max_context_window": 96000},
    ]}))
    profile = {"protocol": "cli", "client": "codex", "model": ""}
    window = model_context.consultation_context_window
    assert window(profile) == 121600
    assert window({**profile, "model": "alternate"}) == 64000
    assert window({**profile, "model": "unknown"}) is None
    assert window({
        **profile, "args": ["exec", "--model=alternate", "-c", "model_context_window=100000"],
    }) == 96000
    assert window({**profile, "context_window_tokens": 32000}) == 32000


def test_compatible_api_reads_exact_model_limit_from_list_without_inference(monkeypatch):
    clients = httpx.Client
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/team/model"):
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [
            {"id": "unrelated", "context_length": 2000000},
            {"id": "team/model", "context_length": 131072, "max_model_len": 32768},
        ]})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: clients(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    profile = {"protocol": "openai", "base_url": "https://metadata.example.test/v1",
               "model": "team/model", "api_key": "test-secret"}
    assert model_context.consultation_context_window(profile) == 32768
    assert model_context.consultation_context_window(profile) == 32768
    assert [request.method for request in requests] == ["GET", "GET"]
    assert requests[0].url.raw_path == b"/v1/models/team%2Fmodel"
    assert requests[1].headers["authorization"] == "Bearer test-secret"


def test_output_limit_alone_is_not_a_context_window():
    assert model_context._metadata_window({"max_tokens": 32768}) is None
