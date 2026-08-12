import json

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
            return RagResult(answer="部分答案", contexts=[chunk], model="internal-model")

        def _build_context(self, contexts):
            return "context"

    class InlineQueue:
        def submit_sync(self, priority, func, timeout=None, lane=None):
            calls.append("queue")
            assert calls == ["retrieve", "queue"]
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
    assert calls == ["retrieve", "queue", "generate"]
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
