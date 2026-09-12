"""Local model acceptance: routing + independent admission, no external tools.

Run from ai-sidecar with PYTHONPATH=. .venv/bin/python scripts/evaluate_creation_skill_governance.py --output PATH
"""
import argparse
import asyncio
import copy
import json
import sqlite3
import time
from pathlib import Path

from creation.agent_loop import CreationAgentLoop
from creation.service import CreationOptions, CreationService
from creation.skill_governance import review_skill, admit_skills


ROOT = Path(__file__).resolve().parents[2]
LEGACY = json.loads((ROOT / "ai-sidecar/tests/fixtures/legacy_solution_skill.json").read_text())
ORIGINAL = "创作一篇如何对快手灵机产品大规模吸引非L0商家的方案，为商家制作爆款视频，进而极大提高GMV爆可品和可以使用SOTA视频生成模型的方案。"


def migrated_skill():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE creation_skills(id INTEGER PRIMARY KEY,client_skill_key TEXT,title TEXT,summary TEXT,skill_description TEXT,execution_steps TEXT,updated_at INTEGER)")
    db.execute("INSERT INTO creation_skills VALUES(1,?,?,?,?,?,0)",
               (LEGACY["id"], LEGACY["title"], LEGACY["summary"],
                json.dumps(LEGACY["skill_description"], ensure_ascii=False, separators=(",", ":")),
                json.dumps(LEGACY["execution_steps"], ensure_ascii=False, separators=(",", ":"))))
    db.executescript((ROOT / "core-engine/src/storage/migrations/113_creation_skill_governance.sql").read_text())
    summary, description, steps = db.execute("SELECT summary,skill_description,execution_steps FROM creation_skills").fetchone()
    return {**LEGACY, "summary": summary, "skill_description": json.loads(description), "execution_steps": json.loads(steps)}


async def main(output, only=None):
    service = CreationService(model="qwen3.5:4b", enable_vector_recall=False)
    repaired = migrated_skill()
    cases = [
        {"id": "legacy_business_sota_1", "query": ORIGINAL, "skill": LEGACY, "kind": "generate"},
        {"id": "legacy_business_sota_2", "query": ORIGINAL, "skill": LEGACY, "kind": "generate"},
        {"id": "repaired_business_sota", "query": ORIGINAL, "skill": repaired, "kind": "generate"},
        {"id": "business_without_sota", "query": "创作一篇快手灵机大规模吸引非L0商家的方案，帮助商家制作爆款视频并提升GMV。", "skill": repaired, "kind": "generate"},
        {"id": "technical_architecture", "query": "设计视频生成平台的技术架构评审文档，包含组件职责、接口协议、容量估算、模型选型、容错与回退；假设条件请明确标注。", "skill": repaired, "kind": "execute_skill"},
        {"id": "explicit_skill_business", "query": "使用@技术架构方案评审文档模板 编写商家增长业务方案，文档标题为《灵机商家增长试点方案》，主目标是提高商家入驻与GMV。", "skill": repaired, "explicit": True, "kind": "execute_skill", "title": "灵机商家增长试点方案"},
        {"id": "no_skill", "query": "创建一份新文档，标题为《商家视频试点方案》，写试点目标和执行步骤，不检索资料。", "kind": "generate", "title": "商家视频试点方案"},
        {"id": "local_edit", "query": "把周一改成周三，其他内容保持原样", "document": "# 商家试点方案\n\n## 安排\n周一启动。", "skill": repaired, "kind": "patch"},
        {"id": "template_question", "query": "技术架构方案评审文档模板适合哪些任务？只回答，不创作文档。", "skill": repaired, "kind": "respond"},
    ]
    results = []
    for case in cases:
        if only and case["id"] not in only:
            continue
        start = time.monotonic()
        skills = [case["skill"]] if "skill" in case else []
        loop = CreationAgentLoop(service)
        state = loop._new_state(user_message=case["query"], root_request=None, current_document=case.get("document", ""),
            conversation=[], selected_skills=skills, options=CreationOptions(enabled_tools=()),
            model_mode="local", session_id="governance-eval-" + case["id"], run_id="eval")
        explicit = [LEGACY["id"]] if case.get("explicit") else []
        state.environment["explicit_skill_ids"] = explicit
        state.environment["governance_required"] = True
        state.environment["requirement"]["operation_context"] = {
            "current_document": state.current_document, "explicit_skill_ids": explicit,
            "available_skills": [{"id": item["id"], "title": item["title"]} for item in skills],
        }
        checks, details = {}, {}
        try:
            decision = await service.route_capabilities(query=case["query"], requirement=state.environment["requirement"],
                selected_skills=skills, enabled_tool_ids=[])
            details["initial_kind"] = decision.get("operation", {}).get("kind")
            checks["model_decision"] = decision.get("source") == "model"
            async for event in loop._apply_routing_decision(state, state.plan[0], decision):
                pass
            op = state.environment["operation"]
            details.update(kind=op["kind"], identity=op.get("document_identity"), admission=state.environment.get("skill_admission"))
            checks["operation"] = op["kind"] == case["kind"]
            if case["id"] == "technical_architecture":
                # Ordinary generation remains a valid route; separately prove the
                # same automatic admission gate accepts a suitable technical skill.
                review = await review_skill(service, repaired, case["query"])
                admitted, audit = admit_skills({"kind": "execute_skill", "skill_ids": [repaired["id"]],
                    "skill_assessments": [review]}, [repaired], [], case["query"])
                details["positive_admission"] = audit
                checks["operation"] = op["kind"] in {"generate", "execute_skill"}
                checks["positive_admission"] = admitted["kind"] == "execute_skill"
            checks["no_wrong_workflow"] = case["kind"] == "execute_skill" or not any(step.get("kind") == "skill" for step in state.plan)
            if case.get("title"):
                checks["title"] = op.get("document_identity", {}).get("title") == case["title"]
        except Exception as error:
            checks["execution"] = False
            details["error"] = type(error).__name__ + ": " + str(error)
        result = {"case": case["id"], "checks": checks, "details": details,
                  "seconds": round(time.monotonic() - start, 2), "passed": all(checks.values())}
        results.append(result)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"model": "qwen3.5:4b", "cases": results}, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    if not all(result["passed"] for result in results):
        raise SystemExit("Skill governance acceptance failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", nargs="+")
    args = parser.parse_args()
    asyncio.run(main(args.output, args.only))
