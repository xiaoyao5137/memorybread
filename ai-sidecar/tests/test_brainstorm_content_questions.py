"""Content question packets bind actual task text and require independent review."""
import copy

import pytest

from creation.brainstorm_content_questions import (
    ContentQuestionError, compile_report_fact_fallback,
    content_report_source_needs, content_review_prompt, content_review_schema,
    content_schema, content_seed_schema, content_seed_system_prompt,
    content_system_prompt, normalize_content_seed, validate_content_packet,
    validate_content_review,
)


GPU_ROOT = "为 AIGC GPU成本优化写周报，保留 GPU 使用表与项目用量表，不得编造本周数值。"
GPU_PERIOD = {"start": "2026-09-21", "end": "2026-09-27", "source": "user", "timezone": "Asia/Shanghai"}
BOOK_ROOT = "为社区读书角写活动介绍，说明共读与迟到参与安排，不编造报名链接。"


def question(prompt, first, second):
    return {"exploration_stage": None, "parent_question_id": None,
        "parent_option_id": None, "single_choice_reason": "", "id": "content_one",
        "dimension_id": "content_detail", "dimension": "具体内容安排",
        "type": "multi_choice", "prompt": prompt,
        "why_now": "确定正文中需要交代的实际安排。", "context_details": "",
        "required": True, "allow_custom": True,
        "options": [
            {"id": "a", "label": first[0], "description": first[1], "details": "",
             "recommended": True, "input_value": None},
            {"id": "b", "label": second[0], "description": second[1], "details": "",
             "recommended": False, "input_value": None}],
        "answer_template": "可补充实际限制或已有证据。", "input_clarification": None}


def gpu_packet():
    return {"questions": [question("GPU 成本变化先核对哪组数据？",
        ("单卡使用", "先核对 GPU 使用表的单卡指标，缺值标待核。"),
        ("项目用量", "先核对项目用量表的分项变化，缺值标待核。"))],
        "content_proofs": [{"question_id": "content_one", "anchor_source": "root_request",
            "anchor_quote": "GPU", "document_effect": "周报成本变化段的核对对象和缺口说明不同。",
            "option_effects": ["先写单卡使用数据的可核实变化及缺项。",
                               "先写项目用量数据的可核实变化及缺项。"]}],
        "no_new_decision_reason": ""}


def book_packet():
    return {"questions": [question("读书角的迟到参与怎样安排？",
        ("直接加入", "迟到居民可加入正在进行的讨论，并先了解当前话题。"),
        ("下轮加入", "迟到居民先听本轮交流，下一轮再参与发言。"))],
        "content_proofs": [{"question_id": "content_one", "anchor_source": "root_request",
            "anchor_quote": "读书角", "document_effect": "参与方式一节会交代迟到居民从何时加入。",
            "option_effects": ["写明迟到者进场后立即参与当前讨论。",
                               "写明迟到者从下一轮开始发言。"]}],
        "no_new_decision_reason": ""}


def authority(root=GPU_ROOT, decisions=None, edits=None, period=GPU_PERIOD, stage="detail"):
    return {"stage": stage, "root_request": root, "decisions": decisions or [],
        "brief_edits": edits or {}, "confirmed_period": period}


def passing_review(packet):
    question_item = packet["questions"][0]
    proof = packet["content_proofs"][0]
    return {"questions": {question_item["id"]: {
        "prompt_quote": question_item["prompt"],
        "anchor_quote": proof["anchor_quote"],
        "document_effect_quote": proof["document_effect"], "verdict": "pass",
        "options": {option["id"]: {"label_quote": option["label"],
            "description_quote": option["description"],
            "effect_quote": proof["option_effects"][index], "verdict": "pass"}
            for index, option in enumerate(question_item["options"])}}}}


