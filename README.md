# MyNews

An iOS news app on a small Azure backend, being rebuilt from a single-user tool into an invite-only
app for many people. A crawler pulls 65 RSS feeds into Cosmos every half hour. v1 had an LLM filter
them against one person's interests and write English summaries, and served a ranked feed that
linked to the original article. v2 will classify and cluster each story once, for everyone, build
each person's brief with plain arithmetic instead of per-user model calls, and still send every
reader to the publisher.

Hobby project. The binding constraint is **low running cost**, against a budget of **25 CHF a
month**. Francs, not dollars: an Azure budget has no currency of its own and counts in the billing
currency, which here is CHF, so the old `monthlyBudgetUsd = 25` was always 25 francs. The first
estimate was ~$8/month. September 2026, v1's last full month, measured **11.08 CHF**, almost half of
it SDK log noise. With v1 retired, harvest alone should cost roughly 1.5–3 CHF a month until v2
(`docs/cost.md`).

## Status

**v1 was retired on 2026-10-21.** The AI digest, the weekly profile rewrite and every HTTP route but
a static `GET /api/health` are gone; `harvest_timer` keeps filling Cosmos every half hour until v2's
harvest replaces it. **v2 is in progress**: one shared backend for invited users, who sign in with
Microsoft Entra External ID; content tiered by what each source permits; and a new, calmer iOS app
built on Swiss design principles, with a finite brief three times a day instead of an endless ranked
feed.

## Layout

```
ios/            SwiftUI app, v1 (iOS 26): feed, detail, settings, SwiftData offline cache.
                No backend since 2026-10-21; v2 replaces it.
infra/          Bicep. Cosmos (free tier), Flex Consumption, Foundry (no model deployments
                until v2), two budgets, alerts.
backend/        Python Azure Functions (the Python v2 programming model, not MyNews v2).
  core/         config, cosmos, foundry, budget, models, sources
  fetch/        HTTP with conditional GET; parsing and canonicalization
  pipeline/     harvest, the one stage still scheduled. gate, score, summarize, cluster,
                digest and profile are v1 code no trigger reaches; v2 deletes them together
                with the v1 harvest.
  scripts/      probe_feeds.py, export_all.py, rescue_dump.py
  tests/        pytest, no network
scripts/        whatif.sh + whatif_guard.py (the preprovision gate), postprovision.sh
docs/           cost.md: what this actually costs, measured
```

## Getting started

```sh
# 1. Dependencies. requirements.txt is hash-locked, compiled from requirements.in
#    (its header has the exact command). pytest stays out of it: the lock is what
#    Azure installs, so it holds runtime dependencies only.
cd backend && uv venv --python 3.12 .venv
uv pip install -r requirements.txt
uv pip install pytest

# 2. Tests — no network, no Azure needed
.venv/bin/python -m pytest -q

# 3. See what every configured feed actually does — no Azure needed
.venv/bin/python scripts/probe_feeds.py
.venv/bin/python scripts/probe_feeds.py heise verge      # or just a few

# 4. Deploy (writes to real Azure — read the warnings below first)
cd .. && azd auth login
az login                      # as well: the what-if goes through az, which signs in apart
azd env new mynews            # or `azd env select mynews`; the name is load-bearing, see below
azd env set ALERT_EMAIL you@example.com
azd env set BUDGET_START_DATE 2026-07-01
azd env set GUARD_BUDGET_START_DATE 2026-10-01
./scripts/whatif.sh           # read the diff before anything changes
azd provision && azd deploy   # the preprovision hook runs whatif.sh again

# 5. Rescue every point-readable document (see "The float16 outage" below)
cd backend && .venv/bin/python scripts/rescue_dump.py
```

`scripts/verify_vector_search.py` is superseded — the container no longer has a
vector policy. See below.

