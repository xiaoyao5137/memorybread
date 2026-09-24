import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from creation import skill_governance
from creation.agent_loop import CreationAgentLoop, LoopState
from creation.operations import OperationError, apply_patches, routing_response_schema
from creation.service import CreationOptions
from creation.skill_governance import (
    admit_skills, apply_title, copied_skill_name, document_heading, fingerprint,
    generated_title_leaks_skill, intent_contract_problem, reads_from_instruction,
    recover_identity, review_skill, skill_review_problem, task_intent, validate_identity,
)
from tests.test_creation_agent_loop import FakeCreationService


LEGACY = json.loads((Path(__file__).parent / "fixtures/legacy_solution_skill.json").read_text())
REQUEST = "为灵机吸引非L0商家，制作爆款视频并提高GMV，可以使用SOTA视频模型。"
TITLE = "灵机商家增长与视频运营方案"
IDENTITY = {"title": TITLE, "source": "generated", "evidence": "吸引非L0商家"}


def assessment(**overrides):
    return {"skill_id": LEGACY["id"], "metadata_consistent": True, "applicable": True,
            "request_evidence": "吸引非L0商家", "conflicts": [], "missing_inputs": [], **overrides}


def operation(**overrides):
    return {"kind": "execute_skill", "skill_ids": [LEGACY["id"]],
            "document_identity": IDENTITY, "skill_assessments": [assessment()], **overrides}


@pytest.mark.parametrize("change", [
    {"metadata_consistent": False}, {"applicable": False}, {"conflicts": ["不同交付物"]},
    {"missing_inputs": ["必需数据"]}, {"request_evidence": "用户需要系统接口设计"},
    {"request_evidence": ""}, {"metadata_consistent": "true"},
])
def test_automatic_admission_requires_each_condition(change):
    result, audit = admit_skills(operation(skill_assessments=[assessment(**change)]), [LEGACY], [], REQUEST)
    assert result["kind"] == "generate"
    assert "skill_ids" not in result
    assert result["document_identity"] == IDENTITY
    assert audit[0]["admitted"] is False
    assert audit[0]["fingerprint"] == fingerprint(LEGACY)
    assert REQUEST not in json.dumps(audit, ensure_ascii=False)


def test_catalog_presence_and_legacy_missing_assessment_are_not_explicit_selection():
    result, audit = admit_skills(operation(skill_assessments=[]), [LEGACY], [], REQUEST)
    assert result["kind"] == "generate"
    assert audit[0]["source"] == "automatic"


def test_user_selection_preserves_workflow_and_separate_document_identity():
    result, audit = admit_skills(operation(skill_assessments=[assessment(applicable=False)]),
                                 [LEGACY], [LEGACY["id"]], REQUEST)
    assert result["kind"] == "execute_skill"
    assert result["document_identity"]["title"] == TITLE
    assert audit[0]["source"] == "user"


def test_valid_technical_workflow_can_be_admitted():
    request = "为视频生成系统设计架构、组件接口与容量验证方案"
    result, audit = admit_skills(operation(skill_assessments=[assessment(request_evidence="组件接口与容量验证")]),
                                 [LEGACY], [], request)
    assert result["kind"] == "execute_skill"
    assert audit[0]["admitted"] is True


@pytest.mark.parametrize("title", [LEGACY["title"], "技术架构方案评审文档", "技术架构方案评审"])
def test_generated_title_cannot_copy_skill_alias(title):
    with pytest.raises(OperationError, match="不能复制"):
        validate_identity({**IDENTITY, "title": title}, REQUEST, "", [LEGACY])


WEEKLY_SKILL = {
    "id": "creation-skill-gpu-cost-weekly-report",
    "title": "GPU成本优化周报模板",
    "example_document": "# GPU成本优化周报创作法 · 虚构主题示例\n\n## 摘要\n\n示例正文",
}
WEEKLY_REQUEST = "请生成GPU成本优化的周报"


