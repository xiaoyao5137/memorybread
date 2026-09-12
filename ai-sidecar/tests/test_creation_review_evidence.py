import pytest

from creation.operations import OperationError
from creation.review_evidence import validate_coverage_explanation


def verify(reason, description, document, passed=True):
    return validate_coverage_explanation(
        {"passed": passed, "reason": reason, "evidence": "每组至少1000条"},
        {"dimension": "分层验证", "value": "分层验证生成质量", "description": description}, document,
    )


def test_passing_explanation_cannot_invent_100_when_document_only_has_1000():
    with pytest.raises(OperationError, match="100") as failure:
        verify("文档明确按不同类目分别生成 100 条。", "按各类目分别生成100条", "每组至少1000条。")
    assert failure.value.code == "CREATION_DELIVERY_UNVERIFIED"


@pytest.mark.parametrize("value,rendered", [
    ("1000", "1,000"), ("1000", "一千"), ("1000", "0.1万"),
    ("1000", "１，０００"), ("1000", "1e3"),
    ("100", "一百"), ("100", "壹佰"), ("105", "一百零五"),
    ("120", "一百二十"), ("5600", "五千六百"), ("45", "四十五"),
    ("100000000", "一亿"), ("130000000", "一亿三千万"),
    ("50%", "百分之五十"), ("50%", "0.5"), ("50%", "五成"),
    ("35%", "三成五"), ("35%", "三成半"),
    ("0.5%", "百分之零点五"), ("0.1%", "千分之一"),
    ("5-8", "五至八"), ("-5", "负五"),
])
def test_equivalent_values_do_not_trigger_a_false_rejection(value, rendered):
    verify("文档明确采用{}条。".format(value), "采用{}条".format(value), "采用{}条。".format(rendered))


@pytest.mark.parametrize("reason", [
    "文档用明确的分层试验方案落实质量验证。",
    "100条只是示例，不强制要求采用。", "文档未采用示例100条，但验证方案已写明。",
    "无需写明100条这一可选细节。", "100条仍待确认。",
    "旧值100已改200，当前采用修订后的参数。",
])
def test_does_not_turn_all_numbers_from_input_into_mandatory_output(reason):
    verify(reason, "例如生成100条", "采用分层试验方案。")


def test_numbers_unrelated_to_the_current_choice_are_out_of_scope():
    verify("文档第一部分说明了200条的背景。", "各类目生成100条", "背景与目标。")
    verify("原稿缺失100条，因此未通过。", "各类目生成100条", "背景与目标。", passed=False)


def test_inequality_explanation_is_not_misread_as_an_exact_value_claim():
    verify("文档的安排满足至少100条的要求。", "至少100条", "实际采用1000条。")


@pytest.mark.parametrize("rendered", ["一万二", "一百二", "几百", "数百", "1×10^2", "100/2", "约±100", "0.1k", "0.0001 million"])
def test_ambiguous_or_unsupported_candidate_notation_remains_unknown(rendered):
    verify("文档明确采用100条。", "采用100条", "采用{}条。".format(rendered))


def test_chinese_lexical_words_are_not_treated_as_missing_parameters():
    verify("文档保证一致性，符合全部要求。", "统一步骤，强调一致性", "固定工作流程。")


def test_mismatch_is_checked_against_whole_document_not_one_evidence_line():
    verify("文档明确采用100条。", "采用100条", "总样本量1000条。\n各类目先验证100条。")


def test_preserves_decimal_and_percentage_magnitudes():
    with pytest.raises(OperationError):
        verify("文档写明10%的比例。", "采用10%的比例", "采用100%的比例。")


def test_chinese_reason_and_arabic_decision_are_compared_numerically():
    with pytest.raises(OperationError):
        verify("文档明确各类生成一百条。", "各类生成100条", "各类生成一千条。")
