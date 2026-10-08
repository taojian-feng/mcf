"""Pilot configuration. Every 'configured' value in the requirements lives here.

Defaults are placeholders until the pilot team confirms them (see ARCHITECTURE.md section 8).
On Azure these are loaded from App Configuration; locally from env / code.
"""
from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, Field

from .models import ExceptionCategory, OwningParty
from .tenants import TENANT_ONE, Tenant, default_tenants

# Contiguous US states plus DC. Excludes AK, HI and territories (R5.9 "US 48 states").
US48 = {
    "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "ID", "IL", "IN", "IA", "KS", "KY",
    "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}


class RetryPolicy(BaseModel):
    initial_delay_s: float = 1.0
    max_retries: int = 5  # R7.4: 1s, 2s, 4s, 8s, 16s


class NodeInfo(BaseModel):
    node_id: str
    kind: str  # ISC | IXD | FC
    region: str | None = None  # FCs serve a demand region; ISC/IXD serve the network


def _default_nodes() -> dict[str, NodeInfo]:
    nodes = [NodeInfo(node_id="ISC-X", kind="ISC"), NodeInfo(node_id="ISC-Y", kind="ISC"),
             NodeInfo(node_id="IXD-1", kind="IXD"),
             NodeInfo(node_id="FC-WEST", kind="FC", region="west"),
             NodeInfo(node_id="FC-EAST", kind="FC", region="east")]
    return {n.node_id: n for n in nodes}


class PilotConfig(BaseModel):
    pilot_upcs: set[str] = Field(default_factory=set)  # tenant one's catalog (1,360 UPCs from master data)
    eligible_states: set[str] = Field(default_factory=lambda: set(US48))

    # Retail clients, and each one's catalog from its item master feed. Tenant one's catalog is pilot_upcs.
    tenants: dict[str, Tenant] = Field(default_factory=default_tenants)
    tenant_upcs: dict[str, set[str]] = Field(default_factory=dict)

    # R5
    ack_timeout: timedelta = timedelta(minutes=30)
    promise_check_interval: timedelta = timedelta(hours=4)

    # R7
    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    # R3
    classification_confidence_threshold: float = 0.8
    resolution_time_limits: dict[ExceptionCategory, timedelta] = Field(
        default_factory=lambda: {
            ExceptionCategory.LOST_SHIPMENT: timedelta(days=5),
            ExceptionCategory.DAMAGED_PRODUCT: timedelta(days=3),
            ExceptionCategory.DELIVERY_DELAY: timedelta(days=2),
            ExceptionCategory.FAILED_DELIVERY: timedelta(days=3),
            ExceptionCategory.LATE_CANCELLATION: timedelta(days=1),
        }
    )

    # R8
    approval_first_reminder: timedelta = timedelta(hours=48)
    approval_reminder_interval: timedelta = timedelta(hours=24)

    # R9
    authorization_list_cache_ttl: timedelta = timedelta(minutes=5)  # must stay < 15 min (R9.8)

    # R11: process action -> owning party (pilot responsibility matrix)
    responsibility_matrix: dict[str, OwningParty] = Field(
        default_factory=lambda: {
            "submit_fulfillment_order": OwningParty.CORP_B,
            "fulfill_order": OwningParty.CORP_A,
            "monitor_delivery_promise": OwningParty.CORP_B,
            "propagate_tracking": OwningParty.CORP_B,
            "classify_exception": OwningParty.CORP_B,
            "cancel_fulfillment_order": OwningParty.CORP_A,
            "carrier_trace": OwningParty.CORP_A,
            "notify_consumer": OwningParty.CORP_B,
            "reship_order": OwningParty.CORP_A,
            "return_intercept": OwningParty.CORP_A,
            "approve_exception_resolution": OwningParty.CORP_B,
            "update_inventory_state": OwningParty.CORP_A,
            "reconcile_inventory": OwningParty.CORP_A,
            "transmit_decant_counts": OwningParty.CORP_A,
            "plan_demand": OwningParty.CORP_B,
            "recommend_replenishment": OwningParty.CORP_B,
            "approve_replenishment": OwningParty.CORP_B,
            "approve_distribution_plan": OwningParty.CORP_B,
            "execute_transfer": OwningParty.CORP_A,
            "monitor_aging": OwningParty.CORP_B,
            "approve_disposition": OwningParty.CORP_B,
            "execute_disposition": OwningParty.CORP_A,
            "compute_metrics": OwningParty.CORP_B,
            "authorize_return": OwningParty.CORP_B,      # the client's return policy, applied by the tower
            "receive_return": OwningParty.CORP_A,        # carrier pickup, receiving and grading
            "decide_refund": OwningParty.CORP_B,
            "dispose_return": OwningParty.CORP_A,
        }
    )
    approval_briefs: bool = True     # a plain-language brief on every approval (Claude when configured)
    handoff_ack_window: timedelta = timedelta(hours=4)
    handoff_default_due: timedelta = timedelta(hours=24)

    # Master data: fulfillment nodes
    nodes: dict[str, NodeInfo] = Field(default_factory=_default_nodes)

    # R1 planning
    min_cover_days: float = 14
    target_cover_days: float = 28
    imbalance_threshold: float = 0.25          # |position - projection| / projection
    imbalance_horizon_days: int = 28
    cold_start_replace_weeks: int = 4
    replenishment_value_threshold: float = 25_000.0
    unit_cost: dict[str, float] = Field(default_factory=dict)  # per UPC; default_unit_cost otherwise
    default_unit_cost: float = 60.0
    feed_delivery_window: timedelta = timedelta(days=7, hours=12)
    replenishment_cooldown: timedelta = timedelta(hours=24)

    # R2 reconciliation
    discrepancy_threshold_units: int = 2
    history_retention: timedelta = timedelta(days=365)

    # R4 disposition
    aging_thresholds_days: list[int] = Field(default_factory=lambda: [60, 90])
    disposition_threshold_days: int = 120
    storage_cost_per_unit_day: float = 0.05
    rebound_cost_per_unit: float = 2.50
    removal_cost_per_unit: float = 1.75
    avoided_cost_horizon_days: int = 90
    disposition_completion_window: timedelta = timedelta(days=14)
    disposition_rerecommend_after: timedelta = timedelta(days=30)

    # R6 reporting
    anomaly_threshold: float = 0.20            # relative deviation from trailing 4-week mean
    baseline_days: int = 28

    # Tenants

    def tenant(self, tenant_id: str | None) -> Tenant | None:
        return self.tenants.get(tenant_id or TENANT_ONE)

    def catalog(self, tenant_id: str | None) -> set[str]:
        tid = tenant_id or TENANT_ONE
        return self.pilot_upcs if tid == TENANT_ONE else self.tenant_upcs.get(tid, set())

    def tenant_of_sku(self, sku: str) -> str | None:
        """Whose item this is: each client owns its own stock (clients list different UPCs)."""
        if sku in self.pilot_upcs:
            return TENANT_ONE
        return next((t for t, upcs in self.tenant_upcs.items() if sku in upcs), None)

    def states_for(self, tenant_id: str | None) -> set[str]:
        t = self.tenant(tenant_id)
        return t.eligible_states if t and t.eligible_states else self.eligible_states
