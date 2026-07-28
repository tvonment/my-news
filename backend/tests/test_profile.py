"""The weekly taste-profile rewrite.

The guards here matter more than the happy path: this job edits the document
that every future scoring run is built from, so a bad rewrite quietly degrades
the feed for a week and there is no error to notice.
"""

from __future__ import annotations

import pytest

from pipeline.profile import (
    MIN_VOTES_TO_REWRITE,
    PROFILE_SCHEMA,
    REWRITE_PROMPT,
    SAMPLE_PER_SIGNAL,
    _format_votes,
)


class TestFormatVotes:
    def test_renders_title_categories_and_reason(self) -> None:
        text = _format_votes(
            "Marked MORE like this",
            [{"title": "Azure Functions Flex adds X", "categories": ["azure", "cloud"], "why": "infra news"}],
        )
        assert "Azure Functions Flex adds X" in text
        assert "azure, cloud" in text
        assert "infra news" in text

    def test_empty_is_stated_explicitly(self) -> None:
        # "(none in this period)" is meaningfully different from an absent
        # section: it tells the model there were no dislikes, rather than
        # leaving it to infer that from silence.
        assert "(none in this period)" in _format_votes("Marked LESS like this", [])

    def test_missing_categories_do_not_break_rendering(self) -> None:
        text = _format_votes("Liked", [{"title": "T"}])
        assert "uncategorized" in text

    def test_none_categories_are_handled(self) -> None:
        # Cosmos returns a null rather than omitting the field when an article
        # was scored before categories existed.
        text = _format_votes("Liked", [{"title": "T", "categories": None, "why": None}])
        assert "uncategorized" in text


class TestRewriteGuards:
    def test_threshold_is_high_enough_to_generalize(self) -> None:
        # One or two votes cannot support a rewrite of the whole profile; the
        # centroids handle early signal on their own.
        assert MIN_VOTES_TO_REWRITE >= 10

    def test_sample_is_bounded(self) -> None:
        # The prompt carries titles for both signals — an unbounded sample would
        # grow the input cost without improving the rewrite.
        assert SAMPLE_PER_SIGNAL <= 100


class TestRewritePrompt:
    # The prompt is hard-wrapped, so assert against a whitespace-normalized copy
    # rather than the raw string — otherwise rewrapping a paragraph silently
    # breaks these tests without changing the prompt's meaning.
    PROMPT = " ".join(REWRITE_PROMPT.split())

    def test_forbids_learning_outlet_names(self) -> None:
        # A profile that says "likes 9to5Mac" would re-rank the same sources
        # forever instead of generalizing to subject matter.
        assert "not outlets" in self.PROMPT

    def test_protects_topics_absent_from_the_sample(self) -> None:
        # The failure mode this prevents is unrecoverable: drop a topic for
        # having no articles this week and nothing on it is ever surfaced to
        # vote on again.
        assert "Absence of evidence is not a dislike" in self.PROMPT

    def test_requires_more_than_one_vote_to_shift(self) -> None:
        assert "One vote is noise" in self.PROMPT


class TestSchema:
    def test_is_strict_compliant(self) -> None:
        assert PROFILE_SCHEMA["additionalProperties"] is False
        assert set(PROFILE_SCHEMA["required"]) == set(PROFILE_SCHEMA["properties"])

    def test_requires_a_changelog(self) -> None:
        # Without it a rewrite is unreviewable — you would see the profile
        # changed but not why.
        assert "changelog" in PROFILE_SCHEMA["required"]
