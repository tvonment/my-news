targetScope = 'subscription'

// ---------------------------------------------------------------------------
// MyNews — personal AI news aggregator
//
// Measured cost: ~11 CHF/month (September 2026: 11.08 CHF; the lines are in
// docs/cost.md), against a 25 CHF budget, on a Visual Studio credit
// subscription that gets no free grants. Budget amounts are in CHF, the billing
// currency; list prices quoted in dollars say so.
//
// Cost-critical invariants. Changing any of these turns a ~11 CHF/month
// project into a ~100+ CHF/month one:
//   * Cosmos: enableFreeTier MUST be true and MUST be set at creation — it
//     cannot be enabled later, and there is exactly ONE free-tier account per
//     subscription. Free tier requires PROVISIONED throughput (not serverless)
//     and total throughput across all containers must stay <= 1000 RU/s:
//     Phase 1 runs `news` at 600 beside `items` at 400, and data.bicep fails
//     the deployment if the two add up to more. Only prod can hold the grant;
//     a second environment (mynews-dev) runs serverless (COSMOS_MODE).
//   * Functions: Flex Consumption, 512 MB, and NO always-ready instances.
//     Always-ready has no free grant: 1x512MB = $5.26/mo, 1x2048MB = $21.02/mo.
//   * Foundry deployments (none since Phase 0; Data Zone Standard from Phase 2,
//     see ai.bicep): pay-per-token SKUs only. 'ProvisionedManaged' is
//     ~$1,460/month per PTU with a ~15 PTU minimum.
// ---------------------------------------------------------------------------

@minLength(1)
@maxLength(64)
@description('Name of the azd environment; used to derive resource names.')
param environmentName string

@description('Primary location for all resources. Switzerland North keeps latency low and data in Switzerland, and supports Flex Consumption, Cosmos and Log Analytics.')
@allowed([
  'switzerlandnorth'
  'westeurope'
  'swedencentral'
  'northeurope'
  'eastus'
  'eastus2'
])
param location string = 'switzerlandnorth'

@description('Email address that receives budget and alert notifications.')
param alertEmail string

@description('Monthly budget for this resource group, in CHF: a budget has no currency of its own and always counts in the billing currency. Alerts at Actual 60 % and 90 % and Forecast 100 % (see budget.bicep).')
@minValue(5)
param monthlyBudget int = 25

@description('First day of the resource-group budget\'s first month, YYYY-MM-DD, from BUDGET_START_DATE. It must equal the live budget\'s (2026-07-01). Never derive it from utcNow(): that moves the start date every month.')
param budgetStartDate string

@description('Monthly amount in CHF for the subscription-wide guard on the shared Visual Studio credit (USD 150, ≈ CHF 130; match it to the credit the portal shows). Alerts at Actual 80 % and Forecast 100 %. 0 skips the guard, e.g. on a subscription with no credit cap.')
@minValue(0)
param subscriptionGuardBudget int = 130

@description('First day of the guard budget\'s first month, YYYY-MM-DD, from GUARD_BUDGET_START_DATE. The provision that creates the guard needs the first of the CURRENT month (a past start date is accepted only within the current period); after that it stays fixed, for the same reason as budgetStartDate.')
param guardBudgetStartDate string

@description('Cosmos backup mode (see data.bicep). Continuous7Days is free, and the switch from Periodic is one-way.')
@allowed([
  'Continuous7Days'
  'Periodic'
])
param cosmosBackup string = 'Continuous7Days'

@description('Ceiling on concurrent Function instances, the runaway-cost brake for everything that is not a model call (see functions.bicep).')
@minValue(1)
param maximumInstanceCount int = 5

@description('Object ID of the developer principal, so you can read Cosmos data locally. azd populates this.')
param principalId string = ''

@description('Cosmos account type, from COSMOS_MODE (see data.bicep): freeTier for prod, serverless for mynews-dev, since the subscription has one free-tier grant and prod holds it. Decided when the account is created.')
@allowed([
  'freeTier'
  'serverless'
])
param cosmosMode string = 'freeTier'

@description('Timer functions to switch off, from DISABLED_TIMERS: a comma list of names, or all. Empty, as in prod, runs every timer; mynews-dev sets all (see appsettings.bicep).')
param disabledTimers string = ''

