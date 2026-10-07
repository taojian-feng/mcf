# MCF Agentic Fulfillment Orchestration

A multi-agent system for the Corp B x Corp A Multi-Channel Fulfillment pilot. The core is cloud-agnostic, Azure is the first cloud, and GCP and AWS come later as adapter packages. The design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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
| Audit_Service | done | R10.1, R10.2, R10.3, R10.5, R10.6, R10.7 |
| Approval_Workflow | done | R8.1 to R8.8 |
| Data_Governance_Service | done (isolation + masking) | R9.1, R9.2, R9.4, R9.6, R9.7, R9.8 |
| Integration_Layer | done (MCF calls, retry, feed validation) | R7.1 to R7.7 |
| Handoff tracker | done | R11.1 to R11.8 |
| Orchestration_Agent | done | R5.1 to R5.10 |
| Exception_Agent | done (rules classifier + Claude on Foundry hook, 6 playbooks) | R3.1 to R3.10 |
| Reconciliation_Agent | done (event-sourced inventory, FIFO lots, point-in-time replay) | R2.1 to R2.8 |
| Planning_Agent | done (forecast/sales/cold-start feeds, ATS cover, replenishment, imbalance, distribution plan) | R1.1 to R1.9 |
| Disposition_Agent | done (aging + storage cost, recommendations, completion tracking) | R4.1 to R4.9 |
| Reporting_Agent | done (10 metrics, 4-week baselines, CSV export, weekly + QBR summaries) | R6.1 to R6.7 |
| Azure adapters | written, not yet run against Azure | Service Bus, Cosmos, Blob, SQL ledger, Key Vault, App Configuration, ACS email, Foundry |
| Azure infra | `infra/azure/main.bicep` compiles (incl. Container Apps Jobs for daily/weekly/quarterly work); `sql/audit.sql` | |

Scheduled work runs as `python -m mcf.main job <name>` (reconciliation_daily, planning_daily, disposition_daily, metrics_daily, weekly_summary, qbr_summary).

Simplifications to revisit with the pilot team: demand is modeled per UPC x region per week; replenishment sources FCs from the IXD first, then an ISC; disposition picks the cheaper of Rebound return or removal; replenishment accuracy compares recommended quantity with the next week's actual sales.

Not yet covered: PII retention purge job (R9.5 is set up via Cosmos TTL on `order_pii`), encryption-at-rest specifics (R9.3, platform default plus TLS), audit retention policy (R10.4).

## Layout

```
mcf/core        models, ports (interfaces), topics, config       no cloud imports
mcf/services    audit, approval, governance, integration, handoffs
mcf/agents      orchestration, exception, reconciliation, planning, disposition, reporting
mcf/adapters    memory/ (local + tests), azure/, http_mcf.py
mcf/runtime.py  wires ports to adapters (MCF_CLOUD=memory|azure)
infra/azure     main.bicep, sql/audit.sql
tests           test_slice.py (order-to-exception), test_agents.py (the other four agents)
```

## Adding GCP or AWS

Implement the ports in `mcf/core/ports.py` under `mcf/adapters/gcp` or `mcf/adapters/aws` and add a branch in `runtime.from_env`. No agent or service code changes. The port-to-service mapping per cloud is in ARCHITECTURE.md section 3.
