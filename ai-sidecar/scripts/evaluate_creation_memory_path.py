"""Isolated production SQLite retrieval plus local-model creation acceptance.

Run from ai-sidecar:
  PYTHONPATH=. .venv/bin/python scripts/evaluate_creation_memory_path.py \
    --output /tmp/creation-memory-path.json --ollama-url http://127.0.0.1:11434
Use --preflight-only to verify real retrieval and isolation without model calls.
"""
import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import sqlite3
import tempfile
import time
from urllib.parse import urlparse

TITLE = "青禾项目验收材料"
CONTENT = "青禾项目验收材料：已完成3项；待办2项；负责人林舟。"
SOURCE_ID = 61001
INSTRUCTION = (
    "请使用记忆搜索读取本地的《青禾项目验收材料》，据该文档写一小段项目小结。"
    "只复述其中的已完成项数、待办项数和负责人，并注明资料标题；"
    "不加编号、日期、比例、合计、原因、效果或建议。"
    "只用该本地文档，不联网、不查询看板，未取得文档就明确失败，不猜测内容。"
)

# Same six-domain SQLite fixture contract as tests/test_creation_references.py;
# unused domains remain empty so production discovery/ranking still executes.
SCHEMA = """
CREATE TABLE bake_documents (
 id INTEGER PRIMARY KEY, title TEXT, doc_type TEXT, summary TEXT, full_content TEXT,
 sections_json TEXT, style_phrases TEXT, prompt_hint TEXT, usage_count INTEGER,
 review_status TEXT, updated_at INTEGER, source_url TEXT, deleted_at INTEGER,
 status TEXT DEFAULT 'draft');
CREATE TABLE bake_knowledge (id INTEGER PRIMARY KEY, title TEXT, summary TEXT,
 content TEXT, detailed_content TEXT, entities TEXT, importance INTEGER,
 user_verified INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER);
CREATE TABLE bake_sops (id INTEGER PRIMARY KEY, title TEXT, summary TEXT,
 content TEXT, detailed_content TEXT, entities TEXT, importance INTEGER,
 user_verified INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER);
CREATE TABLE data_sources (id INTEGER PRIMARY KEY, title TEXT, source_url TEXT,
 status TEXT, deleted_at INTEGER);
CREATE TABLE data_snapshots (id INTEGER PRIMARY KEY, source_id INTEGER,
 collected_at INTEGER, observed_at INTEGER, content_text TEXT, structured_data TEXT,
 status TEXT, period_start_at INTEGER, period_end_at INTEGER);
CREATE TABLE captures (id INTEGER PRIMARY KEY, ts INTEGER, webpage_title TEXT,
 win_title TEXT, ax_text TEXT, ocr_text TEXT, input_text TEXT, audio_text TEXT,
 url TEXT, is_sensitive INTEGER);
"""


