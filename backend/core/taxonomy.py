"""The fixed category vocabulary.

Categories were free text to begin with, and the scorer invented **213 distinct
tags** across ~1,400 articles — `ios`, `apple`, `macos` and `rumors` all
coexisting with no hierarchy. That is unusable for grouping: you cannot build a
filter over a vocabulary that grows with every run.

So the scorer now picks from this closed list, enforced by an `enum` in the
strict JSON schema, and the app can render exactly these buckets.

Keep the list short. Every category added is one more decision per article for
the model and one more chip in the UI, and buckets that rarely fire are worse
than useless — they make the filter look broken.
"""

from __future__ import annotations

# Order is display order in the app, roughly by how much Thomas reads each.
CATEGORIES: tuple[str, ...] = (
    "ai",
    "apple",
    "microsoft",
    "dev",
    "security",
    "hardware",
    "science",
    "world",
    "business",
    "fun",
)

CATEGORY_LABELS: dict[str, str] = {
    "ai": "AI",
    "apple": "Apple",
    "microsoft": "Microsoft",
    "dev": "Dev & Eng",
    "security": "Security",
    "hardware": "Hardware",
    "science": "Science",
    "world": "World",
    "business": "Business",
    "fun": "Fun",
}

# Written into the scoring prompt so the model knows what each bucket means.
# Without this it guesses, and an Azure security advisory lands in whichever of
# `microsoft` or `security` it happens to think of first.
CATEGORY_GUIDE = """\
- ai: model releases, AI research, AI engineering and tooling, AI policy
- apple: Apple products, platforms, developer news, and substantive rumors
- microsoft: Azure, Microsoft 365, Windows, and Microsoft as a company
- dev: software engineering craft, languages, databases, infrastructure, open source
- security: vulnerabilities, breaches, cryptography, privacy engineering
- hardware: chips, devices, robotics, and non-Apple consumer hardware
- science: research outside computing — physics, biology, climate, space
- world: politics, conflict, disasters, elections, and general news of consequence
- business: markets, deals, and the business of technology
- fun: satire, humour, and deliberately non-serious writing

Pick one or two. Prefer the most specific that fits: an Azure vulnerability is
security, not microsoft; an Apple silicon benchmark is apple, not hardware.
"""

SATIRE_CATEGORY = "fun"
