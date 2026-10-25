"""Device-owned model profiles. Remote templates never carry executable commands or secrets."""

from __future__ import annotations
import asyncio
import json
import os
import signal
import shutil
import tempfile
import threading
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlparse

PREFIX = "user-"
_LOCK = threading.RLock()


def profile_path():
    return Path.home() / ".memory-bread" / "user_models.json"


def _read():
    path = profile_path()
    return json.loads(path.read_text()) if path.exists() else {}


def list_profiles():
    with _LOCK:
        return [
            {k: v for k, v in p.items() if k != "api_key"}
            | {"has_api_key": bool(p.get("api_key"))}
            for p in _read().values()
        ]


def get_profile(model):
    with _LOCK:
        profile = _read().get(model)
    if profile is None:
        raise ValueError("自定义模型不存在，请在模型设置中重新配置")
    return profile


def _write(profiles):
    path = profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".user-models-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(profiles, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def prepare_profile(data):
    with _LOCK:
        profiles = _read()
        model_id = data.get("id") or PREFIX + uuid.uuid4().hex
        if (
            not isinstance(model_id, str)
            or not model_id.startswith(PREFIX)
            or len(model_id) > 100
            or not all(c.isalnum() or c == "-" for c in model_id)
        ):
            raise ValueError("无效的模型 ID")
        protocol = data.get("protocol", "openai")
        if protocol not in ("openai", "anthropic", "cli", "gateway"):
            raise ValueError("不支持的调用协议")
        profile = {
            k: data.get(k, "")
            for k in ("name", "model", "base_url", "command", "client")
        }
        if any(not isinstance(v, str) or len(v) > 1000 for v in profile.values()):
            raise ValueError("配置字段必须是文本，且不能超过 1000 字符")
        if not profile["name"].strip():
            raise ValueError("请填写模型名称")
        if protocol == "gateway":
            from model_gateways import PROVIDER_MAP, resolve_gateway
            provider = data.get("provider", "")
            region = data.get("region", "cn-beijing")
            workspace = data.get("workspace_id", "")
            profile["base_url"] = resolve_gateway(provider, region, workspace)
            profile.update(provider=provider, region=region, workspace_id=workspace)
            profile["model"] = profile["model"].strip() or PROVIDER_MAP[provider]["model"]
        if protocol == "cli":
            if not profile["command"].strip():
                raise ValueError("请填写客户端可执行文件路径或命令名")
            args = data.get("args", [])
            if (
                not isinstance(args, list)
                or len(args) > 40
                or any(not isinstance(a, str) or len(a) > 2000 for a in args)
            ):
                raise ValueError("客户端参数必须是字符串数组，最多 40 项")
            profile["args"] = args
            if profile["client"] not in (
                "codex",
                "claude-code",
                "kimi",
                "minimax",
                "glm",
                "custom",
            ):
                raise ValueError("不支持的客户端类型")
        else:
            url = urlparse(profile["base_url"])
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("请填写不含凭据的 HTTP(S) API 地址")
            if url.scheme == "http" and url.hostname not in (
                "localhost",
                "127.0.0.1",
                "::1",
            ):
                raise ValueError("远程 API 必须使用 HTTPS")
            if not profile["model"].strip():
                raise ValueError("请填写实际模型 ID")
        key = data.get("api_key")
        if key is None or key == "":
            previous = profiles.get(model_id, {})
            same_destination = all(previous.get(k, "") == profile.get(k, "") for k in ("base_url", "provider")) and previous.get("protocol") == protocol
            key = previous.get("api_key", "") if same_destination else ""
        if not isinstance(key, str) or len(key) > 8000:
            raise ValueError("无效的 API Key")
        key = key.strip()
        if protocol == "gateway" and not key:
            raise ValueError("请填写该平台的 API Key；切换平台或地域后需要重新填写")
        profile.update(id=model_id, protocol=protocol, api_key=key)
        return profile


def save_profile(data):
    with _LOCK:
        profile = prepare_profile(data)
        profiles = _read()
        profiles[profile["id"]] = profile
        _write(profiles)
    return next(p for p in list_profiles() if p["id"] == profile["id"])


