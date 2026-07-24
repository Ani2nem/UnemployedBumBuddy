from __future__ import annotations

from pipeline import research


def _fake_search(company, is_startup):
    return [
        {
            "organic": [
                {
                    "title": f"{company} raises funding",
                    "snippet": f"{company} raised a seed round",
                    "link": "https://example.com/news",
                }
            ]
        }
    ]


def _fake_llm(system, user):
    return {"brief_text": "Acme is a promising startup.", "tone_guidance": "Be casual and enthusiastic."}


def test_research_company_cache_miss_calls_search_and_llm(company_research_cache_table):
    result = research.research_company(
        "Acme Corp", is_startup=True, search_fn=_fake_search, llm_invoke=_fake_llm
    )
    assert result.cached is False
    assert result.brief_text == "Acme is a promising startup."
    assert result.sources == ["https://example.com/news"]


def test_research_company_second_call_hits_cache(company_research_cache_table):
    research.research_company("Acme Corp", is_startup=True, search_fn=_fake_search, llm_invoke=_fake_llm)

    calls = {"search": 0, "llm": 0}

    def counting_search(company, is_startup):
        calls["search"] += 1
        return _fake_search(company, is_startup)

    def counting_llm(system, user):
        calls["llm"] += 1
        return _fake_llm(system, user)

    result = research.research_company(
        "Acme Corp", is_startup=True, search_fn=counting_search, llm_invoke=counting_llm
    )
    assert result.cached is True
    assert calls == {"search": 0, "llm": 0}


def test_research_company_force_refresh_bypasses_cache(company_research_cache_table):
    research.research_company("Acme Corp", is_startup=True, search_fn=_fake_search, llm_invoke=_fake_llm)

    calls = {"n": 0}

    def counting_search(company, is_startup):
        calls["n"] += 1
        return _fake_search(company, is_startup)

    research.research_company(
        "Acme Corp", is_startup=True, force_refresh=True, search_fn=counting_search, llm_invoke=_fake_llm
    )
    assert calls["n"] == 1


def test_research_company_different_companies_dont_collide(company_research_cache_table):
    r1 = research.research_company("Acme Corp", search_fn=_fake_search, llm_invoke=_fake_llm)
    r2 = research.research_company("Beta Inc", search_fn=_fake_search, llm_invoke=_fake_llm)
    assert r1.company_normalized != r2.company_normalized


def test_build_search_queries_differ_for_startup_vs_big_company():
    startup_queries = research.build_search_queries("Acme", is_startup=True)
    big_co_queries = research.build_search_queries("Acme", is_startup=False)
    assert any("funding" in q for q in startup_queries)
    assert any("news" in q for q in big_co_queries)
