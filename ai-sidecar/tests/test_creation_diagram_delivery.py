"""A standalone diagram remains an image deliverable through routing and recovery."""

import hashlib
import json
import re

import pytest

from creation.agent_loop import CreationAgentLoop
from creation.diagram_delivery import (
    build_evidence_diagram,
    canonicalize_standalone_diagram,
    diagram_delivery_report,
    normalize_standalone_diagram,
)
from creation.operations import OperationError
from creation.service import CreationOptions, ReferenceDocument


REQUEST = "生成一张快手电商短视频AIGC引擎的技术架构图"
DIAGRAM = """```mermaid
flowchart LR
    A["商品与创作需求"]
    B["创作编排"]
    C["视频合成与质检"]
    A --> B
    B --> C
```"""
GROUNDED_DIAGRAM = """```mermaid
flowchart LR
    A["创作入口"]
    B["工作流模板"]
    C["视频生成服务"]
    A --> B
    B --> C
```"""


class DiagramService:
    def __init__(self, diagram=DIAGRAM):
        self.diagram = diagram
        self.memory_queries = []

    async def route_capabilities(self, **_kwargs):
        return {"source": "model", "reasoning": "生成图示", "tools": [],
                "agents": ["document_writer_agent"],
                "operation": {"kind": "generate"}}

    def analyze_requirement(self, query, _options, **_kwargs):
        return {"topic": query, "doc_type": "", "audience": "", "keywords": [],
                "needs_latest": False, "needs_images": True}

    def retrieve_references(self, query, _requirement, _options):
        self.memory_queries.append(query)
        return []

    async def refresh_recalled_documents(self, _references, _query, **_kwargs):
        return {}

    async def run_specialist_agent(self, **kwargs):
        assert kwargs["agent_id"] == "diagram_writer_agent"
        return self.diagram

    @staticmethod
    def _best_reference_content(item):
        return item.full_content

    @staticmethod
    def _clip(value, limit):
        return str(value or "")[:limit]


def diagram_reference(source_type, source_id, title, content, **extra):
    values = {"id": source_id, "title": title, "doc_type": "资料参考",
              "summary": title, "full_content": content, "sections_json": "[]",
              "style_phrases": "[]", "prompt_hint": "", "usage_count": 0,
              "review_status": "auto_created", "updated_at": 1,
              "source_url": extra.pop("source_url", None),
              "relevance_score": 0.9, "quality_score": 0.9,
              "completeness_score": 0.9, "usage_score": 0.5,
              "format_score": 0.5, "freshness_score": 0.5,
              "final_weight": 0.9, "reason": "检索命中",
              "source_type": source_type, "source_id": source_id}
    values.update(extra)
    return ReferenceDocument(**values)


async def collect_events(stream):
    return [event async for event in stream]


def test_standalone_diagram_contract_rejects_prose_and_placeholder():
    assert normalize_standalone_diagram(DIAGRAM)[0] == DIAGRAM
    assert diagram_delivery_report(DIAGRAM)["status"] == "pass"
    for candidate in (
        "# 技术架构方案\n\n文字介绍",
        "# 技术架构方案\n\n" + DIAGRAM,
        "图如下：\n" + DIAGRAM,
        "```mermaid\nflowchart LR\nA[对象 A] --> B[对象 B]\n```",
        "```mermaid\nflowchart LR\nA[输入]\nB[输出]\n```",
        "```mermaid\nflowchart LR\nA[客户端 (Client)] --> B[引擎]\n```",
        "```mermaid\nflowchart LR\nA[\"输入\"]\nB[\"输出\"]\nA --> B\ntitle 示例\n```",
    ):
        assert diagram_delivery_report(candidate)["status"] == "blocked"


def test_common_inline_mermaid_is_canonicalized_without_losing_node_labels():
    raw = """```mermaid
flowchart LR
    subgraph Layer [交互层]
        Client[客户端 (Client)] -->|提交需求| Engine[生成引擎<br/>编排]
    end
    title AIGC 技术架构方案示意
```"""
    normalized = canonicalize_standalone_diagram(raw)
    assert 'subgraph Layer["交互层"]' in normalized
    assert 'Client["客户端 (Client)"]' in normalized
    assert 'Engine["生成引擎 编排"]' in normalized
    assert '交互层 · ' not in normalized
    assert 'Client -->|提交需求| Engine' in normalized
    assert diagram_delivery_report(normalized)["status"] == "pass"


def test_non_strict_chinese_group_title_is_preserved_as_group_claim():
    raw = """```mermaid
flowchart LR
    subgraph 历史已上线与小流量平台
        A[商家版独立站] --> B[图生视频]
    end
```"""
    normalized = canonicalize_standalone_diagram(raw)
    assert 'subgraph DiagramGroup1["历史已上线与小流量平台"]' in normalized
    assert 'A["商家版独立站"]' in normalized
    assert 'B["图生视频"]' in normalized
    assert '历史已上线与小流量平台 · 商家版独立站' not in normalized
    assert diagram_delivery_report(normalized)["status"] == "pass"


@pytest.mark.parametrize("header, expected", [
    ('subgraph 推理加速层[推理加速与动态调度]', '推理加速与动态调度'),
    ('subgraph Runtime["推理加速[动态调度]"]', '推理加速［动态调度］'),
])
def test_group_header_common_labels_keep_visible_claim(header, expected):
    raw = 'flowchart LR\n{}\nA["推理加速引擎"]\nend\nB["资源调度器"]\nA --> B'.format(header)
    normalized = canonicalize_standalone_diagram(raw)
    assert 'subgraph ' in normalized and '["{}"]'.format(expected) in normalized
    assert 'A["推理加速引擎"]' in normalized
    assert diagram_delivery_report(normalized)["status"] == "pass"


@pytest.mark.parametrize("header", [
    'subgraph Runtime[调度] --> X',
    'subgraph Runtime[调度]; click X "https://example.com"',
    'subgraph Runtime[<script>alert(1)</script>]',
])
def test_group_header_rejects_effects_and_unsafe_markup(header):
    raw = 'flowchart LR\n{}\nA["推理引擎"]\nend\nB["调度器"]\nA --> B'.format(header)
    with pytest.raises(ValueError):
        canonicalize_standalone_diagram(raw)


def test_diagram_preserves_direction_and_line_style_during_normalization():
    raw = '''flowchart LR
subgraph 加速层 [拟议方案]
  Engine["推理加速引擎"] -.->|反馈| Scheduler["动态资源调度器"]
end
Scheduler <-->|容量协商| Pool["资源池"]'''
    normalized = canonicalize_standalone_diagram(raw)
    assert 'Engine -.->|反馈| Scheduler' in normalized
    assert 'Scheduler <-->|容量协商| Pool' in normalized
    assert diagram_delivery_report(normalized)["status"] == "pass"


def test_non_strict_groups_keep_membership_and_cross_group_edge():
    raw = """flowchart LR
subgraph Source [原始资料]
  A[热点复刻] --> B[工作流模板]
end
subgraph Plan [Q3目标]
  C[图生视频]
end
B --> C"""
    normalized = canonicalize_standalone_diagram(raw)
    assert normalized.count("subgraph ") == 2
    assert normalized.count('A["热点复刻"]') == 1
    assert normalized.count('B["工作流模板"]') == 1
    assert normalized.count('C["图生视频"]') == 1
    assert 'subgraph Source["原始资料"]' in normalized
    assert 'subgraph Plan["Q3目标"]' in normalized
    assert 'A --> B' in normalized and 'B --> C' in normalized
    assert diagram_delivery_report(normalized)["status"] == "pass"


@pytest.mark.parametrize("cross_group_edge", [
    "A --> B",
    'A["原节点"] --> B',
])
def test_cross_group_edge_reference_keeps_original_node_membership(cross_group_edge):
    raw = """flowchart LR
subgraph Source [原始资料]
  A["原节点"]
end
subgraph Target [拟议方案]
  {edge}
  B["目标节点"]
end""".format(edge=cross_group_edge)
    normalized = canonicalize_standalone_diagram(raw)
    assert 'subgraph Source["原始资料"]\n        A["原节点"]\n    end' in normalized
    assert 'subgraph Target["拟议方案"]\n        B["目标节点"]\n    end' in normalized
    assert "A --> B" in normalized
    assert diagram_delivery_report(normalized)["status"] == "pass"


