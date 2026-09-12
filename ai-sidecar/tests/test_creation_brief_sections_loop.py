import json

import pytest

from creation.agent_loop import CreationAgentLoop, LoopState
from creation.delivery_contract import remember_delivery_failures
from creation.operations import OperationError
from tests.test_creation_brief_coverage import brief_fixture
from tests.test_creation_delivery_contract import DeliveryService, contract
from tests.test_creation_operations import run_args


def prepared():
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**run_args("", "按已确认内容写完整方案"), model_mode="local",
                            creation_mode="brainstorm", creation_brief=brief_fixture(9))
    state.environment.update(operation={"kind": "generate"}, input_base_document="",
                             input_contract=contract(), routed_skill_ids=[],
                             document_identity={"title": "完整方案"})
    loop._input_context(state)
    writer = {"id": "document_writer_agent", "name": "生成文档", "kind": "agent", "action": "writer"}
    state.plan = [writer, {"kind": "agent", "id": "delivery_validation", "name": "核对", "action": "delivery_check"}]
    state.cursor = 1
    return loop, state, writer


async def execute(loop, state, step):
    return [event async for event in loop._execute_step(state, step, creation_model=None,
                                                       creation_api_key=None, creation_base_url=None)]


def install_plan(monkeypatch):
    import creation.brief_writing as module
    calls = []
    async def plan(service, root, decisions, title):
        calls.append(decisions)
        ids = [entry["id"] for entry in decisions if entry["source"] == "user"]
        return {"sections": [{"id": "section{}".format(i), "title": "主题{}".format(i),
                "purpose": "落实本组选择", "decision_ids": ids[i*3:i*3+3]} for i in range(3)],
                "global_decision_ids": []}
    monkeypatch.setattr(module, "plan_brief_sections", plan)
    return module, calls


def install_delivery_reviews(monkeypatch, loop, statuses=("pass",)):
    """Keep the real loop and replace only the model-backed delivery service."""
    reports = iter(statuses)
    checked = []

    async def review(instruction, document, input_contract, environment):
        checked.append(document)
        status = next(reports)
        return {"status": status, "checks": [
            {"id": item["id"], "passed": status == "pass", "reason": "核对当前完整正文",
             "evidence": document if status == "pass" else ""}
            for item in input_contract["acceptance"]],
            "corrections": ["修正正文中的待解决问题"] if status == "revise" else []}

    monkeypatch.setattr(loop.service, "review_creation_delivery", review)
    return checked


def checkpoint_run_args(state, document=""):
    return {**run_args(document, state.user_message), "creation_mode": "brainstorm",
            "creation_brief": state.environment["creation_brief"], "model_mode": state.model_mode}


def display_document(events, previous=""):
    """Mirror CreationPanel: previews become the next request's current_document."""
    for event in events:
        if event["type"] in {"document.preview", "document.replaced"}:
            previous = event["data"]["content"]
    return previous


def paused_checkpoint(events):
    return next(event["data"]["continuation"] for event in reversed(events)
                if event["type"] == "run.paused")


@pytest.mark.asyncio
async def test_sections_keep_every_choice_and_only_assemble_complete_current_cycle(monkeypatch):
    module, calls = install_plan(monkeypatch)
    loop, state, writer = prepared()
    observed = []
    async def write(service, **args):
        observed.append(args)
        own = set(args["section"]["decision_ids"])
        return "\n\n".join(entry["value"] + "：给出输入、步骤、产物和验证说明。" * 4
                          for entry in args["decisions"] if entry["id"] in own)
    monkeypatch.setattr(module, "write_brief_section", write)
    await execute(loop, state, writer)
    assert len(calls) == 1 and len(state.plan) == 6
    section_steps = state.plan[1:4]
    for step in section_steps:
        events = await execute(loop, state, step)
        assert state.current_document == "" and not any(event["type"] == "document.replaced" for event in events)
    events = await execute(loop, state, state.plan[4])
    assert all("确认方向{}".format(i) in state.current_document for i in range(9))
    assert state.current_document.index("主题0") < state.current_document.index("主题1") < state.current_document.index("主题2")
    assert any(event["type"] == "document.replaced" for event in events)
    state.environment["delivery_repair_count"] = 1
    await loop._schedule_brief_sections(state, writer)
    assert len(calls) == 1  # reuse validated ownership, not another free-form plan
    with pytest.raises(OperationError, match="章节尚未完整"):
        loop._assembled_brief_sections(state, 1)


