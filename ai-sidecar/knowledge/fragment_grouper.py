"""
工作片段分组器

将连续的 captures 按语义相似度分组为工作片段。
核心原则：不依赖应用/窗口切换判断任务边界，而是用内容语义判断。
"""

from __future__ import annotations

import re
import logging
import numpy as np
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 文档类 URL 识别：用于"一份文档独占一个 timeline"的边界判断。
_DOC_URL_MARKERS = (
    '/docs/', 'docs.google', '/document/', 'yuque.com',
    'feishu.cn/docx', 'feishu.cn/wiki', 'notion.so', 'confluence',
    '/wiki/', 'shimo.im', '/d/home/', '/s/home/', '/k/home/',
)

_DOC_TITLE_MARKERS = (
    '云文档', '在线文档', 'google docs', '腾讯文档', '飞书文档',
    '语雀', 'notion', 'confluence', '石墨文档', 'docs - google chrome',
)


def _is_document_url(url: Optional[str]) -> bool:
    """URL 是否指向一份文档。"""
    if not url:
        return False
    u = url.strip().lower()
    if not u:
        return False
    return any(marker in u for marker in _DOC_URL_MARKERS)


def _document_identity(url: Optional[str]) -> Optional[str]:
    """从文档 URL 提取稳定的文档标识，用于区分"是否同一份文档"。

    常见文档地址形如 https://docs.example.com/d/home/<document-key>，路径中的键唯一标识一份文档。
    取 path 最后一个非空段作为标识；无法解析时回退到去掉 query/fragment 的完整 path。
    """
    if not _is_document_url(url):
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    path = (parsed.path or '').rstrip('/')
    if not path:
        return (parsed.netloc or url).lower()
    last_segment = path.rsplit('/', 1)[-1]
    # docId 通常是较长的标识串；过短的段（如 home/d）回退到完整 path 以免误并不同文档。
    if len(last_segment) >= 6:
        return f"{parsed.netloc.lower()}::{last_segment}"
    return f"{parsed.netloc.lower()}::{path.lower()}"


def _looks_like_document_capture(capture: dict) -> bool:
    """capture 是否呈现出文档特征；URL 为空时仅用于阻止未经证明的合并。"""
    if _document_identity(capture.get('url')):
        return True
    title = (
        capture.get('webpage_title')
        or capture.get('window_title')
        or capture.get('win_title')
        or ''
    )
    lowered = str(title).strip().lower()
    return bool(lowered) and any(marker in lowered for marker in _DOC_TITLE_MARKERS)


def _content_document_identity(capture: dict) -> Optional[str]:
    """无 URL 内容文档的身份标识（去 URL 化的文档通道）。

    文档不只存在于浏览器：本地办公应用（Word/Excel/PPT/记事本）与 IM 里的
    密集长正文同样是文档。满足以下条件时返回 "app::窗口标题" 形式的稳定身份：
    1. app 属于办公/笔记/IM 类应用；
    2. 正文为密集长文本（行/句段密度判定）；
    3. 窗口标题非空（作为文档名，如群名、文件名）。
    不满足时返回 None。本函数只做身份判定，不改变分组边界。
    """
    app_raw = str(capture.get('app_name') or '')
    app = app_raw.lower()
    if not any(k in app for k in _CONTENT_DOC_APP_KEYWORDS):
        return None
    title = str(
        capture.get('window_title')
        or capture.get('win_title')
        or ''
    ).strip()
    if not title:
        return None
    text = str(
        capture.get('ax_text')
        or capture.get('ocr_text')
        or capture.get('input_text')
        or capture.get('audio_text')
        or ''
    )
    if not is_dense_long_text(text):
        return None
    return f"{app}::{title.lower()}"


_HISTORY_APP_KEYWORDS = (
    'wechat', 'wecom', 'feishu', 'slack', 'teams', 'discord',
    'telegram', 'imessage', 'messages', 'gemini', 'claude', 'chatgpt',
)

# 内容文档（无 URL）来源应用：本地办公套件、笔记/文本类、IM。
# 文档不应强依赖浏览器 URL：Word/Excel/PPT/记事本/IM 里的长正文同样是文档。
_CONTENT_DOC_APP_KEYWORDS = (
    'word', 'excel', 'powerpoint', 'wps', 'pages', 'numbers', 'keynote',
    'notion', 'obsidian', 'typora', 'textedit', '备忘录', 'notes',
    'wechat', '微信', 'wecom', '企业微信', 'feishu', '飞书', 'slack',
    'teams', 'dingtalk', '钉钉', 'discord', 'telegram', 'kim',
)