// --- Identity and access (docs/api.md, section 2.1) --------------------------
// All of these come from the azd env, where scripts/entra_setup.py writes the
// ids. Every default is what core/config.py falls back to, and an empty id or
// key leaves the API closed (503 auth_unavailable on every route but /health)
// instead of open: an environment without its tenant still provisions, and
// still refuses every token.

@description('ENTRA_TENANT_ID: directory id of the External ID tenant ("von Mentlen News"). Empty until the tenant exists.')
param entraTenantId string = ''

@description('ENTRA_TENANT_SUBDOMAIN: the tenant\'s initial domain label, vonmentlennews, so the sign-in host is vonmentlennews.ciamlogin.com.')
param entraTenantSubdomain string = ''

@description('ENTRA_API_CLIENT_ID: application id of mynews-api, the one accepted token audience.')
param entraApiClientId string = ''

@description('ENTRA_ALLOWED_CLIENT_IDS: comma list of the client application ids (mynews-ios, mynews-cli) a token may be issued to.')
param entraAllowedClientIds string = ''

@description('ENTRA_REQUIRED_SCOPE: the delegated scope every token must carry.')
param entraRequiredScope string = 'access_as_user'

@description('ENTRA_ADMIN_ROLE: the app role that opens the admin routes.')
param entraAdminRole string = 'admin'

@secure()
@description('MYNEWS_HMAC_KEY: the key behind every uid. Generated once, kept in the password manager and never regenerated, because a new key orphans every stored uid; postprovision.sh checks its fingerprint. Secure, so it stays out of deployment history.')
param hmacKey string = ''

@description('IDENTITY_DELETE_MODE: manual (the owner clears the admin list of pending identity removals) or graph (Later, if the spike proves app-only Graph deletion).')
@allowed([
  'manual'
  'graph'
])
param identityDeleteMode string = 'manual'

@description('MIN_SUPPORTED_BUILD: the oldest iOS build the API still serves; 0 serves every build.')
@minValue(0)
param minSupportedBuild int = 0

// A date, not a secret: named so the linter's secret-by-name check stays quiet.
@description('APPLE_SECRET_RENEW_BY: YYYY-MM-DD by which the Sign in with Apple client secret must be renewed, or empty. The maintenance timer warns from 30 days before it.')
param appleRenewBy string = ''

var resourceToken = uniqueString(subscription().id, environmentName, location)
var tags = {
  'azd-env-name': environmentName
  project: 'mynews'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-mynews-${environmentName}'
  location: location
  tags: tags
}

module monitoring './modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
  }
}

module data './modules/data.bicep' = {
  name: 'data'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    cosmosBackup: cosmosBackup
    cosmosMode: cosmosMode
  }
}

module ai './modules/ai.bicep' = {
  name: 'ai'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
  }
}

module functions './modules/functions.bicep' = {
  name: 'functions'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    storageAccountName: data.outputs.storageAccountName
    deploymentContainerName: data.outputs.deploymentContainerName
    cosmosAccountName: data.outputs.cosmosAccountName
    foundryAccountName: ai.outputs.foundryAccountName
    developerPrincipalId: principalId
    maximumInstanceCount: maximumInstanceCount
  }
}

// Every app setting, in one sites/config resource that replaces the whole
// collection on each provision (see appsettings.bicep for why it is the single
// manifest). It reads the functions and data modules' outputs, so it runs
// after both have finished: the site, its role assignments, the containers and
// the queues exist before any setting points at them.
module appSettings './modules/appsettings.bicep' = {
  name: 'appsettings'
  scope: rg
  params: {
    functionAppName: functions.outputs.functionAppName
    storageAccountName: data.outputs.storageAccountName
    queueServiceUri: data.outputs.queueServiceUri
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    cosmosEndpoint: data.outputs.cosmosEndpoint
    cosmosDatabaseName: data.outputs.cosmosDatabaseName
    cosmosContainerName: data.outputs.cosmosContainerName
    legacyCosmosContainerName: data.outputs.legacyCosmosContainerName
    entraTenantId: entraTenantId
    entraTenantSubdomain: entraTenantSubdomain
    entraApiClientId: entraApiClientId
    entraAllowedClientIds: entraAllowedClientIds
    entraRequiredScope: entraRequiredScope
    entraAdminRole: entraAdminRole
    hmacKey: hmacKey
    identityDeleteMode: identityDeleteMode
    minSupportedBuild: minSupportedBuild
    appleRenewBy: appleRenewBy
    disabledTimers: disabledTimers
  }
}

