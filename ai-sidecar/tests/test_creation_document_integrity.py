from types import SimpleNamespace

import pytest

from creation.agent_loop import CreationAgentLoop
from creation.document_integrity import (
    integrity_problems,
    merge_rewritten_sections,
    RISK_WRITING_POLICY,
)
from creation.operations import OperationError
from tests.test_creation_agent_loop import FakeCreationService, collect_events


FACT = '根据历史报告，项目每天的成交金额为4328万元，此数字属于历史统计周期，不能用于推算其他周期的实际成交额。'


def test_detects_prose_loop_with_format_variation_and_without_newlines():
    assert 'repeated_content' in integrity_problems('\n'.join(str(i) + '. **' + FACT + '**' for i in range(8)))
    assert 'repeated_content' in integrity_problems(FACT * 8)


def test_allows_short_labels_code_and_two_legitimate_mentions():
    assert not integrity_problems(('状态：待办\n' * 20) + '```text\n' + FACT * 10 + '\n```\n' + FACT * 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('candidate', [FACT * 10, '# 文档\n\n## 目标\n仅剩一个章节。'])
async def test_polish_rejected_atomically_and_base_retained(candidate):
    base = '# 文档\n\n## 目标\n确定目标。\n\n## 执行\n完成执行。'
    state = SimpleNamespace(current_document=base, environment={})
    loop = CreationAgentLoop(FakeCreationService())
    loop._event = lambda *args, **kwargs: {'type': args[1], **kwargs}
    loop._thinking_completed = lambda *args: {'type': 'thinking.completed'}
    events = await collect_events(loop._complete_model_step(state, {'id': 'typography_polish_agent', 'name': '润色', 'action': 'polisher'}, candidate))
    assert state.current_document == base
    assert 'document' not in state.environment
    assert events[0]['type'] == 'document.mutation.rejected'
    assert not any(e['type'] == 'document.patch.applied' for e in events)


def test_structure_guard_relaxed_only_for_delivery_repair():
    base = '# 文档\n\n## 目标\n确定目标。\n\n## 执行\n完成执行。'
    restructured = base + '\n\n## 剧本设计\n新增章节内容。'
    # 普通润色：二级结构变化仍被判 section_structure_changed
    assert 'section_structure_changed' in integrity_problems(restructured, base, preserve_sections=True)
    # 交付修复：允许按核验意见重排结构
    assert 'section_structure_changed' not in integrity_problems(
        restructured, base, preserve_sections=True, allow_structure_change=True)
    # 但正文丢失守卫不受 allow_structure_change 影响，仍然生效
    big_base = '# 文档\n\n## 目标\n' + '确定目标并展开说明。' * 60
    assert 'document_content_lost' in integrity_problems(
        '# 文档\n\n## 目标\n只剩一句。', big_base, preserve_sections=True, allow_structure_change=True)


@pytest.mark.asyncio
async def test_delivery_repair_polisher_may_restructure_and_commit():
    base = '# 文档\n\n## 目标\n确定目标。\n\n## 执行\n完成执行。'
    restructured = base + '\n\n## 剧本设计\n新增章节内容。'
    state = SimpleNamespace(current_document=base, environment={})
    loop = CreationAgentLoop(FakeCreationService())
    loop._event = lambda *args, **kwargs: {'type': args[1], **kwargs}
    loop._thinking_completed = lambda *args: {'type': 'thinking.completed'}
    loop._update_goal = lambda *args: None
    loop._generation_reasoning = lambda *args: ''
    events = await collect_events(loop._complete_model_step(state,
        {'id': 'delivery_repair', 'name': '修正已发现的交付问题', 'action': 'polisher', 'delivery_repair': True},
        restructured))
    # 结构性修复被接受并写回，不再触发 mutation.rejected
    assert state.current_document == restructured
    assert not any(e['type'] == 'document.mutation.rejected' for e in events)
    assert any(e['type'] == 'document.patch.applied' for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['writer', 'skill_step'])
async def test_invalid_initial_or_external_candidate_cannot_commit(action):
    state = SimpleNamespace(current_document='', environment={})
    with pytest.raises(OperationError, match='repeated_content'):
        await collect_events(CreationAgentLoop(FakeCreationService())._complete_model_step(state, {'id': 'writer', 'name': '撰写', 'action': action}, FACT * 10))
    assert state.current_document == ''


def test_risks_are_deduplicated_and_unused_metrics_are_not_inserted():
    risk = {'label': '成交金额', 'value': '4328万元', 'kind': 'period_mismatch', 'actual_period': '2026H1'}
    results = [{'source_id': 1, 'can_use': True, 'data_risks': [risk, risk]}]
    doc = '## 业务\n成交金额：4328万元。'
    rendered, audit = CreationAgentLoop._apply_data_risk_disclosures(doc, results)
    assert audit[0]['risk_count'] == 1
    assert CreationAgentLoop._apply_data_risk_disclosures(rendered, results)[0] == rendered
    assert CreationAgentLoop._apply_data_risk_disclosures('## 目标\n讨论产品方案。', results)[0] == '## 目标\n讨论产品方案。'
    assert '同一来源、周期和风险合并说明一次' in RISK_WRITING_POLICY

@pytest.mark.parametrize('event', [{'done_reason': 'length'}, {'choices': [{'finish_reason': 'length'}]}, {'delta': {'stop_reason': 'max_tokens'}}])
def test_token_exhaustion_is_not_success(event):
    from creation.document_integrity import require_complete_generation
    with pytest.raises(OperationError) as error:
        require_complete_generation(event)
    assert error.value.code == 'CREATION_DOCUMENT_TRUNCATED'


@pytest.mark.asyncio
async def test_qwen_stream_checks_empty_final_chunk_for_truncation():
    from creation.service import CreationService
    class Response:
        async def aiter_lines(self):
            yield '{"response":"正文片段","done":false}'
            yield '{"response":"","done":true,"done_reason":"length"}'
    with pytest.raises(OperationError):
        await collect_events(CreationService.__new__(CreationService)._stream_qwen35_raw(Response()))

@pytest.mark.asyncio
async def test_local_transform_cannot_commit_repeated_replacement():
    import json
    base = '## 目标\n原始段落。\n\n## 保留\n有效内容。'
    state = SimpleNamespace(current_document=base, environment={'operation': {'targets': [{'text': '原始段落。'}]}})
    result = json.dumps({'patches': [{'action': 'replace', 'target': {'text': '原始段落。'}, 'content': FACT * 10}]})
    with pytest.raises(OperationError, match='重复正文'):
        await collect_events(CreationAgentLoop(FakeCreationService())._complete_model_step(state, {'id': 'patch_writer', 'name': '改写', 'action': 'patch_writer'}, result))
    assert state.current_document == base


def test_several_distinct_risk_sources_do_not_trigger_repetition_guard():
    results = [{'source_id': i, 'can_use': True, 'title': '来源' + str(i), 'data_risks': [
        {'label': '来源指标' + str(i), 'value': str(100 + i), 'kind': 'period_mismatch', 'actual_period': '2026H1'}]} for i in range(5)]
    document = '## 历史背景\n' + '\n'.join('来源指标' + str(i) + '：' + str(100 + i) for i in range(5))
    rendered, audit = CreationAgentLoop._apply_data_risk_disclosures(document, results)
    assert len(audit) == 5
    assert not integrity_problems(rendered)


def test_numeric_zero_reference_is_preserved():
    result = {'source_id': 1, 'can_use': True, 'data_risks': [{'label': '失败次数', 'value': 0, 'kind': 'period_mismatch'}]}
    rendered, audit = CreationAgentLoop._apply_data_risk_disclosures('## 统计\n失败次数：0。', [result])
    assert audit[0]['risk_count'] == 1
    assert '| 失败次数 | 0 |' in rendered


def _section(title: str, body: str, repeat: int) -> str:
    return '## ' + title + '\n\n' + body * repeat + '\n\n'


BASE_DOC = ('# 周报\n\n'
            + _section('概览', '概览原始说明，本周整体运行情况保持稳定。', 12)
            + _section('用量', '用量原始数据，按项目维度统计卡数与成本。', 12)
            + _section('风险', '风险原始说明，需核对统计周期与口径。', 12))


def _polish_loop_state(base: str):
    state = SimpleNamespace(current_document=base, environment={})
    loop = CreationAgentLoop(FakeCreationService())
    loop._event = lambda *args, **kwargs: {'type': args[1], **kwargs}
    loop._thinking_completed = lambda *args: {'type': 'thinking.completed'}
    loop._update_goal = lambda *args: None
    loop._generation_reasoning = lambda *args: ''
    return loop, state


@pytest.mark.asyncio
async def test_truncated_delivery_repair_is_salvaged_by_section_merge():
    """修正稿只因写不完而偏短时，已改写的章节要落地，不能整份丢弃。"""
    # 候选稿重写了前两章就停笔；第三章完全缺失。
    candidate = ('# 周报\n\n'
                 + _section('概览', '概览已按验收意见改写。', 2)
                 + _section('用量', '用量已按验收意见改写。', 2))
    problems = integrity_problems(candidate, BASE_DOC, preserve_sections=True, allow_structure_change=True)
    assert problems == ['document_content_lost']
    loop, state = _polish_loop_state(BASE_DOC)

    events = await collect_events(loop._complete_model_step(state,
        {'id': 'delivery_repair', 'name': '修正已发现的交付问题', 'action': 'polisher', 'delivery_repair': True},
        candidate))

    committed = state.environment['document']
    assert not any(e['type'] == 'document.mutation.rejected' for e in events)
    assert any(e['type'] == 'document.mutation.salvaged' for e in events)
    # 已重写的章节采纳新版；最后一章按截断残段处理，缺失章节沿用基线原文。
    assert '概览已按验收意见改写。' in committed
    assert '风险原始说明' in committed and '用量原始数据' in committed
    assert not integrity_problems(committed, BASE_DOC, preserve_sections=True, allow_structure_change=True)
    record = state.environment['salvaged_document_mutations'][0]
    assert record['problems'] == ['document_content_lost']
    assert record['candidate_length'] < record['merged_length']


@pytest.mark.asyncio
async def test_repeated_content_polish_is_still_rejected_without_salvage():
    """拯救只对正文偏短生效；重复正文仍整份丢弃，不拼出不可信正文。"""
    base = '# 文档\n\n' + _section('目标', '确定目标并展开说明。', 40)
    candidate = '# 文档\n\n' + _section('目标', FACT, 6)
    loop, state = _polish_loop_state(base)

    events = await collect_events(loop._complete_model_step(state,
        {'id': 'delivery_repair', 'name': '修正已发现的交付问题', 'action': 'polisher', 'delivery_repair': True},
        candidate))

    assert state.current_document == base
    assert events[0]['type'] == 'document.mutation.rejected'
    assert events[0]['data']['problems'] == ['repeated_content']
    assert 'salvaged_document_mutations' not in state.environment


def test_merge_rewritten_sections_requires_overlapping_sections():
    candidate = '# 周报\n\n## 其他\n\n不相关的新章节内容。\n'
    assert merge_rewritten_sections(BASE_DOC, candidate) == ''
    # 基线没有二级章节时无从对齐，保持原行为交由调用方处理。
    assert merge_rewritten_sections('只有正文没有标题的长文本。', candidate) == ''


def test_merge_rewritten_sections_ignores_headings_inside_code_fence():
    base = '# 周报\n\n## 代码\n\n```md\n## 伪标题\n块内内容\n```\n\n## 附录\n\n附录原文。\n'
    candidate = '# 周报\n\n## 代码\n\n```md\n## 伪标题\n块内内容\n```\n'
    merged = merge_rewritten_sections(base, candidate)
    # 围栏内的 ## 伪标题不会被当作章节，附录也不会被误删。
    assert '附录原文' in merged
