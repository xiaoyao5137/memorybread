#!/usr/bin/env python3

from __future__ import annotations

"""
RAG 查询 HTTP 服务
在端口 7071 上提供 RAG 查询 API
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# 懒加载 RAG pipeline
_rag_pipeline = None
RAG_REFERENCE_LIMIT = 10
LOCAL_ANALYSIS_MODEL_ID = "mbem-v1-local"


def coerce_rag_top_k(value, default: int = RAG_REFERENCE_LIMIT) -> int:
    try:
        top_k = int(value)
    except (TypeError, ValueError):
        top_k = default
    return max(1, min(top_k, RAG_REFERENCE_LIMIT))

def get_rag_pipeline():
    """懒加载 RAG pipeline"""
    global _rag_pipeline
    if _rag_pipeline is None:
        logger.info("初始化 RAG pipeline...")
        from embedding.model import EmbeddingModel
        from rag.retriever import VectorRetriever, Fts5Retriever, KnowledgeFts5Retriever
        from rag.llm.ollama import OllamaBackend
        from rag.pipeline import RagPipeline

        db_path = str(Path.home() / ".memory-bread" / "memory-bread.db")
        qdrant_path = str(Path.home() / ".qdrant")

        from model_registry_global import get_shared_embedding, get_active_ollama_model
        embedding_model = get_shared_embedding()
        # 使用本地 Qdrant 模式
        vector_retriever = VectorRetriever(
            collection="memory_bread_captures",
            qdrant_path=qdrant_path
        )
        fts5_retriever = Fts5Retriever(db_path=db_path)
        knowledge_retriever = KnowledgeFts5Retriever(db_path=db_path)
        ollama_model = get_active_ollama_model()
        llm = OllamaBackend(model=ollama_model, timeout=300, num_predict=1024)

        _rag_pipeline = RagPipeline(
            embedding_model=embedding_model,
            vector_retriever=vector_retriever,
            fts5_retriever=fts5_retriever,
            knowledge_retriever=knowledge_retriever,
            llm=llm,
            top_k=RAG_REFERENCE_LIMIT,
            db_path=db_path,
        )
        logger.info("RAG pipeline 初始化完成")
    return _rag_pipeline

@app.route('/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({"status": "ok", "service": "rag"})

@app.route('/query', methods=['POST'])
def rag_query():
    """RAG 查询接口"""
    try:
        data = request.get_json()
        if not data or 'query' not in data:
            return jsonify({'error': '缺少 query 参数'}), 400

        query = data['query']
        top_k = coerce_rag_top_k(data.get('top_k'))

        logger.info(f"收到 RAG 查询: {query}")

        # 执行 RAG 查询
        pipeline = get_rag_pipeline()
        result = pipeline.query(query, top_k=top_k)

        # 转换为 JSON 格式
        contexts = [
            {
                'capture_id': chunk.capture_id,
                'doc_key': chunk.doc_key,
                'text': chunk.text,
                'score': chunk.score,
                'source': chunk.source,
                'source_type': chunk.metadata.get('source_type') or chunk.source,
                'knowledge_id': chunk.metadata.get('knowledge_id'),
                'artifact_id': chunk.metadata.get('artifact_id'),
                'document_id': chunk.metadata.get('document_id'),
                'app_name': chunk.metadata.get('app_name'),
                'win_title': chunk.metadata.get('win_title'),
                'url': chunk.metadata.get('url') or chunk.metadata.get('source_url'),
                'source_url': chunk.metadata.get('source_url') or chunk.metadata.get('url'),
                'title': chunk.metadata.get('title'),
                'doc_type': chunk.metadata.get('doc_type'),
                'time': chunk.metadata.get('time') or chunk.metadata.get('ts') or chunk.metadata.get('end_time') or chunk.metadata.get('start_time'),
                'summary': chunk.metadata.get('summary'),
                'overview': chunk.metadata.get('overview'),
                'category': chunk.metadata.get('category'),
                'source_timeline_ids': chunk.metadata.get('source_timeline_ids'),
                'linked_knowledge_ids': chunk.metadata.get('linked_knowledge_ids'),
            }
            for chunk in result.contexts
        ]

        response = {
            'answer': result.answer,
            'contexts': contexts,
            'model': LOCAL_ANALYSIS_MODEL_ID,
        }

        logger.info(f"RAG 查询完成，返回 {len(contexts)} 条上下文")
        return jsonify(response)

    except Exception as e:
        logger.error(f"RAG 查询失败: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    from runtime_endpoints import service_bind
    bind_host, bind_port = service_bind("model_api")
    logger.info("启动 RAG 查询服务")
    logger.info("监听地址: http://%s:%s", bind_host, bind_port)

    # 预加载 RAG pipeline（避免首次查询超时）
    logger.info("预加载 RAG pipeline...")
    get_rag_pipeline()
    logger.info("RAG pipeline 预加载完成")

    app.run(host=bind_host, port=bind_port, debug=False, threaded=True)
