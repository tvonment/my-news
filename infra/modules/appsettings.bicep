// ---------------------------------------------------------------------------
// The Function App's settings: the ONE manifest of every setting it has.
//
// A Microsoft.Web/sites/config resource named 'appsettings' REPLACES the whole
// collection on every provision; ARM has no patch for it. A setting missing
// from this file is deleted from the live app, and one added by hand in the
// portal is wiped by the next provision. So every setting lives here and
// nowhere else (functions.bicep's site carries none: two writers of one
// collection would each replace the other's), and
// backend/tests/test_settings_manifest.py checks that every environment
// variable the code reads, and every setting the host needs, is named here and
// in scripts/postprovision.sh, which writes the local copy. core/config.py is
// the source of truth for names and defaults; the defaults below mirror it.
//
// The settings the host reads, not our code, are the dangerous ones to lose,
// because losing them fails nothing at deploy time:
//   AzureWebJobsStorage__accountName       the host's storage: timers stop
//   APPLICATIONINSIGHTS_CONNECTION_STRING  telemetry, and so every alert
//   JOBS_STORAGE__queueServiceUri/...      the queue bindings: purges stop
//   AzureWebJobs.<function>.Disabled       a timer comes back where it was off
//
// Phase 0 removed the settings only v1's digest and profile runs read:
// FOUNDRY_ENDPOINT, MODEL_*, REASONING_EFFORT_*, DIGEST_SCHEDULE,
// PROFILE_SCHEDULE, DAILY_TOKEN_BUDGET_USD and PUSH_ENABLED. core/config.py
// keeps a default for each, so code that still reads them behaves as before.
// The tuning knobs the code reads with a default of its own were never set
// here, because the default is the value: ARTICLE_TTL_DAYS, CLUSTER_THRESHOLD,
// EMBEDDING_DIMENSIONS, EXPLORATION_QUOTA, MAX_ARTICLE_AGE_DAYS,
// MAX_SCORED_PER_RUN, SUMMARIZE_THRESHOLD, USER_AGENT and FOUNDRY_TOKEN_SCOPE.
// Setting one means adding it here, or the next provision removes it again.
//
// Order: main.bicep deploys this after the functions and data modules (it
// reads their outputs), so the site, its role assignments, both containers and
// the job queues all exist before a setting points the app at them. For the
// same reason the two checks below (DISABLED_TIMERS, APPLE_SECRET_RENEW_BY)
// run only when this module deploys, after those: a bad value fails the
// provision before the collection is replaced, so the app keeps the settings
// it had, and the next provision finishes the job.
//
// When Key Vault references arrive (keyvault.bicep, graph-mode deletion
// only), this module must also depend on the vault's Secrets User assignment.
// Without the role a reference stays unresolved, and the code then counts that
// setting as missing (core/config.py, KEYVAULT_REF_PREFIX): auth fails closed.
// ---------------------------------------------------------------------------

@description('The Function App whose settings this replaces.')
param functionAppName string

@description('The host\'s storage account, reached with the app\'s managed identity.')
param storageAccountName string

@description('Queue endpoint of the same account (https://<account>.queue.core.windows.net/), for the JOBS_STORAGE connection.')
param queueServiceUri string

param appInsightsConnectionString string
param cosmosEndpoint string
param cosmosDatabaseName string

@description('Container that all v2 code uses (news).')
param cosmosContainerName string

@description('v1\'s container (items): passed explicitly by the v1 harvest only, until Phase 2 removes it.')
param legacyCosmosContainerName string

@description('Harvest cadence. Frequent and cheap (no LLM). Needed because The Verge, NPR and the Microsoft blogs only carry 10 feed items — a 3x/day crawl would silently drop stories.')
param harvestSchedule string = '0 */30 * * * *'

// No MAINTENANCE_SCHEDULE: maintenance_timer's schedule is a literal in
// jobs/timers.py (daily 02:15 UTC), so a setting of that name would change
// nothing, and the manifest test refuses a setting the code does not read. An
// environment that must not run it switches it off with DISABLED_TIMERS.

