// ---------------------------------------------------------------------------
// Resource-group cost budget: counts everything MyNews spends, nothing else.
//
// Azure OpenAI has no hard spend limit — the docs say so explicitly. Once
// Phase 2 deploys models again, this alert plus the in-code spend reservations
// are the only brakes on a runaway loop. The subscription-wide guard in
// main.bicep watches the shared credit; this one watches this app.
// ---------------------------------------------------------------------------

param budgetName string

@description('Monthly amount in the BILLING currency, which is CHF on this subscription. A budget has no currency field; Azure always counts in the billing currency, so the 25 that the old `amountUsd` name called dollars was always francs.')
@minValue(5)
param amount int

@description('First day of the budget\'s first month, YYYY-MM-DD. Required on purpose, with no default (see below).')
param startDate string

param alertEmail string

@description('Action group from alerts.bicep. Every notification goes to it as well as to the inline email.')
param actionGroupId string

// startDate is fixed and comes from the azd env (BUDGET_START_DATE, the live
// budget's 2026-07-01). It used to default to utcNow()'s first of the month,
// which made the template drift on its own: the computed date matched the live
// budget only in July, the month the budget was created. From August on, every
// provision would have moved the live start date to the current month, and
// what-if would have shown that change on every run, real change or not.
//
// A NEW budget (say, on a fresh subscription) needs the first of the CURRENT
// month: the API accepts a past start date only within the current period.
resource budget 'Microsoft.Consumption/budgets@2023-05-01' = {
  name: budgetName
  properties: {
    category: 'Cost'
    amount: amount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: startDate
    }
    // Thresholds against the measured baseline of ~11 CHF/month (September
    // 2026: 11.08 CHF). The old 40 % line was 10 CHF, below that baseline, so
    // it fired in normal operation every month, and an alert that always fires
    // is an alert nobody reads.
    //   Actual 60 %   = 15 CHF, ~4 CHF above normal: the first sign of a real
    //                   change, with 10 CHF of room still left.
    //   Actual 90 %   = 22.50 CHF: the last warning with money left to save.
    //   Forecast 100 %: catches a runaway days before the money is spent.
    // No Actual 100 %: by the time it fires the money is gone, and the two
    // warnings above have already said so.
    //
    // Each one goes to the inline email AND the action group. The inline email
    // is the path that cannot break; the group is where any later receiver
    // (SMS, push, webhook) gets added. Today both reach the same inbox, so expect
    // two emails per threshold.
    notifications: {
      Actual60Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 60
        thresholdType: 'Actual'
        contactEmails: [
          alertEmail
        ]
        contactGroups: [
          actionGroupId
        ]
      }
      Actual90Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 90
        thresholdType: 'Actual'
        contactEmails: [
          alertEmail
        ]
        contactGroups: [
          actionGroupId
        ]
      }
      Forecast100Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [
          alertEmail
        ]
        contactGroups: [
          actionGroupId
        ]
      }
    }
  }
}

output budgetId string = budget.id
