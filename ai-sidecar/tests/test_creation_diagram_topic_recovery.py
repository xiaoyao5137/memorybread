"""Source and retry contracts for a standalone diagram with only a topic."""

import hashlib
import json

import pytest

from creation.agent_loop import CreationAgentLoop
from creation.diagram_delivery import build_evidence_diagram
from creation.service import CreationOptions, ReferenceDocument


REQUEST = "生成一张快手大模型性能成本优化的技术架构图"
TOPIC = "快手大模型性能成本优化"
SOURCE_BODY = (
    "快手大模型性能成本优化技术架构：推理请求调用模型路由服务，"
    "模型路由服务连接GPU资源池模块，GPU资源池模块输出推理结果。"
)
DIAGRAM = """```mermaid
flowchart LR
    A["推理请求"]
    B["模型路由服务"]
    C["GPU资源池模块"]
    A --> B
    B --> C
```"""


def _reference(source_type, source_id, title, body, *, snapshot_id=None):
    return ReferenceDocument(
        id=source_id, title=title, doc_type="资料参考", summary=title,
        full_content=body, sections_json="[]", style_phrases="[]",
        prompt_hint="", usage_count=0, review_status="auto_created",
        updated_at=1, source_url=None, relevance_score=0.9,
        quality_score=0.9, completeness_score=0.9, usage_score=0.5,
        format_score=0.5, freshness_score=0.5, final_weight=0.9,
        reason="检索命中", source_type=source_type, source_id=source_id,
        source_snapshot_id=snapshot_id,
        source_body_hash=(hashlib.sha256(body.encode("utf-8")).hexdigest()
                          if snapshot_id else None),
        refresh_status="historical_only", refresh_completeness="complete",
    )


def _state_reference(source_type, source_id, title, body, snapshot_id):
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    scope = {
        "schema_version": "document-excerpt.v1",
        "snapshot_id": snapshot_id, "source_body_hash": digest,
        "offset_unit": "utf8_bytes",
        "source_coverage": "complete", "start_byte": 0,
        "end_byte": len(body.encode("utf-8")), "excerpt_hash": digest,
        "excerpt_truncated": False, "allows_full_document_claims": True,
    }
    return {
        "source_type": source_type, "source_id": source_id, "title": title,
        "content": body, "source_snapshot_id": snapshot_id,
        "source_body_hash": digest, "source_scope": scope,
        # These two fields are copied from the recall candidate. They must not
        # turn an unrelated body or a derivative record into original evidence.
        "diagram_subject_body_hash": digest,
        "diagram_full_subject_coverage": 1.0,
        "refresh_status": "historical_only",
    }


class TopicDiagramService:
    def __init__(self, references=None):
        self.references = list(references or [])
        self.memory_queries = []
        self.writer_prompts = []

    async def route_capabilities(self, **_kwargs):
        return {"source": "model", "reasoning": "独立图示", "tools": [],
                "agents": [], "operation": {"kind": "generate"}}

    def analyze_requirement(self, query, _options, **_kwargs):
        return {"topic": query, "doc_type": "", "audience": "",
                "keywords": [TOPIC], "needs_latest": False,
                "entity_context": {"has_high_confidence_entity": False,
                                   "primary_entities": []}}

    def retrieve_references(self, query, _requirement, _options):
        self.memory_queries.append(query)
        return list(self.references)

    async def refresh_recalled_documents(self, _references, _query, **_kwargs):
        return {}

    async def run_specialist_agent(self, **kwargs):
        assert kwargs["agent_id"] == "diagram_writer_agent"
        self.writer_prompts.append(json.loads(kwargs["user_prompt"]))
        return DIAGRAM

    @staticmethod
    def _best_reference_content(item):
        return item.full_content

    @staticmethod
    def _clip(value, limit):
        return str(value or "")[:limit]