def gpu_seed():
    return {"decisions": [{"anchor_quote": "GPU 成本优化",
        "prompt": "本期周报应优先核查哪类成本优化机会？",
        "document_effect": "待核实", "recommended_index": 0,
        "options": [
            {"label": "低利用率集群回收", "description": "先识别低利用率集群并评估可回收容量，缺数据时标待核。",
             "effect": "待核实"},
            {"label": "实例规格成本性能", "description": "比较实例规格与计费差异，缺账单时列为待核事项。",
             "effect": "待核实"}]}],
        "no_new_decision_reason": ""}


def test_compute_cost_report_source_needs_are_stable_independent_and_profile_scoped():
    root = "请生成GPU成本优化的周报"
    needs = content_report_source_needs(root, {})
    assert needs == content_report_source_needs(root, {})
    assert len(needs) == len({item["id"] for item in needs}) == 3
    assert all(item["id"].startswith("need-content-report-") for item in needs)
    assert all(item["skill_id"] == "" and item["tool"] == "data_search" for item in needs)
    assert all(item["period_mode"] == "full_range" for item in needs)
    assert [item["source_form"] for item in needs] == ["report", "report", "any"]
    assert "账单费用" in needs[0]["label"]
    assert "资源用量" in needs[1]["label"] and "GPU卡时" in needs[1]["label"]
    assert "利用率" in needs[2]["label"] and "回收调度" in needs[2]["label"]
    assert all("可核验来源" in item["query"] and "已取得" not in str(item) for item in needs)
    assert content_report_source_needs("写社区读书角活动介绍", {}) == []
    assert content_report_source_needs("写营销成本优化周报", {}) == []
    assert content_report_source_needs(root, {"root_request": "写读书角活动介绍"}) == []
    assert content_report_source_needs("写读书角活动介绍", {"root_request": root}) == needs


def test_report_source_need_labels_do_not_repeat_unverified_root_claims():
    needs = content_report_source_needs("请生成已节省30%的GPU成本优化的周报", {})
    assert len(needs) == 3
    assert all(item["label"].startswith("GPU") for item in needs)
    assert all("已节省" not in item["label"] and "30%" not in item["query"]
               for item in needs)


def test_small_seed_repairs_whitespace_anchor_and_compiles_real_option_effects():
    seed = gpu_seed()
    before = copy.deepcopy(seed)
    packet = normalize_content_seed(seed, **authority(stage="direction"))
    assert seed == before
    question_item = packet["questions"][0]
    proof = packet["content_proofs"][0]
    assert question_item["id"] == "content_direction_1"
    assert question_item["prompt"].startswith("GPU成本优化：")
    assert proof["anchor_source"] == "root_request"
    assert proof["anchor_quote"] == "GPU成本优化"
    assert "识别低利用率集群" in proof["document_effect"]
    assert proof["option_effects"] == [
        "正文将交代：" + item["description"] for item in question_item["options"]]
    assert len(set(proof["option_effects"])) == 2
    validate_content_review(passing_review(packet), packet)
    assert len(str(content_seed_schema())) < len(str(content_schema()))
    assert content_seed_schema()["properties"]["decisions"]["maxItems"] == 1
    assert (content_seed_schema()["properties"]["decisions"]["items"]
        ["properties"]["options"]["maxItems"]) == 2
    assert (content_seed_schema()["properties"]["decisions"]["items"]
        ["properties"]["options"]["minItems"]) == 2
    assert "周期由独立输入预检处理" in content_seed_system_prompt("direction")
    assert "不能写 A/B/C" in content_seed_system_prompt("direction")
    assert "本次只输出 1 道" in content_seed_system_prompt("direction")
    assert "跨领域句式示例" in content_seed_system_prompt("direction")
    assert "滚动指代" in content_seed_system_prompt("direction")