**The environment name is load-bearing.** Every resource name derives from
`uniqueString(subscription, environmentName, location)`, so `mynews` in Switzerland North on the same
subscription addresses the live stack. Any other name builds a second stack beside it, and its Cosmos
account is refused the free tier: there is one per subscription.

**The what-if guard runs before every provision.** `scripts/whatif.sh` runs
`az deployment sub what-if` (`main.bicep` is subscription-scoped) and pipes the JSON into
`scripts/whatif_guard.py`, which stops the provision before ARM touches anything when one of its
rules blocks. Most rules guard a row of "Things that will cost you money" below; the rest guard data
(backup mode, partition keys, the vector policy, any Delete). The float16 outage below is why:
`azd provision` would have quietly re-applied the policy that broke the container. To make a blocked
change on purpose, name the rules you accept, comma-separated, as the guard prints them:
`WHATIF_ALLOW=<rule-id>,<rule-id> azd provision`.

**Phase 0 switches the backup mode on its own, before provisioning.** Moving Cosmos from periodic to
continuous backup is one-way, and it starts a migration during which the account may refuse the
database, container and role-assignment writes that every provision repeats. So the switch is a
single targeted account update, and the provision waits until it has finished:

```sh
az cosmosdb update -g <resource-group> -n <cosmos-account> \
  --backup-policy-type Continuous --continuous-tier Continuous7Days
az cosmosdb show -g <resource-group> -n <cosmos-account> --query backupPolicy.migrationState
# repeat until status is Completed; then the provision sees no backup change and the guard passes
azd provision && azd deploy
```

**The budget start dates are fixed, not computed.** `BUDGET_START_DATE` is the live budget's own
start; the old template derived it from `utcNow()` and so tried to move it every month. A budget that
does not exist yet needs the first of the current month, because the API accepts a past start date
only within the current period: the guard's `2026-10-01` above is right only if the guard is created
in October 2026.

### Phase 0 manual cleanup

`azd provision` deploys in incremental mode: a resource the template stops declaring is left in
place, not deleted. Phase 0 removed three kinds of resource from the Bicep, so each has to be deleted
by hand, once, after the provision that drops it:

```sh
# 1. The three v1 model deployments: once each for scorer, summarizer and embedding.
az cognitiveservices account deployment delete -g <resource-group> -n <foundry-account> \
  --deployment-name <deployment>

# 2. The two queues nothing ever bound to: harvested-articles and harvested-articles-poison.
az resource delete --ids <storage-account-id>/queueServices/default/queues/<queue>

# 3. Monitoring Metrics Publisher on the storage account, where it granted nothing.
az role assignment delete --assignee <function-principal-id> \
  --role "Monitoring Metrics Publisher" --scope <storage-account-id>

# 4. The host key v1 pasted into the phone (and into shell history). No route accepts it any
#    more; regenerate it anyway, so it can never unlock a function-key route added later.
#    Omitting --key-value generates a new value.
az functionapp keys set -g <resource-group> -n <function-app> \
  --key-type functionKeys --key-name default
```

`azd env get-values` has the resource group; `az resource list -g <resource-group> -o table` names
everything in it; `az storage account show … --query id` and
`az functionapp identity show … --query principalId` give the storage account's id and the
Function's principal id.

Then `az cognitiveservices account deployment list -g <resource-group> -n <foundry-account>` has to
print `[]`. Until it does, the ~$280/day runaway ceiling those deployments' capacity allowed is still
live for anything holding a token. Treat deleting `scorer` as one-way: whether gpt-5-nano can be
deployed again is unclear (an old note says new customers lost it on 2026-08-07; Microsoft's schedule
still lists it as GA until it retires on 2027-02-09), so do not count on rolling back to v1's scorer.
The queues go through ARM rather than `az storage queue delete` because the account
has shared-key access off and no person holds a queue data role on it.

## Running the iOS app

