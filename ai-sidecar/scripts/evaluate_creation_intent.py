"""Serial local-model intent checks; never run writers, retrieval, or sessions.

Run from ai-sidecar with PYTHONPATH=. and the shared IPC Python path available.
Use --list to inspect fixtures without contacting a model. An optional prompt
file tests a proposed system prompt against the production schema and validator.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time

from scripts.evaluate_creation_memory_path import INSTRUCTION, model_address


CASES = [
    {"id": "draft-existing-restatement", "instruction": "先生成一版。请基于原始创作要求和当前创作简报中已提交的回答，在当前文档基础上更新一版文档。尚未回答的问题、未确认的选项和待补充事项不能当作用户决定；缺失内容标注待确认，不编造事实。", "has_document": True, "expected": "edit"},
    {"id": "draft-existing-single-goal", "instruction": "请基于原始创作要求和当前创作简报中已提交的回答，在当前文档基础上更新一版文档。尚未回答的问题、未确认的选项和待补充事项不能当作用户决定；缺失内容标注待确认，不编造事实。", "has_document": True, "expected": "edit"},
    {"id": "draft-first-single-goal", "instruction": "请基于原始创作要求和当前创作简报中已提交的回答，生成一版文档。尚未回答的问题、未确认的选项和待补充事项不能当作用户决定；缺失内容标注待确认，不编造事实。", "expected": "create"},
    {"id": "en-edit-restatement", "instruction": "Produce a revised draft. Update the current report with the confirmed conclusions; keep unconfirmed details marked as pending.", "has_document": True, "expected": "edit"},
    {"id": "distinct-writing-goals", "instruction": "另写一份全新的通知，并精简当前已有的报告；这是两份不同产物，两个任务都必须完成。", "has_document": True, "expected": None},
    {"id": "existing-document-as-reference", "instruction": "以当前报告为参考，另写一份通知，不修改现有报告。", "has_document": True, "expected": "create"},
    {"id": "memory-original", "instruction": INSTRUCTION, "expected": "create"},
    {"id": "memory-question", "instruction": INSTRUCTION.replace(
        "据该文档写一小段项目小结。只复述其中的已完成项数、待办项数和负责人，并注明资料标题",
        "回答其中已完成几项、待办几项、负责人是谁，并注明资料标题；只回答问题，不修改文档"), "expected": "answer"},
    {"id": "summary-no-fallback", "instruction": "读取本地活动记录，据此写一小段活动小结，只复述材料中的事实，不补充新事实。", "expected": "create"},
    {"id": "summary-fallback-first", "instruction": "材料找不到就明确说明，不能猜测。请读取本地活动记录，并据此写一小段活动小结，只复述材料中的事实。", "expected": "create"},
    {"id": "short-notice", "instruction": "把这些事实写成一句可以直接发出的通知：活动在周三下午举行，地点为二楼会议室。", "expected": "create"},
    {"id": "short-question", "instruction": "活动在周三下午举行，地点为二楼会议室。活动是什么时候，在哪里？只回答，不修改文档。", "expected": "answer"},
    {"id": "en-summary", "instruction": "Read the local event notes and draft a one-sentence recap using only those facts. If unavailable, report that and do not guess.", "expected": "create"},
    {"id": "en-question", "instruction": "Read the local event notes and tell me when and where it happened. If unavailable, report that and do not guess. Do not modify the document.", "expected": "answer"},
    {"id": "failure-report", "instruction": "只按已有材料写一小段任务失败说明，不能猜测原因；材料不足就告知。", "expected": "create"},
    {"id": "failure-question", "instruction": "查一下已有材料，这次任务失败了吗？只回答，不修改文档；材料不足就告知。", "expected": "answer"},
    {"id": "edit-from-material", "instruction": "读取材料后，用一句话改写当前文档的结论；资料不足就说明原因，其他部分原样保留。", "has_document": True, "expected": "edit"},
    {"id": "literal-patch", "instruction": "把当前文档中的“周一上午”逐字替换为“周二下午”；找不到原文就告知。", "has_document": True, "expected": "patch"},
    {"id": "negated-writing", "instruction": "不要写通知。只告诉我当前文档中的活动时间是什么，保持正文不变。", "has_document": True, "expected": "answer"},
    {"id": "quoted-writing", "instruction": "解释“写一份通知”这句话的意思，别真的写通知，不修改当前正文。", "has_document": True, "expected": "answer"},
    {"id": "mixed-question-writing", "instruction": "请先告诉我会议时间，再据此写一段可以直接发出的通知。", "expected": "create"},
    {"id": "implicit-resume", "instruction": "继续", "has_document": True, "expected": "resume"},
    {"id": "undo", "instruction": "撤销刚才的修改", "has_document": True, "expected": "undo"},
    {"id": "implicit-edit", "instruction": "再短一点", "has_document": True, "expected": "edit"},
    {"id": "polite-edit", "instruction": "能不能把这段改成两句话？", "has_document": True, "expected": "edit"},
    {"id": "polite-answer", "instruction": "能不能解释一下这句话？不要改正文。", "has_document": True, "expected": "answer"},
]


async def evaluate(args, cases):
    # Importing service is unnecessary for listing fixtures and may load runtime
    # dependencies. Keep the no-model inspection path lightweight.
    from creation.skill_governance import task_intent
    from scripts.evaluate_creation_delivery import RecordingService

    class IntentService(RecordingService):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.calls = []

        async def _stream_direct_completion(self, **kwargs):
            if args.prompt_file:
                kwargs["system_prompt"] = args.prompt_file.read_text()
            record = {key: kwargs[key] for key in ("system_prompt", "user_prompt", "json_schema")}
            self.calls.append(record)
            started = time.monotonic()
            parts = []
            try:
                async for part in super()._stream_direct_completion(**kwargs):
                    parts.append(part)
                    yield part
            finally:
                record.update(output="".join(parts), seconds=round(time.monotonic() - started, 2))

    model_address(args.ollama_url)
    results = []
    for case in cases:
        service = IntentService(model=args.model, enable_vector_recall=False, ollama_base_url=args.ollama_url)
        started = time.monotonic()
        record = {"id": case["id"], "fixture": case, "passed": False}
        try:
            result = await task_intent(service, case["instruction"], case.get("has_document", False))
            record.update(result=result, passed=result.get("action") == case["expected"])
        except Exception as exc:
            record["error"] = type(exc).__name__ + ": " + str(exc)
        record.update(seconds=round(time.monotonic() - started, 2), model_calls=service.calls)
        results.append(record)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({
            "model": args.model, "endpoint": args.ollama_url,
            "scope": "real classifier only; synthetic instructions; no retrieval, writing, or session mutation",
            "prompt_override": str(args.prompt_file) if args.prompt_file else None,
            "cases": results}, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({key: record[key] for key in ("id", "passed", "seconds")}), flush=True)
    if not all(record["passed"] for record in results):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3.5:4b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--case", action="append")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    unknown = set(args.case or []) - {case["id"] for case in CASES}
    if unknown:
        parser.error("Unknown cases: " + ", ".join(sorted(unknown)))
    cases = [case for case in CASES if not args.case or case["id"] in args.case]
    if args.list:
        print(json.dumps(cases, ensure_ascii=False, indent=2))
    else:
        if not args.output:
            parser.error("--output is required for model evaluation")
        asyncio.run(evaluate(args, cases))