module alerts './modules/alerts.bicep' = {
  name: 'alerts'
  scope: rg
  params: {
    location: location
    tags: tags
    alertEmail: alertEmail
    cosmosAccountName: data.outputs.cosmosAccountName
    appInsightsId: monitoring.outputs.appInsightsId
  }
}

// After alerts, because every notification also goes to its action group.
module budget './modules/budget.bicep' = {
  name: 'budget'
  scope: rg
  params: {
    budgetName: 'mynews-${environmentName}'
    amount: monthlyBudget
    startDate: budgetStartDate
    alertEmail: alertEmail
    actionGroupId: alerts.outputs.actionGroupId
  }
}

// ---------------------------------------------------------------------------
// Subscription guard: a budget on the whole subscription, not just MyNews.
//
// The Visual Studio Enterprise credit behind this subscription is shared with
// the other projects on it, and it is a hard stop: the spending limit is on, so
// when the credit runs out Azure suspends the subscription, MyNews and every
// neighbour with it, until the credit renews. The resource-group budget cannot
// see that coming because it counts only MyNews: a runaway next door would take
// this app down without it ever firing. This one counts everything.
//   Actual 80 %   : a fifth of the credit left, time to find the spender.
//   Forecast 100 %: on course to hit the cap before the month is out.
// Email only: the action group belongs to MyNews' resource group, and what
// trips this guard is mostly other projects' spend.
//
// It is approximate at the edges: the credit renews on the 21st (this
// subscription's billing periods run from the 21st to the 20th), while a
// Monthly budget counts calendar months, so it under-counts a credit period
// whose spend falls on both sides of the 1st. The forecast narrows that gap.
// timeGrain 'BillingMonth' would line the two up, but is untested here.
//
// It lives here because a subscription-scope budget needs a subscription-scope
// deployment, which this file already is.
// ---------------------------------------------------------------------------
resource creditGuard 'Microsoft.Consumption/budgets@2023-05-01' = if (subscriptionGuardBudget > 0) {
  name: 'mynews-credit-guard'
  properties: {
    category: 'Cost'
    amount: subscriptionGuardBudget
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: guardBudgetStartDate
    }
    notifications: {
      Actual80Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [
          alertEmail
        ]
      }
      Forecast100Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [
          alertEmail
        ]
      }
    }
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output SERVICE_BACKEND_NAME string = functions.outputs.functionAppName
output SERVICE_BACKEND_URI string = functions.outputs.functionAppUri
output COSMOS_ENDPOINT string = data.outputs.cosmosEndpoint
output COSMOS_DATABASE string = data.outputs.cosmosDatabaseName
// `news` from Phase 1 on, so anything that takes its container from the azd
// env now gets `news`. backend/scripts/export_all.py defaults --container to
// COSMOS_CONTAINER: exporting v1's data from such a shell needs
// --container items (LEGACY_COSMOS_CONTAINER).
output COSMOS_CONTAINER string = data.outputs.cosmosContainerName
output LEGACY_COSMOS_CONTAINER string = data.outputs.legacyCosmosContainerName
output FOUNDRY_ENDPOINT string = ai.outputs.foundryEndpoint

// The identity settings as deployed, defaults included, for postprovision.sh
// and anything else that reads the azd env. They are ids, not secrets; the
// HMAC key is deliberately not among them.
output ENTRA_TENANT_ID string = entraTenantId
output ENTRA_TENANT_SUBDOMAIN string = entraTenantSubdomain
output ENTRA_API_CLIENT_ID string = entraApiClientId
output ENTRA_ALLOWED_CLIENT_IDS string = entraAllowedClientIds
output ENTRA_REQUIRED_SCOPE string = entraRequiredScope
output ENTRA_ADMIN_ROLE string = entraAdminRole
