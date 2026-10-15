#!/usr/bin/env python3
"""Exercise v2 against running local services using a new synthetic session.

This intentionally invokes the real configured local model. It never answers an
existing user session. A case root requires an isolated acceptance API and its
answers are simulated in that API's temporary database. --resume only accepts
an earlier report from this script.
Python 3.9 compatible; no third-party dependencies.
"""
import argparse
from datetime import date
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


LEGACY_ROOT_REQUEST = (
    "这是独立功能验收用的虚构创作：为社区读书角写一篇简短的活动介绍，"
    "只设两个章节，分别说明活动构想和参与方式，每章约150字。"
    "受众是第一次参加的普通居民，语气轻松。没有日期、预算、报名链接等真实事实，"
    "不要编造这些信息。不使用任何历史资料，不检索个人记忆，不联网。"
    "各阶段覆盖必要选择即可，避免同义重复。"
)
ROOT_REQUEST = LEGACY_ROOT_REQUEST.replace(
    "只设两个章节，分别说明活动构想和参与方式，每章约150字。",
    "正文包含活动构想和参与方式两节，每节约150字。")


def inspect_prepared_tree(questions, *, stage_id=None, preparation_contract_version=0,
                          content_mode=False):
    """Check old branches, atomic roots, or v17 flat content questions."""
    if not isinstance(questions, list) or any(
            not isinstance(question, dict) or not isinstance(question.get("id"), str)
            or not question["id"] for question in questions):
        raise AssertionError("Prepared tree has invalid questions")
    # Core omits content_mode from public stage responses. Version 17 is the
    # durable public signal for the flat content contract.
    flat_content = (stage_id in ("direction", "detail", "writing")
        and type(preparation_contract_version) is int
        and preparation_contract_version >= 17)
    if not questions:
        if not flat_content:
            raise AssertionError("Prepared tree has no questions")
        return {"root_question_count": 0, "max_depth": 0, "leaf_root_count": 0,
                "all_nonleaf_options_prepared": True}
    by_id = {question["id"]: question for question in questions}
    if len(by_id) != len(questions):
        raise AssertionError("Prepared tree has duplicate IDs or no questions")
    children = {}
    roots = []
    max_depth = 0
    for question in questions:
        options = question.get("options")
        if (not isinstance(options, list) or len(options) < 2
                or any(not isinstance(option, dict) or not isinstance(option.get("id"), str)
                    or not option["id"] for option in options)
                or len({option["id"] for option in options}) != len(options)):
            raise AssertionError("Prepared question has invalid options")
        parent_id = question.get("parent_question_id")
        parent_option_id = question.get("parent_option_id")
        if bool(parent_id) != bool(parent_option_id):
            raise AssertionError("Prepared tree has an incomplete parent link")
        if not parent_id:
            roots.append(question)
        else:
            parent = by_id.get(parent_id)
            if not parent or parent_option_id not in {
                    option["id"] for option in parent["options"]}:
                raise AssertionError("Prepared tree has an invalid parent option")
            children.setdefault(parent_id, set()).add(parent_option_id)
        visited = {question["id"]}
        while parent_id:
            if parent_id in visited:
                raise AssertionError("Prepared tree has a cycle")
            visited.add(parent_id)
            parent = by_id.get(parent_id)
            if parent is None:
                raise AssertionError("Prepared tree has a missing ancestor")
            parent_id = parent.get("parent_question_id")
        max_depth = max(max_depth, len(visited))
    for question in questions:
        branch_options = children.get(question["id"])
        if branch_options is not None and branch_options != {option["id"] for option in question["options"]}:
            raise AssertionError("Some conditional options were not prepared")
    if not roots:
        raise AssertionError("Prepared tree has no root questions")
    if flat_content and (len(roots) != len(questions) or children):
        raise AssertionError("Content questions must form a flat stage")
    terminal_roots_allowed = (flat_content or (stage_id in ("direction", "detail")
        and type(preparation_contract_version) is int and preparation_contract_version >= 15))
    if not terminal_roots_allowed and any(question["id"] not in children for question in roots):
        raise AssertionError("Root choices have no prepared drill-down questions")
    return {"root_question_count": len(roots), "max_depth": max_depth,
            "leaf_root_count": sum(question["id"] not in children for question in roots),
            "all_nonleaf_options_prepared": True}


def content_review_snapshot(questions, no_new_decision_reason=None):
    """Save actual candidate copy for human review, without a semantic pass claim."""
    return {"status": "human_review_required",
        "no_new_decision_reason": no_new_decision_reason, "questions": [
        {"id": question["id"], "prompt": question.get("prompt", ""),
         "why_now": question.get("why_now", ""),
         "context_details": question.get("context_details", ""),
         "parent_question_id": question.get("parent_question_id"),
         "parent_option_id": question.get("parent_option_id"),
         "options": [{"id": option["id"], "label": option.get("label", ""),
             "description": option.get("description", ""),
             "details": option.get("details", ""),
             "recommended": option.get("recommended", False)}
             for option in question["options"]]}
        for question in questions]}


