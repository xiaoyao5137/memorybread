"""Read-only local CLI discovery. Never run inference, install tools, or return credentials."""

from __future__ import annotations
import json
import os
import re
import subprocess
from pathlib import Path
from user_models import (
    DEFAULT_CLIENT_ARGS,
    list_profiles,
    get_analysis_model,
    resolve_client_command,
)

CLIENT_COMMANDS = {
    "codex": ["codex"],
    "claude-code": ["claude"],
    "kimi": ["kimi"],
    "minimax": ["mcode", "minimax", "mmax", "mm"],
    "glm": ["glm", "zcode"],
}


def read_settings(path):
    try:
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            return {}
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            data = json.loads(text)
        elif path.suffix in (".yaml", ".yml"):
            import yaml
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:
                return {}
        else:
            try:
                import tomllib
            except ImportError:
                try:
                    import tomli as tomllib
                except ImportError:
                    # Python 3.9 installations can still read root string settings.
                    data = {}
                    for line in text.splitlines():
                        if line.lstrip().startswith("["):
                            break
                        match = re.fullmatch(
                            r'\s*(model|default_model|model_reasoning_effort|profile)\s*=\s*("(?:[^"\\]|\\.)*")\s*(?:#.*)?',
                            line,
                        )
                        if match:
                            data[match[1]] = json.loads(match[2])
                    return data
            data = tomllib.loads(text)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, ImportError):
        return {}


def config_paths(client):
    home = Path.home()
    roots = {
        "codex": [
            Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "config.toml"
        ],
        "claude-code": [
            Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude")))
            / "settings.json"
        ],
        "kimi": [
            Path(os.environ.get("KIMI_CODE_HOME", str(home / ".kimi-code")))
            / "config.toml",
            home / ".kimi/config.toml",
            home / ".kimi/config.json",
        ],
        "minimax": [
            home / ".minimax/config.yaml",
            home / ".minimax/config.json",
            home / ".minimax/config.toml",
            home / ".minimax-code/config.json",
        ],
        "glm": [
            home / ".glm/config.json",
            home / ".glm/config.toml",
            home / ".zcode/config.json",
        ],
    }
    return roots[client]


def _model(settings):
    value = (
        settings.get("defaultModel")
        or settings.get("default_model")
        or settings.get("model")
        or settings.get("model_id")
    )
    if isinstance(value, dict):
        value = value.get("id") or value.get("model_id")
    return (
        value
        if isinstance(value, str) and len(value) <= 160 and "\n" not in value
        else ""
    )


def detect_client(client):
    if client not in CLIENT_COMMANDS:
        raise ValueError("不支持自动探测的客户端类型")
    profiles = [
        p
        for p in list_profiles()
        if p.get("protocol") == "cli" and p.get("client") == client
    ]
    selected = get_analysis_model()
    profiles.sort(key=lambda p: p["id"] != selected)
    saved = profiles[0] if profiles else {}
    command = (
        resolve_client_command(saved.get("command", ""))
        if saved.get("command")
        else None
    )
    for name in CLIENT_COMMANDS[client]:
        if command:
            break
        command = resolve_client_command(name)
    if not command and client == "codex":
        command = resolve_client_command(
            "/Applications/Codex.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
        )
    args = list(DEFAULT_CLIENT_ARGS.get(client, []))
    model = ""
    source = "default"
    for path in config_paths(client):
        settings = read_settings(path)
        if client == "codex":
            profile = settings.get("profile")
            if isinstance(profile, str) and re.fullmatch(r"[\w-]+", profile):
                profiles_config = settings.get("profiles", {})
                legacy = (
                    profiles_config.get(profile, {})
                    if isinstance(profiles_config, dict)
                    else {}
                )
                settings = {
                    **settings,
                    **(legacy if isinstance(legacy, dict) else {}),
                    **read_settings(path.parent / (profile + ".config.toml")),
                }
        if not model and _model(settings):
            model = _model(settings)
            source = "local_config"
            effort = settings.get("model_reasoning_effort")
            if client == "codex" and effort in (
                "none",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
                "ultra",
            ):
                args = [
                    *args[:-1],
                    "-c",
                    "model_reasoning_effort=" + json.dumps(effort),
                    args[-1],
                ]
    if saved:
        model = saved.get("model") or model
        args = saved.get("args") or args
        source = "saved_profile"
    # Unknown client distributions only get flags confirmed by their help output.
    if command and not args:
        try:
            is_mcode = client == "minimax" and Path(command).name == "mcode"
            result = subprocess.run(
                [command, *(["exec"] if is_mcode else []), "--help"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=3,
                text=True,
                cwd=str(Path.home()),
            )
            help_text = result.stdout[:65536]
            if is_mcode and all(flag in help_text for flag in (
                "--input", "--output-format", "--permission", "--max-steps",
            )):
                args = ["exec", "--input", "-", "--output-format", "text",
                        "--permission", "off", "--max-steps", "1"]
            elif "--print" in help_text:
                args = ["--print"]
                if "--output-format" in help_text:
                    args += ["--output-format", "text"]
            elif "--prompt" in help_text:
                args = ["--prompt", "{prompt}"]
            if args and model and "--model" in help_text:
                args += ["--model", "{model}"]
        except (OSError, subprocess.TimeoutExpired):
            pass
    fields = [
        name
        for name, value in [("command", command), ("args", args), ("model", model)]
        if value
    ]
    if not command:
        message = "未找到已安装的客户端，可手动填写可执行文件路径。"
        status = "not_found"
    else:
        missing = []
        if not args:
            missing.append("调用参数需手动填写")
        if not model:
            missing.append("未找到显式模型 ID，留空使用客户端默认模型")
        message = "已探测到本地客户端，配置可自行修改。" + (
            "；".join(missing) if missing else ""
        )
        status = "found" if not missing else "partial"
    return {
        "client": client,
        "status": status,
        "command": command or "",
        "args": args,
        "model": model,
        "source": source,
        "detected_fields": fields,
        "message": message,
    }
