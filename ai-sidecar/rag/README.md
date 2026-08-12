# RAG 服务模块

记忆面包 的检索增强生成（RAG）服务，提供智能问答功能。

## 架构

```
用户查询
  ↓
1. Query Embedding（bge-m3 向量化）
  ↓
2. 混合检索
   ├─ Qdrant 向量检索（语义相似度）
   ├─ SQLite FTS5 关键词检索
   └─ 知识库 FTS5 检索
  ↓
3. RRF 融合（Reciprocal Rank Fusion）
  ↓
4. Prompt 组装
  ↓
5. LLM 推理（Ollama）
  ↓
6. 返回答案 + 上下文
```

## 核心组件

### 1. 向量存储（embedding/vector_storage.py）
- 负责将向量写入 Qdrant 和 SQLite
- 由 `background_processor.py` 自动调用
- 支持批量和单条存储

### 2. 检索器（rag/retriever.py）
- **VectorRetriever**: Qdrant 向量检索
- **Fts5Retriever**: SQLite FTS5 全文检索
- **KnowledgeFts5Retriever**: 知识库检索

### 3. RAG Pipeline（rag/pipeline.py）
- 完整的 RAG 查询流程编排
- 支持依赖注入，便于测试
- 自动降级（向量检索失败时使用 FTS5）

### 4. LLM 客户端（rag/llm/）
- 支持 Ollama 本地模型
- 兼容 OpenAI API 格式
- 可扩展到云端 API

## 使用方法

### 启动服务

```bash
# 方式 1: 从仓库根目录启动完整开发环境（推荐）
./start.sh start

# 方式 2: 在 ai-sidecar 目录单独启动统一 Model API / RAG API
source .venv/bin/activate
python3 model_api_server.py
```

### HTTP API

**端点**: `http://127.0.0.1:7071/query`

**请求**:
```json
{
  "query": "今天我做了什么工作？",
  "top_k": 5
}
```

**响应**:
```json
{
  "answer": "根据工作记录，你今天...",
  "contexts": [
    {
      "capture_id": 123,
      "text": "...",
      "score": 0.85,
      "source": "vector"
    }
  ],
  "model": "qwen2.5:3b"
}
```

## 依赖

```bash
pip install qdrant-client fastapi uvicorn sentence-transformers
```

## 配置

### 环境变量

- `WORKBUDDY_DB_PATH`: SQLite 数据库路径（默认: `~/.memory-bread/memory-bread.db`）
- `QDRANT_HOST`: Qdrant 服务地址（默认: `localhost`）
- `QDRANT_PORT`: Qdrant 服务端口（默认: `6333`）

### Qdrant 安装

```bash
# Docker 方式
docker run -p 6333:6333 qdrant/qdrant

# 或使用 Qdrant 本地模式（无需 Docker）
# 会自动在 ~/.qdrant/ 创建数据目录
```

## 向量化流程

1. **自动向量化**: `background_processor.py` 定期扫描未向量化的记录
2. **向量存储**: 调用 `embedding/vector_storage.py` 写入 Qdrant 和 SQLite
3. **元数据管理**: SQLite `vector_index` 表存储 `qdrant_point_id` 映射

## 测试

```bash
# 测试模块导入
python test_rag_imports.py

# 测试 HTTP API
curl -X POST http://127.0.0.1:7071/query \
  -H "Content-Type: application/json" \
  -d '{"query": "测试查询", "top_k": 3}'
```

## 性能优化

- **懒加载**: 模型在首次使用时才加载
- **批量处理**: 向量化支持批量操作
- **缓存**: Qdrant 客户端复用连接
- **异步**: 后台处理不阻塞主流程

## 故障排查

### Qdrant 连接失败
```
ERROR: 连接 Qdrant 失败
```
**解决**: 确保 Qdrant 服务已启动（`docker ps` 或检查本地进程）

### 向量维度不匹配
```
ERROR: vector dimension mismatch
```
**解决**: 删除 Qdrant 集合重新创建（`qdrant_manager.py` 会自动处理）

### FTS5 检索失败
```
ERROR: no such table: captures_fts
```
**解决**: 运行数据库迁移（`shared/db-schema/migrations/`）

## 未来扩展

- [ ] Rerank 重排序（使用 bge-reranker）
- [ ] 多模态检索（图像 + 文本）
- [ ] 用户反馈学习（RLHF）
- [ ] 分布式向量库（Qdrant 集群）