@pytest.mark.asyncio
async def test_external_section_uses_existing_pause_result_and_checkpoint_contract(monkeypatch):
    _, _ = install_plan(monkeypatch)
    loop, state, writer = prepared()
    await execute(loop, state, writer)
    state.model_mode = "external"
    step = state.plan[1]
    events = await execute(loop, state, step)
    assert any(event["type"] == "model.request" for event in events)
    pending = state.pending_model_step
    assert pending["step"]["brief_section_id"] == "section0"
    payload = json.JSONDecoder().raw_decode(pending["user_prompt"])[0]
    assert [entry["value"] for entry in payload["owned_decisions"]] == ["确认方向{}".format(i) for i in range(3)]
    assert "确认方向8" in pending["user_prompt"]  # read-only cross-chapter constraints
    restored = LoopState.restore(state.serializable())
    result = "完整的当前章节内容，保留对应选择与参数。"
    events = [event async for event in loop._apply_model_result(restored, result)]
    assert restored.pending_model_step is None
    assert restored.environment["brief_writing"]["sections"]["section0"]["content"] == result
    assert restored.current_document == "" and any(event["type"] == "document.preview" for event in events)
    with pytest.raises(OperationError, match="章节尚未完整"):
        loop._assembled_brief_sections(restored, 0)


@pytest.mark.parametrize("change", ["partial_operation", "skill", "few_choices"])
def test_partial_edit_and_skill_paths_keep_original_writer(change):
    loop, state, writer = prepared()
    if change == "partial_operation": state.environment["operation"]["kind"] = "transform"
    if change == "skill": state.environment["applied_skills"] = [{"id": "chosen", "name": "已采用", "skill_instructions": "规则"}]
    if change == "few_choices": state.environment["input_context"]["brainstorm_decisions"] = state.environment["input_context"]["brainstorm_decisions"][:8]
    assert not loop._uses_brief_sections(state, writer)


def test_explicit_full_regeneration_uses_sections_even_with_existing_draft():
    loop, state, writer = prepared()
    state.environment["input_base_document"] = "原有草稿，用户已要求按脑暴全部选择重新生成"
    state.current_document = state.environment["input_base_document"]
    assert loop._uses_brief_sections(state, writer)
    materials = state.environment["input_base_document"]
    assert materials == state.current_document  # no mutation before generation


@pytest.mark.asyncio
async def test_external_loop_resumes_every_section_with_the_actual_frontend_preview(monkeypatch):
    install_plan(monkeypatch)
    loop, state, _ = prepared()
    checked = install_delivery_reviews(monkeypatch, loop)
    state.model_mode = "external"
    state.cursor = 0
    events = [event async for event in loop.run(**checkpoint_run_args(state),
                                               resume_checkpoint=state.serializable())]
    shown = ""
    request_ids = []
    outputs = ["第{}章的完整正文，保留本章对应的确认参数与具体建议。".format(i) for i in range(3)]
    for index, output in enumerate(outputs):
        pending = paused_checkpoint(events)
        step = pending["pending_model_step"]["step"]
        assert step["brief_section_id"] == "section{}".format(index)
        request_ids.append(pending["pending_model_step"]["request_id"])
        assert not any(event["type"] == "run.completed" for event in events)
        if index:
            assert shown and pending["current_document"] == ""
            with pytest.raises(OperationError, match="文档已发生变化"):
                _ = [event async for event in loop.run(**checkpoint_run_args(state, shown + "\n用户手动修改"),
                    resume_state=pending, model_result=output)]
        events = [event async for event in loop.run(**checkpoint_run_args(state, shown),
            resume_state=pending, model_result=output)]
        shown = display_document(events, shown)
    completed = next(event for event in events if event["type"] == "run.completed")
    document = completed["data"]["document"]
    assert len(set(request_ids)) == 3
    assert all(output in document for output in outputs)
    assert document == shown == checked[0]
    assert len(checked) == 1 and loop.service.writes == 0


@pytest.mark.asyncio
async def test_failed_required_section_keeps_its_checkpoint_and_resumes_only_unfinished_work(monkeypatch):
    module, _ = install_plan(monkeypatch)
    loop, state, _ = prepared()
    checked = install_delivery_reviews(monkeypatch, loop)
    calls = []

    async def write(service, **args):
        identifier = args["section"]["id"]
        calls.append(identifier)
        if identifier == "section1" and calls.count(identifier) == 1:
            raise RuntimeError("temporary model connection failure")
        return "已完成{}的正文，保留独立的参数与对应落实安排。".format(identifier)

    monkeypatch.setattr(module, "write_brief_section", write)
    state.cursor = 0
    interrupted = []
    with pytest.raises((OperationError, RuntimeError)):
        async for event in loop.run(**checkpoint_run_args(state), resume_checkpoint=state.serializable()):
            interrupted.append(event)
    saved = next(event["data"]["checkpoint"] for event in reversed(interrupted)
                 if event["type"] == "operation.checkpoint")
    assert saved["plan"][saved["cursor"]]["brief_section_id"] == "section1"
    assert calls == ["section0", "section1"]
    assert not checked
    assert not any(event["type"] in {"document.replaced", "run.completed"} for event in interrupted)

    resumed = [event async for event in loop.run(**checkpoint_run_args(state, display_document(interrupted)),
                                                resume_checkpoint=saved)]
    assert calls == ["section0", "section1", "section1", "section2"]
    completed = next(event for event in resumed if event["type"] == "run.completed")
    assert all("已完成section{}的正文".format(i) in completed["data"]["document"] for i in range(3))
    assert len(checked) == 1


