"""Bedrock Nova Lite wrapper for the pipeline's structured-JSON LLM calls.

Every pipeline call site (ambiguous visa/level judgment, research brief +
tone, draft generation) wants the same shape: a system prompt, a user
prompt, and a JSON object back. Centralizing the Converse API call and JSON
extraction here keeps each call site down to "build the prompt, parse the
typed result."
"""

from __future__ import annotations

import functools
import json
import re

import boto3

from pipeline.config import NOVA_LITE_MODEL_ID

_JSON_BLOCK_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


class LlmJsonParseError(RuntimeError):
    def __init__(self, raw_text: str):
        super().__init__(f"Nova Lite response was not valid JSON: {raw_text!r}")
        self.raw_text = raw_text


@functools.lru_cache(maxsize=1)
def _client():
    return boto3.client("bedrock-runtime")


def invoke_json(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.3,
) -> dict:
    """Call Nova Lite via the Converse API and parse a JSON object response.

    Instructs the model (via the system prompt convention below) to return
    only a JSON object; extracts the first `{...}` block from the response
    text and parses it. Raises `LlmJsonParseError` if no valid JSON is found
    so callers fail loudly instead of silently treating garbage as a result.
    """
    response = _client().converse(
        modelId=NOVA_LITE_MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    content_blocks = response["output"]["message"]["content"]
    raw_text = "".join(block.get("text", "") for block in content_blocks)

    match = _JSON_BLOCK_PATTERN.search(raw_text)
    if match is None:
        raise LlmJsonParseError(raw_text)
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LlmJsonParseError(raw_text) from exc