def test_generated_title_may_equal_skill_alias_when_the_instruction_names_it():
    # 生产回归：用户指令本身就在描述该技能主题时，唯一忠实的标题必然与
    # 技能别名重合，不能把它当成技能名复制，否则重试也无法恢复。
    assert reads_from_instruction("GPU成本优化周报", WEEKLY_REQUEST)
    assert not generated_title_leaks_skill("GPU成本优化周报", WEEKLY_REQUEST, [WEEKLY_SKILL])
    identity = validate_identity(
        {"title": "GPU成本优化周报", "source": "generated", "evidence": WEEKLY_REQUEST},
        WEEKLY_REQUEST, "", [WEEKLY_SKILL],
    )
    assert identity["title"] == "GPU成本优化周报"


@pytest.mark.parametrize("title", ["GPU成本优化周报模板", "GPU成本优化周报创作法 · 虚构主题示例"])
def test_verbatim_skill_or_example_name_is_rejected_even_when_mentioned(title):
    request = "请用" + title + "生成本周内容"
    assert copied_skill_name(title, [WEEKLY_SKILL])
    with pytest.raises(OperationError, match="不能复制"):
        validate_identity({"title": title, "source": "generated", "evidence": request},
                          request, "", [WEEKLY_SKILL])


def test_ungrounded_alias_is_still_rejected():
    # 指令没提到该主题时，别名相同的标题仍是技能名泄露。
    assert generated_title_leaks_skill("GPU成本优化周报", "写一份项目复盘", [WEEKLY_SKILL])
    assert not generated_title_leaks_skill("项目复盘报告", "写一份项目复盘", [WEEKLY_SKILL])


@pytest.mark.asyncio
async def test_resume_keeps_existing_heading_that_only_aliases_a_skill():
    # 续跑时已有标题的来源就是正文本身；按别名比对会进入无法收敛的改名循环。
    document = "# GPU成本优化周报\n\n## 本周结论\n单卡推理成本环比下降。"

    class Service:
        async def _stream_direct_completion(self, **kwargs):
            raise AssertionError("已有合法标题不应重新命名")
            yield ""

    identity = await recover_identity(Service(), "继续补充风险章节", document, [WEEKLY_SKILL])
    assert identity == {"title": "GPU成本优化周报", "source": "existing",
                        "evidence": "GPU成本优化周报"}


@pytest.mark.asyncio
async def test_instruction_named_deliverable_completes_when_it_aliases_a_skill():
    class Service(FakeCreationService):
        async def route_capabilities(self, **kwargs):
            return {"source": "model", "tools": [], "agents": [],
                    "operation": {"kind": "generate", "document_identity": {
                        "title": "GPU成本优化周报", "source": "generated",
                        "evidence": WEEKLY_REQUEST}}}

        async def stream_agent_document(self, **kwargs):
            yield "# 占位标题\n\n## 本周结论\n单卡推理成本环比下降，交付时延未受影响。"

    events = [event async for event in CreationAgentLoop(Service()).run(
        user_message=WEEKLY_REQUEST, current_document="", conversation=[],
        selected_skills=[WEEKLY_SKILL], options=CreationOptions(enabled_tools=()),
        governance_required=True)]
    assert not any(event["type"] == "run.failed" for event in events)
    final = next(event["data"]["document"] for event in events if event["type"] == "run.completed")
    assert document_heading(final) == "GPU成本优化周报"
    assert WEEKLY_SKILL["title"] not in final


def test_explicit_title_may_legitimately_contain_template_or_equal_skill_name():
    title = LEGACY["title"]
    request = "文档标题为《" + title + "》，介绍模板使用方法。"
    identity = {"title": title, "source": "user", "evidence": request}
    assert validate_identity(identity, request, "", [LEGACY])["title"] == title


