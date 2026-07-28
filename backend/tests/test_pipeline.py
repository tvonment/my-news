"""Gate ranking maths, scoring batch handling and paywall detection.

No network and no Azure: the gate is pure numpy once vectors exist, and the
paywall check is pure text.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from pipeline.gate import (
    RECENCY_HALF_LIFE_H,
    W_DISLIKED,
    W_INTEREST,
    W_LIKED,
    _normalize,
    _recency_bonus,
    _statements_hash,
)
from pipeline.score import BATCH_SIZE, SCORE_SCHEMA, build_system_prompt, format_batch
from pipeline.summarize import MAX_EXTRACT_CHARS, MIN_BODY_CHARS, SUMMARY_SCHEMA, looks_paywalled

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


class TestNormalize:
    def test_rows_become_unit_length(self) -> None:
        m = _normalize(np.asarray([[3.0, 4.0], [1.0, 0.0]], dtype=np.float32))
        assert np.allclose(np.linalg.norm(m, axis=1), 1.0)

    def test_zero_vector_does_not_divide_by_zero(self) -> None:
        m = _normalize(np.zeros((1, 4), dtype=np.float32))
        assert np.isfinite(m).all()

    def test_cosine_of_normalized_rows_is_a_dot_product(self) -> None:
        a = _normalize(np.asarray([[1.0, 1.0]], dtype=np.float32))
        b = _normalize(np.asarray([[1.0, 1.0]], dtype=np.float32))
        assert (a @ b.T).item() == pytest.approx(1.0, abs=1e-6)


class TestStatementsHash:
    def test_stable_for_same_input(self) -> None:
        assert _statements_hash(["azure", "apple"]) == _statements_hash(["azure", "apple"])

    def test_changes_when_statements_change(self) -> None:
        # This is the cache-invalidation mechanism: editing interests in the app
        # must force a re-embed on the next run.
        assert _statements_hash(["azure"]) != _statements_hash(["azure", "apple"])

    def test_order_is_significant(self) -> None:
        assert _statements_hash(["a", "b"]) != _statements_hash(["b", "a"])

    def test_contains_no_null_bytes(self) -> None:
        # Regression: the separator was once a literal NUL, which made the whole
        # module unimportable with "source code string cannot contain null bytes".
        assert "\x00" not in _statements_hash(["a", "b"])


class TestRecencyBonus:
    def test_brand_new_article_scores_one(self) -> None:
        assert _recency_bonus(NOW.isoformat(), NOW) == pytest.approx(1.0)

    def test_half_life_halves_the_bonus(self) -> None:
        older = NOW - timedelta(hours=RECENCY_HALF_LIFE_H)
        assert _recency_bonus(older.isoformat(), NOW) == pytest.approx(0.5, abs=1e-3)

    def test_decays_monotonically(self) -> None:
        values = [_recency_bonus((NOW - timedelta(hours=h)).isoformat(), NOW) for h in (0, 6, 24, 72)]
        assert values == sorted(values, reverse=True)

    def test_future_article_is_not_boosted_above_one(self) -> None:
        future = NOW + timedelta(hours=10)
        assert _recency_bonus(future.isoformat(), NOW) <= 1.0

    def test_unparseable_date_is_neutral_not_fatal(self) -> None:
        assert _recency_bonus("not a date", NOW) == 0.0
        assert _recency_bonus("", NOW) == 0.0

    def test_z_suffix_is_handled(self) -> None:
        assert _recency_bonus("2026-07-28T12:00:00Z", NOW) == pytest.approx(1.0)


class TestGateScoring:
    """The ranking formula, exercised directly on vectors."""

    @staticmethod
    def _score(article, interests, liked, disliked) -> float:
        s = 0.0
        if interests is not None:
            s += W_INTEREST * float((article @ interests.T).max())
        if liked is not None:
            s += W_LIKED * float(article @ liked)
        if disliked is not None:
            s -= W_DISLIKED * float(article @ disliked)
        return s

    def test_matching_an_interest_beats_matching_none(self) -> None:
        interests = _normalize(np.asarray([[1.0, 0.0]], dtype=np.float32))
        on_topic = _normalize(np.asarray([1.0, 0.0], dtype=np.float32))
        off_topic = _normalize(np.asarray([0.0, 1.0], dtype=np.float32))
        assert self._score(on_topic, interests, None, None) > self._score(off_topic, interests, None, None)

    def test_best_interest_wins_rather_than_the_average(self) -> None:
        # An article about exactly one of several interests should score high,
        # not be diluted by the interests it does not match.
        interests = _normalize(np.asarray([[1.0, 0.0], [0.0, 1.0], [0.0, -1.0]], dtype=np.float32))
        article = _normalize(np.asarray([1.0, 0.0], dtype=np.float32))
        assert self._score(article, interests, None, None) == pytest.approx(W_INTEREST, abs=1e-5)

    def test_disliked_similarity_pushes_score_down(self) -> None:
        disliked = _normalize(np.asarray([1.0, 0.0], dtype=np.float32))
        article = _normalize(np.asarray([1.0, 0.0], dtype=np.float32))
        assert self._score(article, None, None, disliked) < 0

    def test_dislike_outweighs_like_at_equal_similarity(self) -> None:
        # Deliberate: an explicit "less like this" should win a tie, otherwise
        # dismissing a topic never actually removes it from the feed.
        assert W_DISLIKED > W_LIKED

    def test_liked_similarity_lifts_score(self) -> None:
        liked = _normalize(np.asarray([1.0, 0.0], dtype=np.float32))
        near = _normalize(np.asarray([1.0, 0.1], dtype=np.float32))
        far = _normalize(np.asarray([0.0, 1.0], dtype=np.float32))
        assert self._score(near, None, liked, None) > self._score(far, None, liked, None)


class TestScorePrompt:
    def test_batch_is_numbered_from_one(self) -> None:
        articles = [{"title": f"T{i}", "lead": "lead", "source_name": "S"} for i in range(3)]
        text = format_batch(articles)
        assert text.startswith("1. ")
        assert "\n\n2. " in text and "\n\n3. " in text

    def test_ordinals_are_used_instead_of_content_hashes(self) -> None:
        # Referencing the 43-char id per article would be pure token overhead.
        articles = [{"title": "T", "lead": "l", "source_name": "S", "id": "A" * 43}]
        assert "A" * 43 not in format_batch(articles)

    def test_lead_is_truncated(self) -> None:
        articles = [{"title": "T", "lead": "x" * 5000, "source_name": "S"}]
        assert len(format_batch(articles)) < 1000

    def test_system_prompt_is_stable_across_calls(self) -> None:
        # Prompt caching keys on the first 1,024 tokens; a single changed
        # character is a full cache miss, so this must not embed a timestamp.
        assert build_system_prompt("profile") == build_system_prompt("profile")

    def test_system_prompt_carries_the_profile(self) -> None:
        assert "SENTINEL-PROFILE" in build_system_prompt("SENTINEL-PROFILE")

    def test_batch_size_matches_the_cost_model(self) -> None:
        assert BATCH_SIZE == 20


class TestStrictSchemas:
    """Azure rejects a strict schema that breaks any of these rules."""

    @staticmethod
    def _check(schema: dict, path: str = "root") -> None:
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, f"{path}: additionalProperties must be False"
            props = set(schema.get("properties", {}))
            assert set(schema.get("required", [])) == props, f"{path}: every property must be required"
            for key, value in schema.get("properties", {}).items():
                TestStrictSchemas._check(value, f"{path}.{key}")
        if schema.get("type") == "array":
            TestStrictSchemas._check(schema.get("items", {}), f"{path}[]")

    def test_score_schema_is_strict_compliant(self) -> None:
        self._check(SCORE_SCHEMA)

    def test_summary_schema_is_strict_compliant(self) -> None:
        self._check(SUMMARY_SCHEMA)

    def test_no_unsupported_validation_keywords(self) -> None:
        # minLength/maxLength/pattern/format are not supported in strict mode.
        banned = {"minLength", "maxLength", "pattern", "format", "minimum", "maximum"}

        def walk(node: object) -> None:
            if isinstance(node, dict):
                assert not (banned & node.keys()), f"unsupported keyword in {node}"
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(SCORE_SCHEMA)
        walk(SUMMARY_SCHEMA)


class TestPaywallDetection:
    def test_short_body_is_treated_as_paywalled(self) -> None:
        assert looks_paywalled("Title", "too short")

    def test_full_article_is_not_paywalled(self) -> None:
        assert not looks_paywalled("Title", "word " * (MIN_BODY_CHARS // 2))

    def test_heise_plus_prefix_is_detected(self) -> None:
        # heise prefixes paywalled items literally with "heise+ | ".
        body = "word " * (MIN_BODY_CHARS // 2)
        assert looks_paywalled("heise+ | Exklusiv-Analyse", body)

    def test_marker_in_body_is_detected(self) -> None:
        body = "Subscribe to continue reading. " + "word " * (MIN_BODY_CHARS // 2)
        assert looks_paywalled("Title", body)

    def test_detection_is_case_insensitive(self) -> None:
        body = "SUBSCRIBE TO CONTINUE. " + "word " * (MIN_BODY_CHARS // 2)
        assert looks_paywalled("Title", body)

    def test_extract_cap_is_far_below_the_long_context_cliff(self) -> None:
        # 272k input tokens reprices the entire request; at ~3 chars/token for
        # German this cap is an order of magnitude clear of it.
        assert MAX_EXTRACT_CHARS / 3 < 272_000 / 5
