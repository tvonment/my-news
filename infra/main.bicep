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
//     and total throughput across all containers must stay <= 1000 RU/s.
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
    cosmosDatabaseName: data.outputs.cosmosDatabaseName
    cosmosContainerName: data.outputs.cosmosContainerName
    foundryAccountName: ai.outputs.foundryAccountName
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    developerPrincipalId: principalId
    maximumInstanceCount: maximumInstanceCount
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
output COSMOS_CONTAINER string = data.outputs.cosmosContainerName
output FOUNDRY_ENDPOINT string = ai.outputs.foundryEndpoint
