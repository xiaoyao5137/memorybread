import json
from itertools import permutations

import pytest

from creation.agent_loop import CreationAgentLoop
from creation.operations import (OperationError, apply_patches, document_nodes,
                                 repair_generated_literal_selectors, resolve_target)
from creation.service import CreationOptions, CreationService
from creation.tools import fallback_routing_decision


@pytest.mark.parametrize("title", ["实施步骤与资源规划", "Release notes", "一、任意章节 42", "成本分析", "架构设计"])
def test_delete_arbitrary_heading_tree_preserves_everything_else(title):
    before = "# 原文\n\n## 保留\n**数字 123**\n\n"
    selected = "## {}\n正文\n### 子节\n内容\n\n".format(title)
    after = "## 后文\n[来源](https://example.com)\n"
    doc = before + selected + after
    target = next(node["id"] for node in document_nodes(doc) if node["title"] == title)
    result, patch = apply_patches(doc, [{"action": "delete", "target": target}])
    assert result == before + after
    assert patch["preserved_untouched"]


def test_inventory_excludes_fences_and_disambiguates_titles():
    doc = "# Root\n~~~md\n## Fake\n~~~\n## Same\none\n## Same\ntwo\n"
    nodes = document_nodes(doc)
    assert [n["title"] for n in nodes] == ["Root", "Same", "Same"]
    result, _ = apply_patches(doc, [{"action": "delete", "target": nodes[2]["id"]}])
    assert result.endswith("## Same\none\n")
    with pytest.raises(OperationError, match="出现多次"):
        resolve_target(doc, {"text": "Same"})


def test_setext_and_exact_occurrence():
    doc = "Title\n=====\n\nFirst\n-----\nvalue value\n\nSecond\n------\nend\n"
    assert [n["title"] for n in document_nodes(doc)] == ["Title", "First", "Second"]
    result, _ = apply_patches(doc, [{"action": "replace", "target": {"text": "value", "occurrence": 2}, "content": "new"}])
    assert "value new" in result


def test_exact_line_selector_wins_over_shorter_prefix_matches():
    doc = "- populated item\n\n- \n\n### Pending\n"
    result, _ = apply_patches(
        doc,
        [{"action": "delete", "target": {"text": "- ", "occurrence": 1}}],
    )
    assert result == "- populated item\n\n\n\n### Pending\n"


def test_literal_selector_keeps_substring_semantics_without_an_exact_line():
    result, _ = apply_patches(
        "prefix value suffix\n",
        [{"action": "replace", "target": {"text": "value", "occurrence": 1}, "content": "new"}],
    )
    assert result == "prefix new suffix\n"


def test_move_insert_and_replace_bind_to_same_base():
    doc = "## A\na\n## B\nb\n## C\nc\n"
    a, b, c = document_nodes(doc)
    result, _ = apply_patches(doc, [{"action": "move", "target": c["id"], "destination": a["id"], "position": "before"},
                                  {"action": "replace", "target": {"text": "b\n"}, "content": "BB\n"}])
    assert result == "## C\nc\n## A\na\n## B\nBB\n"
    result, _ = apply_patches(doc, [{"action": "insert", "target": b["id"], "position": "before", "content": "## New\nnew\n"}])
    assert result == "## A\na\n## New\nnew\n## B\nb\n## C\nc\n"


def test_generated_patch_restores_heading_boundary_without_changing_literal_patch_contract():
    from creation.operations import normalize_generated_patch_markdown
    patches = [{"action": "replace", "target": {"text": "待确认"},
                "content": "待确认### 执行步骤\n\n内容"}]
    normalized = normalize_generated_patch_markdown(patches)
    assert normalized[0]["content"] == "待确认\n\n### 执行步骤\n\n内容"
    assert patches[0]["content"] == "待确认### 执行步骤\n\n内容"
    literal, _ = apply_patches("待确认", patches)
    assert literal == "待确认### 执行步骤\n\n内容"


def test_generated_section_insert_after_document_without_trailing_newline_gets_boundary():
    from creation.operations import normalize_generated_patch_markdown
    document = "# 方案\n\n## 现有内容\n\n结尾无换行"
    root = document_nodes(document)[0]
    patches = [{"action": "insert", "target": root["id"], "position": "after",
                "content": "## 工作定义\n\n正文"}]
    normalized = normalize_generated_patch_markdown(patches, document)
    result, _ = apply_patches(document, normalized, [root["id"]])
    assert result == document + "\n\n## 工作定义\n\n正文"


