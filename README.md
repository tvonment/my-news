# MyNews

A private, single-user iOS news app. A crawler pulls ~35 feeds, an LLM filters them against my
interests and writes English summaries, and the app shows a ranked feed that links to the original
article. Like/dislike teaches it what to surface.

Hobby project. The binding constraint is **low running cost**: ~$8/month of Azure at the expected
volume, against a $99/year Apple Developer Program membership that costs more than all of it.

## Status

| Milestone | State |
|---|---|
| M0 — infra, health endpoint | **live** in Switzerland North |
| M1 — harvest: fetch, dedupe, persist | **live** — 34 feeds, 1,348 articles on first run |
| M2 — embeddings gate, scoring, summaries | **live** — first digest cost $0.09 |
| M3 — iOS app v1 | **runs in the simulator** against the live backend |
| M4 — feedback loop | **working** — votes update the centroids; weekly profile rewrite still to do |
| M5 — in-app settings | not started |
| M6 — digest + APNs push | not started |
| M7 — MCP server, "dig deeper" agent | not started |

Full design and the research behind it: `~/.claude/plans/i-have-an-idea-noble-galaxy.md`.

## Layout

```
ios/            SwiftUI app (iOS 26). Feed, detail, settings; SwiftData offline cache.
infra/          Bicep. Cosmos (free tier), Flex Consumption, Foundry, budget alerts.
backend/        Python Azure Functions (v2 model).
  core/         config, cosmos, foundry, budget, models, sources
  fetch/        HTTP with conditional GET; parsing and canonicalization
  pipeline/     harvest (M1); gate/score/summarize/digest to come
  scripts/      probe_feeds.py, verify_vector_search.py
  tests/        pytest, no network
```

## Getting started

```sh
# 1. Dependencies
cd backend && uv venv --python 3.12 .venv && uv pip install -r requirements.txt pytest

# 2. Tests — no network, no Azure needed
.venv/bin/python -m pytest tests/ -q

# 3. See what every configured feed actually does — no Azure needed
.venv/bin/python scripts/probe_feeds.py
.venv/bin/python scripts/probe_feeds.py heise verge      # or just a few

# 4. Deploy (creates real Azure resources — read the warnings below first)
cd .. && azd auth login && azd up

# 5. Verify the one unproven assumption in the plan
cd backend && .venv/bin/python scripts/verify_vector_search.py
```

## Running the iOS app

```sh
cd ios
xcodebuild -project MyNews.xcodeproj -target MyNews \
  -sdk iphonesimulator -configuration Debug build CODE_SIGNING_ALLOWED=NO

xcrun simctl boot "iPhone 17 Pro"
xcrun simctl install booted build/Debug-iphonesimulator/MyNews.app

# Debug builds accept the backend config from the launch environment, so you
# don't have to type a function key on a simulated keyboard.
export SIMCTL_CHILD_MYNEWS_BASE_URL="https://<your-func>.azurewebsites.net"
export SIMCTL_CHILD_MYNEWS_FUNCTION_KEY="$(az functionapp keys list -g rg-mynews-mynews \
  -n <your-func> --query functionKeys.default -o tsv)"
xcrun simctl launch --terminate-running-process booted com.vonmentlen.mynews
```

On a real device, enter the URL and key in Settings instead — the environment
seeding is `#if DEBUG` only and the key goes to the Keychain,
`AfterFirstUnlockThisDeviceOnly`, so it never lands in a backup.

**Two toolchain notes, both discovered the hard way:**

- **There is no asset catalog.** Xcode 26.4.1 ships the iOS 26.4 SDK but only
  the 26.2 simulator *runtime* is installed here, and `actool` refuses to run
  without a runtime matching the SDK — it fails the whole build with
  *"No simulator runtime version … available to use with iphonesimulator SDK
  version"*. Removing the (placeholder) catalog unblocks it at the cost of the
  default app icon. Fix properly with `xcodebuild -downloadPlatform iOS`, then
  re-add `Assets.xcassets` and the two `ASSETCATALOG_COMPILER_*` build settings.
- **Building to a physical iPhone needs that same download** — the iOS 26.4
  *device* platform isn't installed either.

