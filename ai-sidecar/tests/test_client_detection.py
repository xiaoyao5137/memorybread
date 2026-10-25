import json
from pathlib import Path
import pytest
import client_detection as detection
import user_models


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(user_models, "profile_path", lambda: tmp_path / "profiles.json")
    for env in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "KIMI_CODE_HOME"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(
        detection,
        "resolve_client_command",
        lambda command: "/bin/" + command if command and "/" not in command else None,
    )
    return tmp_path


def test_codex_configuration_and_saved_override_preserve_low_effort(isolated):
    config = isolated / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text(
        'model="gpt-6.1-sol"\nmodel_reasoning_effort="medium"\napi_key="never-return-this"\n'
    )
    result = detection.detect_client("codex")
    assert result["command"] == "/bin/codex" and result["model"] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="medium"' in result["args"]
    assert "never-return-this" not in json.dumps(result)
    profile = user_models.save_profile(
        {
            "name": "Chosen low",
            "protocol": "cli",
            "client": "codex",
            "command": "codex",
            "model": "gpt-6.1-sol",
            "args": ["exec", "--json", "-c", 'model_reasoning_effort="low"', "-"],
        }
    )
    user_models.set_analysis_model(profile["id"])
    result = detection.detect_client("codex")
    assert 'model_reasoning_effort="low"' in result["args"]
    assert result["source"] == "saved_profile"


@pytest.mark.parametrize(
    "client,path,config,model",
    [
        (
            "claude-code",
            ".claude/settings.json",
            {"model": "configured-claude", "env": {"ANTHROPIC_AUTH_TOKEN": "secret"}},
            "configured-claude",
        ),
        (
            "kimi",
            ".kimi-code/config.toml",
            'default_model="kimi-code/my-model"\n',
            "kimi-code/my-model",
        ),
        (
            "minimax",
            ".minimax/config.json",
            {"model": "MiniMax-local"},
            "MiniMax-local",
        ),
        ("glm", ".glm/config.json", {"default_model": "glm-local"}, "glm-local"),
    ],
)
def test_client_configs_are_read_without_returning_keys(
    isolated, monkeypatch, client, path, config, model
):
    file = isolated / path
    file.parent.mkdir(parents=True)
    file.write_text(json.dumps(config) if isinstance(config, dict) else config)

    class Help:
        stdout = "--print --output-format --model"

    monkeypatch.setattr(detection.subprocess, "run", lambda *a, **k: Help())
    result = detection.detect_client(client)
    assert result["model"] == model and result["command"]
    assert result["args"] and "secret" not in json.dumps(result)


def test_missing_binary_and_malformed_config_are_explicit(isolated, monkeypatch):
    monkeypatch.setattr(detection, "resolve_client_command", lambda _: None)
    config = isolated / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text("not valid toml")
    result = detection.detect_client("codex")
    assert (
        result["status"] == "not_found"
        and result["command"] == ""
        and result["model"] == ""
    )
    with pytest.raises(ValueError):
        detection.detect_client("../../run-anything")


def test_detection_route_and_no_writes(isolated):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from creation.user_model_routes import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/creation/user-models/detect/codex").status_code == 200
        assert client.get("/creation/user-models/detect/unknown").status_code == 400
    assert not (isolated / "profiles.json").exists()


def test_minimax_desktop_yaml_default_model(isolated, monkeypatch):
    config = isolated / '.minimax/config.yaml'
    config.parent.mkdir()
    config.write_text('defaultModel: minimax/MiniMax-M3.1-Flash\nprovider:\n  apiKey: never-return-this\n')
    class Help:
        stdout = '--prompt --model'
    monkeypatch.setattr(detection.subprocess, 'run', lambda *a, **k: Help())
    result = detection.detect_client('minimax')
    assert result['command'] == '/bin/mcode'
    assert result['model'] == 'minimax/MiniMax-M3.1-Flash'
    assert 'never-return-this' not in json.dumps(result)


def test_malformed_minimax_yaml_does_not_break_detection(isolated, monkeypatch):
    config = isolated / '.minimax/config.yaml'
    config.parent.mkdir()
    config.write_text('defaultModel: [broken')
    class Help:
        stdout = ''
    monkeypatch.setattr(detection.subprocess, 'run', lambda *a, **k: Help())
    assert detection.detect_client('minimax')['model'] == ''


def test_mcode_headless_flags_are_verified_from_exec_help(isolated, monkeypatch):
    calls = []
    class Help:
        stdout = '--input --output-format --permission --max-steps --model'
    def help_run(command, **kwargs):
        calls.append(command)
        return Help()
    monkeypatch.setattr(detection.subprocess, 'run', help_run)
    result = detection.detect_client('minimax')
    assert calls == [['/bin/mcode', 'exec', '--help']]
    assert result['args'] == ['exec', '--input', '-', '--output-format', 'text',
                              '--permission', 'off', '--max-steps', '1']