_HISTORY_TEXT_PATTERNS = (
    '昨天', '前天', '历史消息', '历史记录', '聊天记录', '更早', '回看', '回顾',
    '历史对话', '上一轮', '上周', '上个月', '昨天的', '前天的', 'earlier', 'history',
    'previous', 'yesterday', 'last week', 'last month',
)

# 同一工作流中常见的应用组合（来回切换不算任务切换）
RELATED_APP_GROUPS = [
    {'Code', 'Cursor', 'VSCode', 'Visual Studio Code', 'Xcode', 'Terminal', 'iTerm2', 'iTerm'},
    {'Slack', 'DingTalk', 'Feishu', 'WeCom', 'Teams', 'Discord'},
    {'Chrome', 'Safari', 'Firefox', 'Arc', 'Edge'},
    {'Word', 'Pages', 'Notion', 'Obsidian', 'Typora', 'Bear'},
    {'Excel', 'Numbers', 'Google Sheets'},
    {'Figma', 'Sketch', 'Adobe XD'},
]

# 中文停用词
STOP_WORDS = {
    '的', '了', '是', '在', '和', '有', '我', '你', '他', '她', '它',
    '们', '这', '那', '就', '都', '也', '还', '但', '而', '或', '与',
    '对', '从', '到', '以', '为', '被', '把', '让', '使', '将', '已',
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'to', 'of', 'in',
    'on', 'at', 'by', 'for', 'with', 'as', 'be', 'been', 'being',
}


# ─────────────────────────────────────────────────────────────────────────
# 文本密度判定（句段/行双口径）
#
# 采集文本的实际结构：ax_text 来自无障碍树，无换行且空格随机插入；
# ocr_text 的换行是控件宽度硬换行而非语义行。因此密度判定不能只按行，
# 必须同时提供"压缩空白后按标点切句段"的口径，两者取最大值。
# ─────────────────────────────────────────────────────────────────────────
_SENTENCE_SPLIT_RE = re.compile(r'[。；！？!?;]')
_DENSE_LONG_LINE_MIN_CHARS = 20    # 长句段/长行阈值
DENSE_TEXT_THRESHOLD = 0.30        # 长文字符占比达到该值视为密集正文
LONG_DENSE_MIN_CHARS = 400         # 密集正文的最小实质字符数


def text_density_score(text: str) -> float:
    """文本中"长文"字符占比（0-1），三种口径取最大值。

    - 行口径：适配 OCR（有换行，统计 ≥20 字行的字符占比）；
    - 句段口径：压缩空白后按 。；！？ 切段（仅在确实切出多段时生效，
      否则无标点文本会被整段误判为长文）；
    - 空白块口径：压缩后按空白切块，适配 AX（无换行无标点、
      但存在自然空格的文本）与 OCR 长行。
    """
    raw = str(text or '')
    if not raw.strip():
        return 0.0
    long_line_chars = 0
    for line in raw.split('\n'):
        stripped = line.strip()
        if len(stripped) >= _DENSE_LONG_LINE_MIN_CHARS:
            long_line_chars += len(stripped)
    compact = ' '.join(raw.split())
    seg_chars = 0
    segments = _SENTENCE_SPLIT_RE.split(compact)
    if len(segments) >= 2:
        for seg in segments:
            seg_stripped = seg.strip()
            if len(seg_stripped) >= _DENSE_LONG_LINE_MIN_CHARS:
                seg_chars += len(seg_stripped)
    chunk_chars = 0
    for chunk in compact.split(' '):
        if len(chunk) >= _DENSE_LONG_LINE_MIN_CHARS:
            chunk_chars += len(chunk)
    total = max(1, len(compact))
    return float(max(long_line_chars, seg_chars, chunk_chars)) / float(total)


def is_dense_long_text(text: str) -> bool:
    """是否为"密集长正文"：实质字符数达标且长文占比达标。

    判定不确定（处于阈值边缘）时偏向 False，调用方应配兜底逻辑。
    """
    compact_len = len(' '.join(str(text or '').split()))
    if compact_len < LONG_DENSE_MIN_CHARS:
        return False
    return text_density_score(text) >= DENSE_TEXT_THRESHOLD