@pytest.mark.asyncio
async def test_brainstorm_diagram_uses_confirmed_design_as_proposal():
    class ProposalService(TopicDiagramService):
        async def run_specialist_agent(self, **kwargs):
            self.writer_prompts.append(json.loads(kwargs["user_prompt"]))
            return '''```mermaid
flowchart LR
A["量化感知训练（拟议）"]
B["模型部署（拟议）"]
A --> B
```'''

    service = ProposalService([])
    events = [event async for event in CreationAgentLoop(service).run(
        user_message="生成一张快手大模型性能成本优化的技术架构",
        root_request="生成一张快手大模型性能成本优化的技术架构",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), creation_mode="brainstorm",
        creation_brief={"decisions": [{"source": "user",
            "dimension_id": "content_direction_1",
            "summary": "引入量化感知训练与部署",
            "user_inputs": ["引入量化感知训练与部署"]}]},
    )]
    completed = next(event for event in events if event["type"] == "run.completed")
    assert "量化感知训练" in completed["data"]["document"]
    assert completed["data"]["delivery_review"]["status"] == "pass"
    assert service.writer_prompts[0]["confirmed_design_choices"] == [
        "引入量化感知训练与部署"]
    assert service.memory_queries == []


@pytest.mark.asyncio
async def test_invalid_proposal_candidates_fall_back_to_confirmed_choice_skeleton():
    class InvalidProposalService(TopicDiagramService):
        async def run_specialist_agent(self, **kwargs):
            self.writer_prompts.append(json.loads(kwargs["user_prompt"]))
            return "这是一份没有 Mermaid 代码的草稿"

    service = InvalidProposalService([])
    events = [event async for event in CreationAgentLoop(service).run(
        user_message="生成一张快手大模型性能成本优化的技术架构",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), creation_mode="brainstorm",
        creation_brief={"decisions": [
            {"source": "user", "dimension_id": "content_direction_1",
             "summary": "推理引擎量化", "user_inputs": ["推理引擎量化"]},
            {"source": "user", "dimension_id": "content_detail_1",
             "summary": "数据压缩与缓存策略", "user_inputs": ["数据压缩与缓存策略"]},
        ]},
    )]
    completed = next(event for event in events if event["type"] == "run.completed")
    diagram = completed["data"]["document"]
    assert "推理引擎量化" in diagram and "数据压缩与缓存策略" in diagram
    assert completed["data"]["delivery_review"]["status"] == "pass"
    assert any(check["id"] == "confirmed_choice_fallback"
               for check in completed["data"]["delivery_review"]["checks"])
    assert len(service.writer_prompts) == 3


@pytest.mark.asyncio
async def test_topic_only_diagram_adopts_bound_original_and_audits_claims():
    source = _reference("document", 91, TOPIC + "技术架构", SOURCE_BODY,
                        snapshot_id=911)
    service = TopicDiagramService([source])
    events = [event async for event in CreationAgentLoop(service).run(
        user_message=REQUEST, current_document="", conversation=[],
        selected_skills=[], options=CreationOptions(),
    )]

    completed = next(event for event in events if event["type"] == "run.completed")
    assert service.memory_queries == [TOPIC]
    assert completed["data"]["document"] == DIAGRAM
    assert [(item["source_type"], item["source_id"])
            for item in completed["data"]["references"]] == [("document", 91)]
    source_check = next(check for check in completed["data"]["delivery_review"]["checks"]
                        if check["id"] == "source_grounding")
    claims_check = next(check for check in completed["data"]["delivery_review"]["checks"]
                        if check["id"] == "source_claims")
    assert source_check["passed"] and claims_check["passed"]
    assert [item["source_id"] for item in service.writer_prompts[0]["references"]] == [91]


def test_topic_audit_rejects_unrelated_original_and_derivative_hit():
    service = TopicDiagramService()
    loop = CreationAgentLoop(service)
    state = loop._new_state(
        user_message=REQUEST, root_request=REQUEST, current_document="",
        conversation=[], selected_skills=[], options=CreationOptions(),
        model_mode="local", session_id="topic-audit", run_id="run-1",
    )
    state.environment.update({
        "diagram_source_required": True,
        "diagram_subject_anchor": TOPIC,
        "diagram_anchor_kind": "topic",
        "tool_results": [{"tool_id": "memory_search", "status": "completed"}],
        "references": [
            _state_reference("document", 91, TOPIC + "技术架构", SOURCE_BODY, 911),
            _state_reference("document", 92, TOPIC + "资料目录",
                             "推荐服务调用搜索服务，搜索服务连接广告系统。", 922),
            _state_reference("knowledge", 93, TOPIC + "二次摘要", SOURCE_BODY, 933),
        ],
    })

    audit = loop._audit_diagram_sources(state)
    assert audit["ready"]
    assert audit["eligible_reference_ids"] == ["document:91"]
    rejected = {item["reference_id"]: item for item in audit["assessments"]
                if not item["eligible"]}
    assert set(rejected) == {"document:92", "knowledge:93"}


