"""Resolve consultation context limits from the configured model's metadata."""

from __future__ import annotations

import json
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import httpx

MIN_CONTEXT_WINDOW = 4096
MAX_CONTEXT_WINDOW = 2_000_000


def context_window_value(value: object) -> int | None:
    if type(value) is int and MIN_CONTEXT_WINDOW <= value <= MAX_CONTEXT_WINDOW:
        return value
    return None


def consultation_context_window(profile: dict) -> int | None:
    manual = context_window_value(profile.get("context_window_tokens"))
    if manual:
        return manual
    protocol = profile["protocol"]
    if protocol == "cli":
        return _codex_window(profile) if profile.get("client") == "codex" else None
    # Metadata is read only from the selected endpoint. Cache successes and
    # misses to avoid probing the same provider for every consultation.
    return _api_window(
        protocol, profile["base_url"], profile["model"].strip(),
        profile.get("api_key", ""), int(time.monotonic()) // 600,
    )


def _codex_window(profile: dict) -> int | None:
    from client_detection import codex_settings, read_settings
    from user_models import DEFAULT_CLIENT_ARGS, codex_home

    home = codex_home()
    overrides: dict = {}
    model_arg = ""
    selected_profile = None
    args = iter(profile.get("args") or DEFAULT_CLIENT_ARGS["codex"])
    for arg in args:
        if arg == "--":
            break
        option, _, value = arg.partition("=")
        if option in {"-m", "--model", "-p", "--profile", "-c", "--config"}:
            value = value or next(args, "")
            if option in {"-m", "--model"}:
                model_arg = value.replace("{model}", profile.get("model", ""))
            elif option in {"-p", "--profile"}:
                selected_profile = value
            else:
                name, _, raw = value.partition("=")
                name = name.strip()
                if name in {"model", "model_context_window", "model_catalog_json", "model_provider"}:
                    try:
                        overrides[name] = json.loads(raw)
                    except ValueError:
                        overrides[name] = raw.strip().strip("'")

    settings = codex_settings(home / "config.toml", selected_profile) | overrides
    configured = context_window_value(settings.get("model_context_window"))
    model = model_arg or profile.get("model") or settings.get("model")
    catalog = settings.get("model_catalog_json")
    if isinstance(catalog, str) and catalog:
        path = home / Path(catalog).expanduser()
    elif settings.get("model_provider", "openai") == "openai":
        path = home / "models_cache.json"
    else:
        return configured

    models = read_settings(path).get("models")
    if not model or not isinstance(models, list):
        return configured
    entry = next((item for item in models if isinstance(item, dict) and item.get("slug") == model), None)
    if entry is None:
        return configured
    window = configured or context_window_value(entry.get("context_window"))
    maximum = context_window_value(entry.get("max_context_window"))
    percent = entry.get("effective_context_window_percent", 100)
    if window is None or type(percent) is not int or not 1 <= percent <= 100:
        return None
    return context_window_value(min(window, maximum or window) * percent // 100)


def _metadata_window(item: dict) -> int | None:
    top_provider = item.get("top_provider")
    fields = (
        item.get("context_window_tokens"), item.get("context_window"),
        item.get("context_length"), item.get("max_model_len"), item.get("max_input_tokens"),
        top_provider.get("context_length") if isinstance(top_provider, dict) else None,
    )
    limits = [window for value in fields if (window := context_window_value(value))]
    return min(limits, default=None)


@lru_cache(maxsize=32)
def _api_window(protocol: str, base_url: str, model: str, api_key: str, _interval: int) -> int | None:
    if not model or not base_url:
        return None
    base_url = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if protocol == "anthropic":
        base_url = base_url if base_url.endswith("/v1") else base_url + "/v1"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    models_url = base_url + "/models"
    with httpx.Client(timeout=2, follow_redirects=False) as client:
        # Some compatible servers implement only the list. Anthropic's single
        # model response can return the resolved ID for a requested alias.
        paths = (models_url,) if model in {".", ".."} else (f"{models_url}/{quote(model, safe='')}", models_url)
        for url in paths:
            try:
                with client.stream("GET", url, headers=headers) as response:
                    if response.status_code in (401, 403):
                        break
                    if not response.is_success:
                        continue
                    body = bytearray()
                    for part in response.iter_bytes():
                        body.extend(part)
                        if len(body) > 4 * 1024 * 1024:
                            break
                    if len(body) > 4 * 1024 * 1024:
                        continue
                    payload = json.loads(body)
            except (httpx.HTTPError, httpx.InvalidURL, ValueError):
                break
            if not isinstance(payload, dict):
                continue
            data = payload.get("data", payload)
            if isinstance(data, list):
                matches = (item for item in data if isinstance(item, dict) and item.get("id") == model)
                return min((window for item in matches if (window := _metadata_window(item))), default=None)
            if isinstance(data, dict) and (
                data.get("id") == model or (protocol == "anthropic" and isinstance(data.get("id"), str))
            ):
                if window := _metadata_window(data):
                    return window
    return None
