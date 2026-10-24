# Phase 1 production rollout

The ordered steps that take environment `mynews` from Phase 0 to Phase 1:

- the `news` container next to `items`;
- the job queues and the full settings manifest;
- the new code, with sign-in and invites.

Commands only, and none of them prints a secret.

- **Who runs what.** Read-only commands can run at any time. Every write (deploy, provision, a Graph
  or Entra change) waits for the owner's go-ahead.
- **Repository.** Run everything from the root of a clean checkout of the Phase 1 commit that CI
  passed, in one shell: later steps reuse the variables and helpers that step 1 defines.
- **Identity.** It is in place already ([docs/identity.md](../identity.md), Status).
- **Dev first.** If `mynews-dev` exists, rehearse there first: `azd env select mynews-dev`, and pass
  `--env mynews-dev` to both scripts.

**Order: provision with the harvest off, deploy, then provision again to switch it back on.**

- The live Phase 0 code reads `COSMOS_CONTAINER` for every harvest read and write, and the provision
  turns that setting from `items` to `news`. One Phase 0 harvest after the provision would seed v1
  documents (`src`, `run`, `art|…`, `user|me`) into `news`.
- The Phase 1 code needs the Phase 1 settings. Its queue workers bind to the `JOBS_STORAGE`
  connection, which only the provision creates, and on the old settings its v2 code would point at
  `items`. So this runbook never deploys it first (`infra/modules/appsettings.bicep` describes the
  same order).

The first provision therefore also switches the harvest off (`DISABLED_TIMERS=harvest_timer`), in
the same settings write that moves `COSMOS_CONTAINER`. No Phase 0 harvest ever runs against `news`,
and no unpaced one runs against `items` once it is down to 400 RU/s. The deploy follows, and a second
provision switches the harvest back on. The harvest pauses for steps 3 to 5, about half an hour.
Started a few minutes after a :00 or :30 run, the cutover skips at most one run.

## 0. The day before

```sh
# Two test identities, created through the user flow ("No account? Create one" on the hosted page).
# Use mailboxes you can read (plus-addresses work); neither gets a role. The token goes to /dev/null:
# only the cached sign-in is wanted.
backend/.venv/bin/python scripts/get_token.py --profile test --login > /dev/null        # stays: the non-admin account
backend/.venv/bin/python scripts/get_token.py --profile throwaway --login > /dev/null   # deleted in step 6.6
```

## 1. Pre-flight (read-only)

```sh
export AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no        # az never installs an extension by itself
git status --short                                   # empty
git log --oneline -1                                 # the commit CI passed
(cd backend && .venv/bin/python -m pytest -q)
az bicep build --file infra/main.bicep --stdout > /dev/null
azd env select mynews
az login && azd auth login                           # if either has expired; the what-if runs through az

# The settings the provision maps, by name; no value is printed. Test azd's exit status, not its
# output: for a missing key, azd 1.37 writes "ERROR: key not found" to stdout as well.
for name in $(grep -o '\${[A-Z0-9_]*' infra/main.parameters.json | cut -c3- | sort -u); do
  if value=$(azd env get-value "$name" 2>/dev/null) && [ -n "$value" ]; then echo "set    $name"
  else echo "EMPTY  $name"; fi
done; unset value
# EMPTY is fine only where the file gives a default (${NAME=...}), and DISABLED_TIMERS must be EMPTY
# here. ENTRA_TENANT_ID, ENTRA_TENANT_SUBDOMAIN, ENTRA_API_CLIENT_ID, ENTRA_ALLOWED_CLIENT_IDS,
# APPLE_SECRET_RENEW_BY and MYNEWS_HMAC_KEY must be set.

# MYNEWS_HMAC_KEY is never regenerated: its fingerprint must match the password manager's, and the
# one postprovision.sh recorded on its first run (it checks again on every run).
printf 'mynews-hmac-fp:%s' "$(azd env get-value MYNEWS_HMAC_KEY)" | shasum -a 256 | cut -c1-12
if fp=$(azd env get-value MYNEWS_HMAC_KEY_FINGERPRINT 2>/dev/null); then echo "recorded: $fp"
else echo "recorded: none yet (the first provision records it)"; fi

# Baseline.
RG=$(azd env get-value AZURE_RESOURCE_GROUP); APP=$(azd env get-value SERVICE_BACKEND_NAME)
SUB=$(azd env get-value AZURE_SUBSCRIPTION_ID); API="$(azd env get-value SERVICE_BACKEND_URI)/api"
COSMOS=$(az cosmosdb list -g "$RG" --query '[0].name' -o tsv)
ST=$(az storage account list -g "$RG" --query "[?starts_with(name, 'stmynews')].name | [0]" -o tsv)
fns() {  # the app's functions, each with its isDisabled flag
  az functionapp function list -g "$RG" -n "$APP" --query '[].[name, isDisabled]' -o tsv | sed 's|.*/||' | sort
}
az cosmosdb sql container list -g "$RG" -a "$COSMOS" -d mynews --query '[].name' -o tsv          # items
az cosmosdb sql container throughput show -g "$RG" -a "$COSMOS" -d mynews -n items \
  --query '{ru: resource.throughput, min: resource.minimumThroughput}'                           # 1000, min 400
fns                                                                    # harvest_timer False, health False
```