@pytest.mark.asyncio
async def test_failed_diagram_resume_researches_without_losing_old_evidence():
    fresh = _reference("document", 91, TOPIC + "技术架构", SOURCE_BODY,
                       snapshot_id=911)
    service = TopicDiagramService([fresh])
    loop = CreationAgentLoop(service)
    previous_candidate = "flowchart LR\nA & B --> C"
    state = loop._new_state(
        user_message=REQUEST, root_request=REQUEST,
        current_document=previous_candidate,
        conversation=[{"role": "user", "content": REQUEST}],
        selected_skills=[], options=CreationOptions(),
        model_mode="local", session_id="same-session", run_id="old-run",
    )
    old_ref = _state_reference("document", 50, TOPIC + "历史资料",
                               "快手大模型性能成本优化曾讨论模型路由连接 GPU 资源池。", 505)
    rejected = {"document": previous_candidate, "reason": "source_claims",
                "run_id": "old-run"}
    state.environment.update({
        "operation": {"kind": "generate", "artifact_kind": "diagram"},
        "document": previous_candidate,
        "references": [old_ref],
        "reference_summaries": [{"source_type": "document", "source_id": 50,
                                 "title": old_ref["title"]}],
        "tool_results": [{"tool_id": "memory_search", "status": "completed",
                          "reference_ids": ["document:50"]}],
        "diagram_rejected_candidates": [rejected],
        "mermaid_diagram": {"diagram_type": "flowchart_lr"},
    })
    state.plan = [{"kind": "agent", "id": "delivery_validation",
                   "name": "核对图示交付", "action": "delivery_check"}]
    state.cursor = 0

    assert loop._repair_standalone_diagram_resume(state)
    assert state.plan[0]["action"] == "memory_search"
    assert state.environment["references"] == []
    assert state.environment["diagram_recall_history"][-1]["references"] == [old_ref]
    assert state.environment["diagram_rejected_candidates"] == [rejected]
    assert state.conversation == [{"role": "user", "content": REQUEST}]
    assert state.session_id == "same-session"

    events = [event async for event in loop._execute_step(
        state, state.plan[0], creation_model=None,
        creation_api_key=None, creation_base_url=None)]
    assert any(event["type"] == "tool.completed" for event in events)
    assert service.memory_queries == [TOPIC]
    assert [item["source_id"] for item in state.environment["references"]] == [91]
    assert state.environment["diagram_recall_history"][-1]["references"] == [old_ref]
    assert state.environment["diagram_rejected_candidates"] == [rejected]
    restored = type(state).restore(json.loads(json.dumps(state.serializable())))
    assert restored.environment["diagram_recall_history"][-1]["references"] == [old_ref]
    assert restored.environment["diagram_rejected_candidates"] == [rejected]