def delete_profile(model_id):
    with _LOCK:
        profiles = _read()
        if model_id not in profiles:
            raise ValueError("模型不存在")
        if get_analysis_model() == model_id:
            set_analysis_model(None)
        del profiles[model_id]
        _write(profiles)


DEFAULT_CLIENT_ARGS = {
    "codex": [
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "--ephemeral",
        "-",
    ],
    "claude-code": ["--print", "--output-format", "text", "--tools", ""],
    "kimi": ["--prompt", "{prompt}", "--output-format", "text"],
}


def _codex_inference_args(args):
    """Keep provider/auth settings, but isolate text inference from personal tools."""
    if not args or args[0] != "exec":
        return args
    from client_detection import config_paths, read_settings

    extra = [] if "--ephemeral" in args else ["--ephemeral"]
    for feature in ("apps", "plugins", "shell_tool", "unified_exec", "skill_search"):
        extra += ["-c", f"features.{feature}=false"]
    extra += ["-c", 'web_search="disabled"']
    for path in config_paths("codex"):
        settings = read_settings(path)
        for name in settings.get("mcp_servers", {}):
            if name and name.isascii() and all(c.isalnum() or c in "_-" for c in name):
                extra += ["-c", f"mcp_servers.{name}.enabled=false"]
    return [args[0], *extra, *args[1:]]


def _codex_output(output, returncode, stderr=""):
    parts, errors = [], []
    failed = False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            item = {}
        if (event.get("type") == "item.completed"
                and item.get("type") == "agent_message"
                and item.get("phase") != "commentary"
                and isinstance(item.get("text"), str)):
            parts.append(item["text"])
        if event.get("type") in ("error", "turn.failed"):
            errors.append(json.dumps(event, ensure_ascii=False).lower())
        failed |= event.get("type") == "turn.failed"
    # Reconnection events can precede a successful turn; only terminal failures
    # or a nonzero process exit invalidate its final answer.
    if returncode or failed:
        details = " ".join(errors) + " " + stderr.lower()
        if any(s in details for s in ("error loading config", "invalid transport", "unexpected argument", "invalid value")):
            raise ModelInvocationError("Codex 配置或启动参数无效，请检查客户端配置（CODEX_CONFIG_INVALID）", "CODEX_CONFIG_INVALID")
        if any(s in details for s in ("workspace routing", "connection", "stream disconnected", "network", "timed out")):
            raise ModelInvocationError("Codex 网络连接失败，请检查网络或代理设置（CODEX_NETWORK_FAILED）", "CODEX_NETWORK_FAILED")
        if any(s in details for s in ("unauthorized", "401", "token expired", "invalid token", "refresh token", "login", "authentication")):
            raise ModelInvocationError("Codex 登录已失效，请重新登录客户端（CODEX_AUTH_FAILED）", "CODEX_AUTH_FAILED")
        if any(s in details for s in ("rate limit", "quota", "usage limit", "429")):
            raise ModelInvocationError("Codex 当前额度不足或请求受限，请稍后重试（CODEX_RATE_LIMITED）", "CODEX_RATE_LIMITED")
        if any(s in details for s in ("context length", "context window", "too many tokens")):
            raise ModelInvocationError("Codex 输入超过模型上下文限制，请缩小提炼批次（CODEX_CONTEXT_LIMIT）", "CODEX_CONTEXT_LIMIT")
        if any(s in details for s in ("model_not_found", "model is not supported", "unsupported model")):
            raise ModelInvocationError("Codex 模型不可用，请检查模型 ID 和账号权限（CODEX_MODEL_UNAVAILABLE）", "CODEX_MODEL_UNAVAILABLE")
        raise ModelInvocationError(f"Codex 推理失败（退出码 {returncode}，CODEX_INFERENCE_FAILED）", "CODEX_INFERENCE_FAILED")
    result = "\n".join(parts).strip()
    if not result:
        raise ModelInvocationError("Codex 没有返回最终推理内容（CODEX_EMPTY_RESPONSE）", "CODEX_EMPTY_RESPONSE")
    return result


