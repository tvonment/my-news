"""Harvest: fetch feeds, dedupe, persist. No LLM, no embeddings, ~$0.

Runs far more often than the digest (every 30-60 min vs 3x/day) because several
feeds carry only 10 items — The Verge, NPR and the Microsoft WordPress blogs.
On a busy day those windows roll over in well under 8 hours, so a digest-rate
crawl would silently and permanently lose stories with no error to alert anyone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from core import cosmos
from core.config import get_settings
from core.models import (
    Article,
    RunRecord,
    SOURCE_PK,
    SourceState,
    article_id,
    article_pk,
    iso,
    utcnow,
)
from core.sources import Source, load_sources
from fetch.http import DEFAULT_TIMEOUT, fetch_feed
from fetch.parse import NotAFeedError, parse_feed

log = logging.getLogger(__name__)


@dataclass
class HarvestOutcome:
    polled: int = 0
    unchanged: int = 0
    failed: int = 0
    items_seen: int = 0
    items_new: int = 0
    stale_feeds: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.stale_feeds is None:
            self.stale_feeds = []


def _load_state(source_id: str) -> SourceState:
    doc = cosmos.read(source_id, SOURCE_PK)
    if doc is None:
        return SourceState(id=source_id)
    known = {f for f in SourceState.__dataclass_fields__ if f != "type"}
    return SourceState(**{k: v for k, v in doc.items() if k in known})


def _is_due(source: Source, state: SourceState, now: datetime) -> bool:
    if not state.last_fetch_at:
        return True
    try:
        last = datetime.fromisoformat(state.last_fetch_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    # Back off exponentially on repeated failure so a dead host does not get
    # hammered every cycle, capped so it still recovers within a day.
    interval = source.poll_minutes * min(2**state.consecutive_failures, 24)
    return now - last >= timedelta(minutes=interval)


def _check_stale(source: Source, state: SourceState, now: datetime) -> bool:
    """A feed that returns 200 but has stopped publishing is a zombie.

    news.microsoft.com/feed/ is the canonical example: HTTP 200, valid XML, a
    fresh Last-Modified header, and no new item since 2025-05-07. Only the
    newest item date reveals it.
    """
    if not state.newest_item_at:
        return False
    try:
        newest = datetime.fromisoformat(state.newest_item_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return now - newest > timedelta(hours=source.stale_after_hours)


def harvest_source(
    source: Source,
    client: httpx.Client,
    now: datetime,
    outcome: HarvestOutcome,
) -> None:
    state = _load_state(source.id)

    if not _is_due(source, state, now):
        return

    outcome.polled += 1
    result = fetch_feed(source.url, state, client=client)
    state.last_fetch_at = iso(now)

    if not result.ok:
        state.consecutive_failures += 1
        state.last_error = result.error
        outcome.failed += 1
        log.warning("Fetch failed for %s: %s", source.id, result.error)
        cosmos.upsert(state.to_doc())
        return

    state.consecutive_failures = 0
    state.last_error = None
    state.last_success_at = iso(now)
    if result.resolved_url:
        # Persist the post-redirect URL so later fetches skip the hop.
        state.resolved_url = result.resolved_url

    if result.etag_ignored:
        # The server answered 200 with a body we already had, despite being
        # given its own ETag. Stop offering it one.
        state.etag_works = False
        log.info("Source %s ignores If-None-Match; using If-Modified-Since only", source.id)
    elif result.status == 304:
        state.etag_works = True

    if result.unchanged:
        outcome.unchanged += 1
        state.body_sha256 = result.body_sha256 or state.body_sha256
        state.etag = result.etag or state.etag
        state.last_modified = result.last_modified or state.last_modified
        cosmos.upsert(state.to_doc())
        return

    try:
        parsed = parse_feed(result.body or b"", fetched_at=now)
    except NotAFeedError as exc:
        state.consecutive_failures += 1
        state.last_error = f"not a feed: {exc}"
        outcome.failed += 1
        log.warning("Source %s did not return a feed: %s", source.id, exc)
        cosmos.upsert(state.to_doc())
        return

    settings = get_settings()
    ttl_seconds = settings.article_ttl_days * 24 * 60 * 60
    new_count = 0

    for item in parsed.items:
        outcome.items_seen += 1
        article = Article(
            id=article_id(item.canonical_url),
            pk=article_pk(item.published),
            url=item.url,
            canonical_url=item.canonical_url,
            source_id=source.id,
            source_name=source.name,
            title=item.title,
            lead=item.lead,
            published_at=iso(item.published),
            fetched_at=iso(now),
            ttl=ttl_seconds,
        )
        # Dedupe is the 409 from create_item, not a preceding query: ~1 RU and
        # no race between the check and the write.
        if cosmos.try_create(article.to_doc()):
            new_count += 1

    outcome.items_new += new_count
    state.etag = result.etag
    state.last_modified = result.last_modified
    state.body_sha256 = result.body_sha256
    if parsed.newest:
        state.newest_item_at = iso(parsed.newest)

    if _check_stale(source, state, now):
        outcome.stale_feeds.append(source.id)
        log.warning(
            "Source %s looks like a zombie: HTTP %s but newest item is %s",
            source.id,
            result.status,
            state.newest_item_at,
        )

    cosmos.upsert(state.to_doc())
    log.info("Harvested %s: %d items, %d new", source.id, len(parsed.items), new_count)


def run_harvest(only_source: str | None = None) -> RunRecord:
    now = utcnow()
    record = RunRecord(id=f"harvest-{now:%Y%m%dT%H%M%SZ}", kind="harvest", started_at=iso(now))
    outcome = HarvestOutcome()

    sources = [s for s in load_sources() if s.enabled]
    if only_source:
        sources = [s for s in sources if s.id == only_source]

    with httpx.Client(follow_redirects=True, timeout=DEFAULT_TIMEOUT, http2=True) as client:
        for source in sources:
            try:
                harvest_source(source, client, now, outcome)
            except Exception:
                # One broken feed must not take down the run; the next cycle
                # retries it with backoff.
                outcome.failed += 1
                log.exception("Unhandled error harvesting %s", source.id)

    record.feeds_polled = outcome.polled
    record.feeds_unchanged = outcome.unchanged
    record.feeds_failed = outcome.failed
    record.items_seen = outcome.items_seen
    record.items_new = outcome.items_new
    record.finished_at = iso(utcnow())
    record.ok = outcome.failed == 0

    cosmos.upsert(record.to_doc())
    log.info(
        "Harvest complete: polled=%d unchanged=%d failed=%d seen=%d new=%d stale=%s",
        outcome.polled,
        outcome.unchanged,
        outcome.failed,
        outcome.items_seen,
        outcome.items_new,
        outcome.stale_feeds,
    )
    return record
