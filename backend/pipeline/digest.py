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
from core.models import RunRecord, iso, utcnow
from pipeline import gate, score, summarize

log = logging.getLogger(__name__)


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

            scored = [a for a in admitted if a.get("state") == "scored"]
            summary_result = summarize.summarize_articles(scored, budget=budget)
            record.items_summarized = summary_result.summarized
            record.budget_exhausted |= summary_result.budget_exhausted

            total = score_result.usage + summary_result.usage
            record.input_tokens = total.input_tokens
            record.cached_input_tokens = total.cached_input_tokens
            record.output_tokens = total.output_tokens
            # Sum the per-call costs rather than repricing the aggregate: the
            # long-context flag is a property of one request, not of a total.
            record.estimated_usd = round(score_result.cost_usd + summary_result.cost_usd, 6)

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
