from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
import struct
from pathlib import Path
from uuid import uuid4

import pytest

from memory_bread_ipc import IpcResponse, IpcServer, transport


async def _unused_dispatch(req):
    return IpcResponse.make_error(req.id, "UNUSED", "unused", 0)


def _request_frame(request_id: str) -> bytes:
    payload = json.dumps({
        "id": request_id,
        "ts": 1,
        "task": {"type": "ping"},
    }).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


async def _wait_for_path(path) -> None:
    for _ in range(100):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"socket 未创建: {path}")


async def _wait_for_server(server: IpcServer) -> None:
    for _ in range(100):
        if server._server is not None and server._server.is_serving():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("IPC server 未进入监听状态")


def _short_socket_path(label: str) -> Path:
    # macOS 的 sockaddr_un 路径长度上限很短，pytest 的 tmp_path 会超过上限。
    return Path("/tmp") / f"mb-{os.getpid()}-{uuid4().hex[:8]}-{label}.sock"


def test_ipc_socket_path_can_be_configured_by_environment(monkeypatch):
    socket_path = _short_socket_path("env")
    monkeypatch.setenv("MEMORY_BREAD_IPC_SOCKET", str(socket_path))

    reloaded = importlib.reload(transport)

    assert reloaded.UNIX_SOCKET_PATH == str(socket_path)
    monkeypatch.delenv("MEMORY_BREAD_IPC_SOCKET")
    importlib.reload(transport)


@pytest.mark.skipif(transport.platform.system() == "Windows", reason="Unix socket only")
async def test_ipc_server_removes_owned_socket_on_stop(monkeypatch):
    socket_path = _short_socket_path("stop")
    monkeypatch.setattr(transport, "UNIX_SOCKET_PATH", str(socket_path))
    server = IpcServer(dispatch_fn=_unused_dispatch)
    task = asyncio.create_task(server.serve())

    await _wait_for_path(socket_path)
    server.stop()
    await asyncio.wait_for(task, timeout=1)

    assert not socket_path.exists()


@pytest.mark.skipif(transport.platform.system() == "Windows", reason="Unix socket only")
async def test_ipc_server_does_not_unlink_live_instance(monkeypatch):
    socket_path = _short_socket_path("owner")
    monkeypatch.setattr(transport, "UNIX_SOCKET_PATH", str(socket_path))
    first = IpcServer(dispatch_fn=_unused_dispatch)
    first_task = asyncio.create_task(first.serve())
    await _wait_for_path(socket_path)

    second = IpcServer(dispatch_fn=_unused_dispatch)
    with pytest.raises(RuntimeError, match="另一个 Sidecar"):
        await second.serve()
    assert socket_path.exists()

    first.stop()
    await asyncio.wait_for(first_task, timeout=1)
    assert not socket_path.exists()


@pytest.mark.skipif(transport.platform.system() == "Windows", reason="Unix socket only")
async def test_ipc_server_replaces_stale_socket(monkeypatch):
    socket_path = _short_socket_path("stale")
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()

    monkeypatch.setattr(transport, "UNIX_SOCKET_PATH", str(socket_path))
    server = IpcServer(dispatch_fn=_unused_dispatch)
    task = asyncio.create_task(server.serve())
    await _wait_for_server(server)

    server.stop()
    await asyncio.wait_for(task, timeout=1)
    assert not socket_path.exists()


@pytest.mark.skipif(transport.platform.system() == "Windows", reason="Unix socket only")
async def test_ipc_server_cancels_dispatch_when_client_disconnects(monkeypatch):
    socket_path = _short_socket_path("cancel")
    monkeypatch.setattr(transport, "UNIX_SOCKET_PATH", str(socket_path))
    dispatch_started = asyncio.Event()
    dispatch_cancelled = asyncio.Event()

    async def waiting_dispatch(_req):
        dispatch_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            dispatch_cancelled.set()

    server = IpcServer(dispatch_fn=waiting_dispatch)
    server_task = asyncio.create_task(server.serve())
    await _wait_for_server(server)

    _reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(_request_frame("cancel-me"))
    await writer.drain()
    await asyncio.wait_for(dispatch_started.wait(), timeout=1)
    writer.close()
    await writer.wait_closed()

    await asyncio.wait_for(dispatch_cancelled.wait(), timeout=1)
    server.stop()
    await asyncio.wait_for(server_task, timeout=1)