@pytest.mark.asyncio
async def test_chapter_diagram_recall_rejects_corpus_artifact_as_subject():
    class ArtifactBiasedService(TopicDiagramService):
        def analyze_requirement(self, query, options, **kwargs):
            result = super().analyze_requirement(query, options, **kwargs)
            result["entity_context"] = {
                "has_high_confidence_entity": True,
                "primary_entities": [{"name": "技术架构大图", "document_frequency_ratio": 0.01}],
            }
            result["retrieval_plan"] = {"mode": "entity_lookup", "primary_target": "技术架构大图"}
            return result

        def retrieve_references(self, query, requirement, options):
            assert query == TOPIC
            assert requirement["entity_context"] == {}
            assert requirement["keywords"] == [TOPIC]
            assert "retrieval_plan" not in requirement
            return super().retrieve_references(query, requirement, options)

    service = ArtifactBiasedService()
    loop = CreationAgentLoop(service)
    state = loop._new_state(
        user_message="按照已确认的章节设计创作正文",
        root_request="生成一张快手大模型性能成本优化的技术架构",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local",
        session_id="artifact-bias", run_id="run-1", creation_mode="brainstorm",
    )
    state.environment["operation"] = {"kind": "generate", "artifact_kind": "diagram"}
    state.environment["retrieval_query"] = state.root_request
    step = loop._tool_plan_step("memory_search")
    events = [item async for item in loop._execute_step(
        state, step, creation_model=None, creation_api_key=None, creation_base_url=None)]
    assert any(item["type"] == "tool.completed" for item in events)
    assert state.environment["diagram_subject_anchor"] == TOPIC
    assert state.environment["diagram_anchor_kind"] == "topic"


def test_topic_architecture_writer_uses_direct_original_without_source_title_nodes():
    service = TopicDiagramService()
    loop = CreationAgentLoop(service)
    state = loop._new_state(
        user_message=REQUEST, root_request=REQUEST, current_document="",
        conversation=[], selected_skills=[], options=CreationOptions(),
        model_mode="local", session_id="source-prompt", run_id="run-1",
    )
    architecture = _state_reference(
        "document", 91, TOPIC + "技术架构", SOURCE_BODY, 911)
    weekly = _state_reference(
        "document", 92, TOPIC + "周报", SOURCE_BODY, 922)
    state.environment.update({
        "diagram_source_required": True,
        "diagram_subject_anchor": TOPIC,
        "diagram_anchor_kind": "topic",
        "diagram_structure_hint": "架构",
        "tool_results": [{"tool_id": "memory_search", "status": "completed"}],
        "references": [architecture, weekly],
    })
    _, user_prompt = loop._base_model_prompts(
        state, {"action": "diagram_writer"})
    payload = json.loads(user_prompt)
    assert [item["source_id"] for item in payload["references"]] == [91]
    assert "title" not in payload["references"][0]
    assert payload["source_roles"] == [{
        "reference_id": "document:91", "eligibility": "direct",
        "refresh_status": "historical_only",
    }]


def test_source_diagram_fallback_keeps_explicit_relations_and_drops_negation():
    service = TopicDiagramService()
    loop = CreationAgentLoop(service)
    state = loop._new_state(
        user_message=REQUEST, root_request=REQUEST, current_document="",
        conversation=[], selected_skills=[], options=CreationOptions(),
        model_mode="local", session_id="source-fallback", run_id="run-1",
    )
    body = (TOPIC + "技术架构。\n"
            "在推理场景，直接使用资源池配额，引入推理引擎，完成推理服务的构建。\n"
            "需要将空闲资源提供给消费方使用。\n"
            "项目组决定首先推进“训推合池”，前置完成调度能力、训推技术、运营能力的拉通。\n"
            "资源效率无法实现最大化提升。")
    reference = _state_reference("document", 91, TOPIC + "技术架构", body, 911)
    scope = reference["source_scope"]
    evidence = [{"text": body, "snapshot_id": 911,
                 "source_body_hash": reference["source_body_hash"],
                 "start_byte": 0, "end_byte": scope["end_byte"],
                 "excerpt_hash": scope["excerpt_hash"]}]
    state.environment.update({
        "diagram_source_required": True,
        "diagram_subject_anchor": TOPIC,
        "diagram_anchor_kind": "topic",
        "tool_results": [{"tool_id": "memory_search", "status": "completed"}],
        "references": [reference],
    })
    diagram = build_evidence_diagram([{**reference, "evidence_spans": evidence}])
    assert "空闲资源" in diagram and "消费方" in diagram
    assert "推理场景" in diagram and "资源池配额" in diagram
    assert "训推合池" in diagram and "调度能力" in diagram
    assert "历史原文 · 方案" in diagram
    assert "最大化提升" not in diagram
    assert loop._diagram_claim_audit(
        state, diagram, loop._audit_diagram_sources(state))["passed"]
