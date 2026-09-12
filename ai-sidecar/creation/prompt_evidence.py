"""Shared bounded source views for document authors and delivery reviewers."""
from typing import Any, Optional

MAX_PROMPT_DATA_RESULTS_CHARS = 22000
MAX_PROMPT_REFERENCE_CHARS = 16000


class CreationEvidencePrompts:
    data_char_budget = MAX_PROMPT_DATA_RESULTS_CHARS
    reference_char_budget = MAX_PROMPT_REFERENCE_CHARS

    @classmethod
    def _compact_prompt_value(cls, value: Any, depth: int = 0) -> Any:
        """把运行时完整对象转为有界的模型事实视图。

        完整 DOM、截图区域、滚动与交互调试状态保留在环境/数据库中，
        但不属于 Agent 需要消费的事实。这里只按通用数据形态裁剪，
        不感知看板名、业务字段或具体指标。
        """
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            text = " ".join(value.split()).strip()
            return text if len(text) <= 600 else text[:600].rstrip() + "..."
        if isinstance(value, dict):
            if depth >= 4:
                return "[nested object omitted]"
            omitted_keys = {
                "dom_content_text",
                "raw_html",
                "html",
                "evidence_regions",
                "scroll_capture",
                "interaction",
                "page_state",
                "browser_script",
                "screenshot",
            }
            compacted: dict[str, Any] = {}
            for key, item in list(value.items())[:32]:
                key_text = str(key)
                if key_text.lower() in omitted_keys:
                    continue
                compacted[key_text] = cls._compact_prompt_value(item, depth + 1)
            return compacted
        if isinstance(value, (list, tuple)):
            if depth >= 4:
                return ["[nested items omitted]"]
            return [
                cls._compact_prompt_value(item, depth + 1)
                for item in list(value)[:24]
            ]
        return cls._compact_prompt_value(str(value), depth + 1)

    @classmethod
    def _prompt_data_results(cls, results: Any) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        used_chars = 2
        indexed_results = [
            (index, raw)
            for index, raw in enumerate(list(results or []))
            if isinstance(raw, dict)
        ]

        def result_rank(entry: tuple[int, dict[str, Any]]) -> tuple[int, int]:
            index, raw = entry
            evidence = raw.get("creation_evidence")
            verified_report = (
                raw.get("source_kind") == "report_url"
                and raw.get("can_use") is True
                and isinstance(evidence, dict)
                and evidence.get("validation_status") == "verified"
            )
            # 全文整合会同时消费多个数据步骤。若继续沿用第一次检索的
            # 原始顺序，前一张报表及其工作记忆会占满 Prompt，后一张已经
            # 验证成功的报表只能被模型写成“待补充”。
            return (0 if verified_report else 1, index)

        for _, raw in sorted(indexed_results, key=result_rank)[:30]:
            item = {
                key: raw.get(key)
                for key in (
                    "source_id",
                    "title",
                    "source_kind",
                    "source_url",
                    "observed_at",
                    "collected_at",
                    "freshness_class",
                    "freshness_score",
                    "refresh_required",
                    "can_use",
                    "evidence_status",
                    "evidence_reason",
                    "unavailable_reason",
                    "data_usage_status",
                    "risk_disclosure_required",
                )
                if raw.get(key) is not None
            }
            # 不可用的报表只向 Agent 暴露来源身份与动作状态。
            # 可用结果才带有界的事实、来源和少量历史阶段。
            if raw.get("can_use") is True:
                excerpt = str(raw.get("content_excerpt") or "").strip()
                if excerpt:
                    item["content_excerpt"] = cls._compact_prompt_value(excerpt)
                if raw.get("structured_data") is not None:
                    structured_data = raw.get("structured_data")
                    compact_structured = cls._compact_prompt_value(structured_data)
                    if (
                        raw.get("source_kind") == "report_url"
                        and isinstance(structured_data, dict)
                        and isinstance(compact_structured, dict)
                        and isinstance(structured_data.get("verified_claims"), list)
                    ):
                        # 为每个实时来源保留公平预算。KPI 已由采集层排序，
                        # 定向字段匹配时保留完整请求集；只有概念偏好而没有
                        # 字段命中时只暴露前四个汇总 KPI，避免 Writer 把项目
                        # 明细二次推导成未经页面支持的新指标。
                        validation_reason = str(
                            structured_data.get("validation") or ""
                        )
                        claim_limit = (
                            12
                            if validation_reason
                            in {"requested_metrics_verified", "requested_metrics_partial", "requested_metrics_qualified"}
                            else 4
                        )
                        compact_structured["verified_claims"] = [
                            cls._compact_prompt_value(claim)
                            for claim in structured_data["verified_claims"][:claim_limit]
                            if isinstance(claim, dict)
                        ]
                    item["structured_data"] = compact_structured
                if raw.get("provenance") is not None:
                    item["provenance"] = cls._compact_prompt_value(
                        raw.get("provenance")
                    )
                history = raw.get("history")
                if isinstance(history, list) and history:
                    item["history"] = cls._compact_prompt_value(history[:3])
            candidate_size = len(str(item))
            if compacted and used_chars + candidate_size > cls.data_char_budget:
                break
            compacted.append(item)
            used_chars += candidate_size
        return compacted

    @staticmethod
    def _reference_identity(raw: dict[str, Any]) -> str:
        source_id = raw.get("source_id")
        if source_id is None:
            source_id = raw.get("id")
        return f"{raw.get('source_type') or 'document'}:{source_id}"

    @classmethod
    def _scope_references_for_step(
        cls,
        references: Any,
        step: Optional[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set]:
        """当前 Skill 步骤自己召回的参考必须排在写作 Prompt 最前面。

        合并列表按首次出现顺序保留，上一步的结果会占据前位；若不按
        matched_skill_steps 重排，本步召回的证据会被预算截断在 Prompt 外。
        """
        items = [
            item for item in list(references or []) if isinstance(item, dict)
        ]
        step_id = (
            str(step.get("skill_step_id") or "").strip()
            if isinstance(step, dict)
            else ""
        )
        if not step_id:
            return items, set()
        matched: list[dict[str, Any]] = []
        rest: list[dict[str, Any]] = []
        for item in items:
            matched_steps = [
                str(value) for value in (item.get("matched_skill_steps") or [])
            ]
            if step_id in matched_steps:
                matched.append(item)
            else:
                rest.append(item)
        matched.sort(
            key=lambda item: float(item.get("final_weight") or 0),
            reverse=True,
        )
        matched_keys = {cls._reference_identity(item) for item in matched}
        return matched + rest, matched_keys

    @classmethod
    def _compact_reference_items(
        cls,
        ordered: list[dict[str, Any]],
        *,
        content_limit: int,
    ) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        used_chars = 2
        for raw in ordered[:16]:
            if not isinstance(raw, dict):
                continue
            item = {
                key: cls._compact_prompt_value(raw.get(key))
                for key in (
                    "id",
                    "source_id",
                    "source_type",
                    "title",
                    "summary",
                    "reason",
                    "final_weight",
                    "source_url",
                    "observed_at",
                    "period_evidence",
                    "can_use",
                    "data_use_policy",
                    "data_freshness",
                    "refresh_status",
                    "refresh_completeness",
                    "refresh_collected_at",
                    "refresh_truncated",
                    "source_snapshot_id",
                    "source_body_hash",
                )
                if raw.get(key) is not None
            }
            # 正文单独按 content_limit 截断（不走通用 600 字压缩），
            # 预算紧张时的压缩重试才有实际可回收空间。
            raw_content = raw.get("content")
            if raw_content is not None:
                text = " ".join(str(raw_content).split()).strip()
                if text:
                    refresh_status = str(raw.get("refresh_status") or "")
                    status_limit = 1600
                    if refresh_status in {"fresh_complete", "fresh_recent"}:
                        status_limit = 6000
                    elif refresh_status in {"fresh_partial", "fresh_recent_partial"}:
                        status_limit = 3000
                    effective_limit = min(content_limit, status_limit)
                    item["content"] = (
                        text
                        if len(text) <= effective_limit
                        else text[:effective_limit].rstrip() + "…"
                    )
                    if isinstance(raw.get("source_scope"), dict):
                        from .source_scope import shorten_source_excerpt
                        try:
                            scope = raw["source_scope"]
                            if (scope.get("snapshot_id") != raw.get("source_snapshot_id")
                                    or scope.get("source_body_hash") != raw.get("source_body_hash")):
                                raise ValueError("Reference version differs from excerpt scope")
                            item.update(shorten_source_excerpt(
                                str(raw_content), scope, effective_limit))
                        except ValueError:
                            # A modified body cannot borrow a previous version's
                            # evidence, even if the URL and title still match.
                            continue
            candidate_size = len(str(item))
            if compacted and used_chars + candidate_size > cls.reference_char_budget:
                break
            compacted.append(item)
            used_chars += candidate_size
        return compacted

    @classmethod
    def _prompt_references(
        cls,
        references: Any,
        step: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        ordered, matched_keys = cls._scope_references_for_step(references, step)
        compacted = cls._compact_reference_items(ordered, content_limit=6000)
        if matched_keys:
            matched_in = sum(
                1
                for item in compacted
                if cls._reference_identity(item) in matched_keys
            )
            if matched_in < len(matched_keys):
                # 预算装不下本步召回的全部证据时，先压缩单条正文再重试，
                # 而不是直接丢弃当前步骤自己的检索结果。
                retry = cls._compact_reference_items(ordered, content_limit=800)
                retry_matched = sum(
                    1
                    for item in retry
                    if cls._reference_identity(item) in matched_keys
                )
                if retry_matched > matched_in:
                    compacted = retry
        return compacted
