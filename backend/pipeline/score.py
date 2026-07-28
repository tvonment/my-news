"""LLM relevance scoring, batched.

Batching is what makes this affordable: the system prompt (taste profile,
categories, rubric) is the dominant input cost in a headline-classification
workload, and putting ~20 articles in one call amortizes it 20x. Unbatched, the
input leg roughly quadruples for identical work.

Articles are referenced by their ordinal position in the batch rather than by
their 43-character content hash. That is worth roughly 40 tokens per article of
pure overhead avoided, and the mapping back is a list index.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core import cosmos, foundry
from core.budget import BudgetExhausted, DailyBudget, Usage
from core.config import get_settings
from core.models import USER_PK
from core.taxonomy import CATEGORIES, CATEGORY_GUIDE, SATIRE_CATEGORY

log = logging.getLogger(__name__)

BATCH_SIZE = 20
PROFILE_DOC_ID = "profile"

SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "The article's number from the input list"},
                    "relevance": {"type": "integer", "description": "0-100, how much Thomas wants to read this"},
                    "categories": {
                        "type": "array",
                        # A closed vocabulary, enforced by the schema rather than
                        # by the prompt. Free text produced 213 distinct tags and
                        # made grouping impossible.
                        "items": {"type": "string", "enum": list(CATEGORIES)},
                    },
                    "why": {"type": "string", "description": "At most 15 words, in English"},
                },
                # Strict mode requires every property listed in `required` and
                # additionalProperties: false at every level.
                "required": ["n", "relevance", "categories", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

DEFAULT_PROFILE = """\
Thomas is a software engineer working with Azure, .NET and AI systems. He reads
to stay current on cloud infrastructure, AI model releases and developer tooling,
and he follows Apple closely both as a developer and out of interest in the
hardware. He also wants a light but real awareness of world news.

He values: concrete technical detail, primary sources, release notes and
engineering write-ups, and stories that change how something should be built.

He does not want: funding-round and valuation news, executive-hire announcements,
opinion pieces without new facts, listicles, thin aggregator rewrites of a story
he can read at the source, and celebrity or lifestyle coverage.
"""

RUBRIC = f"""\
You rank news articles for one person. For each numbered article, return a
relevance score from 0 to 100, one or two categories from the fixed list below,
and a `why` of at most 15 words in English explaining the score.

Categories — use only these:
{CATEGORY_GUIDE}
Articles marked [SATIRE] are deliberately fake. Always categorize them as `fun`
and never treat their claims as real events, however plausible they sound.

Scoring guide:
  85-100  He will almost certainly want to read this today.
  65-84   Clearly relevant to a stated interest.
  40-64   Adjacent or mildly interesting; would read if quiet.
  15-39   Off-topic but not actively unwanted.
  0-14    Explicitly unwanted per the profile.

Judge the article, not the outlet. A thin rewrite of a story available at its
source scores low even on a topic he likes. Return exactly one entry per input
article, using the same number.
"""


@dataclass
class ScoreResult:
    scored: int = 0
    usage: Usage = None  # type: ignore[assignment]
    cost_usd: float = 0.0
    budget_exhausted: bool = False

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = Usage()


def load_profile() -> str:
    doc = cosmos.read(PROFILE_DOC_ID, USER_PK)
    if doc and doc.get("text"):
        return str(doc["text"])
    return DEFAULT_PROFILE


def build_system_prompt(profile: str) -> str:
    """Assemble the cacheable prefix.

    Everything here must be byte-stable across calls within a run: prompt caching
    keys on the first 1,024 tokens and a single changed character is a full miss.
    All volatile content (the articles) goes in the user message instead.
    """
    return f"{RUBRIC}\n\n--- Reader profile ---\n{profile}\n"


def format_batch(articles: list[dict[str, Any]]) -> str:
    lines = []
    for index, article in enumerate(articles, start=1):
        title = article.get("title", "").strip()
        lead = (article.get("lead") or "").strip()[:400]
        source = article.get("source_name", "")
        # Satire is marked in the input rather than left for the model to infer.
        # Postillon headlines are written to be plausible, and a model that
        # mistakes one for real news produces a confidently wrong summary.
        marker = " [SATIRE]" if article.get("satire") else ""
        lines.append(f"{index}. [{source}]{marker} {title}\n   {lead}")
    return "\n\n".join(lines)


def clean_categories(raw: Any, satire: bool) -> list[str]:
    """Keep only known categories; satire is always `fun` regardless of the model."""
    if satire:
        return [SATIRE_CATEGORY]
    seen: list[str] = []
    for value in raw or []:
        name = str(value).strip().lower()
        if name in CATEGORIES and name not in seen:
            seen.append(name)
    return seen[:2]


def score_articles(articles: list[dict[str, Any]], budget: DailyBudget | None = None) -> ScoreResult:
    settings = get_settings()
    budget = budget or DailyBudget()
    result = ScoreResult()

    if not articles:
        return result

    system_prompt = build_system_prompt(load_profile())

    for batch in foundry.batched(articles, BATCH_SIZE):
        try:
            budget.check()
        except BudgetExhausted as exc:
            log.warning("%s", exc)
            result.budget_exhausted = True
            break

        parsed, usage = foundry.structured_call(
            deployment=settings.model_scorer,
            system_prompt=system_prompt,
            user_content=format_batch(batch),
            schema_name="article_scores",
            schema=SCORE_SCHEMA,
            max_output_tokens=BATCH_SIZE * 120,
            reasoning_effort=settings.reasoning_effort_scorer,
        )

        result.usage = result.usage + usage
        result.cost_usd += budget.charge(settings.model_scorer_id, usage)

        by_number = {int(item["n"]): item for item in parsed.get("items", []) if "n" in item}
        for index, article in enumerate(batch, start=1):
            verdict = by_number.get(index)
            if verdict is None:
                # A missing entry means the model dropped an article. Leave it
                # gated so the next run retries it rather than silently losing it.
                log.warning("Scorer omitted article %d (%s)", index, article.get("id"))
                continue
            article["relevance"] = max(0, min(100, int(verdict.get("relevance", 0))))
            article["categories"] = clean_categories(
                verdict.get("categories"), bool(article.get("satire"))
            )
            article["why"] = str(verdict.get("why", ""))[:200]
            article["state"] = "scored"
            cosmos.upsert(article)
            result.scored += 1

    budget.flush()
    log.info(
        "Scored %d articles for $%.4f (%d in / %d cached / %d out)",
        result.scored,
        result.cost_usd,
        result.usage.input_tokens,
        result.usage.cached_input_tokens,
        result.usage.output_tokens,
    )
    return result
