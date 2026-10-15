from creation.prompt_evidence import CreationEvidencePrompts


def test_oversized_middle_source_does_not_hide_later_evidence():
    class SmallBudgetPrompts(CreationEvidencePrompts):
        data_char_budget = 400

    results = SmallBudgetPrompts._prompt_data_results([
        {"source_id": 1, "can_use": True, "content_excerpt": "a" * 180},
        {"source_id": 2, "can_use": True, "content_excerpt": "b" * 600},
        {"source_id": 3, "can_use": True, "content_excerpt": "target fact"},
    ])

    assert [item["source_id"] for item in results] == [1, 3]
    assert results[-1]["content_excerpt"] == "target fact"
