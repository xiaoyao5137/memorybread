"""Installed catalogs compile reviewed choices, never a new policy or user answer."""
import asyncio
import base64
import copy
import hashlib
import importlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_catalog import BrainstormCatalog, CATALOG_PATH, PROFILE_PATH, load_installed_catalog
from creation.brainstorm_stages import BrainstormStageCoordinator
from tests.test_creation_brainstorm import StubCreationService


def directory(domain="finance", stages=("detail",)):
    slots = []
    for index, key in enumerate(("root", "child_a", "child_b")):
        methods = [{"id": "method_a", "object_id": domain, "required_fields": ["source", "value"],
            "label": ["集中记录", "保留字段顺序", "按条目组织"][index],
            "description": "保留全部原字段，在同一记录内维护对照关系。", "tradeoff": "集中维护便于统一核验。",
            "child_slot_ids": ["child_a"] if index == 0 else []},
            {"id": "method_b", "object_id": domain, "required_fields": ["source", "value"],
             "label": ["分来源记录", "采用索引顺序", "按来源组织"][index],
             "description": "保留全部原字段，分别维护来源记录并建立索引。", "tradeoff": "分开维护需要同步索引。",
             "child_slot_ids": ["child_b"] if index == 0 else []}]
        slots.append({"id": key, "topic_id": "records", "object_id": domain,
            "choice_axis": "同一记录对象的维护方式", "required_fields": ["source", "value"],
            "prompt_body": ["对照记录采用哪种维护方式？", "集中记录的条目按什么顺序组织？", "分来源记录的条目按什么顺序组织？"][index],
            "why_now": "选择记录组织方法，不改变既定内容与规则。", "methods": methods})
    return {"schema_version": 1, "kind": "memorybread.brainstorm-catalog", "catalog_id": domain,
        "catalog_version": 1, "skill_key": "skill_" + domain, "skill_profile_sha256": "a" * 64,
        "stages": {stage: {"topics": [{"id": "records", "title": domain + "记录维护",
            "goal": "安排已定记录的组织与核验方式", "root_slot_ids": ["root"]}], "slots": copy.deepcopy(slots)}
            for stage in stages}}


def install(tmp_path, value=None):
    value = copy.deepcopy(value or directory())
    description = {"purpose": "维护记录", "document_types": ["说明"], "problems": [], "domains": [], "deliverables": []}
    steps = [{"id": "record", "title": "维护", "objective": "保留原值", "output": "记录", "agents": [], "skills": [], "tools": [], "retain_webpage_screenshot": True}]
    profile = json.dumps({"schema_version": 1, "kind": "memorybread.creation-profile", "content": {
        "skill_description": description, "execution_steps": steps}}, ensure_ascii=False).encode()
    value["skill_profile_sha256"] = hashlib.sha256(profile).hexdigest()
    raw = json.dumps(value, ensure_ascii=False).encode()
    files = [{"path": name, "media_type": "application/json", "content_base64": base64.b64encode(content).decode(),
              "size_bytes": len(content)} for name, content in ((PROFILE_PATH, profile), (CATALOG_PATH, raw))]
    path = tmp_path / "skills.db"
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE creation_skills(client_skill_key TEXT UNIQUE, installed INTEGER, status TEXT, deleted_at INTEGER, package_files TEXT, skill_description TEXT, execution_steps TEXT)")
        db.execute("INSERT INTO creation_skills VALUES(?,1,'saved',NULL,?,?,?)", (value["skill_key"], json.dumps(files), json.dumps(description), json.dumps(steps)))
    skill = {"id": value["skill_key"], "skillDescription": copy.deepcopy(description), "executionSteps": copy.deepcopy(steps)}
    binding = {"skill_key": value["skill_key"], "profile_sha256": hashlib.sha256(profile).hexdigest(),
               "catalog_sha256": hashlib.sha256(raw).hexdigest(), "catalog": value}
    service = StubCreationService([])
    service.db_path = str(path)
    coordinator = BrainstormStageCoordinator(service)
    def forbidden(*args, **kwargs):
        raise AssertionError("static catalog attempted recall/model repair")
    coordinator._retrieve_memory = forbidden
    coordinator._check_stage_consistency = forbidden
    coordinator._repair_question_copy = forbidden
    return coordinator, service, binding, skill


async def request(coordinator, binding, skill, *, kind="plan", stage="detail", **extra):
    return await coordinator.stage_batch(stage=stage, kind=kind, root_request="保留全部已定规则，安排实施方法。",
        decisions=extra.pop("decisions", []), selected_skills=[skill], trusted_catalog=binding,
        context_id="a" * 64, **extra)


