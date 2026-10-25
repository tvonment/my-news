"""Document shapes, ids and field names for the Cosmos containers.

One container per generation, partition key ``/pk``, type-discriminated
documents. Partition keys are chosen so that no logical partition can grow
without bound: articles and stories are bucketed by day, because 20 GB is a hard
cap *per logical partition* and repartitioning later would mean a full
migration.

Two generations live here side by side until Phase 2 deletes the first:

* **v1** (container ``items``): ``USER_PK``, ``SOURCE_PK``, ``RUN_PK``,
  ``article_pk()``, ``Article``, ``SourceState`` and ``RunRecord``, still written
  by the v1 harvest.
* **v2** (container ``news``): the ``PK_*`` partitions, the id builders, the
  ``T_*`` type discriminators and the ``F_*`` query field names below. The
  storage side of each document is specified in docs/data-model.md.

Every field name a v2 query filters or sorts on is an ``F_*`` constant, and
test_index_coverage.py checks those constants against the container's opt-in
index policy (infra/modules/cosmos-index-policy.json): a filter on a path that
is not indexed is rejected by Cosmos, not merely slow.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from typing import Any, Literal

# The version of the HTTP contract this backend serves (docs/api.md section 4).
# GET /health reports it; within API 1 every change is additive.
API_VERSION = 1

# --- v1 (container `items`) -------------------------------------------------
USER_PK = "user|me"
SOURCE_PK = "src"
RUN_PK = "run"

DocType = Literal[
    # v1
    "article", "config", "feedback", "source_state", "run",
    # v2, Phase 1 (docs/data-model.md section 4)
    "me", "user", "invite", "deletion", "tombstone", "audit", "quota",
    # v2, Phases 2 and 3 (draft)
    "source", "vocab", "vocab_pointer", "ring", "story", "summary", "budget", "pool",
    "lease", "migration", "coverage", "subs", "model", "edition", "fb", "save",
    "sugg_ref", "suggestion", "report",
]

# --- v2 partitions (container `news`) ---------------------------------------
PK_SYS = "sys"
PK_CATALOG = "catalog"
PK_SRC = "src"
PK_USERS = "users"

# --- v2 grammars --------------------------------------------------------------
# Entra object id as tokens carry it: a lower-case GUID. The oid only ever
# reaches a partition key through user_pk(), which checks it, so a malformed
# value cannot address someone else's partition or an odd one.
OID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
UID_RE = re.compile(r"^[0-9a-f]{32}$")
INVITE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
STORY_ID_RE = re.compile(r"^s\d{8}[A-Za-z0-9_-]{12}$")
EDITION_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.(morning|midday|evening)$")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SLUG_MAX = 40
# Client-generated event and report ids: any UUID version, any case on input,
# stored lower-case.
EVENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
CONSENT_VERSION_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def is_slug(value: object) -> bool:
    return isinstance(value, str) and len(value) <= SLUG_MAX and SLUG_RE.match(value) is not None


def is_edition_id(value: object) -> bool:
    """The grammar *and* a real date: 2026-02-30.morning matches the pattern."""
    if not isinstance(value, str) or EDITION_ID_RE.match(value) is None:
        return False
    try:
        date.fromisoformat(value[:10])
    except ValueError:
        return False
    return True


def user_pk(oid: str) -> str:
    """The caller's own partition. ``oid`` comes from a validated token only."""
    if not isinstance(oid, str) or OID_RE.match(oid) is None:
        raise ValueError("oid must be a lower-case GUID")
    return f"user|{oid}"


def article_partition(first_seen: datetime) -> str:
    """v2 article bucket: the first-seen UTC day (P2). v1's article_pk() buckets
    by publish day instead, and stays for the v1 harvest."""
    return f"article|{first_seen.astimezone(timezone.utc):%Y-%m-%d}"


def story_partition(day: date | datetime) -> str:
    """Story bucket by first-seen UTC day (P2); a storyId carries the same date."""
    if isinstance(day, datetime):
        day = day.astimezone(timezone.utc).date()
    return f"story|{day:%Y-%m-%d}"


# --- v2 fixed ids -------------------------------------------------------------
ME_ID = "me"
SUBS_ID = "subs"
MODEL_ID = "model"
POOL_ID = "pool"


def invite_doc_id(invite_id: str) -> str:
    return f"invite|{invite_id}"


def deletion_doc_id(uid: str) -> str:
    return f"deletion|{uid}"


def tombstone_doc_id(uid: str) -> str:
    return f"tombstone|{uid}"


def audit_doc_id(at: datetime, suffix: str) -> str:
    """Time first, so ids sort by time; ``suffix`` (8 random hex) so two entries
    in the same second cannot collide."""
    return f"audit|{at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}|{suffix}"


def quota_day_doc_id(at: datetime) -> str:
    return f"quota|{at.astimezone(timezone.utc):%Y-%m-%d}"


