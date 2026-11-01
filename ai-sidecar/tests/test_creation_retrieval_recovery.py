"""Regression for public events with existing local materials."""
import copy
import pytest

from creation.agent_loop import CreationAgentLoop
from creation.operations import OperationError
from creation.retrieval_evidence import bind_local_evidence_leads
from creation.service import CreationOptions, CreationService, WebSearchResult
from tests.test_creation_delivery_contract import DeliveryService, contract, execute

REQUEST = '生成下本次qcon的事项总结'
QUERY = '检索本次 QCon 会议的具体议程、演讲嘉宾、讨论议题及已确认的事项'
REQUIREMENT = {
    'topic': REQUEST, 'keywords': ['qcon', '事项总结'],
    'entity_context': {'has_high_confidence_entity': True,
                       'primary_entities': [{'name': 'qcon', 'mention_count': 10}]},
}


def test_local_lead_adds_search_without_claiming_facts():
    original = contract(['public_facts'])
    result = bind_local_evidence_leads(original, REQUIREMENT, REQUEST, allowed=True, operation='generate')
    assert [row['source'] for row in result['inputs']] == ['work_context', 'public_facts']
    assert result['inputs'][0]['query'] == REQUEST
    assert result['inputs'][0]['evidence'] == ''
    assert len(original['inputs']) == 1
    assert bind_local_evidence_leads(result, REQUIREMENT, REQUEST, allowed=True, operation='generate') == result


@pytest.mark.parametrize('sources,requirement,instruction,allowed,operation', [
    ([], REQUIREMENT, REQUEST, True, 'generate'),
    (['public_facts'], {}, REQUEST, True, 'generate'),
    (['public_facts'], REQUIREMENT, '总结其他会议', True, 'generate'),
    (['public_facts'], REQUIREMENT, REQUEST, False, 'generate'),
    (['public_facts'], REQUIREMENT, REQUEST, True, 'transform'),
    (['public_facts'], REQUIREMENT, REQUEST + '，不要搜索本地记忆', True, 'generate'),
    (['public_facts'], REQUIREMENT, REQUEST + '，只使用官网信息', True, 'generate'),
    (['work_context', 'public_facts'], REQUIREMENT, REQUEST, True, 'generate'),
])
def test_local_lead_respects_scope(sources, requirement, instruction, allowed, operation):
    original = contract(sources)
    assert bind_local_evidence_leads(original, requirement, instruction, allowed=allowed, operation=operation) == original


def test_actual_step_query_wins_over_root_topic():
    service = CreationService.__new__(CreationService)
    assert service._build_search_queries(QUERY, REQUIREMENT) == [QUERY, 'qcon']
    assert service._build_search_queries('另一个项目 官方发布记录', REQUIREMENT) == ['另一个项目 官方发布记录']


@pytest.mark.asyncio
async def test_irrelevant_nonempty_web_response_retries_focused_query(monkeypatch):
    service = CreationService.__new__(CreationService)
    monkeypatch.setattr(service, '_web_search_engines', lambda: ['bing'])
    calls = []
    async def search(query):
        calls.append(query)
        if len(calls) == 1:
            return [WebSearchResult(title='生成词义', snippet='generate', url='https://example.org/?q=qcon')]
        return [WebSearchResult(title='QCon 会议议程', snippet='技术议题', url='https://example.org/agenda')]
    monkeypatch.setattr(service, '_search_bing', search)
    result = await service.collect_web_context(QUERY, REQUIREMENT)
    assert calls == [QUERY, 'qcon']
    assert [row.title for row in result] == ['QCon 会议议程']


