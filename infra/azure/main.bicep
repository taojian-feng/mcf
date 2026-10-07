// MCF Agentic Fulfillment Orchestration: Azure foundation (pilot scale).
// Deploy: see docs/DEPLOY_AZURE.md (GitHub Actions workflow .github/workflows/deploy-azure.yml).
// Defaults are sized for a dev environment; set serviceBusSku=Premium and agentApps to one app per agent for the pilot.
// Partners: Corp A (MCF API) and Corp B (order management, feeds) run as simulators in their own Container Apps with
// their own identities and databases, talking to the system only over HTTPS (docs/PARTNER_APIS.md).
// Not yet included (next iteration): private endpoints + VNet integration, APIM, Microsoft Foundry deployment, Purview.
// See docs/ARCHITECTURE.md section 4.

@description('Short lowercase prefix for resource names')
param prefix string = 'mcfpilot'
param location string = resourceGroup().location
@description('Agent image. The first deploy uses a public placeholder; the workflow then builds the real image into the registry.')
param image string = 'mcr.microsoft.com/k8se/quickstart:latest'
@description('Standard is enough for dev (topics, scheduled messages, duplicate detection); Premium adds isolation and private endpoints.')
@allowed([ 'Standard', 'Premium' ])
param serviceBusSku string = 'Standard'
@description('Long-running consumer apps. Every app runs every agent handler as competing consumers, so one app is enough for dev.')
param agentApps array = [ 'all' ]
@description('Partner simulators image (sim/Dockerfile). The first deploy uses the public placeholder.')
param simImage string = 'mcr.microsoft.com/k8se/quickstart:latest'
@description('Run the Corp A and Corp B simulators, the partner gateway, and call Corp A over HTTPS. Off: an in-process fake MCF API and no partner traffic.')
param enablePartners bool = true
@description('Average consumer orders per day that the Corp B simulator places.')
param corpbOrdersPerDay string = '140'
@description('Audit database size. S0 (~$15/month) never pauses and easily takes the pilot load; GP_S_Gen5_2 is serverless, which only saves money while idle.')
@allowed([ 'S0', 'S1', 'GP_S_Gen5_2' ])
param sqlSku string = 'S0'
@description('Object id of the principal that deploys (the GitHub Actions service principal); gets Key Vault secret write access.')
param deployerPrincipalId string = ''
@description('Entra ID group that administers the audit SQL database')
param sqlAdminGroupObjectId string
param sqlAdminGroupName string
@description('Days consumer PII is kept before Cosmos TTL deletes it (R9.5)')
param piiRetentionDays int = 90

@description('Send notifications by email (Azure Communication Services with a free Azure-managed sender domain). Off: notifications are only logged.')
param enableEmail bool = true

@description('Region for the audit database. Busy regions sometimes refuse new Azure SQL servers; the rest of the stack stays in location.')
param sqlLocation string = location