## Things that will cost you money if changed

These are load-bearing. Each one is the difference between ~$8/month and a lot more.

| Setting | Where | Why |
|---|---|---|
| `enableFreeTier: true` | `infra/modules/data.bicep` | **Settable only at account creation**, one per subscription. Gives 1,000 RU/s + 25 GB free, and makes TTL deletes free. Incompatible with serverless — which is why this is a provisioned account. |
| Total Cosmos RU/s ≤ 1000 | `infra/modules/data.bicep` | The free grant is a hard budget. A second 400 RU/s container costs $23.36/month. |
| No `alwaysReady` | `infra/modules/functions.bicep` | Always-ready instances have **no free grant**: $5.26/month at 512 MB. |
| `instanceMemoryMB: 512` | `infra/modules/functions.bicep` | Flex bills provisioned memory for the whole execution *including* time blocked on LLM calls. This pipeline is ~95% network wait, so 2048 MB would cost 4x for identical wall-clock. |
| `sku.name: 'GlobalStandard'` | `infra/modules/ai.bicep` | `ProvisionedManaged` is ~$1,460/month per PTU with a ~15 PTU minimum. |
| `reasoning_effort: "none"` | `core/foundry.py` | gpt-5.6 is a reasoning model and reasoning tokens bill at the **output** rate ($6/1M on luna). Leaving the default on a 55-token answer multiplies the output bill several-fold. |

Azure OpenAI has **no hard spend limit**. The brakes are the budget alert in Bicep and
`DAILY_TOKEN_BUDGET_USD` in `core/budget.py`.

## Models

Deployed: **`gpt-5-nano`** for bulk scoring ($0.05/$0.40 per 1M), **`gpt-5.4`** for summaries
($2.50/$15.00), `text-embedding-3-small` at 512 dims for the gate.

The plan called for `gpt-5.6-luna`/`terra`, but this subscription has a quota limit of **0** for
every gpt-5.6 variant in all ten regions checked — a subscription-level gap that needs a manual
quota request, not something a region change fixes. `gpt-5.4` is priced identically to the planned
`gpt-5.6-terra`, so only the scorer changed, and it got 20x cheaper. Switching back is four params
in `infra/modules/ai.bicep`; the gpt-5.6 prices are already in `core/budget.py`.

Note there is **no bare `gpt-5.6`** on Azure — luna/terra/sol are real model names, not size
codenames. And reasoning effort is **not portable between models**: `minimal` works only on the
original GPT-5 family, `none` only on gpt-5.1 and later. The wrong value is rejected outright,
which is why it lives in config beside the model id.

Everything retires within ~12 months; Foundry runs OpenAI models on a 12-month GA lifecycle, so
budget one model swap per ~9 months. Deployment names are config (`MODEL_SCORER`,
`MODEL_SUMMARIZER`) and the JSON schemas are the stable contract across a swap.

See `docs/cost.md` for measured costs from the first live run.

## Feed fetching

Six rules in `fetch/http.py`, each one because a real feed broke a naive fetcher. Run
`scripts/probe_feeds.py` to see them fire live — the interesting column is `revalidate`:

- `304` — the server honours conditional GET.
- `200-but-same-body` — it ignores validators; the body hash saved us the parse.
- `200-but-same-body (etag ignored)` — it ignores `If-None-Match` specifically, so we stop sending it.

`ZOMBIE` in the status column means the feed returns 200 but has stopped publishing.
`news.microsoft.com/feed/` is the canonical example: valid XML, fresh `Last-Modified`, nothing new
since May 2025.

## Conventions

- Never send a User-Agent resembling `ClaudeBot`, `GPTBot`, `anthropic-ai`, `CCBot` or
  `PerplexityBot`. derStandard, Spiegel, ORF, Zeit and FAZ `Disallow` those **by name** while
  leaving `User-agent: *` open. An honestly-named reader is welcome; an AI-sounding one is blocked.
- Paywalls: detect and skip, never bypass.
- Always link to the original article.
- Keep this app off the App Store. Personal use is what makes the EU press-publishers' right and
  the private-copying exception apply; publishing voids both.