def execution_node(slot, *, conditions=None, maximum_depth=2):
    conditions = conditions or []
    return {"id": "stage_build_" + slot["id"], "goal": "catalog_slot:" + slot["id"],
        "depth": len(conditions) + 1, "parent_question_id": conditions[-1]["parent_question_id"] if conditions else None,
        "parent_option_id": conditions[-1]["parent_option_id"] if conditions else None,
        "ancestor_conditions": conditions, "max_options": len(slot["methods"]),
        "max_next_goals": sum(len(method["child_slot_ids"]) for method in slot["methods"])}


async def compile_every_node(coordinator, binding, skill, stage="detail", decisions=None):
    result = await request(coordinator, binding, skill, stage=stage)
    config = binding["catalog"]["stages"][stage]
    slots = {slot["id"]: slot for slot in config["slots"]}
    questions, goals, paths = [], [], {}
    for selected in result["topics"]:
        roots = await request(coordinator, binding, skill, stage=stage, kind="topic_plan", topic=selected, root_limit=8)
        pending = [(goal, []) for goal in roots["root_goals"]]
        while pending:
            goal, conditions = pending.pop(0)
            slot = slots[goal.split(":", 1)[1]]
            value = await request(coordinator, binding, skill, stage=stage, kind="node", topic=selected,
                node=execution_node(slot, conditions=conditions), decisions=decisions or [],
                plan={"prepared_question_prompts": [item["prompt"] for item in questions],
                      "prepared_question_bodies": [paths[item["id"]] for item in questions]})
            question = value["question"]
            assert question["single_choice_reason"] == "各方法服务同一对象，请选择一种实施方式"
            assert question["answer_template"] == "补充你的具体实施方法。"
            assert question["prompt"].endswith(slot["prompt_body"])
            assert [opt["description"] for opt in question["options"]] == [method["description"] for method in slot["methods"]]
            assert [opt["details"] for opt in question["options"]] == [method["tradeoff"] for method in slot["methods"]]
            assert value["memory_brief"] == ""
            assert question["context_details"] == ""
            for group in value["next_goals"]:
                option = next(option for option in question["options"] if option["id"] == group["option_id"])
                ancestor = {"parent_question_id": question["id"], "parent_option_id": option["id"],
                    "parent_prompt": question["prompt"], "option_label": option["label"],
                    "option_description": option["description"], "parent_goal": goal}
                pending.extend((child, conditions + [ancestor]) for child in group["goals"])
            questions.append(question)
            paths[question["id"]] = slot["prompt_body"]
            goals.append(value["next_goals"])
    return result, questions, goals


@pytest.mark.parametrize("domain", ["finance", "board_game", "story"])
@pytest.mark.parametrize("stage", ["direction", "detail", "writing"])
@pytest.mark.asyncio
async def test_three_phases_static_compile_cross_domain_methods_and_true_parent_paths(tmp_path, domain, stage):
    coordinator, service, binding, skill = install(tmp_path, directory(domain, (stage,)))
    decisions = [{"question_id": "user_fixed", "question": "需要包含哪些内容？", "answer": "保留甲乙全部内容",
                  "answer_source": "user", "user_inputs": ["原值不补齐"], "selected_options": [{"label": "全部", "description": "同时沿用全部规则"}]}]
    original = copy.deepcopy(decisions)
    plan, questions, groups = await compile_every_node(coordinator, binding, skill, stage, decisions)
    assert len(questions) == 3 and len(plan["topics"]) == 1
    assert len(groups[0]) == 2 and groups[1:] == [[], []]
    assert all(not {"memory_ids", "memory_evidence"}.intersection(opt)
               for question in questions for opt in question["options"])
    assert decisions == original and service.model_calls == []
    assert bool(plan["chapters"]) == (stage == "writing")


@pytest.mark.asyncio
async def test_actual_reviewed_catalog_all_fifteen_questions_pass_tree_display_and_source_gates(tmp_path):
    path = Path(__file__).resolve().parents[2] / "shared/creation-brainstorm/catalogs/gpu-cost-weekly-report-detail-v1.json"
    value = json.loads(path.read_text())
    coordinator, service, binding, skill = install(tmp_path, value)
    _, questions, groups = await compile_every_node(coordinator, binding, skill)
    assert len(questions) == 15 and sum(bool(group) for group in groups) == 5
    assert service.model_calls == []


