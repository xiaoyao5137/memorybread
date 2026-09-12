"""脑暴每轮记忆检索、证据约束及真实 SQLite 检索集成回归。"""
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from creation.brainstorm import BrainstormCoordinator, BrainstormGenerationError
from creation.service import CreationService
from tests.test_creation_brainstorm import StubCreationService, concise_question_payload
from tests.test_creation_references import _create_unified_memory_db


QUOTE = "项目已经决定优先减少人工修改剧本的时间。"


def reference(identifier=1, content=QUOTE):
    return SimpleNamespace(id=identifier, source_id=identifier, source_type="document",
                           title="原创剧本项目决策", full_content=content, summary="",
                           updated_at=1788566400000, observed_at=None)


class MemoryService(StubCreationService):
    def __init__(self, responses, references=None):
        super().__init__([json.dumps(item, ensure_ascii=False) for item in responses])
        self.references = references if references is not None else [reference()]
        self.queries = []

    async def _stream_direct_completion(self, **kwargs):
        if "历史决定核对器" in kwargs["system_prompt"]:
            facts = json.loads(self.responses[0]).get("inherited_facts", [])
            yield json.dumps({"inherited_facts": facts}, ensure_ascii=False)
            return
        async for chunk in super()._stream_direct_completion(**kwargs):
            yield chunk

    def retrieve_references(self, query, requirement, options):
        self.queries.append(query)
        return self.references


def payload():
    value = concise_question_payload("围绕已知修改成本，应该优先改善哪个环节？")
    value["question"]["options"][0]["memory_evidence"] = [{"memory_id": "m1", "quote": QUOTE}]
    return value


@pytest.mark.asyncio
async def test_each_turn_retrieves_root_and_branch_and_renders_verified_evidence():
    service = MemoryService([payload(), payload()])
    coordinator = BrainstormCoordinator(service)
    first = await coordinator.next_step(root_request="设计原创剧本生成方案", decisions=[], brief_markdown="")
    assert len(service.queries) == 1
    second = await coordinator.next_step(
        root_request="设计原创剧本生成方案", decisions=[{"answer": "人工评审"}],
        brief_markdown="## 自定义修订\n优先评审流程", force_continue=True, focus_hint="审核反馈闭环",
    )
    assert len(service.queries) == 3
    assert "审核反馈闭环" in service.queries[-1] and "人工评审" in service.queries[-1]
    assert QUOTE in service.prompts[0] and QUOTE in service.prompts[1]
    description = second["question"]["options"][0]["details"]
    assert QUOTE in description and "原创剧本项目决策" in description and "document:1" in description
    assert "未附历史记忆引用" in first["question"]["options"][1]["details"]
    assert QUOTE in second["memory_brief"]


def test_initial_brief_exact_mirror_does_not_repeat_root_retrieval():
    root = "设计原创剧本生成方案"
    service = MemoryService([payload()])
    BrainstormCoordinator(service)._retrieve_memory(
        root, [], "# 创作简报\n\n**原始需求：** " + root, ""
    )
    assert service.queries == [root]


@pytest.mark.parametrize("addition", ["\n\n一期只包含人工审核。", "\n", " "])
def test_initial_brief_additions_are_preserved_for_branch_retrieval(addition):
    root = "设计原创剧本生成方案"
    service = MemoryService([payload()])
    BrainstormCoordinator(service)._retrieve_memory(
        root, [], "# 创作简报\n\n**原始需求：** " + root + addition, ""
    )
    assert len(service.queries) == 2
    assert "# 创作简报" in service.queries[1]
    if addition.strip():
        assert addition.strip() in service.queries[1]


def test_initial_brief_mirror_still_retrieves_effective_answers_and_focus():
    root = "设计原创剧本生成方案"
    service = MemoryService([payload()])
    BrainstormCoordinator(service)._retrieve_memory(
        root, [{"answer": "一期仅覆盖人工审核"}],
        "# 创作简报\n\n**原始需求：** " + root, "审核反馈闭环"
    )
    assert len(service.queries) == 2
    assert "一期仅覆盖人工审核" in service.queries[1]
    assert "审核反馈闭环" in service.queries[1]
    assert "# 创作简报" not in service.queries[1]


