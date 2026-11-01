"""Use corpus evidence as a retrieval lead, never as a sourced fact."""
import copy
import re


def local_entity_leads(requirement: dict, query: str) -> list[str]:
    context = requirement.get("entity_context") or {}
    if not context.get("has_high_confidence_entity"):
        return []
    return list(dict.fromkeys(
        str(row.get("name") or "").strip()
        for row in context.get("primary_entities", [])
        if str(row.get("name") or "").strip()
        and str(row["name"]).strip().casefold() in query.casefold()
        and (row.get("mention_count", 0) > 0 or row.get("entity_exact_count", 0) > 0)
    ))


def bind_local_evidence_leads(contract: dict, requirement: dict, instruction: str,
                              *, allowed: bool, operation: str) -> dict:
    # Only supplement an actual factual gap. Pure edits, supplied material and
    # explicit workflows keep their scope. A public subject can have local notes.
    if not allowed or operation not in {"generate", "answer"}:
        return contract
    # Do not widen an explicitly restricted source scope while repairing a
    # model's missing dependency. This guard supplements the UI permission.
    if re.search(r"(?:不要|禁止|不允许|不使用|不用|别|无需).{0,12}(?:本地|记忆|历史)|"
                 r"(?:仅|只)(?:允许)?(?:使用|用|基于|依据|参考).{0,12}(?:官网|官方|公开|联网|网页|提供|给定)|"
                 r"\b(?:no|without|do not use)\s+(?:local\s+)?(?:memory|history)\b",
                 instruction, re.I):
        return contract
    inputs = contract.get("inputs", [])
    if any(row["source"] == "work_context" for row in inputs):
        return contract
    gaps = [row for row in inputs if row["source"] == "public_facts" and row["state"] == "missing"]
    leads = local_entity_leads(requirement, instruction)
    if not gaps or not leads:
        return contract
    result = copy.deepcopy(contract)
    used = {row["id"] for row in inputs}
    key = "local_evidence_leads"
    while key in used:
        key += "_"
    result["inputs"].insert(0, {
        "id": key, "source": "work_context", "state": "missing", "evidence": "",
        "need": "核对本地已有的相关材料是否覆盖本轮事实缺口",
        "query": instruction,
        "reason": "本地语料已命中请求中的实体：" + "、".join(leads)
                  + "；公开主题不排除已有本地材料，命中线索本身不是事实证据。",
    })
    return result


def web_result_matches(result, anchors: list[str]) -> bool:
    if not anchors:
        return True
    # Titles/snippets must identify the subject. URLs alone can contain search
    # parameters without providing any evidence about that subject.
    text = (result.title + " " + result.snippet).casefold()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(anchor.casefold()) + r"(?![a-z0-9])", text)
               if re.fullmatch(r"[a-zA-Z0-9 ._-]+", anchor)
               else anchor.casefold() in text for anchor in anchors)