def quota_week_doc_id(at: datetime) -> str:
    """ISO year and week: the week of 2026-12-31 can belong to 2027 (W53/W01
    edges), and the weekly reset is Monday 00:00 UTC to match."""
    year, week, _ = at.astimezone(timezone.utc).isocalendar()
    return f"quota|w{year}-W{week:02d}"


def run_doc_id(kind: str, started_at: datetime) -> str:
    return f"run|{kind}|{started_at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"


def lease_doc_id(name: str) -> str:
    return f"lease|{name}"


def source_doc_id(source_id: str) -> str:
    return f"src|{source_id}"


def edition_doc_id(edition_id: str) -> str:
    return f"ed|{edition_id}"


def feedback_doc_id(event_id: str) -> str:
    return f"fb|{event_id.lower()}"


def save_doc_id(story_id: str) -> str:
    return f"save|{story_id}"


# --- v2 type discriminators ---------------------------------------------------
T_ME = "me"
T_USER = "user"
T_INVITE = "invite"
T_DELETION = "deletion"
T_TOMBSTONE = "tombstone"
T_AUDIT = "audit"
T_QUOTA = "quota"
# Phases 2 and 3 (draft).
T_SOURCE = "source"
T_VOCAB = "vocab"
T_SOURCE_STATE = "source_state"
T_RING = "ring"
T_ARTICLE = "article"
T_STORY = "story"
T_SUMMARY = "summary"
T_BUDGET = "budget"
T_POOL = "pool"
T_LEASE = "lease"
T_RUN = "run"
T_MIGRATION = "migration"
T_SUBS = "subs"
T_MODEL = "model"
T_EDITION = "edition"
T_FEEDBACK = "fb"
T_SAVE = "save"
T_SUGG_REF = "sugg_ref"
T_SUGGESTION = "suggestion"
T_REPORT = "report"

# --- v2 query field names -----------------------------------------------------
# Exactly the container's includedPaths (docs/data-model.md section 6.1).
F_TYPE = "type"
F_STATUS = "status"
F_KIND = "kind"
F_SIGNAL = "signal"
F_UID = "uid"
F_CREATED_AT = "created_at"
F_LAST_ACTIVE_AT = "last_active_at"
F_DELETION_REQUESTED_AT = "deletion_requested_at"
F_ENRICH_STATUS = "enrich_status"
F_FIRST_SEEN_AT = "first_seen_at"
F_LAST_PUB_AT = "last_pub_at"
F_SOURCE_IDS = "source_ids"
QUERY_FIELDS = (
    F_TYPE, F_STATUS, F_KIND, F_SIGNAL, F_UID, F_CREATED_AT, F_LAST_ACTIVE_AT,
    F_DELETION_REQUESTED_AT, F_ENRICH_STATUS, F_FIRST_SEEN_AT, F_LAST_PUB_AT, F_SOURCE_IDS,
)

# --- v2 statuses --------------------------------------------------------------
MEMBER_ACTIVE = "active"
MEMBER_BLOCKED = "blocked"
MEMBER_DELETING = "deleting"
MEMBER_STATUSES = (MEMBER_ACTIVE, MEMBER_BLOCKED, MEMBER_DELETING)
# Stored invite statuses. The wire adds "used" and "expired", which are derived
# when reading (core.invites.wire_status), never stored.
INVITE_ACTIVE = "active"
INVITE_REVOKED = "revoked"
DELETION_PURGING = "purging"
DELETION_AWAITING = "awaiting_identity_removal"
DELETION_COMPLETED = "completed"
DELETION_STATUSES = (DELETION_PURGING, DELETION_AWAITING, DELETION_COMPLETED)

# --- v2 TTLs, in seconds ------------------------------------------------------
# TTL counts from the document's last write (_ts), so a document that must
# expire at a fixed time recomputes its ttl on every write.
TTL_QUOTA_DAY = 2 * 24 * 3600
TTL_QUOTA_WEEK = 8 * 24 * 3600
TTL_AUDIT = 365 * 24 * 3600
TTL_TOMBSTONE_AFTER_REMOVAL = 30 * 24 * 3600
TTL_DELETION_AFTER_COMPLETE = 90 * 24 * 3600
# An invite document is kept this long past its expiry, then TTL deletes it.
INVITE_KEEP_AFTER_EXPIRY = 90 * 24 * 3600

# --- v2 enums -----------------------------------------------------------------
SLOTS = ("morning", "midday", "evening")
LOCALES = ("en", "de-CH")
CONTENT_LANGUAGES = ("de", "en")
BRIEF_SIZES = (10, 12, 15)
EMPHASIS = ("interests", "balanced", "big_stories")
HISTORY_RETENTION_DAYS = (30, 90, 180)
SECTION_IDS = (
    "switzerland", "world", "business", "technology", "ai", "software", "security",
    "science", "climate",
)