def test_existing_title_is_bound_to_actual_document():
    doc = "# 原业务标题\n\n## 范围\n内容"
    assert validate_identity({"title": "原业务标题", "source": "existing", "evidence": "原业务标题"},
                             "完善全文", doc, [LEGACY])["source"] == "existing"
    with pytest.raises(OperationError):
        validate_identity({"title": "别的标题", "source": "existing", "evidence": "别的标题"}, "完善全文", doc, [])


@pytest.mark.parametrize("doc", ["# 错误标题\n\n## 第一章\n正文\n", "错误标题\n===\n\n## 第一章\n正文\n"])
def test_title_replacement_preserves_body_and_subsections(doc):
    result = apply_title(doc, TITLE)
    assert result == "# " + TITLE + "\n\n## 第一章\n正文\n"
    assert apply_title(result, TITLE) == result


def test_title_does_not_rewrite_heading_inside_fenced_code():
    doc = "```md\n# 示例\n```\n\n# 错误标题\n正文"
    result = apply_title(doc, TITLE)
    assert "```md\n# 示例\n```" in result
    assert document_heading(result) == TITLE


def test_output_roles_keep_process_out_of_document_and_preserve_final_sections():
    state = SimpleNamespace(current_document="", environment={
        "document_identity": IDENTITY, "strict_skill_ids": ["s"], "applied_skills": [],
        "completed_skill_steps": [
            {"skill_id": "s", "step_id": "research", "title": "收集资料", "output_role": "process", "content": "内部研究"},
            {"skill_id": "s", "step_id": "write", "title": "撰写全文", "output_role": "document",
             "content": "# 技能名称\n\n## 商家分层\n运营内容\n\n## 执行计划\n步骤"},
        ]})
    result = CreationAgentLoop(FakeCreationService())._assemble_strict_skill_document(state)
    assert result.startswith("# " + TITLE)
    assert "## 商家分层" in result and "## 执行计划" in result
    assert "收集资料" not in result and "撰写全文" not in result and "内部研究" not in result


def test_schema_requires_identity_and_assessment_only_for_full_generation():
    variants = routing_response_schema([], [], ["s"])["properties"]["operation"]["anyOf"]
    by_kind = {v["properties"]["kind"]["const"]: v for v in variants}
    assert "document_identity" in by_kind["generate"]["required"]
    assert "skill_assessments" in by_kind["execute_skill"]["required"]
    assert "document_identity" not in by_kind["transform"]["properties"]
    assert "document_identity" not in by_kind["patch"]["properties"]


@pytest.mark.asyncio
async def test_independent_review_overrules_positive_initial_model_selection():
    class Service(FakeCreationService):
        async def _stream_direct_completion(self, **kwargs):
            yield json.dumps({"requested_deliverable": "业务增长方案", "actual_workflow_deliverable": "技术架构评审",
                              **{k: v for k, v in assessment(metadata_consistent=False, applicable=False,
                                                         conflicts=["范围与步骤矛盾"]).items() if k != "skill_id"}})
    loop = CreationAgentLoop(Service())
    state = loop._new_state(user_message=REQUEST, root_request=None, current_document="", conversation=[],
                            selected_skills=[LEGACY], options=CreationOptions(enabled_tools=()),
                            model_mode="external", session_id="s", run_id="r")
    events = [event async for event in loop._apply_routing_decision(state, state.plan[0],
              {"tools": [], "agents": [], "operation": operation(), "source": "model"})]
    assert state.environment["operation"]["kind"] == "generate"
    assert not any(step.get("kind") == "skill" for step in state.plan)
    assert any(step.get("action") == "writer" for step in state.plan)
    assert any(event["type"] == "skill.admission" for event in events)
    restored = LoopState.restore(state.serializable())
    assert restored.environment["document_identity"]["title"] == TITLE
    assert "evidence" not in restored.environment["document_identity"]
    assert restored.environment["skill_admission"][0]["admitted"] is False


