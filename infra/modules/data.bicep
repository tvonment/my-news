param location string
param tags object
param resourceToken string

@description('Total provisioned throughput. MUST stay <= 1000 to remain inside the Cosmos free-tier grant. A second container at 400 RU/s would cost ~$23.36/month.')
@minValue(400)
@maxValue(1000)
param containerThroughput int = 1000

var cosmosAccountName = 'cosmos-mynews-${resourceToken}'
var databaseName = 'mynews'
var containerName = 'items'
var deploymentContainerName = 'app-package'

// ---------------------------------------------------------------------------
// Cosmos DB for NoSQL — free tier, PROVISIONED throughput.
//
// `enableFreeTier` can ONLY be set at account creation and there is exactly one
// free-tier account per subscription. It is also mutually exclusive with
// serverless ("Free tier is currently not available for serverless accounts"),
// which is why this is a provisioned account despite the low traffic.
//
// `EnableNoSQLVectorSearch` is kept even though the container no longer carries
// a vector policy (see the container resource below): account capabilities are
// effectively additive, and attempting to remove one risks an ARM rejection for
// no benefit. It costs nothing while unused.
// ---------------------------------------------------------------------------
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: cosmosAccountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    enableFreeTier: true
    // Data-plane access is RBAC-only: the Function's managed identity and the
    // developer principal get a SQL role assignment, so there is no key to store.
    disableLocalAuth: true
    minimalTlsVersion: 'Tls12'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: [
      {
        name: 'EnableNoSQLVectorSearch'
      }
    ]
    backupPolicy: {
      type: 'Periodic'
      periodicModeProperties: {
        // First 2 copies are free.
        backupIntervalInMinutes: 1440
        backupRetentionIntervalInHours: 48
        backupStorageRedundancy: 'Local'
      }
    }
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
    // No throughput here on purpose: database-level (shared) throughput would be
    // cheaper per container, but "vector indexing and search aren't supported on
    // accounts with Shared Throughput". Throughput goes on the container instead.
  }
}

resource container 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: containerName
  properties: {
    options: {
      throughput: containerThroughput
    }
    resource: {
      id: containerName
      partitionKey: {
        paths: [
          '/pk'
        ]
        kind: 'Hash'
        version: 2
      }
      // -1 = TTL enabled with no default expiry. Per-item `ttl` governs; an item
      // with ttl = -1 never expires (that's how liked articles are kept forever).
      defaultTtl: -1
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          {
            path: '/*'
          }
        ]
        excludedPaths: [
          {
            path: '/_etag/?'
          }
          {
            // Keep the ~1 KB vector out of the regular index — it would be
            // duplicated per document for a lookup we never perform. Still
            // correct, and now load-bearing on its own: with the vector policy
            // gone this is the only thing keeping /embedding out of the index.
            path: '/embedding/*'
          }
        ]
      }
      // NO vectorEmbeddingPolicy / vectorIndexes, deliberately.
      //
      // This container originally declared `dataType: 'float16'` on /embedding.
      // ARM accepted it and Microsoft's docs list float16 as supported — but on
      // 2026-08-02 the Cosmos *data plane* began failing to deserialize its own
      // stored policy, returning InternalServerError for every query, every
      // change-feed read and every ReadFeed on the container. Point reads and
      // writes kept working, so the pipeline half-ran for two days while the
      // app 500'd. The policy was dropped in place to recover.
      //
      // It is not re-added as float32 because nothing ever used it: no code path
      // issues a VectorDistance query. All similarity is NumPy over embeddings
      // read out of Cosmos (pipeline/gate.py, pipeline/cluster.py). The policy
      // was pure cost and, in the end, the sole cause of an outage.
      //
      // Re-adding one means re-adding a vector index too, and the embedding
      // dimensions would have to match core/config.py's EMBEDDING_DIMENSIONS.
    }
  }
}

// ---------------------------------------------------------------------------
// Storage — required by the Functions host. Standard LRS: GRS would double the
// per-operation price for redundancy a hobby project does not need.
// ---------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stmynews${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    // Identity-based connections only — the Function's managed identity gets
    // Storage Blob Data Owner and Queue Data Contributor.
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    accessTier: 'Hot'
  }
}

resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobServices
  name: deploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource queueServices 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource harvestQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueServices
  name: 'harvested-articles'
}

resource harvestPoison 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueServices
  name: 'harvested-articles-poison'
}

output cosmosAccountName string = cosmos.name
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output cosmosDatabaseName string = databaseName
output cosmosContainerName string = containerName
output storageAccountName string = storage.name
output deploymentContainerName string = deploymentContainerName
output harvestQueueName string = harvestQueue.name
