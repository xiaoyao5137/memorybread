"""Claim gates distinguish a proposed design from asserted finished facts."""
import copy
import json

import pytest

from creation.brainstorm import BrainstormGenerationError
from tests.test_brainstorm_fixed_matrix import (
    fixture, matrix, prepare, proof, scope_checks, setup,
)

CLAIM_FACTS = ("claims_current_candidate_selected", "claims_hypothetical_path_confirmed",
    "asserts_unconfirmed_realized_fact", "asserts_unconfirmed_user_choice")


def complete_option_text(option):
    return "\n".join(option[field] for field in ("label", "description", "tradeoff")
        if isinstance(option.get(field), str) and option[field].strip())


def speech_checks(record):
    checked = scope_checks(record)
    for key, cell in checked["checks"].items():
        cell.pop("claim", None)
        if key.startswith("q0_o"):
            evidence = complete_option_text(record["question"]["options"][int(key[4:])])
        elif key == "q0_prompt":
            evidence = "\n".join(record["question"][field]
                for field in ("prompt", "why_now", "single_choice_reason", "answer_template")
                if isinstance(record["question"].get(field), str) and record["question"][field].strip())
        else:
            evidence = record["question"]["prompt"]
        cell.update(claim_evidence=evidence)
        cell.update({field: "no" for field in CLAIM_FACTS})
    return checked


async def checked_review(record, original, responses):
    coordinator, service = setup(responses, check_consistency=True)
    async def relationship(**kwargs):
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=original)
    return result, service


@pytest.mark.parametrize("statement", [
    "量化成果仍优先展示；之后先介绍全部来源及原优先级，再说明固定周期。",
    "若沿用父候选方法，则先说明来源，再说明周期；该路径仍待用户选择。",
    "计划先整理来源段落，再整理周期段落，不声称数据已经取得。",
])
@pytest.mark.asyncio
async def test_proposals_inherited_requirements_and_hypotheses_do_not_assert_finished_facts(statement):
    record, original, fixed = fixture()
    record["question"]["options"][1]["description"] = statement
    checked = speech_checks(record)
    responses = [checked] + [matrix(fixed[offset:offset + 12], record["question"])
        for offset in range(0, 24, 12)]
    result, service = await checked_review(record, original, responses)
    assert result == {"review": "pass", "codes": [], "decision_ids": []}
    assert len(service.model_calls) == 3
    fields = service.model_calls[0]["json_schema"]["properties"]["checks"]["properties"]["q0_o1"]["properties"]
    assert "claim" not in fields
    assert fields["claim_evidence"]["enum"] == [complete_option_text(record["question"]["options"][1])]
    assert all(fields[field]["enum"] == ["yes", "no", "unknown"] for field in CLAIM_FACTS)


@pytest.mark.parametrize("field,statement", [
    ("claims_current_candidate_selected", "用户已选择本候选的来源优先说明顺序，按此安排正文。"),
    ("claims_hypothetical_path_confirmed", "父路径假设已经成为用户确认，必须沿用该来源说明办法。"),
    ("asserts_unconfirmed_realized_fact", "本期GPU成本已经下降25%，将该实测结果作为说明段开头。"),
    ("asserts_unconfirmed_realized_fact", "平台完整数据已经取得并核实，用其中104个样本解释成果。"),
    ("asserts_unconfirmed_user_choice", "受众已经锁定为跨部门协作方，沿用该定位安排正文。"),
])
@pytest.mark.asyncio
async def test_unsupported_assertion_inside_a_proposal_blocks_nonrecommended_description(field, statement):
    record, original, fixed = fixture()
    record["question"]["options"][1].update(label="按来源组织说明", description=statement, recommended=False)
    checked = speech_checks(record)
    checked["checks"]["q0_o1"][field] = "yes"
    result, service = await checked_review(record, original, [checked])
    assert result == {"review": "reject", "codes": ["candidate_as_confirmed"], "decision_ids": []}
    assert len(service.model_calls) == 1
    payload = json.loads(service.prompts[0])
    assert payload["question_contexts"]["q0"]["question"]["options"][1]["description"] == statement
    assert payload["authority"]["fixed_decisions"] == fixed
    assert checked["checks"]["q0_o1"]["claim_evidence"] == complete_option_text(record["question"]["options"][1])


@pytest.mark.parametrize("field", CLAIM_FACTS)
@pytest.mark.asyncio
async def test_unknown_in_each_specific_claim_fact_remains_blocking(field):
    record, original, fixed = fixture(1)
    checked = speech_checks(record)
    checked["checks"]["q0_o1"][field] = "unknown"
    with pytest.raises(BrainstormGenerationError, match="insufficient_context"):
        await checked_review(record, original, [checked])


@pytest.mark.parametrize("mutation", [
    lambda cell: cell.pop("claim_evidence"),
    lambda cell: cell.update(claim_evidence="只看无害标签，不读取声明已取得数据的描述"),
    lambda cell: cell.pop("claims_current_candidate_selected"),
    lambda cell: cell.update(asserts_unconfirmed_realized_fact="pass"),
    lambda cell: cell.update(claim="grounded_or_hypothetical"),
])
@pytest.mark.asyncio
async def test_claim_certificate_cannot_omit_full_evidence_or_restore_legacy_global_pass(mutation):
    record, original, fixed = fixture(1)
    checked = speech_checks(record)
    mutation(checked["checks"]["q0_o1"])
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await checked_review(record, original, [checked])


@pytest.mark.asyncio
async def test_tradeoff_is_part_of_complete_bound_claim_evidence():
    record, original, fixed = fixture(1)
    option = record["question"]["options"][1]
    option.update(label="段落组织方法", description="只比较说明段组织顺序",
        tradeoff="用户已确认此候选，数据已经全部取得")
    checked = speech_checks(record)
    checked["checks"]["q0_o1"]["claims_current_candidate_selected"] = "yes"
    result, service = await checked_review(record, original, [checked])
    assert result["codes"] == ["candidate_as_confirmed"]
    assert service.model_calls[0]["temperature"] == 0.0
    fields = service.model_calls[0]["json_schema"]["properties"]["checks"]["properties"]["q0_o1"]["properties"]
    assert fields["claim_evidence"]["enum"] == [complete_option_text(option)]


@pytest.mark.asyncio
async def test_complete_original_authority_can_support_a_real_fact_without_changing_source_answers():
    record, original, fixed = fixture(1)
    original[0].update(answer="资料由用户提供，实测GPU利用率76%", user_inputs=["资料由用户提供，实测GPU利用率76%"])
    from creation.brainstorm_stages import BrainstormStageCoordinator
    fixed = BrainstormStageCoordinator._stage_authority(original, {})[0]
    record["question"]["options"][1]["description"] = "沿用用户提供的实测GPU利用率76%，仅规定说明段落组织方式。"
    checked = speech_checks(record)
    before = copy.deepcopy(original)
    result, service = await checked_review(record, original, [checked, matrix(fixed, record["question"])])
    assert result["review"] == "pass" and original == before
    payload = json.loads(service.prompts[0])
    assert payload["authority"]["fixed_decisions"] == fixed
