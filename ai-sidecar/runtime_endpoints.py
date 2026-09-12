"""Resolve MemoryBread-owned loopback services by logical name.

Packaged processes receive explicit environment values from the Tauri
supervisor. The registry fallback keeps independently launched helpers and
development tools on the same contract. Python 3.9 compatible.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse


_DEFAULT_URLS = {
    "core": "http://127.0.0.1:7070",
    "model_api": "http://127.0.0.1:7071",
    "creation": "http://127.0.0.1:8001",
    "vector_search": "http://127.0.0.1:7072",
    "ollama": "http://127.0.0.1:11434",
}

_URL_ENV = {
    "core": ("CORE_ENGINE_URL", "MEMORY_BREAD_CORE_URL"),
    "model_api": ("MEMORY_BREAD_MODEL_API_URL", "MODEL_API_URL"),
    "creation": ("CREATION_SIDECAR_URL",),
    "vector_search": ("MEMORY_BREAD_VECTOR_SEARCH_URL",),
    "ollama": ("MEMORY_BREAD_OLLAMA_URL", "OLLAMA_HOST"),
}

_BIND_ENV = {
    "core": "MEMORY_BREAD_CORE_BIND",
    "model_api": "MEMORY_BREAD_MODEL_API_BIND",
    "creation": "MEMORY_BREAD_CREATION_BIND",
    "vector_search": "MEMORY_BREAD_VECTOR_SEARCH_BIND",
}


def _safe_loopback_url(raw: str) -> Optional[str]:
    value = raw.strip().rstrip("/")
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    if port is None or not (1 <= port <= 65535):
        return None
    return "http://127.0.0.1:{}".format(port)


def _registry_services() -> Dict[str, dict]:
    raw_path = os.environ.get("MEMORY_BREAD_ENDPOINT_REGISTRY", "").strip()
    if not raw_path:
        return {}
    try:
        payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if payload.get("schema_version") != "local-services.v1":
        return {}
    services = payload.get("services")
    return services if isinstance(services, dict) else {}


def service_base_url(name: str) -> str:
    if name not in _DEFAULT_URLS:
        raise KeyError("unknown local service: {}".format(name))
    for variable in _URL_ENV.get(name, ()):
        candidate = _safe_loopback_url(os.environ.get(variable, ""))
        if candidate:
            return candidate
    entry = _registry_services().get(name)
    if isinstance(entry, dict):
        candidate = _safe_loopback_url(
            "{}://{}:{}".format(
                entry.get("scheme", ""),
                entry.get("host", ""),
                entry.get("port", ""),
            )
        )
        if candidate:
            return candidate
    return _DEFAULT_URLS[name]


def service_bind(name: str) -> Tuple[str, int]:
    variable = _BIND_ENV.get(name)
    if variable:
        candidate = _safe_loopback_url(os.environ.get(variable, ""))
        if candidate:
            parsed = urlparse(candidate)
            return "127.0.0.1", int(parsed.port)
    parsed = urlparse(service_base_url(name))
    return "127.0.0.1", int(parsed.port)


def service_port(name: str) -> int:
    return service_bind(name)[1]