var suffix = uniqueString(resourceGroup().id)
var topics = [
  'orders.received'
  'orders.lifecycle'
  'oms.outbound'
  'exceptions.signals'
  'exceptions.updates'
  'approvals.decisions'
  'handoffs.events'
  'handoffs.updates'
  'timers'
  'feeds.arrived'
  'inventory.events'
  'inventory.state'
  'disposition.events'
  'shipments.facts'
  'corpb.outbound'
]
// subscriber -> topics it consumes (mirrors bus.subscribe calls in mcf/)
var subscriptions = [
  { name: 'orchestration-agent', topics: [ 'orders.received', 'orders.lifecycle', 'exceptions.updates', 'timers' ] }
  { name: 'exception-agent', topics: [ 'exceptions.signals', 'approvals.decisions', 'handoffs.events', 'timers' ] }
  { name: 'approval-workflow', topics: [ 'timers' ] }
  { name: 'handoff-tracker', topics: [ 'timers', 'handoffs.updates' ] }
  { name: 'reconciliation-agent', topics: [ 'inventory.events' ] }
  { name: 'planning-agent', topics: [ 'inventory.state', 'approvals.decisions', 'timers' ] }
  { name: 'disposition-agent', topics: [ 'approvals.decisions', 'disposition.events', 'timers' ] }
  { name: 'reporting-agent', topics: [ 'shipments.facts' ] }
  { name: 'oms-connector', topics: [ 'oms.outbound' ] }
  { name: 'corpb-connector', topics: [ 'corpb.outbound' ] }
  { name: 'integration-layer', topics: [ 'feeds.arrived' ] }
  { name: 'corpa-connector', topics: [ 'handoffs.events' ] }
]
var subscriptionPairs = flatten(map(subscriptions, s => map(s.topics, t => { sub: s.name, topic: t })))
var containers = [
  { name: 'order_pii', ttl: piiRetentionDays * 86400 }
  { name: 'orders', ttl: -1 }
  { name: 'exception_cases', ttl: -1 }
  { name: 'approvals', ttl: -1 }
  { name: 'handoffs', ttl: -1 }
  { name: 'inventory_events', ttl: -1 }
  { name: 'inventory_state', ttl: -1 }
  { name: 'inventory_in_transit', ttl: -1 }
  { name: 'reconciliation_reports', ttl: -1 }
  { name: 'demand_plan', ttl: -1 }
  { name: 'sales_history', ttl: -1 }
  { name: 'cold_start', ttl: -1 }
  { name: 'planning_meta', ttl: -1 }
  { name: 'replenishment_recs', ttl: -1 }
  { name: 'distribution_plans', ttl: -1 }
  { name: 'ats_cover', ttl: -1 }
  { name: 'baseline_flags', ttl: -1 }
  { name: 'aging_alerts', ttl: -1 }
  { name: 'disposition_recs', ttl: -1 }
  { name: 'metrics_daily', ttl: -1 }
  { name: 'summaries', ttl: -1 }
  { name: 'shipment_facts', ttl: -1 }
]

// ------------------------------------------------------------------ identity + observability

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-agents-id'
  location: location
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 90 }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-appi'
  location: location
  kind: 'web'
  properties: { Application_Type: 'web', WorkspaceResourceId: logs.id }
}

// ------------------------------------------------------------------ Service Bus (event bus + timers)

resource sb 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: '${prefix}-sb-${suffix}'
  location: location
  sku: serviceBusSku == 'Premium' ? { name: 'Premium', tier: 'Premium', capacity: 1 } : { name: 'Standard', tier: 'Standard' }
  properties: { disableLocalAuth: true, minimumTlsVersion: '1.2' }
}

resource sbTopics 'Microsoft.ServiceBus/namespaces/topics@2022-10-01-preview' = [for t in topics: {
  parent: sb
  name: t
  properties: { requiresDuplicateDetection: true, duplicateDetectionHistoryTimeWindow: 'PT1H' }
}]

resource sbSubs 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2022-10-01-preview' = [for p in subscriptionPairs: {
  name: '${sb.name}/${p.topic}/${p.sub}'
  properties: { maxDeliveryCount: 10, deadLetteringOnMessageExpiration: true, lockDuration: 'PT1M' }
  dependsOn: [ sbTopics ]
}]

// ------------------------------------------------------------------ Cosmos DB (operational state)

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: '${prefix}-cosmos-${suffix}'
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    disableLocalAuth: true
    minimalTlsVersion: 'Tls12'
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    locations: [ { locationName: location, failoverPriority: 0 } ]
    capabilities: [ { name: 'EnableServerless' } ]
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmos
  name: 'mcf'
  properties: { resource: { id: 'mcf' } }
}

resource cosmosContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = [for c in containers: {
  parent: cosmosDb
  name: c.name
  properties: {
    resource: {
      id: c.name
      partitionKey: { paths: [ '/id' ], kind: 'Hash' }
      defaultTtl: c.ttl
    }
  }
}]

// Each simulated partner keeps its own state in its own database, reachable only by its own identity.
resource partnerDbs 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = [for p in [ 'corpa', 'corpb' ]: if (enablePartners) {
  parent: cosmos
  name: p
  properties: { resource: { id: p } }
}]

resource partnerState 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = [for (p, i) in [ 'corpa', 'corpb' ]: if (enablePartners) {
  parent: partnerDbs[i]
  name: 'state'
  properties: { resource: { id: 'state', partitionKey: { paths: [ '/id' ], kind: 'Hash' } } }
}]

// CorpB_Insight_Data lives in its own account so Corp A retail identities have no data-plane path (R9.1).
resource insightCosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: '${prefix}-insight-${suffix}'
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    disableLocalAuth: true
    minimalTlsVersion: 'Tls12'
    locations: [ { locationName: location, failoverPriority: 0 } ]
    capabilities: [ { name: 'EnableServerless' } ]
  }
}