def test_generated_insert_cannot_repeat_existing_document_as_new_fragment():
    from creation.operations import generated_patch_problems
    document = (
        "# 方案\n\n## 范围\n\n这是第一段已经存在且长度足够的正文内容。\n\n"
        "## 路径\n\n这是第二段已经存在且长度足够的正文内容。\n\n"
        "## 验证\n\n这是第三段已经存在且长度足够的正文内容。\n"
    )
    duplicated = document.replace("## 范围", "## 范围\n\n新增定义", 1)
    assert generated_patch_problems(document, [{
        "action": "insert", "target": document_nodes(document)[0]["id"],
        "position": "after", "content": duplicated,
    }]) == ["insert_repeats_existing_content"]
    assert generated_patch_problems(document, [{
        "action": "insert", "target": document_nodes(document)[0]["id"],
        "position": "after", "content": "\n\n## 新增定义\n\n仅新增内容",
    }]) == []
    small_context_copy = (
        "## 新定义\n\n新增内容。\n\n这是第一段已经存在且长度足够的正文内容。\n\n"
        "这是第二段已经存在且长度足够的正文内容。"
    )
    assert generated_patch_problems(document, [{
        "action": "insert", "target": document_nodes(document)[0]["id"],
        "position": "after", "content": small_context_copy,
    }], "增加定义") == ["insert_repeats_existing_content"]
    assert generated_patch_problems(document, [{
        "action": "insert", "target": document_nodes(document)[0]["id"],
        "position": "after", "content": small_context_copy,
    }], "复制两段原文到附录") == []


def test_generated_full_target_insert_is_recovered_as_insertion_only_replace():
    from creation.operations import recover_generated_insert_supersequence
    document = "# 方案\n\n## 范围\n\n原有范围。\n\n## 路径\n\n原有路径。\n"
    root = document_nodes(document)[0]
    candidate = document.replace("## 路径", "## L0 商家工作定义\n\n新增定义。\n\n## 路径")
    recovered, changed = recover_generated_insert_supersequence(document, [{
        "action": "insert", "target": root["id"], "position": "after",
        "content": candidate,
    }], [root["id"]])
    assert changed is True
    assert recovered == [{
        "action": "replace", "target": root["id"], "content": candidate,
    }]
    assert apply_patches(document, recovered, [root["id"]])[0] == candidate


def test_generated_insert_recovery_rejects_rewritten_or_partial_base():
    from creation.operations import recover_generated_insert_supersequence
    document = "# 方案\n\n## 范围\n\n原有范围。\n\n## 路径\n\n原有路径。\n"
    root = document_nodes(document)[0]
    for candidate in (
        document.replace("原有范围。", "改写范围。"),
        "## 新定义\n\n新增内容。\n\n## 路径\n\n原有路径。",
    ):
        patches = [{"action": "insert", "target": root["id"],
                    "position": "after", "content": candidate}]
        recovered, changed = recover_generated_insert_supersequence(
            document, patches, [root["id"]]
        )
        assert changed is False
        assert recovered == patches


def test_allowed_root_scope_rebinds_to_candidate_with_same_heading_identity():
    from creation.operations import rebind_targets_to_document
    base = "# 方案\n\n## 范围\n\n原始范围。\n"
    candidate = base + "\n## 待确认\n\n- 账号关系待确认\n"
    base_root = document_nodes(base)[0]
    candidate_root = document_nodes(candidate)[0]
    assert rebind_targets_to_document(
        base, candidate, [base_root["id"]]
    ) == [candidate_root["id"]]


def test_allowed_scope_rebind_fails_when_heading_identity_is_ambiguous_or_removed():
    from creation.operations import rebind_targets_to_document
    base = "# 方案\n\n## 范围\n\n原始范围。\n"
    root, section = document_nodes(base)
    assert rebind_targets_to_document(base, "# 新方案\n", [root["id"]]) is None
    assert rebind_targets_to_document(base, "# 方案\n\n## 其他\n\n正文。\n", [section["id"]]) is None


def test_generated_append_rebinds_new_heading_only_with_whole_document_scope():
    from creation.operations import rebind_generated_append_target
    document = "# 方案\n\n## 现有章节\n\n正文\n"
    root, section = document_nodes(document)
    patch = {"action": "insert", "target": {"text": "## 尚未存在的定义"},
             "position": "before", "content": "## 尚未存在的定义\n\n正文"}
    rebound = rebind_generated_append_target(
        [patch], document, [root["id"]], "增加定义介绍"
    )
    assert rebound[0]["target"] == root["id"]
    assert rebound[0]["position"] == "after"
    unchanged = rebind_generated_append_target(
        [patch], document, [section["id"]], "增加定义介绍"
    )
    assert unchanged == [patch]


