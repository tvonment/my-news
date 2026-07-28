# Measured costs

Not estimates — these are the token counts the pipeline actually recorded, read
back from the `run` documents in Cosmos. Every pipeline run writes its real
usage, so this file can be reconciled against reality rather than aspiration:

```sh
curl -s "$FUNC_URL/api/runs" -H "x-functions-key: $KEY"
```

## First live run — 2026-07-28, Switzerland North

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

Extrapolated at 3 digests/day: **~$8.30/month** of model spend.

## What this tells us

**Summarization is 96% of the bill.** Scoring 200 articles costs less than
half a cent; summarizing 16 costs 25x that. The levers that matter are
`SUMMARIZE_THRESHOLD` (currently 65) and the summarizer model — not anything
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

## Fixed costs

Everything except the Functions storage account is $0 at rest:

| Component | At rest |
|---|---:|
| Cosmos DB (free tier: 1,000 RU/s + 25 GB) | $0.00 |
| Flex Consumption (always-ready = 0) | $0.00 |
| Foundry resource + Global Standard deployments | $0.00 |
| App Insights / Log Analytics (1 GB/day cap, inside the 5 GB grant) | $0.00 |
| Storage account (required by the Functions host) | ~$0.15–0.30 |

Guardrails: `DAILY_TOKEN_BUDGET_USD` = $1.50 aborts the LLM stages in-process,
and a Cost Management budget alerts at 40% and 100% of $25 plus a forecast
trigger. Azure OpenAI has no hard spend limit, so these are the only brakes.

## Model choice

The plan specified `gpt-5.6-luna` / `gpt-5.6-terra`. This subscription has a
quota limit of **0** for every gpt-5.6 variant in all ten regions checked, which
is a subscription-level gap, not a regional one — moving to Switzerland North
did not change it. It needs a manual quota-increase request.

Deployed instead:

| Role | Model | Price /1M in → out |
|---|---|---|
| Scorer | `gpt-5-nano` | $0.05 → $0.40 |
| Summarizer | `gpt-5.4` | $2.50 → $15.00 |
| Embeddings | `text-embedding-3-small` (512 dims) | $0.02 |

`gpt-5.4` is the same price as the planned `gpt-5.6-terra`, so the expensive
tier is unchanged; the scorer became 20x cheaper. To switch back once quota is
granted, change the four params in `infra/modules/ai.bicep` and the two
`REASONING_EFFORT_*` settings — the prices are already in `core/budget.py`.

Note that reasoning effort is **not portable between models**: `minimal` is
accepted only by the original GPT-5 family and `none` only by gpt-5.1 and later.
The wrong value is rejected outright, which is why it travels with the model id.
