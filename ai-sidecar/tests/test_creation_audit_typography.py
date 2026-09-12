"""Candidate quote typography cannot bypass source, fact or calendar checks."""

import copy

import pytest

from creation.delivery_contract import (
    _apply_source_audit, _source_audit_groups, source_audit_segments, with_source_scope_check,
)
from creation.operations import OperationError


def audit_fixture(document, provided=None):
    provided = provided or {}
    contract = with_source_scope_check({
        "deliverable": "本轮文档", "inputs": [], "self_contained_reason": "已有材料齐备",
        "acceptance": [{"id": "complete", "criterion": "覆盖本轮需求"}],
    })
    original = provided.get("original_document", "")
    groups = _source_audit_groups(document, original)
    report = {
        "status": "pass", "corrections": [],
        "checks": [{"id": item["id"], "passed": True, "reason": "已独立核对该条件",
                    "evidence": document} for item in contract["acceptance"]],
        "source_audit": {key: {
            "text": parts[0].strip(), "meaning": "候选表达的含义", "kind": "neutral",
            "source_id": "", "basis": "neutral_expression",
        } for key, parts in groups.items()},
    }
    return report, source_audit_segments(document, original), contract


def first_claim(report):
    return next(iter(report["source_audit"].values()))


@pytest.mark.parametrize("document,quote", [
    ("# AI读书会通知\n\n", "# AI 读书会通知"),
    ("# AI 读书会通知\n\n", "# AI读书会通知"),
    ("面向非L0商家", "面向非 L0 商家"),
    ("阅读AI材料", "阅读\u00a0AI\u00a0材料"),
])
def test_candidate_typography_quote_resolves_to_exact_original(document, quote):
    report, segments, contract = audit_fixture(document)
    first_claim(report).update(text=quote, kind="attribute", basis="unsupported")
    original_report = copy.deepcopy(report)
    result = _apply_source_audit(report, segments, {}, contract, document)

    assert result["status"] == "revise"
    failed = next(item for item in result["checks"] if item["id"] == "source_attributes_grounding")
    assert failed["evidence"] == document.strip()
    assert failed["evidence"] in document
    assert document.strip() in "\n".join(result["corrections"])
    assert report == original_report


def test_candidate_typography_does_not_prevent_valid_title_audit():
    document = "# AI读书会通知\n\n"
    provided = {"user_instruction_and_supplied_facts": "请写一份AI读书会通知。"}
    report, segments, contract = audit_fixture(document, provided)
    first_claim(report).update(text="# AI 读书会通知", basis="source_supported",
                               source_id="user_instruction_and_supplied_facts")
    assert _apply_source_audit(report, segments, provided, contract, document)["status"] == "pass"


@pytest.mark.parametrize("document,quote", [
    ("参与AI读书会", "参与AI研讨会"),
    ("面向非L0商家", "面向非 LO 商家"),
    ("共32名AI用户", "共 23 名 AI 用户"),
    ("使用AI lab材料", "使用 AIlab 材料"),
    ("数量1 000人", "数量1000人"),
    ("参与AI读书会", "参与\nAI\n读书会"),
    ("参与AI读书会", "参与 Ai 读书会"),
])
def test_candidate_quote_rejects_facts_identifiers_and_structural_changes(document, quote):
    report, segments, contract = audit_fixture(document)
    first_claim(report)["text"] = quote
    with pytest.raises(OperationError, match="连续原文片段"):
        _apply_source_audit(report, segments, {}, contract, document)


def test_candidate_typography_rejects_multiple_matches_in_one_source_part():
    document = "AI读书会与AI读书会"
    report, segments, contract = audit_fixture(document)
    first_claim(report)["text"] = "AI 读书会"
    with pytest.raises(OperationError, match="连续原文片段"):
        _apply_source_audit(report, segments, {}, contract, document)


def test_candidate_quote_prefers_verbatim_even_when_typography_has_another_match():
    document = "AI读书会与AI 读书会"
    report, segments, contract = audit_fixture(document)
    first_claim(report).update(text="AI 读书会", basis="unsupported")
    result = _apply_source_audit(report, segments, {}, contract, document)
    assert result["status"] == "revise"
    assert any(item["evidence"] == "AI 读书会" for item in result["checks"] if not item["passed"])


@pytest.mark.parametrize("quote", ["AI 读书会", "AI 读书会。\nAI读书会"])
def test_candidate_typography_rejects_ambiguity_and_crossing_unchanged_gaps(quote):
    original = "".join("原句{}。\n旧值{}。\n".format(i, i) for i in range(64))
    document = "".join("原句{}。\nAI读书会。\n".format(i) for i in range(64))
    provided = {"original_document": original}
    report, segments, contract = audit_fixture(document, provided)
    first_key = next(iter(segments))
    assert len(_source_audit_groups(document, original)[first_key]) == 2
    first_claim(report)["text"] = quote
    with pytest.raises(OperationError, match="连续原文片段"):
        _apply_source_audit(report, segments, provided, contract, document)