@pytest.mark.asyncio
async def test_invalid_review_fails_closed():
    class Service:
        async def _stream_direct_completion(self, **kwargs):
            yield '{"metadata_consistent":true}'
    review = await review_skill(Service(), LEGACY, REQUEST)
    assert review["status"] == "unreviewed"
    result, _ = admit_skills(operation(skill_assessments=[review]), [LEGACY], [], REQUEST)
    assert result["kind"] == "generate"


def test_migration_changes_only_exact_legacy_content_and_backs_up_once():
    sql = (Path(__file__).parents[2] / "core-engine/src/storage/migrations/113_creation_skill_governance.sql").read_text()
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE creation_skills(id INTEGER PRIMARY KEY, client_skill_key TEXT, title TEXT, summary TEXT, skill_description TEXT, execution_steps TEXT, updated_at INTEGER)")
    desc = json.dumps(LEGACY["skill_description"], ensure_ascii=False, separators=(",", ":"))
    steps = json.dumps(LEGACY["execution_steps"], ensure_ascii=False, separators=(",", ":"))
    for idx, summary in [(1, LEGACY["summary"]), (2, "用户修改的用途")]:
        c.execute("INSERT INTO creation_skills VALUES(?,?,?,?,?,?,?)", (idx, LEGACY["id"], LEGACY["title"], summary, desc, steps, 123))
    c.executescript(sql)
    upgraded = json.loads(c.execute("SELECT skill_description FROM creation_skills WHERE id=1").fetchone()[0])
    assert upgraded["document_types"] == ["技术架构设计文档", "技术方案评审文档"]
    assert upgraded["applicability"]["version"] == 1
    assert c.execute("SELECT summary,skill_description,execution_steps,updated_at FROM creation_skills WHERE id=2").fetchone() == ("用户修改的用途", desc, steps, 123)
    c.executescript(sql)
    assert c.execute("SELECT COUNT(*) FROM creation_skill_governance_backups").fetchone()[0] == 1
    assert c.execute("SELECT previous_description FROM creation_skill_governance_backups").fetchone()[0] == desc


def test_constraint_skill_cannot_bypass_automatic_admission():
    raw = {"kind": "generate", "constraint_skill_ids": [LEGACY["id"]], "document_identity": IDENTITY}
    result, audit = admit_skills(raw, [LEGACY], [], REQUEST)
    assert result["kind"] == "generate"
    assert result["constraint_skill_ids"] == []
    assert audit[0]["admitted"] is False


def test_mention_is_not_user_naming_authority():
    title = LEGACY["title"]
    request = "使用@" + title + "写商家增长方案"
    with pytest.raises(OperationError):
        validate_identity({"title": title, "source": "user", "evidence": request}, request, "", [LEGACY])


@pytest.mark.asyncio
@pytest.mark.parametrize('requested', [True, False])
async def test_explicit_use_is_resolved_independently_from_initial_generate(requested):
    class Service(FakeCreationService):
        async def _stream_direct_completion(self, **kwargs):
            yield json.dumps({'skill_ids': [LEGACY['id']] if requested else []})
    loop = CreationAgentLoop(Service())
    query = ('使用@' if requested else '不要使用@') + LEGACY['title'] + '，写商家增长方案'
    state = loop._new_state(user_message=query, root_request=None, current_document='', conversation=[],
        selected_skills=[LEGACY], options=CreationOptions(enabled_tools=()), model_mode='local', session_id='s', run_id='r')
    state.environment['explicit_skill_ids'] = [LEGACY['id']]
    events = [event async for event in loop._apply_routing_decision(state, state.plan[0], {
        'source': 'model', 'tools': [], 'agents': [], 'operation': {
            'kind': 'generate', 'document_identity': {**IDENTITY, 'evidence': query}}})]
    assert state.environment['operation']['kind'] == ('execute_skill' if requested else 'generate')
    assert state.environment['document_identity']['title'] == TITLE
    if requested:
        assert state.environment['skill_admission'][0]['source'] == 'user'


