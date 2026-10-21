"""Story clustering and the fixed category taxonomy.

Clustering's failure modes are asymmetric, and the tests are written around
that: wrongly merging two stories *hides* one of them completely, while wrongly
splitting them merely repeats. So the bias is toward splitting.
"""

from __future__ import annotations

import math

import pytest

from core.taxonomy import CATEGORIES, CATEGORY_GUIDE, CATEGORY_LABELS, SATIRE_CATEGORY
from pipeline.cluster import (
    MAX_CLUSTER_AGE_DIFF_HOURS,
    Cluster,
    apply_cluster_metadata,
    cluster_articles,
)
from pipeline.score import clean_categories


def article(aid: str, vec: list[float], relevance: int = 50, source: str = "S", when: str = "2026-07-28T10:00:00Z"):
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return {
        "id": aid,
        "embedding": [v / norm for v in vec],
        "relevance": relevance,
        "source_name": source,
        "published_at": when,
        "title": aid,
    }


class TestClustering:
    def test_identical_stories_merge(self) -> None:
        arts = [
            article("a", [1.0, 0.0], 80, "Outlet A"),
            article("b", [1.0, 0.0], 70, "Outlet B"),
            article("c", [1.0, 0.0], 60, "Outlet C"),
        ]
        clusters = cluster_articles(arts, threshold=0.8)
        assert len(clusters) == 1
        assert clusters[0].size == 3

    def test_unrelated_stories_stay_apart(self) -> None:
        arts = [article("a", [1.0, 0.0]), article("b", [0.0, 1.0])]
        assert len(cluster_articles(arts, threshold=0.8)) == 2

    def test_primary_is_the_highest_scored(self) -> None:
        # The reader should get the outlet the ranking already preferred, not
        # whichever happened to be processed first.
        arts = [
            article("low", [1.0, 0.0], 40, "Weak"),
            article("high", [1.0, 0.0], 90, "Strong"),
        ]
        clusters = cluster_articles(arts, threshold=0.8)
        assert clusters[0].primary["id"] == "high"
        assert clusters[0].members[0]["id"] == "low"

    def test_far_apart_in_time_never_merges(self) -> None:
        # Textually similar but a week apart is a recurring topic, not one story.
        arts = [
            article("now", [1.0, 0.0], 80, "A", "2026-07-28T10:00:00Z"),
            article("old", [1.0, 0.0], 70, "B", "2026-07-18T10:00:00Z"),
        ]
        assert len(cluster_articles(arts, threshold=0.8)) == 2

    def test_age_window_is_generous_enough_for_slow_outlets(self) -> None:
        # A weekly publication covering Monday's news on Wednesday should still
        # join the cluster.
        assert MAX_CLUSTER_AGE_DIFF_HOURS >= 48

    def test_articles_without_embeddings_survive(self) -> None:
        # An article that never reached the gate must still reach the feed.
        arts = [article("a", [1.0, 0.0]), {"id": "no-vec", "relevance": 10, "title": "x"}]
        ids = {c.primary["id"] for c in cluster_articles(arts, threshold=0.8)}
        assert ids == {"a", "no-vec"}

    def test_single_article_is_its_own_cluster(self) -> None:
        clusters = cluster_articles([article("solo", [1.0, 0.0])], threshold=0.8)
        assert len(clusters) == 1 and clusters[0].size == 1

    def test_empty_input(self) -> None:
        assert cluster_articles([], threshold=0.8) == []

    def test_higher_threshold_splits_more(self) -> None:
        arts = [article("a", [1.0, 0.1]), article("b", [1.0, 0.35])]
        assert len(cluster_articles(arts, threshold=0.90)) <= len(
            cluster_articles(arts, threshold=0.99)
        )


