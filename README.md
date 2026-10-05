# MCF Agentic Fulfillment Orchestration

A multi-agent system for the Corp B x Corp A Multi-Channel Fulfillment pilot. The core is cloud-agnostic, Azure is the first cloud, and GCP and AWS come later as adapter packages. The design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Run it locally

```bash
pip install pydantic pytest
python -m pytest            # runs the order-to-exception slice in memory with virtual time
```

## What exists today (v0.1)

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
| Planning, Reconciliation, Disposition, Reporting agents | stubs with planned shape | R1, R2, R4, R6 |
| Azure adapters | written, not yet run against Azure | Service Bus, Cosmos, Blob, SQL ledger, Key Vault, App Configuration, ACS email, Foundry |
| Azure infra | `infra/azure/main.bicep` compiles; `sql/audit.sql` | |

Not yet covered: PII retention purge job (R9.5 is set up via Cosmos TTL on `order_pii`), encryption-at-rest specifics (R9.3, platform default plus TLS), audit retention policy (R10.4).

## Layout

```
mcf/core        models, ports (interfaces), topics, config       no cloud imports
mcf/services    audit, approval, governance, integration, handoffs
mcf/agents      orchestration, exception (+ stubs for the other four)
mcf/adapters    memory/ (local + tests), azure/, http_mcf.py
mcf/runtime.py  wires ports to adapters (MCF_CLOUD=memory|azure)
infra/azure     main.bicep, sql/audit.sql
tests           test_slice.py (24 scenario tests)
```

## Adding GCP or AWS

Implement the ports in `mcf/core/ports.py` under `mcf/adapters/gcp` or `mcf/adapters/aws` and add a branch in `runtime.from_env`. No agent or service code changes. The port-to-service mapping per cloud is in ARCHITECTURE.md section 3.