def test_seed_compiles_other_domain_and_respects_current_root_edit():
    seed = {"decisions": [{"anchor_quote": "读书角", "prompt": "迟到居民怎样加入读书角？",
        "document_effect": "参与方式会写明迟到居民从何时加入。", "recommended_index": 1,
        "options": [{"label": "直接加入", "description": "迟到居民可立即加入正在进行的讨论。",
                     "effect": "正文说明迟到居民直接加入当前讨论。"},
                    {"label": "下轮加入", "description": "迟到居民先旁听本轮，下一轮再发言。",
                     "effect": "正文说明迟到居民下轮再发言。"}]}],
        "no_new_decision_reason": ""}
    packet = normalize_content_seed(seed, **authority(root=BOOK_ROOT, period=None))
    assert packet["questions"][0]["options"][1]["recommended"] is True
    with pytest.raises(ContentQuestionError, match="untrusted_anchor"):
        normalize_content_seed(seed, **authority(edits={"root_request": "改写社区花园活动介绍。"}))


@pytest.mark.parametrize("mutation,code", [
    (lambda s: s["decisions"][0].update(prompt="请生成GPU成本优化的周报"), "not_a_new_question"),
    (lambda s: s["decisions"][0].update(prompt="周报周期应选择哪一周？"), "period_requires_preflight"),
    (lambda s: s["decisions"][0]["options"][0].update(label="2023-10-01 至 2023-10-07"),
     "unsupported_date"),
    (lambda s: s["decisions"][0]["options"][0].update(description="如节省5万元就写入周报。"),
     "unsupported_number"),
    (lambda s: s["decisions"][0]["options"][0].update(effect="如降低15%则写为结果。"),
     "unsupported_number"),
    (lambda s: s["decisions"][0]["options"][0].update(label="A100实例回收"),
     "unsupported_number"),
    (lambda s: s["decisions"][0]["options"][0].update(description="待核实"),
     "empty_option_mechanism"),
    (lambda s: s["decisions"][0]["options"][0].update(label="A"),
     "generic_option_label"),
    (lambda s: s["decisions"][0]["options"][0].update(label="方案一"),
     "generic_option_label"),
    (lambda s: s["decisions"][0].update(recommended_index=3), "invalid_recommendation"),
    (lambda s: s["decisions"][0]["options"][1].update(
        description="先识别低利用率集群并评估可回收容量，缺数据时标待核。"),
     "duplicate_option_effects"),
])
def test_seed_rejects_untrusted_or_empty_decisions(mutation, code):
    seed = gpu_seed()
    mutation(seed)
    with pytest.raises(ContentQuestionError, match=code):
        normalize_content_seed(seed, **authority(stage="direction"))


def test_empty_seed_requires_reason_and_compiles_empty_packet():
    seed = {"decisions": [], "no_new_decision_reason": "有效确认已覆盖全部正文安排。"}
    packet = normalize_content_seed(seed, **authority(stage="writing"))
    assert packet == {"questions": [], "content_proofs": [],
                      "no_new_decision_reason": seed["no_new_decision_reason"]}
    with pytest.raises(ContentQuestionError, match="invalid_no_new_decision_reason"):
        normalize_content_seed({"decisions": [], "no_new_decision_reason": ""},
            **authority(stage="writing"))


def test_seed_drops_duplicate_unpublished_prompts_and_reviews_first_candidate():
    seed = gpu_seed()
    duplicate = copy.deepcopy(seed["decisions"][0])
    duplicate["options"][0]["description"] = "改问另一个方向但使用同一题干。"
    seed["decisions"].append(duplicate)
    packet = normalize_content_seed(seed, **authority(stage="direction"))
    assert len(packet["questions"]) == len(packet["content_proofs"]) == 1
    assert packet["questions"][0]["id"] == "content_direction_1"
    assert packet["questions"][0]["options"][0]["description"] == (
        "先识别低利用率集群并评估可回收容量，缺数据时标待核。")
    validate_content_review(passing_review(packet), packet)


