"""Real sockets and OS processes prove cancellation, not just callback invocation."""

import asyncio
import json
import multiprocessing
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import inference_queue as queue_module
from inference_queue import InferencePreemptedError, InferenceQueue, Priority
from inference_transport import CancellableOllamaClient
from knowledge.extractor_v2 import (
    BakeInferenceTimeoutError, BakeModelRequestError, BakeModelResponseError,
    BakeModelTransportError, KnowledgeExtractorV2,
)
from rag.llm.ollama import OllamaBackend


def _isolate_locks(directory):
    for name, suffix in (
        ("_GLOBAL_SLOT_PREFIX", "slot"),
        ("_INTERACTIVE_DEMAND_LOCK_FILE", "interactive.lock"),
        ("_INTERACTIVE_DEMAND_PROBE_LOCK_FILE", "probe.lock"),
        ("_RAG_LOCK_FILE", "rag.lock"),
        ("_RAG_LOCK_OWNER_FILE", "rag-owner.txt"),
    ):
        setattr(queue_module, name, directory + "/" + suffix)


@pytest.fixture(autouse=True)
def isolated_locks(tmp_path, monkeypatch):
    names = [name for name in vars(queue_module) if name.startswith("_") and (
        "LOCK_FILE" in name or name in {"_GLOBAL_SLOT_PREFIX", "_RAG_LOCK_OWNER_FILE"}
    )]
    for name in names:
        monkeypatch.setattr(queue_module, name, str(tmp_path / name.lower()))


def _extractor(base_url):
    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
    extractor.model = "qwen3.5:test"
    extractor.ollama_base_url = base_url
    extractor.timeout = 10.0
    return extractor


@pytest.fixture
def blocked_model():
    servers = []

    def start(phase):
        entered = threading.Event()
        disconnected = threading.Event()
        finished = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.connection.settimeout(8.0)
                if phase != "headers":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    if phase == "stream":
                        self.wfile.write(b'{"response":"partial","message":{"content":"partial"}}\n')
                    self.wfile.flush()
                entered.set()
                try:
                    # This returns EOF only when the *real* HTTP transport closes.
                    if self.connection.recv(1) == b"":
                        disconnected.set()
                except ConnectionResetError:
                    disconnected.set()
                finally:
                    finished.set()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return "http://127.0.0.1:%d" % server.server_port, entered, disconnected, finished

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _call_model(kind, base_url):
    if kind == "extractor":
        return _extractor(base_url)._ollama_chat([{"role": "user", "content": "fixture"}])
    if kind == "diary":
        return CancellableOllamaClient(base_url).chat(
            model="test", messages=[{"role": "user", "content": "fixture"}], think=False,
        )
    return OllamaBackend(base_url=base_url).complete("fixture")


@pytest.mark.parametrize("phase", ["headers", "prefill", "stream"])
@pytest.mark.parametrize("kind", ["extractor", "diary", "rag"])
@pytest.mark.parametrize("priority", [Priority.P1, Priority.P2])
def test_p0_disconnects_real_background_socket_before_admission(blocked_model, phase, kind, priority):
    base_url, entered, disconnected, finished = blocked_model(phase)
    queue = InferenceQueue(max_concurrency=1, low_memory_threshold_mb=0)
    try:
        background = queue.submit(priority, lambda: _call_model(kind, base_url))
        assert entered.wait(2)
        requested_at = time.monotonic()

        def foreground():
            # EOF may be delivered just after local close; wait only for its proof.
            assert disconnected.wait(0.3)
            return "foreground"

        interactive = queue.submit(Priority.P0, foreground)
        assert interactive.result(timeout=1) == "foreground"
        assert time.monotonic() - requested_at < 0.8
        with pytest.raises(InferencePreemptedError):
            background.result(timeout=1)
        assert finished.wait(0.5)
        assert queue.submit_sync(priority, lambda: "resumed", timeout=1) == "resumed"
    finally:
        queue.shutdown()


def _background_process(directory, base_url, output):
    _isolate_locks(directory)
    queue = InferenceQueue(low_memory_threshold_mb=0, power_provider=lambda: None)
    try:
        try:
            queue.submit_sync(Priority.P2, lambda: _call_model("extractor", base_url), timeout=10)
            output.put("unexpected_success")
        except InferencePreemptedError:
            output.put("preempted")
    finally:
        queue.shutdown()


