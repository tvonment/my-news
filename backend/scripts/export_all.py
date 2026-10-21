#!/usr/bin/env python
"""Export every document in the container before v2 starts changing it.

``rescue_dump.py`` was written for an outage in which queries were dead and only
point reads worked, so it can only recover documents whose ids it can rebuild.
Queries work again, which makes a complete copy possible: this script streams
``SELECT * FROM c`` across all partitions and writes everything to disk.

It exists for Phase 0 of the v2 rework, which mutates the only copy of this
data: the switch to continuous backup is one-way, App* retention drops to 30
days, the v1 routes are deleted, and Phase 5 deletes the ``items`` container.
The periodic backup it replaces keeps 48 hours and restores only by support
ticket, so before any of that happens there has to be a copy we control.

Output, per run, under ``<repo>/rescue/export-<UTC>/`` (gitignored):

- ``<pk prefix>__<type>.jsonl``: one document per line, grouped by the part of
  the partition key before ``|`` (``art``, ``user``, ``src``, ``run`` …) and
  by ``type``.
- ``pinned_likes.jsonl``: articles with ``ttl == -1``, i.e. the ones a like
  made permanent. These are what v2 imports as saves, so they get their own file.
- ``manifest.json``: counts, sizes and sha256 per file, total RU charged,
  and the endpoint/database/container exported.

READ-ONLY. Issues only queries. Paced to stay well under the free tier's
1,000 RU/s, because harvest still writes to this container while it runs.

    COSMOS_ENDPOINT=... .venv/bin/python scripts/export_all.py
    (COSMOS_DATABASE and COSMOS_CONTAINER default to mynews/items)

Credentials come from ``az login`` (AzureCliCredential); the account is
RBAC-only, so the signed-in principal needs a Cosmos data-plane role.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO
from urllib.parse import urlsplit

from azure.cosmos import CosmosClient
from azure.identity import AzureCliCredential

# The Cosmos SDK logs every request and response header at INFO; an export of
# ~15k documents would print tens of thousands of lines.
logging.getLogger("azure").setLevel(logging.WARNING)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"

# Documents per page. Small pages keep each request cheap and let the pacer
# react before a burst of large article documents saturates a second.
PAGE_SIZE = 100

# Ceiling on the export's own consumption. The free-tier account has 1,000 RU/s
# in total, and harvest writes on the hour and half hour; 300 leaves it room.
MAX_RU_PER_SECOND = 300.0


def _out_dir(base: Path | None) -> Path:
    """``<repo>/rescue/export-<UTC>/``, and never anywhere inside ``backend/``.

    ``azure.yaml`` deploys all of ``./backend`` as the Flex package, so an
    export written under it would be uploaded to Azure on the next deploy, and
    this repository is public.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = (base or REPO_ROOT / "rescue") / f"export-{stamp}"
    resolved = target.resolve()
    if resolved == BACKEND_DIR or BACKEND_DIR in resolved.parents:
        raise SystemExit(f"refusing to write under {BACKEND_DIR}: it is the deployment package")
    return resolved


def _group_key(doc: dict[str, Any]) -> str:
    prefix = str(doc.get("pk") or "nopk").split("|", 1)[0] or "nopk"
    kind = str(doc.get("type") or "untyped")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in f"{prefix}__{kind}")
    return safe


class Writer:
    """One open JSONL file per group, hashed as it is written."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[str, IO[str]] = {}
        self.hashes: dict[str, Any] = {}
        self.counts: Counter[str] = Counter()
        self.sizes: Counter[str] = Counter()

    def write(self, name: str, doc: dict[str, Any]) -> None:
        if name not in self.files:
            self.files[name] = (self.root / f"{name}.jsonl").open("w", encoding="utf-8")
            self.hashes[name] = hashlib.sha256()
        line = json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n"
        self.files[name].write(line)
        encoded = line.encode("utf-8")
        self.hashes[name].update(encoded)
        self.counts[name] += 1
        self.sizes[name] += len(encoded)

    def close(self) -> dict[str, dict[str, Any]]:
        for handle in self.files.values():
            handle.close()
        return {
            f"{name}.jsonl": {
                "documents": self.counts[name],
                "bytes": self.sizes[name],
                "sha256": self.hashes[name].hexdigest(),
            }
            for name in sorted(self.files)
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--endpoint", default=os.environ.get("COSMOS_ENDPOINT", ""))
    parser.add_argument("--database", default=os.environ.get("COSMOS_DATABASE") or "mynews")
    parser.add_argument("--container", default=os.environ.get("COSMOS_CONTAINER") or "items")
    parser.add_argument("--out", type=Path, default=None, help="Base directory (default <repo>/rescue)")
    args = parser.parse_args()

    if not args.endpoint:
        print("COSMOS_ENDPOINT is required (azd env get-value COSMOS_ENDPOINT)", file=sys.stderr)
        return 2

    out = _out_dir(args.out)
    out.mkdir(parents=True, exist_ok=False)

    client = CosmosClient(args.endpoint, credential=AzureCliCredential())
    container = client.get_database_client(args.database).get_container_client(args.container)

    writer = Writer(out)
    started = datetime.now(timezone.utc)
    total_ru = 0.0
    window_start, window_ru = time.monotonic(), 0.0
    pinned = 0

    pages = container.query_items(
        query="SELECT * FROM c",
        enable_cross_partition_query=True,
        max_item_count=PAGE_SIZE,
    ).by_page()

    for page in pages:
        docs = list(page)
        headers = container.client_connection.last_response_headers or {}
        charge = float(headers.get("x-ms-request-charge", 0) or 0)
        total_ru += charge
        window_ru += charge

        for doc in docs:
            writer.write(_group_key(doc), doc)
            if doc.get("type") == "article" and doc.get("ttl") == -1:
                writer.write("pinned_likes", doc)
                pinned += 1

        # Pace: once this second's spend passes the ceiling, wait out the rest
        # of the second. Coarse, but enough to keep harvest's writes unthrottled.
        elapsed = time.monotonic() - window_start
        if elapsed >= 1.0:
            window_start, window_ru = time.monotonic(), 0.0
        elif window_ru >= MAX_RU_PER_SECOND:
            time.sleep(1.0 - elapsed)
            window_start, window_ru = time.monotonic(), 0.0

    files = writer.close()
    total_docs = sum(v["documents"] for k, v in files.items() if k != "pinned_likes.jsonl")
    manifest = {
        "started_at": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "account": urlsplit(args.endpoint).hostname,
        "database": args.database,
        "container": args.container,
        "documents": total_docs,
        "pinned_likes": pinned,
        "request_charge_ru": round(total_ru, 2),
        "files": files,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"Exported {total_docs} documents ({pinned} pinned likes) for {total_ru:.0f} RU to {out}")
    for name, meta in files.items():
        print(f"  {name:<40} {meta['documents']:>7} docs  {meta['bytes'] / 1e6:8.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
