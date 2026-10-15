"""The v17 stage endpoint admits only reviewed, task-bound content packets."""

import copy
import json

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_content_questions import (
    compile_report_fact_fallback, normalize_content_seed,
)
from creation.brainstorm_stages import BrainstormStageCoordinator
from tests.test_brainstorm_content_questions import (
    BOOK_ROOT, GPU_PERIOD, GPU_ROOT, book_packet, gpu_packet, passing_review,
)
from tests.test_creation_brainstorm import StubCreationService


CONTEXT_ID = "a" * 64


@pytest.fixture(autouse=True)
def model_generation_path(monkeypatch):
    # Existing tests exercise model generation independently of the compiled
    # report profile. Tests that need the profile restore its real compiler.
    monkeypatch.setattr("creation.brainstorm_stages.compile_report_fact_fallback",
                        lambda **_: None)


def seed_from_packet(packet):
    question = packet["questions"][0]
    proof = packet["content_proofs"][0]
    return {"decisions": [{"anchor_quote": proof["anchor_quote"],
        "prompt": question["prompt"], "document_effect": proof["document_effect"],
        "options": [{"label": option["label"], "description": option["description"],
            "effect": proof["option_effects"][index]}
            for index, option in enumerate(question["options"])],
        "recommended_index": 0}], "no_new_decision_reason": ""}


def gpu_seed():
    return seed_from_packet(gpu_packet())


def expected_packet(stage, period=None):
    return normalize_content_seed(gpu_seed(), stage=stage, root_request=GPU_ROOT,
        decisions=[], brief_edits={}, confirmed_period=period)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["direction", "detail", "writing"])
async def test_content_stage_batch_returns_reviewed_questions_and_keeps_preflight_as_context(stage):
    packet = expected_packet(stage, GPU_PERIOD)
    service = StubCreationService([json.dumps(gpu_seed(), ensure_ascii=False),
                                   json.dumps(passing_review(packet), ensure_ascii=False)])
    preflight = {"status": "needs_user_confirmation", "candidates": ["source_only"]}
    result = await BrainstormStageCoordinator(service).stage_batch(
        stage=stage, kind="content_questions", root_request=GPU_ROOT,
        decisions=[], confirmed_period=GPU_PERIOD,
        context_id=CONTEXT_ID, plan={"input_preflight": preflight})
    assert result == packet
    assert len(service.model_calls) == 2
    context = json.loads(service.prompts[0])
    assert context["root_request"] == GPU_ROOT
    assert context["confirmed_period"] == GPU_PERIOD
    assert context["input_preflight"] == preflight
    assert context["max_questions"] == 1
    assert service.model_calls[1]["temperature"] == 0.0
    assert json.loads(service.prompts[1])["candidate"] == packet


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,decisions,period", [
    ("direction", [], None),
    ("detail", [{"dimension_id": "content_direction_1", "answer_source": "user",
                 "selected_options": [{"label": "费用与用量拆解"}]}], GPU_PERIOD),
    ("writing", [{"dimension_id": "content_detail_1", "answer_source": "user",
                  "selected_options": [{"label": "账单费用对照"}]}], GPU_PERIOD),
])
async def test_compiled_compute_cost_profile_requires_independent_review(
        monkeypatch, stage, decisions, period):
    monkeypatch.setattr("creation.brainstorm_stages.compile_report_fact_fallback",
                        compile_report_fact_fallback)
    root = "请生成GPU成本优化的周报"
    packet = compile_report_fact_fallback(stage=stage, root_request=root,
        decisions=decisions, brief_edits={}, confirmed_period=period)
    assert packet is not None
    service = StubCreationService([json.dumps(passing_review(packet), ensure_ascii=False)])
    result = await BrainstormStageCoordinator(service).stage_batch(
        stage=stage, kind="content_questions", root_request=root,
        decisions=decisions, confirmed_period=period, context_id=CONTEXT_ID)
    assert result == packet
    assert len(service.model_calls) == 1
    assert service.model_calls[0]["temperature"] == 0.0
    assert json.loads(service.prompts[0])["candidate"] == packet


@pytest.mark.asyncio
async def test_compiled_profile_rejection_does_not_publish_it(monkeypatch):
    monkeypatch.setattr("creation.brainstorm_stages.compile_report_fact_fallback",
                        compile_report_fact_fallback)
    root = "请生成GPU成本优化的周报"
    compiled = compile_report_fact_fallback(stage="direction", root_request=root,
        decisions=[], brief_edits={}, confirmed_period=None)
    rejected = passing_review(compiled)
    rejected["questions"][compiled["questions"][0]["id"]]["verdict"] = "generic"
    # The independently rejected compiled candidate is discarded; the model
    # may still produce a reviewed candidate within the bounded fallback path.
    packet = normalize_content_seed(gpu_seed(), stage="direction", root_request=root,
        decisions=[], brief_edits={}, confirmed_period=None)
    service = StubCreationService([json.dumps(rejected, ensure_ascii=False),
                                   json.dumps(gpu_seed(), ensure_ascii=False),
                                   json.dumps(passing_review(packet), ensure_ascii=False)])
    result = await BrainstormStageCoordinator(service).stage_batch(
        stage="direction", kind="content_questions", root_request=root,
        decisions=[], context_id=CONTEXT_ID)
    assert result == packet
    assert len(service.model_calls) == 3
    assert "领域候选未通过独立审查" in service.prompts[1]


