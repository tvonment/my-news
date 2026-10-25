param location string
param tags object
param resourceToken string

param storageAccountName string
param deploymentContainerName string
param cosmosAccountName string
@description('Foundry account the Function identity may call. Nothing calls a model between Phase 0 and Phase 2; the grant stays so Phase 2 needs no new one.')
param foundryAccountName string

@description('Developer object ID, granted Cosmos data-plane read/write so you can inspect data locally. Empty skips the assignment.')
param developerPrincipalId string = ''

@description('Flex instance memory. 512 MB is deliberate: Flex bills provisioned memory for the WHOLE execution including time blocked on network calls (feed fetches now, model calls again from Phase 2), and this pipeline is ~95% network wait. 2048 MB would cost 4x for identical wall-clock.')
@allowed([512, 2048, 4096])
param instanceMemoryMB int = 512

@description('Ceiling on concurrent instances: the runaway-cost brake for everything that is not a model call. This Visual Studio credit subscription gets no free grant, so every execution bills, including a request the app rejects (from Phase 1 a bad token gets its 401 from inside the function, which is still an execution). A flood therefore costs in proportion to how far the app can scale out: at 512 MB, 5 instances flat out are 216,000 GB-s a day, a quarter of what the old cap of 20 allowed. Phase 3\'s load check settles the final value, the smallest one that passes.')
@minValue(1)
param maximumInstanceCount int = 5

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
    // No appSettings here: every setting lives in appsettings.bicep, a
    // sites/config resource that main.bicep deploys after this module. Two
    // writers of one collection would each replace what the other wrote, so
    // the list moved out of the site whole. A site PUT without the array
    // leaves the settings as they are.
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
    }
  }
}

// --- Role assignments for the Function's managed identity ------------------

var storageBlobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageQueueDataContributor = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
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

// No Monitoring Metrics Publisher. It used to be assigned here on the STORAGE
// account, where it grants nothing this app uses: its data actions
// (Metrics/Write, Telemetry/Write) let a caller publish against the resource it
// is assigned on, and nothing publishes against storage. Telemetry does not go
// through Entra at all: the SDK sends with the instrumentation key inside
// APPLICATIONINSIGHTS_CONNECTION_STRING. If ingestion ever moves to Entra
// (APPLICATIONINSIGHTS_AUTHENTICATION_STRING=Authorization=AAD), grant the role
// on the App Insights component, not here.
//
// Removing it from this file does not delete the live assignment, because
// `azd provision` is incremental. It is deleted by hand:
//   az role assignment delete --scope <storage id> \
//     --role 'Monitoring Metrics Publisher' --assignee <function principalId>

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
