"""Runtime configuration, read once from environment variables.

Everything that could plausibly need tuning without a code change lives here so
it can be moved to app settings. Model deployment names in particular are
config, not constants: Foundry runs OpenAI models on a 12-month GA lifecycle, so
this project will outlive at least one model swap.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


def require(value: str, name: str) -> str:
    """Validate a setting at the point of use, not at import time.

    Loading configuration must never fail for a setting the caller does not
    need — otherwise fetching a feed requires a Cosmos endpoint, and tooling
    like scripts/probe_feeds.py cannot run without a full Azure environment.
    """
    if not value:
        raise RuntimeError(
            f"Required setting {name!r} is missing. "
            "Locally it belongs in backend/local.settings.json; in Azure it is an app setting."
        )
    return value


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else float(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else int(raw)


@dataclass(frozen=True)
class Settings:
    cosmos_endpoint: str
    cosmos_database: str
    cosmos_container: str

    foundry_endpoint: str
    model_scorer: str
    model_summarizer: str
    model_embedding: str

    # 512 dims via the `dimensions` parameter (Matryoshka truncation). This MUST
    # match the container's vectorEmbeddingPolicy — a vector policy cannot be
    # edited in place, so a mismatch means recreating the container.
    embedding_dimensions: int

    # Abort LLM stages once the day's estimated spend crosses this. Azure OpenAI
    # has no hard spend limit, so this is the only in-process brake.
    daily_budget_usd: float

    # Relevance 0-100 from the scorer; at or above this an item gets summarized.
    summarize_threshold: int
    # Max items handed to the scorer per digest run, after the embedding gate.
    max_scored_per_run: int
    # Cosine similarity at or above which two articles are the same story.
    cluster_threshold: float
    # Fraction of feed slots reserved for items outside the taste profile, so
    # the feed cannot collapse onto a single topic.
    exploration_quota: float

    article_ttl_days: int
    user_agent: str
    push_enabled: bool

    # Per-host User-Agent overrides. developer.apple.com returns HTTP 500 to
    # curl, NetNewsWire and Chrome UAs — only a Safari UA gets 200.
    user_agent_overrides: dict[str, str] = field(default_factory=dict)


SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        cosmos_endpoint=_env("COSMOS_ENDPOINT"),
        cosmos_database=_env("COSMOS_DATABASE", "mynews"),
        cosmos_container=_env("COSMOS_CONTAINER", "items"),
        foundry_endpoint=_env("FOUNDRY_ENDPOINT"),
        model_scorer=_env("MODEL_SCORER", "scorer"),
        model_summarizer=_env("MODEL_SUMMARIZER", "summarizer"),
        model_embedding=_env("MODEL_EMBEDDING", "embedding"),
        embedding_dimensions=_int_env("EMBEDDING_DIMENSIONS", 512),
        daily_budget_usd=_float_env("DAILY_TOKEN_BUDGET_USD", 1.50),
        summarize_threshold=_int_env("SUMMARIZE_THRESHOLD", 65),
        max_scored_per_run=_int_env("MAX_SCORED_PER_RUN", 200),
        cluster_threshold=_float_env("CLUSTER_THRESHOLD", 0.80),
        exploration_quota=_float_env("EXPLORATION_QUOTA", 0.10),
        article_ttl_days=_int_env("ARTICLE_TTL_DAYS", 21),
        user_agent=_env(
            "USER_AGENT",
            # Deliberately honest and un-AI-sounding. derStandard, Spiegel, ORF,
            # Zeit and FAZ all Disallow ClaudeBot/GPTBot/anthropic-ai/CCBot by
            # name in robots.txt while leaving `User-agent: *` open.
            "MyNewsBot/0.1 (+https://github.com/thomas/mynews; personal reader)",
        ),
        push_enabled=_env("PUSH_ENABLED", "false").lower() == "true",
        user_agent_overrides={"developer.apple.com": SAFARI_UA},
    )
