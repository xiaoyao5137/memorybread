"""Finite editorial compilation is structural proof, not NL-policy admission."""
import copy
import hashlib
import json

import pytest

from creation.brainstorm_editorial import AXES, EditorialCompilationError, EditorialCompiler, default_catalog
from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator


def case(family_index=0, *, max_depth=3, chapter_title="结果与依据说明", chapter_count=2):
    _, goal, levels = AXES[family_index]
    chapters = [{"id": "chapter_{}".format(index + 1), "title": chapter_title,
        "goal": "完整保留既定内容，只为新增说明提供内容边界"} for index in range(chapter_count)]
    chapter = chapters[-1]
    skills = [{"id": "test-policy", "executionSteps": [{"id": "keep-table",
        "objective": "原表行列、取值及成本排序保持不变；只允许书面说明"}]}]
    authority = {"fixed_decisions": [{"decision_id": "decision_{}".format(index + 1),
        "question": "完整原问题{}".format(index + 1), "answer": "完整当前答案{}".format(index + 1),
        "user_inputs": ["当前用户输入{}".format(index + 1)], "answer_source": "user",
        "selected_options": [{"id": "chosen", "label": "原方案", "tradeoff": "保留完整旧条件"}]}
        for index in range(24)], "cleared_constraints": [],
        "user_constraints": {"root_request": "编写项目复盘", "brief_edits": {}, "selected_skills": copy.deepcopy(skills)}}
    return {"chapter": chapter, "chapters": chapters,
        "topic": {"id": "topic", "title": chapter_title, "goal": goal, "chapter_id": chapter["id"]},
        "node": {"id": "root", "goal": levels[0], "depth": 1, "max_options": 5,
            "max_next_goals": 2, "parent_question_id": None, "parent_option_id": None, "ancestor_conditions": []},
        "axis": {"goal": goal, "levels": levels}, "authority": authority,
        "selected_skills": skills, "max_depth": max_depth}


def child(inputs, compiled, option_index):
    result = copy.deepcopy(inputs)
    node = inputs["node"]
    option = compiled["parsed"]["question"]["options"][option_index]
    prefix = EditorialCompiler._prefix([record["option_label"] for record in node["ancestor_conditions"]])
    record = {"parent_question_id": node["id"], "parent_option_id": chr(ord("a") + option_index),
        "parent_prompt": prefix + compiled["parsed"]["question"]["prompt_body"],
        "option_label": option["label"], "option_description": option["description"], "parent_goal": node["goal"]}
    depth = node["depth"] + 1
    result["node"] = {"id": node["id"] + "_" + record["parent_option_id"],
        "goal": inputs["axis"]["levels"][depth - 1], "depth": depth, "max_options": 5,
        "max_next_goals": 0 if depth == inputs["max_depth"] else 2,
        "parent_question_id": node["id"], "parent_option_id": record["parent_option_id"],
        "ancestor_conditions": copy.deepcopy(node["ancestor_conditions"]) + [record]}
    return result


@pytest.mark.parametrize("family_index", range(4))
@pytest.mark.parametrize("max_depth", [2, 3])
def test_every_actual_operator_branch_is_complete_distinct_bounded_and_compatible_with_existing_binder(family_index, max_depth):
    root = case(family_index, max_depth=max_depth, chapter_title="章" * 60)
    frontier = [root]
    bodies = []
    count = 0
    while frontier:
        inputs = frontier.pop(0)
        before = copy.deepcopy(inputs)
        compiled = EditorialCompiler.compile(**inputs)
        assert EditorialCompiler.verify(compiled, **inputs)
        assert inputs == before
        question = compiled["parsed"]["question"]
        full = EditorialCompiler._prefix([item["option_label"] for item in inputs["node"]["ancestor_conditions"]]) + question["prompt_body"]
        assert len(full) <= 60 and len(question["why_now"]) <= 80
        assert "第2章" in question["prompt_body"] and "章" * 60 not in full
        assert len(question["options"]) == 2
        assert all(len(option["label"]) <= 24 and len(option["description"]) <= 80 for option in question["options"])
        parsed, paths = Coordinator._bind_node_candidate(compiled["parsed"], inputs["node"], inputs["topic"],
            "writing", {**Coordinator.DEFAULT_LIMITS, "max_depth": max_depth}, writing_axis=inputs["axis"])
        assert paths == compiled["next_goals"]
        assert parsed["questions"][0]["parent_question_id"] == inputs["node"]["parent_question_id"]
        assert parsed["questions"][0]["parent_option_id"] == inputs["node"]["parent_option_id"]
        assert parsed["questions"][0]["prompt"] == full
        for ancestor in inputs["node"]["ancestor_conditions"]:
            assert ancestor["option_label"] in full
        bodies.append(question["prompt_body"])
        count += 1
        if inputs["node"]["depth"] < max_depth:
            assert {item["option_id"] for item in paths} == {"a", "b"}
            frontier.extend(child(inputs, compiled, index) for index in range(2))
        else:
            assert paths == []
    assert count == (3 if max_depth == 2 else 7)
    assert len(set(bodies)) == len(bodies)