class Evaluation:
    def __init__(self, args):
        self.args = args
        root_request_file = getattr(args, "root_request_file", None)
        if root_request_file:
            target = urllib.parse.urlsplit(args.base_url)
            if (target.scheme != "http" or target.hostname not in ("127.0.0.1", "localhost", "::1")
                    or target.port is None or target.port == 7070):
                raise ValueError("Case replay requires an isolated loopback acceptance API")
        self.path = Path(args.report).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        period_start = getattr(args, "confirmed_period_start", None)
        period_end = getattr(args, "confirmed_period_end", None)
        period_timezone = getattr(args, "confirmed_period_timezone", None)
        if bool(period_start) != bool(period_end):
            raise ValueError("Confirmed period requires both start and end")
        if period_start:
            if date.fromisoformat(period_start) > date.fromisoformat(period_end):
                raise ValueError("Confirmed period end precedes start")
            configured_period = {"start": period_start, "end": period_end,
                "timezone": period_timezone or "Asia/Shanghai"}
        else:
            configured_period = None
        if args.resume:
            self.report = json.loads(self.path.read_text(encoding="utf-8"))
            session_id = self.report.get("session_id")
            if (self.report.get("kind") != "brainstorm-stages-synthetic-evaluation"
                    or not isinstance(self.report.get("root_request"), str)
                    or not self.report["root_request"].strip()
                    or (not root_request_file and self.report["root_request"] not in
                        (ROOT_REQUEST, LEGACY_ROOT_REQUEST))
                    or not isinstance(session_id, str)
                    or not session_id.startswith("stages-eval-")):
                raise ValueError("Refusing to resume a non-evaluation session")
            if root_request_file and self.report["root_request"] != Path(
                    root_request_file).read_text(encoding="utf-8").strip():
                raise ValueError("Root request differs from the saved evaluation")
            saved_period = self.report.get("simulated_confirmed_period")
            if configured_period and configured_period != saved_period:
                raise ValueError("Confirmed period differs from the saved evaluation")
            self.simulated_confirmed_period = saved_period or configured_period
            try:
                parsed_session = uuid.UUID(session_id[len("stages-eval-"):])
            except ValueError:
                raise ValueError("Refusing to resume an invalid evaluation session") from None
            if session_id != "stages-eval-" + str(parsed_session):
                raise ValueError("Refusing to resume an invalid evaluation session")
            if self.report.get("failure"):
                self.report.setdefault("failures", []).append({
                    "message": self.report.pop("failure"), "at": self.report.get("updated_at")})
            self.report["status"] = "running"
            self.report["content_review_status"] = "human_review_required"
        else:
            root_request = (Path(root_request_file).read_text(encoding="utf-8").strip()
                            if root_request_file else ROOT_REQUEST)
            if not root_request:
                raise ValueError("Evaluation root request is empty")
            self.report = {
                "kind": "brainstorm-stages-synthetic-evaluation",
                "session_id": "stages-eval-" + str(uuid.uuid4()),
                "root_request": root_request,
                "answer_policy": "simulated_choices_source_gaps_not_user_confirmation",
                "simulated_confirmed_period": configured_period,
                "started_at": time.time(),
                "actions": [], "stages": {}, "chapters": [],
                "status": "running", "content_review_status": "human_review_required",
            }
            self.simulated_confirmed_period = configured_period
        self.state = None
        self.deadline = time.monotonic() + args.timeout

    def save(self):
        self.report["updated_at"] = time.time()
        if self.state:
            self.report["state"] = self.state
        self.path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")

    def request(self, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.args.base_url.rstrip("/") + path, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=190) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError("HTTP {}: {}".format(error.code, detail)) from error

    def turn(self, action, **fields):
        payload = {"session_id": self.report["session_id"],
                   "root_request": self.report["root_request"],
                   "workflow_version": 2, "action": action}
        if self.state:
            payload["revision"] = self.state["revision"]
        payload.update(fields)
        started = time.monotonic()
        state = self.request("/api/creation/brainstorm/turn", payload)
        if state.get("session_id") != self.report["session_id"]:
            raise AssertionError("Response crossed session boundary")
        self.state = state
        self.report["actions"].append({"action": action,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "revision": state["revision"], "phase": state["phase"],
            "question_id": fields.get("question_id"), "chapter_id": fields.get("chapter_id"),
            "stage": (state.get("stage_workflow") or {}).get("active_stage"),
            "simulated_answer": action == "answer",
            "selected_option_ids": (fields.get("answer") or {}).get("selected_option_ids", [])})
        self.save()
        return state

    def select_simulated_option(self, question):
        options = question["options"]
        marker = question.get("input_clarification") or {}
        if marker.get("kind") == "source_confirmation":
            gaps = [option for option in options
                if (option.get("input_value") or {}).get("action") == "draft_with_gap"]
            if len(gaps) != 1:
                raise AssertionError("Source confirmation has no unique safe gap option")
            return gaps[0]
        if marker.get("kind") == "report_period" and self.simulated_confirmed_period:
            matches = [option for option in options
                if all((option.get("input_value") or {}).get(key) == value
                    for key, value in self.simulated_confirmed_period.items())]
            if len(matches) != 1:
                raise AssertionError("Confirmed period is absent from current options")
            return matches[0]
        return next((option for option in options if option.get("recommended")), options[0])

    def run_chapter(self, chapter):
        chapter_id = chapter["id"]
        if chapter["status"] == "ready":
            self.turn("confirm_chapter", chapter_id=chapter_id)
            chapter = next(c for c in self.state["stage_workflow"]["chapters"] if c["id"] == chapter_id)
        instruction_id = chapter.get("instruction_id")
        if not instruction_id:
            raise AssertionError("Confirmed chapter has no stable instruction ID")
        history_before = self.request("/api/creation/history/{}".format(self.report["history_id"]))
        previous = history_before.get("generated_content") or ""
        payload = {
            "session_id": self.report["session_id"], "instruction_id": instruction_id,
            "brainstorm_chapter_id": chapter_id,
            "user_prompt": "请按已经确认的本章设计完成当前章节，保留此前已完成正文。",
            "root_request": self.report["root_request"], "current_document": previous,
            "creation_mode": "brainstorm", "creation_brief": self.state,
            "model_mode": "local", "enable_rag": False, "enable_web_search": False,
            "enable_image_generation": False, "browser_extension_enabled": False,
            "enabled_tools": [], "selected_skills": [], "confirmed": True,
        }
        req = urllib.request.Request(self.args.base_url.rstrip("/") + "/api/creation/agent/run",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
        started = time.monotonic()
        events = []
        try:
            with self.opener.open(req, timeout=1200) as response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if line.startswith("data:"):
                        event = json.loads(line[5:].strip())
                        events.append(event)
                        if event.get("type") in ("delivery.checked", "run.completed", "run.failed", "run.paused"):
                            print(json.dumps({"chapter": chapter_id, "event": event.get("type"),
                                "status": event.get("status"), "elapsed": round(time.monotonic() - started)},
                                ensure_ascii=False), flush=True)
        finally:
            self.report["chapters"].append({"id": chapter_id, "instruction_id": instruction_id,
                "elapsed_seconds": round(time.monotonic() - started, 3), "events": events})
            self.save()
        terminal = next((e for e in reversed(events) if e.get("type") in
                        ("run.completed", "run.failed", "run.paused")), None)
        if not terminal or terminal["type"] != "run.completed":
            raise AssertionError("Chapter has no successful Agent Loop terminal event")
        document = (terminal.get("data") or {}).get("document") or ""
        review = (terminal.get("data") or {}).get("delivery_review")
        reviews = [(e.get("data") or {}).get("review") for e in events if e.get("type") == "delivery.checked"]
        if not ((review or {}).get("status") == "pass" and any((r or {}).get("status") == "pass" for r in reviews)):
            raise AssertionError("Chapter requires passing review in both delivery.checked and run.completed")
        history = self.request("/api/creation/history/{}".format(self.report["history_id"]))
        persisted = history.get("generated_content") or ""
        if not document.strip() or persisted != document:
            raise AssertionError("Terminal document differs from persisted body")
        if previous and not document.startswith(previous.rstrip()):
            raise AssertionError("Previously completed body changed")
        self.report["chapters"][-1].update({"document_sha256": hashlib.sha256(document.encode()).hexdigest(),
            "persisted_body_matches": True, "previous_body_preserved": True,
            "delivery_pass": True, "document_chars": len(document)})
        self.turn("chapter_completed", chapter_id=chapter_id)

    def run(self):
        if self.report.get("history_id"):
            # A persisted report can outlive an intentionally temporary API host.
            # Never silently attach its history id to an unrelated new database.
            prior = self.request("/api/creation/history/{}".format(self.report["history_id"]))
            if prior.get("session_id") != self.report["session_id"]:
                raise AssertionError("Evaluation history belongs to another session")
        if not self.report.get("history_id"):
            saved = self.request("/api/creation/history/start", {
                "session_id": self.report["session_id"], "prompt": self.report["root_request"],
                "root_request": self.report["root_request"], "creation_mode": "brainstorm",
                "conversation": [{"id": "synthetic-root", "role": "user",
                    "content": self.report["root_request"], "createdAt": int(time.time() * 1000)}],
            })
            self.report["history_id"] = saved["id"]
        self.turn("start")
        if self.args.retry_failed_stage:
            workflow = self.state.get("stage_workflow") or {}
            current = next((s for s in workflow.get("stages", [])
                            if s["id"] == workflow.get("active_stage")), {})
            if current.get("status") == "failed":
                self.turn("retry_stage")
        last_progress = None
        while time.monotonic() < self.deadline:
            workflow = self.state.get("stage_workflow")
            if not workflow or workflow.get("version") != 2:
                raise AssertionError("Running Core did not activate workflow v2")
            stage = next(s for s in workflow["stages"] if s["id"] == workflow["active_stage"])
            progress = stage["progress"]
            signature = (stage["id"], stage["status"], progress["completed_batches"], progress["total_batches"])
            if signature != last_progress:
                print(json.dumps({"stage": stage["id"], "status": stage["status"],
                                  "progress": progress}, ensure_ascii=False), flush=True)
                last_progress = signature
            if stage["status"] == "failed":
                raise AssertionError("Stage failed: " + json.dumps(stage.get("error"), ensure_ascii=False))
            if stage["status"] == "thinking":
                if self.state.get("current_question"):
                    raise AssertionError("Incomplete stage exposed a question")
                time.sleep(self.args.poll_seconds)
                self.turn("status")
                continue
            if self.state["phase"] == "completed":
                if any(chapter["status"] != "completed" for chapter in workflow["chapters"]):
                    raise AssertionError("Workflow completed before all chapters passed delivery")
                for prepared in workflow["stages"]:
                    self.report["stages"][prepared["id"]] = self.stage_snapshot(prepared)
                self.report["status"] = "completed"
                self.save()
                return
            question = self.state.get("current_question")
            # Detail input preflight can expose an explicit period/source
            # confirmation before its content-question batch has run. It is
            # answerable at 0%, while ordinary stage questions require 100%.
            preflight_question = (stage["id"] == "detail" and question
                and question.get("input_clarification"))
            if progress["percent"] != 100 and not preflight_question:
                raise AssertionError("Answerable stage not fully prepared")
            if progress["percent"] == 100:
                self.report["stages"][stage["id"]] = self.stage_snapshot(stage)
            if question:
                preferred = self.select_simulated_option(question)
                self.turn("answer", question_id=question["id"],
                          answer={"selected_option_ids": [preferred["id"]], "custom_text": "", "source": "user"})
                continue
            if stage["id"] != "writing":
                self.turn("confirm_stage")
                continue
            chapter_id = workflow.get("active_chapter_id")
            chapter = next((c for c in workflow["chapters"] if c["id"] == chapter_id), None)
            if not chapter:
                raise AssertionError("Writing phase has no active chapter")
            if not self.args.write_chapters:
                self.report["status"] = "stages_prepared_chapter_ready"
                self.save()
                return
            self.run_chapter(chapter)
        raise TimeoutError("Synthetic evaluation timed out; report can be resumed")

    @staticmethod
    def stage_snapshot(stage):
        questions = stage["questions"]
        return {"question_count": len(questions),
            "batches": stage["progress"]["completed_batches"],
            "generation_id": stage["generation_id"], "prepared_before_answer": True,
            **inspect_prepared_tree(questions, stage_id=stage["id"],
                preparation_contract_version=stage.get("preparation_contract_version", 0)),
            "content_review": content_review_snapshot(questions,
                stage.get("content_no_new_decision_reason"))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:7070")
    parser.add_argument("--report", required=True)
    parser.add_argument("--root-request-file", help="UTF-8 task text for an isolated case replay")
    parser.add_argument("--confirmed-period-start", help="Previously user-confirmed report period start")
    parser.add_argument("--confirmed-period-end", help="Previously user-confirmed report period end")
    parser.add_argument("--confirmed-period-timezone", help="Previously user-confirmed timezone")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failed-stage", action="store_true")
    parser.add_argument("--write-chapters", action="store_true")
    parser.add_argument("--timeout", type=int, default=5400)
    parser.add_argument("--poll-seconds", type=float, default=3)
    args = parser.parse_args()
    evaluation = Evaluation(args)
    try:
        evaluation.run()
    except KeyboardInterrupt:
        evaluation.report["status"] = "interrupted"
        evaluation.report["failure"] = "Evaluation interrupted; saved events and the same operation can be resumed"
        evaluation.save()
        raise
    except Exception as error:
        evaluation.report["status"] = "failed"
        evaluation.report["failure"] = str(error)
        evaluation.save()
        raise
    print(json.dumps({"status": evaluation.report["status"], "report": str(evaluation.path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
