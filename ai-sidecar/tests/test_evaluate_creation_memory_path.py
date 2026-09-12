import socket
import sqlite3

import pytest

from scripts.evaluate_creation_memory_path import (
    CONTENT, TITLE, document_checks, evaluate, isolated_access, model_address,
)


@pytest.mark.asyncio
async def test_preflight_uses_production_sqlite_retrieval_without_model_or_network(tmp_path):
    report = await evaluate(tmp_path / "report.json", preflight_only=True)
    assert report["passed"], report
    assert report["end_to_end_passed"] is None
    assert report["mode"] == "retrieval_preflight"
    assert report["model_calls"] == []
    assert report["isolation"]["network_connects"] == []
    assert report["isolation"]["blocked"] == []
    assert report["retrieval_calls"][0]["references"][0]["full_content"] == CONTENT
    assert report["retrieval_calls"][0]["references"][0]["source_url"] is None


@pytest.mark.parametrize("url", ["https://example.com", "http://192.168.1.1:11434", "http://user:secret@127.0.0.1:11434"])
def test_model_endpoint_cannot_point_to_remote_or_authenticated_service(url):
    with pytest.raises(ValueError):
        model_address(url)


def test_access_guard_blocks_other_sqlite_dns_and_core_endpoint(tmp_path):
    isolated = tmp_path / "fixture.sqlite"
    outside = tmp_path / "should-not-exist.sqlite"
    with isolated_access(isolated, "http://127.0.0.1:11434", allow_model=True) as audit:
        with sqlite3.connect(isolated) as conn:
            conn.execute("CREATE TABLE safe(value TEXT)")
        with pytest.raises(RuntimeError):
            sqlite3.connect(outside)
        with pytest.raises(RuntimeError):
            socket.getaddrinfo("example.com", 80)
        with socket.socket() as sock:
            with pytest.raises(RuntimeError):
                sock.connect(("127.0.0.1", 7072))
    assert not outside.exists()
    assert len(audit["blocked"]) == 3
    assert audit["network_connects"] == []
    # The wrapper restores global functions even when calls are denied.
    with sqlite3.connect(outside) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize("body, expected", [
    ("已完成3项，待办2项，负责人林舟。", True),
    ("已完成三项，待办两项，负责人为林舟。", True),
    ("已完成4项，待办2项，负责人林舟。", False),
    ("已完成3项，待办2项，共五项，负责人林舟。", False),
    ("已完成3项，待办2项，负责人林舟，因此效率显著提升。", False),
    ("已完成3项，待办2项，负责人王敏。", False),
    ("已完成3项，待办2项，负责人林舟。[999]", False),
    ("已完成3项，待办2项，负责人林舟。[document:61001]", True),
    ("已完成 **3** 项，尚有2项待办，由林舟负责。", True),
    ("已完成3项，待办2项，负责人林舟，耗时3天。", False),
    ("已完成项数3，待办项数2，负责人林舟。", True),
    ("已完成项数：三，待办项数为两，负责人林舟。", True),
    ("已完成任务数量为3，待办事项数量是2，负责人林舟。", True),
    ("已完成项数 **3**，待办项数 **2**，负责人林舟。", True),
    ("已完成项数3项，待办项数2项，负责人林舟。", True),
    ("已完成项数2，待办项数3，负责人林舟。", False),
    ("已完成项数3，待办项数2，总数5，负责人林舟。", False),
    ("已完成项数三，待办项数二，总数五，负责人林舟。", False),
    ("已完成项数3，待办项数2，总数3，负责人林舟。", False),
    ("已完成项数3，待办项数2，共2项，负责人林舟。", False),
    ("已完成项数3天，待办项数2，负责人林舟。", False),
    ("未完成3项，待办2项，负责人林舟。", False),
    ("待完成3项，待办2项，负责人林舟。", False),
    ("已完成3项，待办2项；已完成项数2，负责人林舟。", False),
])
def test_document_assertions_reject_wrong_facts_totals_and_causality(body, expected):
    assert all(document_checks(TITLE + "：" + body).values()) is expected


@pytest.mark.parametrize("document", ["", " \n\t"])
def test_empty_document_cannot_pass_even_if_an_answer_may_have_been_generated(document):
    checks = document_checks(document)
    assert not all(checks.values())
    assert checks["document_present"] is False
    assert checks["completed_count"] is False
    assert checks["pending_count"] is False


@pytest.mark.asyncio
async def test_guard_allows_only_configured_loopback_http_service(tmp_path):
    import asyncio
    import httpx

    async def respond(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with isolated_access(tmp_path / "fixture.sqlite", "http://127.0.0.1:{}".format(port), allow_model=True) as audit:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get("http://127.0.0.1:{}/stub".format(port))
            assert response.text == "ok"
            assert audit["blocked"] == []
            assert len(audit["network_connects"]) >= 1
    finally:
        server.close()
        await server.wait_closed()
