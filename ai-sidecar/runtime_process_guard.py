"""
推理运行时进程守卫 —— 保证全局最多 1 个 llama-server。

背景：MemoryBread 托管的 Ollama 运行时（~/.memory-bread/initialization/runtime/
ollama/v*/runtime/）会以子进程形式为每个驻留模型拉起 llama-server。历史上多次
出现宿主 ollama serve 被 SIGKILL 或应用重启后，llama-server 子进程变孤儿
（ppid=1）继续驻留 4~5GB 内存，累积多个实例后挤占内存并拖慢推理吞吐。

本模块提供无状态的进程巡检与清理：
1. 孤儿清理：父进程已不是存活的托管 ollama serve 的 llama-server 一律终止
   （包括没有任何托管 serve 存活时的独苗孤儿）；
2. 多实例收敛：存活的 llama-server 超过 1 个时只保留最新一个；
3. 多宿主收敛：存活的托管 ollama serve 超过 1 个时只保留最老（最稳定）一个。

调用方：
- background_processor 主循环周期性巡检；
- model_manager 重启/修复 Ollama 前后调用；
- start.sh 启动前调用（bash 侧有等价实现）。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 托管运行时的路径特征：只管理 MemoryBread 自己拉起的进程，
# 绝不误杀用户自行安装的 Ollama / llama.cpp。
_MANAGED_RUNTIME_MARKER = os.path.join("initialization", "runtime", "ollama")
# 首次初始化沙箱使用独立随机端口与临时身份，生命周期由 initialization_manager
# 自行管理，守卫不介入，避免误杀质检阶段的合法进程。
_SANDBOX_MARKER = "initialization-sandbox"
_KILL_GRACE_SECONDS = 5.0


class _Proc:
    __slots__ = ("pid", "ppid", "etime_seconds", "command")

    def __init__(self, pid: int, ppid: int, etime_seconds: int, command: str) -> None:
        self.pid = pid
        self.ppid = ppid
        self.etime_seconds = etime_seconds
        self.command = command


def _parse_etime(raw: str) -> int:
    """解析 ps etime 字段：[[DD-]HH:]MM:SS -> 秒数；解析失败返回 -1。"""
    text = raw.strip()
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return -1
    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        return -1
    try:
        values = [int(part) for part in parts]
    except ValueError:
        return -1
    hours = minutes = seconds = 0
    if len(values) == 3:
        hours, minutes, seconds = values
    elif len(values) == 2:
        minutes, seconds = values
    else:
        seconds = values[0]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _list_processes() -> List[_Proc]:
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        logger.warning("runtime guard: ps 调用失败，跳过巡检: %s", exc)
        return []
    procs = []  # type: List[_Proc]
    for line in result.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        procs.append(_Proc(pid, ppid, _parse_etime(parts[2]), parts[3]))
    return procs


def _is_managed_llama_server(proc: _Proc) -> bool:
    if _MANAGED_RUNTIME_MARKER not in proc.command or _SANDBOX_MARKER in proc.command:
        return False
    head = proc.command.split(None, 1)[0] if proc.command else ""
    return os.path.basename(head) == "llama-server"


def _is_managed_ollama_serve(proc: _Proc) -> bool:
    if _MANAGED_RUNTIME_MARKER not in proc.command or _SANDBOX_MARKER in proc.command:
        return False
    tokens = proc.command.split()
    if not tokens:
        return False
    return os.path.basename(tokens[0]) == "ollama" and "serve" in tokens[1:]


def _terminate(pid: int) -> None:
    """先礼后兵：SIGTERM 宽限等待后 SIGKILL。"""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        logger.warning("runtime guard: 无权限终止进程 %s: %s", pid, exc)
        return
    deadline = time.monotonic() + _KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
        logger.warning("runtime guard: 进程 %s 未在宽限期内退出，已 SIGKILL", pid)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        logger.warning("runtime guard: 无权限 SIGKILL 进程 %s: %s", pid, exc)


def _kill_tree(root_pid: int, procs: List[_Proc]) -> List[int]:
    """终止进程及其全部后代（先子后父），返回被终止的 pid 列表。"""
    children = {}  # type: Dict[int, List[int]]
    for proc in procs:
        children.setdefault(proc.ppid, []).append(proc.pid)

    killed = []  # type: List[int]

    def walk(pid: int) -> None:
        for child in children.get(pid, []):
            walk(child)
        _terminate(pid)
        killed.append(pid)

    walk(root_pid)
    return killed


def enforce_runtime_guards(reason: str = "") -> Dict[str, List[int]]:
    """执行单实例收敛，返回 {"killed_runners": [...], "killed_serves": [...]}。

    只处理带托管运行时路径特征的进程；对用户自装的 Ollama 不做任何操作。
    """
    summary = {"killed_runners": [], "killed_serves": []}  # type: Dict[str, List[int]]
    procs = _list_processes()
    if not procs:
        return summary

    servers = [p for p in procs if _is_managed_llama_server(p)]
    serves = [p for p in procs if _is_managed_ollama_serve(p)]
    serve_pids = set(p.pid for p in serves)  # type: set
    orphan_count = len([p for p in servers if p.ppid not in serve_pids])
    # 收敛目标：serve ≤ 1；由存活 serve 托管的 runner ≤ 1；孤儿 runner = 0。
    if len(serves) <= 1 and orphan_count == 0 and len(servers) - orphan_count <= 1:
        return summary

    logger.warning(
        "runtime guard 触发（%s）：发现 %d 个 llama-server / %d 个 ollama serve，开始收敛",
        reason or "unknown", len(servers), len(serves),
    )

    # 1. 多宿主收敛：保留运行时间最长的 ollama serve，其余连同子树终止。
    if len(serves) > 1:
        serves.sort(key=lambda p: p.etime_seconds, reverse=True)
        for victim in serves[1:]:
            summary["killed_serves"].extend(_kill_tree(victim.pid, procs))

    alive_pids = set()  # type: set
    for proc in _list_processes():
        if _is_managed_ollama_serve(proc):
            alive_pids.add(proc.pid)

    # 2. runner 收敛：父进程不是存活托管 serve 的一律视为孤儿；
    #    其余多于 1 个时只保留最新（etime 最小）的一个。
    refreshed = [p for p in _list_processes() if _is_managed_llama_server(p)]
    orphans = [p for p in refreshed if p.ppid not in alive_pids]
    hosted = [p for p in refreshed if p.ppid in alive_pids]
    hosted.sort(key=lambda p: p.etime_seconds)
    for victim in orphans + hosted[1:]:
        _terminate(victim.pid)
        summary["killed_runners"].append(victim.pid)

    if summary["killed_runners"] or summary["killed_serves"]:
        logger.warning(
            "runtime guard 收敛完成：终止孤儿/多余 llama-server %s，终止多余 ollama serve %s",
            summary["killed_runners"], summary["killed_serves"],
        )
    return summary


def shutdown_all_managed_serves(reason: str = "") -> List[int]:
    """终止全部托管 ollama serve 及其子树（升级/重启前调用）。

    替代历史上会造成 llama-server 孤儿的 `pkill -9 ollama`：宿主被 SIGKILL
    后子 runner 会 reparent 到 launchd 继续驻留内存。
    """
    killed = []  # type: List[int]
    serves = [p for p in _list_processes() if _is_managed_ollama_serve(p)]
    if serves:
        logger.info("runtime guard: 关闭 %d 个托管 ollama serve（%s）", len(serves), reason or "unknown")
        procs = _list_processes()
        for serve in serves:
            killed.extend(_kill_tree(serve.pid, procs))
    # 兜底清扫提前逃逸的孤儿 runner，保证重启后全局从 0 个 llama-server 开始。
    summary = enforce_runtime_guards(reason="post-shutdown sweep: " + (reason or "unknown"))
    killed.extend(summary["killed_runners"])
    killed.extend(summary["killed_serves"])
    return killed


def kill_ollama_tree_gracefully(ollama_pid: Optional[int]) -> None:
    """终止指定 ollama serve 及其 llama-server 子进程（修复/重启前调用）。"""
    if not ollama_pid:
        return
    procs = _list_processes()
    _kill_tree(ollama_pid, procs)
    # 兜底：清理可能提前逃逸的托管 runner。
    enforce_runtime_guards(reason="post-restart sweep")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = enforce_runtime_guards(reason="manual sweep")
    print(result)


if __name__ == "__main__":
    main()