// ------------------------------------------------------------------ Storage (feeds, quarantine, exports, audit digests)

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take('${prefix}st${suffix}', 24)
  location: location
  sku: { name: 'Standard_ZRS' }
  kind: 'StorageV2'
  properties: {
    isHnsEnabled: true
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobSvc 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource blobContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for n in [ 'landing', 'quarantine', 'exports', 'reports', 'audit-pending' ]: {
  parent: blobSvc
  name: n
}]

resource auditDigests 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobSvc
  name: 'audit-digests'
}

// WORM: ledger digests cannot be altered or deleted during retention (R10.5)
resource auditDigestsWorm 'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies@2023-05-01' = {
  parent: auditDigests
  name: 'default'
  properties: { immutabilityPeriodSinceCreationInDays: 730, allowProtectedAppendWrites: true }
}

// ------------------------------------------------------------------ Key Vault + App Configuration

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take('${prefix}-kv-${suffix}', 24)
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 90
  }
}

resource appcfg 'Microsoft.AppConfiguration/configurationStores@2023-03-01' = {
  name: '${prefix}-cfg-${suffix}'
  location: location
  sku: { name: 'standard' }
  properties: { disableLocalAuth: true }
}

// ------------------------------------------------------------------ Azure SQL (append-only audit ledger)

resource sql 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: sqlLocation == location ? '${prefix}-sql-${suffix}' : '${prefix}-sql-${uniqueString(resourceGroup().id, sqlLocation)}'
  location: sqlLocation
  properties: {
    minimalTlsVersion: '1.2'
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true
      login: sqlAdminGroupName
      sid: sqlAdminGroupObjectId
      tenantId: subscription().tenantId
      principalType: 'Group'
    }
  }
}

var sqlServerless = sqlSku == 'GP_S_Gen5_2'
resource sqlDb 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent: sql
  name: 'mcf-audit'
  location: sqlLocation
  sku: sqlServerless ? { name: sqlSku, tier: 'GeneralPurpose' } : { name: sqlSku, tier: 'Standard' }
  // every table is a ledger table; DDL in sql/audit.sql makes them append-only
  properties: union({ isLedgerOn: true }, sqlServerless ? { autoPauseDelay: 60, minCapacity: json('0.5') } : {})
}

// Container Apps reach SQL over the Azure backbone. Replace with a private endpoint for the pilot.
resource sqlAllowAzure 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = {
  parent: sql
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

// ------------------------------------------------------------------ Container registry

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: take('${prefix}acr${suffix}', 50)
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// ------------------------------------------------------------------ Event Grid: feed arrival -> Service Bus

resource feedEvents 'Microsoft.EventGrid/systemTopics@2023-12-15-preview' = {
  name: '${prefix}-feed-events'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${uami.id}': {} } }
  properties: { source: storage.id, topicType: 'Microsoft.Storage.StorageAccounts' }
}

resource feedToBus 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2023-12-15-preview' = {
  parent: feedEvents
  name: 'landing-to-servicebus'
  properties: {
    deliveryWithResourceIdentity: {
      identity: { type: 'UserAssigned', userAssignedIdentity: uami.id }
      destination: { endpointType: 'ServiceBusTopic', properties: { resourceId: '${sb.id}/topics/feeds.arrived' } }
    }
    filter: { includedEventTypes: [ 'Microsoft.Storage.BlobCreated' ], subjectBeginsWith: '/blobServices/default/containers/landing/' }
  }
  dependsOn: [ sbTopics, roleSb ]
}

// ------------------------------------------------------------------ Container Apps