Never use `az functionapp config appsettings list` here: it prints every value, `MYNEWS_HMAC_KEY`
included. Step 3 lists the setting names from the deployment's outputs instead, and
`mynews_admin.py health` reports missing settings by name (step 6.2).

## 2. What-if (read-only)

```sh
./scripts/whatif.sh
```

Expect these changes, and the guard to pass without `WHATIF_ALLOW`:

| Change | Resource |
|---|---|
| `~` | `items` throughput 1000 → 400 |
| `+` | `news`: 600 RU/s, `/pk`, `defaultTtl` -1, opt-in indexing from `cosmos-index-policy.json`; created after `items` comes down |
| `+` | queues `account-purge`, `suggestions`, `ops-jobs`, each with its `-poison` queue |
| `+`/`~` | `sites/config/appsettings`: the new `ENTRA_*`, `MYNEWS_HMAC_KEY`, `JOBS_STORAGE__*`, `LEGACY_COSMOS_CONTAINER`, `COSMOS_CONTAINER=news`, timer switches. The site itself may show as modified because its settings moved into this resource |

There must be no Delete, the total must stay at 1,000 RU/s, and every setting the app has today must
still be there. The what-if cannot read the live settings (its "before" for `appsettings` is empty),
so `whatif.sh` lists their names (never the values) and the guard's `app-settings-removed` rule
blocks if the template leaves one out. Today those are Phase 0's six:
`AzureWebJobsStorage__accountName`, `APPLICATIONINSIGHTS_CONNECTION_STRING`, `COSMOS_ENDPOINT`,
`COSMOS_DATABASE`, `COSMOS_CONTAINER` and `HARVEST_SCHEDULE`, all in
`infra/modules/appsettings.bicep`. Its "after" shows the values in
the clear, except `MYNEWS_HMAC_KEY`, which stays the expression `[parameters('hmacKey')]`. If
anything else is listed, read it before going on; if you can't explain it, stop.

## 3. Provision, with the harvest switched off

