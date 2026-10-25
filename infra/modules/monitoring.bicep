param location string
param tags object
param resourceToken string

// No free grant applies to this workspace. Pay-as-you-go accounts get 5 GB a
// month of Log Analytics for free, but this runs on a Visual Studio credit
// subscription, which gets no free grants: every GB bills from the first, at a
// list price of CHF 2.74 in Switzerland North (September's bill charged 2.66,
// the rate docs/cost.md uses). In September 2026 that was 5.09 CHF, the
// largest line on the bill, almost all of it Cosmos SDK header dumps.
//
// So the daily cap is the brake, and it has to go below 1 GB/day, which an int
// parameter cannot express (Bicep has no decimal type): at 1 GB, a day-long
// flood still bills ~2.7 CHF. The value is therefore a string that json()
// turns into the number 0.2 at deployment time: ~0.55 CHF on the worst day.
// Past the cap, ingestion stops until the daily reset, so harvest runs stop
// arriving and `pipeline-silent` (alerts.bicep) fires within ~3 hours; that is
// how a flood announces itself.
@description('Daily ingestion cap in GB, as a decimal string (it goes through json()). An int cannot go below 1 GB, and a 1 GB/day flood costs ~2.7 CHF/day.')
param dailyQuotaGb string = '0.2'

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-mynews-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    // Retention is free for the first 31 days; beyond that it bills per GB-month.
    // The App* tables below do not inherit this; they set their own.
    retentionInDays: 31
    workspaceCapping: {
      dailyQuotaGb: json(dailyQuotaGb)
    }
    features: {
      searchVersion: 1
    }
  }
}

// App* tables keep 30 days instead of the 90 they default to (they never
// followed the workspace's 31). This is not a saving, since App* data is
// retained 90 days at no charge. It is data minimisation: once other people
// use the app, these tables hold their requests, exceptions and dependency
// calls, and nothing here looks further back than a month (the alerts read
// hours). Two more months of other people's telemetry would buy nothing.
//
// The cut deletes whatever is older than 30 days, which is why Phase 0 saves
// its failure and trace-volume baselines to rescue/ before this is provisioned.
// The list is the tables a server-side SDK can write; the rest of the App*
// family (page views, browser timings) only fills from a browser SDK.
var appTables = [
  'AppTraces'
  'AppRequests'
  'AppExceptions'
  'AppDependencies'
  'AppMetrics'
  'AppPerformanceCounters'
  'AppAvailabilityResults'
  'AppEvents'
]

resource appTableRetention 'Microsoft.OperationalInsights/workspaces/tables@2023-09-01' = [
  for table in appTables: {
    parent: workspace
    name: table
    properties: {
      retentionInDays: 30
      // Total equals interactive: no long-term tier, nothing outlives 30 days.
      totalRetentionInDays: 30
    }
  }
]

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
output appInsightsId string = appInsights.id
output appInsightsConnectionString string = appInsights.properties.ConnectionString
