"""Document shapes for the single Cosmos container.

One container, partition key ``/pk``, type-discriminated documents. Partition
keys are chosen so that no logical partition can grow without bound: articles are
bucketed by day, because 20 GB is a hard cap *per logical partition* and
repartitioning later would mean a full migration.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Literal

USER_PK = "user|me"
SOURCE_PK = "src"
RUN_PK = "run"

DocType = Literal["article", "config", "feedback", "source_state", "run"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def article_id(canonical_url: str) -> str:
    """Content-addressed id, so dedupe is a 409 on create rather than a query.

    base64**url** rather than plain base64: Cosmos strongly recommends
    alphanumeric-ASCII ids because ``/`` and ``+`` in an id hit known SDK bugs.
    """
    digest = hashlib.sha256(canonical_url.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def article_pk(published: datetime) -> str:
    """Day bucket. Keeps 'today's feed' a single-partition query — the cheapest
    kind — and bounds every logical partition to roughly one day of articles."""
    return f"art|{published.astimezone(timezone.utc):%Y-%m-%d}"


@dataclass
class Article:
    id: str
    pk: str
    type: DocType = field(default="article", init=False)

    url: str = ""
    canonical_url: str = ""
    source_id: str = ""
    source_name: str = ""
    title: str = ""
    lead: str = ""
    published_at: str = ""
    fetched_at: str = ""

    # Set by the gate stage.
    embedding: list[float] | None = None
    gate_score: float | None = None

    # Set by the score stage.
    relevance: int | None = None
    categories: list[str] = field(default_factory=list)
    why: str | None = None

    # Set by the summarize stage.
    summary: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    reading_minutes: int | None = None
    paywalled: bool = False

    # Set by the cluster stage. `cluster_id` is the id of the primary article.
    cluster_id: str | None = None
    also_reported_by: list[str] = field(default_factory=list)

    state: str = "harvested"  # harvested -> gated -> scored -> summarized
    ttl: int | None = None

    def to_doc(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["type"] = "article"
        # Cosmos rejects a null ttl; omit the property entirely to inherit the
        # container default (which is -1 = never expire unless set per item).
        if doc.get("ttl") is None:
            doc.pop("ttl", None)
        if doc.get("embedding") is None:
            doc.pop("embedding", None)
        return doc


@dataclass
class SourceState:
    """Per-feed fetch cursor.

    Both validators are stored because ETag is broken on a meaningful number of
    real feeds (returns 200 + full body instead of 304) and roughly half the
    large German feeds send neither. ``body_sha256`` is the fallback that makes
    those feeds cheap anyway: same hash means skip parsing.
    """

    id: str
    pk: str = SOURCE_PK
    type: DocType = field(default="source_state", init=False)

    resolved_url: str = ""
    etag: str | None = None
    last_modified: str | None = None
    body_sha256: str | None = None
    # None = untried, True = server honours If-None-Match, False = server returns
    # 200 + full body despite a matching ETag. Learned per source rather than
    # hardcoded, because the set of broken servers changes over time.
    etag_works: bool | None = None
    last_fetch_at: str | None = None
    last_success_at: str | None = None
    # Newest item date seen. Drives zombie-feed detection: news.microsoft.com/feed/
    # returns HTTP 200 with a fresh Last-Modified but has not published since
    # 2025-05-07, so HTTP status alone proves nothing.
    newest_item_at: str | None = None
    consecutive_failures: int = 0
    last_error: str | None = None

    def to_doc(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["type"] = "source_state"
        return doc


@dataclass
class RunRecord:
    """One pipeline run. Carries the real token counts so cost claims stay
    honest rather than aspirational."""

    id: str
    pk: str = RUN_PK
    type: DocType = field(default="run", init=False)

    kind: str = "harvest"  # harvest | digest
    started_at: str = ""
    finished_at: str | None = None
    ok: bool = True
    error: str | None = None

    feeds_polled: int = 0
    feeds_unchanged: int = 0
    feeds_failed: int = 0
    items_seen: int = 0
    items_new: int = 0
    items_gated: int = 0
    items_scored: int = 0
    items_summarized: int = 0

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    embedding_tokens: int = 0
    estimated_usd: float = 0.0
    budget_exhausted: bool = False

    ttl: int = 60 * 60 * 24 * 90  # 90 days

    def to_doc(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["type"] = "run"
        return doc
