"""Citation markers express evidence use, not examples of the marker syntax."""
import pytest

from rag.memory_evidence import CitationStream, cited_memories, render_citations
from rag.retriever import RetrievedChunk
from tests.test_rag import MockLlmBackend, _make_pipeline


@pytest.fixture
def memories():
    return [
        RetrievedChunk(
            capture_id=index,
            doc_key='bake_knowledge:%s' % index,
            text='一条用于独立测试的历史事实。',
            metadata={'source_type': 'bake_knowledge', 'artifact_id': index},
        )
        for index in (1, 2)
    ]


REJECTED = [
    '结论来自本次材料，但仍需关注失败风险 [M1]（注：此处引用 M1 仅作为格式示例说明如何标注记忆，实际回答中未采用该记忆的定义来解释当前结论）。',
    '结论来自本次材料[M1]。（注：此处只是引用格式示例。）',
    '此处引用 [M1] 仅作为格式示例，实际回答中未采用该记忆。',
    '引用格式示例：[M1]。',
    '引用说明：例如 [M1] 表示第一条记忆。',
    '引用格式示例：\n[M1] [M2]',
    '格式示例：`[M1]`。',
    '正文中的代码 `[M1]` 不能作为来源。',
    '正文中的代码 ``literal `[M1]` ``。',
    '```text\n[M1]\n```',
    '~~~markdown\n[M1]\n~~~',
    '未结束的代码 `[M1]',
    '```text\n[M1]',
    '本次答案未采用该记忆[M1]。',
    '本次没有实际使用历史记忆[M1]。',
    '这条记忆未被采用[M1]。',
    '本次未采用 [M1] 中的定义。',
    'Citation format example: [M1].',
    'This answer did not use this memory [M1].',
    'This memory was not used [M1].',
    '测试通过[M1][M2]（以上引用仅作为格式示例）。',
    '测试通过[M1] [M2]（以上引用仅作为格式示例）。',
    '本次未采用这些记忆：[M1] [M2]。',
    '本次答案没有采用该记忆中的建议[M1]。',
    '结论来自材料[M1]（本次未采用该记忆）。',
]


@pytest.mark.parametrize('answer', REJECTED)
def test_examples_code_and_denied_memory_use_are_not_citations(answer, memories):
    assert cited_memories(answer, memories) == []
    rendered = render_citations(answer, memories)
    assert '[M1]' not in rendered and '[M2]' not in rendered
    assert '[记忆' not in rendered


@pytest.mark.parametrize('answer', [
    '记录中尚未复现这个错误[M1]。',
    '该项目未采用缓存方案[M1]。',
    '该项目未采用记忆压缩方案[M1]。',
    '这份资料讨论引用格式的局限性[M1]。',
    '测试失败没有改变原先结论[M1]。',
    '资料中没有这个字段[M1]。',
    '该功能并未上线。[M1]',
    '历史中的一个例子是重试失败[M1]。',
    'No failure was observed [M1].',
    '历史记录显示，项目没有采用该记忆中建议的压缩方案[M1]。',
    '调查显示，该报告没有引用参考资料[M1]。',
    '模型不依赖记忆中的用户画像[M1]。',
    'This report did not reference this memory [M1].',
    '我之前没有采用该记忆中的建议[M1]。',
])
def test_negative_facts_keep_their_supporting_citations(answer, memories):
    assert cited_memories(answer, memories) == [memories[0]]
    assert '[记忆1]' in render_citations(answer, memories)


@pytest.mark.parametrize('answer', [
    '第一条记忆证明测试通过[M1]。引用格式示例：[M2]。',
    '历史记录指出测试通过[M1]，本次未采用该记忆[M2]。',
    '历史记录支持该结论[M1]。\n\n```\n[M2]\n```',
    '代码 `[M2]`。这条历史事实仍然成立[M1]。',
    '```\n[M2]\n```\n历史事实[M1]。',
])
def test_invalid_marker_does_not_hide_other_claims(answer, memories):
    assert cited_memories(answer, memories) == [memories[0]]
    rendered = render_citations(answer, memories)
    assert '[记忆1]' in rendered
    assert '[记忆2]' not in rendered


@pytest.mark.parametrize('answer', REJECTED + [
    '普通链接[资料](https://example.com)，真实历史事实[M1]。',
    '事实[M1]。第二个事实[M2]。',
    '代码 `[M2]`。真实事实[M1]。',
    '未采用该记忆[M1]，另一个结论[M2]。',
    '无效[M99]，未完成[M',
])
@pytest.mark.parametrize('size', [1, 2, 4, 9, 100])
def test_streaming_uses_the_same_full_context_as_final_render(answer, size, memories):
    output = []
    stream = CitationStream(output.append, memories)
    for position in range(0, len(answer), size):
        stream.feed(answer[position:position + size])
    stream.finish()
    assert ''.join(output) == render_citations(answer, memories)


def test_stream_keeps_opening_text_live_but_waits_for_citation_note(memories):
    output = []
    stream = CitationStream(output.append, memories)
    stream.feed('本次材料表明测试通过')
    assert ''.join(output) == '本次材料表明测试通过'
    stream.feed('[M1]')
    assert ''.join(output) == '本次材料表明测试通过'
    stream.feed('（注：此处仅为引用格式示例，未采用该记忆）。')
    stream.finish()
    assert '[记忆' not in ''.join(output)


@pytest.mark.parametrize('stream', [False, True])
def test_pipeline_rejects_example_citation_with_one_answer_generation(stream, memories):
    # Generic reconstruction of the stored-output failure; contains no private
    # report names, measurements, knowledge IDs, or production content.
    stored_answer = '本次材料反映性能提升，但需关注失败风险 [记忆1]（注：此处引用 M1 仅作为格式示例说明如何标注记忆，实际回答中未采用该记忆的定义来解释当前结论）。'
    reconstructed = stored_answer.replace('[记忆1]', '[M1]')
    llm = MockLlmBackend(reconstructed)
    output = []
    result = _make_pipeline().query(
        '这两份材料体现什么结论？', llm=llm, supplied_contexts=memories,
        on_delta=output.append if stream else None,
    )
    assert llm.call_count == 1
    assert result.cited_contexts == []
    assert '[记忆' not in result.answer
    if stream:
        assert ''.join(output) == result.answer


def test_normal_citation_keeps_safe_source_link_and_original_number(memories):
    memories[1].metadata['source_url'] = 'https://example.com/report(1)'
    assert cited_memories('事实[M2]', memories) == [memories[1]]
    assert render_citations('事实[M2]', memories) == '事实[记忆2](https://example.com/report(1%29)'


def test_nonnumeric_superscript_marker_is_removed_without_aborting_answer(memories):
    assert cited_memories('无效编号[M²]', memories) == []
    assert render_citations('无效编号[M²]', memories) == '无效编号'
