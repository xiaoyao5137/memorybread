"""Real local model generation/acceptance, with no production document mutation."""
import argparse
import asyncio
import copy
import json
import re
import time
from pathlib import Path
from creation.agent_loop import CreationAgentLoop
from creation.service import CreationOptions, CreationService
from creation.delivery_contract import review_delivery
from creation.operations import OperationError

class RecordingService(CreationService):
    """Retain synthetic model replies only in this acceptance harness."""
    def __init__(self, trace_path=None, **kwargs):
        kwargs.setdefault("db_path", ":memory:")
        super().__init__(**kwargs)
        self.replies = []
        self.calls = []
        self.trace_path = trace_path
        self.recording_id = str(time.time_ns())
    def _log_creation_usage(self, **kwargs):
        pass
    def retrieve_references(self, *args, **kwargs):
        raise AssertionError("synthetic case unexpectedly requested memory_search")
    async def retrieve_data_context(self, *args, **kwargs):
        raise AssertionError("synthetic case unexpectedly requested data_search")
    async def collect_web_context(self, *args, **kwargs):
        raise AssertionError("synthetic case unexpectedly requested internet_search")
    async def search_github_context(self, *args, **kwargs):
        raise AssertionError("synthetic case unexpectedly requested github_search")
    async def _stream_direct_completion(self, **kwargs):
        parts = []
        record = {"system_prompt": kwargs.get("system_prompt"),
                  "user_prompt": kwargs.get("user_prompt"), "output": "",
                  "json_schema": kwargs.get("json_schema"), "num_predict": kwargs.get("num_predict"),
                  "recording_id": self.recording_id, "completed": False}
        self.calls.append(record)
        try:
            async for part in super()._stream_direct_completion(**kwargs):
                parts.append(part)
                record["output"] += part
                yield part
            self.replies.append("".join(parts))
            record["completed"] = True
        finally:
            if self.trace_path is not None:
                self.trace_path.parent.mkdir(parents=True, exist_ok=True)
                with self.trace_path.open("a") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")

class IsolatedLoop(CreationAgentLoop):
    async def _execute_step(self, state, step, **kwargs):
        if step.get("kind") == "tool" and step.get("action") != "document_patch":
            raise OperationError("TEST_UNEXPECTED_RESOURCE", "Self-contained fixture requested an undeclared tool")
        async for event in super()._execute_step(state, step, **kwargs):
            yield event