@pytest.mark.asyncio
async def test_irrelevant_primary_engine_tries_next_engine(monkeypatch):
    service = CreationService.__new__(CreationService)
    monkeypatch.setattr(service, '_web_search_engines', lambda: ['bing', 'duckduckgo'])
    calls = []
    async def bing(query):
        calls.append('bing')
        return [WebSearchResult(title='即梦 AI', snippet='生成工具', url='https://example.org')]
    async def duck(query):
        calls.append('duckduckgo')
        return [WebSearchResult(title='QCon 议程', snippet='议题', url='https://example.org/agenda')]
    monkeypatch.setattr(service, '_search_bing', bing)
    monkeypatch.setattr(service, '_search_duckduckgo', duck)
    assert len(await service.collect_web_context(QUERY, REQUIREMENT)) == 1
    assert calls == ['bing', 'duckduckgo']


class EventService(DeliveryService):
    def __init__(self, relevant_web=True):
        super().__init__(['public_facts'])
        self.contract['inputs'][0]['query'] = QUERY
        self.relevant_web = relevant_web
        self.web_queries = []

    def analyze_requirement(self, *args, **kwargs):
        return {**super().analyze_requirement(*args, **kwargs), **copy.deepcopy(REQUIREMENT)}

    async def collect_web_context(self, query, requirement):
        self.web_queries.append(query)
        if self.relevant_web:
            return [WebSearchResult(title='QCon 议程', snippet='议题', url='https://example.org/agenda')]
        return []


@pytest.mark.asyncio
async def test_full_loop_recalls_local_before_public_search():
    service = EventService()
    events = await execute(service, instruction=REQUEST, enabled_tools=['memory_search', 'internet_search'])
    assert events[-1]['type'] == 'run.completed'
    assert service.reference_queries == [REQUEST]
    assert service.web_queries == [QUERY]
    tools = [e['actor']['id'] for e in events if e['type'] == 'tool.completed']
    assert tools.index('memory_search') < tools.index('internet_search')
    assert service.writes == 1


@pytest.mark.asyncio
async def test_exhausted_search_stops_before_writing_missing_material_document():
    service = EventService(relevant_web=False)
    with pytest.raises(OperationError) as error:
        await execute(service, instruction=REQUEST, enabled_tools=['memory_search', 'internet_search'])
    assert error.value.code == 'CREATION_EVIDENCE_UNAVAILABLE'
    assert error.value.retryable
    assert service.reference_queries == [REQUEST]
    assert service.web_queries == [QUERY]
    assert service.writes == 0


@pytest.mark.asyncio
async def test_web_gap_does_not_discard_already_recalled_local_sources():
    service = EventService(relevant_web=False)
    loop = CreationAgentLoop(service)
    state = loop._new_state(user_message=REQUEST, root_request=None, current_document='',
                            conversation=[], selected_skills=[], options=CreationOptions(),
                            model_mode='local', session_id=None, run_id=None)
    state.environment['requirement'] = copy.deepcopy(REQUIREMENT)
    state.environment['references'] = [{'title': 'QCon 演讲记录', 'full_content': '已核对的本地材料'}]
    step = {'id': 'internet_search', 'name': '互联网检索', 'kind': 'tool',
            'action': 'internet_search', 'input_query': QUERY}
    events = [e async for e in loop._execute_step(state, step, creation_model=None,
                                                 creation_api_key=None, creation_base_url=None)]
    assert state.environment['references'][0]['title'] == 'QCon 演讲记录'
    assert events[-1]['data']['evidence_status'] == 'empty'
    assert events[-1]['data']['local_reference_count'] == 1


@pytest.mark.asyncio
async def test_irrelevant_web_results_exhaust_bounded_retries(monkeypatch):
    service = CreationService.__new__(CreationService)
    monkeypatch.setattr(service, '_web_search_engines', lambda: ['bing', 'duckduckgo'])
    calls = []
    async def search(query):
        calls.append(query)
        return [WebSearchResult(title='生成词义', snippet='generate', url='https://example.org')]
    monkeypatch.setattr(service, '_search_bing', search)
    monkeypatch.setattr(service, '_search_duckduckgo', search)
    assert await service.collect_web_context(QUERY, REQUIREMENT) == []
    assert calls == [QUERY, QUERY, 'qcon', 'qcon']