**The v1 app no longer has a backend.** Its API — feed, feedback, profile, categories — was retired
with v1 on 2026-10-21, so the app opens on whatever it had cached and every request it makes fails.
v2 is a new app, not an update of this one. The build, signing and simulator notes below carry over;
the URL and key lines point at routes that no longer exist.

```sh
cd ios
xcodebuild -project MyNews.xcodeproj -target MyNews \
  -sdk iphonesimulator -configuration Debug build CODE_SIGNING_ALLOWED=NO

xcrun simctl boot "iPhone 17 Pro"
xcrun simctl install booted build/Debug-iphonesimulator/MyNews.app

# Debug builds accept the backend config from the launch environment, so you
# don't have to type a function key on a simulated keyboard. (v1 only: the API
# these point at was retired on 2026-10-21.)
export SIMCTL_CHILD_MYNEWS_BASE_URL="https://<your-func>.azurewebsites.net"
export SIMCTL_CHILD_MYNEWS_FUNCTION_KEY="$(az functionapp keys list -g rg-mynews-mynews \
  -n <your-func> --query functionKeys.default -o tsv)"
xcrun simctl launch --terminate-running-process booted com.vonmentlen.mynews
```

On a real device, enter the URL and key in Settings instead — the environment
seeding is `#if DEBUG` only and the key goes to the Keychain,
`AfterFirstUnlockThisDeviceOnly`, so it never lands in a backup.

### Running on a real iPhone

```sh
cd ios
xcodebuild -project MyNews.xcodeproj -target MyNews \
  -sdk iphoneos -configuration Debug -allowProvisioningUpdates build

xcrun devicectl list devices          # your phone must read `available`
xcrun devicectl device install app --device <UDID> build/Debug-iphoneos/MyNews.app
```

`-allowProvisioningUpdates` registers the App ID and creates the provisioning
profile on first run. The phone has to be connected and unlocked — it reads
`unavailable` otherwise, and the install fails with a `CoreDeviceError 1011`
naming an ECID rather than saying "not connected".

**`DEVELOPMENT_TEAM` is the certificate's OU, not the value in the identity
name.** `security find-identity` prints `Apple Development: Name (V78H5XSXCS)`,
but that parenthesised string is the *certificate* ID. Using it produces two
errors that both point at your Apple account rather than at the project, which
sends you looking in the wrong place:

```
error: No Account for Team "V78H5XSXCS". Add a new account in Accounts settings…
error: No profiles for 'com.vonmentlen.mynews' were found
```

Read the real Team ID off the certificate instead:

```sh
security find-certificate -c "Apple Development: <Your Name>" -p \
  | openssl x509 -noout -subject
# subject= UID=…, CN=Apple Development: … (V78H5XSXCS), OU=99J753GKC3, …
#                                                        ^^^^^^^^^^ Team ID
```


### The simulator-runtime trap

If `actool` fails the build with *"No simulator runtime version … available to
use with iphonesimulator SDK version"*, the installed simulator runtime doesn't
match the SDK Xcode shipped with. It fails the **whole build** rather than
degrading, and the message doesn't mention that a download is what's missing.

```sh
xcodebuild -downloadPlatform iOS   # ~8.5 GB
```

Xcode 26.4.1 ships the iOS 26.4 SDK but installs only older runtimes, so this
bites on a fresh machine. Removing `Assets.xcassets` is a valid stopgap that
costs only the app icon.

## Things that will cost you money if changed

These are load-bearing. Each one is the difference between a few francs a month and a lot more.
Prices are Microsoft's USD list prices; the bill arrives in CHF. And this subscription, a Visual
Studio credit, gets **no free grants** beyond the Cosmos free tier: Functions and Log Analytics bill
from the first second and the first gigabyte.