def test_writing_rejects_question_already_confirmed_in_detail():
    packet = book_packet()
    previous = {"question_id": "detail-one", "question": packet["questions"][0]["prompt"],
                "answer_source": "user", "answer": "直接加入", "user_inputs": ["直接加入"]}
    with pytest.raises(ContentQuestionError, match="repeats_confirmed_question"):
        validate_content_packet(packet, **authority(root=BOOK_ROOT, stage="writing",
                                                    decisions=[previous]))
    seed = {"decisions": [{"anchor_quote": "读书角", "prompt": previous["question"],
                           "document_effect": "改变迟到参与正文安排。", "recommended_index": 0,
                           "options": [{"label": "直接加入", "description": "迟到居民立即参与讨论。",
                                        "effect": "正文说明直接加入当前讨论。"},
                                       {"label": "下轮加入", "description": "迟到居民下一轮参与。",
                                        "effect": "正文说明下一轮加入讨论。"}]}],
            "no_new_decision_reason": ""}
    with pytest.raises(ContentQuestionError, match="repeats_confirmed_question"):
        normalize_content_seed(seed, **authority(root=BOOK_ROOT, stage="writing",
                                                  decisions=[previous]))


def test_request_source_and_missing_only_choices_are_not_content_decisions():
    seed = {"decisions": [{"anchor_quote": "请生成GPU成本优化的周报",
        "prompt": "本周GPU成本优化周报应聚焦哪些具体核查对象？",
        "document_effect": "正文会列出待核实的表字段或日志路径。",
        "recommended_index": 0,
        "options": [
            {"label": "先核对请求指定表字段",
             "description": "检查根请求中是否已指定具体表名或字段，若有则查询该表。",
             "effect": "正文说明将按请求中指定的字段核查。"},
            {"label": "若无指定则查通用日志",
             "description": "根请求未指定具体对象时，先查找系统通用日志或标准账单表。",
             "effect": "正文列出可找到的通用指标。"}]}],
        "no_new_decision_reason": ""}
    with pytest.raises(ContentQuestionError, match="generic_content_options"):
        normalize_content_seed(seed, **authority(root="请生成GPU成本优化的周报",
            period=None, stage="direction"))
    assert "题干重复 root 的主题词不等于选项具体" in content_review_prompt("direction")


def test_specific_content_choices_can_include_missing_evidence_fallback():
    seed = gpu_seed()
    seed["decisions"][0]["options"][1].update(
        label="仅标注待核实", description="若没有可靠数据，仅列出待核实项。",
        effect="正文标出尚缺的证据。")
    packet = normalize_content_seed(seed, **authority(stage="direction"))
    assert len(packet["questions"][0]["options"]) == 2


def test_one_meta_option_cannot_delegate_content_choice_back_to_user():
    seed = gpu_seed()
    seed["decisions"][0]["options"][1].update(
        label="等待用户确认关注点",
        description="询问用户希望重点呈现哪些指标，再决定正文内容。",
        effect="此选项本身不直接提供正文内容。")
    with pytest.raises(ContentQuestionError, match="delegates_decision_to_user"):
        normalize_content_seed(seed, **authority(stage="direction"))
    assert "每个选项都必须可直接选用" in content_review_prompt("direction")


def test_fact_report_rejects_rolling_period_before_and_after_confirmation():
    seed = gpu_seed()
    seed["decisions"][0]["prompt"] = "本周GPU成本优化周报先核查哪类机会？"
    with pytest.raises(ContentQuestionError, match="unconfirmed_relative_period"):
        normalize_content_seed(seed, **authority(period=None, stage="direction"))
    with pytest.raises(ContentQuestionError, match="relative_period_reference"):
        normalize_content_seed(seed, **authority(period=GPU_PERIOD, stage="direction"))
    seed["decisions"][0]["prompt"] = "本期GPU成本优化周报先核查哪类机会？"
    assert normalize_content_seed(seed, **authority(period=None, stage="direction"))
    seed["decisions"][0]["options"][0]["description"] = "先核查最近一周集群利用率，缺数据标待核。"
    with pytest.raises(ContentQuestionError, match="unconfirmed_relative_period"):
        normalize_content_seed(seed, **authority(period=None, stage="direction"))


