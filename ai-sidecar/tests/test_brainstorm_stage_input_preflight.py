"""The public stage batch path checks inputs before a configured detail catalog."""

import pytest

from creation.brainstorm_stages import BrainstormStageCoordinator


SKILL = {"id": "weekly-skill", "title": "周报模板", "executionSteps": [
    {"id": "meeting", "title": "项目周会纪要",
     "objective": "用记忆搜索取得本周项目周会纪要", "tools": ["memory_search"]},
]}


class LocalSourceService:
    def __init__(self):
        self.calls = []

    def retrieve_references(self, query, _parsed, _options):
        self.calls.append(query)
        return [{"id": 42, "source_type": "document", "title": "项目周会纪要",
                 "full_content": "统计周期：2026-09-21 至 2026-09-27。会议原文。",
                 "review_status": "complete"}]


class EmptyReportSourceService:
    def __init__(self):
        self.db_path = "/tmp/brainstorm-v17-input-empty.db"
        self.calls = []

    def retrieve_references(self, query, _parsed, _options):
        self.calls.append(("memory", query))
        return []

    async def retrieve_data_context(self, query, _parsed, limit=30):
        self.calls.append(("data", query))
        return []


@pytest.mark.asyncio
async def test_stage_batch_reads_memory_then_asks_period_before_catalog():
    service = LocalSourceService()
    coordinator = BrainstormStageCoordinator(service)
    result = await coordinator.stage_batch(stage="detail", kind="input_preflight",
        root_request="请写项目周报", decisions=[], selected_skills=[SKILL],
        input_as_of_local_date="2026-09-28", input_timezone="Asia/Shanghai")
    assert len(service.calls) == 1
    assert result["input_plan_status"] == "question"
    assert len(result["questions"]) == 1
    assert result["questions"][0]["input_clarification"]["kind"] == "report_period"
    assert result["input_preflight"]["needs"][0]["status"] == "candidate"
    assert all(not option["recommended"] for option in result["questions"][0]["options"])

    period_option = result["questions"][0]["options"][0]
    period = dict(period_option["input_value"], source="user")
    decision = {"question_id": result["questions"][0]["id"],
                "input_clarification": result["questions"][0]["input_clarification"],
                "answer_source": "user", "selected_options": [period_option]}
    resumed = await coordinator.stage_batch(stage="detail", kind="input_preflight",
        root_request="请写项目周报", decisions=[], selected_skills=[SKILL],
        confirmed_period=period, input_decisions=[decision],
        input_as_of_local_date="2026-09-28", input_timezone="Asia/Shanghai")
    assert resumed["input_preflight"]["period"]["status"] == "confirmed"
    assert resumed["input_preflight"]["needs"][0]["status"] == "matched"
    assert resumed["questions"] == []


@pytest.mark.asyncio
async def test_content_mode_compute_cost_report_asks_for_actual_source_or_gap():
    service = EmptyReportSourceService()
    coordinator = BrainstormStageCoordinator(service)
    request = dict(stage="detail", kind="input_preflight",
        root_request="请生成GPU成本优化的周报", decisions=[], selected_skills=[],
        confirmed_period={"start": "2026-09-21", "end": "2026-09-27",
                          "source": "user", "timezone": "Asia/Shanghai"},
        input_as_of_local_date="2026-09-30", input_timezone="Asia/Shanghai")
    legacy = await coordinator.stage_batch(**request)
    assert legacy["input_preflight"]["needs"] == []
    current = await coordinator.stage_batch(**request, content_mode=True)
    assert len(current["input_preflight"]["needs"]) == 3
    assert all("GPU" in need["label"] and need["status"] == "missing"
               for need in current["input_preflight"]["needs"])
    assert current["input_plan_status"] == "question"
    assert len(current["questions"]) == 3
    assert all(question["input_clarification"]["kind"] == "source_confirmation"
               and "GPU" in question["prompt"] for question in current["questions"])
    assert len(service.calls) >= 2
