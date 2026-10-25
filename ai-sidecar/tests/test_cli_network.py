import asyncio
import socket
from urllib.parse import urlparse

import pytest

from cli_network import cli_network_environment


def test_connect_tunnel_relays_bytes_over_ipv4_and_closes(monkeypatch):
    async def run():
        families = []
        real_connect = asyncio.open_connection

        async def recording_connect(*args, **kwargs):
            if "family" in kwargs:
                families.append(kwargs["family"])
            return await real_connect(*args, **kwargs)

        monkeypatch.setattr(asyncio, "open_connection", recording_connect)

        async def echo(reader, writer):
            try:
                writer.write(await reader.readexactly(6))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        remote_port = upstream.sockets[0].getsockname()[1]
        try:
            original = {"HOME": "/device", "NO_PROXY": "localhost"}
            async with cli_network_environment(original, enabled=True) as env:
                assert original == {"HOME": "/device", "NO_PROXY": "localhost"}
                proxy = urlparse(env["HTTPS_PROXY"])
                reader, writer = await real_connect(proxy.hostname, proxy.port)
                writer.write(f"CONNECT localhost:{remote_port} HTTP/1.1\r\n\r\n".encode())
                await writer.drain()
                assert await reader.readuntil(b"\r\n\r\n") == b"HTTP/1.1 200 Connection Established\r\n\r\n"
                writer.write(b"opaque")
                await writer.drain()
                assert await reader.readexactly(6) == b"opaque"
                writer.close()
                await writer.wait_closed()
            assert families == [socket.AF_INET]
            with pytest.raises(OSError):
                await real_connect(proxy.hostname, proxy.port)
        finally:
            upstream.close()
            await upstream.wait_closed()

    asyncio.run(run())


@pytest.mark.parametrize("key", ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "all_proxy"])
def test_user_proxy_is_preserved(key):
    async def run():
        original = {key: "http://device-proxy:8080"}
        async with cli_network_environment(original, enabled=True) as env:
            assert env is original
    asyncio.run(run())


def test_cancelled_invocation_closes_open_tunnels():
    async def run():
        ready = asyncio.Event()
        address = []

        async def invocation():
            async with cli_network_environment({}, enabled=True) as env:
                address.append(urlparse(env["HTTPS_PROXY"]))
                ready.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(invocation())
        await ready.wait()
        reader, writer = await asyncio.open_connection(address[0].hostname, address[0].port)
        # Leave a partial CONNECT header open when inference is cancelled.
        writer.write(b"CONNECT ")
        await writer.drain()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.wait_for(reader.read(), 1) == b""
        writer.close()
        await writer.wait_closed()
        with pytest.raises(OSError):
            await asyncio.open_connection(address[0].hostname, address[0].port)

    asyncio.run(run())
