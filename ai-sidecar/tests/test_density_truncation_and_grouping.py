"""密度感知截断 / 分组修复 / 密度差守卫 / 内容文档身份 的回归测试。

回归背景（timeline 2008）：
- IM 汇报正文（19750）被每块 [:800] 截断丢失后半段指标；
- 模糊区"应用回归一票放行"把汇报并进日历/ChatGPT 主题时间线导致稀释。
"""

import json
import sqlite3

import pytest

from background_processor import BackgroundProcessor
from knowledge.extractor_v2 import (
    MERGE_TOTAL_MAX_CHARS,
    KnowledgeExtractorV2,
    _density_aware_truncate,
    _sanitize_capture_text,
    _strip_pressure_noise_lines,
)
from knowledge.fragment_grouper import (
    FragmentGrouper,
    _content_document_identity,
    is_dense_long_text,
    text_density_score,
)

# ─────────────────────────────────────────────────────────────────────────────
# 样本数据：模拟 19750 的 OCR 结构（IM 侧边栏噪声 + 项目进度汇报正文）
# ─────────────────────────────────────────────────────────────────────────────

IM_SIDEBAR_NOISE = "\n".join([
    "出差", "30", "15", "三消息", "26", "328", "稳定性与", "快意对话",
    "商业化9", "33", "保障", "商业化架", "43", "ChatGP", "商业化-",
    "消防群", "广告医", "效运稳定", "1043", "商业化应", "单元化",
    "业务故障", "稳定性-", "商业化值", "V", "商业化核", "商业化异",
    "默认", "重要", "数字员工", "快招 消息号", "新简历评估",
])

REPORT_BODY = (
    "训练进展：\n"
    "1.【PO】电商商品短视频供给\n"
    "a.［进行中］【PO】评测方案优化：\n"
    "i.手指异常识别专家能力建设：当前图片级别召回率87%+，误杀率6.9%，正在基于"
    "快审抽帧策略验证视频级召回率与误杀率，预计下周回收线上dryrun效果；\n"
    "ii.基于标注团队细分标注数据，规划下一步优化方向，预计下周完成整体规划；\n"
    "2.【PO】品牌AI素材场景\n"
    "a.sku生成图片广告素材：\n"
    "i.［进行中］文字生成多样性优化：基于gpt-image2蒸馏数据训练已取得显著效果"
    "（可用率55%提升到77%），正在推进训练集prompt优化，通过丰富提示词强化模型"
    "指令遵循能力，在少量数据下验证效果ok，预计下周扩大数据规模产出新版结果；\n"
    "ii.［进行中］强化学习优化效果：完成强化学习链路搭建，使用少量数据完成验证，"
    "在SFT后模型基础上，实现美学指标平均提升+1.99%，指令遵循+2.45%；\n"
    "b.［进行中］文字乱码评测专家建设：通过PaddleOCR定位文本框+VL单字识别，"
    "结合VL整图识别，在单样本评测成本仅增加0.007元的情况下，实现异常召回率"
    "提升44%（48%提升到92.6%），预计8月中旬接入万擎平台系统；\n"
)

# 汇报正文在 OCR 中位于侧边栏噪声之后（与真实 19750 一致）
REPORT_OCR = IM_SIDEBAR_NOISE + "\n" + REPORT_BODY


# ─────────────────────────────────────────────────────────────────────────────
# 密度判定
# ─────────────────────────────────────────────────────────────────────────────

class TestDensityScore:
    def test_dense_report_scores_high(self):
        assert text_density_score(REPORT_BODY) >= 0.30
        assert is_dense_long_text(REPORT_BODY)

    def test_sidebar_noise_scores_low(self):
        assert text_density_score(IM_SIDEBAR_NOISE) < 0.30
        assert not is_dense_long_text(IM_SIDEBAR_NOISE)

    def test_ax_dense_text_no_newlines(self):
        """AX 文本无换行：句段口径必须能识别密集正文。"""
        ax = ("商业体系AI建设资产复用方案背景AI能力盘点AI业务分类总结。"
              "思路一生成式资产复用短视频生成类数字人直播类图像生成类音频生成类。"
              "原则复用模型文件不复用模型部署，比如Tianmu-Omni模型介绍推理产物"
              "商品短视频商品图口播文案行业脚本内容摘要与标签模特形象资产库。") * 4
        assert "\n" not in ax
        assert text_density_score(ax) >= 0.30
        assert is_dense_long_text(ax)

    def test_short_text_not_dense(self):
        assert not is_dense_long_text("好的，收到")
        assert text_density_score("") == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 噪声剔除