@pytest.mark.asyncio
async def test_streaming_title_is_safe_and_throttled_preview_keeps_final_body():
    class Service(FakeCreationService):
        async def route_capabilities(self, **kwargs):
            return {'source': 'model', 'tools': [], 'agents': [],
                    'operation': {'kind': 'generate', 'document_identity': IDENTITY}}
        async def stream_agent_document(self, **kwargs):
            for chunk in ['# ', '技术架构方案评审文档模板', '\n\n## 商家试点\n', '分批邀请商家参与试点，跟踪视频发布与成交转化，按试点结果调整运营方案。']:
                yield chunk
    events = [event async for event in CreationAgentLoop(Service()).run(
        user_message=REQUEST, current_document='', conversation=[], selected_skills=[LEGACY],
        options=CreationOptions(enabled_tools=()), governance_required=True)]
    previews = [e['data']['content'] for e in events if e['type'] == 'document.preview']
    final = next(e['data']['document'] for e in events if e['type'] == 'run.completed')
    assert previews and all(document_heading(p) == TITLE for p in previews)
    assert document_heading(final) == TITLE
    assert '分批邀请商家参与试点' in final
    assert LEGACY['title'] not in final


class ScriptedModelService:
    """按预设结果依次响应模型调用；异常项模拟模型侧传输故障。"""

    def __init__(self, outcomes, delay=0.0):
        self.outcomes = list(outcomes)
        self.delay = delay
        self.prompts = []
        self.calls = []

    async def _stream_direct_completion(self, **kwargs):
        self.calls.append(kwargs)
        self.prompts.append(kwargs["user_prompt"])
        if self.delay:
            await asyncio.sleep(self.delay)
        outcome = self.outcomes[min(len(self.prompts) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        yield outcome


def intent_piece(request, action, role="task"):
    return {"request": request, "role": role, "action": action}


def intent_json(*pieces):
    return json.dumps({"requests": list(pieces)}, ensure_ascii=False)


INTENT_JSON = intent_json(intent_piece(WEEKLY_REQUEST, "create"))
INTENT_SCHEMA = skill_governance.intent_request_schema()


@pytest.mark.parametrize("result,expected", [
    ({"requests": [intent_piece("写小结", "create")]}, ""),
    (["create"], "不是 JSON 对象"),
    ({"action": "create"}, "requests 数组"),
    ({"requests": []}, "条数超出"),
    ({"requests": [intent_piece("写小结", "create")] * 17}, "条数超出"),
    ({"requests": ["写小结"]}, "字段与请求契约不一致"),
    ({"requests": [{"request": "写小结", "action": "create"}]}, "字段与请求契约不一致"),
    ({"requests": [intent_piece("写小结", "make_coffee")]}, "不在本轮允许的动作集合中"),
    ({"requests": [intent_piece("写小结", "create", "unknown")]}, "不在本轮允许的角色集合中"),
    ({"requests": [intent_piece(7, "create")]}, "字段缺少文本值"),
    ({"requests": [intent_piece(" ", "create")]}, "字段缺少文本值"),
    ({"requests": [intent_piece("编造另一项要求", "create")]}, "逐字引用"),
    ({"requests": [intent_piece("写小结", "none")]}, "role 与 action 不一致"),
    ({"requests": [intent_piece("只用材料", "create", "constraint")]}, "role 与 action 不一致"),
    ({"requests": [intent_piece("没材料就告知", "answer", "failure_fallback")]}, "role 与 action 不一致"),
])
def test_intent_contract_problem_names_the_actual_violation(result, expected):
    problem = intent_contract_problem(result, INTENT_SCHEMA, "写小结；只用材料；没材料就告知")
    assert expected in problem
    assert bool(problem) == bool(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_document", [False, True])
@pytest.mark.parametrize("instruction,pieces,expected", [
    ("请写一份失败报告", [("请写一份失败报告", "create")], "create"),
    ("有没有失败？只回答，不修改文档。", [("有没有失败？", "answer"), ("不修改文档", "none", "constraint")], "answer"),
    ("谢谢", [("谢谢", "respond")], "respond"),
    ("继续", [("继续", "resume")], "resume"),
    ("撤销刚才的修改", [("撤销刚才的修改", "undo")], "undo"),
    ("再短一点", [("再短一点", "edit")], "edit"),
    ("能不能把这段改成两句话？", [("把这段改成两句话", "edit")], "edit"),
    ("把原句逐字替换为新句", [("把原句逐字替换为新句", "patch")], "patch"),
    ("不要写通知。只告诉我何时开会。", [("不要写通知", "none", "constraint"), ("只告诉我何时开会", "answer")], "answer"),
    ("解释“写一份通知”这句话，别写通知。", [("解释“写一份通知”这句话", "answer"), ("别写通知", "none", "constraint")], "answer"),
    ('请把“café”\n整理成一页说明。', [('请把“café”\n整理成一页说明', "create")], "create"),
    ("Explain the findings, then draft a recap; report if unavailable.",
     [("Explain the findings", "answer"), ("draft a recap", "create"), ("report if unavailable", "none", "failure_fallback")], "create"),
    ("Explain the findings; report if unavailable. Do not change the document.",
     [("Explain the findings", "answer"), ("report if unavailable", "none", "failure_fallback"),
      ("Do not change the document", "none", "constraint")], "answer"),
    ("不要改动正文", [("不要改动正文", "none", "constraint")], "respond"),
])
async def test_intent_source_roles_preserve_operation_classes_without_extra_calls(
        instruction, pieces, expected, has_document):
    service = ScriptedModelService([intent_json(*(intent_piece(*piece) for piece in pieces))])
    result = await task_intent(service, instruction, has_document)
    assert result["action"] == expected
    assert result["primary_goal"] in instruction
    assert result["deliverable"] == result["primary_goal"]
    assert bool(result["content_request"]) == (expected in {"create", "edit", "patch"})
    assert set(result) == {"action", "primary_goal", "deliverable", "content_request"}
    assert "requests" not in result  # Extraction records must not become executable steps.
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_continue_generation_with_existing_document_is_an_edit_not_a_resume():
    service = ScriptedModelService([intent_json(intent_piece("继续生成", "edit"))])
    result = await task_intent(service, "继续生成", True)
    assert result == {
        "action": "edit",
        "primary_goal": "继续生成",
        "deliverable": "继续生成",
        "content_request": "继续生成",
    }
    assert "不是恢复历史操作" in service.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_append_wording_cannot_be_promoted_to_new_document_when_document_exists():
    instruction = "增加内部等级的定义介绍"
    service = ScriptedModelService([intent_json(intent_piece(instruction, "create"))])
    result = await task_intent(service, instruction, True)
    assert result == {
        "action": "edit",
        "primary_goal": instruction,
        "deliverable": instruction,
        "content_request": instruction,
    }
    assert "局部编辑判成另建文档" in service.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_explicit_separate_artifact_remains_create_with_open_document():
    instruction = "另写一份内部等级的定义介绍"
    service = ScriptedModelService([intent_json(intent_piece(instruction, "create"))])
    result = await task_intent(service, instruction, True)
    assert result["action"] == "create"


@pytest.mark.asyncio
async def test_intent_selects_the_authored_outcome_after_retrieval_and_before_failure_fallback():
    instruction = "读取本地材料，据该文档写一小段小结；只复述已有事实，未取得材料就明确失败。"
    service = ScriptedModelService([intent_json(
        intent_piece("读取本地材料", "answer"), intent_piece("据该文档写一小段小结", "create"),
        intent_piece("只复述已有事实", "none", "constraint"),
        intent_piece("未取得材料就明确失败", "none", "failure_fallback"))])
    result = await task_intent(service, instruction, False)
    assert result == {"action": "create", "primary_goal": "据该文档写一小段小结",
                      "deliverable": "据该文档写一小段小结", "content_request": "据该文档写一小段小结"}
    assert len(service.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("instruction", [
    "先告诉我会议时间，再写一段通知。", "先写一段通知，再告诉我会议时间。",
])
async def test_intent_mixed_request_keeps_writing_in_either_order(instruction):
    # The scalar classifier repeatedly kept only the preliminary answer. Classify
    # both source requests, then select the writing outcome independently of order.
    pieces = [intent_piece("告诉我会议时间", "answer"), intent_piece("写一段通知", "create")]
    pieces.sort(key=lambda item: instruction.index(item["request"]))
    service = ScriptedModelService([intent_json(*pieces)])
    result = await task_intent(service, instruction, False)
    assert result["action"] == "create"
    assert result["primary_goal"] == "写一段通知"


def test_intent_aggregation_keeps_every_writing_outcome_and_combines_edit_with_patch():
    instruction = "删除附录；解释图表；精简结论。"
    result = skill_governance.aggregate_intent_requests({"requests": [
        intent_piece("删除附录", "patch"), intent_piece("解释图表", "answer"),
        intent_piece("精简结论", "edit")]}, instruction)
    assert result["action"] == "edit"
    assert result["content_request"] == "删除附录；解释图表；精简结论"


def test_intent_responses_and_fallbacks_do_not_override_a_substantive_answer():
    instruction = "告诉我进度；没资料就说明；谢谢。"
    result = skill_governance.aggregate_intent_requests({"requests": [
        intent_piece("告诉我进度", "answer"), intent_piece("没资料就说明", "none", "failure_fallback"),
        intent_piece("谢谢", "respond")]}, instruction)
    assert result["action"] == "answer"
    assert result["primary_goal"] == "告诉我进度"
    assert result["content_request"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("pieces", [
    [("写新文档", "create"), ("改写旧正文", "edit")],
    [("写新文档", "create"), ("撤销修改", "undo")],
    [("恢复操作", "resume"), ("撤销修改", "undo")],
])
async def test_intent_conflicting_scopes_fail_closed_instead_of_dropping_a_request(pieces):
    instruction = "；".join(piece[0] for piece in pieces)
    service = ScriptedModelService([intent_json(*(intent_piece(*piece) for piece in pieces))])
    assert await task_intent(service, instruction, True) == {}
    assert len(service.calls) == skill_governance.INTENT_ATTEMPTS


@pytest.mark.asyncio
async def test_intent_verification_survives_a_transient_model_failure():
    service = ScriptedModelService([RuntimeError("model unavailable"), INTENT_JSON])
    assert (await task_intent(service, WEEKLY_REQUEST, False))["action"] == "create"
    assert len(service.calls) == 2
    assert "意图核验请求失败" in service.prompts[1]


@pytest.mark.asyncio
async def test_intent_verification_recovers_from_an_invalid_action():
    service = ScriptedModelService([intent_json(intent_piece(WEEKLY_REQUEST, "make_coffee")), INTENT_JSON])
    assert (await task_intent(service, WEEKLY_REQUEST, False))["action"] == "create"
    assert "不在本轮允许的动作集合中" in service.prompts[1]


@pytest.mark.asyncio
async def test_intent_repairs_a_fabricated_source_quote():
    service = ScriptedModelService([intent_json(intent_piece("虚构的创作要求", "create")), INTENT_JSON])
    assert (await task_intent(service, WEEKLY_REQUEST, False))["action"] == "create"
    assert len(service.calls) == 2
    assert "逐字引用完整 instruction" in service.prompts[1]


@pytest.mark.asyncio
async def test_intent_rejects_executable_fallback_until_its_role_is_repaired():
    instruction = "解释结果，失败时写说明。"
    service = ScriptedModelService([
        intent_json(intent_piece("解释结果", "answer"), intent_piece("失败时写说明", "create", "failure_fallback")),
        intent_json(intent_piece("解释结果", "answer"), intent_piece("失败时写说明", "none", "failure_fallback")),
    ])
    assert (await task_intent(service, instruction, False))["action"] == "answer"
    assert len(service.calls) == 2
    assert "role 与 action 不一致" in service.prompts[1]


@pytest.mark.asyncio
async def test_intent_verification_fails_closed_and_logs_the_timeout(monkeypatch, caplog):
    monkeypatch.setattr(skill_governance, "INTENT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(skill_governance, "INTENT_ATTEMPTS", 2)
    service = ScriptedModelService([INTENT_JSON], delay=0.2)
    with caplog.at_level("WARNING", logger="creation.skill_governance"):
        assert await task_intent(service, WEEKLY_REQUEST, False) == {}
    assert len(service.calls) == 2
    assert "没有返回" in caplog.text
    assert "本地模型可能正被其他任务占用" in caplog.text

REVIEW_JSON = json.dumps({
    "requested_deliverable": "GPU成本优化周报", "actual_workflow_deliverable": "GPU成本优化周报",
    "metadata_consistent": True, "applicable": True,
    "request_evidence": WEEKLY_REQUEST, "conflicts": [], "missing_inputs": []}, ensure_ascii=False)
REVIEW_SCHEMA = {"required": ["requested_deliverable", "actual_workflow_deliverable", "metadata_consistent",
                             "applicable", "request_evidence", "conflicts", "missing_inputs"]}


@pytest.mark.parametrize("raw,expected", [
    (json.loads(REVIEW_JSON), ""),
    ({"metadata_consistent": True}, "字段与契约不一致"),
    ({**json.loads(REVIEW_JSON), "conflicts": "无"}, "conflicts 必须是文本数组"),
])
def test_skill_review_problem_names_the_actual_violation(raw, expected):
    problem = skill_review_problem(raw, REVIEW_SCHEMA)
    assert expected in problem
    assert bool(problem) == bool(expected)


@pytest.mark.asyncio
async def test_skill_review_survives_a_transient_model_failure():
    # 审查一次超时就会把自动选中的技能静默降级成普通生成，用户看不出
    # 自己选的技能根本没走。
    service = ScriptedModelService([RuntimeError("model unavailable"), REVIEW_JSON])
    review = await review_skill(service, WEEKLY_SKILL, WEEKLY_REQUEST)
    assert review["status"] == "reviewed"
    assert review["applicable"] is True
    assert len(service.prompts) == 2
    assert "审查请求失败" in service.prompts[1]


@pytest.mark.asyncio
async def test_skill_review_fails_closed_and_logs_the_timeout(monkeypatch, caplog):
    # 预算用尽后仍然失败关闭：未审查绝不等于默认可用。
    monkeypatch.setattr(skill_governance, "SKILL_REVIEW_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(skill_governance, "SKILL_REVIEW_ATTEMPTS", 2)
    service = ScriptedModelService([REVIEW_JSON], delay=0.2)
    with caplog.at_level("WARNING", logger="creation.skill_governance"):
        review = await review_skill(service, WEEKLY_SKILL, WEEKLY_REQUEST)
    assert review["status"] == "unreviewed"
    assert review["conflicts"] == ["review_unavailable"]
    assert len(service.prompts) == 2
    assert "没有返回" in caplog.text
    result, audit = admit_skills(
        {"kind": "execute_skill", "skill_ids": [WEEKLY_SKILL["id"]],
         "document_identity": {"title": "GPU成本优化周报", "source": "generated",
                               "evidence": WEEKLY_REQUEST},
         "skill_assessments": [review]},
        [WEEKLY_SKILL], [], WEEKLY_REQUEST)
    assert result["kind"] == "generate"
    assert audit[0]["admitted"] is False