def test_overlapping_and_stale_targets_do_not_change_base():
    doc = "## A\na\n### Child\nx\n"
    parent, child = document_nodes(doc)
    with pytest.raises(OperationError) as error:
        apply_patches(doc, [{"action": "delete", "target": parent["id"]}, {"action": "delete", "target": child["id"]}])
    assert error.value.code == "CREATION_PATCH_OVERLAP"
    with pytest.raises(OperationError) as error:
        apply_patches(doc + "new", [{"action": "delete", "target": parent["id"]}])
    assert error.value.code == "CREATION_TARGET_MISSING"


def test_generated_patch_cannot_escape_interpreted_scope():
    doc = "## Scope\neditable\n## Outside\nprotected\n"
    scope, outside = document_nodes(doc)
    with pytest.raises(OperationError) as error:
        apply_patches(doc, [{"action": "delete", "target": outside["id"]}], [scope["id"]])
    assert error.value.code == "CREATION_PATCH_OUT_OF_SCOPE"
    result, _ = apply_patches(doc, [{"action": "replace", "target": {"text": "editable"}, "content": "edited"}], [scope["id"]])
    assert result == doc.replace("editable", "edited")


class OperationService:
    """No retrieval/generation methods: an accidental call makes the test fail."""
    def __init__(self, operation):
        self.operation = operation
        self.calls = []

    def analyze_requirement(self, query, options, **kwargs):
        self.calls.append(("analyze", query))
        return {"topic": query, "doc_type": "技术方案"}

    async def route_capabilities(self, **kwargs):
        self.calls.append(("route", kwargs))
        return {"tools": [], "agents": [], "operation": self.operation, "source": "model"}

    def build_routing_prompts(self, *args):
        return "routing", "context"

    def parse_routing_decision(self, text):
        return json.loads(text)

    @staticmethod
    def _clip(value, limit):
        return value[:limit]


def run_args(doc, message="自然语言指令"):
    return dict(user_message=message, root_request="最初需要一篇架构设计方案和数据调研",
                current_document=doc, conversation=[], selected_skills=[], options=CreationOptions(),
                session_id="test-operation", run_id="test-run")


@pytest.mark.asyncio
async def test_patch_is_only_executor_and_resume_does_not_repeat_patch():
    doc = "# 标题\n\n## A\n原文\n## B\n尾部\n"
    target = document_nodes(doc)[1]["id"]
    service = OperationService({"kind": "patch", "patches": [{"action": "delete", "target": target}]})
    loop = CreationAgentLoop(service)
    events = [event async for event in loop.run(**run_args(doc))]
    assert events[-1]["data"]["document"] == "# 标题\n\n## B\n尾部\n"
    assert not any(event["actor"]["id"] in {"memory_search", "solution_design_agent", "document_writer_agent", "quality_review_agent"} for event in events)
    assert service.calls[0][1]["query"] == "自然语言指令"
    checkpoint = [e["data"]["checkpoint"] for e in events if e["type"] == "operation.checkpoint"][-1]
    resumed = [event async for event in loop.run(**run_args(doc, "继续"), resume_checkpoint=checkpoint)]
    assert resumed[-1]["data"]["document"] == events[-1]["data"]["document"]
    assert not any(e["type"] == "document.patch.applied" for e in resumed)
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_resume_is_explicit_operation_not_a_new_generation():
    loop = CreationAgentLoop(OperationService({"kind": "resume", "operation_id": "pending-1"}))
    events = [event async for event in loop.run(**run_args("## A\nbody"),
        operation_context={"pending_operations": [{"operation_id": "pending-1", "instruction": "原修改指令"}]} )]
    assert events[-1]["type"] == "operation.resume.requested"
    assert events[-1]["data"]["operation_id"] == "pending-1"
    assert not any(e["type"] == "run.completed" for e in events)


@pytest.mark.asyncio
async def test_respond_can_preserve_empty_document_and_negative_instruction():
    loop = CreationAgentLoop(OperationService({"kind": "respond", "response": "已保留现有内容。"}))
    events = [event async for event in loop.run(**run_args("", "不要删除任何内容"))]
    assert events[-1]["data"]["document"] == ""
    assert events[-1]["data"]["response"] == "已保留现有内容。"


def test_failed_interpretation_does_not_select_tools_from_root_topic():
    decision = fallback_routing_decision("继续", {"doc_type": "数据分析架构设计方案"})
    assert decision["tools"] == decision["agents"] == []


