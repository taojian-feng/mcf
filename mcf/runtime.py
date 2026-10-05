"""Composition root: wire ports to adapters for the selected cloud (MCF_CLOUD=memory|azure)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from mcf.agents.exception import Classifier, ExceptionAgent
from mcf.agents.orchestration import OrchestrationAgent
from mcf.core.config import PilotConfig
from mcf.core.ports import (
    LLM, AuditLedger, BlobStore, Clock, Directory, DocumentStore, MCFTransport, MessageBus, Notifier,
)
from mcf.services.approval import ApprovalService
from mcf.services.audit import AuditService
from mcf.services.governance import GovernanceService
from mcf.services.handoffs import HandoffService
from mcf.services.integration import IntegrationLayer


@dataclass
class Ports:
    clock: Clock
    bus: MessageBus
    store: DocumentStore
    blobs: BlobStore
    ledger: AuditLedger
    directory: Directory
    notifier: Notifier
    mcf: MCFTransport
    llm: LLM | None
    masking_key: bytes


@dataclass
class System:
    ports: Ports
    config: PilotConfig
    audit: AuditService
    governance: GovernanceService
    integration: IntegrationLayer
    approvals: ApprovalService
    handoffs: HandoffService
    orchestration: OrchestrationAgent
    exceptions: ExceptionAgent


def build(ports: Ports, config: PilotConfig, *, sleep: Callable[[float], None] | None = None) -> System:
    audit = AuditService(ports.ledger, ports.blobs, ports.notifier, ports.clock)
    governance = GovernanceService(ports.directory, audit, ports.clock, masking_key=ports.masking_key,
                                   cache_ttl_s=config.authorization_list_cache_ttl.total_seconds())
    kw: dict[str, Any] = {"sleep": sleep} if sleep else {}
    integration = IntegrationLayer(ports.mcf, audit, ports.notifier, ports.blobs, ports.clock, config.retry, **kw)
    approvals = ApprovalService(ports.store, ports.bus, ports.directory, ports.notifier, audit, ports.clock, config)
    handoffs = HandoffService(ports.store, ports.bus, ports.directory, ports.notifier, audit, ports.clock, config)
    orchestration = OrchestrationAgent(ports.store, ports.bus, integration, governance, audit, handoffs,
                                       ports.clock, config)
    exceptions = ExceptionAgent(ports.store, ports.bus, approvals, handoffs, audit, ports.notifier,
                                ports.directory, ports.clock, config, classifier=Classifier(ports.llm))
    return System(ports, config, audit, governance, integration, approvals, handoffs, orchestration, exceptions)


def memory_ports(**overrides: Any) -> Ports:
    from mcf.adapters import memory as m

    clock = overrides.pop("clock", None) or m.VirtualClock()
    defaults = dict(
        clock=clock, bus=m.InMemoryBus(clock), store=m.InMemoryStore(), blobs=m.InMemoryBlobStore(),
        ledger=m.InMemoryLedger(), directory=m.StaticDirectory(), notifier=m.RecordingNotifier(),
        mcf=m.FakeMCF(), llm=None, masking_key=b"local-dev-only-key",
    )
    defaults.update(overrides)
    return Ports(**defaults)


def from_env() -> Ports:
    cloud = os.environ.get("MCF_CLOUD", "memory")
    if cloud == "memory":
        return memory_ports()
    if cloud == "azure":
        from mcf.adapters.azure import azure_ports
        return azure_ports()
    raise NotImplementedError(f"cloud '{cloud}' has no adapters yet (gcp and aws are planned)")
