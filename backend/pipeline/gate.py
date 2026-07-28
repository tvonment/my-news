"""The embedding gate — the cost valve of the whole pipeline.

Everything harvested gets embedded (~$0.02 per 1M tokens, negligible). Only what
clears this gate reaches the LLM, and only the top slice of *that* gets
summarized. Three funnels, each narrower than the last; widening this one is the
single biggest lever on the monthly bill.

Scoring is deliberately cheap arithmetic rather than a model call:

    score = w_interest * max similarity to any interest statement
          + w_liked    * similarity to the liked centroid
          - w_disliked * similarity to the disliked centroid
          + recency bonus

The centroids are incremental means over the embeddings of articles voted on, so
a vote changes the next run's ranking immediately with no LLM involved.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from core import cosmos, foundry
from core.budget import Usage
from core.config import get_settings
from core.models import USER_PK, utcnow

log = logging.getLogger(__name__)

INTERESTS_DOC_ID = "interests"
CENTROIDS_DOC_ID = "centroids"

W_INTEREST = 1.0
W_LIKED = 0.6
W_DISLIKED = 0.8
# Half-life in hours for the recency bonus. News decays fast; a two-day-old
# story needs to be clearly better than a fresh one to earn a slot.
RECENCY_HALF_LIFE_H = 18.0
W_RECENCY = 0.25


@dataclass
class GateResult:
    scored: int = 0
    admitted: int = 0
    embedding_tokens: int = 0


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return matrix / np.clip(norms, 1e-12, None)


def _statements_hash(statements: list[str]) -> str:
    return hashlib.sha256(" ".join(statements).encode("utf-8")).hexdigest()[:16]


def load_interest_vectors() -> tuple[np.ndarray | None, int]:
    """Embed the interest statements, caching the vectors on the config document.

    Re-embedding a dozen short statements on every run would be cheap but
    pointless; the cache is invalidated by hashing the statements themselves, so
    editing them in the app takes effect on the next run with no extra plumbing.
    """
    doc = cosmos.read(INTERESTS_DOC_ID, USER_PK)
    if not doc:
        return None, 0

    statements: list[str] = [s for s in doc.get("statements", []) if s.strip()]
    if not statements:
        return None, 0

    digest = _statements_hash(statements)
    cached = doc.get("vectors")
    if cached and doc.get("vectors_hash") == digest:
        return _normalize(np.asarray(cached, dtype=np.float32)), 0

    vectors, tokens = foundry.embed(statements)
    doc["vectors"] = vectors
    doc["vectors_hash"] = digest
    doc["vectors_updated_at"] = utcnow().isoformat()
    cosmos.upsert(doc)
    log.info("Re-embedded %d interest statements (%d tokens)", len(statements), tokens)
    return _normalize(np.asarray(vectors, dtype=np.float32)), tokens


def load_centroids() -> tuple[np.ndarray | None, np.ndarray | None]:
    doc = cosmos.read(CENTROIDS_DOC_ID, USER_PK)
    if not doc:
        return None, None

    def vec(key: str) -> np.ndarray | None:
        raw = doc.get(key)
        if not raw:
            return None
        return _normalize(np.asarray(raw, dtype=np.float32))

    return vec("liked"), vec("disliked")


def _recency_bonus(published_at: str, now: datetime) -> float:
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    age_h = max(0.0, (now - published).total_seconds() / 3600.0)
    return float(0.5 ** (age_h / RECENCY_HALF_LIFE_H))


def load_candidates(days_back: int = 3, limit: int = 1500) -> list[dict[str, Any]]:
    """Harvested-but-not-yet-gated articles from the last few day partitions.

    Queried per day partition rather than cross-partition: articles are bucketed
    by day precisely so this stays a small number of single-partition reads.
    """
    now = utcnow()
    candidates: list[dict[str, Any]] = []
    for offset in range(days_back):
        pk = f"art|{(now - timedelta(days=offset)):%Y-%m-%d}"
        rows = cosmos.query(
            "SELECT * FROM c WHERE c.type = 'article' AND c.state = 'harvested' "
            "ORDER BY c.published_at DESC OFFSET 0 LIMIT @limit",
            [{"name": "@limit", "value": limit}],
            partition_key=pk,
        )
        candidates.extend(rows)
        if len(candidates) >= limit:
            break
    return candidates[:limit]


def run_gate(candidates: list[dict[str, Any]] | None = None) -> tuple[GateResult, list[dict[str, Any]]]:
    """Embed and rank candidates, returning those admitted to the LLM stage."""
    settings = get_settings()
    now = utcnow()
    result = GateResult()

    articles = candidates if candidates is not None else load_candidates()
    if not articles:
        return result, []

    interest_vectors, interest_tokens = load_interest_vectors()
    liked, disliked = load_centroids()
    result.embedding_tokens += interest_tokens

    texts = [f"{a.get('title', '')}\n{a.get('lead', '')[:600]}" for a in articles]
    vectors: list[list[float]] = []
    for batch in foundry.batched(texts, 96):
        batch_vectors, tokens = foundry.embed(batch)
        vectors.extend(batch_vectors)
        result.embedding_tokens += tokens

    matrix = _normalize(np.asarray(vectors, dtype=np.float32))
    scores = np.zeros(len(articles), dtype=np.float32)

    if interest_vectors is not None:
        # Best-matching interest, not the mean: an article about exactly one of
        # several interests should score high, not be diluted by the others.
        scores += W_INTEREST * (matrix @ interest_vectors.T).max(axis=1)
    if liked is not None:
        scores += W_LIKED * (matrix @ liked)
    if disliked is not None:
        scores -= W_DISLIKED * (matrix @ disliked)

    recency = np.asarray([_recency_bonus(a.get("published_at", ""), now) for a in articles], dtype=np.float32)
    scores += W_RECENCY * recency

    order = np.argsort(-scores)
    admitted: list[dict[str, Any]] = []
    quota = settings.max_scored_per_run
    explore_slots = int(quota * settings.exploration_quota)

    # Keep a slice for items the profile would reject, so a narrowing feed can
    # still surface a genuinely new interest. Without this the centroids become
    # self-reinforcing and the feed collapses onto whatever was liked first.
    ranked = list(order[: quota - explore_slots])
    rest = list(order[quota - explore_slots :])
    if explore_slots and rest:
        stride = max(1, len(rest) // explore_slots)
        ranked.extend(rest[::stride][:explore_slots])

    for index in ranked:
        article = articles[index]
        article["embedding"] = [float(x) for x in matrix[index]]
        article["gate_score"] = float(scores[index])
        article["state"] = "gated"
        cosmos.upsert(article)
        admitted.append(article)

    result.scored = len(articles)
    result.admitted = len(admitted)
    log.info(
        "Gate: embedded %d, admitted %d (%d exploration), %d embedding tokens",
        result.scored,
        result.admitted,
        min(explore_slots, max(0, len(rest))),
        result.embedding_tokens,
    )
    return result, admitted


def update_centroid(signal: str, embedding: list[float]) -> None:
    """Fold one vote into the running mean for its signal.

    Stored as a mean plus a count so the update is O(1) and never needs to
    re-read the voting history.
    """
    doc = cosmos.read(CENTROIDS_DOC_ID, USER_PK) or {
        "id": CENTROIDS_DOC_ID,
        "pk": USER_PK,
        "type": "config",
    }
    key = "liked" if signal == "like" else "disliked"
    count_key = f"{key}_count"

    current = doc.get(key)
    count = int(doc.get(count_key, 0))
    new = np.asarray(embedding, dtype=np.float32)

    if current is None or count == 0:
        updated = new
        count = 1
    else:
        mean = np.asarray(current, dtype=np.float32)
        count += 1
        updated = mean + (new - mean) / count

    doc[key] = [float(x) for x in updated]
    doc[count_key] = count
    doc["updated_at"] = utcnow().isoformat()
    cosmos.upsert(doc)