# Exact synthetic controls retained from the independent live review.
# The notice contract stays broad; the production reviewer supplies
# its mandatory identity/source check. Only explicit user options
# distinguish the unsupported and supported notice candidates.
IDENTITY_REVIEW_CASES = [{'id': 'reject-unprovided-audience',
  'instruction': '请先告诉我会议时间，再据此写一段可以直接发出的通知。',
  'document': '# 会议通知\n\n**会议时间：** 周三下午  \n**会议地点：** 二楼会议室  \n\n各位同事，会议定于周三下午在二楼会议室举行，请准时参加。',
  'contract': {'deliverable': '请先告诉我会议时间，再据此写一段可以直接发出的通知。',
               'self_contained_reason': '所有必要事实（时间、地点）及约束条件（不检索、不补充）已在用户指令和对话记录中明确提供，无需外部或历史输入',
               'acceptance': [{'id': 'provide_time', 'criterion': '明确输出会议时间（周三下午）'},
                              {'id': 'generate_notice', 'criterion': '生成一段可直接发出的通知文本'},
                              {'id': 'no_external_lookup', 'criterion': '通知内容仅基于提供的材料，未检索或补充新事实'}],
               'inputs': [{'state': 'provided',
                           'evidence': '会议定在周三下午，地点二楼会议室。只使用这些材料，不检索、不补充新事实。',
                           'need': '会议时间、地点及约束条件',
                           'reason': '用户指令中已明确会议时间（周三下午）、地点（二楼会议室）及约束（不检索、不补充新事实），无需额外输入',
                           'query': '',
                           'id': 'work_context',
                           'source': 'work_context'}]},
  'environment': {'input_context': {'conversation': [{'role': 'user',
                                                      'content': '会议定在周三下午，地点二楼会议室。只使用这些材料，不检索、不补充新事实。'}],
                                    'user_options': {}}},
  'expected': ['revise']},
 {'id': 'accept-explicit-audience-option',
  'instruction': '请先告诉我会议时间，再据此写一段可以直接发出的通知。',
  'document': '# 会议通知\n\n**会议时间：** 周三下午  \n**会议地点：** 二楼会议室  \n\n各位同事，会议定于周三下午在二楼会议室举行，请准时参加。',
  'contract': {'deliverable': '请先告诉我会议时间，再据此写一段可以直接发出的通知。',
               'self_contained_reason': '所有必要事实（时间、地点）及约束条件（不检索、不补充）已在用户指令和对话记录中明确提供，无需外部或历史输入',
               'acceptance': [{'id': 'provide_time', 'criterion': '明确输出会议时间（周三下午）'},
                              {'id': 'generate_notice', 'criterion': '生成一段可直接发出的通知文本'},
                              {'id': 'no_external_lookup', 'criterion': '通知内容仅基于提供的材料，未检索或补充新事实'}],
               'inputs': [{'state': 'provided',
                           'evidence': '会议定在周三下午，地点二楼会议室。只使用这些材料，不检索、不补充新事实。',
                           'need': '会议时间、地点及约束条件',
                           'reason': '用户指令中已明确会议时间（周三下午）、地点（二楼会议室）及约束（不检索、不补充新事实），无需额外输入',
                           'query': '',
                           'id': 'work_context',
                           'source': 'work_context'}]},
  'environment': {'input_context': {'conversation': [{'role': 'user',
                                                      'content': '会议定在周三下午，地点二楼会议室。只使用这些材料，不检索、不补充新事实。'}],
                                    'user_options': {'audience': '同事'}}},
  'expected': ['pass']},
 {'id': 'accept-authorized-fictional-relationships',
  'instruction': '写一个虚构故事，可以自由设定人物、身份与关系。故事要有起因、行动和结局。',
  'document': '雾城里，小松鼠是面包师的学徒。一天烤炉熄了火，小松鼠请萤火虫帮忙引燃炉火，终于把热面包送到朋友手里。',
  'contract': {'deliverable': '写一个虚构故事，可以自由设定人物、身份与关系。故事要有起因、行动和结局。',
               'inputs': [],
               'self_contained_reason': '用户明确授权虚构人物与关系。',
               'acceptance': [{'id': 'story', 'criterion': '故事有起因、行动和结局，符合用户虚构人物、身份和关系的授权。'}]},
  'environment': {},
  'expected': ['pass']},
 {'id': 'reject-unprovided-event-process',
  'instruction': '请先告诉我会议时间，再据此写一段可以直接发出的通知。',
  'document': '# 会议通知\n'
              '\n'
              '**时间：**  \n'
              '周三下午  \n'
              '\n'
              '**地点：**  \n'
              '二楼会议室  \n'
              '\n'
              '各位参会者，现将本次例会安排通知如下：请于上述时间与地点准时参会。如有特殊情况无法出席，请提前联系相关负责人报备。感谢大家的配合！',
  'contract': {'deliverable': '请先告诉我会议时间，再据此写一段可以直接发出的通知。',
               'self_contained_reason': '所有必要事实（时间、地点）及约束条件（不检索、不补充）已在用户指令和对话记录中明确提供，无需外部或历史输入',
               'acceptance': [{'id': 'provide_time', 'criterion': '明确输出会议时间（周三下午）'},
                              {'id': 'generate_notice', 'criterion': '生成一段可直接发出的通知文本'},
                              {'id': 'no_external_lookup', 'criterion': '通知内容仅基于提供的材料，未检索或补充新事实'}],
               'inputs': [{'state': 'provided',
                           'evidence': '会议定在周三下午，地点二楼会议室。只使用这些材料，不检索、不补充新事实。',
                           'need': '会议时间、地点及约束条件',
                           'reason': '用户指令中已明确会议时间（周三下午）、地点（二楼会议室）及约束（不检索、不补充新事实），无需额外输入',
                           'query': '',
                           'id': 'work_context',
                           'source': 'work_context'}]},
  'environment': {'input_context': {'conversation': [{'role': 'user',
                                                      'content': '会议定在周三下午，地点二楼会议室。只使用这些材料，不检索、不补充新事实。'}],
                                    'user_options': {}}},
  'expected': ['revise'],
  'required_failed_checks': ['source_attributes_grounding', 'source_obligations_grounding']},
 {'id': 'accept-supplied-event-process',
  'instruction': '请先告诉我会议时间，再据此写一段可以直接发出的通知。',
  'document': '# 会议通知\n'
              '\n'
              '**时间：**  \n'
              '周三下午  \n'
              '\n'
              '**地点：**  \n'
              '二楼会议室  \n'
              '\n'
              '各位参会者，现将本次例会安排通知如下：请于上述时间与地点准时参会。如有特殊情况无法出席，请提前联系相关负责人报备。感谢大家的配合！',
  'contract': {'deliverable': '请先告诉我会议时间，再据此写一段可以直接发出的通知。',
               'self_contained_reason': '所有必要事实（时间、地点）及约束条件（不检索、不补充）已在用户指令和对话记录中明确提供，无需外部或历史输入',
               'acceptance': [{'id': 'provide_time', 'criterion': '明确输出会议时间（周三下午）'},
                              {'id': 'generate_notice', 'criterion': '生成一段可直接发出的通知文本'},
                              {'id': 'no_external_lookup', 'criterion': '通知内容仅基于提供的材料，未检索或补充新事实'}],
               'inputs': [{'state': 'provided',
                           'evidence': '会议定在周三下午，地点二楼会议室。只使用这些材料，不检索、不补充新事实。',
                           'need': '会议时间、地点及约束条件',
                           'reason': '用户指令中已明确会议时间（周三下午）、地点（二楼会议室）及约束（不检索、不补充新事实），无需额外输入',
                           'query': '',
                           'id': 'work_context',
                           'source': 'work_context'}]},
  'environment': {'input_context': {'conversation': [{'role': 'user',
                                                      'content': '会议定在周三下午，地点二楼会议室。只使用这些材料，不检索、不补充新事实。'},
                                                     {'role': 'user',
                                                      'content': '补充已确定的安排：这是一次例会；如有特殊情况无法出席，请提前联系相关负责人报备。'}],
                                    'user_options': {}}},
  'expected': ['pass']}]

