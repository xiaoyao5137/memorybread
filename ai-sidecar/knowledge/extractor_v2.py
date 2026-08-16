"""
时间线提炼与 bake 提炼模块 V2 - 强制使用 LLM，支持去重和出现次数统计
"""

from __future__ import annotations

import ast
import json
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable, Tuple
from datetime import datetime
import numpy as np

logger = logging.getLogger(__name__)

# 烘焙统一保留模型完整的 32K 上下文窗口；输出预算独立限制为 8K。
# 现网正常 bundle 输出最大约 4.7K，8K 留足余量，同时阻止重复字段把
# completion 撑满整个上下文窗口。
BAKE_CONTEXT_WINDOW_TOKENS = 32768
BAKE_NUM_PREDICT = 8192
# 紧凑重试虽然会压缩输入，但候选本身需要长输出的场景（截断重试）不应再
# 被更低的输出上限二次截断；32768 上下文仍可容纳 18k 输入 + 8192 输出。
BAKE_RETRY_NUM_PREDICT = 8192
BAKE_PROMPT_SAFETY_TOKENS = 1024
BAKE_INPUT_TOKEN_BUDGET = (
    BAKE_CONTEXT_WINDOW_TOKENS - BAKE_NUM_PREDICT - BAKE_PROMPT_SAFETY_TOKENS
)
BAKE_RETRY_INPUT_TOKEN_BUDGET = 18_000
BAKE_RETRY_REPEAT_PENALTY = 1.15
BAKE_TIMEOUT_RETRY_INPUT_TOKEN_BUDGET = 12_000
BAKE_TIMEOUT_RETRY_NUM_PREDICT = 4096
BAKE_TIMEOUT_RETRY_REPEAT_PENALTY = 1.20
# estimate_tokens 对中文结构化 prompt 会低估约 20%-30%；预算判断使用保守倍率，
# 实际用量仍以 Ollama 返回的 prompt_eval_count 为准。
BAKE_TOKEN_ESTIMATE_SAFETY_FACTOR = 1.35
BAKE_CAPTURE_AX_MAX_CHARS = 16_000
BAKE_CAPTURE_CONTEXT_MAX_CHARS = 20_000
BAKE_DOCUMENT_MERGE_EXISTING_CONTEXT_MAX_CHARS = 24_000
BAKE_DOCUMENT_MERGE_CANDIDATE_CONTEXT_MAX_CHARS = 32_000
BAKE_ERROR_LOG_PATH = Path.home() / ".memory-bread" / "logs" / "bake_extract_errors.log"

_DOCUMENT_IDENTITY_LIST_FIELDS = (
    "aliases",
    "entity_aliases",
    "product_names",
    "project_names",
)
_DOCUMENT_IDENTITY_COVERAGE_FIELDS = (
    "title",
    "summary",
    "full_content",
    "tags",
    "aliases",
    "entity_aliases",
    "product_names",
    "project_names",
    "entities",
    "structured_content",
    "prompt_hint",
)
_DOCUMENT_IDENTITY_SUFFIXES = (
    "系统",
    "平台",
    "产品",
    "项目",
    "工具",
    "应用",
    "服务",
    "引擎",
    "模块",
    "计划",
    "专项",
)
_DOCUMENT_IDENTITY_GENERIC_TERMS = frozenset(
    {
        "系统",
        "平台",
        "产品",
        "项目",
        "工具",
        "应用",
        "服务",
        "引擎",
        "模块",
        "计划",
        "专项",
        "文档",
        "页面",
        "功能",
        "版本",
    }
)

# RAG 查询优先锁:model_api_server 在 RAG 调用期间持有此文件锁。
# 时间线提炼在调 LLM 前非阻塞 acquire；拿不到则跳过本轮，让 RAG 优先完成。
_RAG_LOCK_FILE = "/tmp/memory-bread-rag.lock"

# 相似度去重的增长上限：与 background_processor 的 _TIMELINE_MAX_* 保持一致。
# 已膨胀到上限的时间线不再作为合并候选，从源头避免宽泛主题时间线成为"垃圾桶"。
# 上限需宽松：短时高频采集（同一 2 小时内上百条）是正常时间线，真正的异常
# 特征是"跨天合并"，由跨度上限兜底。
_SIMILAR_MERGE_MAX_OCCURRENCE = 200
_SIMILAR_MERGE_MAX_MEMBER_COUNT = 500
_SIMILAR_MERGE_MAX_SPAN_HOURS = 24.0
# 实体重叠加分上限：堵住"实体加分把低相似度候选推过阈值"的漏洞。
_SIMILAR_ENTITY_BONUS_MAX = 0.03
# 相似度去重的基础阈值（作用于未含实体加分的基础余弦相似度）。
# 历史上使用 0.72，曾导致两个措辞相近但主题不同的任务
# （"排查 MemoryBread ID1230 低价值数据" vs "排查时间线 2148 + 创作润色死循环"，
# 余弦相似度约 0.76）被误合并，把 21 条与主题无关的采集记录并入同一条时间线
# （timeline 2713 脏数据）。阈值提高到 0.80，并叠加实体一致性门，避免
# "同一项目里的不同任务"仅凭 overview 措辞相似就被并成一条时间线。
_SIMILAR_MERGE_THRESHOLD = 0.80
# 基础相似度达到该值时视为近乎重复的同一事件，可直接合并，不再要求实体交集。
_SIMILAR_MERGE_NEAR_DUP_THRESHOLD = 0.86

# 确定性丢弃标记：LLM 已明确判定片段无价值或提炼质量不足时返回该结构。
# 调用方据此把 captures 标记为已消费，避免同一批低价值采集被无限重提炼；
# 抢占/RAG 活跃/JSON 解析失败等临时性失败仍返回 None，保留重试机会。
_DISCARDED_KEY = '_discarded'


def discarded_knowledge(reason: str) -> Dict[str, Any]:
    return {_DISCARDED_KEY: True, 'discard_reason': reason}


class BakeOutputError(RuntimeError):
    code = "BAKE_OUTPUT_INVALID"
    retryable = True
    scope = "candidate"
    http_status = 422
    public_message = "烘焙输出不符合结构要求"


class BakeOutputTruncatedError(BakeOutputError):
    code = "BAKE_OUTPUT_TRUNCATED"


class BakeModelRequestError(RuntimeError):
    """保留模型 HTTP 分类，让返回的 5xx 进入候选有界重试而非永久卡头。"""

    def __init__(self, status_code: int, response_body: str = ""):
        self.status_code = int(status_code)
        self.response_body = str(response_body or "")[:2_000]
        self.scope = "candidate"
        if self.status_code == 429:
            self.code = "MODEL_RATE_LIMITED"
            self.retryable = True
            self.scope = "service"
            self.http_status = 503
            self.public_message = "本地模型当前繁忙，请稍后重试"
        elif self.status_code in {401, 403, 404}:
            # 鉴权失败或模型不存在是运行环境配置问题，对每条候选重试都不会成功；
            # 保留队列等待服务恢复，避免把所有候选逐条误判为坏数据并丢弃。
            self.code = "MODEL_UNAVAILABLE"
            self.retryable = True
            self.scope = "service"
            self.http_status = 503
            self.public_message = "本地模型服务配置不可用"
        elif self.status_code in {408, 504}:
            self.code = "INFERENCE_TIMEOUT"
            self.retryable = True
            self.http_status = 504
            self.public_message = "本地模型请求超时，请稍后重试"
        elif self.status_code >= 500:
            self.code = "BAKE_MODEL_UPSTREAM_ERROR"
            self.retryable = True
            self.http_status = 502
            self.public_message = "本地模型执行烘焙请求失败"
        else:
            self.code = "BAKE_MODEL_REQUEST_INVALID"
            self.retryable = False
            self.http_status = 422
            self.public_message = "本地模型拒绝了烘焙请求"
        super().__init__(f"本地模型请求失败（HTTP {self.status_code}）")


class BakeModelTransportError(RuntimeError):
    """只有无法连接模型服务才属于服务级错误，不由单个候选消耗重试次数。"""

    code = "MODEL_UNAVAILABLE"
    retryable = True
    scope = "service"
    http_status = 503
    public_message = "本地模型服务暂不可用"


class BakeInferenceTimeoutError(RuntimeError):
    """推理超时与候选内容及输出规模相关，必须走候选有界重试。"""

    code = "INFERENCE_TIMEOUT"
    retryable = True
    scope = "candidate"
    http_status = 504
    public_message = "本地模型请求超时，请稍后重试"


class BakeModelResponseError(BakeOutputError):
    code = "BAKE_MODEL_RESPONSE_INVALID"
    public_message = "本地模型返回了无法解析的响应"


def _rag_is_active() -> bool:
    """非阻塞检测 RAG 查询是否正在占用 Ollama。True 表示忙，提炼应跳过本轮。"""
    import fcntl
    try:
        fd = open(_RAG_LOCK_FILE, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
        return False  # 成功拿到锁 → RAG 不在跑
    except (IOError, OSError):
        return True   # 拿不到锁 → RAG 正在占用 Ollama


_JSON_STRING_CLOSERS = set(':,}] \t\r\n')

# 思考块闭合标签（拼接构造，避免源码被模板处理器误伤）。
_THINK_CLOSE_TAG = "<" + "/think>"


def _json_repair_round(text: str) -> str:
    """单遍组合修复：游离引号、字符串外过度转义、字符串内字面换行。

    小模型输出的缺陷常交织出现（同一文本里既有未转义的游离引号，又有
    字符串外 `\\"` 过度转义，还有字面换行）。拆成多个独立扫描器会互相
    干扰，这里合并为单遍扫描：字符串内的 `\\"` 用前瞻判断真实语义，
    后面紧跟 JSON 结构字符才是转义引号收尾，否则属于过度转义，去掉
    反斜杠后保留为字符串内容。

    注意：本函数不幂等（第一轮把游离引号转义成 `\\"`，第二轮会把
    它们当过度转义改回去），调用方只能应用一轮，靠多策略变体覆盖
    歧义场景，不能迭代反复应用。
    """
    out: List[str] = []
    in_string = False
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if in_string:
            if ch == '\\' and idx + 1 < length:
                nxt = text[idx + 1]
                if nxt == '"':
                    j = idx + 2
                    while j < length and text[j] in ' \t\r\n':
                        j += 1
                    if j >= length or text[j] in _JSON_STRING_CLOSERS:
                        # `\"` 后接结构字符：模型想在这里结束字符串。
                        # 合法 JSON 的字符串闭合只能是裸引号（`\"` 永远属于
                        # 字符串内容），所以去掉多余反斜杠后用裸引号闭合。
                        in_string = False
                        out.append('"')
                    else:
                        # 过度转义：`\"` 后仍是正文，去反斜杠保内容。
                        out.append('"')
                    idx += 2
                    continue
                out.append(ch)
                out.append(nxt)
                idx += 2
                continue
            if ch == '"':
                j = idx + 1
                while j < length and text[j] in ' \t\r\n':
                    j += 1
                if j >= length or text[j] in _JSON_STRING_CLOSERS:
                    in_string = False
                    out.append(ch)
                else:
                    out.append('\\"')
                idx += 1
                continue
            if ch == '\n':
                out.append('\\n')
            elif ch == '\r':
                out.append('\\r')
            elif ch == '\t':
                out.append('\\t')
            else:
                out.append(ch)
            idx += 1
            continue
        if ch == '\\' and idx + 1 < length:
            nxt = text[idx + 1]
            if nxt == '"':
                # 字符串外不存在转义语境，`\"` 还原为字符串开始的普通引号。
                out.append('"')
                in_string = True
                idx += 2
                continue
            if nxt in 'nrt':
                # 模型把 `\n` 等转义序列写在结构层当格式化空白，
                # JSON 结构层不接受转义序列，还原为真实空白字符。
                out.append({'n': '\n', 'r': '\r', 't': '\t'}[nxt])
                idx += 2
                continue
        if ch == '"':
            in_string = True
        out.append(ch)
        idx += 1
    return ''.join(out)


def _json_repair_round_openers(text: str) -> str:
    """宽松变体：在默认修复基础上，字符串外的 `\\"` 一律当字符串起始引号。

    与 `_json_repair_round` 的区别：后者在字符串内遇到 `\\"` 后接结构
    字符时会认为字符串闭合；当模型对整个数组元素过度转义（如
    `[\\"creation\\", \\"planning\\"]`）时，闭合判断会提前截断字符串。
    本变体改为：只有裸 `"` 才能闭合字符串，`\\"` 永远属于字符串内容，
    字符串外的 `\\"` 还原为普通引号。两种口径都试，总有一种能收敛。
    """
    out: List[str] = []
    in_string = False
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if in_string:
            if ch == '\\' and idx + 1 < length:
                out.append(ch)
                out.append(text[idx + 1])
                idx += 2
                continue
            if ch == '"':
                j = idx + 1
                while j < length and text[j] in ' \t\r\n':
                    j += 1
                if j >= length or text[j] in _JSON_STRING_CLOSERS:
                    in_string = False
                    out.append(ch)
                else:
                    out.append('\\"')
                idx += 1
                continue
            if ch == '\n':
                out.append('\\n')
            elif ch == '\r':
                out.append('\\r')
            elif ch == '\t':
                out.append('\\t')
            else:
                out.append(ch)
            idx += 1
            continue
        if ch == '\\' and idx + 1 < length:
            nxt = text[idx + 1]
            if nxt == '"':
                out.append('"')
                in_string = True
                idx += 2
                continue
            if nxt in 'nrt':
                out.append({'n': '\n', 'r': '\r', 't': '\t'}[nxt])
                idx += 2
                continue
        if ch == '"':
            in_string = True
        out.append(ch)
        idx += 1
    return ''.join(out)


def _strip_trailing_commas(text: str) -> str:
    """删除字符串外部的尾逗号（`,` 后直接跟 `}` / `]`）。

    小模型常在最后一个字段后多写一个逗号，json.loads 与 ast.literal_eval
    都不接受，需要在解析前去掉。
    """
    out: List[str] = []
    in_string = False
    escape = False
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            out.append(ch)
            idx += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            idx += 1
            continue
        if ch == ',':
            nxt = idx + 1
            while nxt < length and text[nxt] in ' \t\r\n':
                nxt += 1
            if nxt < length and text[nxt] in '}]':
                idx += 1
                continue
        out.append(ch)
        idx += 1
    return ''.join(out)


def _quote_bare_array_items(text: str) -> str:
    """给数组内未加引号的裸文本元素补上引号（最后兑底变体）。

    小模型会输出 `"steps": [1. 发布通告…\\n2. 组织人员…]` 这类裸文本
    数组元素。仅在所有常规修复都失败后才尝试，避免误伤合法的数字数组。
    """
    out: List[str] = []
    in_string = False
    escape = False
    stack: List[str] = []
    expect_value = False
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            out.append(ch)
            idx += 1
            continue
        if ch == '"':
            in_string = True
            expect_value = False
            out.append(ch)
            idx += 1
            continue
        if ch in '{[':
            stack.append(ch)
            expect_value = (ch == '[')
            out.append(ch)
            idx += 1
            continue
        if ch in '}]':
            if stack:
                stack.pop()
            expect_value = False
            out.append(ch)
            idx += 1
            continue
        if ch == ',':
            if stack and stack[-1] == '[':
                expect_value = True
            out.append(ch)
            idx += 1
            continue
        if ch in ' \t\r\n':
            out.append(ch)
            idx += 1
            continue
        if expect_value and stack and stack[-1] == '[':
            # 捕获裸元素直到同层的 ',' 或 ']'（裸元素内部可能包含嵌套结构）。
            j = idx
            buf: List[str] = []
            bare_in_string = False
            bare_escape = False
            depth = 0
            while j < length:
                cj = text[j]
                if bare_in_string:
                    if bare_escape:
                        bare_escape = False
                    elif cj == '\\':
                        bare_escape = True
                    elif cj == '"':
                        bare_in_string = False
                    buf.append(cj)
                    j += 1
                    continue
                if cj == '"':
                    bare_in_string = True
                    buf.append(cj)
                    j += 1
                    continue
                if cj in '{[':
                    depth += 1
                    buf.append(cj)
                    j += 1
                    continue
                if cj in '}]':
                    if depth > 0:
                        depth -= 1
                        buf.append(cj)
                        j += 1
                        continue
                    break
                if cj == ',' and depth == 0:
                    break
                buf.append(cj)
                j += 1
            item = ''.join(buf).strip()
            if item:
                escaped = (
                    item.replace('\\', '\\\\')
                    .replace('"', '\\"')
                    .replace('\n', '\\n')
                    .replace('\r', '\\r')
                    .replace('\t', '\\t')
                )
                out.append('"' + escaped + '"')
            idx = j
            expect_value = False
            continue
        out.append(ch)
        idx += 1
    return ''.join(out)


def _collect_json_repair_variants(candidate: str) -> List[str]:
    """生成多策略修复变体，逐个交给 json.loads 尝试。

    现网失败样本常同时带多种缺陷（游离引号、字面换行、`\\"` 过度转义
    交织），且引号语义存在局部不可判定的歧义，单一启发式必有盲区。
    因此不做迭代修复（修复函数不幂等，迭代会震荡），而是并行生成
    多个策略变体：closer 前瞻、保留转义口径、全局去反斜杠后再修，
    叠加全角引号归一与尾逗号清理，总有一种能收敛。
    """
    normalized = (
        candidate
        .replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )
    # 模型会在同一段输出里混用 `\\n` 两字符转义序列与字面换行，甚至
    # 输出 `\\<字面换行>`、`\\\\<字面换行>` 这类非法序列。按“反斜杠
    # 后面紧跟字面换行”的非法程度归一：奇数个反斜杠时末尾反斜杠非法，
    # 直接换成 `n`；偶数个反斜杠时反斜杠都已配对，字面换行本身非法，
    # 补一对 `\\n`。归一后文本不再含字面换行歧义，后续字符串状态扫描
    # 才不会被误读。
    def _normalize_illegal_newline_escapes(text: str) -> str:
        return re.sub(
            r"\\+\n",
            lambda m: ((m.group(0)[:-1] + "n")
                       if len(m.group(0)) % 2 == 0
                       else (m.group(0)[:-1] + "\\n")),
            text,
        )

    newline_fixed = _normalize_illegal_newline_escapes(candidate)
    bases = [candidate]
    if normalized != candidate:
        bases.append(normalized)
    if newline_fixed != candidate:
        bases.append(newline_fixed)
        fixed_normalized = (
            newline_fixed
            .replace("“", '"')
            .replace("”", '"')
            .replace("‘", "'")
            .replace("’", "'")
        )
        if fixed_normalized not in bases:
            bases.append(fixed_normalized)

    families: List[str] = []
    for base in bases:
        families.append(base)
        families.append(_json_repair_round(base))
        families.append(_json_repair_round_openers(base))
        # 全局去掉引号前的反斜杠：专治对整个数组元素过度转义
        # （如 `[\\"creation\\", \\"planning\\"]`）导致前瞻判断失效的场景。
        stripped = base.replace('\\"', '"')
        if stripped != base:
            families.append(stripped)
            families.append(_json_repair_round(stripped))

    variants: List[str] = []
    seen = set()

    def emit(text: str) -> None:
        for fixed in (text, _strip_trailing_commas(text)):
            if fixed and fixed not in seen:
                seen.add(fixed)
                variants.append(fixed)

    for family in families:
        emit(family)
    return variants


def _try_parse_json_like_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    candidate = text.strip()
    if not candidate:
        return None

    variants = _collect_json_repair_variants(candidate)
    # 裸数组元素补引号只作为最后兑底：会把合法数字数组变成字符串数组，
    # 但走到这一步时输出已经无法解析，保底拿到结构化结果优于丢弃候选。
    bare_quoted = _quote_bare_array_items(candidate)
    if bare_quoted != candidate:
        variants.extend(_collect_json_repair_variants(bare_quoted))

    for variant in variants:
        for parser in (json.loads, ast.literal_eval):
            try:
                value = parser(variant)
            except (json.JSONDecodeError, SyntaxError, ValueError):
                continue
            if isinstance(value, dict):
                return value

    return None


def _try_parse_json_like_value(text: str) -> Optional[Any]:
    """Parse one isolated JSON value with the same repairs as bundle parsing."""
    candidate = str(text or '').strip()
    if not candidate:
        return None
    for variant in _collect_json_repair_variants(candidate):
        for parser in (json.loads, ast.literal_eval):
            try:
                value = parser(variant)
            except (json.JSONDecodeError, SyntaxError, ValueError):
                continue
            if isinstance(value, (dict, list)):
                return value
    return None


def _extract_named_json_value(text: str, key: str) -> Optional[Any]:
    """Recover a complete top-level artifact without parsing sibling artifacts.

    Bundle output is ordered so SOP appears before the two long-form assets. A
    malformed knowledge/details string later in the response must therefore not
    erase an already complete SOP object.
    """
    raw = str(text or '')
    match = re.search(rf'["\']{re.escape(key)}["\']\s*:', raw)
    if not match:
        return None
    index = match.end()
    while index < len(raw) and raw[index].isspace():
        index += 1
    if index >= len(raw) or raw[index] not in '{[':
        return None

    opening = raw[index]
    closing = '}' if opening == '{' else ']'
    depth = 0
    in_string = False
    escape = False
    for cursor in range(index, len(raw)):
        char = raw[cursor]
        if in_string:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return _try_parse_json_like_value(raw[index:cursor + 1])
    return None


def _recover_bake_bundle_artifacts(raw: str) -> Dict[str, Any]:
    recovered: Dict[str, Any] = {}
    for key in ('classification', 'sop', 'knowledge', 'design'):
        value = _extract_named_json_value(raw, key)
        if value is not None:
            recovered[key] = value
    return recovered


def _scan_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    """括号配对扫描，取第一个平衡的 `{...}` 片段再做容错解析。"""
    start = text.find('{')
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                candidate = text[start:idx + 1]
                return _try_parse_json_like_object(candidate)

    return None


def _salvage_truncated_json(text: str) -> Optional[Dict[str, Any]]:
    """从被截断的输出中抢救最长的可闭合前缀。

    截断（done_reason=length）或重复退化会让 JSON 在半路断开。重复退化
    场景下前缀通常已包含完整的首个产物块，把最近的完整值边界之后截断、
    补齐未闭合括号即可得到可用结果，避免候选被误判终态。
    """
    candidate = str(text or '').strip()
    if not candidate:
        return None
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
    start = candidate.find('{')
    if start == -1:
        return None
    candidate = candidate[start:]

    stack: List[str] = []
    in_string = False
    escape = False
    good_positions: List[int] = []
    for idx, ch in enumerate(candidate):
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
                if stack:
                    good_positions.append(idx + 1)
            continue
        if ch == '"':
            in_string = True
        elif ch in '{[':
            stack.append(ch)
        elif ch in '}]':
            if stack:
                stack.pop()
            if stack:
                good_positions.append(idx + 1)

    for position in reversed(good_positions[-6:]):
        prefix = candidate[:position].rstrip()
        while prefix.endswith(','):
            prefix = prefix[:-1].rstrip()
        # 前缀末尾如果是悬空的 `:`（键后断流），无法闭合，试更早的边界。
        if prefix.endswith(':'):
            continue
        open_stack: List[str] = []
        in_string = False
        escape = False
        for ch in prefix:
            if in_string:
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in '{[':
                open_stack.append(ch)
            elif ch in '}]':
                if open_stack:
                    open_stack.pop()
        if in_string or not open_stack:
            continue
        closers = ''.join('}' if c == '{' else ']' for c in reversed(open_stack))
        parsed = _try_parse_json_like_object(prefix + closers)
        if parsed is not None:
            return parsed
    return None


def _extract_json_object(raw: Any) -> Optional[Dict[str, Any]]:
    """尽量从 LLM 输出中提取第一个合法 JSON 对象。"""
    if raw is None:
        return None

    text = str(raw).strip()
    if not text:
        return None

    fragments: List[str] = [text]
    if text.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", text)
        stripped = re.sub(r"\s*```$", "", stripped)
        if stripped and stripped != text:
            fragments.append(stripped)
    # 小模型会先在 think 思考块里复述一遍 JSON，再在围栏里输出正文；
    # think 内的裸 JSON 带字面换行会抢先命中括号扫描导致整体解析失败，
    # 因此也要单独尝试思考块闭合标签之后的内容以及每个围栏块。
    if _THINK_CLOSE_TAG in text:
        tail = text.rsplit(_THINK_CLOSE_TAG, 1)[1].strip()
        if tail:
            fragments.append(tail)
    for match in re.finditer(r"```(?:json)?[ \t]*\r?\n(.*?)```", text, re.S):
        block = match.group(1).strip()
        if block:
            fragments.append(block)
        if len(fragments) >= 8:
            break

    for fragment in fragments:
        parsed = _try_parse_json_like_object(fragment)
        if parsed is not None:
            return parsed

    for fragment in fragments:
        parsed = _scan_first_json_object(fragment)
        if parsed is not None:
            return parsed

    # 截断抢救同样要先修复再扫描：原始文本里的游离引号/字面换行会让
    # 括号状态机误判字符串边界，找不到正确的闭合点。
    salvage_candidates: List[str] = []
    for fragment in fragments:
        salvage_candidates.append(fragment)
        repaired = _json_repair_round(fragment)
        if repaired != fragment:
            salvage_candidates.append(repaired)
    for candidate_text in salvage_candidates:
        parsed = _salvage_truncated_json(candidate_text)
        if parsed is not None:
            return parsed

    return None


def _stringify_response_fragment(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("content", "response", "text", "message", "thinking"):
            fragment = _stringify_response_fragment(value.get(key))
            if fragment.strip():
                return fragment
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)
    if isinstance(value, list):
        parts = [_stringify_response_fragment(item) for item in value]
        return "\n".join(part for part in parts if part.strip())
    return str(value)


def _extract_attr(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _extract_ollama_response_text(response: Dict[str, Any]) -> str:
    candidates = [
        _extract_attr(response, "message"),
        _extract_attr(response, "response"),
        _extract_attr(response, "content"),
        _extract_attr(response, "output"),
    ]
    for item in candidates:
        content = _extract_attr(item, "content")
        text = _stringify_response_fragment(content).strip()
        if text:
            return text

        direct_text = _extract_attr(item, "text")
        text = _stringify_response_fragment(direct_text).strip()
        if text:
            return text

        text = _stringify_response_fragment(item).strip()
        if text:
            return text
    return ""


def _preview_text(value: Any, limit: int = 500) -> str:
    text = _stringify_response_fragment(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ...(已截断)"


def _append_bake_error_log(message: str, **fields: Any) -> None:
    try:
        BAKE_ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "ts_ms": int(time.time() * 1000),
            "message": message,
            **fields,
        }
        with BAKE_ERROR_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str))
            fh.write("\n")
    except Exception as exc:
        logger.warning("写入 bake 提炼错误日志失败: %s", exc)


UI_NOISE_LINE_PATTERNS = (
    re.compile(r'^(file|edit|selection|view|go|run|terminal|window|help)(\s+\w+){0,10}$', re.IGNORECASE),
    re.compile(r'^(welcome|explorer|extensions?)$', re.IGNORECASE),
    re.compile(r'^[\d\s]{4,}$'),
    re.compile(r'^[=+\-_*~•·。，、…<>|/\\]{3,}$'),
)

UI_NOISE_KEYWORDS = {
    'file', 'edit', 'selection', 'view', 'go', 'run', 'terminal', 'window', 'help',
    'welcome', 'explorer', 'bash tool output', 'taskoutput tool output',
}

