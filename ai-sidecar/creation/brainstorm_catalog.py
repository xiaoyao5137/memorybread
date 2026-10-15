"""Compile installed, hash-bound decision catalogs without model text or recall."""

import base64
import copy
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .brainstorm import BrainstormGenerationError

CATALOG_PATH = "references/brainstorm-catalog.json"
PROFILE_PATH = "references/memorybread-creation.json"
MAX_PACKAGE_BYTES = 4 * 1024 * 1024
MAX_CATALOG_BYTES = 256 * 1024
GOAL_PREFIX = "catalog_slot:"


def _fail():
    raise BrainstormGenerationError("可信脑暴目录无效、已变更或与当前技能绑定不一致")


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError, UnicodeError):
        _fail()


def _keys(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        _fail()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        _fail()
    return value


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        _fail()
    return value


def _ids(value, minimum=0, maximum=32):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        _fail()
    result = [_id(item) for item in value]
    if len(set(result)) != len(result):
        _fail()
    return result


def _semantic(value):
    """UI camelCase and persisted snake_case represent the same profile fields."""
    aliases = {"documentTypes": "document_types", "metadataReview": "metadata_review",
        "outputRole": "output_role", "retainWebpageScreenshot": "retain_webpage_screenshot"}
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            canonical = aliases.get(key, key)
            if canonical in result:
                _fail()
            result[canonical] = _semantic(item)
        result.pop("metadata_review", None)
        if {"id", "objective", "agents"}.issubset(result):
            result.setdefault("output_role", "")
        return result
    return value


def load_installed_catalog(db_path: str, binding: Dict[str, Any],
                           selected_skills: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A caller blob is never authority; compare it to the local installed package."""
    _keys(binding, ("skill_key", "profile_sha256", "catalog_sha256", "catalog"))
    key = _id(binding["skill_key"])
    for field in ("profile_sha256", "catalog_sha256"):
        if not isinstance(binding[field], str) or not re.fullmatch(r"[a-f0-9]{64}", binding[field]):
            _fail()
    selected = [item for item in selected_skills if isinstance(item, dict)
        and (item.get("id") or item.get("clientSkillKey")) == key]
    if len(selected) != 1 or not db_path:
        _fail()
    try:
        path = Path(db_path).resolve(strict=True)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            size = connection.execute("SELECT length(package_files),length(skill_description),length(execution_steps) FROM creation_skills "
                "WHERE client_skill_key=? AND installed=1 AND status='saved' AND deleted_at IS NULL", (key,)).fetchone()
            if size is None or any(type(length) is not int or not 0 < length <= MAX_PACKAGE_BYTES for length in size):
                _fail()
            row = connection.execute("SELECT package_files,skill_description,execution_steps FROM creation_skills "
                "WHERE client_skill_key=? AND installed=1 AND status='saved' AND deleted_at IS NULL", (key,)).fetchone()
        files, description, steps = _json(row[0]), _json(row[1]), _json(row[2])
        if not isinstance(files, list):
            _fail()
        decoded = {}
        for name in (PROFILE_PATH, CATALOG_PATH):
            matches = [item for item in files if isinstance(item, dict) and item.get("path") == name]
            if len(matches) != 1:
                _fail()
            entry = matches[0]
            content = base64.b64decode(entry.get("content_base64", ""), validate=True)
            if (type(entry.get("size_bytes")) is not int or len(content) != entry["size_bytes"]
                    or not 0 < len(content) <= MAX_CATALOG_BYTES):
                _fail()
            decoded[name] = content
        if (hashlib.sha256(decoded[PROFILE_PATH]).hexdigest() != binding["profile_sha256"]
                or hashlib.sha256(decoded[CATALOG_PATH]).hexdigest() != binding["catalog_sha256"]):
            _fail()
        profile, catalog = _json(decoded[PROFILE_PATH]), _json(decoded[CATALOG_PATH])
        if (not isinstance(profile, dict) or type(profile.get("schema_version")) is not int or profile.get("schema_version") != 1
                or profile.get("kind") != "memorybread.creation-profile" or not isinstance(profile.get("content"), dict)):
            _fail()
        for profile_key, current, selected_key in (("skill_description", description, "skillDescription"),
                                                   ("execution_steps", steps, "executionSteps")):
            if (_semantic(profile["content"].get(profile_key)) != _semantic(current)
                    or _semantic(selected[0].get(selected_key, selected[0].get(profile_key))) != _semantic(current)):
                _fail()
        if (catalog != binding["catalog"] or catalog.get("skill_key") != key
                or catalog.get("skill_profile_sha256") != binding["profile_sha256"]):
            _fail()
        return catalog
    except BrainstormGenerationError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
        _fail()


class BrainstormCatalog:
    """A fully checked graph; method text is package configuration, never model output."""

    def __init__(self, value: Dict[str, Any], limits: Dict[str, int]):
        _keys(value, ("schema_version", "kind", "catalog_id", "catalog_version", "skill_key", "skill_profile_sha256", "stages"))
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["kind"] != "memorybread.brainstorm-catalog"
                or type(value["catalog_version"]) is not int or value["catalog_version"] < 1):
            _fail()
        for field in ("catalog_id", "skill_key"):
            _id(value[field])
        if not isinstance(value["skill_profile_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["skill_profile_sha256"]):
            _fail()
        stages = value["stages"]
        if not isinstance(stages, dict) or not stages or set(stages) - {"direction", "detail", "writing"}:
            _fail()
        self.stages = copy.deepcopy(stages)
        self.paths = {}
        self.limits = limits
        for name, stage in self.stages.items():
            self._validate_stage(name, stage)

    def _validate_stage(self, name, stage):
        _keys(stage, ("topics", "slots"))
        topics, slots = stage["topics"], stage["slots"]
        if (not isinstance(topics, list) or not 1 <= len(topics) <= self.limits["max_topics"]
                or not isinstance(slots, list) or not 1 <= len(slots) <= 144):
            _fail()
        by_topic, by_slot = {}, {}
        for topic in topics:
            _keys(topic, ("id", "title", "goal", "root_slot_ids"))
            key = _id(topic["id"])
            _text(topic["title"], 60)
            _text(topic["goal"], 200)
            _ids(topic["root_slot_ids"], 1, 8)
            if key in by_topic:
                _fail()
            by_topic[key] = topic
        for slot in slots:
            _keys(slot, ("id", "topic_id", "object_id", "choice_axis", "required_fields", "prompt_body", "why_now", "methods"))
            key = _id(slot["id"])
            if key in by_slot or _id(slot["topic_id"]) not in by_topic:
                _fail()
            _id(slot["object_id"])
            _text(slot["choice_axis"], 80)
            _ids(slot["required_fields"], 1)
            _text(slot["prompt_body"], 60)
            _text(slot["why_now"], 80)
            methods = slot["methods"]
            if not isinstance(methods, list) or not 2 <= len(methods) <= 5:
                _fail()
            seen = set()
            for method in methods:
                _keys(method, ("id", "object_id", "required_fields", "label", "description", "tradeoff", "child_slot_ids"))
                method_id = _id(method["id"])
                if (method_id in seen or method["object_id"] != slot["object_id"]
                        or method["required_fields"] != slot["required_fields"]):
                    _fail()
                seen.add(method_id)
                _text(method["label"], 24)
                _text(method["description"], 80)
                _text(method["tradeoff"], 160)
                _ids(method["child_slot_ids"], 0, 8)
            if any(method["child_slot_ids"] for method in methods) and not all(method["child_slot_ids"] for method in methods):
                _fail()
            by_slot[key] = slot
        paths = {}
        def visit(key, topic_id, ancestors):
            if key in paths or key not in by_slot or len(ancestors) >= self.limits["max_depth"]:
                _fail()
            slot = by_slot[key]
            if slot["topic_id"] != topic_id:
                _fail()
            paths[key] = ancestors
            for index, method in enumerate(slot["methods"]):
                for child in method["child_slot_ids"]:
                    visit(child, topic_id, ancestors + [(key, index)])
        for topic in topics:
            for root in topic["root_slot_ids"]:
                visit(root, topic["id"], [])
            count = sum(by_slot[key]["topic_id"] == topic["id"] for key in paths)
            if count > self.limits["max_questions_per_topic"]:
                _fail()
        if set(paths) != set(by_slot):
            _fail()
        self.paths[name] = paths
        stage["by_slot"] = by_slot

    def configured(self, stage):
        return stage in self.stages

    def topics(self, stage):
        return [{"id": item["id"], "title": item["title"], "goal": item["goal"],
                 **({"chapter_id": item["id"]} if stage == "writing" else {})}
                for item in self.stages[stage]["topics"]]

    def selected_topic(self, stage, value):
        matches = [item for item in self.topics(stage) if item == value]
        if len(matches) != 1:
            _fail()
        return matches[0]

    def roots(self, stage, topic):
        return [GOAL_PREFIX + key for item in self.stages[stage]["topics"]
                if item["id"] == topic["id"] for key in item["root_slot_ids"]]

    def slot(self, stage, node, topic):
        goal = node["goal"]
        key = goal[len(GOAL_PREFIX):] if goal.startswith(GOAL_PREFIX) else ""
        slot = self.stages[stage]["by_slot"].get(key)
        if not slot or slot["topic_id"] != topic["id"]:
            _fail()
        path = self.paths[stage][key]
        if node["depth"] != len(path) + 1 or len(node["ancestor_conditions"]) != len(path):
            _fail()
        prefix_labels = []
        for (parent, ordinal), condition in zip(path, node["ancestor_conditions"]):
            parent_slot = self.stages[stage]["by_slot"][parent]
            method = parent_slot["methods"][ordinal]
            prefix = "若" + "且".join("「" + label + "」" for label in prefix_labels) + "，" if prefix_labels else ""
            if (condition["parent_option_id"] != chr(ord("a") + ordinal)
                    or condition["option_label"] != method["label"]
                    or condition["option_description"] != method["description"]
                    or condition["parent_prompt"] != prefix + parent_slot["prompt_body"]
                    or "parent_goal" in condition and condition["parent_goal"] != GOAL_PREFIX + parent):
                _fail()
            prefix_labels.append(method["label"])
        if len(slot["methods"]) > node["max_options"]:
            _fail()
        count = sum(len(method["child_slot_ids"]) for method in slot["methods"])
        if count > node["max_next_goals"] or count and node["depth"] >= self.limits["max_depth"]:
            _fail()
        return slot

    @staticmethod
    def candidate(slot):
        return {"question": {"type": "single_choice", "single_choice_reason": "各方法服务同一对象，请选择一种实施方式",
            "prompt_body": slot["prompt_body"], "why_now": slot["why_now"],
            "answer_template": "补充你的具体实施方法。", "options": [
                {"label": method["label"], "description": method["description"],
                 "recommended": index == 0, "memory_choice": "original"}
                for index, method in enumerate(slot["methods"])]},
            "next_goals": [{"option_index": index + 1, "goal": GOAL_PREFIX + child}
                for index, method in enumerate(slot["methods"]) for child in method["child_slot_ids"]]}