def test_duplicate_node_declarations_in_different_groups_still_fail():
    raw = """flowchart LR
subgraph Source [原始资料]
  A["原节点"]
end
subgraph Target [拟议方案]
  A["原节点"]
  B["目标节点"]
end
A --> B"""
    with pytest.raises(ValueError, match="同一节点不能属于多个图示分组"):
        canonicalize_standalone_diagram(raw)


def test_group_identifier_cannot_be_silently_used_as_an_edge_node():
    raw = '''flowchart LR
subgraph Compute["计算层"]
  A["算力"]
end
B["优化目标"]
B --> Compute'''
    with pytest.raises(ValueError, match="分组标识不能作为连线节点"):
        canonicalize_standalone_diagram(raw)


def test_json_escaped_node_quotes_are_normalized_without_changing_edges():
    raw = '''```mermaid
flowchart LR
A[\\"输入\\"]
B[\\"输出\\"]
A --> B
```'''
    normalized = canonicalize_standalone_diagram(raw)
    assert 'A["输入"]' in normalized and 'B["输出"]' in normalized
    assert 'A --> B' in normalized
    assert diagram_delivery_report(normalized)["status"] == "pass"


def _fallback_reference(source_id, lines, *, title="资料标题", status="historical_only"):
    body = "\n".join(lines)
    source_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    spans = []
    for line in lines:
        start = body.find(line)
        start_byte = len(body[:start].encode("utf-8"))
        data = line.encode("utf-8")
        spans.append({"text": line, "snapshot_id": source_id + 10,
                      "source_body_hash": source_hash,
                      "start_byte": start_byte,
                      "end_byte": start_byte + len(data),
                      "excerpt_hash": hashlib.sha256(data).hexdigest()})
    return {"source_type": "document", "source_id": source_id,
            "source_snapshot_id": source_id + 10,
            "source_body_hash": source_hash,
            "refresh_status": status,
            "title": title, "evidence_spans": spans, "content": body,
            "source_scope": {
                "snapshot_id": source_id + 10,
                "source_body_hash": source_hash,
                "start_byte": 0, "end_byte": len(body.encode("utf-8")),
                "excerpt_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }}


def test_deterministic_fallback_builds_three_independent_source_chains():
    originals = [
        _fallback_reference(817, [
            "1.热点视频复刻，沉淀工作流模板",
            "a.全参考通用工作流，打通自动生产链路，小流量生产",
            "1.自由对话\u00a0Agent 实现图生视频、智能商详等 AIGC 功能",
        ]),
        _fallback_reference(40, [
            "●剧本库：完成剧本库管理、剧本推荐能力的上线，提供给首销、灵机业务使用",
        ]),
        _fallback_reference(649, [
            "3.原创剧本可以上传到个人资产库，走爆款复刻的视频生成链路生成视频",
        ]),
    ]
    diagram = build_evidence_diagram(originals)
    assert diagram_delivery_report(diagram)["status"] == "pass"
    assert diagram.count("subgraph Source") == 3
    assert "历史原文 · document:817" in diagram
    assert "历史已上线 · document:40" in diagram
    assert "历史原文 · document:649" in diagram
    labels = dict(re.findall(r'^\s*(Node\d+)\["([^"]+)"\]$', diagram, re.M))
    relations = {(labels[a], labels[b]) for a, b in
                 re.findall(r'^\s*(Node\d+) --> (Node\d+)$', diagram, re.M)}
    assert len(labels) == 12 and len(relations) == 7
    assert {("热点视频复刻", "工作流模板"),
            ("全参考通用工作流", "自动生产链路"),
            ("自由对话 Agent", "图生视频"),
            ("自由对话 Agent", "智能商详"),
            ("剧本库管理", "灵机业务"),
            ("原创剧本", "个人资产库"),
            ("个人资产库", "爆款复刻的视频生成链路")} == relations


def test_deterministic_fallback_rejects_unbound_or_title_only_claims():
    source = _fallback_reference(1, ["入口调用工作流模板"])
    source["title"] = "已上线供应商模型A 架构"
    source["evidence_spans"][0]["excerpt_hash"] = "0" * 64
    with pytest.raises(ValueError, match="没有可安全提取"):
        build_evidence_diagram([source])
    source["evidence_spans"] = []
    with pytest.raises(ValueError, match="没有可安全提取"):
        build_evidence_diagram([source])
    source = _fallback_reference(1, ["入口调用工作流模板"])
    forged = "入口调用虚构服务"
    source["evidence_spans"][0]["text"] = forged
    source["evidence_spans"][0]["end_byte"] = len(forged.encode("utf-8"))
    source["evidence_spans"][0]["excerpt_hash"] = hashlib.sha256(
        forged.encode("utf-8")).hexdigest()
    with pytest.raises(ValueError, match="没有可安全提取"):
        build_evidence_diagram([source])


def test_deterministic_fallback_extracts_local_core_and_adjacent_plan_components():
    originals = [
        _fallback_reference(277, [
            "●KR1：Q3完成商家平台上线，以Multi-Agent + Workflowt为核心架构，"
            "上线短视频、商品和直播三类内容。",
        ]),
        _fallback_reference(116, [
            "对话agent",
            "agent方案：固定流程 + 预置话术+\n短期记忆，结构化引导",
        ]),
    ]
    diagram = build_evidence_diagram(originals)
    assert diagram_delivery_report(diagram)["status"] == "pass"
    assert diagram.count("subgraph Source") == 2
    assert "历史原文 · Q3规划 · document:277" in diagram
    assert "历史原文 · 方案 · document:116" in diagram
    labels = dict(re.findall(r'^\s*(Node\d+)\["([^"]+)"\]$', diagram, re.M))
    relations = {(labels[a], labels[b]) for a, b in
                 re.findall(r'^\s*(Node\d+) --> (Node\d+)$', diagram, re.M)}
    assert relations == {
        ("Multi-Agent + Workflowt", "短视频"),
        ("Multi-Agent + Workflowt", "商品"),
        ("Multi-Agent + Workflowt", "直播"),
        ("对话agent", "固定流程"),
        ("对话agent", "预置话术"),
        ("对话agent", "短期记忆"),
        ("对话agent", "结构化引导"),
    }


def test_deterministic_fallback_preserves_explicit_skill_and_quality_chains():
    originals = [
        _fallback_reference(116, [
            "1.工作流：输入商品id➡️skill1生成html商品\n"
            "详情骨架➡️skill2基于骨架槽位生\n图➡️输出完整详情页",
        ]),
        _fallback_reference(277, [
            "●KR3：建立线上Badcase—标注—评\n"
            "测—模型优化—重新上线的闭\n环，双周复盘",
        ]),
    ]
    diagram = build_evidence_diagram(originals)
    labels = dict(re.findall(r'^\s*(Node\d+)\["([^"]+)"\]$', diagram, re.M))
    relations = {(labels[a], labels[b]) for a, b in
                 re.findall(r'^\s*(Node\d+) --> (Node\d+)$', diagram, re.M)}
    assert {("输入商品id", "skill1生成html商品详情骨架"),
            ("skill1生成html商品详情骨架", "skill2基于骨架槽位生图"),
            ("skill2基于骨架槽位生图", "输出完整详情页"),
            ("Badcase", "标注"), ("标注", "评测"),
            ("评测", "模型优化"), ("模型优化", "重新上线")} == relations
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="画商详和质量治理架构图", root_request="画商详和质量治理架构图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local", session_id="chains", run_id="run-1",
    )
    state.environment["references"] = originals
    claims = loop._diagram_claim_audit(
        state, diagram, {"eligible_reference_ids": ["document:116", "document:277"]})
    assert claims["passed"], claims["unsupported_claims"]


def test_distant_windows_keep_independent_offsets_and_edges():
    first = "热点视频复刻，沉淀工作流模板"
    second = "商品工作流：输入商品id➡️skill1生成页面骨架➡️skill2槽位生图➡️输出详情页"
    body = first + "\n" + ("其他资料。\n" * 120) + second
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    start = len(body[:body.index(second)].encode("utf-8"))
    def window(text, start_byte):
        raw = text.encode("utf-8")
        return {"content": text, "source_scope": {
            "snapshot_id": 77, "source_body_hash": body_hash,
            "start_byte": start_byte, "end_byte": start_byte + len(raw),
            "excerpt_hash": hashlib.sha256(raw).hexdigest(),
            "source_coverage": "complete",
        }}
    reference = {"source_type": "document", "source_id": 67,
                 "source_snapshot_id": 77, "source_body_hash": body_hash,
                 "refresh_status": "historical_only", **window(first, 0),
                 "evidence_windows": [window(second, start)]}
    reference["evidence_spans"] = CreationAgentLoop._diagram_prompt_evidence_spans(reference)
    assert len(reference["evidence_spans"]) == 2
    assert {span["start_byte"] for span in reference["evidence_spans"]} == {0, start}
    graph = build_evidence_diagram([reference])
    assert graph.count(" --> ") == 4
    assert '"工作流模板"' in graph and '"输入商品id"' in graph
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="画视频和商详能力图", root_request="画视频和商详能力图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local", session_id="windows", run_id="run-1",
    )
    state.environment["references"] = [reference]
    claims = loop._diagram_claim_audit(
        state, graph, {"eligible_reference_ids": ["document:67"]})
    assert claims["passed"], claims["unsupported_claims"]
    assert "工作流模板 → 商品工作流" not in graph


