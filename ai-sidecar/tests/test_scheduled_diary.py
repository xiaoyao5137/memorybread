import json
import sqlite3
import sys
from datetime import date, datetime, time as datetime_time, timedelta
from types import SimpleNamespace

from scheduled_task_executor import TaskExecutor


class _EnergyPolicy:
    def __init__(self, allow_diary: bool) -> None:
        self.allow_diary = allow_diary

    def current_profile(self):
        return SimpleNamespace(
            allow_diary=self.allow_diary,
            mode="charging" if self.allow_diary else "battery",
            battery_percent=58,
        )


def _create_common_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE scheduled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            user_instruction TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            template_id TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            run_count INTEGER NOT NULL DEFAULT 0,
            last_run_at INTEGER,
            last_run_status TEXT,
            next_run_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE task_executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            completed_at INTEGER,
            status TEXT NOT NULL DEFAULT 'running',
            knowledge_count INTEGER DEFAULT 0,
            token_used INTEGER DEFAULT 0,
            result_text TEXT,
            error_message TEXT,
            latency_ms INTEGER
        );

        CREATE TABLE timelines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id INTEGER,
            overview TEXT,
            details TEXT,
            category TEXT,
            importance INTEGER,
            start_time INTEGER,
            end_time INTEGER,
            duration_minutes INTEGER,
            frag_app_name TEXT,
            entities TEXT,
            user_verified INTEGER,
            observed_at INTEGER,
            event_time_start INTEGER,
            event_time_end INTEGER,
            history_view INTEGER,
            content_origin TEXT,
            activity_type TEXT,
            is_self_generated INTEGER,
            evidence_strength TEXT,
            created_at_ms INTEGER
        );
        """
    )
    conn.commit()


def _insert_task(conn: sqlite3.Connection, template_id: str, name: str) -> int:
    conn.execute(
        """
        INSERT INTO scheduled_tasks
            (name, user_instruction, cron_expression, template_id, created_at, updated_at)
        VALUES (?, ?, '0 9 * * *', ?, 1, 1)
        """,
        (name, name, template_id),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _fake_llm(**_kwargs) -> str:
    return (
        "## 今日产出\n"
        "- 完成了日记生成链路\n"
        "## 问题与解决\n"
        "- 修复了忙时任务被吞的问题\n"
        "## 明日计划\n"
        "- 验证周记汇总"
    )


def _insert_timeline_for_day(
    conn: sqlite3.Connection,
    day: str,
    overview: str = "实现日记 API",
) -> None:
    noon = datetime.combine(date.fromisoformat(day), datetime_time(hour=12))
    ts_ms = int(noon.timestamp() * 1000)
    conn.execute(
        """
        INSERT INTO timelines
            (capture_id, overview, details, category, importance, start_time,
             end_time, duration_minutes, frag_app_name, entities, is_self_generated, created_at_ms)
        VALUES (1, ?, '新增 /api/diaries', '开发', 4, ?, ?, 45, 'IDE', '[]', 0, ?)
        """,
        (overview, ts_ms, ts_ms + 45 * 60 * 1000, ts_ms),
    )


def test_daily_diary_task_writes_yesterday_diary(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    _create_common_tables(conn)
    task_id = _insert_task(conn, "daily_journal", "生成昨日工作日记")

    _, _, diary_date = TaskExecutor._resolve_diary_period("daily")
    _insert_timeline_for_day(conn, diary_date)
    conn.commit()
    conn.close()

    executor = TaskExecutor(
        db_path=str(db_path),
        energy_policy=_EnergyPolicy(True),
    )
    executor._llm_generate = _fake_llm
    result = executor.execute_task(task_id)

    assert result["status"] == "success"
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT period_type, diary_date, content, source_timeline_ids FROM diaries"
    ).fetchone()
    content = json.loads(row[2])
    assert row[0] == "daily"
    assert row[1] == diary_date
    assert content["work_outputs"] == ["完成了日记生成链路"]
    assert "next_plan" not in content
    assert "明日计划" not in content["markdown"]
    assert content["language"] == "zh-CN"
    assert content["work_environment"] == {
        "apps": ["IDE"],
        "categories": ["开发"],
        "entities": [],
    }
    assert json.loads(row[3]) == [1]


def test_daily_diary_task_only_generates_latest_completed_day(tmp_path, monkeypatch):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    _create_common_tables(conn)
    task_id = _insert_task(conn, "daily_journal", "生成昨日工作日记")
    monkeypatch.setattr(
        TaskExecutor,
        "_resolve_recent_daily_dates",
        staticmethod(lambda today=None, days=2: ["2026-07-08", "2026-07-09"]),
    )

    _insert_timeline_for_day(conn, "2026-07-08", "修复日记刷新")
    _insert_timeline_for_day(conn, "2026-07-09", "补齐最近日记")
    conn.commit()
    conn.close()

    executor = TaskExecutor(
        db_path=str(db_path),
        energy_policy=_EnergyPolicy(True),
    )
    executor._llm_generate = _fake_llm
    result = executor.execute_task(task_id)

    assert result["status"] == "success"
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT diary_date, source_timeline_ids FROM diaries WHERE period_type = 'daily' ORDER BY diary_date"
    ).fetchall()
    assert [row[0] for row in rows] == ["2026-07-09"]
    assert [json.loads(row[1]) for row in rows] == [[2]]


def test_daily_diary_catchup_does_not_overwrite_user_edited_diary(tmp_path, monkeypatch):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    _create_common_tables(conn)
    task_id = _insert_task(conn, "daily_journal", "生成昨日工作日记")
    monkeypatch.setattr(
        TaskExecutor,
        "_resolve_recent_daily_dates",
        staticmethod(lambda today=None, days=2: ["2026-07-08", "2026-07-09"]),
    )

    executor = TaskExecutor(
        db_path=str(db_path),
        energy_policy=_EnergyPolicy(True),
    )
    executor._ensure_diaries_table(conn)
    conn.execute(
        """
        INSERT INTO diaries
            (period_type, period_start, period_end, diary_date, content, is_system_generated)
        VALUES ('daily', '2026-07-08', '2026-07-08', '2026-07-08', ?, 0)
        """,
        (json.dumps({"markdown": "用户手写日记"}, ensure_ascii=False),),
    )
    _insert_timeline_for_day(conn, "2026-07-08", "这天不应覆盖")
    _insert_timeline_for_day(conn, "2026-07-09", "这天需要生成")
    conn.commit()
    conn.close()

    executor._llm_generate = _fake_llm
    result = executor.execute_task(task_id)

    assert result["status"] == "success"
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT diary_date, content, is_system_generated FROM diaries WHERE period_type = 'daily' ORDER BY diary_date"
    ).fetchall()
    assert rows[0][0] == "2026-07-08"
    assert json.loads(rows[0][1])["markdown"] == "用户手写日记"
    assert rows[0][2] == 0
    assert rows[1][0] == "2026-07-09"
    assert json.loads(rows[1][1])["work_outputs"] == ["完成了日记生成链路"]


def test_idle_diary_backfill_generates_latest_missing_historical_date(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    _create_common_tables(conn)
    task_id = _insert_task(conn, "daily_journal", "生成昨日工作日记")

    _insert_timeline_for_day(conn, "2026-07-06", "较早缺口")
    _insert_timeline_for_day(conn, "2026-07-08", "最近缺口")
    _insert_timeline_for_day(conn, "2026-07-10", "今天不应自动生成")
    conn.commit()
    conn.close()

    executor = TaskExecutor(
        db_path=str(db_path),
        energy_policy=_EnergyPolicy(True),
    )
    executor._llm_generate = _fake_llm
    result = executor.execute_idle_diary_backfill_once(
        lookback_days=7,
        today=date(2026, 7, 10),
    )

    assert result["status"] == "success"
    assert result["diary_date"] == "2026-07-08"
    assert result["exec_id"] is not None

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT diary_date, source_timeline_ids FROM diaries WHERE period_type = 'daily' ORDER BY diary_date"
    ).fetchall()
    task_row = conn.execute(
        "SELECT run_count, last_run_status FROM scheduled_tasks WHERE id = ?",
        (task_id,),
    ).fetchone()

    assert [row[0] for row in rows] == ["2026-07-08"]
    assert json.loads(rows[0][1]) == [2]
    assert task_row == (1, "success")


def test_idle_diary_backfill_creates_default_daily_task_when_missing(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    _create_common_tables(conn)
    _insert_timeline_for_day(conn, "2026-07-08", "完成了默认日记任务自愈")
    conn.commit()
    conn.close()

    executor = TaskExecutor(
        db_path=str(db_path),
        energy_policy=_EnergyPolicy(True),
    )
    executor._llm_generate = _fake_llm
    result = executor.execute_idle_diary_backfill_once(
        lookback_days=7,
        today=date(2026, 7, 10),
    )

    assert result["status"] == "success"
    assert result["diary_date"] == "2026-07-08"

    conn = sqlite3.connect(db_path)
    task_row = conn.execute(
        "SELECT template_id, name, enabled, next_run_at, run_count, last_run_status FROM scheduled_tasks"
    ).fetchone()
    assert task_row == ("daily_journal", "生成昨日工作日记", 1, 0, 1, "success")


def test_idle_diary_backfill_skips_existing_diaries(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    _create_common_tables(conn)
    _insert_task(conn, "daily_journal", "生成昨日工作日记")

    executor = TaskExecutor(
        db_path=str(db_path),
        energy_policy=_EnergyPolicy(True),
    )
    executor._ensure_diaries_table(conn)
    conn.execute(
        """
        INSERT INTO diaries
            (period_type, period_start, period_end, diary_date, content, is_system_generated)
        VALUES ('daily', '2026-07-08', '2026-07-08', '2026-07-08', ?, 1)
        """,
        (json.dumps({"markdown": "已有系统日记"}, ensure_ascii=False),),
    )
    _insert_timeline_for_day(conn, "2026-07-07", "旧缺口")
    _insert_timeline_for_day(conn, "2026-07-08", "已有日记")
    conn.commit()
    conn.close()

    executor._llm_generate = _fake_llm
    result = executor.execute_idle_diary_backfill_once(
        lookback_days=7,
        today=date(2026, 7, 10),
    )

    assert result["status"] == "success"
    assert result["diary_date"] == "2026-07-07"

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT diary_date, content FROM diaries WHERE period_type = 'daily' ORDER BY diary_date"
    ).fetchall()
    assert [row[0] for row in rows] == ["2026-07-07", "2026-07-08"]
    assert json.loads(rows[1][1])["markdown"] == "已有系统日记"


def test_daily_diary_context_filters_low_value_activity():
    executor = TaskExecutor(db_path=":memory:")
    context, token_estimate = executor._build_daily_diary_context([
        {
            "id": 1,
            "overview": "浏览技术文档并切换多个页面",
            "details": "查看资料，没有形成结论",
            "category": "浏览",
            "importance": 2,
            "duration_minutes": 20,
            "start_time": 1783454400000,
        },
        {
            "id": 2,
            "overview": "完成了日记缺口补齐逻辑，修复默认任务缺失问题",
            "details": "增加自愈任务创建与最近日期补偿",
            "category": "开发",
            "importance": 3,
            "duration_minutes": 45,
            "start_time": 1783458000000,
        },
    ])

    assert "完成了日记缺口补齐逻辑" in context
    assert "浏览技术文档" not in context
    assert token_estimate > 0


def test_daily_diary_context_identifies_work_environment():
    executor = TaskExecutor(db_path=":memory:")
    context, _ = executor._build_daily_diary_context([
        {
            "id": 1,
            "overview": "完成了登录接口",
            "details": "补充鉴权测试",
            "category": "开发",
            "importance": 4,
            "duration_minutes": 50,
            "start_time": 1783454400000,
            "app_name": "Visual Studio Code",
            "entities": ["MemoryBread", "鉴权"],
        },
        {
            "id": 2,
            "overview": "确定了发布结论",
            "details": "发布检查已通过",
            "category": "协作",
            "importance": 4,
            "duration_minutes": 20,
            "start_time": 1783458000000,
            "app_name": "飞书",
            "entities": '["MemoryBread", "发布"]',
        },
    ])

    assert "【工作环境摘要】" in context
    assert "输出语言：简体中文" in context
    assert "Visual Studio Code、飞书" in context
    assert "主要工作类型：开发、协作" in context
    assert "项目/主题线索：MemoryBread、鉴权、发布" in context


def test_daily_diary_language_defaults_to_chinese_and_allows_explicit_override(monkeypatch):
    monkeypatch.delenv("MEMORY_BREAD_DIARY_LANGUAGE", raising=False)

    assert TaskExecutor._resolve_diary_output_language("生成工作日记") == "zh-CN"
    assert TaskExecutor._resolve_diary_output_language("请用英文输出工作日记") == "en"
    assert TaskExecutor._resolve_diary_output_language(
        "默认使用简体中文；本任务请用英文输出"
    ) == "en"


def test_daily_diary_markdown_is_compacted_to_fixed_sections_and_limits():
    long_item = "完成了一个很长的功能说明" * 8
    raw = (
        "这里是一段冗长的背景铺垫。\n"
        "## 今日产出\n"
        f"1. {long_item}\n"
        "2. 完成第二项\n3. 完成第三项\n4. 完成第四项\n5. 不应保留的第五项\n"
        "## 问题与解决\n- 修复问题一\n- 修复问题二\n- 不应保留的问题三\n"
        "## 明日计划\n- 验证中文输出\n"
        "## 额外总结\n- 这段也不应保留"
    )

    compacted = TaskExecutor._normalize_daily_diary_markdown(raw)
    outputs = TaskExecutor._extract_markdown_section_items(compacted, ("今日产出",))
    problems = TaskExecutor._extract_markdown_section_items(compacted, ("问题与解决",))

    assert compacted.count("## ") == 2
    assert "背景铺垫" not in compacted
    assert "额外总结" not in compacted
    assert "明日计划" not in compacted
    assert "验证中文输出" not in compacted
    assert len(outputs) == 4
    assert len(outputs[0]) <= 45
    assert len(problems) == 2


def test_daily_diary_instruction_excludes_future_plans():
    instruction = TaskExecutor._daily_diary_instruction("2026-07-15")

    assert "## 今日产出" in instruction
    assert "## 问题与解决" in instruction
    assert "## 明日计划" not in instruction
    assert "不要生成明日计划" in instruction


def test_diary_rollup_context_ignores_legacy_daily_plans():
    context = TaskExecutor(db_path=":memory:")._build_diary_rollup_context([
        {
            "diary_date": "2026-07-15",
            "content": {
                "title": "2026-07-15 工作日记",
                "work_outputs": ["完成日记生成约束"],
                "next_plan": ["明天继续开发"],
            },
        },
    ])

    assert "完成日记生成约束" in context
    assert "明天继续开发" not in context


def test_daily_diary_chinese_output_check_rejects_english_body():
    assert TaskExecutor._is_chinese_diary_output("## 今日产出\n- 完成了中文日记输出约束")
    assert not TaskExecutor._is_chinese_diary_output(
        "## 今日产出\n- Implemented the diary generation pipeline and shipped tests"
    )
    assert not TaskExecutor._is_chinese_diary_output(
        "## 今日产出\n"
        "- Constraints (from the user's latest prompt):\n"
        "## 问题与解决\n- 已要求输出中文"
    )


def test_diary_ollama_client_bypasses_system_proxy(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "ollama", SimpleNamespace(Client=FakeClient))
    executor = TaskExecutor(db_path=":memory:")

    assert isinstance(executor._get_llm_client(), FakeClient)
    assert captured == {"host": "http://127.0.0.1:11434", "trust_env": False}


def test_llm_generate_rewrites_english_diary_once(monkeypatch):
    class FakeTracker:
        _prompt_tokens = 0

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def set_response(self, _response):
            pass

        def set_tokens(self, **_kwargs):
            pass

    class FakeQueue:
        @staticmethod
        def submit_sync(_priority, fn, **_kwargs):
            return fn()

    class FakeClient:
        def __init__(self):
            self.calls = []

        def chat(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                content = "## 今日产出\n- Implemented the diary pipeline and added tests"
            else:
                content = "## 今日产出\n- 完成日记生成链路并补充测试"
            return {"message": {"content": content}}

    monkeypatch.setitem(
        sys.modules,
        "monitor.llm_tracker",
        SimpleNamespace(LLMCallTracker=FakeTracker, estimate_tokens=lambda text: len(text) // 4),
    )
    monkeypatch.setitem(
        sys.modules,
        "model_registry_global",
        SimpleNamespace(get_active_ollama_model=lambda: "test-model"),
    )
    monkeypatch.setitem(
        sys.modules,
        "inference_queue",
        SimpleNamespace(
            LANE_P2_DIARY="diary",
            Priority=SimpleNamespace(P2=2),
            get_global_queue=lambda: FakeQueue(),
        ),
    )

    executor = TaskExecutor(db_path=":memory:")
    client = FakeClient()
    executor._llm_client = client
    result = executor._llm_generate(
        user_instruction=executor._daily_diary_instruction("2026-07-12"),
        context="[IDE] Implemented the diary pipeline",
        output_language="zh-CN",
        concise=True,
    )

    assert result == "## 今日产出\n- 完成日记生成链路并补充测试"
    assert len(client.calls) == 2
    assert "必须使用简体中文" in client.calls[0]["messages"][0]["content"]
    assert client.calls[0]["think"] is False
    assert client.calls[0]["options"]["num_predict"] == 768
    assert "严格改写为简体中文" in client.calls[1]["messages"][-1]["content"]
    assert client.calls[1]["think"] is False


def test_weekly_diary_uses_timelines_as_sources(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    _create_common_tables(conn)
    task_id = _insert_task(conn, "weekly_report", "生成上周工作周记")

    executor = TaskExecutor(
        db_path=str(db_path),
        energy_policy=_EnergyPolicy(True),
    )
    executor._ensure_diaries_table(conn)
    start, end, diary_date = TaskExecutor._resolve_diary_period("weekly")
    start_day = date.fromisoformat(start)
    for offset in range(2):
        day = (start_day + timedelta(days=offset)).isoformat()
        _insert_timeline_for_day(conn, day, f"完成了第 {offset + 1} 项产出")
    conn.commit()
    conn.close()

    executor._llm_generate = _fake_llm
    result = executor.execute_task(task_id)

    assert result["status"] == "success"
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT period_type, diary_date, content, source_timeline_ids, source_diary_ids "
        "FROM diaries WHERE period_type = 'weekly'"
    ).fetchone()
    content = json.loads(row[2])
    assert row[0] == "weekly"
    assert row[1] == diary_date
    assert content["source_count"] == 2
    assert json.loads(row[3]) == [1, 2]
    assert json.loads(row[4]) == []


def test_diary_task_defers_on_battery_without_creating_execution(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    _create_common_tables(conn)
    task_id = _insert_task(conn, "daily_journal", "生成昨日工作日记")
    conn.close()

    executor = TaskExecutor(
        db_path=str(db_path),
        energy_policy=_EnergyPolicy(False),
    )
    executor._llm_generate = _fake_llm

    result = executor.execute_task(task_id)

    assert result["status"] == "deferred"
    assert result["reason"] == "waiting_for_external_power"
    conn = sqlite3.connect(db_path)
    execution_count = conn.execute("SELECT COUNT(*) FROM task_executions").fetchone()[0]
    task_row = conn.execute(
        "SELECT run_count, last_run_status FROM scheduled_tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    conn.close()
    assert execution_count == 0
    assert task_row == (0, None)
