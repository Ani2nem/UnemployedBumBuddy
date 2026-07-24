from __future__ import annotations

from pipeline import draft
from pipeline.models import ProjectMatch


def _fake_embed(text):
    return [1.0, 0.0] if "ml" in text.lower() else [0.0, 1.0]


def test_select_style_examples_filters_by_tag(style_examples_table):
    draft.ingest_style_example_embedding("ex1", "cold_outreach", "Hi, I loved building ML systems...", embed_fn=_fake_embed)
    draft.ingest_style_example_embedding("ex2", "referral", "Hey, my friend mentioned...", embed_fn=_fake_embed)

    selected = draft.select_style_examples("ml job", scenario_tag="cold_outreach", embed_fn=_fake_embed)
    assert [ex.example_id for ex in selected] == ["ex1"]


def test_select_style_examples_ranks_by_embedding_similarity(style_examples_table):
    draft.ingest_style_example_embedding("ex1", "cold_outreach", "Hi, I loved building ML systems...", embed_fn=_fake_embed)
    draft.ingest_style_example_embedding("ex2", "cold_outreach", "Hey there, excited about web dev...", embed_fn=_fake_embed)

    selected = draft.select_style_examples("looking for an ml expert", embed_fn=_fake_embed, top_k=1)
    assert selected[0].example_id == "ex1"


def test_fetch_project_details_returns_matching_items(projects_table):
    projects_table.put_item(Item={"project_id": "p1", "title": "ML thing", "description": "desc", "url": "https://x"})
    details = draft.fetch_project_details(["p1", "missing"])
    assert len(details) == 1
    assert details[0]["project_id"] == "p1"


def test_draft_application_builds_prompt_and_parses_result(projects_table, style_examples_table, make_posting):
    projects_table.put_item(Item={"project_id": "p1", "title": "ML thing", "description": "desc", "url": "https://x"})
    draft.ingest_style_example_embedding("ex1", "cold_outreach", "Hi, I loved building ML systems...", embed_fn=_fake_embed)

    posting = make_posting(title="ML Engineer", description_text="We need someone great at ml")

    def fake_llm(system, user):
        assert "ML thing" in user
        assert "Be casual" in user
        return {
            "draft_text": "Hi! Excited about this ML role...",
            "projects_referenced": ["p1"],
            "confidence_notes": "strong fit",
        }

    result = draft.draft_application(
        posting,
        [ProjectMatch(project_id="p1", title="ML thing", similarity=0.9)],
        "Be casual and enthusiastic",
        llm_invoke=fake_llm,
        embed_fn=_fake_embed,
    )
    assert result.draft_text == "Hi! Excited about this ML role..."
    assert result.projects_referenced == ["p1"]
    assert result.confidence_notes == "strong fit"