# ─────────────────────────────────────────────────────────────────────────────

class TestNoiseStripping:
    def test_short_run_removed(self):
        """连续 ≥5 条短行（侧边栏形态）整段剔除。"""
        text = IM_SIDEBAR_NOISE + "\n" + REPORT_BODY
        stripped = _strip_pressure_noise_lines(text)
        assert "召回率87%" in stripped
        assert "三消息" not in stripped
        assert "消防群" not in stripped

    def test_chat_short_replies_kept(self):
        """聊天中 姓名+短回复（<5 连续短行）不被误剔。"""
        text = "吴垚\n是的\n沈雨辰\n好的麒哥，我也再优化一下当前版本的召回率问题\n郝康\n收到"
        stripped = _strip_pressure_noise_lines(text)
        assert "吴垚" in stripped
        assert "是的" in stripped

    def test_metric_short_lines_kept(self):
        """含指标特征的短行不计入噪声。"""
        text = "指标\n92.6%\n+1.99%\n0.007¥\n其他"
        stripped = _strip_pressure_noise_lines(text)
        assert "92.6%" in stripped
        assert "+1.99%" in stripped

    def test_shell_words_removed(self):
        stripped = _strip_pressure_noise_lines("消息\n话题\n发送\n换行\n正文内容比较长的一行测试文本")
        assert "发送" not in stripped.split("\n")
        assert "正文内容比较长的一行测试文本" in stripped


# ─────────────────────────────────────────────────────────────────────────────
# 密度感知截断（单文本）
# ─────────────────────────────────────────────────────────────────────────────

class TestDensityAwareTruncate:
    def test_dense_text_gets_full_quota(self):
        long_report = REPORT_BODY * 5
        assert len(long_report) > 2000
        result = _density_aware_truncate(long_report, 2000)
        assert len(result) == 2000

    def test_noise_text_compressed(self):
        noise = (IM_SIDEBAR_NOISE + "\n" + "短\n行\n堆\n积") * 8
        result = _density_aware_truncate(noise, 2000)
        assert len(result) <= 1000

    def test_small_text_untouched(self):
        result = _density_aware_truncate(REPORT_BODY, 2000)
        assert "召回率87%" in result


# ─────────────────────────────────────────────────────────────────────────────
# 合并提炼文本构建（timeline 2008 回归）
# ─────────────────────────────────────────────────────────────────────────────

def _make_capture(cid, ts, app, ocr_text, ax_text=None, title=""):
    return {
        "id": cid,
        "ts": ts,
        "app_name": app,
        "window_title": title,
        "ocr_text": ocr_text,
        "ax_text": ax_text,
        "input_text": "",
        "audio_text": "",
        "url": None,
        "webpage_title": "",
    }


def _build_merged(captures):
    # _build_merged_blocks 不依赖实例状态，可直接以空 self 调用
    return KnowledgeExtractorV2._build_merged_blocks(object.__new__(KnowledgeExtractorV2), captures)


