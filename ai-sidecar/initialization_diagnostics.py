"""Bounded, content-free failure evidence for support (Python 3.9 compatible).

Never serialize exception messages, source lines, environment values or arbitrary
log bodies here. The journal survives service restarts and is uploaded via the
same fixed log whitelist as other diagnostic files.
"""

import json
import logging
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
MAX_JOURNAL_BYTES = 256 * 1024
_WRITE_LOCK = threading.Lock()

EVIDENCE_FIELDS = {
    "reason", "port", "tcp_reachable", "managed_process_owned", "pid", "exit_code",
    "probe_attempts", "wait_ms", "exception_type", "cause_type", "errno", "http_status",
    "proxy_configured", "proxy_bypassed", "response_valid", "migration", "sqlite_errorcode",
    "missing_migration_count",
    "engine_version",
}


def safe_evidence(evidence):
    result = {}
    for key in EVIDENCE_FIELDS:
        value = evidence.get(key)
        if isinstance(value, (bool, int)):
            result[key] = value
        elif isinstance(value, str) and len(value) <= 100 and all(
            char.isascii() and (char.isalnum() or char in "_.-") for char in value
        ):
            result[key] = value
    return result


def exception_evidence(error):
    cause = error.__cause__ or error.__context__
    # URLError keeps the underlying socket exception in .reason.
    reason = getattr(error, "reason", None)
    if isinstance(reason, BaseException):
        cause = reason
    result = {"exception_type": type(error).__name__}
    if cause is not None:
        result["cause_type"] = type(cause).__name__
    for name, value in (
        ("errno", getattr(cause or error, "errno", None)),
        ("http_status", getattr(error, "code", None)),
    ):
        if isinstance(value, int):
            result[name] = value
    return safe_evidence(result)