def test_authorized_amount_can_be_used_but_period_digits_cannot_authorize_metrics():
    seed = gpu_seed()
    seed["decisions"][0]["options"][0]["description"] = "核对是否触及5万元预算上限，缺账单时标待核。"
    packet = normalize_content_seed(seed, **authority(
        root=GPU_ROOT + "本期预算上限5万元。", stage="direction"))
    assert "5万元" in packet["questions"][0]["options"][0]["description"]
    seed["decisions"][0]["options"][0]["description"] = "核对是否降低21%，缺数据时标待核。"
    with pytest.raises(ContentQuestionError, match="unsupported_number"):
        normalize_content_seed(seed, **authority(stage="direction"))


@pytest.mark.parametrize("stage", ["direction", "detail", "writing"])
def test_cross_domain_content_packets_are_valid_and_not_mutated(stage):
    for packet, values in ((gpu_packet(), authority(stage=stage)),
                           (book_packet(), authority(root=BOOK_ROOT, period=None, stage=stage))):
        before = copy.deepcopy(packet)
        assert validate_content_packet(packet, **values) == before
        assert packet == before
    schema = content_schema()
    assert set(schema["required"]) == {"questions", "content_proofs", "no_new_decision_reason"}
    assert schema["additionalProperties"] is False
    assert "事实性周报" in content_system_prompt("direction")
    assert "unknown" in content_review_prompt(stage)
    assert "先核对某表" in content_review_prompt(stage)
    assert "判 unanswerable" in content_review_prompt(stage)


def test_actual_user_decision_and_manual_edit_can_anchor_but_superseded_answer_cannot():
    decision = {"question_id": "earlier", "answer_source": "user",
        "answer": "原定先介绍共读流程", "user_inputs": ["迟到居民后续可参与"]}
    packet = book_packet()
    packet["questions"][0]["prompt"] = "迟到居民后续可参与时怎样接入读书角？"
    packet["content_proofs"][0].update(anchor_source="user_decision", anchor_quote="迟到居民")
    assert validate_content_packet(packet, **authority(root=BOOK_ROOT, decisions=[decision]))
    edits = {"earlier": "改为先说明迟到居民入场安排"}
    packet["content_proofs"][0].update(anchor_source="manual_edit", anchor_quote="迟到居民")
    assert validate_content_packet(packet, **authority(root=BOOK_ROOT, decisions=[decision], edits=edits))
    packet["content_proofs"][0].update(anchor_source="user_decision")
    assert validate_content_packet(packet, **authority(root=BOOK_ROOT, decisions=[decision], edits=edits))
    packet["questions"][0]["prompt"] = "原定先介绍共读流程后，迟到参与怎样安排？"
    packet["content_proofs"][0]["anchor_quote"] = "原定先介绍共读流程"
    with pytest.raises(ContentQuestionError, match="untrusted_anchor"):
        validate_content_packet(packet, **authority(root=BOOK_ROOT, decisions=[decision], edits=edits))


def test_nonempty_root_edit_replaces_old_root_as_factual_anchor():
    packet = gpu_packet()
    edits = {"root_request": "改为编写读书角参与介绍，沿用已确认安排。"}
    with pytest.raises(ContentQuestionError, match="untrusted_anchor"):
        validate_content_packet(packet, **authority(edits=edits))
    packet = book_packet()
    packet["questions"][0]["prompt"] = "读书角的参与顺序怎样安排？"
    packet["content_proofs"][0]["anchor_source"] = "root_request"
    assert validate_content_packet(packet, **authority(edits=edits))


def test_cleared_root_or_decision_cannot_anchor_old_user_text():
    with pytest.raises(ContentQuestionError, match="untrusted_anchor"):
        validate_content_packet(gpu_packet(), **authority(edits={"root_request": ""}))
    decision = {"question_id": "earlier", "answer_source": "user",
        "answer": "迟到居民后续可参与", "user_inputs": ["迟到居民先了解当前话题"]}
    packet = book_packet()
    packet["questions"][0]["prompt"] = "迟到居民先了解当前话题后怎样加入读书角？"
    packet["content_proofs"][0].update(anchor_source="user_decision", anchor_quote="迟到居民")
    with pytest.raises(ContentQuestionError, match="untrusted_anchor"):
        validate_content_packet(packet, **authority(root=BOOK_ROOT, period=None,
            decisions=[decision], edits={"earlier": ""}))