class TestBuildMergedBlocks:
    def test_report_metrics_survive(self):
        """timeline 2008 回归：7 条混主题采集，汇报尾部指标不再被截丢。

        旧逻辑：每块 [:800]，19750 的汇报正文从第 444 字才开始，
        800 字配额只留 ~350 字给正文，"文字乱码评测专家"等尾部段落丢失。
        """
        calendar_ocr = "日历\n2026年07月\n短视频AIGC专项日会\n会议时间\n我的待办\n今天" * 6
        chatgpt_ocr = "创建定时任务：每日上午9:30推送三篇视频图片AIGC论文解读与快手电商落地思路，优先近30天论文，避免重复，引用一手来源"
        captures = [
            _make_capture(19742, 1753849084000, "Kim", calendar_ocr),
            _make_capture(19745, 1753849328000, "ChatGPT", chatgpt_ocr),
            _make_capture(19746, 1753849344000, "Kim", calendar_ocr),
            _make_capture(19747, 1753849394000, "ChatGPT", chatgpt_ocr),
            _make_capture(19748, 1753849418000, "ChatGPT", chatgpt_ocr),
            _make_capture(19749, 1753849504000, "Kim", REPORT_OCR),
            _make_capture(19750, 1753849508000, "Kim", REPORT_OCR),
        ]
        merged = _build_merged(captures)
        assert merged
        # 汇报头部指标
        assert "召回率87%" in merged
        # 汇报尾部指标（旧逻辑必丢）
        assert "92.6%" in merged
        assert "+1.99%" in merged
        # 总长不超限（允许兜底截断标记占位）
        assert len(merged) <= MERGE_TOTAL_MAX_CHARS + 20

    def test_dense_block_not_capped_at_800(self):
        captures = [_make_capture(1, 1753849508000, "Kim", REPORT_OCR)]
        merged = _build_merged(captures)
        assert "92.6%" in merged

    def test_over_budget_many_dense_blocks_keep_metrics(self):
        """10 个密集块总长超 6000：重复块去重 + 指标窗口截断，尾部指标不丢。

        对应 timeline 2008 成员全量回填场景：旧逻辑压缩到 250 后
        仍超限，尾部硬切把汇报块整体切掉。
        """
        dense_filler = (
            "项目进展同步：模型训练任务持续运行，资源调度与任务排队情况稳定，"
            "推理服务吞吐量提升，端到端链路延迟下降，评测流水线自动触发，"
            "指标看板持续更新，团队每日例会同步风险与依赖事项，推进计划按周排期。"
        ) * 10  # 密集长文但无数值指标，单块约 1000 字
        captures = []
        for i in range(6):
            captures.append(_make_capture(100 + i, 1753849000000 + i * 30000, "Kim", dense_filler + str(i)))
        # 完全重复的块（应被去重）
        captures.append(_make_capture(200, 1753849130000, "Kim", dense_filler + "0"))
        captures.append(_make_capture(300, 1753849200000, "Kim", REPORT_OCR))
        merged = _build_merged(captures)
        assert merged
        # 重复块只保留一份：共 7 个块、 6 个分隔符
        assert merged.count("\n\n---\n\n") == 6
        # 汇报尾部指标在超预算压缩后仍保留
        assert "92.6%" in merged
        assert "+1.99%" in merged
        assert len(merged) <= MERGE_TOTAL_MAX_CHARS + 20

    def test_empty_captures(self):
        assert _build_merged([]) == ""
        assert _build_merged([_make_capture(1, 1, "X", "")]) == ""


# ─────────────────────────────────────────────────────────────────────────────
# 分组模糊区修复
# ─────────────────────────────────────────────────────────────────────────────

class TestAmbiguousZoneGrouping:
    def setup_method(self):
        self.grouper = FragmentGrouper(embedding_model=None)

    def test_dense_long_text_without_overlap_splits(self):
        """长正文与组内内容无关键词重叠 → 强制独立成组（2008 修复核心）。"""
        group = [_make_capture(1, 1, "Kim", "日历\n2026年07月\n短视频AIGC专项日会\n会议安排")]
        new_capture = _make_capture(2, 2, "Kim", REPORT_OCR)
        assert self.grouper._check_context_continuity(group, new_capture) is False

    def test_dense_long_text_with_overlap_merges(self):
        """同一汇报的连续滚动截图（关键词大量重叠）→ 保持同组。"""
        group = [_make_capture(1, 1, "Kim", REPORT_OCR)]
        new_capture = _make_capture(2, 2, "Kim", REPORT_OCR)
        assert self.grouper._check_context_continuity(group, new_capture) is True

    def test_app_return_alone_no_longer_passes(self):
        """应用回归不再一票放行：同 app 但零关键词重叠 → 切开。"""
        group = [_make_capture(1, 1, "Chrome", "季度预算审批流程说明与报销规范")]
        new_capture = _make_capture(2, 2, "Chrome", "周末天气查询结果与出行建议")
        assert self.grouper._check_context_continuity(group, new_capture) is False

    def test_app_return_with_weak_overlap_passes(self):
        """应用回归 + 恰好 1 个关键词重叠 → 仍可作为弱证据合并。"""
        group = [_make_capture(1, 1, "Chrome", "预算 审批 流程说明 报销规范")]
        new_capture = _make_capture(2, 2, "Chrome", "预算 最新进展动态")
        assert self.grouper._check_context_continuity(group, new_capture) is True

    def test_keyword_overlap_two_merges(self):
        group = [_make_capture(1, 1, "Chrome", "蒸馏 训练效率 优化方案讨论")]
        new_capture = _make_capture(2, 2, "Kim", "蒸馏 训练效率 最新评测结果")
        assert self.grouper._check_context_continuity(group, new_capture) is True