def test_production_prompt_discloses_operation_and_no_required_memory():
    service = object.__new__(CreationService)
    system, user = service.build_routing_prompts("本轮修改", {"operation_context": {"current_document": "已有正文", "pending_operations": []}})
    assert "已有正文" in user
    assert "patch|transform|generate|respond|answer|resume|undo|execute_skill" in system
    assert "memory_search" in system
    assert "结构性能力，不需要你决策" not in system
    assert "不得擅自从待确认事项或未提交选项中挑一项" in system


@pytest.mark.asyncio
async def test_transform_generates_bounded_patches_without_full_document_writer():
    doc = "## Notes\nA long sentence.\n## Keep\nUnchanged.\n"
    target = document_nodes(doc)[0]["id"]

    class TransformService(OperationService):
        async def run_specialist_agent(self, **kwargs):
            self.calls.append(("transform", kwargs))
            assert kwargs["json_mode"] is True
            assert "allowed_targets" in kwargs["user_prompt"]
            return json.dumps({"patches": [{"action": "replace", "target": {"text": "A long sentence."}, "content": "Short."}]})

    service = TransformService({"kind": "transform", "targets": [target]})
    events = [event async for event in CreationAgentLoop(service).run(**run_args(doc, "Simplify Notes"))]
    assert events[-1]["data"]["document"] == doc.replace("A long sentence.", "Short.")
    assert [call[0] for call in service.calls] == ["route", "analyze", "transform"]
    assert not any(e["actor"]["id"] in {"document_writer_agent", "quality_review_agent", "memory_search"} for e in events)


@pytest.mark.asyncio
async def test_answer_does_not_replace_the_document():
    class AnswerService(OperationService):
        async def run_specialist_agent(self, **kwargs):
            return "根据当前文档，讨论安排在周一。"

    doc = "## 安排\n周一讨论。"
    events = [event async for event in CreationAgentLoop(AnswerService({"kind": "answer"})).run(**run_args(doc, "解释一下这个安排"))]
    assert events[-1]["data"]["document"] == doc
    assert events[-1]["data"]["response"] == "根据当前文档，讨论安排在周一。"
    assert not any(e["type"].startswith("document.") for e in events)


@pytest.mark.asyncio
async def test_model_handoff_reuses_current_interpretation_and_does_not_route_again():
    doc = "## A\nremove\n## B\nkeep\n"
    service = OperationService({"kind": "respond", "response": "unused"})
    loop = CreationAgentLoop(service)
    first = [e async for e in loop.run(**run_args(doc), model_mode="external")]
    request = next(e for e in first if e["type"] == "model.request")
    checkpoint = first[-1]["data"]["continuation"]
    assert checkpoint["pending_model_step"]["request_id"] == request["data"]["request_id"]
    decision = {"tools": [], "agents": [], "operation": {"kind": "patch",
        "patches": [{"action": "delete", "target": {"id": document_nodes(doc)[0]["id"]}}]}}
    final = [e async for e in loop.run(**run_args(doc), resume_state=checkpoint, model_result=json.dumps(decision))]
    assert final[-1]["data"]["document"] == "## B\nkeep\n"
    assert not any(e["type"] == "model.request" for e in final)


@pytest.mark.parametrize('operation', [
    {'kind': 'respond', 'response': 1},
    {'kind': 'resume', 'operation_id': ['id']},
    {'kind': 'execute_skill', 'skill_ids': []},
    {'kind': 'generate', 'constraint_skill_ids': [None]},
])
def test_operation_contract_rejects_wrong_types(operation):
    from creation.operations import validate_operation
    with pytest.raises(OperationError):
        validate_operation(operation)


@pytest.mark.parametrize('kind,fields', [('patch', {'patches': [{'action': 'delete', 'target': 'node'}]}),
                                       ('respond', {'response': 'Understood'}),
                                       ('resume', {'operation_id': 'pending'})])
def test_direct_operation_cannot_silently_run_resources(kind, fields):
    from creation.tools import validate_routing_decision
    with pytest.raises(OperationError):
        validate_routing_decision({'operation': {'kind': kind, **fields}, 'tools': ['memory_search'], 'agents': []})


def test_dependency_ordering_never_adds_capabilities():
    from creation.tools import order_selected_capabilities
    plan = [{'id': key} for key in ['document_writer_agent', 'solution_design_agent', 'industry_research_agent']]
    ordered = order_selected_capabilities(plan)
    assert [item['id'] for item in ordered] == ['industry_research_agent', 'solution_design_agent', 'document_writer_agent']
    assert order_selected_capabilities([{'id': 'document_patch'}]) == [{'id': 'document_patch'}]


