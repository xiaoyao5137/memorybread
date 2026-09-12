"""Real local-model user paths using synthetic materials and isolated storage.

Run selected cases serially; retain outputs for human semantic review as well as
deterministic assertions. A failed case is never treated as an accepted output.
"""
import argparse
import asyncio
import json
import re
import time
from pathlib import Path

from creation.service import CreationOptions
from scripts.evaluate_creation_delivery import IsolatedLoop, RecordingService


CASES = [
    {
        "id": "conversation-correction",
        "instruction": "仅按我前面更正后的数据写一份简短收入小结，计算相比上周的增长率，不使用外部资料，不加入原因分析或新事实。",
        "conversation": [
            {"role": "user", "content": "写收入小结。上周收入80万元，本周收入100万元。"},
            {"role": "assistant", "content": "可以把下期目标建议设为150万元。"},
            {"role": "user", "content": "更正：本周实际收入为96万元，150万元只是建议，不能写入实际收入。"},
        ],
        "contains": ["96", "80", "20%"], "excludes": ["100万元", "150万元"],
        "forbidden_pattern": r"20\d{2}(?:年|[-/])",
    },
    {
        "id": "brainstorm-confirmed-material",
        "instruction": "按已确认简报生成一份简短活动通知，只整理已确认的事实，不需要检索，不补充任何新事实。",
        "creation_mode": "brainstorm",
        "creation_brief": {"decisions": [
            {"question_id": "time", "dimension": "时间", "summary": "活动在周三下午举行。", "source": "user"},
            {"question_id": "place", "dimension": "地点", "summary": "地点为二楼会议室。", "source": "user"},
            {"question_id": "audience", "dimension": "参与者", "summary": "参与者为项目组全体成员。", "source": "user"},
        ]},
        "contains": ["周三下午", "二楼会议室", "全体成员"],
        "forbidden_pattern": r"本周|下周|上周|20\d{2}(?:年|[-/])",
    },
    {
        "id": "supplied-table",
        "instruction": "将以下材料整理为Markdown表格，表头为项目、状态、负责人；不补充事实，不使用外部资料：青禾：已完成，林舟；远帆：进行中，顾宁；星桥：未开始，周岚。",
        "contains": ["|", "青禾", "已完成", "林舟", "远帆", "进行中", "顾宁", "星桥", "未开始", "周岚"],
        "table_rows": [["项目", "状态", "负责人"], ["青禾", "已完成", "林舟"],
                       ["远帆", "进行中", "顾宁"], ["星桥", "未开始", "周岚"]],
    },
    {
        "id": "fiction-complete-story",
        "instruction": "写一篇约200字的完整童话：一只蜗牛在月亮上开面包店。故事要有困难、解决办法和结尾，纯属虚构，不检索任何资料。",
        "contains": ["蜗牛", "月亮", "面包"], "min_length": 140, "max_length": 310,
        "min_cjk": 160, "max_cjk": 240,
        "forbidden_pattern": r"[（(](?:困难|解决办法|结尾|制作过程)[^）)]*[）)]",
    },
    {
        "id": "answer-without-mutation",
        "instruction": "只根据当前文档回答：会议是什么时候，议题是什么？不要改动文档，不检索。",
        "document": "# 会议说明\n\n## 安排\n周三下午讨论项目进展。\n\n## 联系人\n联系项目组。\n",
        "answer": True, "contains": ["周三下午", "项目进展"],
    },
    {
        "id": "local-rewrite-preserves-facts",
        "instruction": "精简时间安排中的重复措辞，保留时间和议题，其他部分原样保留，不查询资料。",
        "document": "# 会议通知\n\n## 时间安排\n本次会议的具体时间已经确定，会议将于周一上午举行。请参会人员在周一上午按时参加本次会议。会议的主要内容为讨论当前的项目进展情况。\n\n## 联系方式\n请联系项目组。\n",
        "contains": ["周一上午", "项目进展"],
        "prefix": "# 会议通知\n\n## 时间安排\n",
        "suffix": "\n\n## 联系方式\n请联系项目组。\n", "shorter": True,
    },
    {
        "id": "mixed-question-writing",
        "instruction": "请先告诉我会议时间，再据此写一段可以直接发出的通知。",
        "conversation": [
            {"role": "user", "content": "会议定在周三下午，地点二楼会议室。只使用这些材料，不检索、不补充新事实。"},
        ],
        "contains": ["周三下午", "二楼会议室"],
        # Unknown participants must not acquire a role from a notice template.
        # These regression markers supplement, not replace, human fact review.
        "excludes": ["同事", "员工", "客户", "家长", "领导", "同学", "例会", "报备", "负责人"],
        "forbidden_pattern": r"本周|下周|上周|20\d{2}(?:年|[-/])",
    },
]


