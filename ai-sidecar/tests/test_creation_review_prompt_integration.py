import copy
import json

import pytest

from creation.delivery_contract import (_source_audit_groups, _source_audit_group_ranges,
    _source_check_evidence_in_batch, review_delivery, validate_review, with_source_scope_check)
from creation.operations import OperationError
from tests.test_creation_delivery_contract import StreamService, contract, review_for_model


class BatchService:
    def __init__(self, failed=(), malformed_after=None, source_supported=False):
        self.failed, self.calls = set(failed), []
        self.malformed_after, self.source_supported = malformed_after, source_supported

    async def _stream_complete_agent_output(self, **kwargs):
        self.calls.append(kwargs)
        if self.malformed_after is not None and len(self.calls) > self.malformed_after:
            yield '{"source_audit":'
            return
        payload = json.JSONDecoder().raw_decode(kwargs['user_prompt'])[0]
        last = [key for key, text in payload['candidate_document_lines'].items() if text.strip()][-1]
        checks = {key: {'passed': True, 'reason': '本批实际核对通过', 'evidence': last} for key in payload['required_check_ids']}
        audit = {}
        for key, parts in payload['candidate_source_segments'].items():
            assert all(isinstance(part, str) for part in parts)
            audit[key] = {'text': parts[0].strip(), 'meaning': '核对片段：' + key,
                'source_id': 'user_instruction_and_supplied_facts' if self.source_supported else '',
                'kind': 'attribute' if key in self.failed or self.source_supported else 'neutral',
                'basis': 'source_supported' if self.source_supported else 'unsupported' if key in self.failed else 'neutral_expression'}
        yield json.dumps({'status': 'pass', 'source_audit': audit, 'checks': checks, 'corrections': []}, ensure_ascii=False)


def long_document():
    return '# 完整方案\n\n' + '\n\n'.join('片段{:03d}：'.format(i) + '依据用户材料说明输入与产物，保持事实边界。' * 10 for i in range(90)) + '\n尾部独立事实。'


def payloads(service):
    return [json.JSONDecoder().raw_decode(call['user_prompt'])[0] for call in service.calls]


async def test_long_review_batches_direct_text_with_complete_body_and_materials():
    document = long_document()
    groups, service = _source_audit_groups(document), BatchService()
    env = {'input_context': {'root_request': '完整目标', 'creation_brief': '最后一项', 'conversation': [{'role': 'user', 'content': '用户更正'}]}}
    before = copy.deepcopy(env)
    result = await review_delivery(service, '生成', document, contract(), env)
    actual, prompts = {}, payloads(service)
    assert len(prompts) == (len(groups) + 7) // 8
    for i, payload in enumerate(prompts):
        assert 'candidate_document' not in payload
        assert ''.join(payload['candidate_document_lines'].values()) == document
        assert any(not text.strip() for text in payload['candidate_document_lines'].values())
        assert 1 <= len(payload['candidate_source_segments']) <= 8
        assert not set(actual).intersection(payload['candidate_source_segments'])
        actual.update(payload['candidate_source_segments'])
        assert payload['provided_materials'] == prompts[0]['provided_materials']
        assert payload['source_catalog'] == prompts[0]['source_catalog']
        assert len(payload['required_check_ids']) == (4 if i == 0 else 3)
        assert 'line_range' not in json.dumps(payload['candidate_source_segments'])
        assert service.calls[i]['json_schema']['properties']['source_audit']['required'] == list(payload['candidate_source_segments'])
    assert actual == groups and env == before
    assert validate_review(result, with_source_scope_check(contract()), document)['status'] == 'pass'


