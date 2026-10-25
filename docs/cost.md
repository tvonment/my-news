# Measured costs

Two measurements, and they disagree for a reason. The **bill** (Cost Management, in CHF, the
billing currency) is the one that counts. The **token counts** every v1 digest run recorded in its
`run` document are exact for model calls and blind to everything else: they priced tokens at USD
list prices and never saw a log line, an execution, an alert rule or a storage transaction. That is
how a project costed on tokens at ~$8/month came in at 11.08 CHF, with models only 4.56 of it.

The `run` documents are still in Cosmos (partition `run`, 90-day TTL), but `GET /api/runs` went with
the rest of the v1 API on 2026-10-21. `backend/scripts/export_all.py`, read-only, copies them with
every other partition into `rescue/export-<UTC>/`, which is gitignored.

## Measured baseline — September 2026 (CHF)

v1's last full month, from Cost Management for the resource group:

| Line | CHF |
|---|---:|
| Log Analytics | 5.09 |
| Foundry models | 4.56 |
| Functions (Flex Consumption) | 0.63 |
| Alerts | 0.46 |
| Storage | 0.34 |
| **Total** | **11.08** |

Cosmos DB billed nothing: the free tier is the one grant that held. October was forecast at
**12.10** before Phase 0 landed on the 21st.

**The free grants assumed below never applied.** The July fixed-cost table further down, and the
~$8/month the README used to promise, counted on the Flex Consumption free grant and on Log
Analytics' 5 GB a month. This is a Visual Studio subscription spending a monthly credit, and a
credit subscription gets neither: every execution and every ingested gigabyte bills from the first.
The usage records leave no doubt. The whole subscription ingested 1.9 GB of logs in September, well
inside the 5 GB the table counted on, and every day of it billed at the same 2.66 CHF/GB. Alerts
billed 0.46 too, although `alerts.bicep` had counted both rules as free; its header now says why
neither is.

**Log Analytics was the largest line, and almost all of it was the Cosmos SDK logging its own
traffic.** The Cosmos client does not log through azure-core's HTTP logger, which `function_app.py`
silenced in August. It dumps every request and response (URL, method, every header, status) under
its own `azure.cosmos._cosmos_http_logging_policy` logger, which sits beside the silenced one rather
than under it, so every Cosmos call kept dumping at INFO: 941k traces in 30 days, while adaptive
sampling discarded the run summaries that mattered.

**What Phase 0 changes:**

- **The logger fix.** `function_app.py` sets `azure`, `azure.identity`, `httpx` and `httpcore` to
  WARNING and `trafilatura` to ERROR. The two HTTP policies are pinned by name as well, so lowering
  `azure` to debug something else cannot reopen them, and `test_logging_config.py` holds the levels.
  Phase 0's check is a trace-volume drop of more than 90 % within 24 hours. Behind it, the
  workspace's daily cap falls from 1 GB to 0.2 GB.
- **Model deployments removed.** The three v1 deployments are out of `ai.bicep` and deleted by hand
  (README, "Phase 0 manual cleanup"). The Foundry account stays, since it has no fixed fee, but
  holds nothing to call, so the 4.56 models line goes to zero, and with it the ~$280/day a runaway
  loop could have spent at those deployments' capacity.
- **Harvest only.** The digest, the profile rewrite and the HTTP API are gone. What runs is
  `harvest_timer` every 30 minutes (fetch, dedupe, store, no model calls) plus a static, I/O-free
  `/api/health`.

**Expected run-rate: roughly 1.5–3 CHF/month until v2.** The floor is what September spent on
everything but logs and models: Functions 0.63, alerts 0.46 and storage 0.34, 1.43 together, a
little less once digest runs stop. On top comes whatever Log Analytics still ingests after the fix.
October's bill will be a blend of v1 until the 21st and harvest only after it.

## Guardrails now

Azure OpenAI has no hard spend limit, and until v2 nothing needs one: there are no deployments to
call. Out of band:

- **25 CHF on the resource group** (`monthlyBudget`), alerting at Actual 60 % (15 CHF), Actual 90 %
  (22.50 CHF) and Forecast 100 %, to the email and the action group. The old 40 % line was 10 CHF,
  below September's 11.08, so it fired every month in normal operation, and an alert that always
  fires is one nobody reads.
- **130 CHF on the whole subscription** (`subscriptionGuardBudget`), alerting at Actual 80 % and
  Forecast 100 %. The 25 CHF budget sees only this resource group, but the credit (~$150) is shared
  with other projects, and when it runs out Azure suspends the subscription, this app included.
- **`maximumInstanceCount` 5** and **the 0.2 GB/day ingestion cap** bound everything that isn't a
  model call.