def test_deterministic_fallback_does_not_join_distant_plan_spans():
    source = _fallback_reference(116, [
        "对话agent",
        "一段没有架构关系的独立内容" * 10,
        "agent方案：固定流程 + 预置话术+\n短期记忆，结构化引导",
    ])
    source["evidence_spans"] = [source["evidence_spans"][0],
                                source["evidence_spans"][2]]
    with pytest.raises(ValueError, match="没有可安全提取"):
        build_evidence_diagram([source])


@pytest.mark.parametrize("line", [
    "完成商品解析。新的独立事项：剧本库提供给运营使用。",
    "完成商品解析；剧本库提供给运营使用。",
    "完成商品解析，新的独立事项：剧本库提供给运营使用。",
])
def test_deterministic_fallback_does_not_join_separate_completion_and_provision(line):
    source = _fallback_reference(901, [line])
    with pytest.raises(ValueError, match="没有可安全提取"):
        build_evidence_diagram([source])


def test_canonicalizer_rejects_compact_multi_node_edges_without_dropping_nodes():
    raw = 'flowchart LR\nA["输入"] & B["补充"] --> C["输出"]'
    with pytest.raises(ValueError, match="不能合并多个节点"):
        canonicalize_standalone_diagram(raw)
    grouped = """flowchart LR
subgraph Stage [阶段]
  A[输入] & B[补充] --> C[输出]
end"""
    with pytest.raises(ValueError, match="不能合并多个节点"):
        canonicalize_standalone_diagram(grouped)


def test_diagram_title_does_not_add_false_nodes_or_relations():
    normalized = canonicalize_standalone_diagram(
        DIAGRAM, title="快手电商短视频AIGC引擎技术架构图（方案示意）")
    assert normalized == DIAGRAM
    assert "DiagramOverview" not in normalized
    assert diagram_delivery_report(normalized)["status"] == "pass"
    with_model_title = canonicalize_standalone_diagram(
        DIAGRAM.replace('    A["商品与创作需求"]',
                        '    title 快手电商短视频AIGC引擎技术架构图\n    A["商品与创作需求"]'))
    assert with_model_title == DIAGRAM


def test_valid_grouped_diagram_keeps_visual_groups_without_heading_nodes():
    grouped = """```mermaid
flowchart LR
    subgraph StageOne["历史原文能力"]
        A["创作入口"]
        B["工作流模板"]
    end
    subgraph StageTwo["规划方向"]
        C["图生视频"]
    end
    A --> B
    B --> C
```"""
    assert canonicalize_standalone_diagram(grouped) == grouped
    assert diagram_delivery_report(grouped)["status"] == "pass"
    assert diagram_delivery_report(grouped.replace(
        '    B --> C', '    subgraph Nested["错误嵌套"]\n        B --> C'))["status"] == "blocked"


def test_canonicalizer_drops_only_presentation_directives_inside_group():
    raw = """flowchart LR
subgraph Historical [历史原文]
direction TB
A["工作流模板"] --> B["自动生产链路"]
end
classDef status fill:#abc
class A status"""
    normalized = canonicalize_standalone_diagram(raw)
    assert 'subgraph Historical["历史原文"]' in normalized
    assert 'A["工作流模板"]' in normalized
    assert 'A --> B' in normalized
    assert "direction" not in normalized and "classDef" not in normalized
    assert diagram_delivery_report(normalized)["status"] == "pass"


@pytest.mark.asyncio
async def test_direct_diagram_request_uses_diagram_writer_and_passes_delivery():
    service = DiagramService()
    events = await collect_events(CreationAgentLoop(service).run(
        user_message=REQUEST, current_document="", conversation=[],
        selected_skills=[], options=CreationOptions(),
    ))
    completed = next(item for item in events if item["type"] == "run.completed")
    assert completed["data"]["document"] == DIAGRAM
    assert completed["data"]["artifact_kind"] == "diagram"
    assert completed["data"]["delivery_review"]["status"] == "pass"
    decision = next(item for item in events if item["type"] == "agent.completed"
                    and "routing_decision" in item.get("data", {}))["data"]
    assert decision["routing_decision"]["operation"]["artifact_kind"] == "diagram"
    assert "diagram_writer_agent" in [step["id"] for step in decision["execution_plan"]]
    assert "document_writer_agent" not in [step["id"] for step in decision["execution_plan"]]
    assert "memory_search" in decision["routing_decision"]["tools"]
    assert [step["id"] for step in decision["execution_plan"]].index("memory_search") < [
        step["id"] for step in decision["execution_plan"]].index("diagram_writer_agent")
    assert service.memory_queries == ["快手电商短视频AIGC引擎"]
    assert any(item["type"] == "tool.completed" and item["actor"]["id"] == "memory_search"
               for item in events)


@pytest.mark.asyncio
async def test_automatic_document_skill_route_cannot_replace_standalone_diagram():
    request = "生成一张快手大模型性能成本优化的技术架构图"
    skill_id = "creation-skill-general-solution-review-doc"

    class DocumentSkillBiasedRoute(DiagramService):
        async def route_capabilities(self, **_kwargs):
            return {"source": "model", "reasoning": "使用架构方案文档模板", "tools": [],
                    "agents": ["document_writer_agent"],
                    "operation": {"kind": "execute_skill", "skill_ids": [skill_id],
                        "document_identity": {"title": "快手大模型性能成本优化的技术架构图",
                                              "source": "generated", "evidence": request},
                        "skill_assessments": [{"skill_id": skill_id,
                            "metadata_consistent": True, "applicable": True,
                            "request_evidence": request, "conflicts": [], "missing_inputs": []}]}}

    skill = {"id": skill_id, "title": "技术架构方案评审文档模板",
             "summary": "生成完整的技术架构方案评审文档",
             "execution_steps": [{"id": "write-review", "title": "编写评审文档",
                                  "agents": ["document_writer_agent"], "tools": []}]}
    events = await collect_events(CreationAgentLoop(DocumentSkillBiasedRoute()).run(
        user_message=request, current_document="", conversation=[],
        selected_skills=[skill], explicit_skill_ids=[], options=CreationOptions(),
    ))
    decision = next(item for item in events if item["type"] == "agent.completed"
                    and "routing_decision" in item.get("data", {}))["data"]
    assert decision["routing_decision"]["operation"]["kind"] == "generate"
    assert decision["routing_decision"]["operation"]["artifact_kind"] == "diagram"
    assert "skill_ids" not in decision["routing_decision"]["operation"]
    step_ids = [item["id"] for item in decision["execution_plan"]]
    assert step_ids.index("mermaid_diagram") < step_ids.index("diagram_writer_agent")
    assert "delivery_validation" in step_ids
    assert skill_id not in step_ids
    assert "document_writer_agent" not in step_ids
    assert next(item for item in events if item["type"] == "run.completed")["data"]["artifact_kind"] == "diagram"


