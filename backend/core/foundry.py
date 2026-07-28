"""Foundry / Azure OpenAI access via the v1 API.

Deliberately the plain ``OpenAI`` client against ``{endpoint}/openai/v1/`` rather
than ``AzureOpenAI``. The v1 API is GA and version-agnostic: no dated
``api-version`` to bump every month, and the client handles Entra token refresh
itself when ``api_key`` is a token provider.

Also deliberately NOT the Azure Functions OpenAI binding extension — that has
been in preview since 2024, its packages have never left ``0.x-alpha``, and on
Python it would force the whole app onto the preview extension bundle.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any, Iterable

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import OpenAI

from core.budget import LONG_CONTEXT_THRESHOLD, Usage
from core.config import get_settings, require

log = logging.getLogger(__name__)

# The v1 API documents this scope for Entra ID auth. The older
# `https://cognitiveservices.azure.com/.default` also exists; if a token is ever
# rejected, this is the first thing to flip.
TOKEN_SCOPE = os.environ.get("FOUNDRY_TOKEN_SCOPE", "https://ai.azure.com/.default")


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    settings = get_settings()
    endpoint = require(settings.foundry_endpoint, "FOUNDRY_ENDPOINT")
    token_provider = get_bearer_token_provider(DefaultAzureCredential(), TOKEN_SCOPE)
    return OpenAI(
        base_url=f"{endpoint.rstrip('/')}/openai/v1/",
        api_key=token_provider,
        max_retries=3,
        timeout=120.0,
    )


def _usage_from(response: Any) -> Usage:
    """Pull token counts off a Responses API result.

    ``output_tokens`` includes reasoning tokens, which are hidden but billed at
    the full output rate — the single easiest way to multiply this project's
    bill is to leave ``reasoning.effort`` unset.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage()

    input_details = getattr(usage, "input_tokens_details", None)
    cached = int(getattr(input_details, "cached_tokens", 0) or 0)
    total_input = int(getattr(usage, "input_tokens", 0) or 0)

    return Usage(
        input_tokens=max(0, total_input - cached),
        cached_input_tokens=cached,
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        # Decided here, from this request's own prompt size — the only place
        # that information exists.
        long_context=total_input > LONG_CONTEXT_THRESHOLD,
    )


def _output_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return text
    # Fall back to walking the output items when the convenience accessor is absent.
    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            piece = getattr(content, "text", None)
            if piece:
                chunks.append(piece)
    return "".join(chunks)


def structured_call(
    *,
    deployment: str,
    system_prompt: str,
    user_content: str,
    schema_name: str,
    schema: dict[str, Any],
    max_output_tokens: int = 4096,
    reasoning_effort: str = "none",
) -> tuple[dict[str, Any], Usage]:
    """One strict-JSON-schema call, returning the parsed object and its usage.

    ``reasoning_effort='none'`` is the default on purpose. Every gpt-5.x model is
    a reasoning model and reasoning tokens bill at the output rate; on a
    55-token answer, leaving the default in place can multiply the output leg
    several-fold for no benefit on a classification task.

    The system prompt goes first and unchanged so it can act as a cacheable
    prefix — everything volatile belongs in ``user_content``.
    """
    response = get_client().responses.create(
        model=deployment,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
        reasoning={"effort": reasoning_effort},
        # Reasoning models reject `max_tokens`; the Responses API wants this name.
        max_output_tokens=max_output_tokens,
    )

    raw = _output_text(response)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.error("Model returned non-JSON despite strict schema: %.400s", raw)
        raise

    return parsed, _usage_from(response)


def embed(texts: list[str]) -> tuple[list[list[float]], int]:
    """Embed a batch, returning vectors and the token count.

    Truncated to ``embedding_dimensions`` via Matryoshka. This MUST match the
    container's vector policy — and that policy cannot be edited in place.
    """
    if not texts:
        return [], 0

    settings = get_settings()
    response = get_client().embeddings.create(
        model=settings.model_embedding,
        input=texts,
        dimensions=settings.embedding_dimensions,
    )
    vectors = [item.embedding for item in response.data]
    tokens = int(getattr(response.usage, "total_tokens", 0) or 0)
    return vectors, tokens


def batched(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