async def test_delivery_review_compresses_duplicate_evidence_and_reserves_output_budget():
    from creation.prompt_evidence import creation_context_window_tokens
    from monitor.llm_tracker import estimate_tokens
    document = long_document()
    environment = {
        'data_results': [{
            'source_id': 'report-{}'.format(index),
            'title': '报表{}'.format(index),
            'source_kind': 'report_url',
            'can_use': True,
            'content_excerpt': 'GPU利用率{}%，统计周期为2026年第{}周。'.format(index, index) * 30,
            'structured_data': {'metric': 'GPU利用率', 'value': index, 'period': '2026-W{}'.format(index)},
        } for index in range(30)],
        'references': [{
            'source_id': index,
            'title': '参考资料{}'.format(index),
            'content': '成本优化事实{}。'.format(index) * 200,
            'can_use': True,
        } for index in range(16)],
        'input_context': {'root_request': '生成GPU成本优化周报'},
    }
    before = copy.deepcopy(environment)
    service = BatchService()
    await review_delivery(service, '生成GPU成本优化周报', document, contract(), environment)
    call = service.calls[0]
    payload = json.JSONDecoder().raw_decode(call['user_prompt'])[0]
    prompt_tokens = estimate_tokens(call['system_prompt'] + '\n\n' + call['user_prompt']) + 256
    inventory = payload['provided_materials']['retrieved_evidence']
    assert prompt_tokens + call['num_predict'] + 512 < creation_context_window_tokens()
    assert 'structured_data' not in inventory['data_results'][0]
    assert 'content_excerpt' not in inventory['data_results'][0]
    assert payload['source_catalog']
    assert any('GPU利用率' in item['text'] for item in payload['source_catalog'].values())
    assert environment == before


async def test_short_review_keeps_existing_prompt_representation():
    service = StreamService([json.dumps(review_for_model(document='line-1'))])
    result = await review_delivery(service, '保留正文', '现有正文。', contract(), {})
    assert payloads(service)[0]['candidate_document'] == '现有正文。'
    assert result['status'] == 'pass'


@pytest.mark.parametrize('modified', [False, True])
async def test_both_source_outcomes_only_allow_lines_in_current_changed_batch(modified):
    original, document = repeated_middle_fragment_documents('\n') if modified else ('', long_document())
    service = BatchService()
    await review_delivery(service, '追加正文', document, contract(), {'input_base_document': original})
    ranges = _source_audit_group_ranges(document, original)
    line_ranges, offset = {}, 0
    for index, line in enumerate(document.splitlines(keepends=True), 1):
        line_ranges['line-{}'.format(index)] = (offset, offset + len(line.rstrip('\n')))
        offset += len(line)
    for call, payload in zip(service.calls, payloads(service)):
        segments = {key: ''.join(parts) for key, parts in payload['candidate_source_segments'].items()}
        expected = {key for key, value in payload['candidate_document_lines'].items() if value.strip()
            and _source_check_evidence_in_batch(value.rstrip('\n'), document, segments, ranges, line_ranges[key])}
        properties = call['json_schema']['properties']['checks']['properties']
        for key in payload['required_check_ids'][:3]:
            variants = properties[key]['oneOf']
            positive = next(row for row in variants if row['properties']['passed']['const'] is True)
            assert set(positive['properties']['evidence']['enum']) == expected
            negative = next(row for row in variants if row['properties']['passed']['const'] is False)
            assert set(negative['properties']['evidence']['enum']) == expected
            assert '' not in expected
            if modified:
                assert 'line-2' not in expected
        # Whole-document acceptance conditions keep their independent scope.
        if 'a' in properties:
            negative = next(row for row in properties['a']['oneOf'] if row['properties']['passed']['const'] is False)
            assert 'line-1' in negative['properties']['evidence']['enum']


async def test_source_tail_and_early_failures_survive_middle_passes_and_collision_ids():
    document = long_document()
    groups = _source_audit_groups(document)
    keys = list(groups)
    failed = [keys[0], keys[9], keys[-1]]
    conditions = contract()
    conditions['acceptance'].append({'id': 'source_attributes_grounding', 'criterion': '用户原有条件'})
    env, service = {}, BatchService(failed)
    result = await review_delivery(service, '生成', document, conditions, env)
    by_id = {check['id']: check for check in result['checks']}
    assert len(by_id) == len(result['checks']) == 5
    assert by_id['source_attributes_grounding']['passed'] is True
    check = by_id['source_attributes_grounding_']
    assert check['passed'] is False and result['status'] == 'revise'
    for key in failed:
        quote = groups[key][0].strip()
        assert any(row['evidence'] == quote and row['id'] == check['id'] for row in env['delivery_pending_review']['checks'])
        assert quote in '\n'.join(result['corrections'])
        assert quote in check['reason'] or quote == check['evidence']
    assert all(prompt['required_check_ids'][:3] == ['source_scope_grounding', check['id'], 'source_obligations_grounding'] for prompt in payloads(service))
    assert validate_review(result, with_source_scope_check(conditions), document)['status'] == 'revise'
    env['delivery_repair_count'] = 1
    assert (await review_delivery(BatchService(), '生成', document, conditions, env))['status'] == 'pass'
    assert 'delivery_pending_review' not in env


