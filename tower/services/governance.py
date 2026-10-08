"""Data_Governance_Service (R9): insight-data isolation and consumer PII masking."""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from enum import Enum
from typing import Any

from tower.core.models import AuditKind, ConsumerOrder
from tower.core.ports import Clock, Directory
from tower.core.tenants import TENANT_ONE

from .audit import AuditService


class Purpose(str, Enum):
    DECISION = "agent_decision"      # mask every PII field (R9.4)
    METRICS = "metric_computation"   # mask every PII field (R9.4)
    FULFILLMENT = "fulfillment"      # address unmasked, everything else masked (R9.6)


class AccessDenied(PermissionError):
    pass


class MaskingFailed(RuntimeError):
    pass


PII_FIELDS = ("name", "email", "phone")


class GovernanceService:
    def __init__(self, directory: Directory, audit: AuditService, clock: Clock, *,
                 masking_key: bytes, cache_ttl_s: float = 300):
        self.directory = directory
        self.audit = audit
        self.clock = clock
        self._key = masking_key
        self._ttl = cache_ttl_s
        self._auth_cache: dict[str, tuple[datetime, set[str]]] = {}

    # ---------------------------------------------------------------- isolation (R9.1, R9.2, R9.8)

    def _authorized(self, tenant_id: str) -> set[str]:
        now = self.clock.now()
        hit = self._auth_cache.get(tenant_id)
        if hit is None or (now - hit[0]).total_seconds() >= self._ttl:
            people = set()
            per_tenant = getattr(self.directory, "insight_access", None)
            if per_tenant:
                people |= per_tenant(tenant_id)
            if tenant_id == TENANT_ONE:
                people |= self.directory.pilot_authorization_list()
            hit = self._auth_cache[tenant_id] = (now, people)
        return hit[1]

    def check_insight_access(self, principal: str, resource: str, *, tenant_id: str = TENANT_ONE) -> None:
        """Each client's sales and forecasts are visible only to people and agents on that client's list."""
        if principal in self._authorized(tenant_id):
            return
        self.audit.denied(principal, f"{tenant_id.replace('_', '')}_insight_access_denied", input_refs=[resource],
                          rationale=f"principal not on {tenant_id}'s insight access list", tenant_id=tenant_id)
        raise AccessDenied(f"{principal} is not authorized for {tenant_id} insight data")

    # ---------------------------------------------------------------- PII (R9.4, R9.6, R9.7)

    def token(self, value: str) -> str:
        """Irreversible, stable token (HMAC-SHA256) so masked data can still be joined."""
        return "tok_" + hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()[:20]

    def view_order(self, order: ConsumerOrder, purpose: Purpose, *, requester: str) -> dict[str, Any] | None:
        """Return the order with PII masked for the purpose, or None (withheld) if masking fails."""
        try:
            data = order.model_dump(mode="json")
            consumer = data["consumer"]
            for f in PII_FIELDS:
                if consumer.get(f) is not None:
                    consumer[f] = self.token(consumer[f])
            if purpose != Purpose.FULFILLMENT:
                addr = consumer["address"]
                # Keep the state only: needed for eligibility and regional metrics, not identifying.
                consumer["address"] = {"state": addr["state"], "masked": self.token(
                    "|".join(str(addr.get(k) or "") for k in ("line1", "line2", "city", "postal_code")))}
            self._verify_masked(order, data, purpose)
            return data
        except Exception as exc:
            self.audit.record(
                AuditKind.SYSTEM,
                "data_governance", "masking_failed_data_withheld",
                rationale=str(exc), order_ids=[order.order_id], input_refs=[requester],
            )
            return None

    @staticmethod
    def _verify_masked(order: ConsumerOrder, data: dict[str, Any], purpose: Purpose) -> None:
        flat = repr(data)
        c = order.consumer
        secrets = [c.name, c.email, c.phone]
        if purpose != Purpose.FULFILLMENT:
            secrets += [c.address.line1, c.address.postal_code]
        for s in secrets:
            if s and s in flat:
                raise MaskingFailed("PII value present after masking")
