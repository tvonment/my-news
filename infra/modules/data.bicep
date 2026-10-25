param location string
param tags object
param resourceToken string

@description('freeTier: a provisioned account inside the subscription\'s one free-tier grant (1,000 RU/s + 25 GB). That is prod (mynews). serverless: billed per request, with no throughput to set, for mynews-dev: the grant is taken by prod, and the same 1,000 RU/s provisioned outside it would bill ~$58/month around the clock. Decided when the account is created, the only time the free tier can be set.')
@allowed([
  'freeTier'
  'serverless'
])
param cosmosMode string = 'freeTier'

@description('RU/s for `news`, the v2 container. Phase 1 runs it at 600 beside `items` at 400; Phase 5 raises it to 1,000 once `items` is gone. 400 is the least a container with its own throughput takes. Ignored on a serverless account.')
@minValue(400)
@maxValue(1000)
param newsThroughput int = 600

@description('RU/s for `items`, v1\'s container, which only the v1 harvest still writes. Lowered from 1,000 to 400 in Phase 1 so that `news` fits beside it. Ignored on a serverless account.')
@minValue(400)
@maxValue(1000)
param itemsThroughput int = 400

@description('Cosmos backup mode. Continuous7Days is free and restores, self-service, to any second of the last 7 days. Moving an account from Periodic to Continuous is ONE-WAY: Azure has no way back, so Periodic only describes an account that has not been switched yet.')
@allowed([
  'Continuous7Days'
  'Periodic'
])
param cosmosBackup string = 'Continuous7Days'

var cosmosAccountName = 'cosmos-mynews-${resourceToken}'
var databaseName = 'mynews'
// Both names are also app settings (appsettings.bicep): COSMOS_CONTAINER is
// what core.cosmos.get_container() returns, so all v2 code lands in `news`;
// LEGACY_COSMOS_CONTAINER is passed explicitly, only by the v1 harvest and the
// admin reads of its runs.
var newsContainerName = 'news'
var itemsContainerName = 'items'
var deploymentContainerName = 'app-package'
var serverless = cosmosMode == 'serverless'

// ---------------------------------------------------------------------------
// Cosmos DB for NoSQL — free tier, PROVISIONED throughput (prod).
//
// `enableFreeTier` can ONLY be set at account creation and there is exactly one
// free-tier account per subscription. It is also mutually exclusive with
// serverless ("Free tier is currently not available for serverless accounts"),
// which is why prod is a provisioned account despite the low traffic.
//
// A second environment in the same subscription (mynews-dev) cannot have the
// grant, so it runs serverless (cosmosMode, from COSMOS_MODE): no RU/s to
// provision, only requests and storage billed. Every property below that
// depends on the mode is written so that cosmosMode 'freeTier' sends exactly
// what prod has always been sent.
//
// `EnableNoSQLVectorSearch` is kept even though no container carries a vector
// policy (see the containers below): account capabilities are effectively
// additive, and attempting to remove one risks an ARM rejection for no
// benefit. It costs nothing while unused. A serverless account is new, so it
// carries only what makes it serverless.
// ---------------------------------------------------------------------------
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: cosmosAccountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    enableFreeTier: !serverless
    // Data-plane access is RBAC-only: the Function's managed identity and the
    // developer principal get a SQL role assignment, so there is no key to store.
    disableLocalAuth: true
    minimalTlsVersion: 'Tls12'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    // Pinned to the live value. Azure turned it on at creation although this
    // template never asked, so leaving it unset makes every what-if report a
    // difference, the kind of standing noise that hides a real change. With a
    // single region there is nothing to fail over to; the value changes nothing.
    enableAutomaticFailover: true
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: serverless
      ? [
          {
            name: 'EnableServerless'
          }
        ]
      : [
          {
            name: 'EnableNoSQLVectorSearch'
          }
        ]
    // Continuous backup, 7-day tier, because from Phase 1 this account holds
    // other people's data: their accounts, saves and history, not just one
    // owner's reading list. Periodic mode kept two daily copies, so a restore
    // could lose up to a day and needed a support ticket. Continuous mode
    // restores self-service to any second of the last 7 days (point-in-time
    // restore), so a bad migration or a buggy purge can be undone to the moment
    // before it ran. The 7-day tier costs nothing.
    //
    // The switch is ONE-WAY: once the account is Continuous, Azure will not move
    // it back, and setting `cosmosBackup` to 'Periodic' afterwards fails the
    // deployment. Continuous mode also means data a user deletes stays
    // restorable for up to 7 days, which the privacy policy has to say.
    //
    // The migration runs in the background after the provision that starts it,
    // and Azure refuses account-level updates until it finishes: wait for
    // `az cosmosdb show ... --query backupPolicy.migrationState` to report
    // Completed before provisioning again.
    backupPolicy: cosmosBackup == 'Continuous7Days' ? {
      type: 'Continuous'
      continuousModeProperties: {
        tier: 'Continuous7Days'
      }
    } : {
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
    // accounts with Shared Throughput". Throughput goes on the containers instead.
  }
}

