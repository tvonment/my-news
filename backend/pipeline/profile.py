"""Weekly rewrite of the taste profile.

The centroids are the fast, numeric half of the learning loop — a vote changes
the next run's ranking immediately. This is the slow, legible half: once a week
the recent votes are handed to the summarizer model, which rewrites the profile
document that the scorer's system prompt is built from.

Two properties make this worth the tokens:

* **It is readable.** The profile is plain markdown shown in the app, so the
  reason an article was ranked highly is inspectable rather than buried in 512
  floats.
* **It is editable.** A manual edit is respected and versioned like any other,
  which makes the system steerable when it drifts somewhere you don't want.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core import cosmos, foundry
from core.budget import BudgetExhausted, DailyBudget, Usage
from core.config import get_settings
from core.models import USER_PK, utcnow
from pipeline.score import DEFAULT_PROFILE, PROFILE_DOC_ID

log = logging.getLogger(__name__)

SAMPLE_PER_SIGNAL = 50

# Below this the sample is too thin to generalize from, and a rewrite would
# overfit to a handful of taps — turning one liked Apple story into "Thomas
# mainly wants Apple news". The centroids still learn from every vote in the
# meantime; only the prose rewrite waits.
MIN_VOTES_TO_REWRITE = 12

PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "profile": {
            "type": "string",
            "description": "The rewritten profile, markdown, 150-300 words",
        },
        "changelog": {
            "type": "string",
            "description": "One sentence on what changed versus the previous version, and why",
        },
    },
    "required": ["profile", "changelog"],
    "additionalProperties": False,
}

REWRITE_PROMPT = """\
You maintain a reader's taste profile for a personal news filter. You are given
the current profile and a sample of what they recently marked "more like this"
and "less like this". Rewrite the profile so it better predicts those votes.

Rules:

- Keep it 150-300 words of plain markdown prose. No headings, no bullet lists of
  keywords — the profile is read by another model as context, and by the reader
  as an explanation of what the filter believes about them.
- Describe interests and dislikes in terms of SUBJECT and TREATMENT, not
  outlets. "Wants engineering write-ups with concrete detail" generalizes;
  "likes 9to5Mac" does not, and would just re-rank the same sources forever.
- Preserve stated interests that simply had no articles in this sample. Absence
  of evidence is not a dislike — dropping a topic because it did not appear
  narrows the feed permanently and it can never recover, since nothing on that
  topic will be surfaced to vote on again.
- Only add or remove an interest when several votes point the same way. One vote
  is noise.
- Keep the reader's own explicit interests unless the votes clearly contradict
  them.

Also return a one-sentence changelog describing what you changed and why.
"""


@dataclass
class ProfileResult:
    rewritten: bool = False
    reason: str = ""
    version: int = 0
    changelog: str = ""
    usage: Usage = None  # type: ignore[assignment]
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = Usage()


def _recent_votes(signal: str, limit: int) -> list[dict[str, Any]]:
    return cosmos.query(
        "SELECT TOP @limit c.title, c.categories, c.why, c.created_at FROM c "
        "WHERE c.type = 'feedback' AND c.signal = @signal ORDER BY c.created_at DESC",
        [{"name": "@limit", "value": limit}, {"name": "@signal", "value": signal}],
        partition_key=USER_PK,
    )


def _format_votes(label: str, votes: list[dict[str, Any]]) -> str:
    if not votes:
        return f"{label}: (none in this period)"
    lines = [f"{label}:"]
    for v in votes:
        cats = ", ".join(v.get("categories") or []) or "uncategorized"
        why = (v.get("why") or "").strip()
        lines.append(f"- {v.get('title', '')} [{cats}]" + (f" — scored: {why}" if why else ""))
    return "\n".join(lines)


def load_profile_doc() -> dict[str, Any]:
    return cosmos.read(PROFILE_DOC_ID, USER_PK) or {
        "id": PROFILE_DOC_ID,
        "pk": USER_PK,
        "type": "config",
        "text": DEFAULT_PROFILE,
        "version": 0,
        "changelog": "Initial profile.",
        "source": "default",
        "history": [],
    }


def save_profile(text: str, changelog: str, source: str) -> int:
    """Persist a new profile version, keeping a short audit trail."""
    doc = load_profile_doc()
    history = list(doc.get("history") or [])
    if doc.get("version"):
        history.append(
            {
                "version": doc["version"],
                "changelog": doc.get("changelog", ""),
                "updated_at": doc.get("updated_at"),
            }
        )

    doc["text"] = text.strip()
    doc["version"] = int(doc.get("version", 0)) + 1
    doc["changelog"] = changelog.strip()
    doc["source"] = source
    doc["updated_at"] = utcnow().isoformat()
    # Keep the last 20 changelog entries so drift is reviewable without the
    # document growing without bound.
    doc["history"] = history[-20:]
    cosmos.upsert(doc)
    return doc["version"]


def rewrite_profile(budget: DailyBudget | None = None) -> ProfileResult:
    settings = get_settings()
    budget = budget or DailyBudget()

    liked = _recent_votes("like", SAMPLE_PER_SIGNAL)
    disliked = _recent_votes("dislike", SAMPLE_PER_SIGNAL)
    total = len(liked) + len(disliked)

    if total < MIN_VOTES_TO_REWRITE:
        reason = (
            f"only {total} votes so far; need {MIN_VOTES_TO_REWRITE} before rewriting "
            "(the centroids are already learning from them)"
        )
        log.info("Skipping profile rewrite: %s", reason)
        return ProfileResult(rewritten=False, reason=reason)

    try:
        budget.check()
    except BudgetExhausted as exc:
        return ProfileResult(rewritten=False, reason=str(exc))

    current = load_profile_doc()
    user_content = (
        f"Current profile:\n{current.get('text', '')}\n\n"
        f"{_format_votes('Marked MORE like this', liked)}\n\n"
        f"{_format_votes('Marked LESS like this', disliked)}"
    )

    parsed, usage = foundry.structured_call(
        deployment=settings.model_summarizer,
        system_prompt=REWRITE_PROMPT,
        user_content=user_content,
        schema_name="taste_profile",
        schema=PROFILE_SCHEMA,
        max_output_tokens=1200,
        reasoning_effort=settings.reasoning_effort_summarizer,
    )

    cost = budget.charge(settings.model_summarizer_id, usage)
    budget.flush()

    text = str(parsed.get("profile", "")).strip()
    if len(text) < 200:
        # A suspiciously short rewrite would quietly gut the scorer's context.
        # Keep the existing profile and say so.
        reason = f"model returned only {len(text)} characters; keeping the previous profile"
        log.warning(reason)
        return ProfileResult(rewritten=False, reason=reason, usage=usage, cost_usd=cost)

    changelog = str(parsed.get("changelog", "")).strip() or "Rewritten from recent feedback."
    version = save_profile(text, changelog, source="auto")

    log.info("Profile rewritten to v%d from %d votes ($%.4f): %s", version, total, cost, changelog)
    return ProfileResult(
        rewritten=True,
        reason=f"rewritten from {total} votes",
        version=version,
        changelog=changelog,
        usage=usage,
        cost_usd=cost,
    )