@pytest.mark.asyncio
async def test_quality_rewrite_uses_a_new_section_attempt_without_a_delivery_repair(monkeypatch):
    module, plan_calls = install_plan(monkeypatch)
    loop, state, writer = prepared()

    async def write(service, **args):
        return "旧轮次{}的完整正文。".format(args["section"]["id"])

    monkeypatch.setattr(module, "write_brief_section", write)
    await execute(loop, state, writer)
    first_steps = state.plan[1:4]
    for step in first_steps:
        await execute(loop, state, step)
    await execute(loop, state, state.plan[4])
    old_document = state.current_document
    old_cycle = state.environment["brief_writing"]["cycle"]
    state.cursor = len(state.plan)
    state.environment["quality_issues"] = [{"code": "test_missing_content", "severity": "hard",
        "agent_id": "document_writer_agent", "summary": "第二章缺少输入输出定义",
        "evidence": {"short_sections": ["主题1"], "missing": ["输入", "输出"]}}]
    loop._replan_after_feedback(state, {"id": "quality_review_agent", "action": "review"}, status="completed")
    retry = state.plan[state.cursor]
    assert retry["action"] == "writer" and retry["quality_cycle"] == 1
    state.cursor += 1  # run advances its cursor before executing the writer.
    await execute(loop, state, retry)
    new_steps = state.plan[state.cursor:state.cursor + 3]
    new_cycle = state.environment["brief_writing"]["cycle"]
    assert state.environment.get("delivery_repair_count", 0) == 0
    assert new_cycle != old_cycle and len(plan_calls) == 1
    assert {step["id"] for step in first_steps}.isdisjoint(step["id"] for step in new_steps)
    from creation.brief_writing import build_section_prompts
    _, prompt = build_section_prompts(**loop._brief_section_prompt_args(state, new_steps[1]))
    payload = json.JSONDecoder().raw_decode(prompt)[0]
    assert payload["repair_findings"] == [{"id": "test_missing_content", "reason": "第二章缺少输入输出定义",
        "evidence": {"short_sections": ["主题1"], "missing": ["输入", "输出"]}, "source": "quality_review"}]
    assert not loop._brief_section_prompt_args(state, new_steps[0])["section"]["repair_findings"]
    _ = [event async for event in loop._complete_model_step(state, new_steps[0], "新轮次第一章的完整正文。")]
    preview = loop._assembled_brief_sections(state, new_cycle, complete=False)
    assert "新轮次第一章" in preview and "旧轮次" not in preview
    with pytest.raises(OperationError, match="章节尚未完整"):
        loop._assembled_brief_sections(state, new_cycle)
    assert state.current_document == old_document


@pytest.mark.asyncio
async def test_post_loop_repair_executes_new_sections_before_rechecking_the_assembled_document(monkeypatch):
    module, plan_calls = install_plan(monkeypatch)
    loop, state, writer = prepared()
    checked = install_delivery_reviews(monkeypatch, loop)
    await execute(loop, state, writer)
    record = state.environment["brief_writing"]
    for section in record["plan"]["sections"]:
        record["sections"][section["id"]] = {"cycle": record["cycle"], "content": "旧章" + section["id"]}
    state.current_document = loop._assembled_brief_sections(state, record["cycle"])
    state.environment["document"] = state.current_document
    state.cursor = len(state.plan)
    state.environment["delivery_repair_count"] = 1
    calls = []

    async def write(service, **args):
        calls.append(args["section"]["id"])
        return "修正后的{}完整正文，已落实本章的全部有效要求。".format(args["section"]["id"])

    monkeypatch.setattr(module, "write_brief_section", write)
    report = {"status": "revise", "checks": [], "corrections": ["修正遗漏"]}
    state.environment["delivery_review"] = report
    events = [event async for event in loop._repair_delivery(state, state.current_document, report,
        creation_model=None, creation_api_key=None, creation_base_url=None)]
    assert calls == ["section0", "section1", "section2"] and len(plan_calls) == 1
    assert state.cursor == len(state.plan) and not state.pending_model_step
    assert len(checked) == 1 and checked[0] == state.current_document
    assert "旧章" not in checked[0] and all("修正后的section{}".format(i) in checked[0] for i in range(3))
    assert not state.environment.get("delivery_repair_stalled")
    assert any(event["type"] == "delivery.rechecked" for event in events)
    checkpoints = [event["data"]["checkpoint"] for event in events if event["type"] == "operation.checkpoint"]
    assert len(checkpoints) >= 4 and loop.service.writes == 0


async def repair_sections(monkeypatch, contents):
    install_plan(monkeypatch)
    loop, state, writer = prepared()
    await execute(loop, state, writer)
    record = state.environment["brief_writing"]
    for index, content in enumerate(contents):
        record["sections"]["section{}".format(index)] = {"cycle": record["cycle"], "content": content}
    state.current_document = loop._assembled_brief_sections(state, record["cycle"])
    state.environment["document"] = state.current_document
    state.environment["delivery_repair_count"] = 1
    return loop, state, state.plan[1:4]