```sh
# A few minutes after a :00 or :30 harvest, so that the restart doesn't cut a run short.
azd env set DISABLED_TIMERS harvest_timer
azd provision          # preprovision runs whatif.sh again; postprovision.sh checks the HMAC key fingerprint
                       # (recording it on the first run) and writes backend/local.settings.json

az cosmosdb sql container list -g "$RG" -a "$COSMOS" -d mynews --query '[].name' -o tsv          # items, news
for c in items news; do
  az cosmosdb sql container throughput show -g "$RG" -a "$COSMOS" -d mynews -n "$c" --query resource.throughput
done                                                                                              # 400, 600
az cosmosdb sql container show -g "$RG" -a "$COSMOS" -d mynews -n news \
  --query 'resource.{pk: partitionKey.paths, ttl: defaultTtl, excluded: indexingPolicy.excludedPaths[].path}'
# pk ["/pk"], ttl -1, excluded ["/*", "/_etag/?"]
az rest --method get --query 'value[].name' -o tsv --url \
  "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/$ST/queueServices/default/queues?api-version=2023-05-01"
# account-purge, account-purge-poison, ops-jobs, ops-jobs-poison, suggestions, suggestions-poison
az deployment group show -g "$RG" -n appsettings --query 'properties.outputs.settingNames.value' -o tsv | sort
# names only: AzureWebJobs.harvest_timer.Disabled, COSMOS_CONTAINER, ENTRA_*, JOBS_STORAGE__*, MYNEWS_HMAC_KEY, ...
sleep 60               # the settings change restarts the app
fns                    # still the Phase 0 code: harvest_timer True (off), health False
curl -sS "$API/health" # 200, Phase 0's static body
```

## 4. Deploy the code

```sh
azd deploy
sleep 60
fns
# 19 functions: health; harvest_timer True (still off) and maintenance_timer; account_purge_worker
# and ops_jobs_worker; me_delete, me_export, me_get, me_patch, me_redeem; the nine admin_* routes

curl -sS -D - -H 'X-Request-Id: smoke-health-0001' "$API/health"
# 200 {"status":"ok","api":1,"minBuild":0,"time":...}, header X-Request-Id: smoke-health-0001
curl -sS -i "$API/me"
# 401 unauthenticated, WWW-Authenticate: Bearer realm="mynews"
```

If the deploy fails, see 7.1.

## 5. Switch the harvest back on

```sh
azd env set DISABLED_TIMERS ''
azd provision          # the what-if: the settings resource changes, nothing is created or deleted
sleep 60
fns                    # harvest_timer False; its next run is at the next :00 or :30
```

## 6. Smoke tests

```sh
PY=backend/.venv/bin/python
call() {  # call METHOD PATH [curl options...]: body on stdout, "HTTP <status>" on stderr; $TOKEN never reaches argv
  m=$1; p=$2; shift 2
  printf 'Authorization: Bearer %s\n' "$TOKEN" |
    curl -sS -X "$m" -H @- -w '%{stderr}\nHTTP %{http_code}\n' "$@" "$API$p"
}
JSON='Content-Type: application/json'
```

### 6.1 Fail closed, without a valid token

```sh
curl -sS -i "$API/me"                              # 401 unauthenticated, WWW-Authenticate: Bearer realm="mynews"
TOKEN=not.a.token; call GET /me                    # 401 invalid_token
curl -sS -o /dev/null -w '%{http_code}\n' "$API/no-such-route"   # 404 from the host, no envelope
```

### 6.2 The owner (admin)

```sh
TOKEN=$($PY scripts/get_token.py)
$PY scripts/get_token.py --decode | python3 -c 'import json, sys; c = json.load(sys.stdin)["claims"]; print(c["ver"], c["scp"], c.get("roles"))'
# 2.0 access_as_user ['admin']
call GET /me -H 'Accept-Language: de-CH,de;q=0.9,en;q=0.8'   # 200 Me, roles ["admin"]: the first call bootstraps your membership
$PY scripts/mynews_admin.py health                 # status ok, every check ok; hmacKeyFingerprint = step 1's
$PY scripts/mynews_admin.py overview               # users total=1; pipeline.lastHarvest is the last run before step 3
```

### 6.3 The invite gate, as the non-admin `test` account

