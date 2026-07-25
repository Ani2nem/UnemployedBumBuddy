"""Project-to-JD matching via Titan Text Embeddings V2 cosine similarity.

Projects are embedded once at ingestion time and the embedding is stored
back on the `Projects` item, so matching at JD-time only costs one embedding
call (the JD) plus an in-memory cosine-similarity scan - no per-project LLM
call and no vector DB, per docs/ARCHITECTURE.md "Draft generation".
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from pipeline import embeddings
from pipeline.config import PROJECT_MATCH_SIMILARITY_THRESHOLD, PROJECT_MATCH_TOP_K
from pipeline.dynamo import get_table
from pipeline.models import ProjectEmbedding, ProjectMatch
from shared.tables import PROJECTS_TABLE


def embed_project_text(title: str, description: str) -> list[float]:
    return embeddings.embed_text(f"{title}\n\n{description}")


def ingest_project_embedding(
    project_id: str,
    title: str,
    description: str,
    *,
    embed_fn: Callable[[str, str], list[float]] = embed_project_text,
) -> ProjectEmbedding:
    """Embed a project once and persist the embedding on its `Projects` item.

    Call this whenever a project is added or its description changes - not
    per JD match, keeping embedding cost proportional to the (small,
    slow-growing) project corpus rather than to job volume.
    """
    embedding = embed_fn(title, description)
    get_table(PROJECTS_TABLE).update_item(
        Key={"project_id": project_id},
        UpdateExpression="SET title = :title, embedding = :embedding",
        # DynamoDB's resource API rejects native Python floats - Decimal(str(x))
        # avoids the binary-float-to-Decimal precision artifacts a plain
        # Decimal(x) conversion would introduce.
        ExpressionAttributeValues={
            ":title": title,
            ":embedding": [Decimal(str(x)) for x in embedding],
        },
    )
    return ProjectEmbedding(project_id=project_id, title=title, embedding=embedding)


def load_project_embeddings() -> list[ProjectEmbedding]:
    """Full scan of `Projects` - the corpus is small (a growing personal
    portfolio, not a general catalog), so this is cheap and avoids needing a
    vector DB or a GSI.
    """
    table = get_table(PROJECTS_TABLE)
    items: list[dict] = []
    response = table.scan()
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    return [
        ProjectEmbedding(
            project_id=item["project_id"],
            title=item.get("title", ""),
            embedding=[float(x) for x in item["embedding"]],
        )
        for item in items
        if "embedding" in item
    ]


def match_projects(
    jd_text: str,
    *,
    project_embeddings: list[ProjectEmbedding] | None = None,
    top_k: int = PROJECT_MATCH_TOP_K,
    threshold: float = PROJECT_MATCH_SIMILARITY_THRESHOLD,
    embed_fn: Callable[[str], list[float]] = embeddings.embed_text,
) -> list[ProjectMatch]:
    """Top `top_k` projects above `threshold` cosine similarity to `jd_text`.

    Pass `project_embeddings` to reuse a corpus already loaded elsewhere in
    the same batch (e.g. a Step Functions Map iteration processing many jobs
    per invocation) instead of re-scanning `Projects` per job.
    """
    corpus = project_embeddings if project_embeddings is not None else load_project_embeddings()
    if not corpus:
        return []

    jd_embedding = embed_fn(jd_text)
    scored = [
        ProjectMatch(
            project_id=project.project_id,
            title=project.title,
            similarity=embeddings.cosine_similarity(jd_embedding, project.embedding),
        )
        for project in corpus
    ]
    scored.sort(key=lambda match: match.similarity, reverse=True)
    above_threshold = [match for match in scored if match.similarity >= threshold]
    return above_threshold[:top_k]


# --- Lambda entrypoint (Step Functions "MatchProject") ---


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Event: `{"job": JobPosting dict, "research": {...}}` (research is
    unused here - matching is JD-text-only - but present since the ASL passes
    it through uniformly to every per-candidate stage).
    """
    job = event["job"]
    matches = match_projects(job["description_text"])
    return {
        "matches": [
            {"project_id": m.project_id, "title": m.title, "similarity": m.similarity}
            for m in matches
        ]
    }
