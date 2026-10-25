#!/bin/sh
# Runs after `azd provision` (azure.yaml, postprovision hook), and the same way
# by hand. Two jobs:
#
#   1. Check MYNEWS_HMAC_KEY against the fingerprint recorded in the azd env.
#      Every uid is HMAC-SHA256(key, oid), and the registry, tombstones,
#      deletion records, audit entries and invite redemptions all store one,
#      so a new key orphans every one of them: the key is generated once, kept
#      in the password manager and never regenerated. The fingerprint is
#      sha256("mynews-hmac-fp:" + key)[:12], the same value GET
#      /api/admin/health reports, so the password manager, the azd env and the
#      live app can be compared without anyone seeing the key.
#   2. Write backend/local.settings.json, so `func start` runs the app locally
#      against the environment just provisioned, reaching Cosmos with your own
#      Entra identity (DefaultAzureCredential / `az login`).
#
# local.settings.json mirrors the app settings in infra/modules/appsettings.bicep
# name for name (backend/tests/test_settings_manifest.py checks that both carry
# every setting the code reads). The local differences:
#   - AzureWebJobsStorage and JOBS_STORAGE are Azurite (UseDevelopmentStorage),
#     in place of the identity-based AzureWebJobsStorage__accountName and
#     JOBS_STORAGE__queueServiceUri/__credential: host state and the job queues
#     stay on this machine. With a connection string present the host ignores
#     the JOBS_STORAGE__* identity settings, and JOBS_STORAGE__queueServiceUri
#     is empty because this host has no Azure queue to peek, so a local
#     /admin/health fails its config check (that setting) and its queue check.
#   - LOCAL_READONLY=true: a local run cannot write to the real Cosmos account
#     unless a script opens allow_prod_writes() (its --allow-prod-write flag).
#   - Every timer is disabled, so a local host never harvests or runs
#     maintenance against the real account.
#   - No APPLICATIONINSIGHTS_CONNECTION_STRING: local telemetry stays off.
#
# The only secret in the file is MYNEWS_HMAC_KEY, which the local app needs to
# derive uids (Cosmos has disableLocalAuth = true, so there is no key to hold).
# The file is git-ignored, left out of the package `azd deploy` uploads, and
# written 0600; nothing here prints the key.
#
# Phase 0 retired v1's digest and profile runs, so FOUNDRY_ENDPOINT, MODEL_*,
# DIGEST_SCHEDULE, PROFILE_SCHEDULE, DAILY_TOKEN_BUDGET_USD and PUSH_ENABLED
# are not written; core/config.py keeps a default for each. The MODEL_* values
# earlier provisions left in the azd env are no longer read: azd does not
# remove values for outputs that are gone.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/backend/local.settings.json"

# One value from the azd env, or nothing. Inside the hook azd exports the
# environment it provisioned, so `azd provision -e mynews-dev` writes
# mynews-dev's settings, not the default environment's. A missing key is not
# an empty one: azd prints "key not found ..." on stdout and exits 1, so the
# output counts only when the lookup succeeded.
get() {
  if value="$(azd env get-value "$1" ${AZURE_ENV_NAME:+-e "$AZURE_ENV_NAME"} 2>/dev/null)"; then
    printf '%s' "$value"
  fi
}

COSMOS_ENDPOINT="$(get COSMOS_ENDPOINT)"
COSMOS_DATABASE="$(get COSMOS_DATABASE)"
COSMOS_CONTAINER="$(get COSMOS_CONTAINER)"
LEGACY_COSMOS_CONTAINER="$(get LEGACY_COSMOS_CONTAINER)"

if [ -z "$COSMOS_ENDPOINT" ]; then
  echo "postprovision: COSMOS_ENDPOINT is empty — is the deployment finished?" >&2
  exit 1
fi
# LEGACY_COSMOS_CONTAINER is a Phase 1 output. Without it the env still holds
# the outputs of an older provision, whose COSMOS_CONTAINER is v1's `items`:
# writing that would point the v2 code at the wrong container.
if [ -z "$LEGACY_COSMOS_CONTAINER" ]; then
  echo "postprovision: LEGACY_COSMOS_CONTAINER is not in the azd env, so its outputs predate" >&2
  echo "  infra/main.bicep's container split (COSMOS_CONTAINER=$COSMOS_CONTAINER). Provision first." >&2
  exit 1
fi

# --- MYNEWS_HMAC_KEY ---------------------------------------------------------
# The fingerprint, exactly as core/auth.hmac_key_fingerprint computes it. printf
# is a shell builtin and shasum reads stdin, so the key is never on a command
# line where `ps` could show it.
fingerprint() { printf '%s' "mynews-hmac-fp:$1" | shasum -a 256 | cut -c1-12; }