WORK_ACTION_KEYWORDS = (
    '修复', '排查', '实现', '更新', '重启', '验证', '提炼', '分析', '编写',
    '调试', '优化', '新增', '删除', '合并', 'review', '检查', '对齐',
)


def _normalize_inline_text(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '').replace('\r', ' ').replace('\n', ' ')).strip()


def _sanitize_capture_text(raw_text: str) -> str:
    lines = str(raw_text or '').replace('\r', '\n').split('\n')
    cleaned: List[str] = []
    prev = ''
    for line in lines:
        normalized = _normalize_inline_text(line)
        if not normalized:
            continue
        lowered = normalized.lower()
        if any(pattern.match(normalized) for pattern in UI_NOISE_LINE_PATTERNS):
            continue
        if lowered in UI_NOISE_KEYWORDS:
            continue
        if normalized == prev:
            continue
        cleaned.append(normalized)
        prev = normalized

    if cleaned:
        return '\n'.join(cleaned)
    return _normalize_inline_text(raw_text)


# ─────────────────────────────────────────────────────────────────────────
# 密度感知截断（修复 timeline 2008 类丢失）
#
# 旧逻辑每块固定 [:800]、总量尾部硬切，导致 IM 侧边栏噪声占满配额、
# 靠后的汇报正文被裁掉。新策略：
# 1. 按行/句段密度给块分配配额：密集正文块 3000，噪声块 800；
# 2. 总量超限时优先丢弃明确 UI 壳层行与连续短字孤立行；
# 3. 仍超限时从密度最低的块开始压缩，密集正文块最后才被截。
# 判定全部是确定性字符串统计，在 LLM 调用前完成，不增加推理负担；
# 判定不确定时偏向不切（宁可多送噪声，不可丢正文）。
# ─────────────────────────────────────────────────────────────────────────
MERGE_BLOCK_QUOTA_DENSE = 3000
MERGE_BLOCK_QUOTA_DEFAULT = 800
MERGE_COMPRESSED_QUOTA_LOW = 250
MERGE_TOTAL_MAX_CHARS = 6000
MERGE_BLOCK_SEPARATOR = "\n\n---\n\n"

UI_SHELL_SHORT_LINES = {
    '消息', '话题', '发送', '换行', '搜索', '设置', '收藏', '通讯录', '工作台',
    '联系人', '日历', '待办', '默认', '重要', '文件', '编辑', '视图', '帮助',
    '插入', '分享', '正文', '目录',
}

_SHORT_NOISE_LINE_MAX_CHARS = 5
_SHORT_NOISE_RUN_MIN = 5
_SHORT_LINE_KEEP_RE = re.compile(r'[%％¥￥]|\d\.\d|\+\d')


def _strip_pressure_noise_lines(text: str) -> str:
    """超长压力下剔除明确 UI 噪声行与连续短字孤立行。

    - 明确噪声：UI_NOISE_LINE_PATTERNS / UI_NOISE_KEYWORDS / UI_SHELL_SHORT_LINES；
    - 连续短字孤立行：连续 ≥5 条各自 ≤5 字的行（IM 侧边栏/联系人列表形态）
      整段剔除；含指标特征（%、¥、小数、+数字）的行不计入短行。
    聊天中"姓名+短回复"（如 吴垚/是的）连续短行通常 <5 条，不会被误剔。
    """
    kept: List[str] = []
    run: List[str] = []

    def flush() -> None:
        if len(run) < _SHORT_NOISE_RUN_MIN:
            kept.extend(run)
        del run[:]

    for line in str(text or '').split('\n'):
        normalized = _normalize_inline_text(line)
        if not normalized:
            continue
        lowered = normalized.lower()
        if (
            any(p.match(normalized) for p in UI_NOISE_LINE_PATTERNS)
            or lowered in UI_NOISE_KEYWORDS
            or normalized in UI_SHELL_SHORT_LINES
        ):
            continue
        if len(normalized) <= _SHORT_NOISE_LINE_MAX_CHARS and not _SHORT_LINE_KEEP_RE.search(normalized):
            run.append(normalized)
            continue
        flush()
        kept.append(normalized)
    flush()
    return '\n'.join(kept)


def _density_aware_truncate(text: str, quota: int) -> str:
    """单文本配额截断：先剔噪声行，再按密度分配实际保留长度。

    密集正文获得完整配额；噪声为主的文本压缩到一半配额。
    判定不确定（密度处于阈值边缘）时偏向多保留。
    截断时优先保留数值指标密集段，避免尾部指标被切掉。
    """
    stripped = _strip_pressure_noise_lines(text)
    if len(stripped) <= quota:
        return stripped
    from knowledge.fragment_grouper import text_density_score, DENSE_TEXT_THRESHOLD
    density = text_density_score(stripped)
    if density < DENSE_TEXT_THRESHOLD:
        quota = max(200, quota // 2)
    return _truncate_preserving_metrics(stripped, quota)


_NUMERIC_METRIC_LINE_RE = re.compile(r'\d+(?:\.\d+)?\s*[%％倍]|\d+\.\d+')


def _truncate_preserving_metrics(body: str, cap: int) -> str:
    """块截断保留数值指标：在预算内保留数值指标最密集的连续段落。

    汇报类正文的指标（87%、92.6、+1.99% 等）常位于块中后段，
    纯头部硬切会整体丢失；此处用滑动窗口找到指标行数最多的
    连续行段（指标优先，窗口可用整个 cap 预算），头部用剩余
    空间填充上下文；指标数相同时保留覆盖更长的段落。
    """
    if len(body) <= cap:
        return body
    lines = str(body).split('\n')
    metric_flags = [bool(_NUMERIC_METRIC_LINE_RE.search(ln)) for ln in lines]
    if not any(metric_flags):
        return body[:cap]
    tail_budget = max(cap, 150)
    best: Optional[Tuple[int, int, int, int]] = None  # (metric_count, span_chars, start, end)
    start = 0
    cur_chars = 0
    cur_metrics = 0
    for end in range(len(lines)):
        cur_chars += len(lines[end]) + 1
        if metric_flags[end]:
            cur_metrics += 1
        while start <= end and cur_chars > tail_budget:
            cur_chars -= len(lines[start]) + 1
            if metric_flags[start]:
                cur_metrics -= 1
            start += 1
        if cur_metrics > 0:
            key = (cur_metrics, cur_chars, start, end)
            if best is None or (key[0], key[1]) > (best[0], best[1]):
                best = key
    if best is None:
        return body[:cap]
    _, _, w_start, w_end = best
    window_text = '\n'.join(lines[w_start:w_end + 1])
    if w_start == 0:
        return window_text[:cap]
    head_budget = max(cap - len(window_text) - 1, 0)
    return body[:head_budget] + '\n' + window_text


def _overview_quality_reason(overview: str, source_text: str) -> Optional[str]:
    compact = _normalize_inline_text(overview)
    if not compact or len(compact) < 16:
        return 'overview_too_short'

    if '\n' in str(overview or ''):
        return 'overview_contains_newline'

    lowered = compact.lower()
    ui_hits = sum(1 for keyword in UI_NOISE_KEYWORDS if keyword in lowered)
    has_action = any(keyword in compact for keyword in WORK_ACTION_KEYWORDS)
    if ui_hits >= 2 and not has_action:
        return 'ui_noise_dominant'

    words = re.findall(r'[a-zA-Z]+|\d+', lowered)
    if words:
        noisy_terms = sum(
            1
            for word in words
            if word.isdigit() or word in UI_NOISE_KEYWORDS
        )
        if len(words) >= 10 and (noisy_terms / len(words)) >= 0.32 and not has_action:
            return 'ui_noise_ratio_high'

    source_compact = _normalize_inline_text(source_text)
    if source_compact and compact in source_compact and not has_action:
        return 'overview_is_raw_copy'

    return None


def _overview_to_summary(overview: str, max_len: int = 42) -> str:
    compact = _normalize_inline_text(overview)
    if not compact:
        return "工作片段"
    sentence = re.split(r'[。！？!?]', compact, maxsplit=1)[0].strip() or compact
    if len(sentence) <= max_len:
        return sentence
    return sentence[:max_len].rstrip() + "…"


MERGE_SYSTEM_PROMPT ="""你是一个工作片段提炼助手。以下是用户在一段连续时间内的屏幕采集记录（按时间顺序），它们属于同一个工作片段。

**你的任务**:先验证这些采集是否真的属于同一个工作任务；只有同一任务才提炼为一个完整的工作片段知识条目。

**提炼规则**:
0. **先做任务一致性判定**:
   - 时间接近、应用相同、窗口标题相同或出现相同 UI 菜单，都不能单独证明是同一任务
   - 切换应用本身也不能单独证明是不同任务；若不同工具中的正文明确围绕同一目标，可保持同组
   - 若采集实际包含两个及以上互不依赖的目标（例如代码排查、天气查询、无关消息流、另一段 AI 对话），`is_coherent=false`
   - `capture_groups` 必须把“采集ID全集”完整分区：每个 ID 恰好出现一次，不得遗漏、重复或编造；同一任务可包含不连续的采集 ID
   - 同一任务时 `is_coherent=true`，`capture_groups` 只包含一个覆盖全部 ID 的分组
1. 识别这段时间内用户在做的一件完整的事
2. **从工作内容中提炼工作项**:综合分析所有帧的内容，识别用户在做哪个项目/功能的工作
   - 从代码注释、函数名、文件路径、Git commit、文档标题、聊天主题等内容中提炼
   - 格式:"项目名-功能模块"（如"MemoryBread-时间线提炼优化"）或"项目名"（如"个人博客"）
   - 如果内容明确提到具体任务（如"修复 bug #123"），可以更具体（如"MemoryBread-修复排查步骤 bug"）
   - 如果无法从内容中识别，填写 null
3. **识别工作进度和状态**:从内容中推断当前工作的进展
   - work_status: "pending"（待启动）| "in_progress"（进行中）| "completed"（已完成）| "blocked"（阻塞）
   - work_progress: 具体进度描述（如"已完成核心逻辑"、"待其他团队协作"、"等待需求确认"）
4. 生成概述（50-150字）:描述做了什么、关键进展、结果，使用过去时态
5. 生成明细（200-500字）:
   - 保留有追溯价值的具体信息（代码逻辑、会议决策、学到的知识点）
   - 过滤掉 UI 操作、重复内容、无意义的切换记录
   - 不要堆砌原始文本，要提炼和归纳
6. 识别关键实体（人名、项目名、技术词汇）
7. 判断分类和重要性

**输出格式（JSON）**:
{
  "is_coherent": true,
  "coherence_reason": "所有采集围绕同一个工作目标",
  "capture_groups": [[101, 102, 103]],
  "work_item": "项目名或项目名-功能模块，如 'MemoryBread-时间线提炼优化'，无法识别时填 null",
  "work_status": "pending|in_progress|completed|blocked",
  "work_progress": "具体进度描述，如 '已完成核心逻辑，待集成测试'",
  "overview": "概述，50-150字，不含换行符",
  "details": "明细，200-500字，使用空格代替换行符",
  "entities": ["实体1", "实体2"],
  "category": "会议|文档|代码|聊天|学习|其他",
  "importance": 1-5,
  "history_view": true,
  "content_origin": "live_interaction|historical_content|document_reference|other",
  "activity_type": "meeting|coding|reading|chat|ask_ai|reviewing_history|other",
  "event_time_start": 1710000000000,
  "event_time_end": 1710003600000,
  "evidence_strength": "low|medium|high"
}

**注意补充判断**:
- **工作项识别示例**:
  * 代码文件 "extractor_v2.py" + 注释 "优化时间线提炼逻辑" → work_item: "MemoryBread-时间线提炼优化"
  * Git commit "fix: 修复排查步骤 bug" → work_item: "MemoryBread-修复排查步骤 bug"
  * 聊天记录讨论 "个人博客的评论功能需求" → work_item: "个人博客-评论功能"
  * 文档标题 "用户认证系统重构方案" → work_item: "用户认证系统-重构"
  * 如果只看到 "修复 bug"、"写代码" 等模糊描述，无法识别具体项目，填 null
- **工作进度识别示例**:
  * 看到 "TODO"、"开始实现" → work_status: "in_progress", work_progress: "刚开始开发"
  * 看到 "测试通过"、"已上线" → work_status: "completed", work_progress: "已完成并上线"
  * 看到 "等待"、"阻塞"、"依赖" → work_status: "blocked", work_progress: "等待其他团队协作"
  * 看到 "80% 完成"、"还剩最后一步" → work_status: "in_progress", work_progress: "已完成 80%"
- 如果用户今天在 IM/聊天/AI 工具里回看昨天、前天、更早的消息或历史对话，`history_view=true`
- `observed_at` 不需要输出，由系统记录当前片段结束时间
- `event_time_start/event_time_end` 只在内容明确提到事情发生时间时填写；不明确时返回 null
- 询问 Gemini/Claude/ChatGPT 等 AI 助手，通常可标为 `activity_type=ask_ai`
- 查看历史消息/历史会话，通常可标为 `activity_type=reviewing_history` 且 `content_origin=historical_content`
- 直接实时聊天或会议记录，通常 `content_origin=live_interaction`
- 证据弱、推断成分高时降低 `evidence_strength`

**重要性评分**:
- 5分:关键决策、重要会议纪要、核心代码逻辑
- 4分:项目进展、技术文档、重要沟通
- 3分:日常工作记录、一般文档
- 2分:简单操作记录
- 1分:无关紧要的内容

**注意**:输出必须是有效的 JSON，字符串中的引号要转义，不要包含未转义的换行符。
"""

BAKE_SHARED_PROMPT = """你在执行 bake pipeline 的类别特异提炼。输入是一条来自情节记忆/episodic memory 的候选工作片段。

目标不是泛泛总结，而是判断这条候选是否足以沉淀为某一类稳定资产。所有判断都必须保守:证据不足就 reject，不要为了凑产出而改写成看似合理的结果。

你会收到候选的 summary / overview / details / entities，以及关联 capture 的上下文文本。可以综合这些信息，但必须只基于输入证据，不要臆测。

**必须 reject 的情形（无论内容看起来多有价值）**:
- 内容仅是界面自动生成的历史操作元数据、变更日志壳或动态通知（例如"某某于X月X日更新了…"、"某某创建了…"、"某某评论了…"等格式），没有说明具体项目进度、工作状态、执行结果、结论或其他可复用事实
- 判断依据:capture_context 只有"[人名/角色] 于 [日期] [动词]了 [对象]"一类动作壳，且没有可独立理解的业务事实。若消息正文明确陈述了进度、结果或结论，不得仅因记录来自过去、他人、群聊或动态流而拒绝；必须保留真实主体与时间，不能冒充当前用户刚完成的工作

输出要求:
- 必须返回且只返回 1 个 JSON 对象
- 顶层字段固定且仅允许:accepted, reason, payload
- `accepted` 为 true 时，`payload` 必须符合该类别 schema
- `accepted` 为 false 时，`payload` 必须为 null，并用 `reason` 简要说明为什么不适合该类别
- 不要输出解释性前后缀，不要输出代码块，不要输出思考过程
- 只有各类别 schema 中明确标注为 Markdown 的字段可以包含 Markdown；必须作为 JSON 字符串输出，换行使用 `\n` 转义
- 其他字符串保持单行，避免换行和超长段落
- schema 外字段一律不要输出
- 输出前自检:结果必须能被 JSON 解析，且顶层只有 accepted/reason/payload 三个字段"""

BAKE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "accepted": {"type": "boolean"},
        "reason": {"type": ["string", "null"]},
        "payload": {"type": ["object", "null"]},
    },
    "required": ["accepted", "reason", "payload"],
    "additionalProperties": False,
}


def _bounded_string(max_length: int, *, nullable: bool = False) -> Dict[str, Any]:
    return {
        "type": ["string", "null"] if nullable else "string",
        "maxLength": max_length,
    }


def _bounded_string_array(max_items: int, item_max_length: int) -> Dict[str, Any]:
    return {
        "type": "array",
        "maxItems": max_items,
        "items": _bounded_string(item_max_length),
    }


def _artifact_response_schema(payload_schema: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "accepted": {"type": "boolean"},
            "reason": _bounded_string(240, nullable=True),
            "payload": {
                "type": ["object", "null"],
                **payload_schema,
            },
        },
        "required": ["accepted", "reason", "payload"],
        "additionalProperties": False,
    }


BAKE_KNOWLEDGE_PAYLOAD_SCHEMA = {
    "properties": {
        "summary": _bounded_string(160),
        "overview": _bounded_string(600, nullable=True),
        "details": _bounded_string(3_000),
        "entities": _bounded_string_array(16, 80),
        "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        "occurrence_count": {"type": "integer", "minimum": 1},
        "observed_at": {"type": ["integer", "null"]},
        "event_time_start": {"type": ["integer", "null"]},
        "event_time_end": {"type": ["integer", "null"]},
        "history_view": {"type": "boolean"},
        "content_origin": _bounded_string(40, nullable=True),
        "activity_type": _bounded_string(40, nullable=True),
        "evidence_strength": _bounded_string(16, nullable=True),
        "evidence_summary": _bounded_string(400, nullable=True),
        "match_score": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "match_level": _bounded_string(16, nullable=True),
        "review_status": _bounded_string(32, nullable=True),
    },
    "additionalProperties": False,
}

BAKE_DESIGN_PAYLOAD_SCHEMA = {
    "properties": {
        "name": _bounded_string(200),
        "category": _bounded_string(40, nullable=True),
        "summary": _bounded_string(500, nullable=True),
        "full_content": _bounded_string(8_000),
        "details": _bounded_string(2_000, nullable=True),
        "prompt_hint": _bounded_string(1_000, nullable=True),
        "status": _bounded_string(24, nullable=True),
        "tags": _bounded_string_array(16, 80),
        "applicable_tasks": _bounded_string_array(8, 80),
        "structure_sections": {
            "type": "array",
            "maxItems": 16,
            "items": {
                "type": "object",
                "properties": {
                    "title": _bounded_string(160),
                    "keywords": _bounded_string_array(12, 80),
                    "notes": _bounded_string(500, nullable=True),
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
        "style_phrases": _bounded_string_array(16, 160),
        "replacement_rules": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "from": _bounded_string(160),
                    "to": _bounded_string(160),
                },
                "required": ["from", "to"],
                "additionalProperties": False,
            },
        },
        "diagram_code": _bounded_string(2_000, nullable=True),
        "evidence_summary": _bounded_string(400, nullable=True),
        "match_score": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "match_level": _bounded_string(16, nullable=True),
        "review_status": _bounded_string(32, nullable=True),
    },
    "additionalProperties": False,
}

BAKE_SOP_PAYLOAD_SCHEMA = {
    "properties": {
        "summary": _bounded_string(200),
        "overview": _bounded_string(600, nullable=True),
        "source_title": _bounded_string(200, nullable=True),
        "trigger_keywords": _bounded_string_array(16, 80),
        "extracted_problem": _bounded_string(800, nullable=True),
        "steps": {
            "type": "array",
            "minItems": 3,
            "maxItems": 8,
            "items": _bounded_string(500),
        },
        "step_evidence": {
            "type": "array",
            "minItems": 3,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "step_index": {"type": "integer", "minimum": 1},
                    "capture_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 12,
                        "items": {"type": ["string", "integer"]},
                    },
                },
                "required": ["step_index", "capture_ids"],
                "additionalProperties": False,
            },
        },
        "confidence": _bounded_string(16, nullable=True),
        "evidence_summary": _bounded_string(400, nullable=True),
        "match_score": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "match_level": _bounded_string(16, nullable=True),
        "review_status": _bounded_string(32, nullable=True),
    },
    "required": ["summary", "steps", "step_evidence"],
    "additionalProperties": False,
}

BAKE_BUNDLE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "object",
            "properties": {
                "primary_type": {
                    "type": "string",
                    "enum": ["data", "knowledge", "document", "sop", "none"],
                },
                "reason": _bounded_string(400, nullable=True),
            },
            "required": ["primary_type", "reason"],
            "additionalProperties": False,
        },
        "sop": _artifact_response_schema(BAKE_SOP_PAYLOAD_SCHEMA),
        "knowledge": _artifact_response_schema(BAKE_KNOWLEDGE_PAYLOAD_SCHEMA),
        "design": _artifact_response_schema(BAKE_DESIGN_PAYLOAD_SCHEMA),
    },
    "required": ["classification", "knowledge", "design", "sop"],
    "additionalProperties": False,
}


def _compact_payload_schema(
    schema: Dict[str, Any],
    *,
    string_limit: int,
    array_limit: int,
) -> Dict[str, Any]:
    """递归收紧重试输出，避免小模型再次陷入超长字段或数组循环。"""
    compact = json.loads(json.dumps(schema))

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if "maxLength" in node:
            node["maxLength"] = min(int(node["maxLength"]), string_limit)
        if "maxItems" in node:
            node["maxItems"] = min(int(node["maxItems"]), array_limit)
        for value in node.values():
            if isinstance(value, dict):
                visit(value)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

    visit(compact)
    return compact


BAKE_COMPACT_BUNDLE_RESPONSE_SCHEMA = _compact_payload_schema(
    BAKE_BUNDLE_RESPONSE_SCHEMA,
    string_limit=1_200,
    array_limit=8,
)
BAKE_TIMEOUT_BUNDLE_RESPONSE_SCHEMA = _compact_payload_schema(
    BAKE_BUNDLE_RESPONSE_SCHEMA,
    string_limit=800,
    array_limit=6,
)


def _ollama_compatible_format(response_format: Any) -> Any:
    """生成本地模型 grammar 可稳定编译的传输 Schema。

    Ollama 会把 JSON Schema 的 ``maxLength`` 展开为 grammar 重复规则。多个
    2K-8K 的长文本字段会让 grammar 初始化直接返回 400；业务提示词与输出 token
    预算已经负责长度控制，因此传输层只移除该关键字，保留字段、类型、枚举、数值
    范围及数组上限等结构约束。
    """
    if not isinstance(response_format, dict):
        return response_format

    compatible = json.loads(json.dumps(response_format))

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("maxLength", None)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(compatible)
    return compatible

BAKE_MERGE_DOCUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "no_change": {"type": "boolean"},
        "title": {"type": "string"},
        "summary": {"type": ["string", "null"]},
        "content_patch": {"type": ["string", "null"]},
        # 兼容旧模型/旧测试返回；新提示词只允许模型输出 content_patch，完整正文由
        # sidecar 在本地确定性拼接，避免长文档被模型可见窗口截断后整体覆盖。
        "full_content": {"type": ["string", "null"]},
        "insert_mode": {
            "type": ["string", "null"],
            "enum": [
                "append",
                "insert_after_section",
                "replace_section",
                "no_change",
                None,
            ],
        },
        "target_section_index": {"type": ["integer", "null"]},
        "evidence_summary": {"type": ["string", "null"]},
        "new_info_summary": {"type": ["string", "null"]},
        "match_score": {"type": ["number", "null"]},
        "match_level": {
            "type": ["string", "null"],
            "enum": ["high", "medium", "low", None],
        },
    },
    "required": ["no_change", "title"],
    "additionalProperties": False,
}


BAKE_TEMPLATE_MARKERS = (
    "模板",
    "骨架",
    "槽位",
    "填写",
    "章节标题",
    "文章格式",
    "文章脉络",
    "文字描述风格",
    "常用口语词",
    "ai替代词",
    "替代词",
    "表达风格",
    "写法",
    "文风",
    "段落",
    "框架",
    "提纲",
    "复用",
)

BAKE_KNOWLEDGE_MARKERS = (
    "经验",
    "结论",
    "决策",
    "约束",
    "事实",
    "知识",
    "参考",
    "参照",
    "观察",
    "原则",
    "根因",
    "教训",
    "发现",
    "踩坑",
    "原因",
)

BAKE_SOP_MARKERS = (
    "sop",
    "步骤",
    "step",
    "触发条件",
    "前置条件",
    "检查点",
    "预期结果",
    "排查",
    "流程",
    "行动动线",
    "行动路线",
    "处理路线",
    "解决问题",
    "指引",
    "操作",
    "执行",
    "验证",
)

BAKE_DESIGN_MARKERS = (
    "设计",
    "方案",
    "模板",
    "骨架",
    "槽位",
    "章节标题",
    "文章格式",
    "文章脉络",
    "文字描述风格",
    "常用口语词",
    "ai替代词",
    "替代词",
    "文档模板",
    "汇报总结",
    "写作参考",
)

DOCUMENT_URL_MARKERS = (
    "/docs/",
    "docs.google",
    "/document/",
    "yuque.com",
    "feishu.cn/docx",
    "feishu.cn/wiki",
    "notion.so",
    "confluence",
    "/wiki/",
    "shimo.im",
    "/d/home/",
    "/s/home/",
    "/k/home/",
)

DOCUMENT_TITLE_MARKERS = (
    "云文档",
    "在线文档",
    "google docs",
    "google 文档",
    "飞书文档",
    "语雀",
    "notion",
    "confluence",
    "石墨文档",
    ".doc",
    ".docx",
    ".pages",
    ".md",
)

CHAT_APP_MARKERS = (
    "kim",
    "kem",
    "微信",
    "wechat",
    "slack",
    "teams",
    "microsoft teams",
    "钉钉",
    "dingtalk",
    "飞书",
    "feishu",
    "lark",
)

BROWSER_APP_MARKERS = (
    "chrome",
    "safari",
    "arc",
    "edge",
    "firefox",
    "chatgpt atlas",
)

DOCUMENT_EDITOR_APP_MARKERS = (
    "microsoft word",
    "word",
    "pages",
    "wps",
    "libreoffice writer",
    "obsidian",
    "typora",
    "cursor",
    "visual studio code",
    "code",
)

CODE_EDITOR_APP_MARKERS = (
    "cursor",
    "visual studio code",
    "code",
    "xcode",
)

MIN_DOCUMENT_EVIDENCE_CHARS = 200

BAKE_SCORE_METADATA_KEYS = {
    "match_score",
    "match_level",
    "review_status",
    "score",
    "confidence",
    "status",
    "auto_created",
    "confirmed",
    "ignored",
}

BAKE_SCORE_METADATA_KEYWORDS = (
    "match_score",
    "match level",
    "match_level",
    "review_status",
    "confidence",
)

BAKE_MISMATCH_MAX_SCORE = 0.49


