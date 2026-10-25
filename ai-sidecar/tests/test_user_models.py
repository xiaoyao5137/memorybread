import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import user_models as models


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "profile_path", lambda: tmp_path / "profiles.json")


def profile(**kwargs):
    return models.save_profile(
        {
            "name": "My model",
            "model": "version-new",
            "protocol": "openai",
            "base_url": "https://example.com/v1",
            "api_key": "test-secret",
            **kwargs,
        }
    )


def test_credentials_are_private_and_survive_edit():
    p = profile()
    assert p["has_api_key"] and "api_key" not in p
    assert "test-secret" not in json.dumps(models.list_profiles())
    assert os.stat(models.profile_path()).st_mode & 0o777 == 0o600
    profile(id=p["id"], name="Renamed", api_key="")
    assert models.get_profile(p["id"])["api_key"] == "test-secret"
    assert models.list_profiles()[0]["name"] == "Renamed"
    models.delete_profile(p["id"])
    with pytest.raises(ValueError):
        models.get_profile(p["id"])


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol": "shell"},
        {"base_url": "http://example.com/v1"},
        {"base_url": "https://secret@example.com/v1"},
        {"base_url": "https://example.com?key=secret"},
        {"name": ""},
        {"id": "../../etc/passwd"},
        {"protocol": "cli", "command": "tool", "client": "custom", "args": "--shell"},
    ],
)
def test_rejects_invalid_profiles(changes):
    with pytest.raises(ValueError):
        profile(**changes)


def test_real_cli_stdin_args_no_shell_and_failures(tmp_path):
    script = tmp_path / "client.py"
    script.write_text('import sys; print("answer:"+sys.stdin.read()+"|"+sys.argv[1])')
    p = profile(
        protocol="cli",
        command=sys.executable,
        client="glm",
        args=[str(script), "$(touch SHOULD_NOT_EXIST)"],
    )
    result = asyncio.run(
        models.cli_complete(
            models.get_profile(p["id"]), [{"role": "user", "content": "hello"}]
        )
    )
    assert result == "answer:user: hello|$(touch SHOULD_NOT_EXIST)"
    bad = profile(
        protocol="cli",
        command=sys.executable,
        client="custom",
        args=["-c", "raise SystemExit(7)"],
    )
    with pytest.raises(ValueError, match="7"):
        asyncio.run(models.cli_complete(models.get_profile(bad["id"]), []))


def test_cli_timeout_reaps_process(tmp_path):
    pid = tmp_path / "pid"
    code = f'import os,time; open({str(pid)!r},"w").write(str(os.getpid())); time.sleep(30)'
    p = profile(
        protocol="cli", command=sys.executable, client="minimax", args=["-c", code]
    )
    with pytest.raises(TimeoutError):
        asyncio.run(models.cli_complete(models.get_profile(p["id"]), [], timeout=0.3))
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


def test_codex_extracts_only_final_messages(tmp_path):
    events = [
        {"type": "item.completed", "item": {"type": "reasoning", "text": "private"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}},
    ]
    p = profile(
        protocol="cli",
        command=sys.executable,
        client="codex",
        args=["-c", f"print({chr(10).join(json.dumps(e) for e in events)!r})"],
    )
    # A blank model uses the client's configured default.
    raw = models.get_profile(p["id"])
    raw["model"] = ""
    assert asyncio.run(models.cli_complete(raw, [])) == "final"


def test_codex_reconnection_can_recover_without_leaking_commentary():
    events = [
        [], {"type": "item.completed", "item": None},
        {"type": "error", "message": "Reconnecting... (workspace routing discovery failed)"},
        {"type": "item.completed", "item": {"type": "agent_message", "phase": "commentary", "text": "thinking"}},
        {"type": "item.completed", "item": {"type": "agent_message", "phase": "final_answer", "text": "OK"}},
        {"type": "turn.completed"},
    ]
    output = "\n".join(json.dumps(e) for e in events)
    assert models._codex_output(output, 0) == "OK"


@pytest.mark.parametrize("message,code", [
    ("workspace routing discovery failed", "CODEX_NETWORK_FAILED"),
    ("401 unauthorized", "CODEX_AUTH_FAILED"),
    ("usage limit reached", "CODEX_RATE_LIMITED"),
    ("context window exceeded", "CODEX_CONTEXT_LIMIT"),
    ("too many tokens", "CODEX_CONTEXT_LIMIT"),
    ("unknown failure", "CODEX_INFERENCE_FAILED"),
])
def test_codex_terminal_failure_is_actionable_and_private(message, code):
    output = json.dumps({"type": "turn.failed", "error": {"message": message + " PRIVATE_CAPTURE_TEXT"}})
    with pytest.raises(ValueError, match=code) as exc:
        models._codex_output(output, 1)
    assert "PRIVATE_CAPTURE_TEXT" not in str(exc.value)