@pytest.mark.parametrize("phase", ["headers", "prefill", "stream"])
def test_cross_process_p0_releases_model_connection_and_machine_slot(tmp_path, monkeypatch, blocked_model, phase):
    # Parent and spawned worker must use the exact same isolated machine locks.
    _isolate_locks(str(tmp_path))
    monkeypatch.setenv("MEMORY_BREAD_MODEL_PARALLELISM", "1")
    base_url, entered, disconnected, _ = blocked_model(phase)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    process = context.Process(target=_background_process, args=(str(tmp_path), base_url, output))
    process.start()
    queue = InferenceQueue(low_memory_threshold_mb=0, power_provider=lambda: None)
    try:
        assert entered.wait(5)
        requested_at = time.monotonic()
        result = queue.submit_sync(Priority.P0, lambda: disconnected.wait(0.3), timeout=2)
        assert result is True
        assert time.monotonic() - requested_at < 1.0
        assert output.get(timeout=2) == "preempted"
        process.join(timeout=2)
        assert process.exitcode == 0
        assert not queue_module.interactive_demand_active()
    finally:
        queue.shutdown()
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        output.close()


def test_connect_phase_cancellation_unwinds_network_backend(monkeypatch):
    from httpcore._backends.anyio import AnyIOBackend

    entered = threading.Event()
    cleaned = threading.Event()

    async def connecting(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(AnyIOBackend, "connect_tcp", connecting)
    queue = InferenceQueue(max_concurrency=1, low_memory_threshold_mb=0)
    try:
        background = queue.submit(Priority.P1, lambda: _call_model("extractor", "http://127.0.0.1:1"))
        assert entered.wait(1)
        assert queue.submit_sync(Priority.P0, lambda: cleaned.is_set(), timeout=1) is True
        with pytest.raises(InferencePreemptedError):
            background.result(timeout=1)
    finally:
        queue.shutdown()


@pytest.mark.parametrize("failure,expected", [
    (httpx.ReadTimeout("fixture"), BakeInferenceTimeoutError),
    (httpx.ConnectError("fixture"), BakeModelTransportError),
    (httpx.RemoteProtocolError("fixture"), BakeModelTransportError),
    (httpx.Response(403, text="fixture"), BakeModelRequestError),
    (httpx.Response(200, text="not-json\n"), BakeModelResponseError),
])
def test_extractor_transport_preserves_error_categories(monkeypatch, failure, expected):
    real_client = httpx.AsyncClient

    def respond(request):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr("inference_transport.httpx.AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(respond),
    ))
    with pytest.raises(expected):
        _call_model("extractor", "http://127.0.0.1:1")


@pytest.mark.parametrize("events,expected", [
    ([{"error": "private model error"}], "本地模型返回错误"),
    ([{"message": {"content": "partial"}}, {"error": "private model error"}], "本地模型返回错误"),
    ([], "本地模型连接提前结束"),
    ([{"message": {"content": "partial"}}], "本地模型连接提前结束"),
])
def test_diary_client_rejects_error_events_and_incomplete_streams(monkeypatch, events, expected):
    from ollama import ResponseError

    real_client = httpx.AsyncClient
    clients = []

    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            for event in events:
                yield (json.dumps(event) + "\n").encode()

        async def aclose(self):
            self.closed = True

    stream = Stream()

    def make_client(**kwargs):
        client = real_client(**kwargs, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream),
        ))
        clients.append(client)
        return client

    monkeypatch.setattr("inference_transport.httpx.AsyncClient", make_client)
    with pytest.raises(ResponseError, match=expected) as error:
        _call_model("diary", "http://127.0.0.1:1")
    assert "partial" not in str(error.value)
    assert "private" not in str(error.value)
    assert stream.closed and clients[0].is_closed


@pytest.mark.parametrize("done_reason", ["stop", "length"])
def test_diary_client_requires_complete_stream_and_preserves_usage(monkeypatch, done_reason):
    real_client = httpx.AsyncClient
    events = [
        {"message": {"content": "complete "}},
        {"message": {"content": "report"}},
        {"done": True, "done_reason": done_reason, "prompt_eval_count": 7, "eval_count": 2},
    ]
    monkeypatch.setattr("inference_transport.httpx.AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(
            200, text="\n".join(json.dumps(event) for event in events),
        )),
    ))
    result = _call_model("diary", "http://127.0.0.1:1")
    assert result["message"]["content"] == "complete report"
    assert result["done"] is True
    assert result["done_reason"] == done_reason
    assert result["prompt_eval_count"] == 7 and result["eval_count"] == 2
