import json
import logging

import pytest

from knowledge import extractor_v2
from monitor import llm_tracker

SECRET = 'PRIVATE_BODY https://docs.example.com/private?token=SECRET'


def test_usage_write_failure_does_not_log_private_exception(monkeypatch, caplog):
    def fail_connect(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(llm_tracker.sqlite3, 'connect', fail_connect)
    with caplog.at_level(logging.WARNING):
        result = llm_tracker.log_llm_usage('bake', 'local-model', 10, 5, 20)
    assert result is None
    assert 'LLM_USAGE_WRITE_FAILED' in caplog.text
    for value in ['PRIVATE_BODY', 'docs.example.com', 'SECRET']:
        assert value not in caplog.text


def test_initialization_keeps_identity_in_memory_only(monkeypatch, caplog):
    from unittest.mock import MagicMock
    session = MagicMock()
    session.__enter__.return_value.get.return_value.status_code = 200
    monkeypatch.setattr(extractor_v2.KnowledgeExtractorV2, '_ollama_session', lambda self: session)
    with caplog.at_level(logging.INFO):
        extractor = extractor_v2.KnowledgeExtractorV2(model='local-model', user_identity=SECRET)
    assert extractor.user_identity == SECRET
    assert '用户身份已配置' in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize('failure_stage', ['embedding', 'database'])
def test_similarity_lookup_exception_never_logs_source_text(failure_stage, caplog):
    from unittest.mock import MagicMock
    extractor = extractor_v2.KnowledgeExtractorV2.__new__(extractor_v2.KnowledgeExtractorV2)
    extractor.embedding_model = MagicMock()
    extractor.embedding_model.encode.return_value = [MagicMock(vector=[1.0, 0.0])]
    connection = MagicMock()
    if failure_stage == 'embedding':
        extractor.embedding_model.encode.side_effect = RuntimeError(SECRET)
    else:
        connection.execute.side_effect = RuntimeError(SECRET)
    with caplog.at_level(logging.ERROR):
        result = extractor._find_similar_knowledge(SECRET, connection)
    assert result is None
    assert 'SIMILARITY_LOOKUP_FAILED' in caplog.text
    for text in ['PRIVATE_BODY', 'docs.example.com', 'SECRET']:
        assert text not in caplog.text


@pytest.mark.parametrize('usage', [SECRET, {'prompt_tokens': SECRET, 'completion_tokens': [SECRET]},
                                    {'prompt_tokens': True, 'completion_tokens': -1}])
def test_private_tracker_rejects_usage_metadata(monkeypatch, usage):
    records = []
    monkeypatch.setattr(llm_tracker, 'log_llm_usage', lambda **kw: records.append(kw))
    with llm_tracker.LLMCallTracker('bake', 'local-model', capture_content=False) as tracker:
        tracker.set_response({'usage': usage, 'prompt_eval_count': SECRET,
                              'eval_count': SECRET, 'done_reason': {'secret': SECRET},
                              'message': {'content': SECRET}})
    assert SECRET not in json.dumps(records)
    assert type(records[0]['prompt_tokens']) is int
    assert type(records[0]['completion_tokens']) is int


def test_usage_database_boundary_cannot_store_text_in_numeric_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / 'usage.db')
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE llm_usage_logs(ts INTEGER,caller TEXT,caller_id TEXT,model_name TEXT,'
                     'prompt_tokens INTEGER,completion_tokens INTEGER,total_tokens INTEGER,latency_ms INTEGER,'
                     'status TEXT,error_msg TEXT)')
    llm_tracker.log_llm_usage('bake', 'local-model', SECRET, SECRET, SECRET, db_path=path)
    llm_tracker.log_llm_usage('bake', 'local-model', 2**63-1, 2**63-1, 0, db_path=path)
    with sqlite3.connect(path) as conn:
        rows = conn.execute('SELECT prompt_tokens,completion_tokens,total_tokens,latency_ms FROM llm_usage_logs ORDER BY ts,rowid').fetchall()
    assert rows == [(0, 0, 0, 0), (2**63-1, 2**63-1, 2**63-1, 0)]


@pytest.mark.parametrize('content,done', [
    (json.dumps({'summary': SECRET}), 'stop'),
    (SECRET, 'stop'),
    ('{"summary":"' + SECRET, 'length'),
])
def test_bake_success_invalid_and_truncated_responses_keep_text_only_in_memory(monkeypatch, tmp_path, caplog, content, done):
    records = []
    monkeypatch.setattr(llm_tracker, 'log_llm_usage', lambda **kw: records.append(kw))
    log = tmp_path / 'diagnostics.jsonl'
    monkeypatch.setattr(extractor_v2, 'BAKE_ERROR_LOG_PATH', log)
    extractor = object.__new__(extractor_v2.KnowledgeExtractorV2)
    extractor.model = 'local-model'
    extractor._ollama_chat = lambda **kw: {
        'message': {'content': content}, 'done_reason': done,
        'prompt_eval_count': 25, 'eval_count': 15,
    }
    with caplog.at_level(logging.INFO):
        parsed, meta = extractor._call_bake_llm('bundle:42', SECRET, SECRET, capture_trace=True)
    assert meta['raw_content'] == content  # Retry recovery still receives the original in memory.
    assert meta['raw_preview'] is None and meta['response_preview'] is None
    assert records[0]['raw_preview'] is None and records[0]['response_preview'] is None
    assert records[0]['prompt_tokens'] == 25 and records[0]['completion_tokens'] == 15
    assert records[0]['done_reason'] == done
    persisted = json.dumps(records) + caplog.text + (log.read_text() if log.exists() else '')
    for secret in ['PRIVATE_BODY', 'https://docs.example.com', 'SECRET']:
        assert secret not in persisted
    if done == 'stop' and content.startswith('{'):
        assert parsed['summary'] == SECRET


