"""Clients onboarded through the tower, and the directory view that includes them.

Clients set up by hand live in config/pilot.yaml, which every deploy copies into App Configuration.
A client the onboarding agent takes on is a record in the `clients` collection instead: its application,
the checks, the approval, and once Corp A approves, its contract and its people. ClientDirectory lays
those records over the configured directory, so every agent, the console and the gateway see an
onboarded client the same way they see Corp B and Corp C, without a deploy.

A configured client always wins: an onboarded record can't replace a client from pilot.yaml.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel, Field

from tower.core.models import CLIENT_CATEGORIES
from tower.core.ports import DocumentStore
from tower.core.tenants import Tenant, default_tenants

COLLECTION = "clients"
ACTIVE = ("active", "live")        # approved and set up; "live" once its first order went through
AGENTS_WITH_INSIGHT_ACCESS = ("planning_agent", "reporting_agent")


class ClientApplication(BaseModel):
    """What Corp A's onboarding person fills in for a new retail client."""
    name: str = Field(min_length=2, max_length=60)
    tenant_id: str | None = None                 # derived from the name when blank
    channels: list[str] = Field(default_factory=lambda: ["online"])
    promise_days: list[int] = Field(default_factory=lambda: [3])
    delivery_states: list[str] | None = None     # None: the US 48 states
    return_window_days: int = Field(default=30, ge=0, le=365)
    expected_orders_per_day: int = Field(ge=1, le=100_000)
    expected_skus: int = Field(default=100, ge=1, le=1_000_000)
    outbound_url: str | None = None              # where the client wants order updates
    approver_email: str                          # decides the client's exceptions and stock moves
    ops_email: str                               # day-to-day contact
    billing_email: str | None = None             # gets the monthly invoice (ops_email when blank)
    rates: dict[str, float] = Field(default_factory=dict)   # negotiated rates; the rest are standard
    notes: str = Field(default="", max_length=2000)


class ClientRecord(BaseModel):
    tenant_id: str
    name: str
    status: str                                  # blocked, pending_approval, rejected, active, live
    application: ClientApplication
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    load: dict[str, Any] = Field(default_factory=dict)       # network volume before and after this client
    contract: dict[str, Any] | None = None       # the Tenant, once approved
    approval_id: str | None = None
    submitted_by: str
    submitted_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None
    activated_at: datetime | None = None
    catalog_at: datetime | None = None
    first_order_at: datetime | None = None
    nudged_at: datetime | None = None

    def tenant(self) -> Tenant | None:
        return Tenant.model_validate(self.contract) if self.contract else None


def client_roles(tenant_id: str, approver: str) -> dict[str, set[str]]:
    return {approver: {f"approver:{c.value}:{tenant_id}" for c in CLIENT_CATEGORIES}}


def client_contacts(tenant_id: str, app: ClientApplication) -> dict[str, str]:
    ops = app.ops_email
    return {f"{tenant_id}_ops": ops, f"{tenant_id}_escalation": ops, f"{tenant_id}_data_contact": ops,
            f"{tenant_id}_inventory": ops, f"{tenant_id}_planner": ops, f"{tenant_id}_review": ops,
            f"{tenant_id}_billing": app.billing_email or ops}


class ClientDirectory:
    """A Directory that also knows the onboarded clients. Records are re-read every `ttl_s` seconds."""

    def __init__(self, base, store: DocumentStore, *, ttl_s: float = 60, now: Callable[[], float] = time.monotonic):
        self.base, self.store, self.ttl_s, self._now = base, store, ttl_s, now
        self._cache: tuple[float, list[ClientRecord]] | None = None

    def __getattr__(self, name: str):          # anything else the configured directory offers (e.g. roles)
        return getattr(self.base, name)

    def invalidate(self) -> None:
        self._cache = None

    def records(self) -> list[ClientRecord]:
        if self._cache is None or self._now() - self._cache[0] >= self.ttl_s:
            docs = self.store.query(COLLECTION)
            recs = [ClientRecord.model_validate(d) for d in docs if d.get("status") in ACTIVE and d.get("contract")]
            self._cache = (self._now(), recs)
        return self._cache[1]

    def _configured(self) -> dict[str, Tenant]:
        load = getattr(self.base, "tenants", None)
        return load() if load else default_tenants()

    def _onboarded(self) -> list[ClientRecord]:
        configured = self._configured()
        return [r for r in self.records() if r.tenant_id not in configured]

    # ---------------------------------------------------------------- Directory

    def has_role(self, principal: str, role: str) -> bool:
        if self.base.has_role(principal, role):
            return True
        return any(role in client_roles(r.tenant_id, r.application.approver_email).get(principal, ())
                   for r in self._onboarded())

    def principals_with_role(self, role: str) -> list[str]:
        found = set(self.base.principals_with_role(role))
        for r in self._onboarded():
            found |= {p for p, roles in client_roles(r.tenant_id, r.application.approver_email).items() if role in roles}
        return sorted(found)

    def pilot_authorization_list(self) -> set[str]:
        return self.base.pilot_authorization_list()

    def insight_access(self, tenant_id: str) -> set[str]:
        for r in self._onboarded():
            if r.tenant_id == tenant_id:
                return set(AGENTS_WITH_INSIGHT_ACCESS) | {r.application.approver_email}
        return self.base.insight_access(tenant_id)

    def contact(self, name: str) -> str | None:
        found = self.base.contact(name)
        if found:
            return found
        for r in self._onboarded():
            hit = client_contacts(r.tenant_id, r.application).get(name)
            if hit:
                return hit
        return None

    # ---------------------------------------------------------------- the client registry

    def tenants(self) -> dict[str, Tenant]:
        out = dict(self._configured())
        for r in self.records():
            t = r.tenant()
            if t is not None and r.tenant_id not in out:
                out[r.tenant_id] = t
        return out

    def pilot_config(self):
        from tower.core.config import PilotConfig
        load = getattr(self.base, "pilot_config", None)
        config = load() if load else PilotConfig()
        config.tenants = self.tenants()
        return config