def model_address(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use an HTTP loopback model endpoint without credentials")
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("Model endpoint must be loopback")
        except ValueError as exc:
            raise ValueError("Model endpoint must be loopback") from exc
    return host, parsed.port or 80


@contextmanager
def isolated_access(database, model_url, allow_model=False):
    """Block actual access, and retain attempted violations even if caught upstream."""
    host, port = model_address(model_url)
    expected = Path(database).resolve()
    audit = {"sqlite_opens": [], "network_connects": [], "blocked": []}
    original_sqlite = sqlite3.connect
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_resolve = socket.getaddrinfo

    def deny(kind, target):
        audit["blocked"].append({"kind": kind, "target": str(target)})
        raise RuntimeError("Isolated creation acceptance blocked " + kind)

    def connect_sqlite(path, *args, **kwargs):
        if str(path) != ":memory:" and Path(path).resolve() != expected:
            return deny("sqlite", path)
        audit["sqlite_opens"].append(str(path))
        return original_sqlite(path, *args, **kwargs)

    def resolve(address, requested_port, *args, **kwargs):
        normalized_address = address.decode("ascii") if isinstance(address, bytes) else address
        if not allow_model or normalized_address not in {host, "localhost", "127.0.0.1", "::1"} or int(requested_port) != port:
            return deny("dns", (address, requested_port))
        return original_resolve(address, requested_port, *args, **kwargs)

    def check_connection(address):
        allowed = False
        if allow_model and isinstance(address, tuple) and len(address) >= 2:
            try:
                allowed = ipaddress.ip_address(address[0]).is_loopback and int(address[1]) == port
            except ValueError:
                pass
        if not allowed:
            deny("network", address)
        audit["network_connects"].append(list(address))

    def connect(sock, address):
        check_connection(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check_connection(address)
        return original_connect_ex(sock, address)

    sqlite3.connect = connect_sqlite
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = resolve
    try:
        yield audit
    finally:
        sqlite3.connect = original_sqlite
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.getaddrinfo = original_resolve


def create_fixture(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO bake_documents VALUES (?, ?, ?, ?, ?, '[]', '[]', '', 0, "
                     "'auto_created', ?, NULL, NULL, 'draft')",
                     (SOURCE_ID, TITLE, "项目记录", CONTENT, CONTENT, int(time.time() * 1000)))


def document_checks(document):
    # Citation labels are identifiers, not factual numbers. All other numbers,
    # including invented totals/dates/percentages, must come from the fixture.
    body = re.sub(r"[\[【](?:来源[：:]\s*)?(?:1|document:" + str(SOURCE_ID) + r")[\]】]", "", document)
    body = re.sub(r"[*_`]", "", body)
    number = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)"
    noun = r"(?:工作|事项|任务)?"
    separator = r"\s*(?:为|是|[：:])?\s*"
    counts = {}
    accepted_number_spans = set()
    for field, label, expected in (
        ("completed_count", r"(?<![未待])(?:已完成|完成)", {"3", "三"}),
        ("pending_count", r"(?:待办|待完成|未完成)", {"2", "二", "两"}),
    ):
        patterns = (
            label + noun + separator + "(?P<value>" + number + r")\s*项",
            label + noun + r"(?:项数|数量|数)" + separator + "(?P<value>" + number +
            r")\s*(?:项)?(?=\s*(?:[，,。；;、\n]|$))",
            "(?P<value>" + number + r")\s*项" + noun + label,
        )
        mentions = [match for pattern in patterns for match in re.finditer(pattern, body)]
        counts[field] = bool(mentions) and all(match.group("value") in expected for match in mentions)
        accepted_number_spans.update(match.span("value") for match in mentions if match.group("value") in expected)
    numbers = list(re.finditer(r"\d+(?:\.\d+)?", body))
    chinese_numbers = list(re.finditer(r"([零〇一二两三四五六七八九十百千万亿]+)\s*(项|人|位|个|天|年|月|倍|%)", body))
    totals = re.search(r"(?:合计|总计|总数|总共|共)" + separator + number, body)
    causal = re.search(r"由于|因为|因此|导致|带来|得益于|促进|提升|提高|改善|增长|归因|表明|说明了|显著|顺利推进|按期|提前", body)
    return {
        "document_present": bool(document.strip()),
        **counts,
        "owner": bool(re.search(r"(?:负责人\s*(?:为|是|[：:])?\s*林舟|林舟负责)", body)),
        "source_title": TITLE in body,
        # A correct numeral in a total or a second metric is still unsupported.
        "no_added_numbers": totals is None
            and all(match.span() in accepted_number_spans for match in numbers)
            and all(match.span(1) in accepted_number_spans and match.group(2) == "项" for match in chinese_numbers),
        "no_causal_language": causal is None,
    }


def make_service(database, model, model_url):
    # Imports are inside the access guard: incidental package initialization cannot
    # read a production database or contact an unrelated service.
    from creation.service import CreationService

    class RecordingService(CreationService):
        def __init__(self):
            super().__init__(db_path=str(database), model=model,
                             ollama_base_url=model_url, enable_vector_recall=False)
            self.retrieval_calls = []
            self.model_calls = []

        def _log_creation_usage(self, **kwargs):
            pass

        def retrieve_references(self, query, requirement, options):
            refs = super().retrieve_references(query, requirement, options)
            self.retrieval_calls.append({"query": query, "requirement": requirement,
                                         "references": [asdict(ref) for ref in refs]})
            return refs

        async def _stream_direct_completion(self, **kwargs):
            record = {"system_prompt": kwargs.get("system_prompt"),
                      "user_prompt": kwargs.get("user_prompt"), "output": ""}
            self.model_calls.append(record)
            async for chunk in super()._stream_direct_completion(**kwargs):
                record["output"] += chunk
                yield chunk

    return RecordingService()


async def evaluate(output, model="qwen3.5:4b", model_url="http://127.0.0.1:11434",
                   preflight_only=False, timeout=1200):
    output = Path(output).resolve()
    report = {"model": model, "endpoint": model_url,
              "mode": "retrieval_preflight" if preflight_only else "real_local_model_end_to_end",
              "source_fixture": {"source_type": "document", "source_id": SOURCE_ID,
                                 "title": TITLE, "full_content": CONTENT, "source_url": None},
              "instruction": INSTRUCTION, "passed": False, "end_to_end_passed": None,
              "checks": {}, "manual_review": "Inspect document and retained model/source provenance for semantic accuracy."}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mb-creation-memory-", dir="/tmp") as directory:
        database = Path(directory) / "fixture.sqlite"
        with isolated_access(database, model_url, allow_model=not preflight_only) as audit:
            report["isolation"] = audit
            service = None
            try:
                create_fixture(database)
                report["fixture_sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
                service = make_service(database, model, model_url)
                from creation.agent_loop import CreationAgentLoop
                from creation.operations import OperationError
                from creation.service import CreationOptions
                options = CreationOptions(enabled_tools=("memory_search",), max_references=3,
                                          browser_extension_enabled=False)
                if preflight_only:
                    requirement = service.analyze_requirement(TITLE, options)
                    refs = service.retrieve_references(TITLE, requirement, options)
                    refresh = await service.refresh_recalled_documents(refs, TITLE, browser_extension_enabled=False)
                    report["checks"] = {"real_sqlite_reference": any(
                        ref.source_id == SOURCE_ID and ref.full_content == CONTENT for ref in refs),
                        "no_model_calls": not service.model_calls, "no_source_refresh": refresh["attempted"] == 0}
                else:
                    class MemoryOnlyLoop(CreationAgentLoop):
                        async def _execute_step(self, state, step, **kwargs):
                            if step.get("kind") == "tool" and step.get("action") != "memory_search":
                                raise OperationError("TEST_UNEXPECTED_RESOURCE", "Fixture permits only local memory search")
                            async for event in super()._execute_step(state, step, **kwargs):
                                yield event

                    events = []
                    report["events"] = events
                    async def run():
                        async for event in MemoryOnlyLoop(service).run(
                            user_message=INSTRUCTION, root_request=INSTRUCTION, current_document="",
                            conversation=[], selected_skills=[], options=options,
                            session_id="synthetic-memory-path", run_id="memory-path-acceptance"):
                            if event["type"] != "operation.checkpoint" and not event["type"].endswith(".delta"):
                                events.append(event)
                    await asyncio.wait_for(run(), timeout=timeout)
                    final = events[-1] if events else {}
                    data = final.get("data", {})
                    document = str(data.get("document") or "")
                    report["document"] = document
                    report["events"] = events
                    report["delivery_review"] = data.get("delivery_review")
                    report["checks"] = {**document_checks(document),
                        "completed": final.get("type") == "run.completed",
                        "delivery_accepted": data.get("delivery_review", {}).get("status") == "pass",
                        "memory_tool_executed": any(e.get("type") == "tool.started" and
                            e.get("actor", {}).get("id") == "memory_search" for e in events),
                        "real_sqlite_reference": any(ref["source_id"] == SOURCE_ID and ref["full_content"] == CONTENT
                            for call in service.retrieval_calls for ref in call["references"]),
                        "source_reached_output": any(ref.get("source_id") == SOURCE_ID for ref in data.get("references", []))}
            except Exception as error:
                report["error"] = {"type": type(error).__name__, "message": str(error)}
            finally:
                if service is not None:
                    report["retrieval_calls"] = service.retrieval_calls
                    report["model_calls"] = service.model_calls
                    report["checks"]["vector_recall_disabled"] = not service.enable_vector_recall
                report["checks"]["isolation_enforced"] = not audit["blocked"]
                report["checks"]["fixture_unchanged"] = database.exists() and hashlib.sha256(database.read_bytes()).hexdigest() == report.get("fixture_sha256")
                report["passed"] = not report.get("error") and all(report["checks"].values())
                if not preflight_only:
                    report["end_to_end_passed"] = report["passed"]
                report["seconds"] = round(time.monotonic() - started, 2)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="qwen3.5:4b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    result = asyncio.run(evaluate(args.output, args.model, args.ollama_url, args.preflight_only, args.timeout))
    print(json.dumps({"mode": result["mode"], "passed": result["passed"], "checks": result["checks"]}, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)
