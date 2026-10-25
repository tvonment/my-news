#!/bin/sh
# Runs after `azd provision`. Writes backend/local.settings.json from the azd
# environment so `func start` talks to the real Cosmos account using your own
# Entra identity (via DefaultAzureCredential / `az login`).
#
# No keys are written: Cosmos has disableLocalAuth = true.
#
# The values mirror the Function's app settings in
# infra/modules/functions.bicep. Locally, Azurite (UseDevelopmentStorage)
# stands in for the host's storage account and telemetry stays off, so
# AzureWebJobsStorage__accountName and APPLICATIONINSIGHTS_CONNECTION_STRING
# are not written.
#
# Phase 0 retired v1's digest and profile runs, so FOUNDRY_ENDPOINT, MODEL_*,
# DIGEST_SCHEDULE, PROFILE_SCHEDULE, DAILY_TOKEN_BUDGET_USD and PUSH_ENABLED
# are gone here too; core/config.py keeps a default for each. The MODEL_*
# values earlier provisions left in the azd env are no longer read: azd does
# not remove values for outputs that are gone.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/backend/local.settings.json"

get() { azd env get-value "$1" 2>/dev/null || echo ""; }

COSMOS_ENDPOINT="$(get COSMOS_ENDPOINT)"
COSMOS_DATABASE="$(get COSMOS_DATABASE)"
COSMOS_CONTAINER="$(get COSMOS_CONTAINER)"

if [ -z "$COSMOS_ENDPOINT" ]; then
  echo "postprovision: COSMOS_ENDPOINT is empty — is the deployment finished?" >&2
  exit 1
fi

cat > "$OUT" <<JSON
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "COSMOS_ENDPOINT": "$COSMOS_ENDPOINT",
    "COSMOS_DATABASE": "$COSMOS_DATABASE",
    "COSMOS_CONTAINER": "$COSMOS_CONTAINER",
    "HARVEST_SCHEDULE": "0 */30 * * * *"
  }
}
JSON

echo "postprovision: wrote $OUT"