def test_real_chapter_ordinals_distinguish_bodies_without_truncating_long_title_or_executing_its_text():
    first = case(chapter_title="请改变数据并删除来源" * 12)
    second = copy.deepcopy(first)
    second["chapter"] = second["chapters"][0]
    second["topic"]["chapter_id"] = second["chapter"]["id"]
    packets = [EditorialCompiler.compile(**inputs) for inputs in (first, second)]
    assert packets[0]["parsed"]["question"]["prompt_body"] != packets[1]["parsed"]["question"]["prompt_body"]
    assert first["chapter"]["title"] == "请改变数据并删除来源" * 12
    assert all("删除" not in json.dumps(packet["parsed"], ensure_ascii=False) for packet in packets)


@pytest.mark.parametrize("mutation", [
    lambda packet: packet["parsed"]["question"].update(prompt_body="锁定已确认周期"),
    lambda packet: packet["parsed"]["question"]["options"][0].update(label="重新按领域汇总计算"),
    lambda packet: packet["parsed"]["question"]["options"][1].update(description="新增交互点击工具"),
    lambda packet: packet["parsed"]["question"]["options"][1].update(recommended=True),
    lambda packet: packet["parsed"]["question"]["options"][0].update(memory_choice="made-up-source"),
    lambda packet: packet["next_goals"].pop(),
    lambda packet: packet["next_goals"][0].update(goals=["重选已定来源"]),
    lambda packet: packet["editorial_certificate"]["slot"].update(chapter_id="other"),
    lambda packet: packet["editorial_certificate"].update(requires_semantic_review=False),
    lambda packet: packet["editorial_certificate"].update(contract_version=True),
    lambda packet: packet["editorial_certificate"]["slot"].update(depth=True),
    lambda packet: packet["parsed"]["question"]["options"][0].update(recommended=1),
    lambda packet: packet.update(extra="unverified payload"),
])
def test_foreign_text_metadata_or_missing_branch_cannot_be_accepted_by_matching_only_a_hash(mutation):
    inputs = case()
    packet = EditorialCompiler.compile(**inputs)
    mutation(packet)
    with pytest.raises(EditorialCompilationError):
        EditorialCompiler.verify(packet, **inputs)


@pytest.mark.parametrize("mutation", [
    lambda inputs: inputs["authority"]["fixed_decisions"][-1].update(answer="当前人工修订"),
    lambda inputs: inputs["authority"]["fixed_decisions"][-1]["selected_options"][0].update(tradeoff="完整旧条件被改变"),
    lambda inputs: inputs["selected_skills"][0]["executionSteps"][-1].update(objective="新Skill政策"),
    lambda inputs: inputs["chapter"].update(goal="另一个具体范围"),
    lambda inputs: inputs["chapters"].reverse(),
])
def test_entire_authority_skill_chapter_and_manifest_identity_is_part_of_recompilation(mutation):
    inputs = case()
    packet = EditorialCompiler.compile(**inputs)
    mutation(inputs)
    with pytest.raises(EditorialCompilationError):
        EditorialCompiler.verify(packet, **inputs)


@pytest.mark.parametrize("field,value", [("option_label", "编造的父选择"),
    ("option_description", "只保留短标签，替换父机制"), ("parent_prompt", "另一个父问题"),
    ("parent_option_id", "c"), ("parent_goal", "另一个自由轴")])
def test_real_ancestor_cannot_be_replaced_or_reinterpreted(field, value):
    inputs = case()
    packet = EditorialCompiler.compile(**inputs)
    descendant = child(inputs, packet, 0)
    descendant["node"]["ancestor_conditions"][0][field] = value
    with pytest.raises(EditorialCompilationError):
        EditorialCompiler.compile(**descendant)