@pytest.mark.parametrize("mutation", [
    lambda v: v.update(unknown=True),
    lambda v: v["stages"]["detail"]["topics"][0].update(unknown=True),
    lambda v: v["stages"]["detail"]["slots"][0].update(memory_ids=["forged"]),
    lambda v: v["stages"]["detail"]["slots"][0]["methods"][0].update(params={"wait": 3}),
    lambda v: v["stages"]["detail"]["slots"][0]["methods"][0].update(object_id="other_object"),
    lambda v: v["stages"]["detail"]["slots"][0]["methods"][0].update(required_fields=["value", "source"]),
    lambda v: v["stages"]["detail"]["slots"][0]["methods"][0].update(child_slot_ids=["missing"]),
    lambda v: v["stages"]["detail"]["slots"][1]["methods"][0].update(child_slot_ids=["root"]),
    lambda v: v["stages"]["detail"]["slots"][0]["methods"][1].update(child_slot_ids=["child_a"]),
    lambda v: v["stages"]["detail"]["slots"][0]["methods"][1].update(child_slot_ids=[]),
    lambda v: v["stages"]["detail"]["slots"][0]["methods"][0].update(label="长" * 25),
    lambda v: v["stages"]["detail"]["slots"][1].update(prompt_body="长" * 61),
    lambda v: v["stages"]["detail"]["slots"][2].update(topic_id="other"),
    lambda v: v["stages"]["detail"]["slots"][2].update(prompt_body=v["stages"]["detail"]["slots"][1]["prompt_body"]),
])
@pytest.mark.asyncio
async def test_malformed_or_policy_capable_catalog_never_falls_back(tmp_path, mutation):
    value = directory()
    mutation(value)
    coordinator, service, binding, skill = install(tmp_path, value)
    with pytest.raises(BrainstormGenerationError):
        await request(coordinator, binding, skill)
    assert not service.model_calls


@pytest.mark.parametrize("change", ["hash", "profile", "uninstalled", "deleted", "removed", "selected_changed", "duplicate_file", "catalog_changed"])
@pytest.mark.asyncio
async def test_package_selected_profile_and_permission_binding_are_independent_of_caller_blob(tmp_path, change):
    coordinator, service, binding, skill = install(tmp_path)
    if change == "hash":
        binding["catalog_sha256"] = "b" * 64
    elif change == "profile":
        binding["profile_sha256"] = "b" * 64
    elif change == "selected_changed":
        skill["executionSteps"][0]["objective"] = "改写原规则"
    elif change == "catalog_changed":
        binding["catalog"]["stages"]["detail"]["slots"][0]["methods"][0]["description"] = "改写既定动作"
    else:
        with sqlite3.connect(service.db_path) as db:
            if change == "duplicate_file":
                files = json.loads(db.execute("SELECT package_files FROM creation_skills").fetchone()[0])
                files.append(files[-1])
                db.execute("UPDATE creation_skills SET package_files=?", (json.dumps(files),))
            else:
                db.execute({"uninstalled": "UPDATE creation_skills SET installed=0", "deleted": "UPDATE creation_skills SET deleted_at=1", "removed": "DELETE FROM creation_skills"}[change])
    with pytest.raises(BrainstormGenerationError):
        await request(coordinator, binding, skill)
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_unknown_slot_and_forged_parent_condition_are_rejected_without_model(tmp_path):
    coordinator, service, binding, skill = install(tmp_path)
    topic = (await request(coordinator, binding, skill))["topics"][0]
    slots = binding["catalog"]["stages"]["detail"]["slots"]
    root = execution_node(slots[0])
    root["goal"] = "catalog_slot:unknown"
    with pytest.raises(BrainstormGenerationError):
        await request(coordinator, binding, skill, kind="node", topic=topic, node=root)
    condition = {"parent_question_id": "parent", "parent_option_id": "a", "parent_prompt": slots[0]["prompt_body"],
        "option_label": "累计次数才执行旧动作", "option_description": slots[0]["methods"][0]["description"]}
    with pytest.raises(BrainstormGenerationError):
        await request(coordinator, binding, skill, kind="node", topic=topic, node=execution_node(slots[1], conditions=[condition]))
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_retrieval_revocation_keeps_static_original_methods_and_no_old_source_context(tmp_path):
    coordinator, service, binding, skill = install(tmp_path)
    topic = (await request(coordinator, binding, skill))["topics"][0]
    root = binding["catalog"]["stages"]["detail"]["slots"][0]
    result = await request(coordinator, binding, skill, kind="node", topic=topic, node=execution_node(root),
        brief_edits={"root_request": "不检索个人历史资料，只用当前输入"})
    assert result["question"]["context_details"] == ""
    assert all(not {"memory_ids", "memory_evidence"}.intersection(option) for option in result["question"]["options"])
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_static_api_does_not_wait_for_inference_queue_and_authenticates_caller_package(tmp_path, monkeypatch):
    app_module = importlib.import_module("creation.app")
    coordinator, service, binding, skill = install(tmp_path)
    monkeypatch.setattr(app_module, "brainstorm_stage_coordinator", coordinator)
    def no_model_slot():
        raise AssertionError("static compiler waited for model queue")
    monkeypatch.setattr(app_module, "get_global_queue", no_model_slot)
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {"stage": "detail", "kind": "plan", "root_request": "保留已有规则", "decisions": [],
                   "selected_skills": [skill], "trusted_catalog": binding}
        response = await client.post("/creation/brainstorm/stage/batch", json=payload)
        assert response.status_code == 200
        payload["trusted_catalog"]["catalog_sha256"] = "b" * 64
        invalid = await client.post("/creation/brainstorm/stage/batch", json=payload)
        assert invalid.status_code == 502
    assert not service.model_calls


