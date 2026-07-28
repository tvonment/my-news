param location string
param tags object
param resourceToken string

@description('Daily ingestion cap in GB. The Log Analytics free grant is 5 GB/month per BILLING ACCOUNT (shared with any other resource you own), and West Europe overage is $2.99/GB — so cap it.')
param dailyQuotaGb int = 1

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-mynews-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    // Retention is free for the first 31 days; beyond that it bills per GB-month.
    retentionInDays: 31
    workspaceCapping: {
      dailyQuotaGb: dailyQuotaGb
    }
    features: {
      searchVersion: 1
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-mynews-${resourceToken}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    // Cap the sampling percentage server-side as a second line of defence
    // alongside host.json adaptive sampling.
    SamplingPercentage: 100
    DisableIpMasking: false
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

output workspaceId string = workspace.id
output appInsightsName string = appInsights.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
