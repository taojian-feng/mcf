"""Orchestration_Agent (R5): order lifecycle, MCF submission, delivery-promise monitoring.

Two channels share the lifecycle. Online orders are parcels to a consumer with a promise in days. Store
orders are cases to a store by freight, due inside the store's delivery window; before they go to Corp A
each line is allocated against the client's shared stock (services/allocation.py)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import (
    ACTIVE_STATES, ConsumerOrder, OrderLine, OrderRecord, OrderState, StoreOrder, UnhappyPathSignal,
    is_valid_transition,
)
from tower.core.ports import Clock, DocumentStore, Event, MessageBus
from tower.services.allocation import AllocationService
from tower.services.audit import AuditService
from tower.services.governance import GovernanceService, Purpose
from tower.services.handoffs import HandoffService
from tower.services.integration import IntegrationLayer

AGENT = "orchestration_agent"
ORDERS = "orders"
ORDER_PII = "order_pii"  # encrypted container with PII retention TTL; read only through governance


class OrchestrationAgent:
    def __init__(self, store: DocumentStore, bus: MessageBus, integration: IntegrationLayer,
                 governance: GovernanceService, audit: AuditService, handoffs: HandoffService,
                 clock: Clock, config: PilotConfig, allocation: AllocationService | None = None):
        self.store, self.bus, self.integration = store, bus, integration
        self.governance, self.audit, self.handoffs = governance, audit, handoffs
        self.clock, self.config = clock, config
        self.allocation = allocation or AllocationService(store, clock, config)
        bus.subscribe(topics.ORDERS_RECEIVED, self.on_order_received, subscription="orchestration-agent")
        bus.subscribe(topics.ORDERS_LIFECYCLE, self.on_lifecycle, subscription="orchestration-agent")
        bus.subscribe(topics.EXCEPTION_UPDATES, self.on_exception_update, subscription="orchestration-agent")
        bus.subscribe(topics.TIMERS, self.on_timer, subscription="orchestration-agent")

    # ---------------------------------------------------------------- intake (R5.1, R5.9)

    def on_order_received(self, event: Event) -> None:
        if event.type == "store_order_received":
            self.on_store_order(StoreOrder.model_validate(event.payload))
            return
        order = ConsumerOrder.model_validate(event.payload)
        if self.store.get(ORDERS, order.order_id):
            return  # at-least-once delivery: already handled

        reasons = self._ineligibility(order)
        if reasons:
            self.bus.publish(Event(topics.OMS_OUTBOUND, "order_rejected",
                                   {"order_id": order.client_order_id or order.order_id, "tenant_id": order.tenant_id,
                                    "reasons": reasons}, self.clock.now(), correlation_id=order.order_id))
            self._audit("reject_ineligible_order", order.order_id, rationale="; ".join(reasons),
                        skus=[ln.upc for ln in order.lines], action_key="submit_fulfillment_order",
                        tenant_id=order.tenant_id)
            return

        self.store.put(ORDER_PII, order.order_id, order.model_dump(mode="json"))
        rec = OrderRecord(
            order_id=order.order_id, tenant_id=order.tenant_id, client_order_id=order.client_order_id,
            placed_at=order.placed_at,
            promised_by=order.placed_at.date() + timedelta(days=order.delivery_promise_days),
            delivery_promise_days=order.delivery_promise_days, lines=order.lines,
        )
        rec.transitions.append({"state": OrderState.RECEIVED.value, "at": self.clock.now().isoformat()})
        self._submit(rec)
        self._schedule("order_promise_check", rec.order_id, self.config.promise_check_interval)

    # ---------------------------------------------------------------- store orders (offline channel)

    def on_store_order(self, order: StoreOrder) -> None:
        if self.store.get(ORDERS, order.order_id):
            return
        client_id = order.client_order_id or order.order_id
        reasons = self._store_ineligibility(order)
        allocation = [] if reasons else self.allocation.allocate(order)
        if not reasons and not any(a.cases_allocated for a in allocation):
            reasons = ["no stock to spare for this store order after the web reserve"]
        if reasons:
            self.bus.publish(Event(topics.OMS_OUTBOUND, "order_rejected",
                                   {"order_id": client_id, "tenant_id": order.tenant_id, "store_id": order.store_id,
                                    "reasons": reasons, "allocation": [a.as_dict() for a in allocation]},
                                   self.clock.now(), correlation_id=order.order_id))
            self._audit("reject_store_order", order.order_id, rationale="; ".join(reasons),
                        skus=[ln.upc for ln in order.lines], action_key="submit_fulfillment_order",
                        tenant_id=order.tenant_id)
            return
        rec = OrderRecord(
            order_id=order.order_id, tenant_id=order.tenant_id, client_order_id=client_id, channel="store",
            store_id=order.store_id, ship_to={**order.ship_to.model_dump(mode="json"), "store_name": order.store_name},
            window_start=order.window_start, placed_at=order.placed_at, promised_by=order.window_end,
            delivery_promise_days=(order.window_end - order.placed_at.date()).days,
            lines=[OrderLine(line_id=a.line_id, upc=a.upc, quantity=a.cases_allocated * a.case_pack)
                   for a in allocation if a.cases_allocated],
            allocation=[a.as_dict() for a in allocation],
        )
        rec.transitions.append({"state": OrderState.RECEIVED.value, "at": self.clock.now().isoformat()})
        short = [a for a in allocation if a.short_cases]
        self._audit("allocate_store_order", rec.order_id, action_key="submit_fulfillment_order",
                    rationale=(f"{len(short)} of {len(allocation)} lines cut to protect web stock" if short
                               else "every line allocated in full"),
                    result={"lines": rec.allocation}, skus=[a.upc for a in allocation], tenant_id=rec.tenant_id)
        self.bus.publish(Event(topics.OMS_OUTBOUND, "store_order_allocated",
                               {"order_id": client_id, "tenant_id": rec.tenant_id, "store_id": rec.store_id,
                                "lines": rec.allocation}, self.clock.now(), correlation_id=rec.order_id))
        self._submit(rec)
        self._schedule("order_promise_check", rec.order_id, self.config.promise_check_interval)

    def _store_ineligibility(self, order: StoreOrder) -> list[str]:
        tenant = self.config.tenant(order.tenant_id)
        if tenant is None or not tenant.active or "store" not in tenant.channels:
            return [f"client {order.tenant_id} has no active store contract"]
        catalog = self.config.catalog(tenant.tenant_id)
        reasons = [f"UPC {ln.upc} is not in the client's catalog" for ln in order.lines if ln.upc not in catalog]
        if order.ship_to.country != "US" or order.ship_to.state.upper() not in self.config.states_for(tenant.tenant_id):
            reasons.append(f"store state {order.ship_to.state} is outside the client's delivery area")
        return reasons

    def _ineligibility(self, order: ConsumerOrder) -> list[str]:
        """Checked against the client's own contract: its catalog, delivery area and delivery promises."""
        tenant = self.config.tenant(order.tenant_id)
        if tenant is None or not tenant.active:
            return [f"client {order.tenant_id} has no active contract"]
        catalog = self.config.catalog(tenant.tenant_id)
        reasons = [f"UPC {ln.upc} is not in the client's catalog" for ln in order.lines if ln.upc not in catalog]
        addr = order.consumer.address
        if addr.country != "US" or addr.state.upper() not in self.config.states_for(tenant.tenant_id):
            reasons.append(f"delivery state {addr.state} is outside the client's delivery area")
        if order.delivery_promise_days not in tenant.promise_days:
            reasons.append(f"a {order.delivery_promise_days}-day promise is not in the client's SLA "
                           f"({', '.join(map(str, tenant.promise_days))} days)")
        return reasons

    # ---------------------------------------------------------------- submission + ack (R5.1, R5.2, R5.8)

    def _submit(self, rec: OrderRecord) -> None:
        rec.submission_attempts += 1
        if rec.channel == "store":
            result = self.integration.create_store_fulfillment_order(rec.model_dump(mode="json"), rec.ship_to or {},
                                                                     agent=AGENT)
        else:
            pii = ConsumerOrder.model_validate(self.store.get(ORDER_PII, rec.order_id))
            view = self.governance.view_order(pii, Purpose.FULFILLMENT, requester=AGENT)
            result = self.integration.create_fulfillment_order(view, agent=AGENT) if view else None
        rec.ack_timer_id = self._schedule("order_ack_timeout", rec.order_id, self.config.ack_timeout,
                                          attempt=rec.submission_attempts)
        self._save(rec)
        self._audit("submit_fulfillment_order", rec.order_id, tenant_id=rec.tenant_id,
                    rationale=f"attempt {rec.submission_attempts}",
                    result={"ok": bool(result and result.ok), "error": result.error_code if result else "withheld"},
                    skus=[ln.upc for ln in rec.lines])

    def _on_ack_timeout(self, rec: OrderRecord, attempt: int) -> None:
        if rec.state != OrderState.RECEIVED or attempt != rec.submission_attempts:
            return  # acknowledged meanwhile, or a stale timer
        if rec.submission_attempts < 2:
            self._submit(rec)  # R5.2: one retry, immediately (well within 5 minutes)
            return
        rec.submission_failed = True
        self._save(rec)
        self._signal(rec, "fulfillment_submission_failed",
                     {"attempts": rec.submission_attempts, "last_state": rec.state.value})

    # ---------------------------------------------------------------- lifecycle (R5.3, R5.6, R5.7)

    def on_lifecycle(self, event: Event) -> None:
        rec = self._load(event.payload["order_id"])
        if rec is None:
            return
        if event.type == "tracking":
            self._propagate_tracking(rec, event.payload)
            return
        if event.type == "ship_notice":
            self._forward_ship_notice(rec, event.payload)
            return
        if event.type != "status":
            return
        new = OrderState(event.payload["state"])
        at = datetime.fromisoformat(event.payload["at"]) if event.payload.get("at") else self.clock.now()
        if not is_valid_transition(rec.state, new):
            self._audit("ignore_invalid_transition", rec.order_id, tenant_id=rec.tenant_id,
                        rationale=f"{rec.state.value} -> {new.value} not allowed")
            return
        if rec.state == OrderState.RECEIVED and rec.ack_timer_id:
            self.bus.cancel_scheduled(rec.ack_timer_id)
            rec.ack_timer_id = None
        rec.state = new
        rec.transitions.append({"state": new.value, "at": at.isoformat()})
        if new == OrderState.DELIVERED:
            rec.delivered_on = at.date()
            rec.on_time = rec.delivered_on <= rec.promised_by
        self._save(rec)
        self._audit(f"order_{new.value}", rec.order_id, tenant_id=rec.tenant_id, action_key="fulfill_order",
                    result={"on_time": rec.on_time} if new == OrderState.DELIVERED else {})

    def _propagate_tracking(self, rec: OrderRecord, payload: dict[str, Any]) -> None:
        tracking = {k: payload.get(k) for k in ("carrier", "tracking_number", "status", "events")}
        rec.tracking.append({**tracking, "received_at": self.clock.now().isoformat()})
        self._save(rec)
        self.bus.publish(Event(topics.OMS_OUTBOUND, "tracking_update",
                               {"order_id": rec.client_order_id or rec.order_id, "tenant_id": rec.tenant_id,
                                **({"store_id": rec.store_id} if rec.store_id else {}), **tracking},
                               self.clock.now(), correlation_id=rec.order_id))
        self._audit("propagate_tracking", rec.order_id, action_key="propagate_tracking", tenant_id=rec.tenant_id)

    def _forward_ship_notice(self, rec: OrderRecord, payload: dict[str, Any]) -> None:
        """The store receives against this: what is on the truck, and when it is booked at the dock."""
        notice = {k: v for k, v in payload.items() if k != "order_id"}
        if any(n.get("asnId") == notice.get("asnId") for n in rec.ship_notices):
            return
        rec.ship_notices.append({**notice, "received_at": self.clock.now().isoformat()})
        self._save(rec)
        self.bus.publish(Event(topics.OMS_OUTBOUND, "advance_ship_notice",
                               {"order_id": rec.client_order_id or rec.order_id, "tenant_id": rec.tenant_id,
                                "store_id": rec.store_id, **notice}, self.clock.now(), correlation_id=rec.order_id))
        self._audit("forward_ship_notice", rec.order_id, action_key="propagate_tracking", tenant_id=rec.tenant_id,
                    result={"asn": notice.get("asnId"), "pallets": notice.get("pallets"),
                            "appointment": notice.get("appointment")})

    # ---------------------------------------------------------------- promise monitoring (R5.4, R5.5, R5.10)

    def _check_promise(self, rec: OrderRecord) -> None:
        if rec.state not in ACTIVE_STATES:
            return
        projected = None
        res = self.integration.call("GetFulfillmentOrder", {"sellerFulfillmentOrderId": rec.order_id},
                                    agent=AGENT, order_ids=[rec.order_id])
        if res.ok and res.data and res.data.get("estimatedArrivalDate"):
            projected = date.fromisoformat(str(res.data["estimatedArrivalDate"])[:10])

        if projected is None:
            rec.missing_projection_checks += 1
            if rec.missing_projection_checks == 2:
                self._signal(rec, "projected_delivery_unavailable",
                             {"last_state": rec.state.value, "consecutive_checks": 2})
        else:
            rec.missing_projection_checks = 0
            rec.projected_delivery = projected
            breach_key = projected.isoformat()
            if projected > rec.promised_by and breach_key not in rec.alerted_breaches:
                rec.alerted_breaches.append(breach_key)
                self._signal(rec, "projected_promise_breach",
                             {"projected": breach_key, "promised_by": rec.promised_by.isoformat(),
                              "last_state": rec.state.value})
        self._save(rec)
        self._audit("monitor_delivery_promise", rec.order_id, tenant_id=rec.tenant_id, action_key="monitor_delivery_promise",
                    result={"projected": projected.isoformat() if projected else None,
                            "promised_by": rec.promised_by.isoformat()})
        self._schedule("order_promise_check", rec.order_id, self.config.promise_check_interval)

    # ---------------------------------------------------------------- exceptions (R3.6)

    def on_exception_update(self, event: Event) -> None:
        rec = self._load(event.payload.get("order_id") or "")
        if rec is None:
            return
        rec.exceptions.append({k: event.payload.get(k) for k in ("case_id", "category", "status")}
                              | {"at": self.clock.now().isoformat()})
        self._save(rec)

    # ---------------------------------------------------------------- timers

    def on_timer(self, event: Event) -> None:
        if event.type not in ("order_ack_timeout", "order_promise_check"):
            return
        rec = self._load(event.payload["order_id"])
        if rec is None:
            return
        if event.type == "order_ack_timeout":
            self._on_ack_timeout(rec, event.payload["attempt"])
        else:
            self._check_promise(rec)

    # ---------------------------------------------------------------- helpers

    def _signal(self, rec: OrderRecord, signal_type: str, details: dict[str, Any]) -> None:
        sig = UnhappyPathSignal(signal_type=signal_type, source=AGENT, order_id=rec.order_id,
                                details=details, received_at=self.clock.now(), tenant_id=rec.tenant_id)
        self.bus.publish(Event(topics.EXCEPTION_SIGNALS, signal_type, sig.model_dump(mode="json"),
                               self.clock.now(), correlation_id=rec.order_id))
        self._audit(f"raise_{signal_type}", rec.order_id, rationale=str(details), tenant_id=rec.tenant_id)

    def _schedule(self, kind: str, order_id: str, after: timedelta, **extra: Any) -> str:
        return self.bus.publish(Event(topics.TIMERS, kind, {"order_id": order_id, **extra}, self.clock.now(),
                                      correlation_id=order_id), deliver_at=self.clock.now() + after)

    def _load(self, order_id: str) -> OrderRecord | None:
        doc = self.store.get(ORDERS, order_id)
        return OrderRecord.model_validate(doc) if doc else None

    def _save(self, rec: OrderRecord) -> None:
        self.store.put(ORDERS, rec.order_id, rec.model_dump(mode="json"))

    def _audit(self, action: str, order_id: str, *, action_key: str = "monitor_delivery_promise",
               rationale: str = "", result: dict[str, Any] | None = None, skus: list[str] | None = None,
               tenant_id: str | None = None) -> None:
        self.audit.decision(AGENT, action, rationale=rationale, result=result or {}, order_ids=[order_id],
                            skus=skus or [], input_refs=[f"order:{order_id}"], tenant_id=tenant_id,
                            owning_party=self.handoffs.owning_party(action_key, actor=AGENT))