BAKE_KNOWLEDGE_PROMPT = """类别:knowledge

只提炼未来工作中需要回想起来参照使用的有用知识。知识既包括长期稳定的解释、经验、约束、决策、结论和方法理解，也包括有明确对象、时间与证据的工作事实。
当你理解该时间线及对应采集记录描述的信息，是未来工作中会被拿来参考的事实、经验、约束、决策、结论、方法理解，或未来工作中需要参考某个设计方案的知识时，accepted=true。
事实不要求永远不变：项目进度、工作状态和执行结果只要在观测时点明确成立，未来需要据此继续推进、追溯变化或避免重复确认，就属于知识。典型事实包括：
- 某项工作已完成、尚未完成、正在处理、被阻塞、等待协作或已取消；
- 功能是否上线、集成或测试是否完成、问题是否解决、验收是否通过，以及其他明确的执行结果；
- 已确认的结论、决定、责任人、截止时间、依赖关系、风险和下一步承诺；
- 群聊、会议或任务沟通中对上述事实的明确同步。不能仅因为载体是聊天或内容以后可能更新就 reject。
例如“Agent Demo 尚未挂到 AIGC 页面，负责人计划两天内处理”和“导演 Agent 集成遇到单 Tool 性能问题，正在测试”都是应提炼的项目进度事实。
如果只是噪声或零散操作，没有形成任何可复用的知识点，就 reject。

本类别承接以事实、解释、经验、约束、决策或结论为主要复用价值的内容。若候选的主要价值是结构化数值观测、指标卡、表格、报表、查询结果，或以“对象 + 指标 + 数值”为核心的业务/系统状态快照，则交给 data，不要把数据上下文另建为 knowledge。项目或任务的语义状态、进度、结果和结论不是这里所说的数据状态快照，不得仅因它会随时间变化而排除出 knowledge。

accepted=true 时，payload schema:
{
  "summary": "知识标题/摘要，简洁明确",
  "overview": "对该知识的概述，可为空",
  "details": "详细描述，Markdown格式，说明该知识是什么、适用场景、未来如何参照使用、证据依据",
  "entities": ["实体1", "实体2"],
  "importance": 1-5,
  "occurrence_count": 1,
  "observed_at": 1710000000000,
  "event_time_start": null,
  "event_time_end": null,
  "history_view": false,
  "content_origin": "live_interaction|historical_content|document_reference|other|null",
  "activity_type": "meeting|coding|reading|chat|ask_ai|reviewing_history|other|null",
  "evidence_strength": "low|medium|high|null",
  "evidence_summary": "一句话说明依据",
  "match_score": 0.0,
  "match_level": "high|medium|low",
  "review_status": "auto_created"
}

约束:
- `summary` 必须体现沉淀后的知识点，不要直接照抄流水账
- `details` 必须是可渲染 Markdown，建议包含 `## 适用场景`、`## 可参考内容`、`## 证据依据`
- 提炼进度或结果事实时，`summary` 和 `details` 必须写清事实对象及观测时点成立的状态/结果/结论；来源明确写出的阻塞、责任人、截止时间和下一步承诺应一并保留
- 严格区分已发生事实与计划、预计、建议、猜测：可以记录“已承诺/计划/预计做什么”这一事实，但不得把计划中的动作改写成已经完成
- 只有“同步一下进度”“后续再看”等没有给出具体对象和实际状态的空泛消息才应 reject；来源明确、可归因的事实即使只出现一次也可以 accepted=true
- 输入即使含有写作模板特征或行动步骤，只要其中存在独立可复用的事实/经验/约束/决策/结论，就在 knowledge 中保留这部分；模板部分会由 design 处理，步骤部分由 sop 处理，不要替对方做拒绝判断
- `match_score` 使用 0-1 小数
- 接受的结果统一使用 `auto_created`
- 若只是模糊猜测或噪声，直接 reject"""

BAKE_DESIGN_PROMPT = """类别:design（用于沉淀「文档」资产）

这里的 design 对应用户产品中的「文档」tab，用来沉淀用户在工作中产出或参考过的「成体系内容资料」，覆盖以下场景:
1. 用户自己写出/正在写的方案、设计稿、汇报、总结、技术文档、运营文档、PRD、项目计划、会议纪要、需求说明等成形内容；
2. 用户认真阅读/反复参照的他人文档、资料、教程、范文，且其内容、结构、风格对未来工作有可借鉴价值；
3. 围绕一个主题成体系组织的产物片段，例如完整的 Prompt 指令集合、视频脚本分镜表、数据 Schema 说明、配置脚本注释、计算公式与参数说明等——只要这些片段是被「当作文档来阅读/编写/反复修改」的（而不是一闪而过的中间产物），都属于 design。

重要：候选输入可能只覆盖了文档的一部分（用户当时只看到了一段），但同一份文档往往会被多次浏览或编辑。如果候选包含 `url_document_context`，那是同一 URL 在过去被多次 capture 拼接出来的累计正文，应优先据此还原文档全貌；`url_document_context` 与当前 `capture_context` 来自同一份文档时不要重复抄录，按文档自身的章节顺序合并即可。

只要候选片段对应的是「一份/一段成体系的文档型内容」（无论是产出还是参考、无论一次还是多次浏览拼出来的），就值得沉淀为 design，accepted=true。
不要求文档必须"章节齐全/结构完整"——这是加分项，会反映在 match_score 的高低，而不是接受/拒绝的门槛。

什么时候 reject:
- 输入只是零散对话/聊天/任务清单/通知消息，没有围绕一份具体文档；
- 聊天中只是出现文档标题、云文档卡片、链接或“查看某文档”的指令，但没有采集到该文档正文；
- 输入只是一次性代码改动、操作流水或排查动线，且不涉及任何文档形态的内容；
- 输入只是无意义的浏览噪声、UI 切换、应用界面观察；
- 输入虽然涉及某个产品页面，但页面只是工具操作界面（如登录页、价格页、设置页），不承载任何文档式正文。

注意：「查看文档」本身就是充分理由——只要用户查看的是一份有实质内容的文档（内部文档、教程、规范、申请表单说明等），即使没有主动编辑或操作，也应 accepted=true。history_view=true 且有文档正文可识别时，不要因为缺少"主动产出"而 reject。

如果输入既包含文档内容又包含事实知识或行动步骤，design 类别仍可 accepted=true，只要文档主体存在。事实知识/行动路线会由 knowledge/sop 类别各自再判断，不需要在 design 这里互相挤兑。

accepted=true 时，payload schema:
{
  "name": "文档名称（必须表示整份文档；优先用文档自身的标题/页面 webpage_title；没有则用一句话概括其主题）",
  "category": "方案|设计|汇报|总结|技术文档|运营文档|会议纪要|资料参考|其他",
  "summary": "一句话概括这份文档讲了什么、是用户产出还是参考、未来在什么场景可再用，控制在 80 字以内",
  "full_content": "文档完整正文，Markdown 格式。要求是文档**本身**的内容；如有 url_document_context 应据此尽可能还原全貌，按文档原本的章节顺序整理；列表、代码、Prompt 指令、表格用 Markdown 语法保留；无法识别完整原文时，把 capture/url_document_context 中可见的核心段落如实写入；不要写'结构参考/使用建议/写作风格'这类分析元信息。",
  "details": "可选元信息说明，Markdown 格式。这里只放对文档使用方有帮助的辅助信息：未来什么场景可参考、风格/结构上值得借鉴的点、需要注意的限制。不要重复 full_content 的正文内容；如无可写，留空字符串。",
  "prompt_hint": "未来生成或撰写类似文档时可直接使用的提示词建议；若候选只是阅读型，可写未来检索/复用该文档时的关键词",
  "status": "draft|enabled",
  "tags": ["标签1", "标签2"],
  "applicable_tasks": ["creation"],
  "structure_sections": [{"title":"章节标题","keywords":["关键词"],"notes":"该章节的内容概要"}],
  "style_phrases": ["值得复用的表达/口语词，可为空数组"],
  "replacement_rules": [{"from":"AI腔或不合适表达","to":"更贴近用户风格的替代表达"}],
  "diagram_code": null,
  "evidence_summary": "一句话说明文档证据（看到了什么文档/写了什么内容；如使用了 url_document_context，请说明合并了几次 capture）",
  "match_score": 0.0,
  "match_level": "high|medium|low",
  "review_status": "auto_created"
}

约束:
- `full_content` 是首要产出，必须基于输入证据（包括 url_document_context）如实还原文档原文，禁止生造、禁止用"## 文档概要 / ## 关键内容 / ## 结构参考 / ## 使用建议"这类分析模板替代正文
- `name` 表示整份文档的稳定名称，禁止使用“文档增量”“新增内容”“补充内容”“更新版”等过程性前后缀
- `summary` 必须是简洁的一句话；不要把元信息塞进 summary
- `details` 仅用于"使用提示/借鉴点"，不是正文复述；没东西写就用空字符串 ""
- `structure_sections` 至少 1 条；如果文档结构不清，可只放一条整体概要
- `style_phrases`、`replacement_rules` 当作可选，识别不到时使用空数组
- match_score 评估"这份文档对未来工作的可参考价值":
  - 既有清晰章节骨架/可复用风格，又有明确主题: 0.75-0.95（high）
  - 是一份完整文档或重要参考资料但模板特征不强: 0.55-0.75（medium）
  - 内容偏单薄但仍有沉淀价值: 0.35-0.55（low）
- 接受的结果统一使用 `auto_created`；证据不足时直接 reject"""

BAKE_SOP_PROMPT = """类别:sop

只提炼未来遇到相同需求/问题场景时，可以参考给出行动路线建议的操作手册。
操作必须来自同一条时间线中的至少 2 条采集记录。Core 会给出 `sop_evidence_mode`，只允许以下两条证据通道：
- direct_interaction：action_trace 必须同时存在 `evidence_role=action` 的真实动作和 `evidence_role=result` 的可归因结果；
- semantic_workflow：允许结合 timeline 语义归纳已经发生的工作，但同样必须存在 action/result 证据锚点，不能仅凭总结、Agent 回复或上下文帧创建操作。
导航、滚动、应用切换、无输入的 key_pause、Agent/聊天执行汇报以及未归因的页面文字变化都属于 context，不得证明执行步骤或验证结果。两条通道都不得根据页面文字、按钮名称、计划或说明推测用户可能做过的操作。`effective_capture_count` 只统计 action/result 节点。
当 source_capture_count >= 2、sop_evidence_mode 非空，且证据描述了同一个问题或需求的实际行动动线、触发条件、处理路线、排查/执行步骤和验证方式时，accepted=true。
如果完全没有可复用的行动指引（纯事实陈述、纯文档阅读、纯噪声），reject。

`sop_evidence_mode` 非空表示 Core 已完成硬证据门禁，模型的职责是组织已有 action/result 证据，不得再次要求额外的连续 UI 动作。只有已有证据确实无法组成 3 条不臆造的步骤时才 reject。

accepted=true 时，payload schema:
{
  "summary": "SOP 标题/摘要",
  "overview": "该 SOP 解决什么问题，可为空",
  "source_title": "来源标题，可为空",
  "trigger_keywords": ["触发词1", "触发词2"],
  "extracted_problem": "触发场景/问题",
  "steps": ["步骤1", "步骤2", "步骤3"],
  "step_evidence": [
    {"step_index": 1, "capture_ids": ["实际支持步骤1的capture_id"]},
    {"step_index": 2, "capture_ids": ["实际支持步骤2的capture_id"]},
    {"step_index": 3, "capture_ids": ["实际支持步骤3的capture_id"]}
  ],
  "confidence": "high|medium|low",
  "evidence_summary": "一句话说明步骤依据",
  "match_score": 0.0,
  "match_level": "high|medium|low",
  "review_status": "auto_created"
}

约束:
- `source_capture_count` 小于 2、`sop_evidence_mode` 为空或 `effective_capture_count` 小于 2 时必须 reject；任何一条 capture、重复静态帧或纯滚动序列都不能独立成为 SOP
- `steps` 必须为 3-8 条，且必须是可执行动作；详细 Markdown 由 Core 根据这些字段确定性生成，不要输出 details
- `step_evidence` 必须与 steps 逐项对应，step_index 从 1 开始；capture_ids 必须来自 action_trace 且 `evidence_role` 为 action 或 result
- 每条执行步骤必须能由真实 action/result 节点直接证明；不得从 Agent 汇报、聊天回复或结果文字反推未采集到的修改、命令、工具、参数或界面细节
- direct_interaction 与 semantic_workflow 都必须有真实动作锚点和可归因结果；semantic_workflow 只能补充组织语义，不能放宽证据角色
- 指标卡、表格、查询结果、状态快照、Agent 汇报或说明文档都不等于用户操作流程
- 如果只是经验总结或模板骨架，不要误判成 SOP"""

BAKE_BUNDLE_PROMPT = f"""你在执行一次性 bake bundle 提炼。输入是一条时间线候选工作片段。

第一步先判断候选的主资产类型 `classification.primary_type`，用于描述主导复用价值和后续展示排序；然后独立评估每一种稳定资产：
- data：主要价值是结构化数值观测、业务指标、价格/用量/成本、指标卡、表格、报表、查询结果，或以“对象 + 指标 + 数值”为核心的业务/系统状态快照；
- knowledge：主要价值是有明确对象和证据的事实、解释、经验、约束、决策或结论；事实包括项目进度、工作状态变化、非数值执行结果、阻塞、责任人、截止时间和下一步承诺，不要求永久不变；
- document：主要价值是一份成体系、可整体复用的正文；
- sop：主要价值是来源明确记录的多步行动路线；
- none：没有足够可复用内容。

分类边界：项目/任务“已完成、未完成、进行中、被阻塞、已上线、测试通过/失败、问题已解决/未解决”等语义状态和结果应优先归 knowledge，并保留观测时间；不能因为它们会变化或来自聊天就归 data/none。只有主要价值可独立表达为结构化测量值、指标序列或报表快照时才归 data。计划和预计可以作为“已经形成的承诺/安排”进入 knowledge，但不得被改写成已经执行完成。

`primary_type` 只能选择一个，但它不构成其他资产的拒绝理由。knowledge、design、sop 必须各自依据所属类别的证据独立判断；同一候选同时包含可复用事实、成体系正文和实际执行的多步路线时，可以有多个 accepted=true。禁止仅因为 primary_type 不同而返回 not_primary_type。数据页面上的字段定义、计算公式、换算说明通常只是理解数据的上下文，不能据此推测知识或操作；但若 action_trace 另外明确记录了真实多步动作，仍应按 SOP 证据独立判断。

不能把界面中渲染的历史操作记录、变更日志或动态消息流冒充当前用户产出；但其中明确陈述的项目进度、执行结果或结论仍可作为对应主体在对应时点成立的 knowledge 事实。只有没有实质事实的自动动作壳才 reject。

最终只返回一个 JSON 对象，顶层必须严格按 classification、sop、knowledge、design 的顺序输出。SOP 放在两个长文本资产之前，保证后段正文异常时已完成的操作结果仍可独立保存。classification 固定为：
{{"primary_type":"data|knowledge|document|sop|none","reason":"一句话说明主导复用价值"}}
每个资产子对象固定为：
{{"accepted": true/false, "reason": "原因或 null", "payload": {{...}} 或 null}}

不要输出解释、代码块或思考过程。Markdown 只能出现在类别 schema 明确允许的字符串字段中。

以下是三个类别各自的判断规则和 payload schema；最终输出顺序仍以 classification、sop、knowledge、design 为准：

--- knowledge ---
{BAKE_KNOWLEDGE_PROMPT}

--- design ---
{BAKE_DESIGN_PROMPT}

--- sop ---
{BAKE_SOP_PROMPT}
"""

BAKE_COMPACT_BUNDLE_PROMPT = (
    BAKE_BUNDLE_PROMPT
    + """

这是失败后的紧凑重试。必须优先保证 JSON 完整闭合：
- 每个 Markdown 字段只保留最有证据的要点，不复述同一段内容
- 数组只保留最重要的项目
- 同一个 JSON 字段只输出一次；禁止重复 key、重复段落或循环扩写
- classification 必须只选择一个 primary_type，但 knowledge、design、sop 仍按各自证据独立判断，可以同时 accepted=true
- 禁止使用 not_primary_type 作为拒绝理由；拒绝时写清该资产自身缺少的证据
- 若内容无法在限制内可靠表达，对相应类别返回 accepted=false
"""
)

MERGE_SYSTEM_PROMPT ="""你是一个工作片段提炼助手。以下是用户在一段连续时间内的屏幕采集记录（按时间顺序），它们属于同一个工作片段。

**你的任务**:先验证这些采集是否真的属于同一个工作任务；只有同一任务才提炼为一个完整的工作片段知识条目。

**提炼规则**:
0. **先做任务一致性判定**:
   - 时间接近、应用相同、窗口标题相同或出现相同 UI 菜单，都不能单独证明是同一任务
   - 切换应用本身也不能单独证明是不同任务；若不同工具中的正文明确围绕同一目标，可保持同组
   - 若采集实际包含两个及以上互不依赖的目标（例如代码排查、天气查询、无关消息流、另一段 AI 对话），`is_coherent=false`
   - `capture_groups` 必须把“采集ID全集”完整分区：每个 ID 恰好出现一次，不得遗漏、重复或编造；同一任务可包含不连续的采集 ID
   - 同一任务时 `is_coherent=true`，`capture_groups` 只包含一个覆盖全部 ID 的分组
1. 识别这段时间内用户在做的一件完整的事
2. **从工作内容中提炼工作项**:综合分析所有帧的内容，识别用户在做哪个项目/功能的工作
   - 从代码注释、函数名、文件路径、Git commit、文档标题、聊天主题等内容中提炼
   - 格式:"项目名-功能模块"（如"MemoryBread-时间线提炼优化"）或"项目名"（如"个人博客"）
   - 如果内容明确提到具体任务（如"修复 bug #123"），可以更具体（如"MemoryBread-修复排查步骤 bug"）
   - 如果无法从内容中识别，填写 null
3. **识别工作进度和状态**:从内容中推断当前工作的进展
   - work_status: "pending"（待启动）| "in_progress"（进行中）| "completed"（已完成）| "blocked"（阻塞）
   - work_progress: 具体进度描述（如"已完成核心逻辑"、"待其他团队协作"、"等待需求确认"）
4. 生成概述（50-150字）:描述做了什么、关键进展、结果，使用过去时态
5. 生成明细（200-500字）:
   - 保留有追溯价值的具体信息（代码逻辑、会议决策、学到的知识点）
   - 过滤掉 UI 操作、重复内容、无意义的切换记录
   - 不要堆砌原始文本，要提炼和归纳
6. 识别关键实体（人名、项目名、技术词汇）
7. 判断分类和重要性

**输出格式（JSON）**:
{
  "is_coherent": true,
  "coherence_reason": "所有采集围绕同一个工作目标",
  "capture_groups": [[101, 102, 103]],
  "work_item": "项目名或项目名-功能模块，如 'MemoryBread-时间线提炼优化'，无法识别时填 null",
  "work_status": "pending|in_progress|completed|blocked",
  "work_progress": "具体进度描述，如 '已完成核心逻辑，待集成测试'",
  "overview": "概述，50-150字，不含换行符",
  "details": "明细，200-500字，使用空格代替换行符",
  "entities": ["实体1", "实体2"],
  "category": "会议|文档|代码|聊天|学习|其他",
  "importance": 1-5,
  "history_view": true,
  "content_origin": "live_interaction|historical_content|document_reference|other",
  "activity_type": "meeting|coding|reading|chat|ask_ai|reviewing_history|other",
  "event_time_start": 1710000000000,
  "event_time_end": 1710003600000,
  "evidence_strength": "low|medium|high"
}

**注意补充判断**:
- **工作项识别示例**:
  * 代码文件 "extractor_v2.py" + 注释 "优化时间线提炼逻辑" → work_item: "MemoryBread-时间线提炼优化"
  * Git commit "fix: 修复排查步骤 bug" → work_item: "MemoryBread-修复排查步骤 bug"
  * 聊天记录讨论 "个人博客的评论功能需求" → work_item: "个人博客-评论功能"
  * 文档标题 "用户认证系统重构方案" → work_item: "用户认证系统-重构"
  * 如果只看到 "修复 bug"、"写代码" 等模糊描述，无法识别具体项目，填 null
- **工作进度识别示例**:
  * 看到 "TODO"、"开始实现" → work_status: "in_progress", work_progress: "刚开始开发"
  * 看到 "测试通过"、"已上线" → work_status: "completed", work_progress: "已完成并上线"
  * 看到 "等待"、"阻塞"、"依赖" → work_status: "blocked", work_progress: "等待其他团队协作"
  * 看到 "80% 完成"、"还剩最后一步" → work_status: "in_progress", work_progress: "已完成 80%"
- 如果用户今天在 IM/聊天/AI 工具里回看昨天、前天、更早的消息或历史对话，`history_view=true`
- `observed_at` 不需要输出，由系统记录当前片段结束时间
- `event_time_start/event_time_end` 只在内容明确提到事情发生时间时填写；不明确时返回 null
- 询问 Gemini/Claude/ChatGPT 等 AI 助手，通常可标为 `activity_type=ask_ai`
- 查看历史消息/历史会话，通常可标为 `activity_type=reviewing_history` 且 `content_origin=historical_content`
- 直接实时聊天或会议记录，通常 `content_origin=live_interaction`
- 证据弱、推断成分高时降低 `evidence_strength`

**重要性评分**:
- 5分:关键决策、重要会议纪要、核心代码逻辑
- 4分:项目进展、技术文档、重要沟通
- 3分:日常工作记录、一般文档
- 2分:简单操作记录
- 1分:无关紧要的内容

**注意**:输出必须是有效的 JSON，字符串中的引号要转义，不要包含未转义的换行符。
"""

SYSTEM_PROMPT = """你是一个专业的工作记录提炼助手。你的任务是从 OCR 识别的屏幕文本中提取有价值的工作信息。

**提炼规则**:
1. 忽略 UI 元素（按钮、菜单、状态栏等）
2. 提取核心工作内容（会议记录、文档内容、代码片段、聊天记录等）
3. 生成"概述"和"明细"两部分内容:
   - 概述:简洁描述在做什么事情（30-100字），使用过去时态，避免使用"正在"等词
   - 明细:详细记录具体内容细节，保留关键信息以便后期追溯（200-500字）
4. 识别关键实体（人名、项目名、时间、地点）
5. 如果内容无价值（纯 UI、重复内容），返回 "SKIP"

**输出格式**（JSON）:
{
  "overview": "概述文本，描述做了什么事情，不要包含换行符",
  "details": "明细文本，使用空格代替换行符",
  "entities": ["实体1", "实体2"],
  "category": "会议|文档|代码|聊天|其他",
  "importance": 1-5
}

**重要性评分标准**:
- 5分:关键决策、重要会议纪要、核心代码逻辑
- 4分:项目进展、技术文档、重要沟通
- 3分:日常工作记录、一般文档
- 2分:简单操作记录
- 1分:无关紧要的内容

**明细内容要求**:
- 保留足够的上下文信息
- 记录关键对话内容和参与人
- 保留代码片段和技术细节
- 记录决策过程和理由
- 便于后期回忆和追溯
- 所有文本必须在一行内，不要使用换行符

**注意**:输出必须是有效的 JSON 格式，字符串中的引号要转义，不要包含未转义的换行符。
"""

DATA_FACT_CONTRACT_VERSION = "timeline-data-fact.v3"
DATA_FACT_PROMPT = """

**结构化数据事实（与上述时间线提炼在同一次输出中完成）**:
- 顶层必须额外输出 `data_facts` 数组；没有可靠数据事实时输出空数组 `[]`。
- 只提取已经发生或已经观测、同时包含明确数值的数据事实。计划、建议、假设、示例、UI 计数和缺少对象的裸数字不要输出。已经实际提交执行的生成参数、已经完成的任务耗时和已经通过的验收结果属于观测事实；仍停留在输入框、计划、建议或目标中的配置不属于观测事实。
- 先判断来源是否以结构化测量结果为主体：指标卡、汇总区、表格、报表、查询结果、价格/用量/成本或状态快照都属于数据主体。业务对象的数量和汇总值是数据；只有分页条数、按钮角标、菜单序号、步骤编号等导航或界面计数才是 UI 计数。
- 对数据主体，优先提取顶部/总体汇总指标，再提取对象明确的明细行。按决策价值与证据完整度排序，单次最多输出 24 条；不要因为同屏存在按钮或菜单就改判为操作流程。
- `data_facts` 与工作动作判断相互独立。产品价格、套餐规格、容量、费率等参考数据即使来自被动浏览的官网，也属于可靠数据；只要存在这类事实，必须正常填写 `overview/details`，不得返回 `SKIP`、空概述或因为“未购买/未执行操作”而清空 `data_facts`。
- 每条事实必须保留完整业务关系，不能用“AIGC”“成本”“收入”等宽泛词替代具体对象。
- `evidence_quote` 必须从输入采集文本复制粘贴，保持原始空格、标点、大小写、单位和词序；不能润色、纠错、拼接或补词。它必须是原文中一段连续的子串，禁止用省略号缩写。面对无换行的大表格，也只截取能够同时覆盖当前对象和值的最短连续片段。
- `title` 必须同时说明具体对象、动作或目标场景和指标，不包含具体数值。不得把“用户设置了”“随后配置了”“整个任务”“任务总耗时”等叙述残片直接当标题；必须恢复它具体属于哪个产品、任务、生成内容或业务场景。
- `statement` 是完整、可独立理解的事实句，必须包含对象、动作或目标场景、指标和值。工作语境可以帮助补全标题和独立事实句，但 `evidence_quote` 仍只能逐字复制原始采集证据。
- `value` 只放原始数字、数字范围或复合数值；`unit` 使用证据中的原始单位。`16分31秒`、`1小时20分钟` 等复合时长必须作为一个完整 value 保留且 unit 留空，不能只取最后的 `31秒`。不要自行换算单位。
- 同一对象、动作、目标场景、指标和数值在重复截图或“整个任务耗时/任务总耗时”等同义句中多次出现时只输出一条，不得按措辞拆成多个数据源。

提取前必须在内部依次完成以下检查，但不要输出检查过程：
1. **事实状态**：先判断数字是已发生/已观测结果，还是目标、上限、验收条件、检查清单、预案阈值、配置建议。处在“检查清单、预案、目标、要求、应当、阈值、切换前检查”等上下文中的 `< 1%`、`CPU < 40%` 一律不是观测事实；只有原文另有“监控显示当前值为…”“实测为…”等明确观测证据时才可输出。
2. **最近对象**：`subject` 必须是 `evidence_quote` 中离指标最近、明确命名的产品、套餐、系统、模型、资产或业务对象。不得使用浏览器窗口标题、网页宣传语、文档标题、章节名、句尾 OCR 残片或“相关核心”等不完整短语补对象。
3. **关系完整**：套餐、版本、地区、当前/此前、按年/按月等比较维度必须保留。不得把“每用户每月 4 USD，按年计费”改写成“每年 4 USD”，也不得只挑比较中的最后一个值。
4. **原子事实**：一条事实只表达一个对象在一个维度下的一个指标值；同一页面有多个套餐、指标或计费方式时分别输出多条事实，让相同对象和指标通过 `dimension` 聚合比较。
5. **标题自检**：标题必须是“明确对象 + 完整指标”，不能含具体值，不能是口号、页面名、动作残句或宽泛主题。将标题、维度和值重新组合后，应能准确复述 `evidence_quote`，否则丢弃该事实。
6. **逐字对象**：`subject` 必须从 `evidence_quote` 逐字复制，禁止翻译、扩写、同义替换或改成类别名；`title` 和 `statement` 也必须逐字包含同一个 `subject`。输出前必须再次在输入中搜索 `evidence_quote`；搜索不到就重新复制原文或丢弃该条，绝不能凭记忆重写引文。
7. **场景完整**：配置参数和任务耗时不得使用“用户”“任务”“生成参数”等泛称作为最终语义。优先从同一采集块中的任务标题、产品名、提示词目标或完成结果恢复场景，并写入 `title`、`action`、`target_context` 和 `statement`；若仍无法说明具体场景，则不要输出该事实。
8. **字段名和执行工具不是对象**：`duration`、`aspect_ratio`、`已用时`、`任务总耗时` 等字段名或泛称只能放在 metric/dimension，不能单独作为 subject。若证据附近只有字段名，必须结合工作语境把具体生成内容、产品任务或业务用途写入 `target_context` 和标题；模型名、系统名、`视频参数配置`、`生成控制`、`过程监控`、`API接口调用` 都只是执行环境，不算具体目标场景。

`data_facts` 中每个对象的固定 schema：
{
  "title": "具体对象 + 指标",
  "subject": "指标所属的具体资产、产品、系统、模型或业务对象",
  "action": "复用|优化|迁移|生成|审核等；没有动作时为空字符串",
  "target_context": "动作作用的目标业务或场景；没有时为空字符串",
  "dimension": "当前|此前|国内|海外等比较维度；没有时为空字符串",
  "metric": "规范且完整的指标名",
  "value": "数值、数值范围或复合数值",
  "unit": "原始单位；没有时为空字符串",
  "statement": "包含完整上下文的独立事实句",
  "evidence_quote": "输入中逐字存在的最短充分证据",
  "confidence": "high|medium|low"
}

示例：输入“生服模特库在电商AIGC中的复用已成功合并，节省约6.28万成本”，应输出：
{"title":"生服模特库在电商AIGC中复用的成本节省金额","subject":"生服模特库","action":"复用","target_context":"电商AIGC","dimension":"","metric":"成本节省金额","value":"6.28","unit":"万","statement":"生服模特库在电商AIGC中的复用节省约6.28万成本。","evidence_quote":"生服模特库在电商AIGC中的复用已成功合并，节省约6.28万成本","confidence":"high"}

套餐示例：输入“Sync Standard $4 USD 每用户每月，按年计费；$5 USD 每用户每月，按月计费；1 GB 总存储空间”时，至少分别输出：
- `subject=Sync Standard, metric=每用户月费, dimension=按年计费, value=4, unit=USD`
- `subject=Sync Standard, metric=每用户月费, dimension=按月计费, value=5, unit=USD`
- `subject=Sync Standard, metric=总存储空间, dimension=空字符串, value=1, unit=GB`
三条事实的标题分别使用“Sync Standard 每用户月费”和“Sync Standard 总存储空间”；每条 `evidence_quote` 都必须包含对应套餐名和数值，价格事实还必须包含计费维度。其他套餐按相同方式独立输出，禁止只保留最后一个套餐或最后一个数字。

正确价格事实示例：`{"title":"Sync Standard 每用户月费","subject":"Sync Standard","action":"","target_context":"","dimension":"按年计费","metric":"每用户月费","value":"4","unit":"USD","statement":"Sync Standard 按年计费时每用户月费为 4 USD。","evidence_quote":"Sync Standard $4 USD 每用户每月，按年计费","confidence":"high"}`。若无法截取一段同时包含 `subject`、维度、值和单位的原文证据，则不要输出该事实。
"""

