"""Token accounting and the daily spend brake.

Azure OpenAI has **no hard spend limit** — the docs say so explicitly. A runaway
loop will keep billing until someone notices. The Cost Management budget alert
in Bicep is the out-of-band warning; this module is the in-process brake that
actually stops work.

Prices are USD per 1M tokens, West Europe, Global Standard, verified against the
Azure Retail Prices API on 2026-07-27.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timezone
from typing import Any

from core import cosmos
from core.config import get_settings
from core.models import USER_PK, utcnow

log = logging.getLogger(__name__)

# gpt-5.6 is the first family to bill cache *writes* in addition to reads, so a
# churning prompt prefix is actively expensive rather than merely un-discounted.
#
# The long-context columns apply when a request exceeds 272,000 INPUT tokens, at
# which point the *entire request* reprices — 2x input, 1.5x output — not just
# the overflow. Headlines never approach this; a summarizer fed an unbounded
# article could, which is why extraction is capped.
@dataclass(frozen=True)
class ModelPrice:
    input: float
    cached_input: float
    cache_write: float
    output: float
    long_input: float = 0.0
    long_cached_input: float = 0.0
    long_cache_write: float = 0.0
    long_output: float = 0.0


PRICES: dict[str, ModelPrice] = {
    "gpt-5.6-luna": ModelPrice(1.00, 0.10, 1.25, 6.00, 2.00, 0.20, 2.50, 9.00),
    "gpt-5.6-terra": ModelPrice(2.50, 0.25, 3.125, 15.00, 5.00, 0.50, 6.25, 22.50),
    "gpt-5.6-sol": ModelPrice(5.00, 0.50, 6.25, 30.00, 10.00, 1.00, 12.50, 45.00),
    "text-embedding-3-small": ModelPrice(0.02, 0.02, 0.0, 0.0),
    "text-embedding-3-large": ModelPrice(0.13, 0.13, 0.0, 0.0),
}

LONG_CONTEXT_THRESHOLD = 272_000


@dataclass
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    # Whether the ORIGINATING REQUEST crossed the long-context threshold. This is
    # a property of one call and must be decided at call time — it cannot be
    # re-derived from a token count, because a month of small requests sums well
    # past 272k without any of them ever being repriced.
    long_context: bool = False

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.output_tokens + other.output_tokens,
            # Aggregates are for reporting token counts only. Do not price a sum:
            # sum the per-call costs instead, or long-context calls contaminate
            # the rate applied to every short call in the batch.
            self.long_context or other.long_context,
        )


def estimate_usd(model_id: str, usage: Usage) -> float:
    """Cost of a single call.

    Unknown models cost 0 rather than raising: a pricing gap must not take the
    pipeline down, though it will show up as an implausibly cheap run record.
    """
    price = PRICES.get(model_id)
    if price is None:
        log.warning("No price entry for model %r; treating as free in accounting", model_id)
        return 0.0

    if usage.long_context:
        rates = (price.long_input, price.long_cached_input, price.long_cache_write, price.long_output)
        log.warning(
            "Request crossed the %d-token long-context threshold; the WHOLE request reprices "
            "(2x input, 1.5x output)",
            LONG_CONTEXT_THRESHOLD,
        )
    else:
        rates = (price.input, price.cached_input, price.cache_write, price.output)

    r_in, r_cached, r_write, r_out = rates
    return (
        usage.input_tokens * r_in
        + usage.cached_input_tokens * r_cached
        + usage.cache_write_tokens * r_write
        + usage.output_tokens * r_out
    ) / 1_000_000


class BudgetExhausted(RuntimeError):
    """Raised when the day's estimated spend has crossed the configured ceiling."""


class DailyBudget:
    """Tracks estimated spend for one UTC day, persisted in Cosmos.

    Persisted rather than in-process because Flex scales to zero: an in-memory
    counter resets on every cold start, which is exactly the situation where a
    runaway loop would go unnoticed.
    """

    def __init__(self, limit_usd: float | None = None) -> None:
        self.limit_usd = limit_usd if limit_usd is not None else get_settings().daily_budget_usd
        self._day = utcnow().astimezone(timezone.utc).strftime("%Y-%m-%d")
        self._doc_id = f"budget|{self._day}"
        self._spent = self._load()
        self._dirty = False

    def _load(self) -> float:
        doc = cosmos.read(self._doc_id, USER_PK)
        return float(doc.get("spent_usd", 0.0)) if doc else 0.0

    @property
    def spent(self) -> float:
        return self._spent

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit_usd - self._spent)

    def check(self) -> None:
        """Raise if the budget is already gone. Call before each expensive stage."""
        if self._spent >= self.limit_usd:
            raise BudgetExhausted(
                f"Daily budget of ${self.limit_usd:.2f} reached "
                f"(${self._spent:.4f} spent on {self._day}); skipping LLM stages"
            )

    def charge(self, model_id: str, usage: Usage) -> float:
        cost = estimate_usd(model_id, usage)
        self._spent += cost
        self._dirty = True
        return cost

    def flush(self) -> None:
        if not self._dirty:
            return
        doc: dict[str, Any] = {
            "id": self._doc_id,
            "pk": USER_PK,
            "type": "config",
            "day": self._day,
            "spent_usd": round(self._spent, 6),
            "limit_usd": self.limit_usd,
            "updated_at": utcnow().isoformat(),
            "ttl": 60 * 60 * 24 * 35,
        }
        cosmos.upsert(doc)
        self._dirty = False

    def __enter__(self) -> "DailyBudget":
        return self

    def __exit__(self, *exc: object) -> None:
        # Flush even on failure — tokens spent before the error still cost money.
        self.flush()