def test_codex_empty_output_and_terminal_failure_cannot_become_success():
    with pytest.raises(ValueError, match="CODEX_EMPTY_RESPONSE"):
        models._codex_output('{"type":"turn.completed"}', 0)
    output = '\n'.join([
        '{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}',
        '{"type":"turn.failed"}',
    ])
    with pytest.raises(ValueError, match="CODEX_INFERENCE_FAILED"):
        models._codex_output(output, 0)


def test_codex_startup_diagnostics_are_bounded_drained_and_redacted(tmp_path):
    script = tmp_path / "broken-cli.py"
    script.write_text('import sys; sys.stderr.write("Error loading config.toml: PRIVATE_CAPTURE_TEXT" + "x" * 200000); sys.exit(1)')
    p = profile(protocol="cli", client="codex", command=sys.executable, model="", args=[str(script)])
    with pytest.raises(ValueError, match="CODEX_CONFIG_INVALID") as exc:
        asyncio.run(models.cli_complete(models.get_profile(p["id"]), [], timeout=5))
    assert "PRIVATE_CAPTURE_TEXT" not in str(exc.value)


def test_codex_inference_preserves_provider_and_effort_but_disables_personal_tools(monkeypatch, tmp_path):
    import client_detection
    config = tmp_path / "config.toml"
    config.write_text('model_provider="private"\n[mcp_servers.node_repl]\ncommand="node"\n')
    monkeypatch.setattr(client_detection, "config_paths", lambda client: [config])
    args = models._codex_inference_args(["exec", "--json", "-c", 'model_reasoning_effort="low"', "-"])
    assert "--ignore-user-config" not in args
    assert 'model_reasoning_effort="low"' in args
    assert "mcp_servers.node_repl.enabled=false" in args
    assert "features.apps=false" in args and "features.plugins=false" in args
    assert "--ephemeral" in args


