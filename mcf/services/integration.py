"""Integration_Layer (R7): MCF API access with retry policy, and inbound feed validation."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from mcf.core.config import RetryPolicy
from mcf.core.models import AuditKind, OwningParty
from mcf.core.ports import BlobStore, Clock, MCFError, MCFTransport, Notifier

from .audit import OPS_TEAM, AuditService

MCF_OPERATIONS = frozenset({
    "CreateFulfillmentOrder", "GetFulfillmentOrder", "FulfillmentOrderStatus", "GetFulfillmentPreview",
    "GetPackageTrackingDetail", "CreateInboundShipmentPlan", "UpdateInboundShipment",
    "CreateFeedDocument", "ApproveDistributionPlan",
})

BLANK_BOX_OVERPACK = "BLANK_BOX_OVERPACK"


@dataclass
class CallResult:
    ok: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    attempts: int = 0
    retryable: bool = False


@dataclass
class FeedResult:
    feed_id: str
    valid: list[BaseModel]
    rejected: list[dict[str, Any]]  # {"index", "record", "violations"}


class IntegrationLayer:
    def __init__(self, transport: MCFTransport, audit: AuditService, notifier: Notifier, blobs: BlobStore,
                 clock: Clock, retry: RetryPolicy, *, sleep: Callable[[float], None] = time.sleep):
        self.transport = transport
        self.audit = audit
        self.notifier = notifier
        self.blobs = blobs
        self.clock = clock
        self.retry = retry
        self.sleep = sleep

    # ---------------------------------------------------------------- MCF API (R7.1, R7.4-R7.6)

    def call(self, operation: str, payload: dict[str, Any], *, agent: str,
             order_ids: list[str] | None = None) -> CallResult:
        if operation not in MCF_OPERATIONS:
            raise ValueError(f"unsupported MCF operation {operation}")
        attempts, delay = 0, self.retry.initial_delay_s
        while True:
            attempts += 1
            try:
                return CallResult(ok=True, data=self.transport.call(operation, payload), attempts=attempts)
            except MCFError as err:
                if err.retryable and attempts <= self.retry.max_retries:
                    self.sleep(delay)
                    delay *= 2
                    continue
                exhausted = err.retryable
                self.audit.record(
                    AuditKind.INTEGRATION_FAILURE, agent, f"mcf_{operation}_failed",
                    rationale=f"{err.code}: {'retries exhausted' if exhausted else 'non-retryable'}",
                    result={"attempts": attempts, "code": err.code}, order_ids=order_ids or [],
                    owning_party=OwningParty.CORP_A,
                )
                if exhausted:
                    self.notifier.send(OPS_TEAM, f"MCF {operation} failing",
                                       f"{err.code} after {attempts} attempts; orders={order_ids}",
                                       severity="high")
                return CallResult(ok=False, error_code=err.code, attempts=attempts, retryable=err.retryable)

    def create_fulfillment_order(self, order_view: dict[str, Any], *, agent: str) -> CallResult:
        """order_view must be the FULFILLMENT-purpose view from GovernanceService (R9.6)."""
        c = order_view["consumer"]
        payload = {
            "sellerFulfillmentOrderId": order_view["order_id"],
            "displayableOrderId": order_view["order_id"],
            "displayableOrderDate": order_view["placed_at"],
            "shippingSpeedCategory": "Expedited" if order_view["delivery_promise_days"] == 2 else "Standard",
            "destinationAddress": {
                "name": c["name"],  # tokenized; carriers need only the address
                "addressLine1": c["address"]["line1"], "addressLine2": c["address"].get("line2"),
                "city": c["address"]["city"], "stateOrRegion": c["address"]["state"],
                "postalCode": c["address"]["postal_code"], "countryCode": c["address"]["country"],
            },
            "items": [
                {"sellerSku": ln["upc"], "sellerFulfillmentOrderItemId": ln["line_id"], "quantity": ln["quantity"]}
                for ln in order_view["lines"]
            ],
            "packagingInstruction": BLANK_BOX_OVERPACK,  # R7.7
        }
        return self.call("CreateFulfillmentOrder", payload, agent=agent, order_ids=[order_view["order_id"]])

    # ---------------------------------------------------------------- feeds (R7.2, R7.3)

    def ingest_feed(self, feed_id: str, records: list[dict[str, Any]], schema: type[BaseModel], *,
                    originating_party_contact: str) -> FeedResult:
        valid, rejected = [], []
        for i, raw in enumerate(records):
            try:
                valid.append(schema.model_validate(raw))
            except ValidationError as ve:
                rejected.append({
                    "index": i, "record": raw,
                    "violations": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in ve.errors()],
                })
        if rejected:
            self.blobs.write("quarantine", f"{feed_id}.json", json.dumps(rejected, default=str).encode())
            self.audit.record(AuditKind.VALIDATION_REJECT, "integration_layer", "feed_records_quarantined",
                              input_refs=[feed_id], result={"rejected": len(rejected), "accepted": len(valid)})
            self.notifier.send(
                originating_party_contact, f"Validation errors in feed {feed_id}",
                json.dumps([{"index": r["index"], "violations": r["violations"]} for r in rejected], indent=2),
            )
        return FeedResult(feed_id=feed_id, valid=valid, rejected=rejected)
