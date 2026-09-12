import json
import pytest

import model_api_server
from model_api_server import (
    _analyze_floating_assist_intent,
    _build_floating_assist_rag_query,
    _build_floating_assist_rag_query_from_intent,
    _extract_floating_assist_question,
)
from rag.pipeline import _extract_core_retrieval_query
from rag.pipeline import RagResult
from rag.retriever import RetrievedChunk


class FakeIntentLlm:
    model_name = "fake-intent"

    def __init__(self, response: str):
        self.response = response
        self.last_prompt = ""
        self.last_system = ""
        self.last_kwargs = {}

    def is_available(self):
        return True

    def complete(self, prompt: str, system: str = "", **kwargs):
        from rag.llm.base import LlmResponse

        self.last_prompt = prompt
        self.last_system = system
        self.last_kwargs = kwargs
        return LlmResponse(text=self.response, model=self.model_name, tokens=12)


@pytest.mark.parametrize('question,material,expected', [
    ('灵机商家招商的文档', False, True),
    ('灵机商家招商的文档', True, True),
    ('SMACT的文档', True, True),
    ('帮我查找这个截图对应的文档', True, True),
    ('结合之前的方案分析这张图片', True, True),
    ('总结附件并提供相关资料', True, True),
    ('比较这份方案和其他方案', True, True),
    ('总结其他文档', True, True),
    ('解释这张截图', True, True),
    ('他表达了什么？', True, True),
    ('翻译附件', True, True),
    ('总结这份文档', False, True),
    ('不要检索历史，只总结附件', True, False),
    ('find the merchant onboarding document', True, True),
    ('summarize this attachment', True, True),
])
@pytest.mark.parametrize('model_response', [None, 'not json', '{"core_question":"误判", "needs_rag":false}',
                                           '{"core_question":"误判", "needs_rag":"false"}'])
def test_retrieval_policy_overrides_model_and_fallback(question, material, expected, model_response):
    metadata = {'source': 'floating_assist', 'manual_instruction': question}
    if material:
        metadata.update(ocr_text='屏幕内容', attachments=[{'name': 'screen.png'}])
    llm = FakeIntentLlm(model_response) if model_response else None
    intent = _analyze_floating_assist_intent(question, metadata, llm)
    assert intent.needs_rag is expected
    assert intent.retrieval_reason