# App renames/updates can relocate the bundled CLI. Only recover known bundle
# paths; never reinterpret an arbitrary missing executable as a different client.
CODEX_BUNDLE_SUFFIX = "/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
CODEX_BUNDLE_COMMANDS = tuple(
    root + app + CODEX_BUNDLE_SUFFIX
    for root in ("/Applications/", str(Path.home() / "Applications") + "/")
    for app in ("ChatGPT.app", "Codex.app")
)


class ModelInvocationError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def resolve_client_command(command):
    supplied = os.path.expanduser(command)
    resolved = shutil.which(supplied)
    if not resolved and supplied in CODEX_BUNDLE_COMMANDS:
        resolved = shutil.which("codex")
        if not resolved:
            resolved = next((path for path in CODEX_BUNDLE_COMMANDS
                             if Path(path).is_file() and os.access(path, os.X_OK)), None)
    if not resolved and "/" not in command:
        for directory in [
            Path.home() / ".local/bin",
            Path.home() / ".minimax-code/bin",
            Path.home() / ".minimax-code/node_modules/.bin",
            Path("/opt/homebrew/bin"),
            Path("/usr/local/bin"),
            Path.home() / ".npm-global/bin",
        ]:
            candidate = directory / command
            if candidate.is_file() and os.access(candidate, os.X_OK):
                resolved = str(candidate)
                break
    if not resolved and "/" not in command:
        node_root = Path.home() / ".nvm/versions/node"
        for directory in sorted(node_root.glob("*/bin"), reverse=True):
            candidate = directory / command
            if candidate.is_file() and os.access(candidate, os.X_OK):
                resolved = str(candidate)
                break
    return resolved


async def cli_complete(profile, messages, timeout=300):
    """No shell, isolated working directory, bounded output; kill process group on cancellation."""
    command = resolve_client_command(profile["command"])
    if not command:
        raise ModelInvocationError("找不到客户端，请确认已安装并填写正确的可执行文件路径", "CLIENT_NOT_FOUND")
    client = profile["client"]
    args = profile.get("args", [])
    default_args = not args or args == DEFAULT_CLIENT_ARGS.get(client)
    if not args:
        args = list(DEFAULT_CLIENT_ARGS.get(client, []))
    if client == "codex":
        args = _codex_inference_args(args)
    has_model = any(a in ("--model", "-m") or a.startswith("--model=") for a in args)
    if profile.get("model") and not has_model and client in ("codex", "claude-code", "kimi"):
        args = [*args, "--model", profile["model"]]
    prompt = "\n\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages
    )
    args = [
        arg.replace("{prompt}", prompt).replace("{model}", profile.get("model", ""))
        for arg in args
    ]
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    if client == "codex":
        for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TOOL_CALL_ID",
                     "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_APP_TOOLS_PIPE_PATH",
                     "CODEX_PERMISSION_PROFILE"):
            env.pop(name, None)
    from cli_network import cli_network_environment
    async with AsyncExitStack() as stack:
        cwd = stack.enter_context(tempfile.TemporaryDirectory(prefix="memorybread-inference-"))
        env = await stack.enter_async_context(
            cli_network_environment(env, enabled=client == "codex")
        )
        if client == "kimi" and default_args:
            agent = Path(cwd) / "inference.md"
            agent.write_text(
                "---\nname: memorybread-inference\ndescription: Text inference only\ntools: []\nsubagents: []\n---\nAnswer the supplied request. Return only the requested answer.\n"
            )
            args += ["--agent-file", str(agent)]
        proc = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )

        async def exchange():
            stderr = bytearray()

            async def read_stderr():
                while chunk := await proc.stderr.read(65536):
                    # Drain all diagnostics to avoid blocking the CLI, but retain
                    # only a bounded buffer for private error classification.
                    stderr.extend(chunk[:max(0, 65536 - len(stderr))])

            async def write_input():
                try:
                    proc.stdin.write(prompt.encode())
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()

            writer = asyncio.create_task(write_input())
            diagnostics = asyncio.create_task(read_stderr())
            result = bytearray()
            try:
                while chunk := await proc.stdout.read(65536):
                    result.extend(chunk)
                    if len(result) > 4 * 1024 * 1024:
                        raise ValueError("客户端输出超过 4 MB 限制")
                await writer
                await proc.wait()
                await diagnostics
            finally:
                writer.cancel()
                diagnostics.cancel()
                await asyncio.gather(writer, diagnostics, return_exceptions=True)
            output = result.decode("utf-8", errors="replace").strip()
            if client == "codex":
                return _codex_output(output, proc.returncode, stderr.decode("utf-8", errors="replace"))
            if proc.returncode:
                raise ValueError(
                    f"客户端调用失败（退出码 {proc.returncode}），请检查客户端登录状态与参数"
                )
            if client == "kimi" and default_args:
                output = "\n".join(
                    line[2:] if line.startswith("• ") else line
                    for line in output.splitlines()
                )
            if not output:
                raise ValueError("客户端没有返回推理内容")
            return output

        try:
            return await asyncio.wait_for(exchange(), timeout)
        finally:
            # The leader can exit while descendants still hold stdout open.
            # Always reap the process group, including that timeout case.
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                elif proc.returncode is None:
                    proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


