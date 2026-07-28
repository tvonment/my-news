targetScope = 'subscription'

// ---------------------------------------------------------------------------
// MyNews — personal AI news aggregator
//
// Cost-critical invariants (see plan §4, §5, §11). Changing any of these turns
// a ~$8/month project into a ~$100+/month one:
//   * Cosmos: enableFreeTier MUST be true and MUST be set at creation — it
//     cannot be enabled later, and there is exactly ONE free-tier account per
//     subscription. Free tier requires PROVISIONED throughput (not serverless)
//     and total throughput across all containers must stay <= 1000 RU/s.
//   * Functions: Flex Consumption, 512 MB, and NO always-ready instances.
//     Always-ready has no free grant: 1x512MB = $5.26/mo, 1x2048MB = $21.02/mo.
//   * Foundry deployments: sku.name MUST be 'GlobalStandard'. 'ProvisionedManaged'
//     is ~$1,460/month per PTU with a ~15 PTU minimum.
// ---------------------------------------------------------------------------

@minLength(1)
@maxLength(64)
@description('Name of the azd environment; used to derive resource names.')
param environmentName string

@description('Primary location for all resources. West Europe keeps latency low and data in the EU.')
@allowed([
  'westeurope'
  'swedencentral'
  'northeurope'
  'eastus'
  'eastus2'
])
param location string = 'westeurope'

@description('Email address that receives budget alerts.')
param alertEmail string

@description('Monthly budget ceiling in USD. Alerts fire at 40% and 100% of this.')
@minValue(5)
param monthlyBudgetUsd int = 25

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
    foundryEndpoint: ai.outputs.foundryEndpoint
    scorerDeployment: ai.outputs.scorerDeployment
    summarizerDeployment: ai.outputs.summarizerDeployment
    embeddingDeployment: ai.outputs.embeddingDeployment
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    developerPrincipalId: principalId
  }
}

module budget './modules/budget.bicep' = {
  name: 'budget'
  scope: rg
  params: {
    budgetName: 'mynews-${environmentName}'
    amountUsd: monthlyBudgetUsd
    alertEmail: alertEmail
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
output MODEL_SCORER string = ai.outputs.scorerDeployment
output MODEL_SUMMARIZER string = ai.outputs.summarizerDeployment
output MODEL_EMBEDDING string = ai.outputs.embeddingDeployment
