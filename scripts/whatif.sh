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

# The result holds full before/after payloads. What-if masks app settings as
# "*******", but the alert email and the principal ids are in it, so it lives
# in a private temp file (mktemp makes it 0600) for as long as this runs.
OUT="$(mktemp "${TMPDIR:-/tmp}/whatif.XXXXXX")"
trap 'rm -f "$OUT"' EXIT
trap 'exit 130' HUP INT TERM

echo "whatif.sh: what-if of infra/main.bicep for '$ENV_NAME' in $LOCATION..."

# The parameters are the ones infra/main.parameters.json maps from the azd env;
# keep the two lists in step. az can't read that file itself: its ${VAR}
# placeholders are azd's, not ARM's. Everything else takes its Bicep default,
# as it does under azd.
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
  --result-format FullResourcePayloads \
  --no-prompt true \
  --no-pretty-print -o json > "$OUT"

status=0
python3 scripts/whatif_guard.py "$OUT" || status=$?
exit "$status"
