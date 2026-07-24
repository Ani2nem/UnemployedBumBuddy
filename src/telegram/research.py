"""Serper + Bedrock Nova Lite helpers backing the async Q&A worker.

This is scoped to answering ad hoc Telegram questions about a job/company
already in front of the user - it is not the pipeline's per-job research
step (that belongs to feat/pipeline and is cached per-company there).
"""

from __future__ import annotations

import boto3
import httpx

from src.telegram import config

_bedrock = boto3.client("bedrock-runtime", region_name=config.BEDROCK_REGION)

_ANSWER_PROMPT = """You are helping a job applicant evaluate a specific opportunity over Telegram chat.
Answer the user's question in 2-4 short sentences, plain text (no markdown), grounded only in the \
search results below. If the results don't answer the question, say so plainly instead of guessing.

Job context:
{job_context}

Question: {question}

Search results:
{search_results}
"""


def search_web(query: str, num_results: int = 5) -> list[dict[str, str]]:
    response = httpx.post(
        config.SERPER_API_URL,
        json={"q": query, "num": num_results},
        headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
        timeout=httpx.Timeout(10.0),
    )
    response.raise_for_status()
    body = response.json()
    return [
        {
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
            "link": item.get("link", ""),
        }
        for item in body.get("organic", [])[:num_results]
    ]


def _format_search_results(results: list[dict[str, str]]) -> str:
    if not results:
        return "(no search results)"
    return "\n".join(f"- {r['title']}: {r['snippet']} ({r['link']})" for r in results)


def answer_question(question: str, job_context: str, search_results: list[dict[str, str]]) -> str:
    prompt = _ANSWER_PROMPT.format(
        job_context=job_context or "(no specific job in context)",
        question=question,
        search_results=_format_search_results(search_results),
    )
    response = _bedrock.converse(
        modelId=config.BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 400, "temperature": 0.3},
    )
    return response["output"]["message"]["content"][0]["text"].strip()
