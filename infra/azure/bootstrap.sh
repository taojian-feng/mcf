#!/usr/bin/env bash
# One-time setup for the MCF dev environment. Run it in Azure Cloud Shell (Bash) as the subscription owner:
#
#   curl -fsSL https://raw.githubusercontent.com/taojian-feng/mcf/main/infra/azure/bootstrap.sh | bash
#
# or, from a clone:  bash infra/azure/bootstrap.sh
#
# It creates nothing that costs money. It registers resource providers, creates the resource group,
# an Entra app the GitHub deploy workflow signs in as (OIDC, no stored password), and the Entra group
# that administers the audit database. It prints the values to add to GitHub at the end.
# Safe to re-run: every step checks for what already exists.
set -euo pipefail

REPO="${REPO:-taojian-feng/mcf}"
GH_REF="${GH_REF:-refs/heads/main}"   # the workflows sign in from this branch
LOCATION="${LOCATION:-eastus2}"
RG="${RG:-mcfpilot-dev-rg}"
APP_NAME="${APP_NAME:-mcfpilot-github-deploy}"
SQL_ADMIN_GROUP="${SQL_ADMIN_GROUP:-mcfpilot-sql-admins}"

if [[ -n "${SUBSCRIPTION_ID:-}" ]]; then az account set --subscription "$SUBSCRIPTION_ID"; fi
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
TENANT_ID="$(az account show --query tenantId -o tsv)"
echo "Subscription: $(az account show --query name -o tsv) ($SUBSCRIPTION_ID)"
echo "Region: $LOCATION   Resource group: $RG   Repo: $REPO"

echo "==> Registering resource providers (first time can take a few minutes)"
PROVIDERS=(Microsoft.App Microsoft.ServiceBus Microsoft.DocumentDB Microsoft.Sql Microsoft.Storage
           Microsoft.KeyVault Microsoft.AppConfiguration Microsoft.EventGrid Microsoft.OperationalInsights
           Microsoft.Insights Microsoft.ManagedIdentity Microsoft.ContainerRegistry Microsoft.Communication
           Microsoft.CognitiveServices)
for p in "${PROVIDERS[@]}"; do az provider register --namespace "$p" --output none; done
for p in "${PROVIDERS[@]}"; do
  until [[ "$(az provider show --namespace "$p" --query registrationState -o tsv)" == "Registered" ]]; do
    echo "    waiting for $p"; sleep 10
  done
done

echo "==> Resource group"
az group create --name "$RG" --location "$LOCATION" --tags project=mcf env=dev --output none
RG_ID="$(az group show --name "$RG" --query id -o tsv)"

echo "==> Entra app for GitHub Actions (OIDC)"
APP_ID="$(az ad app list --display-name "$APP_NAME" --query '[0].appId' -o tsv)"
if [[ -z "$APP_ID" ]]; then APP_ID="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"; fi
SP_OBJECT_ID="$(az ad sp list --filter "appId eq '$APP_ID'" --query '[0].id' -o tsv)"
if [[ -z "$SP_OBJECT_ID" ]]; then SP_OBJECT_ID="$(az ad sp create --id "$APP_ID" --query id -o tsv)"; fi

# GitHub sends one of two subject forms: by name, or (newer repos) with immutable owner and repo ids.
# Register both so the deploy signs in whichever this repo uses. The ids come from the GitHub API
# (OWNER_ID / REPO_NUM override it, e.g. for a private repo the anonymous API can't see).
SUBJECTS=("repo:${REPO}:ref:${GH_REF}")
if [[ -z "${OWNER_ID:-}" || -z "${REPO_NUM:-}" ]]; then
  REPO_META="$(curl -fsS "https://api.github.com/repos/${REPO}" || true)"
  if [[ -n "$REPO_META" ]]; then OWNER_ID="$(jq -r .owner.id <<<"$REPO_META")"; REPO_NUM="$(jq -r .id <<<"$REPO_META")"; fi
fi
if [[ -n "${OWNER_ID:-}" && -n "${REPO_NUM:-}" ]]; then
  SUBJECTS+=("repo:${REPO%%/*}@${OWNER_ID}/${REPO#*/}@${REPO_NUM}:ref:${GH_REF}")
else
  echo "    Could not look up the repo ids; set OWNER_ID and REPO_NUM if sign-in fails with AADSTS700213."
fi
i=0
for SUBJECT in "${SUBJECTS[@]}"; do
  i=$((i + 1))
  if [[ -z "$(az ad app federated-credential list --id "$APP_ID" --query "[?subject=='$SUBJECT'].name" -o tsv)" ]]; then
    az ad app federated-credential create --id "$APP_ID" --output none --parameters "{
      \"name\": \"github-main-${i}\",
      \"issuer\": \"https://token.actions.githubusercontent.com\",
      \"subject\": \"${SUBJECT}\",
      \"audiences\": [\"api://AzureADTokenExchange\"]
    }"
  fi
done

echo "==> Roles for the deploy app, scoped to the resource group only"
# Contributor creates resources; RBAC Administrator lets the template grant the agents' identity its data roles.
for role in "Contributor" "Role Based Access Control Administrator"; do
  for attempt in 1 2 3 4 5 6; do  # a new service principal can take a minute to replicate
    if az role assignment create --assignee-object-id "$SP_OBJECT_ID" --assignee-principal-type ServicePrincipal \
         --role "$role" --scope "$RG_ID" --output none 2>/dev/null; then break; fi
    [[ $attempt == 6 ]] && { echo "Could not assign $role"; exit 1; }
    sleep 10
  done
done

echo "==> Entra group that administers the audit database"
GROUP_ID="$(az ad group list --display-name "$SQL_ADMIN_GROUP" --query '[0].id' -o tsv)"
if [[ -z "$GROUP_ID" ]]; then
  GROUP_ID="$(az ad group create --display-name "$SQL_ADMIN_GROUP" --mail-nickname "$SQL_ADMIN_GROUP" --query id -o tsv)"
fi
ME="$(az ad signed-in-user show --query id -o tsv)"
for member in "$ME" "$SP_OBJECT_ID"; do
  if [[ "$(az ad group member check --group "$GROUP_ID" --member-id "$member" --query value -o tsv)" != "true" ]]; then
    az ad group member add --group "$GROUP_ID" --member-id "$member"
  fi
done

cat <<OUT

Done. Add these in GitHub: repo Settings > Secrets and variables > Actions.

  Secrets
    AZURE_CLIENT_ID          $APP_ID
    AZURE_TENANT_ID          $TENANT_ID
    AZURE_SUBSCRIPTION_ID    $SUBSCRIPTION_ID

  Variables
    AZURE_RESOURCE_GROUP     $RG
    AZURE_DEPLOYER_OBJECT_ID $SP_OBJECT_ID
    SQL_ADMIN_GROUP_ID       $GROUP_ID
    SQL_ADMIN_GROUP_NAME     $SQL_ADMIN_GROUP

None of these are passwords; the workflow signs in with a short-lived GitHub OIDC token.
OUT