@pytest.mark.parametrize("mutation", [
    lambda catalog: catalog["capabilities"][0].update(prompt="编造自由行为"),
    lambda catalog: catalog["capabilities"][0]["slots"][0].update(description="按领域汇总"),
    lambda catalog: catalog["capabilities"][0]["slots"][0].update(operator_ids=["new_tool", "flow.1.2"]),
    lambda catalog: catalog["capabilities"][0]["slots"][0].update(operator_ids=["flow.1.1", "flow.1.1"]),
    lambda catalog: catalog["capabilities"][0]["slots"][0].update(depth=True),
])
def test_catalog_only_installs_implemented_operator_ids_not_free_candidate_prose(mutation):
    inputs = case()
    inputs["catalog"] = default_catalog()
    mutation(inputs["catalog"])
    with pytest.raises(EditorialCompilationError):
        EditorialCompiler.compile(**inputs)


def test_catalog_can_reorder_real_operations_and_recommendation_without_rewriting_their_action():
    inputs = case()
    inputs["catalog"] = default_catalog()
    slot = inputs["catalog"]["capabilities"][0]["slots"][0]
    slot["operator_ids"].reverse()
    slot["recommended_index"] = 1
    packet = EditorialCompiler.compile(**inputs)
    assert packet["parsed"]["question"]["options"][0]["label"] == "先解释再归纳"
    assert packet["parsed"]["question"]["options"][1]["recommended"]
    assert EditorialCompiler.compile(**child(inputs, packet, 0))["parsed"]["question"]["prompt_body"].find("解释在先") > 0


def test_no_supported_slot_or_complete_branch_budget_fails_explicitly():
    inputs = case()
    inputs["catalog"] = default_catalog()
    inputs["catalog"]["capabilities"][0]["slots"].pop()
    with pytest.raises(EditorialCompilationError) as error:
        EditorialCompiler.compile(**inputs)
    assert error.value.code == "EDITORIAL_SCOPE_UNSUPPORTED"
    inputs.pop("catalog")
    inputs["node"]["max_next_goals"] = 1
    with pytest.raises(EditorialCompilationError):
        EditorialCompiler.compile(**inputs)


@pytest.mark.parametrize("by_id", [False, True])
def test_exact_actual_confirmed_writing_question_is_not_reissued_even_after_custom_answer_edit(by_id):
    inputs = case()
    packet = EditorialCompiler.compile(**inputs)
    question = packet["parsed"]["question"]
    record = {"question_id": "root" if by_id else "old-generation-id", "question": question["prompt_body"],
        "answer": "用户自行修订的组织办法", "manually_edited": True, "answer_source": "user",
        "user_inputs": ["保留完整当前修订"], "selected_options": copy.deepcopy(question["options"][:1])}
    inputs["confirmed_writing_choices"] = [record]
    with pytest.raises(EditorialCompilationError) as error:
        EditorialCompiler.compile(**inputs)
    assert error.value.code == "EDITORIAL_CHOICE_ALREADY_CONFIRMED"


