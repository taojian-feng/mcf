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
| Container Apps | agents app (0.5 vCPU), partner gateway and two partner simulators (0.25 vCPU each), all always on; 6 short daily jobs | ~$100 |
| App Configuration | Standard | ~$36 |
| Service Bus | Standard | ~$10 |
| Azure SQL (audit ledger) | Standard S0 (partner traffic keeps it busy, so serverless would never pause) | ~$15 |
| Cosmos DB (2 accounts) | Serverless | ~$5–15 with simulated traffic |
| Container Registry, Key Vault, Storage, Log Analytics | Basic tiers | ~$15 |
| **Total** | | **about $180** |

Without the partner simulators (`enablePartners=false`) it is about $100. For production, set `serviceBusSku=Premium` (about $700) and split `agentApps` to one app per agent.

## 3. Providers and quotas

`bootstrap.sh` registers these resource providers for you: `Microsoft.App`, `ServiceBus`, `DocumentDB`, `Sql`,
`Storage`, `KeyVault`, `AppConfiguration`, `EventGrid`, `OperationalInsights`, `Insights`, `ManagedIdentity`,
`ContainerRegistry`, `Communication`.

Default quotas on a pay-as-you-go subscription cover this deployment. The ones that most often get in the way:

- **Region capacity for Azure SQL or Cosmos DB.** New subscriptions are sometimes refused in busy regions with
  "region is not accepting new customers" or "Location ... is not accepting creation of new ... SQL Database
  servers". For SQL the workflow handles this itself: it tries the resource group's region, then `centralus`,
  `westus3`, `northcentralus` and `southcentralus`, and keeps the audit database wherever it lands. You can also
  pick a region in the workflow's `sql_location` box. For Cosmos DB, re-run the bootstrap with another region,
  for example `LOCATION=centralus RG=mcfpilot-dev2-rg`.
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

   If the repo is private, that link needs a sign-in: run `gh auth login` in Cloud Shell, then
   `gh repo clone taojian-feng/mcf && OWNER_ID=166564305 REPO_NUM=1406376717 bash mcf/infra/azure/bootstrap.sh`.

   Options go in front as environment variables, for example
   `LOCATION=centralus RG=mcfpilot-dev-rg SUBSCRIPTION_ID=<id> bash infra/azure/bootstrap.sh`.

   It registers providers, creates the resource group, an Entra app that GitHub signs in as (federated with
   pushes and manual runs on `main`, scoped to the resource group with Contributor and RBAC
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

### People: who approves and who gets alerts

Everything lives in one file, [`config/pilot.yaml`](../config/pilot.yaml). To start, set `default_email` to
your address and leave the rest blank: you then approve everything and get every alert. Fill in other slots as
real owners are named. Lists take several emails, for example `disposition: [ann@corpb.com, raj@corpb.com]`.

Edit it on GitHub (open the file, click the pencil, commit to `main`). The **Sync pilot people (dev)** workflow
then copies it into Azure within a couple of minutes, and every full deploy does the same. Changes made by hand in
the portal to `mcf:roles`, `mcf:contacts` or `mcf:pilot_authorization_list` are overwritten by the file.

If the repository is public and you'd rather keep emails out of it, put the same YAML in a repository secret
named `PILOT_PEOPLE`. The workflows use the secret instead of the file whenever it is set.

### Email

On by default. The deploy creates an Azure Communication Services email sender with a free Azure-managed
domain (`DoNotReply@<random>.azurecomm.net`), so alerts go out as email with no setup. Anyone with no address
in the people file is logged instead (portal > the `mcfpilot-all` Container App > Log stream). Set the Bicep
parameter `enableEmail=false` to log only. Azure-managed domains are rate limited and land in spam more often;
for the pilot, connect your own domain in the portal (Email Communication Services > Provision domains).

### Logs

Portal > the `mcfpilot-all` Container App > Log stream, or Log Analytics with
`ContainerAppConsoleLogs_CL | order by TimeGenerated desc`.

### Partners: Corp A and Corp B

The deploy runs both partner companies as simulators, each in its own Container App with its own identity and
database, talking to the system only over HTTPS (contract and behaviour: [PARTNER_APIS.md](PARTNER_APIS.md)):

- **Corp B** (`mcfpilot-corpb-oms`) sends its item catalog and planning files at go-live, starts ordering about
  15 minutes later (about 140 orders a day, `corpbOrdersPerDay`), sends a forecast an hour in, and new forecast
  and sales files every Monday.
- **Corp A** (`mcfpilot-corpa-mcf`) is the MCF API. Once the catalog is listed with it, it receives opening
  stock, then accepts, packs, ships and delivers orders on realistic timings, with the occasional late,
  lost or undeliverable parcel, nightly stock counts, and an ops team that works the system's handoffs.
- **The partner gateway** (`mcfpilot-gateway`) is how both get in: Corp B with an API key, Corp A with signed
  webhooks.

The shared credentials are created once in Key Vault by the deploy. What you will notice is email: daily
reconciliation, exceptions that need a person, handoff escalations and approvals.

To pause the partners, scale both simulators to zero (portal > Container App > Scale, or
`az containerapp update -g mcfpilot-dev-rg -n mcfpilot-corpb-oms --min-replicas 0 --max-replicas 0`, then the same
for `mcfpilot-corpa-mcf`). To remove them, deploy with `enablePartners=false`. To connect a real Corp A later, point
`MCF_API_BASE_URL` at it and put its OAuth client in `mcf-client-id` / `mcf-client-secret`.

### Still off

- **Claude.** The agents run on deterministic rules without it. Turning it on needs Claude deployed in
  Microsoft Foundry on this subscription; put its key in Key Vault as `foundry-api-key` and set
  `MCF_FOUNDRY_RESOURCE` on the app.
- **Approving in a browser.** Approval requests arrive by email, but there is no page to approve them on yet.

## 8. Tear down

```bash
az group delete -n mcfpilot-dev-rg --yes
```

Key Vault keeps deleted vaults for 90 days with purge protection on, so a fresh deploy into a resource group with
the same name collides with the old vault name. Use a new resource group name (`RG=mcfpilot-dev2-rg`) for the next
bootstrap.
