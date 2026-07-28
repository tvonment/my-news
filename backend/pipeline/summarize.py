"""Full-text extraction and summarization.

Runs only on articles above the relevance threshold — roughly 10% of what the
scorer sees, which is what keeps the expensive model affordable.

Two hard rules:

* **Cap the extracted text.** gpt-5.x reprices the *entire request* above 272,000
  input tokens (2x input, 1.5x output). Headlines never approach that; an
  unbounded article page could, and the cliff is a step function, so extraction
  is truncated well below it.
* **Detect paywalls, never bypass them.** Bypass techniques breach site terms in
  every case and defeat the honest-User-Agent posture that keeps this crawler
  robots-compliant in the first place. A paywalled item keeps its feed teaser,
  gets flagged, and still links out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
import trafilatura

from core import cosmos, foundry
from core.budget import BudgetExhausted, DailyBudget, Usage
from core.config import get_settings
from fetch.http import DEFAULT_TIMEOUT, user_agent_for
from pipeline.cluster import Cluster

log = logging.getLogger(__name__)

# ~8k tokens at roughly 4 chars/token for English, fewer for German. Far below
# the 272k long-context cliff, and well past the length where a summary improves.
MAX_EXTRACT_CHARS = 32_000
# Below this, extraction almost certainly hit a paywall or consent wall rather
# than an article.
MIN_BODY_CHARS = 800

PAYWALL_MARKERS = (
    "heise+ |",
    "spiegel+",
    "zeit+",
    "faz+",
    "nur mit abo",
    "abonnement",
    "subscribe to continue",
    "this article is for subscribers",
)

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exactly three bullets, in English, each one sentence",
        },
        "entities": {"type": "array", "items": {"type": "string"}},
        "reading_minutes": {"type": "integer"},
    },
    "required": ["summary", "entities", "reading_minutes"],
    "additionalProperties": False,
}

SUMMARY_PROMPT = """\
Summarize one news story for a reader who wants the substance fast.

Return exactly three bullets in ENGLISH, regardless of the source language.
Each bullet is one sentence and states a fact from the text: what happened,
what it means, and what is concretely new or changed. Do not editorialize, do
not repeat the headline, and do not pad. If the text is too thin to support
three distinct bullets, say so plainly in the bullets you can support.

You may be given the SAME story as covered by several outlets. When you are:
write one summary of the story, not one per outlet. Prefer facts that more than
one outlet reports. Where they genuinely disagree on a fact, say so in the
bullet rather than silently picking one — a contradiction between outlets is
itself information the reader wants.

If the input is marked SATIRE, it is deliberately fake. Summarize it as a joke
and make that unmistakable in the first bullet. Never describe a satirical
claim as something that happened.