def write_event(base_dir, event, state, evidence=None, error=None, attempt=None):
    """Best effort: diagnostics must never break initialization or recovery."""
    try:
        record = {
            "schema_version": "initialization-diagnostics.v1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "run_id": state.get("run_id"),
            "diagnostic_id": state.get("run_id"),
            "mode": state.get("mode"),
            "stage_id": state.get("current_stage"),
            "state": state.get("state"),
            "error_code": state.get("error_code"),
            "service_pid": os.getpid(),
            "previous_run_id": state.get("previous_run_id"),
            "evidence": safe_evidence(evidence or {}),
        }
        environment = state.get("environment") or {}
        record["environment"] = {key: environment[key] for key in (
            "os", "os_version", "architecture", "memory_gb", "disk_free_gb"
        ) if key in environment}
        if attempt is not None:
            record["attempt"] = attempt
        stage = next((item for item in state.get("stages", [])
                      if item.get("id") == state.get("current_stage")), {})
        record["duration_ms"] = stage.get("duration_ms")
        if error is not None:
            record["exception"] = exception_evidence(error)
            # Symbols and line numbers locate the failure without source text,
            # full paths, local variables or exception messages.
            record["frames"] = [
                {"module": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(error.__traceback__)[-12:]
            ]
        data = (json.dumps(record, ensure_ascii=True) + "\n").encode("utf-8")
        path = Path(base_dir) / "logs" / "initialization-diagnostics.jsonl"
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + len(data) > MAX_JOURNAL_BYTES:
                path.replace(path.with_name(path.name + ".1"))
            with path.open("ab") as output:
                output.write(data)
    except Exception:
        logger.warning("initialization diagnostic journal could not be written", exc_info=False)


def failure_copy(code, stage_id, evidence):
    """Only curated explanations reach the first-run UI; no raw exceptions."""
    reason = evidence.get("reason")
    if code == "RUNTIME_START_FAILED":
        messages = {
            "process_exited": "本地 AI 程序启动后意外退出，暂时无法使用提炼、咨询和创作功能。",
            "port_conflict": "本地 AI 需要的连接被其他程序占用，暂时无法连接。",
            "health_timeout": "本地 AI 在等待时间内没有回应检查，暂时无法确认它是否可用。",
            "spawn_failed": "未能打开本地 AI 程序，可能与程序文件或运行权限有关。",
            "legacy_exit_timeout": "旧的本地 AI 服务尚未退出，新服务暂时无法启动。",
            "engine_unresponsive": "本地 AI 程序仍在运行，但没有正常回应检查，需要重新启动。",
        }
        return ("本地 AI 暂时无法使用", messages.get(reason, "本地 AI 尚未准备好，暂时无法使用提炼、咨询和创作功能。"),
                "请点击“重试失败阶段”。若仍未恢复，请重新打开记忆面包，或点击“上报诊断”交由我们排查。")
    messages = {
        "UNSUPPORTED_PLATFORM": ("当前系统暂不支持", "此版本无法在当前操作系统上完成本地 AI 准备。"),
        "UNSUPPORTED_ARCHITECTURE": ("当前设备暂不支持", "此版本尚不支持当前设备的处理器类型。"),
        "RUNTIME_DOWNLOAD_FAILED": ("下载尚未完成", "准备本地 AI 所需的文件未能下载完成。"),
        "RUNTIME_NETWORK_FAILED": ("下载连接中断", "准备本地 AI 时未能连接下载服务。"),
        "RUNTIME_DOWNLOAD_TIMEOUT": ("下载等待时间较长", "本轮下载未能在等待时间内完成。"),
        "RUNTIME_TLS_FAILED": ("无法建立安全下载连接", "下载服务的安全连接校验没有通过。"),
        "RUNTIME_HTTP_FAILED": ("下载服务暂时不可用", "下载服务器没有正常返回所需文件。"),
        "RUNTIME_CHECKSUM_MISMATCH": ("下载文件校验未通过", "下载的文件与预期不一致，需要重新获取。"),
        "RUNTIME_ARCHIVE_INVALID": ("下载文件无法使用", "下载包无法打开，或缺少运行所需文件。"),
        "RUNTIME_WRITE_FAILED": ("无法保存本地 AI 文件", "程序无法将运行所需文件写入磁盘。"),
        "RUNTIME_RESUME_INVALID": ("下载需要重新连接", "下载服务返回的文件片段不一致，本轮续传未能完成。"),
        "RUNTIME_INSTALL_FAILED": ("本地 AI 安装未完成", "下载的程序未能完成安装，需要重试或由我们进一步排查。"),
        "INSUFFICIENT_DISK_SPACE": ("磁盘空间不足", "准备本地 AI 需要至少 6 GB 可用磁盘空间。"),
        "MODEL_DOWNLOAD_FAILED": ("模型还没有准备好", "采集提炼或语义检索所需的模型未能完成下载或校验。"),
        "SKILLS_TOOLS_INITIALIZATION_FAILED": ("内置工具没有准备好", "部分内置技能或工具未能加载，暂时无法完成准备。"),
        "QUALITY_GATE_FAILED": ("本地能力检查未通过", "部分组件还不能正常使用，需要修复后重新检查。"),
        "FEATURE_SMOKE_TEST_FAILED": ("功能验证未通过", "采集、提炼、咨询或创作的试运行未能完成。"),
    }
    if code in messages:
        title, message = messages[code]
        if code == "MODEL_DOWNLOAD_FAILED" and stage_id in {"capture_model", "vector_model"}:
            label = "语义检索模型" if stage_id == "vector_model" else "采集提炼模型"
            title, message = label + "还没有准备好", label + "未能完成下载或检查。"
    elif code.startswith("DATABASE_") or code in {"CORE_SERVICE_UNAVAILABLE", "CORE_PORT_CONFLICT"}:
        title, message = "本地记忆库暂时无法使用", "记忆库的打开或检查未能完成。"
    else:
        title, message = "有一项准备没有完成", "准备过程中遇到问题，暂时无法继续。"
    return title, message, None
