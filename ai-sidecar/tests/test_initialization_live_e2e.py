from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Optional

import pytest

from initialization_manager import SANDBOX_COLD_INSTALL_STAGES


SIDECAR = os.environ.get("MEMORY_BREAD_MODEL_API_URL", "http://127.0.0.1:7071")


def _request(path: str, method: str = "GET", body: Optional[dict] = None) -> dict:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{SIDECAR}{path}",
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


@pytest.mark.live_initialization
@pytest.mark.skipif(
    os.environ.get("MEMORY_BREAD_RUN_LIVE_INITIALIZATION_E2E") != "1",
    reason="真实冷初始化会下载完整运行时与模型，仅在显式开启时执行",
)
def test_live_cold_initialization_uses_only_sandbox_and_restores_normal_state():
    normal_before = _request("/api/initialization/status")["initialization"]
    enabled = False
    try:
        sandbox = _request(
            "/api/initialization/test-mode",
            "POST",
            {"confirmation": "ENABLE_INITIALIZATION_TEST_MODE"},
        )["initialization"]
        enabled = True
        assert sandbox["mode"] == "sandbox"
        assert sandbox["sandbox_isolation"] == {
            "enforced": True,
            "cold_start": True,
            "normal_runtime_hidden": True,
            "normal_models_hidden": True,
            "normal_database_hidden": True,
        }

        _request("/api/initialization/start", "POST", {"mode": "sandbox"})
        deadline = time.monotonic() + 2 * 60 * 60
        finished = _request("/api/initialization/status")["initialization"]
        while finished["state"] == "running" and time.monotonic() < deadline:
            time.sleep(2)
            finished = _request("/api/initialization/status")["initialization"]

        assert finished["state"] == "completed", finished
        stages = {stage["id"]: stage for stage in finished["stages"]}
        for stage_id in SANDBOX_COLD_INSTALL_STAGES:
            assert stages[stage_id]["status"] == "succeeded", stages[stage_id]
        assert finished["quality_gate"]["passed"] is True
        assert all(check["status"] == "passed" for check in finished["smoke_tests"])
    finally:
        if enabled:
            restored = _request(
                "/api/initialization/test-mode",
                "DELETE",
                {"confirmation": "DISABLE_INITIALIZATION_TEST_MODE"},
            )["initialization"]
            assert restored["mode"] == "normal"
            assert restored["run_id"] == normal_before["run_id"]
            assert restored["state"] == normal_before["state"]
