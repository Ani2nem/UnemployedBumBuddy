"""Draft generation: one Nova Lite call, few-shot from `StyleExamples`.

Retrieves 2-3 relevant style examples (tag-filtered when a scenario tag is
given, otherwise/also ranked by embedding similarity to the JD - same
embed-once-at-ingestion pattern as `project_match.py`), then combines them
with the JD, the chosen project(s)/links, and the research stage's tone
guidance into a single structured drafting call. See docs/ARCHITECTURE.md
"Draft generation".
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from pipeline import embeddings, llm
from pipeline.dynamo import get_table
from pipeline.models import DraftResult, ProjectMatch, StyleExample
from shared.contracts import JobPosting
from shared.serialize import job_posting_from_dict
from shared.tables import PROJECTS_TABLE, STYLE_EXAMPLES_TABLE

DEFAULT_STYLE_EXAMPLE_COUNT = 3


# --- StyleExamples retrieval ---


def ingest_style_example_embedding(
    example_id: str,
    scenario_tag: str,
    text: str,
    *,
    embed_fn: Callable[[str], list[float]] = embeddings.embed_text,
) -> StyleExample:
    """Embed a style example once and persist it, mirroring
    `project_match.ingest_project_embedding` - call at ingestion time, not
    per draft.
    """
    embedding = embed_fn(text)
    get_table(STYLE_EXAMPLES_TABLE).update_item(
        Key={"example_id": example_id},
        UpdateExpression="SET scenario_tag = :tag, #text = :text, embedding = :embedding",
        ExpressionAttributeNames={"#text": "text"},
        ExpressionAttributeValues={
            ":tag": scenario_tag,
            ":text": text,
            ":embedding": [Decimal(str(x)) for x in embedding],
        },
    )
    return StyleExample(example_id=example_id, scenario_tag=scenario_tag, text=text, embedding=embedding)


def load_style_examples() -> list[StyleExample]:
    """Full scan of `StyleExamples` - small personal corpus, same
    justification as `project_match.load_project_embeddings`.
    """
    table = get_table(STYLE_EXAMPLES_TABLE)
    items: list[dict] = []
    response = table.scan()
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    return [
        StyleExample(
            example_id=item["example_id"],
            scenario_tag=item.get("scenario_tag", ""),
            text=item.get("text", ""),
            embedding=[float(x) for x in item["embedding"]] if "embedding" in item else None,
        )
        for item in items
    ]


def select_style_examples(
    jd_text: str,
    *,
    scenario_tag: str | None = None,
    top_k: int = DEFAULT_STYLE_EXAMPLE_COUNT,
    examples: list[StyleExample] | None = None,
    embed_fn: Callable[[str], list[float]] = embeddings.embed_text,
) -> list[StyleExample]:
    """Pick up to `top_k` style examples for few-shot drafting.

    Narrows to `scenario_tag` first when given and any examples carry that
    tag; ranks the remaining pool by embedding similarity to `jd_text` when
    embeddings are available, otherwise falls back to the first `top_k` in
    whatever order they were loaded.
    """
    pool = examples if examples is not None else load_style_examples()
    if scenario_tag is not None:
        tagged = [ex for ex in pool if ex.scenario_tag == scenario_tag]
        if tagged:
            pool = tagged

    embedded = [ex for ex in pool if ex.embedding is not None]
    if not embedded:
        return pool[:top_k]

    jd_embedding = embed_fn(jd_text)
    scored = sorted(
        embedded,
        key=lambda ex: embeddings.cosine_similarity(jd_embedding, ex.embedding),
        reverse=True,
    )
    return scored[:top_k]


# --- Project detail lookup (bridges project_match.py's ProjectMatch -> full detail) ---


def fetch_project_details(project_ids: list[str]) -> list[dict]:
    table = get_table(PROJECTS_TABLE)
    details = []
    for project_id in project_ids:
        item = table.get_item(Key={"project_id": project_id}).get("Item")
        if item is not None:
            details.append(item)
    return details


# --- Nova Lite drafting call ---

_DRAFT_SYSTEM_PROMPT = (
    "You are drafting a job application/outreach message in the candidate's own "
    "voice, based on style examples of their past writing. Respond with ONLY a "
    'JSON object of the form {"draft_text": "...", "projects_referenced": '
    '["project_id", ...], "confidence_notes": "..."}. '
    "draft_text: the full application/outreach message, written to sound like the "
    "candidate - match the style examples' phrasing, structure, and tone - tailored "
    "to the job description, adjusted for the given company tone guidance, and "
    "naturally referencing the given project(s), not just listing them. "
    "projects_referenced: which of the given project ids were actually mentioned in "
    "draft_text. "
    "confidence_notes: one sentence flagging anything uncertain (e.g. weak project "
    "fit, missing info) the human reviewer should know before approving."
)


def _build_draft_user_prompt(
    posting: JobPosting,
    projects: list[dict],
    tone_guidance: str,
    style_examples: list[StyleExample],
    *,
    previous_draft: str | None = None,
    edit_feedback: str | None = None,
) -> str:
    projects_block = "\n".join(
        f"- {p['project_id']}: {p.get('title', '')} - {p.get('description', '')} "
        f"({p.get('url', p.get('link', ''))})"
        for p in projects
    )
    examples_block = "\n\n".join(
        f"Style example {i + 1} ({example.scenario_tag}):\n{example.text}"
        for i, example in enumerate(style_examples)
    )
    revision_block = (
        f"\n\nThis is a REVISION. The candidate rejected the previous draft below and asked for "
        f"changes - apply their feedback, don't just rephrase the same draft.\n"
        f"Previous draft:\n{previous_draft}\n\n"
        f"Candidate's edit instructions:\n{edit_feedback}"
        if previous_draft is not None
        else ""
    )
    return (
        f"Job title: {posting.title}\n"
        f"Company: {posting.company}\n"
        f"Job description:\n{posting.description_text[:4000]}\n\n"
        f"Tone guidance for this company: {tone_guidance}\n\n"
        f"Candidate's relevant projects:\n{projects_block or '(none matched)'}\n\n"
        f"Style examples of the candidate's own past writing:\n{examples_block or '(none available)'}"
        f"{revision_block}"
    )


def draft_application(
    posting: JobPosting,
    project_matches: list[ProjectMatch],
    tone_guidance: str,
    *,
    scenario_tag: str | None = None,
    project_details: list[dict] | None = None,
    style_examples: list[StyleExample] | None = None,
    previous_draft: str | None = None,
    edit_feedback: str | None = None,
    llm_invoke: Callable[[str, str], dict] = llm.invoke_json,
    embed_fn: Callable[[str], list[float]] = embeddings.embed_text,
) -> DraftResult:
    """`previous_draft`/`edit_feedback` are set only on a revision (the ASL's
    `ReviseDraft` state calls this same function after a Telegram "Edit" tap) -
    the initial draft leaves both `None`.
    """
    projects = (
        project_details
        if project_details is not None
        else fetch_project_details([match.project_id for match in project_matches])
    )
    examples = (
        style_examples
        if style_examples is not None
        else select_style_examples(posting.description_text, scenario_tag=scenario_tag, embed_fn=embed_fn)
    )

    llm_result = llm_invoke(
        _DRAFT_SYSTEM_PROMPT,
        _build_draft_user_prompt(
            posting,
            projects,
            tone_guidance,
            examples,
            previous_draft=previous_draft,
            edit_feedback=edit_feedback,
        ),
    )
    return DraftResult(
        draft_text=llm_result.get("draft_text", ""),
        projects_referenced=list(llm_result.get("projects_referenced", [])),
        confidence_notes=llm_result.get("confidence_notes", ""),
    )


# --- Lambda entrypoint (Step Functions "GenerateDraft" and "ReviseDraft") ---


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Same function backs both ASL states - `GenerateDraft`'s event has no
    `previous_draft`/`edit_feedback`; `ReviseDraft`'s does (see the ASL's
    `ReviseDraft.Parameters`), which is exactly what `draft_application`'s
    revision path expects.
    """
    job = job_posting_from_dict(event["job"])
    research = event.get("research") or {}
    project_matches = [
        ProjectMatch(project_id=m["project_id"], title=m["title"], similarity=m["similarity"])
        for m in (event.get("project_match") or {}).get("matches", [])
    ]
    previous_draft = event.get("previous_draft") or {}

    result = draft_application(
        job,
        project_matches,
        research.get("tone_guidance", ""),
        previous_draft=previous_draft.get("draft_text"),
        edit_feedback=event.get("edit_feedback"),
    )
    return {
        "draft_text": result.draft_text,
        "projects_referenced": result.projects_referenced,
        "confidence_notes": result.confidence_notes,
    }