| Setting | Where | Why |
|---|---|---|
| `enableFreeTier: true` | `infra/modules/data.bicep` | **Settable only at account creation**, one per subscription. Gives 1,000 RU/s + 25 GB free, and makes TTL deletes free. Incompatible with serverless — which is why this is a provisioned account. |
| Total Cosmos RU/s ≤ 1000 | `infra/modules/data.bicep` | The free grant is a hard budget. A second 400 RU/s container costs $23.36/month. |
| No `alwaysReady` | `infra/modules/functions.bicep` | Always-ready instances bill around the clock, idle or not, and get no free grant on any subscription: $5.26/month at 512 MB. |
| `instanceMemoryMB: 512` | `infra/modules/functions.bicep` | Flex bills provisioned memory for the whole execution *including* time blocked on the network: feed fetches now, model calls again in v2. This pipeline is ~95% network wait, so 2048 MB would cost 4x for identical wall-clock. |
| `maximumInstanceCount: 5` | `infra/main.bicep` → `functions.bicep` | The runaway brake for everything that isn't a model call. With no free grant every execution bills, even a request the app rejects, so a flood costs in proportion to how far the app scales out: 5 instances at 512 MB, flat out, are 216,000 GB-s a day, a quarter of what the old cap of 20 allowed. v2's load test settles the final value, the smallest one that passes. |
| `dailyQuotaGb: '0.2'` | `infra/modules/monitoring.bicep` | Log Analytics bills every gigabyte here, and September's 5.09 CHF, the largest line, was almost all Cosmos SDK header dumps. The logger levels in `function_app.py` are the fix; the cap bounds a relapse at ~6 GB a month. Past it, ingestion stops until the daily reset, so `pipeline-silent` fires within ~3 hours: a flood announces itself. A string because Bicep has no decimal type; `json()` turns it into 0.2. |
| Foundry deployments: pay-per-token, capped `capacity` | `infra/modules/ai.bicep` | None until v2. The three v1 `GlobalStandard` deployments were removed in Phase 0 and deleted by hand (above), and with them a ~$280/day runaway ceiling: Azure OpenAI has no hard spend limit, and capacity is the most a loop can burn. v2 deploys Data Zone Standard (EU) for the chat models and regional Standard for embeddings, with a `@maxValue` on every capacity. Never `ProvisionedManaged`: ~$1,460/month per PTU with a ~15 PTU minimum, idle or not. |
| `REASONING_EFFORT_*` | app settings, from v2 | Not deployed: the settings went with the v1 digest, and nothing calls a model until v2. When models return, every gpt-5.x model is a reasoning model and reasoning tokens bill at the **output** rate, so leaving the model default on a 55-token answer multiplies the output bill several-fold. The value is not portable: `minimal` works only on the original GPT-5 family, `none` only on gpt-5.1 and later. |

Azure OpenAI has **no hard spend limit**. Until v2 the brake is structural: the Foundry account holds
no deployments, so there is nothing to call. The out-of-band brakes are two budgets: **25 CHF** on
the resource group (`monthlyBudget`: Actual 60 % and 90 %, Forecast 100 %), and a **130 CHF** guard
on the whole subscription (`subscriptionGuardBudget`: Actual 80 %, Forecast 100 %), because the
Visual Studio credit behind it is shared with other projects and suspends all of them, this one
included, when it runs out. v2 adds fail-closed daily and monthly spend reservations in
`core/budget.py`.

## The float16 outage

On **2026-08-02 08:30 UTC** every query against the Cosmos container started
returning:

```
(InternalServerError) Error converting value "float16" to type 'Microsoft.Azure.Cosmos.VectorDataType'.
Path 'vectorEmbeddingPolicy.vectorEmbeddings[0].dataType'
```

The container had been created with `dataType: 'float16'`. Microsoft's docs list
float16 as a supported value and ARM accepted it — five days later the *data
plane* could no longer deserialize the policy it had stored. Nothing had been
deployed. The Bicep comment guarded the wrong risk: *"If ARM rejects this, use
float32."* ARM never rejects it. **Control-plane validation is looser than
data-plane support**, and there is no way to discover that at deploy time.

What it took down, and what it didn't:

| | |
|---|---|
| Queries — cross-partition, single-partition, change feed, ReadFeed | **all 500** |
| Raw REST, every `x-ms-version` from 2015-12-16 to 2025-04-15 | **all 500** |
| Point reads | mostly fine, intermittently 500; a retry clears it |
| Writes | fine — harvest kept creating documents it could no longer read |

Point reads surviving is what makes this recoverable: `scripts/rescue_dump.py`
reconstructs document ids without asking Cosmos what exists. Config and
`source_state` ids are fixed; article ids are content-addressed
(`base64url(sha256(canonical_url))`, bucketed by publish date), so re-fetching
the feeds regenerates them. Feedback events are unrecoverable — their ids embed a
microsecond timestamp — but the learned state lives in the `centroids` document,
which is a point read.

**The fix was to delete the vector policy, not repair it.** Nothing in this
codebase ever issued a `VectorDistance` query; all similarity is NumPy over
embeddings read out of Cosmos (`pipeline/gate.py`, `pipeline/cluster.py`). The
policy was unused infrastructure that existed only to break production. Two
things follow:

- `az cosmosdb sql container update` is **read-modify-write**. Omitting
  `--vector-embeddings` silently re-applies the stored policy. `--idx` alone
  fixes nothing, and `--idx` replaces the *whole* indexing policy, so
  `excludedPaths` has to be restated verbatim.
- `azd provision` would have re-applied `float16` straight from `data.bicep`.
  Edit the Bicep **before** the next provision, and gate it with
  `az deployment sub what-if` (`main.bicep` is subscription-scoped).

Two things made a two-day total outage invisible:

- **No alerting existed.** App Insights' Failure Anomalies rule failed to deploy
  on day one with `MissingSubscriptionRegistration: Microsoft.AlertsManagement`,
  and nobody noticed a failed deployment of a monitoring rule. The only monitor
  was the cost budget — and an outage costs *less* than working, so it can never
  fire.
- **`/api/health` lied.** It probed Cosmos with a point read, the one operation
  that kept working. Sampled 8 times mid-outage it answered `healthy` 3 times. It
  now issues a cheap single-partition query as well, so it exercises the path
  that actually breaks. A health check that doesn't touch the failing dependency
  is worse than none — it launders an outage into a green dashboard.

Building the alert then hit two traps worth remembering, because both make a
dead pipeline look identical to a healthy one:

- **Adaptive sampling eats the evidence when you need it most.** The Azure SDK
  logs every HTTP request at Information level, so one harvest creating 112
  documents emitted 2,154 trace lines in ten minutes and blew through
  `host.json`'s 5-items/second cap — discarding its own `Harvest complete`
  summary along with the noise. An alert matching that text would have paged on
  a *healthy busy* pipeline. `function_app.py` now silences the SDK's HTTP and
  identity loggers (2,154 → 170) and the alert counts function executions, which
  `host.json` excludes from sampling outright.
- **`"Host.Results": "Warning"` means successful executions are never recorded
  at all** — only failures reach Application Insights. Any rule counting
  successes would have read zero forever. It is now `Information`.

The rule that shipped counts *successful* `harvest_timer` executions, not
executions: harvest ran on schedule through the entire two-day outage and failed
every single time, so counting runs alone would have stayed green throughout.
Checked against the real window — 23 executions, 0 successful.

*Postscript, 2026-10-21.* Two of the fixes above have since been revised. `/api/health` is static
again — liveness only, no I/O — because an anonymous route that queries Cosmos spends free-tier RU on
every unauthenticated hit, and it echoed deployment names and raw exception text to anyone who asked.
Dependency health moved to signals only the operator sees: the successful-harvest alert above and,
from v2, an admin health route. And the logger fix missed one: the Cosmos client dumps its HTTP
traffic through its own `azure.cosmos._cosmos_http_logging_policy`, which sits beside the silenced
logger rather than under it, and kept every request's headers flowing at INFO — 941k traces in 30
days and the largest line on September's bill (`docs/cost.md`). Phase 0 silences the whole `azure`
tree.