@pytest.mark.asyncio
async def test_invalid_model_contract_retries_interpretation_without_tool_execution():
    service = object.__new__(CreationService)
    service.model = 'test'
    calls = []
    async def completion(**kwargs):
        calls.append(kwargs)
        yield json.dumps({'operation': {'kind': 'respond', 'response': 'Understood'},
                          'tools': ['memory_search'] if len(calls) == 1 else [], 'agents': []})
    service._stream_direct_completion = completion
    service._log_creation_usage = lambda **kwargs: None
    decision = await service.route_capabilities(query='Only acknowledge', requirement={})
    assert decision['source'] == 'model'
    assert decision['tools'] == []
    assert len(calls) == 2
    assert '未通过操作契约校验' in calls[1]['user_prompt']


@pytest.mark.asyncio
async def test_route_rejects_operation_kind_not_offered_by_current_intent_and_repairs_it():
    service = object.__new__(CreationService)
    service.model = 'test'
    calls = []
    document = '# 方案\n\n已有内容。'
    target = document_nodes(document)[0]['id']

    async def completion(**kwargs):
        calls.append(kwargs)
        operation = ({'kind': 'resume', 'operation_id': 'stale-operation'}
                     if len(calls) == 1 else {'kind': 'transform', 'targets': [target]})
        yield json.dumps({'operation': operation, 'tools': [], 'agents': [], 'reasoning': '继续当前正文'})

    service._stream_direct_completion = completion
    service._log_creation_usage = lambda **kwargs: None
    decision = await service.route_capabilities(query='继续生成', requirement={
        'task_intent': {'action': 'edit'},
        'operation_context': {
            'current_document': document,
            'pending_operations': [{'operation_id': 'stale-operation', 'instruction': '旧任务'}],
        },
    })
    assert len(calls) == 2
    assert decision['operation'] == {'kind': 'transform', 'targets': [target]}
    assert '不属于本轮意图和当前状态允许的操作' in calls[1]['user_prompt']


@pytest.mark.asyncio
async def test_repeated_invalid_model_contract_fails_closed_after_bounded_retry():
    service = object.__new__(CreationService)
    service.model = 'test'
    calls = []
    async def completion(**kwargs):
        calls.append(kwargs)
        yield 'invalid json'
    service._stream_direct_completion = completion
    service._log_creation_usage = lambda **kwargs: None
    decision = await service.route_capabilities(query='arbitrary topic', requirement={})
    assert len(calls) == 2
    assert decision['source'] == 'fallback'
    assert decision['tools'] == decision['agents'] == []


@pytest.mark.asyncio
async def test_structured_contract_rejection_surfaces_its_own_error_code():
    # 带错误码的结构化契约拒绝不能降级成 fallback：那会把它改写成
    # “本轮操作解析失败，请重试当前指令”，用户只能对确定性失败反复重试。
    service = object.__new__(CreationService)
    service.model = 'test'
    skill = {'id': 'creation-skill-general-solution-review-doc', 'title': '技术架构方案评审文档模板'}
    calls, logged = [], []
    async def completion(**kwargs):
        calls.append(kwargs)
        yield json.dumps({'tools': [], 'agents': [], 'reasoning': '生成方案',
                          'operation': {'kind': 'generate', 'document_identity': {
                              'title': '技术架构方案评审文档', 'source': 'generated',
                              'evidence': '为商家制作爆款视频并提高GMV'}}})
    service._stream_direct_completion = completion
    service._log_creation_usage = lambda **kwargs: logged.append(kwargs)
    with pytest.raises(OperationError) as error:
        await service.route_capabilities(query='为商家制作爆款视频并提高GMV', requirement={},
                                         selected_skills=[skill])
    assert error.value.code == 'CREATION_TITLE_SKILL_LEAK'
    assert '不能复制' in str(error.value)
    # 仍然先给一次修复重试，并把真实原因写回修复提示与用量埋点。
    assert len(calls) == 2
    assert '不能复制' in calls[1]['user_prompt']
    assert logged[-1]['status'] == 'failed'
    assert '不能复制' in logged[-1]['error_msg']


@pytest.mark.parametrize('patch', [
    {'action': 'replace', 'target': {'text': 'keep'}},
    {'action': 'delete', 'target': {'text': 'keep'}, 'unknown_action': 'extra'},
    {'action': [], 'target': {'text': 'keep'}},
])
def test_malformed_patch_cannot_be_silently_reinterpreted_as_deletion(patch):
    with pytest.raises(OperationError):
        apply_patches('keep', [patch])


