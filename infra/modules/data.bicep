param location string
param tags object
param resourceToken string

@description('Total provisioned throughput. MUST stay <= 1000 to remain inside the Cosmos free-tier grant. A second container at 400 RU/s would cost ~$23.36/month.')
@minValue(400)
@maxValue(1000)
param containerThroughput int = 1000

@description('Embedding dimensions. text-embedding-3-small is truncated to this via the `dimensions` parameter (Matryoshka). NOTE: a vector policy cannot be edited in place — changing this means recreating the container and re-ingesting.')
param embeddingDimensions int = 512

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
// `EnableNoSQLVectorSearch` on a free-tier account is the one assumption in the
// plan no first-party doc confirms — this deployment is the test. If it fails,
// drop the capability + vectorEmbeddingPolicy and fall back to packed-blob
// similarity in NumPy (see scripts/verify_vector_search.py).
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
            // duplicated per document for a lookup we never perform.
            path: '/embedding/*'
          }
        ]
        vectorIndexes: [
          {
            path: '/embedding'
            // quantizedFlat, not diskANN: DiskANN only pays off above ~50k
            // vectors and costs ~65 RU/insert vs ~35 RU for quantizedFlat.
            // Note both silently full-scan below 1,000 indexed vectors.
            type: 'quantizedFlat'
          }
        ]
      }
      vectorEmbeddingPolicy: {
        vectorEmbeddings: [
          {
            path: '/embedding'
            // float16 halves vector storage and insert RU vs float32 for a
            // "small" recall impact. If ARM rejects this, use 'float32'.
            dataType: 'float16'
            dimensions: embeddingDimensions
            distanceFunction: 'cosine'
          }
        ]
      }
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
