import csv
import json
import sys
from pathlib import Path

SCRIPTS_PATH = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_PATH))

from score_temporal_evidence_manual_review import cohen_kappa, score  # noqa: E402
from temporal_evidence_stage_a import (  # noqa: E402
    SCHEMA_VERSION,
    _extract_json_payload,
    baseline_decision,
    evaluate_corpus,
    load_corpus,
    prototype_decision,
    validate_candidate,
    validate_corpus,
)

CORPUS_PATH = Path(__file__).parent / "fixtures" / "temporal_evidence_stage_a.jsonl"


def _cases():
    return load_corpus(CORPUS_PATH)


def test_stage_a_corpus_is_frozen_balanced_and_large_enough():
    report = validate_corpus(_cases())

    assert report["valid"] is True
    assert report["errors"] == []
    assert report["total"] == 360
    assert report["split_counts"] == {"development": 240, "holdout": 120}
    assert set(report["source_counts"].values()) == {60}
    assert len(report["scenario_family_counts"]) == 30
    assert set(report["scenario_family_counts"].values()) == {12}


def test_stage_a_fixture_uses_only_synthetic_projects():
    raw = CORPUS_PATH.read_text(encoding="utf-8")

    assert "示例项目" in raw
    for forbidden in ("TurborDiffusion", "Cache Dit", "3700", "kuaishou.com"):
        assert forbidden not in raw


def test_manual_review_sample_is_stratified_and_has_no_answers():
    review_path = (
        Path(__file__).parents[2]
        / "doc"
        / "temporal-evidence-stage-a"
        / "manual-review-sample.csv"
    )
    with review_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 60
    assert all(not row["decision"] and not row["annotator"] for row in rows)


def test_manual_review_scorer_reports_kappa_without_source_text():
    left = {
        "a": {"decision": "eligible", "status": "completed"},
        "b": {"decision": "background_only", "status": "planned"},
    }
    right = {
        "a": {"decision": "eligible", "status": "completed"},
        "b": {"decision": "background_only", "status": "planned"},
    }

    result, complete = score(left, right)

    assert complete is True
    assert result["passes_recommended_gate"] is True
    assert result["fields"]["decision"]["cohen_kappa"] == 1.0
    assert "source_text" not in json.dumps(result)
    assert cohen_kappa(["a", "b"], ["a", "a"]) == 0.0


def test_document_level_baseline_exposes_mixed_date_failure():
    case = next(
        item
        for item in _cases()
        if item["scenario_family"] == "revision_date_distracts_historical_event"
    )

    assert case["expected"]["decision"] == "background_only"
    assert baseline_decision(case)["decision"] == "eligible"


def test_claim_gate_accepts_old_document_with_real_in_period_event():
    case = next(
        item
        for item in _cases()
        if item["scenario_family"] == "old_document_contains_new_event"
    )

    assert prototype_decision(case)["decision"] == "eligible"


def test_claim_gate_rejects_hallucinated_quote_and_status_enhancement():
    cases = {
        item["scenario_family"]: item
        for item in _cases()
        if item["scenario_family"]
        in {"hallucinated_quote", "status_semantic_enhancement"}
    }

    hallucinated = prototype_decision(cases["hallucinated_quote"])
    enhanced = prototype_decision(cases["status_semantic_enhancement"])

    assert hallucinated["decision"] == "unresolved"
    assert "evidence_quote_not_grounded" in hallucinated["failures"]
    assert enhanced["decision"] == "unresolved"
    assert "status_stronger_than_evidence" in enhanced["failures"]


def test_observation_time_cannot_be_promoted_to_event_time():
    case = next(
        item
        for item in _cases()
        if item["scenario_family"] == "explicit_completed_in_period"
    )
    bad_candidate = dict(case["candidate"])
    bad_candidate["time_basis"] = "observation_time"

    valid, failures = validate_candidate(case, bad_candidate)

    assert valid is False
    assert "observation_promoted_to_event" in failures


def test_deterministic_stage_a_metrics_are_reproducible():
    report = evaluate_corpus(_cases())

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["baseline"]["accuracy"] == 0.4
    assert report["baseline"]["false_admissions"] == 156
    assert report["baseline"]["retention_rate"] == 0.888889
    assert report["prototype"]["accuracy"] == 1.0
    assert report["prototype"]["false_admissions"] == 0
    assert report["prototype"]["retention_rate"] == 1.0


def test_live_json_parser_accepts_markdown_fence_from_local_model():
    payload = _extract_json_payload('```json\n{"items": []}\n```')

    assert payload == {"items": []}


def test_shared_stage_a_schema_is_valid_json_and_draft_only():
    schema_path = (
        Path(__file__).parents[2]
        / "shared"
        / "temporal-evidence"
        / "temporal-evidence.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["properties"]["schema_version"]["const"] == (
        "memorybread.temporal-evidence.v1"
    )