MYNEWS_HMAC_KEY="$(get MYNEWS_HMAC_KEY)"
RECORDED_FINGERPRINT="$(get MYNEWS_HMAC_KEY_FINGERPRINT)"

if [ -z "$MYNEWS_HMAC_KEY" ]; then
  if [ -n "$RECORDED_FINGERPRINT" ]; then
    echo "postprovision: MYNEWS_HMAC_KEY is empty, but key $RECORDED_FINGERPRINT was in use here." >&2
    echo "  The app now answers 503 auth_unavailable. Restore that key from the password manager" >&2
    echo "  (azd env set MYNEWS_HMAC_KEY '<key>') and provision again." >&2
    exit 1
  fi
  echo "postprovision: warning: MYNEWS_HMAC_KEY is not set, so every route but /health answers" >&2
  echo "  503 auth_unavailable. Generate it ONCE, keep it in the password manager, and provision again:" >&2
  # shellcheck disable=SC2016 # printed for the operator to run, not expanded here
  echo '    azd env set MYNEWS_HMAC_KEY "$(openssl rand -hex 32)"' >&2
else
  KEY_FINGERPRINT="$(fingerprint "$MYNEWS_HMAC_KEY")"
  if [ -z "$RECORDED_FINGERPRINT" ]; then
    # First provision with a key: remember which one, so a different key is
    # caught next time. A fresh checkout or a new machine starts without the
    # record, which is what the note in the password manager is for.
    if azd env set MYNEWS_HMAC_KEY_FINGERPRINT "$KEY_FINGERPRINT" ${AZURE_ENV_NAME:+-e "$AZURE_ENV_NAME"} >/dev/null; then
      echo "postprovision: MYNEWS_HMAC_KEY fingerprint $KEY_FINGERPRINT, recorded as MYNEWS_HMAC_KEY_FINGERPRINT."
      echo "  Note it next to the key in the password manager; /api/admin/health reports the same value."
    else
      echo "postprovision: could not record MYNEWS_HMAC_KEY_FINGERPRINT=$KEY_FINGERPRINT in the azd env; set it by hand." >&2
    fi
  elif [ "$KEY_FINGERPRINT" != "$RECORDED_FINGERPRINT" ]; then
    echo "postprovision: MYNEWS_HMAC_KEY has CHANGED: its fingerprint is $KEY_FINGERPRINT, but" >&2
    echo "  $RECORDED_FINGERPRINT was in use. The provision has already deployed it, and every uid the app" >&2
    echo "  derives now misses the ones stored in the registry, tombstones, deletions and audit log." >&2
    echo "  Restore the original key from the password manager (azd env set MYNEWS_HMAC_KEY '<key>')" >&2
    echo "  and provision again. local.settings.json was left as it was." >&2
    exit 1
  else
    echo "postprovision: MYNEWS_HMAC_KEY fingerprint $KEY_FINGERPRINT matches the recorded one."
  fi
  if [ "${#MYNEWS_HMAC_KEY}" -lt 32 ]; then
    # Not fixable later without re-deriving every stored uid, so say it now.
    echo "postprovision: warning: MYNEWS_HMAC_KEY is shorter than 32 characters." >&2
  fi
fi

# --- Identity ------------------------------------------------------------------
# What core/auth.py needs before it accepts any token. Missing or malformed ids
# fail closed there (503 auth_unavailable, the names in the log); said here
# first, where the cause is one `azd env set` away.
ENTRA_TENANT_ID="$(get ENTRA_TENANT_ID)"
ENTRA_TENANT_SUBDOMAIN="$(get ENTRA_TENANT_SUBDOMAIN)"
ENTRA_API_CLIENT_ID="$(get ENTRA_API_CLIENT_ID)"
ENTRA_ALLOWED_CLIENT_IDS="$(get ENTRA_ALLOWED_CLIENT_IDS)"
ENTRA_REQUIRED_SCOPE="$(get ENTRA_REQUIRED_SCOPE)"
ENTRA_ADMIN_ROLE="$(get ENTRA_ADMIN_ROLE)"
IDENTITY_DELETE_MODE="$(get IDENTITY_DELETE_MODE)"
MIN_SUPPORTED_BUILD="$(get MIN_SUPPORTED_BUILD)"
APPLE_SECRET_RENEW_BY="$(get APPLE_SECRET_RENEW_BY)"

GUID='[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
is_guid() { printf '%s\n' "$1" | grep -Eqi "^$GUID\$"; }
# The allowed clients as core/config.py reads them: comma-separated, blanks
# dropped. At least one, and every one a GUID.
clients_ok() {
  ids="$(printf '%s' "$1" | tr ',' '\n' | tr -d ' \t' | grep -v '^$' || true)"
  [ -n "$ids" ] && ! printf '%s\n' "$ids" | grep -Eqiv "^$GUID\$"
}

