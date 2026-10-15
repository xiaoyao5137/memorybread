"""Confirmation status belongs to actual user state, never generated copy.

This is a language/provenance contract for newly generated free candidates,
not a factual classifier for arbitrary user or historical text. A candidate
may describe a user-provided fact directly, but cannot narrate completed user
confirmation. Actual answers and program-bound UI state own that status.
"""
import re
from typing import Any, Dict, List


COPY_FIELDS = ("why_now", "single_choice_reason", "answer_template")

# Completed-confirmation language only; no topic/actor/business terms. These
# forms are forbidden in model-owned candidate copy even when quoted, negated
# or conditional. The model can state facts or ask the remaining question
# without narrating confirmation state. Authority and source excerpts are
# never traversed or modified by this guard.
_CONFIRMATION_MARKER = re.compile(
    r"(?:已(?:经|被|获|经被)?\s*(?:确认|锁定|确定|决定|选定|选择|敲定|明确)"
    r"|(?:此前|先前|之前)(?:已|已经)?\s*(?:确认|锁定|确定|决定|选定|选择|敲定)"
    r"|(?:确认|锁定|确定|决定|选定|选择)\s*(?:完毕|完成|过)"
    r"|\balready\s+(?:been\s+)?(?:confirmed|chosen|selected|decided|fixed|locked)\b"
    r"|\b(?:has|have)\s+(?:been\s+)?(?:confirmed|chosen|selected|decided)\b)", re.IGNORECASE)


def has_confirmation_marker(text: Any) -> bool:
    return isinstance(text, str) and _CONFIRMATION_MARKER.search(text) is not None


def confirmation_status_violations(candidate: Dict[str, Any]) -> List[Dict[str, str]]:
    """Locate forbidden status narration only in actual generated fields."""
    violations = []

    def add(identifier: str, path: str, text: Any) -> None:
        if has_confirmation_marker(text):
            violations.append({"unit_id": identifier, "field": path})

    if "review_questions" in candidate:
        records = candidate["review_questions"]
    elif "question" in candidate:
        records = [{"question": candidate["question"], "candidate_copy": candidate.get("candidate_copy", {})}]
    else:
        records = []
    for qi, record in enumerate(records):
        question = record["question"]
        annotations = record.get("candidate_copy", question)
        add("q{}_prompt".format(qi), "questions[{}].prompt".format(qi), question["prompt"])
        for field in COPY_FIELDS:
            add("q{}_{}".format(qi, field), "questions[{}].{}".format(qi, field), annotations.get(field))
        for oi, option in enumerate(question["options"]):
            for field in ("label", "description", "tradeoff"):
                add("q{}_o{}".format(qi, oi), "questions[{}].options[{}].{}".format(qi, oi, field), option.get(field))
    for gi, group in enumerate(candidate.get("next_goals", [])):
        for index, goal in enumerate(group["goals"]):
            add("g{}_{}".format(gi, index), "next_goals[{}].goals[{}]".format(gi, index), goal)
    for index, goal in enumerate(candidate.get("root_goals", [])):
        add("root{}".format(index), "root_goals[{}]".format(index), goal)
    for index, record in enumerate(candidate.get("planned_topics", [])):
        for field in ("title", "goal"):
            add("topic{}".format(index), "planned_topics[{}].topic.{}".format(index, field), record["topic"][field])
        chapter = record.get("chapter")
        if chapter:
            for field in ("title", "goal"):
                add("chapter{}".format(index), "planned_topics[{}].chapter.{}".format(index, field), chapter[field])
    for index, text in enumerate(candidate.get("pending_flags", [])):
        add("flag{}".format(index), "pending_flags[{}]".format(index), text)
    return violations