// Identity (docs/api.md, section 2.1). Until the External ID tenant and its
// app registrations exist, these are empty, and core/auth.py answers every
// route except /health with 503 auth_unavailable: closed by construction,
// never open. scripts/entra_setup.py writes the values into the azd env.
@description('Directory (tenant) id of the External ID tenant. Fixes the exact issuer https://<tid>.ciamlogin.com/<tid>/v2.0 and the JWKS URL.')
param entraTenantId string = ''

@description('The tenant\'s initial domain label (vonmentlennews): the sign-in host <subdomain>.ciamlogin.com that clients use as their authority.')
param entraTenantSubdomain string = ''

@description('Application id of mynews-api: the only accepted aud.')
param entraApiClientId string = ''

@description('Comma list of the client application ids (iOS, CLI) a token\'s azp must be one of.')
param entraAllowedClientIds string = ''

@description('Delegated scope every token must carry in scp.')
param entraRequiredScope string = 'access_as_user'

@description('App role that opens the admin routes.')
param entraAdminRole string = 'admin'

// Every uid is HMAC-SHA256(MYNEWS_HMAC_KEY, oid): the registry, tombstones,
// deletion records, audit entries and invite redemptions all store one. A new
// key therefore orphans all of them, so it is generated once, kept in the
// password manager and never regenerated; postprovision.sh checks its
// fingerprint against the one recorded in the azd env. Empty fails closed.
@secure()
@description('MYNEWS_HMAC_KEY. Secure: kept out of deployment history and logs.')
param hmacKey string = ''

@description('How an Entra user is removed after a purge: manual (the owner works through the admin list) or graph (Later, only if the spike proves app-only Graph deletion).')
@allowed([
  'manual'
  'graph'
])
param identityDeleteMode string = 'manual'

@description('Oldest iOS build (CFBundleVersion) the API still serves; GET /health reports it as minBuild. 0 serves every build.')
@minValue(0)
param minSupportedBuild int = 0

// A date, not a secret: named so the linter's secret-by-name check stays quiet.
@description('APPLE_SECRET_RENEW_BY: YYYY-MM-DD by which the Sign in with Apple client secret must be renewed (it lasts at most 6 months), or empty.')
param appleRenewBy string = ''

@description('DISABLED_TIMERS: timers to switch off in this environment, as a comma list of function names, or all. Empty (prod) runs every timer. mynews-dev sets all, so it harvests (and from Phase 2 cycles) only on demand and never competes with prod for Foundry capacity.')
param disabledTimers string = ''

// Every timer the app registers (jobs/timers.py), each with an explicit
// AzureWebJobs.<function>.Disabled. 'false' is the same as no setting, but a
// switch that is always present only ever changes its value, so turning a
// timer back on never removes a setting. A timer added in code goes into this
// list too; the manifest test holds it to the code's registered timers.
var timerFunctions = [
  'harvest_timer'
  'maintenance_timer'
]
// DISABLED_TIMERS lower-cased, split on commas, blanks dropped, so
// "harvest_timer, maintenance_timer" and "ALL" both work. A name that is not a
// timer fails the provision: a typo must not leave the timer running while the
// environment looks configured.
var disabledTimerNames = filter(map(split(toLower(disabledTimers), ','), name => trim(name)), name => !empty(name))
var unknownTimers = filter(disabledTimerNames, name => name != 'all' && !contains(timerFunctions, name))
var disableAllTimers = contains(disabledTimerNames, 'all')
var timerSwitches = empty(unknownTimers)
  ? toObject(
      timerFunctions,
      name => 'AzureWebJobs.${name}.Disabled',
      name => (disableAllTimers || contains(disabledTimerNames, name)) ? 'true' : 'false'
    )
  : fail('DISABLED_TIMERS names no timer called ${join(unknownTimers, ', ')}. Timers: ${join(timerFunctions, ', ')}, or all.')

