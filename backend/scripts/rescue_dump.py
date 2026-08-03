#!/usr/bin/env python
"""Recover every point-readable document before the container is mutated.

Written for the 2026-08-02 outage: Cosmos began returning
``InternalServerError`` for *every* query on this container because its data
plane can no longer parse the ``float16`` vector data type in its own stored
policy. Queries, the change feed and ReadFeed are all dead — verified against
the raw REST API across every ``x-ms-version`` from 2015 to 2025 — so there is
no way to enumerate the container. Point reads survive, with an intermittent
500 that clears on retry.

That leaves exactly one recovery strategy: read back the documents whose ids we
can reconstruct without asking Cosmos what exists.

- config and ``source_state`` documents have fixed, known ids.
- articles are content-addressed (``id = base64url(sha256(canonical_url))``,
  ``pk = art|<published date>``), so re-fetching the same feeds regenerates the
  ids of every article still inside a feed window — which is the set the gate
  cares about anyway.

Feedback events are unrecoverable by design: their ids embed
``datetime.now().isoformat()`` at microsecond precision. The derived state
survives in the ``centroids`` document, which is why that one is the gate.

READ-ONLY. Issues only ``read_item`` against Cosmos, plus ordinary feed GETs.

    cd backend && .venv/bin/python scripts/rescue_dump.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from azure.cosmos import CosmosClient  # noqa: E402
from azure.cosmos.exceptions import (  # noqa: E402
    CosmosHttpResponseError,
    CosmosResourceNotFoundError,
)
from azure.identity import AzureCliCredential  # noqa: E402

from core.config import get_settings, require  # noqa: E402
from core.models import (  # noqa: E402
    SOURCE_PK,
    USER_PK,
    article_id,
    article_pk,
    utcnow,
)
from core.sources import _from_seed  # noqa: E402
from fetch.http import DEFAULT_TIMEOUT, fetch_feed  # noqa: E402
from fetch.parse import NotAFeedError, parse_feed  # noqa: E402

# The vector policy also breaks the SDK's own retry logging; keep the noise down.
logging.getLogger("azure").setLevel(logging.ERROR)

# Config documents written ad hoc on USER_PK. Ids come from the modules that
# own them: score.PROFILE_DOC_ID, gate.INTERESTS_DOC_ID/CENTROIDS_DOC_ID,
# sources.SOURCES_DOC_ID. Importing those modules would pull in numpy and the
# OpenAI client for no reason, so they are restated here.
CONFIG_DOC_IDS = ("profile", "interests", "centroids", "sources", "settings")

# budget|YYYY-MM-DD documents. Wider than any plausible retention.
BUDGET_DAYS = 40

# Retry budget for the intermittent 500s. Six attempts with exponential backoff
# covers every failure observed while diagnosing the outage.
READ_ATTEMPTS = 6


def _out_dir() -> Path:
    """Sibling of the repo, never inside ``backend/``.

    ``azure.yaml`` ships all of ``./backend`` as the Flex deployment package, so
    a dump written under it would be uploaded to Azure on the next deploy.
    """
    return (
        Path(__file__).resolve().parents[2]
        / "rescue"
        / f"dump-{utcnow():%Y%m%dT%H%M%SZ}"
    )


class Rescuer:
    def __init__(self) -> None:
        settings = get_settings()
        endpoint = require(settings.cosmos_endpoint, "COSMOS_ENDPOINT")
        client = CosmosClient(endpoint, credential=AzureCliCredential())
        self.container = client.get_database_client(
            require(settings.cosmos_database, "COSMOS_DATABASE")
        ).get_container_client(require(settings.cosmos_container, "COSMOS_CONTAINER"))
        # Keyed by (pk, id) so a document reached by two routes is stored once.
        self.docs: dict[tuple[str, str], dict[str, Any]] = {}
        self.gave_up: list[str] = []

    def read(self, doc_id: str, pk: str) -> dict[str, Any] | None:
        """Point read, retrying the transient 500s the broken container throws."""
        for attempt in range(READ_ATTEMPTS):
            try:
                return self.container.read_item(item=doc_id, partition_key=pk)
            except CosmosResourceNotFoundError:
                return None
            except CosmosHttpResponseError as exc:
                if exc.status_code not in (408, 429, 500, 503):
                    raise
                time.sleep(0.3 * 2**attempt)
        self.gave_up.append(f"{pk}/{doc_id}")
        return None

    def keep(self, doc: dict[str, Any] | None) -> bool:
        if not doc:
            return False
        self.docs[(doc["pk"], doc["id"])] = doc
        return True

    def collect(self, doc_id: str, pk: str) -> bool:
        return self.keep(self.read(doc_id, pk))

    def count(self, pk: str) -> int:
        return sum(1 for doc_pk, _ in self.docs if doc_pk == pk)


def rescue_config(r: Rescuer) -> None:
    print(f"config documents on {USER_PK}")
    for doc_id in CONFIG_DOC_IDS:
        found = r.collect(doc_id, USER_PK)
        print(f"  {doc_id:<10} {'ok' if found else 'not found'}")

    now = utcnow()
    budgets = sum(
        r.collect(f"budget|{(now - timedelta(days=n)):%Y-%m-%d}", USER_PK)
        for n in range(BUDGET_DAYS)
    )
    print(f"  {'budgets':<10} {budgets} of the last {BUDGET_DAYS} days")


def rescue_source_state(r: Rescuer, sources: list[Any]) -> None:
    print(f"source_state documents on {SOURCE_PK}")
    found = sum(r.collect(source.id, SOURCE_PK) for source in sources)
    print(f"  recovered {found} of {len(sources)}")


def rescue_articles(r: Rescuer, sources: list[Any]) -> None:
    """Re-derive article ids by re-fetching the feeds they came from.

    ``article_id`` is a hash of the canonical URL and ``article_pk`` a day
    bucket, so a fresh parse of the same feed regenerates the exact coordinates
    of every article we stored from it.
    """
    print("articles, re-derived from live feeds")
    now = utcnow()
    recovered = 0

    with httpx.Client(
        follow_redirects=True, timeout=DEFAULT_TIMEOUT, http2=True
    ) as client:
        for source in sources:
            result = fetch_feed(source.url, None, client=client)
            if not result.ok or not result.body:
                print(f"  {source.id:<20} fetch failed: {result.error or result.status}")
                continue
            try:
                parsed = parse_feed(result.body, fetched_at=now)
            except NotAFeedError as exc:
                print(f"  {source.id:<20} not a feed: {exc}")
                continue

            hits = 0
            for item in parsed.items:
                doc_id = article_id(item.canonical_url)
                # `_entry_datetime` falls back to `fetched_at` for feeds that
                # carry no date, and a UTC-midnight boundary shifts the bucket,
                # so probe the neighbouring days too. A miss costs ~1 RU.
                for offset in (0, -1, 1):
                    if r.collect(doc_id, article_pk(item.published + timedelta(days=offset))):
                        hits += 1
                        break
            recovered += hits
            print(f"  {source.id:<20} {hits}/{len(parsed.items)}")

    print(f"  recovered {recovered} articles")


def write_dump(r: Rescuer, out: Path) -> dict[str, int]:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for doc in r.docs.values():
        by_type.setdefault(doc.get("type", "unknown"), []).append(doc)

    out.mkdir(parents=True, exist_ok=True)

    def write(name: str, payload: Any) -> None:
        (out / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    for doc_type, items in by_type.items():
        write(f"{doc_type}.json", items)
    write("all-documents.json", list(r.docs.values()))

    counts = {k: len(v) for k, v in by_type.items()}
    write(
        "manifest.json",
        {
            "taken_at": utcnow().isoformat(),
            "reason": "float16 vectorEmbeddingPolicy outage; queries and ReadFeed "
            "both return InternalServerError, so this is a point-read rescue",
            "counts": counts,
            "total": len(r.docs),
            "unreadable": r.gave_up,
        },
    )
    return counts


def main() -> int:
    r = Rescuer()
    sources = _from_seed()

    rescue_config(r)
    rescue_source_state(r, sources)
    rescue_articles(r, [s for s in sources if s.enabled])

    out = _out_dir()
    counts = write_dump(r, out)

    print(f"\n{len(r.docs)} documents -> {out}")
    for doc_type, n in sorted(counts.items()):
        print(f"  {doc_type:<14} {n}")
    if r.gave_up:
        print(f"\n{len(r.gave_up)} documents were unreadable after {READ_ATTEMPTS} attempts:")
        for ref in r.gave_up[:20]:
            print(f"  {ref}")

    # The gate. `centroids` is the only copy of the learned model — the running
    # mean of liked and disliked embeddings that survives a container recreate.
    # Nothing downstream should mutate the container without it on disk.
    if ("user|me", "centroids") not in r.docs:
        print("\nFAIL: `centroids` was not recovered. Do not mutate the container.")
        return 1

    print("\nOK: `centroids` recovered. Safe to proceed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
