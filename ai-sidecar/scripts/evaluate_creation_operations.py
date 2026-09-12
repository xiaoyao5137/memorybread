"""Run synthetic operation-routing acceptance cases against a local model.

No search/production-document tools are executed. Run from ai-sidecar:
PYTHONPATH=. .venv/bin/python scripts/evaluate_creation_operations.py --output FILE
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

from creation.operations import apply_patches, document_nodes
from creation.service import CreationService


DOCUMENT = "# 会议说明\n\n## 会议安排\n周一上午讨论。\n\n## 附录\n旧说明。\n\n## 联系方式\n请联系项目组。\n"
CASES = [
    {"instruction": "移除附录，保留其他内容", "kinds": ["patch"], "delete": "## 附录\n旧说明。\n\n"},
    {"instruction": "Delete the appendix section and leave everything else unchanged.", "kinds": ["patch"], "delete": "## 附录\n旧说明。\n\n"},
    {"instruction": "把周一上午改成周三下午", "kinds": ["patch"], "replace": ["周一上午", "周三下午"]},
    {"instruction": "不要删除附录，只回答你是否理解", "kinds": ["respond"]},
    {"instruction": "接着完成刚才没做完的修改", "kinds": ["resume"], "pending": True},
    {"instruction": "把会议安排写得更简洁，其他部分保持原样", "kinds": ["transform"]},
    {"instruction": "移除附录，然后检索最新公开会议工具信息，补充一段推荐", "kinds": ["transform"], "tools": ["internet_search"]},
    {"instruction": "查一下最近的公开会议软件动态，只回答问题，不要修改文档", "kinds": ["answer"], "tools": ["internet_search"]},
]


async def evaluate(output: Path, model: str) -> None:
    service = CreationService(model=model, enable_vector_recall=False)
    results = []
    for case in CASES:
        context = {"current_document": DOCUMENT, "nodes": document_nodes(DOCUMENT),
                   "session_goal": "创作市场调研、技术架构和实施设计方案",
                   "pending_operations": ([{"operation_id": "pending-1", "instruction": "把会议安排中的周一改为周三", "status": "failed"}]
                                          if case.get("pending") else [])}
        start = time.monotonic()
        try:
            decision = await service.route_capabilities(query=case["instruction"],
                requirement={"operation_context": context}, selected_skills=[],
                enabled_tool_ids=["memory_search", "internet_search", "data_search"])
        except Exception as error:
            # 结构化契约拒绝会带着自己的错误码上抛（不再降级成 fallback），
            # 这里记录后继续跑剩余用例，不能让整个验收中断。
            results.append({"instruction": case["instruction"], "decision": None,
                            "seconds": round(time.monotonic() - start, 2),
                            "error": str(error),
                            "error_code": getattr(error, "code", type(error).__name__),
                            "checks": {"valid_model_decision": False}, "passed": False})
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"model": model, "cases": results}, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
            continue
        checks = {"valid_model_decision": decision.get("source") == "model", "kind": decision.get("operation", {}).get("kind") in case["kinds"],
                  "no_unneeded_agents": not decision.get("agents"),
                  "tools": set(decision.get("tools", [])) == set(case.get("tools", []))}
        try:
            if decision.get("operation", {}).get("kind") == "patch":
                document, _ = apply_patches(DOCUMENT, decision["operation"]["patches"])
                if "delete" in case:
                    checks["exact_document"] = document == DOCUMENT.replace(case["delete"], "")
                if "replace" in case:
                    checks["exact_document"] = document == DOCUMENT.replace(*case["replace"])
            if case.get("pending"):
                checks["resume_target"] = decision.get("operation", {}).get("operation_id") == "pending-1"
        except ValueError as error:
            checks["patch_valid"] = False
        results.append({"instruction": case["instruction"], "decision": decision,
                        "seconds": round(time.monotonic() - start, 2), "checks": checks, "passed": all(checks.values())})
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"model": model, "cases": results}, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    if not all(item["passed"] for item in results):
        raise SystemExit("Some operation acceptance cases failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="qwen3.5:4b")
    args = parser.parse_args()
    asyncio.run(evaluate(args.output, args.model))