@pytest.mark.asyncio
async def test_catalog_cancellation_checkpoint_prevents_publishing(tmp_path):
    coordinator, service, binding, skill = install(tmp_path)
    task = asyncio.create_task(request(coordinator, binding, skill))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_early_terminal_catalog_does_not_invent_questions_to_fill_depth(tmp_path):
    coordinator, service, binding, skill = install(tmp_path)
    plan = await request(coordinator, binding, skill, limits={"max_depth": 3})
    topic = plan["topics"][0]
    slots = binding["catalog"]["stages"]["detail"]["slots"]
    root = await request(coordinator, binding, skill, kind="node", topic=topic,
        node=execution_node(slots[0]), limits={"max_depth": 3})
    option = root["question"]["options"][0]
    condition = {"parent_question_id": root["question"]["id"], "parent_option_id": option["id"],
        "parent_prompt": root["question"]["prompt"], "option_label": option["label"],
        "option_description": option["description"]}
    leaf = await request(coordinator, binding, skill, kind="node", topic=topic,
        node=execution_node(slots[1], conditions=[condition]), limits={"max_depth": 3})
    assert leaf["next_goals"] == [] and service.model_calls == []


@pytest.mark.asyncio
async def test_over_budget_catalog_is_rejected_before_any_plan_or_paid_node(tmp_path):
    value = directory()
    stage = value["stages"]["detail"]
    fourth = copy.deepcopy(stage["slots"][1])
    fourth.update(id="fourth", prompt_body="集中记录如何维护额外条目索引？")
    stage["slots"].append(fourth)
    stage["slots"][0]["methods"][0]["child_slot_ids"].append("fourth")
    coordinator, service, binding, skill = install(tmp_path, value)
    with pytest.raises(BrainstormGenerationError):
        await request(coordinator, binding, skill, limits={"max_questions_per_topic": 3})
    assert service.model_calls == []


@pytest.mark.parametrize("field", ["question", "prompt_body", "params", "memory_choice"])
@pytest.mark.asyncio
async def test_request_cannot_inject_free_policy_text_into_static_node(tmp_path, field):
    coordinator, service, binding, skill = install(tmp_path)
    topic = (await request(coordinator, binding, skill))["topics"][0]
    node = execution_node(binding["catalog"]["stages"]["detail"]["slots"][0])
    node[field] = "新增等待后才执行既定动作"
    with pytest.raises(BrainstormGenerationError):
        await request(coordinator, binding, skill, kind="node", topic=topic, node=node)
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_legacy_unconfigured_stage_keeps_original_model_budget(tmp_path):
    from tests.brainstorm_axis_fixtures import axis_plan_manifest, axis_plan_raw
    coordinator, service, binding, skill = install(tmp_path)
    service.responses = [json.dumps(axis_plan_raw("direction", ["audience"])),
        json.dumps({"checks": {"topic0_goal": "pass"}})]
    async def no_sources(*args):
        return {"status": "empty", "sources": []}
    coordinator._stage_memory = no_sources
    coordinator._check_stage_consistency = BrainstormStageCoordinator._check_stage_consistency.__get__(coordinator)
    result = await request(coordinator, binding, skill, stage="direction")
    assert result == axis_plan_manifest("direction", ["audience"])
    assert [call["num_predict"] for call in service.model_calls] == [3000, 1200]
    assert set(service.model_calls[0]["json_schema"]["properties"]) == {"axis_ids"}
    reviewed = json.loads(service.prompts[1])
    assert reviewed["validation_task"]["units"][0]["text"] == result["topics"][0]["goal"]
    assert reviewed["authority"]["user_constraints"]["selected_skills"] == [skill]


