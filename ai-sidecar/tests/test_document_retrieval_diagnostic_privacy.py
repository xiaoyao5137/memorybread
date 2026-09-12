import logging
import urllib.request
from rag.retriever import VectorRetriever


def test_internal_vector_http_failure_logs_no_private_payload(monkeypatch, caplog):
    secret = 'PRIVATE_BODY https://docs.example.com/private?token=SECRET'
    def fail(*args, **kwargs):
        raise RuntimeError(secret)
    monkeypatch.setattr(urllib.request, 'urlopen', fail)
    retriever = VectorRetriever.__new__(VectorRetriever)
    with caplog.at_level(logging.WARNING):
        result = retriever._try_internal_http([1.0, 0.0], 10, 0.5, None)
    assert result is None
    assert 'VECTOR_SEARCH_HTTP_FAILED' in caplog.text
    for text in ['PRIVATE_BODY', 'docs.example.com', 'SECRET']:
        assert text not in caplog.text


def test_selected_and_rejected_context_metadata_does_not_leak(caplog):
    from rag.pipeline import RagPipeline
    from rag.retriever import RetrievedChunk
    secret = 'PRIVATE_BODY https://docs.example.com/private?token=SECRET'
    valid = RetrievedChunk(capture_id=1, text='缓存更新前检查版本，避免旧正文覆盖新修订。', score=1.0,
        source='document', doc_key='document:1', metadata={'source_type':'document','activity_type':secret})
    invalid = RetrievedChunk(capture_id=2, text='other', score=0.5,
        source='vector', doc_key='other:2', metadata={'source_type':secret})
    with caplog.at_level(logging.INFO):
        selected = RagPipeline._select_contexts([valid, invalid], top_k=2)
    assert selected == [valid]
    for text in ['PRIVATE_BODY', 'docs.example.com', 'SECRET']:
        assert text not in caplog.text


def test_creation_web_failure_keeps_fallback_without_logging_private_query(monkeypatch, caplog):
    import asyncio
    from creation.service import CreationService
    secret = 'PRIVATE_BODY https://docs.example.com/private?token=SECRET'
    service = CreationService.__new__(CreationService)
    monkeypatch.setattr(service, '_web_search_engines', lambda: ['bing', 'duckduckgo'])
    monkeypatch.setattr(service, '_build_search_queries', lambda *args: [secret])
    attempts = []
    async def fail(query):
        attempts.append(query)
        raise RuntimeError(secret)
    monkeypatch.setattr(service, '_search_bing', fail)
    monkeypatch.setattr(service, '_search_duckduckgo', fail)
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(service.collect_web_context(secret, {}))
    assert result == [] and attempts == [secret, secret]
    assert 'WEB_SEARCH_FAILED' in caplog.text
    for text in ['PRIVATE_BODY', 'docs.example.com', 'SECRET']:
        assert text not in caplog.text