DATA_PAGE_CONTRACT_VERSION = "timeline-data-page.v1"
DATA_PAGE_PROMPT = """

**数据页面分类（与上述时间线提炼在同一次输出中完成）**:
- 顶层必须额外输出 `data_pages` 数组；本片段没有任何数据页面时输出空数组 `[]`。
- 只允许对输入中明确给出「页面URL」的采集块做分类；`url` 必须逐字抄自输入中的页面URL，禁止编造、拼接、补全或修改参数。
- 每个条目固定 schema：
{"url": "输入中逐字存在的页面URL", "page_kind": "data_report|data_platform|data_content|none", "title": "页面标题或简短页面描述"}
- `page_kind` 判定口径：
  - `data_report`：呈现指标/报表/看板数据、数值会随时间更新的页面（如监控看板、经营报表、Grafana）
  - `data_platform`：数据查询/管理平台的页面（如资源用量一览、服务质量查询平台）
  - `data_content`：内容里包含可靠数据但页面本身不是报表/平台（如文档、聊天记录中的数字）
  - `none`：页面与数据无关；不确定时一律填 `none`
- 同一 URL 只输出一次；不要为没有 URL 的采集（桌面应用、聊天窗口等）编造 URL。
"""

_DATA_PAGE_KINDS = {"data_report", "data_platform", "data_content", "none"}

# 小模型常在字符串值里直接写英文双引号导致 JSON 失效，所有提炼调用统一追加该约束
JSON_OUTPUT_RULES = (
    "字符串值内如需引用原文的英文双引号，必须转义为 \\\"，"
    "或改用中文引号“”；确保输出是能被 json.loads 直接解析的合法 JSON。"
)


def _normalize_page_url(url: Any) -> str:
    return str(url or "").strip().rstrip("/")


def _validated_data_pages(raw_pages: Any, allowed_urls: set) -> List[Dict[str, Any]]:
    """验证模型数据页面分类；只接受结构完整且 URL 逐字存在于本组采集的条目。"""
    if not isinstance(raw_pages, list):
        return []
    accepted = []
    seen = set()
    for item in raw_pages:
        if not isinstance(item, dict):
            continue
        url = _normalize_page_url(item.get("url"))
        kind = str(item.get("page_kind") or "").strip()
        if not url or kind not in _DATA_PAGE_KINDS:
            continue
        if not (url.startswith("http://") or url.startswith("https://")):
            continue
        if url not in allowed_urls:
            continue
        if url in seen:
            continue
        seen.add(url)
        accepted.append({
            "url": url,
            "page_kind": kind,
            "title": str(item.get("title") or "").strip()[:240],
        })
    return accepted


def _validated_capture_groups(
    raw_groups: Any,
    capture_ids: List[int],
    minimum_groups: int = 2,
) -> List[List[int]]:
    """校验模型给出的任务分组是输入采集 ID 的完整、不重不漏分区。"""
    if not isinstance(raw_groups, list) or not raw_groups:
        return []

    # 小模型偶尔会把“唯一分组”输出成 [1, 2, 3]，而不是 [[1, 2, 3]]。
    # 这种形态只有在调用方允许单组、且 ID 仍能通过完整分区校验时才可归一化；
    # incoherent 分支要求 minimum_groups=2，因此不会把待拆分结果误当成单组放行。
    if all(not isinstance(item, list) for item in raw_groups):
        raw_groups = [raw_groups]
    if len(raw_groups) < minimum_groups:
        return []

    ordered_ids = [int(capture_id) for capture_id in capture_ids]
    allowed = set(ordered_ids)
    position = {capture_id: index for index, capture_id in enumerate(ordered_ids)}
    seen = set()
    groups: List[List[int]] = []

    for raw_group in raw_groups:
        if not isinstance(raw_group, list) or not raw_group:
            return []
        group: List[int] = []
        for raw_id in raw_group:
            if isinstance(raw_id, bool):
                return []
            try:
                capture_id = int(raw_id)
            except (TypeError, ValueError):
                return []
            if capture_id not in allowed or capture_id in seen:
                return []
            seen.add(capture_id)
            group.append(capture_id)
        group.sort(key=lambda capture_id: position[capture_id])
        groups.append(group)

    if seen != allowed:
        return []
    groups.sort(key=lambda group: min(position[capture_id] for capture_id in group))
    return groups


def _validated_coherent_capture_group(
    raw_groups: Any,
    capture_ids: List[int],
) -> List[List[int]]:
    """校验同任务分组，并兼容小模型只返回代表帧的扁平数组。"""
    exact_groups = _validated_capture_groups(
        raw_groups,
        capture_ids,
        minimum_groups=1,
    )
    if len(exact_groups) == 1:
        return exact_groups

    # is_coherent=true 已经明确表示全部输入属于一个任务。部分小模型仍会在
    # capture_groups 中只列代表帧；仅对“合法、无重复、非空的扁平子集”扩展为
    # 输入全集。嵌套分组、未知 ID、重复 ID 继续 fail-closed，避免掩盖歧义。
    if not isinstance(raw_groups, list) or not raw_groups:
        return []
    if any(isinstance(item, list) for item in raw_groups):
        return []

    ordered_ids = [int(capture_id) for capture_id in capture_ids]
    allowed = set(ordered_ids)
    seen = set()
    for raw_id in raw_groups:
        if isinstance(raw_id, bool):
            return []
        try:
            capture_id = int(raw_id)
        except (TypeError, ValueError):
            return []
        if capture_id not in allowed or capture_id in seen:
            return []
        seen.add(capture_id)
    return [ordered_ids]


_HALFWIDTH_SYMBOL_MAP = {
    '，': ',', '。': '.', '：': ':', '；': ';', '！': '!', '？': '?',
    '（': '(', '）': ')', '［': '[', '］': ']', '【': '[', '】': ']',
    '｛': '{', '｝': '}', '％': '%', '＋': '+', '－': '-', '＝': '=',
    '＜': '<', '＞': '>', '＄': '$', '￥': '¥',
}


def _normalize_fact_evidence(value: Any) -> str:
    normalized = re.sub(r"\s+", "", str(value or "")).casefold()
    # OCR 与模型输出的全角/半角标点差异不应破坏逐字回证
    for full, half in _HALFWIDTH_SYMBOL_MAP.items():
        normalized = normalized.replace(full, half)
    return normalized


def _normalized_source_with_offsets(source_text: str) -> tuple[str, List[int]]:
    """返回与证据校验一致的规范化文本，并保留到原文字符的映射。"""
    normalized_chars: List[str] = []
    source_offsets: List[int] = []
    for index, char in enumerate(str(source_text or "")):
        if char.isspace():
            continue
        normalized = _HALFWIDTH_SYMBOL_MAP.get(char, char).casefold()
        for normalized_char in normalized:
            normalized_chars.append(normalized_char)
            source_offsets.append(index)
    return "".join(normalized_chars), source_offsets


def _all_substring_spans(text: str, needle: str) -> List[tuple[int, int]]:
    if not needle:
        return []
    spans: List[tuple[int, int]] = []
    cursor = 0
    while True:
        start = text.find(needle, cursor)
        if start < 0:
            break
        spans.append((start, start + len(needle)))
        cursor = start + 1
    return spans


def _realign_fact_evidence(source_text: str, fact: Dict[str, Any]) -> str:
    """依据模型给出的语义锚点和值，从原文找回最短连续证据。

    AX 大表没有可靠换行，因此不依赖行边界。该步骤不识别业务字段，也不创造
    数值；只要求数值和至少一个对象/场景/指标锚点能在原文中邻近命中。
    """
    normalized_source, offsets = _normalized_source_with_offsets(source_text)
    if not normalized_source or not offsets:
        return str(fact.get("evidence_quote") or "")

    value = _normalize_fact_evidence(fact.get("value"))
    value_spans = _all_substring_spans(normalized_source, value)
    if not value_spans:
        value_tokens = re.findall(r"[0-9][0-9:.,%]*", value)
        if value_tokens:
            value_spans = _all_substring_spans(normalized_source, value_tokens[0])
    if not value_spans:
        return str(fact.get("evidence_quote") or "")

    anchor_fields = ("subject", "target_context", "metric", "dimension")
    anchors = []
    for priority, field in enumerate(anchor_fields):
        anchor = _normalize_fact_evidence(fact.get(field))
        spans = _all_substring_spans(normalized_source, anchor)
        if anchor and spans:
            anchors.append((priority, field, anchor, spans))
    if not anchors:
        return str(fact.get("evidence_quote") or "")

    unit = _normalize_fact_evidence(fact.get("unit"))
    unit_spans = _all_substring_spans(normalized_source, unit) if unit else []
    candidates: List[tuple[int, int, int, str]] = []
    for priority, field, _anchor, anchor_spans in anchors:
        for anchor_start, anchor_end in anchor_spans:
            for value_start, value_end in value_spans:
                start = min(anchor_start, value_start)
                end = max(anchor_end, value_end)
                if unit and unit not in normalized_source[start:end]:
                    nearby_units = [
                        span for span in unit_spans
                        if max(end, span[1]) - min(start, span[0]) <= 500
                    ]
                    if nearby_units:
                        unit_start, unit_end = min(
                            nearby_units,
                            key=lambda span: max(end, span[1]) - min(start, span[0]),
                        )
                        start = min(start, unit_start)
                        end = max(end, unit_end)
                raw_start = offsets[start]
                raw_end = offsets[end - 1] + 1
                if raw_end - raw_start <= 500:
                    candidates.append((raw_start, raw_end, priority, field))
    if not candidates:
        return str(fact.get("evidence_quote") or "")
    raw_start, raw_end, _priority, _field = min(
        candidates,
        # 优先保留具体对象/场景，只有它们无法回证时才退到
        # 指标或维度；同一类锚点内再选最短连续原文片段。
        key=lambda span: (span[2], span[1] - span[0]),
    )
    return str(source_text or "")[raw_start:raw_end]


def _expand_fact_evidence(source_text: str, subject: str, evidence_quote: str) -> str:
    """将过短的逐字引文扩到最近对象；只切取原文，不生成或改写内容。"""
    if not subject or not evidence_quote or subject in evidence_quote:
        return evidence_quote
    evidence_start = source_text.find(evidence_quote)
    if evidence_start < 0:
        return evidence_quote
    subject_positions = [
        match.start()
        for match in re.finditer(re.escape(subject), source_text)
    ]
    if not subject_positions:
        return evidence_quote
    evidence_end = evidence_start + len(evidence_quote)
    subject_start = min(
        subject_positions,
        key=lambda position: min(
            abs(position - evidence_start),
            abs(position - evidence_end),
        ),
    )
    subject_end = subject_start + len(subject)
    expanded_start = min(subject_start, evidence_start)
    expanded_end = max(subject_end, evidence_end)
    if expanded_end - expanded_start > 500:
        return evidence_quote
    return source_text[expanded_start:expanded_end]


def _evidence_matches_source(evidence: str, source_normalized: str) -> bool:
    """校验 evidence 是否可回证。

    优先整段逐字命中；若模型输出带省略号（…/...），退化为分段按顺序
    逐段命中原文（每段仍须逐字存在）。
    """
    if not evidence:
        return False
    if evidence in source_normalized:
        return True
    fragments = [
        frag.strip() for frag in re.split(r"\.{3}|\u2026", evidence) if frag.strip()
    ]
    if len(fragments) < 2:
        return False
    cursor = 0
    for frag in fragments:
        pos = source_normalized.find(frag, cursor)
        if pos == -1:
            return False
        cursor = pos + len(frag)
    return True


_DATA_FACT_RETRY_VALUE_RE = re.compile(
    r"(?:\d+(?:[.,]\d+)?\s*(?:%|％|毫秒|秒|分(?:钟)?|小时|天|元|万|亿|"
    r"usd|gb|tb|mb|kb|qps|倍|张|条|个|次|人|份|项))|"
    r"(?:\d+\s*[:：×xX]\s*\d+)",
    re.IGNORECASE,
)
_DATA_FACT_RETRY_CONTEXT_RE = re.compile(
    r"耗时|时长|画幅|比例|分辨率|参数|配置|结果|完成|通过|成功|失败|"
    r"成本|收入|营收|利润|gmv|利用率|准确率|召回率|错误率|延迟|"
    r"容量|用量|总量|数量|单价|预算|余额|qps|cpu|gpu|内存|存储",
    re.IGNORECASE,
)


def _data_fact_retry_needed(source_text: str) -> bool:
    """判断主提炼遗漏事实时是否值得进行一次聚焦补提炼。

    这里只决定是否追加一次受控模型调用，不直接判定事实是否成立；计划、建议、
    UI 计数和证据回查仍由模型契约与 `_validated_data_facts` fail-closed 门禁负责。
    """
    text = str(source_text or "")
    return bool(
        _DATA_FACT_RETRY_VALUE_RE.search(text)
        and _DATA_FACT_RETRY_CONTEXT_RE.search(text)
    )


def _generic_fact_anchor(value: str) -> bool:
    normalized = re.sub(r"[\s_\-:：/]+", "", str(value or "")).casefold()
    return normalized in {
        "duration",
        "aspectratio",
        "width",
        "height",
        "size",
        "value",
        "参数",
        "生成参数",
        "请求参数",
        "配置参数",
        "已用时",
        "耗时",
        "总耗时",
        "任务耗时",
        "任务总耗时",
        "整个任务",
        "本次任务",
        "该任务",
    }


def _specific_fact_context(value: str) -> bool:
    normalized = re.sub(r"[\s_\-:：/]+", "", str(value or "")).casefold()
    if len(normalized) < 6:
        return False
    if normalized in {
        "视频参数配置",
        "生成参数配置",
        "任务处理过程",
        "api接口调用",
        "接口调用",
        "当前任务",
        "本次任务",
        "整个任务",
    }:
        return False
    # 模型/系统名加“参数配置、生成控制、过程监控”等执行壳，仍没有说明
    # 参数最终服务于什么交付物或业务用途。此类文本看似具体，实际仍会产出
    # “Kling 生成控制时长”一类无法脱离采集记录理解的标题。
    return not normalized.endswith(
        ("参数配置", "生成控制", "过程监控", "接口调用", "任务处理过程")
    )


def _generic_execution_context(value: str) -> bool:
    normalized = re.sub(r"[\s_\-:：/]+", "", str(value or "")).casefold()
    return bool(normalized) and normalized.endswith(
        ("参数配置", "生成控制", "过程监控", "接口调用", "任务处理过程")
    )


def _value_like_fact_subject(subject: str, value: str) -> bool:
    normalized_subject = _normalize_fact_evidence(subject)
    normalized_value = _normalize_fact_evidence(value)
    if not normalized_value or normalized_value not in normalized_subject:
        return False
    residue = normalized_subject.replace(normalized_value, "", 1)
    meaningful_residue = re.sub(r"[^a-z\u4e00-\u9fff]", "", residue.casefold())
    # `308秒`、`15秒`、`横屏16:9` 是值的展示形态，不是业务对象；
    # `MemoryBread V2` 等带数字的命名对象保留足够多的文本语义，不会命中。
    return len(meaningful_residue) <= 4


def _fact_value_is_stated(fact: Dict[str, Any]) -> bool:
    statement = _normalize_fact_evidence(fact.get("statement"))
    value = _normalize_fact_evidence(fact.get("value"))
    if not statement or not value:
        return False
    if value in statement:
        return True
    value_tokens = re.findall(r"[0-9][0-9:.%]*", value)
    return bool(value_tokens) and all(token in statement for token in value_tokens)


def _fact_specific_statement(fact: Dict[str, Any]) -> str:
    """用已回证的原子字段生成展示句，避免多个事实共享模型汇总句。"""
    subject = str(fact.get("subject") or "").strip()
    action = str(fact.get("action") or "").strip()
    target_context = str(fact.get("target_context") or "").strip()
    dimension = str(fact.get("dimension") or "").strip()
    metric = str(fact.get("metric") or "").strip()
    value = str(fact.get("value") or "").strip()
    unit = str(fact.get("unit") or "").strip()

    context = subject
    if target_context and target_context not in context:
        context = f"{context}在{target_context}中"
    if action and action not in context:
        context = f"{context}{action}"
    metric_label = f"{dimension}{metric}" if dimension else metric
    display_value = value if not unit or value.endswith(unit) else f"{value}{unit}"
    statement = f"{context}的{metric_label}为{display_value}。"
    if len(statement) <= 500:
        return statement

    # statement 的契约上限是 500 字符。超长时优先丢弃执行上下文，再截断
    # dimension；metric/value 始终保留，避免边界裁剪再次生成不含自身值的句子。
    suffix = f"{metric}为{display_value}。"
    prefix = f"{subject}的"
    dimension_budget = max(0, 500 - len(prefix) - len(suffix))
    return f"{prefix}{dimension[:dimension_budget]}{suffix}"


def _validated_data_facts(raw_facts: Any, source_text: str, relaxed_subject: bool = False) -> tuple[List[Dict[str, Any]], int]:
    """验证模型事实；不修补语义，只接受能够逐字回证的完整结构。

    防幻觉底线只有两条：evidence 逐字回证原文、subject/value 的数字或文本
    必须真实命中 evidence。title/statement 是否逐字复现 subject/metric/value
    属于展示结构而非可靠性问题——小模型常输出概括式标题，强包含校验会把真
    实数据拒掉（v2 上线后 95% 时间线 0 落库事实的根因）。
    `relaxed_subject` 仅为历史调用兼容保留；对象回证底线不再放宽。
    """
    if not isinstance(raw_facts, list):
        return [], int(raw_facts is not None)

    source_normalized = _normalize_fact_evidence(source_text)
    accepted: List[Dict[str, Any]] = []
    accepted_keys = set()
    rejected = 0
    reject_reasons: Dict[str, int] = {}

    def _reject(reason: str) -> None:
        nonlocal rejected
        rejected += 1
        reject_reasons[reason] = reject_reasons.get(reason, 0) + 1

    # 与提示词保持一致；控制单次模型输出规模，但不再因校验层额外截断事实。
    fact_limit = 24
    for raw in raw_facts[:fact_limit]:
        if not isinstance(raw, dict):
            _reject("not_dict")
            continue
        fact = {
            "title": _normalize_inline_text(raw.get("title")),
            "subject": _normalize_inline_text(raw.get("subject")),
            "action": _normalize_inline_text(raw.get("action")),
            "target_context": _normalize_inline_text(raw.get("target_context")),
            "dimension": _normalize_inline_text(raw.get("dimension")),
            "metric": _normalize_inline_text(raw.get("metric")),
            "value": _normalize_inline_text(raw.get("value")),
            "unit": _normalize_inline_text(raw.get("unit")),
            "statement": _normalize_inline_text(raw.get("statement")),
            "evidence_quote": _normalize_inline_text(raw.get("evidence_quote")),
            "confidence": _normalize_inline_text(raw.get("confidence") or "medium").lower(),
        }
        fact["evidence_quote"] = _expand_fact_evidence(
            source_text,
            fact["subject"],
            fact["evidence_quote"],
        )
        evidence = _normalize_fact_evidence(fact["evidence_quote"])
        subject = _normalize_fact_evidence(fact["subject"])
        value = _normalize_fact_evidence(fact["value"])
        unit = _normalize_fact_evidence(fact["unit"])
        metric = _normalize_fact_evidence(fact["metric"])
        target_context = _normalize_fact_evidence(fact["target_context"])
        semantic_anchor_hit = any(
            anchor and anchor in evidence
            for anchor in (subject, target_context, metric)
        )
        evidence_is_sufficient = (
            _evidence_matches_source(evidence, source_normalized)
            and semantic_anchor_hit
            and (not value or value in evidence or all(
                token in evidence
                for token in re.findall(r"[0-9][0-9:.]*", value)
            ))
            and (not unit or unit in evidence)
        )
        if not evidence_is_sufficient:
            fact["evidence_quote"] = _realign_fact_evidence(source_text, fact)
        evidence = _normalize_fact_evidence(fact["evidence_quote"])

        # 防幻觉失败时优先降级为原文可证的结构，不因展示字段缺失丢掉数值。
        grounded_anchors = [
            (field, _normalize_fact_evidence(fact[field]))
            for field in ("subject", "target_context", "metric")
            if fact[field]
            and _normalize_fact_evidence(fact[field]) in evidence
        ]
        if (
            not grounded_anchors
            or not fact["value"]
            or not any(char.isdigit() for char in fact["value"])
            or not fact["evidence_quote"]
        ):
            _reject("missing_grounded_anchor_or_value")
            continue
        grounded_subject = _normalize_fact_evidence(fact["subject"])
        if not grounded_subject or grounded_subject not in evidence:
            fact["subject"] = next(
                fact[field] for field, _anchor in grounded_anchors
            )
        if not fact["metric"]:
            fact["metric"] = fact["subject"]
        if not fact["title"]:
            fact["title"] = f"{fact['subject']} {fact['metric']}".strip()
        if not fact["statement"]:
            fact["statement"] = fact["evidence_quote"]
        if fact["unit"] and _normalize_fact_evidence(fact["unit"]) not in evidence:
            fact["unit"] = ""
        if fact["dimension"] and _normalize_fact_evidence(fact["dimension"]) not in evidence:
            fact["dimension"] = ""

        # 字段名、任务泛称和“已用时”只能作为指标锚点，不能独自承担业务对象。
        # 必须同时带有具体产品、工作项或目标场景，避免 duration/任务总耗时
        # 重新变成无上下文标题。
        if _generic_fact_anchor(fact["subject"]) and not _specific_fact_context(
            fact["target_context"]
        ):
            _reject("generic_anchor_without_specific_context")
            continue
        if _generic_execution_context(fact["target_context"]) and (
            _generic_fact_anchor(fact["subject"])
            or _value_like_fact_subject(fact["subject"], fact["value"])
        ):
            _reject("generic_execution_context")
            continue

        required = ("title", "subject", "metric", "value", "statement", "evidence_quote")
        if any(not fact[field] for field in required):
            _reject("missing_required_field")
            continue
        if fact["confidence"] not in {"low", "medium", "high"}:
            _reject("bad_confidence")
            continue
        if any(len(fact[field]) > limit for field, limit in (
            ("title", 120), ("subject", 80), ("metric", 60),
            ("value", 40), ("unit", 24), ("statement", 500), ("evidence_quote", 500),
        )):
            _reject("field_too_long")
            continue
        evidence = _normalize_fact_evidence(fact["evidence_quote"])
        if not _evidence_matches_source(evidence, source_normalized):
            _reject("evidence_not_verifiable")
            continue
        subject = _normalize_fact_evidence(fact["subject"])
        value = _normalize_fact_evidence(fact["value"])
        unit = _normalize_fact_evidence(fact["unit"])
        dimension = _normalize_fact_evidence(fact["dimension"])
        # 数字型 subject（如 "41.92%"、"2104张"、"T-2天"）是模型常见输出形态，
        # 只要其数字 token 逐字命中 evidence 即视为可靠。
        numeric_subject = any(ch.isdigit() for ch in subject)
        subject_tokens = set(re.findall(r"[0-9][0-9:.%]*", subject))
        numeric_subject_ok = (
            numeric_subject
            and bool(subject_tokens)
            and all(t in evidence for t in subject_tokens)
        )
        metric = _normalize_fact_evidence(fact["metric"])
        target_context = _normalize_fact_evidence(fact["target_context"])
        # 至少一个语义锚点和值必须回证；其余展示字段可降级，不整条丢弃。
        subject_ok = numeric_subject_ok or subject in evidence
        semantic_anchor_ok = subject_ok or metric in evidence or target_context in evidence
        # value 常带格式差异（如 "87%+" vs 原文 "87%"）：逐字命中失败时，
        # 退化为 value 的全部数字 token 都命中 evidence 即视为可靠。
        value_ok = value in evidence
        if not value_ok and value:
            value_digits = re.findall(r"[0-9][0-9:.]*", value)
            if value_digits and all(d in evidence for d in value_digits):
                value_ok = True
        if not semantic_anchor_ok:
            _reject("semantic_anchor_not_grounded")
            continue
        if not value_ok:
            _reject("value_not_grounded")
            continue
        if unit and unit not in evidence:
            _reject("unit_not_grounded")
            continue
        if dimension and dimension not in evidence:
            _reject("dimension_not_grounded")
            continue
        semantic_key = tuple(
            _normalize_fact_evidence(fact[field])
            for field in (
                "subject",
                "action",
                "target_context",
                "dimension",
                "metric",
                "value",
                "unit",
            )
        )
        if semantic_key in accepted_keys:
            continue
        accepted_keys.add(semantic_key)
        accepted.append(fact)

    # statement 是展示字段，不能成为结构化事实之间的串线入口。小模型偶尔会把
    # 同批多个指标写成同一句汇总：其中一条可能不含自己的值，其余条虽然碰巧
    # 含值，仍会在数据详情里展示成完全相同的内容。对缺少当前值或被多个原子
    # 事实复用的句子，用已经过上述回证门禁的字段确定性重写；逐字证据继续单独
    # 保存在 evidence_quote，不损失来源可追溯性。
    statement_counts: Dict[str, int] = {}
    for fact in accepted:
        normalized_statement = _normalize_fact_evidence(fact["statement"])
        statement_counts[normalized_statement] = statement_counts.get(normalized_statement, 0) + 1
    for fact in accepted:
        normalized_statement = _normalize_fact_evidence(fact["statement"])
        if statement_counts.get(normalized_statement, 0) > 1 or not _fact_value_is_stated(fact):
            fact["statement"] = _fact_specific_statement(fact)

    if rejected:
        logger.info(
            "data_facts 校验拒绝 %d/%d 条，原因分布: %s",
            rejected, rejected + len(accepted), reject_reasons,
        )
    return accepted, rejected


