"""The synthetic stage evaluator follows current and protected tree shapes."""
import argparse
import importlib.util
import json
import uuid
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evaluate-brainstorm-stages.py"
SPEC = importlib.util.spec_from_file_location("evaluate_brainstorm_stages", SCRIPT)
EVALUATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVALUATOR)


def question(identifier, parent_id=None, parent_option_id=None):
    return {"id": identifier, "prompt": "具体如何安排？", "why_now": "决定实际安排。",
        "parent_question_id": parent_id, "parent_option_id": parent_option_id,
        "options": [{"id": "a", "label": "安排甲", "description": "采用甲办法。", "recommended": True},
                    {"id": "b", "label": "安排乙", "description": "采用乙办法。", "recommended": False}]}


def legacy_tree():
    return [question("root"), question("child_a", "root", "a"),
        question("child_b", "root", "b")]


def args(tmp_path, *, resume=False, root_request_file=None,
         base_url="http://127.0.0.1:7070", confirmed_period_start=None,
         confirmed_period_end=None, confirmed_period_timezone=None):
    return argparse.Namespace(report=str(tmp_path / "evaluation.json"), resume=resume,
        timeout=30, base_url=base_url, root_request_file=root_request_file,
        confirmed_period_start=confirmed_period_start,
        confirmed_period_end=confirmed_period_end,
        confirmed_period_timezone=confirmed_period_timezone,
        retry_failed_stage=False, poll_seconds=0, write_chapters=False)


def test_v16_direction_and_detail_accept_terminal_atomic_roots():
    roots = [question("first"), question("second")]
    for stage_id in ("direction", "detail"):
        result = EVALUATOR.inspect_prepared_tree(roots, stage_id=stage_id,
            preparation_contract_version=16)
        assert result == {"root_question_count": 2, "max_depth": 1,
            "leaf_root_count": 2, "all_nonleaf_options_prepared": True}


def test_protected_old_tree_and_writing_still_require_root_drill_down():
    tree = legacy_tree()
    for stage_id, version in (("direction", 14), ("detail", 14), ("writing", 16)):
        assert EVALUATOR.inspect_prepared_tree(tree, stage_id=stage_id,
            preparation_contract_version=version)["max_depth"] == 2
        with pytest.raises(AssertionError, match="Root choices have no prepared drill-down"):
            EVALUATOR.inspect_prepared_tree([question("root")], stage_id=stage_id,
                preparation_contract_version=version)


@pytest.mark.parametrize("stage_id", ["direction", "detail", "writing"])
def test_v17_content_stage_accepts_only_flat_questions_and_a_reviewable_empty_pool(stage_id):
    result = EVALUATOR.inspect_prepared_tree([question("first"), question("second")],
        stage_id=stage_id, preparation_contract_version=17)
    assert result["root_question_count"] == result["leaf_root_count"] == 2
    empty = EVALUATOR.inspect_prepared_tree([], stage_id=stage_id,
        preparation_contract_version=17)
    assert empty["root_question_count"] == empty["max_depth"] == 0
    with pytest.raises(AssertionError, match="flat stage"):
        EVALUATOR.inspect_prepared_tree(legacy_tree(), stage_id=stage_id,
            preparation_contract_version=17)
    with pytest.raises(AssertionError, match="no questions"):
        EVALUATOR.inspect_prepared_tree([], stage_id=stage_id,
            preparation_contract_version=16)


def test_v17_empty_stage_snapshot_keeps_no_decision_reason_for_human_review():
    stage = {"id": "writing", "status": "ready", "generation_id": "content-gen",
        "preparation_contract_version": 17, "content_mode": True,
        "content_no_new_decision_reason": "全部有效用户输入已足够成文。",
        "questions": [], "progress": {"completed_batches": 1, "total_batches": 1, "percent": 100}}
    snapshot = EVALUATOR.Evaluation.stage_snapshot(stage)
    assert snapshot["question_count"] == 0
    assert snapshot["content_review"] == {"status": "human_review_required",
        "no_new_decision_reason": "全部有效用户输入已足够成文。", "questions": []}
    # The public Core response hides content_mode but keeps the empty-pool reason.
    stage.pop("content_mode")
    public_snapshot = EVALUATOR.Evaluation.stage_snapshot(stage)
    assert public_snapshot["question_count"] == 0
    assert public_snapshot["content_review"]["no_new_decision_reason"] == "全部有效用户输入已足够成文。"