@pytest.mark.asyncio
async def test_diagram_writer_retry_includes_rejected_mermaid_for_syntax_repair():
    class RetryingDiagramService(DiagramService):
        def __init__(self):
            super().__init__()
            self.writer_prompts = []

        async def run_specialist_agent(self, **kwargs):
            self.writer_prompts.append(json.loads(kwargs["user_prompt"]))
            if len(self.writer_prompts) == 1:
                return 'flowchart LR\nA & B --> C'
            return DIAGRAM

    service = RetryingDiagramService()
    events = await collect_events(CreationAgentLoop(service).run(
        user_message="生成一张商品创作流程图", current_document="", conversation=[],
        selected_skills=[], options=CreationOptions(),
    ))
    assert next(item for item in events if item["type"] == "run.completed")["data"]["document"] == DIAGRAM
    assert len(service.writer_prompts) == 2
    assert service.writer_prompts[1]["rejected_candidate"] == 'flowchart LR\nA & B --> C'
    assert "每个节点单独写" in service.writer_prompts[1]["diagram_retry"]


@pytest.mark.asyncio
async def test_failed_diagram_writer_checkpoint_retries_writer_on_resume():
    class RecoveringDiagramService(DiagramService):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def run_specialist_agent(self, **_kwargs):
            self.calls += 1
            return 'flowchart LR\nA & B --> C' if self.calls <= 3 else DIAGRAM

    service = RecoveringDiagramService()
    checkpoints = []
    with pytest.raises(OperationError):
        async for event in CreationAgentLoop(service).run(
                user_message="生成一张商品创作流程图", current_document="",
                conversation=[], selected_skills=[], options=CreationOptions(),
                session_id="diagram-recovery"):
            if event["type"] == "operation.checkpoint":
                checkpoints.append(event["data"]["checkpoint"])
    last = checkpoints[-1]
    assert last["plan"][last["cursor"]]["action"] == "diagram_writer"

    events = await collect_events(CreationAgentLoop(service).run(
        user_message="生成一张商品创作流程图", current_document="",
        conversation=[], selected_skills=[], options=CreationOptions(),
        session_id="diagram-recovery", resume_checkpoint=last,
    ))
    assert next(item for item in events if item["type"] == "run.completed")["data"]["document"] == DIAGRAM


@pytest.mark.asyncio
async def test_diagram_writer_retry_moves_status_suffix_into_node_label():
    invalid = 'flowchart LR\nA["网关"]:::(待核实)\nB["调度"]\nA --> B'
    corrected = 'flowchart LR\nA["网关（待核实）"]\nB["调度"]\nA --> B'

    class StatusSuffixDiagramService(DiagramService):
        def __init__(self):
            super().__init__()
            self.writer_prompts = []
            self.system_prompts = []

        async def run_specialist_agent(self, **kwargs):
            self.writer_prompts.append(json.loads(kwargs["user_prompt"]))
            self.system_prompts.append(kwargs["system_prompt"])
            return invalid if len(self.writer_prompts) == 1 else corrected

    service = StatusSuffixDiagramService()
    events = await collect_events(CreationAgentLoop(service).run(
        user_message="生成一张商品创作流程图", current_document="", conversation=[],
        selected_skills=[], options=CreationOptions(),
    ))
    assert len(service.writer_prompts) == 2
    assert "状态直接写在双引号标签内" in service.system_prompts[0]
    retry = service.writer_prompts[1]
    assert retry["rejected_candidate"] == invalid
    assert "::: 样式后缀" in retry["diagram_retry"]
    assert 'A["网关（拟议）"]' in retry["diagram_retry"]
    completed = next(item for item in events if item["type"] == "run.completed")
    assert completed["data"]["document"] == normalize_standalone_diagram(corrected)[0]
    assert completed["data"]["delivery_review"]["status"] == "pass"


@pytest.mark.asyncio
async def test_explicit_redraw_uses_diagram_writer_when_existing_diagram_is_open():
    class EditBiasedRoute(DiagramService):
        async def route_capabilities(self, **_kwargs):
            return {"source": "model", "reasoning": "已有图示", "tools": [],
                    "agents": [],
                    "operation": {"kind": "transform", "targets": ["原图"]}}

    events = await collect_events(CreationAgentLoop(EditBiasedRoute()).run(
        user_message="请重新生成一张商品创作流程图", root_request="生成一张商品创作流程图",
        current_document=DIAGRAM, conversation=[], selected_skills=[],
        options=CreationOptions(),
    ))
    decision = next(item for item in events if item["type"] == "agent.completed"
                    and "routing_decision" in item.get("data", {}))["data"]["routing_decision"]
    assert decision["operation"]["kind"] == "generate"
    assert decision["operation"]["artifact_kind"] == "diagram"
    assert next(item for item in events if item["type"] == "run.completed")["data"]["document"] == DIAGRAM


def test_diagram_recall_query_removes_output_format_without_dropping_subject():
    extract = CreationAgentLoop._diagram_subject_for_retrieval
    assert extract("生成一张快手电商短视频AIGC引擎的技术架构图") == "快手电商短视频AIGC引擎"
    assert extract("请用 Mermaid 画一张订单服务与支付服务的调用关系图") == "订单服务与支付服务的调用"
    long_instruction = (
        "请依据本地原始资料，重新生成一张“订单服务与支付服务的调用关系图”。"
        "节点只写订单服务和支付服务；最后只交付 Mermaid 图。"
    )
    assert extract(long_instruction) == "订单服务与支付服务的调用"


def test_diagram_followup_keeps_more_specific_root_subject_for_recall():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message=("请依据本地原始资料，重新生成一张快手电商短视频 AIGC 能力关系图。"
                      "节点只用原文短语，最后只交付 Mermaid 图。"),
        root_request=REQUEST, current_document="", conversation=[],
        selected_skills=[], options=CreationOptions(), model_mode="local",
        session_id="diagram-followup", run_id="run-1",
    )
    state.environment["operation"] = {"artifact_kind": "diagram"}
    assert loop._step_context_query(state, {"id": "memory_search"}) == "快手电商短视频AIGC引擎"


def test_diagram_writer_receives_bounded_source_body_and_keeps_provisional_title():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message=REQUEST, root_request=REQUEST, current_document="",
        conversation=[], selected_skills=[], options=CreationOptions(),
        model_mode="local", session_id="source-context", run_id="run-1",
    )
    state.environment["document_identity"] = {"title": "电商短视频引擎技术架构图"}
    state.environment["reference_summaries"] = [{"source_id": 42, "title": "内容引擎资料"}]
    detail = "工作说明" * 350 + "架构细节：内容中台连接脚本规划，脚本规划连接多模态合成。"
    state.environment["references"] = [
        {"id": 7, "source_type": "document", "source_id": 42,
         "title": "内容引擎资料", "summary": "简短摘要", "content": detail},
        {"id": 8, "source_type": "document", "source_id": 43,
         "title": "禁用资料", "summary": "不得使用", "content": "错误事实", "can_use": False},
    ]
    system, user = loop._base_model_prompts(state, {"action": "diagram_writer"})
    payload = json.loads(user)
    assert payload["diagram_title"].endswith("（方案示意）")
    assert len(payload["references"]) == 1
    assert payload["references"][0]["source_id"] == 42
    assert payload["references"][0]["title"] == "内容引擎资料"
    assert "架构细节：内容中台连接脚本规划" in payload["references"][0]["content"]
    assert "不得把计划画成已上线架构" in system


def test_diagram_recall_excerpt_uses_relevant_late_body_with_exact_source_scope():
    body = ("前言与目录。" * 400) + "\n订单服务调用支付服务，并向交易总线输出结果。\n"
    view = CreationAgentLoop._diagram_reference_excerpt(
        body, 300, ["订单服务", "支付服务"], 55, "complete",
        hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )
    assert "订单服务调用支付服务" in view["content"]
    scope = view["source_scope"]
    assert scope["start_byte"] > 0
    assert body.encode("utf-8")[scope["start_byte"]:scope["end_byte"]] == view["content"].encode("utf-8")
    assert scope["excerpt_hash"] == hashlib.sha256(view["content"].encode("utf-8")).hexdigest()
    assert scope["excerpt_truncated"] is True
    assert scope["allows_full_document_claims"] is False


