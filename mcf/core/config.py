"""Pilot configuration. Every 'configured' value in the requirements lives here.

Defaults are placeholders until the pilot team confirms them (see ARCHITECTURE.md section 8).
On Azure these are loaded from App Configuration; locally from env / code.
"""
from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, Field

from .models import ExceptionCategory, OwningParty

# Contiguous US states plus DC. Excludes AK, HI and territories (R5.9 "US 48 states").
US48 = {
    "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "ID", "IL", "IN", "IA", "KS", "KY",
    "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}


class RetryPolicy(BaseModel):
    initial_delay_s: float = 1.0
    max_retries: int = 5  # R7.4: 1s, 2s, 4s, 8s, 16s


class PilotConfig(BaseModel):
    pilot_upcs: set[str] = Field(default_factory=set)  # 1,360 UPCs loaded from master data
    eligible_states: set[str] = Field(default_factory=lambda: set(US48))

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
        }
    )
    handoff_ack_window: timedelta = timedelta(hours=4)
    handoff_default_due: timedelta = timedelta(hours=24)