async def test_partial_changes_never_reintroduce_original_gaps():
    original = ''.join('既有事实{}。\n原值{}。\n'.format(i, i) for i in range(90))
    document = ''.join('既有事实{}。\n新值{}。\n'.format(i, i) for i in range(90))
    groups = _source_audit_groups(document, original)
    assert len(groups) > 8 and any(len(parts) > 1 for parts in groups.values())
    env, service = {'input_base_document': original}, BatchService()
    await review_delivery(service, '仅修改值', document, contract(), env)
    actual = {}
    for prompt in payloads(service):
        actual.update(prompt['candidate_source_segments'])
        assert prompt['provided_materials']['original_document'] == original
        assert all(part in document and '既有事实' not in part for parts in prompt['candidate_source_segments'].values() for part in parts)
    assert actual == groups


async def test_general_contract_once_then_source_batches_then_eight_choice_batches():
    document = long_document()
    entries = [{'id': 'choice-{}'.format(i), 'source': 'user', 'dimension': '维度', 'value': '用户选择', 'description': ''} for i in range(17)]
    conditions = contract()
    conditions['acceptance'] += [{'id': row['id'], 'criterion': '保留选择'} for row in entries]
    service = BatchService()
    result = await review_delivery(service, '生成', document, conditions, {'input_context': {'brainstorm_decisions': entries}})
    prompts = payloads(service)
    count = (len(_source_audit_groups(document)) + 7) // 8
    assert len(prompts) == count + 3
    assert sum('a' in prompt['required_check_ids'] for prompt in prompts) == 1
    assert all(len(prompt['required_check_ids']) == 3 for prompt in prompts[1:count])
    assert [len(prompt['required_check_ids']) for prompt in prompts[count:]] == [8, 8, 1]
    for prompt in prompts[count:]:
        assert prompt['source_catalog'] == {} and prompt['candidate_source_segments'] == {}
        assert 'candidate_document' not in prompt and ''.join(prompt['candidate_document_lines'].values()) == document
    assert len(result['checks']) == len({row['id'] for row in result['checks']}) == 3 + len(conditions['acceptance'])


async def test_later_batch_invalid_output_does_not_modify_original_pending_state():
    env = {'delivery_pending_review': {'scope': 'old', 'checks': [{'id': 'a', 'reason': '旧问题', 'evidence': '旧文'}], 'corrections': ['旧修正']}}
    before = copy.deepcopy(env)
    with pytest.raises(OperationError):
        await review_delivery(BatchService(malformed_after=1), '生成', long_document(), contract(), env)
    assert env == before


async def test_calendar_global_once_sees_tail_but_not_unchanged_old_date():
    original = '旧安排在2020年1月1日。\n'
    document = original + long_document() + '\n新安排在2031年3月3日。'
    service = BatchService()
    await review_delivery(service, '追加', document, contract(), {'input_base_document': original})
    calendar = [row for prompt in payloads(service) for row in prompt['contract']['acceptance'] if row['id'].startswith('calendar_grounding')]
    assert len(calendar) == 1 and '2031年3月3日' in calendar[0]['criterion'] and '2020年1月1日' not in calendar[0]['criterion']


async def test_every_code_downgraded_calendar_claim_is_saved_as_independent_finding():
    env = {}
    result = await review_delivery(BatchService(source_supported=True), '整理给定材料', '本周三会议。下周四培训。', contract(), env)
    assert result['status'] == 'revise'
    claims = [row for row in env['delivery_pending_review']['checks'] if row.get('anchor_kind') == 'source_claim']
    assert any('本周三' in row['evidence'] for row in claims)
    assert any('下周四' in row['evidence'] for row in claims)