closed=""
is_guid "$ENTRA_TENANT_ID" || closed="$closed ENTRA_TENANT_ID"
is_guid "$ENTRA_API_CLIENT_ID" || closed="$closed ENTRA_API_CLIENT_ID"
clients_ok "$ENTRA_ALLOWED_CLIENT_IDS" || closed="$closed ENTRA_ALLOWED_CLIENT_IDS"
if [ -n "$closed" ]; then
  echo "postprovision: warning: not set, or not GUIDs:$closed." >&2
  echo "  The API stays closed (503 auth_unavailable) until scripts/entra_setup.py has written them" >&2
  echo "  to the azd env and you provision again." >&2
fi

# --- local.settings.json -------------------------------------------------------
# The timers the app registers, each disabled locally. The same list as
# timerFunctions in infra/modules/appsettings.bicep.
TIMERS="harvest_timer maintenance_timer"

# Written by Python rather than a heredoc: json.dump escapes whatever a value
# holds, and the values reach it through the environment, never a command line.
# The file is created 0600 and renamed over the old one, so it is never
# readable by others and never half-written. The draft is made in .azure/,
# beside azd's own copy of the key: a run killed mid-write then cannot leave
# the key in a file git would pick up (only local.settings.json itself is
# ignored in backend/) or that `azd deploy` would ship (it skips only that
# name).
export OUT TIMERS COSMOS_ENDPOINT COSMOS_DATABASE COSMOS_CONTAINER LEGACY_COSMOS_CONTAINER \
  ENTRA_TENANT_ID ENTRA_TENANT_SUBDOMAIN ENTRA_API_CLIENT_ID ENTRA_ALLOWED_CLIENT_IDS \
  ENTRA_REQUIRED_SCOPE ENTRA_ADMIN_ROLE MYNEWS_HMAC_KEY IDENTITY_DELETE_MODE \
  MIN_SUPPORTED_BUILD APPLE_SECRET_RENEW_BY
python3 - <<'PY'
import json
import os
import tempfile

env = os.environ
values = {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    # Host state and job queues on Azurite.
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "JOBS_STORAGE": "UseDevelopmentStorage=true",
    "JOBS_STORAGE__queueServiceUri": "",
    "JOBS_STORAGE__credential": "managedidentity",
    "COSMOS_ENDPOINT": env["COSMOS_ENDPOINT"],
    "COSMOS_DATABASE": env["COSMOS_DATABASE"],
    "COSMOS_CONTAINER": env["COSMOS_CONTAINER"],
    "LEGACY_COSMOS_CONTAINER": env["LEGACY_COSMOS_CONTAINER"],
    "LOCAL_READONLY": "true",
    # The timers are disabled below, but the host still resolves
    # harvest_timer's %HARVEST_SCHEDULE% when it indexes it. maintenance_timer's
    # schedule is a literal in jobs/timers.py, so it needs no setting.
    "HARVEST_SCHEDULE": "0 */30 * * * *",
}
# Empty stays empty: core/config.py then applies the same defaults the
# deployed app got from main.parameters.json.
for name in (
    "ENTRA_TENANT_ID",
    "ENTRA_TENANT_SUBDOMAIN",
    "ENTRA_API_CLIENT_ID",
    "ENTRA_ALLOWED_CLIENT_IDS",
    "ENTRA_REQUIRED_SCOPE",
    "ENTRA_ADMIN_ROLE",
    "MYNEWS_HMAC_KEY",
    "IDENTITY_DELETE_MODE",
    "MIN_SUPPORTED_BUILD",
    "APPLE_SECRET_RENEW_BY",
):
    values[name] = env.get(name, "")
for timer in env["TIMERS"].split():
    values[f"AzureWebJobs.{timer}.Disabled"] = "true"

out = env["OUT"]
backend = os.path.dirname(out)
drafts = os.path.join(os.path.dirname(backend), ".azure")
# A rename is atomic only within one volume, so .azure/ must share backend/'s.
if not (os.path.isdir(drafts) and os.stat(drafts).st_dev == os.stat(backend).st_dev):
    drafts = backend
fd, tmp = tempfile.mkstemp(dir=drafts, prefix=".local.settings.", suffix=".tmp")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"IsEncrypted": False, "Values": values}, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, out)
except BaseException:
    os.unlink(tmp)
    raise
PY

echo "postprovision: wrote $OUT (Cosmos container $COSMOS_CONTAINER, legacy $LEGACY_COSMOS_CONTAINER; timers off; LOCAL_READONLY=true)"
