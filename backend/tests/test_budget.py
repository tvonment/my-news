"""Token accounting.

Prices are West Europe / Global Standard, verified against the Azure Retail
Prices API on 2026-07-27. If these assertions ever fail after a price change,
update PRICES and docs/cost.md together — the cost model is only useful while
it matches reality.
"""

from __future__ import annotations

import pytest

from core.budget import LONG_CONTEXT_THRESHOLD, PRICES, Usage, estimate_usd


class TestPriceTable:
    def test_luna_is_the_cheapest_5_6_tier(self) -> None:
        luna, terra, sol = PRICES["gpt-5.6-luna"], PRICES["gpt-5.6-terra"], PRICES["gpt-5.6-sol"]
        assert luna.input < terra.input < sol.input
        assert luna.output < terra.output < sol.output

    def test_verified_luna_rates(self) -> None:
        luna = PRICES["gpt-5.6-luna"]
        assert (luna.input, luna.cached_input, luna.cache_write, luna.output) == (1.00, 0.10, 1.25, 6.00)

    def test_cache_write_is_billed_on_5_6(self) -> None:
        # gpt-5.6 is the first family to charge for cache writes; a churning
        # prompt prefix is now actively expensive, not merely un-discounted.
        for name in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
            assert PRICES[name].cache_write > 0

    def test_cached_reads_are_ninety_percent_off(self) -> None:
        for name in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
            price = PRICES[name]
            assert price.cached_input == pytest.approx(price.input * 0.10)


class TestEstimateUsd:
    def test_simple_call(self) -> None:
        cost = estimate_usd("gpt-5.6-luna", Usage(input_tokens=1_000_000, output_tokens=1_000_000))
        assert cost == pytest.approx(7.00)

    def test_cached_input_is_cheaper(self) -> None:
        cached = estimate_usd("gpt-5.6-luna", Usage(cached_input_tokens=1_000_000))
        fresh = estimate_usd("gpt-5.6-luna", Usage(input_tokens=1_000_000))
        assert cached == pytest.approx(0.10)
        assert cached < fresh

    def test_embeddings(self) -> None:
        assert estimate_usd("text-embedding-3-small", Usage(input_tokens=1_000_000)) == pytest.approx(0.02)

    def test_long_context_reprices_the_whole_request(self) -> None:
        # Crossing 272k input tokens is a step function on the entire request:
        # 2x input and 1.5x output, not just on the overflow.
        tokens = dict(input_tokens=LONG_CONTEXT_THRESHOLD + 1, output_tokens=1000)
        under_cost = estimate_usd("gpt-5.6-luna", Usage(**tokens))
        over_cost = estimate_usd("gpt-5.6-luna", Usage(**tokens, long_context=True))

        assert over_cost > under_cost * 1.9

    def test_aggregated_usage_is_not_repriced_as_long_context(self) -> None:
        """A month of small calls must not be billed at long-context rates.

        The flag describes one request's prompt size. Re-deriving it from a
        summed token count would silently inflate every reported cost — and trip
        the daily budget brake early — once monthly volume passed 272k tokens.
        """
        month = Usage(input_tokens=5_000_000, output_tokens=1_000_000)
        assert month.long_context is False
        assert estimate_usd("gpt-5.6-luna", month) == pytest.approx(11.00)

    def test_summing_preserves_a_long_context_call(self) -> None:
        total = Usage(input_tokens=10) + Usage(input_tokens=300_000, long_context=True)
        assert total.long_context is True

    def test_unknown_model_is_free_rather_than_fatal(self) -> None:
        # A pricing gap must not take the pipeline down, but it will show up as
        # an implausibly cheap run record.
        assert estimate_usd("gpt-9-imaginary", Usage(input_tokens=1_000_000)) == 0.0

    def test_zero_usage_costs_nothing(self) -> None:
        assert estimate_usd("gpt-5.6-luna", Usage()) == 0.0


class TestUsageArithmetic:
    def test_addition_accumulates_every_field(self) -> None:
        total = Usage(1, 2, 3, 4) + Usage(10, 20, 30, 40)
        assert (total.input_tokens, total.cached_input_tokens) == (11, 22)
        assert (total.cache_write_tokens, total.output_tokens) == (33, 44)


class TestRealisticScenario:
    def test_medium_volume_month_is_within_budget(self) -> None:
        """Scenario B from the plan: 3 runs/day, 200 items scored/day.

        Guards the headline claim that this lands at roughly $8/month of model
        spend, so a model or price change that breaks the budget shows up here
        rather than on a credit card statement.
        """
        # Scoring on luna: ~1.09M input, ~0.33M output per month.
        scoring = estimate_usd("gpt-5.6-luna", Usage(input_tokens=1_094_000, output_tokens=334_000))
        # Summaries on terra: 608 articles x (1900 in / 200 out).
        summarizing = estimate_usd("gpt-5.6-terra", Usage(input_tokens=1_155_000, output_tokens=122_000))
        embeddings = estimate_usd("text-embedding-3-small", Usage(input_tokens=1_220_000))

        total = scoring + summarizing + embeddings
        assert 6.0 < total < 10.0, f"expected ~$8/month, got ${total:.2f}"