@pytest.mark.parametrize("source", ["agent_assumption", "user_excluded", "candidate"])
def test_assumptions_exclusions_and_candidates_never_supply_fact_anchors(source):
    packet = gpu_packet()
    packet["questions"][0]["prompt"] = "历史指标的成本变化先核对哪组数据？"
    packet["content_proofs"][0].update(anchor_source="user_decision", anchor_quote="历史指标")
    decisions = [{"question_id": "old", "answer_source": source,
        "answer": "历史指标已证实", "user_inputs": []}]
    with pytest.raises(ContentQuestionError, match="untrusted_anchor"):
        validate_content_packet(packet, **authority(decisions=decisions))


def test_preflight_or_untrusted_period_cannot_create_an_anchor():
    packet = gpu_packet()
    packet["questions"][0]["prompt"] = "2026-09-21 的 GPU 成本变化先核对哪组数据？"
    packet["content_proofs"][0].update(anchor_quote="2026-09-21")
    with pytest.raises(ContentQuestionError, match="untrusted_anchor"):
        validate_content_packet(packet, **authority(period=GPU_PERIOD))
    with pytest.raises(ContentQuestionError, match="untrusted_period"):
        validate_content_packet(packet, **authority(period={"start": "2026-09-21",
            "end": "2026-09-27", "timezone": "Asia/Shanghai", "source": "input_preflight"}))


@pytest.mark.parametrize("period", [
    {"start": "2026-09-31", "end": "2026-10-07", "timezone": "Asia/Shanghai", "source": "user"},
    {"start": "2026-10-07", "end": "2026-09-21", "timezone": "Asia/Shanghai", "source": "user"},
    {"start": "2026-09-21", "end": "2026-09-27", "timezone": "", "source": "user"},
])
def test_invalid_confirmed_period_never_becomes_factual_authority(period):
    with pytest.raises(ContentQuestionError, match="untrusted_period"):
        validate_content_packet(gpu_packet(), **authority(period=period))


@pytest.mark.parametrize("mutation,code", [
    (lambda p: p["questions"][0]["options"][0].update(recommended=False), "invalid_recommendation"),
    (lambda p: p["questions"][0]["options"][0].update(label="字" * 25), "invalid_option_label"),
    (lambda p: p["questions"][0].update(parent_question_id="old"), "invalid_flat_question"),
    (lambda p: p["content_proofs"][0]["option_effects"].__setitem__(1,
        p["content_proofs"][0]["option_effects"][0]), "duplicate_option_effects"),
    (lambda p: p["content_proofs"][0].update(anchor_quote="本次创作"), "untrusted_anchor"),
    (lambda p: p["questions"][0].update(extra="unexpected"), "invalid_question"),
])
def test_invalid_structure_and_empty_differences_fail_closed(mutation, code):
    packet = gpu_packet()
    mutation(packet)
    with pytest.raises(ContentQuestionError, match=code):
        validate_content_packet(packet, **authority())


def test_pure_generic_style_question_is_rejected_even_with_a_real_task_anchor():
    packet = gpu_packet()
    packet["questions"][0]["prompt"] = "GPU 如何更好体现价值？"
    with pytest.raises(ContentQuestionError, match="generic_question"):
        validate_content_packet(packet, **authority())


@pytest.mark.parametrize("field,maximum,code", [
    ("anchor_quote", 24, "invalid_anchor_quote"),
    ("document_effect", 160, "invalid_document_effect"),
])
def test_proof_copy_respects_core_budgets(field, maximum, code):
    packet = gpu_packet()
    packet["content_proofs"][0][field] = "字" * (maximum + 1)
    with pytest.raises(ContentQuestionError, match=code):
        validate_content_packet(packet, **authority())
    packet = gpu_packet()
    packet["content_proofs"][0]["option_effects"][0] = "字" * 161
    with pytest.raises(ContentQuestionError, match="invalid_option_effects"):
        validate_content_packet(packet, **authority())


