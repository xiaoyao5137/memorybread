"""Selected source policy applies to every actual choice in every stage."""
import copy
import hashlib
import json

import pytest

from creation.brainstorm_stages import BrainstormStageCoordinator, _StageConsistencyError
from tests.test_brainstorm_fixed_matrix import fixture, proof, scope_checks
from tests.test_brainstorm_stage_nodes import prepare
from tests.test_creation_brainstorm import StubCreationService


MODEL = {"creation_model": None, "creation_api_key": None, "creation_base_url": None}
SKILLS = [{"id": "weekly-source-policy", "description": "生成GPU成本周报，保留全部必需板块。",
    "executionSteps": [{"instruction": "必须保留GPU使用与项目用量两张表及本周Token数据。GPU表保留原始口径的计算卡数、配额利用、GPU UTIL与收益；项目表保留原行列关联，按数值年化成本降序取前10，不按领域聚合。",
        "metadata": {"complete_policy": "缺失指标用—，不得虚构或替代。来源统计周期必须在目标周期内。"}}]}]


def macro_record():
    record, _, _ = fixture(0)
    record["topic"].update(title="受众定位", goal="确定周报核心范围与受众定位")
    record["question"].update(prompt="明确受众关注深度：仅需年度总成本与 ROI，还是需单卡利用率与潮汐收益？",
        why_now="选择说明深度，不代替资料核验。", single_choice_reason="两种说明深度互斥。",
        answer_template="可补充你的说明深度。")
    record["question"]["options"][0].update(label="宏观导向",
        description="仅呈现年度总成本、年化成本与ROI，满足管理层与财务部门对整体效益的决策需求，数据粒度粗但结论清晰。")
    record["question"]["options"][1].update(label="微观导向",
        description="在宏观指标基础上，增加单卡利用率、潮汐调度收益等微观细节，满足技术团队对资源调度优化的深度分析需求。")
    return record


async def checked_review(record, stage, mutate=None):
    skills = copy.deepcopy(SKILLS)
    if stage == "writing":
        record["topic"]["chapter_id"] = "chapter1"
    checked = scope_checks(record, writing_organization=stage == "writing", selected_skills=skills)
    if mutate is not None:
        mutate(checked)

    class PolicyService(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.prompts.append(kwargs["user_prompt"])
            self.model_calls.append(kwargs)
            schemas = kwargs["json_schema"]["properties"]["checks"]["properties"]
            yield json.dumps({"checks": {identifier: checked["checks"][identifier]
                for identifier in schemas}}, ensure_ascii=False)

    service = PolicyService([])
    coordinator = BrainstormStageCoordinator(service)
    async def relationship(**kwargs):
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    before = copy.deepcopy((record, skills))
    result = await prepare(coordinator, stage=stage, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=[], selected_skills=skills)
    assert (record, skills) == before
    return result, service


@pytest.mark.parametrize("stage", ["direction", "detail", "writing"])
@pytest.mark.asyncio
async def test_macro_only_choice_cannot_delete_mandatory_full_selected_skill_content(stage):
    record = macro_record()
    def rejection(checked):
        checked["checks"]["q0_o0"]["violates_selected_skill_policy"] = "yes"
    result, service = await checked_review(record, stage, rejection)
    assert result == {"review": "reject", "codes": ["topic_scope"], "decision_ids": []}
    assert len(service.model_calls) == 1
    payload = json.loads(service.prompts[0])
    assert payload["question_contexts"]["q0"]["question"]["options"][0]["description"] == record["question"]["options"][0]["description"]
    assert payload["authority"]["user_constraints"]["selected_skills"] == SKILLS


@pytest.mark.parametrize("stage", ["direction", "detail", "writing"])
@pytest.mark.asyncio
async def test_macro_focus_can_change_explanation_depth_while_every_mandatory_item_remains(stage):
    record = macro_record()
    record["question"].update(prompt="完整必需板块均保留，说明以宏观结论还是微观解释为重点？")
    for option in record["question"]["options"]:
        option["description"] = ("完整保留GPU使用与项目用量两张原表、本周Token数据和原行列关联及年化成本top10规则；"
            + ("说明先讲宏观结论。" if option["id"] == "a" else "说明先解释微观细节。"))
    result, service = await checked_review(record, stage)
    assert result == {"review": "pass", "codes": [], "decision_ids": []}
    digest = hashlib.sha256(json.dumps(SKILLS, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    covered = []
    for call in service.model_calls:
        payload = json.loads(call["user_prompt"])
        assert payload["authority"]["selected_skill_policy_binding"] == {
            "profiles_path": "authority.user_constraints.selected_skills", "profile_count": 1,
            "profiles_sha256": digest}
        schemas = call["json_schema"]["properties"]["checks"]["properties"]
        covered.extend(schemas)
        for schema in schemas.values():
            properties = schema["properties"]
            assert properties["selected_skill_policy_ref"]["enum"] == [digest]
            assert properties["violates_selected_skill_policy"]["enum"] == ["yes", "no", "unknown"]
            assert ("proposes_nonwriting_operation" in properties) == (stage == "writing")
    assert covered == ["q0_prompt", "q0_space", "q0_o0", "q0_o1"]


@pytest.mark.parametrize("mutation,code", [
    (lambda checked: checked["checks"]["q0_o0"].pop("selected_skill_policy_ref"), "checker_invalid"),
    (lambda checked: checked["checks"]["q0_o0"].update(selected_skill_policy_ref="summary-only"), "checker_invalid"),
    (lambda checked: checked["checks"]["q0_o0"].pop("violates_selected_skill_policy"), "checker_invalid"),
    (lambda checked: checked["checks"]["q0_o0"].update(violates_selected_skill_policy="unknown"), "insufficient_context"),
])
@pytest.mark.asyncio
async def test_direction_policy_certificate_requires_exact_complete_profile_and_known_verdict(mutation, code):
    with pytest.raises(_StageConsistencyError, match=code):
        await checked_review(macro_record(), "direction", mutation)


@pytest.mark.parametrize("kind", ["question", "choice_scope", "option", "next_goal"])
def test_direction_detail_bound_policy_schema_covers_each_actual_unit_including_future_goal(kind):
    record = macro_record()
    unit = {"id": "actual", "kind": kind, "context_key": "q0", "option_id": "a",
        "branch_option_id": "a", "text": "安排说明展开顺序", "fixed_compatibility_required": True}
    context = {"question": record["question"], "selected_topic": record["topic"], "effective_goal": record["topic"]["goal"],
        "ancestor_conditions": []}
    schema = BrainstormStageCoordinator._bound_scope_schema(unit, context,
        {"fixed_decisions": [], "cleared_constraints": [], "user_constraints": {"selected_skills": SKILLS}},
        {"pass": None, "insufficient_context": None})
    assert schema["properties"]["violates_selected_skill_policy"]["enum"] == ["yes", "no", "unknown"]
    assert "proposes_nonwriting_operation" not in schema["properties"]
    assert schema["properties"]["future_fixed"]["enum"] == ["not_applicable"]
