"""The digest run: gate -> score -> summarize, under one shared budget.

Runs 3x/day, unlike harvest which runs every 30 minutes. All of the project's
model spend happens here, so the whole run shares a single DailyBudget: if the
ceiling is reached mid-run the remaining stages stop cleanly, the articles stay
in their current state, and the next run picks them up.
"""

from __future__ import annotations

import logging

from core import cosmos
from core.budget import DailyBudget
from core.models import USER_PK, RunRecord, iso, utcnow
from core.taxonomy import CATEGORIES, COUNTS_DOC_ID
from pipeline import cluster, gate, score, summarize

log = logging.getLogger(__name__)


def refresh_category_counts() -> dict[str, int]:
    """Recount the buckets and store them for `GET /api/categories` to point-read.

    Scoring is the only thing that assigns a category, so the counts can only
    change here. Doing it once per digest instead of once per app foreground is
    the difference between ~168 RU a few times a day and ~168 RU every time the
    phone comes out of a pocket.

    Only the closed taxonomy is counted; the ~213 free-text tags the scorer
    invented before the vocabulary closed are dropped, as they always were.
    """
    counts = {name: 0 for name in CATEGORIES}
    rows = cosmos.query(
        "SELECT c.categories FROM c WHERE c.type = 'article' AND IS_DEFINED(c.relevance) "
        "AND (NOT IS_DEFINED(c.cluster_id) OR IS_NULL(c.cluster_id) OR c.cluster_id = c.id)"
    )
    for row in rows:
        for name in row.get("categories") or []:
            if name in counts:
                counts[name] += 1

    cosmos.upsert(
        {
            "id": COUNTS_DOC_ID,
            "pk": USER_PK,
            "type": "config",
            "counts": counts,
            "updated_at": iso(utcnow()),
        }
    )
    return counts


def run_digest() -> RunRecord:
    now = utcnow()
    record = RunRecord(id=f"digest-{now:%Y%m%dT%H%M%SZ}", kind="digest", started_at=iso(now))

    with DailyBudget() as budget:
        try:
            gate_result, admitted = gate.run_gate()
            record.items_gated = gate_result.admitted
            record.embedding_tokens = gate_result.embedding_tokens

            if not admitted:
                log.info("Nothing admitted by the gate; skipping the LLM stages")
                record.finished_at = iso(utcnow())
                cosmos.upsert(record.to_doc())
                return record

            score_result = score.score_articles(admitted, budget=budget)
            record.items_scored = score_result.scored
            record.budget_exhausted |= score_result.budget_exhausted

            # Cluster before summarizing, not after: the whole point is to spend
            # one summarizer call per story instead of one per article.
            scored = [a for a in admitted if a.get("state") == "scored"]
            clusters = cluster.cluster_articles(scored)
            for group in clusters:
                cluster.apply_cluster_metadata(group)
            record.clusters_formed = sum(1 for c in clusters if c.size > 1)

            summary_result = summarize.summarize_clusters(clusters, budget=budget)
            record.items_summarized = summary_result.summarized
            record.budget_exhausted |= summary_result.budget_exhausted

            total = score_result.usage + summary_result.usage
            record.input_tokens = total.input_tokens
            record.cached_input_tokens = total.cached_input_tokens
            record.output_tokens = total.output_tokens
            # Sum the per-call costs rather than repricing the aggregate: the
            # long-context flag is a property of one request, not of a total.
            record.estimated_usd = round(score_result.cost_usd + summary_result.cost_usd, 6)

            # Scoring just changed which buckets articles sit in, so this is the
            # one moment the counts are stale. The early return above skips it on
            # purpose: nothing was scored, so nothing moved.
            refresh_category_counts()

        except Exception as exc:
            record.ok = False
            record.error = str(exc)
            log.exception("Digest run failed")

    record.finished_at = iso(utcnow())
    cosmos.upsert(record.to_doc())
    log.info(
        "Digest %s: gated=%d scored=%d summarized=%d cost=$%.4f budget_exhausted=%s",
        record.id,
        record.items_gated,
        record.items_scored,
        record.items_summarized,
        record.estimated_usd,
        record.budget_exhausted,
    )
    return record