Also return the key named entities (people, companies, products) and an integer
estimate of reading time in minutes for the original article.
"""

# Beyond three versions the marginal fact rate collapses while the token cost
# keeps rising — outlets four and five are almost always rewriting the same wire
# copy the first three already carried.
MAX_SOURCES_PER_SUMMARY = 3


@dataclass
class SummarizeResult:
    summarized: int = 0
    paywalled: int = 0
    failed: int = 0
    # Extra outlets folded into a summary rather than shown as their own card.
    clustered_sources: int = 0
    usage: Usage = None  # type: ignore[assignment]
    cost_usd: float = 0.0
    budget_exhausted: bool = False

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = Usage()


def looks_paywalled(title: str, body: str) -> bool:
    if len(body) < MIN_BODY_CHARS:
        return True
    haystack = f"{title}\n{body[:2000]}".lower()
    return any(marker in haystack for marker in PAYWALL_MARKERS)


def extract_article(url: str, client: httpx.Client) -> str | None:
    """Fetch a page and pull out the main text, or None if that fails."""
    try:
        response = client.get(url, headers={"User-Agent": user_agent_for(url)})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Could not fetch %s: %s", url, exc)
        return None

    text = trafilatura.extract(
        response.text,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    return text[:MAX_EXTRACT_CHARS] if text else None


def _build_input(cluster: Cluster, client: httpx.Client) -> tuple[str, bool]:
    """Assemble the text for one story, drawing on up to three outlets.

    Returns the prompt body and whether every version we could reach was
    paywalled — in which case there is nothing to synthesize from beyond teasers.
    """
    parts: list[str] = []
    all_paywalled = True

    for article in cluster.all_articles()[:MAX_SOURCES_PER_SUMMARY]:
        title = article.get("title", "")
        source = article.get("source_name", "")
        body = extract_article(article.get("url", ""), client) or ""

        if looks_paywalled(title, body):
            article["paywalled"] = True
            body = article.get("lead", "") or ""
        else:
            all_paywalled = False

        if body:
            parts.append(f"--- {source}: {title} ---\n{body}")

    marker = "SATIRE — this is deliberately fake\n\n" if cluster.primary.get("satire") else ""
    return marker + "\n\n".join(parts), all_paywalled


def summarize_clusters(
    clusters: list[Cluster],
    budget: DailyBudget | None = None,
) -> SummarizeResult:
    """Summarize one story per cluster rather than one per article.

    This is why clustering pays for itself: five outlets covering an
    announcement previously cost five summarizer calls and produced five nearly
    identical cards. Now it is one call and one card, so the expensive model
    runs less often even though it reads more text.
    """
    settings = get_settings()
    budget = budget or DailyBudget()
    result = SummarizeResult()

    candidates = [
        c
        for c in clusters
        if (c.primary.get("relevance") or 0) >= settings.summarize_threshold
    ]
    if not candidates:
        return result

    with httpx.Client(follow_redirects=True, timeout=DEFAULT_TIMEOUT, http2=True) as client:
        for cluster in candidates:
            try:
                budget.check()
            except BudgetExhausted as exc:
                log.warning("%s", exc)
                result.budget_exhausted = True
                break

            primary = cluster.primary
            body, all_paywalled = _build_input(cluster, client)

            if all_paywalled:
                result.paywalled += 1
            if len(body) < 120:
                # Nothing worth spending a summarizer call on.
                primary["state"] = "summarized"
                _persist(cluster)
                continue

            try:
                parsed, usage = foundry.structured_call(
                    deployment=settings.model_summarizer,
                    system_prompt=SUMMARY_PROMPT,
                    user_content=body,
                    schema_name="article_summary",
                    schema=SUMMARY_SCHEMA,
                    max_output_tokens=800,
                    reasoning_effort=settings.reasoning_effort_summarizer,
                )
            except Exception:
                log.exception("Summarization failed for cluster %s", primary.get("id"))
                result.failed += 1
                continue

            result.usage = result.usage + usage
            result.cost_usd += budget.charge(settings.model_summarizer_id, usage)

            primary["summary"] = [str(b) for b in parsed.get("summary", [])][:3]
            primary["entities"] = [str(e) for e in parsed.get("entities", [])][:12]
            primary["reading_minutes"] = int(parsed.get("reading_minutes", 0) or 0)
            primary["state"] = "summarized"
            _persist(cluster)
            result.summarized += 1
            result.clustered_sources += len(cluster.other_sources)

    budget.flush()
    log.info(
        "Summarized %d stories from %d extra sources (%d paywalled, %d failed) for $%.4f",
        result.summarized,
        result.clustered_sources,
        result.paywalled,
        result.failed,
        result.cost_usd,
    )
    return result


def _persist(cluster: Cluster) -> None:
    """Save the primary plus every collapsed member.

    Members are marked summarized so a later run does not pay to summarize them
    individually, but they keep their own record and stay searchable.
    """
    cosmos.upsert(cluster.primary)
    for member in cluster.members:
        member["state"] = "summarized"
        cosmos.upsert(member)
