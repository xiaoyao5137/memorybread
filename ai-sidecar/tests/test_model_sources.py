"""
model_sources 测试 — 离线优先解析与境内镜像级联下载

覆盖：
- 应用本地目录命中时不触发缓存扫描与下载
- huggingface 缓存快照命中
- 全部缺失时按镜像级联下载（ModelScope 失败 -> 下一个源）
- 下载源顺序可由环境变量覆盖
- 全部失败时抛出带原因的 RuntimeError
"""

from __future__ import annotations

from pathlib import Path

import pytest

from embedding import model_sources


def _make_complete_model_dir(target: Path) -> None:
    for name in model_sources.MODEL_FILES:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stub")


class TestResolveOfflineFirst:
    def test_app_local_dir_wins(self, tmp_path, monkeypatch):
        model_dir = tmp_path / "models" / "bge-small-zh-v1.5"
        _make_complete_model_dir(model_dir)
        monkeypatch.setenv("MEMORYBREAD_EMBEDDING_MODEL_DIR", str(tmp_path / "models"))

        called = {}

        def _fail_scan():
            called["scan"] = True
            raise AssertionError("本地目录命中时不应扫描缓存")

        monkeypatch.setattr(model_sources, "_find_hf_cache_snapshot", _fail_scan)

        resolved = model_sources.resolve_embedding_model_source()
        assert Path(resolved) == model_dir
        assert "scan" not in called

    def test_hf_cache_snapshot_used_when_local_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORYBREAD_EMBEDDING_MODEL_DIR", str(tmp_path / "models"))
        snapshot = tmp_path / "snapshot"
        _make_complete_model_dir(snapshot)
        monkeypatch.setattr(model_sources, "_find_hf_cache_snapshot", lambda: snapshot)

        resolved = model_sources.resolve_embedding_model_source()
        assert Path(resolved) == snapshot

    def test_incomplete_local_dir_falls_through_to_snapshot(self, tmp_path, monkeypatch):
        model_dir = tmp_path / "models" / "bge-small-zh-v1.5"
        (model_dir).mkdir(parents=True)
        (model_dir / "config.json").write_text("stub")  # 关键文件不完整
        monkeypatch.setenv("MEMORYBREAD_EMBEDDING_MODEL_DIR", str(tmp_path / "models"))
        snapshot = tmp_path / "snapshot"
        _make_complete_model_dir(snapshot)
        monkeypatch.setattr(model_sources, "_find_hf_cache_snapshot", lambda: snapshot)

        resolved = model_sources.resolve_embedding_model_source()
        assert Path(resolved) == snapshot


class TestMirrorCascade:
    def test_download_falls_back_to_next_source(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORYBREAD_EMBEDDING_SOURCES", "modelscope,hfmirror")
        attempts = []

        def fake_download_file(url, target):
            attempts.append(url)
            if url.startswith("https://modelscope.cn"):
                raise ConnectionError("modelscope unreachable")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("stub")

        monkeypatch.setattr(model_sources, "_download_file", fake_download_file)
        errors = model_sources.download_embedding_model(tmp_path / "model")
        assert any("modelscope" in e for e in errors)
        assert any(url.startswith("https://hf-mirror.com") for url in attempts)
        assert model_sources._is_complete(tmp_path / "model")

    def test_all_sources_failed_returns_errors(self, tmp_path, monkeypatch):
        def fake_download_file(url, target):
            raise ConnectionError("offline")

        monkeypatch.setattr(model_sources, "_download_file", fake_download_file)
        errors = model_sources.download_embedding_model(tmp_path / "model")
        assert len(errors) == len(model_sources.DEFAULT_SOURCES)

    def test_skips_existing_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORYBREAD_EMBEDDING_SOURCES", "modelscope")
        target = tmp_path / "model"
        (target / "modules.json").parent.mkdir(parents=True)
        (target / "modules.json").write_text("already-here")
        urls = []

        def fake_download_file(url, target_path):
            urls.append(url)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text("stub")

        monkeypatch.setattr(model_sources, "_download_file", fake_download_file)
        model_sources.download_embedding_model(target)
        assert not any(u.endswith("modules.json") for u in urls)

    def test_resolve_raises_with_reasons_when_all_failed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORYBREAD_EMBEDDING_MODEL_DIR", str(tmp_path / "models"))
        monkeypatch.setattr(model_sources, "_find_hf_cache_snapshot", lambda: None)
        monkeypatch.setattr(
            model_sources,
            "download_embedding_model",
            lambda target_dir: ["modelscope: offline", "hfmirror: offline"],
        )
        with pytest.raises(RuntimeError) as excinfo:
            model_sources.resolve_embedding_model_source()
        assert "modelscope: offline" in str(excinfo.value)


class TestSourceConfiguration:
    def test_default_order_is_domestic_first(self, monkeypatch):
        monkeypatch.delenv("MEMORYBREAD_EMBEDDING_SOURCES", raising=False)
        assert model_sources._configured_sources()[0] == "modelscope"

    def test_env_overrides_order(self, monkeypatch):
        monkeypatch.setenv("MEMORYBREAD_EMBEDDING_SOURCES", "huggingface,hfmirror")
        assert model_sources._configured_sources() == ["huggingface", "hfmirror"]

    def test_invalid_entries_ignored(self, monkeypatch):
        monkeypatch.setenv("MEMORYBREAD_EMBEDDING_SOURCES", "bogus,")
        assert model_sources._configured_sources() == list(model_sources.DEFAULT_SOURCES)
