"""Real-model user-instruction matrix. No production sessions are modified.

Routing and input assessment are real. This stage intentionally does not assert
that a selected tool has executed; executor and delivery tests cover that boundary.
"""
import argparse
import asyncio
import json
import time
from pathlib import Path
from creation.service import CreationService
from creation.delivery_contract import bind_resources
from creation.operations import apply_patches, document_nodes, resolve_target
from scripts.evaluate_creation_delivery import RecordingService

DOCUMENT = "# 会议说明\n\n## 会议安排\n周一上午讨论项目进展。\n\n## 附录\n旧说明。\n\n## 联系方式\n请联系项目组。\n"
CASES = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/creation_instruction_cases.json").read_text())

async def evaluate(args):
    service = RecordingService(model=args.model, enable_vector_recall=False, ollama_base_url=args.ollama_url)
    results = []
    if args.resume and args.output.exists(): results = json.loads(args.output.read_text())["cases"]
    completed = {item["id"] for item in results if item["passed"]}
    unknown = set(args.case or []) - {c["id"] for c in CASES}
    if unknown: raise ValueError("Unknown fixture IDs: " + ", ".join(sorted(unknown)))
    selected = [c for c in CASES if not args.case or c["id"] in args.case]
    for case in selected:
        if case["id"] in completed: continue
        reply_start = len(service.replies)
        start = time.monotonic(); doc = case.get("document", DOCUMENT)
        record = {"id": case["id"], "instruction": case["instruction"]}
        try:
            context = {"current_document": doc, "nodes": document_nodes(doc),
                "session_goal": "创作市场调研、技术架构和实施设计方案" if doc else case["instruction"],
                "pending_operations": [{"operation_id": "pending-1", "instruction": "把周一改为周三", "status": "failed"}] if case.get("pending") else []}
            decision = await service.route_capabilities(query=case["instruction"], requirement={"operation_context": context}, selected_skills=[], enabled_tool_ids=["memory_search", "data_search", "internet_search"])
            record["candidate"] = decision; kind = decision.get("operation", {}).get("kind")
            if kind in {"generate", "transform", "answer", "execute_skill"}:
                c = await service.assess_creation_inputs(case["instruction"], doc, kind, [])
                record["input_contract"] = c; decision = bind_resources(decision, c)
            record["effective"] = decision
            checks = {"model_route": decision.get("source") == "model", "operation": kind in case["kinds"], "resources": set(decision.get("tools", [])) == set(case["tools"])}
            if case.get("allowed_tools"):
                checks["resources"] = set(case["tools"]) <= set(decision.get("tools", [])) <= set(case["allowed_tools"])
            if kind in {"patch", "resume", "respond", "transform"}:
                # The executor folds its query planner into data_search; it is
                # not an additional document workflow (see _compose_plan_from_decision).
                folded = {"data_query_planner"} if "data_search" in decision.get("tools", []) else set()
                checks["no_full_workflow"] = not (set(decision.get("agents", [])) - folded)
            if kind == "transform" and case.get("scope_titles"):
                expected = [n for n in document_nodes(doc) if n["title"] in case["scope_titles"]]
                ranges = [resolve_target(doc, target) for target in decision["operation"]["targets"]]
                checks["authorized_scope"] = bool(ranges) and all(any(n["start"] <= start and end <= n["end"] for n in expected) for start, end in ranges)
                checks["all_requested_scopes"] = all(any(n["start"] <= start and end <= n["end"] for start, end in ranges) for n in expected)
            if kind == "patch":
                final, _ = apply_patches(doc, decision["operation"]["patches"])
                if "delete" in case: checks["exact_result"] = final == doc.replace(case["delete"], "")
                if "replace" in case: checks["exact_result"] = final == doc.replace(*case["replace"])
                if "contains" in case: checks["literal_added"] = case["contains"] in final and "## 联系方式\n请联系项目组。" in final
            if case.get("pending"): checks["resume_target"] = decision.get("operation", {}).get("operation_id") == "pending-1"
            record["checks"] = checks; record["passed"] = all(checks.values())
            if not record["passed"]: record["model_replies"] = service.replies[reply_start:]
        except Exception as e: record.update(passed=False, error=str(e), cause=str(e.__cause__), model_replies=service.replies[reply_start:])
        record["seconds"] = round(time.monotonic() - start, 2)
        results = [r for r in results if r["id"] != case["id"]] + [record]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"model": args.model, "endpoint": args.ollama_url, "scope": "real routing and input assessment; synthetic documents; no external retrieval", "cases": results}, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: record[k] for k in ("id", "passed", "seconds")}, ensure_ascii=False), flush=True)
    if not all(r["passed"] for r in results): raise SystemExit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--model", default="qwen3.5:4b")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--output", type=Path, required=True); parser.add_argument("--case", action="append"); parser.add_argument("--resume", action="store_true")
    asyncio.run(evaluate(parser.parse_args()))
