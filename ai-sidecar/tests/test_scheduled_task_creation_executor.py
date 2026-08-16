"""
任务创作智能体执行路径测试

覆盖：
1. 创作路径消费 SSE、写创作记录（含 source 字段）、执行记录带 creation_history_id
2. 创作服务不可用 / run.failed 时回退咨询智能体路径
3. 日记任务不受 executor_kind 影响
4. @ 提及解析边界规则与桌面端保持一致
"""

import base64
import json
import sqlite3
from types import SimpleNamespace

import httpx

from scheduled_task_executor import TaskExecutor


class _AllowAllEnergyPolicy:
    def current_profile(self):
        return SimpleNamespace(allow_diary=True, mode="charging", battery_percent=100)


def _create_tables(conn: sqlite3.Connection, with_history_column: bool) -> None:
    creation_history_column = (
        "creation_history_id INTEGER," if with_history_column else ""
    )
    conn.executescript(
        """
        CREATE TABLE scheduled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            user_instruction TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            template_id TEXT,
            notification_channel_ids TEXT DEFAULT '[]',
            executor_kind TEXT NOT NULL DEFAULT 'consult',
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
            latency_ms INTEGER,
            {creation_history_column}
            dummy INTEGER DEFAULT 0
        );
        """.format(creation_history_column=creation_history_column)
    )
    conn.commit()


def _insert_task(
    conn: sqlite3.Connection,
    name: str,
    instruction: str,
    executor_kind: str = "consult",
) -> int:
    conn.execute(
        """
        INSERT INTO scheduled_tasks
            (name, user_instruction, cron_expression, template_id,
             notification_channel_ids, executor_kind, created_at, updated_at)
        VALUES (?, ?, '0 9 * * *', NULL, '[]', ?, 1, 1)
        """,
        (name, instruction, executor_kind),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class _FakeStreamResponse:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        return b"stream unavailable"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeStreamClient:
    def __init__(self, recorded, response):
        self._recorded = recorded
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stream(self, method, url, json=None, **_kwargs):
        self._recorded.append({"method": method, "url": url, "json": json})
        return self._response


def _installed_skill_fixture():
    skill_md = "# 行业白皮书技能\n按模板生成白皮书。"
    return [
        {
            "client_skill_key": "skill-key-1",
            "title": "行业白皮书",
            "summary": "生成行业白皮书",
            "skill_description": "按固定结构生成行业白皮书",
            "execution_steps": ["收集素材", "撰写正文"],
            "package_files": [
                {
                    "path": "SKILL.md",
                    "media_type": "text/markdown",
                    "content_base64": base64.b64encode(skill_md.encode("utf-8")).decode("ascii"),
                    "size_bytes": len(skill_md.encode("utf-8")),
                }
            ],
        }
    ]


def _mock_creation_services(
    monkeypatch,
    sse_lines,
    skills=None,
    history_id=42,
    history_posts=None,
    skills_error=None,
    history_error=None,
):
    """统一替换 httpx 的 get/post/Client，返回 agent/run 的请求记录列表。"""
    run_requests = []

    def fake_get(url, **_kwargs):
        if skills_error is not None:
            raise skills_error
        assert "/api/creation/skills" in url
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: (skills if skills is not None else []),
        )

    def fake_post(url, json=None, **_kwargs):
        assert url.endswith("/api/creation/history")
        if history_posts is not None:
            history_posts.append(json)
        if history_error is not None:
            raise history_error
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"id": history_id},
        )

    def fake_client(**_kwargs):
        response = _FakeStreamResponse(sse_lines)
        return _FakeStreamClient(run_requests, response)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "Client", fake_client)
    return run_requests