## Models

**None deployed since v1 was retired.** v1 ran three `GlobalStandard` deployments:
**`gpt-5-nano`** for bulk scoring ($0.05/$0.40 per 1M), **`gpt-5.4`** for summaries ($2.50/$15.00)
and `text-embedding-3-small` at 512 dims for the gate. Phase 0 deleted them along with the digest
that called them (see "Phase 0 manual cleanup").

v1's plan called for `gpt-5.6-luna`/`terra`, but in July 2026 this subscription had a quota limit of
**0** for every gpt-5.6 variant in all ten regions checked — a subscription-level gap that needs a
manual quota request, not something a region change fixes. `gpt-5.4` is priced identically to the
planned `gpt-5.6-terra`, so only the scorer changed, and it got 20x cheaper. Phase 0 deleted that
scorer, and gpt-5-nano retires on 2027-02-09 in any case, so v2 does not plan on it.

v2 deploys a classifier and a summarizer as Data Zone Standard (EU), so prompts are processed inside
the EU data zone rather than wherever there is capacity, and embeddings as a regional Standard
deployment, each with a capped capacity. Data Zone quota is pooled across the subscription and
shared with its other projects, so check it before deploying.

Note there is **no bare `gpt-5.6`** on Azure — luna/terra/sol are real model names, not size
codenames. And reasoning effort is **not portable between models**: `minimal` works only on the
original GPT-5 family, `none` only on gpt-5.1 and later. The wrong value is rejected outright,
which is why it lives in config beside the model id.

Everything retires within ~12 months; Foundry runs OpenAI models on a 12-month GA lifecycle, so
budget one model swap per ~9 months. Deployment names are config, not constants, and the JSON
schemas are the stable contract across a swap.

See `docs/cost.md` for the measured September bill and the first live run.

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

- **Invite-only TestFlight first, the App Store (Switzerland storefront) later.** v1 stayed off the
  App Store because personal use is what made the EU press-publishers' right and the private-copying
  exception apply. Serving other people voids both, so v2 justifies what it shows source by source
  instead.
- **Content is tiered by each source's permission**, flagged per catalog source and enforced in code.
  Tier A, a published allowance (MyNews does not ask publishers for permission; the beta is expected
  to stay private): the model may read the full text. Tier B, no reservation found: the model reads
  the headline and the publisher's own feed description. Tier C, an explicit AI/TDM reservation: the
  model reads the title only. B and C display the same: the headline and that description, capped,
  verbatim. A source's flag is set once; the code refuses everything the flag does not allow.
- **AI summaries only where the publisher allows it** (Tier A), labelled as AI-written and
  translated only where the permission covers translation. Everything else shows the publisher's
  own headline and teaser, and publisher text is never machine-translated.
- **Always link to the original article.** Every story, every tier.
- **Paywalls: detect, label and respect.** Never bypass.
- **Identify honestly.** From v2 the crawler is a named bot with a contact page,
  `MyNewsBot/2.0 (+https://tvonment.github.io/my-news/bot/)`, and it checks robots.txt and TDM
  reservations (Content-Signal, TDMRep) before fetching anything beyond the feed. Never choose a
  User-Agent to get around AI-bot rules: derStandard, Spiegel, ORF, Zeit and FAZ `Disallow`
  `ClaudeBot`, `GPTBot`, `anthropic-ai`, `CCBot` and `PerplexityBot` **by name** while leaving
  `User-agent: *` open, and that is a publisher saying no to AI use, not a filter to slip past. v1
  got this wrong: its UA was chosen not to match those rules. The v1 harvest that runs until v2
  still sends it, still backfills thin leads from article pages without checking robots.txt, and
  still carries a Safari UA override for developer.apple.com; v2's harvest drops all three.
