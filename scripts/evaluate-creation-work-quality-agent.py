#!/usr/bin/env python3
"""One synthetic real Agent Loop run, with Core persistence acceptance.

Default mode only describes the request. --execute explicitly authorizes the
model run and synthetic history creation. The isolated Core database must be
supplied, must exist under /tmp, and is inspected read-only for this session.
No existing session, personal source, external URL, or embedding is used.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


FACT = "素材需求通过群消息交接，需求内容和完成状态散落在消息中。"
INSTRUCTION = """请为下述纯合成场景写一份约300至500字的内部评审决策片段，使用Markdown标题和自然段，读者是参与素材交接的产品与运营团队。
唯一给定事实：素材需求通过群消息交接，需求内容和完成状态散落在消息中。
本次需要决定是否同意试行共享需求清单。请建议先验证共享清单而暂缓系统开发，说明这一取舍是为了确认等待是否来自信息缺失，避免原因未明确时投入开发。
以下是允许提出的设计建议，并非已确认制度或事实：清单记录交付内容、验收要求和当前状态；发起需求时补齐验收要求，接收方确认下一步，状态变化时更新。启动前由相关团队确认参与范围、维护角色、记录权限及比较口径；拟记录补问内容和等待原因，与当前消息交接方式比较。若关键验收要求仍不明确则调整字段；若记录负担高且没有减少补问则暂停扩大试点并重新判断原因。
结尾提出本轮具体待决定事项：是否同意验证这条路径，以及由谁确认试点范围和记录条件。人员、日期、费用、容量和效果均未提供，只说明启动前如何确认，不编造或承诺。自然区分当前事实与未来建议，不要求逐句添加标签，不添加全篇方案栏目或复杂实施规范。
只使用以上输入，不检索个人记忆、历史资料、数据库、互联网或网页，不调用资料检索工具；不要要求补充材料，这些输入足以形成上述设计建议。"""
WORK_CONTEXT = {"schema_version": "creation.work-context.v1", "profile_id": "proposal_review",
    "purpose": "内部方案评审中的短决策片段", "decision_goal": "是否同意开展共享需求清单试点",
    "reader_context": "参与素材交接的产品与运营团队",
    "constraints": "约300至500字；不承诺未确认人员、日期、费用、容量或效果；仅使用给定唯一事实与明确允许的设计建议",
    "style_samples": [], "review_enabled": True}
TERMINALS = frozenset(("run.completed", "run.failed", "run.paused"))


def body_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_value(value, default):
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def actual_operation(database, session_id, history_id):
    """Query only this new synthetic session, never list other history rows."""
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        history = connection.execute("SELECT id, session_id, generated_content, lifecycle_status, revision_no, "
            "agent_trace_json, creation_brief_json FROM creation_history WHERE id=? AND session_id=?",
            (history_id, session_id)).fetchone()
        operations = connection.execute("SELECT operation_id, history_id, status, result_json, checkpoint_json, "
            "last_sequence FROM creation_operations WHERE session_id=? ORDER BY created_at",
            (session_id,)).fetchall()
    if history is None:
        raise AssertionError("Synthetic history was not found in the explicitly supplied isolated Core database")
    persisted = dict(history)
    trace = json_value(persisted.pop("agent_trace_json"), [])
    brief = json_value(persisted.pop("creation_brief_json"), {})
    persisted["document_sha256"] = body_hash(persisted["generated_content"])
    persisted["trace_terminal_events"] = [event for event in trace if event.get("type") in TERMINALS]
    persisted["work_context"] = (brief or {}).get("work_context")
    operation_records = []
    for row in operations:
        record = dict(row)
        result = json_value(record.pop("result_json"), {})
        checkpoint = json_value(record.pop("checkpoint_json"), {})
        record["result_event"] = result
        record["result_document_sha256"] = body_hash((result.get("data") or {}).get("document") or "")
        record["work_quality_review"] = (checkpoint.get("environment") or {}).get("work_quality_review")
        operation_records.append(record)
    return {"history": persisted, "operations": operation_records}


def acceptance(events, history, persisted, expected_work_ids):
    terminal = next((event for event in reversed(events) if event.get("type") in TERMINALS), None)
    data = (terminal or {}).get("data") or {}
    document = data.get("document") or ""
    reviews = [(event.get("data") or {}).get("review") or {} for event in events
        if event.get("type") == "delivery.checked"]
    final_review = data.get("delivery_review") or {}
    work_checks = [item for item in final_review.get("checks", []) if item.get("id") in expected_work_ids]
    operations = persisted["operations"]
    committed_id = data.get("committed_operation_id")
    operation = next((item for item in operations if item["operation_id"] == committed_id), None)
    semantic = (operation or {}).get("work_quality_review") or {}
    # Request availability does not prove absence of retrieval. Inspect actual
    # executed tool lifecycle events and completion references as well.
    executed_tools = [event for event in events if (event.get("actor") or {}).get("kind") == "tool"
        and event.get("type") not in {"plan.updated", "tool.available"}]
    db_history = persisted["history"]
    invariants = {
        "successful_terminal": bool(terminal and terminal["type"] == "run.completed"),
        "terminal_delivery_pass": final_review.get("status") == "pass",
        "delivery_checked_pass": any(item.get("status") == "pass" for item in reviews),
        "configured_work_checks_all_pass": ({item.get("id") for item in work_checks} == set(expected_work_ids)
            and all(item.get("passed") is True for item in work_checks)),
        "nonempty_body": bool(document.strip()),
        "api_history_completed": history.get("lifecycle_status") == "completed",
        "database_history_completed": db_history["lifecycle_status"] == "completed",
        "api_body_matches_terminal": history.get("generated_content") == document,
        "database_body_matches_terminal": db_history["generated_content"] == document,
        "api_database_revisions_match_terminal": history.get("revision_no") == db_history["revision_no"] == data.get("revision_no"),
        "committed_operation_completed": bool(operation and operation["status"] == "completed"),
        "operation_result_body_hash_matches": bool(operation and operation["result_document_sha256"] == body_hash(document)),
        "persisted_semantic_report_pass": semantic.get("status") == "pass",
        "semantic_report_hash_matches_body": semantic.get("document_hash") == body_hash(document),
        "persisted_work_context_enabled": (db_history.get("work_context") or {}).get("review_enabled") is True,
        "no_tool_execution": not executed_tools,
        "no_recalled_references": not data.get("references") and history.get("reference_count", 0) == 0,
        "no_partial_completion": not data.get("incomplete") and not data.get("failed_steps"),
    }
    return {"invariants": invariants, "document": document, "document_sha256": body_hash(document),
        "document_chars": len(document),
        "requested_body_length_observation": {"requested_approximate_chinese_characters": [300, 500],
            "total_markdown_characters": len(document),
            "chinese_characters": sum("\u4e00" <= character <= "\u9fff" for character in document),
            "acceptance_boundary": "长度仅作为人工观察；不把Markdown总字符数作为额外质量硬门槛。"},
        "terminal": terminal, "delivery_checked_reviews": reviews,
        "work_checks": work_checks, "semantic_review": semantic, "executed_tool_events": executed_tools,
        "status": "pass" if all(invariants.values()) else "failed"}


class Evaluation:
    def __init__(self, args):
        parsed = urllib.parse.urlparse(args.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path not in {"", "/"}:
            raise ValueError("Evaluation requires a loopback Core root URL")
        self.args = args
        self.database = Path(args.database).resolve()
        if not self.database.is_file() or Path("/tmp").resolve() not in self.database.parents:
            raise ValueError("Supply an existing isolated Core database under /tmp")
        self.path = Path(args.report).resolve()
        if self.path.exists():
            raise ValueError("Use a new report filename; do not overwrite an earlier real evaluation")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.report = {"kind": "creation-work-quality-real-agent-synthetic-evaluation",
            "started_at": time.time(), "base_url": args.base_url, "isolated_core_database": str(self.database),
            "session_id": "synthetic-work-quality-" + uuid.uuid4().hex,
            "instruction_id": "synthetic-instruction-" + uuid.uuid4().hex,
            "sole_fact": FACT, "instruction": INSTRUCTION, "work_context": WORK_CONTEXT,
            "events": [], "status": "prepared",
            "evidence_boundary": "A single real synthetic workflow proves recorded transport/review/persistence behavior; it does not prove general semantic accuracy or actual business outcomes."}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")

    def request(self, route, payload=None):
        request = urllib.request.Request(self.args.base_url.rstrip("/") + route,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None,
            headers={"Content-Type": "application/json"})
        with self.opener.open(request, timeout=20) as response:
            return json.load(response)

    def run(self):
        saved = self.request("/api/creation/history/start", {"session_id": self.report["session_id"],
            "prompt": INSTRUCTION, "root_request": INSTRUCTION, "creation_mode": "direct",
            "creation_brief": {"work_context": WORK_CONTEXT}, "conversation": []})
        self.report["history_id"] = saved["id"]
        self.report["status"] = "running"
        self.save()
        payload = {"session_id": self.report["session_id"], "instruction_id": self.report["instruction_id"],
            "user_prompt": INSTRUCTION, "root_request": INSTRUCTION, "current_document": "",
            "creation_mode": "direct", "model_mode": "local", "confirmed": True,
            "work_context": WORK_CONTEXT, "enable_image_generation": False,
            "selected_skills": [], "available_skills": [], "explicit_skill_ids": []}
        request = urllib.request.Request(self.args.base_url.rstrip("/") + "/api/creation/agent/run",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.args.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:].strip())
                    self.report["events"].append(event)
                    if event.get("type") in {"delivery.checked", "run.completed", "run.failed", "run.paused", "agent.started", "tool.completed"}:
                        print(json.dumps({"event": event.get("type"), "summary": event.get("summary"),
                            "elapsed_seconds": round(time.monotonic() - started, 1)}, ensure_ascii=False), flush=True)
                    if not event.get("type", "").endswith(".delta"):
                        self.save()
        except Exception as error:
            self.report["transport_error"] = type(error).__name__ + ": " + str(error)
        finally:
            self.report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.save()
        history = self.request("/api/creation/history/{}".format(self.report["history_id"]))
        if history.get("session_id") != self.report["session_id"]:
            raise AssertionError("Core history crossed the new synthetic session boundary")
        persisted = actual_operation(self.database, self.report["session_id"], self.report["history_id"])
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / "shared/creation-work-quality/profiles.json").read_text(encoding="utf-8"))
        profile = next(item for item in config["profiles"] if item["id"] == WORK_CONTEXT["profile_id"])
        dimensions = {item["id"]: item for item in config["criteria"]}
        expected = [dimensions[key]["issue_code"] for key in profile["criteria"]]
        result = acceptance(self.report["events"], history, persisted, expected)
        self.report.update(result)
        self.report["persisted_history_api"] = history
        self.report["core_database_persistence"] = persisted
        self.report["completed_at"] = time.time()
        body_path = self.path.with_suffix(".md")
        body_path.write_text(result["document"], encoding="utf-8")
        self.report["body_artifact"] = str(body_path)
        self.save()
        print(json.dumps({"status": self.report["status"], "report": str(self.path),
            "failed_invariants": [key for key, value in result["invariants"].items() if not value]}, ensure_ascii=False), flush=True)
        return 0 if self.report["status"] == "pass" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:17071")
    parser.add_argument("--database", default="/tmp/mb-creation-quality-20260927/core.db")
    parser.add_argument("--report", required=True)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"status": "prepared_only_no_network", "instruction": INSTRUCTION,
            "work_context": WORK_CONTEXT, "base_url": args.base_url, "database": args.database,
            "report": args.report}, ensure_ascii=False, indent=2))
        return 0
    evaluator = Evaluation(args)
    try:
        return evaluator.run()
    except Exception as error:
        evaluator.report["status"] = "failed"
        evaluator.report["error"] = type(error).__name__ + ": " + str(error)
        evaluator.save()
        print(evaluator.report["error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
