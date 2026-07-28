param location string
param tags object
param resourceToken string

// ---------------------------------------------------------------------------
// Microsoft Foundry resource + model deployments.
//
// Costs $0 at rest: Global Standard deployments carry token meters only, with no
// hourly meter. The things that DO bill while idle and must never appear here:
//   * sku.name 'ProvisionedManaged'  -> ~$1,460/month per PTU (~15 PTU minimum)
//   * fine-tuned model hosting       -> $1.70/hour (~$1,241/month), idle or not
//   * Foundry Agent Service "Standard setup" -> mandates BYO Azure AI Search
//     (Basic = $73.73/month)
//
// Model names are exact: there is NO bare `gpt-5.6` on Azure. luna/terra/sol are
// real deployable model names, not size codenames. All three are GA, version
// 2026-07-09, and retire 2027-07-09.
// ---------------------------------------------------------------------------

@description('Bulk scorer. luna is the cheapest 5.6 tier: $1.00/1M input, $6.00/1M output (West Europe, Global Standard).')
param scorerModelName string = 'gpt-5.6-luna'

@description('Summarizer. terra costs $2.50/$15.00 per 1M but only runs on the ~10% of items that clear the relevance threshold.')
param summarizerModelName string = 'gpt-5.6-terra'

param modelVersion string = '2026-07-09'

@description('Embeddings. No retirement scheduled before 2027-04-15 — the most stable model in the catalog.')
param embeddingModelName string = 'text-embedding-3-small'

param embeddingModelVersion string = '1'

@description('Throughput in thousands of tokens per minute.')
param scorerCapacity int = 100
param summarizerCapacity int = 50
param embeddingCapacity int = 50

var foundryName = 'aif-mynews-${resourceToken}'

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: foundryName
    publicNetworkAccess: 'Enabled'
    // Key-based auth off: the Function reaches Foundry with its managed identity
    // via the Cognitive Services OpenAI User role.
    disableLocalAuth: true
  }
}

// Cognitive Services rejects concurrent deployment creation on one account, so
// these are chained rather than deployed in parallel.
resource scorer 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: 'scorer'
  sku: {
    name: 'GlobalStandard'
    capacity: scorerCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: scorerModelName
      version: modelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource summarizer 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: 'summarizer'
  sku: {
    name: 'GlobalStandard'
    capacity: summarizerCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: summarizerModelName
      version: modelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [
    scorer
  ]
}

resource embedding 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: 'embedding'
  sku: {
    name: 'GlobalStandard'
    capacity: embeddingCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: embeddingModelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [
    summarizer
  ]
}

output foundryAccountName string = foundry.name
// The v1 API's base_url accepts the `*.openai.azure.com` and `*.services.ai.azure.com`
// forms. An AIServices account's properties.endpoint is `*.cognitiveservices.azure.com`,
// which is NOT one of them — so construct the openai form from the custom subdomain.
output foundryEndpoint string = 'https://${foundryName}.openai.azure.com'
output foundryAccountEndpoint string = foundry.properties.endpoint
output scorerDeployment string = scorer.name
output summarizerDeployment string = summarizer.name
output embeddingDeployment string = embedding.name
