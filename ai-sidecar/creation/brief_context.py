"""Authoritative Brainstorm choices shared by writing and delivery checks."""

import hashlib
import json
from typing import Any, Dict, List


# Core's skip-answer receipt is an unanswered placeholder, not a substantive
# proposed value. Unknown or richer assumptions must retain their provenance.
_SKIPPED_ANSWER = "暂未确定，生成时由创作 Agent 补充并保留为待核验假设"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _entry(question_id: str, dimension: str, key: str, value: str,
           source: str, description: str = "") -> Dict[str, str]:
    identity = json.dumps([question_id, key], ensure_ascii=False).encode("utf-8")
    return {
        "id": "brainstorm-" + hashlib.sha256(identity).hexdigest()[:20],
        "question_id": question_id,
        "dimension": dimension,
        "value": value,
        "description": description,
        "source": source,
    }


def _superseded_placeholders(decisions: List[Dict[str, Any]], edits: Dict[str, Any],
                             history: Dict[str, Any]) -> Dict[str, str]:
    def scope(item):
        dimension_id = _text(item.get("dimension_id"))
        question = history.get(_text(item.get("question_id")), {}).get("question", {})
        parent = item.get("parent_question_id", question.get("parent_question_id"))
        stage = item.get("exploration_stage", question.get("exploration_stage", "explore"))
        # Missing structural provenance is not evidence that two equally named
        # dimensions address the same question. Child branches remain separate.
        if not dimension_id or not isinstance(parent, str) or not isinstance(stage, str):
            return None
        return dimension_id, parent, stage

    superseded = {}
    pending = {}
    for item in decisions:
        question_id = _text(item.get("question_id"))
        identity = scope(item)
        if not question_id or identity is None:
            continue
        source = _text(item.get("source", item.get("answer_source", "user")))
        value = _text(item.get("summary", item.get("answer")))
        if question_id in edits and source != "user_excluded":
            source, value = "user", _text(edits[question_id])
        if source == "agent_assumption" and value == _SKIPPED_ANSWER:
            pending.setdefault(identity, []).append(question_id)
        elif source == "user" and value:
            # A stable dimension can be revisited from another selected option
            # of the same parent; parent_option_id does not redefine its scope.
            for previous_id in pending.pop(identity, []):
                superseded[previous_id] = question_id
    return superseded


def effective_brief_decisions(creation_brief: Any) -> List[Dict[str, str]]:
    """Expand every current decision without dropping early or lengthy choices.

    The Core response's decisions list defines the current scope. History only
    enriches those decisions through their selected option IDs; unselected
    options, question details and current unanswered questions are never mined.
    Only entries whose source is ``user`` are confirmed coverage obligations.
    Edits, exclusions, cleared values and assumptions keep their own provenance.
    """
    if not isinstance(creation_brief, dict):
        return []
    raw_decisions = creation_brief.get("decisions")
    if not isinstance(raw_decisions, list):
        return []
    decisions = [item for item in raw_decisions if isinstance(item, dict)]
    edits = creation_brief.get("brief_edits")
    edits = edits if isinstance(edits, dict) else {}
    invalidated = creation_brief.get("invalidated_question_ids")
    invalidated = set(value for value in invalidated if isinstance(value, str)) if isinstance(invalidated, list) else set()
    decisions = [item for item in decisions if _text(item.get("question_id")) not in invalidated]

    from .brainstorm import BrainstormCoordinator

    memory_allowed = BrainstormCoordinator._memory_allowed(
        _text(creation_brief.get("root_request")), decisions, edits,
        creation_brief.get("user_input_revisions"),
    )
    original_decisions = decisions
    if not memory_allowed:
        decisions, _ = BrainstormCoordinator._user_only_context(
            _text(creation_brief.get("root_request")), decisions, edits,
        )

    history = {}
    if memory_allowed and isinstance(creation_brief.get("history"), list):
        for turn in creation_brief["history"]:
            if not isinstance(turn, dict) or not isinstance(turn.get("question"), dict):
                continue
            question_id = _text(turn["question"].get("id"))
            if question_id:
                history[question_id] = turn
    superseded = _superseded_placeholders(decisions, edits, history)
    originals = {_text(item.get("question_id")): item for item in original_decisions}
    result = []
    for index, decision in enumerate(decisions):
        question_id = _text(decision.get("question_id"))
        # Legacy snapshots without IDs still receive deterministic local IDs.
        identity = question_id or "legacy-decision-{}".format(index)
        source = _text(decision.get("source", decision.get("answer_source", "user")))
        dimension = _text(decision.get("dimension")) or _text(decision.get("dimension_id"))
        value = _text(decision.get("summary", decision.get("answer")))
        if not memory_allowed:
            dimension = (_text(decision.get("excluded_topic"))
                         or _text(decision.get("cleared_topic")) or dimension)
        if source == "user_excluded":
            result.append(_entry(identity, dimension, "excluded", value, source))
            continue
        if question_id in edits:
            value = _text(edits[question_id])
            result.append(_entry(identity, dimension, "edited", value,
                                 "user" if value else "user_cleared"))
            continue
        if source != "user":
            if value:
                entry = _entry(identity, dimension, "summary", value, source)
                if question_id in superseded:
                    entry.update(source="superseded_assumption", superseded_by=superseded[question_id])
                result.append(entry)
            continue
        if decision.get("manually_edited"):
            result.append(_entry(identity, dimension, "edited", value,
                                 "user" if value else "user_cleared"))
            continue

        turn = history.get(question_id, {})
        question = turn.get("question", {})
        answer = turn.get("answer", {})
        answer = answer if isinstance(answer, dict) else {}
        options = question.get("options", [])
        options = {item["id"]: item for item in options if isinstance(item, dict)
                   and isinstance(item.get("id"), str)} if isinstance(options, list) else {}
        selected_ids = answer.get("selected_option_ids", [])
        selected_ids = selected_ids if isinstance(selected_ids, list) else []
        input_decision = decision if memory_allowed else originals.get(question_id, {})
        user_inputs = input_decision.get("user_inputs", [])
        user_inputs = user_inputs if isinstance(user_inputs, list) else []
        input_values = {_text(item) for item in user_inputs if _text(item)}
        if (not input_values and value and any(
                not isinstance(option_id, str) or not _text(options.get(option_id, {}).get("label"))
                for option_id in selected_ids)):
            # An incomplete legacy history must not cause a partially resolved
            # question to silently discard its other saved choices.
            result.append(_entry(identity, dimension, "summary", value, source))
            continue
        values = set()
        option_ids = set()
        for option_id in selected_ids:
            if not isinstance(option_id, str) or option_id in option_ids:
                continue
            option_ids.add(option_id)
            option = options.get(option_id, {})
            label = _text(option.get("label"))
            if label and (not input_values or label in input_values):
                result.append(_entry(identity, dimension, "option:" + option_id,
                                     label, source, _text(option.get("description"))))
                values.add(label)
        custom = _text(answer.get("custom_text"))
        if custom and custom not in values and (not input_values or custom in input_values):
            result.append(_entry(identity, dimension, "custom", custom, source))
            values.add(custom)

        for input_index, raw_input in enumerate(user_inputs):
            user_input = _text(raw_input)
            if not user_input or user_input in values:
                continue
            if not memory_allowed and BrainstormCoordinator._source_directive(user_input) == 1:
                continue
            result.append(_entry(identity, dimension, "input:{}".format(input_index), user_input, source))
            values.add(user_input)
        if not values and value:
            result.append(_entry(identity, dimension, "summary", value, source))
    return result