# ─────────────────────────────────────────────────────────────────────────────
# 内容文档身份（去 URL 化）
# ─────────────────────────────────────────────────────────────────────────────

class TestContentDocumentIdentity:
    def test_im_dense_capture_is_document(self):
        capture = _make_capture(1, 1, "Kim", REPORT_OCR, title="AIGC共建项目")
        identity = _content_document_identity(capture)
        assert identity == "kim::aigc共建项目"

    def test_noise_capture_not_document(self):
        capture = _make_capture(1, 1, "Kim", IM_SIDEBAR_NOISE, title="某群")
        assert _content_document_identity(capture) is None

    def test_non_doc_app_not_document(self):
        capture = _make_capture(1, 1, "Google Chrome", REPORT_BODY * 3, title="页面")
        assert _content_document_identity(capture) is None

    def test_no_title_not_document(self):
        capture = _make_capture(1, 1, "Kim", REPORT_OCR, title="")
        assert _content_document_identity(capture) is None


# ─────────────────────────────────────────────────────────────────────────────
# 内容密度差守卫
# ─────────────────────────────────────────────────────────────────────────────

def _create_minimal_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE captures (
            id INTEGER PRIMARY KEY,
            ts INTEGER NOT NULL,
            app_name TEXT,
            win_title TEXT,
            ocr_text TEXT,
            ax_text TEXT,
            input_text TEXT,
            audio_text TEXT,
            timeline_id INTEGER,
            url TEXT,
            webpage_title TEXT,
            is_sensitive INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE timelines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id INTEGER,
            summary TEXT,
            overview TEXT,
            details TEXT,
            entities TEXT,
            category TEXT,
            importance INTEGER,
            occurrence_count INTEGER,
            capture_ids TEXT,
            start_time INTEGER,
            end_time INTEGER,
            observed_at INTEGER,
            event_time_start INTEGER,
            event_time_end INTEGER,
            history_view INTEGER NOT NULL DEFAULT 0,
            content_origin TEXT,
            activity_type TEXT,
            evidence_strength TEXT
        )
        """
    )
    conn.commit()
    return conn


class TestDensityGuard:
    def test_sparse_group_blocked_from_dense_timeline(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _create_minimal_db(db_path)
        conn.execute("INSERT INTO timelines (id, capture_id, capture_ids) VALUES (1, 100, '[]')")
        conn.execute(
            "INSERT INTO captures (id, ts, app_name, ocr_text, timeline_id) VALUES (100, 1, 'Kim', ?, 1)",
            (REPORT_OCR,),
        )
        conn.commit()
        conn.close()

        processor = BackgroundProcessor(db_path=db_path)
        # 稀薄新片段（侧边栏噪声为主，实质字符 >40）进密集汇报时间线 → 拦截
        sparse_group = [_make_capture(200, 2, "Kim", IM_SIDEBAR_NOISE * 2)]
        assert processor._density_compatible_with_timeline(sparse_group, 1) is False
        # 同样密集的新片段 → 放行
        dense_group = [_make_capture(201, 3, "Kim", REPORT_OCR)]
        assert processor._density_compatible_with_timeline(dense_group, 1) is True

    def test_short_captures_not_blocked(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _create_minimal_db(db_path)
        conn.execute("INSERT INTO timelines (id, capture_id, capture_ids) VALUES (1, 100, '[]')")
        conn.execute(
            "INSERT INTO captures (id, ts, app_name, ocr_text, timeline_id) VALUES (100, 1, 'Kim', '短内容', 1)"
        )
        conn.commit()
        conn.close()

        processor = BackgroundProcessor(db_path=db_path)
        group = [_make_capture(200, 2, "Kim", "另一条短内容")]
        assert processor._density_compatible_with_timeline(group, 1) is True