```sh
TOKEN=$($PY scripts/get_token.py --profile test)
$PY scripts/get_token.py --profile test --decode | python3 -c 'import json, sys; print(json.load(sys.stdin)["claims"].get("roles"))'   # None
call GET /me                                                           # 403 invite_required
call GET /admin/overview                                               # 403 admin_required
call POST /me/redeem -H "$JSON" --data '{"code": "nope"}'              # 400 invite_malformed
call POST /me/redeem -H "$JSON" --data '{"code": "0000-0000-0000"}'    # 404 invite_not_found (1 of 10 attempts today)
call POST /me/redeem --data '{"code": "0000-0000-0000"}'               # 415 unsupported_media_type

$PY scripts/mynews_admin.py invites create --label "Smoke test" --expires-days 1   # prints the code, once
CODE=XXXX-XXXX-XXXX                                                    # paste it
call POST /me/redeem -H "$JSON" --data "{\"code\": \"$CODE\"}"         # 201 Me, roles []
call POST /me/redeem -H "$JSON" --data "{\"code\": \"$CODE\"}"         # 200 Me: already a member, nothing consumed
call GET /me                                                           # 200
call PATCH /me -H "$JSON" --data '{"settings": {"briefSize": 15}}'     # 200, settings.briefSize 15
call PATCH /me -H "$JSON" --data '{"roles": ["admin"]}'                # 400 validation_failed, unknown_field
call GET /admin/overview                                               # 403 admin_required, still
$PY scripts/mynews_admin.py invites list --status used                 # "Smoke test": used, 1/1
$PY scripts/mynews_admin.py users list                                 # you (admin yes) and test (admin no)
```

### 6.4 A used code, from a second identity

```sh
TOKEN=$($PY scripts/get_token.py --profile throwaway)
call POST /me/redeem -H "$JSON" --data "{\"code\": \"$CODE\"}"         # 409 invite_used
```

### 6.5 Block and unblock `test`

```sh
TOKEN=$($PY scripts/get_token.py --profile test)
TEST_UID=$(call GET /me | python3 -c 'import json, sys; print(json.load(sys.stdin)["uid"])')
$PY scripts/mynews_admin.py users block "$TEST_UID" --reason "smoke test"
call GET /me                                                           # 403 account_blocked
call GET /me/export -o /dev/null                                       # 200: export stays open to a blocked member
$PY scripts/mynews_admin.py users unblock "$TEST_UID"
call GET /me                                                           # 200
```

### 6.6 Deletion, end to end, as `throwaway`

```sh
$PY scripts/mynews_admin.py invites create --label "Smoke deletion" --expires-days 1
CODE2=XXXX-XXXX-XXXX                                                   # paste it
TOKEN=$($PY scripts/get_token.py --profile throwaway)
call POST /me/redeem -H "$JSON" --data "{\"code\": \"$CODE2\"}"        # 201
ME=$(call GET /me)
DEL_UID=$(printf '%s' "$ME" | python3 -c 'import json, sys; print(json.load(sys.stdin)["uid"])')
DEL_OID=$(printf '%s' "$ME" | python3 -c 'import json, sys; print(json.load(sys.stdin)["oid"])')
call GET /me/export -D - -o /dev/null      # 200, Content-Disposition: attachment; filename="mynews-export-<date>.json"
call DELETE /me                            # 202 {"state":"deleting","identityRemoval":"manual"}
call DELETE /me                            # 202 again, and no second queue message
call GET /me                               # 410 account_deleted {"state":"deleting"}; {"state":"deleted"} once purged
$PY scripts/mynews_admin.py deletions list --status all
# DEL_UID: purging, then awaiting_identity_removal with docsDeleted and DEL_OID

# The partition and the registry document are gone (read-only, with your own Cosmos data role):
COSMOS_ENDPOINT=$(azd env get-value COSMOS_ENDPOINT) OID=$DEL_OID $PY - <<'EOF'
import os
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity import DefaultAzureCredential
news = CosmosClient(os.environ["COSMOS_ENDPOINT"], DefaultAzureCredential()).get_database_client("mynews").get_container_client("news")
oid = os.environ["OID"]
print("left in user|<oid>:", list(news.query_items("SELECT VALUE COUNT(1) FROM c", partition_key=f"user|{oid}"))[0])  # 0
try:
    news.read_item(oid, partition_key="users")
    print("registry: STILL THERE")
except CosmosResourceNotFoundError:
    print("registry: gone")
EOF

# Identity removal (docs/identity.md §8.1): Entra admin center › Users › search DEL_OID › Delete,
# then Users › Deleted users › Delete permanently. Then:
$PY scripts/mynews_admin.py deletions complete "$DEL_UID"           # completed; the oid is erased
$PY scripts/get_token.py --profile throwaway --sign-out
```

