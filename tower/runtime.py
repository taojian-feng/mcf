"""Composition root: wire ports to adapters for the selected cloud (MCF_CLOUD=memory|azure)."""
from __future__ import annotations

import os
from datetime import timedelta
from dataclasses import dataclass
from typing import Any, Callable

from tower.agents.billing import BillingAgent
from tower.agents.disposition import DispositionAgent
from tower.agents.exception import Classifier, ExceptionAgent
from tower.agents.onboarding import OnboardingAgent
from tower.agents.orchestration import OrchestrationAgent
from tower.agents.planning import PlanningAgent
from tower.agents.reconciliation import ReconciliationAgent
from tower.agents.reporting import ReportingAgent
from tower.agents.returns import ReturnsAgent
from tower.core.config import PilotConfig
from tower.core.ports import (
    LLM, AuditLedger, BlobStore, Clock, Directory, DocumentStore, MCFTransport, MessageBus, Notifier,
    SecretStore,
)
from tower.services.approval import ApprovalService
from tower.services.audit import AuditService
from tower.services.briefs import BriefWriter
from tower.services.clients import ClientDirectory
from tower.services.governance import GovernanceService
from tower.services.handoffs import HandoffService
from tower.services.integration import IntegrationLayer


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
    secrets: SecretStore | None = None
    console_url: str | None = None      # where approvers open the approval console
    gateway_url: str | None = None      # where clients send catalogs and orders (for welcome emails)


def approval_links(ports: Ports):
    """Signed console links for approval emails, when a console is deployed."""
    if not (ports.console_url and ports.secrets):
        return None
    from tower.services.approval_links import LINK_KEY_SECRET, ApprovalLinks
    return ApprovalLinks(ports.console_url, ports.secrets.get_secret(LINK_KEY_SECRET).encode(), ports.clock)


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
    reconciliation: ReconciliationAgent
    planning: PlanningAgent
    disposition: DispositionAgent
    reporting: ReportingAgent
    returns: ReturnsAgent
    onboarding: OnboardingAgent
    billing: BillingAgent

    def run_job(self, name: str) -> object:
        """Scheduled jobs (Container Apps Jobs on Azure)."""
        jobs = {
            "reconciliation_daily": self.reconciliation.run_daily,
            "planning_daily": self.planning.run_daily,
            "disposition_daily": self.disposition.run_daily,
            "metrics_daily": self.reporting.compute_daily,
            "weekly_summary": lambda: self.reporting.run_weekly(self.ports.clock.now().date() - timedelta(days=1)),
            "qbr_summary": lambda: self.reporting.run_quarterly(self.ports.clock.now().date() - timedelta(days=1)),
            "onboarding_daily": self.onboarding.run_daily,
            "billing_daily": self.billing.run_daily,
            "invoicing_monthly": self.billing.run_monthly,
        }
        return jobs[name]()


def build(ports: Ports, config: PilotConfig, *, sleep: Callable[[float], None] | None = None) -> System:
    if not isinstance(ports.directory, ClientDirectory):     # clients onboarded through the tower, too
        ports.directory = ClientDirectory(ports.directory, ports.store)
    audit = AuditService(ports.ledger, ports.blobs, ports.notifier, ports.clock)
    governance = GovernanceService(ports.directory, audit, ports.clock, masking_key=ports.masking_key,
                                   cache_ttl_s=config.authorization_list_cache_ttl.total_seconds())
    kw: dict[str, Any] = {"sleep": sleep} if sleep else {}
    integration = IntegrationLayer(ports.mcf, audit, ports.notifier, ports.blobs, ports.clock, config.retry, **kw)
    briefs = BriefWriter(ports.store, ports.llm) if config.approval_briefs else None
    approvals = ApprovalService(ports.store, ports.bus, ports.directory, ports.notifier, audit, ports.clock, config,
                                links=approval_links(ports), briefs=briefs)
    handoffs = HandoffService(ports.store, ports.bus, ports.directory, ports.notifier, audit, ports.clock, config)
    orchestration = OrchestrationAgent(ports.store, ports.bus, integration, governance, audit, handoffs,
                                       ports.clock, config)
    exceptions = ExceptionAgent(ports.store, ports.bus, approvals, handoffs, audit, ports.notifier,
                                ports.directory, ports.clock, config, classifier=Classifier(ports.llm))
    reconciliation = ReconciliationAgent(ports.store, ports.bus, audit, ports.notifier, ports.directory, ports.clock,
                                         config)
    planning = PlanningAgent(ports.store, ports.bus, integration, governance, approvals, handoffs, reconciliation,
                             audit, ports.notifier, ports.directory, ports.clock, config)
    disposition = DispositionAgent(ports.store, ports.bus, approvals, handoffs, reconciliation, planning, audit,
                                   ports.notifier, ports.directory, ports.clock, config)
    reporting = ReportingAgent(ports.store, ports.bus, ports.blobs, planning, governance, audit, ports.notifier,
                               ports.directory, ports.clock, config, llm=ports.llm)
    returns = ReturnsAgent(ports.store, ports.bus, integration, audit, ports.clock, config)
    onboarding = OnboardingAgent(ports.store, ports.bus, approvals, audit, ports.notifier, ports.directory,
                                 ports.clock, config, gateway_url=ports.gateway_url)
    billing = BillingAgent(ports.store, ports.bus, ports.blobs, approvals, audit, ports.notifier, ports.directory,
                           ports.clock, config)
    return System(ports, config, audit, governance, integration, approvals, handoffs, orchestration, exceptions,
                  reconciliation, planning, disposition, reporting, returns, onboarding, billing)


def memory_ports(**overrides: Any) -> Ports:
    from tower.adapters import memory as m

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
        from tower.adapters.azure import azure_ports
        return azure_ports()
    raise NotImplementedError(f"cloud '{cloud}' has no adapters yet (gcp and aws are planned)")
