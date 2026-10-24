"""Harvest: fetch feeds, dedupe, persist. No LLM, no embeddings, ~$0.

Runs far more often than the digest (every 30-60 min vs 3x/day) because several
feeds carry only 10 items — The Verge, NPR and the Microsoft WordPress blogs.
On a busy day those windows roll over in well under 8 hours, so a digest-rate
crawl would silently and permanently lose stories with no error to alert anyone.

Since Phase 1 this v1 harvest lives in v1's container, ``items``
(``LEGACY_COSMOS_CONTAINER``), next to the v2 container ``news`` that every new
module uses (docs/data-model.md section 10). Every Cosmos call here names the
legacy container explicitly: ``get_container()`` without a name is ``news``, so
a call that forgot it would quietly write v1 documents into the v2 container.
And the whole run is paced to RU_RATE_LEGACY_HARVEST (350 RU/s): ``items`` was
lowered to 400 RU/s to make room for ``news`` inside the free tier, and a busy
harvest used to burst to 550-600. Phase 2 deletes this module together with
the v1 stages, in the release that ships harvest v2.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import trafilatura

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
from fetch.http import DEFAULT_TIMEOUT, fetch_feed, user_agent_for
from fetch.parse import NotAFeedError, parse_feed

log = logging.getLogger(__name__)


# A feed teaser shorter than this carries too little signal to embed against —
# the gate ends up ranking on the title alone, which systematically favours
# verbose rumor blogs over terse first-party announcements.
MIN_LEAD_CHARS = 200
ENRICHED_LEAD_CHARS = 700
# Bounded per run so a first harvest of a 1,000-item backlog cannot turn into a
# thousand outbound page fetches.
MAX_ENRICHMENTS_PER_RUN = 40


@dataclass
class HarvestOutcome:
    polled: int = 0
    unchanged: int = 0
    failed: int = 0
    items_seen: int = 0
    items_new: int = 0
    items_too_old: int = 0
    enriched: int = 0
    stale_feeds: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.stale_feeds is None:
            self.stale_feeds = []


def _legacy() -> str:
    """The container every call in this module passes explicitly (``items``)."""
    return get_settings().legacy_cosmos_container


def _load_state(source_id: str) -> SourceState:
    doc = cosmos.read(source_id, SOURCE_PK, container=_legacy())
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
    *,
    force: bool = False,
) -> None:
    state = _load_state(source.id)

    # A forced run (the ops-jobs worker) polls now, whatever the schedule and
    # the failure backoff say.
    if not force and not _is_due(source, state, now):
        return

    outcome.polled += 1
    result = fetch_feed(source.url, state, client=client)
    state.last_fetch_at = iso(now)

    if not result.ok:
        state.consecutive_failures += 1
        state.last_error = result.error
        outcome.failed += 1
        log.warning("Fetch failed for %s: %s", source.id, result.error)
        cosmos.upsert(state.to_doc(), container=_legacy())
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
        cosmos.upsert(state.to_doc(), container=_legacy())
        return

    try:
        parsed = parse_feed(result.body or b"", fetched_at=now)
    except NotAFeedError as exc:
        state.consecutive_failures += 1
        state.last_error = f"not a feed: {exc}"
        outcome.failed += 1
        log.warning("Source %s did not return a feed: %s", source.id, exc)
        cosmos.upsert(state.to_doc(), container=_legacy())
        return

    settings = get_settings()
    ttl_seconds = settings.article_ttl_days * 24 * 60 * 60
    new_count = 0

    max_age = timedelta(days=settings.max_article_age_days)

    for item in parsed.items:
        outcome.items_seen += 1
        # Skip the archive. A feed's deep back-catalogue is indistinguishable
        # from new content on first sight, and storing it costs RU for articles
        # the gate would never surface anyway — it only looks at recent days.
        if now - item.published > max_age:
            outcome.items_too_old += 1
            continue
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
            satire=source.satire,
        )
        # Dedupe is the 409 from create_item, not a preceding query: ~1 RU and
        # no race between the check and the write.
        if cosmos.try_create(article.to_doc(), container=_legacy()):
            new_count += 1
            if (
                source.enrich
                and len(article.lead) < MIN_LEAD_CHARS
                and outcome.enriched < MAX_ENRICHMENTS_PER_RUN
            ):
                if enrich_lead(article, client):
                    outcome.enriched += 1
                    cosmos.upsert(article.to_doc(), container=_legacy())

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

    cosmos.upsert(state.to_doc(), container=_legacy())
    log.info("Harvested %s: %d items, %d new", source.id, len(parsed.items), new_count)


def enrich_lead(article: Article, client: httpx.Client) -> bool:
    """Backfill a usable lead from the article page.

    Some of the best sources have the worst teasers — Hugging Face's feed has no
    description field at all, OpenAI's is a ~150-character stub — so they lose
    the embedding gate to rumor blogs that write three verbose sentences. This
    levels that up rather than penalising the verbose sources, which would just
    trade one bias for another.

    Costs one page fetch and no tokens. Failure is not an error: the article
    keeps its thin lead and simply competes as before.
    """
    try:
        response = client.get(article.url, headers={"User-Agent": user_agent_for(article.url)})
        response.raise_for_status()
        text = trafilatura.extract(
            response.text, include_comments=False, include_tables=False, favor_precision=True
        )
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("Could not enrich %s: %s", article.url, exc)
        return False

    if not text:
        return False

    lead = " ".join(text.split())[:ENRICHED_LEAD_CHARS]
    if len(lead) <= len(article.lead):
        return False

    article.lead = lead
    article.lead_enriched = True
    return True


def run_harvest(only_source: str | None = None, *, force: bool = False) -> RunRecord:
    """One harvest of every enabled source (or of ``only_source``). ``force``
    skips the per-source schedule, for the ops-jobs worker's on-demand run.

    Paced as a whole: the pacer sees every call's request charge, including
    the source list and the run record, and sleeps once the run has spent more
    than RU_RATE_LEGACY_HARVEST per second with a one-second burst.
    """
    with cosmos.paced(cosmos.RU_RATE_LEGACY_HARVEST):
        return _run_harvest(only_source, force=force)


def _run_harvest(only_source: str | None, *, force: bool) -> RunRecord:
    now = utcnow()
    record = RunRecord(id=f"harvest-{now:%Y%m%dT%H%M%SZ}", kind="harvest", started_at=iso(now))
    outcome = HarvestOutcome()

    sources = [s for s in load_sources() if s.enabled]
    if only_source:
        sources = [s for s in sources if s.id == only_source]

    with httpx.Client(follow_redirects=True, timeout=DEFAULT_TIMEOUT, http2=True) as client:
        for source in sources:
            try:
                harvest_source(source, client, now, outcome, force=force)
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
    record.items_enriched = outcome.enriched
    record.items_too_old = outcome.items_too_old
    record.finished_at = iso(utcnow())
    record.ok = outcome.failed == 0

    cosmos.upsert(record.to_doc(), container=_legacy())
    log.info(
        "Harvest complete: polled=%d unchanged=%d failed=%d seen=%d new=%d too_old=%d stale=%s",
        outcome.polled,
        outcome.unchanged,
        outcome.failed,
        outcome.items_seen,
        outcome.items_new,
        outcome.items_too_old,
        outcome.stale_feeds,
    )
    return record
