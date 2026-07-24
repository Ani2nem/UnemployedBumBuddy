from __future__ import annotations

from pipeline import project_match as pm

_VOCAB = ("ml", "web", "infra")


def _fake_embed_project(title, description):
    text = f"{title} {description}".lower()
    return [1.0 if word in text else 0.0 for word in _VOCAB]


def _fake_embed_jd(text):
    text = text.lower()
    return [1.0 if word in text else 0.0 for word in _VOCAB]


def test_ingest_and_load_round_trips_embedding(projects_table):
    pm.ingest_project_embedding("p1", "ML Recommender", "A machine learning ml system", embed_fn=_fake_embed_project)

    corpus = pm.load_project_embeddings()
    assert len(corpus) == 1
    assert corpus[0].project_id == "p1"
    assert corpus[0].embedding == [1.0, 0.0, 0.0]


def test_match_projects_ranks_by_similarity_above_threshold(projects_table):
    pm.ingest_project_embedding("p1", "ML Recommender", "machine learning ml system", embed_fn=_fake_embed_project)
    pm.ingest_project_embedding("p2", "Web App", "full stack web application", embed_fn=_fake_embed_project)
    pm.ingest_project_embedding("p3", "Infra Tool", "infra automation tool", embed_fn=_fake_embed_project)

    matches = pm.match_projects(
        "Looking for an ml engineer to build recommendation systems",
        embed_fn=_fake_embed_jd,
        threshold=0.5,
    )
    assert [m.project_id for m in matches] == ["p1"]


def test_match_projects_returns_empty_below_threshold(projects_table):
    pm.ingest_project_embedding("p1", "Web App", "full stack web application", embed_fn=_fake_embed_project)

    matches = pm.match_projects("ml recommendation engine", embed_fn=_fake_embed_jd, threshold=0.9)
    assert matches == []


def test_match_projects_empty_corpus_returns_empty(projects_table):
    assert pm.match_projects("anything", embed_fn=_fake_embed_jd) == []


def test_match_projects_respects_top_k(projects_table):
    for i in range(5):
        pm.ingest_project_embedding(f"p{i}", "ML thing", "machine learning ml", embed_fn=_fake_embed_project)

    matches = pm.match_projects("ml project", embed_fn=_fake_embed_jd, threshold=0.5, top_k=2)
    assert len(matches) == 2