async def evaluate(args):
    results = []
    if args.resume and args.output.exists():
        results = json.loads(args.output.read_text())["cases"]
    passed_ids = {item["id"] for item in results if item["passed"]}
    unknown = set(args.case or []) - {case["id"] for case in CASES}
    if unknown:
        raise ValueError("Unknown cases: " + ", ".join(sorted(unknown)))
    for case in CASES:
        if (args.case and case["id"] not in args.case) or (case["id"] in passed_ids and not args.case):
            continue
        service = RecordingService(model=args.model, enable_vector_recall=False,
                                   ollama_base_url=args.ollama_url,
                                   trace_path=args.output.with_suffix(".calls.jsonl"))
        events = []
        started = time.monotonic()
        record = {"id": case["id"], "fixture": case, "passed": False}
        async def run_case():
            async for event in IsolatedLoop(service).run(
                user_message=case["instruction"], root_request=case["instruction"],
                current_document=case.get("document", ""), conversation=case.get("conversation", []),
                selected_skills=[], options=CreationOptions(),
                session_id="synthetic-user-paths", run_id=case["id"],
                creation_mode=case.get("creation_mode", "direct"),
                creation_brief=case.get("creation_brief"),
            ):
                events.append(event)
        try:
            await asyncio.wait_for(run_case(), timeout=args.timeout)
            final = events[-1]
            data = final.get("data", {})
            document = data.get("document", "")
            answer = data.get("response", "")
            output = answer if case.get("answer") else document
            checks = {
                "completed": final["type"] == "run.completed",
                "accepted": data.get("delivery_review", {}).get("status") == "pass",
                "required_content": all(value in output for value in case.get("contains", [])),
                "excluded_content": not any(value in output for value in case.get("excludes", [])),
                "no_unprovided_calendar": not case.get("forbidden_pattern") or not re.search(case["forbidden_pattern"], output),
                "bounded_length": case.get("min_length", 1) <= len(output) <= case.get("max_length", 100000),
            }
            if case.get("answer"):
                checks["read_only"] = document == case["document"]
            if "prefix" in case:
                checks["preserved_outside"] = document.startswith(case["prefix"]) and document.endswith(case["suffix"])
            if case.get("shorter"):
                checks["shorter"] = len(document) < len(case["document"])
            if case.get("table_rows"):
                rows = [[cell.strip().strip("*`") for cell in line.strip().strip("|").split("|")]
                        for line in document.splitlines() if line.strip().startswith("|")]
                rows = [row for row in rows if not all(re.fullmatch(r"[:\-\s]+", cell) for cell in row)]
                checks["table_row_bindings"] = rows == case["table_rows"]
            if "min_cjk" in case:
                from creation.delivery_contract import candidate_text_metrics
                count = candidate_text_metrics(output)["body_cjk_characters"]
                checks["cjk_length"] = case["min_cjk"] <= count <= case["max_cjk"]
            record.update(checks=checks, passed=all(checks.values()), document=document, response=answer,
                          delivery_review=data.get("delivery_review"))
        except Exception as error:
            record.update(error=type(error).__name__ + ": " + str(error),
                          error_code=getattr(error, "code", None))
        record["seconds"] = round(time.monotonic() - started, 2)
        record["events"] = [{"type": event["type"], "actor": event["actor"]} for event in events
                            if event["type"] != "operation.checkpoint"]
        record["model_replies"] = service.replies
        record["model_calls"] = service.calls
        results = [item for item in results if item["id"] != case["id"]] + [record]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"model": args.model, "scope": "real model; synthetic supplied facts; isolated storage; no retrieval", "cases": results}, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({key: record[key] for key in ("id", "passed", "seconds")}), flush=True)
    if not results or not all(item["passed"] for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3.5:4b")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--timeout", type=float, default=360)
    asyncio.run(evaluate(parser.parse_args()))