def test_diagram_recall_excerpt_prefers_late_workflow_relations_over_early_goals():
    body = ("AIGC引擎目标是控制成本和提高使用规模。" * 180)
    body += "\nAIGC引擎架构中，创作入口调用工作流模板，工作流模板连接剧本库与视频生成服务并输出短视频。\n"
    view = CreationAgentLoop._diagram_reference_excerpt(
        body, 500, ["AIGC", "引擎"], 55, "complete",
        hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )
    assert "工作流模板连接剧本库" in view["content"]
    assert view["source_scope"]["start_byte"] > 1000


def test_diagram_recall_excerpt_starts_at_relation_after_unrelated_metrics():
    body = ("短视频AIGC业务目标与成本。" * 70)
    body += ("\n直播切片GMV和发布量。" * 16)
    body += ("\n热点视频复刻，沉淀工作流模板，打通自动生产链路。"
             "\n自由对话 Agent 实现图生视频与智能商详。")
    view = CreationAgentLoop._diagram_reference_excerpt(
        body, 360, ["短视频AIGC", "AIGC"], 55, "complete",
        hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )
    assert view["content"].startswith("热点视频复刻")
    assert "自由对话 Agent" in view["content"]
    assert "直播切片GMV" not in view["content"]
    scope = view["source_scope"]
    assert body.encode("utf-8")[scope["start_byte"]:scope["end_byte"]] == view["content"].encode("utf-8")


def test_diagram_writer_receives_exact_relation_spans_without_adjacent_metrics():
    body = ("热点视频复刻，沉淀工作流模板，提升视频转化效果\n"
            "剧本库建设，脚本库\n"
            "商家版独立站DAU，日均生成视频数\n"
            "自由对话 Agent 实现图生视频、智能商详\n"
            "剧本库：完成剧本库管理、剧本推荐能力的上线，提供给灵机使用")
    view = CreationAgentLoop._diagram_reference_excerpt(
        body, len(body), ["工作流", "剧本库"], 55, "complete",
        hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )
    spans = CreationAgentLoop._diagram_prompt_evidence_spans(view)
    text = "\n".join(item["text"] for item in spans)
    assert "热点视频复刻，沉淀工作流模板" in text
    assert "自由对话 Agent 实现图生视频" in text
    assert "剧本库管理、剧本推荐能力的上线" in text
    assert "DAU" not in text and "剧本库建设" not in text
    assert "提升视频转化" not in text
    for span in spans:
        assert body.encode("utf-8")[span["start_byte"]:span["end_byte"]] == span["text"].encode("utf-8")
        assert hashlib.sha256(span["text"].encode("utf-8")).hexdigest() == span["excerpt_hash"]


def _bound_diagram_reference(source_id, content, snapshot_id, *, preferred="", **extra):
    source_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    view = CreationAgentLoop._diagram_reference_excerpt(
        content, 1200, ["订单服务", "工作流", "剧本库"], snapshot_id,
        "complete", source_hash, preferred_excerpt=preferred,
    )
    return {"source_type": "document", "source_id": source_id,
            "title": extra.pop("title", "架构原始资料"),
            "source_snapshot_id": snapshot_id, "source_body_hash": source_hash,
            "refresh_status": "historical_only", **view, **extra}


def test_bridge_original_adds_distinct_architecture_flow_with_edge_provenance():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="画订单服务的能力关系图", root_request="画订单服务的能力关系图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local", session_id="bridge", run_id="run-1",
    )
    direct_body = "订单服务技术架构：创作入口调用工作流模板，工作流模板服务业务甲。"
    direct = _bound_diagram_reference(1, direct_body, 11, title="订单服务技术架构")
    related_body = "剧本库：完成剧本库管理、剧本推荐能力的上线，提供给业务甲使用。"
    bridge = {"anchor_snapshot_id": 11,
              "anchor_source_body_hash": direct["source_body_hash"],
              "anchor_excerpt": direct_body,
              "target_snapshot_id": 22,
              "target_source_body_hash": hashlib.sha256(related_body.encode()).hexdigest(),
              "target_excerpt": related_body,
              "entity": "业务甲", "matched_terms": ["剧本库"],
              "relation_terms": ["提供给"], "link_kind": "entity_local"}
    related = _bound_diagram_reference(
        2, related_body, 22, preferred=related_body,
        title="业务甲剧本库进展", bridge_evidence=bridge,
        selection_reasons=["historical_bridge"],
    )
    directory = {"source_type": "document", "source_id": 3,
                 "title": "行业竞品目录", "content": "业务甲与剧本库、工作流、视频生成目录"}
    state.environment.update({
        "diagram_subject_anchor": "订单服务", "diagram_source_required": True,
        "tool_results": [{"tool_id": "memory_search", "status": "completed"}],
        "references": [related, directory, direct],
    })
    audit = loop._audit_diagram_sources(state)
    assert audit["eligible_reference_ids"] == ["document:2", "document:1"]
    assert next(item for item in audit["assessments"]
                if item["reference_id"] == "document:2")["eligibility"] == "bridged"
    diagram = """```mermaid
flowchart LR
    A["创作入口"]
    B["工作流模板"]
    C["剧本库"]
    D["业务甲"]
    A --> B
    C --> D
```"""
    claims = loop._diagram_claim_audit(state, diagram, audit)
    assert claims["passed"]
    edge_sources = {item["claim"]: item["reference_ids"]
                    for item in claims["claim_provenance"] if item["kind"] == "edge"}
    assert edge_sources == {"创作入口 → 工作流模板": ["document:1"],
                            "剧本库 → 业务甲": ["document:2"]}
    assert set(claims["used_reference_ids"]) == {"document:1", "document:2"}
    false_cross_source = diagram.replace("    C --> D", "    B --> C")
    assert not loop._diagram_claim_audit(state, false_cross_source, audit)["passed"]


def test_status_group_cannot_relabel_historical_plan_as_live():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="画订单服务规划图", root_request="画订单服务规划图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local", session_id="status", run_id="run-1",
    )
    body = "订单服务规划：Q3目标以工作流为核心架构，工作流上线图生视频能力。"
    state.environment.update({
        "diagram_subject_anchor": "订单服务", "diagram_source_required": True,
        "tool_results": [{"tool_id": "memory_search", "status": "completed"}],
        "references": [_bound_diagram_reference(1, body, 11)],
    })
    audit = loop._audit_diagram_sources(state)
    diagram = """```mermaid
flowchart LR
    subgraph Live["历史已上线"]
        A["工作流"]
        B["图生视频"]
    end
    A --> B
```"""
    assert diagram_delivery_report(diagram)["status"] == "pass"
    assert "历史已上线" in loop._diagram_claim_audit(
        state, diagram, audit)["unsupported_claims"]


def test_historical_online_group_accepts_completed_launch_clause():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="画剧本库能力图", root_request="画剧本库能力图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local", session_id="online", run_id="run-1",
    )
    body = "剧本库：完成剧本库管理、剧本推荐能力的上线，提供给灵机业务使用。"
    state.environment["references"] = [_bound_diagram_reference(1, body, 11)]
    diagram = """```mermaid
flowchart LR
    subgraph HistoricalOnline["历史已上线"]
        A["剧本库管理"]
        B["灵机业务"]
    end
    A --> B
```"""
    claims = loop._diagram_claim_audit(
        state, diagram, {"eligible_reference_ids": ["document:1"]})
    assert claims["passed"], claims["unsupported_claims"]


def test_one_relation_can_support_parallel_objects_but_not_a_chained_shortcut():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="画商品内容流程图", root_request="画商品内容流程图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local", session_id="parallel", run_id="run-1",
    )
    body = "以Multi-Agent + Workflow为核心架构，上线短视频、商品和直播三类内容。"
    state.environment["references"] = [_bound_diagram_reference(1, body, 11)]
    diagram = """```mermaid
flowchart LR
    A["Multi-Agent + Workflow"]
    B["短视频"]
    C["商品"]
    D["直播"]
    A --> B
    A --> C
    A --> D
```"""
    assert loop._diagram_claim_audit(
        state, diagram, {"eligible_reference_ids": ["document:1"]})["passed"]
    chained_body = "工作流调用剧本库，剧本库生成视频。"
    state.environment["references"] = [_bound_diagram_reference(1, chained_body, 11)]
    shortcut = """```mermaid
flowchart LR
    A["工作流"]
    B["剧本库"]
    C["视频"]
    A --> C
```"""
    assert not loop._diagram_claim_audit(
        state, shortcut, {"eligible_reference_ids": ["document:1"]})["passed"]
    omitted_intermediate = shortcut.replace('    B["剧本库"]\n', '')
    assert not loop._diagram_claim_audit(
        state, omitted_intermediate, {"eligible_reference_ids": ["document:1"]})["passed"]


