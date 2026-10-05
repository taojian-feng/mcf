# Deploying the dev environment to Azure

This puts the whole system in one resource group: every agent in one Container App, the scheduled jobs as
Container Apps Jobs, and the data services from [ARCHITECTURE.md](ARCHITECTURE.md). It runs against an
in-process sandbox MCF API until a real one exists, and logs notifications to stdout until email is set up.

## 1. Get a subscription

1. Go to <https://azure.microsoft.com/free> and sign up. You get $200 of credit for 30 days. A card is needed
   for identity checks; it isn't charged on the free tier.
2. When it's ready, open <https://portal.azure.com>, search **Subscriptions** and copy the **Subscription ID**.
3. Upgrade to **pay-as-you-go** (Subscriptions > your subscription > Upgrade). The free tier blocks some of the
   services here and caps quotas. You keep any unused credit when you upgrade.
4. Optional but recommended: Cost Management > Budgets > add a monthly budget of $150 with an email alert.

## 2. What it costs (dev sizing, East US 2, rough)

| Service | Sizing | Per month |
|---|---|---|
| Container Apps | 1 app at 0.5 vCPU / 1 GiB always on, 6 short daily jobs | ~$40 |
| App Configuration | Standard | ~$36 |
| Service Bus | Standard | ~$10 |
| Azure SQL (audit ledger) | Serverless, auto-pauses after an hour idle | ~$5–15 |
| Cosmos DB (2 accounts) | Serverless | ~$1–5 at dev volume |
| Container Registry, Key Vault, Storage, Log Analytics | Basic tiers | ~$10 |
| **Total** | | **about $100** |

For production, set `serviceBusSku=Premium` (about $700) and split `agentApps` to one app per agent.

## 3. Providers and quotas

`bootstrap.sh` registers these resource providers for you: `Microsoft.App`, `ServiceBus`, `DocumentDB`, `Sql`,
`Storage`, `KeyVault`, `AppConfiguration`, `EventGrid`, `OperationalInsights`, `Insights`, `ManagedIdentity`,
`ContainerRegistry`.

Default quotas on a pay-as-you-go subscription cover this deployment. The ones that most often get in the way:

- **Region capacity for Azure SQL or Cosmos DB.** New subscriptions are sometimes refused in busy regions with
  "region is not accepting new customers" or "subscription is not allowed to create in this region". Re-run the
  bootstrap with another region, for example `LOCATION=centralus`, and delete the old resource group.
- **Container Apps.** Uses about 0.5 vCPU of the default 100-core-per-environment allowance.

## 4. Choose how deployments run

| | GitHub Actions with OIDC (recommended) | From your own machine |
|---|---|---|
| How | You run a setup script once; every deploy is a button in GitHub Actions | You run `az` commands with your own login |
| Credentials | None stored. GitHub gets a short-lived token per run | Your user account |
| Access | Limited to one resource group | Everything you can do |
| Repeatable | Yes, every run is logged in Actions | Depends on who runs it |

The rest of this guide uses GitHub Actions.

## 5. One-time setup (about 10 minutes)

1. Open **Cloud Shell** from the portal toolbar (the `>_` icon) and choose **Bash**.
2. Run:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/taojian-feng/mcf/main/infra/azure/bootstrap.sh | bash
   ```

   Options go in front as environment variables, for example
   `LOCATION=centralus RG=mcfpilot-dev-rg SUBSCRIPTION_ID=<id> bash infra/azure/bootstrap.sh`.

   It registers providers, creates the resource group, an Entra app that GitHub signs in as (federated with
   `repo:taojian-feng/mcf:environment:azure-dev`, scoped to the resource group with Contributor and RBAC
   Administrator), and the Entra group `mcfpilot-sql-admins` that administers the audit database. It creates
   nothing billable, and it is safe to re-run.
3. In GitHub, open the repo's **Settings > Secrets and variables > Actions** and add what the script printed:
   - Secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
   - Variables: `AZURE_RESOURCE_GROUP`, `AZURE_DEPLOYER_OBJECT_ID`, `SQL_ADMIN_GROUP_ID`, `SQL_ADMIN_GROUP_NAME`

## 6. Deploy

GitHub **Actions > Deploy to Azure (dev) > Run workflow**. A first run takes 15 to 25 minutes, most of it Cosmos DB
and SQL provisioning. The workflow:

1. Deploys `infra/azure/main.bicep`, keeping whatever image is already running (a placeholder the first time).
2. Builds the image from `Dockerfile` and pushes it to the new Container Registry.
3. Creates the `pii-masking-key` secret in Key Vault if it is missing.
4. Seeds App Configuration with `mcf:pilot_config` (the defaults in `mcf/core/config.py`), `mcf:roles`,
   `mcf:contacts` and `mcf:pilot_authorization_list`. It never overwrites keys that already exist, so edits you
   make in the portal stick.
5. Creates the append-only audit ledger tables and the agents' database user (`infra/azure/sql/audit.sql`),
   opening the SQL firewall to the runner's IP only for that step.
6. Rolls the new image out to the app and jobs.
7. Runs the reconciliation job once and fails the run if it doesn't succeed.

Re-run it for every change you want deployed.

## 7. After the first deploy

- **Who can approve.** Edit `mcf:roles` in App Configuration (portal > the `mcfpilot-cfg-…` resource >
  Configuration explorer), for example `{"you@example.com": ["approver:disposition:corp_b", "approver:distribution_plan:corp_b"]}`.
  Contacts (for example `exception_operator`, `corp_b_inventory`) go in `mcf:contacts`. Also add approvers and
  agent names to `mcf:pilot_authorization_list` so they can see Corp B insight data.
- **Logs.** Portal > the `mcfpilot-all` Container App > Log stream, or Log Analytics with
  `ContainerAppConsoleLogs_CL | order by TimeGenerated desc`. Notifications show up here as JSON lines until email
  is configured.
- **Claude.** Optional for now; the agents run without it. To turn it on, deploy Claude in Microsoft Foundry, put
  its key in Key Vault as `foundry-api-key`, and set `MCF_FOUNDRY_RESOURCE` on the app.
- **Email.** Optional. Create an Azure Communication Services email resource and set `MCF_ACS_ENDPOINT` and
  `MCF_EMAIL_SENDER`.
- **Real MCF API.** Set the app's `mcfApiMode` parameter to `live`, `MCF_API_BASE_URL`, and the Key Vault secret
  `mcf-access-token`.

## 8. Tear down

```bash
az group delete -n mcfpilot-dev-rg --yes
```

Key Vault keeps deleted vaults for 90 days with purge protection on, so a fresh deploy into a resource group with
the same name collides with the old vault name. Use a new resource group name (`RG=mcfpilot-dev2-rg`) for the next
bootstrap.
