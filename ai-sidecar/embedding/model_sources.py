"""
Embedding 模型源解析 — 离线优先 + 境内镜像级联

目标：
1. 运行时零境外网络依赖：本地已有模型文件时以离线模式加载，
   绝不触发 huggingface.co 的在线校验（国内网络下会导致启动长时间卡顿）。
2. 首次下载走境内镜像：ModelScope（境内直连）-> hf-mirror 代理 -> huggingface 官方，
   逐个尝试，任一成功即止。
3. 下载产物存放在应用自有模型目录，并按 HuggingFace 权重文件组织，
   SentenceTransformer 可直接以本地目录加载。

环境变量：
- MEMORYBREAD_EMBEDDING_MODEL_DIR: 覆盖模型存放根目录
- MEMORYBREAD_EMBEDDING_SOURCES: 逗号分隔的下载源顺序，
  可选值 modelscope / hfmirror / huggingface（默认全部，按此顺序）
"""

from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

MODEL_REPO_ID = "BAAI/bge-small-zh-v1.5"
_MODEL_DIR_NAME = "bge-small-zh-v1.5"

# bge-small-zh-v1.5 的完整权重文件清单（不含 README，缺省不影响加载）。
# 任一关键文件缺失即视为本地模型不完整。
MODEL_FILES = (
    "modules.json",
    "config_sentence_transformers.json",
    "config.json",
    "model.safetensors",
    "sentence_bert_config.json",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "vocab.txt",
    "1_Pooling/config.json",
)
_REQUIRED_FILES = ("modules.json", "config.json", "model.safetensors", "tokenizer.json")

# 下载源级联：境内源优先。
DEFAULT_SOURCES = ("modelscope", "hfmirror", "huggingface")

_CONNECT_TIMEOUT = 10
_DOWNLOAD_TIMEOUT = 600
_DOWNLOAD_CHUNK = 1024 * 1024

_MODELSCOPE_BASE = "https://modelscope.cn/models/{repo}/resolve/master/{file}"
_HFMIRROR_BASE = "https://hf-mirror.com/{repo}/resolve/main/{file}"
_HUGGINGFACE_BASE = "https://huggingface.co/{repo}/resolve/main/{file}"

_SOURCE_URL_TEMPLATES = {
    "modelscope": _MODELSCOPE_BASE,
    "hfmirror": _HFMIRROR_BASE,
    "huggingface": _HUGGINGFACE_BASE,
}