def test_named_diagram_audit_rejects_unparsed_diagram_type_and_empty_provenance():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="画订单服务关系图", root_request="画订单服务关系图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local", session_id="unparsed", run_id="run-1",
    )
    body = "订单服务调用工作流。"
    state.environment["references"] = [_bound_diagram_reference(1, body, 11)]
    sequence = """```mermaid
sequenceDiagram
    participant A as 订单服务
    participant B as 工作流
    A->>B: 完全虚构的调用
```"""
    assert diagram_delivery_report(sequence)["status"] == "pass"
    claims = loop._diagram_claim_audit(
        state, sequence, {"eligible_reference_ids": ["document:1"]})
    assert not claims["passed"] and not claims["used_reference_ids"]


def test_present_status_requires_fresh_local_evidence_not_unrelated_fresh_source():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="画订单服务架构图", root_request="画订单服务架构图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local", session_id="current", run_id="run-1",
    )
    historical = _bound_diagram_reference(1, "订单服务调用工作流。", 11)
    unrelated = _bound_diagram_reference(
        2, "推荐服务连接搜索服务。", 12, refresh_status="fresh_complete")
    state.environment["references"] = [historical, unrelated]
    audit = {"eligible_reference_ids": ["document:1", "document:2"]}
    for status in ("现网核心架构", "当前架构", "目前使用"):
        diagram = """```mermaid
flowchart LR
    subgraph Live["{}"]
        A["订单服务"]
        B["工作流"]
    end
    A --> B
```""".format(status)
        assert status in loop._diagram_claim_audit(
            state, diagram, audit)["unsupported_claims"]


def test_negative_relation_cannot_support_diagram_edge():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="画工作流关系图", root_request="画工作流关系图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local", session_id="negative", run_id="run-1",
    )
    state.environment["references"] = [
        _bound_diagram_reference(1, "工作流不调用剧本库。", 11)]
    diagram = """```mermaid
flowchart LR
    A["工作流"]
    B["剧本库"]
    A --> B
```"""
    assert not loop._diagram_claim_audit(
        state, diagram, {"eligible_reference_ids": ["document:1"]})["passed"]


def test_planned_relation_requires_plan_context_in_graph():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="画工作流架构图", root_request="画工作流架构图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local", session_id="plan-edge", run_id="run-1",
    )
    body = "计划由工作流调用剧本库。"
    state.environment["references"] = [_bound_diagram_reference(1, body, 11)]
    unqualified = """```mermaid
flowchart LR
    A["工作流"]
    B["剧本库"]
    A --> B
```"""
    source_audit = {"eligible_reference_ids": ["document:1"]}
    assert not loop._diagram_claim_audit(state, unqualified, source_audit)["passed"]
    qualified = unqualified.replace(
        '    A["工作流"]\n    B["剧本库"]',
        '    subgraph Planned["历史规划"]\n        A["工作流"]\n'
        '        B["剧本库"]\n    end',
    )
    assert loop._diagram_claim_audit(state, qualified, source_audit)["passed"]


def test_historical_kr_group_is_a_plan_even_when_clause_says_q3_complete():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="画Q3能力规划图", root_request="画Q3能力规划图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local", session_id="kr", run_id="run-1",
    )
    body = "KR1：Q3完成平台上线，以Multi-Agent + Workflow为核心架构，上线短视频内容。"
    state.environment["references"] = [_bound_diagram_reference(1, body, 11)]
    diagram = """```mermaid
flowchart LR
    subgraph Planned["历史Q3规划"]
        A["Multi-Agent + Workflow"]
        B["短视频"]
    end
    A --> B
```"""
    assert loop._diagram_claim_audit(
        state, diagram, {"eligible_reference_ids": ["document:1"]})["passed"]


@pytest.mark.asyncio
async def test_named_diagram_without_usable_local_source_stops_before_writer():
    class NamedDiagramService(DiagramService):
        def analyze_requirement(self, query, _options, **_kwargs):
            return {"topic": query, "keywords": ["订单服务"],
                    "entity_context": {"has_high_confidence_entity": True,
                                       "primary_entities": [{"name": "订单服务"}]}}

    service = NamedDiagramService()
    with pytest.raises(OperationError) as failure:
        await collect_events(CreationAgentLoop(service).run(
            user_message="画一张订单服务的技术架构图", current_document="",
            conversation=[], selected_skills=[], options=CreationOptions(),
        ))
    assert failure.value.code == "CREATION_EVIDENCE_UNAVAILABLE"
    assert service.memory_queries == ["订单服务"]


@pytest.mark.asyncio
async def test_real_architecture_rejects_papers_and_unrelated_original_document():
    class TangentialRecallService(DiagramService):
        def __init__(self):
            super().__init__()
            self.writer_calls = 0

        def analyze_requirement(self, query, _options, **_kwargs):
            return {"topic": query, "keywords": ["AIGC", "快手电商短视频", "引擎"],
                    "entity_context": {"has_high_confidence_entity": True,
                                       "primary_entities": [
                                           {"name": "快手电商短视频", "document_frequency_ratio": 0.0021},
                                           {"name": "引擎", "document_frequency_ratio": 0.0878}]}}

        def retrieve_references(self, query, _requirement, _options):
            self.memory_queries.append(query)
            return [
                diagram_reference("knowledge", 1926, "AIGC 前沿论文日报",
                                  "FreqForcing、OmniPhys、Hindsight Memory Network 与 CASIAL。"
                                  "研究这些论文对快手电商的启发。",
                                  matched_entities=("快手电商短视频",)),
                diagram_reference("operation", 304, "平台建设操作说明",
                                  "电商短视频内容平台可参考 Workflow 与 Agent 编排。",
                                  matched_entities=("快手电商短视频",)),
                diagram_reference("document", 1070, "Meta Prompt 迭代文档",
                                  "提示词工作流调用模型服务并输出生成结果。",
                                  source_url="https://example.test/meta-prompt"),
            ]

        async def run_specialist_agent(self, **kwargs):
            self.writer_calls += 1
            return await super().run_specialist_agent(**kwargs)

    service = TangentialRecallService()
    with pytest.raises(OperationError) as failure:
        await collect_events(CreationAgentLoop(service).run(
            user_message=REQUEST, current_document="", conversation=[],
            selected_skills=[], options=CreationOptions(),
        ))
    assert failure.value.code == "CREATION_EVIDENCE_UNAVAILABLE"
    assert service.memory_queries == ["快手电商短视频AIGC引擎"]
    assert service.writer_calls == 0