def _fetch_execution_row(db_path):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        """SELECT status, result_text, creation_history_id
           FROM task_executions ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    conn.close()
    return row


def test_creation_task_consumes_sse_and_links_history(tmp_path, monkeypatch):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    # 不带 creation_history_id 列，顺带验证 sidecar 的补列防御。
    _create_tables(conn, with_history_column=False)
    instruction = "请 @行业白皮书 撰写报告，交给 @文档撰写 Agent 审校"
    task_id = _insert_task(conn, "行业周报", instruction, executor_kind="creation")
    conn.close()

    completed_document = "# 最终报告\n正文内容"
    sse_lines = [
        'data: {"type": "run.started", "data": {}}',
        'data: {"type": "document.delta", "data": {"content": "# 最终"}}',
        "data: " + json.dumps(
            {"type": "run.completed", "data": {"document": completed_document}},
            ensure_ascii=False,
        ),
        'data: {"type": "run.ignored-after-completed", "data": {}}',
    ]
    history_posts = []
    run_requests = _mock_creation_services(
        monkeypatch,
        sse_lines,
        skills=_installed_skill_fixture(),
        history_posts=history_posts,
    )

    executor = TaskExecutor(db_path=str(db_path))
    result = executor.execute_task(task_id)

    assert result["status"] == "success"
    assert result["result"] == completed_document

    # 创作 Agent 请求：指令透传 + 被 @ 的技能进入 selected_skills，Agent 提及不混入。
    assert len(run_requests) == 1
    payload = run_requests[0]["json"]
    assert run_requests[0]["url"].endswith("/api/creation/agent/run")
    assert payload["user_prompt"] == instruction
    assert payload["root_request"] == instruction
    assert payload["model_mode"] == "local"
    assert payload["confirmed"] is True
    assert payload["session_id"] == "session-task-{}-{}".format(task_id, result["exec_id"])
    assert [skill["title"] for skill in payload["selected_skills"]] == ["行业白皮书"]
    selected = payload["selected_skills"][0]
    assert selected["id"] == "skill-key-1"
    assert selected["workflowRole"] == "primary"
    assert "行业白皮书技能" in selected["skillInstructions"]

    # 创作记录写入携带 source 字段与执行流水。
    assert len(history_posts) == 1
    history_payload = history_posts[0]
    assert history_payload["source_kind"] == "scheduled_task"
    assert history_payload["source_ref_id"] == task_id
    assert history_payload["generated_content"] == completed_document
    assert [event["type"] for event in history_payload["agent_trace"]][:2] == [
        "run.started",
        "document.delta",
    ]

    # 执行记录带 creation_history_id，且补列防御已生效。
    status, result_text, creation_history_id = _fetch_execution_row(db_path)
    assert status == "success"
    assert result_text == completed_document
    assert creation_history_id == 42


def test_history_write_failure_keeps_execution_success(tmp_path, monkeypatch):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    _create_tables(conn, with_history_column=True)
    task_id = _insert_task(conn, "行业周报", "生成周报", executor_kind="creation")
    conn.close()

    sse_lines = [
        'data: {"type": "run.completed", "data": {"document": "周报正文"}}',
    ]
    _mock_creation_services(monkeypatch, sse_lines, history_error=httpx.ConnectError("down"))

    executor = TaskExecutor(db_path=str(db_path))
    result = executor.execute_task(task_id)

    # 创作记录写入失败不阻断任务成功，creation_history_id 为空。
    assert result["status"] == "success"
    assert result["result"] == "周报正文"
    status, _, creation_history_id = _fetch_execution_row(db_path)
    assert status == "success"
    assert creation_history_id is None


def test_creation_service_unavailable_falls_back_to_consult(tmp_path, monkeypatch):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    _create_tables(conn, with_history_column=True)
    task_id = _insert_task(conn, "行业周报", "生成周报", executor_kind="creation")
    conn.close()

    _mock_creation_services(monkeypatch, [], skills_error=httpx.ConnectError("refused"))

    executor = TaskExecutor(db_path=str(db_path))
    executor._query_knowledge = lambda conn, user_instruction: []
    executor._llm_generate = lambda **_kwargs: "咨询智能体结果"
    result = executor.execute_task(task_id)

    assert result["status"] == "success"
    assert result["result"].startswith("[创作智能体执行失败（")
    assert "咨询智能体结果" in result["result"]
    status, _, creation_history_id = _fetch_execution_row(db_path)
    assert status == "success"
    assert creation_history_id is None


def test_run_failed_event_falls_back_to_consult(tmp_path, monkeypatch):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    _create_tables(conn, with_history_column=True)
    task_id = _insert_task(conn, "行业周报", "生成周报", executor_kind="creation")
    conn.close()

    sse_lines = [
        'data: {"type": "run.failed", "summary": "创作模型不可用"}',
    ]
    run_requests = _mock_creation_services(monkeypatch, sse_lines, skills=[])

    executor = TaskExecutor(db_path=str(db_path))
    executor._query_knowledge = lambda conn, user_instruction: []
    executor._llm_generate = lambda **_kwargs: "咨询智能体结果"
    result = executor.execute_task(task_id)

    assert len(run_requests) == 1
    assert result["status"] == "success"
    assert "创作模型不可用" in result["result"]
    assert "咨询智能体结果" in result["result"]


def test_diary_task_keeps_diary_path_even_with_creation_kind(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    conn = sqlite3.connect(db_path)
    _create_tables(conn, with_history_column=True)
    task_id = _insert_task(
        conn, "生成昨日工作日记", "生成昨日工作日记", executor_kind="creation"
    )
    conn.close()

    executor = TaskExecutor(db_path=str(db_path))
    executor.energy_policy = _AllowAllEnergyPolicy()

    def _forbid_creation(*_args, **_kwargs):
        raise AssertionError("日记任务不应进入创作智能体路径")

    executor._execute_creation_task = _forbid_creation
    executor._execute_diary_task = lambda conn, task, period: {
        "result_text": "日记内容",
        "source_count": 2,
        "token_estimate": 100,
    }
    result = executor.execute_task(task_id)

    assert result["status"] == "success"
    assert result["result"] == "日记内容"
    status, _, creation_history_id = _fetch_execution_row(db_path)
    assert status == "success"
    assert creation_history_id is None


def test_parse_mentioned_skills_follows_boundary_rules():
    executor = TaskExecutor(db_path=":memory:")

    # 最长名称优先；"@行业" 后紧跟提及字符 X 不算命中（由兜底正则捕获为未安装提及）；
    # 工具/Agent 提及不算技能。
    mentioned = executor._parse_task_mentioned_skills(
        "用 @行业白皮书 和 @数据检索 生成 @行业X 参考 @未安装技能",
        ["行业", "行业白皮书"],
    )
    assert mentioned == ["行业白皮书", "行业X", "未安装技能"]

    # 无提及时返回空列表。
    assert executor._parse_task_mentioned_skills("普通指令", ["行业白皮书"]) == []
