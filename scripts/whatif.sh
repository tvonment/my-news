#!/bin/sh
# Runs before `azd provision` (azure.yaml, preprovision hook). Asks ARM what the
# provision would change, then hands the answer to scripts/whatif_guard.py,
# which refuses changes that cost money or lose data. A non-zero exit here
# stops the provision before anything is touched. It runs the same by hand:
#
#   ./scripts/whatif.sh
#
# A blocked change you do want gets through by naming its rule, and only that
# rule:
#
#   WHATIF_ALLOW=cosmos-backup azd provision
#
# what-if is a POST to ARM, but it only predicts: it creates, changes and
# deletes nothing. It needs `az login`, on top of `azd auth login`: the two
# CLIs sign in separately, and what-if goes through az.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Inside the hook azd exports the environment it is provisioning, so
# `azd provision -e mynews-dev` is checked against mynews-dev and not against
# whichever environment happens to be the default.
VALUES="$(azd env get-values ${AZURE_ENV_NAME:+-e "$AZURE_ENV_NAME"})"

# get-values prints one KEY="value" line per variable. Picked out with sed and
# never eval'd, so a value can't run as shell code.
value() {
  printf '%s\n' "$VALUES" | sed -n "s/^$1=\"\{0,1\}\([^\"]*\)\"\{0,1\}\$/\1/p" | tail -n 1
}

missing=""
for key in AZURE_ENV_NAME AZURE_LOCATION ALERT_EMAIL BUDGET_START_DATE GUARD_BUDGET_START_DATE; do
  [ -n "$(value "$key")" ] || missing="$missing $key"
done
if [ -n "$missing" ]; then
  echo "whatif.sh: not set in the azd environment:$missing" >&2
  echo "  set each with: azd env set <NAME> <value>   (the budget dates are YYYY-MM-DD)" >&2
  exit 2
fi

ENV_NAME="$(value AZURE_ENV_NAME)"
LOCATION="$(value AZURE_LOCATION)"
ALERT_EMAIL="$(value ALERT_EMAIL)"
BUDGET_START_DATE="$(value BUDGET_START_DATE)"
GUARD_BUDGET_START_DATE="$(value GUARD_BUDGET_START_DATE)"
# Optional. Empty only leaves out the developer's own Cosmos data role, which no
# guard rule reads; incremental mode never removes it for being left out.
PRINCIPAL_ID="$(value AZURE_PRINCIPAL_ID)"
# The subscription azd provisions into, not whatever `az account show` says.
# Shared with other projects, the two can differ, and a what-if against the
# wrong one shows a whole new stack (which the guard does stop, as a new
# Cosmos account, but with the wrong explanation).
SUBSCRIPTION="$(value AZURE_SUBSCRIPTION_ID)"
# The app as the last provision left it. Both are empty in an environment that
# has never been provisioned, which has no settings to lose.
APP_NAME="$(value SERVICE_BACKEND_NAME)"
RESOURCE_GROUP="$(value AZURE_RESOURCE_GROUP)"

# The result holds full before/after payloads: the alert email, the principal
# ids and every app setting the template sets in the clear (a secure parameter
# stays the unevaluated expression [parameters('hmacKey')]), so it lives in a
# private temp file (mktemp makes it 0600) for as long as this runs.
OUT="$(mktemp "${TMPDIR:-/tmp}/whatif.XXXXXX")"
LIVE="$(mktemp "${TMPDIR:-/tmp}/whatif-settings.XXXXXX")"
trap 'rm -f "$OUT" "$LIVE"' EXIT
trap 'exit 130' HUP INT TERM

echo "whatif.sh: what-if of infra/main.bicep for '$ENV_NAME' in $LOCATION..."

