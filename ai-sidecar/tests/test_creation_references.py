from __future__ import annotations

import httpx
import pytest

from creation.service import CreationOptions, CreationService


def document_408() -> dict:
    return {
        "id": 408,
        "title": "示例公司员工周年礼物领取指南",
        "doc_type": "资料参考",
        "summary": "面向员工周年场景的礼物领取参考资料",
        "full_content": "员工可按周年节点自主选择并领取礼物。",
        "sections_json": "[]",
        "style_phrases": "[]",
        "prompt_hint": "适用于员工周年礼物与领取指南类创作",
        "usage_count": 0,
        "review_status": "auto_created",
        "updated_at": 1_785_081_300_661,
        "source_url": None,
    }


def test_vector_evidence_survives_keyword_dedup_and_relevance_filter(tmp_path):
    service = CreationService.__new__(CreationService)
    service.db_path = str(tmp_path / "memory-bread.db")
    tmp_path.joinpath("memory-bread.db").touch()
    service.enable_vector_recall = True
    service._embedding_model = object()
    service._query_document_rows = lambda *_args: [document_408()]
    service._vector_recall = lambda *_args: [
        {**document_408(), "_vector_similarity": 0.82}
    ]

    references = service.retrieve_references(
        "写一份周年员工的礼物指南",
        {
            "doc_type": "指南",
            "keywords": ["写一份周年员", "礼物指南"],
        },
        CreationOptions(max_references=6),
    )

    assert [item.id for item in references] == [408]
    assert references[0].relevance_score == 0.82


def test_chinese_keyword_fallback_and_doc_type_cover_gift_guide():
    service = CreationService.__new__(CreationService)

    keywords = service._extract_keywords("写一份周年员工的礼物指南")

    assert any("周年" in keyword for keyword in keywords)
    assert any("礼物" in keyword for keyword in keywords)
    assert service._infer_doc_type("写一份周年员工的礼物指南") == "指南"


@pytest.mark.asyncio
async def test_github_search_maps_public_repository_metadata(monkeypatch):
    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, params):
            assert url == "https://api.github.com/search/repositories"
            assert params["q"]
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "items": [
                        {
                            "full_name": "example/memory-agent",
                            "html_url": "https://github.com/example/memory-agent",
                            "description": "A public memory agent toolkit",
                            "stargazers_count": 321,
                            "language": "Python",
                            "updated_at": "2026-07-01T00:00:00Z",
                        }
                    ]
                },
            )

    monkeypatch.setattr(
        "creation.service.httpx.AsyncClient",
        lambda **_kwargs: FakeAsyncClient(),
    )
    service = CreationService.__new__(CreationService)

    results = await service.search_github_context(
        "检索 GitHub 记忆 Agent 仓库",
        {"topic": "记忆 Agent", "keywords": ["memory", "agent"]},
    )

    assert len(results) == 1
    assert results[0].full_name == "example/memory-agent"
    assert results[0].stars == 321
