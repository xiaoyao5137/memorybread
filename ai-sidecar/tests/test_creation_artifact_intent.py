"""Standalone visual deliverables stay distinct from documents with figures."""

import json
from pathlib import Path

import pytest

from creation.operations import OperationError, routing_response_schema, validate_operation
from creation.skill_governance import (
    aggregate_intent_requests,
    explicit_standalone_artifact_kind,
    intent_contract_problem,
    intent_request_schema,
    task_intent,
)


class _Model:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def _stream_direct_completion(self, **kwargs):
        self.calls.append(kwargs)
        yield json.dumps(self.result, ensure_ascii=False)


def _request(text, kind="document"):
    return {"requests": [{"request": text, "role": "task", "action": "create",
                          "artifact_kind": kind}]}


@pytest.mark.parametrize("instruction,expected", [
    ("请生成一张系统技术架构图", "diagram"),
    ("请画一个业务流程图", "diagram"),
    ("根据已有方案生成一张技术架构图", "diagram"),
    ("生成一张快手大模型性能成本优化的技术架构", "diagram"),
    ("请生成一张数据平台架构", "diagram"),
    ("生成一张架构方案文档", ""),
    ("生成一张适配业务的技术架构图", "diagram"),
    ("生成一张系统部署图，附一段简短说明", "diagram"),
    ("生成一张宣传海报", "image"),
    ("请画一幅人物插画", "image"),
    ("请生成一张图", "image"),
    ("写一份方案，配一张流程图", ""),
    ("请在当前文档中画一张流程图", ""),
    ("生成一份包含架构图的技术报告", ""),
    ("先写一份方案，再生成一张技术架构图", ""),
    ("检索 GitHub 开源仓库并用 PlantUML 画一张时序图，形成技术方案", ""),
    ("不要画一张架构图", ""),
    ("不需要画一张架构图", ""),
    ("不要写文档，只生成一张技术架构图", "diagram"),
    ("不需要生成文档，只生成一张技术架构图", "diagram"),
    ("解释‘生成一张架构图’是什么意思", ""),
])
def test_explicit_standalone_artifact_kind_uses_direct_output_request(instruction, expected):
    assert explicit_standalone_artifact_kind(instruction) == expected


@pytest.mark.asyncio
async def test_explicit_diagram_overrides_a_legacy_document_intent():
    instruction = "请生成一张系统技术架构图"
    model = _Model(_request(instruction))

    intent = await task_intent(model, instruction, False)

    assert intent["action"] == "create"
    assert intent["artifact_kind"] == "diagram"
    assert intent["primary_goal"] == instruction
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_explicit_image_is_exposed_without_falling_back_to_document():
    instruction = "生成一张宣传海报"
    intent = await task_intent(_Model(_request(instruction)), instruction, False)
    assert intent["artifact_kind"] == "image"


@pytest.mark.asyncio
async def test_document_with_embedded_diagram_remains_document():
    instruction = "写一份方案，配一张流程图"
    intent = await task_intent(_Model(_request(instruction)), instruction, False)
    assert intent["action"] == "create"
    assert intent.get("artifact_kind", "document") == "document"


def test_artifact_intent_retains_source_and_rejects_an_unsupported_claim():
    instruction = "生成一张技术架构图"
    schema = intent_request_schema()
    assert intent_contract_problem(_request(instruction, "diagram"), schema, instruction) == ""
    assert "不一致" in intent_contract_problem(_request(instruction, "image"), schema, instruction)
    assert "逐字引用" in intent_contract_problem(_request("虚构的要求", "diagram"), schema, instruction)
    assert "产物类型" in intent_contract_problem(_request(instruction, "video"), schema, instruction)
    plain = "写一段介绍"
    assert "视觉产物依据" in intent_contract_problem(_request(plain, "diagram"), schema, plain)
    embedded = "写一份方案，配一张流程图"
    assert "文档内配图" in intent_contract_problem(_request(embedded, "diagram"), schema, embedded)


def test_independent_visual_and_document_outputs_do_not_collapse_into_one():
    instruction = "写一份方案；再生成一张技术架构图"
    requests = {"requests": [
        {"request": "写一份方案", "role": "task", "action": "create"},
        {"request": "再生成一张技术架构图", "role": "task", "action": "create"},
    ]}
    with pytest.raises(ValueError, match="不同产物类型"):
        aggregate_intent_requests(requests, instruction)


def test_generate_contract_allows_diagram_and_preserves_legacy_document():
    base = {"kind": "generate", "document_identity": {
        "title": "系统技术架构图", "source": "generated", "evidence": "技术架构图"}}
    assert "artifact_kind" not in validate_operation(base)
    assert validate_operation({**base, "artifact_kind": "diagram"})["artifact_kind"] == "diagram"
    with pytest.raises(OperationError) as error:
        validate_operation({**base, "artifact_kind": "image"})
    assert error.value.code == "CREATION_CAPABILITY_UNAVAILABLE"

    variants = routing_response_schema([], [], [])["properties"]["operation"]["anyOf"]
    generate = next(item for item in variants if item["properties"]["kind"]["const"] == "generate")
    assert generate["properties"]["artifact_kind"]["enum"] == ["document", "diagram"]
    assert "artifact_kind" not in generate["required"]

    shared = json.loads((Path(__file__).resolve().parents[2] /
                         "shared/creation-operations/routing.schema.json").read_text())
    shared_generate = next(item for item in shared["properties"]["operation"]["anyOf"]
                           if item["properties"]["kind"]["const"] == "generate")
    assert shared_generate["properties"]["artifact_kind"]["enum"] == ["document", "diagram"]