def test_evidence_windows_keep_late_decisions_and_full_source_context():
    # 决定横跨第一段结束处，来源末尾的取消条件也必须展示给核对器。
    content = ("".join("背景{:03d}。".format(index) for index in range(37)) + QUOTE + "\n"
               + "".join("补充{:03d}。".format(index) for index in range(100))
               + "该目标已取消，当前不再沿用。")
    memory = {"sources": [{"memory_id": "m1", "content": content}]}
    evidence = BrainstormCoordinator._memory_evidence_catalog(memory)
    assert all(8 <= len(item["quote"]) <= 240 and item["quote"] in content for item in evidence)
    assert len({item["evidence_id"] for item in evidence}) == len(evidence)
    assert any(QUOTE in item["quote"] for item in evidence)
    assert "当前不再沿用" in evidence[-1]["quote"]
    positions = [(content.index(item["quote"]), len(item["quote"])) for item in evidence]
    covered = {index for start, length in positions for index in range(start, start + length)}
    assert covered == set(range(len(content)))
    selected = next(item for item in evidence if QUOTE in item["quote"])
    facts = BrainstormCoordinator._validate_inherited_facts(
        {"inherited_facts": [{"dimension_id": "business_outcome", "evidence_id": selected["evidence_id"]}]},
        memory, [{"id": "business_outcome"}],
    )
    assert facts == [{"dimension_id": "business_outcome", "memory_id": "m1", "quote": selected["quote"]}]


@pytest.mark.parametrize("fact", [
    {"dimension_id": "business_outcome", "evidence_id": "m99e1"},
    {"dimension_id": "business_outcome", "evidence_id": ["m1e1"]},
    {"dimension_id": "business_outcome", "evidence_id": "m1e1", "memory_id": "missing"},
    {"dimension_id": "business_outcome", "evidence_id": "m1e1", "quote": "该项目已决定自动发布无需人工审核。"},
    {"dimension_id": "not_a_dimension", "evidence_id": "m1e1"},
])
def test_evidence_ids_fail_closed_for_unknown_sources_and_conflicting_legacy_fields(fact):
    memory = {"sources": [{"memory_id": "m1", "content": QUOTE}]}
    with pytest.raises(BrainstormGenerationError):
        BrainstormCoordinator._validate_inherited_facts(
            {"inherited_facts": [fact]}, memory, [{"id": "business_outcome"}],
        )


def test_duplicate_dimensions_are_rejected_with_evidence_ids():
    fact = {"dimension_id": "business_outcome", "evidence_id": "m1e1"}
    with pytest.raises(BrainstormGenerationError, match="重复"):
        BrainstormCoordinator._validate_inherited_facts(
            {"inherited_facts": [fact, fact]}, {"sources": [{"memory_id": "m1", "content": QUOTE}]},
            [{"id": "business_outcome"}, {"id": "users_workflow"}],
        )


@pytest.mark.asyncio
async def test_verified_existing_decision_advances_coverage_and_is_carried_to_brief():
    value = payload()
    value["inherited_facts"] = [{"dimension_id": "business_outcome", "memory_id": "m1", "quote": QUOTE}]
    value["question"]["dimension_id"] = "users_workflow"
    service = MemoryService([value])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown=""
    )
    assert result["question"]["dimension_id"] == "users_workflow"
    assert "沿用历史结论" in result["question"]["context_details"]
    assert QUOTE in result["memory_brief"]
    assert "业务目标与预期决策" not in result["open_flags"]


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [
    {"memory_id": "missing", "quote": QUOTE},
    {"memory_id": "m1", "quote": "此前已经决定全部自动发布无需审核。"},
])
async def test_fabricated_source_or_quote_retries_without_retrieving_again(evidence):
    invalid = payload()
    invalid["question"]["options"][0]["memory_evidence"] = [evidence]
    service = MemoryService([invalid, payload()])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown=""
    )
    assert len(service.prompts) == 2 and len(service.queries) == 1
    assert "document:1" in result["question"]["options"][0]["details"]
    assert "全部自动发布" not in result["memory_brief"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_no_hits_and_failure_have_distinct_honest_status(failure, caplog):
    service = MemoryService([concise_question_payload("下一步优先改善什么？")], references=[])
    if failure:
        def fail(*args):
            raise RuntimeError("private memory secret")
        service.retrieve_references = fail
    result = await BrainstormCoordinator(service).next_step(root_request="设计原创剧本生成方案", decisions=[], brief_markdown="")
    expected = "检索失败" if failure else "未检索到"
    assert expected in result["question"]["context_details"]
    assert expected in result["memory_brief"]
    assert "private memory secret" not in caplog.text


def test_retrieval_budget_dedup_branch_fairness_and_partial_failure():
    service = MemoryService([])
    calls = []
    def retrieve(query, *args):
        calls.append(query)
        return [reference(i + (100 if len(calls) == 2 else 0), "根记忆" * 3000) for i in range(10)]
    service.retrieve_references = retrieve
    coordinator = BrainstormCoordinator(service)
    memory = coordinator._retrieve_memory("根请求", [], "", "分支")
    assert len(memory["sources"]) == 6
    assert memory["sources"][1]["source_id"] == 100
    assert sum(len(source["content"]) for source in memory["sources"]) <= coordinator.MAX_MEMORY_CHARS
    def partial(query, *args):
        if "当前探索" in query:
            raise RuntimeError("unavailable")
        return [reference()]
    service.retrieve_references = partial
    assert coordinator._retrieve_memory("根请求", [], "", "分支")["status"] == "partial"


def test_old_memory_section_does_not_pollute_query_and_current_edits_take_priority():
    service = MemoryService([])
    coordinator = BrainstormCoordinator(service)
    coordinator._retrieve_memory("原始需求", [],
        "## 历史记忆参考（当前用户修订优先）\n旧记忆不要再次检索\n## 用户修订\n改为人工审核", "")
    assert "旧记忆不要再次检索" not in service.queries[-1]
    assert "改为人工审核" in service.queries[-1]
    assert "current_brief 人工修订" in coordinator._system_prompt()
    assert "不同来源冲突时先澄清" in coordinator._system_prompt()


def make_real_service(tmp_path):
    path = tmp_path / "brainstorm.db"
    _create_unified_memory_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO bake_documents VALUES (1, ?, '方案', ?, ?, '[]', '[]', '', 0, 'auto_created', 1788566400000, NULL, NULL, 'draft')",
                     ("原创剧本生成项目决策", QUOTE, "原创剧本生成项目。" + QUOTE))
    service = CreationService.__new__(CreationService)
    service.db_path = str(path)
    service.enable_vector_recall = False
    service._embedding_model = None
    return service