# Cosmos system properties: stripped from anything written back, and from the
# data export.
SYSTEM_PROPERTIES = frozenset({"_rid", "_self", "_etag", "_attachments", "_ts"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """The one timestamp format, on the wire and in storage: UTC, whole seconds,
    a literal Z. Because it is fixed-width, comparing two of these strings
    compares the times, which every range filter (c.last_active_at >= @since)
    and every inlined predicate (c.expires_at > '...') relies on."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """The inverse of iso(), for timestamps this backend wrote. Raises ValueError."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no offset")
    return parsed.astimezone(timezone.utc)


def article_id(canonical_url: str) -> str:
    """Content-addressed id, so dedupe is a 409 on create rather than a query.

    base64**url** rather than plain base64: Cosmos strongly recommends
    alphanumeric-ASCII ids because ``/`` and ``+`` in an id hit known SDK bugs.
    """
    digest = hashlib.sha256(canonical_url.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def article_pk(published: datetime) -> str:
    """Day bucket. Keeps 'today's feed' a single-partition query — the cheapest
    kind — and bounds every logical partition to roughly one day of articles."""
    return f"art|{published.astimezone(timezone.utc):%Y-%m-%d}"


@dataclass
class Article:
    id: str
    pk: str
    type: DocType = field(default="article", init=False)

    url: str = ""
    canonical_url: str = ""
    source_id: str = ""
    source_name: str = ""
    title: str = ""
    lead: str = ""
    published_at: str = ""
    fetched_at: str = ""

    # Set by the gate stage.
    embedding: list[float] | None = None
    gate_score: float | None = None

    # Set by the score stage.
    relevance: int | None = None
    categories: list[str] = field(default_factory=list)
    why: str | None = None

    # Set by the summarize stage.
    summary: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    reading_minutes: int | None = None
    paywalled: bool = False
    # Carried from the source. Satire has to travel with the article all the way
    # to the summarizer and the UI, because a summary strips the tonal cues that
    # tell a reader it is a joke.
    satire: bool = False
    # True when the lead was backfilled from the article page because the feed's
    # own teaser was too thin to rank on.
    lead_enriched: bool = False

    # Set by the cluster stage. `cluster_id` is the id of the primary article.
    cluster_id: str | None = None
    also_reported_by: list[str] = field(default_factory=list)

    state: str = "harvested"  # harvested -> gated -> scored -> summarized
    ttl: int | None = None

    def to_doc(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["type"] = "article"
        # Cosmos rejects a null ttl; omit the property entirely to inherit the
        # container default (which is -1 = never expire unless set per item).
        if doc.get("ttl") is None:
            doc.pop("ttl", None)
        if doc.get("embedding") is None:
            doc.pop("embedding", None)
        # `IS_DEFINED()` is TRUE for an explicit null in Cosmos, so writing
        # `cluster_id: null` makes `NOT IS_DEFINED(c.cluster_id)` false and any
        # query using it silently drops every unclustered article. Omit the key
        # instead of storing a null.
        if doc.get("cluster_id") is None:
            doc.pop("cluster_id", None)
        return doc


@dataclass
class SourceState:
    """Per-feed fetch cursor.

    Both validators are stored because ETag is broken on a meaningful number of
    real feeds (returns 200 + full body instead of 304) and roughly half the
    large German feeds send neither. ``body_sha256`` is the fallback that makes
    those feeds cheap anyway: same hash means skip parsing.
    """

    id: str
    pk: str = SOURCE_PK
    type: DocType = field(default="source_state", init=False)

    resolved_url: str = ""
    etag: str | None = None
    last_modified: str | None = None
    body_sha256: str | None = None
    # None = untried, True = server honours If-None-Match, False = server returns
    # 200 + full body despite a matching ETag. Learned per source rather than
    # hardcoded, because the set of broken servers changes over time.
    etag_works: bool | None = None
    last_fetch_at: str | None = None
    last_success_at: str | None = None
    # Newest item date seen. Drives zombie-feed detection: news.microsoft.com/feed/
    # returns HTTP 200 with a fresh Last-Modified but has not published since
    # 2025-05-07, so HTTP status alone proves nothing.
    newest_item_at: str | None = None
    consecutive_failures: int = 0
    last_error: str | None = None

    def to_doc(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["type"] = "source_state"
        return doc


@dataclass
class RunRecord:
    """One pipeline run. Carries the real token counts so cost claims stay
    honest rather than aspirational."""

    id: str
    pk: str = RUN_PK
    type: DocType = field(default="run", init=False)

    kind: str = "harvest"  # harvest | digest
    started_at: str = ""
    finished_at: str | None = None
    ok: bool = True
    error: str | None = None

    feeds_polled: int = 0
    feeds_unchanged: int = 0
    feeds_failed: int = 0
    items_seen: int = 0
    items_new: int = 0
    items_enriched: int = 0
    items_too_old: int = 0
    items_gated: int = 0
    clusters_formed: int = 0
    items_scored: int = 0
    items_summarized: int = 0

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    embedding_tokens: int = 0
    estimated_usd: float = 0.0
    budget_exhausted: bool = False

    ttl: int = 60 * 60 * 24 * 90  # 90 days

    def to_doc(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["type"] = "run"
        return doc
