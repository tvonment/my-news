param budgetName string
param amountUsd int
param alertEmail string

@description('Budget start date, first of the month. Must not be in the past for a new budget.')
param startDate string = '${substring(utcNow('yyyy-MM-dd'), 0, 8)}01'

// Azure OpenAI has no hard spend limit — the docs say so explicitly. This alert
// plus the in-code daily token budget are the only brakes on a runaway loop.
resource budget 'Microsoft.Consumption/budgets@2023-05-01' = {
  name: budgetName
  properties: {
    category: 'Cost'
    amount: amountUsd
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: startDate
    }
    notifications: {
      // Early warning while there is still time to react.
      Actual40Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 40
        contactEmails: [
          alertEmail
        ]
        thresholdType: 'Actual'
      }
      Actual100Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        contactEmails: [
          alertEmail
        ]
        thresholdType: 'Actual'
      }
      // Forecast catches a runaway loop days before it actually spends the money.
      Forecast100Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        contactEmails: [
          alertEmail
        ]
        thresholdType: 'Forecasted'
      }
    }
  }
}

output budgetId string = budget.id
