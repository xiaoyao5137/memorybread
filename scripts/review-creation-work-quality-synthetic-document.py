#!/usr/bin/env python3
"""Review one previously recorded synthetic document once; never generate.

Loads the production module without creation.app's global service. Embedding,
source retrieval, Core mutations, and automatic retries are excluded. Captured
prompts and responses contain only the supplied synthetic evaluation content.
"""

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
import time
import types
import urllib.parse
from pathlib import Path


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def operation_state(database, source):
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT generated_content,lifecycle_status,revision_no FROM creation_history "
            "WHERE id=? AND session_id=?", (source["history_id"], source["session_id"])).fetchone()
        operations = connection.execute("SELECT operation_id,status,result_json FROM creation_operations "
            "WHERE session_id=? ORDER BY created_at", (source["session_id"],)).fetchall()
    if row is None:
        raise ValueError("Synthetic history was not found in the isolated Core database")
    return {"document_hash": digest(row[0]), "history_status": row[1], "revision_no": row[2],
        "operations": [{"id": item[0], "status": item[1],
            "result_hash": digest((json.loads(item[2] or "{}").get("data") or {}).get("document") or "")}
            for item in operations]}


async def evaluate(args):
    root = Path(__file__).resolve().parents[1]
    source_path = Path(args.source_report).resolve()
    output = Path(args.report).resolve()
    if output.exists():
        raise ValueError("Use a new report path; previous review evidence must remain unchanged")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if source.get("kind") != "creation-work-quality-real-agent-synthetic-evaluation":
        raise ValueError("This evaluator accepts only the recorded synthetic Agent Loop report")
    document = source["document"]
    if digest(document) != source["document_sha256"]:
        raise ValueError("Synthetic source document hash is inconsistent")
    database, review_database = Path(args.core_database).resolve(), Path(args.review_database).resolve()
    if any(Path("/tmp").resolve() not in path.parents for path in (database, review_database)):
        raise ValueError("Both databases must be explicit isolated paths under /tmp")
    endpoint = urllib.parse.urlparse(args.ollama_base_url)
    if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Synthetic review must use the local model endpoint")
    # Avoid importing creation.__init__, which installs the app singleton and
    # enables embeddings before a test service can supply its isolated options.
    sys.path.insert(0, str(root / "ai-sidecar"))
    package = types.ModuleType("creation")
    package.__path__ = [str(root / "ai-sidecar/creation")]
    package.__package__ = "creation"
    sys.modules["creation"] = package
    from creation.service import CreationService
    from creation.work_quality import load_work_quality_profiles, review_work_document

    responses = []

    class CapturedService(CreationService):
        async def _stream_direct_completion(self, **kwargs):
            capture = {key: kwargs[key] for key in ("system_prompt", "user_prompt", "json_schema")}
            capture["output"] = ""
            responses.append(capture)
            async for chunk in super()._stream_direct_completion(**kwargs):
                capture["output"] += chunk
                yield chunk

        def retrieve_references(self, *args, **kwargs):
            raise AssertionError("Synthetic semantic review must never retrieve sources")

        async def retrieve_data_context(self, *args, **kwargs):
            raise AssertionError("Synthetic semantic review must never retrieve data")

        async def search_web(self, *args, **kwargs):
            raise AssertionError("Synthetic semantic review must never browse")

    before = operation_state(database, source)
    service = CapturedService(ollama_base_url=args.ollama_base_url, db_path=str(review_database),
        model=args.model, enable_vector_recall=False)
    started = time.monotonic()
    result = await review_work_document(service, source["instruction"], document, source["work_context"])
    after = operation_state(database, source)
    report = {"kind": "creation-work-quality-independent-synthetic-review", "source_report": str(source_path),
        "original_model_review_status": source["semantic_review"]["status"],
        "original_manual_content_status": (source.get("manual_content_review") or {}).get("status"),
        "document_hash": digest(document), "elapsed_seconds": round(time.monotonic() - started, 3),
        "status": result["status"], "review": result, "responses": responses,
        "criteria_snapshot": load_work_quality_profiles(), "core_state_before": before, "core_state_after": after,
        "read_only": before == after, "one_bounded_review_request": len(responses) == 1,
        "embedding_disabled": service.enable_vector_recall is False,
        "evidence_boundary": "One semantic re-review of the unchanged synthetic document; no new generation or operation commit. Model and earlier human verdicts remain separately recorded."}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "elapsed_seconds", "read_only", "one_bounded_review_request")}, ensure_ascii=False), flush=True)
    if not report["read_only"] or not report["one_bounded_review_request"]:
        raise AssertionError("Synthetic review isolation or bounded request invariant failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--core-database", default="/tmp/mb-creation-quality-20260927/core.db")
    parser.add_argument("--review-database", default="/tmp/mb-creation-quality-20260927/quality-debug.db")
    parser.add_argument("--ollama-base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", required=True)
    asyncio.run(evaluate(parser.parse_args()))


if __name__ == "__main__":
    main()