def _models_root() -> Path:
    override = os.environ.get("MEMORYBREAD_EMBEDDING_MODEL_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".memory-bread" / "models"


def app_model_dir() -> Path:
    """应用自有模型目录（下载产物的最终落盘位置）。"""
    return _models_root() / _MODEL_DIR_NAME


def _is_complete(model_dir: Path) -> bool:
    if not model_dir.is_dir():
        return False
    return all((model_dir / name).is_file() for name in _REQUIRED_FILES)


def _find_hf_cache_snapshot() -> Optional[Path]:
    """在 huggingface 缓存中查找完整的模型快照目录。

    使用 huggingface_hub 的缓存扫描以正确兼容 HF_HOME / HF_HUB_CACHE
    覆盖（打包环境将 HOME 重定向到应用运行时目录）。
    """
    try:
        from huggingface_hub import scan_cache_dir
    except ImportError:
        return None
    try:
        scan = scan_cache_dir()
    except Exception as exc:  # noqa: BLE001 - 缓存扫描失败不应阻断启动
        logger.warning("扫描 huggingface 缓存失败: %s", exc)
        return None
    for repo in getattr(scan, "repos", []) or []:
        if repo.repo_id != MODEL_REPO_ID:
            continue
        for revision in getattr(repo, "revisions", []) or []:
            snapshot = getattr(revision, "snapshot_path", None)
            if snapshot and _is_complete(Path(snapshot)):
                return Path(snapshot)
    return None


def resolve_embedding_model_source() -> str:
    """解析可用于 SentenceTransformer 加载的本地模型路径。

    顺序：应用自有模型目录 -> huggingface 缓存快照 -> 镜像级联下载。
    返回值一定是已存在的本地目录；全部来源失败时抛出 RuntimeError。
    """
    local_dir = app_model_dir()
    if _is_complete(local_dir):
        logger.info("Embedding 模型命中应用本地目录: %s", local_dir)
        return str(local_dir)

    snapshot = _find_hf_cache_snapshot()
    if snapshot is not None:
        logger.info("Embedding 模型命中 huggingface 缓存: %s", snapshot)
        return str(snapshot)

    logger.info("Embedding 模型本地缺失，开始按镜像级联下载: %s", MODEL_REPO_ID)
    errors = download_embedding_model(local_dir)
    if _is_complete(local_dir):
        return str(local_dir)
    if errors:
        raise RuntimeError(
            "Embedding 模型下载失败，请检查网络后重试。详细原因: " + "; ".join(errors)
        )
    raise RuntimeError("Embedding 模型下载失败，请检查网络后重试")


# ── 下载实现 ─────────────────────────────────────────────────────────────────

def _configured_sources() -> List[str]:
    raw = os.environ.get("MEMORYBREAD_EMBEDDING_SOURCES", "").strip().lower()
    if not raw:
        return list(DEFAULT_SOURCES)
    sources = [s.strip() for s in raw.split(",") if s.strip() in _SOURCE_URL_TEMPLATES]
    return sources or list(DEFAULT_SOURCES)


def _url_for(source: str, rel_path: str) -> str:
    template = _SOURCE_URL_TEMPLATES[source]
    # ModelScope 与 HuggingFace 路径分隔符均为 /，直接透传相对路径。
    return template.format(repo=MODEL_REPO_ID, file=rel_path)


def _download_file(url: str, target: Path) -> None:
    """下载单个文件。

    优先用 requests（实测境内 CDN 下明显快于 urllib，且默认携带
    UA 跟随重定向）；不可用时回退到标准库实现。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".part")
    try:
        import requests
    except ImportError:
        requests = None
    if requests is not None:
        with requests.get(
            url,
            timeout=(_CONNECT_TIMEOUT, _DOWNLOAD_TIMEOUT),
            headers={"User-Agent": "MemoryBread/1.0"},
            stream=True,
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            with open(temp, "wb") as output:
                for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                    if chunk:
                        output.write(chunk)
        os.replace(temp, target)
        return
    request = urllib.request.Request(url, headers={"User-Agent": "MemoryBread/1.0"})
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as response:
        with open(temp, "wb") as output:
            while True:
                chunk = response.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                output.write(chunk)
    os.replace(temp, target)


def download_embedding_model(target_dir: Path) -> List[str]:
    """按配置的镜像级联下载模型到 target_dir，返回每个源的失败原因。

    单文件下载失败会回滚该文件，随后切换下一个镜像源重试全部文件；
    已成功落盘的文件不会重复下载。
    """
    errors: List[str] = []
    for source in _configured_sources():
        try:
            logger.info("尝试从 %s 下载 Embedding 模型...", source)
            pending = [
                name for name in MODEL_FILES
                if not (target_dir / name).is_file()
            ]
            for rel_path in pending:
                _download_file(_url_for(source, rel_path), target_dir / rel_path)
            if _is_complete(target_dir):
                logger.info("Embedding 模型下载完成，来源: %s", source)
                return errors
            raise RuntimeError("下载完成但关键文件缺失")
        except Exception as exc:  # noqa: BLE001 - 单源失败需继续尝试下一个镜像
            message = "{}: {}".format(source, exc)
            errors.append(message)
            logger.warning("Embedding 模型下载失败，切换下一个镜像: %s", message)
    return errors


def build_local_files_only_kwargs() -> dict:
    """构造 SentenceTransformer 的离线加载参数。

    当前固定的 sentence-transformers 版本支持 local_files_only；
    若加载时抛出不识别参数错误，由调用方去掉该参数重试。
    """
    return {"local_files_only": True}