class KnowledgeExtractorV2:
    """时间线提炼器 V2 - 强制使用 LLM"""

    def __init__(self, model: Optional[str] = None, embedding_model=None, user_identity: str = ""):
        """
        初始化时间线提炼器

        Args:
            model: Ollama 模型名称
            embedding_model: 向量模型（用于去重）
            user_identity: 用户身份关键词，多个用逗号分隔（如 "张三,zhangsan"）
        """
        import requests
        self.ollama_base_url = "http://127.0.0.1:11434"
        # 使用全局统一的 Ollama 模型名，如果未传入则自动获取
        if model is None:
            from model_registry_global import get_active_ollama_model
            model = get_active_ollama_model()
        self.model = model
        # Ollama 推理 HTTP 超时：4B 模型在 M 系 Mac 上对 ~10k token prompt 的 P50 ≈ 135s、
        # P95 ≈ 342s，原先 90s 的设置导致 ~66% 请求被错杀。1200s 给冷启动 + 大 prompt 留余量。
        # 必须 ≥ model_api_server 的 300s 长输入预算；Core 外层当前为 310s。
        self.timeout = 1200

        # 测试 Ollama 是否可用
        try:
            r = requests.get(f"{self.ollama_base_url}/api/tags", timeout=5)
            if r.status_code >= 400:
                raise BakeModelRequestError(r.status_code, r.text)
            logger.info(f"✅ Ollama 服务连接成功，模型: {model}")
        except BakeModelRequestError:
            raise
        except requests.RequestException as exc:
            raise BakeModelTransportError("无法连接本地模型服务") from exc

        self.embedding_model = embedding_model
        if embedding_model:
            logger.info("✅ 向量模型已加载，将启用知识去重")

        self.user_identity = user_identity.strip()
        if self.user_identity:
            logger.info(f"✅ 用户身份已配置: {self.user_identity}")

    def _ollama_chat(self, messages, format=None, options=None):
        """使用可中断流调用 Ollama；在线 P0 到达时立即关闭后台响应。"""
        import requests
        from inference_queue import (
            current_task_preempt_requested,
            raise_if_preempted,
            register_current_preempt_callback,
        )

        raise_if_preempted()
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": False,  # 禁用 thinking 模式，确保 content 有内容
            # keep_alive=10m：与 RAG 查询保持一致，避免 Ollama 在查询和提炼之间频繁 swap
            "keep_alive": "10m",
        }
        if format:
            payload["format"] = _ollama_compatible_format(format)
        if options:
            payload["options"] = options

        try:
            with requests.post(
                f"{self.ollama_base_url}/api/chat",
                json=payload,
                timeout=self.timeout,
                stream=True,
            ) as response:
                unregister = register_current_preempt_callback(response.close)
                try:
                    if response.status_code >= 400:
                        raise BakeModelRequestError(response.status_code, response.text)
                    response.raise_for_status()
                    final: Dict[str, Any] = {}
                    content_parts: list[str] = []
                    thinking_parts: list[str] = []
                    accumulated_len = 0
                    last_repetition_check = 0
                    for raw_line in response.iter_lines():
                        raise_if_preempted()
                        if not raw_line:
                            continue
                        try:
                            chunk = json.loads(raw_line.decode("utf-8", errors="replace"))
                        except json.JSONDecodeError as exc:
                            raise BakeModelResponseError(
                                "本地模型流式响应不是合法 JSON"
                            ) from exc
                        final.update(chunk)
                        message = chunk.get("message") or {}
                        if message.get("content"):
                            piece = str(message["content"])
                            content_parts.append(piece)
                            accumulated_len += len(piece)
                            # 重复退化早断：同一 160 字块出现 4 次以上说明模型陷入
                            # 重复键循环（会把 num_predict 全部烧完），提前中止并
                            # 交由紧凑重试的 repeat_penalty 摆脱循环。
                            if accumulated_len >= 3000 and accumulated_len - last_repetition_check >= 1024:
                                last_repetition_check = accumulated_len
                                joined = "".join(content_parts)
                                window = joined[-160:]
                                if window.strip() and joined.count(window) >= 4:
                                    response.close()
                                    raise BakeOutputTruncatedError(
                                        "本地模型输出陷入重复退化，已提前中止"
                                    )
                        if message.get("thinking"):
                            thinking_parts.append(str(message["thinking"]))
                    final["message"] = {
                        **(final.get("message") or {}),
                        "content": "".join(content_parts),
                        "thinking": "".join(thinking_parts),
                    }
                finally:
                    unregister()
            raise_if_preempted()
            return final
        except Exception as exc:
            if current_task_preempt_requested():
                raise_if_preempted()
            if isinstance(
                exc,
                (
                    BakeInferenceTimeoutError,
                    BakeModelRequestError,
                    BakeModelTransportError,
                    BakeModelResponseError,
                ),
            ):
                raise
            if isinstance(exc, requests.Timeout):
                raise BakeInferenceTimeoutError("本地模型请求超时") from exc
            if isinstance(exc, requests.ConnectionError):
                raise BakeModelTransportError("无法连接本地模型服务") from exc
            if isinstance(exc, requests.RequestException):
                raise BakeModelTransportError("本地模型传输失败") from exc
            raise

    def _recover_missing_data_facts(
        self,
        source_text: str,
        result: Dict[str, Any],
        caller_id: str,
    ) -> tuple[List[Dict[str, Any]], int]:
        """主提炼遗漏数据时进行一次聚焦补提炼，仍走同一事实契约与证据门禁。"""
        if not _data_fact_retry_needed(source_text):
            return [], 0

        context_parts = []
        for field in ("work_item", "overview", "details"):
            value = _normalize_inline_text(result.get(field))
            if value and value not in context_parts:
                context_parts.append(value)
        context_hint = "\n".join(context_parts)
        if len(context_hint) > 1200:
            context_hint = context_hint[:1200]

        retry_prompt = (
            "主时间线提炼没有产出通过校验的数据事实，但原始采集包含明确数值。"
            "请只补提炼 data_facts，不要输出时间线、知识、文档或操作对象。\n"
            "工作语境只用于恢复标题、动作与目标场景，不能作为 evidence_quote；"
            "evidence_quote 必须逐字复制后面的原始采集证据。已经实际提交执行的"
            "生成参数、完成任务的耗时和验收结果属于观测事实；计划、建议和未执行"
            "配置不属于观测事实。复合时长必须完整保留，同义句和重复截图只输出一条。\n\n"
            f"工作语境:\n{context_hint or '未提供'}\n\n"
            f"原始采集证据:\n{source_text}\n\n"
            '只输出 {"data_facts": [...]}。'
        )
        try:
            response = self._ollama_chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是结构化数据事实补提炼器。"
                            + DATA_FACT_PROMPT.replace(
                                "（与上述时间线提炼在同一次输出中完成）", ""
                            )
                        ),
                    },
                    {"role": "user", "content": retry_prompt},
                ],
                format="json",
                options={
                    "temperature": 0.1,
                    "num_ctx": BAKE_CONTEXT_WINDOW_TOKENS,
                    "num_predict": BAKE_RETRY_NUM_PREDICT,
                    "repeat_penalty": BAKE_RETRY_REPEAT_PENALTY,
                },
            )
            parsed = _extract_json_object(_extract_ollama_response_text(response))
            if not parsed:
                logger.warning("数据事实聚焦补提炼无法解析: caller_id=%s", caller_id)
                return [], 0
            facts, rejected = _validated_data_facts(
                parsed.get("data_facts"), source_text
            )
            logger.info(
                "数据事实聚焦补提炼完成: caller_id=%s accepted=%d rejected=%d",
                caller_id,
                len(facts),
                rejected,
            )
            return facts, rejected
        except Exception as exc:
            logger.warning(
                "数据事实聚焦补提炼失败，不影响时间线落库: caller_id=%s error=%s",
                caller_id,
                type(exc).__name__,
            )
            return [], 0

    def _build_merge_system_prompt(self) -> str:
        """构建带用户身份的 MERGE_SYSTEM_PROMPT"""
        identity_clause = ""
        if self.user_identity:
            names = [n.strip() for n in self.user_identity.split(",") if n.strip()]
            names_str = "、".join(f'"{n}"' for n in names)
            identity_clause = (
                f"\n\n**用户身份信息**:屏幕的使用者是 {names_str}。"
                "在提炼时，请注意:\n"
                "- 如果屏幕内容是该用户自己操作、输入、编写的工作，activity_type 应正确标注为对应类型（coding/reading/chat 等）\n"
                "- 如果屏幕内容显示的是其他人（非该用户）的工作、他人的对话记录、别人的代码或文档，overview 中应明确说明「用户在查看他人的…」，importance 降低 1-2 分\n"
                "- 如果无法判断内容主体，按正常流程提炼，不要猜测"
            )
        return MERGE_SYSTEM_PROMPT + DATA_FACT_PROMPT + DATA_PAGE_PROMPT + identity_clause

    def _build_prompt(self, capture_data: Dict[str, Any]) -> str:
        """构建提炼 prompt"""
        app_name = capture_data.get('app_name', '未知应用')
        window_title = capture_data.get('window_title', '未知窗口')
        timestamp = capture_data.get('timestamp', datetime.now().isoformat())
        raw_text = (
            capture_data.get('ax_text')
            or capture_data.get('ocr_text')
            or capture_data.get('input_text')
            or capture_data.get('audio_text')
            or ''
        )
        ocr_text = _sanitize_capture_text(raw_text)

        # 限制文本长度，避免超过上下文；密度感知截断：先剔噪声行，
        # 密集正文保留完整配额，噪声为主的文本压缩更狠
        ocr_text = _density_aware_truncate(ocr_text, 2000)

        url_line = ""
        page_url = _normalize_page_url(capture_data.get('url'))
        if page_url:
            url_line = f"**页面URL**:{page_url}\n"

        prompt = f"""**应用名称**:{app_name}
**窗口标题**:{window_title}
{url_line}**时间戳**:{timestamp}
**OCR 文本**:
{ocr_text}

请提炼上述内容。要求:必须总结工作动作与结果，禁止照抄菜单词/窗口壳层词或原始 OCR 长串。"""

        return prompt

    def _find_similar_knowledge(
        self,
        overview: str,
        db_conn,
        threshold: float = _SIMILAR_MERGE_THRESHOLD,
        entities: Optional[List[str]] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> Optional[int]:
        """
        查找相似的知识条目

        Args:
            overview: 新的概述
            db_conn: 数据库连接
            threshold: 相似度阈值（0-1），默认 _SIMILAR_MERGE_THRESHOLD，
                作用于未含实体加分的基础余弦相似度
            entities: 新知识的实体列表，用于增强相似度判断

        Returns:
            相似知识条目的 ID，如果没有则返回 None
        """
        if not self.embedding_model:
            return None

        try:
            # 1. 获取新概述的向量
            new_embedding = self.embedding_model.encode([overview])[0]
            new_vector = np.array(new_embedding.vector)
            new_norm = np.linalg.norm(new_vector)
            if new_norm == 0:
                return None

            # 2. 获取所有现有知识条目（仅取最近 500 条，且限制在 24 小时内）
            merge_window_ms = 24 * 60 * 60 * 1000  # 24 小时
            time_filter = ""
            if end_time is not None:
                earliest_time = end_time - merge_window_ms
                time_filter = f" AND end_time >= {earliest_time}"
                logger.debug(f"合并窗口: 24小时内 (end_time={end_time}, earliest={earliest_time})")

            cursor = db_conn.execute(
                f"SELECT id, overview, entities, start_time, end_time, occurrence_count FROM timelines WHERE overview IS NOT NULL{time_filter} ORDER BY created_at DESC LIMIT 500"
            )
            existing_entries = cursor.fetchall()

            if not existing_entries:
                logger.debug("未找到候选合并记录（24小时内无记录）")
                return None

            logger.debug(f"候选合并记录: {len(existing_entries)} 条（24小时内）")

            # 3. 批量编码现有 overview，避免逐条调用
            existing_ids = [row[0] for row in existing_entries]
            existing_overviews = [row[1] or '' for row in existing_entries]
            existing_entities_raw = [row[2] for row in existing_entries]
            existing_start_times = [row[3] for row in existing_entries]
            existing_end_times = [row[4] for row in existing_entries]
            existing_occurrence_counts = [row[5] for row in existing_entries]

            batch_embeddings = self.embedding_model.encode(existing_overviews)
            existing_vectors = np.array([np.array(e.vector) for e in batch_embeddings])
            existing_norms = np.linalg.norm(existing_vectors, axis=1)

            # 4. 批量计算余弦相似度
            valid_mask = existing_norms > 0
            similarities = np.zeros(len(existing_entries))
            if valid_mask.any():
                similarities[valid_mask] = (
                    existing_vectors[valid_mask] @ new_vector
                ) / (existing_norms[valid_mask] * new_norm)

            # 5. 实体重叠增强:同名实体出现在两条知识中，小幅加分（有上限）。
            # 加分只用于候选排序，最终是否过阈以基础相似度为准，并叠加实体一致性门。
            new_entity_set = set(e.lower() for e in (entities or []) if e)
            base_similarities = similarities.copy()
            entity_overlaps = np.zeros(len(existing_entries), dtype=int)
            for i, raw in enumerate(existing_entities_raw):
                if not new_entity_set or not raw:
                    continue
                try:
                    existing_entity_set = set(e.lower() for e in json.loads(raw) if e)
                    overlap = new_entity_set & existing_entity_set
                    entity_overlaps[i] = len(overlap)
                    if overlap:
                        similarities[i] += min(0.02 * len(overlap), _SIMILAR_ENTITY_BONUS_MAX)
                except Exception:
                    pass

            # 5.5 增长上限过滤：已膨胀到上限的时间线不再作为合并候选，
            # 从源头避免宽泛主题时间线持续吞噬不相关内容（过度合并的根治）。
            for i, occ in enumerate(existing_occurrence_counts):
                try:
                    occ_val = int(occ or 0)
                except (TypeError, ValueError):
                    occ_val = 0
                if occ_val >= _SIMILAR_MERGE_MAX_OCCURRENCE:
                    similarities[i] = 0
                    continue
                ex_start = existing_start_times[i]
                ex_end = existing_end_times[i]
                if ex_start and ex_end and ex_end > ex_start:
                    if (int(ex_end) - int(ex_start)) / 3600000.0 > _SIMILAR_MERGE_MAX_SPAN_HOURS:
                        similarities[i] = 0

            # 6. 连续片段保护:时间重叠或紧邻的同一事件，直接排除合并
            continuity_gap_ms = 15 * 60 * 1000
            if start_time is not None and end_time is not None:
                for i, (existing_start, existing_end) in enumerate(zip(existing_start_times, existing_end_times)):
                    if existing_start is None or existing_end is None:
                        continue
                    overlaps = start_time <= existing_end and end_time >= existing_start
                    near_continuation = 0 <= start_time - existing_end <= continuity_gap_ms
                    if overlaps or near_continuation:
                        similarities[i] = 0  # 直接排除
                        logger.debug(
                            "跳过连续片段 (ID=%s, overlap=%s, gap_ms=%s)",
                            existing_ids[i],
                            overlaps,
                            max(0, start_time - existing_end),
                        )

            # 7. 取相似度最高的条目，并过"基础阈值 + 实体一致性"双重门：
            #    - 基础相似度（不含实体加分）必须达到 threshold；
            #    - 未达近乎重复阈值时，若新旧知识都带实体，必须至少有一个实体交集，
            #      否则视为"同一项目里的不同任务"，拒绝把整段采集并入已有时间线。
            best_idx = int(np.argmax(similarities))
            best_sim = float(similarities[best_idx])
            best_base_sim = float(base_similarities[best_idx])
            if best_sim >= threshold and best_base_sim >= threshold:
                entry_id = existing_ids[best_idx]
                near_duplicate = best_base_sim >= _SIMILAR_MERGE_NEAR_DUP_THRESHOLD
                has_existing_entities = bool(
                    str(existing_entities_raw[best_idx] or '').strip() not in ('', '[]')
                )
                entity_gate_ok = (
                    near_duplicate
                    or not new_entity_set
                    or not has_existing_entities
                    or int(entity_overlaps[best_idx]) > 0
                )
                if entity_gate_ok:
                    logger.info(
                        f"发现相似知识条目 (ID={entry_id}, 相似度={best_sim:.2f}, "
                        f"基础相似度={best_base_sim:.2f}, 实体交集={int(entity_overlaps[best_idx])})"
                    )
                    return entry_id
                logger.info(
                    "相似度合并被实体一致性门拦截: ID=%s 相似度=%.2f 但新旧知识无任何共同实体，改为新建时间线",
                    entry_id, best_sim,
                )

            return None

        except Exception as e:
            logger.error(f"查找相似知识失败: {e}")
            return None

    def _truncate_text(self, value: Any, limit: int) -> str:
        text = str(value or '').strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + " ...(已截断)"

    def _sanitize_bake_details_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            cleaned: Dict[str, Any] = {}
            for raw_key, raw_value in value.items():
                key = str(raw_key or '').strip()
                normalized_key = key.lower().replace('-', '_').replace(' ', '_')
                if normalized_key in BAKE_SCORE_METADATA_KEYS:
                    continue
                if any(keyword in normalized_key for keyword in BAKE_SCORE_METADATA_KEYWORDS):
                    continue
                nested = self._sanitize_bake_details_value(raw_value)
                if nested in (None, '', [], {}):
                    continue
                cleaned[key] = nested
            return cleaned

        if isinstance(value, list):
            cleaned_items = [self._sanitize_bake_details_value(item) for item in value]
            return [item for item in cleaned_items if item not in (None, '', [], {})]

        return value

    def _sanitize_bake_details_text(self, value: Any) -> str:
        if value is None:
            return ''

        if isinstance(value, (dict, list)):
            cleaned = self._sanitize_bake_details_value(value)
            if cleaned in (None, '', [], {}):
                return ''
            return json.dumps(cleaned, ensure_ascii=False)

        raw_text = str(value or '').strip()
        if not raw_text:
            return ''

        parsed = _try_parse_json_like_object(raw_text)
        if isinstance(parsed, dict):
            cleaned = self._sanitize_bake_details_value(parsed)
            if cleaned in (None, '', [], {}):
                return ''
            return json.dumps(cleaned, ensure_ascii=False)

        lines = []
        for line in raw_text.splitlines():
            normalized_line = line.lower()
            if any(keyword in normalized_line for keyword in BAKE_SCORE_METADATA_KEYWORDS):
                continue
            lines.append(line)
        return '\n'.join(lines).strip()

    def _build_bake_semantic_text(self, candidate: Dict[str, Any], payload: Dict[str, Any]) -> tuple[str, str]:
        candidate_text = "\n".join(
            str(candidate.get(field) or '')
            for field in (
                'summary',
                'overview',
                'details',
                'capture_ax_text',
                'capture_ocr_text',
                'capture_input_text',
                'capture_audio_text',
            )
        )
        entities = candidate.get('entities') or []
        if entities:
            candidate_text += "\n" + " ".join(str(item) for item in entities if item)

        payload_text = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else str(payload or '')
        return candidate_text, payload_text

    def _build_bake_candidate_text(self, candidate: Dict[str, Any]) -> str:
        entities = candidate.get('entities') or []
        entities_text = "、".join(str(item) for item in entities if item)
        sanitized_details = self._sanitize_bake_details_text(candidate.get('details'))
        capture_parts = [
            self._truncate_text(candidate.get('capture_ax_text'), BAKE_CAPTURE_AX_MAX_CHARS),
            self._truncate_text(candidate.get('capture_ocr_text'), 1000),
            self._truncate_text(candidate.get('capture_input_text'), 500),
            self._truncate_text(candidate.get('capture_audio_text'), 500),
        ]
        capture_text = "\n\n".join(part for part in capture_parts if part)
        if len(capture_text) > BAKE_CAPTURE_CONTEXT_MAX_CHARS:
            capture_text = (
                capture_text[:BAKE_CAPTURE_CONTEXT_MAX_CHARS].rstrip()
                + "\n...(已截断)"
            )

        url_aggregated_text = candidate.get('url_aggregated_text') or ''
        url_aggregated_count = candidate.get('url_aggregated_capture_count') or 0
        source_capture_count = self._source_capture_count(candidate)
        capture_url = candidate.get('capture_url') or ''
        webpage_title = candidate.get('capture_webpage_title') or ''
        action_trace_text = self._render_bake_action_trace(candidate.get('action_trace'))
        key_timestamps = candidate.get('key_timestamps')
        key_timestamps_text = (
            json.dumps(key_timestamps, ensure_ascii=False)
            if isinstance(key_timestamps, (dict, list))
            else str(key_timestamps or '')
        )
        url_block = ''
        if url_aggregated_text:
            context_label = (
                "multi_capture_context: 同一条时间线的多条采集记录，按时间顺序拼接的可见内容如下"
                if source_capture_count >= 2
                else "url_document_context: 同一 url 在过去被多次浏览/编辑，按时间顺序拼接的可见正文如下"
            )
            url_block = (
                f"\n\n{context_label}"
                f"（源记录共 {source_capture_count} 帧，本段纳入 {url_aggregated_count} 帧；仅用于 knowledge/document 上下文，不能替代 SOP 的 action_trace 证据）：\n"
                f"url: {capture_url}\n"
                f"webpage_title: {webpage_title}\n"
                f"---\n{url_aggregated_text}"
            )

        return (
            f"source_timeline_id: {candidate.get('source_timeline_id')}\n"
            f"source_capture_id: {candidate.get('source_capture_id')}\n"
            f"source_capture_count: {source_capture_count}\n"
            f"effective_capture_count: {self._effective_capture_count(candidate)}\n"
            f"sop_evidence_mode: {candidate.get('sop_evidence_mode') or ''}\n"
            f"timeline_category: {candidate.get('timeline_category') or ''}\n"
            f"work_item: {candidate.get('work_item') or ''}\n"
            f"work_status: {candidate.get('work_status') or ''}\n"
            f"work_progress: {candidate.get('work_progress') or ''}\n"
            f"summary: {self._truncate_text(candidate.get('summary'), 180)}\n"
            f"overview: {self._truncate_text(candidate.get('overview'), 280)}\n"
            f"details: {self._truncate_text(sanitized_details, 700)}\n"
            f"importance: {candidate.get('importance')}\n"
            f"occurrence_count: {candidate.get('occurrence_count')}\n"
            f"observed_at: {candidate.get('observed_at')}\n"
            f"event_time_start: {candidate.get('event_time_start')}\n"
            f"event_time_end: {candidate.get('event_time_end')}\n"
            f"start_time: {candidate.get('start_time')}\n"
            f"end_time: {candidate.get('end_time')}\n"
            f"duration_minutes: {candidate.get('duration_minutes')}\n"
            f"time_range_start: {candidate.get('time_range_start')}\n"
            f"time_range_end: {candidate.get('time_range_end')}\n"
            f"key_timestamps: {self._truncate_text(key_timestamps_text, 2_000)}\n"
            f"history_view: {bool(candidate.get('history_view', False))}\n"
            f"content_origin: {candidate.get('content_origin') or ''}\n"
            f"activity_type: {candidate.get('activity_type') or ''}\n"
            f"evidence_strength: {candidate.get('evidence_strength') or ''}\n"
            f"capture_ts: {candidate.get('capture_ts')}\n"
            f"capture_app_name: {self._truncate_text(candidate.get('capture_app_name'), 80)}\n"
            f"capture_win_title: {self._truncate_text(candidate.get('capture_win_title'), 120)}\n"
            f"capture_url: {capture_url}\n"
            f"capture_webpage_title: {webpage_title}\n"
            f"document_evidence: {json.dumps(self._resolve_document_evidence(candidate), ensure_ascii=False)}\n"
            f"entities: {self._truncate_text(entities_text, 160)}\n\n"
            f"capture_context:\n{capture_text}"
            f"\n\naction_trace（严格按 ts 排序，步骤证据必须引用 capture_id）:\n{action_trace_text}"
            f"{url_block}"
        )

    def _render_bake_action_trace(self, value: Any) -> str:
        if not isinstance(value, list):
            return ''
        rendered = []
        for item in value:
            if not isinstance(item, dict):
                continue
            header = (
                f"capture_id={item.get('capture_id')} ts={item.get('ts')} "
                f"event_type={item.get('event_type') or ''} "
                f"operation_evidence={bool(item.get('operation_evidence', False))} "
                f"evidence_kind={item.get('evidence_kind') or 'context'} "
                f"evidence_role={item.get('evidence_role') or 'context'} "
                f"evidence_reason={item.get('evidence_reason') or 'missing_contract'} "
                f"app={self._truncate_text(item.get('app_name'), 100)} "
                f"title={self._truncate_text(item.get('win_title'), 180)}"
            )
            parts = [header]
            if item.get('ax_focused_role') or item.get('ax_focused_id'):
                parts.append(
                    "focus="
                    f"{self._truncate_text(item.get('ax_focused_role'), 100)}:"
                    f"{self._truncate_text(item.get('ax_focused_id'), 240)}"
                )
            if item.get('url'):
                parts.append(f"url={self._truncate_text(item.get('url'), 500)}")
            if item.get('webpage_title'):
                parts.append(
                    f"webpage_title={self._truncate_text(item.get('webpage_title'), 180)}"
                )
            if item.get('state_delta'):
                parts.append(
                    f"state_delta: {self._truncate_text(item.get('state_delta'), 320)}"
                )
            for field, label, max_chars in (
                ('input_text', 'input', 300),
                ('visible_text', 'visible', 360 if item.get('operation_evidence', False) else 160),
                ('audio_text', 'audio', 300),
            ):
                text = str(item.get(field) or '').strip()
                if text:
                    parts.append(f"{label}: {self._truncate_text(text, max_chars)}")
            rendered.append('\n'.join(parts))
        return '\n---\n'.join(rendered)

    @staticmethod
    def _source_capture_count(candidate: Dict[str, Any]) -> int:
        """读取源采集帧数；旧调用未传契约字段时按单帧保守处理。"""
        try:
            return max(1, int(candidate.get('source_capture_count') or 1))
        except (TypeError, ValueError):
            return 1

    @classmethod
    def _effective_capture_count(cls, candidate: Dict[str, Any]) -> int:
        try:
            explicit = int(candidate.get('effective_capture_count') or 0)
        except (TypeError, ValueError):
            explicit = 0
        trace = candidate.get('action_trace')
        trace_count = 0
        if isinstance(trace, list):
            trace_count = sum(
                1
                for item in trace
                if isinstance(item, dict)
                and bool(item.get('operation_evidence', False))
                and str(item.get('evidence_role') or '').strip() in {'action', 'result'}
            )
        if explicit > 0:
            return explicit
        return trace_count

    @classmethod
    def _sop_has_multiple_source_captures(cls, candidate: Dict[str, Any]) -> bool:
        mode = str(candidate.get('sop_evidence_mode') or '').strip()
        if mode in {'direct_interaction', 'semantic_workflow'}:
            return (
                cls._source_capture_count(candidate) >= 2
                and cls._effective_capture_count(candidate) >= 2
            )
        # 旧 Core 没有显式证据通道时保留旧门禁，避免把兼容请求放宽。
        return cls._source_capture_count(candidate) >= 2 and cls._effective_capture_count(candidate) >= 3

    @staticmethod
    def _build_document_source_text(candidate: Dict[str, Any]) -> str:
        """只提取文档可见正文，用于确定性内容判重，不混入 timeline 元数据。"""
        aggregated = str(candidate.get('url_aggregated_text') or '').strip()
        if aggregated:
            return aggregated
        return "\n".join(
            str(candidate.get(field) or '').strip()
            for field in (
                'capture_ax_text',
                'capture_ocr_text',
                'capture_input_text',
                'capture_audio_text',
            )
            if str(candidate.get(field) or '').strip()
        )

    @staticmethod
    def _normalize_document_dedupe_text(value: str) -> str:
        return " ".join(str(value or '').lower().split())

    def _count_marker_hits(self, text: str, markers: tuple[str, ...]) -> int:
        normalized = str(text or '').lower()
        return sum(1 for marker in markers if marker and marker.lower() in normalized)

    def _resolve_document_evidence(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        provided = candidate.get('document_evidence')
        if isinstance(provided, dict) and provided.get('kind'):
            kind = str(provided.get('kind') or 'insufficient').strip().lower()
            allows_auto_create = provided.get('allows_auto_create')
            if allows_auto_create is None:
                allows_auto_create = kind != 'insufficient'
            return {
                'kind': kind,
                'source_surface': str(provided.get('source_surface') or 'other').strip().lower(),
                'has_document_url': bool(provided.get('has_document_url')),
                'has_document_page_title': bool(provided.get('has_document_page_title')),
                'has_substantive_document_body': bool(
                    provided.get('has_substantive_document_body')
                ),
                'allows_auto_create': bool(allows_auto_create) and kind != 'insufficient',
            }

        app_name = str(candidate.get('capture_app_name') or '').strip().lower()
        if any(marker in app_name for marker in CHAT_APP_MARKERS):
            source_surface = 'chat'
        elif any(marker in app_name for marker in BROWSER_APP_MARKERS):
            source_surface = 'browser'
        elif any(marker in app_name for marker in DOCUMENT_EDITOR_APP_MARKERS):
            source_surface = 'document_editor'
        else:
            source_surface = 'other'

        capture_url = str(candidate.get('capture_url') or '').strip().lower()
        title = str(
            candidate.get('capture_webpage_title')
            or candidate.get('capture_win_title')
            or ''
        ).strip().lower()
        has_document_url = any(marker in capture_url for marker in DOCUMENT_URL_MARKERS)
        has_document_page_title = any(
            marker in title for marker in DOCUMENT_TITLE_MARKERS
        )
        aggregated_body = str(candidate.get('url_aggregated_text') or '').strip()
        capture_body = '\n'.join(
            str(candidate.get(field) or '')
            for field in (
                'capture_ax_text',
                'capture_ocr_text',
                'capture_input_text',
                'capture_audio_text',
            )
        )
        has_substantive_document_body = (
            max(
                sum(1 for char in aggregated_body if not char.isspace()),
                sum(1 for char in capture_body if not char.isspace()),
            )
            >= MIN_DOCUMENT_EVIDENCE_CHARS
        )
        has_meaningful_native_title = bool(title) and title != app_name
        is_code_editor = any(marker in app_name for marker in CODE_EDITOR_APP_MARKERS)

        if not has_substantive_document_body or source_surface == 'chat':
            kind = 'insufficient'
        elif has_document_url:
            kind = 'document_url'
        elif source_surface == 'browser' and has_document_page_title:
            kind = 'browser_document'
        elif (
            source_surface == 'document_editor'
            and has_meaningful_native_title
            and (not is_code_editor or has_document_page_title)
        ):
            kind = 'native_document'
        else:
            kind = 'insufficient'

        return {
            'kind': kind,
            'source_surface': source_surface,
            'has_document_url': has_document_url,
            'has_document_page_title': has_document_page_title,
            'has_substantive_document_body': has_substantive_document_body,
            'allows_auto_create': kind != 'insufficient',
        }

    def _should_reject_template_like_knowledge(self, candidate: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        candidate_text, payload_text = self._build_bake_semantic_text(candidate, payload)
        candidate_template_hits = self._count_marker_hits(candidate_text, BAKE_TEMPLATE_MARKERS)
        payload_template_hits = self._count_marker_hits(payload_text, BAKE_TEMPLATE_MARKERS)
        knowledge_hits = self._count_marker_hits(candidate_text + "\n" + payload_text, BAKE_KNOWLEDGE_MARKERS)
        return candidate_template_hits >= 2 and payload_template_hits >= 1 and knowledge_hits == 0

    def _should_reject_sop_like_knowledge(self, candidate: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        candidate_text, payload_text = self._build_bake_semantic_text(candidate, payload)
        candidate_sop_hits = self._count_marker_hits(candidate_text, BAKE_SOP_MARKERS)
        payload_sop_hits = self._count_marker_hits(payload_text, BAKE_SOP_MARKERS)
        knowledge_hits = self._count_marker_hits(candidate_text + "\n" + payload_text, BAKE_KNOWLEDGE_MARKERS)
        return candidate_sop_hits >= 2 and payload_sop_hits >= 1 and knowledge_hits == 0

    def _resolve_bake_artifact_mismatch_reason(self, artifact_type: str, candidate: Dict[str, Any], payload: Dict[str, Any]) -> Optional[str]:
        candidate_text, payload_text = self._build_bake_semantic_text(candidate, payload)

        template_hits = self._count_marker_hits(candidate_text + "\n" + payload_text, BAKE_TEMPLATE_MARKERS)
        sop_hits = self._count_marker_hits(candidate_text + "\n" + payload_text, BAKE_SOP_MARKERS)
        knowledge_hits = self._count_marker_hits(candidate_text + "\n" + payload_text, BAKE_KNOWLEDGE_MARKERS)
        design_hits = self._count_marker_hits(candidate_text + "\n" + payload_text, BAKE_DESIGN_MARKERS)
        candidate_template_hits = self._count_marker_hits(candidate_text, BAKE_TEMPLATE_MARKERS)
        candidate_sop_hits = self._count_marker_hits(candidate_text, BAKE_SOP_MARKERS)
        candidate_design_hits = self._count_marker_hits(candidate_text, BAKE_DESIGN_MARKERS)

        if artifact_type == 'knowledge':
            if self._should_reject_template_like_knowledge(candidate, payload):
                return 'template_like_content'
            if self._should_reject_sop_like_knowledge(candidate, payload):
                return 'sop_like_content'
            return None

        if artifact_type == 'design':
            if sop_hits >= 5 and design_hits == 0 and candidate_design_hits == 0:
                return 'sop_like_content'
            return None

        if artifact_type == 'sop':
            if not self._sop_has_multiple_source_captures(candidate):
                return 'insufficient_multi_capture_evidence'
            if design_hits >= 3 and candidate_design_hits >= 2 and candidate_sop_hits <= 1:
                return 'design_like_content'
            if knowledge_hits >= 3 and sop_hits <= 1:
                return 'knowledge_like_content'
            return None

        return None

    def _downgrade_mismatch_payload(self, payload: Dict[str, Any], reason: str) -> Dict[str, Any]:
        adjusted = dict(payload)
        score = adjusted.get('match_score')
        if isinstance(score, (int, float)):
            adjusted['match_score'] = min(float(score), BAKE_MISMATCH_MAX_SCORE)
        else:
            adjusted['match_score'] = BAKE_MISMATCH_MAX_SCORE
        adjusted['match_level'] = 'low'
        adjusted['review_status'] = 'auto_created'
        evidence = str(adjusted.get('evidence_summary') or '').strip()
        adjusted['evidence_summary'] = f"{evidence} | mismatch_guard={reason}" if evidence else f"mismatch_guard={reason}"
        return adjusted

    def _normalize_sop_step_evidence(
        self,
        candidate: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """严格校验逐步证据；不再用整条 timeline 给缺证据步骤兜底。"""
        adjusted = dict(payload)
        source_ids = []
        evidence_ids = []
        evidence_roles = {}
        capture_timestamps = {}
        evidence_mode = str(candidate.get('sop_evidence_mode') or '').strip()
        trace = candidate.get('action_trace')
        if isinstance(trace, list):
            for item in trace:
                if not isinstance(item, dict):
                    continue
                capture_id = str(item.get('capture_id') or '').strip()
                if capture_id and capture_id not in source_ids:
                    source_ids.append(capture_id)
                role = str(item.get('evidence_role') or '').strip()
                is_evidence = bool(item.get('operation_evidence', False)) and role in {
                    'action', 'result'
                }
                if capture_id and is_evidence and capture_id not in evidence_ids:
                    evidence_ids.append(capture_id)
                    evidence_roles[capture_id] = role
                    try:
                        capture_timestamps[capture_id] = int(item.get('ts') or 0)
                    except (TypeError, ValueError):
                        capture_timestamps[capture_id] = 0
        source_id_set = set(source_ids)
        evidence_id_set = set(evidence_ids)

        steps = payload.get('steps')
        if not isinstance(steps, list) or not 3 <= len(steps) <= 8:
            return None, 'insufficient_sop_steps'
        if any(not str(step or '').strip() for step in steps):
            return None, 'insufficient_sop_steps'
        required_evidence_count = 2 if evidence_mode in {
            'direct_interaction', 'semantic_workflow'
        } else 3
        if len(evidence_ids) < required_evidence_count:
            return None, 'insufficient_operation_evidence_nodes'
        available_roles = set(evidence_roles.values())
        if 'action' not in available_roles:
            return None, 'missing_real_action'
        if 'result' not in available_roles:
            return None, 'missing_attributed_result'

        evidence_by_step = {}
        raw_evidence = payload.get('step_evidence')
        if not isinstance(raw_evidence, list):
            return None, 'missing_sop_step_evidence'
        for item in raw_evidence:
            if not isinstance(item, dict):
                return None, 'invalid_sop_step_evidence'
            try:
                step_index = int(item.get('step_index'))
            except (TypeError, ValueError):
                return None, 'invalid_sop_step_evidence'
            if step_index < 1 or step_index > len(steps) or step_index in evidence_by_step:
                return None, 'invalid_sop_step_evidence'
            capture_ids = item.get('capture_ids')
            if not isinstance(capture_ids, list) or not capture_ids:
                return None, 'missing_sop_step_evidence'
            normalized_ids = []
            for capture_id in capture_ids:
                normalized = str(capture_id or '').strip()
                if not normalized or normalized in normalized_ids:
                    continue
                if normalized not in source_id_set:
                    return None, 'invalid_sop_step_evidence'
                if normalized not in evidence_id_set:
                    return None, 'non_operation_sop_step_evidence'
                normalized_ids.append(normalized)
            if not normalized_ids:
                return None, 'missing_sop_step_evidence'
            evidence_by_step[step_index] = normalized_ids

        if set(evidence_by_step) != set(range(1, len(steps) + 1)):
            return None, 'missing_sop_step_evidence'

        previous_ts = None
        distinct_ids = set()
        for step_index in range(1, len(steps) + 1):
            capture_ids = evidence_by_step[step_index]
            step_ts = min(capture_timestamps.get(capture_id, 0) for capture_id in capture_ids)
            if previous_ts is not None and step_ts < previous_ts:
                return None, 'non_chronological_sop_step_evidence'
            previous_ts = step_ts
            distinct_ids.update(capture_ids)
        if len(distinct_ids) < 2:
            return None, 'insufficient_distinct_sop_evidence'

        adjusted['step_evidence'] = [
            {
                'step_index': index,
                'capture_ids': evidence_by_step[index],
            }
            for index in range(1, len(steps) + 1)
        ]
        return adjusted, None

    def _call_bake_llm(
        self,
        caller_id: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: Dict[str, Any] = BAKE_RESPONSE_SCHEMA,
        *,
        num_predict: int = BAKE_NUM_PREDICT,
        repeat_penalty: float = 1.0,
    ) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        from monitor.llm_tracker import LLMCallTracker, estimate_tokens

        started_at = time.time()
        logger.info("bake llm start caller=%s", caller_id)
        with LLMCallTracker(
            caller="bake",
            model_name=self.model,
            caller_id=caller_id,
        ) as tracker:
            response = self._ollama_chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                format=response_schema,
                options={
                    "temperature": 0.0,
                    "num_ctx": BAKE_CONTEXT_WINDOW_TOKENS,
                    "num_predict": num_predict,
                    "repeat_penalty": repeat_penalty,
                },
            )
            raw_content = _extract_ollama_response_text(response)
            tracker.set_response(response)
            tracker.set_trace(
                raw_preview=_preview_text(raw_content, 4000),
                response_preview=_preview_text(response, 4000),
                done_reason=response.get("done_reason"),
            )
            if tracker._prompt_tokens == 0:
                tracker.set_tokens(
                    prompt=estimate_tokens(system_prompt + user_prompt),
                    completion=estimate_tokens(raw_content),
                )

        elapsed_ms = int((time.time() - started_at) * 1000)
        done_reason = response.get("done_reason")
        if done_reason == "length":
            _append_bake_error_log(
                "bake LLM output exceeded num_predict",
                caller_id=caller_id,
                model=self.model,
                num_predict=num_predict,
                prompt_chars=len(system_prompt) + len(user_prompt),
                raw_len=len(raw_content),
                prompt_tokens=(response.get("usage") or {}).get("prompt_tokens") or response.get("prompt_eval_count"),
                completion_tokens=(response.get("usage") or {}).get("completion_tokens") or response.get("eval_count"),
                elapsed_ms=elapsed_ms,
                done_reason=done_reason,
                raw_head=_preview_text(raw_content, 2000),
                raw_tail=raw_content[-2000:] if raw_content else "",
                response_preview=_preview_text(response, 4000),
            )
            logger.error(
                "bake LLM output exceeded num_predict caller=%s num_predict=%s raw_len=%s",
                caller_id,
                num_predict,
                len(raw_content),
            )
        logger.info(
            "bake llm done caller=%s elapsed_ms=%s raw_len=%s",
            caller_id,
            elapsed_ms,
            len(raw_content),
        )

        parsed = _extract_json_object(raw_content)
        if parsed is None:
            # 监控日志只保留 800 字预览，定位解析失败必须拿完整原文；
            # 失败样本量小，全文落盘到专用错误日志供离线分析。
            _append_bake_error_log(
                "bake LLM output unparseable",
                caller_id=caller_id,
                model=self.model,
                done_reason=done_reason,
                raw_len=len(raw_content),
                raw_full=raw_content[:131072],
            )
            logger.warning(
                "bake llm raw response caller=%s raw=%s response=%s",
                caller_id,
                _preview_text(raw_content, 800),
                _preview_text(response, 800),
            )
        usage = response.get('usage') or {}
        usage_summary = {
            'prompt_tokens': usage.get('prompt_tokens') or response.get('prompt_eval_count') or estimate_tokens(system_prompt + user_prompt),
            'completion_tokens': usage.get('completion_tokens') or response.get('eval_count') or estimate_tokens(raw_content),
        }
        return parsed, {
            'usage': usage_summary,
            'model': response.get('model') or self.model,
            'raw_content': raw_content,
            'raw_preview': _preview_text(raw_content),
            'response_preview': _preview_text(response),
            'done_reason': response.get('done_reason'),
            'empty_content': not bool(raw_content.strip()),
            'elapsed_ms': elapsed_ms,
        }

    def _extract_bake_artifact(self, candidate: Dict[str, Any], artifact_type: str, artifact_prompt: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
        candidate_text = self._build_bake_candidate_text(candidate)
        system_prompt = BAKE_SHARED_PROMPT + "\n\n" + artifact_prompt
        user_prompt = f"候选输入如下:\n\n{candidate_text}"
        caller_id = f"{artifact_type}:{candidate.get('source_timeline_id')}"
        started_at = time.time()
        logger.info("bake artifact start type=%s caller=%s", artifact_type, caller_id)

        try:
            parsed, meta = self._call_bake_llm(caller_id, system_prompt, user_prompt)
        except Exception as e:
            elapsed_ms = int((time.time() - started_at) * 1000)
            logger.error("bake %s 提炼失败 caller=%s elapsed_ms=%s error=%s", artifact_type, caller_id, elapsed_ms, e)
            return {
                'accepted': False,
                'reason': f'llm_error: {e}',
                'payload': None,
            }, {
                'usage': None,
                'model': self.model,
                'degraded': True,
                'elapsed_ms': elapsed_ms,
            }

        elapsed_ms = int((time.time() - started_at) * 1000)

        if not parsed:
            reason = 'empty_content' if meta.get('empty_content') else (
                'truncated_json' if meta.get('done_reason') == 'length' else 'invalid_json'
            )
            logger.warning(
                "bake %s 提炼响应不可解析 caller=%s reason=%s elapsed_ms=%s raw=%s response=%s",
                artifact_type,
                caller_id,
                reason,
                elapsed_ms,
                meta.get('raw_preview', ''),
                meta.get('response_preview', ''),
            )
            return {
                'accepted': False,
                'reason': reason,
                'payload': None,
            }, {
                'usage': meta['usage'],
                'model': meta['model'],
                'degraded': True,
                'elapsed_ms': elapsed_ms,
            }

        accepted = bool(parsed.get('accepted', False))
        reason = parsed.get('reason')
        payload = parsed.get('payload')
        if accepted and payload is None:
            logger.warning(
                "bake %s accepted without payload caller=%s elapsed_ms=%s",
                artifact_type,
                caller_id,
                elapsed_ms,
            )
            return {
                'accepted': False,
                'reason': 'accepted_without_payload',
                'payload': None,
            }, {
                'usage': meta['usage'],
                'model': meta['model'],
                'degraded': True,
                'elapsed_ms': elapsed_ms,
            }

        if accepted and not isinstance(payload, dict):
            logger.warning(
                "bake %s malformed payload caller=%s elapsed_ms=%s payload_type=%s",
                artifact_type,
                caller_id,
                elapsed_ms,
                type(payload).__name__,
            )
            return {
                'accepted': False,
                'reason': 'malformed_payload',
                'payload': None,
            }, {
                'usage': meta['usage'],
                'model': meta['model'],
                'degraded': True,
                'elapsed_ms': elapsed_ms,
            }

        if accepted:
            if artifact_type == 'sop':
                payload, evidence_error = self._normalize_sop_step_evidence(candidate, payload)
                if evidence_error:
                    return {
                        'accepted': False,
                        'reason': evidence_error,
                        'payload': None,
                    }, {
                        'usage': meta['usage'],
                        'model': meta['model'],
                        'degraded': False,
                        'elapsed_ms': elapsed_ms,
                    }
            if artifact_type == 'design':
                document_evidence = self._resolve_document_evidence(candidate)
                if not document_evidence['allows_auto_create']:
                    logger.info(
                        "bake design rejected by document evidence guard caller=%s source_timeline_id=%s evidence_kind=%s source_surface=%s has_document_url=%s has_document_page_title=%s has_substantive_document_body=%s",
                        caller_id,
                        candidate.get('source_timeline_id'),
                        document_evidence['kind'],
                        document_evidence['source_surface'],
                        document_evidence['has_document_url'],
                        document_evidence['has_document_page_title'],
                        document_evidence['has_substantive_document_body'],
                    )
                    return {
                        'accepted': False,
                        'reason': 'insufficient_document_evidence',
                        'payload': None,
                    }, {
                        'usage': meta['usage'],
                        'model': meta['model'],
                        'degraded': False,
                        'elapsed_ms': elapsed_ms,
                    }

            mismatch_reason = self._resolve_bake_artifact_mismatch_reason(artifact_type, candidate, payload)
            if mismatch_reason and (
                artifact_type == 'knowledge'
                or mismatch_reason in {
                    'insufficient_sop_evidence',
                    'insufficient_multi_capture_evidence',
                }
            ):
                logger.info(
                    "bake artifact rejected as mismatch caller=%s elapsed_ms=%s reason=%s",
                    caller_id,
                    elapsed_ms,
                    mismatch_reason,
                )
                return {
                    'accepted': False,
                    'reason': mismatch_reason,
                    'payload': None,
                }, {
                    'usage': meta['usage'],
                    'model': meta['model'],
                    'degraded': False,
                    'elapsed_ms': elapsed_ms,
                }

            if mismatch_reason:
                payload = self._downgrade_mismatch_payload(payload, mismatch_reason)
                reason = reason or mismatch_reason
                logger.info(
                    "bake %s mismatch downgraded caller=%s elapsed_ms=%s reason=%s score=%s level=%s",
                    artifact_type,
                    caller_id,
                    elapsed_ms,
                    mismatch_reason,
                    payload.get('match_score'),
                    payload.get('match_level'),
                )

        logger.info(
            "bake artifact done type=%s caller=%s accepted=%s elapsed_ms=%s reason=%s",
            artifact_type,
            caller_id,
            accepted,
            elapsed_ms,
            reason,
        )

        if not accepted:
            return {
                'accepted': False,
                'reason': reason or 'rejected',
                'payload': None,
            }, {
                'usage': meta['usage'],
                'model': meta['model'],
                'degraded': False,
                'elapsed_ms': elapsed_ms,
            }

        return {
            'accepted': True,
            'reason': reason,
            'payload': payload,
        }, {
            'usage': meta['usage'],
            'model': meta['model'],
            'degraded': False,
            'elapsed_ms': elapsed_ms,
        }

    @staticmethod
    def _unwrap_bake_artifact_envelope(
        parsed: Any,
    ) -> tuple[Optional[Dict[str, Any]], Optional[str], str]:
        """兼容小模型把单个资产对象误包成数组的稳定退化形态。"""
        if isinstance(parsed, dict):
            return parsed, None, 'object'
        if parsed is None:
            return None, 'artifact_absent', 'null'
        if not isinstance(parsed, list):
            return None, 'artifact_malformed', type(parsed).__name__
        if not parsed:
            return None, 'empty_artifact', 'array_empty'
        if len(parsed) == 1:
            if isinstance(parsed[0], dict):
                return parsed[0], None, 'array_single_recovered'
            return None, 'artifact_malformed', 'array_single_malformed'
        if not all(isinstance(item, dict) for item in parsed):
            return None, 'artifact_malformed', 'array_mixed'
        accepted = [item for item in parsed if bool(item.get('accepted'))]
        if len(accepted) == 1:
            return accepted[0], None, 'array_unique_accepted_recovered'
        return None, 'ambiguous_artifact_array', 'array_ambiguous'

    def _normalize_bake_artifact_result(
        self,
        candidate: Dict[str, Any],
        artifact_type: str,
        parsed: Any,
        meta: Dict[str, Any],
        *,
        caller_id: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """校验一次 bundle 调用中的单个类别结果，并复用原有 mismatch guard。"""
        elapsed_ms = int(meta.get('elapsed_ms') or 0)
        usage = meta.get('usage')
        model = meta.get('model') or self.model

        parsed, shape_error, shape = self._unwrap_bake_artifact_envelope(parsed)
        recovered = shape in {
            'array_single_recovered',
            'array_unique_accepted_recovered',
        }
        logger.info(
            "bake bundle artifact shape type=%s caller=%s shape=%s recovered=%s",
            artifact_type,
            caller_id,
            shape,
            recovered,
        )
        if parsed is None:
            return {
                'accepted': False,
                'reason': meta.get('parse_failure_reason') or shape_error or 'artifact_absent',
                'payload': None,
            }, {
                'usage': usage,
                'model': model,
                'degraded': True,
                'elapsed_ms': elapsed_ms,
                'artifact_shape': shape,
                'compatibility_recovered': False,
            }

        accepted = bool(parsed.get('accepted', False))
        reason = parsed.get('reason')
        payload = parsed.get('payload')
        if accepted and payload is None:
            return {
                'accepted': False,
                'reason': 'accepted_without_payload',
                'payload': None,
            }, {
                'usage': usage,
                'model': model,
                'degraded': True,
                'elapsed_ms': elapsed_ms,
                'artifact_shape': shape,
                'compatibility_recovered': recovered,
            }
        if accepted and not isinstance(payload, dict):
            return {
                'accepted': False,
                'reason': 'malformed_payload',
                'payload': None,
            }, {
                'usage': usage,
                'model': model,
                'degraded': True,
                'elapsed_ms': elapsed_ms,
                'artifact_shape': shape,
                'compatibility_recovered': recovered,
            }

        if accepted:
            if artifact_type == 'sop':
                payload, evidence_error = self._normalize_sop_step_evidence(candidate, payload)
                if evidence_error:
                    return {
                        'accepted': False,
                        'reason': evidence_error,
                        'payload': None,
                    }, {
                        'usage': usage,
                        'model': model,
                        'degraded': False,
                        'elapsed_ms': elapsed_ms,
                        'artifact_shape': shape,
                        'compatibility_recovered': recovered,
                    }
            if artifact_type == 'design':
                document_evidence = self._resolve_document_evidence(candidate)
                if not document_evidence['allows_auto_create']:
                    logger.info(
                        "bake design rejected by document evidence guard caller=%s source_timeline_id=%s evidence_kind=%s source_surface=%s has_document_url=%s has_document_page_title=%s has_substantive_document_body=%s",
                        caller_id,
                        candidate.get('source_timeline_id'),
                        document_evidence['kind'],
                        document_evidence['source_surface'],
                        document_evidence['has_document_url'],
                        document_evidence['has_document_page_title'],
                        document_evidence['has_substantive_document_body'],
                    )
                    return {
                        'accepted': False,
                        'reason': 'insufficient_document_evidence',
                        'payload': None,
                    }, {
                        'usage': usage,
                        'model': model,
                        'degraded': False,
                        'elapsed_ms': elapsed_ms,
                        'artifact_shape': shape,
                        'compatibility_recovered': recovered,
                    }

            mismatch_reason = self._resolve_bake_artifact_mismatch_reason(
                artifact_type,
                candidate,
                payload,
            )
            if mismatch_reason and (
                artifact_type == 'knowledge'
                or mismatch_reason in {
                    'insufficient_sop_evidence',
                    'insufficient_multi_capture_evidence',
                }
            ):
                return {
                    'accepted': False,
                    'reason': mismatch_reason,
                    'payload': None,
                }, {
                    'usage': usage,
                    'model': model,
                    'degraded': False,
                    'elapsed_ms': elapsed_ms,
                    'artifact_shape': shape,
                    'compatibility_recovered': recovered,
                }
            if mismatch_reason:
                payload = self._downgrade_mismatch_payload(payload, mismatch_reason)
                reason = reason or mismatch_reason

        logger.info(
            "bake bundle artifact normalized type=%s caller=%s accepted=%s reason=%s",
            artifact_type,
            caller_id,
            accepted,
            reason,
        )
        if not accepted:
            return {
                'accepted': False,
                'reason': reason or 'rejected',
                'payload': None,
            }, {
                'usage': usage,
                'model': model,
                'degraded': False,
                'elapsed_ms': elapsed_ms,
                'artifact_shape': shape,
                'compatibility_recovered': recovered,
            }

        return {
            'accepted': True,
            'reason': reason,
            'payload': payload,
        }, {
            'usage': usage,
            'model': model,
            'degraded': False,
            'elapsed_ms': elapsed_ms,
            'artifact_shape': shape,
            'compatibility_recovered': recovered,
        }

    def extract_bake_knowledge(self, candidate: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        return self._extract_bake_artifact(candidate, 'knowledge', BAKE_KNOWLEDGE_PROMPT)

    def extract_bake_design(self, candidate: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        return self._extract_bake_artifact(candidate, 'design', BAKE_DESIGN_PROMPT)

    def extract_bake_sop(self, candidate: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        return self._extract_bake_artifact(candidate, 'sop', BAKE_SOP_PROMPT)

    def _bundle_prompt_token_estimate(self, candidate: Dict[str, Any]) -> int:
        from monitor.llm_tracker import estimate_tokens

        candidate_text = self._build_bake_candidate_text(candidate)
        schema_text = json.dumps(BAKE_BUNDLE_RESPONSE_SCHEMA, ensure_ascii=False)
        raw_estimate = estimate_tokens(
            f"{BAKE_BUNDLE_PROMPT}\n候选输入如下:\n\n{candidate_text}\n{schema_text}"
        )
        return int(raw_estimate * BAKE_TOKEN_ESTIMATE_SAFETY_FACTOR + 0.999)

    def _candidate_with_context_budget(
        self,
        candidate: Dict[str, Any],
        context_char_budget: int,
    ) -> Dict[str, Any]:
        """按一个共享字符预算裁剪 capture 与 URL 聚合正文，避免两份上下文叠加失控。"""
        adjusted = dict(candidate)
        total_budget = max(0, int(context_char_budget))
        action_trace = candidate.get('action_trace')
        action_trace_chars = len(
            json.dumps(action_trace, ensure_ascii=False)
        ) if isinstance(action_trace, list) else 0
        action_trace_budget = min(action_trace_chars, total_budget // 2)
        adjusted['action_trace'] = self._trim_bake_action_trace(
            action_trace,
            action_trace_budget,
        )
        total_budget = max(0, total_budget - action_trace_budget)
        url_text = str(candidate.get('url_aggregated_text') or '').strip()

        if url_text:
            # 聚合正文通常已经包含主 capture；只保留少量主 capture 作为定位上下文，
            # 把大部分预算留给去重后的累计文档正文。
            capture_budget = min(3_000, total_budget // 5)
            url_budget = max(0, total_budget - capture_budget)
            adjusted['url_aggregated_text'] = self._head_tail_context(
                url_text,
                url_budget,
            )
        else:
            capture_budget = total_budget

        capture_fields = (
            ('capture_ax_text', 0.70),
            ('capture_ocr_text', 0.15),
            ('capture_input_text', 0.10),
            ('capture_audio_text', 0.05),
        )
        allocated = 0
        for index, (field, share) in enumerate(capture_fields):
            field_budget = (
                max(0, capture_budget - allocated)
                if index == len(capture_fields) - 1
                else max(0, int(capture_budget * share))
            )
            allocated += field_budget
            adjusted[field] = self._head_tail_context(
                candidate.get(field),
                field_budget,
            )
        return adjusted

    def _trim_bake_action_trace(self, value: Any, char_budget: int) -> List[Dict[str, Any]]:
        """裁剪每帧正文但始终保留帧 ID 与时间顺序，供步骤证据引用。"""
        if not isinstance(value, list):
            return []
        frames = [dict(item) for item in value if isinstance(item, dict)]
        if not frames:
            return []
        serialized = json.dumps(frames, ensure_ascii=False)
        if len(serialized) <= max(0, int(char_budget)):
            return frames

        minimal = []
        for frame in frames:
            minimal.append({
                'capture_id': frame.get('capture_id'),
                'ts': frame.get('ts'),
                'event_type': self._truncate_text(frame.get('event_type'), 32),
                'operation_evidence': bool(frame.get('operation_evidence', False)),
                'evidence_kind': self._truncate_text(frame.get('evidence_kind'), 32),
                'evidence_role': self._truncate_text(frame.get('evidence_role'), 16),
                'evidence_reason': self._truncate_text(frame.get('evidence_reason'), 48),
                'app_name': self._truncate_text(frame.get('app_name'), 48),
                'win_title': self._truncate_text(frame.get('win_title'), 80),
                'ax_focused_role': self._truncate_text(frame.get('ax_focused_role'), 48),
                'ax_focused_id': self._truncate_text(frame.get('ax_focused_id'), 80),
                'state_delta': self._truncate_text(frame.get('state_delta'), 160),
            })
        minimal_size = len(json.dumps(minimal, ensure_ascii=False))
        remaining = max(0, int(char_budget) - minimal_size)
        per_frame = remaining // len(minimal)
        if per_frame <= 0:
            return minimal

        for target, source in zip(minimal, frames):
            url_budget = min(160, per_frame // 5)
            text_budget = max(0, per_frame - url_budget)
            if source.get('url') and url_budget > 0:
                target['url'] = self._head_tail_context(source.get('url'), url_budget)
            target['input_text'] = self._head_tail_context(
                source.get('input_text'),
                text_budget // 5,
            )
            target['visible_text'] = self._head_tail_context(
                source.get('visible_text'),
                text_budget * 13 // 20,
            )
            target['audio_text'] = self._head_tail_context(
                source.get('audio_text'),
                text_budget * 3 // 20,
            )
        return minimal

    def _prepare_bake_bundle_candidate(
        self,
        candidate: Dict[str, Any],
        input_token_budget: int = BAKE_INPUT_TOKEN_BUDGET,
    ) -> Dict[str, Any]:
        """把 bundle prompt 压进输入预算，为完整 JSON 输出预留固定空间。"""
        original = dict(candidate)
        original_estimate = self._bundle_prompt_token_estimate(original)
        if original_estimate <= input_token_budget:
            return original

        context_fields = (
            'action_trace',
            'url_aggregated_text',
            'capture_ax_text',
            'capture_ocr_text',
            'capture_input_text',
            'capture_audio_text',
        )
        high = sum(len(str(candidate.get(field) or '')) for field in context_fields)
        low = 0
        best = self._candidate_with_context_budget(candidate, 0)
        while low <= high:
            middle = (low + high) // 2
            current = self._candidate_with_context_budget(candidate, middle)
            if self._bundle_prompt_token_estimate(current) <= input_token_budget:
                best = current
                low = middle + 1
            else:
                high = middle - 1

        fitted_estimate = self._bundle_prompt_token_estimate(best)
        logger.info(
            "bake bundle prompt fitted source_timeline_id=%s estimated_tokens=%s->%s budget=%s context_chars=%s",
            candidate.get('source_timeline_id'),
            original_estimate,
            fitted_estimate,
            input_token_budget,
            sum(len(str(best.get(field) or '')) for field in context_fields),
        )
        return best

    def estimate_bake_bundle_prompt_tokens(self, candidate: Dict[str, Any]) -> int:
        """在入队前估算 bundle 的完整输入规模，用于选择运行时熔断档位。"""
        return self._bundle_prompt_token_estimate(candidate)

    def estimate_merge_document_prompt_tokens(
        self,
        existing_document: Dict[str, Any],
        candidate: Dict[str, Any],
    ) -> int:
        """估算文档合并输入；与实际调用保持相同的首尾截断上限。"""
        from monitor.llm_tracker import estimate_tokens

        existing_content = self._head_tail_context(
            existing_document.get('full_content'),
            BAKE_DOCUMENT_MERGE_EXISTING_CONTEXT_MAX_CHARS,
        )
        candidate_content = self._head_tail_context(
            self._build_bake_candidate_text(candidate),
            BAKE_DOCUMENT_MERGE_CANDIDATE_CONTEXT_MAX_CHARS,
        )
        schema_text = json.dumps(BAKE_MERGE_DOCUMENT_SCHEMA, ensure_ascii=False)
        prompt_payload = {
            'title': existing_document.get('title'),
            'summary': existing_document.get('summary'),
            'sections_json': existing_document.get('sections_json'),
            'existing_content_context': existing_content,
            'new_capture_context': candidate_content,
        }
        return estimate_tokens(
            json.dumps(prompt_payload, ensure_ascii=False) + schema_text
        ) + 900

    def merge_bake_document(self, existing_document: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
        """将新的 candidate capture 内容合并到已有文档，返回更新后的字段。

        两层过滤 + 懒惰合并策略：
        1. 字面去重：可见文档正文完全相同 → 直接丢弃（0次LLM）
        2. LLM判断：正文有任何差异时调用（1次LLM，合并去重+插入判断为一体）

        不能用 embedding 高相似直接判定 no_change：同一文档的小幅修订天然高度
        相似，但新增的数字、结论或步骤仍必须进入补丁合并。
        """
        existing_title = existing_document.get('title') or ''
        existing_content = existing_document.get('full_content') or ''
        candidate_text = self._build_bake_candidate_text(candidate)
        candidate_source_text = self._build_document_source_text(candidate)

        # Layer 1: 只比较同一语义域的正文，不能再拿生成正文 hash 与带元数据的
        # candidate prompt hash 比较；那两者天然不可能相等。
        existing_normalized = self._normalize_document_dedupe_text(existing_content)
        candidate_normalized = self._normalize_document_dedupe_text(candidate_source_text)
        if existing_normalized and candidate_normalized == existing_normalized:
            logger.info("L1去重：可见正文完全匹配 source_timeline_id=%s", candidate.get('source_timeline_id'))
            merged = {'no_change': True, 'title': existing_title}
        else:
            # Layer 2: 任何非完全相同的来源正文都交给补丁合并，避免漏掉小改动。
            merged = self._merge_with_llm_once(existing_document, candidate, candidate_text)

        return self._ensure_document_identity_coverage(
            existing_document,
            candidate,
            merged,
        )

    def _merge_with_llm_once(self, existing_document: Dict[str, Any], candidate: Dict[str, Any], candidate_text: str) -> Dict[str, Any]:
        """让 LLM 只返回内容补丁，再在本地合入同一条文档，保证已有正文不会丢失。"""
        existing_title = existing_document.get('title') or ''
        existing_content = existing_document.get('full_content') or ''
        existing_summary = existing_document.get('summary') or ''
        sections_json = existing_document.get('sections_json', '[]')

        try:
            sections = json.loads(sections_json) if isinstance(sections_json, str) else sections_json
        except Exception:
            sections = []

        section_structure = "\n".join(
            f"{i+1}. {s.get('title', '未命名章节')}: {str(s.get('notes') or '')[:60]}"
            for i, s in enumerate(sections)
        ) if sections else "（文档无章节结构）"

        system_prompt = """你在执行同一份 bake 文档的内容合并。

**输入**：已有文档 + 新 capture 内容

**任务**：
1. 判断新内容是否已被文档完全覆盖（即新内容是已有内容的子集或同义复述）
2. 如果有新信息，只输出已有文档中尚不存在的 Markdown 内容补丁
3. 不要复述已有正文，不要输出合并后的完整文档；程序会在本地保留旧正文并合入补丁
4. 文档标题必须保持为输入中的已有标题，不得改成“文档增量”“新增内容”“补充内容”等过程性名称

**输出 JSON**：
{
  "no_change": true/false,  // true=新内容已完全覆盖，无需更新
  "title": "文档标题",
  "summary": "一句话摘要",
  "content_patch": "仅包含新增信息的 Markdown 片段（仅当no_change=false时必填）",
  "insert_mode": "append|no_change",
  "target_section_index": null,
  "evidence_summary": "本次更新说明",
  "new_info_summary": "新增信息点（一句话）",
  "match_score": 0.0-1.0,
  "match_level": "high|medium|low"
}

**规则**：
- no_change=true 时 content_patch 必须为 null
- no_change=false 时 content_patch 只能包含新增且有据可查的内容
- 即使新内容是在修正旧章节，也用带明确小标题的补充说明表达，不要复制或改写旧正文
- 严禁输出 full_content；旧正文不在模型侧重写
- 严禁重复：相同信息只保留一次
"""
        existing_context = self._head_tail_context(
            existing_content,
            BAKE_DOCUMENT_MERGE_EXISTING_CONTEXT_MAX_CHARS,
        )
        candidate_context = self._head_tail_context(
            candidate_text,
            BAKE_DOCUMENT_MERGE_CANDIDATE_CONTEXT_MAX_CHARS,
        )
        user_prompt = (
            f"已有文档:\ntitle: {existing_title}\nsummary: {existing_summary}\n"
            f"章节结构:\n{section_structure}\n\n"
            f"existing_content_context（只用于查重，原文由程序完整保留）:\n{existing_context}\n\n"
            f"new_capture_context:\n{candidate_context}"
        )

        parsed, _ = self._call_bake_llm(
            f"merge_doc:{candidate.get('source_timeline_id')}",
            system_prompt,
            user_prompt,
            response_schema=BAKE_MERGE_DOCUMENT_SCHEMA,
        )

        if not parsed or not isinstance(parsed, dict):
            return {'title': existing_title, 'no_change': True}

        if bool(parsed.get('no_change')):
            parsed.pop('content_patch', None)
            parsed.pop('full_content', None)
            parsed['title'] = existing_title
            return parsed

        content_patch = str(parsed.pop('content_patch', '') or '').strip()
        if content_patch:
            merged_content = self._append_document_patch(existing_content, content_patch)
            if merged_content == existing_content:
                parsed['no_change'] = True
                parsed['insert_mode'] = 'no_change'
                parsed.pop('full_content', None)
            else:
                parsed['no_change'] = False
                parsed['insert_mode'] = 'append'
                parsed['full_content'] = merged_content
            parsed['title'] = existing_title
            return parsed

        # 向后兼容旧模型偶发返回 full_content，但只有它逐字包含全部旧正文时才接受。
        # 任何“重写后变短”的结果都视为 no_change，防止再次发生 171 号文档式截断。
        legacy_full_content = str(parsed.get('full_content') or '').strip()
        if legacy_full_content and self._content_preserves_existing(
            existing_content,
            legacy_full_content,
        ):
            parsed['full_content'] = legacy_full_content
            parsed['title'] = existing_title
            return parsed

        if legacy_full_content:
            logger.warning(
                "拒绝会丢失旧正文的文档合并结果 source_timeline_id=%s existing_len=%s merged_len=%s",
                candidate.get('source_timeline_id'),
                len(existing_content),
                len(legacy_full_content),
            )
        return {'title': existing_title, 'no_change': True}

    @staticmethod
    def _head_tail_context(value: Any, limit: int) -> str:
        """长文本保留首尾用于模型查重；该窗口从不参与最终正文覆盖。"""
        if limit <= 0:
            return ''
        text = str(value or '').strip()
        if len(text) <= limit:
            return text
        marker = "\n\n...(中间内容仅在查重上下文中省略，存储原文保持完整)...\n\n"
        if limit <= len(marker):
            return text[:limit]
        available = max(0, limit - len(marker))
        head_chars = available * 2 // 3
        tail_chars = available - head_chars
        tail = text[-tail_chars:].lstrip() if tail_chars > 0 else ''
        return text[:head_chars].rstrip() + marker + tail

    @staticmethod
    def _content_preserves_existing(existing_content: str, merged_content: str) -> bool:
        existing = str(existing_content or '').strip()
        merged = str(merged_content or '').strip()
        return not existing or existing in merged

    @staticmethod
    def _document_identity_values(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if item is not None]
        if isinstance(value, dict):
            return [str(item) for item in value.values() if item is not None]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text[:1] in {'[', '{'}:
                try:
                    return KnowledgeExtractorV2._document_identity_values(json.loads(text))
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            return [text]
        return [str(value)]

    @staticmethod
    def _normalize_document_identity(value: Any) -> str:
        text = str(value or '').strip()
        text = re.sub(r"^[\s'\"“”‘’《》【】\[\]（）()]+", "", text)
        text = re.sub(r"[\s'\"“”‘’《》【】\[\]（）()，。；;：:、]+$", "", text)
        text = re.sub(r"\s+", " ", text)
        if not 2 <= len(text) <= 48:
            return ""
        if text.casefold() in _DOCUMENT_IDENTITY_GENERIC_TERMS:
            return ""
        if re.fullmatch(r"[\d\s./:_-]+", text):
            return ""
        if not re.search(r"[A-Za-z\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text):
            return ""
        return text

    @staticmethod
    def _document_identity_base(value: str) -> str:
        for suffix in _DOCUMENT_IDENTITY_SUFFIXES:
            if value.endswith(suffix) and len(value) - len(suffix) >= 2:
                return value[:-len(suffix)]
        return value

    @classmethod
    def _extract_document_identities(cls, candidate: Dict[str, Any]) -> List[str]:
        """从确定性字段和命名句提取产品名、项目名及别名。"""
        source_fields = (
            "summary",
            "overview",
            "details",
            "work_item",
            "capture_ax_text",
            "capture_ocr_text",
            "capture_input_text",
            "capture_audio_text",
            "capture_webpage_title",
            "capture_win_title",
            "url_aggregated_text",
        )
        source_text = "\n".join(
            str(candidate.get(field) or '') for field in source_fields
        )
        identities: List[str] = []

        def add(value: Any) -> None:
            normalized = cls._normalize_document_identity(value)
            if not normalized:
                return
            base = cls._document_identity_base(normalized)
            for index, existing in enumerate(identities):
                existing_base = cls._document_identity_base(existing)
                if existing.casefold() == normalized.casefold():
                    return
                if existing_base.casefold() == base.casefold():
                    if len(normalized) < len(existing):
                        identities[index] = normalized
                    return
            identities.append(normalized)

        for field in _DOCUMENT_IDENTITY_LIST_FIELDS:
            for value in cls._document_identity_values(candidate.get(field)):
                add(value)

        naming_pattern = re.compile(
            r"(?:^|[^A-Za-z0-9_\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])"
            r"([A-Za-z0-9_.-]{2,40}|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,20})"
            r"(?=\s*(?:是一款|是一个|是一项|是面向|是用于))",
            re.IGNORECASE,
        )
        for match in naming_pattern.finditer(source_text):
            add(match.group(1))

        labeled_pattern = re.compile(
            r"(?:产品名(?:称)?|项目名(?:称)?|系统名(?:称)?|别名|又称|中文名|英文名)"
            r"\s*(?:是|为|[:：])?\s*[\"'“”‘’《》]?"
            r"([A-Za-z0-9_.-]{2,40}|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,20})",
            re.IGNORECASE,
        )
        for match in labeled_pattern.finditer(source_text):
            add(match.group(1))

        for value in cls._document_identity_values(candidate.get("entities")):
            normalized = cls._normalize_document_identity(value)
            if not normalized:
                continue
            is_named_artifact = normalized.endswith(_DOCUMENT_IDENTITY_SUFFIXES)
            is_naming_subject = bool(
                re.search(
                    re.escape(normalized)
                    + r"\s*(?:是一款|是一个|是一项|是面向|是用于)",
                    source_text,
                    re.IGNORECASE,
                )
            )
            if is_named_artifact or is_naming_subject:
                add(normalized)

        return identities

    @classmethod
    def _document_identity_coverage_text(
        cls,
        existing_document: Dict[str, Any],
        merged: Dict[str, Any],
    ) -> str:
        values: List[str] = []
        for payload in (existing_document, merged):
            for field in _DOCUMENT_IDENTITY_COVERAGE_FIELDS:
                value = payload.get(field)
                if value is None:
                    continue
                if isinstance(value, (dict, list, tuple, set)):
                    values.append(json.dumps(value, ensure_ascii=False, default=str))
                else:
                    values.append(str(value))
        return "\n".join(values).casefold()

    @classmethod
    def _ensure_document_identity_coverage(
        cls,
        existing_document: Dict[str, Any],
        candidate: Dict[str, Any],
        merged: Dict[str, Any],
    ) -> Dict[str, Any]:
        """禁止合并结果以 no_change 吞掉来源中的产品名、项目名或别名。"""
        result = dict(merged or {})
        identities = cls._extract_document_identities(candidate)
        if not identities:
            return result

        coverage_text = cls._document_identity_coverage_text(existing_document, result)
        missing = []
        for identity in identities:
            variants = {identity.casefold(), cls._document_identity_base(identity).casefold()}
            if not any(value and value in coverage_text for value in variants):
                missing.append(identity)
        if not missing:
            return result

        existing_title = str(existing_document.get('title') or '')
        final_content = str(
            result.get('full_content')
            or existing_document.get('full_content')
            or ''
        ).strip()
        identity_patch = "## 产品、项目与别名\n" + "\n".join(
            f"- {identity}" for identity in missing
        )
        result['title'] = existing_title
        result['no_change'] = False
        result['insert_mode'] = 'append'
        result['full_content'] = cls._append_document_patch(
            final_content,
            identity_patch,
        )
        result.pop('content_patch', None)

        coverage_note = "确定性补充来源身份词：" + "、".join(missing)
        existing_evidence = str(result.get('evidence_summary') or '').strip()
        result['evidence_summary'] = (
            f"{existing_evidence}；{coverage_note}" if existing_evidence else coverage_note
        )
        logger.warning(
            "文档合并身份词未覆盖，拒绝 no_change 并补入正文 source_timeline_id=%s identities=%s",
            candidate.get('source_timeline_id'),
            missing,
        )
        return result

    @classmethod
    def _append_document_patch(cls, existing_content: str, content_patch: str) -> str:
        existing = str(existing_content or '').strip()
        patch = str(content_patch or '').strip()
        if not patch or patch in existing:
            return existing
        # 模型若无视“只返回增量”的要求、返回了包含完整旧正文的扩展版，也只在
        # 能逐字证明旧正文未丢失时接受，避免把整篇文档重复追加一遍。
        if cls._content_preserves_existing(existing, patch):
            return patch
        if not existing:
            return patch
        return f"{existing}\n\n{patch}"

    def extract_bake_bundle(
        self,
        candidate: Dict[str, Any],
        preempt_check: Optional[Callable[[], bool]] = None,
        retry_attempt: int = 0,
        retry_error_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """用一次 LLM 调用同时提炼 knowledge/document/SOP。"""
        bundle_started_at = time.time()
        source_timeline_id = candidate.get('source_timeline_id')
        logger.info("bake bundle start source_timeline_id=%s", source_timeline_id)

        # 检查抢占信号
        if preempt_check and preempt_check():
            logger.info("bake bundle 收到抢占信号，中断提炼 source_timeline_id=%s", source_timeline_id)
            from inference_queue import QueueEvictedError
            raise QueueEvictedError("后台推理已让出在线咨询或创作任务")

        retry_attempt = max(0, int(retry_attempt or 0))
        compact_retry = retry_attempt > 0
        retry_error_code = str(retry_error_code or '').strip().upper()
        timeout_retry = compact_retry and retry_error_code in {
            'INFERENCE_TIMEOUT',
            'GATEWAY_TIMEOUT',
        }
        input_token_budget = (
            BAKE_TIMEOUT_RETRY_INPUT_TOKEN_BUDGET
            if timeout_retry
            else BAKE_RETRY_INPUT_TOKEN_BUDGET if compact_retry else BAKE_INPUT_TOKEN_BUDGET
        )
        prepared_candidate = self._prepare_bake_bundle_candidate(
            candidate,
            input_token_budget,
        )
        candidate_text = self._build_bake_candidate_text(prepared_candidate)
        user_prompt = f"候选输入如下:\n\n{candidate_text}"
        caller_id = f"bundle:{source_timeline_id}"
        try:
            parsed, meta = self._call_bake_llm(
                caller_id,
                BAKE_COMPACT_BUNDLE_PROMPT if compact_retry else BAKE_BUNDLE_PROMPT,
                user_prompt,
                response_schema=(
                    BAKE_TIMEOUT_BUNDLE_RESPONSE_SCHEMA
                    if timeout_retry
                    else BAKE_COMPACT_BUNDLE_RESPONSE_SCHEMA
                    if compact_retry
                    else BAKE_BUNDLE_RESPONSE_SCHEMA
                ),
                num_predict=(
                    BAKE_TIMEOUT_RETRY_NUM_PREDICT
                    if timeout_retry
                    else BAKE_RETRY_NUM_PREDICT if compact_retry else BAKE_NUM_PREDICT
                ),
                repeat_penalty=(
                    BAKE_TIMEOUT_RETRY_REPEAT_PENALTY
                    if timeout_retry
                    else BAKE_RETRY_REPEAT_PENALTY if compact_retry else 1.0
                ),
            )
        except Exception as exc:
            elapsed_ms = int((time.time() - bundle_started_at) * 1000)
            logger.error(
                "bake bundle 提炼失败 caller=%s elapsed_ms=%s error=%s",
                caller_id,
                elapsed_ms,
                exc,
            )
            raise

        bundle_fragment_recovered = False
        if not isinstance(parsed, dict):
            parse_failure_reason = 'empty_content' if meta.get('empty_content') else (
                'truncated_json' if meta.get('done_reason') == 'length' else 'invalid_json'
            )
            recovered = _recover_bake_bundle_artifacts(meta.get('raw_content') or '')
            if recovered:
                parsed = recovered
                bundle_fragment_recovered = True
                meta = {**meta, 'parse_failure_reason': parse_failure_reason}
                logger.warning(
                    "bake bundle recovered independent artifacts caller=%s keys=%s reason=%s",
                    caller_id,
                    sorted(recovered),
                    parse_failure_reason,
                )
            else:
                error_type = (
                    BakeOutputTruncatedError
                    if parse_failure_reason == 'truncated_json'
                    else BakeOutputError
                )
                raise error_type(f"bake bundle output invalid: {parse_failure_reason}")

        results: Dict[str, Dict[str, Any]] = {}
        result_meta: Dict[str, Dict[str, Any]] = {}
        classification = parsed.get('classification')
        primary_type = (
            str(classification.get('primary_type') or '').strip()
            if isinstance(classification, dict)
            else ''
        )
        for artifact_type in ('sop', 'knowledge', 'design'):
            artifact, artifact_meta = self._normalize_bake_artifact_result(
                candidate,
                artifact_type,
                parsed.get(artifact_type),
                meta,
                caller_id=caller_id,
            )
            results[artifact_type] = artifact
            result_meta[artifact_type] = artifact_meta

        degraded = any(
            bool(item.get('degraded')) for item in result_meta.values()
        ) or bundle_fragment_recovered
        artifact_shapes = {
            artifact_type: item.get('artifact_shape') or 'object'
            for artifact_type, item in result_meta.items()
        }
        compatibility_recovered = {
            artifact_type: bool(item.get('compatibility_recovered'))
            for artifact_type, item in result_meta.items()
        }
        total_elapsed_ms = int((time.time() - bundle_started_at) * 1000)
        per_stage_ms = {'bundle': int(meta.get('elapsed_ms') or total_elapsed_ms)}
        logger.info(
            "bake bundle done source_timeline_id=%s total_elapsed_ms=%s stage_elapsed_ms=%s degraded=%s artifact_shapes=%s compatibility_recovered=%s accepted={knowledge:%s,design:%s,sop:%s}",
            source_timeline_id,
            total_elapsed_ms,
            per_stage_ms,
            degraded,
            artifact_shapes,
            compatibility_recovered,
            results['knowledge'].get('accepted'),
            results['design'].get('accepted'),
            results['sop'].get('accepted'),
        )

        return {
            'knowledge': results['knowledge'],
            'design': results['design'],
            'sop': results['sop'],
            'primary_type': primary_type or None,
            'classification_reason': (
                classification.get('reason')
                if isinstance(classification, dict)
                else None
            ),
            'usage': meta.get('usage'),
            'model': meta.get('model') or self.model,
            'degraded': degraded,
            'artifact_shapes': artifact_shapes,
            'compatibility_recovered': compatibility_recovered,
            'bundle_fragment_recovered': bundle_fragment_recovered,
            'stage_elapsed_ms': per_stage_ms,
            'total_elapsed_ms': total_elapsed_ms,
        }

    def extract_sync(
        self,
        capture_data: Dict[str, Any],
        db_conn=None
    ) -> Optional[Dict[str, Any]]:
        """
        同步版本的提炼方法

        Args:
            capture_data: 采集数据
            db_conn: 数据库连接（用于去重）

        Returns:
            提炼后的知识，如果无价值或重复则返回 None
        """
        try:
            # 1. 构建 prompt
            prompt = self._build_prompt(capture_data)

            # 2. 调用本地 LLM（带埋点）
            logger.info(f"开始提炼采集记录 {capture_data.get('id')}")
            # RAG 优先:若 RAG 查询正在占用 Ollama，跳过本轮提炼
            if _rag_is_active():
                logger.info("RAG 查询正在进行，本轮提炼跳过")
                return None
            from monitor.llm_tracker import LLMCallTracker, estimate_tokens
            with LLMCallTracker(
                caller="knowledge",
                model_name=self.model,
                caller_id=str(capture_data.get('id')),
            ) as tracker:
                response = self._ollama_chat(
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT + DATA_FACT_PROMPT + DATA_PAGE_PROMPT},
                        {"role": "user", "content": prompt + "\n\n" + JSON_OUTPUT_RULES}
                    ],
                    format="json",
                    options={"temperature": 0.3, "num_predict": BAKE_NUM_PREDICT},
                )
                content = _extract_ollama_response_text(response)
                tracker.set_response(response)
                if tracker._prompt_tokens == 0:
                    tracker.set_tokens(
                        prompt=estimate_tokens(SYSTEM_PROMPT + DATA_FACT_PROMPT + DATA_PAGE_PROMPT + prompt),
                        completion=estimate_tokens(content),
                    )

            # 3. 解析结果
            result = _extract_json_object(content)
            if result is None:
                raise json.JSONDecodeError("No valid JSON object found", content, 0)

            # 4. 跳过无价值内容
            overview = _normalize_inline_text(result.get('overview', ''))
            if overview == 'SKIP' or not overview:
                logger.info(f"采集记录 {capture_data.get('id')} 无价值，跳过")
                return discarded_knowledge('no_value')

            source_text = _sanitize_capture_text(
                capture_data.get('ax_text')
                or capture_data.get('ocr_text')
                or capture_data.get('input_text')
                or capture_data.get('audio_text')
                or ''
            )
            quality_reason = _overview_quality_reason(overview, source_text)
            if quality_reason:
                logger.info("采集记录 %s 提炼质量不足，跳过: %s", capture_data.get('id'), quality_reason)
                return discarded_knowledge('quality')

            details = result.get('details', '')
            data_facts, rejected_data_fact_count = _validated_data_facts(
                result.get('data_facts'),
                source_text,
            )
            if not data_facts:
                recovered_facts, recovered_rejected = self._recover_missing_data_facts(
                    source_text,
                    result,
                    f"capture:{capture_data.get('id')}",
                )
                data_facts = recovered_facts
                rejected_data_fact_count += recovered_rejected
            page_url = _normalize_page_url(capture_data.get('url'))
            allowed_urls = {page_url} if page_url else set()
            data_pages = _validated_data_pages(result.get('data_pages'), allowed_urls)
            summary = _overview_to_summary(overview)
            knowledge = {
                'capture_id': capture_data['id'],
                'summary': summary,
                'overview': overview,
                'details': details,
                'entities': json.dumps(result.get('entities', []), ensure_ascii=False),
                'category': result.get('category', '其他'),
                'importance': result.get('importance', 3),
                'occurrence_count': 1,
                'observed_at': capture_data.get('ts'),
                'event_time_start': result.get('event_time_start'),
                'event_time_end': result.get('event_time_end'),
                'history_view': bool(result.get('history_view', False)),
                'content_origin': result.get('content_origin'),
                'activity_type': result.get('activity_type'),
                'is_self_generated': False,
                'evidence_strength': result.get('evidence_strength'),
                'work_item': result.get('work_item'),
                'work_status': result.get('work_status'),
                'work_progress': result.get('work_progress'),
                'data_fact_contract': DATA_FACT_CONTRACT_VERSION,
                'data_facts': data_facts,
                'data_fact_rejected_count': rejected_data_fact_count,
                'data_page_contract': DATA_PAGE_CONTRACT_VERSION,
                'data_pages': data_pages,
            }

            # 5. 去重检查和知识合并
            if db_conn:
                entities = result.get('entities') or []
                similar_id = self._find_similar_knowledge(
                    overview,
                    db_conn,
                    entities=entities,
                    start_time=capture_data.get('ts'),
                    end_time=capture_data.get('ts'),
                )
                if similar_id:
                    # 合并知识:更新明细内容，追加新的细节
                    cursor = db_conn.execute(
                        "SELECT details FROM timelines WHERE id = ?",
                        (similar_id,)
                    )
                    existing_details = cursor.fetchone()[0] or ""

                    # 合并明细:保留原有内容，追加新内容
                    merged_details = existing_details
                    if details and details not in existing_details:
                        merged_details += f"\n\n--- 补充 ({datetime.now().strftime('%Y-%m-%d %H:%M')}) ---\n{details}"

                    # 更新现有记录
                    db_conn.execute(
                        """UPDATE timelines
                           SET occurrence_count = occurrence_count + 1,
                               details = ?,
                               work_item = COALESCE(NULLIF(?, ''), work_item),
                               work_status = COALESCE(NULLIF(?, ''), work_status),
                               work_progress = COALESCE(NULLIF(?, ''), work_progress),
                               updated_at = CURRENT_TIMESTAMP
                           WHERE id = ?""",
                        (
                            merged_details,
                            knowledge.get('work_item'),
                            knowledge.get('work_status'),
                            knowledge.get('work_progress'),
                            similar_id,
                        )
                    )
                    db_conn.commit()
                    logger.info(f"知识已合并到现有条目 (ID={similar_id})")
                    knowledge['_merged_timeline_id'] = similar_id
                    return knowledge

            # 6. 返回结构化知识
            logger.info(f"成功提炼采集记录 {capture_data.get('id')}: {overview[:50]}...")
            return knowledge

        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}, 响应内容: {content[:500]}")
            return None
        except Exception as e:
            logger.error(f"时间线提炼失败: {e}")
            return None

    async def extract(
        self,
        capture_data: Dict[str, Any],
        db_conn=None
    ) -> Optional[Dict[str, Any]]:
        """异步版本（调用同步方法）"""
        return self.extract_sync(capture_data, db_conn)

    def _build_merged_blocks(self, captures: List[Dict[str, Any]]) -> str:
        """按密度感知配额构建合并提炼文本。

        步骤：
        1. 每块按行/句段密度分配配额：密集正文块 3000 字，其余 800 字；
           正文完全相同的重复 capture（如连续同屏采集）只保留一份；
        2. 拼接后超过总长限制时，先剔除各块中明确 UI 噪声行与连续短字孤立行；
        3. 仍超限时按密度加权等比缩减各块（密集正文保留更多，保底 250 字），
           截断时保留数值指标密集段；
        4. 最终兜底才从尾部硬切（旧行为）。
        判定全部为确定性字符串统计，不增加 LLM 调用。
        """
        from knowledge.fragment_grouper import text_density_score, DENSE_TEXT_THRESHOLD

        entries: List[Dict[str, Any]] = []
        seen_bodies = set()
        for c in captures:
            text = (
                c.get('ax_text')
                or c.get('ocr_text')
                or c.get('input_text')
                or c.get('audio_text')
                or ''
            )
            sanitized_text = _sanitize_capture_text(text)
            if not sanitized_text.strip():
                continue
            # 正文完全相同的重复采集（连续同屏）去重，只保留首次出现
            if sanitized_text in seen_bodies:
                continue
            seen_bodies.add(sanitized_text)
            ts_str = datetime.fromtimestamp(c['ts'] / 1000).strftime('%H:%M:%S')
            app = c.get('app_name', '')
            title = c.get('window_title', '')
            # 块头部注入页面 URL / 网页标题，供 data_pages 分类逐字引用
            page_url = _normalize_page_url(c.get('url'))
            page_title = str(c.get('webpage_title') or '').strip()
            page_meta = []
            if page_title:
                page_meta.append(f"页面标题: {page_title}")
            if page_url:
                page_meta.append(f"页面URL: {page_url}")
            header = f"[采集ID:{c['id']} 时间:{ts_str}] {app} - {title}"
            if page_meta:
                header += "（" + "，".join(page_meta) + "）"
            density = text_density_score(sanitized_text)
            quota = (
                MERGE_BLOCK_QUOTA_DENSE
                if density >= DENSE_TEXT_THRESHOLD
                else MERGE_BLOCK_QUOTA_DEFAULT
            )
            entries.append({
                'header': header,
                'body': _truncate_preserving_metrics(sanitized_text, quota),
                'density': density,
            })

        if not entries:
            return ''

        sep_len = len(MERGE_BLOCK_SEPARATOR)

        def joined_len() -> int:
            return sum(len(e['header']) + 1 + len(e['body']) for e in entries) + sep_len * (len(entries) - 1)

        # 阶段2：总长超限 → 优先剔除明确 UI 噪声行与连续短字孤立行
        if joined_len() > MERGE_TOTAL_MAX_CHARS:
            for e in entries:
                e['body'] = _strip_pressure_noise_lines(e['body'])

        # 阶段3：仍超限 → 按密度加权等比缩减（密集正文保留更多，保底 250 字），
        # 避免低密度块压到 250 后仍超限、密集块被尾部硬切整块丢失。
        overhead = sum(len(e['header']) + 1 for e in entries) + sep_len * (len(entries) - 1)
        body_budget = MERGE_TOTAL_MAX_CHARS - overhead
        total_body_len = sum(len(e['body']) for e in entries)
        if body_budget > 0 and total_body_len > body_budget:
            weights = [max(e['density'], 0.05) for e in entries]
            weight_sum = sum(weights) or 1.0
            caps: List[int] = []
            for e, w in zip(entries, weights):
                share = int(body_budget * w / weight_sum)
                caps.append(max(min(len(e['body']), share), MERGE_COMPRESSED_QUOTA_LOW))
            for e, cap in zip(entries, caps):
                if len(e['body']) > cap:
                    e['body'] = _truncate_preserving_metrics(e['body'], cap)

        blocks = [f"{e['header']}\n{e['body']}" for e in entries if e['body'].strip()]
        if not blocks:
            return ''
        merged_text = MERGE_BLOCK_SEPARATOR.join(blocks)
        # 阶段4：兜底尾部硬切（正常情况下不会走到这里）
        if len(merged_text) > MERGE_TOTAL_MAX_CHARS:
            merged_text = merged_text[:MERGE_TOTAL_MAX_CHARS] + "\n...(已截断)"
        return merged_text

    def extract_merged(
        self,
        captures: List[Dict[str, Any]],
        preempt_check=None,
    ) -> Optional[Dict[str, Any]]:
        """
        将多条 captures 合并提炼为一个工作片段知识条目。

        Args:
            captures: 按时间升序排列的 capture 列表
            preempt_check: 抢占检查函数，返回 True 表示需要中断

        Returns:
            提炼后的知识条目，包含 capture_ids/start_time/end_time/duration_minutes
        """
        if not captures:
            return None

        # 检查抢占信号
        if preempt_check and preempt_check():
            logger.info("extract_merged 收到抢占信号，中断提炼")
            return None

        # 单条直接走原有逻辑
        if len(captures) == 1:
            result = self.extract_sync(captures[0])
            if result and result.get(_DISCARDED_KEY):
                # 确定性丢弃（无价值/质量不足）：透传标记，由调用方消费掉 capture
                return result
            if result:
                result['capture_ids'] = json.dumps([captures[0]['id']])
                result['start_time'] = captures[0]['ts']
                result['end_time'] = captures[0]['ts']
                result['duration_minutes'] = 0
                result['frag_app_name'] = captures[0].get('app_name')
                result['frag_win_title'] = captures[0].get('window_title')
                # 为单个 capture 也生成 key_timestamps
                result['key_timestamps'] = json.dumps([{
                    'capture_ids': [captures[0]['id']],
                    'start_ts': captures[0]['ts'],
                    'end_ts': captures[0]['ts'],
                    'summary': result.get('summary', '')
                }])
            return result

        try:
            logger.info("extract_merged 启动: captures=%s", len(captures))
            # 0. 先做语义分段（分段本来就要逐段调 AI 提炼），并确定性过滤
            # 低价值分段：被丢弃的 capture 不会混进合并提炼文本，也不会写入
            # 时间线 capture_ids（timeline 2713 类污染的另一条路径），
            # 且不产生额外 LLM 开销。
            segments, discarded_capture_ids, segment_data_pages = self._generate_segments(captures)
            if discarded_capture_ids:
                discarded_set = set(discarded_capture_ids)
                kept_captures = [c for c in captures if c['id'] not in discarded_set]
                logger.info(
                    "合并提炼丢弃过滤: 剔除 %d 条低价值 captures ids=%s，保留 %d 条",
                    len(discarded_capture_ids),
                    discarded_capture_ids,
                    len(kept_captures),
                )
                captures = kept_captures
                if not captures:
                    return discarded_knowledge('no_value')

            # 1. 构建合并 prompt：按密度感知配额拼接所有 capture 的文本
            merged_text = self._build_merged_blocks(captures)
            if not merged_text:
                return None

            all_capture_ids = [int(c['id']) for c in captures]
            user_prompt = (
                "以下是待校验并提炼的连续采集记录。"
                f"采集ID全集:{json.dumps(all_capture_ids, ensure_ascii=False)}。"
                "先判断它们是否确属同一个工作任务；若不属于，按任务返回完整分组，不要强行归纳。"
                "输出必须是对工作内容的归纳，不允许照抄 UI 菜单词、窗口壳层词或原始 OCR 长串。\n\n"
                f"{merged_text}"
            )

            # 2. 调用 LLM（带埋点）
            logger.info(f"合并提炼 {len(captures)} 条 captures")
            # RAG 优先:若 RAG 查询正在占用 Ollama，跳过本轮提炼
            if _rag_is_active():
                logger.info("RAG 查询正在进行，本轮合并提炼跳过")
                return None
            # 检查抢占信号
            if preempt_check and preempt_check():
                logger.info("extract_merged 在 LLM 调用前收到抢占信号")
                return None
            from monitor.llm_tracker import LLMCallTracker, estimate_tokens
            capture_ids_str = ",".join(str(c['id']) for c in captures[:5])
            with LLMCallTracker(
                caller="knowledge",
                model_name=self.model,
                caller_id=f"merge:{capture_ids_str}",
            ) as tracker:
                _sys_prompt = self._build_merge_system_prompt()
                # 强化 JSON 输出约束:在 user prompt 中再次强调，并列出完整字段清单，
                # 避免小模型在长 system prompt 下遗漏末尾的 data_facts/data_pages 契约字段
                enhanced_user_prompt = (
                    f"{user_prompt}\n\n**重要**:你必须且只能输出一个有效的 JSON 对象，"
                    "不要输出任何其他内容、解释或 markdown 代码块。"
                    "JSON 顶层必须包含全部字段:is_coherent, coherence_reason, capture_groups, "
                    "work_item, work_status, work_progress, "
                    "overview, details, entities, category, importance, history_view, "
                    "content_origin, activity_type, event_time_start, event_time_end, "
                    "evidence_strength, data_facts, data_pages。"
                    "data_facts 与 data_pages 没有内容时必须输出空数组 []，不得省略字段。"
                    + JSON_OUTPUT_RULES
                )
                response = self._ollama_chat(
                    messages=[
                        {"role": "system", "content": _sys_prompt},
                        {"role": "user", "content": enhanced_user_prompt},
                    ],
                    format="json",
                    options={
                        "temperature": 0.3,
                        "num_ctx": BAKE_CONTEXT_WINDOW_TOKENS,
                        "num_predict": BAKE_NUM_PREDICT,
                    },
                )
                content = _extract_ollama_response_text(response)
                if response.get("done_reason") == "length":
                    logger.warning(
                        "合并提炼输出被 num_predict 截断，JSON 可能不完整"
                    )
                tracker.set_response(response)
                if tracker._prompt_tokens == 0:
                    tracker.set_tokens(
                        prompt=estimate_tokens(_sys_prompt + enhanced_user_prompt),
                        completion=estimate_tokens(content),
                    )

            # 3. 解析结果
            result = _extract_json_object(content)
            if result is None:
                logger.error(
                    "合并提炼 JSON 解析失败: No valid JSON object found: line 1 column 1 (char 0), "
                    "响应内容: %s",
                    content[:2000] if content else "(empty)"
                )
                return None

            # 3.1 强制契约字段缺失时的紧凑补发：小模型在长 system prompt 下可能
            # 省略末尾的任务一致性/data_facts/data_pages；同链路补一次，只回填缺失的契约
            # 字段，不覆盖已提炼的主结果。
            missing_contract_fields = [
                field
                for field in (
                    "is_coherent",
                    "coherence_reason",
                    "capture_groups",
                    "data_facts",
                    "data_pages",
                )
                if field not in result
            ]
            if missing_contract_fields:
                logger.info(
                    "合并提炼输出缺少数据契约字段 %s，同链路补发一次",
                    missing_contract_fields,
                )
                retry_user_prompt = (
                    f"{user_prompt}\n\n**重要**:你必须且只能输出一个有效的 JSON 对象。"
                    f"上一次输出缺少字段:{'、'.join(missing_contract_fields)}。"
                    "本次 JSON 顶层必须包含全部字段:is_coherent, coherence_reason, "
                    "capture_groups, work_item, work_status, work_progress, "
                    "overview, details, entities, category, importance, history_view, "
                    "content_origin, activity_type, event_time_start, event_time_end, "
                    "evidence_strength, data_facts, data_pages。"
                    "overview/details 保持简短；data_facts 与 data_pages 没有内容时输出空数组 []。"
                    + JSON_OUTPUT_RULES
                )
                try:
                    retry_response = self._ollama_chat(
                        messages=[
                            {"role": "system", "content": _sys_prompt},
                            {"role": "user", "content": retry_user_prompt},
                        ],
                        format="json",
                        options={
                            "temperature": 0.3,
                            "num_ctx": BAKE_CONTEXT_WINDOW_TOKENS,
                            "num_predict": BAKE_RETRY_NUM_PREDICT,
                        },
                    )
                    retry_result = _extract_json_object(
                        _extract_ollama_response_text(retry_response)
                    )
                    if retry_result:
                        for field in missing_contract_fields:
                            if field in retry_result:
                                result[field] = retry_result[field]
                except Exception as retry_exc:
                    logger.warning("合并提炼数据契约补发失败: %s", retry_exc)

            # 3.2 落库前任务一致性门禁：缺少判定、分组遗漏/重复/编造都
            # fail-closed，保留 captures 等下一轮重试，绝不降级成混合时间线。
            is_coherent = result.get('is_coherent')
            if isinstance(is_coherent, str):
                normalized_coherence = is_coherent.strip().casefold()
                if normalized_coherence in {'true', '1', 'yes', '是', '一致'}:
                    is_coherent = True
                elif normalized_coherence in {'false', '0', 'no', '否', '不一致'}:
                    is_coherent = False

            if not isinstance(is_coherent, bool):
                logger.warning(
                    "合并提炼缺少有效 is_coherent，拒绝落库: ids=%s value=%r",
                    all_capture_ids,
                    result.get('is_coherent'),
                )
                return None

            if is_coherent:
                capture_groups = _validated_coherent_capture_group(
                    result.get('capture_groups'),
                    all_capture_ids,
                )
            else:
                capture_groups = _validated_capture_groups(
                    result.get('capture_groups'),
                    all_capture_ids,
                    minimum_groups=2,
                )
            if not capture_groups or (is_coherent and len(capture_groups) != 1):
                logger.warning(
                    "合并提炼 capture_groups 不是有效完整分区，拒绝落库: coherent=%s ids=%s groups=%s",
                    is_coherent,
                    all_capture_ids,
                    result.get('capture_groups'),
                )
                return None

            if not is_coherent:
                logger.info(
                    "合并提炼一致性门禁触发: %d captures → %d 个任务组 groups=%s",
                    len(all_capture_ids), len(capture_groups), capture_groups,
                )
                return {
                    '_split_required': True,
                    'capture_groups': capture_groups,
                    'split_reason': str(result.get('coherence_reason') or '').strip(),
                }

            overview = _normalize_inline_text(result.get('overview', ''))
            if not overview or overview == 'SKIP':
                logger.warning("合并提炼未返回有效 overview，跳过本片段（不兜底）: result=%s", result)
                return discarded_knowledge('no_value')

            quality_reason = _overview_quality_reason(overview, merged_text)
            if quality_reason:
                logger.warning("合并提炼 overview 质量不足，跳过本片段（不兜底）: reason=%s overview=%s", quality_reason, overview)
                return discarded_knowledge('quality')

            # 4. 计算片段元数据
            start_time = captures[0]['ts']
            end_time = captures[-1]['ts']
            duration_minutes = int((end_time - start_time) / 60000)

            # 主要应用:出现次数最多的 app_name
            from collections import Counter
            app_counter = Counter(
                c.get('app_name') for c in captures if c.get('app_name')
            )
            frag_app_name = app_counter.most_common(1)[0][0] if app_counter else None

            # 主要窗口:最后一条的 win_title（最能代表当前状态）
            frag_win_title = next(
                (c.get('window_title') for c in reversed(captures) if c.get('window_title')),
                None
            )

            summary = _overview_to_summary(overview)
            data_facts, rejected_data_fact_count = _validated_data_facts(
                result.get('data_facts'),
                merged_text,
            )
            if not data_facts:
                recovered_facts, recovered_rejected = self._recover_missing_data_facts(
                    merged_text,
                    result,
                    f"merge:{','.join(str(capture_id) for capture_id in all_capture_ids[:5])}",
                )
                data_facts = recovered_facts
                rejected_data_fact_count += recovered_rejected
            allowed_urls = {
                _normalize_page_url(c.get('url'))
                for c in captures
                if _normalize_page_url(c.get('url'))
            }
            data_pages = _validated_data_pages(result.get('data_pages'), allowed_urls)
            if not data_pages and segment_data_pages:
                # 主调用遗漏时复用分段提炼已产出的分类结果（不新增推理），
                # 仍按本组 capture URL 白名单再校验一次。
                data_pages = _validated_data_pages(segment_data_pages, allowed_urls)
                if data_pages:
                    logger.info(
                        "合并提炼 data_pages 由分段提炼结果兜底: %s",
                        [p.get('url') for p in data_pages],
                    )
            # 语义分段已在步骤 0 生成（并过滤掉确定性丢弃的分段）

            knowledge = {
                'capture_ids': json.dumps([c['id'] for c in captures]),
                'summary': summary,
                'overview': overview,
                'details': result.get('details', ''),
                'entities': json.dumps(result.get('entities', []), ensure_ascii=False),
                'category': result.get('category', '其他'),
                'importance': result.get('importance', 3),
                'occurrence_count': 1,
                'start_time': start_time,
                'end_time': end_time,
                'duration_minutes': duration_minutes,
                'key_timestamps': json.dumps(segments),
                'frag_app_name': frag_app_name,
                'frag_win_title': frag_win_title,
                'observed_at': end_time,
                'event_time_start': result.get('event_time_start'),
                'event_time_end': result.get('event_time_end'),
                'history_view': bool(result.get('history_view', False)),
                'content_origin': result.get('content_origin'),
                'activity_type': result.get('activity_type'),
                'is_self_generated': False,
                'evidence_strength': result.get('evidence_strength'),
                'work_item': result.get('work_item'),
                'work_status': result.get('work_status'),
                'work_progress': result.get('work_progress'),
                'data_fact_contract': DATA_FACT_CONTRACT_VERSION,
                'data_facts': data_facts,
                'data_fact_rejected_count': rejected_data_fact_count,
                'data_page_contract': DATA_PAGE_CONTRACT_VERSION,
                'data_pages': data_pages,
            }

            if discarded_capture_ids:
                knowledge['_discarded_capture_ids'] = discarded_capture_ids

            logger.info(
                f"合并提炼完成: {len(captures)} captures → 1 knowledge, "
                f"时长={duration_minutes}分钟, overview={overview[:50]}..."
            )
            return knowledge

        except json.JSONDecodeError as e:
            logger.error(f"合并提炼 JSON 解析失败: {e}, 响应内容: {content[:1000]}")
            return None
        except Exception as e:
            logger.error(f"合并提炼失败: {e}")
            return None

    def _generate_segments(
        self, captures: List[Dict[str, Any]]
    ) -> tuple:
        """生成语义分段，使用AI提炼每个分段的总结。

        Returns:
            (segments, discarded_capture_ids, segment_data_pages)：确定性丢弃
            （无价值/质量不足）的分段不进 segments，其 capture ids 单独返回，
            由调用方消费；segment_data_pages 为各分段提炼已校验的数据页面，
            供主调用产出缺失时兜底（不新增推理）。
        """
        try:
            segments_map = {}
            discarded_capture_ids: List[int] = []
            segment_data_pages: List[Dict[str, Any]] = []
            for cap in captures:
                key = f"{cap.get('app_name')}|{cap.get('window_title', '')}"
                if key not in segments_map:
                    segments_map[key] = {
                        'capture_ids': [],
                        'start_ts': cap['ts'],
                        'end_ts': cap['ts'],
                        'app_name': cap.get('app_name', ''),
                        'window_title': cap.get('window_title', ''),
                        'texts': []
                    }
                seg = segments_map[key]
                seg['capture_ids'].append(cap['id'])
                seg['end_ts'] = cap['ts']
                text = (cap.get('ocr_text') or cap.get('ax_text') or '').strip()
                if text:
                    seg['texts'].append(text)

            logger.info(f"生成语义分段: {len(captures)} captures → {len(segments_map)} segments")
            segments = []
            for idx, seg in enumerate(segments_map.values()):
                merged_text = '\n\n'.join(seg['texts'])
                if merged_text:
                    # 分段内首个带 URL 的采集，透传给单条提炼以支持 data_pages 分类
                    seg_url = ''
                    seg_webpage_title = ''
                    for cap in captures:
                        if cap.get('id') in seg['capture_ids'] and _normalize_page_url(cap.get('url')):
                            seg_url = cap.get('url') or ''
                            seg_webpage_title = str(cap.get('webpage_title') or '')
                            break
                    segment_capture = {
                        'id': seg['capture_ids'][0],
                        'app_name': seg['app_name'],
                        'window_title': seg['window_title'],
                        'timestamp': datetime.fromtimestamp(seg['end_ts'] / 1000).isoformat(),
                        'ocr_text': merged_text[:2000],  # 限制长度避免过长
                        'url': seg_url,
                        'webpage_title': seg_webpage_title,
                    }
                    logger.info(f"分段 {idx+1}/{len(segments_map)}: 调用 AI 提炼 ({len(seg['capture_ids'])} captures)")
                    extracted = self.extract_sync(segment_capture)
                    if extracted and extracted.get(_DISCARDED_KEY):
                        # 确定性丢弃：该分段不计入时间线成员，透传给调用方消费
                        logger.info(
                            "分段 %d 确定性丢弃 (reason=%s): captures=%s",
                            idx + 1,
                            extracted.get('discard_reason', 'unknown'),
                            seg['capture_ids'],
                        )
                        discarded_capture_ids.extend(seg['capture_ids'])
                        continue
                    summary = extracted.get('summary', '') if extracted else ''
                    for page in (extracted.get('data_pages') or [] if extracted else []):
                        if isinstance(page, dict):
                            segment_data_pages.append(page)
                    logger.info(f"分段 {idx+1} AI 总结: {summary[:80]}...")
                else:
                    summary = ''

                if not summary:
                    summary = f"{seg['app_name']}活动"

                segments.append({
                    'capture_ids': seg['capture_ids'],
                    'start_ts': seg['start_ts'],
                    'end_ts': seg['end_ts'],
                    'summary': summary
                })
            logger.info(f"语义分段生成完成: {len(segments)} segments")
            return segments, discarded_capture_ids, segment_data_pages
        except Exception as e:
            logger.error(f"生成语义分段失败: {e}", exc_info=True)
            return [], [], []
