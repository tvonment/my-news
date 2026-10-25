param location string
param tags object
param resourceToken string

// ---------------------------------------------------------------------------
// Microsoft Foundry resource — and, since Phase 0, NO model deployments.
//
// v1 ran three GlobalStandard deployments here: scorer (gpt-5-nano, 100k TPM),
// summarizer (gpt-5.4, 50k TPM) and embedding (text-embedding-3-small, 50k
// TPM). They were retired in Phase 0 together with the digest and profile runs
// that called them; harvest makes no model calls. Between them those capacities
// allowed a worst-case runaway of ~$280/day, and Azure OpenAI has no hard spend
// limit, so a deployment nothing uses is pure liability.
//
// Removing them from this file does NOT remove them from Azure: `azd provision`
// deploys in incremental mode, which never deletes a resource the template
// stops mentioning. So they are also deleted by hand, once:
//   az cognitiveservices account deployment delete -g rg-mynews-mynews \
//     -n aif-mynews-ysd3mgjeph5zu --deployment-name <scorer|summarizer|embedding>
// and `az cognitiveservices account deployment list -g rg-mynews-mynews
// -n aif-mynews-ysd3mgjeph5zu` must return []. Until it does, the ceiling is
// still live for anything holding a token.
//
// Plan as if that deletion is one-way for gpt-5-nano. An earlier note said new
// customers lost access on 2026-08-07 and that holding a deployment was what
// kept this subscription's rights; Microsoft's model schedule (updated
// 2026-09-23) still lists it as GA until it retires on 2027-02-09. Neither
// claim was verified, and v2 does not need it, so do not count on redeploying
// it to roll back to v1's scorer.
//
// v2 adds its deployments in Phase 2: the classifier and summarizer as Data
// Zone Standard (EU), so prompts are processed inside the EU data zone rather
// than in whichever region has capacity, and the embedding model as a regional
// Standard deployment. Rules for when they come back:
//   * Check quota first. In July 2026 every gpt-5.6 variant had a limit of 0 on
//     this subscription in all ten regions checked, and Data Zone quota is
//     pooled across the subscription, so it is shared with the other projects.
//   * Chain them with dependsOn: Cognitive Services rejects concurrent
//     deployment creation on one account.
//   * Put a @maxValue on every capacity: capacity is the runaway ceiling.
//
// The account itself costs $0 at rest. The things that DO bill while idle and
// must never appear here:
//   * sku.name 'ProvisionedManaged'  -> ~$1,460/month per PTU (~15 PTU minimum)
//   * fine-tuned model hosting       -> $1.70/hour (~$1,241/month), idle or not
//   * Foundry Agent Service "Standard setup" -> mandates BYO Azure AI Search
//     (Basic = $73.73/month)
// ---------------------------------------------------------------------------

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
    // via the Cognitive Services OpenAI User role (kept through Phase 0 and 1,
    // when nothing calls it, so Phase 2 needs no new grant). The Microsoft
    // sample leaves this false; there is no key to protect here, so it stays
    // true.
    disableLocalAuth: true
  }
}

// Model deployments live on the ACCOUNT, not the project, so inference and the
// OpenAI v1 API endpoint are unaffected by this. The project exists so the
// resource is a first-class Foundry resource and so agent/eval work later has
// a home.
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

output foundryAccountName string = foundry.name
output foundryProjectName string = project.name
// The OpenAI v1 API's base_url accepts the `*.openai.azure.com` and
// `*.services.ai.azure.com` forms. An AIServices account's properties.endpoint is
// `*.cognitiveservices.azure.com`, which is NOT one of them — so construct the
// openai form from the custom subdomain.
output foundryEndpoint string = 'https://${foundryName}.openai.azure.com'
output foundryAccountEndpoint string = foundry.properties.endpoint
