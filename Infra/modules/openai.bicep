param accounts_my_ai_summarizer_name string = 'my-ai-summarizer'

resource accounts_my_ai_summarizer_name_resource 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = {
  name: accounts_my_ai_summarizer_name
  location: 'eastus'
  sku: {
    name: 'S0'
  }
  kind: 'OpenAI'
  properties: {
    apiProperties: {}
    customSubDomainName: accounts_my_ai_summarizer_name
    networkAcls: {
      defaultAction: 'Allow'
      virtualNetworkRules: []
      ipRules: []
    }
    allowProjectManagement: false
    publicNetworkAccess: 'Enabled'
    storedCompletionsDisabled: false
  }
}

resource accounts_my_ai_summarizer_name_Default 'Microsoft.CognitiveServices/accounts/defenderForAISettings@2025-10-01-preview' = {
  parent: accounts_my_ai_summarizer_name_resource
  name: 'Default'
  properties: {
    state: 'Disabled'
  }
}

resource accounts_my_ai_summarizer_name_gpt_4_1_mini 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = {
  parent: accounts_my_ai_summarizer_name_resource
  name: 'gpt-4.1-mini'
  sku: {
    name: 'GlobalStandard'
    capacity: 250
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4.1-mini'
      version: '2025-04-14'
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    currentCapacity: 250
    raiPolicyName: 'Microsoft.DefaultV2'
    deploymentState: 'Running'
  }
}

resource accounts_my_ai_summarizer_name_Microsoft_Default 'Microsoft.CognitiveServices/accounts/raiPolicies@2025-10-01-preview' = {
  parent: accounts_my_ai_summarizer_name_resource
  name: 'Microsoft.Default'
  properties: {
    mode: 'Blocking'
    contentFilters: [
      {
        name: 'Hate'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Prompt'
        action: 'NONE'
      }
      {
        name: 'Hate'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Completion'
        action: 'NONE'
      }
      {
        name: 'Sexual'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Prompt'
        action: 'NONE'
      }
      {
        name: 'Sexual'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Completion'
        action: 'NONE'
      }
      {
        name: 'Violence'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Prompt'
        action: 'NONE'
      }
      {
        name: 'Violence'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Completion'
        action: 'NONE'
      }
      {
        name: 'Selfharm'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Prompt'
        action: 'NONE'
      }
      {
        name: 'Selfharm'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Completion'
        action: 'NONE'
      }
    ]
  }
}

resource accounts_my_ai_summarizer_name_Microsoft_DefaultV2 'Microsoft.CognitiveServices/accounts/raiPolicies@2025-10-01-preview' = {
  parent: accounts_my_ai_summarizer_name_resource
  name: 'Microsoft.DefaultV2'
  properties: {
    mode: 'Blocking'
    contentFilters: [
      {
        name: 'Hate'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Prompt'
        action: 'NONE'
      }
      {
        name: 'Hate'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Completion'
        action: 'NONE'
      }
      {
        name: 'Sexual'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Prompt'
        action: 'NONE'
      }
      {
        name: 'Sexual'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Completion'
        action: 'NONE'
      }
      {
        name: 'Violence'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Prompt'
        action: 'NONE'
      }
      {
        name: 'Violence'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Completion'
        action: 'NONE'
      }
      {
        name: 'Selfharm'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Prompt'
        action: 'NONE'
      }
      {
        name: 'Selfharm'
        severityThreshold: 'Medium'
        blocking: true
        enabled: true
        source: 'Completion'
        action: 'NONE'
      }
      {
        name: 'Jailbreak'
        blocking: true
        enabled: true
        source: 'Prompt'
        action: 'NONE'
      }
      {
        name: 'Protected Material Text'
        blocking: true
        enabled: true
        source: 'Completion'
        action: 'NONE'
      }
      {
        name: 'Protected Material Code'
        blocking: false
        enabled: true
        source: 'Completion'
        action: 'NONE'
      }
    ]
  }
}