var baseEnv = concat([
  { name: 'MCF_CLOUD', value: 'azure' }
  { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
  { name: 'MCF_SERVICEBUS_NAMESPACE', value: '${sb.name}.servicebus.windows.net' }
  { name: 'MCF_COSMOS_URL', value: cosmos.properties.documentEndpoint }
  { name: 'MCF_COSMOS_DB', value: 'mcf' }
  { name: 'MCF_BLOB_URL', value: storage.properties.primaryEndpoints.blob }
  { name: 'MCF_SQL_SERVER', value: sql.properties.fullyQualifiedDomainName }
  { name: 'MCF_SQL_DB', value: 'mcf-audit' }
  { name: 'MCF_KEYVAULT_URL', value: kv.properties.vaultUri }
  { name: 'MCF_APPCONFIG_URL', value: appcfg.properties.endpoint }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appi.properties.ConnectionString }
], enableEmail ? [
  { name: 'MCF_ACS_ENDPOINT', value: 'https://${acs!.properties.hostName}' }
  { name: 'MCF_EMAIL_SENDER', value: 'DoNotReply@${emailDomain!.properties.mailFromSenderDomain}' }
] : [])
// Agents and jobs also know where the partners are; the gateway does not need to (and must not, or the
// partners' webhook URL would depend on themselves).
var commonEnv = concat(baseEnv, enablePartners ? [
  { name: 'MCF_API_MODE', value: 'live' }
  { name: 'MCF_API_BASE_URL', value: 'https://${corpaApp!.properties.configuration.ingress.fqdn}' }
  { name: 'MCF_CORPB_URL', value: 'https://${corpbApp!.properties.configuration.ingress.fqdn}' }
] : [ { name: 'MCF_API_MODE', value: 'sandbox' } ])
// ------------------------------------------------------------------ Email (Azure Communication Services)

resource emailSvc 'Microsoft.Communication/emailServices@2023-04-01' = if (enableEmail) {
  name: '${prefix}-email-${suffix}'
  location: 'global'
  properties: { dataLocation: 'United States' }
}

resource emailDomain 'Microsoft.Communication/emailServices/domains@2023-04-01' = if (enableEmail) {
  parent: emailSvc
  name: 'AzureManagedDomain'
  location: 'global'
  properties: { domainManagement: 'AzureManaged', userEngagementTracking: 'Disabled' }
}

resource acs 'Microsoft.Communication/communicationServices@2023-04-01' = if (enableEmail) {
  name: '${prefix}-acs-${suffix}'
  location: 'global'
  properties: { dataLocation: 'United States', linkedDomains: [ emailDomain.id ] }
}

// The agents send email with their managed identity.
resource roleAcs 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableEmail) {
  scope: acs
  name: guid(acs.id, uami.id, 'contributor')
  properties: { principalId: uami.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c') }
}

var registries = [ { server: acr.properties.loginServer, identity: uami.id } ]

resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-cae'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: { customerId: logs.properties.customerId, sharedKey: logs.listKeys().primarySharedKey }
    }
  }
}

