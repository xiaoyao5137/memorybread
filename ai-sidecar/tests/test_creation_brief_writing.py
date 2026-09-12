import copy
import json

import pytest

from creation.brief_writing import (
    _plan_schema,
    _validate_plan,
    build_section_prompts,
    normalize_section_content,
    plan_brief_sections,
    write_brief_section,
)
from creation.operations import OperationError


def decisions(count=9):
    return [{"id": "choice-{}".format(i), "source": "user", "dimension": "主题{}".format(i),
             "value": "选择{}".format(i), "description": "完整说明{}".format(i)} for i in range(count)]


def plan(count=9):
    return {"sections": [{"id": "s{}".format(i // 8 + 1), "title": "章节{}".format(i // 8),
                          "purpose": "承接本组主题", "decision_ids": ["choice-{}".format(j)
                              for j in range(i, min(i + 8, count))]}
                         for i in range(0, count, 8)], "global_decision_ids": ["choice-0"]}


def assignment_plan(count=9):
    return {"sections": [{key: value for key, value in section.items() if key != "decision_ids"}
                          for section in plan(count)["sections"]],
            "assignments": {"D{:02d}".format(index + 1): "s{}".format(index // 8 + 1) for index in range(count)},
            "global_decision_ids": ["D01"]}


def section_payload(user_prompt):
    return json.JSONDecoder().raw_decode(user_prompt)[0]


class Service:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def _stream_complete_agent_output(self, **kwargs):
        self.calls.append(kwargs)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        raw = json.dumps(reply, ensure_ascii=False) if isinstance(reply, dict) else reply
        midpoint = len(raw) // 2
        yield raw[:midpoint]
        yield raw[midpoint:]

    async def stream_agent_document(self, **kwargs):
        async for part in self._stream_complete_agent_output(**kwargs):
            yield part


@pytest.mark.parametrize("count", [1, 9, 33, 96])
async def test_plan_assigns_every_confirmed_id_once_without_truncation(count):
    service = Service([assignment_plan(count)])
    source = decisions(count)
    source[0]["description"] = "完整首项要求" * 700
    source[-1]["description"] += "末项要求"
    original = copy.deepcopy(source)
    result = await plan_brief_sections(service, "完整创作目标", source, "文档标题")
    assert result == plan(count)
    assert source == original
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["root_request"] == "完整创作目标"
    assert payload["document_title"] == "文档标题"
    assert payload["confirmed_decisions"] == [{**item, "id": "D{:02d}".format(i)} for i, item in enumerate(source, 1)]
    assert all(item["id"] not in service.calls[0]["user_prompt"] for item in source)
    assert service.calls[0]["json_mode"] is True
    assert service.calls[0]["disable_thinking"] is True


async def test_nonconfirmed_entries_do_not_become_assignment_requirements():
    source = decisions(1) + [{"id": "assumption", "source": "agent_assumption", "value": "未确认内容"},
                             {"id": "cleared", "source": "user_cleared", "value": ""},
                             {"id": "excluded", "source": "user_excluded", "value": "不要写"}]
    service = Service([assignment_plan(1)])
    await plan_brief_sections(service, "目标", source)
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["confirmed_decisions"] == [{**decisions(1)[0], "id": "D01"}]
    assert [item["source"] for item in payload["scope_constraints"]] == ["agent_assumption", "user_cleared", "user_excluded"]


@pytest.mark.parametrize("source", [[], decisions(97), [{"source": "user", "value": "没有标识"}],
                                    decisions(1) * 2, [{"id": "empty", "source": "user", "value": ""}]])
async def test_invalid_source_rejects_before_model_without_slicing(source):
    service = Service([])
    with pytest.raises(OperationError) as error:
        await plan_brief_sections(service, "目标", source)
    assert error.value.code == "CREATION_INPUT_CONTRACT_INVALID"
    assert not service.calls


def invalid_plan(kind):
    result = plan()
    if kind == "missing":
        result["sections"][0]["decision_ids"].pop(0)
    elif kind == "unknown":
        result["sections"][0]["decision_ids"][0] = "invented"
    elif kind == "duplicate_ownership":
        result["sections"][1]["decision_ids"].append("choice-0")
    elif kind == "duplicate_within":
        result["sections"][1]["decision_ids"].append("choice-8")
    elif kind == "too_many_in_section":
        result["sections"][0]["decision_ids"].append("choice-8")
        result["sections"][1]["decision_ids"] = []
    elif kind == "too_many_sections":
        result["sections"] += [{"id": "extra-{}".format(i), "title": "附加", "purpose": "总览",
                                "decision_ids": []} for i in range(11)]
    elif kind == "too_many_empty":
        result["sections"] += [{"id": "extra-{}".format(i), "title": "附加", "purpose": "总览",
                                "decision_ids": []} for i in range(3)]
    elif kind == "too_many_globals":
        result["global_decision_ids"] = ["choice-{}".format(i) for i in range(7)]
    elif kind == "unknown_global":
        result["global_decision_ids"] = ["missing"]
    elif kind == "duplicate_global":
        result["global_decision_ids"] *= 2
    elif kind == "empty_title":
        result["sections"][0]["title"] = " "
    elif kind == "heading_title":
        result["sections"][0]["title"] = "## 注入章节"
    elif kind == "multiline_title":
        result["sections"][0]["title"] = "本章\n## 越界章节"
    elif kind == "long_title":
        result["sections"][0]["title"] = "标题" * 100
    elif kind == "long_purpose":
        result["sections"][0]["purpose"] = "说明" * 700
    elif kind == "duplicate_section_id":
        result["sections"][1]["id"] = result["sections"][0]["id"]
    elif kind == "invalid_section_id":
        result["sections"][0]["id"] = "包含路径/的标识"
    elif kind == "extra_facts":
        result["sections"][0]["facts"] = "模型新增事实"
    return result


@pytest.mark.parametrize("kind", ["missing", "unknown", "duplicate_ownership", "duplicate_within",
    "too_many_in_section", "too_many_sections", "too_many_empty", "too_many_globals",
    "unknown_global", "duplicate_global", "empty_title", "heading_title", "multiline_title", "long_title", "long_purpose", "duplicate_section_id",
    "invalid_section_id", "extra_facts"])
def test_existing_stable_id_plan_contract_remains_strict(kind):
    with pytest.raises(OperationError) as error:
        _validate_plan(invalid_plan(kind), decisions())
    assert error.value.code == "CREATION_INPUT_CONTRACT_INVALID"


async def test_invalid_json_can_be_corrected_once_without_accepting_partial_plan():
    service = Service(['{"sections": [', assignment_plan()])
    assert await plan_brief_sections(service, "目标", decisions()) == plan()
    assert len(service.calls) == 2


async def test_global_reference_does_not_replace_primary_ownership():
    wrong = assignment_plan()
    del wrong["assignments"]["D01"]
    service = Service([wrong, assignment_plan()])
    assert await plan_brief_sections(service, "目标", decisions()) == plan()
    assert "D01" in service.calls[1]["user_prompt"].split("上次规划校验失败：", 1)[1]


async def test_up_to_two_purpose_only_sections_can_support_overall_goal():
    expected = plan(1)
    expected["sections"] += [{"id": "s2", "title": "目标", "purpose": "交代整体目标", "decision_ids": []},
                             {"id": "s3", "title": "结语", "purpose": "整体收束", "decision_ids": []}]
    reply = assignment_plan(1)
    reply["sections"] = [{key: value for key, value in item.items() if key != "decision_ids"} for item in expected["sections"]]
    assert await plan_brief_sections(Service([reply]), "目标", decisions(1)) == expected


async def test_truncated_plan_is_not_accepted_as_partial_structure():
    service = Service([OperationError("CREATION_DOCUMENT_TRUNCATED", "预算已耗尽")])
    with pytest.raises(OperationError) as error:
        await plan_brief_sections(service, "目标", decisions())
    assert error.value.code == "CREATION_INPUT_CONTRACT_INVALID"


def test_assignment_schema_requires_every_alias_once_and_only_known_section_values():
    aliases = ["D{:02d}".format(i) for i in range(1, 34)]
    schema = _plan_schema(aliases)
    assignments = schema["properties"]["assignments"]
    assert assignments["required"] == aliases
    assert set(assignments["properties"]) == set(aliases)
    assert assignments["additionalProperties"] is False
    assert assignments["properties"]["D33"]["enum"] == ["s{}".format(i) for i in range(1, 13)]
    assert "decision_ids" not in schema["properties"]["sections"]["items"]["properties"]


@pytest.mark.parametrize("count", [9, 33, 96])
async def test_oversized_theme_splits_in_original_choice_order_without_losing_values(count):
    source = decisions(count)
    source[0]["description"] = "每类100条"
    source[-1]["description"] = "总体1000条，保留末项"
    reply = assignment_plan(count)
    reply["sections"] = [{"id": "s1", "title": "同一主题", "purpose": "原有主题任务"}]
    reply["assignments"] = {"D{:02d}".format(i): "s1" for i in range(count, 0, -1)}
    reply["global_decision_ids"] = ["D{:02d}".format(count)]
    original_source, original_reply = copy.deepcopy(source), copy.deepcopy(reply)
    result = await plan_brief_sections(Service([reply]), "完整目标", source)
    assert [identifier for section in result["sections"] for identifier in section["decision_ids"]] == [item["id"] for item in source]
    assert len(result["sections"]) == (count + 7) // 8
    assert all(len(section["decision_ids"]) <= 8 for section in result["sections"])
    assert result["sections"][1]["id"] == "s1-part2"
    assert result["sections"][1]["title"] == "同一主题（续 2）"
    assert all(section["purpose"] == "原有主题任务" for section in result["sections"])
    assert result["global_decision_ids"] == [source[-1]["id"]]
    _, last_prompt = build_section_prompts(result["sections"][-1], source, "目标", [], {})
    assert source[-1]["description"] in last_prompt
    assert source == original_source and reply == original_reply


async def test_split_preserves_model_theme_order_and_input_order_within_each_theme():
    reply = assignment_plan(12)
    reply["sections"].reverse()
    reply["assignments"] = {"D{:02d}".format(i): "s2" if i % 2 else "s1" for i in range(12, 0, -1)}
    result = await plan_brief_sections(Service([reply]), "目标", decisions(12))
    assert [section["id"] for section in result["sections"]] == ["s2", "s1"]
    assert result["sections"][0]["decision_ids"] == ["choice-{}".format(i) for i in range(0, 12, 2)]
    assert result["sections"][1]["decision_ids"] == ["choice-{}".format(i) for i in range(1, 12, 2)]


def invalid_assignment(kind):
    reply = assignment_plan()
    if kind == "missing_alias":
        del reply["assignments"]["D09"]
    elif kind == "unknown_alias":
        reply["assignments"]["D10"] = "s1"
    elif kind == "multiple_owners":
        reply["assignments"]["D01"] = ["s1", "s2"]
    elif kind == "undeclared_owner":
        reply["assignments"]["D01"] = "s12"
    elif kind == "empty_assignment":
        reply["assignments"]["D01"] = ""
    elif kind == "duplicate_section":
        reply["sections"][1]["id"] = "s1"
    elif kind == "invalid_section":
        reply["sections"][1]["id"] = "s13"
    elif kind == "unknown_global":
        reply["global_decision_ids"] = ["D10"]
    elif kind == "duplicate_global":
        reply["global_decision_ids"] = ["D01", "D01"]
    elif kind == "extra_section_facts":
        reply["sections"][0]["facts"] = "不能补写事实"
    elif kind == "invalid_title":
        reply["sections"][0]["title"] = "主题\n## 注入新章节"
    elif kind == "too_many_empty":
        reply["sections"] += [{"id": "s{}".format(i), "title": "概述", "purpose": "概述"} for i in range(3, 6)]
    return reply


@pytest.mark.parametrize("kind", ["missing_alias", "unknown_alias", "multiple_owners", "undeclared_owner",
    "empty_assignment", "duplicate_section", "invalid_section", "unknown_global", "duplicate_global",
    "extra_section_facts", "invalid_title", "too_many_empty"])
async def test_assignment_validation_retries_with_all_aliases_then_fails_closed(kind):
    service = Service([invalid_assignment(kind), invalid_assignment(kind)])
    with pytest.raises(OperationError) as error:
        await plan_brief_sections(service, "目标", decisions())
    assert error.value.code == "CREATION_INPUT_CONTRACT_INVALID"
    assert len(service.calls) == 2
    retry = service.calls[1]["user_prompt"]
    assert "上次规划校验失败" in retry and "D01" in retry and "D09" in retry


async def test_duplicate_json_assignment_keys_are_rejected_instead_of_last_wins():
    reply = json.dumps(assignment_plan())
    duplicate = reply.replace('"D01": "s1"', '"D01": "s1", "D01": "s2"')
    service = Service([duplicate, assignment_plan()])
    assert await plan_brief_sections(service, "目标", decisions()) == plan()
    assert "重复字段或分配别名：D01" in service.calls[1]["user_prompt"]


async def test_too_many_sections_after_lossless_split_retries_without_deleting_empty_or_owned_sections():
    wrong = assignment_plan(96)
    wrong["sections"] = [{"id": "s1", "title": "大主题", "purpose": "全部主题"},
                         {"id": "s2", "title": "结语", "purpose": "结语"}]
    wrong["assignments"] = {alias: "s1" for alias in wrong["assignments"]}
    service = Service([wrong, assignment_plan(96)])
    result = await plan_brief_sections(service, "目标", decisions(96))
    assert result == plan(96)
    assert "拆分后超过 12 章" in service.calls[1]["user_prompt"]
    assert "D96" in service.calls[1]["user_prompt"]


def test_section_prompt_separates_owned_requirements_from_other_choice_boundaries_and_keeps_corrections():
    source = decisions(12)
    source[0]["description"] = "每个类别各取100条，与总体1000条分别统计。" * 100
    section = {"id": "s1", "title": "本章", "purpose": "覆盖本章主题", "decision_ids": ["choice-0", "choice-1"]}
    materials = {"creation_brief": "完整脑暴历史以及其他选项清单不得回灌",
        "brainstorm_decisions": source,
        "user_instruction_and_supplied_facts": "补充真实输入",
        "original_document": "", "root_request": "全局目标",
        "conversation": [{"role": "assistant", "content": "错误建议内容"},
                         {"role": "user", "content": "更正：此前100改为200"}],
        "supplied_lines": {"brief-1": "历史脑暴重复内容", "instruction-1": "本轮要求",
                           "conversation-1-1": "用户更正原文", "document-1": "原始附件材料"},
        "retrieved_evidence": {"sources": ["真实材料"]}, "user_options": {"audience": "读者"},
        "candidate_document": "全稿候选不得传给单节"}
    before = copy.deepcopy(materials)
    system, user = build_section_prompts(section, source, "完整目标", ["choice-0", "choice-10"], materials, "标题")
    payload = section_payload(user)
    assert payload["owned_decisions"] == source[:2]
    assert payload["global_decisions"] == [source[10]]
    assert payload["other_confirmed_decisions"] == [
        {key: item[key] for key in ("dimension", "value", "description")} for item in source[2:]]
    assert payload["provided_materials"]["conversation"] == [{"role": "user", "content": "更正：此前100改为200"}]
    assert payload["provided_materials"]["supplied_lines"] == {
        "instruction-1": "本轮要求", "conversation-1-1": "用户更正原文", "document-1": "原始附件材料"}
    for excluded in ("完整脑暴历史", "历史脑暴重复内容", "错误建议内容", "全稿候选", "choice-11"):
        assert excluded not in user
    assert "真实材料" in user and "补充真实输入" in user
    assert "数值、单位、统计对象" in system
    assert "纯事实、参数或观点" in system
    assert materials == before


def test_section_repair_keeps_only_its_candidate_and_findings_as_nonfact_context():
    section = {**plan(1)["sections"][0], "current_content": "本章待改稿", "repair_findings": ["缺少具体数值"]}
    system, user = build_section_prompts(section, decisions(1), "目标", [], {})
    payload = section_payload(user)
    assert payload["current_content"] == "本章待改稿"
    assert payload["repair_findings"] == ["缺少具体数值"]
    assert "不是新事实来源" in system


async def test_planner_and_writer_share_effective_scope_without_superseded_assumptions():
    source = decisions(1) + [
        {"id": "excluded", "source": "user_excluded", "dimension": "排除主题", "value": "不要补写该方向"},
        {"id": "cleared", "source": "user_cleared", "dimension": "清空维度", "value": ""},
        {"id": "pending", "source": "agent_assumption", "dimension": "未决情况", "value": "尚待核验"},
        {"id": "old", "source": "superseded_assumption", "value": "过时占位内容不得复活"}]
    service = Service([assignment_plan(1)])
    await plan_brief_sections(service, "", source)
    planner = json.loads(service.calls[0]["user_prompt"])
    scope = {"root_request_was_edited": True, "open_flags": ["开放问题"]}
    system, user = build_section_prompts(plan(1)["sections"][0], source, "", [], {"brief_scope": scope})
    writer = section_payload(user)
    assert writer["scope_constraints"] == planner["scope_constraints"]
    assert len(writer["scope_constraints"]) == 3
    assert writer["provided_materials"]["brief_scope"] == scope
    assert planner["root_request"] == writer["root_request"] == ""
    assert "过时占位内容" not in user + service.calls[0]["user_prompt"]
    assert "不得恢复" in system and "不能升级为事实" in system


def test_other_chapter_constraints_stay_visible_even_when_not_selected_as_globals():
    source = decisions(3)
    source[0].update(value="手部存在幻觉", description="整理和触碰时变形")
    source[1].update(value="彻底剔除手部", description="完全禁止手部出现")
    source[2].update(value="固定时长与分镜", description="5-8秒/2-3镜")
    section = {"id": "s1", "title": "模型边界", "purpose": "说明已知限制", "decision_ids": ["choice-0"],
               "other_sections": [{"id": "s2", "title": "脚本约束", "purpose": "不应重复灌入的规划说明"},
                                  {"id": "s3", "title": "镜头参数"}]}
    original = copy.deepcopy(section)
    system, user = build_section_prompts(section, source, "整体方案", [], {})
    payload = section_payload(user)
    assert [item["value"] for item in payload["owned_decisions"]] == ["手部存在幻觉"]
    assert payload["global_decisions"] == []
    assert [item["description"] for item in payload["other_confirmed_decisions"]] == ["完全禁止手部出现", "5-8秒/2-3镜"]
    assert payload["other_sections"] == [{"title": "脚本约束"}, {"title": "镜头参数"}]
    assert "不应重复灌入的规划说明" not in user
    assert "不是本章必须逐项展开的清单" in system
    assert "不能因为其他章负责展开" in system
    assert "不把所有描述都当成已实测事实或硬性阈值" in system
    decoded, end = json.JSONDecoder().raw_decode(user)
    assert decoded == payload
    assert '本次仅输出 "模型边界" 章节正文' in user[end:]
    assert "不扩写其他章节" in user[end:] and "不新增未经定义的业务含义或阈值" in user[end:]
    assert section == original


def test_material_deduplication_uses_only_exact_equality_and_keeps_user_corrections():
    source = decisions(1)
    materials = {"root_request": "完整根目标", "user_instruction_and_supplied_facts": "本轮生成要求",
        "conversation": [{"role": "user", "content": "完整根目标"},
                         {"role": "user", "content": "本轮生成要求"},
                         {"role": "user", "content": "原来100条"},
                         {"role": "user", "content": "更正200条\n保留类别条件"},
                         {"role": "user", "content": "完整根目标 "}],
        "supplied_lines": {"root-1": "完整根目标", "instruction-1": "本轮生成要求",
                           "conversation-2-1": "原来100条", "conversation-3-1": "更正200条",
                           "conversation-3-2": "保留类别条件", "brief-1": "旧脑暴渲染",
                           "attachment-1": "原始独立材料", "attachment-2": "原始独立材料",
                           "attachment-3": "原始独立材料 "}}
    original = copy.deepcopy(materials)
    _, user = build_section_prompts(plan(1)["sections"][0], source, "完整根目标", [], materials)
    provided = section_payload(user)["provided_materials"]
    assert provided["conversation"] == [{"role": "user", "content": "原来100条"},
                                         {"role": "user", "content": "更正200条\n保留类别条件"},
                                         {"role": "user", "content": "完整根目标 "}]
    assert provided["supplied_lines"] == {"attachment-1": "原始独立材料", "attachment-3": "原始独立材料 "}
    assert "更正200条" in user and "原来100条" in user and "保留类别条件" in user
    assert materials == original


@pytest.mark.parametrize("field,values", [("decision_ids", ["unknown"]),
                                           ("decision_ids", ["choice-0", "choice-0"]),
                                           ("globals", ["unknown"])])
def test_section_prompt_rejects_invalid_references_instead_of_silently_dropping(field, values):
    section = plan(1)["sections"][0]
    globals_ = values if field == "globals" else []
    if field == "decision_ids":
        section[field] = values
    with pytest.raises(OperationError):
        build_section_prompts(section, decisions(1), "目标", globals_, {})


@pytest.mark.parametrize("raw,expected", [
    ("# 文档标题\n\n## 章节标题\n\n正文\n\n### 子节\n后文", "正文\n\n### 子节\n后文"),
    ("正文\n\n## 后续主题\n后文", "正文\n\n### 后续主题\n后文"),
    ("```markdown\n## 本章\n正文\n```", "正文"),
    ("~~~md\n正文\n~~~", "正文"),
    ("```python\n# 代码注释\n## 仍然是注释\nprint(1)\n```", "```python\n# 代码注释\n## 仍然是注释\nprint(1)\n```"),
    ("````markdown\n## 本章\n正文\n```python\n# 注释\n```\n````", "正文\n```python\n# 注释\n```"),
    ("正文\n~~~text\n## 示例标题\n~~~", "正文\n~~~text\n## 示例标题\n~~~"),
    ("100 条。", "100 条。"),
])
def test_section_normalization_preserves_body_and_fenced_examples(raw, expected):
    assert normalize_section_content(raw) == expected


@pytest.mark.parametrize("raw", ["", " \n ", "# 只有文档标题\n## 只有章节标题"])
def test_empty_section_is_rejected(raw):
    with pytest.raises(OperationError) as error:
        normalize_section_content(raw)
    assert error.value.code == "CREATION_DOCUMENT_INVALID"


@pytest.mark.parametrize("raw", ["正文\n```python\nprint(1)", "~~~markdown\n正文",
                                 "````python\nprint(1)\n```", "```\n正文\n```不是结束围栏"])
def test_unclosed_fences_are_not_saved_as_complete_section(raw):
    with pytest.raises(OperationError) as error:
        normalize_section_content(raw)
    assert error.value.code == "CREATION_DOCUMENT_TRUNCATED"


async def test_writer_uses_complete_document_service_and_same_pure_prompts():
    section = plan(1)["sections"][0]
    service = Service(["## 本章\n\n采用100条，保留具体参数。"])
    expected_system, expected_user = build_section_prompts(section, decisions(1), "目标", [], {}, "标题")
    output = await write_brief_section(service, section, decisions(1), "目标", [], {}, "标题")
    assert output == "采用100条，保留具体参数。"
    assert service.calls == [{"system_prompt": expected_system, "user_prompt": expected_user,
                             "creation_model": None, "creation_api_key": None, "creation_base_url": None}]


async def test_writer_preserves_explicit_model_routing():
    service = Service(["本章内容"])
    await write_brief_section(service, plan(1)["sections"][0], decisions(1), "目标", [], {},
                              creation_model="explicit-model", creation_api_key="test-key",
                              creation_base_url="https://example.invalid/v1")
    assert service.calls[0]["creation_model"] == "explicit-model"
    assert service.calls[0]["creation_api_key"] == "test-key"
    assert service.calls[0]["creation_base_url"] == "https://example.invalid/v1"


async def test_writer_does_not_commit_partial_text_if_stream_fails():
    class FailingService:
        async def stream_agent_document(self, **kwargs):
            yield "已有半个章节"
            raise OperationError("CREATION_DOCUMENT_TRUNCATED", "输出中断")

    with pytest.raises(OperationError) as error:
        await write_brief_section(FailingService(), plan(1)["sections"][0], decisions(1), "目标", [], {})
    assert error.value.code == "CREATION_DOCUMENT_TRUNCATED"


def test_small_recovery_plan_assigns_each_confirmed_choice_without_model_outline():
    from creation.brief_writing import plan_source_owned_sections
    source = decisions(3) + [{'id': 'not-confirmed', 'source': 'agent_assumption', 'value': '不能作为确认'}]
    result = plan_source_owned_sections(source)
    assert [section['decision_ids'] for section in result['sections']] == [['choice-0'], ['choice-1'], ['choice-2']]
    assert all(section['title'] == original['dimension'] for section, original in zip(result['sections'], source))
    assert result['global_decision_ids'] == []




async def test_source_owned_proposal_preserves_user_facts_and_marks_new_arrangements_as_proposals():
    row = {'mode': 'proposal', 'inputs': '取得用户提供的素材。', 'steps': '先整理再生成样稿。',
           'output': '可供比较的候选内容。', 'verification': '比较原流程与样稿的效果，并记录差异。', 'body': ''}
    service = Service([row])
    section = {**plan(1)['sections'][0], 'source_owned': True, 'source_owned_mode': 'proposal', 'include_open_flags': True}
    output = await write_brief_section(service, section, decisions(1), '设计方案', [],
                                       {'brief_scope': {'open_flags': ['成本接受度待确认']}})
    assert decisions(1)[0]['description'] in output
    assert '以下安排用于试行和验证，不代表现有能力或已实现效果。' in output
    assert all(value in output for value in (row['inputs'], row['steps'], row['output'], row['verification']))
    assert '以下资源是否存在、是否可用均待确认；拟核实并收集：' + row['inputs'] in output
    assert '建议执行（以所需输入确认可用为前提）：' + row['steps'] in output
    assert '拟交付：' + row['output'] in output
    assert '拟验证（效果尚待验证）：' + row['verification'] in output
    assert '成本接受度待确认' in output
    assert 'json_schema' in service.calls[0]


async def test_source_owned_prose_keeps_narrative_form_without_forcing_proposal_layout():
    row = {'mode': 'prose', 'inputs': '', 'steps': '', 'output': '', 'verification': '', 'body': '雨声渐起，角色走入了已选定的场景。'}
    output = await write_brief_section(Service([row]), {**plan(1)['sections'][0], 'source_owned': True, 'source_owned_mode': 'prose'},
                                       decisions(1), '写一个故事', [], {})
    assert output == row['body'] and '落实建议' not in output


async def test_source_owned_proposal_repairs_missing_application_field_without_losing_facts():
    good = {'mode': 'proposal', 'inputs': '输入', 'steps': '执行', 'output': '产物', 'verification': '验证', 'body': ''}
    service = Service([{**good, 'verification': ''}, good])
    result = await write_brief_section(service, {**plan(1)['sections'][0], 'source_owned': True, 'source_owned_mode': 'proposal'}, decisions(1), '方案', [], {})
    assert len(service.calls) == 2 and decisions(1)[0]['value'] in result


async def test_recovery_mode_is_classified_from_root_and_bound_in_generation_schema():
    from creation.brief_writing import source_owned_output_mode
    classifier = Service([{'is_plan': True}])
    assert await source_owned_output_mode(classifier, '为完整目标设计方案') == 'proposal'
    assert json.loads(classifier.calls[0]['user_prompt']) == {'request': '为完整目标设计方案'}
    response = {'mode': 'proposal', 'inputs': '输入', 'steps': '执行', 'output': '产物', 'verification': '验证', 'body': ''}
    service = Service([response])
    await write_brief_section(service, {**plan(1)['sections'][0], 'source_owned': True, 'source_owned_mode': 'proposal'},
                              decisions(1), '为完整目标设计方案', [], {})
    properties = service.calls[0]['json_schema']['properties']
    assert properties['mode'] == {'const': 'proposal'} and properties['body'] == {'const': ''}
    assert properties['steps']['minLength'] == 1


async def test_unconfirmed_numeric_threshold_repairs_before_section_is_saved():
    row = {'mode': 'proposal', 'inputs': '收集资料。', 'steps': '筛选月收入50万元以上的对象。',
           'output': '方案。', 'verification': '比较实际结果。', 'body': ''}
    repaired = {**row, 'steps': '依据实际资料确认筛选标准，再选择试点对象。'}
    service = Service([row, repaired])
    result = await write_brief_section(service, {**plan(1)['sections'][0], 'source_owned': True,
        'source_owned_mode': 'proposal'}, decisions(1), '提出方案', [], {})
    assert '50万元' not in result
    assert '新增未经确认的量化限定' in service.calls[1]['user_prompt']


async def test_repeated_unconfirmed_quantities_are_sanitized_without_failing_the_run():
    first = {'mode': 'proposal', 'inputs': '收集资料。', 'steps': '生成60秒样片。继续人工复核。',
             'output': '形成599个候选。', 'verification': '在2026年9月检查10条结果。', 'body': ''}
    second = {**first, 'steps': '生成60秒样片。继续人工复核并记录差异。'}
    service = Service([first, second])
    result = await write_brief_section(service, {**plan(1)['sections'][0], 'source_owned': True,
        'source_owned_mode': 'proposal'}, decisions(1), '提出方案', [], {})
    assert all(value not in result for value in ('60秒', '599个', '2026年', '9月', '10条'))
    assert '继续人工复核并记录差异。' in result
    assert decisions(1)[0]['description'] in result
    assert len(service.calls) == 2
    assert all(value not in service.calls[1]['user_prompt']
               for value in ('60秒', '599个', '2026年', '9月', '10条'))


async def test_repeated_invalid_structure_uses_source_owned_fallback_and_continues():
    invalid = {'mode': 'proposal', 'inputs': '', 'steps': '', 'output': '', 'verification': '', 'body': ''}
    service = Service([invalid, invalid])
    result = await write_brief_section(service, {**plan(1)['sections'][0], 'source_owned': True,
        'source_owned_mode': 'proposal'}, decisions(1), '提出方案', [], {})
    assert decisions(1)[0]['description'] in result
    assert '核实执行所需资料、资源及适用边界' in result
    assert '结论以完成验证后的证据为准' in result
    assert len(service.calls) == 2


def test_proposal_quantities_keep_given_values_and_ignore_step_numbering():
    from creation.brief_writing import _unprovided_proposal_quantities
    row = {'inputs': '收集样本。', 'steps': '1. 使用5秒内容；2. 比较结果。', 'output': '5秒样片。', 'verification': '检查结果。'}
    assert _unprovided_proposal_quantities(row, '用户确认使用5秒内容') == []
    assert _unprovided_proposal_quantities(row, '用户确认使用五秒内容') == []
    assert _unprovided_proposal_quantities(row, '用户确认使用5.0秒内容') == []
    assert _unprovided_proposal_quantities(row, '用户确认5元成本') == ['5秒']
    assert _unprovided_proposal_quantities(row, '没有已确认时长') == ['5秒']


def test_old_generated_input_prefix_upgrades_without_changing_confirmed_facts():
    from creation.brief_writing import normalize_proposal_input_status
    text = '**已确认依据**\n用户确认事实。\n\n### 输入与前提\n\n拟收集并核实可用性：需调查的输入。'
    result = normalize_proposal_input_status(text)
    assert result.startswith('**已确认依据**\n用户确认事实。')
    assert result.endswith('以下资源是否存在、是否可用均待确认；拟核实并收集：需调查的输入。')
    assert normalize_proposal_input_status(result) == result