class SourceCheckEvidenceService(BatchService):
    def __init__(self, first_group, check_id, evidence, recover=True):
        super().__init__([first_group])
        self.first_group, self.check_id, self.evidence = first_group, check_id, evidence
        self.recover = recover

    async def _stream_complete_agent_output(self, **kwargs):
        async for chunk in super()._stream_complete_agent_output(**kwargs):
            result = json.loads(chunk)
            if self.first_group in result['source_audit'] and (len(self.calls) == 1 or not self.recover):
                result['checks'][self.check_id].update(passed=False, evidence=self.evidence,
                    reason='独立检查发现的待核限定')
                result['status'] = 'revise'
            yield json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize('outside', ['unchanged', 'other_batch', 'unchanged_gap'])
async def test_out_of_batch_failure_evidence_is_retried_even_with_another_real_negative(outside):
    original = '既有团队成员安排。\n'
    document = original + long_document()
    evidence = 'line-1'
    check_id = 'source_scope_grounding'
    if outside == 'other_batch':
        evidence = 'line-{}'.format(len(document.splitlines()))
        check_id = 'source_attributes_grounding'
    elif outside == 'unchanged_gap':
        original = ''.join('既有事实{}。\n原值{}。\n'.format(i, i) for i in range(90))
        document = ''.join('既有事实{}。\n新值{}。\n'.format(i, i) for i in range(90))
        evidence = 'line-3'
    groups = _source_audit_groups(document, original)
    service = SourceCheckEvidenceService(next(iter(groups)), check_id, evidence)
    env = {'input_base_document': original}
    result = await review_delivery(service, '只修改新增内容', document, contract(), env)
    assert len(service.calls) == (len(groups) + 7) // 8 + 1
    assert '失败证据未落在本批实际新增或修改' in service.calls[1]['user_prompt']
    assert result['status'] == 'revise'
    by_id = {check['id']: check for check in result['checks']}
    assert by_id['source_scope_grounding']['passed'] is True
    assert by_id['source_attributes_grounding']['passed'] is False
    old_evidence = document.splitlines()[int(evidence.split('-')[1]) - 1]
    assert not any(row['evidence'] == old_evidence for row in env['delivery_pending_review']['checks'])


async def test_repeated_out_of_batch_failure_is_rejected_without_changing_pending_state():
    original = '原有团队身份。\n'
    document = original + long_document()
    service = SourceCheckEvidenceService(next(iter(_source_audit_groups(document, original))),
        'source_scope_grounding', 'line-1', recover=False)
    env = {'input_base_document': original, 'delivery_pending_review': {'scope': 'old', 'checks': [], 'corrections': []}}
    before = copy.deepcopy(env)
    with pytest.raises(OperationError, match='无法核验'):
        await review_delivery(service, '仅追加', document, contract(), env)
    assert len(service.calls) == 2 and env == before


@pytest.mark.parametrize('original,document', [
    ('', '待核身份带来另一项待核性质。'),
    ('已有前缀。', '已有前缀。待核身份带来另一项待核性质。'),
])
async def test_same_group_independent_failure_kind_survives_with_a_valid_changed_line(original, document):
    groups = _source_audit_groups(document, original)
    assert len(groups) == 1
    service = SourceCheckEvidenceService(next(iter(groups)), 'source_scope_grounding', 'line-1')
    env = {'input_base_document': original}
    result = await review_delivery(service, '处理新增内容', document, contract(), env)
    by_id = {check['id']: check for check in result['checks']}
    assert result['status'] == 'revise' and len(service.calls) == 1
    assert by_id['source_scope_grounding']['passed'] is False
    assert by_id['source_attributes_grounding']['passed'] is False
    assert '独立检查发现的待核限定' in '\n'.join(result['corrections'])


