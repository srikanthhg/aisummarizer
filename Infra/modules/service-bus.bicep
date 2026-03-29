param namespaces_ai_summarizer_name string = 'ai-summarizer'

resource namespaces_ai_summarizer_name_resource 'Microsoft.ServiceBus/namespaces@2025-05-01-preview' = {
  name: namespaces_ai_summarizer_name
  location: 'eastus'
  sku: {
    name: 'Basic'
    tier: 'Basic'
  }
  properties: {
    platformCapabilities: {
      confidentialCompute: {
        mode: 'Disabled'
      }
    }
    geoDataReplication: {
      maxReplicationLagDurationInSeconds: 0
      locations: [
        {
          locationName: 'eastus'
          roleType: 'Primary'
        }
      ]
    }
    premiumMessagingPartitions: 0
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
    zoneRedundant: true
  }
}

resource namespaces_ai_summarizer_name_RootManageSharedAccessKey 'Microsoft.ServiceBus/namespaces/authorizationrules@2025-05-01-preview' = {
  parent: namespaces_ai_summarizer_name_resource
  name: 'RootManageSharedAccessKey'
  location: 'eastus'
  properties: {
    rights: [
      'Listen'
      'Manage'
      'Send'
    ]
  }
}

resource namespaces_ai_summarizer_name_default 'Microsoft.ServiceBus/namespaces/networkrulesets@2025-05-01-preview' = {
  parent: namespaces_ai_summarizer_name_resource
  name: 'default'
  location: 'eastus'
  properties: {
    publicNetworkAccess: 'Enabled'
    defaultAction: 'Allow'
    virtualNetworkRules: []
    ipRules: []
    trustedServiceAccessEnabled: false
  }
}