def test_real_http_openai_anthropic_and_creation_rag_dispatch():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, payload, dict(self.headers)))
            body = (
                {
                    "content": [
                        {"type": "thinking", "thinking": "private"},
                        {"type": "text", "text": "anthropic-ok"},
                    ]
                }
                if self.path.endswith("/messages")
                else {"choices": [{"message": {"content": "openai-ok"}}]}
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        p = profile(base_url=base + "/v1")
        from creation.service import CreationService

        service = CreationService()

        async def generate():
            return "".join(
                [
                    c
                    async for c in service._generate_cloud(
                        "system", "prompt", p["id"], "device-profile", ""
                    )
                ]
            )

        assert asyncio.run(generate()) == "openai-ok"
        from rag.llm.cloud import CloudChatBackend

        backend = CloudChatBackend(p["id"], "device-profile")
        chunks = []
        assert (
            backend.complete_stream("question", on_delta=chunks.append).text
            == "openai-ok"
        )
        assert chunks == ["openai-ok"]
        models.set_analysis_model(p["id"])
        from knowledge.extractor_v2 import KnowledgeExtractorV2

        extractor = KnowledgeExtractorV2(model="unused-local-runtime")
        assert (
            extractor._ollama_chat([{"role": "user", "content": "capture"}])["message"][
                "content"
            ]
            == "openai-ok"
        )
        a = profile(protocol="anthropic", base_url=base + "/v1")

        async def chat():
            return "".join(
                [
                    c
                    async for c in service._chat_cloud(
                        [
                            {"role": "system", "content": "system"},
                            {"role": "user", "content": "hi"},
                        ],
                        a["id"],
                        "device-profile",
                        "",
                    )
                ]
            )

        assert asyncio.run(chat()) == "anthropic-ok"
        assert calls[0][0] == "/v1/chat/completions"
        assert calls[0][1]["model"] == "version-new"
        assert calls[0][2]["Authorization"] == "Bearer test-secret"
        assert calls[-1][0] == "/v1/messages" and calls[-1][1]["system"] == "system"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_profile_routes_redact_and_handle_errors():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from creation.user_model_routes import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert (
            client.post("/creation/user-models", json={"name": ""}).status_code == 400
        )
        p = client.post(
            "/creation/user-models",
            json={
                "name": "local",
                "protocol": "cli",
                "command": "kimi",
                "client": "kimi",
                "api_key": "secret",
            },
        ).json()
        assert "secret" not in json.dumps(p)
        assert client.get("/creation/user-models").json()["models"][0]["id"] == p["id"]
        assert (
            client.put("/creation/user-models/analysis-selection", json={}).status_code
            == 400
        )
        assert (
            client.put(
                "/creation/user-models/analysis-selection",
                json={"model_id": "user-missing"},
            ).status_code
            == 400
        )
        assert (
            client.put(
                "/creation/user-models/analysis-selection", json={"model_id": p["id"]}
            ).json()["model_id"]
            == p["id"]
        )
        assert (
            client.get("/creation/user-models").json()["analysis_model_id"] == p["id"]
        )
        assert client.delete("/creation/user-models/" + p["id"]).status_code == 200
        assert client.get("/creation/user-models").json()["analysis_model_id"] is None
        assert client.delete("/creation/user-models/" + p["id"]).status_code == 404


@pytest.mark.parametrize("client", ["claude-code", "kimi"])
def test_native_client_default_contract(tmp_path, client):
    command = tmp_path / "fake-client"
    command.write_text(f"""#!{sys.executable}
import sys
from pathlib import Path
args=sys.argv[1:]
if {client!r}=='kimi':
    assert '--prompt' in args
    assert 'user: question'==args[args.index('--prompt')+1]
    agent=Path(args[args.index('--agent-file')+1]).read_text()
    assert 'tools: []' in agent and 'subagents: []' in agent
    print('• reply')
else:
    assert args[args.index('--tools')+1]==''
    assert sys.stdin.read()=='user: question'
    print('reply')
""")
    command.chmod(0o700)
    assert (
        asyncio.run(
            models.cli_complete(
                {"command": str(command), "client": client, "model": "", "args": []},
                [{"role": "user", "content": "question"}],
            )
        )
        == "reply"
    )


def test_sync_rag_profile_inside_event_loop(monkeypatch):
    async def chat(*args):
        yield "ok"

    monkeypatch.setattr(models, "chat_profile", chat)

    async def run():
        return models.complete_profile("user-any", "prompt")

    assert asyncio.run(run()) == "ok"


def test_cli_timeout_cleans_children_after_parent_exits(tmp_path):
    pid = tmp_path / "child-pid"
    child = f'import os,time; open({str(pid)!r},"w").write(str(os.getpid())); time.sleep(30)'
    parent = f'import subprocess,sys; subprocess.Popen([sys.executable,"-c",{child!r}])'
    p = profile(
        protocol="cli", command=sys.executable, client="custom", args=["-c", parent]
    )
    with pytest.raises(TimeoutError):
        asyncio.run(models.cli_complete(models.get_profile(p["id"]), [], timeout=0.5))
    import subprocess

    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", pid.read_text()], capture_output=True, text=True
    )
    assert not result.stdout.strip() or result.stdout.strip().startswith("Z")


def test_builtin_brand_model_resolves_to_active_runtime(monkeypatch):
    import httpx
    from creation.service import CreationService

    original_client = httpx.AsyncClient
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, text=json.dumps({"message": {"content": "local-ok"}, "done": True})
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond)),
    )
    service = CreationService()
    service.model = "active-local-runtime"

    async def run():
        return "".join(
            [
                part
                async for part in service._stream_direct_completion(
                    system_prompt="system",
                    user_prompt="prompt",
                    creation_model="mbcd-std-v1",
                    creation_api_key=None,
                    creation_base_url=None,
                    num_predict=128,
                    temperature=0.5,
                )
            ]
        )

    assert asyncio.run(run()) == "local-ok"
    assert calls[0]["model"] == "active-local-runtime"


def test_analysis_selection_survives_reload_and_delete():
    p = profile()
    assert models.get_analysis_model() is None
    assert models.set_analysis_model(p["id"]) == p["id"]
    assert models.get_analysis_model() == p["id"]
    assert (
        models.profile_path().with_name("analysis_model.json").stat().st_mode & 0o777
        == 0o600
    )
    with pytest.raises(ValueError):
        models.set_analysis_model("user-missing")
    assert models.get_analysis_model() == p["id"]
    models.delete_profile(p["id"])
    assert models.get_analysis_model() is None


