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
Summarize one news article for a reader who wants the substance fast.

Return exactly three bullets in ENGLISH, regardless of the article's language.
Each bullet is one sentence and states a fact from the article: what happened,
what it means, and what is concretely new or changed. Do not editorialize, do
not repeat the headline, and do not pad. If the text is too thin to support
three distinct bullets, say so plainly in the bullets you can support.

Also return the key named entities (people, companies, products) and an integer
estimate of reading time in minutes for the original article.
"""


@dataclass
class SummarizeResult:
    summarized: int = 0
    paywalled: int = 0
    failed: int = 0
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


def summarize_articles(
    articles: list[dict[str, Any]],
    budget: DailyBudget | None = None,
) -> SummarizeResult:
    settings = get_settings()
    budget = budget or DailyBudget()
    result = SummarizeResult()

    candidates = [a for a in articles if (a.get("relevance") or 0) >= settings.summarize_threshold]
    if not candidates:
        return result

    with httpx.Client(follow_redirects=True, timeout=DEFAULT_TIMEOUT, http2=True) as client:
        for article in candidates:
            try:
                budget.check()
            except BudgetExhausted as exc:
                log.warning("%s", exc)
                result.budget_exhausted = True
                break

            title = article.get("title", "")
            body = extract_article(article.get("url", ""), client) or ""

            if looks_paywalled(title, body):
                # Keep the teaser, flag it, move on. The app still links out.
                article["paywalled"] = True
                article["state"] = "summarized"
                body = article.get("lead", "")
                if len(body) < 120:
                    cosmos.upsert(article)
                    result.paywalled += 1
                    continue
                result.paywalled += 1

            try:
                parsed, usage = foundry.structured_call(
                    deployment=settings.model_summarizer,
                    system_prompt=SUMMARY_PROMPT,
                    user_content=f"Title: {title}\nSource: {article.get('source_name', '')}\n\n{body}",
                    schema_name="article_summary",
                    schema=SUMMARY_SCHEMA,
                    max_output_tokens=800,
                    reasoning_effort=settings.reasoning_effort_summarizer,
                )
            except Exception:
                log.exception("Summarization failed for %s", article.get("id"))
                result.failed += 1
                continue

            result.usage = result.usage + usage
            result.cost_usd += budget.charge(settings.model_summarizer_id, usage)

            article["summary"] = [str(b) for b in parsed.get("summary", [])][:3]
            article["entities"] = [str(e) for e in parsed.get("entities", [])][:12]
            article["reading_minutes"] = int(parsed.get("reading_minutes", 0) or 0)
            article["state"] = "summarized"
            cosmos.upsert(article)
            result.summarized += 1

    budget.flush()
    log.info(
        "Summarized %d (%d paywalled, %d failed) for $%.4f",
        result.summarized,
        result.paywalled,
        result.failed,
        result.cost_usd,
    )
    return result
