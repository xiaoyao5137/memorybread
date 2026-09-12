"""Verified runtime downloads. Python 3.9; diagnostics never contain URLs or paths."""
import errno
import hashlib
import http.client
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional


class DownloadFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def failure_code(exc: Exception) -> str:
    if isinstance(exc, DownloadFailure):
        return exc.code
    reason = getattr(exc, 'reason', exc)
    if isinstance(reason, ssl.SSLError):
        return 'RUNTIME_TLS_FAILED'
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'RUNTIME_DOWNLOAD_TIMEOUT'
    if isinstance(reason, OSError) and reason.errno in (errno.ENOSPC, errno.EDQUOT):
        return 'INSUFFICIENT_DISK_SPACE'
    if isinstance(reason, PermissionError) or (isinstance(reason, OSError) and reason.errno in (errno.EACCES, errno.EPERM, errno.EROFS)):
        return 'RUNTIME_WRITE_FAILED'
    if isinstance(exc, urllib.error.HTTPError):
        return 'RUNTIME_HTTP_FAILED'
    return 'RUNTIME_NETWORK_FAILED'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class RuntimeDownloader:
    def __init__(self, root: Path, expected_sha: str, progress: Callable[[int], None],
                 total_seconds: float = 1800, source_seconds: float = 600):
        self.root = root
        self.expected_sha = expected_sha
        self.progress = progress
        self.total_seconds = total_seconds
        self.source_seconds = source_seconds
        self.events: List[dict] = []

    def _record(self, source: int, attempt: int, started: float, code: Optional[str], exc=None):
        self.events.append({'id': 'runtime.source_%d.attempt_%d' % (source, attempt),
                            'status': 'failed' if code else 'passed', 'error_code': code,
                            'duration_ms': int((time.monotonic() - started) * 1000),
                            'http_status': exc.code if isinstance(exc, urllib.error.HTTPError) else None})
        target = self.root / 'download-diagnostics.json'
        temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps(self.events[-30:]), encoding='utf-8')
        temp.replace(target)

    def download(self, urls: List[str]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.total_seconds
        last_code = 'RUNTIME_DOWNLOAD_FAILED'
        for source, url in enumerate(dict.fromkeys(urls), 1):
            # Cache identity includes source and expected artifact; never append across mirrors.
            key = hashlib.sha256((url + self.expected_sha).encode()).hexdigest()[:24]
            archive = self.root / (key + '.part')
            metadata = self.root / (key + '.json')
            source_deadline = min(deadline, time.monotonic() + self.source_seconds)
            for attempt in range(1, 4):
                started = time.monotonic()
                if started >= deadline:
                    raise DownloadFailure('RUNTIME_DOWNLOAD_TIMEOUT')
                if started >= source_deadline:
                    last_code = 'RUNTIME_DOWNLOAD_TIMEOUT'
                    break
                try:
                    # A completed download survives process exit before extraction.
                    if not archive.exists() or sha256(archive) != self.expected_sha:
                        self._transfer(url, archive, metadata, source_deadline)
                    if sha256(archive) != self.expected_sha:
                        archive.unlink(missing_ok=True)
                        metadata.unlink(missing_ok=True)
                        raise DownloadFailure('RUNTIME_CHECKSUM_MISMATCH')
                    self._record(source, attempt, started, None)
                    return archive
                except Exception as exc:
                    last_code = failure_code(exc)
                    self._record(source, attempt, started, last_code, exc)
                    if last_code in ('INSUFFICIENT_DISK_SPACE', 'RUNTIME_WRITE_FAILED'):
                        raise DownloadFailure(last_code) from exc
                    # Bad content/certificates/permanent HTTP errors won't improve on this source.
                    if last_code in ('RUNTIME_CHECKSUM_MISMATCH', 'RUNTIME_TLS_FAILED'):
                        break
                    if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500 and exc.code not in (408, 416, 429):
                        break
                    if attempt < 3:
                        time.sleep(max(0, min(attempt, source_deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            last_code = 'RUNTIME_DOWNLOAD_TIMEOUT'
        raise DownloadFailure(last_code)

    def _transfer(self, url: str, archive: Path, metadata: Path, deadline: float):
        try:
            saved = json.loads(metadata.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
        validator = saved.get('validator')
        offset = archive.stat().st_size if archive.exists() and validator else 0
        headers = {'User-Agent': 'MemoryBread-Initializer/1', 'Accept-Encoding': 'identity'}
        if offset:
            headers.update({'Range': 'bytes=%d-' % offset, 'If-Range': validator})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DownloadFailure('RUNTIME_DOWNLOAD_TIMEOUT')
        try:
            response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=min(15, remaining))
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                archive.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
            raise
        with response:
            etag = response.headers.get('ETag', '')
            current_validator = etag if etag and not etag.startswith('W/') else response.headers.get('Last-Modified')
            total = int(response.headers.get('Content-Length') or 0)
            status = getattr(response, 'status', None) or (200 if url.startswith('file:') else 0)
            if status == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                if (not offset or not match or int(match[1]) != offset
                        or int(match[2]) < offset or int(match[3]) <= int(match[2])
                        or (total and total != int(match[2]) - offset + 1)
                        or current_validator != validator):
                    archive.unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)
                    raise DownloadFailure('RUNTIME_RESUME_INVALID')
                total = int(match[3])
            elif status == 200:
                offset = 0
            else:
                raise DownloadFailure('RUNTIME_HTTP_FAILED')
            # Truncate before changing metadata so crashes cannot pair old bytes with a new validator.
            with archive.open('ab' if offset else 'wb') as output:
                metadata.write_text(json.dumps({'validator': current_validator}), encoding='utf-8')
                downloaded = offset
                while True:
                    if time.monotonic() >= deadline:
                        raise DownloadFailure('RUNTIME_DOWNLOAD_TIMEOUT')
                    chunk = getattr(response, 'read1', response.read)(256 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        self.progress(min(80, int(downloaded * 80 / total)))
                if total and downloaded != total:
                    raise http.client.IncompleteRead(b'', total - downloaded)