def test_extractor_uses_selected_profile_without_ollama_and_switches_live(monkeypatch):
    from knowledge.extractor_v2 import KnowledgeExtractorV2
    from inference_queue import QueueEvictedError

    p = profile()
    models.set_analysis_model(p["id"])
    monkeypatch.setattr(
        KnowledgeExtractorV2,
        "_ollama_session",
        lambda self: pytest.fail("must not connect Ollama"),
    )
    extractor = KnowledgeExtractorV2(model="qwen3.5:4b")
    calls = []

    async def chat(model, messages, **kwargs):
        calls.append((model, messages, kwargs))
        yield '{"data_facts":[]}'

    monkeypatch.setattr(models, "chat_profile", chat)
    response = extractor._ollama_chat(
        [{"role": "user", "content": "captured evidence"}],
        format="json",
        options={"num_predict": 4096},
    )
    assert response["message"]["content"] == '{"data_facts":[]}' and response["done"]
    assert calls[0][0] == p["id"] and calls[0][2]["max_tokens"] == 4096
    assert "JSON" in calls[0][1][0]["content"]
    assert calls[0][1][1]["content"] == "captured evidence"
    second = profile(name="Second")
    models.set_analysis_model(second["id"])
    extractor._ollama_chat([{"role": "user", "content": "next"}])
    assert calls[-1][0] == second["id"]

    def preempted():
        raise QueueEvictedError("preempted")

    monkeypatch.setattr("inference_queue.raise_if_preempted", preempted)
    with pytest.raises(QueueEvictedError):
        extractor._ollama_chat([])
    assert len(calls) == 2


@pytest.mark.parametrize("fails", [False, True])
def test_bake_usage_tracks_selected_profile_even_if_selection_changes(monkeypatch, fails):
    from knowledge.extractor_v2 import KnowledgeExtractorV2
    from monitor import llm_tracker

    selected = profile()
    next_profile = profile(name="Next model")
    models.set_analysis_model(selected["id"])
    extractor = object.__new__(KnowledgeExtractorV2)
    extractor.model = "qwen3.5:4b"
    records = []
    calls = []
    monkeypatch.setattr(llm_tracker, "log_llm_usage", lambda **kw: records.append(kw))

    async def chat(model, messages, **kwargs):
        calls.append(model)
        models.set_analysis_model(next_profile["id"])
        if fails:
            raise RuntimeError("client unavailable")
        yield '{"summary":"done"}'

    monkeypatch.setattr(models, "chat_profile", chat)
    if fails:
        with pytest.raises(RuntimeError, match="client unavailable"):
            extractor._call_bake_llm("bundle:42", "system", "evidence")
    else:
        extractor._call_bake_llm("bundle:42", "system", "evidence")
    assert calls == [selected["id"]]
    assert len(records) == 1
    assert records[0]["model_name"] == selected["id"]
    assert records[0]["status"] == ("failed" if fails else "success")
    assert extractor.model == "qwen3.5:4b"


