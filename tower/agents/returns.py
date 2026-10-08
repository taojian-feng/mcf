"""Returns_Agent: consumer returns, from the client's request to the refund decision.

  1. The client's order management asks for a return (POST /v1/returns). The tower applies the client's
     return policy: the order was delivered, it is inside the return window, and the units were not
     already returned. Then Corp A issues an RMA and a prepaid label, which go back to the client.
  2. Corp A reports the parcel moving and arriving at its returns bench, then grades every unit:
     SELLABLE (back to stock; Corp A books the receipt), DAMAGED, DEFECTIVE or WRONG_ITEM. Units that
     never turn up are missing.
  3. The tower decides, per line, how many units the client can refund and which to hold for review,
     tells the client with the reasons, and tells Corp A what to do with the units it cannot restock.
     A label that runs out unused closes the return as expired.

Every decision is deterministic and goes to the audit ledger with its numbers, so a client can see why
a refund was held.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import (
    OrderRecord, OrderState, OwningParty, ReturnGrade, ReturnReason, ReturnRecord, ReturnRequest, ReturnState,
)
from tower.core.ports import Clock, DocumentStore, Event, MessageBus
from tower.services.audit import AuditService
from tower.services.integration import IntegrationLayer

AGENT = "returns_agent"
RETURNS = "returns"
ORDERS = "orders"
EXPIRY_GRACE = timedelta(days=10)       # after the label's ship-by date, for a parcel already in the mail
# What happens to units Corp A cannot put back on the shelf.
DISPOSITION = {ReturnGrade.DAMAGED: "liquidate", ReturnGrade.DEFECTIVE: "return_to_vendor",
               ReturnGrade.WRONG_ITEM: "hold_for_client"}
STATUS = {"IN_TRANSIT": ReturnState.IN_TRANSIT, "DELIVERED": ReturnState.ARRIVED}


class ReturnsAgent:
    def __init__(self, store: DocumentStore, bus: MessageBus, integration: IntegrationLayer, audit: AuditService,
                 clock: Clock, config: PilotConfig):
        self.store, self.bus, self.integration, self.audit = store, bus, integration, audit
        self.clock, self.config = clock, config
        bus.subscribe(topics.RETURNS_EVENTS, self.on_event, subscription="returns-agent")
        bus.subscribe(topics.TIMERS, self.on_timer, subscription="returns-agent")

    def on_event(self, event: Event) -> None:
        if event.type == "return_requested":
            self.on_request(ReturnRequest.model_validate(event.payload))
            return
        rec = self._load(event.payload.get("return_id") or "")
        if rec is None:
            return
        if event.type == "return_status":
            self._on_status(rec, event.payload)
        elif event.type == "return_received":
            self._on_received(rec, event.payload)

    # ---------------------------------------------------------------- 1. authorization

    def on_request(self, req: ReturnRequest) -> None:
        if self.store.get(RETURNS, req.return_id):
            return  # at-least-once delivery
        doc = self.store.get(ORDERS, req.order_id)
        order = OrderRecord.model_validate(doc) if doc else None
        reasons = self._policy_problems(req, order)
        if not reasons:
            rec = ReturnRecord(return_id=req.return_id, tenant_id=req.tenant_id,
                               client_return_id=req.client_return_id or req.return_id, order_id=req.order_id,
                               client_order_id=req.client_order_id or req.order_id, requested_at=req.requested_at,
                               lines=req.lines)
            res = self.integration.create_return(rec.model_dump(mode="json"),
                                                 {ln.upc: ln.line_id for ln in order.lines}, agent=AGENT)
            if not res.ok:
                reasons = [f"Corp A could not authorize the return ({res.error_code}); send it again later"
                           if res.retryable else f"Corp A refused the return ({res.error_code})"]
        if reasons:
            self._tell(req.tenant_id, "return_rejected", req.client_return_id or req.return_id,
                       req.client_order_id or req.order_id, {"reasons": reasons}, req.return_id)
            self._audit("reject_return", req.return_id, req.order_id, req.tenant_id, "authorize_return",
                        rationale="; ".join(reasons), skus=[ln.upc for ln in req.lines])
            return
        data = res.data or {}
        tenant = self.config.tenant(req.tenant_id)
        rec.rma_id, rec.return_to, rec.label = data.get("rmaId"), data.get("returnToNode"), data.get("label")
        rec.ship_by = (datetime.fromisoformat(data["shipBy"]).date() if data.get("shipBy")
                       else self.clock.now().date() + timedelta(days=tenant.return_ship_by_days if tenant else 14))
        self._save(rec)
        self._tell(rec.tenant_id, "return_authorized", rec.client_return_id, rec.client_order_id,
                   {"rma_id": rec.rma_id, "label": rec.label, "ship_by": rec.ship_by.isoformat(),
                    "lines": [ln.model_dump(mode="json") for ln in rec.lines]}, rec.return_id)
        self._audit("authorize_return", rec.return_id, rec.order_id, rec.tenant_id, "authorize_return",
                    rationale=f"delivered {order.delivered_on}, inside the return window",
                    result={"rma_id": rec.rma_id, "units": sum(ln.quantity for ln in rec.lines)},
                    skus=[ln.upc for ln in rec.lines])
        at = datetime.combine(rec.ship_by, datetime.min.time(), self.clock.now().tzinfo) + EXPIRY_GRACE
        self.bus.publish(Event(topics.TIMERS, "return_expiry", {"return_id": rec.return_id}, self.clock.now(),
                               correlation_id=rec.order_id), deliver_at=max(at, self.clock.now()))

    def _policy_problems(self, req: ReturnRequest, order: OrderRecord | None) -> list[str]:
        """The client's return policy, as its contract states it."""
        if order is None or order.tenant_id != req.tenant_id:
            return [f"order {req.client_order_id} is unknown"]
        if order.channel != "online":
            return ["store orders are returned through the store, not here"]
        if order.state != OrderState.DELIVERED or order.delivered_on is None:
            return [f"order {req.client_order_id} has not been delivered (it is {order.state.value})"]
        tenant = self.config.tenant(req.tenant_id)
        window = tenant.return_window_days if tenant else 30
        age = (req.requested_at.date() - order.delivered_on).days
        if age > window:
            return [f"delivered {age} days ago; the return window is {window} days"]
        out = []
        if len({ln.upc for ln in req.lines}) != len(req.lines):
            out.append("one line per item")
        bought = Counter()
        for ln in order.lines:
            bought[ln.upc] += ln.quantity
        returned = self._already_returned(order.order_id)
        for ln in req.lines:
            if ln.upc not in bought:
                out.append(f"UPC {ln.upc} is not on order {req.client_order_id}")
            elif ln.quantity > bought[ln.upc] - returned[ln.upc]:
                out.append(f"UPC {ln.upc}: {ln.quantity} asked, {bought[ln.upc] - returned[ln.upc]} left to return")
        return out

    def _already_returned(self, order_id: str) -> Counter:
        out = Counter()
        for doc in self.store.query(RETURNS, order_id=order_id):
            if doc["state"] != ReturnState.EXPIRED.value:
                for ln in doc["lines"]:
                    out[ln["upc"]] += ln["quantity"]
        return out

    # ---------------------------------------------------------------- 2. the parcel moves

    def _on_status(self, rec: ReturnRecord, p: dict[str, Any]) -> None:
        new = STATUS.get(p.get("status") or "")
        if new is None or rec.state in (ReturnState.CLOSED, ReturnState.EXPIRED, new):
            return
        if new == ReturnState.IN_TRANSIT and rec.state == ReturnState.ARRIVED:
            return  # webhooks can arrive out of order; never go backwards
        rec.state = new
        rec.tracking.append({k: p.get(k) for k in ("status", "carrier", "tracking_number", "at")})
        self._save(rec)
        self._tell(rec.tenant_id, "return_update", rec.client_return_id, rec.client_order_id,
                   {"status": new.value, "carrier": p.get("carrier"), "tracking_number": p.get("tracking_number")},
                   rec.return_id)

    # ---------------------------------------------------------------- 3. graded: refund and disposition

    def _on_received(self, rec: ReturnRecord, p: dict[str, Any]) -> None:
        if rec.state == ReturnState.CLOSED:
            return
        rec.state, rec.received_at = ReturnState.CLOSED, self.clock.now()
        rec.graded = [{"sku": it.get("sellerSku"), "quantity": int(it.get("quantity") or 0), "grade": it.get("grade")}
                      for it in p.get("items") or []]
        rec.outcome = self.refund_decision(rec.lines, rec.graded)
        refund = sum(o["refund_units"] for o in rec.outcome)
        held = sum(o["held_units"] for o in rec.outcome)
        unsellable = [{"sellerSku": g["sku"], "grade": g["grade"], "quantity": g["quantity"],
                       "disposition": DISPOSITION[ReturnGrade(g["grade"])]}
                      for g in rec.graded if g["grade"] in {k.value for k in DISPOSITION} and g["quantity"]]
        if unsellable and rec.rma_id:
            res = self.integration.call("SetReturnDisposition", {"rmaId": rec.rma_id, "items": unsellable},
                                        agent=AGENT, order_ids=[rec.order_id])
            rec.dispositions = [{**u, "accepted": res.ok} for u in unsellable]
        self._save(rec)
        self._tell(rec.tenant_id, "return_received", rec.client_return_id, rec.client_order_id,
                   {"rma_id": rec.rma_id, "lines": rec.outcome, "refund_units": refund, "held_units": held},
                   rec.return_id)
        self._audit("decide_refund", rec.return_id, rec.order_id, rec.tenant_id, "decide_refund",
                    rationale=(f"{refund} units to refund, {held} held for review" if held
                               else f"all {refund} units to refund"),
                    result={"lines": rec.outcome}, skus=[ln.upc for ln in rec.lines])
        if rec.dispositions:
            self._audit("dispose_return", rec.return_id, rec.order_id, rec.tenant_id, "dispose_return",
                        rationale="unsellable units: damaged are liquidated, defective go back to the vendor, "
                                  "wrong items are held for the client",
                        result={"items": rec.dispositions}, skus=[d["sellerSku"] for d in rec.dispositions])

    @staticmethod
    def refund_decision(lines, graded: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Per line: units to refund and units to hold, with the reason for each hold.

        Refund what came back as sold or as the consumer said: sellable units, defective units, and damage
        when the consumer reported damage in transit. Hold damage the consumer did not report (it happened
        after delivery), a different item in the parcel, and units that never arrived."""
        by_sku: dict[str, Counter] = {}
        for g in graded:
            by_sku.setdefault(g["sku"], Counter())[g["grade"]] += g["quantity"]
        out = []
        for ln in lines:
            got = by_sku.get(ln.upc, Counter())
            damaged_ok = ln.reason == ReturnReason.DAMAGED_IN_TRANSIT
            refund = got[ReturnGrade.SELLABLE.value] + got[ReturnGrade.DEFECTIVE.value] + (
                got[ReturnGrade.DAMAGED.value] if damaged_ok else 0)
            refund = min(refund, ln.quantity)
            holds = []
            if got[ReturnGrade.DAMAGED.value] and not damaged_ok:
                holds.append({"units": got[ReturnGrade.DAMAGED.value],
                              "why": f"arrived damaged, but the reason given was {ln.reason.value}"})
            if got[ReturnGrade.WRONG_ITEM.value]:
                holds.append({"units": got[ReturnGrade.WRONG_ITEM.value], "why": "the parcel held a different item"})
            missing = ln.quantity - sum(got.values())
            if missing > 0:
                holds.append({"units": missing, "why": "not in the parcel"})
            out.append({"line_id": ln.line_id, "upc": ln.upc, "expected": ln.quantity,
                        "grades": dict(got), "refund_units": refund,
                        "held_units": ln.quantity - refund, "holds": holds})
        return out

    # ---------------------------------------------------------------- expiry

    def on_timer(self, event: Event) -> None:
        if event.type != "return_expiry":
            return
        rec = self._load(event.payload["return_id"])
        if rec is None or rec.state != ReturnState.AUTHORIZED:
            return
        rec.state = ReturnState.EXPIRED
        self._save(rec)
        self._tell(rec.tenant_id, "return_expired", rec.client_return_id, rec.client_order_id,
                   {"rma_id": rec.rma_id, "ship_by": rec.ship_by.isoformat() if rec.ship_by else None},
                   rec.return_id)
        self._audit("expire_return", rec.return_id, rec.order_id, rec.tenant_id, "authorize_return",
                    rationale=f"no carrier scan by {rec.ship_by} plus {EXPIRY_GRACE.days} days")

    # ---------------------------------------------------------------- helpers

    def _tell(self, tenant_id: str, kind: str, client_return_id: str, client_order_id: str,
              extra: dict[str, Any], correlation: str) -> None:
        self.bus.publish(Event(topics.OMS_OUTBOUND, kind, {"return_id": client_return_id,
                                                           "order_id": client_order_id, "tenant_id": tenant_id,
                                                           **extra}, self.clock.now(), correlation_id=correlation))

    def _audit(self, action: str, return_id: str, order_id: str, tenant_id: str, action_key: str, *,
               rationale: str = "", result: dict[str, Any] | None = None, skus: list[str] | None = None) -> None:
        self.audit.decision(AGENT, action, rationale=rationale, result=result or {}, order_ids=[order_id],
                            skus=skus or [], input_refs=[f"return:{return_id}"], tenant_id=tenant_id,
                            owning_party=self.config.responsibility_matrix.get(action_key, OwningParty.CORP_B))

    def _load(self, return_id: str) -> ReturnRecord | None:
        doc = self.store.get(RETURNS, return_id)
        return ReturnRecord.model_validate(doc) if doc else None

    def _save(self, rec: ReturnRecord) -> None:
        self.store.put(RETURNS, rec.return_id, rec.model_dump(mode="json"))
