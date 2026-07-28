#!/bin/sh
# Runs after `azd provision`. Writes backend/local.settings.json from the azd
# environment so `func start` talks to the real Cosmos and Foundry resources
# using your own Entra identity (via DefaultAzureCredential / `az login`).
#
# No keys are written: Cosmos and Foundry both have disableLocalAuth = true.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/backend/local.settings.json"

get() { azd env get-value "$1" 2>/dev/null || echo ""; }

COSMOS_ENDPOINT="$(get COSMOS_ENDPOINT)"
COSMOS_DATABASE="$(get COSMOS_DATABASE)"
COSMOS_CONTAINER="$(get COSMOS_CONTAINER)"
FOUNDRY_ENDPOINT="$(get FOUNDRY_ENDPOINT)"
MODEL_SCORER="$(get MODEL_SCORER)"
MODEL_SUMMARIZER="$(get MODEL_SUMMARIZER)"
MODEL_EMBEDDING="$(get MODEL_EMBEDDING)"

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
    "FOUNDRY_ENDPOINT": "$FOUNDRY_ENDPOINT",
    "MODEL_SCORER": "$MODEL_SCORER",
    "MODEL_SUMMARIZER": "$MODEL_SUMMARIZER",
    "MODEL_EMBEDDING": "$MODEL_EMBEDDING",
    "HARVEST_SCHEDULE": "0 */30 * * * *",
    "DIGEST_SCHEDULE": "0 0 5,10,17 * * *",
    "DAILY_TOKEN_BUDGET_USD": "1.50",
    "PUSH_ENABLED": "false"
  }
}
JSON

echo "postprovision: wrote $OUT"
echo
echo "Next: verify the one unproven assumption in the plan —"
echo "  cd backend && .venv/bin/python scripts/verify_vector_search.py"
