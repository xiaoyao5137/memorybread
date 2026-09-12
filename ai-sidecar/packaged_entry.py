"""PyInstaller 入口：在一个受签名的 helper 中承载三个本地 AI 服务。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import multiprocessing
import os
import sys
import threading


def run_sidecar() -> None:
    import main as sidecar_main

    # packaged_entry 使用第一个位置参数选择 helper 内承载的服务，
    # Sidecar 自己还会再次解析 sys.argv；不要把外层的 "sidecar" 子命令传进去。
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    asyncio.run(sidecar_main._main())


def run_model_api() -> None:
    import model_api_server as server

    logging.basicConfig(level=logging.INFO)

    def warmup_rag_pipeline() -> None:
        try:
            server.get_rag_pipeline()
            server.logger.info("RAG pipeline 预热完成")
        except Exception as exc:
            server.logger.error("RAG pipeline 预热失败: %s", exc, exc_info=True)

    threading.Thread(
        target=warmup_rag_pipeline,
        daemon=True,
        name="rag-warmup",
    ).start()
    server.logger.info("RAG pipeline 异步预热已启动")
    server._idle_diary_backfill_worker.start()
    from runtime_endpoints import service_bind

    host, port = service_bind("model_api")
    server.app.run(
        host=host,
        port=port,
        debug=False,
        threaded=True,
        use_reloader=False,
    )


def run_creation_service() -> None:
    import uvicorn
    from creation.app import app

    from runtime_endpoints import service_bind

    host, port = service_bind("creation")
    uvicorn.run(app, host=host, port=port, log_level="info")


def run_diagnostics_self_check() -> None:
    # Import only definitions: no listeners, downloads, model loads or user state writes.
    from initialization_manager import InitializationManager
    from runtime_download import RuntimeDownloader
    from runtime_readiness import CapabilityWarmup

    assert callable(InitializationManager.consultation_readiness)
    assert callable(RuntimeDownloader.download)
    assert callable(CapabilityWarmup.request)
    print("diagnostics.v2: readiness verified-download resumable-upload")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="记忆面包本地 AI 服务")
    parser.add_argument(
        "service",
        choices=("sidecar", "model-api", "creation", "diagnostics-self-check"),
        help="要启动的内置服务",
    )
    return parser.parse_args()


def main() -> None:
    multiprocessing.freeze_support()
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    service = parse_args().service
    if service == "diagnostics-self-check":
        run_diagnostics_self_check()
    elif service == "sidecar":
        run_sidecar()
    elif service == "model-api":
        run_model_api()
    else:
        run_creation_service()


if __name__ == "__main__":
    main()
