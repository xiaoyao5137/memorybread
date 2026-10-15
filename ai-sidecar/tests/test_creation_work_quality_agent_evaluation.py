"""Ensure synthetic live acceptance cannot claim completion from SSE alone."""

import copy
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/evaluate-creation-work-quality-agent.py"
SPEC = importlib.util.spec_from_file_location("work_quality_agent_evaluation", SCRIPT)
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def complete_evidence():
    document = "# 合成决策片段\n\n" + "建议先验证已给定输入中的共享清单方案，再依据观察结果决定是否扩大。" * 10
    digest = evaluation.body_hash(document)
    review = {"status": "pass", "checks": [{"id": "work_decision_help", "passed": True,
        "reason": "本轮决策内容已说明。", "evidence": document.splitlines()[-1]}]}
    terminal = {"type": "run.completed", "data": {"document": document, "delivery_review": review,
        "committed_operation_id": "synthetic-operation", "revision_no": 2,
        "references": [], "failed_steps": []}}
    events = [{"type": "delivery.checked", "data": {"review": review}}, terminal]
    history = {"lifecycle_status": "completed", "generated_content": document, "revision_no": 2,
        "reference_count": 0}
    persisted = {"history": {**history, "work_context": {"review_enabled": True}},
        "operations": [{"operation_id": "synthetic-operation", "status": "completed",
            "result_document_sha256": digest, "work_quality_review": {"status": "pass", "document_hash": digest}}]}
    return events, history, persisted


def test_real_chain_acceptance_requires_both_terminal_and_database_evidence():
    events, history, persisted = complete_evidence()
    result = evaluation.acceptance(events, history, persisted, ["work_decision_help"])
    assert result["status"] == "pass"
    assert all(result["invariants"].values())
    assert result["document_sha256"] == evaluation.body_hash(history["generated_content"])


@pytest.mark.parametrize("mutation", ["database_body", "database_state", "operation_state", "semantic_hash", "missing_work_check", "partial"])
def test_sse_success_cannot_hide_persistence_or_review_failure(mutation):
    events, history, persisted = copy.deepcopy(complete_evidence())
    if mutation == "database_body":
        persisted["history"]["generated_content"] = "different persisted body"
    elif mutation == "database_state":
        persisted["history"]["lifecycle_status"] = "failed"
    elif mutation == "operation_state":
        persisted["operations"][0]["status"] = "partial"
    elif mutation == "semantic_hash":
        persisted["operations"][0]["work_quality_review"]["document_hash"] = "different body hash"
    elif mutation == "missing_work_check":
        events[-1]["data"]["delivery_review"]["checks"] = []
    elif mutation == "partial":
        events[-1]["data"]["incomplete"] = True
    result = evaluation.acceptance(events, history, persisted, ["work_decision_help"])
    assert result["status"] == "failed"


def test_actual_executed_source_tool_fails_synthetic_source_isolation_acceptance():
    events, history, persisted = complete_evidence()
    events.insert(0, {"type": "tool.completed", "actor": {"kind": "tool", "id": "memory_search"},
        "data": {"result_count": 0}})
    result = evaluation.acceptance(events, history, persisted, ["work_decision_help"])
    assert result["status"] == "failed"
    assert result["invariants"]["no_tool_execution"] is False


def test_paused_terminal_is_not_counted_as_complete_even_with_stale_passing_review():
    events, history, persisted = complete_evidence()
    events[-1]["type"] = "run.paused"
    result = evaluation.acceptance(events, history, persisted, ["work_decision_help"])
    assert result["status"] == "failed"
    assert result["invariants"]["successful_terminal"] is False