resource apps 'Microsoft.App/containerApps@2024-03-01' = [for a in agentApps: {
  name: '${prefix}-${a}'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${uami.id}': {} } }
  properties: {
    managedEnvironmentId: cae.id
    configuration: { registries: registries }
    template: {
      containers: [ {
        name: a
        image: image
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: concat(commonEnv, [ { name: 'MCF_AGENT', value: a } ])
      } ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
  dependsOn: [ roleAcr ]
}]

// Scheduled agent work (daily reconciliation, planning, aging, metrics; weekly and quarterly summaries). Times are UTC.
var jobs = [
  { name: 'reconciliation_daily', cron: '0 6 * * *' }
  { name: 'planning_daily', cron: '0 7 * * *' }
  { name: 'disposition_daily', cron: '30 7 * * *' }
  { name: 'metrics_daily', cron: '0 8 * * *' }
  { name: 'weekly_summary', cron: '0 9 * * 1' }
  { name: 'qbr_summary', cron: '0 10 1 1,4,7,10 *' }
]

resource scheduledJobs 'Microsoft.App/jobs@2024-03-01' = [for j in jobs: {
  name: '${prefix}-${replace(j.name, '_', '-')}'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${uami.id}': {} } }
  properties: {
    environmentId: cae.id
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 3600
      replicaRetryLimit: 1
      scheduleTriggerConfig: { cronExpression: j.cron, parallelism: 1, replicaCompletionCount: 1 }
      registries: registries
    }
    template: {
      containers: [ {
        name: 'job'
        image: image
        command: [ 'python', '-m', 'tower.main', 'job', j.name ]
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: commonEnv
      } ]
    }
  }
  dependsOn: [ roleAcr ]
}]

// ------------------------------------------------------------------ Partner gateway and partner simulators

// The placeholder image serves on port 80 and has no Python; real images serve on 8080.
var agentsPlaceholder = startsWith(image, 'mcr.microsoft.com/')
var simPlaceholder = startsWith(simImage, 'mcr.microsoft.com/')

// Front door for Corp B orders and Corp A webhooks. Authenticates partners and publishes to the bus.
resource gatewayApp 'Microsoft.App/containerApps@2024-03-01' = if (enablePartners) {
  name: '${prefix}-gateway'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${uami.id}': {} } }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      registries: registries
      ingress: { external: true, targetPort: agentsPlaceholder ? 80 : 8080, transport: 'auto', allowInsecure: false }
    }
    template: {
      containers: [ {
        name: 'gateway'
        image: image
        command: agentsPlaceholder ? null : [ 'python', '-m', 'tower.edge.gateway' ]
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
        env: concat(baseEnv, [ { name: 'PORT', value: '8080' } ])
      } ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
  dependsOn: [ roleAcr ]
}

resource corpaId 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (enablePartners) {
  name: '${prefix}-corpa-id'
  location: location
}

resource corpbId 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (enablePartners) {
  name: '${prefix}-corpb-id'
  location: location
}

// Corp A: the MCF API and the fulfillment network behind it. One replica: it is the single writer of its inventory.
resource corpaApp 'Microsoft.App/containerApps@2024-03-01' = if (enablePartners) {
  name: '${prefix}-corpa-mcf'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${corpaId.id}': {} } }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      registries: [ { server: acr.properties.loginServer, identity: corpaId.id } ]
      ingress: { external: true, targetPort: simPlaceholder ? 80 : 8080, transport: 'auto', allowInsecure: false }
    }
    template: {
      containers: [ {
        name: 'corpa'
        image: simImage
        command: simPlaceholder ? null : [ 'python', '-m', 'sim.corpa' ]
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
        env: [
          { name: 'AZURE_CLIENT_ID', value: corpaId!.properties.clientId }
          { name: 'SIM_KEYVAULT_URL', value: kv.properties.vaultUri }
          { name: 'SIM_COSMOS_URL', value: cosmos.properties.documentEndpoint }
          { name: 'SIM_COSMOS_DB', value: 'corpa' }
          { name: 'CORPA_WEBHOOK_URL', value: 'https://${gatewayApp!.properties.configuration.ingress.fqdn}/v1/mcf/notifications' }
        ]
      } ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [ roleCorpaAcr, partnerState ]
}

// Corp B: order management (sends orders, receives updates) and the weekly planning files.
resource corpbApp 'Microsoft.App/containerApps@2024-03-01' = if (enablePartners) {
  name: '${prefix}-corpb-oms'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${corpbId.id}': {} } }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      registries: [ { server: acr.properties.loginServer, identity: corpbId.id } ]
      ingress: { external: true, targetPort: simPlaceholder ? 80 : 8080, transport: 'auto', allowInsecure: false }
    }
    template: {
      containers: [ {
        name: 'corpb'
        image: simImage
        command: simPlaceholder ? null : [ 'python', '-m', 'sim.corpb' ]
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
        env: [
          { name: 'AZURE_CLIENT_ID', value: corpbId!.properties.clientId }
          { name: 'SIM_KEYVAULT_URL', value: kv.properties.vaultUri }
          { name: 'SIM_COSMOS_URL', value: cosmos.properties.documentEndpoint }
          { name: 'SIM_COSMOS_DB', value: 'corpb' }
          { name: 'CORPB_GATEWAY_URL', value: 'https://${gatewayApp!.properties.configuration.ingress.fqdn}' }
          { name: 'CORPB_LANDING_URL', value: storage.properties.primaryEndpoints.blob }
          { name: 'CORPB_ORDERS_PER_DAY', value: corpbOrdersPerDay }
        ]
      } ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [ roleCorpbAcr, partnerState ]
}

resource landing 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  parent: blobSvc
  name: 'landing'
}

resource roleCorpaAcr 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enablePartners) {
  scope: acr
  name: guid(acr.id, '${prefix}-corpa-id', roles.acrPull)
  properties: { principalId: corpaId!.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull) }
}

resource roleCorpbAcr 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enablePartners) {
  scope: acr
  name: guid(acr.id, '${prefix}-corpb-id', roles.acrPull)
  properties: { principalId: corpbId!.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull) }
}

// Shared partner credentials (OAuth client, webhook secret, API keys) are seeded by the deploy workflow.
resource roleCorpaKv 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enablePartners) {
  scope: kv
  name: guid(kv.id, '${prefix}-corpa-id', roles.keyVaultSecretsUser)
  properties: { principalId: corpaId!.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser) }
}