v2 adds fail-closed daily and monthly spend reservations in `core/budget.py`. And because the credit
is licensed for dev/test only, v2 moves to pay-as-you-go before any public launch, which is where
the grants assumed below are on offer.

## First live run — 2026-07-28, Switzerland North

*History: v1's first run, priced from its `run` documents at USD list prices.*

**Harvest** (34 feeds, cold start, everything new):

| | |
|---|---:|
| Feeds polled | 34 |
| Feeds failed | 0 |
| Items seen | 1,357 |
| Items new | 1,348 |
| Duplicates caught by the 409 dedupe | 9 |

The 9 duplicates are cross-feed collisions on the very first run — the same
story published through two feeds, caught by content-hash id without a query.

**Digest** (gate → score → summarize):

| Stage | Volume | Cost |
|---|---:|---:|
| Gate — embeddings | 488 candidates, 35,054 tokens | $0.0007 |
| Score — `gpt-5-nano` | 200 articles, 19,918 in / 6,333 out | $0.0035 |
| Summarize — `gpt-5.4` | 16 articles (3 paywalled) | $0.0872 |
| **Total** | | **$0.0907** |

Extrapolated at 3 digests/day: **~$8.30/month** of model spend. September's actual models line was
4.56 CHF: the estimate ran high on models and was blind to everything else.

## What this tells us

**Summarization is 96% of the model bill.** Scoring 200 articles costs less than
half a cent; summarizing 16 costs 25x that. The levers that matter are
`SUMMARIZE_THRESHOLD` (65 in v1) and the summarizer model — not anything
about the scorer. Raising the threshold to 75 would roughly halve the total.

**Prompt caching is not firing** (`cached=0` on every call). Caching needs a
byte-identical prefix of **at least 1,024 tokens**, and the current system
prompt — rubric plus profile — is well short of that. Padding it purely to
cross the threshold would add input tokens at full price on the first call of
every burst to save 90% on the rest of a $0.0035 line item. Not worth it at
this volume; revisit if the scorer ever dominates.

**The gate saw 488 candidates, not 1,348.** Correct behaviour: articles are
bucketed by publication day and the gate looks back 3 days, while feeds like
`openai.com/news/rss.xml` carry 1,051 items spanning years. Old items are
stored and deduped but never re-enter the expensive path.

## Fixed costs — as assumed in July

> **Wrong for this subscription.** The table assumed free grants that a Visual Studio credit
> subscription does not get (see the September baseline above). "At rest" also hid that this
> pipeline is never at rest: harvest runs 48 times a day, and every run bills. Kept as the record
> of what was assumed.

Everything except the Functions storage account is $0 at rest:

| Component | At rest |
|---|---:|
| Cosmos DB (free tier: 1,000 RU/s + 25 GB) | $0.00 |
| Flex Consumption (always-ready = 0) | $0.00 |
| Foundry resource + Global Standard deployments | $0.00 |
| App Insights / Log Analytics (1 GB/day cap, inside the 5 GB grant) | $0.00 |
| Storage account (required by the Functions host) | ~$0.15–0.30 |

v1's guardrails were `DAILY_TOKEN_BUDGET_USD` = $1.50, which aborted the LLM stages in-process, and
a Cost Management budget alerting at 40% and 100% of 25 (francs, though the parameter said USD) plus
a forecast trigger. "Guardrails now" above has the current set.

## Model choice

*v1's models. Phase 0 deleted all three deployments.*

v1's plan specified `gpt-5.6-luna` / `gpt-5.6-terra`. In July 2026 this
subscription had a quota limit of **0** for every gpt-5.6 variant in all ten
regions checked, which is a subscription-level gap, not a regional one — moving
to Switzerland North did not change it. It needs a manual quota-increase request.

Deployed instead:

| Role | Model | Price /1M in → out |
|---|---|---|
| Scorer | `gpt-5-nano` | $0.05 → $0.40 |
| Summarizer | `gpt-5.4` | $2.50 → $15.00 |
| Embeddings | `text-embedding-3-small` (512 dims) | $0.02 |

`gpt-5.4` is the same price as the planned `gpt-5.6-terra`, so the expensive
tier is unchanged; the scorer became 20x cheaper. The switch-back params in
`infra/modules/ai.bicep` went with the deployments, and the scorer is not coming
back: gpt-5-nano retires on 2027-02-09, and whether it could even be redeployed
after deletion is unverified. v2 picks its models afresh, as
capped pay-per-token deployments: Data Zone Standard (EU) for the chat models,
regional Standard for embeddings.

Note that reasoning effort is **not portable between models**: `minimal` is
accepted only by the original GPT-5 family and `none` only by gpt-5.1 and later.
The wrong value is rejected outright, which is why it travels with the model id.