@pytest.mark.asyncio
async def test_readonly_verification_never_creates_missing_database(tmp_path):
    coordinator, service, binding, skill = install(tmp_path)
    missing = tmp_path / "missing.db"
    service.db_path = str(missing)
    with pytest.raises(BrainstormGenerationError):
        await request(coordinator, binding, skill)
    assert not missing.exists() and service.model_calls == []


@pytest.mark.asyncio
async def test_previously_prepared_body_cannot_be_reused_under_catalog_prefix(tmp_path):
    coordinator, service, binding, skill = install(tmp_path)
    topic = (await request(coordinator, binding, skill))["topics"][0]
    root = binding["catalog"]["stages"]["detail"]["slots"][0]
    with pytest.raises(BrainstormGenerationError):
        await request(coordinator, binding, skill, kind="node", topic=topic, node=execution_node(root),
            plan={"prepared_question_bodies": [root["prompt_body"]], "prepared_question_prompts": []})
    assert service.model_calls == []


@pytest.mark.parametrize("slot_index", [0, 1])
@pytest.mark.asyncio
async def test_confirmed_catalog_question_blocks_whole_graph_before_plan(tmp_path, slot_index):
    coordinator, service, binding, skill = install(tmp_path)
    slot = binding["catalog"]["stages"]["detail"]["slots"][slot_index]
    decisions = [{"question_id": "answered", "question": slot["prompt_body"],
                  "answer_source": "user", "answer": "已经确定具体做法"}]
    original = copy.deepcopy(decisions)
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_reopened") as caught:
        await request(coordinator, binding, skill, decisions=decisions)
    assert caught.value.decision_ids == ["decision_1"]
    assert decisions == original and service.model_calls == []


@pytest.mark.asyncio
async def test_confirmed_option_set_cannot_be_presented_as_new_methods(tmp_path):
    coordinator, service, binding, skill = install(tmp_path)
    methods = binding["catalog"]["stages"]["detail"]["slots"][0]["methods"]
    decisions = [{"question_id": "fixed_" + str(index), "question": "既有共同适用规则" + str(index),
        "answer_source": "user", "answer": method["description"],
        "selected_options": [{"label": method["label"], "description": method["description"]}]}
        for index, method in enumerate(methods)]
    original = copy.deepcopy(decisions)
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_reopened") as caught:
        await request(coordinator, binding, skill, decisions=decisions)
    assert caught.value.decision_ids == ["decision_1", "decision_2"]
    assert decisions == original and service.model_calls == []


@pytest.mark.parametrize("source,edit", [("user_cleared", None), ("user", ""), ("agent_assumption", None), ("user_excluded", None)])
@pytest.mark.asyncio
async def test_cleared_excluded_or_unconfirmed_question_is_not_fixed_authority(tmp_path, source, edit):
    coordinator, service, binding, skill = install(tmp_path)
    slot = binding["catalog"]["stages"]["detail"]["slots"][0]
    decisions = [{"question_id": "old", "question": slot["prompt_body"], "answer_source": source,
                  "answer": "旧值", "selected_options": copy.deepcopy(slot["methods"])}]
    edits = {"old": edit} if edit is not None else {}
    original = copy.deepcopy(decisions)
    result = await request(coordinator, binding, skill, decisions=decisions, brief_edits=edits)
    assert len(result["topics"]) == 1 and decisions == original and service.model_calls == []


@pytest.mark.asyncio
async def test_manual_answer_still_marks_original_question_confirmed(tmp_path):
    coordinator, service, binding, skill = install(tmp_path)
    slot = binding["catalog"]["stages"]["detail"]["slots"][0]
    decisions = [{"question_id": "edited", "question": slot["prompt_body"],
                  "answer_source": "agent_assumption", "answer": "旧假设"}]
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_reopened"):
        await request(coordinator, binding, skill, decisions=decisions, brief_edits={"edited": "当前人工确认的具体做法"})
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_manual_revision_does_not_reactivate_old_selected_method_pairs(tmp_path):
    coordinator, service, binding, skill = install(tmp_path)
    methods = binding["catalog"]["stages"]["detail"]["slots"][0]["methods"]
    decisions = [{"question_id": "fixed_" + str(index), "question": "修订前的其他问题" + str(index),
        "answer_source": "user", "answer": "旧选择", "selected_options": [copy.deepcopy(method)]}
        for index, method in enumerate(methods)]
    edits = {item["question_id"]: "当前人工回答，旧选项仅是修订前语境" for item in decisions}
    result = await request(coordinator, binding, skill, decisions=decisions, brief_edits=edits)
    assert len(result["topics"]) == 1 and service.model_calls == []
