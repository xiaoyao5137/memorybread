"""章节级代码图示计划的解析与校验。

Visual Plan 只描述“哪一章的哪些关系适合用什么代码图示表达”，不包含行业、
产品或方案类型的固定模板。模型负责语义判断，Harness 只做结构校验、预算收敛
和安全降级。
"""

import json
import re
from typing import Any, Dict, List, Tuple


VISUAL_PLAN_SCHEMA_VERSION = "creation.visual-plan.v1"
MAX_VISUAL_PLAN_DIAGRAMS = 8
DEFAULT_VISUAL_PLAN_DIAGRAMS = 4
ALLOWED_VISUAL_POLICIES = {"auto", "required", "off"}
ALLOWED_MERMAID_DIAGRAM_TYPES = {
    "flowchart",
    "flowchart_lr",
    "sequence",
    "state",
    "class",
    "er",
    "journey",
    "gantt",
    "mindmap",
}
ALLOWED_DIAGRAM_PLACEMENTS = {
    "after_intro",
    "before_details",
    "after_details",
}


def empty_visual_plan(policy: str = "auto") -> Dict[str, Any]:
    normalized_policy = policy if policy in ALLOWED_VISUAL_POLICIES else "auto"
    return {
        "schema_version": VISUAL_PLAN_SCHEMA_VERSION,
        "policy": normalized_policy,
        "max_diagrams": DEFAULT_VISUAL_PLAN_DIAGRAMS,
        "diagrams": [],
    }


def _bounded_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _stable_diagram_id(value: Any, section_title: str, index: int) -> str:
    candidate = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "")).strip("-")
    if candidate:
        return candidate[:80]
    section_slug = re.sub(r"\s+", "-", section_title).strip("-")
    return (section_slug or "section-diagram")[:64] + "-" + str(index + 1)


def normalize_visual_plan(raw: Any) -> Dict[str, Any]:
    """把模型输出收敛为稳定、有限且可序列化的章节图示计划。"""
    if not isinstance(raw, dict):
        return empty_visual_plan()

    policy = str(raw.get("policy") or "auto").strip().lower()
    if policy not in ALLOWED_VISUAL_POLICIES:
        policy = "auto"
    try:
        requested_max = int(raw.get("max_diagrams", DEFAULT_VISUAL_PLAN_DIAGRAMS))
    except (TypeError, ValueError):
        requested_max = DEFAULT_VISUAL_PLAN_DIAGRAMS
    max_diagrams = max(0, min(requested_max, MAX_VISUAL_PLAN_DIAGRAMS))
    if policy == "off":
        max_diagrams = 0

    diagrams: List[Dict[str, Any]] = []
    seen_ids = set()
    seen_sections = set()
    candidates = raw.get("diagrams")
    if not isinstance(candidates, list):
        candidates = []
    for index, item in enumerate(candidates):
        if len(diagrams) >= max_diagrams or not isinstance(item, dict):
            break
        section_title = _bounded_text(item.get("section_title"), 120)
        reason = _bounded_text(item.get("reason"), 240)
        diagram_type = str(item.get("diagram_type") or "").strip().lower()
        if not section_title or not reason or diagram_type not in ALLOWED_MERMAID_DIAGRAM_TYPES:
            continue
        section_key = re.sub(r"\W+", "", section_title).lower()
        if not section_key or section_key in seen_sections:
            continue
        diagram_id = _stable_diagram_id(item.get("id"), section_title, index)
        if diagram_id in seen_ids:
            diagram_id = _stable_diagram_id("", section_title, index)
        raw_points = item.get("source_points")
        source_points = []
        if isinstance(raw_points, list):
            for point in raw_points:
                normalized = _bounded_text(point, 160)
                if normalized and normalized not in source_points:
                    source_points.append(normalized)
                if len(source_points) >= 16:
                    break
        # 没有任何可回溯内容时不调度画图，避免只凭标题生成装饰图。
        if not source_points:
            continue
        placement = str(item.get("placement") or "after_intro").strip().lower()
        if placement not in ALLOWED_DIAGRAM_PLACEMENTS:
            placement = "after_intro"
        try:
            max_nodes = int(item.get("max_nodes", 12))
        except (TypeError, ValueError):
            max_nodes = 12
        diagrams.append(
            {
                "id": diagram_id,
                "section_title": section_title,
                "purpose": _bounded_text(item.get("purpose"), 240),
                "diagram_type": diagram_type,
                "required": bool(item.get("required", False)),
                "reason": reason,
                "source_points": source_points,
                "placement": placement,
                "max_nodes": max(2, min(max_nodes, 24)),
            }
        )
        seen_ids.add(diagram_id)
        seen_sections.add(section_key)

    return {
        "schema_version": VISUAL_PLAN_SCHEMA_VERSION,
        "policy": policy,
        "max_diagrams": max_diagrams,
        "diagrams": diagrams,
    }


def _json_candidates(text: str) -> List[str]:
    candidates = [
        match.group(1).strip()
        for match in re.finditer(
            r"```(?:json)?\s*([\s\S]*?)```",
            text or "",
            re.IGNORECASE,
        )
    ]
    stripped = (text or "").strip()
    if stripped:
        candidates.append(stripped)
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            candidates.append(stripped[start : end + 1])
    return candidates


def parse_chapter_design_result(text: str) -> Tuple[str, Dict[str, Any]]:
    """解析章节设计 Agent 输出；旧版自由文本会无损降级且不自动配图。"""
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        raw_plan = payload.get("visual_plan")
        if not isinstance(raw_plan, dict):
            continue
        blueprint = str(payload.get("blueprint_markdown") or "").strip()
        if not blueprint:
            blueprint = str(payload.get("chapter_blueprint") or "").strip()
        return blueprint or (text or "").strip(), normalize_visual_plan(raw_plan)
    return (text or "").strip(), empty_visual_plan()