resource roleCorpbKv 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enablePartners) {
  scope: kv
  name: guid(kv.id, '${prefix}-corpb-id', roles.keyVaultSecretsUser)
  properties: { principalId: corpbId!.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser) }
}

// Corp B drops planning files into the landing container only.
resource roleCorpbLanding 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enablePartners) {
  scope: landing
  name: guid(storage.id, 'landing', '${prefix}-corpb-id', roles.storageBlobDataContributor)
  properties: { principalId: corpbId!.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor) }
  dependsOn: [ blobContainers ]
}

resource roleCorpaCosmos 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = if (enablePartners) {
  parent: cosmos
  name: guid(cosmos.id, '${prefix}-corpa-id', 'corpa-db')
  properties: {
    principalId: corpaId!.properties.principalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: '${cosmos.id}/dbs/corpa'
  }
}

resource roleCorpbCosmos 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = if (enablePartners) {
  parent: cosmos
  name: guid(cosmos.id, '${prefix}-corpb-id', 'corpb-db')
  properties: {
    principalId: corpbId!.properties.principalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: '${cosmos.id}/dbs/corpb'
  }
  dependsOn: [ roleCorpaCosmos ] // Cosmos accepts one role assignment write at a time
}

// ------------------------------------------------------------------ RBAC for the agents' managed identity

var roles = {
  serviceBusDataOwner: '090c5cfd-751d-490a-894a-3ce6f1109419'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  appConfigDataReader: '516239f1-63e1-4d78-a4de-a74fb236a071'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
  appConfigDataOwner: '5ae67dd6-50cb-40e7-96ff-dc2bfa4b606b'
}

resource roleAcr 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, uami.id, roles.acrPull)
  properties: { principalId: uami.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull) }
}

resource roleDeployerCfg 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  scope: appcfg
  name: guid(appcfg.id, deployerPrincipalId, roles.appConfigDataOwner)
  properties: { principalId: deployerPrincipalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.appConfigDataOwner) }
}

resource roleDeployerKv 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  scope: kv
  name: guid(kv.id, deployerPrincipalId, roles.keyVaultSecretsOfficer)
  properties: { principalId: deployerPrincipalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsOfficer) }
}

resource roleSb 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: sb
  name: guid(sb.id, uami.id, roles.serviceBusDataOwner)
  properties: { principalId: uami.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataOwner) }
}

resource roleBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, uami.id, roles.storageBlobDataContributor)
  properties: { principalId: uami.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor) }
}

resource roleKv 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, uami.id, roles.keyVaultSecretsUser)
  properties: { principalId: uami.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser) }
}

resource roleCfg 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appcfg
  name: guid(appcfg.id, uami.id, roles.appConfigDataReader)
  properties: { principalId: uami.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.appConfigDataReader) }
}

// Cosmos data-plane RBAC (built-in Data Contributor) on the operational account only.
// Insight account access is granted per pilot agent identity, not to this shared identity.
resource cosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmos
  name: guid(cosmos.id, uami.id, 'data-contributor')
  properties: {
    principalId: uami.properties.principalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: cosmos.id
  }
}

output serviceBusNamespace string = '${sb.name}.servicebus.windows.net'
output cosmosUrl string = cosmos.properties.documentEndpoint
output blobUrl string = storage.properties.primaryEndpoints.blob
output sqlServer string = sql.properties.fullyQualifiedDomainName
output keyVaultUrl string = kv.properties.vaultUri
output appConfigUrl string = appcfg.properties.endpoint
output appConfigName string = appcfg.name
output agentsIdentityClientId string = uami.properties.clientId
output keyVaultName string = kv.name
output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output sqlServerName string = sql.name
output sqlLocation string = sqlLocation
output emailSender string = enableEmail ? 'DoNotReply@${emailDomain!.properties.mailFromSenderDomain}' : ''
output agentsIdentityName string = uami.name
output containerAppNames array = [for a in agentApps: '${prefix}-${a}']
output gatewayUrl string = enablePartners ? 'https://${gatewayApp!.properties.configuration.ingress.fqdn}' : ''
output corpaUrl string = enablePartners ? 'https://${corpaApp!.properties.configuration.ingress.fqdn}' : ''
output corpbUrl string = enablePartners ? 'https://${corpbApp!.properties.configuration.ingress.fqdn}' : ''
