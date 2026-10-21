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
// Cost, because the whole project runs on a 25 CHF/month budget against a
// measured ~11 CHF (Switzerland North list prices, in CHF):
//   * Metric alerts: the 10 free monitored time-series are per BILLING ACCOUNT,
//     and they are already used up on this one (September 2026 billed 0.08 CHF
//     for the rule below). So each series bills ~0.08 CHF/month. One rule with
//     one dimension value = one time-series.
//   * Log search alerts: no free allowance either, and no cheaper hourly meter.
//     Each rule bills ~0.42 CHF/month, the 15-minute-or-slower rate, so
//     `pipelineSilent` costs that although it only runs once an hour. Prefer
//     one combined query over a second rule.
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

// Metric and log alerts need a real action group. The budgets also email
// `contactEmails` inline, which only Consumption budgets support, and since
// Phase 0 the resource-group budget (budget.bicep) notifies this group too.
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

// The backstop: harvest runs every 30 minutes, so three hours without a
// SUCCESSFUL one means the pipeline is dead regardless of cause — broken
// dependency, failed deploy, unbound timer, throttled account.
//
// It still watches `harvest_timer` only, and that is now the whole pipeline:
// Phase 0 retired v1's digest and profile timers, and nothing else runs until
// Phase 2's cycle, which joins this same query rather than a second rule.
//
// This counts `requests` (function executions), not a log line. The first
// version of this rule matched the "Harvest complete" trace, which adaptive
// sampling drops precisely on the runs that do the most work: a harvest that
// created 112 documents emitted >2,000 SDK trace lines and its own summary line
// was discarded with them. `host.json` now excludes Request from sampling, so
// executions are the one signal guaranteed to be complete.
//
// `success == true` matters as much as the count: the two-day float16 outage ran
// harvest on schedule the whole time and every single run failed. A rule that
// only counted executions would have stayed green throughout it.
//
// `summarize` is load-bearing too: a bare filter returns zero ROWS when nothing
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
          query: 'requests | where name == "harvest_timer" | where success == true | summarize Runs = count()'
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
