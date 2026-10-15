#!/usr/bin/env python3
"""Read-only synthetic acceptance against the real local creation endpoint.

No existing sessions, personal memories or external sources are used. A model
grade is recorded separately from deterministic transport/body invariants.
"""
import argparse
import hashlib
import json
import time
import urllib.request
from pathlib import Path


GOOD = """# 素材交接试点建议

当前提供的事实：素材需求通过群消息交接，需求内容和完成状态散落在消息中。未提供实际等待时长、人员容量或效果数据。

建议先试行一份共享需求清单，集中记录交付内容、验收要求和当前状态。选择清单而暂缓系统开发，是为了先确认等待是否来自信息缺失，避免在原因未明确时投入开发。

试点开始前，由相关团队确认愿意参与的范围、维护清单的角色和记录权限；这些条件尚未确认，不承诺日期或覆盖规模。参与者发起需求时补齐验收要求，接收方据此确认下一步，状态变化时更新清单。

拟记录交接中补问的内容和等待原因，并与当前消息交接方式比较。如果关键验收要求仍不明确，先调整需求字段；如果记录负担高且没有减少补问，暂停扩大试点并重新判断原因。比较口径和记录方式需要双方在启动前确认。

本次评审请决定是否同意验证这条路径，并明确谁来确认试点范围与记录条件；当前没有收益预测或已验证效果。
"""
BAD = "# 素材交接方案\n\n通过多维能力协同持续赋能增长，构建高效闭环。后续分阶段推进，持续优化并形成长效机制。"
CONTEXT = {"schema_version": "creation.work-context.v1", "profile_id": "proposal_review",
    "purpose": "内部方案评审", "decision_goal": "是否同意开展素材交接试点",
    "reader_context": "参与素材交接的产品与运营团队", "constraints": "未确认日期、投入、负责人及效果，不能写成承诺",
    "style_samples": ["旧项目收入999万元。表达参考：先给判断，再交代依据和待决定的事。"], "review_enabled": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:17071/api")
    parser.add_argument("--report", required=True)
    parser.add_argument("--actions", default="bad,good,trial,summary")
    args = parser.parse_args()
    path = Path(args.report).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    report = {"kind": "creation-work-quality-synthetic-evaluation", "started_at": time.time(),
              "source": GOOD, "work_context": CONTEXT, "cases": [], "status": "running"}
    for name in args.actions.split(","):
        action = "review" if name in {"bad", "good"} else name
        document = BAD if name == "bad" else GOOD
        original = document
        payload = {"action": action, "instruction": "基于给定事实形成素材交接试点的评审内容，不检索历史资料或联网。",
                   "current_document": document, "work_context": CONTEXT}
        started = time.monotonic()
        request = urllib.request.Request(args.base_url.rstrip("/") + "/creation/work-quality/preview",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=310) as response:
                result = json.load(response)
            assert result["read_only"] is True
            assert result["document_hash"] == hashlib.sha256(original.encode("utf-8")).hexdigest()
            assert "999" not in result.get("content", "")
            assert document == original
            entry = {"case": name, "latency_seconds": round(time.monotonic() - started, 2),
                     "invariants": "pass", "result": result}
        except Exception as error:
            entry = {"case": name, "latency_seconds": round(time.monotonic() - started, 2), "failure": type(error).__name__ + ": " + str(error)}
        report["cases"].append(entry)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(name + ": " + entry.get("result", {}).get("status", entry.get("failure", "unknown")), flush=True)
    report["status"] = "completed" if all("failure" not in item for item in report["cases"]) else "failed"
    report["completed_at"] = time.time()
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
