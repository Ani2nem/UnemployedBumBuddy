"""Titan Text Embeddings V2 wrapper + cosine similarity.

Shared by `project_match.py` (JD-to-project matching) and
`visa_level_filter.py` (LCA filing title-similarity, when `pw_wage_level`
alone is ambiguous) so both use one embedding call convention and one
similarity function.
"""

from __future__ import annotations

import functools
import json

import boto3
import numpy as np

from pipeline.config import TITAN_EMBED_DIMENSIONS, TITAN_EMBED_MODEL_ID


@functools.lru_cache(maxsize=1)
def _client():
    return boto3.client("bedrock-runtime")


def embed_text(text: str) -> list[float]:
    """Embed a single string with Titan Text Embeddings V2.

    Requests unit-normalized output so cosine similarity reduces to a dot
    product - callers doing many comparisons against a fixed corpus (project
    matching) can rely on that if they want to skip the norm division.
    """
    body = {
        "inputText": text,
        "dimensions": TITAN_EMBED_DIMENSIONS,
        "normalize": True,
    }
    response = _client().invoke_model(
        modelId=TITAN_EMBED_MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(response["body"].read())
    return payload["embedding"]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    vec_a = np.asarray(a, dtype=np.float64)
    vec_b = np.asarray(b, dtype=np.float64)
    denom = np.linalg.norm(vec_a) * np.linalg.norm(vec_b)
    if denom == 0.0:
        return 0.0
    return float(np.dot(vec_a, vec_b) / denom)