@pytest.mark.asyncio
async def test_route_repairs_a_target_not_grounded_in_the_document():
    service = object.__new__(CreationService)
    service.model = 'test'
    calls = []
    async def completion(**kwargs):
        calls.append(kwargs)
        yield json.dumps({'tools': [], 'agents': [], 'operation': {'kind': 'patch',
            'patches': [{'action': 'replace', 'target': {'text': '原句.' if len(calls) == 1 else '原句。'},
                         'content': '新句。'}]}})
    service._stream_direct_completion = completion
    service._log_creation_usage = lambda **kwargs: None
    decision = await service.route_capabilities(query='把原句改为新句。', requirement={'task_intent': {'action': 'edit'}, 'operation_context': {'current_document': '原句。'}})
    assert len(calls) == 2
    assert decision['source'] == 'model'
    assert decision['operation']['patches'][0]['target']['text'] == '原句。'


@pytest.mark.asyncio
@pytest.mark.parametrize('base_url,trust_env', [('http://localhost:11434', False),
    ('http://127.0.0.1:11434', False), ('http://[::1]:11434', False), ('https://remote.example', True)])
async def test_local_model_transport_bypasses_environment_proxy(monkeypatch, base_url, trust_env):
    import httpx
    service = object.__new__(CreationService)
    service.model = 'qwen3.5:4b'
    service.ollama_base_url = base_url
    captured = {}
    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def raise_for_status(self): pass
        async def aiter_lines(self): yield json.dumps({'response': '{"ok":true}'})
    class Client:
        def __init__(self, **kwargs): captured.update(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs):
            captured['payload'] = kwargs['json']
            return Response()
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    chunks = [chunk async for chunk in service._stream_direct_completion(system_prompt='system',
        user_prompt='input', creation_model=None, creation_api_key=None, creation_base_url=None,
        num_predict=100, temperature=0, disable_thinking=True, json_mode=True)]
    assert captured['trust_env'] is trust_env
    assert captured['payload']['options']['repeat_penalty'] == 1.0
    assert captured['payload']['options']['presence_penalty'] == 0.0
    assert ''.join(chunks) == '{"ok":true}'


def test_decoding_schema_avoids_expanding_complex_bounded_repetitions():
    from creation.operations import decoding_schema, routing_response_schema
    contract = routing_response_schema(['memory_search'], [], [])
    compact = decoding_schema(contract)
    assert '"maxItems": 64' in json.dumps(contract)
    assert '"maxItems": 64' not in json.dumps(compact)
    assert '"maxLength"' not in json.dumps(compact)
    assert '"maxItems": 0' in json.dumps(compact)
    assert compact['properties']['operation']['anyOf'][0]['properties']['patches']['items'] == contract['properties']['operation']['anyOf'][0]['properties']['patches']['items']


@pytest.mark.asyncio
async def test_truncated_routing_context_does_not_change_full_document_node_identity():
    service = object.__new__(CreationService)
    service.model = 'test'
    doc = '# Root\n' + 'long body\n' * 8000 + '## Appendix\nremove\n'
    target = document_nodes(doc)[-1]['id']
    async def completion(**kwargs):
        yield json.dumps({'tools': [], 'agents': [], 'operation': {'kind': 'patch',
            'patches': [{'action': 'delete', 'target': target}]}})
    service._stream_direct_completion = completion
    service._log_creation_usage = lambda **kwargs: None
    decision = await service.route_capabilities(query='Remove appendix', requirement={'task_intent': {'action': 'edit'}, 'operation_context': {
        'current_document': doc[:64000], 'document_truncated': True, 'nodes': document_nodes(doc)}})
    assert decision['source'] == 'model'
    result, _ = apply_patches(doc, decision['operation']['patches'])
    assert result == doc.removesuffix('## Appendix\nremove\n')


def test_direct_patch_accepts_literal_changes_but_rejects_generated_prose():
    from creation.operations import validate_literal_patch
    validate_literal_patch('周一上午讨论。', [{'action': 'replace', 'target': {'text': '周一上午讨论。'},
        'content': '周三下午讨论。'}], '把周一上午改成周三下午')
    with pytest.raises(OperationError, match='transform'):
        validate_literal_patch('很长的原句。', [{'action': 'replace', 'target': {'text': '很长的原句。'},
            'content': '简洁而且新生成的内容。'}], '精简原句')


