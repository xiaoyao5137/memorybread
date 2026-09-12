from pathlib import Path
import hashlib
import time

from embedding import model_sources


def test_download_urls_are_revision_pinned():
    url = model_sources._url_for("huggingface", "model.safetensors")

    assert model_sources.MODEL_REVISION in url
    assert "/resolve/main/" not in url


def test_managed_source_is_first_by_default(monkeypatch):
    monkeypatch.delenv("MEMORYBREAD_EMBEDDING_SOURCES", raising=False)

    assert model_sources._configured_sources()[0] == "managed"


def test_incomplete_model_rejects_unverified_files(tmp_path: Path):
    for relative_path in model_sources.MODEL_FILES:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-the-pinned-artifact")

    assert model_sources.embedding_model_complete(tmp_path) is False


def test_file_download_uses_verified_source_bound_downloader(monkeypatch, tmp_path: Path):
    payload = b"verified-model"
    digest = hashlib.sha256(payload).hexdigest()
    source_file = tmp_path / "verified.part"
    source_file.write_bytes(payload)
    observed = {}

    class FakeDownloader:
        def __init__(self, root, expected_sha, progress, total_seconds, source_seconds):
            observed.update(root=root, expected_sha=expected_sha, total_seconds=total_seconds)

        def download(self, urls):
            observed["urls"] = urls
            return source_file

    monkeypatch.setitem(model_sources.MODEL_FILE_SHA256, "model.safetensors", digest)
    monkeypatch.setattr(model_sources, "RuntimeDownloader", FakeDownloader)
    target = tmp_path / "model" / "model.safetensors"
    url = "https://mirror.example/pinned/model.safetensors"

    model_sources._download_file(url, target, time.monotonic() + 30)

    assert target.read_bytes() == payload
    assert observed["expected_sha"] == digest
    assert observed["urls"] == [url]
