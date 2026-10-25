"""IPv4 CONNECT transport for CLI inference; TLS stays end to end.

Some dual-stack networks accept IPv6 TCP connections then reset TLS. Rust's
HTTP client does not retry IPv4 after that TLS failure. A per-invocation loopback
tunnel selects IPv4 without changing system DNS, TLS verification, or Codex auth.
Explicit upstream proxy settings always take precedence.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager


@asynccontextmanager
async def cli_network_environment(env, *, enabled=False):
    proxy_keys = {"http_proxy", "https_proxy", "all_proxy"}
    if not enabled or any(k.lower() in proxy_keys and v for k, v in env.items()):
        yield env
        return

    tasks = set()
    writers = set()

    async def connect(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.add(writer)
        remote = None
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            if len(header) > 8192:
                return
            method, authority, version = header.split(b"\r\n", 1)[0].decode("ascii").split()
            if method != "CONNECT" or version != "HTTP/1.1":
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                await writer.drain()
                return
            host, port = authority.rsplit(":", 1)
            if not host or any(c in host for c in "/@[]:") or not 1 <= int(port) <= 65535:
                return
            upstream, remote = await asyncio.wait_for(
                asyncio.open_connection(host, int(port), family=socket.AF_INET), 15
            )
            writers.add(remote)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            copies = [asyncio.create_task(copy(reader, remote)),
                      asyncio.create_task(copy(upstream, writer))]
            try:
                await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pending in copies:
                    pending.cancel()
                await asyncio.gather(*copies, return_exceptions=True)
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError):
            # Never log CONNECT headers, credentials, prompts, or response bytes.
            pass
        finally:
            for stream in (writer, remote):
                if stream is not None:
                    writers.discard(stream)
                    stream.close()
            tasks.discard(task)

    server = await asyncio.start_server(connect, "127.0.0.1", 0, limit=8192)
    port = server.sockets[0].getsockname()[1]
    proxy = f"http://127.0.0.1:{port}"
    try:
        yield {**env, "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy}
    finally:
        server.close()
        await server.wait_closed()
        for writer in list(writers):
            writer.close()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