@pytest.mark.parametrize('operation', [
    {'kind': 'patch', 'patches': [None]},
    {'kind': 'patch', 'patches': [{'action': 'delete', 'target': []}]},
    {'kind': 'patch', 'patches': [{'action': 'insert', 'target': {'text': 'body'}, 'position': [], 'content': 'x'}]},
    {'kind': 'patch', 'patches': [{'action': 'move', 'target': {'text': 'body'}, 'destination': {'id': []}, 'position': 'after'}]},
    {'kind': 'transform', 'targets': [{'text': 'body', 'occurrence': True}]},
    {'kind': 'transform', 'targets': [{'text': 'body', 'occurrence': None}]},
    {'kind': 'transform', 'targets': [{'text': 'body', 'unknown': 'scope'}]},
])
def test_nested_operation_contract_is_rejected_before_selector_normalization(operation):
    from creation.operations import validate_operation
    with pytest.raises(OperationError) as error:
        validate_operation(operation)
    assert error.value.code == 'CREATION_OPERATION_INVALID'


@pytest.mark.asyncio
async def test_route_repairs_malformed_nested_patch_before_any_execution():
    service = object.__new__(CreationService)
    service.model = 'test'
    calls = []
    async def completion(**kwargs):
        calls.append(kwargs)
        yield json.dumps({'tools': [], 'agents': [], 'operation': {'kind': 'patch',
            'patches': [None] if len(calls) == 1 else [
                {'action': 'replace', 'target': {'text': 'Monday'}, 'content': 'Tuesday'}]}})
    service._stream_direct_completion = completion
    service._log_creation_usage = lambda **kwargs: None
    decision = await service.route_capabilities(query='Replace Monday with Tuesday', requirement={
        'task_intent': {'action': 'edit'}, 'operation_context': {'current_document': 'Monday meeting'}})
    assert len(calls) == 2
    assert decision['source'] == 'model'
    assert apply_patches('Monday meeting', decision['operation']['patches'])[0] == 'Tuesday meeting'


def test_overlapping_literal_occurrences_require_explicit_disambiguation():
    with pytest.raises(OperationError) as error:
        resolve_target('aaa', {'text': 'aa'})
    assert error.value.code == 'CREATION_TARGET_AMBIGUOUS'
    result, _ = apply_patches('aaa', [{'action': 'replace', 'target': {'text': 'aa', 'occurrence': 2}, 'content': 'b'}])
    assert result == 'ab'


def test_multiline_setext_delete_removes_the_complete_rendered_heading():
    doc = '# Root\n\nLong heading\ncontinued here\n---\nBody\n\n## Keep\nTail\n'
    nodes = document_nodes(doc)
    assert [node['title'] for node in nodes] == ['Root', 'Long heading continued here', 'Keep']
    result, _ = apply_patches(doc, [{'action': 'delete', 'target': nodes[1]['id']}])
    assert result == '# Root\n\n## Keep\nTail\n'


@pytest.mark.parametrize('non_heading', [
    '- list item\n---\n',
    '1. ordered item\n---\n',
    '> quoted text\n---\n',
    '    indented code\n---\n',
    '[source]: https://example.com\n---\n',
])
def test_thematic_break_after_a_nonparagraph_does_not_create_a_section(non_heading):
    doc = '# Root\n\n' + non_heading + '\n## Keep\nbody\n'
    assert [node['title'] for node in document_nodes(doc)] == ['Root', 'Keep']


def test_invalid_backtick_fence_info_does_not_hide_real_sections():
    doc = '# Root\n```` code`\n## Actual\nbody\n'
    assert [node['title'] for node in document_nodes(doc)] == ['Root', 'Actual']


@pytest.mark.parametrize('opaque_block', [
    '<!--\n## Fake\n-->\n',
    '<script>\n## Fake\n\n## Also fake\n</script>\n',
    '<div>\n## Fake\n</div>\n',
    '<custom-widget>\n## Fake\n</custom-widget>\n',
    '<?processing\n## Fake\n?>\n',
    '<![CDATA[\n## Fake\n]]>\n',
])
def test_html_blocks_do_not_offer_phantom_markdown_section_targets(opaque_block):
    doc = '# Root\n\n' + opaque_block + '\n## Real\nbody\n'
    assert [node['title'] for node in document_nodes(doc)] == ['Root', 'Real']


def test_empty_atx_heading_and_crlf_offsets_match_rendered_sections():
    doc = '# ###\r\n\r\n## Real ##\r\nbody\r\n'
    nodes = document_nodes(doc)
    assert [node['title'] for node in nodes] == ['', 'Real']
    result, _ = apply_patches(doc, [{'action': 'delete', 'target': nodes[1]['id']}])
    assert result == '# ###\r\n\r\n'


