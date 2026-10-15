"""Displayed explanatory copy must not escape the candidate fact contract."""
import copy
import json

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator
from tests.test_brainstorm_claim_speech_acts import checked_review, speech_checks
from tests.test_brainstorm_fixed_matrix import fixture
from tests.test_creation_brainstorm import StubCreationService


COPY_FIELDS = ("why_now", "single_choice_reason", "answer_template")


def full_question_copy(question):
    return "\n".join(question[field] for field in ("prompt",) + COPY_FIELDS
        if isinstance(question.get(field), str) and question[field].strip())


@pytest.mark.parametrize("field", COPY_FIELDS)
@pytest.mark.asyncio
async def test_unsupported_confirmation_in_any_displayed_annotation_is_bound_and_rejected(field):
    record, _, _ = fixture(0)
    statement = "当前候选路径已经由用户确认，按已锁定的对象继续。"
    record["question"][field] = statement
    before = copy.deepcopy(record)
    checked = speech_checks(record)
    checked["checks"]["q0_prompt"]["claims_hypothetical_path_confirmed"] = "yes"
    result, service = await checked_review(record, [], [checked])
    assert result == {"review": "reject", "codes": ["candidate_as_confirmed"], "decision_ids": []}
    assert record == before and len(service.model_calls) == 1
    call = service.model_calls[0]
    context = json.loads(call["user_prompt"])["question_contexts"]["q0"]
    assert context["candidate_copy"][field] == statement
    assert field not in context["question"]
    schema = call["json_schema"]["properties"]["checks"]["properties"]["q0_prompt"]
    assert schema["properties"]["claim_evidence"]["enum"] == [full_question_copy(record["question"])]


@pytest.mark.parametrize("field", ("prompt",) + COPY_FIELDS)
@pytest.mark.asyncio
async def test_unprovided_prior_user_choice_is_not_outside_the_candidate_claim_predicates(field):
    record, _, _ = fixture(0)
    record["question"][field] = "基于已确认的受众定位，为管理层与跨部门协作方安排说明。"
    checked = speech_checks(record)
    checked["checks"]["q0_prompt"]["asserts_unconfirmed_user_choice"] = "yes"
    result, service = await checked_review(record, [], [checked])
    assert result["codes"] == ["candidate_as_confirmed"]
    properties = service.model_calls[0]["json_schema"]["properties"]["checks"]["properties"]["q0_prompt"]["properties"]
    assert properties["asserts_unconfirmed_user_choice"]["enum"] == ["yes", "no", "unknown"]
    assert all(checked["checks"]["q0_prompt"][name] == "no" for name in (
        "claims_current_candidate_selected", "claims_hypothetical_path_confirmed", "asserts_unconfirmed_realized_fact"))


@pytest.mark.asyncio
async def test_benign_annotations_are_complete_fact_evidence_without_extra_units_or_calls():
    record, _, _ = fixture(0)
    record["question"].update(why_now="本轮只比较还未选择的表达方案。",
        single_choice_reason="各方案是同一段落的不同组织办法。",
        answer_template="可补充其它组织方式，不表示已经采纳。",
        context_details="程序绑定的独立引用证据")
    for option in record["question"]["options"]:
        option["details"] = "独立引用门禁已核验的来源内容"
    result, service = await checked_review(record, [], [speech_checks(record)])
    assert result["review"] == "pass" and len(service.model_calls) == 1
    payload = json.loads(service.prompts[0])
    assert len(payload["validation_task"]["units"]) == 4
    assert "程序绑定的独立引用证据" not in service.prompts[0]
    assert "独立引用门禁已核验的来源内容" not in service.prompts[0]
    schemas = service.model_calls[0]["json_schema"]["properties"]["checks"]["properties"]
    assert schemas["q0_prompt"]["properties"]["claim_evidence"]["enum"] == [full_question_copy(record["question"])]
    assert schemas["q0_space"]["properties"]["claim_evidence"]["enum"] == [record["question"]["prompt"]]


@pytest.mark.asyncio
async def test_old_prompt_only_evidence_cannot_omit_explanatory_assertions():
    record, _, _ = fixture(0)
    record["question"]["why_now"] = "用户已经确认当前未选方案。"
    checked = speech_checks(record)
    checked["checks"]["q0_prompt"]["claim_evidence"] = record["question"]["prompt"]
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await checked_review(record, [], [checked])


def test_generation_and_saved_review_have_identical_complete_copy_bindings():
    record, _, _ = fixture(0)
    record["question"].update(why_now="当前目标仍待选择。", single_choice_reason="方案互斥。",
        answer_template="请补充你的方式。")
    original = copy.deepcopy(record)
    generated = BrainstormStageCoordinator._consistency_candidate({"question": record["question"], "next_goals": []})
    saved = BrainstormStageCoordinator._consistency_candidate({"review_questions": [record]})
    expected = {field: record["question"][field] for field in COPY_FIELDS}
    assert generated["candidate_copy"] == saved["review_questions"][0]["candidate_copy"] == expected
    task = {"ancestor_conditions": [], "goal": record["topic"]["goal"]}
    _, generation_contexts = BrainstormStageCoordinator._consistency_units(generated, record["topic"], task)
    _, review_contexts = BrainstormStageCoordinator._consistency_units(saved, record["topic"], task)
    assert generation_contexts["q0"]["candidate_copy"] == review_contexts["q0"]["candidate_copy"] == expected
    assert all(field not in generated["question"] for field in COPY_FIELDS)
    assert record == original


@pytest.mark.parametrize("saved_review", [False, True])
@pytest.mark.asyncio
async def test_explanations_cannot_certify_their_own_new_decision_space(saved_review):
    record, _, _ = fixture(0)
    marker = "这是候选自称的新颖性依据，不得用于选择轴证明"
    record["question"].update({field: marker for field in COPY_FIELDS})
    result = {"review_questions": [record]} if saved_review else {
        "question": record["question"], "next_goals": []}
    candidate = BrainstormStageCoordinator._consistency_candidate(result)
    question = candidate["review_questions"][0]["question"] if saved_review else candidate["question"]
    certificate = {"question_evidence": question["prompt"], "question_role_binding": "new_choice",
        "options": {option["id"]: {"evidence": option["label"], "role_binding": "new_method",
            "action_span": "\n".join(option[field] for field in ("label", "description", "tradeoff")
                if isinstance(option.get(field), str) and option[field].strip())}
            for option in question["options"]}, "relation": "same_unresolved_axis", "decision_ids": []}
    service = StubCreationService([json.dumps(certificate, ensure_ascii=False)])
    coordinator = BrainstormStageCoordinator(service)
    before = copy.deepcopy(candidate)
    checked = await coordinator._check_new_decision_space(candidate=candidate, topic=record["topic"],
        task={"fixed_decisions": [], "ancestor_conditions": [], "goal": record["topic"]["goal"]},
        creation_model=None, creation_api_key=None, creation_base_url=None)
    assert len(checked) == 1 and len(service.model_calls) == 1
    assert marker not in service.prompts[0]
    assert candidate == before