@pytest.mark.parametrize('separator', ['\n', '\r\n', '\n\n'])
@pytest.mark.parametrize('select_old_line', [True, False])
async def test_repeated_line_text_binds_the_selected_old_or_changed_occurrence(separator, select_old_line):
    repeated = '重复的待核商家身份。'
    original = repeated + separator
    document = original + '新增的另一项待核性质。' + separator + repeated + separator
    groups = _source_audit_groups(document, original)
    assert any(repeated in part for parts in groups.values() for part in parts)
    last_line = max(index for index, line in enumerate(document.splitlines(), 1) if line.strip())
    selected = 'line-1' if select_old_line else 'line-{}'.format(last_line)
    service = SourceCheckEvidenceService(next(iter(groups)), 'source_scope_grounding', selected, recover=False)
    env = {'input_base_document': original}
    before = copy.deepcopy(env)
    if select_old_line:
        with pytest.raises(OperationError, match='无法核验'):
            await review_delivery(service, '保留旧文，只审核新增部分', document, contract(), env)
        assert len(service.calls) == 2 and env == before
        assert '失败证据未落在本批实际新增或修改' in service.calls[1]['user_prompt']
    else:
        result = await review_delivery(service, '保留旧文，只审核新增部分', document, contract(), env)
        assert len(service.calls) == 1 and result['status'] == 'revise'
        check = next(row for row in result['checks'] if row['id'] == 'source_scope_grounding')
        assert check['passed'] is False and check['evidence'] == repeated
        assert set(check) == {'id', 'passed', 'reason', 'evidence'}


def repeated_middle_fragment_documents(separator):
    original = separator.join(['repeat。', 'anchor0。', 'anchor1。', 'anchor2。']) + separator
    document = separator.join(['new0。', 'repeat。', 'anchor0。', 'repeat。', 'anchor1。', 'new2。', 'anchor2。']) + separator
    # More than 64 changes make the first source group contain three changed
    # parts, with an unchanged duplicate before the selected middle part.
    for index in range(64):
        original += 'stable{}。'.format(index) + separator
        document += 'new{}。'.format(index + 3) + separator + 'stable{}。'.format(index) + separator
    return original, document


@pytest.mark.parametrize('separator', ['\n', '\r\n', '\n\n'])
def test_middle_changed_part_keeps_its_diff_position_past_unchanged_duplicate(separator):
    original, document = repeated_middle_fragment_documents(separator)
    groups = _source_audit_groups(document, original)
    ranges = _source_audit_group_ranges(document, original)
    first_key = next(iter(groups))
    assert groups[first_key] == ['new0。' + separator, 'repeat。' + separator, 'new2。' + separator]
    assert {key: [document[start:end] for start, end in parts] for key, parts in ranges.items()} == groups
    old_start = document.index('repeat。')
    new_start = document.index('repeat。', old_start + 1)
    assert ranges[first_key][1] == (new_start, new_start + len('repeat。' + separator))
    batch = {first_key: ''.join(groups[first_key])}
    assert _source_check_evidence_in_batch('repeat。', document, batch, ranges,
        (old_start, old_start + len('repeat。'))) is False
    assert _source_check_evidence_in_batch('repeat。', document, batch, ranges,
        (new_start, new_start + len('repeat。'))) is True


@pytest.mark.parametrize('select_old_line', [True, False])
async def test_middle_duplicate_source_failure_uses_exact_selected_line_in_real_review(select_old_line):
    original, document = repeated_middle_fragment_documents('\n')
    groups = _source_audit_groups(document, original)
    first = next(iter(groups))
    service = SourceCheckEvidenceService(first, 'source_scope_grounding',
        'line-2' if select_old_line else 'line-4', recover=False)
    env = {'input_base_document': original}
    before = copy.deepcopy(env)
    if select_old_line:
        with pytest.raises(OperationError, match='无法核验'):
            await review_delivery(service, '保留旧文，只检查实际新增内容', document, contract(), env)
        assert len(service.calls) == 2 and env == before
        assert '失败证据未落在本批实际新增或修改' in service.calls[1]['user_prompt']
    else:
        result = await review_delivery(service, '保留旧文，只检查实际新增内容', document, contract(), env)
        assert len(service.calls) == (len(groups) + 7) // 8
        assert result['status'] == 'revise'
        check = next(item for item in result['checks'] if item['id'] == 'source_scope_grounding')
        assert check['passed'] is False and check['evidence'] == 'repeat。'
        assert any(item['evidence'] == 'repeat。' for item in env['delivery_pending_review']['checks'])