@pytest.mark.parametrize('container', [
    '- list item\n  continued\n---\n',
    '> quote\ncontinued\n---\n',
    '- list item\n  ## Nested\n  body\n',
    '- list item\n\n  Nested\n  ---\n  body\n',
    '> ## Quoted\n> body\n',
])
def test_nested_markdown_containers_do_not_change_document_section_boundaries(container):
    doc = '# Root\n\n' + container + '\n## Keep\nbody\n'
    nodes = document_nodes(doc)
    assert [node['title'] for node in nodes] == ['Root', 'Keep']
    result, _ = apply_patches(doc, [{'action': 'delete', 'target': nodes[1]['id']}])
    assert result == '# Root\n\n' + container + '\n'


def test_ordered_list_marker_cannot_interrupt_a_multiline_heading_paragraph():
    doc = '# Root\n\nHeading\n2. continuation\n---\n\n## Keep\nbody\n'
    assert [node['title'] for node in document_nodes(doc)] == ['Root', 'Heading 2. continuation', 'Keep']


@pytest.mark.parametrize('order', list(permutations(range(4))))
def test_compound_delete_replace_insert_move_are_atomic_and_order_independent(order):
    doc = '# Root\n\n## A\nremove\n## B\nold\n## C\nkeep\n## D\nmove\n'
    root, a, b, c, d = document_nodes(doc)
    patches = [
        {'action': 'delete', 'target': a['id']},
        {'action': 'replace', 'target': {'text': 'old'}, 'content': 'new'},
        {'action': 'insert', 'target': c['id'], 'position': 'after', 'content': '\nExtra.\n'},
        {'action': 'move', 'target': d['id'], 'destination': b['id'], 'position': 'before'},
    ]
    result, audit = apply_patches(doc, [patches[index] for index in order], [root['id']])
    assert result == '# Root\n\n## D\nmove\n## B\nnew\n## C\nkeep\n\nExtra.\n'
    assert audit['preserved_untouched'] is True


def test_move_requires_both_source_and_destination_to_be_in_scope():
    doc = '## Editable\nmove\n## Protected\nkeep\n'
    source, destination = document_nodes(doc)
    with pytest.raises(OperationError) as error:
        apply_patches(doc, [{'action': 'move', 'target': source['id'],
                            'destination': destination['id'], 'position': 'after'}], [source['id']])
    assert error.value.code == 'CREATION_PATCH_OUT_OF_SCOPE'


def test_generated_selector_repair_accepts_unique_spacing_and_trailing_note_drift():
    doc = (
        '# 方案\n\n## 爆款视频生成执行模式\n\n'
        '- 矩阵增量账号模式：利用“原号+新号”双轨制，新号全量AI生成。\n'
        '- 商家对运营介入程度（托管vs辅助）的偏好\n'
        '- 新品牌号与主号关联机制的具体设计\n\n## 其他\n\n保留。\n'
    )
    scope = document_nodes(doc)[1]['id']
    patches = [
        {'action': 'replace', 'target': {'text': '- 矩阵增量账号模式：利用“原号 + 新号”双轨制，新号全量 AI 生成。', 'occurrence': 1},
         'content': '- 矩阵增量账号模式：利用“原号+新号”双轨制，新号全量AI生成。\n- 全托管模式：平台自动完成。'},
        {'action': 'replace', 'target': {'text': '- 商家对运营介入程度（托管 vs 辅助）的偏好\n- 新品牌号与主号关联机制的具体设计（待确认）', 'occurrence': 1},
         'content': '- 新品牌号与主号关联机制的具体设计（待确认）'},
        {'action': 'delete', 'target': {'text': '- 商家对运营介入程度（托管 vs 辅助）的偏好\n', 'occurrence': 1}},
    ]
    repaired, changed = repair_generated_literal_selectors(doc, patches, [scope])
    assert changed is True
    assert len(repaired) == 2
    updated, _ = apply_patches(doc, repaired, [scope])
    assert '全托管模式' in updated
    assert '运营介入程度' not in updated
    assert '## 其他\n\n保留。' in updated


def test_generated_selector_repair_fails_closed_when_spacing_match_is_ambiguous():
    doc = '# 方案\n\n## 范围\n\n- 原号+新号\n- 原号 + 新号\n'
    scope = document_nodes(doc)[1]['id']
    patches = [{'action': 'delete', 'target': {'text': '- 原号  +  新号', 'occurrence': 1}}]
    repaired, changed = repair_generated_literal_selectors(doc, patches, [scope])
    assert changed is False
    with pytest.raises(OperationError) as error:
        apply_patches(doc, repaired, [scope])
    assert error.value.code == 'CREATION_TARGET_MISSING'
