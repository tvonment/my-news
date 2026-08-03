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
    # Deliberately fake news. Labelled everywhere downstream.
    satire: bool = False
    # Fetch the article page to backfill a usable lead. Set for feeds whose
    # teaser is too thin to embed meaningfully — Hugging Face sends no
    # description at all, OpenAI sends ~150 characters — which otherwise lose
    # the gate to verbose rumor blogs regardless of quality.
    enrich: bool = False


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
                satire=bool(merged.get("satire", False)),
                enrich=bool(merged.get("enrich", False)),
            )
        )
    return sources


def load_sources(seed_if_missing: bool = True) -> list[Source]:
    """Load the live source list, adopting any newly-added seed entries.

    Cosmos is authoritative once seeded — that is what lets the settings screen
    edit sources without a deploy. But a source added to `sources.yaml` after
    the first run would otherwise never appear, which silently turns "I added 30
    feeds" into a no-op harvest.

    So the merge is additive by id: unknown seed entries are adopted, and
    anything already in Cosmos is left exactly as it is, including a source you
    disabled or retuned by hand.
    """
    doc = cosmos.read(SOURCES_DOC_ID, USER_PK)
    if doc is None:
        if not seed_if_missing:
            return []
        sources = _from_seed()
        log.info("No sources document found; seeding %d sources from sources.yaml", len(sources))
        save_sources(sources)
        return sources

    # Filter to fields `Source` still declares. Unfiltered `Source(**entry)`
    # means the day a field is renamed or dropped, every stored entry raises
    # TypeError and load_sources() takes down harvest, feed and refresh at once.
    # `pipeline/harvest.py` reads SourceState the same defensive way.
    fields = set(Source.__dataclass_fields__)
    known = {
        entry["id"]: Source(**{k: v for k, v in entry.items() if k in fields})
        for entry in doc.get("sources", [])
    }
    seed = _from_seed()
    added = [s for s in seed if s.id not in known]

    # `enrich` and `satire` describe the feed itself — whether its teaser is too
    # thin to rank on, whether its content is fake — not a preference. They are
    # resynced from the seed so a correction propagates, while `enabled`,
    # `poll_minutes` and the rest stay under your control.
    retuned = []
    for s in seed:
        current = known.get(s.id)
        if current and (current.enrich != s.enrich or current.satire != s.satire):
            current.enrich, current.satire = s.enrich, s.satire
            retuned.append(s.id)

    if added or retuned:
        if added:
            log.info("Adopting %d new seed sources: %s", len(added), ", ".join(s.id for s in added))
        if retuned:
            log.info("Resynced feed flags for: %s", ", ".join(retuned))
        merged = list(known.values()) + added
        save_sources(merged)
        return merged

    return list(known.values())


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
