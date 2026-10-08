"""Retail clients (tenants) of the control tower.

Corp A runs one logistics network for many retailers. Each retailer is a tenant with its own contract
(SLA, delivery area), catalog, API keys, people and data boundary. Corp B is tenant one.

Every order, exception, approval, handoff and audit record carries the tenant it belongs to. Tenants
are identified only by the credential they call with: an id in a request body is never trusted.

Order ids belong to the client, so two clients can both send order "1001". Inside the tower and at
Corp A's API an order is known by its fulfillment id: the tenant's order prefix plus the client's id
(tenant one has no prefix, so its ids are unchanged). Messages back to a client use the client's id.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from .models import OwningParty

TENANT_ONE = "corp_b"
PROVIDER = OwningParty.CORP_A.value


class Tenant(BaseModel):
    tenant_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$")
    name: str
    active: bool = True
    order_prefix: str = ""                     # fulfillment id = prefix + client order id
    feed_folder: str = ""                      # landing/<feed_folder>/<feed type>/<file>
    api_key_secret: str = ""                   # Key Vault secret the client calls our gateway with
    outbound_url: str | None = None            # client's endpoint for order updates (None: MCF_<ID>_URL)
    outbound_key_secret: str = ""              # Key Vault secret we call the client's endpoint with
    promise_days: list[int] = Field(default_factory=lambda: [2, 3])  # delivery promises the SLA covers
    eligible_states: set[str] | None = None    # None: the US 48 states

    @model_validator(mode="after")
    def _defaults(self) -> "Tenant":
        compact = self.tenant_id.replace("_", "")
        self.feed_folder = self.feed_folder or compact
        self.api_key_secret = self.api_key_secret or f"partner-{compact}-api-key"
        self.outbound_key_secret = self.outbound_key_secret or f"{compact}-inbound-api-key"
        if self.tenant_id != TENANT_ONE and not self.order_prefix:
            self.order_prefix = f"{compact.upper()}-"
        return self

    def fulfillment_id(self, client_order_id: str) -> str:
        return f"{self.order_prefix}{client_order_id}"

    def client_order_id(self, fulfillment_id: str) -> str:
        p = self.order_prefix
        return fulfillment_id[len(p):] if p and fulfillment_id.startswith(p) else fulfillment_id


def tenant_one() -> Tenant:
    # Tenant one predates the registry: its secrets and feed folder keep their original names.
    return Tenant(tenant_id=TENANT_ONE, name="Corp B", feed_folder="corpb",
                  api_key_secret="partner-corpb-api-key", outbound_key_secret="corpb-inbound-api-key")


def default_tenants() -> dict[str, Tenant]:
    return {TENANT_ONE: tenant_one()}


def party_key(party: OwningParty | None, tenant_id: str | None) -> str:
    """Whose people own an action: Corp A's for provider actions, else the client's.

    Roles and contacts are named by this key (approver:<category>:<key>, <key>_ops), so each client's
    approvals and alerts go to that client's people only.
    """
    if party == OwningParty.CORP_A:
        return PROVIDER
    return tenant_id or TENANT_ONE
