"""Feed parsing, format sniffing and URL canonicalization.

Filenames and Content-Type headers lie about feed format, verified 2026-07-27:

* ``apple.com/newsroom/rss-feed.rss`` serves **Atom** with
  ``Content-Type: application/rss+xml``
* ``heise.de/rss/heise.rdf`` serves **RSS 2.0** despite the ``.rdf`` extension
* ``theverge.com/rss/index.xml`` is **Atom**

So format is determined by the root element. ``feedparser`` handles all three
dialects itself, which is why it is the parser here — the sniffing exists to
label sources correctly and to reject HTML pages that masquerade as feeds
(``azure.microsoft.com/en-us/updates/feed/`` 302s to a 195 KB HTML page).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

log = logging.getLogger(__name__)

MAX_ITEMS_PER_FEED = 120
MAX_ITEM_TEXT_CHARS = 20_000

# Query parameters that identify a referrer or campaign rather than a document.
# The first three are real cases from the survey: derStandard appends ?ref=rss,
# heise appends ?wt_mc=..., Spiegel appends #ref=rss.
_TRACKING_PARAMS = frozenset(
    {
        "ref",
        "wt_mc",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "fbclid",
        "gclid",
        "msclkid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "at_medium",
        "at_campaign",
        "oc",
        "cmp",
        "sh",
    }
)

_ROOT_RE = re.compile(rb"<\s*([A-Za-z_][\w.:-]*)")
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


class NotAFeedError(ValueError):
    """Raised when a URL returns something that is not RSS/Atom/RDF."""


def sniff_format(body: bytes) -> str:
    """Return 'rss', 'atom', 'rdf' — or raise if this is not a feed at all."""
    head = body[:4096]
    for match in _ROOT_RE.finditer(head):
        tag = match.group(1).decode("ascii", "ignore").lower()
        if tag.startswith(("?xml", "!doctype", "!--")):
            continue
        local = tag.split(":")[-1]
        if local == "rss":
            return "rss"
        if local == "feed":
            return "atom"
        if local == "rdf":
            return "rdf"
        if local == "html":
            raise NotAFeedError("served an HTML page, not a feed")
        break
    raise NotAFeedError("no recognizable feed root element")


def canonicalize_url(url: str) -> str:
    """Strip tracking noise so the same article from two paths hashes identically.

    This is layer 1 of dedupe and it is free. Layer 2 is a normalized-title hash
    (catches verbatim APA/dpa syndication), layer 3 is embedding similarity.
    """
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    if parts.port and not ((scheme == "https" and parts.port == 443) or (scheme == "http" and parts.port == 80)):
        netloc = f"{host}:{parts.port}"

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"

    # Fragments are never part of document identity here; Spiegel's #ref=rss is
    # the motivating case.
    return urlunsplit((scheme, netloc, path, urlencode(kept), ""))


def normalize_title(title: str) -> str:
    """Lowercase, collapse whitespace and drop a trailing outlet suffix.

    Catches the same wire story republished verbatim under different URLs.
    """
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", title or "")).strip()
    text = re.split(r"\s+[|–—-]\s+", text)[0] if text.count(" ") > 3 else text
    return text.casefold().strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", value)).strip()[:MAX_ITEM_TEXT_CHARS]


@dataclass
class ParsedItem:
    title: str
    url: str
    canonical_url: str
    lead: str
    published: datetime


@dataclass
class ParsedFeed:
    format: str
    items: list[ParsedItem]
    newest: datetime | None


def _entry_datetime(entry: Any, fallback: datetime) -> datetime:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return fallback


def _entry_lead(entry: Any) -> str:
    # Teaser length varies by two orders of magnitude across real feeds: Hugging
    # Face sends no description field at all, the Gurman author feed sends empty
    # ones, and the Azure blog sends ~22k chars. Prefer the summary, fall back to
    # content, and accept that some items legitimately have no lead.
    for key in ("summary", "description"):
        value = entry.get(key) if isinstance(entry, dict) else getattr(entry, key, None)
        if value:
            return strip_html(value)
    contents = entry.get("content") if isinstance(entry, dict) else getattr(entry, "content", None)
    if contents:
        first = contents[0]
        value = first.get("value") if isinstance(first, dict) else getattr(first, "value", None)
        if value:
            return strip_html(value)
    return ""


def parse_feed(body: bytes, fetched_at: datetime | None = None) -> ParsedFeed:
    """Parse a feed body into items, capped and normalized."""
    fmt = sniff_format(body)
    fetched_at = fetched_at or datetime.now(timezone.utc)

    parsed = feedparser.parse(body)
    entries = list(parsed.entries)[:MAX_ITEMS_PER_FEED]

    items: list[ParsedItem] = []
    newest: datetime | None = None

    for entry in entries:
        link = entry.get("link") if isinstance(entry, dict) else getattr(entry, "link", None)
        title = entry.get("title") if isinstance(entry, dict) else getattr(entry, "title", None)
        if not link or not title:
            continue

        published = _entry_datetime(entry, fetched_at)
        # A feed dated in the future is a clock or parsing problem; clamp it so
        # one bad item cannot dominate recency ranking forever.
        if published > fetched_at:
            published = fetched_at
        if newest is None or published > newest:
            newest = published

        items.append(
            ParsedItem(
                title=strip_html(title),
                url=link,
                canonical_url=canonicalize_url(link),
                lead=_entry_lead(entry),
                published=published,
            )
        )

    return ParsedFeed(format=fmt, items=items, newest=newest)
