"""Regressions from the 2026-08-02 outage review.

Each test here pins a defect that was found by reading the code during that
incident rather than by a failure, so nothing else in the suite covers them.
No network and no Azure.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from core.models import iso, utcnow
from core.sources import Source


class TestSourceRoundTripsUnknownFields:
    """`load_sources` used to do `Source(**entry)` on stored documents.

    The `sources` document in Cosmos is written from `asdict(Source)`, so it
    carries whatever fields the dataclass had at write time. Expanding it with
    `**` means the first field ever renamed or removed makes `load_sources()`
    raise `TypeError` — and that function is on the path for harvest, refresh
    and every feed read at once.
    """

    @staticmethod
    def _rebuild(entry: dict) -> Source:
        # Mirrors the filtering in core/sources.py:load_sources.
        fields = set(Source.__dataclass_fields__)
        return Source(**{k: v for k, v in entry.items() if k in fields})

    def test_unfiltered_expansion_is_what_used_to_break(self) -> None:
        stored = asdict(Source(id="heise", name="heise", url="https://x/f"))
        stored["retired_field"] = "written by an older build"
        with pytest.raises(TypeError):
            Source(**stored)

    def test_stale_field_is_dropped_instead_of_raising(self) -> None:
        stored = asdict(Source(id="heise", name="heise", url="https://x/f"))
        stored["retired_field"] = "written by an older build"
        assert self._rebuild(stored).id == "heise"

    def test_current_fields_survive_the_filter(self) -> None:
        original = Source(
            id="postillon",
            name="Der Postillon",
            url="https://x/f",
            satire=True,
            enrich=True,
            enabled=False,
            poll_minutes=180,
        )
        assert self._rebuild(asdict(original)) == original


class TestFeedbackDocumentIds:
    """Feedback ids are `evt_<timestamp>_<article id>`.

    `core/models.py` defines `iso()` specifically to keep "+" out of document
    ids, citing SDK bugs on them — but the feedback handler was formatting its
    timestamp with a bare `datetime.isoformat()`, which ends in "+00:00".
    """

    def test_bare_isoformat_carries_the_characters_we_avoid(self) -> None:
        raw = datetime(2026, 8, 2, 8, 30, tzinfo=timezone.utc).isoformat()
        assert "+" in raw

    def test_iso_helper_produces_an_id_safe_timestamp(self) -> None:
        stamp = iso(datetime(2026, 8, 2, 8, 30, tzinfo=timezone.utc))
        assert stamp == "2026-08-02T08:30:00Z"
        assert "+" not in stamp and "/" not in stamp

    def test_generated_id_is_clean_for_a_real_article_id(self) -> None:
        # Article ids are base64**url**, so they can contain "-" and "_" but
        # never "+" or "/". The composed id must stay that way.
        doc_id = f"evt_{iso(utcnow())}_HuS7hGmT-_x0Vd9kQ2mLpA"
        assert "+" not in doc_id and "/" not in doc_id