def test_new_contract_keeps_complete_branch_and_parent_checks():
    with pytest.raises(AssertionError, match="Some conditional options were not prepared"):
        EVALUATOR.inspect_prepared_tree(legacy_tree()[:2], stage_id="direction",
            preparation_contract_version=16)
    broken = legacy_tree()
    broken[2]["parent_option_id"] = "unknown"
    with pytest.raises(AssertionError, match="invalid parent option"):
        EVALUATOR.inspect_prepared_tree(broken, stage_id="direction",
            preparation_contract_version=16)


def test_content_snapshot_records_copy_without_claiming_semantic_acceptance():
    result = EVALUATOR.content_review_snapshot([question("first")])
    assert result["status"] == "human_review_required"
    assert result["questions"][0]["prompt"] == "具体如何安排？"
    assert [option["description"] for option in result["questions"][0]["options"]] == [
        "采用甲办法。", "采用乙办法。"]
    assert "pass" not in json.dumps(result)


@pytest.mark.parametrize("version,questions", [
    (16, legacy_tree()),
    (17, [question("flat")]),
])
def test_current_single_body_container_reaches_review_ready_without_network(
        tmp_path, version, questions):
    evaluation = EVALUATOR.Evaluation(args(tmp_path))
    stage = {"id": "writing", "status": "ready", "generation_id": "synthetic-gen",
        "preparation_contract_version": version, "questions": questions,
        "progress": {"completed_batches": 1, "total_batches": 1, "percent": 100}}
    state = {"session_id": evaluation.report["session_id"], "revision": 1,
        "phase": "ready", "current_question": None,
        "stage_workflow": {"version": 2, "active_stage": "writing", "stages": [stage],
            "active_chapter_id": "body", "chapters": [{"id": "body", "status": "ready"}]}}
    calls = []

    def fake_request(path, payload=None):
        calls.append((path, payload))
        assert path == "/api/creation/history/start"
        return {"id": 101}

    def fake_turn(action, **fields):
        assert action == "start" and not fields
        evaluation.state = state
        return state

    evaluation.request = fake_request
    evaluation.turn = fake_turn
    evaluation.run()
    assert evaluation.report["status"] == "stages_prepared_chapter_ready"
    assert evaluation.report["stages"]["writing"]["content_review"]["status"] == "human_review_required"
    assert calls[0][1]["root_request"] == EVALUATOR.ROOT_REQUEST


def test_detail_input_preflight_question_is_answered_before_content_batch_completes(tmp_path):
    evaluation = EVALUATOR.Evaluation(args(tmp_path,
        confirmed_period_start="2026-09-21", confirmed_period_end="2026-09-27",
        confirmed_period_timezone="Asia/Shanghai"))
    period_question = question("input_report_period")
    period_question["input_clarification"] = {"kind": "report_period", "need_ids": []}
    period_question["options"][0]["label"] = "最近完整一周（2026-09-21 至 2026-09-27）"
    period_question["options"][0]["input_value"] = {
        "start": "2026-09-21", "end": "2026-09-27", "timezone": "Asia/Shanghai"}
    period_question["options"][0]["recommended"] = False
    period_question["options"][1]["recommended"] = True
    period_question["options"][1]["input_value"] = {
        "start": "2026-09-28", "end": "2026-10-04", "timezone": "Asia/Shanghai"}
    preflight_stage = {"id": "detail", "status": "answering", "generation_id": "input-gen",
        "preparation_contract_version": 17, "questions": [period_question],
        "input_preflight": {"status": "awaiting_user"},
        "progress": {"completed_batches": 0, "total_batches": 0, "percent": 0}}
    writing_stage = {"id": "writing", "status": "ready", "generation_id": "content-gen",
        "preparation_contract_version": 17, "questions": [question("writing-flat")],
        "progress": {"completed_batches": 1, "total_batches": 1, "percent": 100}}
    preflight_state = {"session_id": evaluation.report["session_id"], "revision": 1,
        "phase": "exploring", "current_question": period_question,
        "stage_workflow": {"version": 2, "active_stage": "detail",
            "stages": [preflight_stage, writing_stage], "chapters": [],
            "active_chapter_id": None}}
    ready_state = {**preflight_state, "revision": 2, "current_question": None,
        "stage_workflow": {"version": 2, "active_stage": "writing",
            "stages": [preflight_stage, writing_stage],
            "chapters": [{"id": "body", "status": "ready"}],
            "active_chapter_id": "body"}}
    actions = []

    def fake_request(path, payload=None):
        assert path == "/api/creation/history/start"
        return {"id": 101}

    def fake_turn(action, **fields):
        actions.append((action, fields))
        evaluation.state = preflight_state if action == "start" else ready_state
        return evaluation.state

    evaluation.request = fake_request
    evaluation.turn = fake_turn
    evaluation.run()
    assert [action for action, _ in actions] == ["start", "answer"]
    assert actions[1][1]["question_id"] == "input_report_period"
    assert actions[1][1]["answer"]["selected_option_ids"] == ["a"]
    assert evaluation.report["status"] == "stages_prepared_chapter_ready"
    assert "detail" not in evaluation.report["stages"]