class FragmentGrouper:
    """
    将连续的 captures 分组为工作片段。

    分组策略（优先级从高到低）：
    1. 时间间隔 > HARD_SPLIT_MINUTES → 强制切断
    2. 语义相似度 >= SAME_TASK_THRESHOLD → 合并
    3. 语义相似度 < DIFF_TASK_THRESHOLD → 切断
    4. 模糊区域 → 用应用回归 + 关键词重叠辅助判断
    """

    HARD_SPLIT_MINUTES = 30     # 超过此时间强制切断
    SOFT_SPLIT_MINUTES = 10     # 超过此时间，要求更高相似度
    SAME_TASK_THRESHOLD = 0.65  # 高于此值：同一件事
    DIFF_TASK_THRESHOLD = 0.40  # 低于此值：不同的事
    SAME_TASK_THRESHOLD_SOFT = 0.72  # 间隔较长时的更高阈值
    # 跨应用、跨窗口或跨页面时，不能再使用已被当前组“主题中心”污染后的
    # 相似度直接放行。只有新旧两帧本身近乎重复，才有足够证据认为这是
    # 同一任务在不同工具间延续；否则先切开，后续仍可由时间线去重合并。
    CROSS_SURFACE_NEAR_DUP_THRESHOLD = 0.80
    MIN_GROUP_WAIT = 3          # 至少积累3条才开始处理，避免切断进行中的任务

    def __init__(self, embedding_model=None):
        self.embedding_model = embedding_model

    def group_captures(self, captures: list[dict]) -> list[list[dict]]:
        """
        输入：按时间升序排列的 captures 列表
        输出：分组后的片段列表，每个片段是一组 captures

        注意：最后一组可能是进行中的任务，调用方应自行决定是否处理。
        """
        if not captures:
            return []

        if len(captures) == 1:
            return [captures]

        # 批量向量化（有 embedding_model 时）
        vectors = self._batch_encode(captures)

        groups: list[list[dict]] = []
        current_group: list[dict] = [captures[0]]
        current_vectors: list = [vectors[0]] if vectors else []

        for i in range(1, len(captures)):
            curr = captures[i]
            prev = captures[i - 1]
            gap_minutes = (curr['ts'] - prev['ts']) / 60000

            # 规则1：强制切断
            if gap_minutes > self.HARD_SPLIT_MINUTES:
                groups.append(current_group)
                current_group = [curr]
                current_vectors = [vectors[i]] if vectors else []
                continue

            # 规则1.5：文档边界 —— 一份文档独占一个片段。
            # 当前组的"主文档"由组内首个文档型 capture 确定；
            # 新 capture 指向不同文档，或在文档组里出现非文档内容时，强制切断。
            doc_split = self._document_boundary_split(current_group, curr)
            if doc_split:
                groups.append(current_group)
                current_group = [curr]
                current_vectors = [vectors[i]] if vectors else []
                continue

            # 规则1.6：工作表面切换门禁。
            #
            # timeline 3194 的根因是：杭州天气先与 Kim 的通用 UI 词进入模糊区，
            # 随后组主题向量被 ChatGPT 的固定侧栏持续强化，最终使完全无关的
            # Kim、天气、GPU 页面都被“组主题相似度”吸入同一片段。跨表面时改用
            # 相邻两帧的直接相似度，并要求近乎重复；无向量时无法证明连续性，
            # 直接切开。误切可在后续语义去重恢复，误并会不可逆污染时间线。
            if self._surface_changed(prev, curr):
                direct_similarity = (
                    self._cosine_similarity(vectors[i], vectors[i - 1])
                    if vectors else 0.0
                )
                if direct_similarity < self.CROSS_SURFACE_NEAR_DUP_THRESHOLD:
                    logger.info(
                        "工作表面切换切片: prev_id=%s curr_id=%s direct_similarity=%.3f",
                        prev.get('id'), curr.get('id'), direct_similarity,
                    )
                    groups.append(current_group)
                    current_group = [curr]
                    current_vectors = [vectors[i]] if vectors else []
                    continue

            # 规则2/3/4：语义判断
            if vectors:
                should_merge = self._semantic_judge(
                    curr_vector=vectors[i],
                    group_vectors=current_vectors,
                    gap_minutes=gap_minutes,
                    current_group=current_group,
                    curr_capture=curr,
                )
                # 夹心检测：curr 与主题相似度不高，但下一条会回归主题 → curr 是短暂插入，强制切断
                if should_merge and i + 1 < len(captures):
                    theme_vec = self._compute_theme_vector(current_vectors)
                    curr_sim = self._cosine_similarity(vectors[i], theme_vec)
                    if curr_sim < self.SAME_TASK_THRESHOLD:
                        next_sim = self._cosine_similarity(vectors[i + 1], theme_vec)
                        next_gap = (captures[i + 1]['ts'] - curr['ts']) / 60000
                        if next_sim >= self.SAME_TASK_THRESHOLD and next_gap < self.SOFT_SPLIT_MINUTES:
                            should_merge = False
            else:
                # 无向量模型时，退化为关键词判断
                should_merge = self._keyword_judge(current_group, curr)

            if should_merge:
                current_group.append(curr)
                if vectors:
                    current_vectors.append(vectors[i])
            else:
                groups.append(current_group)
                current_group = [curr]
                current_vectors = [vectors[i]] if vectors else []

        if current_group:
            groups.append(current_group)

        logger.info(f"分组完成: {len(captures)} 条 captures → {len(groups)} 个片段")
        return groups

    # ─────────────────────────────────────────────────────────────────────────
    # 内部方法
    # ─────────────────────────────────────────────────────────────────────────

    def _group_primary_document(self, group: list[dict]) -> Optional[str]:
        """当前组的主文档标识：组内第一个文档型 capture 决定。无则 None。"""
        for cap in group:
            ident = _document_identity(cap.get('url'))
            if ident:
                return ident
        return None

    @staticmethod
    def _group_has_unknown_document(group: list[dict]) -> bool:
        """组内存在看起来是文档、但 URL 为空或无法识别的 capture。"""
        return any(
            _looks_like_document_capture(cap) and _document_identity(cap.get('url')) is None
            for cap in group
        )

    def _document_boundary_split(self, current_group: list[dict], curr: dict) -> bool:
        """判断是否因文档边界而强制切片。

        文档 capture 只有在双方都具有非空 URL、且文档 identity 完全相同时才能合并。
        不同 URL、普通 capture、以及看起来像文档但 URL 为空的 capture 都必须切开。
        """
        curr_doc = _document_identity(curr.get('url'))
        primary_doc = self._group_primary_document(current_group)

        if primary_doc is not None:
            return curr_doc != primary_doc

        # URL 为空的文档型 capture 无法证明属于同一文档，保持单条隔离。
        if self._group_has_unknown_document(current_group):
            return True

        # 普通组遇到已知文档或 URL 为空的文档型 capture，都先切出独立片段。
        return curr_doc is not None or _looks_like_document_capture(curr)

    @staticmethod
    def _surface_changed(previous: dict, current: dict) -> bool:
        """相邻采集是否切换了可辨认的工作表面。

        应用不同是确定的表面切换；同一应用下，非空 URL 或窗口标题发生变化
        也视为切换。空标题不作为证据，避免采集能力短暂缺失导致误切。
        """
        previous_app = str(previous.get('app_name') or '').strip().casefold()
        current_app = str(current.get('app_name') or '').strip().casefold()
        if previous_app and current_app and previous_app != current_app:
            return True

        def normalized_url(value: object) -> str:
            raw = str(value or '').strip()
            if not raw:
                return ''
            try:
                parsed = urlparse(raw)
                # 页内锚点只是同一页面的阅读位置，不是工作表面切换。
                raw = parsed._replace(fragment='').geturl()
            except Exception:
                pass
            return raw.rstrip('/').casefold()

        previous_url = normalized_url(previous.get('url'))
        current_url = normalized_url(current.get('url'))
        if previous_url or current_url:
            return previous_url != current_url

        previous_title = str(
            previous.get('window_title') or previous.get('win_title') or ''
        ).strip().casefold()
        current_title = str(
            current.get('window_title') or current.get('win_title') or ''
        ).strip().casefold()
        return bool(
            previous_title
            and current_title
            and previous_title != current_title
        )

    def _batch_encode(self, captures: list[dict]) -> Optional[list]:
        """批量向量化所有 captures"""
        if not self.embedding_model:
            return None
        try:
            texts = [self._get_semantic_text(c) for c in captures]
            embeddings = self.embedding_model.encode(texts)
            return [np.array(e.vector) for e in embeddings]
        except Exception as ex:
            logger.warning(f"向量化失败，退化为关键词判断: {ex}")
            return None

    @staticmethod
    def _capture_text(capture: dict) -> str:
        """按可访问性、OCR、用户输入、音频转写的顺序选择可提炼文本。"""
        return str(
            capture.get('ax_text')
            or capture.get('ocr_text')
            or capture.get('input_text')
            or capture.get('audio_text')
            or ''
        )

    def _get_semantic_text(self, capture: dict) -> str:
        """提取用于语义判断的文本，过滤短行噪声"""
        text = self._capture_text(capture)
        lines = [l.strip() for l in text.split('\n') if len(l.strip()) > 10]
        return ' '.join(lines)[:512]

    def _compute_theme_vector(self, vectors: list) -> np.ndarray:
        """
        计算当前片段的"主题向量"。
        最近3条 capture 权重更高，反映当前任务焦点。
        """
        if len(vectors) == 1:
            return vectors[0]

        n = len(vectors)
        weights = np.ones(n) * 0.5
        # 最近3条加权
        for j, idx in enumerate(range(max(0, n - 3), n)):
            weights[idx] = [0.3, 0.35, 0.4][j] if n >= 3 else 0.5
        weights = weights / weights.sum()

        stacked = np.stack(vectors, axis=0)
        theme = np.average(stacked, axis=0, weights=weights)
        norm = np.linalg.norm(theme)
        return theme / norm if norm > 0 else theme

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    def _semantic_judge(
        self,
        curr_vector: np.ndarray,
        group_vectors: list,
        gap_minutes: float,
        current_group: list[dict],
        curr_capture: dict,
    ) -> bool:
        """基于语义相似度判断是否合并"""
        theme_vector = self._compute_theme_vector(group_vectors)
        similarity = self._cosine_similarity(curr_vector, theme_vector)

        # 间隔较长时要求更高相似度
        threshold = (
            self.SAME_TASK_THRESHOLD_SOFT
            if gap_minutes > self.SOFT_SPLIT_MINUTES
            else self.SAME_TASK_THRESHOLD
        )

        if similarity >= threshold:
            return True
        elif similarity < self.DIFF_TASK_THRESHOLD:
            return False
        else:
            # 模糊区域：用上下文辅助判断
            return self._check_context_continuity(current_group, curr_capture)

    def _looks_like_history_review(self, capture: dict) -> bool:
        app_name = (capture.get('app_name') or '').lower()
        title = (capture.get('window_title') or '').lower()
        text = self._capture_text(capture).lower()
        if not any(keyword in app_name or keyword in title for keyword in _HISTORY_APP_KEYWORDS):
            return False
        return any(pattern in text or pattern in title for pattern in _HISTORY_TEXT_PATTERNS)

    def _history_mode_changed(self, current_group: list[dict], new_capture: dict) -> bool:
        if not current_group:
            return False
        prev = current_group[-1]
        return self._looks_like_history_review(prev) != self._looks_like_history_review(new_capture)

    def _check_context_continuity(
        self,
        current_group: list[dict],
        new_capture: dict,
    ) -> bool:
        """
        模糊区域辅助判断（语义相似度落在 DIFF/SAME 阈值之间时才会调用）：
        1. 长正文保护：新 capture 是密集长正文时，只在与组内内容关键词重叠
           ≥2（同一正文的延续，如连续滚动截图）时允许合并，否则强制独立成组，
           避免汇报/文档正文被其他主题稀释（timeline 2008 教训）；
        2. 关键词重叠 ≥2 → 合并；
        3. 应用回归降级为弱信号：仅在有关键词重叠时才作为合并依据，
           不再一票放行。
        模糊区存疑时偏向切开：误切可在检索/日记阶段恢复，误并稀释不可恢复。
        """
        # 历史回看与实时互动切换时强制切片，避免时间语义混片
        if self._history_mode_changed(current_group, new_capture):
            return False

        # 关键词重叠（只看最近5条，避免早期内容干扰）
        recent_text = ' '.join(
            self._capture_text(c)
            for c in current_group[-5:]
        )
        new_text = self._capture_text(new_capture)

        group_kw = self._extract_keywords(recent_text)
        new_kw = self._extract_keywords(new_text)
        overlap = len(group_kw & new_kw)

        if is_dense_long_text(new_text):
            return overlap >= 2

        if overlap >= 2:
            return True

        # 应用回归 + 关键词弱重叠：仅作为弱证据，单独的应用回归不再放行
        group_apps = {c.get('app_name') for c in current_group if c.get('app_name')}
        if overlap >= 1 and new_capture.get('app_name') in group_apps:
            return True

        return False

    def _keyword_judge(self, current_group: list[dict], new_capture: dict) -> bool:
        """无向量模型时的退化判断（仅关键词 + 应用回归）"""
        return self._check_context_continuity(current_group, new_capture)

    def _extract_keywords(self, text: str) -> set:
        """提取关键词：中文2字以上词组 + 英文3字以上单词，过滤停用词"""
        words = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}', text)
        return {w for w in words if w.lower() not in STOP_WORDS}
