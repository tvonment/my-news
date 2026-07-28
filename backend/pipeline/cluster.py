"""Group articles that are the same story.

When Apple announces something, five outlets cover it and you currently get five
cards. The embeddings needed to detect that are already stored on each article
from the gate stage, so clustering costs arithmetic and no tokens.

The threshold is the whole design. Too low and unrelated stories on the same
topic collapse into one; too high and near-identical wire copy stays duplicated.
0.80 on title+lead embeddings is the middle of the range NewsCatcher documents
for exactly this job (0.6 diverse / 0.7 balanced / 0.8 tight), and it is
deliberately at the tight end here: wrongly merging two distinct stories hides
one of them completely, while wrongly splitting them merely repeats — the
failure modes are not symmetric.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from core.config import get_settings

log = logging.getLogger(__name__)

# Only cluster stories published near each other. Two articles can be textually
# similar because they cover the same recurring subject a month apart — that is
# a topic, not a story, and merging them would hide the newer one.
MAX_CLUSTER_AGE_DIFF_HOURS = 72


@dataclass
class Cluster:
    primary: dict[str, Any]
    members: list[dict[str, Any]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return 1 + len(self.members)

    @property
    def other_sources(self) -> list[str]:
        seen: list[str] = []
        for m in self.members:
            name = m.get("source_name", "")
            if name and name != self.primary.get("source_name") and name not in seen:
                seen.append(name)
        return seen

    def all_articles(self) -> list[dict[str, Any]]:
        return [self.primary, *self.members]


def _hours_apart(a: dict[str, Any], b: dict[str, Any]) -> float:
    from datetime import datetime

    def parse(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    ta, tb = parse(a.get("published_at")), parse(b.get("published_at"))
    if ta is None or tb is None:
        return 0.0
    return abs((ta - tb).total_seconds()) / 3600.0


def cluster_articles(
    articles: list[dict[str, Any]], threshold: float | None = None
) -> list[Cluster]:
    """Greedy single-pass clustering, highest relevance first.

    Seeding from the best-scored article means the primary of each cluster is
    the version the ranking already preferred, so the reader gets the outlet
    that covered it best rather than whichever happened to be processed first.
    """
    threshold = threshold if threshold is not None else get_settings().cluster_threshold

    usable = [a for a in articles if a.get("embedding")]
    if len(usable) < 2:
        return [Cluster(primary=a) for a in articles]

    order = sorted(usable, key=lambda a: -(a.get("relevance") or a.get("gate_score") or 0))
    matrix = np.asarray([a["embedding"] for a in order], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.clip(norms, 1e-12, None)
    similarity = matrix @ matrix.T

    assigned = [False] * len(order)
    clusters: list[Cluster] = []

    for i, article in enumerate(order):
        if assigned[i]:
            continue
        assigned[i] = True
        cluster = Cluster(primary=article)
        for j in range(i + 1, len(order)):
            if assigned[j]:
                continue
            if similarity[i, j] < threshold:
                continue
            if _hours_apart(article, order[j]) > MAX_CLUSTER_AGE_DIFF_HOURS:
                continue
            assigned[j] = True
            cluster.members.append(order[j])
        clusters.append(cluster)

    # Articles with no embedding never join a cluster, but must not vanish.
    clusters.extend(Cluster(primary=a) for a in articles if not a.get("embedding"))

    merged = sum(1 for c in clusters if c.size > 1)
    if merged:
        log.info(
            "Clustered %d articles into %d groups (%d multi-source, largest %d)",
            len(articles),
            len(clusters),
            merged,
            max(c.size for c in clusters),
        )
    return clusters


def apply_cluster_metadata(cluster: Cluster) -> None:
    """Stamp cluster identity onto every member.

    Non-primary members keep their own record and stay searchable; they are
    simply collapsed in the feed rather than deleted, so nothing is lost.
    """
    primary_id = cluster.primary["id"]
    cluster.primary["cluster_id"] = primary_id
    cluster.primary["also_reported_by"] = cluster.other_sources
    cluster.primary["cluster_size"] = cluster.size
    for member in cluster.members:
        member["cluster_id"] = primary_id
        member["also_reported_by"] = []
        member["cluster_size"] = cluster.size