@pytest.mark.asyncio
async def test_section_repairs_keep_all_scoped_source_findings_and_their_literal_chapter_evidence(monkeypatch):
    loop, state, steps = await repair_sections(monkeypatch, ["第一章：错误文本甲。", "第二章：错误文本乙。", "第三章内容。"])
    first = {"id": "source_attributes_grounding", "reason": "经验被错误表述成已实测",
             "evidence": "错误文本甲。", "anchor_kind": "source_claim"}
    last = {"id": first["id"], "reason": "擅自补充了未经给定的身份定义", "evidence": "错误文本乙。", "passed": False}
    report = {"status": "revise", "checks": [last], "corrections": [last["reason"] + "；正文定位：" + last["evidence"]]}
    state.environment["delivery_review"] = report
    remember_delivery_failures(state.user_message, state.environment, state.environment["input_contract"],
                               report["checks"], report, [first, first])
    payloads = [loop._brief_section_prompt_args(state, step)["section"] for step in steps]
    assert payloads[0]["repair_findings"] == [{key: first[key] for key in ("id", "reason", "evidence")}]
    assert any(item["reason"] == last["reason"] and item["evidence"] == last["evidence"]
               for item in payloads[1]["repair_findings"])
    assert first["reason"] not in json.dumps(payloads[1], ensure_ascii=False)
    assert last["reason"] not in json.dumps(payloads[0], ensure_ascii=False)
    assert not payloads[2]["repair_findings"]
    # Both actual writer modes consume this same section argument projection.
    state.model_mode = "external"
    await execute(loop, state, steps[0])
    external = json.JSONDecoder().raw_decode(state.pending_model_step["user_prompt"])[0]
    assert external["repair_findings"] == payloads[0]["repair_findings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("obsolete", ["scope", "removed", "passed", "not_repairing"])
async def test_section_repairs_do_not_revive_obsolete_pending_findings(monkeypatch, obsolete):
    loop, state, steps = await repair_sections(monkeypatch, ["当前章仍有旧引用。", "第二章内容。", "第三章内容。"])
    old = {"id": "source_attributes_grounding", "reason": "不应复活的旧问题", "evidence": "旧引用。"}
    report = {"status": "revise", "checks": [{"id": "a", "passed": False, "reason": "当前全篇要求", "evidence": ""}],
              "corrections": ["当前修正要求"]}
    remember_delivery_failures(state.user_message, state.environment, state.environment["input_contract"],
                               [], report, [old])
    state.environment["delivery_review"] = report
    state.environment["delivery_pending_review"]["corrections"].append("无定位的过期修正要求")
    if obsolete == "scope":
        state.environment["delivery_pending_review"]["scope"] = "another-request"
    elif obsolete == "removed":
        state.environment["brief_writing"]["sections"]["section0"]["content"] = "已经修改后的正文。"
    elif obsolete == "passed":
        report["checks"].append({**old, "passed": True})
    else:
        state.environment["delivery_repair_count"] = 0
    args = loop._brief_section_prompt_args(state, steps[0])
    text = json.dumps(args["section"]["repair_findings"], ensure_ascii=False)
    assert "不应复活的旧问题" not in text and "无定位的过期修正要求" not in text
    assert "当前全篇要求" in text and "当前修正要求" in text


@pytest.mark.asyncio
async def test_selection_failure_is_owned_by_one_chapter_even_without_a_quote(monkeypatch):
    loop, state, steps = await repair_sections(monkeypatch, ["第一章内容。", "第二章内容。", "第三章内容。"])
    identifier = state.environment["brief_writing"]["plan"]["sections"][1]["decision_ids"][0]
    issue = {"id": identifier, "passed": False, "reason": "缺少本选择独有的样本参数", "evidence": ""}
    state.environment["delivery_review"] = {"status": "revise", "checks": [issue], "corrections": [issue["reason"]]}
    findings = [loop._brief_section_prompt_args(state, step)["section"]["repair_findings"] for step in steps]
    assert not findings[0] and not findings[2]
    assert findings[1] == [{key: issue[key] for key in ("id", "reason", "evidence")}]


@pytest.mark.asyncio
@pytest.mark.parametrize("quote,reason", [
    ("*   **时长控制**：每镜生成时间严格对齐至视频编码标准的短片段（约 2-3 秒），总时长不超过 8 秒，以适配低算力终端的快速预览与分发需求。",
     "5-8秒/2-3镜：5–8 秒/2–3 镜数字出现，但时长归属冲突，并额外声称已经验证为黄金窗口。"),
    ("*   **类目适配逻辑**：基于过往经验数据，重点聚焦服装与美妆等高静态展示成功率潜力（预估>80%）的品类，针对不同类目构建差异化的光影一致性评分验证体系。",
     "服装与美妆类目成功率较高：部分位置保留经验/预估，但另处写成经验数据显示、统计数据显示、成功率可达和必须达到的目标，丢掉原“可能”的边界。"),
])
async def test_current_owned_choice_failure_survives_an_earlier_chapters_removed_quote(monkeypatch, quote, reason):
    # These are the actual cross-chapter quote/owner patterns from the isolated
    # v7 repair. Rewriting the quoted chapter cannot resolve its later owner.
    loop, state, steps = await repair_sections(monkeypatch, [quote, "主归属章节仍待修正。", "其他章节。"])
    identifier = state.environment["brief_writing"]["plan"]["sections"][1]["decision_ids"][0]
    issue = {"id": identifier, "passed": False, "reason": reason, "evidence": quote}
    source_issue = {**issue, "id": "source_attributes_grounding", "reason": "仅针对前章旧引用的来源问题"}
    report = {"status": "revise", "checks": [issue, source_issue], "corrections": []}
    state.environment["delivery_review"] = report
    remember_delivery_failures(state.user_message, state.environment, state.environment["input_contract"],
                               report["checks"], report)
    record = state.environment["brief_writing"]
    record["cycle"] += 1
    record["sections"]["section0"] = {"cycle": record["cycle"], "content": "前章已经修正并移除旧引用。"}
    steps = [{**step, "brief_section_cycle": record["cycle"]} for step in steps]
    assert quote in state.current_document
    expected = {key: issue[key] for key in ("id", "reason", "evidence")}
    expected["evidence_location"] = "previous_quote_changed_or_removed"
    findings = [loop._brief_section_prompt_args(state, step)["section"]["repair_findings"] for step in steps]
    assert findings == [[], [expected], []]
    assert state.environment["delivery_review"] == report
    state.model_mode = "external"
    await execute(loop, state, steps[1])
    payload = json.JSONDecoder().raw_decode(state.pending_model_step["user_prompt"])[0]
    assert payload["repair_findings"] == [expected]


@pytest.mark.asyncio
async def test_removed_owned_choice_keeps_literal_report_reason_with_embedded_quote(monkeypatch):
    quote = "前章写成每镜2–3秒。"
    reason = "原文“" + quote + "”混淆了已确认的时长对象，主归属章节仍需修正。"
    loop, state, steps = await repair_sections(monkeypatch, [quote, "主归属章节。", "其他章节。"])
    identifier = state.environment["brief_writing"]["plan"]["sections"][1]["decision_ids"][0]
    state.environment["delivery_review"] = {"status": "revise", "checks": [
        {"id": identifier, "passed": False, "reason": reason, "evidence": quote}], "corrections": []}
    state.environment["brief_writing"]["sections"]["section0"]["content"] = "前章已纠正。"
    findings = loop._brief_section_prompt_args(state, steps[1])["section"]["repair_findings"]
    assert findings == [{"id": identifier, "reason": reason, "evidence": quote,
                         "evidence_location": "previous_quote_changed_or_removed"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("current_passed", [False, True])
async def test_removed_historical_choice_without_a_current_failure_is_not_revived(monkeypatch, current_passed):
    quote = "先前章节中的旧定位。"
    loop, state, steps = await repair_sections(monkeypatch, [quote, "主归属章节。", "其他章节。"])
    identifier = state.environment["brief_writing"]["plan"]["sections"][1]["decision_ids"][0]
    old = {"id": identifier, "passed": False, "reason": "旧的选择失败", "evidence": quote}
    initial = {"status": "revise", "checks": [old], "corrections": []}
    remember_delivery_failures(state.user_message, state.environment, state.environment["input_contract"],
                               initial["checks"], initial)
    current = {"id": "a", "passed": False, "reason": "当前全篇要求", "evidence": ""}
    state.environment["delivery_review"] = {"status": "revise", "checks": [current], "corrections": []}
    if current_passed:
        state.environment["delivery_review"]["checks"].append({**old, "passed": True})
    state.environment["brief_writing"]["sections"]["section0"]["content"] = "前章已修正。"
    for step in steps:
        findings = loop._brief_section_prompt_args(state, step)["section"]["repair_findings"]
        assert findings == [{key: current[key] for key in ("id", "reason", "evidence")}]


@pytest.mark.asyncio
async def test_multi_batch_composite_reasons_use_independent_rows_and_do_not_repeat_other_chapters(monkeypatch):
    contents = ["第一章无来源声明。" * 300, "第二章无来源声明。" * 300, "第三章正常。"]
    loop, state, steps = await repair_sections(monkeypatch, contents)
    rows = [{"id": "source_attributes_grounding", "passed": False,
             "reason": "独立问题{}".format(index), "evidence": content} for index, content in enumerate(contents[:2])]
    composite = {**rows[0], "reason": rows[0]["reason"] + "\n" + rows[1]["reason"] + "；正文定位：" + rows[1]["evidence"]}
    report = {"status": "revise", "checks": [composite],
              "corrections": ["纠正这两处：" + rows[0]["evidence"] + "；" + rows[1]["evidence"]]}
    state.environment["delivery_review"] = report
    remember_delivery_failures(state.user_message, state.environment, state.environment["input_contract"],
                               report["checks"], report, rows)
    all_findings = [loop._brief_section_prompt_args(state, step)["section"]["repair_findings"] for step in steps]
    for index in range(2):
        serialized = json.dumps(all_findings[index], ensure_ascii=False)
        assert contents[index] in serialized and contents[1 - index] not in serialized
        assert "独立问题{}".format(index) in serialized and "独立问题{}".format(1 - index) not in serialized
        assert serialized.count(contents[index]) == 1
    assert not all_findings[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_anchor_also_has_one_reason", [False, True])
async def test_human_rejection_combined_reason_keeps_its_distinct_original_location(monkeypatch, first_anchor_also_has_one_reason):
    # The real v7 human fixture groups F01 + F02 on the opening paragraph, while
    # each reason also occurs separately at other locations. Reason-only set
    # coverage must not erase either meaning from this first literal anchor.
    opening = "当前快手灵机平台虽已具备 SOTA（State-of-the-Art）视频生成模型能力，但在吸引非 L0（高阶/成熟级）商家利用该工具进行规模化内容生产方面仍面临显著瓶颈。"
    other_identity = "非 L0 商家（即缺乏专业拍摄设备或熟练度的中小商户）。"
    other_measurement = "经验数据显示，此类静态展示方案在服装与美妆类目的生成成功率可达 80% 以上。"
    loop, state, steps = await repair_sections(monkeypatch, [opening, other_identity, other_measurement])
    identity_reason = "非 L0 一处解释为高阶/成熟级，另一处解释为缺少设备或熟练度的中小商户；原文没有任何此类定义。"
    evidence_reason = "根请求说可以使用 SOTA，正文却称平台已具备该能力；经验判断又变成统计证据，没有相应实测材料。"
    combined = {"id": "no_hallucination", "reason": identity_reason + "\n" + evidence_reason,
                "evidence": opening, "anchor_kind": "source_claim"}
    rows = [combined, {**combined, "reason": identity_reason, "evidence": other_identity},
            {**combined, "reason": evidence_reason, "evidence": other_measurement}]
    if first_anchor_also_has_one_reason:
        rows.append({**combined, "reason": identity_reason})
    report = {"status": "revise", "checks": [{"id": "no_hallucination", "passed": False,
              "reason": "应纠正未经给定的身份定义及经验实测混淆", "evidence": other_identity}], "corrections": []}
    state.environment["delivery_review"] = report
    remember_delivery_failures(state.user_message, state.environment, state.environment["input_contract"],
                               report["checks"], report, rows)
    findings = loop._brief_section_prompt_args(state, steps[0])["section"]["repair_findings"]
    assert {key: combined[key] for key in ("id", "reason", "evidence")} in findings
    assert all(row["evidence"] == opening for row in findings)
    state.model_mode = "external"
    await execute(loop, state, steps[0])
    payload = json.JSONDecoder().raw_decode(state.pending_model_step["user_prompt"])[0]
    assert {key: combined[key] for key in ("id", "reason", "evidence")} in payload["repair_findings"]


@pytest.mark.asyncio
async def test_whole_document_evidence_projects_only_literal_current_section_lines(monkeypatch):
    contents = ["第一章的错误声明。", "第二章的另一错误声明。", "第三章的边界声明。"]
    loop, state, steps = await repair_sections(monkeypatch, contents)
    state.environment["delivery_review"] = {"status": "revise", "checks": [
        {"id": "a", "passed": False, "reason": "统一纠正表达的事实确定性", "evidence": state.current_document}],
        "corrections": []}
    for index, step in enumerate(steps):
        findings = loop._brief_section_prompt_args(state, step)["section"]["repair_findings"]
        assert findings == [{"id": "a", "reason": "统一纠正表达的事实确定性", "evidence": contents[index],
                             "evidence_scope": "本章逐字匹配的原文片段"}]


@pytest.mark.asyncio
async def test_rewritten_first_chapter_does_not_broadcast_removed_quote_to_remaining_chapters(monkeypatch):
    contents = ["前章错误原文。" * 200, "后章错误原文。" * 200, "尾章正常。"]
    loop, state, steps = await repair_sections(monkeypatch, contents)
    rows = [{"id": "source_attributes_grounding", "passed": False,
             "reason": "问题{}".format(index), "evidence": content} for index, content in enumerate(contents[:2])]
    report = {"status": "revise", "checks": [rows[0]],
              "corrections": ["修正前章：" + contents[0], "共同纠正：" + contents[0] + "；" + contents[1]]}
    state.environment["delivery_review"] = report
    remember_delivery_failures(state.user_message, state.environment, state.environment["input_contract"],
                               report["checks"], report, rows)
    # The complete reviewed body remains unchanged until the new assembler runs.
    state.environment["brief_writing"]["sections"]["section0"]["content"] = "前章已纠正且移除旧引用。"
    findings = loop._brief_section_prompt_args(state, steps[1])["section"]["repair_findings"]
    serialized = json.dumps(findings, ensure_ascii=False)
    assert "问题1" in serialized and contents[1] in serialized
    assert "问题0" not in serialized and contents[0] not in serialized and "修正前章" not in serialized
    assert not loop._brief_section_prompt_args(state, steps[2])["section"]["repair_findings"]


@pytest.mark.asyncio
async def test_quality_findings_preserve_current_writer_evidence_without_skill_catalog_rules(monkeypatch):
    loop, state, steps = await repair_sections(monkeypatch, ["旧稿错误句。", "其他正文。", "末章正文。"])
    state.environment["quality_issues"] = [
        {"code": "writer_missing", "agent_id": "document_writer_agent", "summary": "本章需要修正", "evidence": "旧稿错误句。"},
        {"code": "other_skill", "agent_id": "skill_step", "summary": "候选技能的规则不得传入"},
        {"code": "already_passed", "agent_id": "document_writer_agent", "summary": "已经通过", "passed": True},
        {"code": "skill_bound", "agent_id": "document_writer_agent", "skill_id": "not-applied", "summary": "未采用的技能规范"},
        {"code": "skill_capability", "agent_id": "document_writer_agent", "required_capabilities": ["skill:not-applied"],
         "summary": "仅供未采用技能的能力约束"},
    ]
    findings = [loop._brief_section_prompt_args(state, step)["section"]["repair_findings"] for step in steps]
    assert findings[0] == [{"id": "writer_missing", "reason": "本章需要修正", "evidence": "旧稿错误句。", "source": "quality_review"}]
    assert not findings[1] and not findings[2]
    state.environment["brief_writing"]["sections"]["section0"]["content"] = "该章已经修正。"
    assert all(not loop._brief_section_prompt_args(state, step)["section"]["repair_findings"] for step in steps)


@pytest.mark.asyncio
async def test_removed_multiline_claim_cannot_relocate_through_another_chapters_shared_footer(monkeypatch):
    quote = "错误身份。\n共同脚注。"
    loop, state, steps = await repair_sections(monkeypatch, [quote, "第二章正文。\n共同脚注。", "共同脚注。"])
    issue = {"id": "source_attributes_grounding", "reason": "原第一章身份错误", "evidence": quote,
             "anchor_kind": "source_claim"}
    report = {"status": "revise", "checks": [{"id": "a", "passed": False, "reason": "当前全局要求", "evidence": ""}],
              "corrections": []}
    state.environment["delivery_review"] = report
    remember_delivery_failures(state.user_message, state.environment, state.environment["input_contract"],
                               report["checks"], report, [issue])
    assert any(item["reason"] == issue["reason"] for item in loop._brief_section_prompt_args(state, steps[0])["section"]["repair_findings"])
    state.environment["brief_writing"]["sections"]["section0"]["content"] = "已经纠正第一章。"
    for step in steps:
        assert issue["reason"] not in json.dumps(loop._brief_section_prompt_args(state, step)["section"]["repair_findings"], ensure_ascii=False)


@pytest.mark.parametrize('failed_kind,expected', [('confirmed', True), ('other', False), ('passed', False)])
def test_small_brief_switches_to_assigned_sections_only_after_confirmed_coverage_failure(failed_kind, expected):
    loop, _, writer = prepared()
    state = loop._new_state(**run_args('', '按已确认内容写完整方案'), model_mode='local',
                            creation_mode='brainstorm', creation_brief=brief_fixture(3))
    state.environment.update(operation={'kind': 'generate'}, input_contract=contract(), routed_skill_ids=[])
    context = loop._input_context(state)
    identifier = context['brainstorm_decisions'][0]['id']
    state.environment['delivery_review'] = {'status': 'revise', 'checks': [
        {'id': identifier if failed_kind != 'other' else 'source_scope_grounding',
         'passed': failed_kind == 'passed', 'reason': '本项尚未展开'}]}
    assert not loop._uses_brief_sections(state, writer)
    assert loop._uses_brief_sections(state, {**writer, 'delivery_regenerate': True}) is expected
    assert not loop._uses_brief_sections(state, {**writer, 'delivery_regenerate': True, 'brief_assembled': True})


def test_source_owned_recovery_never_falls_back_to_whole_document_polisher():
    loop, state, writer = prepared()
    state.environment['input_context']['brainstorm_decisions'] = \
        state.environment['input_context']['brainstorm_decisions'][:2]
    state.environment['brief_writing'] = {
        'source_owned': True, 'output_mode': 'proposal', 'plan': {'sections': [], 'global_decision_ids': []},
        'sections': {}, 'cycle': 0,
    }
    state.environment['delivery_review'] = {'status': 'revise', 'checks': [
        {'id': 'source_attributes_grounding', 'passed': False, 'reason': '核对建议语态'}]}
    assert loop._uses_brief_sections(state, {**writer, 'delivery_regenerate': True})


async def test_small_coverage_recovery_schedules_all_choices_without_free_outline(monkeypatch):
    async def mode(*args, **kwargs):
        return "proposal"
    monkeypatch.setattr("creation.brief_writing.source_owned_output_mode", mode)
    import creation.brief_writing as module
    async def unexpected(*args, **kwargs):
        raise AssertionError('small coverage recovery must use source-owned plan')
    monkeypatch.setattr(module, 'plan_brief_sections', unexpected)
    loop, _, writer = prepared()
    state = loop._new_state(**run_args('', '按已确认内容写完整方案'), model_mode='local',
                            creation_mode='brainstorm', creation_brief=brief_fixture(3))
    state.environment.update(operation={'kind': 'generate'}, input_contract=contract(), routed_skill_ids=[])
    context = loop._input_context(state)
    state.environment['delivery_review'] = {'status': 'revise', 'checks': [
        {'id': context['brainstorm_decisions'][0]['id'], 'passed': False, 'reason': '未展开'}]}
    await execute(loop, state, {**writer, 'delivery_regenerate': True})
    plan = state.environment['brief_writing']['plan']
    assert [row['decision_ids'] for row in plan['sections']] == [[row['id']] for row in context['brainstorm_decisions']]
    assert sum('brief_section_id' in step for step in state.plan) == 3


async def test_fresh_source_owned_rewrite_does_not_turn_reviewer_inventions_into_inputs(monkeypatch):
    async def mode(*args, **kwargs):
        return "proposal"
    monkeypatch.setattr("creation.brief_writing.source_owned_output_mode", mode)
    loop, _, writer = prepared()
    state = loop._new_state(**run_args('', '按已确认内容写完整方案'), model_mode='local',
                            creation_mode='brainstorm', creation_brief=brief_fixture(3))
    state.environment.update(operation={'kind': 'generate'}, input_contract=contract(), routed_skill_ids=[])
    context = loop._input_context(state)
    identifier = context['brainstorm_decisions'][0]['id']
    state.environment['delivery_review'] = {'status': 'revise', 'checks': [
        {'id': identifier, 'passed': False, 'reason': '必须新增幽灵设备已购置这个事实', 'evidence': '旧稿推测'}],
        'corrections': ['必须新增幽灵设备已购置这个事实']}
    await execute(loop, state, {**writer, 'delivery_regenerate': True})
    step = next(step for step in state.plan if step.get('brief_section_id'))
    args = loop._brief_section_prompt_args(state, step)
    assert '幽灵设备已购置' not in json.dumps(args, ensure_ascii=False)
    assert args['section']['repair_findings'][0]['id'] == identifier
    assert args['section']['repair_findings'][0]['source'] == 'input_contract'
    assert args['decisions'] == context['brainstorm_decisions']


@pytest.mark.asyncio
async def test_restored_acceptance_rebinds_current_choices_without_reference_promotion(monkeypatch):
    loop, state, _ = prepared()
    brief = state.environment["creation_brief"]
    brief["brief_markdown"] = "历史记忆参考：另一会话已确认20项决策，必须使用5秒镜头"
    state.environment["input_contract"]["acceptance"] = [{"id": "old", "criterion": "必须使用5秒镜头"}]
    state.environment["delivery_repair_count"] = 2
    state.environment["delivery_checked_hash"] = "obsolete"
    old_requirements = state.environment["input_contract"]["inputs"]
    seen = []
    async def assess(*args, **kwargs):
        seen.append(kwargs["brief_context"])
        fresh = contract()
        fresh["acceptance"] = [{"id": "current", "criterion": "遵守本轮要求"}]
        return fresh
    monkeypatch.setattr(loop.service, "assess_creation_inputs", assess)
    await loop._refresh_brainstorm_acceptance(state)
    await loop._refresh_brainstorm_acceptance(state)
    assert len(seen) == 1
    assert "20项" not in seen[0] and "5秒" not in seen[0]
    assert "已确认决策" in seen[0]
    assert state.environment["delivery_repair_count"] == 2
    assert state.environment["input_contract"]["inputs"] == old_requirements
    assert "delivery_checked_hash" not in state.environment
    ids = {item["id"] for item in state.environment["input_contract"]["acceptance"]}
    assert "old" not in ids and "current" in ids
    assert all(item["id"] in ids for item in loop._input_context(state)["brainstorm_decisions"] if item["source"] == "user")
    assert "20项" in brief["brief_markdown"]
    assert "20项" in loop._brainstorm_prompt_context(brief)


@pytest.mark.asyncio
async def test_acceptance_refresh_does_not_expand_local_edits(monkeypatch):
    loop, state, _ = prepared()
    state.environment["operation"] = {"kind": "transform"}
    original = json.dumps(state.environment["input_contract"], sort_keys=True)
    async def unexpected(*args, **kwargs):
        raise AssertionError("local edits must retain their existing scope")
    monkeypatch.setattr(loop.service, "assess_creation_inputs", unexpected)
    await loop._refresh_brainstorm_acceptance(state)
    assert json.dumps(state.environment["input_contract"], sort_keys=True) == original