async def test_live_pass_cannot_use_another_batch_to_force_positive_branch():
    class CrossBatch(BatchService):
        async def _stream_complete_agent_output(self, **kwargs):
            async for chunk in super()._stream_complete_agent_output(**kwargs):
                raw = json.loads(chunk)
                raw['status'] = 'checked'
                yield json.dumps(raw, ensure_ascii=False)
    service = CrossBatch()
    with pytest.raises(OperationError, match='无法核验最终交付结果'):
        await review_delivery(service, '生成', long_document(), contract(), {})
    assert len(service.calls) == 2
    assert '来源检查引用不属于当前审计批次' in service.calls[1]['user_prompt']


@pytest.mark.parametrize('state', ['current', 'stale_cycle', 'edited', 'duplicate'])
def test_application_evidence_is_bound_to_current_unique_section(state):
    from creation.delivery_contract import _decision_section_lines
    content = '**已确认依据**\n\n- 用户已选方向。\n\n**落实建议**\n\n### 执行步骤\n\n收集样本，比较结果后选择方案。'
    document = '# 文档\n\n' + content
    env = {'brief_writing': {'source_owned': True, 'cycle': 2,
        'plan': {'sections': [{'id': 's1', 'decision_ids': ['choice']}]},
        'sections': {'s1': {'cycle': 2, 'content': content}}}}
    if state == 'stale_cycle': env['brief_writing']['sections']['s1']['cycle'] = 1
    if state == 'edited': document = document.replace('收集样本', '收集数据')
    if state == 'duplicate': document += '\n\n' + content
    result = _decision_section_lines(env, document)
    if state == 'current':
        assert list(result['choice'].values()) == ['收集样本，比较结果后选择方案。']
        assert all('已选' not in text for text in result['choice'].values())
    else:
        assert result == {}


@pytest.mark.parametrize('failed_field', [None, 'inputs', 'steps', 'output', 'verification'])
async def test_structured_coverage_keeps_each_component_failure(failed_field):
    from creation.delivery_contract import PROPOSAL_FIELDS, _review_owned_proposal
    content = '**已确认依据**\n\n- 改善流程。\n\n**落实建议**\n\n' + '\n\n'.join('### ' + label + '\n\n' + field + '的实际安排。' for field, label in PROPOSAL_FIELDS)
    document = '# 方案\n\n' + content
    env = {'input_context': {'root_request': '提出改进方案', 'brainstorm_decisions': [{'id': 'a', 'source': 'user', 'value': '改善流程'}]},
        'brief_writing': {'source_owned': True, 'output_mode': 'proposal', 'cycle': 0,
            'plan': {'sections': [{'id': 's', 'decision_ids': ['a']}]}, 'sections': {'s': {'cycle': 0, 'content': content}}}}
    raw = {'a': {field: {'reason': '符合本维度' if field != failed_field else '本维度缺少实际内容', 'adequate': field != failed_field} for field, _ in PROPOSAL_FIELDS}}
    service = StreamService([json.dumps(raw)])
    result = await _review_owned_proposal(service, '提出方案', document, contract(), env)
    assert result['status'] == ('revise' if failed_field else 'pass')
    assert result['checks'][0]['passed'] is (failed_field is None)
    assert result['checks'][0]['evidence'] in document
    assert bool(result['corrections']) is bool(failed_field)
    env['brief_writing']['cycle'] = 1
    assert await _review_owned_proposal(service, '提出方案', document, contract(), env) is None


def test_source_snapshot_uses_current_decision_status_without_erasing_reference():
    from creation.delivery_contract import delivery_source_materials, source_audit_catalog
    old = '历史参考：确认过20项。旧摘要遗漏了本轮选项说明。'
    choice = {'id': 'a', 'source': 'user', 'dimension': '对象', 'value': '已选范围', 'description': '已选项的具体属性。'}
    env = {'creation_brief': {'open_flags': ['另一个待答事项']},
        'input_context': {'creation_brief': old, 'brainstorm_decisions': [choice]}}
    before = copy.deepcopy(env)
    provided = delivery_source_materials('按当前回答生成', env)
    current = json.loads(provided['creation_brief'])
    assert current['current_decisions'] == [choice]
    assert current['open_flags'] == ['另一个待答事项']
    assert provided['reference_context'] == old
    assert '20项' not in source_audit_catalog(provided)['creation_brief']['text']
    assert env == before