@pytest.mark.asyncio
async def test_content_generation_and_review_use_current_edited_root_only():
    edits = {"root_request": BOOK_ROOT}
    seed = seed_from_packet(book_packet())
    packet = normalize_content_seed(seed, stage="direction", root_request=GPU_ROOT,
        decisions=[], brief_edits=edits, confirmed_period=None)
    service = StubCreationService([json.dumps(seed, ensure_ascii=False),
                                   json.dumps(passing_review(packet), ensure_ascii=False)])
    result = await BrainstormStageCoordinator(service).stage_batch(
        stage="direction", kind="content_questions", root_request=GPU_ROOT,
        decisions=[], brief_edits=edits, context_id=CONTEXT_ID)
    assert result == packet
    for prompt in service.prompts:
        context = json.loads(prompt)
        assert context["root_request"] == BOOK_ROOT
        assert GPU_ROOT not in prompt
    assert json.loads(service.prompts[1])["brief_edits"] == edits


@pytest.mark.asyncio
async def test_content_stage_batch_retries_rejected_semantics_without_partial_publish():
    packet = expected_packet("direction")
    rejected = copy.deepcopy(passing_review(packet))
    rejected["questions"][packet["questions"][0]["id"]]["verdict"] = "generic"
    service = StubCreationService([json.dumps(gpu_seed(), ensure_ascii=False),
                                   json.dumps(rejected, ensure_ascii=False),
                                   json.dumps(gpu_seed(), ensure_ascii=False),
                                   json.dumps(passing_review(packet), ensure_ascii=False)])
    result = await BrainstormStageCoordinator(service).stage_batch(
        stage="direction", kind="content_questions", root_request=GPU_ROOT,
        decisions=[], context_id=CONTEXT_ID)
    assert result == packet
    assert len(service.model_calls) == 4
    assert "上次候选被拒绝" in service.prompts[2]
    assert "任意任务" in service.prompts[2]


@pytest.mark.asyncio
async def test_content_review_rejection_identifies_unverified_facts_for_retry():
    packet = expected_packet("direction")
    rejected = copy.deepcopy(passing_review(packet))
    rejected["questions"][packet["questions"][0]["id"]]["options"]["choice_1"]["verdict"] = "unsupported_fact"
    service = StubCreationService([json.dumps(gpu_seed(), ensure_ascii=False),
                                   json.dumps(rejected, ensure_ascii=False),
                                   json.dumps(gpu_seed(), ensure_ascii=False),
                                   json.dumps(passing_review(packet), ensure_ascii=False)])
    result = await BrainstormStageCoordinator(service).stage_batch(
        stage="direction", kind="content_questions", root_request=GPU_ROOT,
        decisions=[], context_id=CONTEXT_ID)
    assert result == packet
    assert "未核实的资料" in service.prompts[2]


@pytest.mark.asyncio
async def test_content_stage_batch_fails_closed_when_every_candidate_is_generic():
    packet = expected_packet("direction")
    rejected = copy.deepcopy(passing_review(packet))
    rejected["questions"][packet["questions"][0]["id"]]["verdict"] = "generic"
    service = StubCreationService([json.dumps(gpu_seed(), ensure_ascii=False),
                                   json.dumps(rejected, ensure_ascii=False)]
                                  * BrainstormStageCoordinator.MAX_GENERATION_ATTEMPTS)
    with pytest.raises(BrainstormGenerationError, match="已保留原有创作状态"):
        await BrainstormStageCoordinator(service).stage_batch(
            stage="direction", kind="content_questions", root_request=GPU_ROOT,
            decisions=[], context_id=CONTEXT_ID)
    assert len(service.model_calls) == 2 * BrainstormStageCoordinator.MAX_GENERATION_ATTEMPTS


@pytest.mark.asyncio
async def test_content_stage_batch_rejects_wrong_context_and_legacy_shapes_before_model_call():
    service = StubCreationService([])
    coordinator = BrainstormStageCoordinator(service)
    with pytest.raises(BrainstormGenerationError, match="可信输入身份"):
        await coordinator.stage_batch(stage="direction", kind="content_questions",
            root_request=GPU_ROOT, decisions=[], context_id="")
    with pytest.raises(BrainstormGenerationError, match="不能复用"):
        await coordinator.stage_batch(stage="direction", kind="content_questions",
            root_request=GPU_ROOT, decisions=[], context_id=CONTEXT_ID,
            topic={"id": "legacy"})
    with pytest.raises(BrainstormGenerationError, match="可信目录"):
        await coordinator.stage_batch(stage="direction", kind="content_questions",
            root_request=GPU_ROOT, decisions=[], context_id=CONTEXT_ID,
            selected_skills=[{"skill_id": "selected"}])
    assert service.model_calls == []
