import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from runtime_download import DownloadFailure, RuntimeDownloader, failure_code, verified_tls_context


@pytest.fixture
def server():
    calls = []
    responder = {'fn': lambda path, headers: (200, {}, b'correct artifact')}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, dict(self.headers)))
            code, headers, content = responder['fn'](self.path, self.headers)
            self.send_response(code)
            for key, value in headers.items():
                self.send_header(key, value)
            if 'Content-Length' not in headers:
                self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield 'http://127.0.0.1:%d' % httpd.server_port, responder, calls
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def downloader(tmp_path, content=b'correct artifact', **kwargs):
    return RuntimeDownloader(tmp_path, hashlib.sha256(content).hexdigest(), lambda _: None, **kwargs)


def seed_partial(d, url, content, validator='"v1"'):
    key = hashlib.sha256((url + d.expected_sha).encode()).hexdigest()[:24]
    archive = d.root / (key + '.part')
    archive.write_bytes(content)
    archive.with_suffix('.json').write_text(json.dumps({'validator': validator}))
    return archive


def test_bad_mirror_falls_through_to_verified_source(server, tmp_path):
    base, responder, calls = server
    responder['fn'] = lambda path, _: (200, {}, b'bad mirror' if path == '/mirror' else b'correct artifact')
    d = downloader(tmp_path)
    assert d.download([base + '/mirror', base + '/official']).read_bytes() == b'correct artifact'
    assert len(calls) == 2
    assert d.events[0]['error_code'] == 'RUNTIME_CHECKSUM_MISMATCH'
    assert d.events[-1]['status'] == 'passed'
    assert base not in (tmp_path / 'download-diagnostics.json').read_text()


def test_resume_validates_content_range_and_validator(server, tmp_path):
    base, responder, calls = server
    responder['fn'] = lambda _, headers: (206, {'ETag': '"v1"', 'Content-Range': 'bytes 8-15/16'}, b'artifact')
    d = downloader(tmp_path)
    seed_partial(d, base, b'correct ')
    assert d.download([base]).read_bytes() == b'correct artifact'
    assert calls[0][1]['Range'] == 'bytes=8-'
    assert calls[0][1]['If-Range'] == '"v1"'


@pytest.mark.parametrize('headers', [
    {'ETag': '"v1"', 'Content-Range': 'bytes 0-7/16'},
    {'ETag': '"v2"', 'Content-Range': 'bytes 8-15/16'},
])
def test_invalid_resume_discards_partial_and_restarts(server, tmp_path, monkeypatch, headers):
    monkeypatch.setattr('runtime_download.time.sleep', lambda _: None)
    base, responder, calls = server
    responder['fn'] = lambda _, request: ((206, headers, b'artifact') if request.get('Range')
                                         else (200, {'ETag': '"v2"'}, b'correct artifact'))
    d = downloader(tmp_path)
    seed_partial(d, base, b'correct ')
    assert d.download([base]).read_bytes() == b'correct artifact'
    assert d.events[0]['error_code'] == 'RUNTIME_RESUME_INVALID'
    assert 'Range' not in calls[1][1]


def test_range_ignored_replaces_instead_of_appending(server, tmp_path):
    base, _, _ = server
    d = downloader(tmp_path)
    seed_partial(d, base, b'old bytes')
    assert d.download([base]).read_bytes() == b'correct artifact'


def test_416_resets_cache(server, tmp_path, monkeypatch):
    monkeypatch.setattr('runtime_download.time.sleep', lambda _: None)
    base, responder, _ = server
    responder['fn'] = lambda _, headers: (416, {}, b'') if headers.get('Range') else (200, {}, b'correct artifact')
    d = downloader(tmp_path)
    seed_partial(d, base, b'old bytes')
    assert d.download([base]).read_bytes() == b'correct artifact'


def test_completed_cache_reused_without_network(server, tmp_path):
    base, _, calls = server
    d = downloader(tmp_path)
    seed_partial(d, base, b'correct artifact')
    assert d.download([base]).read_bytes() == b'correct artifact'
    assert calls == []


def test_failed_download_keeps_partial_for_next_run(server, tmp_path, monkeypatch):
    monkeypatch.setattr('runtime_download.time.sleep', lambda _: None)
    base, responder, _ = server
    responder['fn'] = lambda *_: (503, {}, b'')
    d = downloader(tmp_path)
    part = seed_partial(d, base, b'correct ')
    with pytest.raises(DownloadFailure, match='RUNTIME_HTTP_FAILED'):
        d.download([base])
    assert part.read_bytes() == b'correct '
    assert len(d.events) == 3


def test_budget_stops_before_network(tmp_path):
    with pytest.raises(DownloadFailure, match='RUNTIME_DOWNLOAD_TIMEOUT'):
        downloader(tmp_path, total_seconds=0).download(['http://unused.invalid'])


def test_failure_categories():
    import errno
    import ssl
    import urllib.error
    assert failure_code(urllib.error.URLError(ssl.SSLCertVerificationError())) == 'RUNTIME_TLS_FAILED'
    assert failure_code(OSError(errno.ENOSPC, 'full')) == 'INSUFFICIENT_DISK_SPACE'
    assert failure_code(PermissionError()) == 'RUNTIME_WRITE_FAILED'
    assert failure_code(OSError(errno.EACCES, 'denied')) == 'RUNTIME_WRITE_FAILED'


def test_interrupted_body_resumes_on_same_source(server, tmp_path, monkeypatch):
    monkeypatch.setattr('runtime_download.time.sleep', lambda _: None)
    base, responder, calls = server
    responder['fn'] = lambda _, headers: (
        (206, {'ETag': '"v1"', 'Content-Range': 'bytes 8-15/16'}, b'artifact')
        if headers.get('Range') else
        (200, {'ETag': '"v1"', 'Content-Length': '16'}, b'correct ')
    )
    d = downloader(tmp_path)
    assert d.download([base]).read_bytes() == b'correct artifact'
    assert len(calls) == 2
    assert calls[1][1]['Range'] == 'bytes=8-'


def test_https_downloads_use_explicit_verified_ca_context(tmp_path, monkeypatch):
    captured = {}

    class Response:
        headers = {'Content-Length': '16'}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read1(self, _size):
            if captured.get('read'):
                return b''
            captured['read'] = True
            return b'correct artifact'

        read = read1

    def fake_urlopen(_request, timeout, context):
        captured['timeout'] = timeout
        captured['context'] = context
        return Response()

    monkeypatch.setattr('runtime_download.urllib.request.urlopen', fake_urlopen)
    d = downloader(tmp_path)
    assert d.download(['https://download.example/artifact']).read_bytes() == b'correct artifact'
    assert captured['context'] is d.tls_context
    assert captured['context'].cert_store_stats()['x509_ca'] > 0


def test_verified_tls_context_has_packaged_ca_roots():
    assert verified_tls_context().cert_store_stats()['x509_ca'] > 0


def test_shared_cache_download_is_serialized(server, tmp_path):
    base, responder, calls = server

    def slow_response(_path, _headers):
        time.sleep(0.2)
        return 200, {}, b'correct artifact'

    responder['fn'] = slow_response
    results = []
    failures = []

    def run_download():
        try:
            results.append(downloader(tmp_path).download([base + '/artifact']).read_bytes())
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=run_download) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert failures == []
    assert results == [b'correct artifact', b'correct artifact']
    assert len(calls) == 1
    assert not list(tmp_path.glob('download-diagnostics.json.tmp-*'))