@pytest.mark.asyncio
async def test_real_sqlite_retrieval_reaches_model_and_returned_evidence(tmp_path):
    service = make_real_service(tmp_path)
    prompts = []
    async def complete(**kwargs):
        if "历史决定核对器" in kwargs["system_prompt"]:
            yield '{"inherited_facts": []}'
            return
        prompts.append(kwargs["user_prompt"])
        yield json.dumps(payload(), ensure_ascii=False)
    service._stream_direct_completion = complete
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown=""
    )
    assert QUOTE in prompts[0]
    assert QUOTE in result["memory_brief"]
    assert "原创剧本生成项目决策" in result["question"]["options"][0]["details"]


@pytest.mark.asyncio
async def test_continuation_directions_also_receive_verified_evidence():
    value = {"status": "ready", "readiness_reason": "切换方向", "continuation_directions": payload()["question"]["options"]}
    service = MemoryService([value])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown="", suggest_directions=True
    )
    assert QUOTE in result["continuation_directions"][0]["details"]
    assert "未附历史记忆引用" in result["continuation_directions"][1]["details"]


def test_long_memory_keeps_relevant_decision_instead_of_only_document_prefix():
    content = "背景说明。" * 2000 + "\n原创剧本的人工审核反馈已确认采用双人复核。"
    excerpt = BrainstormCoordinator._memory_excerpt(content, ["原创剧本", "人工审核", "反馈"], 200)
    assert "双人复核" in excerpt
    assert excerpt in content and len(excerpt) <= 200


@pytest.mark.asyncio
async def test_invalid_inheritance_cannot_skip_coverage():
    invalid = payload()
    invalid["inherited_facts"] = [{"dimension_id": "business_outcome", "memory_id": "missing", "quote": QUOTE}]
    result = await BrainstormCoordinator(MemoryService([invalid])).next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown=""
    )
    assert result["question"]["dimension_id"] == "business_outcome"
    assert "沿用历史结论" not in result["memory_brief"]


@pytest.mark.asyncio
async def test_memory_notice_is_not_hidden_by_internal_model_terminology():
    value = payload()
    value['question']['why_now'] = 'next_question_goal 要求补齐 business_outcome'
    result = await BrainstormCoordinator(MemoryService([value])).next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown=""
    )
    assert 'next_question_goal' not in result['question']['why_now']
    assert 'business_outcome' not in result['question']['why_now']
    assert '本轮已检索历史记忆' in result['question']['context_details']