async def chat_profile(model, messages, *, max_tokens=8192):
    async for part in chat_configuration(
        get_profile(model), messages, max_tokens=max_tokens
    ):
        yield part


async def chat_configuration(p, messages, *, max_tokens=8192):
    if p["protocol"] == "cli":
        yield await cli_complete(p, messages)
        return
    import httpx

    if p["protocol"] == "gateway":
        from model_gateways import resolve_gateway
        p = {**p, "base_url": resolve_gateway(p["provider"], p.get("region", "cn-beijing"), p.get("workspace_id", ""))}
    if p["protocol"] == "anthropic":
        system = "\n".join(str(m["content"]) for m in messages if m["role"] == "system")
        payload = {
            "model": p["model"],
            "max_tokens": max_tokens,
            "system": system,
            "messages": [m for m in messages if m["role"] != "system"],
        }
        url = p["base_url"].rstrip("/")
        url += "/messages" if url.endswith("/v1") else "/v1/messages"
        headers = {"x-api-key": p["api_key"], "anthropic-version": "2023-06-01"}
    else:
        payload = {"model": p["model"], "messages": messages, "max_tokens": max_tokens}
        if p.get("provider") == "aliyun" and p["model"].startswith("qwen"):
            payload["enable_thinking"] = False
        url = p["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": "Bearer " + p["api_key"]} if p["api_key"] else {}
    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(url, headers=headers, json=payload)
        if not response.is_success:
            hint = {
                401: "密钥无效或与地域不匹配，请重新填写该平台 API Key",
                403: "账号没有模型访问权限，请在平台开通模型服务",
                404: "模型 ID 或接入地址不可用，请检查平台支持的模型",
                429: "请求受限或额度不足，请检查平台余额、配额并稍后重试",
            }.get(response.status_code, "请检查密钥、模型 ID 和平台服务状态")
            raise ValueError(f"模型 API 调用失败（HTTP {response.status_code}）：{hint}")
        data = response.json()
    if p["protocol"] == "anthropic":
        if data.get("stop_reason") == "max_tokens":
            raise ValueError("模型输出达到长度上限，生成未完成，请提高输出预算后重试")
        text = "".join(
            v.get("text", "")
            for v in data.get("content", [])
            if v.get("type") == "text"
        )
    else:
        if data["choices"][0].get("finish_reason") == "length":
            raise ValueError("模型输出达到长度上限，生成未完成，请提高输出预算后重试")
        text = data["choices"][0]["message"].get("content", "")
    if not text:
        raise ValueError("模型未返回文本内容")
    yield text


def complete_profile(model, prompt, system=""):
    """Bridge synchronous RAG backends, including callers already on an event loop."""

    async def collect():
        return "".join(
            [
                part
                async for part in chat_profile(
                    model,
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                )
            ]
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(collect())
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(collect())).result()


def get_analysis_model():
    """Read on each extraction so switches apply across sidecar processes."""
    with _LOCK:
        path = profile_path().with_name("analysis_model.json")
        selected = (
            json.loads(path.read_text()).get("model_id") if path.exists() else None
        )
        return selected if selected in _read() else None


def set_analysis_model(model_id):
    with _LOCK:
        if model_id is not None:
            get_profile(model_id)
        path = profile_path().with_name("analysis_model.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".analysis-model-")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"model_id": model_id}, handle)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return model_id
