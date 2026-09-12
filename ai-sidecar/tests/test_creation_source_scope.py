import hashlib
import pytest
from creation.source_scope import source_excerpt, shorten_source_excerpt
from creation.prompt_evidence import CreationEvidencePrompts


def test_unicode_source_scope_tracks_actual_excerpt_and_repeated_clipping():
    body = "  甲😀\n乙正文\n未读章节"
    view = source_excerpt(body, 8, 61, "partial")
    scope = view["source_scope"]
    assert body.encode()[scope["start_byte"]:scope["end_byte"]].decode() == view["content"]
    assert scope["source_coverage"] == "partial"
    assert scope["excerpt_truncated"]
    assert not scope["allows_full_document_claims"]
    short = shorten_source_excerpt(view["content"], scope, 4)
    assert short["content"] == "  甲😀"
    assert short["source_scope"]["end_byte"] == 9
    assert short["source_scope"]["source_body_hash"] == hashlib.sha256(body.encode()).hexdigest()
    assert short["source_scope"]["excerpt_truncated"]
    assert short["source_scope"]["source_coverage"] == "partial"


def test_source_scope_rejects_stale_body_and_tampered_excerpt():
    with pytest.raises(ValueError):
        source_excerpt("new body", 50, 61, "complete", "old hash")
    view = source_excerpt("first\nsecond", 50, 61, "complete")
    assert not view["source_scope"]["excerpt_truncated"]
    assert view["source_scope"]["allows_full_document_claims"]
    assert not shorten_source_excerpt(view["content"], view["source_scope"], 2)["source_scope"]["allows_full_document_claims"]
    with pytest.raises(ValueError):
        shorten_source_excerpt("first second", view["source_scope"], 5)


def test_prompt_compaction_preserves_exact_partial_scope_and_rejects_wrong_version():
    body = "甲😀\n  段落\n" * 1000
    view = source_excerpt(body, 8000, 61, "partial")
    reference = dict(view, id=1, source_id=1, source_type="document",
                     source_snapshot_id=61, source_body_hash=view["source_scope"]["source_body_hash"],
                     refresh_status="fresh_partial", refresh_completeness="partial")
    result = CreationEvidencePrompts._prompt_references([reference])
    assert len(result) == 1
    item = result[0]
    scope = item["source_scope"]
    assert item["content"] == body.encode()[:scope["end_byte"]].decode()
    assert scope["source_coverage"] == "partial"
    assert scope["excerpt_truncated"]
    assert CreationEvidencePrompts._prompt_references([dict(reference, source_snapshot_id=62)]) == []


def test_later_reference_revision_replaces_higher_scoring_old_body_atomically():
    from creation.agent_loop import CreationAgentLoop
    old = dict(source_excerpt('旧正文', 100, 7, 'complete'), source_type='document',
               source_id=1, source_snapshot_id=7, final_weight=.99, skill_step_id='first')
    old['source_body_hash'] = old['source_scope']['source_body_hash']
    new = dict(source_excerpt('新正文', 100, 8, 'partial'), source_type='document',
               source_id=1, source_snapshot_id=8, final_weight=.6, skill_step_id='second')
    new['source_body_hash'] = new['source_scope']['source_body_hash']
    merged = CreationAgentLoop._merge_reference_states([old], [new], limit=10)
    assert merged[0]['content'] == '新正文'
    assert merged[0]['source_snapshot_id'] == merged[0]['source_scope']['snapshot_id'] == 8
    assert merged[0]['matched_skill_steps'] == ['first', 'second']
    legacy = {'source_type':'document','source_id':1,'content':'过时摘要','final_weight':1}
    again = CreationAgentLoop._merge_reference_states(merged, [legacy], limit=10)[0]
    assert again['content'] == '新正文'
    assert again['matched_skill_steps'] == ['first', 'second']