@pytest.mark.asyncio
async def test_real_architecture_uses_only_original_subject_relation_source():
    class GroundedRecallService(DiagramService):
        def __init__(self, diagram=DIAGRAM):
            super().__init__(diagram)
            self.reference_limit = 0

        def analyze_requirement(self, query, _options, **_kwargs):
            return {"topic": query, "keywords": ["AIGC", "快手电商短视频", "引擎"],
                    "entity_context": {"has_high_confidence_entity": True,
                                       "primary_entities": [
                                           {"name": "快手电商短视频", "document_frequency_ratio": 0.0021},
                                           {"name": "引擎", "document_frequency_ratio": 0.0878}]}}

        def retrieve_references(self, query, _requirement, _options):
            self.memory_queries.append(query)
            self.reference_limit = _options.max_references
            body = "快手电商短视频AIGC引擎方向。\n" + ("AIGC引擎业务目标包括成本与使用规模。" * 120)
            body += ("\n创作入口调用工作流模板，"
                     "工作流模板连接剧本库与视频生成服务并输出短视频。\n")
            return [
                diagram_reference("knowledge", 1926, "AIGC 前沿论文日报",
                                  "FreqForcing、OmniPhys 与 Hindsight Memory Network 研究。",
                                  matched_entities=("快手电商短视频",)),
                diagram_reference("document", 817, "短视频 AIGC 方向",
                                  body, source_url="https://example.test/architecture",
                                  source_snapshot_id=55,
                                  source_body_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                                  refresh_status="historical_only", refresh_completeness="complete"),
            ]

        async def run_specialist_agent(self, **kwargs):
            payload = json.loads(kwargs["user_prompt"])
            assert [item["source_id"] for item in payload["references"]] == [817]
            assert "工作流模板连接剧本库" in "\n".join(
                item["text"] for item in payload["references"][0]["evidence_spans"])
            assert payload["references"][0]["refresh_status"] == "historical_only"
            assert not payload["web_results"] and not payload["design_notes"]
            assert "不能证明现网部署" in kwargs["system_prompt"]
            return await super().run_specialist_agent(**kwargs)

    service = GroundedRecallService(GROUNDED_DIAGRAM)
    events = await collect_events(CreationAgentLoop(service).run(
        user_message=REQUEST, current_document="", conversation=[],
        selected_skills=[], options=CreationOptions(),
    ))
    completed = next(item for item in events if item["type"] == "run.completed")
    assert completed["data"]["delivery_review"]["status"] == "pass"
    assert service.reference_limit == 30
    assert [(item["source_type"], item["source_id"])
            for item in completed["data"]["references"]] == [("document", 817)]
    source_check = next(item for item in completed["data"]["delivery_review"]["checks"]
                        if item["id"] == "source_grounding")
    assert source_check["passed"] and "document:817" in source_check["evidence"]
    bogus = """```mermaid
flowchart LR
    A["创作入口"]
    B["FreqForcing"]
    C["50人审"]
    A --> B
    B --> C
```"""
    with pytest.raises(OperationError) as failure:
        await collect_events(CreationAgentLoop(GroundedRecallService(bogus)).run(
            user_message=REQUEST, current_document="", conversation=[],
            selected_skills=[], options=CreationOptions(),
        ))
    assert failure.value.code == "CREATION_EVIDENCE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_invalid_model_diagrams_fall_back_to_verified_original_relations():
    class InvalidModelService(DiagramService):
        def __init__(self):
            super().__init__()
            self.writer_calls = 0

        def analyze_requirement(self, query, _options, **_kwargs):
            return {"topic": query, "keywords": ["快手电商短视频AIGC引擎"],
                    "entity_context": {"has_high_confidence_entity": True,
                                       "primary_entities": [{"name": "快手电商短视频"}]}}

        def retrieve_references(self, query, _requirement, _options):
            body = ("快手电商短视频AIGC引擎方向。\n"
                    "热点视频复刻，沉淀工作流模板。\n"
                    "全参考通用工作流，打通自动生产链路。")
            return [diagram_reference(
                "document", 817, "短视频AIGC方向", body,
                source_url="https://example.test/source", source_snapshot_id=55,
                source_body_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                refresh_status="historical_only", refresh_completeness="complete",
            )]

        async def run_specialist_agent(self, **kwargs):
            self.writer_calls += 1
            return "flowchart LR\nA & B --> C"

    service = InvalidModelService()
    events = await collect_events(CreationAgentLoop(service).run(
        user_message=REQUEST, current_document="", conversation=[],
        selected_skills=[], options=CreationOptions(),
    ))
    completed = next(item for item in events if item["type"] == "run.completed")
    document = completed["data"]["document"]
    assert service.writer_calls == 3
    assert "热点视频复刻" in document and "自动生产链路" in document
    assert document.count("-->") == 2
    assert completed["data"]["delivery_review"]["status"] == "pass"
    assert any(item["type"] == "agent.started"
               and "已核验原文关系" in item["summary"] for item in events)


@pytest.mark.asyncio
async def test_diagram_delivery_rejects_unsourced_chinese_component_and_paper_name():
    service = DiagramService()
    loop = CreationAgentLoop(service)
    state = loop._new_state(
        user_message=REQUEST, root_request=REQUEST, current_document="",
        conversation=[], selected_skills=[], options=CreationOptions(),
        model_mode="local", session_id="source-check", run_id="run-1",
    )
    body = ("快手电商短视频AIGC引擎架构中，创作入口调用工作流模板，"
            "工作流模板连接视频生成服务。自由对话 Agent 实现图生视频、智能商详。")
    view = loop._diagram_reference_excerpt(
        body, 1600, ["AIGC", "引擎"], 55, "complete",
        hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )
    state.environment["operation"] = {"kind": "generate", "artifact_kind": "diagram"}
    state.environment["diagram_source_required"] = True
    state.environment["diagram_subject_anchor"] = "快手电商短视频"
    state.environment["tool_results"] = [{"tool_id": "memory_search", "status": "completed"}]
    state.environment["references"] = [{"source_type": "document", "source_id": 817,
        "title": "短视频 AIGC 方向", "source_snapshot_id": 55,
        "source_body_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(), **view},
        {"source_type": "knowledge", "source_id": 1926,
         "title": "AIGC 论文日报", "content": "FreqForcing 论文研究"}]
    bad = """```mermaid
flowchart LR
    A["创作入口"]
    B["虚构分发中台"]
    C["FreqForcing"]
    A --> B
    B --> C
```"""
    report = await loop._review_creation_delivery(state, bad)
    assert report["status"] == "blocked"
    assert next(check for check in report["checks"] if check["id"] == "source_grounding")["passed"]
    claims = next(check for check in report["checks"] if check["id"] == "source_claims")
    assert not claims["passed"]
    assert "虚构分发中台" in claims["reason"] and "FreqForcing" in claims["reason"]
    wrong_edge = """```mermaid
flowchart LR
    A["创作入口"]
    B["工作流模板"]
    C["视频生成服务"]
    A --> C
```"""
    edge_report = await loop._review_creation_delivery(state, wrong_edge)
    assert edge_report["status"] == "blocked"
    assert "创作入口 → 视频生成服务" in next(
        check for check in edge_report["checks"] if check["id"] == "source_claims")["reason"]
    parallel_outputs = """```mermaid
flowchart LR
    A["自由对话 Agent"]
    B["图生视频"]
    C["智能商详"]
    A --> B
    A --> C
```"""
    parallel_report = await loop._review_creation_delivery(state, parallel_outputs)
    assert parallel_report["status"] == "pass"
    false_title_edge = """```mermaid
flowchart LR
    DiagramOverview["历史原文所述能力关系示意"]
    A["自由对话 Agent"]
    B["图生视频"]
    DiagramOverview --> A
    A --> B
```"""
    title_report = await loop._review_creation_delivery(state, false_title_edge)
    assert title_report["status"] == "blocked"
    assert "历史原文所述能力关系示意" in next(
        check for check in title_report["checks"] if check["id"] == "source_claims")["reason"]


@pytest.mark.asyncio
async def test_invalid_mermaid_syntax_is_regenerated_before_delivery():
    class RetryService(DiagramService):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def run_specialist_agent(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return "以下是一篇架构说明文档。"
            return await super().run_specialist_agent(**kwargs)

    service = RetryService()
    events = await collect_events(CreationAgentLoop(service).run(
        user_message=REQUEST, current_document="", conversation=[],
        selected_skills=[], options=CreationOptions(),
    ))
    assert service.calls == 2
    assert next(item for item in events if item["type"] == "run.completed")["data"]["document"] == DIAGRAM


def test_document_identity_cannot_prepend_heading_to_diagram_event():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message=REQUEST, root_request=REQUEST,
        current_document=DIAGRAM, conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local", session_id="same-session", run_id="run-1",
    )
    state.environment["operation"] = {"kind": "generate", "artifact_kind": "diagram"}
    state.environment["document_identity"] = {"title": "架构图", "source": "generated", "evidence": REQUEST}
    event = loop._event(state, "document.replaced", "已生成图示", data={"content": DIAGRAM})
    assert event["data"]["content"] == DIAGRAM


@pytest.mark.asyncio
async def test_unavailable_standalone_image_does_not_fall_back_to_document():
    service = DiagramService()
    with pytest.raises(OperationError) as failure:
        await collect_events(CreationAgentLoop(service).run(
            user_message="生成一张宣传海报", current_document="", conversation=[],
            selected_skills=[], options=CreationOptions(),
        ))
    assert failure.value.code == "CREATION_CAPABILITY_UNAVAILABLE"


def test_failed_legacy_document_checkpoint_replans_same_operation():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message=REQUEST, root_request=REQUEST,
        current_document="# 旧文字稿\n\n架构说明，未画图。",
        conversation=[], selected_skills=[], options=CreationOptions(),
        model_mode="local", session_id="same-session", run_id="old-run",
    )
    state.environment["operation"] = {"kind": "generate"}
    state.environment["document"] = state.current_document
    state.environment["mermaid_diagram"] = {"diagram_type": "flowchart_lr"}
    state.plan = [{"kind": "agent", "id": "delivery_validation",
                   "name": "核对本轮交付条件", "action": "delivery_check"}]
    state.cursor = 0
    old_body = state.current_document
    old_hash = loop._repair_standalone_diagram_resume(state)
    assert old_hash
    assert state.environment["superseded_artifact_candidates"][-1]["content"] == old_body
    assert state.environment["operation"]["artifact_kind"] == "diagram"
    assert [step["action"] for step in state.plan] == ["memory_search", "diagram_writer", "delivery_check"]
    assert state.current_document == old_body