// The maintenance timer warns about the Apple secret from 30 days before this
// date, but only if the date parses: the code treats a malformed value as unset
// (a warning in the log), which would silence the one reminder that sign-in
// with Apple is about to break. So anything that is not exactly YYYY-MM-DD
// stops the provision instead. dateTimeAdd() refuses what is no date at all,
// and the round trip catches what parses but is written another way (2027-4-1).
var appleRenewByChecked = empty(appleRenewBy)
  ? ''
  : (dateTimeAdd('${appleRenewBy}T00:00:00Z', 'PT0S', 'yyyy-MM-dd') == appleRenewBy
      ? appleRenewBy
      : fail('APPLE_SECRET_RENEW_BY must be a date written YYYY-MM-DD, or empty; it is "${appleRenewBy}".'))

var settings = union(
  {
    // --- Host --------------------------------------------------------------
    // Identity-based: the system-assigned identity holds Storage Blob Data
    // Owner and Queue Data Contributor (functions.bicep), and the account
    // refuses shared keys.
    AzureWebJobsStorage__accountName: storageAccountName
    APPLICATIONINSIGHTS_CONNECTION_STRING: appInsightsConnectionString

    // --- Job queues (data.bicep): account-purge, suggestions, ops-jobs -----
    // The connection the queue bindings name (connection="JOBS_STORAGE"). Its
    // own connection rather than AzureWebJobsStorage, so local runs can point
    // the jobs at Azurite (JOBS_STORAGE=UseDevelopmentStorage=true) while
    // everything else stays as it is. /admin/health peeks the queues through
    // the same URI.
    JOBS_STORAGE__queueServiceUri: queueServiceUri
    JOBS_STORAGE__credential: 'managedidentity'

    // --- Cosmos ------------------------------------------------------------
    // COSMOS_CONTAINER turns from `items` to `news` with the first Phase 1
    // provision. Phase 0 code reads COSMOS_CONTAINER for everything, so a
    // Phase 0 harvest running on these settings would write v1 documents into
    // `news`. The cutover (docs/runbooks/phase1-rollout.md) therefore
    // provisions with DISABLED_TIMERS=harvest_timer, deploys, and provisions
    // again with it cleared: no Phase 0 harvest ever runs on these settings,
    // and the Phase 1 code never runs without them. (Deploying the Phase 1
    // code first would be safe too, since its v1 harvest passes
    // LEGACY_COSMOS_CONTAINER, default `items`, on every call and every route
    // but /health fails closed until ENTRA_* arrive; the runbook's order
    // also keeps the job functions from starting without JOBS_STORAGE.)
    COSMOS_ENDPOINT: cosmosEndpoint
    COSMOS_DATABASE: cosmosDatabaseName
    COSMOS_CONTAINER: cosmosContainerName
    LEGACY_COSMOS_CONTAINER: legacyCosmosContainerName
    // The guard against local runs writing to production. Off here: this is
    // the deployed app. postprovision.sh turns it on in local.settings.json.
    LOCAL_READONLY: 'false'

    // --- Schedules (UTC) ---------------------------------------------------
    // harvest_timer binds %HARVEST_SCHEDULE%; maintenance_timer's is fixed.
    HARVEST_SCHEDULE: harvestSchedule

    // --- Identity and access -----------------------------------------------
    ENTRA_TENANT_ID: entraTenantId
    ENTRA_TENANT_SUBDOMAIN: entraTenantSubdomain
    ENTRA_API_CLIENT_ID: entraApiClientId
    ENTRA_ALLOWED_CLIENT_IDS: entraAllowedClientIds
    ENTRA_REQUIRED_SCOPE: entraRequiredScope
    ENTRA_ADMIN_ROLE: entraAdminRole
    MYNEWS_HMAC_KEY: hmacKey
    IDENTITY_DELETE_MODE: identityDeleteMode

    // --- Clients and operator reminders ------------------------------------
    MIN_SUPPORTED_BUILD: string(minSupportedBuild)
    APPLE_SECRET_RENEW_BY: appleRenewByChecked
  },
  timerSwitches
)

resource site 'Microsoft.Web/sites@2023-12-01' existing = {
  name: functionAppName
}

resource appSettings 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: site
  name: 'appsettings'
  properties: settings
}

// Names only, never values. `az deployment group show -g <rg> -n appsettings
// --query properties.outputs.settingNames` then shows what a provision
// deployed without the appsettings list call, which returns every value.
output settingNames array = objectKeys(settings)