class TestClusterMetadata:
    def test_primary_lists_other_sources_and_members_do_not(self) -> None:
        c = Cluster(
            primary=article("p", [1.0, 0.0], 90, "Primary"),
            members=[article("m", [1.0, 0.0], 50, "Other")],
        )
        apply_cluster_metadata(c)
        assert c.primary["also_reported_by"] == ["Other"]
        assert c.primary["cluster_id"] == "p"
        # Members point at the primary and carry no list of their own, so they
        # cannot render as a second card claiming the same coverage.
        assert c.members[0]["cluster_id"] == "p"
        assert c.members[0]["also_reported_by"] == []

    def test_the_primary_source_is_not_listed_as_an_other(self) -> None:
        c = Cluster(
            primary=article("p", [1.0, 0.0], 90, "Same"),
            members=[article("m", [1.0, 0.0], 50, "Same")],
        )
        apply_cluster_metadata(c)
        assert c.primary["also_reported_by"] == []

    def test_duplicate_sources_appear_once(self) -> None:
        c = Cluster(
            primary=article("p", [1.0, 0.0], 90, "P"),
            members=[article("a", [1.0, 0.0], 50, "X"), article("b", [1.0, 0.0], 40, "X")],
        )
        apply_cluster_metadata(c)
        assert c.primary["also_reported_by"] == ["X"]


class TestTaxonomy:
    def test_labels_cover_every_category(self) -> None:
        assert set(CATEGORY_LABELS) == set(CATEGORIES)

    def test_guide_documents_every_category(self) -> None:
        # A category the model is offered but never told the meaning of gets
        # applied on vibes.
        for name in CATEGORIES:
            assert f"- {name}:" in CATEGORY_GUIDE

    def test_stays_small_enough_to_render_as_chips(self) -> None:
        assert len(CATEGORIES) <= 12

    def test_satire_category_exists(self) -> None:
        assert SATIRE_CATEGORY in CATEGORIES


class TestCleanCategories:
    def test_unknown_categories_are_dropped(self) -> None:
        # The enum should prevent these, but a model that ignores the schema
        # must not reintroduce the 213-tag sprawl.
        assert clean_categories(["ai", "kubernetes", "display"], satire=False) == ["ai"]

    def test_case_and_whitespace_tolerated(self) -> None:
        assert clean_categories([" AI ", "Apple"], satire=False) == ["ai", "apple"]

    def test_capped_at_two(self) -> None:
        assert len(clean_categories(["ai", "apple", "dev", "security"], satire=False)) == 2

    def test_duplicates_removed(self) -> None:
        assert clean_categories(["ai", "ai"], satire=False) == ["ai"]

    def test_satire_always_wins(self) -> None:
        # Even if the model insists a Postillon piece is world news.
        assert clean_categories(["world", "business"], satire=True) == [SATIRE_CATEGORY]

    def test_empty_input(self) -> None:
        assert clean_categories(None, satire=False) == []


class TestNullClusterIdTrap:
    """Regression: a null `cluster_id` silently emptied the feed.

    `dataclasses.asdict` wrote `cluster_id: null` on every article, and Cosmos's
    `IS_DEFINED()` returns TRUE for an explicit null — so the feed's
    `NOT IS_DEFINED(c.cluster_id)` clause was false for every *unclustered*
    article and the feed collapsed from ~400 items to the 22 cluster primaries.
    Nothing errored; the feed just went quiet.

    The feed query that also tolerated legacy nulls was retired with the v1
    routes on 2026-10-21. The write side stays guarded: harvest still writes
    these documents, and any later query on `cluster_id` meets the same trap.
    """

    def test_unclustered_article_omits_the_key_entirely(self) -> None:
        from core.models import Article

        doc = Article(id="a", pk="art|2026-07-28").to_doc()
        assert "cluster_id" not in doc

    def test_clustered_article_keeps_it(self) -> None:
        from core.models import Article

        a = Article(id="a", pk="art|2026-07-28")
        a.cluster_id = "primary-id"
        assert a.to_doc()["cluster_id"] == "primary-id"