def test_legacy_diagram_recovery_reuses_existing_memory_references():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message=REQUEST, root_request=REQUEST,
        current_document="# 旧文字稿\n\n架构说明，未画图。",
        conversation=[], selected_skills=[], options=CreationOptions(),
        model_mode="local", session_id="same-session", run_id="old-run",
    )
    state.environment["operation"] = {"kind": "generate"}
    state.environment["references"] = [{"source_type": "document", "source_id": 42,
                                          "title": "内容引擎资料", "content": "可用正文"}]
    state.environment["mermaid_diagram"] = {"diagram_type": "flowchart_lr"}
    loop._repair_standalone_diagram_resume(state)
    assert [step["action"] for step in state.plan] == ["diagram_writer", "delivery_check"]


def test_brainstorm_chapter_recovers_frozen_diagram_after_writer_failure():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="按照已确认的章节设计创作正文",
        root_request="生成一张快手大模型性能成本优化的技术架构",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local",
        session_id="brainstorm-diagram", run_id="failed-run",
    )
    state.environment["operation"] = {"kind": "generate", "artifact_kind": "diagram"}
    state.environment["diagram_rejected_candidates"] = [{"reason": "语法无效"}]
    state.plan = [{"kind": "agent", "id": "delivery_validation",
                   "name": "核对图示交付", "action": "delivery_check"}]
    state.cursor = 0

    loop._repair_standalone_diagram_resume(state)
    assert [step["action"] for step in state.plan][-2:] == ["diagram_writer", "delivery_check"]
    assert state.environment["operation"]["artifact_kind"] == "diagram"


def test_proposal_retry_does_not_repeat_irrelevant_source_lookup():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="创作正文", root_request="生成一张性能成本技术架构",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local",
        session_id="proposal-retry", run_id="failed-run",
    )
    state.environment["operation"] = {"kind": "generate", "artifact_kind": "diagram"}
    state.environment["creation_brief"] = {"decisions": [{
        "source": "user", "dimension_id": "content_direction_1",
        "summary": "推理引擎量化", "user_inputs": ["推理引擎量化"],
    }]}
    state.environment["diagram_rejected_candidates"] = [{"reason": "现有状态未经核验"}]
    state.plan = [{"kind": "agent", "id": "delivery_validation",
                   "name": "核对图示交付", "action": "delivery_check"}]
    state.cursor = 0
    loop._repair_standalone_diagram_resume(state)
    assert state.environment["diagram_design_proposal"] is True
    assert [step["action"] for step in state.plan] == ["diagram_writer", "delivery_check"]


def test_diagram_coverage_uses_only_saved_user_decisions():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="创作正文", root_request="生成一张性能成本技术架构图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local",
        session_id="decision-coverage", run_id="run-1",
    )
    state.environment["creation_brief"] = {"decisions": [
        {"source": "user", "summary": "在图中包含‘推理加速引擎’与‘动态资源调度器’"},
        {"source": "agent_assumption", "summary": "也许包含‘未确认缓存节点’"},
    ]}
    source_only = 'flowchart LR\nA["历史资源"]\nB["历史推理"]\nA --> B'
    assert loop._diagram_missing_confirmed_labels(state, source_only) == [
        "推理加速引擎", "动态资源调度器"]
    proposed = ('flowchart LR\nA["推理加速引擎"]\nB["动态资源调度器"]\n'
                'A --- B')
    assert loop._diagram_missing_confirmed_labels(state, proposed) == []


def test_diagram_coverage_includes_selected_direction_object_without_quotes():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="创作正文", root_request="生成一张性能成本技术架构",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local",
        session_id="direction-coverage", run_id="run-1",
    )
    state.environment["creation_brief"] = {"decisions": [
        {"source": "user", "dimension_id": "content_direction_1",
         "summary": "动态批处理与缓存策略；说明适用场景",
         "user_inputs": ["动态批处理与缓存策略"]},
        {"source": "user", "dimension_id": "content_writing_1",
         "summary": "直接写入通用做法", "user_inputs": ["直接写入通用做法"]},
    ]}
    source_only = 'flowchart LR\nA["已有推理服务"]\nB["GPU"]\nA --> B'
    assert loop._diagram_missing_confirmed_labels(state, source_only) == [
        "动态批处理", "缓存策略"]
    proposal = 'flowchart LR\nA["动态批处理与缓存策略"]\nB["推理服务"]\nA --> B'
    assert loop._diagram_missing_confirmed_labels(state, proposal) == []


def test_diagram_proposal_uses_confirmed_design_without_requiring_action_verb():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="创作正文", root_request="生成一张性能成本技术架构",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local",
        session_id="proposal-coverage", run_id="run-1",
    )
    state.environment["creation_brief"] = {"decisions": [{
        "source": "user", "dimension_id": "content_direction_1",
        "summary": "引入量化感知训练与部署", "user_inputs": ["引入量化感知训练与部署"],
    }]}
    state.environment["diagram_design_proposal"] = True
    system, payload = loop._base_model_prompts(state, {"action": "diagram_writer"})
    assert "概念方案图" in system
    assert json.loads(payload)["confirmed_design_choices"] == ["引入量化感知训练与部署"]
    graph = 'flowchart LR\nA["量化感知训练（拟议）"]\nB["模型部署（拟议）"]\nA --> B'
    assert loop._diagram_missing_confirmed_labels(state, graph) == []
    assert loop._diagram_proposal_claims(graph) == []
    assert loop._diagram_proposal_claims('flowchart LR\nA["当前 GPU 2650 张"]') == [
        "当前 GPU 2650 张"]
    assert loop._diagram_proposal_claims(
        'flowchart LR\nA["快手集群实测数据"]\nB["推理服务"]\nA -->|降低显存占用| B'
    ) == ["快手集群实测数据", "降低显存占用"]
    disconnected = ('flowchart LR\nA["训练"]\nB["部署"]\nC["监测"]\nD["分析"]\n'
                    'A --> B\nC --> D')
    assert loop._diagram_proposal_structure(disconnected) == "概念方案存在未连接的孤立节点或链路"
    assert loop._diagram_proposal_structure(graph) == ""


@pytest.mark.asyncio
async def test_source_only_diagram_cannot_pass_confirmed_design_delivery():
    loop = CreationAgentLoop(DiagramService())
    state = loop._new_state(
        user_message="创作正文", root_request="生成一张性能成本技术架构图",
        current_document="", conversation=[], selected_skills=[],
        options=CreationOptions(), model_mode="local",
        session_id="decision-review", run_id="run-1",
    )
    state.environment["operation"] = {"kind": "generate", "artifact_kind": "diagram"}
    state.environment["creation_brief"] = {"decisions": [{
        "source": "user", "summary": "包含‘推理加速引擎’与‘动态资源调度器’",
    }]}
    review = await loop._review_creation_delivery(state, DIAGRAM)
    assert review["status"] == "blocked"
    assert any(item["id"] == "confirmed_design_coverage" and not item["passed"]
               for item in review["checks"])