def test_simulated_source_confirmation_chooses_only_explicit_gap_option(tmp_path):
    evaluation = EVALUATOR.Evaluation(args(tmp_path))
    source = question("source")
    source["input_clarification"] = {"kind": "source_confirmation", "need_ids": ["need"]}
    source["options"][0]["input_value"] = {"action": "retry_local_recall"}
    source["options"][1]["input_value"] = {"action": "draft_with_gap"}
    assert source["options"][0]["recommended"] is True
    assert evaluation.select_simulated_option(source)["id"] == "b"

    source["options"][1]["input_value"] = {"action": "confirm_unverified_source"}
    with pytest.raises(AssertionError, match="no unique safe gap option"):
        evaluation.select_simulated_option(source)
    source["options"][0]["input_value"] = {"action": "draft_with_gap"}
    source["options"][1]["input_value"] = {"action": "draft_with_gap"}
    with pytest.raises(AssertionError, match="no unique safe gap option"):
        evaluation.select_simulated_option(source)


def test_simulated_period_requires_exact_saved_dates_and_timezone(tmp_path):
    evaluation = EVALUATOR.Evaluation(args(tmp_path,
        confirmed_period_start="2026-09-21", confirmed_period_end="2026-09-27",
        confirmed_period_timezone="Asia/Shanghai"))
    assert evaluation.report["simulated_confirmed_period"] == {
        "start": "2026-09-21", "end": "2026-09-27", "timezone": "Asia/Shanghai"}
    period = question("period")
    period["input_clarification"] = {"kind": "report_period", "need_ids": []}
    period["options"][0]["input_value"] = {
        "start": "2026-09-28", "end": "2026-10-04", "timezone": "Asia/Shanghai"}
    period["options"][1]["input_value"] = {
        "start": "2026-09-21", "end": "2026-09-27", "timezone": "Asia/Shanghai"}
    assert period["options"][0]["recommended"] is True
    assert evaluation.select_simulated_option(period)["id"] == "b"
    period["options"][1]["input_value"]["timezone"] = "UTC"
    with pytest.raises(AssertionError, match="absent from current options"):
        evaluation.select_simulated_option(period)

    evaluation.save()
    resumed = EVALUATOR.Evaluation(args(tmp_path, resume=True,
        confirmed_period_start="2026-09-21", confirmed_period_end="2026-09-27",
        confirmed_period_timezone="Asia/Shanghai"))
    assert resumed.simulated_confirmed_period == evaluation.report["simulated_confirmed_period"]
    with pytest.raises(ValueError, match="Confirmed period differs"):
        EVALUATOR.Evaluation(args(tmp_path, resume=True,
            confirmed_period_start="2026-09-28", confirmed_period_end="2026-10-04",
            confirmed_period_timezone="Asia/Shanghai"))


