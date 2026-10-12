# Logistics Control Tower

A multi-agent control tower for Corp A, a logistics provider that fulfills online and offline demand for retail clients. Corp B is the first client; orders reach Corp A's Multi-Channel Fulfillment (MCF) API. The direction is set out in the [design review](https://claude.ai/code/artifact/2dcfb7c4-a6a2-4bfa-9802-7bb6a2d89361). The core is cloud-agnostic, Azure is the first cloud, and GCP and AWS come later as adapter packages. The design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Run it locally

```bash
pip install pydantic pytest
python -m pytest            # runs the order-to-exception slice in memory with virtual time
```

## Deploy to Azure

See [docs/DEPLOY_AZURE.md](docs/DEPLOY_AZURE.md): a one-time Cloud Shell setup script, then a manual GitHub Actions workflow.

## What exists today (v0.2)

| Area | Status | Requirements covered |
|---|---|---|
| Ports + in-memory adapters | done | foundation for all |
| Retail clients (tenants) | done: client registry, tenant on every record, per-client contract, people and data fence | roadmap step 4 |
| Stores (offline channel) | done: store orders, allocation that protects web stock, B2B freight, advance ship notices; Corp C simulator | roadmap step 5 |
| Returns | done: return policy, RMA and label, tracking, grading, refund decision, disposition, label expiry | roadmap step 6 |
| Operator copilot and briefs | done: a brief on every approval (Claude, rules fallback); "Ask the tower" page with read-only, per-client tools; copilot evaluation | roadmap step 7 |
| Client onboarding and billing | done: onboarding agent (checks against the network, Corp A approval, derived keys, welcome, go-live tracking), catalog upload API, Corp A's rate card, daily charges, monthly invoices approved by Corp A | roadmap step 8 |
| Planning for every client | done: demand plans, restock, rebalancing, aged-stock decisions, daily metrics and weekly/quarterly reviews per client, each inside its own data fence; Corp C sends forecasts and sales | after step 8 |
| Planning judgment | built, off by default: recent orders vs plan per item; demand calls and reviews of restocks, rebalancing and aged stock by rules, a fixed blend, or Claude (`plan_review`); planning evaluation | after step 8 |
| Audit_Service | done | R10.1, R10.2, R10.3, R10.5, R10.6, R10.7 |
| Approval_Workflow | done | R8.1 to R8.8 |
| Data_Governance_Service | done (isolation + masking) | R9.1, R9.2, R9.4, R9.6, R9.7, R9.8 |
| Integration_Layer | done (MCF calls, retry, feed validation) | R7.1 to R7.7 |
| Handoff tracker | done | R11.1 to R11.8 |
| Orchestration_Agent | done | R5.1 to R5.10 |
| Exception_Agent | done (investigates the order record with Claude, rules fallback, 6 playbooks) | R3.1 to R3.10 |
| Reconciliation_Agent | done (event-sourced inventory, FIFO lots, point-in-time replay) | R2.1 to R2.8 |
| Planning_Agent | done (forecast/sales/cold-start feeds, ATS cover, replenishment, imbalance, distribution plan) | R1.1 to R1.9 |
| Disposition_Agent | done (aging + storage cost, recommendations, completion tracking) | R4.1 to R4.9 |
| Reporting_Agent | done (10 metrics, 4-week baselines, CSV export, weekly + QBR summaries) | R6.1 to R6.7 |
| Azure adapters | running in Azure dev | Service Bus, Cosmos, Blob, SQL ledger, Key Vault, App Configuration, ACS email, Foundry |
| Azure infra | `infra/azure/main.bicep` compiles (incl. Container Apps Jobs for daily/weekly/quarterly work); `sql/audit.sql` | |

Scheduled work runs as `python -m tower.main job <name>` (reconciliation_daily, planning_daily, disposition_daily, metrics_daily, weekly_summary, qbr_summary).

Simplifications to revisit with the pilot team: demand is modeled per UPC x region per week; replenishment sources FCs from the IXD first, then an ISC; disposition picks the cheaper of Rebound return or removal; replenishment accuracy compares recommended quantity with the next week's actual sales.

Not yet covered: PII retention purge job (R9.5 is set up via Cosmos TTL on `order_pii`), encryption-at-rest specifics (R9.3, platform default plus TLS), audit retention policy (R10.4).

## Layout

```
tower/core        models, ports (interfaces), topics, config       no cloud imports
tower/services    audit, approval, governance, integration, handoffs
tower/agents      orchestration, exception, reconciliation, planning, disposition, reporting
tower/adapters    memory/ (local + tests), azure/, http_mcf.py
tower/runtime.py  wires ports to adapters (MCF_CLOUD=memory|azure)
infra/azure     main.bicep, sql/audit.sql
tests           test_slice.py (order-to-exception), test_agents.py (the other four agents)
```

## Adding GCP or AWS

Implement the ports in `tower/core/ports.py` under `tower/adapters/gcp` or `tower/adapters/aws` and add a branch in `runtime.from_env`. No agent or service code changes. The port-to-service mapping per cloud is in ARCHITECTURE.md section 3.