def test_uncited_claim_of_memory_is_rejected():
    value = payload()
    value['question']['options'][0]['memory_evidence'] = []
    value['question']['options'][0]['description'] = '依据 m1 的目标，推荐人工审核。'
    with pytest.raises(BrainstormGenerationError, match='不存在的记忆来源'):
        BrainstormCoordinator._ground_options(value, {'sources': []})


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get('RUN_LIVE_BRAINSTORM_MEMORY') != '1',
                    reason='显式启用真实本地模型验收')
async def test_live_local_model_inherits_current_decision_and_respects_user_override(tmp_path):
    service = make_real_service(tmp_path)
    service.ollama_base_url = os.environ.get('BRAINSTORM_OLLAMA_URL', 'http://127.0.0.1:11434')
    service.model = os.environ.get('BRAINSTORM_LOCAL_MODEL', 'qwen3.5:4b')
    coordinator = BrainstormCoordinator(service)
    result = await coordinator.next_step(root_request='设计原创剧本生成方案', decisions=[], brief_markdown='')
    assert result['question']['dimension_id'] != 'business_outcome'
    assert '沿用历史结论' in result['memory_brief']
    assert QUOTE in result['memory_brief']
    assert 'next_question_goal' not in result['question']['why_now']
    (tmp_path / 'live-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    memory = coordinator._retrieve_memory('设计原创剧本生成方案', [], '', '')
    inherited = await coordinator._assess_memory(
        memory=memory, root_request='设计原创剧本生成方案', decisions=[],
        brief_markdown='## 用户人工修订\n之前减少人工修改时间的目标已取消。现在改为提升原创性，不沿用过去的目标。',
        coverage=coordinator._required_coverage('设计原创剧本生成方案', []),
        creation_model=service.model, creation_api_key=None, creation_base_url=None,
    )
    assert not any(item['dimension_id'] == 'business_outcome' for item in inherited)


@pytest.mark.parametrize('field', ['memory_ids', 'description'])
def test_explicit_source_id_uses_actual_source_excerpt_without_model_transcription(field):
    value = payload()
    value['question']['options'][0].pop('memory_evidence')
    value['question']['options'][0][field] = ['m1'] if field == 'memory_ids' else '根据 m1 的历史目标继续探索。'
    memory = {'sources': [{'memory_id': 'm1', 'content': QUOTE}]}
    BrainstormCoordinator._ground_options(value, memory)
    assert value['question']['options'][0]['memory_evidence'] == [{'memory_id': 'm1', 'quote': QUOTE}]


@pytest.mark.asyncio
async def test_model_inline_citation_metadata_is_replaced_with_readable_source():
    value = payload()
    value['question']['options'][0]['description'] = '依据 m1 的目标继续。memory_ids: ["m1"]'
    result = await BrainstormCoordinator(MemoryService([value])).next_step(
        root_request='设计原创剧本生成方案', decisions=[], brief_markdown='',
    )
    description = result['question']['options'][0]['description']
    assert 'memory_ids' not in description and 'm1' not in description
    assert '相关资料' in description and QUOTE not in description
    assert '《原创剧本项目决策》' in result['question']['options'][0]['details']
    assert QUOTE in result['question']['options'][0]['details']


def test_persisted_memory_brief_reaches_final_creation_context_as_reference():
    from creation.agent_loop import CreationAgentLoop
    brief = '## 历史记忆参考（当前用户修订优先）\n沿用历史结论：《项目决策》「' + QUOTE + '」'
    context = CreationAgentLoop._brainstorm_prompt_context({'brief_markdown': brief, 'decisions': []})
    assert QUOTE in context
    assert '历史记忆参考（当前用户修订优先）' in context


@pytest.mark.asyncio
async def test_repaired_ids_and_recommendation_sort_preserve_each_options_own_evidence():
    first_quote = "原始项目明确选择先改进人工审稿机制。"
    second_quote = "另一个方向确定优先改善反馈回流机制。"
    value = payload()
    first, second = value["question"]["options"]
    first.update(id="same", recommended=False, memory_evidence=[{"memory_id": "m1", "quote": first_quote}])
    second.update(id="same", recommended=True, memory_evidence=[{"memory_id": "m2", "quote": second_quote}])
    service = MemoryService([value], [reference(1, first_quote), reference(2, second_quote)])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown=""
    )
    options = result["question"]["options"]
    assert options[0]["label"] == second["label"]
    assert len({item["id"] for item in options}) == 2
    assert second_quote in options[0]["details"] and first_quote not in options[0]["details"]
    assert first_quote in options[1]["details"] and second_quote not in options[1]["details"]
    assert len(service.model_calls) == 1