// ---------------------------------------------------------------------------
// Throughput: `news` 600 + `items` 400 = the free grant's 1,000 RU/s.
//
// The grant covers 1,000 RU/s for the whole account, and every RU/s above it
// bills around the clock (a 400 RU/s container is ~$23.36/month). It is the
// SUM of the two containers that has to fit, which no @maxValue on either
// parameter can say. So fail() stops the deployment with the message below
// instead of provisioning past the grant. The condition reads parameters only,
// so ARM rejects the template at validation, before any resource here
// changes, and the what-if in scripts/whatif.sh fails on it as well.
// whatif_guard.py's cosmos-throughput rule checks the same total from the
// other side: everything the account will hold, the containers this template
// does not mention included.
//
// A serverless account takes no throughput at all: Cosmos rejects a container
// that names one, so there the options are empty.
// ---------------------------------------------------------------------------
var freeTierThroughput = 1000
var containerOptions = serverless
  ? {
      news: {}
      items: {}
    }
  : (newsThroughput + itemsThroughput <= freeTierThroughput
      ? {
          news: {
            throughput: newsThroughput
          }
          items: {
            throughput: itemsThroughput
          }
        }
      : fail('Cosmos: news ${newsThroughput} + items ${itemsThroughput} RU/s is over the ${freeTierThroughput} RU/s the free tier covers, and every RU/s above it bills around the clock. Lower one of them (infra/modules/data.bicep).'))

