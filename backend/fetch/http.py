"""Conditional, polite feed fetching.

Every rule here exists because a real feed broke a naive fetcher during the
2026-07-27 survey. In order:

1. **Per-host User-Agent override.** ``developer.apple.com/news/rss/news.rss``
   returns HTTP 500 to curl, NetNewsWire *and* Chrome user agents. Only a Safari
   UA gets 200.
2. **Honest default UA.** derStandard, Spiegel, ORF, Zeit and FAZ all `Disallow`
   ClaudeBot / GPTBot / anthropic-ai / CCBot *by name* while leaving
   ``User-agent: *`` open. A plainly-named reader is welcome; an AI-sounding one
   is blocked.
3. **gzip.** Measured ~80% saving on derStandard, FAZ and heise — and it matters
   more than conditional GET, because those large German feeds are precisely the
   ones that send no validator.
4. **If-Modified-Since preferred over If-None-Match.** ETag validation is
   outright broken on a meaningful set of feeds: they return 200 with a full body
   instead of 304. Rather than hardcode a blocklist, this learns per source.
5. **Body-hash fallback.** Roughly half the large feeds send neither validator.
   Hashing the body still lets us skip parsing, which is the expensive part.
6. **Hard caps.** ``openai.com/news/rss.xml`` returns 1,051 items in 638 KB and
   one devblogs item was 566 KB on its own.

Note on caching: state is keyed by *source*, never by validator value. ETags are
not unique across hosts — ``devblogs/feed`` and ``devblogs/typescript/feed``
return the same ETag for different content, as do ``techcrunch.com`` and
``9to5mac.com``. A cache indexed by validator alone would let one feed's 304
suppress another's fetch.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from core.config import get_settings
from core.models import SourceState

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass
class FetchResult:
    ok: bool
    status: int | None = None
    unchanged: bool = False
    body: bytes | None = None
    etag: str | None = None
    last_modified: str | None = None
    body_sha256: str | None = None
    resolved_url: str | None = None
    error: str | None = None
    # True when we sent If-None-Match and the server answered 200 with a body we
    # already had — i.e. its ETag validation does not work.
    etag_ignored: bool = False


def user_agent_for(url: str) -> str:
    settings = get_settings()
    host = urlsplit(url).hostname or ""
    for suffix, agent in settings.user_agent_overrides.items():
        if host == suffix or host.endswith(f".{suffix}"):
            return agent
    return settings.user_agent


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_feed(
    url: str,
    state: SourceState | None = None,
    client: httpx.Client | None = None,
) -> FetchResult:
    """Fetch one feed, returning ``unchanged=True`` when there is nothing new.

    ``state`` carries the cursor from the previous run and is not mutated here —
    the caller decides what to persist.
    """
    headers = {
        "User-Agent": user_agent_for(url),
        "Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    }

    sent_if_none_match = False
    if state is not None:
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        # Only offer the ETag while we still believe the server honours it.
        if state.etag and getattr(state, "etag_works", None) is not False:
            headers["If-None-Match"] = state.etag
            sent_if_none_match = True

    owns_client = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=DEFAULT_TIMEOUT, http2=True)

    try:
        with client.stream("GET", url, headers=headers) as response:
            resolved = str(response.url)

            if response.status_code == 304:
                return FetchResult(
                    ok=True,
                    status=304,
                    unchanged=True,
                    etag=state.etag if state else None,
                    last_modified=state.last_modified if state else None,
                    body_sha256=state.body_sha256 if state else None,
                    resolved_url=resolved,
                )

            if response.status_code >= 400:
                return FetchResult(
                    ok=False,
                    status=response.status_code,
                    resolved_url=resolved,
                    error=f"HTTP {response.status_code}",
                )

            chunks: list[bytes] = []
            size = 0
            truncated = False
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    truncated = True
                    break
                chunks.append(chunk)

            if truncated:
                log.warning("Feed %s exceeded %d bytes; truncating", url, MAX_BODY_BYTES)

            body = b"".join(chunks)
            digest = _sha256(body)

            # Servers that ignore conditional headers still hand us the same
            # bytes. Hashing turns a wasted download into a skipped parse.
            if state is not None and state.body_sha256 == digest:
                return FetchResult(
                    ok=True,
                    status=response.status_code,
                    unchanged=True,
                    body=body,
                    etag=response.headers.get("ETag") or state.etag,
                    last_modified=response.headers.get("Last-Modified") or state.last_modified,
                    body_sha256=digest,
                    resolved_url=resolved,
                    etag_ignored=sent_if_none_match,
                )

            last_modified = response.headers.get("Last-Modified")
            # Some feeds send the header with an empty value, which is worse than
            # not sending it — storing it would produce a meaningless request header.
            if last_modified is not None and not last_modified.strip():
                last_modified = None

            return FetchResult(
                ok=True,
                status=response.status_code,
                unchanged=False,
                body=body,
                etag=response.headers.get("ETag"),
                last_modified=last_modified,
                body_sha256=digest,
                resolved_url=resolved,
            )

    except httpx.HTTPError as exc:
        return FetchResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if owns_client:
            client.close()
