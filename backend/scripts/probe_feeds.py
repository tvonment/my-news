#!/usr/bin/env python
"""Probe the configured feeds and report what each one actually does.

Run this whenever a feed misbehaves, or before adding a new source:

    cd backend && .venv/bin/python scripts/probe_feeds.py            # all feeds
    cd backend && .venv/bin/python scripts/probe_feeds.py heise verge

It performs two fetches per feed — the second one conditional, using the cursor
from the first — so it reports whether validators actually work rather than
whether the server merely claims to send them. Touches no Azure resources.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from core.models import SourceState  # noqa: E402
from core.sources import _from_seed  # noqa: E402
from fetch.http import DEFAULT_TIMEOUT, fetch_feed, user_agent_for  # noqa: E402
from fetch.parse import NotAFeedError, parse_feed  # noqa: E402


def probe(source, client: httpx.Client) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    row: dict[str, object] = {"id": source.id, "ua": "safari" if "Safari" in user_agent_for(source.url) else "bot"}

    first = fetch_feed(source.url, None, client=client)
    if not first.ok:
        row["status"] = f"FAIL {first.error}"
        return row

    row["http"] = first.status
    row["bytes"] = len(first.body or b"")

    try:
        parsed = parse_feed(first.body or b"", fetched_at=now)
    except NotAFeedError as exc:
        row["status"] = f"NOT-A-FEED {exc}"
        return row

    row["format"] = parsed.format
    row["items"] = len(parsed.items)
    leads = [len(i.lead) for i in parsed.items]
    row["lead_median"] = sorted(leads)[len(leads) // 2] if leads else 0

    if parsed.newest:
        age_h = (now - parsed.newest).total_seconds() / 3600
        row["newest_age_h"] = round(age_h, 1)
        row["zombie"] = age_h > source.stale_after_hours

    state = SourceState(
        id=source.id,
        etag=first.etag,
        last_modified=first.last_modified,
        body_sha256=first.body_sha256,
    )
    second = fetch_feed(source.url, state, client=client)
    if second.status == 304:
        row["revalidate"] = "304"
    elif second.unchanged:
        row["revalidate"] = "200-but-same-body" + (" (etag ignored)" if second.etag_ignored else "")
    else:
        row["revalidate"] = "changed"

    row["status"] = "ok"
    return row


def main() -> int:
    wanted = {a.lower() for a in sys.argv[1:]}
    sources = [s for s in _from_seed() if s.enabled]
    if wanted:
        sources = [s for s in sources if any(w in s.id.lower() for w in wanted)]

    print(f"Probing {len(sources)} feeds\n")
    header = f"{'id':<20} {'ua':<7} {'fmt':<5} {'items':>5} {'lead':>5} {'age_h':>7}  {'revalidate':<28} status"
    print(header)
    print("-" * len(header))

    failures = 0
    with httpx.Client(follow_redirects=True, timeout=DEFAULT_TIMEOUT, http2=True) as client:
        for source in sources:
            row = probe(source, client)
            status = str(row.get("status", "?"))
            if status != "ok":
                failures += 1
            zombie = " ZOMBIE" if row.get("zombie") else ""
            print(
                f"{row['id']:<20} {str(row.get('ua','')):<7} {str(row.get('format','-')):<5} "
                f"{str(row.get('items','-')):>5} {str(row.get('lead_median','-')):>5} "
                f"{str(row.get('newest_age_h','-')):>7}  {str(row.get('revalidate','-')):<28} {status}{zombie}"
            )

    print(f"\n{len(sources) - failures}/{len(sources)} feeds healthy")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
