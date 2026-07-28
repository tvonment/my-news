"""Source list loading.

``sources.yaml`` is the seed. On first run it is written into Cosmos as the
``sources`` config document, and from then on Cosmos is authoritative — that is
what lets the iOS settings screen (M5) add and remove feeds without a deploy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import yaml

from core import cosmos
from core.models import USER_PK, utcnow

log = logging.getLogger(__name__)

SOURCES_DOC_ID = "sources"
SEED_PATH = Path(__file__).resolve().parent.parent / "sources.yaml"


@dataclass
class Source:
    id: str
    name: str
    url: str
    category: str = "general"
    lang: str = "en"
    poll_minutes: int = 60
    stale_after_hours: int = 72
    full_text: bool = False
    enabled: bool = True


def _from_seed() -> list[Source]:
    raw: dict[str, Any] = yaml.safe_load(SEED_PATH.read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {}) or {}
    sources: list[Source] = []
    for entry in raw.get("sources", []) or []:
        merged = {**defaults, **entry}
        sources.append(
            Source(
                id=merged["id"],
                name=merged["name"],
                url=merged["url"],
                category=merged.get("category", "general"),
                lang=merged.get("lang", "en"),
                poll_minutes=int(merged.get("poll_minutes", 60)),
                stale_after_hours=int(merged.get("stale_after_hours", 72)),
                full_text=bool(merged.get("full_text", False)),
                enabled=bool(merged.get("enabled", True)),
            )
        )
    return sources


def load_sources(seed_if_missing: bool = True) -> list[Source]:
    doc = cosmos.read(SOURCES_DOC_ID, USER_PK)
    if doc is not None:
        return [Source(**entry) for entry in doc.get("sources", [])]

    if not seed_if_missing:
        return []

    sources = _from_seed()
    log.info("No sources document found; seeding %d sources from sources.yaml", len(sources))
    save_sources(sources)
    return sources


def save_sources(sources: list[Source]) -> None:
    cosmos.upsert(
        {
            "id": SOURCES_DOC_ID,
            "pk": USER_PK,
            "type": "config",
            "updated_at": utcnow().isoformat(),
            "sources": [asdict(s) for s in sources],
        }
    )