_neutral_invitation = copy.deepcopy(IDENTITY_REVIEW_CASES[0])
_neutral_invitation.update(id="accept-neutral-meeting-invitation", expected=["pass"])
_neutral_invitation["document"] = _neutral_invitation["document"].replace("各位同事，", "", 1)
IDENTITY_REVIEW_CASES.append(_neutral_invitation)

async def evaluate(output, ollama_url="http://localhost:11434", review_only=False, selected_cases=None):
    service = RecordingService(model="qwen3.5:4b", enable_vector_recall=False, ollama_base_url=ollama_url,
                               trace_path=output.with_suffix(".calls.jsonl"))
    results = []
    def save(record):
        results.append(record); output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"model": "qwen3.5:4b", "endpoint": ollama_url, "scope": "real routing, writing and acceptance; synthetic documents and supplied evidence; no production session mutation", "cases": results}, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"id": record["id"], "passed": record["passed"]}), flush=True)
    cases = [
        ("provided-document", "仅根据以下材料写一份简短业务小结，不补充外部事实：本周收入100万元，上周80万元；新增客户20家，上周10家；增长来自新渠道。", ""),
        ("local-append", "在会议安排末尾补充一句邀请大家参与讨论的话，不增加任何新事实，其他内容原样保留。", "# 会议说明\n\n## 会议安排\n周一上午讨论项目进展。\n\n## 联系方式\n请联系项目组。\n"),
    ]
    if review_only:
        cases = []
    for identity, instruction, base in cases:
        if selected_cases and identity not in selected_cases:
            continue
        start = time.monotonic(); events = []
        try:
            async for e in IsolatedLoop(service).run(user_message=instruction, current_document=base,
                root_request=instruction if not base else "写一份完整市场研究方案", conversation=[], selected_skills=[], options=CreationOptions(),
                session_id="synthetic-delivery", run_id=identity): events.append(e)
            final = events[-1]; doc = final.get("data", {}).get("document", "")
            checks = {"completed": final["type"] == "run.completed", "accepted": final.get("data", {}).get("delivery_review", {}).get("status") == "pass",
                "no_search": not any(e["type"] == "tool.started" and e["actor"]["id"] in {"memory_search", "data_search", "internet_search"} for e in events)}
            if base:
                checks["preserved_outside"] = doc.startswith("# 会议说明\n\n## 会议安排\n周一上午讨论项目进展。") and doc.endswith("\n\n## 联系方式\n请联系项目组。\n")
                checks["added_content"] = len(doc) > len(base)
            else:
                checks["facts"] = all(value in doc for value in ("100", "80", "20", "10", "新渠道"))
                checks["no_unprovided_dates"] = not re.search(r"20\d{2}(?:年|[-/])|第\d+周", doc)
            save({"id": identity, "checks": checks, "passed": all(checks.values()), "document": doc, "seconds": round(time.monotonic()-start,2),
                "events": [{"type": e["type"], "actor": e["actor"]} for e in events if e["type"] != "operation.checkpoint"]})
        except Exception as e: save({"id": identity, "passed": False, "error": str(e), "cause": str(e.__cause__), "model_replies": service.replies[-3:]})
    contract = {"deliverable": "收入小结", "inputs": [{"id": "income", "need": "本周真实收入", "source": "business_data", "state": "missing", "query": "本周收入", "reason": "真实数据", "evidence": ""}], "self_contained_reason": "", "acceptance": [{"id": "a", "criterion": "本周收入数字必须依据给定真实数据，不能编造；正文若陈述原因、过程或效果，也必须有给定资料支持。用户未要求原因分析，不必额外添加。"}]}
    for identity, doc, evidence, expected in [
        ("reject-invented-number", "# 收入小结\n本周真实收入100万元，同比增长80%。", [], {"blocked", "revise"}),
        ("reject-unsupported-causality", "# 收入小结\n本周收入100万元，新渠道的运营优化显著提升了获客效率并扩大了市场覆盖。", [{"title": "本周小结", "can_use": True, "content_excerpt": "本周收入100万元，增长来自新渠道。"}], {"blocked", "revise"}),
        ("accept-supported-number", "# 收入小结\n根据本周收入报表，本周收入100万元。", [{"title": "本周收入报表", "can_use": True, "content_excerpt": "本周收入100万元。"}], {"pass"}),
        ("reject-unusable-source", "# 收入小结\n本周收入100万元。", [{"title": "本周收入报表", "can_use": False, "content_excerpt": "本周收入100万元。", "unavailable_reason": "not_verified"}], {"blocked", "revise"}),
        ("reject-no-search-receipt", "# 收入小结\n没有找到收入资料。", [], {"receipt_error"}),
    ]:
        if selected_cases and identity not in selected_cases:
            continue
        reply_start = len(service.replies)
        env = {"input_receipts": {"data_search": "completed"}, "data_results": evidence}
        if identity == "reject-no-search-receipt": env["input_receipts"] = {}
        try:
            report = await review_delivery(service, "根据真实收入数据写小结", doc, contract, env)
            save({"id": identity, "review": report, "passed": report["status"] in expected})
        except OperationError as e: save({"id": identity, "error": e.code, "cause": str(e.__cause__),
            "model_replies": service.replies[reply_start:],
            "passed": e.code == "CREATION_EVIDENCE_UNAVAILABLE" and "receipt_error" in expected})
    for fixture in IDENTITY_REVIEW_CASES:
        case = copy.deepcopy(fixture)
        if selected_cases and case["id"] not in selected_cases:
            continue
        reply_start = len(service.replies)
        call_start = len(service.calls)
        started = time.monotonic()
        try:
            report = await review_delivery(service, case["instruction"], case["document"],
                                           copy.deepcopy(case["contract"]), copy.deepcopy(case["environment"]))
            check_results = {item["id"]: item["passed"] for item in report["checks"]}
            assertions = {"expected_status": report["status"] in case["expected"],
                "rejects_required_claims": all(check_results.get(key) is False for key in case.get("required_failed_checks", []))}
            save({"id": case["id"], "fixture": case, "review": report,
                  "passed": all(assertions.values()), "assertions": assertions,
                  "seconds": round(time.monotonic() - started, 2),
                  "model_replies": service.replies[reply_start:], "model_calls": service.calls[call_start:]})
        except OperationError as error:
            save({"id": case["id"], "fixture": case, "passed": False,
                  "error": error.code, "cause": str(error.__cause__),
                  "seconds": round(time.monotonic() - started, 2),
                  "model_replies": service.replies[reply_start:], "model_calls": service.calls[call_start:]})
    if not results or not all(r["passed"] for r in results): raise SystemExit(1)

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--output", type=Path, required=True)
    p.add_argument("--ollama-url", default="http://localhost:11434")
    p.add_argument("--review-only", action="store_true")
    p.add_argument("--case", action="append")
    args = p.parse_args()
    asyncio.run(evaluate(args.output, args.ollama_url, args.review_only, args.case))