def test_connection_draft_does_not_save_or_modify_existing_profiles(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from creation.user_model_routes import router

    p = profile()
    models.set_analysis_model(p["id"])
    original = models.profile_path().read_text()
    calls = []

    async def infer(config, messages, **kwargs):
        calls.append(config)
        yield "OK"

    monkeypatch.setattr(models, "chat_configuration", infer)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        data = {**models.get_profile(p["id"]), "name": "Unsaved edit", "api_key": ""}
        result = client.post("/creation/user-models/test-connection", json=data)
        assert result.status_code == 200 and result.json()["message"] == "连接成功"
        assert calls[0]["api_key"] == "test-secret"
        assert models.profile_path().read_text() == original
        assert models.get_analysis_model() == p["id"]
        assert (
            client.post(
                "/creation/user-models/test-connection", json={"name": ""}
            ).status_code
            == 400
        )


def test_relocated_bundled_codex_recovers_but_arbitrary_paths_do_not(monkeypatch):
    old = '/Applications/Codex.app' + models.CODEX_BUNDLE_SUFFIX
    current = '/Applications/ChatGPT.app' + models.CODEX_BUNDLE_SUFFIX
    monkeypatch.setattr(models.shutil, 'which', lambda command: current if command == 'codex' else None)
    assert models.resolve_client_command(old) == current
    assert models.resolve_client_command('/missing/custom/codex') is None


def test_relocated_bundle_discovery_without_path(monkeypatch, tmp_path):
    old = str(tmp_path / 'old-codex')
    current = tmp_path / 'new-codex'
    current.write_text('#!/bin/sh\n')
    current.chmod(0o700)
    monkeypatch.setattr(models, 'CODEX_BUNDLE_COMMANDS', (old, str(current)))
    monkeypatch.setattr(models.shutil, 'which', lambda command: None)
    assert models.resolve_client_command(old) == str(current)


def test_private_usage_retains_only_typed_error_code(monkeypatch):
    from monitor import llm_tracker
    records = []
    monkeypatch.setattr(llm_tracker, 'log_llm_usage', lambda **kw: records.append(kw))
    for error, expected in [
        (models.ModelInvocationError('PRIVATE_BODY', 'CLIENT_NOT_FOUND'), 'CLIENT_NOT_FOUND'),
        (models.ModelInvocationError('PRIVATE_BODY', 'CODEX_AUTH_FAILED'), 'CODEX_AUTH_FAILED'),
        (models.ModelInvocationError('PRIVATE_BODY', 'PRIVATE_BODY'), 'INFERENCE_FAILED'),
        (ValueError('CLIENT_NOT_FOUND PRIVATE_BODY'), 'INFERENCE_FAILED'),
    ]:
        with pytest.raises(ValueError):
            with llm_tracker.LLMCallTracker('knowledge', 'user-test', capture_content=False):
                raise error
        assert records[-1]['error_msg'] == expected
    assert 'PRIVATE_BODY' not in json.dumps(records)


@pytest.mark.parametrize('provider', ['aliyun', 'minimax', 'zhipu', 'kimi', 'deepseek'])
def test_gateway_defaults_and_private_credentials(provider):
    from model_gateways import PROVIDER_MAP
    p = profile(protocol='gateway', provider=provider, model='', base_url='https://untrusted.example/v1')
    assert p['base_url'] == PROVIDER_MAP[provider]['base_url']
    assert p['model'] == PROVIDER_MAP[provider]['model']
    assert 'api_key' not in p
    profile(id=p['id'], protocol='gateway', provider=provider, api_key='')
    assert models.get_profile(p['id'])['api_key'] == 'test-secret'


def test_gateway_workspace_validation_and_key_isolation():
    p = profile(protocol='gateway', provider='aliyun', workspace_id='llm-example')
    assert p['base_url'] == 'https://llm-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
    for changes in ({'provider': 'kimi'}, {'region': 'ap-southeast-1'}, {'workspace_id': 'other'}):
        with pytest.raises(ValueError, match='API Key'):
            models.prepare_profile({**models.get_profile(p['id']), **changes, 'api_key': ''})
    for changes in ({'provider': 'unknown'}, {'region': 'bad'}, {'workspace_id': 'evil.example/path'}):
        with pytest.raises(ValueError):
            models.prepare_profile({**models.get_profile(p['id']), **changes})


def test_gateway_inference_resolves_official_host(monkeypatch):
    import httpx
    calls = []
    async def post(self, url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"facts":["测试"]}'}, 'finish_reason': 'stop'}]})
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    p = profile(protocol='gateway', provider='aliyun', model='qwen-plus')
    models.set_analysis_model(p['id'])
    result = models.complete_profile(models.get_analysis_model(), '提炼测试材料')
    assert json.loads(result)['facts'] == ['测试']
    assert calls[0][0] == 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
    assert calls[0][1]['json']['enable_thinking'] is False
    assert calls[0][1]['headers']['Authorization'] == 'Bearer test-secret'


def test_gateway_selected_for_actual_extractor(monkeypatch):
    import httpx
    from knowledge.extractor_v2 import KnowledgeExtractorV2
    p = profile(protocol="gateway", provider="aliyun", model="qwen-plus")
    models.set_analysis_model(p["id"])
    async def post(self, url, **kwargs):
        assert url.startswith("https://dashscope.aliyuncs.com/")
        assert kwargs["json"]["model"] == "qwen-plus"
        assert "JSON" in kwargs["json"]["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"overview":"完成网关验证"}'}, "finish_reason":"stop"}]})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    extractor = KnowledgeExtractorV2(model="unused-local-model")
    result = extractor._ollama_chat([{"role":"user", "content":"合成采集材料"}], format="json")
    assert result["model"] == p["id"]
    assert json.loads(result["message"]["content"])["overview"] == "完成网关验证"
