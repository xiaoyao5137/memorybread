"""A single bounded adjudication follows two fully valid conflicting reviews."""

import copy
import json

import pytest

from creation.delivery_contract import review_delivery
from creation.operations import OperationError
from tests.test_creation_delivery_contract import contract


class ConflictService:
    def __init__(self, failures=None, resolve=None, mutations=None):
        self.failures = failures or {'source_attributes_grounding': 'line-1'}
        self.resolve = resolve or (lambda payload: resolutions(payload))
        self.mutations = mutations or {}
        self.calls, self.first_group = [], None

    async def _stream_complete_agent_output(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.JSONDecoder().raw_decode(kwargs['user_prompt'])[0]
        if 'conflicts' in payload:
            result = self.resolve(payload)
        else:
            if self.first_group is None:
                self.first_group = next(iter(payload['candidate_source_segments']))
            line = next(key for key, value in payload['candidate_document_lines'].items() if value.strip())
            checks = {key: {'passed': True, 'reason': '按本条件核对', 'evidence': line}
                      for key in payload['required_check_ids']}
            if self.first_group in payload['candidate_source_segments']:
                for key, evidence in self.failures.items():
                    checks[key].update(passed=False, evidence=evidence, reason='怀疑候选增加了原材料没有的限定')
            result = {'status': 'revise' if any(not row['passed'] for row in checks.values()) else 'pass',
                'checks': checks, 'corrections': [], 'source_audit': {
                    key: {'text': parts[0].strip(), 'meaning': '本组原判为有来源支持', 'kind': 'attribute',
                          'basis': 'source_supported', 'source_id': 'user_instruction_and_supplied_facts'}
                    for key, parts in payload['candidate_source_segments'].items()}}
            if len(self.calls) in self.mutations:
                self.mutations[len(self.calls)](result)
        yield result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def resolutions(payload, conclusion='not_applicable'):
    return {'resolutions': {key: {'conclusion': conclusion, 'line_id': target['line_id'],
        'quote': payload['candidate_document_lines'][target['line_id']].strip(),
        'source_ref': '', 'reason': '逐项比较后明确原判不属于本条件范围'}
        for key, target in payload['conflicts'].items()}}


def payloads(service):
    return [json.JSONDecoder().raw_decode(call['user_prompt'])[0] for call in service.calls]


async def test_confirmed_user_experience_keeps_its_original_uncertainty_with_complete_sources():
    experience = '根据过往维护经验，排队耗时可能减少约两成。'
    document = experience + '\n\n' + '\n\n'.join('背景段{}：'.format(i) + '完整上下文须保留。' * 24 for i in range(65)) + '\n末尾材料标记。'
    confirmed = {'id': 'experience-choice', 'source': 'user', 'dimension': '经验判断',
                 'value': '采用原有经验', 'description': experience}
    env = {'input_context': {'root_request': '保持用户原来的不确定程度',
        'creation_brief': '整体简报包括已确认选择和开放事项。',
        'conversation': [{'role': 'user', 'content': '沿用已确认经验，不能声称已完成实测。'}],
        'brainstorm_decisions': [confirmed, {'id': 'unconfirmed', 'source': 'assistant', 'value': '建议'}]}}
    def resolve(payload):
        result = resolutions(payload, 'source_supported')
        row = next(iter(result['resolutions'].values()))
        row.update(source_ref=source_ref(payload, payload['confirmed_user_decisions'][0]['source_id'], experience),
                   reason='候选沿用用户确认的经验判断及“可能”，没有升级为实测事实；原失败是误报。')
        return result
    service = ConflictService(resolve=resolve)
    result = await review_delivery(service, '根据已给资料整理', document, contract(), env)
    prompts = payloads(service)
    narrow = next(prompt for prompt in prompts if 'conflicts' in prompt)
    assert prompts.index(narrow) == 2 and sum('conflicts' in prompt for prompt in prompts) == 1
    assert ''.join(narrow['candidate_document_lines'].values()) == document
    assert 'candidate_document' not in narrow and narrow['provided_materials'] == prompts[0]['provided_materials']
    assert narrow['source_catalog'] == prompts[0]['source_catalog']
    assert all('previous_reason' not in target for target in narrow['conflicts'].values())
    assert [{key: value for key, value in row.items() if key != 'source_id'}
            for row in narrow['confirmed_user_decisions']] == [confirmed]
    assert len(narrow['candidate_source_segments']) == 1
    assert result['status'] == 'pass' and 'delivery_pending_review' not in env


async def test_confirmed_failures_preserve_all_three_independent_claims():
    document = '新增等级身份。\n宣称已实测有效。\n要求额外办理手续。'
    failures = dict(zip(('source_scope_grounding', 'source_attributes_grounding', 'source_obligations_grounding'),
                        ('line-1', 'line-2', 'line-3')))
    service = ConflictService(failures, lambda payload: resolutions(payload, 'unsupported'))
    env = {}
    result = await review_delivery(service, '整理已给资料', document, contract(), env)
    assert len(service.calls) == 3 and result['status'] == 'revise'
    claims = [row for row in env['delivery_pending_review']['checks'] if row.get('anchor_kind') == 'source_claim']
    assert {(row['id'], row['evidence']) for row in claims} == set(zip(failures, document.splitlines()))
    assert all(line in '\n'.join(result['corrections']) for line in document.splitlines())
    assert all(not row['passed'] for row in result['checks'] if row['id'] in failures)


async def test_false_positive_resolutions_do_not_clear_an_unrelated_failed_condition():
    service = ConflictService({'source_attributes_grounding': 'line-1', 'a': 'line-1'})
    result = await review_delivery(service, '整理', '候选正文。', contract(), {})
    by_id = {row['id']: row for row in result['checks']}
    assert result['status'] == 'revise' and by_id['a']['passed'] is False
    assert by_id['source_attributes_grounding']['passed'] is True
    assert set(payloads(service)[2]['conflicts']) == {'source_attributes_grounding'}


@pytest.mark.parametrize('bad', ['wrong_line', 'wrong_quote', 'unknown_source', 'source_quote',
                                 'missing_source', 'bad_authorization', 'missing_key', 'extra_key', 'extra_boolean', 'json'])
async def test_repeated_invalid_adjudication_stops_without_changing_pending_state(bad):
    def resolve(payload):
        result = resolutions(payload)
        rows = result['resolutions']
        row = next(iter(rows.values()))
        if bad == 'uncertain': row['conclusion'] = 'uncertain'
        elif bad == 'wrong_line': row['line_id'] = 'line-1'
        elif bad == 'wrong_quote': row['quote'] = '不存在的候选原文'
        elif bad == 'unknown_source': row.update(source_ref='unknown')
        elif bad == 'source_quote': row.update(source_ref='creation_brief')
        elif bad == 'missing_source': row['conclusion'] = 'source_supported'
        elif bad == 'bad_authorization': row.update(conclusion='authorized_creation', source_ref=source_ref(payload, 'creation_brief', '简报材料'))
        elif bad == 'missing_key': rows.clear()
        elif bad == 'extra_key': rows['unknown'] = copy.deepcopy(row)
        elif bad == 'extra_boolean': row['passed'] = True
        else: return '{"resolutions":'
        return result
    repeated = '相同的候选句。'
    document = repeated + '\n新增间隔。\n' + repeated
    env = {'input_base_document': repeated + '\n', 'input_context': {'creation_brief': '简报材料'},
           'delivery_pending_review': {'scope': 'old', 'checks': [], 'corrections': ['保留旧问题']}}
    before = copy.deepcopy(env)
    service = ConflictService({'source_attributes_grounding': 'line-3'}, resolve)
    with pytest.raises(OperationError) as exc:
        await review_delivery(service, '整理', document, contract(), env)
    assert exc.value.code == 'CREATION_DELIVERY_UNVERIFIED' and len(service.calls) == 4 and env == before


@pytest.mark.parametrize('mutated_attempt', [1, 2])
@pytest.mark.parametrize('malformed', ['missing_audit', 'status_disagrees'])
async def test_format_repair_does_not_consume_bound_conflict_retry(mutated_attempt, malformed):
    def mutate(raw):
        if malformed == 'missing_audit': raw.pop('source_audit')
        else: raw['status'] = 'pass'
    service = ConflictService(mutations={mutated_attempt: mutate})
    report = await review_delivery(service, '整理', '候选原文。', contract(), {})
    assert report['status'] == 'pass'
    assert len(service.calls) == 4
    assert sum('conflicts' in prompt for prompt in payloads(service)) == 1


async def test_repeated_invalid_reviews_still_stop_without_adjudication():
    def mutate(raw):
        raw.pop('source_audit')
    service = ConflictService(mutations={1: mutate, 2: mutate})
    with pytest.raises(OperationError):
        await review_delivery(service, '整理', '候选原文。', contract(), {})
    assert len(service.calls) == 2
    assert all('conflicts' not in prompt for prompt in payloads(service))


async def test_format_repair_then_conflicts_preserve_confirmed_failure():
    service = ConflictService(resolve=lambda payload: resolutions(payload, 'unsupported'),
                              mutations={1: lambda raw: raw.pop('source_audit')})
    report = await review_delivery(service, '整理', '新增未授权义务。', contract(), {})
    assert report['status'] == 'revise'
    assert len(service.calls) == 4
    assert '新增未授权义务。' in '\n'.join(report['corrections'])


async def test_same_error_words_without_the_typed_conflict_do_not_trigger_adjudication(monkeypatch):
    def invalid(*args, **kwargs):
        raise OperationError('CREATION_DELIVERY_UNVERIFIED', '来源审计没有同类负面片段')
    monkeypatch.setattr('creation.delivery_contract._apply_source_audit', invalid)
    service = ConflictService()
    with pytest.raises(OperationError):
        await review_delivery(service, '整理', '候选原文。', contract(), {})
    assert len(service.calls) == 2


async def test_conflict_ids_respect_user_condition_name_collisions():
    conditions = contract()
    conditions['acceptance'].append({'id': 'source_scope_grounding', 'criterion': '用户自定义条件'})
    service = ConflictService({'source_scope_grounding_': 'line-1'})
    result = await review_delivery(service, '整理', '候选原文。', conditions, {})
    assert set(payloads(service)[2]['conflicts']) == {'source_scope_grounding_'}
    assert result['status'] == 'pass' and len({row['id'] for row in result['checks']}) == len(result['checks'])


async def test_adjudication_recovers_bold_and_han_number_spacing_to_original_quotes():
    document = '文档断言**服装**与**美妆**的表现显著更高。'
    experience = '基于过往经验，成功率可能超80%。'
    decision = {'id': 'experience', 'source': 'user', 'value': '保留经验', 'description': experience}
    def resolve(payload):
        result = resolutions(payload, 'unsupported')
        next(iter(result['resolutions'].values())).update(quote=document.replace('**', ''),
            source_ref=source_ref(payload, payload['confirmed_user_decisions'][0]['source_id'], experience), reason='用户只提供带有不确定性的经验，候选增加比较性结论。')
        return result
    service = ConflictService(resolve=resolve)
    env = {'input_context': {'brainstorm_decisions': [decision]}}
    result = await review_delivery(service, '保留原经验强度', document, contract(), env)
    assert result['status'] == 'revise' and len(service.calls) == 3
    claims = [row for row in env['delivery_pending_review']['checks'] if row.get('anchor_kind') == 'source_claim']
    assert claims[0]['evidence'] == document
    assert document in '\n'.join(result['corrections'])


@pytest.mark.parametrize('candidate,quote', [
    ('AI读书会与AI读书会', 'AI读书会'),
    ('AI读书会与AI读书会', 'AI 读书会'),
    ('**AI读书会**与**AI读书会**', 'AI 读书会'),
    ('文档使用**80%**指标。', '文档使用81%指标。'),
])
async def test_candidate_typography_cannot_resolve_ambiguity_or_change_factual_values(candidate, quote):
    def resolve(payload):
        result = resolutions(payload, 'unsupported')
        next(iter(result['resolutions'].values()))['quote'] = quote
        return result
    service = ConflictService(resolve=resolve)
    env = {'delivery_pending_review': {'scope': 'old', 'checks': [], 'corrections': ['原有问题']}}
    before = copy.deepcopy(env)
    with pytest.raises(OperationError):
        await review_delivery(service, '整理', candidate, contract(), env)
    assert env == before and len(service.calls) == 4


def source_ref(payload, source_id, text):
    return next(key for key, ref in payload['source_references'].items()
                if ref['source_id'] == source_id and ref['text'] == text)


def test_source_reference_catalog_preserves_repetition_lines_fields_and_long_sources():
    from creation.delivery_contract import _conflict_source_references
    parts = {'source': ['重复。\n重复。', '重复。', '甲' * 1100 + '\r\n尾行。']}
    refs = _conflict_source_references(parts)
    assert len(refs) == 7
    for ref in refs.values():
        assert ref['text'] == parts[ref['source_id']][ref['part_index']][ref['start']:ref['end']]
        assert 0 < len(ref['text']) <= 480 and '\n' not in ref['text'] and '\r' not in ref['text']
    assert len([ref for ref in refs.values() if ref['text'] == '重复。']) == 3
    assert ''.join(ref['text'] for ref in refs.values() if ref['part_index'] == 2) == '甲' * 1100 + '尾行。'


async def test_repeated_confirmed_source_is_bound_by_position_and_schema_uses_only_reference_ids():
    def resolve(payload):
        result = resolutions(payload, 'source_supported')
        ref_id = next(key for key, ref in payload['source_references'].items()
            if ref['source_id'] == 'confirmed-selection:experience' and ref['part_index'] == 2)
        next(iter(result['resolutions'].values()))['source_ref'] = ref_id
        return result
    service = ConflictService(resolve=resolve)
    env = {'input_context': {'brainstorm_decisions': [
        {'id': 'experience', 'source': 'user', 'dimension': '经验', 'value': '经验可能超80%。', 'description': '经验可能超80%。'}]}}
    result = await review_delivery(service, '整理', '经验可能超80%。', contract(), env)
    assert result['status'] == 'pass'
    call = service.calls[2]
    properties = call['json_schema']['properties']['resolutions']['properties']['source_attributes_grounding']['properties']
    assert list(properties) == ['line_id', 'quote', 'source_ref', 'reason', 'conclusion']
    assert 'source_id' not in properties and 'source_quote' not in properties
    assert properties['source_ref']['enum'] == [''] + list(payloads(service)[2]['source_references'])


async def test_previous_unverified_explanation_is_not_sent_to_the_narrow_reviewer():
    def mutate(raw):
        for row in raw['checks'].values():
            if not row['passed']:
                row['reason'] = '旧判词唯一标记：错误地断言这个经验不是用户确认。'
    service = ConflictService(mutations={1: mutate, 2: mutate})
    await review_delivery(service, '整理', '候选原文。', contract(), {})
    narrow = service.calls[2]
    assert '旧判词唯一标记' not in narrow['user_prompt']
    assert 'previous_reason' not in narrow['user_prompt']


async def test_middle_repeated_changed_fragment_remains_bound_through_conflict_resolution():
    from creation.delivery_contract import _source_audit_groups
    from tests.test_creation_review_prompt_integration import repeated_middle_fragment_documents

    original, document = repeated_middle_fragment_documents('\n')
    groups = _source_audit_groups(document, original)
    service = ConflictService(failures={'source_attributes_grounding': 'line-4'})
    env = {'input_base_document': original}
    result = await review_delivery(service, '保留旧文，只检查实际新增内容', document, contract(), env)
    prompts = payloads(service)
    focused = [payload for payload in prompts if 'conflicts' in payload]
    assert len(focused) == 1
    assert focused[0]['conflicts']['source_attributes_grounding']['line_id'] == 'line-4'
    assert focused[0]['candidate_source_segments'][next(iter(groups))][1] == 'repeat。\n'
    assert len(service.calls) == (len(groups) + 7) // 8 + 2
    assert result['status'] == 'pass'
    assert 'delivery_pending_review' not in env


async def test_adjudication_quote_choices_bind_the_fixed_line_not_its_neighbor():
    document = '按类目统计可用率。\n低于阈值时执行已确认的重生成流程。'
    service = ConflictService({'source_obligations_grounding': 'line-2'})
    await review_delivery(service, '按已确认策略整理', document, contract(), {})
    row = service.calls[2]['json_schema']['properties']['resolutions']['properties']['source_obligations_grounding']['properties']
    assert row['line_id'] == {'const': 'line-2'}
    assert row['quote']['enum'] == [document.splitlines()[1]]
    assert document.splitlines()[0] not in row['quote']['enum']


async def test_bound_reference_does_not_support_added_calendar_scope():
    def resolve(payload):
        result = resolutions(payload, 'source_supported')
        next(iter(result['resolutions'].values()))['source_ref'] = source_ref(
            payload, 'creation_brief', '周三开会。')
        return result
    service = ConflictService(resolve=resolve)
    with pytest.raises(OperationError) as exc:
        await review_delivery(service, '本周三开会。', '本周三开会。', contract(),
                              {'input_context': {'creation_brief': '周三开会。'}})
    assert exc.value.code == 'CREATION_DELIVERY_UNVERIFIED'


async def test_legacy_free_source_quote_cannot_bypass_reference_contract():
    def resolve(payload):
        result = resolutions(payload, 'source_supported')
        row = next(iter(result['resolutions'].values()))
        row.pop('source_ref')
        row.update(source_id='user_instruction_and_supplied_facts', source_quote='整理')
        return result
    with pytest.raises(OperationError):
        await review_delivery(ConflictService(resolve=resolve), '整理', '候选原文。', contract(), {})


async def test_uncertain_is_a_bound_revision_without_certifying_or_erasing_user_facts():
    service = ConflictService(resolve=lambda payload: resolutions(payload, 'uncertain'))
    env = {}
    report = await review_delivery(service, '保持用户已确认事实', '候选原文。', contract(), env)
    assert report['status'] == 'revise' and len(service.calls) == 3
    assert not next(row for row in report['checks'] if row['id'] == 'source_attributes_grounding')['passed']
    assert any('不能把复核不确定当作用户事实错误' in text for text in report['corrections'])
    assert not any(text.startswith('删除或中性改写缺乏来源依据') for text in report['corrections'])
    assert env['delivery_pending_review']['checks']


async def test_long_explanation_is_preserved_not_misclassified_as_invalid_evidence():
    reason = '依据用户明确提供的事实逐项核对。' * 30
    def resolve(payload):
        result = resolutions(payload)
        next(iter(result['resolutions'].values()))['reason'] = reason
        return result
    service = ConflictService(resolve=resolve)
    report = await review_delivery(service, '整理', '候选原文。', contract(), {})
    assert report['status'] == 'pass' and len(service.calls) == 3
    assert next(row for row in report['checks'] if row['id'] == 'source_attributes_grounding')['reason'] == reason


@pytest.mark.parametrize('invalid', ['json', 'source_ref', 'missing_field'])
async def test_narrow_format_repair_recovers_once_without_rewriting_document(invalid):
    attempts = []
    def resolve(payload):
        attempts.append(payload)
        result = resolutions(payload, 'unsupported')
        if len(attempts) == 1:
            if invalid == 'json': return '{"resolutions":'
            row = next(iter(result['resolutions'].values()))
            if invalid == 'source_ref': row['source_ref'] = 'unknown'
            else: row.pop('reason')
        return result
    service = ConflictService(resolve=resolve)
    report = await review_delivery(service, '整理', '候选原文。', contract(), {})
    assert report['status'] == 'revise' and len(service.calls) == 4
    assert attempts[0] == attempts[1]
    assert '上次冲突复核格式或证据绑定无效' in service.calls[-1]['user_prompt']


async def test_bound_uncertainty_schedules_agent_repair_instead_of_protocol_failure():
    from creation.agent_loop import CreationAgentLoop
    from creation.service import CreationOptions
    from tests.test_creation_agent_loop import FakeCreationService

    class Service(FakeCreationService):
        async def review_creation_delivery(self, instruction, document, conditions, environment):
            return await review_delivery(ConflictService(resolve=lambda payload: resolutions(payload, 'uncertain')),
                                         instruction, document, conditions, environment)

    loop = CreationAgentLoop(Service())
    state = loop._new_state(user_message='保留来源限定', root_request='创建方案', current_document='候选原文。',
        conversation=[], selected_skills=[], options=CreationOptions(enabled_tools=()), model_mode='local',
        session_id='uncertain-repair', run_id='uncertain-repair')
    state.environment.update(input_contract=contract(), operation={'kind': 'generate'}, document=state.current_document)
    events = [event async for event in loop._execute_step(state,
        {'kind': 'agent', 'id': 'delivery_check', 'name': '验收', 'action': 'delivery_check'},
        creation_model=None, creation_api_key=None, creation_base_url=None)]
    assert any(event['type'] == 'delivery.checked' for event in events)
    assert state.environment['delivery_review']['status'] == 'revise'
    assert state.environment['delivery_repair_count'] == 1
    assert state.plan[state.cursor]['delivery_repair'] is True
    assert state.plan[state.cursor + 1]['action'] == 'delivery_check'


@pytest.mark.parametrize('label,expected', [('候选原文存在无依据断言', 'revise'), ('不属于本检查范围', 'pass'), ('无法判断', 'revise')])
async def test_explicit_candidate_conclusion_labels_keep_failure_direction(label, expected):
    service = ConflictService(resolve=lambda payload: resolutions(payload, label))
    result = await review_delivery(service, '按原材料写', '原材料内容。', contract(), {})
    assert result['status'] == expected
    schema = service.calls[2]['json_schema']['properties']['resolutions']['properties']['source_attributes_grounding']
    assert label in schema['properties']['conclusion']['enum']


@pytest.mark.parametrize('passed,expected', [(True, 'pass'), (False, 'revise')])
def test_internal_checked_status_is_derived_without_overriding_individual_failure(passed, expected):
    from creation.delivery_contract import _decode_review_checks
    result = {'status': 'checked', 'checks': {'a': {'passed': passed, 'reason': '逐项判断', 'evidence': 'line-1'}}}
    _decode_review_checks(result, ['a'])
    assert result['status'] == expected
    assert result['checks'][0]['passed'] is passed