def test_bake_transport_exception_is_not_copied_to_usage_diagnostics(monkeypatch):
    records = []
    monkeypatch.setattr(llm_tracker, 'log_llm_usage', lambda **kw: records.append(kw))
    extractor = object.__new__(extractor_v2.KnowledgeExtractorV2)
    extractor.model = 'local-model'
    def fail(**kw):
        raise RuntimeError(SECRET)
    extractor._ollama_chat = fail
    with pytest.raises(RuntimeError, match='PRIVATE_BODY'):
        extractor._call_bake_llm('document_summary:42:61', 'system', 'user')
    assert records[0]['status'] == 'failed'
    assert records[0]['error_msg'] == 'INFERENCE_FAILED'
    assert SECRET not in json.dumps(records)


def test_diagnostic_sink_rejects_arbitrary_fields_and_malformed_metadata(monkeypatch, tmp_path):
    log = tmp_path / 'diagnostics.jsonl'
    monkeypatch.setattr(extractor_v2, 'BAKE_ERROR_LOG_PATH', log)
    extractor_v2._append_bake_error_log(SECRET, raw_full=SECRET, caller_id=SECRET,
        done_reason={'secret': SECRET}, raw_len=12, prompt_tokens=SECRET, response_preview=SECRET)
    event = json.loads(log.read_text())
    assert set(event) == {'ts_ms', 'message', 'raw_len'}
    assert event['raw_len'] == 12
    assert SECRET not in log.read_text()


def test_tracker_content_policy_retains_existing_behavior_for_other_callers(monkeypatch):
    records = []
    monkeypatch.setattr(llm_tracker, 'log_llm_usage', lambda **kw: records.append(kw))
    with llm_tracker.LLMCallTracker('other', 'local-model') as tracker:
        tracker.set_trace(raw_preview='existing preview')
    assert records[0]['raw_preview'] == 'existing preview'
    with llm_tracker.LLMCallTracker('bake', 'local-model', capture_content=False) as tracker:
        tracker.set_trace(raw_preview=SECRET, response_preview=SECRET, done_reason={'raw': SECRET})
        tracker.set_error(SECRET)
    assert records[1]['error_msg'] == 'INFERENCE_FAILED'
    assert records[1]['raw_preview'] is None and records[1]['done_reason'] is None


@pytest.mark.parametrize('path,data', [
    ('/bake/extract', {'trigger_reason': SECRET, 'retry_error_code': SECRET,
                       'candidate': {'source_timeline_id': SECRET}}),
    ('/bake/merge_document', {'existing_document': {'title': SECRET},
                             'candidate': {'source_timeline_id': SECRET}}),
])
def test_bake_api_failure_logs_do_not_echo_request_metadata_or_exception(monkeypatch, caplog, path, data):
    import model_api_server
    def fail():
        raise RuntimeError(SECRET)
    monkeypatch.setattr(model_api_server, 'get_bake_extractor', fail)
    with caplog.at_level(logging.INFO):
        response = model_api_server.app.test_client().post(path, json=data)
    assert response.status_code == 500
    assert SECRET not in caplog.text
    assert SECRET not in response.get_data(as_text=True)
    assert 'RuntimeError' in caplog.text


@pytest.mark.parametrize('mode', ['single', 'bundle'])
def test_artifact_rejection_reason_stays_in_business_result_not_diagnostic_log(monkeypatch, caplog, mode):
    extractor = object.__new__(extractor_v2.KnowledgeExtractorV2)
    extractor.model = 'local-model'
    parsed = {'accepted': False, 'reason': SECRET, 'payload': None}
    meta = {'elapsed_ms': 1, 'usage': {}, 'model': 'local-model'}
    monkeypatch.setattr(extractor, '_build_bake_candidate_text', lambda candidate: SECRET)
    monkeypatch.setattr(extractor, '_call_bake_llm', lambda *args, **kwargs: (parsed, meta))
    with caplog.at_level(logging.INFO):
        if mode == 'single':
            result, _ = extractor._extract_bake_artifact({'source_timeline_id': 42}, 'design', 'prompt')
        else:
            result, _ = extractor._normalize_bake_artifact_result(
                {'source_timeline_id': 42}, 'design', parsed, meta, caller_id='bundle:42')
    assert result['reason'] == SECRET
    assert result['accepted'] is False
    assert 'accepted=False' in caplog.text
    assert SECRET not in caplog.text
