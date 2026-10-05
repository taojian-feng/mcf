"""Domain models shared by every agent and service. No cloud imports here."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class OwningParty(str, Enum):
    CORP_B = "corp_b"
    CORP_A = "corp_a"


# ---------------------------------------------------------------- orders (R5)

class OrderState(str, Enum):
    RECEIVED = "received"
    ACKNOWLEDGED = "acknowledged"
    PROCESSING = "processing"
    PACKED = "packed"
    SHIPPED = "shipped"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


ACTIVE_STATES = {s for s in OrderState if s not in (OrderState.DELIVERED, OrderState.CANCELLED)}

# Forward-only lifecycle from the requirements diagram; cancel allowed from any active state.
_FORWARD = [
    OrderState.RECEIVED, OrderState.ACKNOWLEDGED, OrderState.PROCESSING, OrderState.PACKED,
    OrderState.SHIPPED, OrderState.OUT_FOR_DELIVERY, OrderState.DELIVERED,
]


def is_valid_transition(src: OrderState, dst: OrderState) -> bool:
    if src not in ACTIVE_STATES:
        return False
    if dst == OrderState.CANCELLED:
        return True
    # MCF status feeds can skip intermediate states (e.g. processing -> shipped); never go backwards.
    return _FORWARD.index(dst) > _FORWARD.index(src)


class Address(BaseModel):
    line1: str
    line2: str | None = None
    city: str
    state: str  # USPS 2-letter code
    postal_code: str
    country: str = "US"


class ConsumerPII(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    address: Address


class OrderLine(BaseModel):
    line_id: str
    upc: str
    quantity: int = Field(gt=0)


class ConsumerOrder(BaseModel):
    """Order as received from Corp B order management."""
    order_id: str
    placed_at: datetime
    delivery_promise_days: int = Field(ge=2, le=3)
    lines: list[OrderLine]
    consumer: ConsumerPII


class OrderRecord(BaseModel):
    """Order lifecycle record owned by the Orchestration agent. Holds no raw PII."""
    order_id: str
    placed_at: datetime
    promised_by: date
    delivery_promise_days: int
    lines: list[OrderLine]
    state: OrderState = OrderState.RECEIVED
    transitions: list[dict[str, Any]] = Field(default_factory=list)
    submission_attempts: int = 0
    submission_failed: bool = False
    ack_timer_id: str | None = None
    projected_delivery: date | None = None
    missing_projection_checks: int = 0
    alerted_breaches: list[str] = Field(default_factory=list)
    tracking: list[dict[str, Any]] = Field(default_factory=list)
    exceptions: list[dict[str, Any]] = Field(default_factory=list)
    delivered_on: date | None = None
    on_time: bool | None = None


# ---------------------------------------------------------------- exceptions (R3)

class ExceptionCategory(str, Enum):
    LOST_SHIPMENT = "lost_shipment"
    DAMAGED_PRODUCT = "damaged_product"
    DELIVERY_DELAY = "delivery_delay"
    FAILED_DELIVERY = "failed_or_undeliverable_delivery"
    LATE_CANCELLATION = "order_cancellation_after_remorse"


class ExceptionStatus(str, Enum):
    OPEN = "open"
    AWAITING_APPROVAL = "awaiting_approval"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    ESCALATED_AND_CLOSED = "escalated_and_closed"
    CANCELLED = "cancelled"


TERMINAL_EXCEPTION_STATES = {
    ExceptionStatus.RESOLVED, ExceptionStatus.ESCALATED_AND_CLOSED, ExceptionStatus.CANCELLED,
}


class UnhappyPathSignal(BaseModel):
    """Raw signal into the Exception agent (from Integration, Orchestration, Disposition)."""
    signal_id: str = Field(default_factory=lambda: new_id("sig"))
    signal_type: str  # e.g. projected_promise_breach, carrier_lost, fulfillment_submission_failed
    source: str  # producing agent/service
    order_id: str | None = None
    sku: str | None = None
    node_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime | None = None


class Classification(BaseModel):
    category: ExceptionCategory | None
    confidence: float
    candidates: dict[str, float] = Field(default_factory=dict)
    rationale: str = ""


class ExceptionCase(BaseModel):
    case_id: str = Field(default_factory=lambda: new_id("exc"))
    signal: UnhappyPathSignal
    opened_at: datetime
    classification: Classification | None = None
    status: ExceptionStatus = ExceptionStatus.OPEN
    playbook_id: str | None = None
    playbook_version: str | None = None
    completed_steps: list[str] = Field(default_factory=list)
    failed_step: str | None = None
    approval_id: str | None = None
    pending_handoffs: list[str] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------- audit (R10)

class AuditKind(str, Enum):
    AGENT_DECISION = "agent_decision"
    HUMAN_ACTION = "human_action"
    DENIED_ATTEMPT = "denied_attempt"
    INTEGRATION_FAILURE = "integration_failure"
    VALIDATION_REJECT = "validation_reject"
    SYSTEM = "system"


class AuditRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: new_id("aud"))
    kind: AuditKind
    at: datetime
    actor: str  # agent identity or user principal
    action: str
    rationale: str = ""
    input_refs: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    order_ids: list[str] = Field(default_factory=list)
    skus: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    owning_party: OwningParty | None = None
    subject_id: str | None = None  # recommendation / approval / handoff id acted upon


# ---------------------------------------------------------------- approvals (R8)

class ApprovalCategory(str, Enum):
    DISTRIBUTION_PLAN = "distribution_plan"
    REPLENISHMENT = "replenishment"
    DISPOSITION = "disposition"
    EXCEPTION_RESOLUTION = "exception_resolution"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalItem(BaseModel):
    approval_id: str = Field(default_factory=lambda: new_id("apr"))
    category: ApprovalCategory
    owning_party: OwningParty
    originating_agent: str
    subject_id: str
    content: dict[str, Any]
    rationale: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime
    routed_to: list[str] = Field(default_factory=list)
    decided_by: str | None = None
    decided_at: datetime | None = None
    reason: str | None = None
    reminders_sent: int = 0


# ---------------------------------------------------------------- handoffs (R11)

class HandoffStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Handoff(BaseModel):
    handoff_id: str = Field(default_factory=lambda: new_id("hof"))
    action: str
    requesting_party: OwningParty
    responsible_party: OwningParty
    created_at: datetime
    due_at: datetime
    status: HandoffStatus = HandoffStatus.OPEN
    blocked_steps: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    escalations: list[str] = Field(default_factory=list)
    acknowledged_at: datetime | None = None
    completed_at: datetime | None = None
