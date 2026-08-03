// ---------------------------------------------------------------------------
// Alerting.
//
// This module exists because the 2026-08-02 float16 outage ran for two days
// before anyone noticed, and the only reason was that nothing was watching.
// App Insights' own Failure Anomalies rule had failed to deploy on day one with
// `MissingSubscriptionRegistration: Microsoft.AlertsManagement` — a failed
// deployment of a monitoring rule is exactly the kind of failure nobody chases.
// The sole surviving monitor was the cost budget, and an outage costs LESS than
// working, so it could never fire.
//
// Cost, because this project's whole constraint is ~$8/month:
//   * Metric alerts: the first 10 monitored time-series per month are free.
//     One rule with one dimension filter = one time-series. Free.
//   * Log search alerts: the first time-series is included at no cost. Free.
//   * Standard availability (web) tests are billed PER EXECUTION with no free
//     grant. One test from 3 locations every 5 minutes is ~26k executions a
//     month, which is the kind of number that quietly doubles the bill. That is
//     why there is no web test here: `pipelineSilent` below catches the same
//     "everything is down" condition from telemetry we already pay to ingest.
// ---------------------------------------------------------------------------

param location string
param tags object
param alertEmail string
param cosmosAccountName string
param appInsightsId string

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  name: cosmosAccountName
}

// The budget sends to `contactEmails` inline, which only Consumption budgets
// support. Metric and log alerts need a real action group.
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-mynews'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'mynews'
    enabled: true
    emailReceivers: [
      {
        name: 'owner'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

// The alert that would have caught the float16 outage on the first cycle.
// Cosmos answered every query with InternalServerError while still accepting
// writes, so request volume looked normal and only the status code gave it away.
resource cosmosServerErrors 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'mynews-cosmos-5xx'
  location: 'global'
  tags: tags
  properties: {
    description: 'Cosmos is returning server errors. The data plane is failing, not the app.'
    severity: 1
    enabled: true
    scopes: [
      cosmos.id
    ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'ServerErrors'
          metricNamespace: 'Microsoft.DocumentDB/databaseAccounts'
          metricName: 'TotalRequests'
          dimensions: [
            {
              name: 'StatusCode'
              operator: 'Include'
              values: [
                '500'
              ]
            }
          ]
          operator: 'GreaterThan'
          threshold: 0
          timeAggregation: 'Count'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    autoMitigate: true
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
  }
}

// The backstop: harvest runs every 30 minutes and logs one line when it finishes,
// so three hours of silence means the pipeline is dead regardless of the cause —
// broken dependency, failed deploy, unbound timer, throttled account.
//
// `summarize` is load-bearing: a bare filter returns zero ROWS when nothing
// matched, and a rule with no rows to evaluate does not fire. Summarising to a
// single row means the "nothing happened" case still produces a 0 to compare.
resource pipelineSilent 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'mynews-pipeline-silent'
  location: location
  tags: tags
  properties: {
    description: 'No harvest has completed in 3 hours. The pipeline has stopped.'
    severity: 1
    enabled: true
    scopes: [
      appInsightsId
    ]
    evaluationFrequency: 'PT1H'
    windowSize: 'PT3H'
    criteria: {
      allOf: [
        {
          query: 'traces | where message startswith "Harvest complete" | summarize Runs = count()'
          metricMeasureColumn: 'Runs'
          timeAggregation: 'Total'
          operator: 'LessThan'
          threshold: 1
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
  }
}

output actionGroupId string = actionGroup.id