# The parameters with a default in infra/main.parameters.json (${VAR=default}),
# each passed only when the azd env sets it, exactly as azd falls back to the
# Bicep default for an unset one: an empty value would fail an @allowed list
# (cosmosMode, identityDeleteMode) or an int (minSupportedBuild). Without them
# the what-if would preview other settings than the provision deploys, and the
# provision-time checks on DISABLED_TIMERS and APPLE_SECRET_RENEW_BY
# (appsettings.bicep) would never run here. Collected as positional
# parameters, the one list POSIX sh has, so a value with spaces stays one
# argument.
#
# hmacKey (MYNEWS_HMAC_KEY) is left out on purpose: what-if never evaluates a
# secure parameter (the setting previews as [parameters('hmacKey')] either
# way) and no guard rule reads it, so passing the key would only put it on a
# command line, where `ps` shows it, for nothing.
set --
for pair in \
  cosmosMode=COSMOS_MODE \
  disabledTimers=DISABLED_TIMERS \
  entraTenantId=ENTRA_TENANT_ID \
  entraTenantSubdomain=ENTRA_TENANT_SUBDOMAIN \
  entraApiClientId=ENTRA_API_CLIENT_ID \
  entraAllowedClientIds=ENTRA_ALLOWED_CLIENT_IDS \
  entraRequiredScope=ENTRA_REQUIRED_SCOPE \
  entraAdminRole=ENTRA_ADMIN_ROLE \
  identityDeleteMode=IDENTITY_DELETE_MODE \
  minSupportedBuild=MIN_SUPPORTED_BUILD \
  appleRenewBy=APPLE_SECRET_RENEW_BY
do
  setting="$(value "${pair#*=}")"
  if [ -n "$setting" ]; then
    set -- "$@" "${pair%%=*}=$setting"
  fi
done

# The parameters are the ones infra/main.parameters.json maps from the azd env;
# keep the two lists in step (backend/tests/test_settings_manifest.py holds
# them together). az can't read that file itself: its ${VAR} placeholders are
# azd's, not ARM's. Everything else takes its Bicep default, as it does under
# azd.
#
# --result-format FullResourcePayloads (the default, pinned): the guard reads
#   the before/after payloads. ResourceIdOnly would leave it nothing to check.
# No --exclude-change-types: cosmos-throughput sums the unchanged containers
#   too, so NoChange has to stay in.
# --no-prompt: a parameter this list misses fails the run instead of waiting
#   for input azd's non-interactive hook can't give.
# --name: a fixed name of our own. az's default is the template's file name,
#   and a subscription-level deployment called "main" left in another location
#   by a neighbouring project would fail this one.
AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no az deployment sub what-if \
  ${SUBSCRIPTION:+--subscription "$SUBSCRIPTION"} \
  --name mynews-whatif \
  --location "$LOCATION" \
  --template-file infra/main.bicep \
  --parameters \
    environmentName="$ENV_NAME" \
    location="$LOCATION" \
    alertEmail="$ALERT_EMAIL" \
    budgetStartDate="$BUDGET_START_DATE" \
    guardBudgetStartDate="$GUARD_BUDGET_START_DATE" \
    principalId="$PRINCIPAL_ID" \
    "$@" \
  --result-format FullResourcePayloads \
  --no-prompt true \
  --no-pretty-print -o json > "$OUT"

# The app's setting names as they are now, for the guard's
# app-settings-removed rule: what-if can't read app settings itself (ARM serves
# them only through a list action), so it reports every one as new. --query
# keeps the names and drops the values inside az, so no value is printed or
# written here. A failed list stops the provision (set -e): a guard that can't
# compare must not pass.
set --
if [ -n "$APP_NAME" ] && [ -n "$RESOURCE_GROUP" ]; then
  AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no az functionapp config appsettings list \
    ${SUBSCRIPTION:+--subscription "$SUBSCRIPTION"} \
    --resource-group "$RESOURCE_GROUP" \
    --name "$APP_NAME" \
    --query '[].name' -o json > "$LIVE"
  set -- --live-settings "$LIVE"
fi

status=0
python3 scripts/whatif_guard.py "$OUT" "$@" || status=$?
exit "$status"