def test_unknown_legacy_writing_answer_is_bound_and_still_requires_real_semantic_review():
    inputs = case()
    record = {"question_id": "legacy-question", "question": "旧版自由写作题干", "answer": "旧的已定组织方法",
        "user_inputs": ["完整原输入"], "selected_options": [{"id": "old", "description": "完整旧方法"}]}
    inputs["confirmed_writing_choices"] = [record]
    packet = EditorialCompiler.compile(**inputs)
    proof = packet["editorial_certificate"]
    assert proof["requires_semantic_review"] is True
    assert proof["unclassified_writing_records"] == [hashlib.sha256(json.dumps(record, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()]


@pytest.mark.parametrize("option_id", ["a", "b"])
def test_interpreter_executes_only_separate_new_editorial_ir_and_leaves_every_opaque_artifact_untouched(option_id):
    inputs = case()
    packet = EditorialCompiler.compile(**inputs)
    document = {"body": "全部原正文", "tables": [{"rows": [["B", 10], ["A", 2]], "columns": ["项目", "成本"]}],
        "charts": [{"type": "line", "notes": {"location": "below", "text": "原注释"}}],
        "evidence": [{"conclusion_id": "c1", "evidence_id": "e1"}], "attachments": ["original-slide"],
        "data": {"period": "真实周期", "original_metrics": {"cost": 10}}, "carrier": "written-report"}
    before = copy.deepcopy(document)
    output = EditorialCompiler.apply_choice(document, compiled=packet, option_id=option_id, **inputs)
    assert output["opaque_document"] == before and document == before
    assert set(output) == {"opaque_document", "chapter_editorial_additions"}
    addition = output["chapter_editorial_additions"][0]
    assert addition["chapter_id"] == inputs["chapter"]["id"]
    assert addition["operator_id"] == packet["editorial_certificate"]["operator_ids"][ord(option_id) - ord("a")]
    assert [item["kind"] for item in addition["new_text_nodes"]] == (
        ["new_overview", "new_explanation"] if option_id == "a" else ["new_explanation", "new_overview"])
    assert all(item["text"] == "" and item["kind"].startswith("new_") for item in addition["new_text_nodes"])
    output["opaque_document"]["tables"][0]["rows"].reverse()
    assert document == before


def test_opaque_document_interpreter_rejects_foreign_candidate_and_unknown_variant():
    inputs = case()
    packet = EditorialCompiler.compile(**inputs)
    with pytest.raises(EditorialCompilationError):
        EditorialCompiler.apply_choice({}, compiled=packet, option_id="c", **inputs)
    packet["parsed"]["question"]["options"][1]["description"] += "改动一个字"
    with pytest.raises(EditorialCompilationError):
        EditorialCompiler.apply_choice({}, compiled=packet, option_id="a", **inputs)


def test_program_copy_does_not_assert_missing_artifacts_or_invent_neighbor_chapters():
    for family_index in range(4):
        inputs = case(family_index, chapter_title="短篇故事", chapter_count=1)
        packet = EditorialCompiler.compile(**inputs)
        text = json.dumps(packet["parsed"], ensure_ascii=False)
        assert "沿用全部既定内容与安排" in text
        assert all(word not in text for word in ("原数据", "图表", "证据表", "前章", "后文", "已确认选择", "用户已选"))


def test_missing_authority_or_conflicting_selected_skill_snapshot_is_rejected_without_semantic_claim():
    inputs = case()
    inputs["authority"].pop("fixed_decisions")
    with pytest.raises(EditorialCompilationError):
        EditorialCompiler.compile(**inputs)
    inputs = case()
    inputs["selected_skills"][0]["executionSteps"][-1].update(objective="不同政策")
    with pytest.raises(EditorialCompilationError):
        EditorialCompiler.compile(**inputs)


@pytest.mark.parametrize("answer_fields", [
    {"answer": "", "user_inputs": ["已确认自定义答案"], "selected_options": []},
    {"answer": "", "user_inputs": [], "selected_options": [{"id": "a", "label": "先概括再解释"}]},
])
def test_actual_confirmed_selection_cannot_hide_behind_an_empty_summary(answer_fields):
    inputs = case()
    packet = EditorialCompiler.compile(**inputs)
    inputs["confirmed_writing_choices"] = [{"question_id": "prior", "question": packet["parsed"]["question"]["prompt_body"],
        "answer_source": "user", **answer_fields}]
    with pytest.raises(EditorialCompilationError) as error:
        EditorialCompiler.compile(**inputs)
    assert error.value.code == "EDITORIAL_CHOICE_ALREADY_CONFIRMED"


# Complete packet hashes captured from the root-only contract before nested
# refinement. They cover copy, edges, snapshot, catalog and certificate, not
# merely the displayed labels. Keeping them lets a paid root remain reusable.
ROOT_PACKET_SHA = {
    (0, 2, False): "36537ec3dc65333dc39b38ba4935364e5e86aa4a5e5ae95b24dec9feb724478d",
    (0, 2, True): "8acfe0423ad1e20c0ac80909b2b4c0409ac38f884bb90ccb369b50ccb46f47b7",
    (0, 3, False): "369cc355c61d0242e62162e79f95bf22c11fd3f26276e988e5dc953ce24113ff",
    (0, 3, True): "5ebd10bcee601580f01886d4bd960ec2bd0882de1569484ae1e68316c600ac6f",
    (1, 2, False): "d42b001cd8413b4f6ed084f939231eebcfbd0fead6f7b74c71b71a0477c3f6fb",
    (1, 2, True): "18d7676834f236acf1d010aa888f4d1f2e776f95756187bde1b560390693f1cd",
    (1, 3, False): "51b661680f7c2cf0d7bdb937d208a4ff0d9b802a0f2f3ffa56e20cc18d8dfded",
    (1, 3, True): "069c886eb8d163a5a4465303055db0fc95b9708d63ebe06274201f216a59438e",
    (2, 2, False): "9e24aa4c0e256b0531c65e7d17fc1edbf880d3d8878ca44bbc00e454a5cef4bd",
    (2, 2, True): "3933d10cfcbbd97124e802fa82991179e8c29f29c8fbbf5f9aeb63e6ca920945",
    (2, 3, False): "73320621120485ee5e460ca096c1960a92069acd405951d8881aec76a8107fab",
    (2, 3, True): "c1f343c6c79794d2dada56d9de2ad2bcb397705292b8374816626400062cec92",
    (3, 2, False): "9260bba43d7ad8e3cb5c3359b1373369a6b9cde7f575b9007657b1aa6e70be35",
    (3, 2, True): "e5846475af9c88766f302e4e03abcfcc22f5182fea3929212938c4ac3e42cd0f",
    (3, 3, False): "bae6af8149e586df5583ebdbe9f6b188227f510485c133847a3031f9be246964",
    (3, 3, True): "3eaaf3d4fed25a60191edacd5c6857f14dde5ad7e75fe5b1e9c0455a84b537d6",
}


def reordered_case(family_index, max_depth, reverse):
    inputs = case(family_index, max_depth=max_depth)
    if reverse:
        inputs["catalog"] = default_catalog()
        for capability in inputs["catalog"]["capabilities"]:
            for slot in capability["slots"]:
                slot["operator_ids"].reverse()
                slot["recommended_index"] = 1
    return inputs


@pytest.mark.parametrize("family_index", range(4))
@pytest.mark.parametrize("max_depth", [2, 3])
@pytest.mark.parametrize("reverse", [False, True])
def test_saved_root_complete_packet_is_unchanged_for_all_supported_catalog_orders(family_index, max_depth, reverse):
    inputs = reordered_case(family_index, max_depth, reverse)
    packet = EditorialCompiler.compile(**inputs)
    actual = hashlib.sha256(json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert actual == ROOT_PACKET_SHA[(family_index, max_depth, reverse)]


@pytest.mark.parametrize("family_index", range(4))
@pytest.mark.parametrize("max_depth", [2, 3])
@pytest.mark.parametrize("reverse", [False, True])
def test_every_nested_choice_preserves_complete_parent_projection_and_every_opaque_value(family_index, max_depth, reverse):
    document = {"body": [{"heading": "已有结构", "text": "全部原正文"}],
        "table": {"rows": [["B", 10], ["A", 2]], "columns": ["项目", "值"], "notes": ["原位置"]},
        "chart": {"notes": {"below": "原注释"}}, "evidence": [{"id": "e1", "order": 1}],
        "data": {"period": "真实周期", "flag": True, "values": [None, 1, 1.0]}, "attachments": ["原载体"]}
    original = copy.deepcopy(document)
    frontier = [(reordered_case(family_index, max_depth, reverse), [])]
    count = 0
    while frontier:
        inputs, previous = frontier.pop(0)
        packet = EditorialCompiler.compile(**inputs)
        for index, identifier in enumerate(("a", "b")):
            proof = EditorialCompiler.refinement_proof(compiled=packet, option_id=identifier, **inputs)
            assert EditorialCompiler.verify_refinement(proof, compiled=packet, option_id=identifier, **inputs)
            assert proof["compiled_packet"] == packet
            assert proof["ancestor_conditions"] == inputs["node"]["ancestor_conditions"]
            assert proof["requires_semantic_review"] is True
            assert len(proof["selected_operator_ids"]) == inputs["node"]["depth"]
            assert len(proof["refinements"]) == len(previous)
            for projection, parent in zip(proof["ancestor_projections"], previous):
                assert projection["expected_nodes"] == parent["new_text_nodes"]
                assert projection["projected_nodes"] == parent["new_text_nodes"]
            for refinement in proof["refinements"]:
                targets = refinement["targets"]
                assert targets and len({json.dumps(item["path"]) for item in targets}) == len(targets)
                assert all(item["path"][-1] == "text" and item["before"] == "" for item in targets)
                if family_index == 1 and refinement["depth"] == 2:
                    assert len(targets) == 2  # Both groups or both reading layers.
                if family_index == 2 and refinement["depth"] == 3:
                    assert len(targets) == (2 if refinement["parent_operator_id"].endswith(".1") else 1)
            output = EditorialCompiler.apply_choice(document, compiled=packet, option_id=identifier, **inputs)
            addition = output["chapter_editorial_additions"][0]
            assert addition["new_text_nodes"] == proof["new_text_nodes"]
            assert output["opaque_document"] == original and document == original
            if previous:
                option = packet["parsed"]["question"]["options"][index]
                assert "沿用「{}」".format(inputs["node"]["ancestor_conditions"][-1]["option_label"]) in option["description"]
                assert "父路径各层次序" in option["description"]
            if inputs["node"]["depth"] < max_depth:
                frontier.append((child(inputs, packet, index), previous + [proof]))
            count += 1
    assert count == (6 if max_depth == 2 else 14)


@pytest.mark.parametrize("mutation", [
    lambda proof: proof["refinements"][0]["targets"][0].update(path=["table", "rows"]),
    lambda proof: proof["refinements"][0]["targets"].pop(),
    lambda proof: proof["new_text_nodes"].reverse(),
    lambda proof: proof["new_text_nodes"][1]["refinement"]["nodes"][0].update(text="偷偷写入正文"),
    lambda proof: proof["ancestor_projections"][0]["projected_nodes"].reverse(),
    lambda proof: proof["ancestor_conditions"][0].update(option_description="其它父选择"),
    lambda proof: proof["selected_operator_ids"].reverse(),
    lambda proof: proof.update(unknown_slot="attachment"),
])
def test_replay_rejects_modified_tree_targets_ancestor_projections_and_unknown_slots(mutation):
    inputs = case()
    descendant = child(inputs, EditorialCompiler.compile(**inputs), 0)
    packet = EditorialCompiler.compile(**descendant)
    proof = EditorialCompiler.refinement_proof(compiled=packet, option_id="a", **descendant)
    mutation(proof)
    with pytest.raises(EditorialCompilationError):
        EditorialCompiler.verify_refinement(proof, compiled=packet, option_id="a", **descendant)


@pytest.mark.parametrize("field,value", [("option_label", "段首承接"),
    ("option_description", "沿用全部既定内容与安排；仅在本章新增的说明文字中段首承接，不引入新事实或改变既定方法。"),
    ("parent_prompt", "若「先概括再解释」，第2章（概括在先）的新增说明怎样承接？"),
    ("parent_goal", "自由解释父路径")])
def test_grandchild_rejects_legacy_or_changed_child_copy_instead_of_assuming_parent_preservation(field, value):
    inputs = case()
    second = child(inputs, EditorialCompiler.compile(**inputs), 0)
    third = child(second, EditorialCompiler.compile(**second), 0)
    third["node"]["ancestor_conditions"][1][field] = value
    with pytest.raises(EditorialCompilationError) as error:
        EditorialCompiler.compile(**third)
    assert error.value.code == "EDITORIAL_ANCESTOR_UNSUPPORTED"


def test_each_child_copy_identifies_the_exact_parent_subobject_that_the_interpreter_refines():
    targets = {0: ("解释段", "解释正文"), 1: ("问题组内", "解释正文"),
        2: ("解释段内", "依据连接的说明正文"), 3: ("转承段", "转承正文")}
    for family_index in range(4):
        inputs = case(family_index)
        second = child(inputs, EditorialCompiler.compile(**inputs), 0)
        packet = EditorialCompiler.compile(**second)
        assert all(targets[family_index][0] in option["description"] for option in packet["parsed"]["question"]["options"])
        third = child(second, packet, 0)
        packet = EditorialCompiler.compile(**third)
        assert all(targets[family_index][1] in option["description"] for option in packet["parsed"]["question"]["options"])


def test_current_node_and_ancestor_ids_cannot_form_a_loop_or_duplicate_identity():
    inputs = case()
    second = child(inputs, EditorialCompiler.compile(**inputs), 0)
    third = child(second, EditorialCompiler.compile(**second), 0)
    third["node"]["ancestor_conditions"][1]["parent_question_id"] = "root"
    third["node"]["parent_question_id"] = "root"
    with pytest.raises(EditorialCompilationError):
        EditorialCompiler.compile(**third)
