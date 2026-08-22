"""
记忆面包 AI Sidecar 入口点

启动方式：
    python main.py                    # 生产模式（加载所有 AI 模型）
    python main.py --dry-run          # 干运行（仅测试 IPC 通信，不加载模型）
    python main.py --log-level DEBUG  # 调试日志

环境变量：
    SIDECAR_LOG_LEVEL: 日志级别（DEBUG/INFO/WARNING/ERROR），默认 INFO
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
IPC_PYTHON_DIR = PROJECT_ROOT.parent / "shared" / "ipc-protocol" / "python"
if str(IPC_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(IPC_PYTHON_DIR))

from memory_bread_ipc import IpcServer
from runtime_lock import SidecarAlreadyRunningError, SidecarInstanceLock

# 模块级 runtime 状态：Flask daemon 线程通过它访问 BackgroundProcessor
_RUNTIME_STATE: dict = {
    "dispatch": None,
    "bg_processor": None,
}
_STATE_DIR = Path.home() / ".memory-bread" / "state"
_RUNTIME_STATUS_FILE = _STATE_DIR / "sidecar_runtime_status.json"
_INSTANCE_LOCK_FILE = _STATE_DIR / "sidecar.instance.lock"
_RUNTIME_STATUS_CACHE: Optional[dict] = None
_RUNTIME_STATUS_LOCK = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# 日志配置
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sidecar.main")


def _write_runtime_status(
    *,
    mode: str,
    full_dispatch_ready: bool,
    background_processor_running: bool,
    critical_checks_passed: bool,
    embedding_ok: bool,
    issues: Optional[list[str]] = None,
    checks: Optional[dict] = None,
) -> None:
    """写入关键后台能力状态，供 Core Engine 监控页读取。"""
    global _RUNTIME_STATUS_CACHE
    with _RUNTIME_STATUS_LOCK:
        _RUNTIME_STATUS_CACHE = {
            "mode": mode,
            "full_dispatch_ready": full_dispatch_ready,
            "background_processor_running": background_processor_running,
            "critical_checks_passed": critical_checks_passed,
            "embedding_ok": embedding_ok,
            "issues": list(issues or []),
            "checks": checks or {},
        }
        payload = {
            **_RUNTIME_STATUS_CACHE,
            "updated_at_ms": int(time.time() * 1000),
        }
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _RUNTIME_STATUS_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_RUNTIME_STATUS_FILE)
        except Exception as exc:
            logger.warning("写入 sidecar_runtime_status.json 失败: %s", exc)


def _runtime_status_heartbeat() -> None:
    """独立于 asyncio 推理循环刷新状态，避免长任务造成健康误报。"""
    while True:
        time.sleep(60)
        cached_status = _RUNTIME_STATUS_CACHE
        if cached_status is None:
            continue
        _write_runtime_status(**cached_status)


# ─────────────────────────────────────────────────────────────────────────────
# CLI 参数
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="记忆面包 AI Sidecar",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不加载 AI 模型，仅测试 IPC 服务是否正常启动",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("SIDECAR_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）",
    )
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# 内部向量搜索 HTTP 服务（端口 7072）
# 在 sidecar 进程内运行，复用 VectorStorage 单例的 Qdrant 客户端，
# 避免 model_api_server 另开 Qdrant 连接导致文件锁冲突。
# ─────────────────────────────────────────────────────────────────────────────

def _start_vector_search_server() -> None:
    """在 daemon 线程中启动内部向量搜索 HTTP 服务（端口 7072）。"""
    try:
        from flask import Flask, jsonify, request as flask_request
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue, Range

        _vs_app = Flask("vector_search_internal")
        logging.getLogger("werkzeug").setLevel(logging.WARNING)

        def build_qdrant_filter(raw_filters: Optional[dict]):
            if not raw_filters:
                return None
            conditions = []

            def add_range(key: str, gte_key: str, lte_key: str) -> None:
                gte = raw_filters.get(gte_key)
                lte = raw_filters.get(lte_key)
                if gte is not None or lte is not None:
                    conditions.append(FieldCondition(key=key, range=Range(gte=gte, lte=lte)))

            def add_match(key: str, value) -> None:
                if value is None:
                    return
                if isinstance(value, list):
                    normalized = [item for item in dict.fromkeys(value) if item is not None and item != ""]
                    if len(normalized) == 1:
                        conditions.append(FieldCondition(key=key, match=MatchValue(value=normalized[0])))
                    elif len(normalized) > 1:
                        conditions.append(FieldCondition(key=key, match=MatchAny(any=normalized)))
                else:
                    conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))

            add_range("time", "start_ts", "end_ts")
            add_range("observed_at", "observed_start_ts", "observed_end_ts")
            add_range("event_time_start", "event_start_ts", "event_end_ts")
            add_match("source_type", raw_filters.get("source_types"))
            add_match("app_name", raw_filters.get("app_names"))
            add_match("category", raw_filters.get("category"))
            add_match("activity_type", raw_filters.get("activity_types"))
            add_match("content_origin", raw_filters.get("content_origins"))
            add_match("history_view", raw_filters.get("history_view"))
            add_match("is_self_generated", raw_filters.get("is_self_generated"))
            add_match("evidence_strength", raw_filters.get("evidence_strengths"))
            return Filter(must=conditions) if conditions else None

        @_vs_app.route('/vector_search', methods=['POST'])
        def vector_search():
            data = flask_request.get_json()
            if not data or 'query_vector' not in data:
                return jsonify({'error': 'missing query_vector'}), 400
            query_vector = data['query_vector']
            top_k = int(data.get('top_k', 10))
            score_threshold = float(data.get('score_threshold', 0.3))
            qdrant_filter = build_qdrant_filter(data.get('filters'))
            try:
                from embedding.vector_storage import get_vector_storage
                vs = get_vector_storage()
                client = vs._get_qdrant_client()
                if client is None:
                    return jsonify({'error': 'Qdrant client not available'}), 503
                results = client.query_points(
                    collection_name=vs._collection_name,
                    query=query_vector,
                    # 文档分块后一个 URL 可能命中多个 chunk；先多取一些，
                    # 再按 doc_key 折叠，避免长文档挤掉其他来源。上限过小会
                    # 把中等相关度（0.5 左右）的文档截断在候选池之外。
                    limit=min(max(top_k * 4, top_k), 400),
                    score_threshold=score_threshold,
                    query_filter=qdrant_filter,
                ).points
                hits = []
                seen_doc_keys = set()
                for hit in results:
                    payload = dict(hit.payload or {})
                    capture_id = int(payload.get('capture_id') or 0)
                    doc_key = payload.get('doc_key') or f"capture:{capture_id}"
                    if doc_key in seen_doc_keys:
                        continue
                    seen_doc_keys.add(doc_key)
                    hits.append({
                        'capture_id': capture_id,
                        'doc_key': doc_key,
                        'text': payload.get('text', ''),
                        'score': float(hit.score),
                        'source': 'vector',
                        'metadata': payload,
                    })
                    if len(hits) >= top_k:
                        break
                return jsonify({'results': hits})
            except Exception as e:
                logging.getLogger(__name__).error("vector_search 失败: %s", e)
                return jsonify({'error': str(e)}), 500

        @_vs_app.route('/health', methods=['GET'])
        def health():
            return jsonify({'status': 'ok', 'service': 'vector_search'})

        @_vs_app.route('/internal/extraction_status', methods=['GET'])
        def extraction_status():
            bg = _RUNTIME_STATE.get("bg_processor")
            if bg is None:
                return jsonify({
                    "running": False,
                    "extracting_captures": [],
                    "extracting_group_count": 0,
                    "last_extraction_at_ms": None,
                })
            snapshot = bg.get_extraction_status()
            snapshot["running"] = True
            return jsonify(snapshot)

        logging.getLogger(__name__).info("内部向量搜索服务已启动 (port 7072)")
        _vs_app.run(host='127.0.0.1', port=7072, debug=False, threaded=True, use_reloader=False)
    except Exception as e:
        logging.getLogger(__name__).warning("内部向量搜索服务启动失败（不影响主服务）: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

async def _run_main(args: argparse.Namespace) -> None:
    limited_mode = os.environ.get("SIDECAR_LIMITED_MODE") == "1"
    _write_runtime_status(
        mode="starting",
        full_dispatch_ready=False,
        background_processor_running=False,
        critical_checks_passed=False,
        embedding_ok=False,
        issues=["Sidecar 正在启动，完整能力尚未就绪"],
    )
    threading.Thread(
        target=_runtime_status_heartbeat,
        daemon=True,
        name="runtime-status-heartbeat",
    ).start()

    # 启动内部向量搜索 HTTP 服务（daemon 线程）
    threading.Thread(target=_start_vector_search_server, daemon=True, name="vector-search-server").start()

    from memory_bread_ipc import IpcResponse, PingResult
    from ocr.worker import OcrWorker
    from ocr.engine import OcrEngine

    ocr_worker = OcrWorker(engine=OcrEngine.create_default())
    runtime_state = _RUNTIME_STATE

    async def limited_dispatch(req):
        if req.task.type == "ping":
            return IpcResponse.make_ok(req.id, PingResult(), 0)
        if req.task.type == "ocr":
            return await ocr_worker.handle(req)
        return IpcResponse.make_error(req.id, "NOT_IMPLEMENTED", f"任务类型 '{req.task.type}' 在基础 IPC 模式下不可用", 0)

    runtime_state["dispatch"] = limited_dispatch

    async def dispatch_proxy(req):
        dispatch_fn = runtime_state["dispatch"]
        return await dispatch_fn(req)

    async def auto_upgrade_to_full_mode() -> None:
        """
        后台任务：定期检查 Ollama 服务是否从不可用变为可用。
        如果检测到可用，自动升级到完整模式。
        """
        check_interval = 30  # 每 30 秒检查一次
        max_checks = 60      # 最多检查 60 次（30 分钟），之后放弃
        checks_done = 0

        logger.info("自动升级监控已启动：将定期检查 Ollama 服务状态")

        while checks_done < max_checks:
            await asyncio.sleep(check_interval)
            checks_done += 1

            # 如果已经升级到完整模式，停止监控
            if runtime_state.get("dispatch") != limited_dispatch:
                logger.info("已升级到完整模式，停止自动升级监控")
                return

            # 检查 Ollama 是否可用
            try:
                from startup_checks import check_critical_requirements_silent
                checks_result = await asyncio.to_thread(check_critical_requirements_silent)

                if checks_result.get('critical_passed'):
                    logger.info("🔄 检测到 Ollama 服务已可用，开始升级到完整模式")

                    # 初始化完整功能
                    if not checks_result.get('embedding_ok'):
                        logger.warning("向量模型不可用，以降级模式启动（RAG 向量检索不可用，提炼功能正常）")

                    from dispatcher_v2 import Dispatcher
                    dispatcher = Dispatcher(ocr_worker=ocr_worker)
                    await dispatcher.initialize()
                    runtime_state["dispatch"] = dispatcher.dispatch
                    logger.info("生产模式：已切换到完整任务分发器")

                    from background_processor import BackgroundProcessor
                    db_path = str(Path.home() / ".memory-bread" / "memory-bread.db")
                    bg_processor = BackgroundProcessor(db_path=db_path, interval=30, batch_size=20)
                    runtime_state["bg_processor"] = bg_processor
                    asyncio.create_task(bg_processor.run())
                    asyncio.create_task(bg_processor.backfill_document_vectors())

                    issues = [] if checks_result.get('embedding_ok') else ["向量模型不可用，RAG 向量检索降级"]
                    _write_runtime_status(
                        mode="full",
                        full_dispatch_ready=True,
                        background_processor_running=True,
                        critical_checks_passed=True,
                        embedding_ok=bool(checks_result.get('embedding_ok')),
                        issues=issues,
                        checks=checks_result,
                    )
                    logger.info("✅ 自动升级成功：后台处理器已启动（向量化 + 时间线提炼）")
                    return
                else:
                    # 检查未通过，记录详细信息以便诊断
                    if checks_done % 10 == 0:  # 每 10 次（5 分钟）记录一次详细状态
                        logger.info(
                            "自动升级检查未通过（第 %d/%d 次）: critical_passed=%s, embedding_ok=%s, message=%s",
                            checks_done, max_checks,
                            checks_result.get('critical_passed'),
                            checks_result.get('embedding_ok'),
                            checks_result.get('message', 'unknown')
                        )
            except Exception as exc:
                # 将 debug 改为 warning，确保异常能被看到
                logger.warning("自动升级检查遇到异常（第 %d 次）: %s", checks_done, exc, exc_info=checks_done % 10 == 0)

        logger.warning("自动升级监控超时（30 分钟），停止尝试。如需启用完整功能，请重启应用。")

    async def bootstrap_full_dispatch() -> None:
        if limited_mode:
            logger.warning("SIDECAR_LIMITED_MODE=1，保持基础 IPC 模式，仅保留 ping/OCR 能力")
            _write_runtime_status(
                mode="limited",
                full_dispatch_ready=False,
                background_processor_running=False,
                critical_checks_passed=False,
                embedding_ok=False,
                issues=["SIDECAR_LIMITED_MODE=1，仅启用 ping/OCR，时间线提炼与 bake 不会运行"],
            )
            return

        try:
            from startup_checks import run_startup_checks, get_ollama_setup_detail
            checks_result = await asyncio.to_thread(run_startup_checks)
            # 只有 Ollama+LLM 核心检查通过才能启动提炼；向量模型失败仅降级（不阻塞）
            if not checks_result.get('critical_passed'):
                detail = await asyncio.to_thread(get_ollama_setup_detail)
                _write_runtime_status(
                    mode="basic_ipc",
                    full_dispatch_ready=False,
                    background_processor_running=False,
                    critical_checks_passed=False,
                    embedding_ok=bool(checks_result.get('embedding_ok')),
                    issues=[
                        "核心启动检查未通过，仅保留 ping/OCR 能力",
                        detail.get('message', 'Ollama/LLM 不可用'),
                    ],
                    checks=checks_result,
                )
                logger.warning(
                    "核心启动检查未通过，保持基础 IPC 模式，仅保留 ping/OCR 能力（原因: %s）",
                    detail.get('message', 'unknown'),
                )
                # 启动自动升级任务：定期检查 Ollama 是否变为可用
                asyncio.create_task(auto_upgrade_to_full_mode())
                return
            if not checks_result.get('embedding_ok'):
                logger.warning("向量模型不可用，以降级模式启动（RAG 向量检索不可用，提炼功能正常）")

            from dispatcher_v2 import Dispatcher
            dispatcher = Dispatcher(ocr_worker=ocr_worker)
            await dispatcher.initialize()
            runtime_state["dispatch"] = dispatcher.dispatch
            logger.info("生产模式：已切换到完整任务分发器")

            from background_processor import BackgroundProcessor
            db_path = str(Path.home() / ".memory-bread" / "memory-bread.db")
            bg_processor = BackgroundProcessor(db_path=db_path, interval=30, batch_size=20)
            runtime_state["bg_processor"] = bg_processor
            asyncio.create_task(bg_processor.run())
            asyncio.create_task(bg_processor.backfill_document_vectors())
            issues = [] if checks_result.get('embedding_ok') else ["向量模型不可用，RAG 向量检索降级"]
            _write_runtime_status(
                mode="full",
                full_dispatch_ready=True,
                background_processor_running=True,
                critical_checks_passed=True,
                embedding_ok=bool(checks_result.get('embedding_ok')),
                issues=issues,
                checks=checks_result,
            )
            logger.info("后台处理器已启动（向量化 + 时间线提炼）")
        except Exception as exc:
            _write_runtime_status(
                mode="basic_ipc",
                full_dispatch_ready=False,
                background_processor_running=False,
                critical_checks_passed=False,
                embedding_ok=False,
                issues=[f"完整能力初始化失败：{exc}"],
            )
            logger.error("完整能力初始化失败，保持基础 IPC 模式: %s", exc, exc_info=True)

    if args.dry_run:
        logger.info("dry-run 模式：使用基础 IPC 分发器（ping + OCR）")
        _write_runtime_status(
            mode="dry_run",
            full_dispatch_ready=False,
            background_processor_running=False,
            critical_checks_passed=False,
            embedding_ok=False,
            issues=["dry-run 模式，仅启用 ping/OCR"],
        )
    else:
        asyncio.create_task(bootstrap_full_dispatch())

    server = IpcServer(dispatch_fn=dispatch_proxy)

    # 注册优雅关闭信号
    loop = asyncio.get_running_loop()

    def shutdown():
        server.stop()
        bg_processor = runtime_state.get("bg_processor")
        if bg_processor:
            bg_processor.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    logger.info("记忆面包 AI Sidecar 启动完成，等待 Rust Core Engine 连接...")
    try:
        await server.serve()
    finally:
        bg_processor = runtime_state.get("bg_processor")
        if bg_processor:
            bg_processor.stop()
        ocr_worker.close()
        _write_runtime_status(
            mode="stopped",
            full_dispatch_ready=False,
            background_processor_running=False,
            critical_checks_passed=False,
            embedding_ok=False,
            issues=["Sidecar 已停止"],
        )
    logger.info("Sidecar 已正常退出")


async def _main() -> None:
    args = _parse_args()
    logging.getLogger().setLevel(args.log_level)
    instance_lock = SidecarInstanceLock(_INSTANCE_LOCK_FILE)
    try:
        instance_lock.acquire()
    except SidecarAlreadyRunningError as exc:
        logger.error("拒绝启动重复 Sidecar: %s", exc)
        return

    try:
        await _run_main(args)
    finally:
        instance_lock.release()


if __name__ == "__main__":
    asyncio.run(_main())