def test_empty_packet_requires_reason_and_independent_pass():
    packet = {"questions": [], "content_proofs": [],
        "no_new_decision_reason": "当前用户输入与有效确认已经说明全部正文安排。"}
    assert validate_content_packet(packet, **authority()) == packet
    schema = content_review_schema(packet)
    assert set(schema["properties"]) == {"no_new_decision"}
    review = {"no_new_decision": {"reason_quote": packet["no_new_decision_reason"],
                                  "verdict": "pass"}}
    validate_content_review(review, packet)
    review["no_new_decision"]["verdict"] = "unknown"
    with pytest.raises(ContentQuestionError, match="review_rejected"):
        validate_content_review(review, packet)
    packet["no_new_decision_reason"] = "已确认全部安排。"
    assert validate_content_packet(packet, **authority()) == packet
    packet["no_new_decision_reason"] = ""
    with pytest.raises(ContentQuestionError, match="invalid_no_new_decision_reason"):
        validate_content_packet(packet, **authority())
    nonempty = gpu_packet()
    nonempty["no_new_decision_reason"] = "无需再问。"
    with pytest.raises(ContentQuestionError, match="invalid_no_new_decision_reason"):
        validate_content_packet(nonempty, **authority())


def test_review_binds_every_actual_option_and_rejects_unsupported_fact():
    packet = gpu_packet()
    schema = content_review_schema(packet)
    assert set(schema["properties"]["questions"]["properties"]) == {"content_one"}
    review = passing_review(packet)
    validate_content_review(review, packet)
    review["questions"]["content_one"]["options"]["b"]["verdict"] = "unsupported_fact"
    with pytest.raises(ContentQuestionError, match="review_rejected"):
        validate_content_review(review, packet)
    review = passing_review(packet)
    review["questions"]["content_one"]["options"]["b"]["description_quote"] = "另一项文案"
    with pytest.raises(ContentQuestionError, match="review_quote_mismatch"):
        validate_content_review(review, packet)


def test_review_cannot_silently_skip_nonrecommended_alternative():
    packet = gpu_packet()
    review = passing_review(packet)
    review["questions"]["content_one"]["options"].pop("b")
    with pytest.raises(ContentQuestionError, match="invalid_review"):
        validate_content_review(review, packet)


def _fallback_decision(stage, label):
    return {"dimension_id": "content_{}_fallback_1".format(stage),
        "answer_source": "user", "answer": label, "user_inputs": [label],
        "selected_options": [{"label": label}]}


def test_report_fallback_asks_three_distinct_gpu_content_decisions_without_claiming_metrics():
    root = "请生成GPU成本优化的周报"
    direction = _fallback_decision("direction", "费用与用量拆解")
    detail = _fallback_decision("detail", "账单费用对照")
    packets = [compile_report_fact_fallback(stage=stage, root_request=root,
        decisions=prior, brief_edits={}, confirmed_period=GPU_PERIOD)
        for stage, prior in (("direction", []), ("detail", [direction]),
                             ("writing", [direction, detail]))]
    assert all(packet is not None for packet in packets)
    prompts = [packet["questions"][0]["prompt"] for packet in packets]
    assert len(set(prompts)) == 3
    assert prompts == [
        "「GPU成本优化」本期先核对哪组费用和资源事实？",
        "「GPU成本优化」费用与用量拆解，先核哪项对照口径？",
        "「GPU成本优化」节省结论缺同口径对照时，正文具体写什么？",
    ]
    assert [option["label"] for option in packets[0]["questions"][0]["options"]] == [
        "费用与用量拆解", "低效占用与优化动作"]
    assert [option["label"] for option in packets[1]["questions"][0]["options"]] == [
        "账单费用对照", "资源用量对照"]
    assert "GPU卡时" in packets[1]["questions"][0]["options"][1]["description"]
    assert "可比期账单金额" in packets[2]["questions"][0]["options"][1]["description"]
    for stage, packet, prior in zip(("direction", "detail", "writing"),
                                    packets, ([], [direction], [direction, detail])):
        assert validate_content_packet(packet, stage=stage, root_request=root,
            decisions=prior, brief_edits={}, confirmed_period=GPU_PERIOD) == packet
        assert len(packet["questions"][0]["options"]) == 2
        assert len(set(packet["content_proofs"][0]["option_effects"])) == 2
        whole = str(packet)
        assert "本周" not in whole and "2026-09-21" not in whole
        assert "已节省" not in whole and "当前无账单" not in whole
        assert "缺" in whole and ("核对" in whole or "对照" in whole)