@pytest.mark.parametrize('endpoint', ['/query', '/query/stream'])
def test_document_lookup_enters_retrieval_despite_false_model_intent(monkeypatch, endpoint):
    calls = []
    class Pipeline:
        def query(self, query, **kwargs):
            calls.append(kwargs)
            if kwargs.get('references_only') or endpoint == '/query':
                assert 'supplied_contexts' not in kwargs
            return RagResult(answer='检索已执行', contexts=[], model='local')
        def _build_context(self, contexts):
            return ''
    class Queue:
        def submit(self, priority, func, **kwargs):
            import concurrent.futures
            future = concurrent.futures.Future()
            try:
                future.set_result(self.submit_sync(priority, func, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            return future

        def submit_sync(self, priority, func, **kwargs):
            return func()
    monkeypatch.setattr(model_api_server, '_rag_pipeline', Pipeline())
    monkeypatch.setattr(model_api_server, 'get_global_queue', lambda: Queue())
    monkeypatch.setattr(model_api_server, '_build_rag_llm_override', lambda *a, **k:
                        FakeIntentLlm('{"core_question":"灵机商家招商的文档","needs_rag":false}'))
    monkeypatch.setattr(model_api_server, '_save_rag_session', lambda *a, **k: 1)
    monkeypatch.setattr(model_api_server, 'log_llm_usage', lambda *a, **k: None)
    monkeypatch.setattr('model_registry_global.check_memory_pressure', lambda: 'normal')
    response = model_api_server.app.test_client().post(endpoint, json={
        'query': '灵机商家招商的文档', 'manual_instruction': '灵机商家招商的文档',
        'source': 'floating_assist', 'ocr_text': '无关页面菜单',
    }, buffered=True)
    assert response.status_code == 200
    assert 'RAG_STREAM_FAILED' not in response.get_data(as_text=True)
    assert len(calls) == (2 if endpoint.endswith('stream') else 1)


def test_floating_assist_question_ignores_bare_url_with_query_string():
    ocr_text = "\n".join(
        [
            "docs.example.com/k/home/page?ro=false#section=h.s1",
            "Loop Engineering 怎么落地到 Top5 任务？",
        ]
    )

    assert _extract_floating_assist_question(ocr_text) == "Loop Engineering 怎么落地到 Top5 任务？"


def test_floating_assist_rag_query_does_not_use_url_as_core_question():
    raw_query = "你是记忆面包的工作场景助手。\n当前屏幕 OCR：\ndocs.example.com/k/home/page?ro=false#section=h.s1"
    metadata = {"source": "floating_assist"}

    assert _build_floating_assist_rag_query(raw_query, metadata) == raw_query


def test_manual_floating_assist_retrieval_uses_only_manual_instruction():
    raw_query = "\n".join(
        [
            "你是记忆面包的工作场景助手。用户在悬浮咨询面板中手工输入了一条指令。",
            "请优先回答这条手工指令；如果同时提供了当前屏幕 OCR 内容，请把它作为辅助上下文。",
            "不要提及供应商模型、密钥、成本或内部实现。",
            "",
            "用户手工指令：",
            "smact文档",
            "",
            "当前屏幕 OCR：",
            "记忆面包悬浮咨询面板",
        ]
    )
    query_with_attachment = "\n".join(
        [
            "用户手工指令:",
            "分析 SMACT 与 SMOCC",
            "",
            "用户随本次请求附加了以下文件。请结合附件信息回答；如果当前模型无法直接读取图片内容，请明确基于用户指令和可见上下文给出结果，不要声称已经看到了图片细节。",
            "1. gpu-metrics.pdf（application/pdf，12 KB）",
        ]
    )

    assert _extract_core_retrieval_query(raw_query) == "smact文档"
    assert _extract_core_retrieval_query(query_with_attachment) == "分析 SMACT 与 SMOCC"


def test_floating_assist_model_intent_understands_ocr_before_rag_query():
    llm = FakeIntentLlm(
        """
        {
          "core_question": "Loop Engineering 怎么落地到 Top5 任务？",
          "retrieval_query": "Loop Engineering Top5 任务 自动化闭环 Token 预算",
          "screen_context_summary": "屏幕展示的是关于从人 Prompt Agent 升级到自动化 Loop 的执行建议。",
          "answer_requirements": ["给出落地路径", "覆盖 Token 预算", "不要反问"],
          "needs_rag": true,
          "confidence": 0.86
        }
        """
    )
    raw_query = "你是记忆面包的工作场景助手。\n当前屏幕 OCR：\ndocs.example.com/k/home/page?ro=false\nLoop Engineering 怎么落地？"

    intent = _analyze_floating_assist_intent(raw_query, {"source": "floating_assist"}, llm)
    rag_query = _build_floating_assist_rag_query_from_intent(raw_query, intent)

    assert intent.source == "model"
    assert intent.confidence == 0.86
    assert llm.last_kwargs["num_predict"] == 384
    assert "屏幕 OCR" in llm.last_prompt
    assert "核心问题：Loop Engineering 怎么落地到 Top5 任务？" in rag_query
    assert "检索问题：Loop Engineering Top5 任务 自动化闭环 Token 预算" in rag_query
    assert "屏幕理解：屏幕展示的是关于从人 Prompt Agent 升级到自动化 Loop 的执行建议。" in rag_query
    assert _extract_core_retrieval_query(rag_query) == "Loop Engineering Top5 任务 自动化闭环 Token 预算"


def test_rag_stream_sends_references_before_answer_and_finishes_with_elapsed(monkeypatch):
    chunk = RetrievedChunk(
        capture_id=1,
        doc_key="document:1",
        text="提前召回资料",
        score=0.9,
        source="document",
        metadata={"source_type": "document", "title": "资料一"},
    )

    calls: list[str] = []

    class FakePipeline:
        def query(
            self,
            query,
            top_k=None,
            llm=None,
            references_only=False,
            supplied_contexts=None,
            on_contexts=None,
            on_delta=None,
        ):
            if references_only:
                calls.append("retrieve")
                return RagResult(answer="", contexts=[chunk], model="references-only")
            calls.append("generate")
            on_contexts([chunk])
            on_delta("部分")
            on_delta("答案")
            return RagResult(answer="部分答案", contexts=[chunk], cited_contexts=[chunk], model="internal-model")

        def _build_context(self, contexts):
            return "context"

    class InlineQueue:
        def submit(self, priority, func, **kwargs):
            import concurrent.futures
            future = concurrent.futures.Future()
            try:
                future.set_result(self.submit_sync(priority, func, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            return future

        def submit_sync(self, priority, func, timeout=None, lane=None):
            calls.append("queue")
            assert calls == ["queue"]
            return func()

    monkeypatch.setattr(model_api_server, "_rag_pipeline", FakePipeline())
    monkeypatch.setattr(model_api_server, "get_global_queue", lambda: InlineQueue())
    monkeypatch.setattr(model_api_server, "_build_rag_llm_override", lambda *args, **kwargs: None)
    monkeypatch.setattr(model_api_server, "_save_rag_session", lambda *args, **kwargs: 1)
    monkeypatch.setattr(model_api_server, "log_llm_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("model_registry_global.check_memory_pressure", lambda: "normal")

    response = model_api_server.app.test_client().post(
        "/query/stream",
        json={"query": "测试问题", "top_k": 5, "source": "monitor"},
        buffered=True,
    )
    events = [
        json.loads(line[6:])
        for line in response.get_data(as_text=True).splitlines()
        if line.startswith("data: ")
    ]
    types = [event["type"] for event in events]

    assert response.status_code == 200
    assert types.index("references") < types.index("delta")
    statuses = [event["stage"] for event in events if event["type"] == "status"]
    assert statuses == ["queued", "retrieving", "waiting_generation", "answering"]
    assert calls == ["queue", "retrieve", "generate"]
    assert [event["text"] for event in events if event["type"] == "delta"] == ["部分", "答案"]
    done = next(event for event in events if event["type"] == "done")
    assert done["answer"] == "部分答案"
    assert done["model"] == "mbem-v1-local"
    assert done["elapsed_ms"] >= 0
    assert done["inference_elapsed_ms"] >= 0


def test_local_brand_model_is_resolved_only_inside_sidecar():
    assert model_api_server._brand_model_id("provider-local-model") == "mbem-v1-local"
    assert model_api_server._brand_model_id("provider-plus-model") == "mbcd-plus-v1"
    assert (
        model_api_server._runtime_model_name("mbcd-std-v1")
        == model_api_server.MANAGER_MODELS["mbem-v1-local"].model_id
    )


def test_local_model_catalog_response_uses_only_memorybread_branding():
    meta = model_api_server.get_model("mbem-v1-local")
    payload = model_api_server._model_to_dict(
        meta,
        {"status": "installed", "is_active": False},
    )
    serialized = json.dumps(payload, ensure_ascii=False).lower()

    assert payload["name"] == "MBEM v1.0"
    assert payload["provider"] == "memorybread"
    assert "qwen" not in serialized
    assert "ollama" not in serialized


def test_attachment_only_history_preserves_original_question_and_updates_one_record(tmp_path, monkeypatch):
    import sqlite3
    database = tmp_path / 'history.db'
    with sqlite3.connect(str(database)) as conn:
        conn.execute('CREATE TABLE rag_sessions (id INTEGER PRIMARY KEY, ts INTEGER, scene_type TEXT, user_query TEXT, retrieved_ids TEXT, prompt_used TEXT, llm_response TEXT, latency_ms INTEGER, model TEXT)')
    monkeypatch.setattr(model_api_server, 'DB_PATH', str(database))
    metadata = {'source': 'floating_assist', 'manual_instruction': '比较这两张图', 'attachments': [
        {'id': 'a', 'path': '/local/a.png', 'type': 'image/png', 'name': 'a.png'},
        {'id': 'b', 'path': '/local/b.png', 'type': 'image/png', 'name': 'b.png'},
    ]}
    record_id = model_api_server._save_rag_session('改写后的查询', '', '已提交', [], 0, metadata)
    metadata['_session_id'] = record_id
    assert model_api_server._save_rag_session('另一个查询', '', '生成失败，可重试', [], 10, metadata) == record_id
    with sqlite3.connect(str(database)) as conn:
        rows = conn.execute('SELECT user_query, retrieved_ids, llm_response FROM rag_sessions').fetchall()
    assert len(rows) == 1
    assert rows[0][0] == '比较这两张图'
    assert json.loads(rows[0][1])[0]['attachments'] == metadata['attachments']
    assert rows[0][2] == '生成失败，可重试'


def test_stream_failure_keeps_accepted_history(tmp_path, monkeypatch):
    import sqlite3
    database = tmp_path / 'history.db'
    with sqlite3.connect(str(database)) as conn:
        conn.execute('CREATE TABLE rag_sessions (id INTEGER PRIMARY KEY, ts INTEGER, scene_type TEXT, user_query TEXT, retrieved_ids TEXT, prompt_used TEXT, llm_response TEXT, latency_ms INTEGER, model TEXT)')
    class BrokenQueue:
        def submit(self, priority, func, **kwargs):
            import concurrent.futures
            future = concurrent.futures.Future()
            try:
                future.set_result(self.submit_sync(priority, func, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            return future

        def submit_sync(self, *args, **kwargs):
            raise RuntimeError('test generation failure')
    monkeypatch.setattr(model_api_server, 'DB_PATH', str(database))
    monkeypatch.setattr(model_api_server, '_rag_pipeline', object())
    monkeypatch.setattr(model_api_server, 'get_global_queue', lambda: BrokenQueue())
    monkeypatch.setattr(model_api_server, '_build_rag_llm_override', lambda *a, **k: None)
    monkeypatch.setattr(model_api_server, 'log_llm_usage', lambda *a, **k: None)
    monkeypatch.setattr('model_registry_global.check_memory_pressure', lambda: 'normal')
    response = model_api_server.app.test_client().post('/query/stream', json={
        'query': '比较附件', 'manual_instruction': '比较附件', 'source': 'floating_assist',
        'attachments': [{'path': '/local/a.png', 'name': 'a.png'}],
    }, buffered=True)
    assert 'RAG_STREAM_FAILED' in response.get_data(as_text=True)
    with sqlite3.connect(str(database)) as conn:
        rows = conn.execute('SELECT user_query, retrieved_ids, llm_response FROM rag_sessions').fetchall()
    assert len(rows) == 1
    assert rows[0][0] == '比较附件'
    assert json.loads(rows[0][1])[0]['attachments'][0]['path'] == '/local/a.png'
    assert '已提交' not in rows[0][2]


def test_current_material_survives_wrong_intent_summary():
    metadata = {'source': 'floating_assist', 'manual_instruction': '他表达了什么？',
                'ocr_text': '今天生日：小陈。记得送他们一张贺卡。',
                'attachments': [{'name': 'reminder.png'}]}
    intent = model_api_server.FloatingAssistIntent(
        core_question='QCon 筹备进展', retrieval_query='QCon', source='model', needs_rag=False)
    prompt = _build_floating_assist_rag_query_from_intent('原始附件说明', intent, metadata)
    assert '核心问题：他表达了什么？' in prompt
    assert metadata['ocr_text'] in prompt
    assert '原始附件说明' in prompt
    fallback = _analyze_floating_assist_intent('原始附件说明', metadata, None)
    assert fallback.needs_rag is True


def test_floating_assist_query_makes_material_gap_conditional_and_allows_general_answer():
    """通用知识问题：模板不得无条件要求材料不足即说明缺失，须允许模型用自身知识作答"""
    intent = model_api_server.FloatingAssistIntent(
        core_question='小米体重计的原理是什么？',
        retrieval_query='小米体重计 原理',
        screen_context_summary='',
        answer_requirements=['直接回答核心问题'],
        confidence=0.45,
        needs_rag=True,
        source='fallback',
    )
    prompt = _build_floating_assist_rag_query_from_intent(
        '小米体重计的原理是什么？', intent,
        {'source': 'floating_assist', 'manual_instruction': '小米体重计的原理是什么？'})
    # 无条件的“材料不足时明确说明缺失”已移除
    assert '材料不足时明确说明缺失' not in prompt
    # 改为条件化：仅当需解读材料或依赖私人/内部事实且缺失时才说明缺失
    assert '才明确说明缺失' in prompt
    # 通用知识/常识问题即使无材料或记忆也要用自身知识作答，不得拒答
    assert '通用知识' in prompt
    assert '不得拒答' in prompt
    # 候选记忆为宽松召回，须自行判断是否可用
    assert '宽松召回' in prompt


def test_public_rag_stream_error_maps_vector_service_to_actionable_message():
    """向量检索繁忙/不可用须给出可操作文案，而非笼统的“咨询生成失败”"""
    busy = '记忆检索服务繁忙（后台可能正在建立索引），请稍后重试'
    # retriever.py 内部向量搜索重试耗尽后抛出的错误
    assert model_api_server._public_rag_stream_error(
        '内部向量搜索服务不可用，已阻止降级到关键词兜底') == busy
    # 直连 Qdrant 不可用 / 向量检索失败同样归为检索繁忙
    assert model_api_server._public_rag_stream_error(
        'Qdrant 不可用，向量检索无法执行') == busy
    assert model_api_server._public_rag_stream_error(
        '向量检索失败: locked') == busy
    # 回归护栏：其它分类不被误伤
    assert model_api_server._public_rag_stream_error(
        'ollama 连接被拒绝') == '本地模型服务暂时不可用，请检查模型状态后重试'
    assert model_api_server._public_rag_stream_error(
        '未知错误') == '咨询生成失败，请稍后重试'


def test_supplied_empty_contexts_never_search_memory(tmp_path):
    from rag.pipeline import RagPipeline
    pipeline = object.__new__(RagPipeline)
    pipeline._top_k = 5
    pipeline._db_path = str(tmp_path / 'empty.db')
    pipeline._system = '助手'
    pipeline._read_user_identity = lambda: {}
    pipeline._build_identity_clause = lambda _: ''
    llm = FakeIntentLlm('这是提醒主管给今天生日的同事送贺卡。')
    # No embedding or retriever is installed: any accidental recall must fail.
    result = pipeline.query('他表达了什么？今天生日：小陈。请送贺卡。', llm=llm, supplied_contexts=[])
    assert result.contexts == []
    assert '今天生日：小陈' in llm.last_prompt
    assert '贺卡' in result.answer


def test_stream_current_image_keeps_evidence_and_recalls_by_default(monkeypatch):
    evidence = '今天生日：小陈。记得送他们一张贺卡。'
    calls = []
    class Pipeline:
        def query(self, query, **kwargs):
            assert evidence in query
            assert '核心问题：他表达了什么？' in query
            if kwargs.get('references_only') or len(calls) == 2:
                assert 'supplied_contexts' not in kwargs
            else:
                assert kwargs['supplied_contexts'] == []
            calls.append(kwargs.get('references_only', False))
            if kwargs.get('on_contexts'):
                kwargs['on_contexts']([])
            if kwargs.get('on_delta'):
                kwargs['on_delta']('生日提醒')
            return RagResult(answer='生日提醒', contexts=[], model='local')
        def _build_context(self, contexts):
            return ''
    class Queue:
        def submit(self, priority, func, **kwargs):
            import concurrent.futures
            future = concurrent.futures.Future()
            try:
                future.set_result(self.submit_sync(priority, func, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            return future

        def submit_sync(self, priority, func, **kwargs):
            return func()
    monkeypatch.setattr(model_api_server, '_rag_pipeline', Pipeline())
    monkeypatch.setattr(model_api_server, 'get_global_queue', lambda: Queue())
    monkeypatch.setattr(model_api_server, '_build_rag_llm_override', lambda *a, **k: None)
    monkeypatch.setattr(model_api_server, '_save_rag_session', lambda *a, **k: 1)
    monkeypatch.setattr(model_api_server, 'log_llm_usage', lambda *a, **k: None)
    monkeypatch.setattr('model_registry_global.check_memory_pressure', lambda: 'normal')
    payload = {'query': '用户手工指令：\n他表达了什么？', 'source': 'floating_assist',
               'manual_instruction': '他表达了什么？', 'ocr_text': evidence,
               'attachments': [{'name': 'birthday.png'}]}
    response = model_api_server.app.test_client().post('/query/stream', json=payload, buffered=True)
    events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith('data: ')]
    assert next(e for e in events if e['type'] == 'done')['answer'] == '生日提醒'
    assert calls == [True, False]
    response = model_api_server.app.test_client().post('/query', json=payload)
    assert response.status_code == 200
    assert calls == [True, False, False]