// `items`: v1's container, unchanged except for its throughput. Only the v1
// harvest writes it until Phase 2 removes that harvest. Phase 5 deletes it by
// hand after the migration and a 14-day soak, and must remove this resource in
// the same change: provisions are incremental, so a container the template
// still declares is simply created again, at itemsThroughput.
resource itemsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: itemsContainerName
  properties: {
    options: containerOptions.items
    resource: {
      id: itemsContainerName
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
      // v1's policy indexes everything. It is never changed in place
      // (docs/data-model.md, section 10): `items` only has to last until
      // Phase 5, and a re-index is a background job that leaves queries
      // incomplete while it runs.
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

// `news`: every v2 document (docs/data-model.md), one container,
// type-discriminated, partitioned by `pk`.
//
// Created after `items` has come down to 400, so the account never holds more
// than the grant at any moment, and a lowering that fails leaves `news`
// uncreated instead of both containers at 1,600 RU/s around the clock (~$35
// a month above the grant). The hour of the switch still bills a little:
// provisioned throughput bills each container's highest RU/s in the hour, so
// that hour counts `items` at its old 1,000 beside `news` at 600, about $0.05.
//
// Indexing is opt-in from creation: cosmos-index-policy.json excludes `/*` and
// lists the twelve paths the queries filter or sort on, plus a composite index
// for every ORDER BY over two properties. A write then pays for those paths
// only, where v1's index-everything policy paid for every field of a 2 KB
// article. In exchange, a query on a path the policy doesn't list FAILS (scans
// are never enabled), which is why backend/tests/test_index_coverage.py checks
// every SQL string against the file. The file is the whole policy, so changing
// it means editing that JSON (and docs/data-model.md section 6.1, which quotes
// it verbatim) and provisioning: never `az cosmosdb sql container update
// --idx`, which replaces the entire policy with whatever it is given. Adding a
// path later starts a background re-index, during which queries on that path
// return incomplete results, so the Phase 2 and 3 paths are in from the start.
//
// No vector policy, for the reasons on `items`; whatif_guard.py's
// cosmos-vector-policy rule blocks one appearing here too.
resource newsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: newsContainerName
  dependsOn: [
    itemsContainer
  ]
  properties: {
    options: containerOptions.news
    resource: {
      id: newsContainerName
      partitionKey: {
        paths: [
          '/pk'
        ]
        kind: 'Hash'
        version: 2
      }
      // As on `items`: per-document `ttl` decides, and -1 keeps a document.
      defaultTtl: -1
      indexingPolicy: loadJsonContent('cosmos-index-policy.json')
    }
  }
}

// ---------------------------------------------------------------------------
// Storage — required by the Functions host, and home of the job queues.
// Standard LRS: GRS would double the per-operation price for redundancy a
// hobby project does not need.
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

// ---------------------------------------------------------------------------
// Job queues, reached through the JOBS_STORAGE connection (appsettings.bicep:
// JOBS_STORAGE__queueServiceUri + __credential, i.e. the Function's managed
// identity, which already holds Storage Queue Data Contributor here):
//   account-purge  DELETE /me and the maintenance timer -> account_purge_worker
//   ops-jobs       a forced harvest (Phase 2: admin runs) -> ops_jobs_worker
//   suggestions    Phase 3's feed suggestions; no worker is registered before
//                  one works, so anything put here waits
// Messages carry ids only (docs/data-model.md, section 9).
//
// How the host works them is set in backend/host.json, which can't hold
// comments, so the reasons are here:
//   batchSize 4, newBatchThreshold 2: an instance works at most batchSize +
//     newBatchThreshold = 6 messages of one queue at once. Every purge paces
//     itself to 50 RU/s on its own, so that count multiplies: 6 is at most
//     300 of the 600 RU/s `news` has, where the defaults (16 + 8) would ask
//     1,200. Each extra instance adds as much again, which a beta's handful
//     of deletions stays far below.
//   maxPollingInterval 5 s: while an instance is up, an idle queue is checked
//     every few seconds, so a purge starts soon after its DELETE /me.
//   visibilityTimeout 30 s: a failed message is retried half a minute later,
//     not five minutes later as before.
//   maxDequeueCount 3: the third failure moves the message to -poison.
//   functionTimeout 20 min: a heavy reader's purge (~3,000 documents, ~6 min
//     at 50 RU/s) and a forced harvest both fit with room to spare.
//
// Each has its poison queue declared too. The host would create one the first
// time a message fails maxDequeueCount (3) times (backend/host.json), but
// /admin/health peeks the poison queues, and a check that reads "not found"
// until the first failure says nothing. Empty queues cost nothing.
//
// They replace `harvested-articles` and its poison queue, which came with the
// scaffold, were never bound to any function, and were deleted by hand in
// Phase 0 (provisions are incremental, so dropping a queue from this file
// never deletes it).
// ---------------------------------------------------------------------------
resource queueServices 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

var jobQueueNames = [
  'account-purge'
  'suggestions'
  'ops-jobs'
]
var queueNames = flatten(map(jobQueueNames, name => [
  name
  '${name}-poison'
]))

resource queues 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = [
  for name in queueNames: {
    parent: queueServices
    name: name
  }
]

output cosmosAccountName string = cosmos.name
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output cosmosDatabaseName string = databaseName
// A module's outputs exist only once its whole deployment has finished, so the
// app settings built from these (main.bicep) land after both containers and
// every queue exist.
output cosmosContainerName string = newsContainerName
output legacyCosmosContainerName string = itemsContainerName
output storageAccountName string = storage.name
output deploymentContainerName string = deploymentContainerName
// The account's queue endpoint, https://<account>.queue.core.windows.net/
// (trailing slash included).
output queueServiceUri string = storage.properties.primaryEndpoints.queue
output queueNames array = queueNames