def test_report_fallback_supports_other_domain_and_current_root_edit():
    old = "请生成GPU成本优化的周报"
    edited = "请撰写云计算费用优化月报，核对成本与实例使用记录"
    packet = compile_report_fact_fallback(stage="direction", root_request=old,
        decisions=[], brief_edits={"root_request": edited}, confirmed_period=None)
    assert packet is not None
    assert packet["content_proofs"][0]["anchor_quote"] == "云计算费用优化"
    assert "GPU" not in str(packet)
    assert all("云计算" in option["description"]
               for option in packet["questions"][0]["options"])
    assert validate_content_packet(packet, stage="direction", root_request=old,
        decisions=[], brief_edits={"root_request": edited}, confirmed_period=None) == packet


@pytest.mark.parametrize("root", ["写周报", "请写一份周报", "请写工作周报",
                                  "请撰写社区读书角活动月报", "请写差旅费用优化月报",
                                  "写活动介绍"])
def test_report_fallback_refuses_vague_or_nonreport_subject(root):
    assert compile_report_fact_fallback(stage="direction", root_request=root,
        decisions=[], brief_edits={}, confirmed_period=None) is None


def test_report_fallback_needs_a_real_prior_content_choice_for_followups():
    root = "请生成GPU成本优化的周报"
    for stage in ("detail", "writing"):
        assert compile_report_fact_fallback(stage=stage, root_request=root,
            decisions=[], brief_edits={}, confirmed_period=GPU_PERIOD) is None
    skipped = _fallback_decision("direction", "费用与用量拆解")
    skipped["answer_source"] = "agent_assumption"
    assert compile_report_fact_fallback(stage="detail", root_request=root,
        decisions=[skipped], brief_edits={}, confirmed_period=GPU_PERIOD) is None
    both = _fallback_decision("direction", "费用与用量拆解")
    both["selected_options"].append({"label": "低效占用与优化动作"})
    assert compile_report_fact_fallback(stage="detail", root_request=root,
        decisions=[both], brief_edits={}, confirmed_period=GPU_PERIOD) is None


def test_report_fallback_detail_follows_confirmed_action_path():
    root = "请生成GPU成本优化的周报"
    direction = _fallback_decision("direction", "低效占用与优化动作")
    packet = compile_report_fact_fallback(stage="detail", root_request=root,
        decisions=[direction], brief_edits={}, confirmed_period=GPU_PERIOD)
    assert packet is not None
    assert [option["label"] for option in packet["questions"][0]["options"]] == [
        "低效占用核查", "措施效果对照"]
    assert "利用率" in packet["questions"][0]["options"][0]["description"]
    assert "回收或调度记录" in packet["questions"][0]["options"][1]["description"]
    detail = _fallback_decision("detail", "措施效果对照")
    writing = compile_report_fact_fallback(stage="writing", root_request=root,
        decisions=[direction, detail], brief_edits={}, confirmed_period=GPU_PERIOD)
    assert writing is not None
    assert "措施记录及执行前后同口径账单与用量" in (
        writing["questions"][0]["options"][1]["description"])