@pytest.mark.parametrize("basis,source_id,provided", [
    ("source_supported", "made-up-source", {"user_instruction_and_supplied_facts": "AI读书会"}),
    ("source_supported", "", {}),
    ("authorized_creation", "creation_brief", {"creation_brief": "AI读书会"}),
])
def test_candidate_typography_still_requires_actual_source_and_user_authorization(
        basis, source_id, provided):
    document = "AI读书会"
    report, segments, contract = audit_fixture(document, provided)
    first_claim(report).update(text="AI 读书会", basis=basis, source_id=source_id)
    with pytest.raises(OperationError, match="来源"):
        _apply_source_audit(report, segments, provided, contract, document)


def test_candidate_typography_is_restored_before_calendar_source_check():
    document = "本周三AI活动"
    provided = {"user_instruction_and_supplied_facts": "周三举办活动。"}
    report, segments, contract = audit_fixture(document, provided)
    first_claim(report).update(text="本周三 AI 活动", basis="source_supported",
                               source_id="user_instruction_and_supplied_facts")
    result = _apply_source_audit(report, segments, provided, contract, document)
    assert result["status"] == "revise"
    failed = next(item for item in result["checks"] if item["id"] == "source_attributes_grounding")
    assert failed["passed"] is False
    assert failed["evidence"] == document
    assert "不支持正文时间限定" in failed["reason"]


@pytest.mark.parametrize("change", [
    {"meaning": ""}, {"kind": "unknown"}, {"basis": "unknown"}, {"extra": "unknown"},
])
def test_candidate_typography_does_not_bypass_other_audit_contract_fields(change):
    document = "AI读书会"
    report, segments, contract = audit_fixture(document)
    first_claim(report).update(text="AI 读书会", **change)
    with pytest.raises(OperationError):
        _apply_source_audit(report, segments, {}, contract, document)


@pytest.mark.parametrize('padding', ['\n', '\r\n', '  \n\t'])
def test_audit_quote_outer_whitespace_recovers_exact_continuous_original(padding):
    document = '    * **测试部署：** 进行对比实验。\n\n### 专项说明\n保留用户已确认的策略。'
    report, segments, contract = audit_fixture(document)
    claim = first_claim(report)
    original = claim['text']
    claim.update(text=padding + original + padding, kind='attribute', basis='unsupported')
    result = _apply_source_audit(report, segments, {}, contract, document)
    assert result['status'] == 'revise'
    failed = next(row for row in result['checks'] if row['id'] == 'source_attributes_grounding')
    assert failed['evidence'] == original


def test_outer_whitespace_does_not_allow_crossing_omitted_source_parts():
    from creation.delivery_contract import _source_audit_quote
    assert _source_audit_quote('\n上半。下半。\n', ['上半。', '下半。']) is None
    assert _source_audit_quote('\n重复。\n', ['重复。重复。']) is None
    assert _source_audit_quote('\n\t', ['真实原文。']) is None


def test_complete_audit_explanation_keeps_evidence_checks_without_arbitrary_length_gate():
    document = '保留已确认的方案。'
    report, segments, contract = audit_fixture(document)
    explanation = '此处说明候选语义与原材料的对应关系。' * 30
    first_claim(report).update(meaning=explanation, kind='attribute', basis='unsupported')
    result = _apply_source_audit(report, segments, {}, contract, document)
    assert result['status'] == 'revise'
    assert any(row['evidence'] == document and not row['passed'] for row in result['checks'])


def test_live_audit_schema_uses_group_bound_candidate_ids_and_restores_original():
    from creation.delivery_contract import _audit_quote_catalog, _audit_schema, _decode_audit_quotes
    groups = {'first': ['正文。\n\n### 标题\n'], 'second': ['另一段。']}
    quotes = _audit_quote_catalog(groups)
    schema = _audit_schema({key: ''.join(parts) for key, parts in groups.items()}, {}, quotes)
    for key in groups:
        choices = schema['properties'][key]['properties']['text']['enum']
        assert choices and all(quotes[identifier]['segment_id'] == key for identifier in choices)
    ref = schema['properties']['first']['properties']['text']['enum'][0]
    raw = {'source_audit': {'first': {'text': ref}}}
    _decode_audit_quotes(raw, quotes)
    assert raw['source_audit']['first']['text'] == '正文。'
    with pytest.raises(OperationError, match='不属于'):
        _decode_audit_quotes({'source_audit': {'second': {'text': ref}}}, quotes)
