param location string
param tags object
param resourceToken string

param storageAccountName string
param deploymentContainerName string
param cosmosAccountName string
param cosmosDatabaseName string
param cosmosContainerName string
param foundryAccountName string
param foundryEndpoint string
param scorerDeployment string
param summarizerDeployment string
param embeddingDeployment string
param scorerModelId string
param summarizerModelId string
param embeddingModelId string
param appInsightsConnectionString string

@description('Developer object ID, granted Cosmos data-plane read/write so you can inspect data locally. Empty skips the assignment.')
param developerPrincipalId string = ''

@description('Flex instance memory. 512 MB is deliberate: Flex bills provisioned memory for the WHOLE execution including time blocked on LLM HTTP calls, and this pipeline is ~95% network wait. 2048 MB would cost 4x for identical wall-clock.')
@allowed([512, 2048, 4096])
param instanceMemoryMB int = 512

@description('Ceiling on concurrent instances. Low by design — this is a single-user app and the cap is a runaway-cost brake.')
param maximumInstanceCount int = 20

@description('Harvest cadence. Frequent and cheap (no LLM). Needed because The Verge, NPR and the Microsoft blogs only carry 10 feed items — a 3x/day crawl would silently drop stories.')
param harvestSchedule string = '0 */30 * * * *'

@description('Digest cadence, in UTC. Flex does not support WEBSITE_TIME_ZONE/TZ, so this cannot follow local time — these are 07:00/12:00/19:00 Vienna in summer, one hour earlier in winter.')
param digestSchedule string = '0 0 5,10,17 * * *'

@description('Abort LLM stages once the day\'s estimated spend crosses this. Azure OpenAI has NO hard spend limit, so this in-code brake and the budget alert are the only protection.')
param dailyTokenBudgetUsd string = '1.50'

var functionAppName = 'func-mynews-${resourceToken}'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  name: cosmosAccountName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'plan-mynews-${resourceToken}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'backend' })
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        instanceMemoryMB: instanceMemoryMB
        maximumInstanceCount: maximumInstanceCount
        // `alwaysReady` is deliberately absent. Always-ready instances have NO
        // free grant — one 512 MB instance is $5.26/month, which would be the
        // single largest Azure line item in this project.
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        {
          name: 'AzureWebJobsStorage__accountName'
          value: storage.name
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsightsConnectionString
        }
        {
          name: 'COSMOS_ENDPOINT'
          value: cosmos.properties.documentEndpoint
        }
        {
          name: 'COSMOS_DATABASE'
          value: cosmosDatabaseName
        }
        {
          name: 'COSMOS_CONTAINER'
          value: cosmosContainerName
        }
        {
          name: 'FOUNDRY_ENDPOINT'
          value: foundryEndpoint
        }
        {
          name: 'MODEL_SCORER'
          value: scorerDeployment
        }
        {
          name: 'MODEL_SUMMARIZER'
          value: summarizerDeployment
        }
        {
          name: 'MODEL_EMBEDDING'
          value: embeddingDeployment
        }
        // Underlying model ids drive token pricing; the deployment names above
        // are just labels and carry no cost information.
        {
          name: 'MODEL_SCORER_ID'
          value: scorerModelId
        }
        {
          name: 'MODEL_SUMMARIZER_ID'
          value: summarizerModelId
        }
        {
          name: 'MODEL_EMBEDDING_ID'
          value: embeddingModelId
        }
        // Reasoning effort is per-model, not global: 'minimal' works only on the
        // original GPT-5 family (gpt-5-nano), while 'none' requires gpt-5.1 or
        // later (gpt-5.4). Sending the wrong one is rejected by the API.
        {
          name: 'REASONING_EFFORT_SCORER'
          value: 'minimal'
        }
        {
          name: 'REASONING_EFFORT_SUMMARIZER'
          value: 'none'
        }
        {
          name: 'HARVEST_SCHEDULE'
          value: harvestSchedule
        }
        {
          name: 'DIGEST_SCHEDULE'
          value: digestSchedule
        }
        {
          name: 'DAILY_TOKEN_BUDGET_USD'
          value: dailyTokenBudgetUsd
        }
        {
          name: 'PUSH_ENABLED'
          value: 'false'
        }
      ]
    }
  }
}

// --- Role assignments for the Function's managed identity ------------------

var storageBlobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageQueueDataContributor = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var monitoringMetricsPublisher = '3913510d-42f4-4e42-8a64-420c390055eb'
var cognitiveServicesOpenAIUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource blobOwnerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, functionApp.id, storageBlobDataOwner)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwner)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource queueContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, functionApp.id, storageQueueDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageQueueDataContributor)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource metricsPublisherAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, functionApp.id, monitoringMetricsPublisher)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringMetricsPublisher)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource openAiUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, functionApp.id, cognitiveServicesOpenAIUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUser)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Cosmos data-plane access is a SQL role assignment, not an Azure RBAC one.
// 00000000-...-0002 is the built-in "Cosmos DB Built-in Data Contributor".
var cosmosDataContributorId = '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'

resource cosmosFunctionRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, functionApp.id, 'data-contributor')
  properties: {
    roleDefinitionId: cosmosDataContributorId
    principalId: functionApp.identity.principalId
    scope: cosmos.id
  }
}

resource cosmosDeveloperRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = if (!empty(developerPrincipalId)) {
  parent: cosmos
  name: guid(cosmos.id, developerPrincipalId, 'data-contributor')
  properties: {
    roleDefinitionId: cosmosDataContributorId
    principalId: developerPrincipalId
    scope: cosmos.id
  }
}

output functionAppName string = functionApp.name
output functionAppUri string = 'https://${functionApp.properties.defaultHostName}'
output functionAppPrincipalId string = functionApp.identity.principalId
