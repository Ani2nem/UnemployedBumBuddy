"""Company research: Serper search + one Nova Lite call, cached per company.

Cached in `CompanyResearchCache` keyed by `company_normalized` with a
`COMPANY_RESEARCH_CACHE_TTL_DAYS`-day TTL, since many postings share an
employer and research is per-company, not per-job (see docs/ARCHITECTURE.md
"Research pipeline").
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from pipeline import llm
from pipeline.config import COMPANY_RESEARCH_CACHE_TTL_DAYS
from pipeline.dynamo import get_table
from pipeline.employer_normalize import normalize_employer_name
from pipeline.models import ResearchResult
from shared.tables import COMPANY_RESEARCH_CACHE_TABLE

SERPER_SEARCH_URL = "https://google.serper.dev/search"


# --- Serper search ---


def build_search_queries(company: str, is_startup: bool) -> list[str]:
    """Query templates differ for startups (founder/funding-focused) vs big
    companies (team/culture/recent-news-focused), per the architecture doc.
    """
    if is_startup:
        return [
            f"{company} startup founders background",
            f"{company} funding round investors",
            f"{company} company mission product",
        ]
    return [
        f"{company} engineering team culture",
        f"{company} recent news",
    ]


def serper_search(query: str, *, client: httpx.Client) -> dict[str, Any]:
    api_key = os.environ["SERPER_API_KEY"]
    response = client.post(
        SERPER_SEARCH_URL,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query},
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()


def gather_search_results(company: str, is_startup: bool) -> list[dict[str, Any]]:
    queries = build_search_queries(company, is_startup)
    with httpx.Client() as client:
        return [serper_search(query, client=client) for query in queries]


def _summarize_search_results(search_results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for result in search_results:
        knowledge_graph = result.get("knowledgeGraph")
        if knowledge_graph:
            lines.append(
                f"Knowledge graph: {knowledge_graph.get('title', '')} - "
                f"{knowledge_graph.get('description', '')}"
            )
        for item in result.get("organic", [])[:5]:
            lines.append(f"- {item.get('title', '')}: {item.get('snippet', '')} ({item.get('link', '')})")
    return "\n".join(lines)


def _extract_sources(search_results: list[dict[str, Any]], limit: int = 5) -> list[str]:
    sources: list[str] = []
    for result in search_results:
        for item in result.get("organic", [])[:3]:
            link = item.get("link")
            if link and link not in sources:
                sources.append(link)
    return sources[:limit]


# --- Nova Lite brief + tone guidance ---

_RESEARCH_SYSTEM_PROMPT = (
    "You are a research assistant preparing a short company brief for a job "
    "applicant before they apply. Given raw web search snippets about a company, "
    'respond with ONLY a JSON object of the form {"brief_text": "...", '
    '"tone_guidance": "..."}. '
    "brief_text: a concise 3-5 sentence brief suitable for a Telegram message - "
    "what the company does, notable recent news or funding, and anything relevant "
    "to an applicant deciding whether to apply. "
    "tone_guidance: 1-2 sentences on how to tone-match an outreach message to this "
    "company (e.g. formal vs casual, mission-driven vs technical, scrappy startup "
    "vs enterprise polish)."
)


def _build_research_user_prompt(company: str, search_context: str) -> str:
    if not search_context:
        return f"Company: {company}\nNo search results were found for this company."
    return f"Company: {company}\nWeb search snippets:\n{search_context}"


# --- CompanyResearchCache ---


def _cache_key(company: str) -> str:
    return normalize_employer_name(company)


def get_cached_research(company: str) -> ResearchResult | None:
    item = get_table(COMPANY_RESEARCH_CACHE_TABLE).get_item(
        Key={"company_normalized": _cache_key(company)}
    ).get("Item")
    if item is None:
        return None
    # DynamoDB TTL deletion isn't instantaneous, so also check defensively here.
    expires_at = item.get("expires_at")
    if expires_at is not None and int(expires_at) < int(time.time()):
        return None
    return ResearchResult(
        company_normalized=item["company_normalized"],
        brief_text=item["brief_text"],
        tone_guidance=item["tone_guidance"],
        sources=list(item.get("sources", [])),
        cached=True,
    )


def put_cached_research(result: ResearchResult) -> None:
    now = int(time.time())
    get_table(COMPANY_RESEARCH_CACHE_TABLE).put_item(
        Item={
            "company_normalized": result.company_normalized,
            "brief_text": result.brief_text,
            "tone_guidance": result.tone_guidance,
            "sources": result.sources,
            "researched_at": now,
            # infra provisions this table's TTL attribute - must be named
            # "expires_at" to match (see feat/infra CDK stack).
            "expires_at": now + COMPANY_RESEARCH_CACHE_TTL_DAYS * 86400,
        }
    )


# --- Orchestration ---


def research_company(
    company: str,
    *,
    is_startup: bool = False,
    force_refresh: bool = False,
    search_fn: Callable[[str, bool], list[dict[str, Any]]] = gather_search_results,
    llm_invoke: Callable[[str, str], dict] = llm.invoke_json,
) -> ResearchResult:
    """Return a research brief + tone guidance for `company`, from cache when
    possible. Only spends a Serper + Nova Lite call on a cache miss.
    """
    if not force_refresh:
        cached = get_cached_research(company)
        if cached is not None:
            return cached

    search_results = search_fn(company, is_startup)
    search_context = _summarize_search_results(search_results)
    llm_result = llm_invoke(_RESEARCH_SYSTEM_PROMPT, _build_research_user_prompt(company, search_context))

    result = ResearchResult(
        company_normalized=_cache_key(company),
        brief_text=llm_result.get("brief_text", ""),
        tone_guidance=llm_result.get("tone_guidance", ""),
        sources=_extract_sources(search_results),
        cached=False,
    )
    put_cached_research(result)
    return result


# --- Lambda entrypoint (Step Functions "ResearchCompany") ---


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Event: `{"job": JobPosting dict}`.

    `is_startup` has no signal on `JobPosting` today, so this always uses the
    big-company query template (`build_search_queries`'s `is_startup=False`
    branch) - a startup-detection heuristic is a reasonable follow-up, not
    required for the pipeline to run.
    """
    job = event["job"]
    result = research_company(job["company"])
    return {
        "company_normalized": result.company_normalized,
        "brief_text": result.brief_text,
        "tone_guidance": result.tone_guidance,
        "sources": result.sources,
        "cached": result.cached,
    }
