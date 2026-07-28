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
// MODEL CHOICE — the plan called for gpt-5.6-luna/terra, but this subscription
// has a quota LIMIT OF 0 for every gpt-5.6 variant in all ten regions checked
// (westeurope, swedencentral, northeurope, francecentral, germanywestcentral,
// switzerlandnorth, eastus, eastus2, westus3, japaneast). That is a
// subscription-level gap, not a regional one, and needs a manual quota request.
//
// So the two-tier design maps onto models that DO have quota here:
//   scorer     gpt-5-nano  $0.05/$0.40  <- was gpt-5.6-luna  ($1.00/$6.00)
//   summarizer gpt-5.4     $2.50/$15.00 <- was gpt-5.6-terra ($2.50/$15.00)
// The summarizer tier is unchanged in price; the scorer got 20x cheaper.
//
// To switch back once gpt-5.6 quota is granted, change the four params below to
// 'gpt-5.6-luna' / 'gpt-5.6-terra' with version '2026-07-09', update PRICES and
// the reasoning-effort settings in the backend, and redeploy.
// ---------------------------------------------------------------------------

@description('Bulk scorer, run on every item that clears the embedding gate. gpt-5-nano is the cheapest model in the catalog at $0.05/1M input, $0.40/1M output, and this subscription has 47,900 units of free quota for it.')
param scorerModelName string = 'gpt-5-nano'

@description('Version for the scorer. gpt-5-nano retires 2027-02-06. This subscription already has a gpt-5-nano deployment, so it keeps "existing customer" rights past the 2026-08-07 no-new-customers date.')
param scorerModelVersion string = '2025-08-07'

@description('Summarizer, run only on items above the relevance threshold. Same $2.50/$15.00 per 1M as the originally planned gpt-5.6-terra.')
param summarizerModelName string = 'gpt-5.4'

@description('Version for the summarizer. gpt-5.4 retires 2027-03-05.')
param summarizerModelVersion string = '2026-03-05'

@description('Embeddings. No retirement scheduled before 2027-04-15 — the most stable model in the catalog.')
param embeddingModelName string = 'text-embedding-3-small'

param embeddingModelVersion string = '1'

@description('Throughput in thousands of tokens per minute.')
param scorerCapacity int = 100
param summarizerCapacity int = 50
param embeddingCapacity int = 50

var foundryName = 'aif-mynews-${resourceToken}'

// This is a Microsoft Foundry resource, not a plain "AI Services" account. The
// difference is `allowProjectManagement: true` plus at least one child project:
// that combination is what the current Foundry portal (ai.azure.com) recognizes
// and what unlocks the project-scoped surface (agents, evaluations, the tool
// catalog). Without it you get a Cognitive Services multi-service account that
// serves inference perfectly well but does not appear as a Foundry resource.
//
// The hub-based model (Microsoft.MachineLearningServices workspaces plus
// separately wired Key Vault, Storage and AI Search connections) is the older
// approach and is not used here — projects are now native child resources.
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
    allowProjectManagement: true
    customSubDomainName: foundryName
    publicNetworkAccess: 'Enabled'
    // Key-based auth off: the Function reaches Foundry with its managed identity
    // via the Cognitive Services OpenAI User role. The Microsoft sample leaves
    // this false; there is no key to protect here, so it stays true.
    disableLocalAuth: true
  }
}

// Model deployments live on the ACCOUNT, not the project, so inference and the
// v1 API endpoint are unaffected by this. The project exists so the resource is
// a first-class Foundry resource and so agent/eval work later has a home.
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: '${foundryName}-proj'
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'MyNews'
    description: 'Personal AI news aggregator: scoring, summarization and embeddings.'
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
      version: scorerModelVersion
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
      version: summarizerModelVersion
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
output foundryProjectName string = project.name
// The v1 API's base_url accepts the `*.openai.azure.com` and `*.services.ai.azure.com`
// forms. An AIServices account's properties.endpoint is `*.cognitiveservices.azure.com`,
// which is NOT one of them — so construct the openai form from the custom subdomain.
output foundryEndpoint string = 'https://${foundryName}.openai.azure.com'
output foundryAccountEndpoint string = foundry.properties.endpoint
// Underlying model ids, so the backend can price token usage correctly — the
// deployment names ('scorer'/'summarizer') carry no pricing information.
output scorerModelId string = scorerModelName
output summarizerModelId string = summarizerModelName
output embeddingModelId string = embeddingModelName
output scorerDeployment string = scorer.name
output summarizerDeployment string = summarizer.name
output embeddingDeployment string = embedding.name
