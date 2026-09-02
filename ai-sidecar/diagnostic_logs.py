"""Read bounded tails from the packaged runtime's diagnostic log whitelist.

This module deliberately exposes no arbitrary path input. It exists so the desktop
client can still collect core.log when Core Engine itself is unavailable.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple


MAX_LOG_BYTES = 128 * 1024
LOG_SPECS: Tuple[Tuple[str, str, str], ...] = (
    ("core", "core.log · Core Engine", "core.log"),
    ("sidecar", "sidecar.log · AI Sidecar", "sidecar.log"),
    ("model_api", "model_api.log · Model API", "model_api.log"),
    ("creation", "creation.log · Creation Service", "creation.log"),
    ("bake_extract_errors", "bake_extract_errors.log · Bake 提炼错误", "bake_extract_errors.log"),
    ("ui", "ui.log · Desktop UI", "ui.log"),
)


def diagnostic_log_dir() -> Path:
    return Path.home() / ".memory-bread" / "logs"


def _spec_for_key(key: str) -> Optional[Tuple[str, str, str]]:
    return next((spec for spec in LOG_SPECS if spec[0] == key), None)


def _modified_at_ms(path: Path) -> Optional[int]:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return None


def list_diagnostic_logs(log_dir: Optional[Path] = None) -> List[Dict[str, object]]:
    allowed_dir = log_dir or diagnostic_log_dir()
    items: List[Dict[str, object]] = []
    for key, label, file_name in LOG_SPECS:
        path = allowed_dir / file_name
        try:
            stat = path.stat()
            exists = path.is_file()
            size_bytes = stat.st_size if exists else 0
        except OSError:
            exists = False
            size_bytes = 0
        items.append(
            {
                "key": key,
                "label": label,
                "exists": exists,
                "size_bytes": size_bytes,
                "modified_at": _modified_at_ms(path) if exists else None,
            }
        )
    return items


def read_diagnostic_log(
    key: str,
    log_dir: Optional[Path] = None,
    max_bytes: int = MAX_LOG_BYTES,
) -> Optional[Dict[str, object]]:
    spec = _spec_for_key(key)
    if spec is None:
        return None
    allowed_dir = (log_dir or diagnostic_log_dir()).resolve()
    path = allowed_dir / spec[2]
    try:
        canonical_path = path.resolve(strict=True)
    except OSError:
        return None
    if canonical_path.parent != allowed_dir or not canonical_path.is_file():
        return None
    total_size_bytes = canonical_path.stat().st_size
    bounded_bytes = max(1, min(int(max_bytes), MAX_LOG_BYTES))
    with canonical_path.open("rb") as handle:
        if total_size_bytes > bounded_bytes:
            handle.seek(-bounded_bytes, os.SEEK_END)
        content_bytes = handle.read(bounded_bytes)
    return {
        "key": spec[0],
        "label": spec[1],
        "content": content_bytes.decode("utf-8", errors="replace"),
        "truncated": total_size_bytes > len(content_bytes),
        "total_size_bytes": total_size_bytes,
        "returned_bytes": len(content_bytes),
        "modified_at": _modified_at_ms(canonical_path),
    }