def test_action_report_marks_simulated_selection_without_network(tmp_path):
    evaluation = EVALUATOR.Evaluation(args(tmp_path))
    calls = []

    def fake_request(path, payload=None):
        calls.append((path, payload))
        return {"session_id": evaluation.report["session_id"], "revision": len(calls),
            "phase": "exploring", "stage_workflow": {"active_stage": "detail"}}

    evaluation.request = fake_request
    evaluation.turn("answer", question_id="source", answer={
        "selected_option_ids": ["gap"], "custom_text": "", "source": "user"})
    action = evaluation.report["actions"][-1]
    assert action["simulated_answer"] is True
    assert action["selected_option_ids"] == ["gap"]
    assert evaluation.report["answer_policy"] == "simulated_choices_source_gaps_not_user_confirmation"
    assert calls[0][1]["session_id"] == evaluation.report["session_id"]


def test_resume_requires_synthetic_session_identity_and_nonempty_request(tmp_path):
    settings = args(tmp_path, resume=True)
    report = {"kind": "brainstorm-stages-synthetic-evaluation",
        "session_id": "stages-eval-" + str(uuid.uuid4()),
        "root_request": EVALUATOR.LEGACY_ROOT_REQUEST}
    Path(settings.report).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    assert EVALUATOR.Evaluation(settings).report["root_request"] == EVALUATOR.LEGACY_ROOT_REQUEST
    # A case request is allowed only when the same root-request file is given.
    # Plain --resume remains limited to the two built-in synthetic prompts.
    for field, value in (("session_id", "creation-1234567890123-example"),
                         ("root_request", "用户真实创作请求"),
                         ("root_request", ""),
                         ("kind", "user-creation")):
        invalid = {**report, field: value}
        Path(settings.report).write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match="Refusing to resume"):
            EVALUATOR.Evaluation(settings)


def test_case_root_file_requires_isolated_api_and_marks_simulated_answers(tmp_path):
    root_file = tmp_path / "root.txt"
    root_file.write_text("请生成GPU成本优化的周报\n", encoding="utf-8")
    with pytest.raises(ValueError, match="isolated loopback acceptance API"):
        EVALUATOR.Evaluation(args(tmp_path, root_request_file=str(root_file)))
    assert not (tmp_path / "evaluation.json").exists()

    settings = args(tmp_path, root_request_file=str(root_file),
        base_url="http://127.0.0.1:17170")
    evaluation = EVALUATOR.Evaluation(settings)
    assert evaluation.report["root_request"] == "请生成GPU成本优化的周报"
    assert evaluation.report["session_id"].startswith("stages-eval-")
    uuid.UUID(evaluation.report["session_id"].removeprefix("stages-eval-"))
    assert evaluation.report["answer_policy"] == "simulated_choices_source_gaps_not_user_confirmation"
    assert evaluation.report["content_review_status"] == "human_review_required"


@pytest.mark.parametrize("base_url", [
    "http://localhost:7070", "http://[::1]:7070", "http://127.0.0.1:7070/",
    "http://192.0.2.10:17170", "https://127.0.0.1:17170",
])
def test_case_root_file_rejects_desktop_aliases_and_non_loopback_hosts(tmp_path, base_url):
    root_file = tmp_path / "root.txt"
    root_file.write_text("请生成GPU成本优化的周报", encoding="utf-8")
    with pytest.raises(ValueError, match="isolated loopback acceptance API"):
        EVALUATOR.Evaluation(args(tmp_path, root_request_file=str(root_file),
            base_url=base_url))
    assert not (tmp_path / "evaluation.json").exists()


def test_case_resume_rejects_changed_root_file_and_real_session_id(tmp_path):
    root_file = tmp_path / "root.txt"
    root_file.write_text("请生成GPU成本优化的周报", encoding="utf-8")
    settings = args(tmp_path, root_request_file=str(root_file),
        base_url="http://127.0.0.1:17170")
    evaluation = EVALUATOR.Evaluation(settings)
    evaluation.save()
    resume_settings = args(tmp_path, resume=True, root_request_file=str(root_file),
        base_url="http://127.0.0.1:17170")
    assert EVALUATOR.Evaluation(resume_settings).report["session_id"] == evaluation.report["session_id"]

    root_file.write_text("另一个请求", encoding="utf-8")
    with pytest.raises(ValueError, match="Root request differs"):
        EVALUATOR.Evaluation(resume_settings)
    root_file.write_text("请生成GPU成本优化的周报", encoding="utf-8")

    forged = {**evaluation.report, "session_id": "creation-1234567890123-example"}
    Path(settings.report).write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing to resume"):
        EVALUATOR.Evaluation(resume_settings)