### 6.7 The v1 harvest stays in `items` (after the next :00 or :30)

```sh
$PY scripts/mynews_admin.py overview          # pipeline lastHarvest.at is after step 5, lastHarvest.ok=true
COSMOS_ENDPOINT=$(azd env get-value COSMOS_ENDPOINT) $PY - <<'EOF'
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
news = CosmosClient(os.environ["COSMOS_ENDPOINT"], DefaultAzureCredential()).get_database_client("mynews").get_container_client("news")
for pk in ("src", "run", "user|me"):          # v1's partitions: nothing of v1 may land in news
    print(pk, list(news.query_items("SELECT VALUE COUNT(1) FROM c", partition_key=pk))[0])   # 0 each
EOF
```

## 7. Rollback

`az functionapp config appsettings set` echoes every setting with its value, so each call below ends
in `-o none`. It changes only the settings it names, and a hand-set value lasts until the next
`azd provision`, which restores `appsettings.bicep`.

### 7.1 During the cutover

- **Step 3 fails.** Fix the cause and run it again. The settings are written last, in one replace, so
  the app holds either the old set (Phase 0 harvesting into `items`, as before) or the new one (the
  harvest off).
- **Step 4 fails, and the fix will take more than an hour or two.** Feeds that keep only their newest
  10 items start losing stories while the harvest is off. Bring back Phase 0's harvest, pointed at
  `items`:

  ```sh
  az functionapp config appsettings set -g "$RG" -n "$APP" -o none \
    --settings COSMOS_CONTAINER=items AzureWebJobs.harvest_timer.Disabled=false
  ```

  Once the fix is in, run step 4 and then step 5 straight away: step 5's provision sets
  `COSMOS_CONTAINER=news` again. Any provision before then is safe too. The azd env still says
  `DISABLED_TIMERS=harvest_timer`, so it switches the harvest off together with the move to `news`.

### 7.2 After the rollout

```sh
# R1. Close the API, keep the harvest: every route but /health answers 503 auth_unavailable.
az functionapp config appsettings set -g "$RG" -n "$APP" --settings ENTRA_API_CLIENT_ID= -o none

# R2. Stop one function, e.g. the purge worker (its messages wait in the queue). For a timer, use
#     DISABLED_TIMERS and provision instead, which lasts.
az functionapp config appsettings set -g "$RG" -n "$APP" --settings AzureWebJobs.account_purge_worker.Disabled=true -o none

# R3. Back to the Phase 0 code (if the new harvest itself misbehaves). Phase 0 reads COSMOS_CONTAINER
#     for the harvest, so point it back at items FIRST, or its next run writes into news.
az functionapp config appsettings set -g "$RG" -n "$APP" --settings COSMOS_CONTAINER=items -o none
azd env set DISABLED_TIMERS harvest_timer    # a provision now switches the harvest off instead of
                                             # sending Phase 0's harvest to news
git switch --detach v2/phase-0               # the code that ran before (main, once PR #1 is merged)
azd deploy
git switch v2/phase-1
# Roll forward with the fixed code: step 4 (deploy), then step 5 (the harvest back on).
```

`news`, the queues and the settings are additive and cost nothing idle, so they stay. `items` stays at
400 RU/s: raising it again would first need `news` deleted (the total is capped at 1,000).

## 8. Done

- Record the date, the commit and the step 6 results in the plan's Progress note.
- Keep the `test` profile: it is the non-admin account later checks use.
- The 7-day refresh soak ([docs/identity.md §9](../identity.md#9-the-device-spike-go-or-no-go-before-any-phase-2-code))
  runs alongside, independent of this rollout.
