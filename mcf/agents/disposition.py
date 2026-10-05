"""Disposition_Agent (R4): aging and storage-cost monitoring, disposition recommendations, tracking to completion."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from mcf.core import topics
from mcf.core.config import PilotConfig
from mcf.core.models import ApprovalCategory, OwningParty, UnhappyPathSignal, new_id
from mcf.core.ports import Clock, Directory, DocumentStore, Event, MessageBus, Notifier
from mcf.services.approval import ApprovalService
from mcf.services.audit import AuditService
from mcf.services.handoffs import HandoffService

from .planning import PlanningAgent
from .reconciliation import STATE, ReconciliationAgent

AGENT = "disposition_agent"
INVENTORY_CONTACT = "corp_b_inventory"
ALERTS = "aging_alerts"
RECS = "disposition_recs"
OPEN_STATES = {"pending_approval", "approved", "initiated"}
TERMINAL = {"completed", "failed"}


class DispositionAgent:
    def __init__(self, store: DocumentStore, bus: MessageBus, approvals: ApprovalService, handoffs: HandoffService,
                 inventory: ReconciliationAgent, planning: PlanningAgent, audit: AuditService, notifier: Notifier,
                 directory: Directory, clock: Clock, config: PilotConfig):
        self.store, self.bus, self.approvals, self.handoffs = store, bus, approvals, handoffs
        self.inventory, self.planning, self.audit, self.notifier = inventory, planning, audit, notifier
        self.directory, self.clock, self.config = directory, clock, config
        bus.subscribe(topics.APPROVAL_DECISIONS, self.on_approval, subscription="disposition-agent")
        bus.subscribe(topics.DISPOSITION_EVENTS, self.on_disposition_event, subscription="disposition-agent")
        bus.subscribe(topics.TIMERS, self.on_timer, subscription="disposition-agent")

    # ---------------------------------------------------------------- daily evaluation (R4.1 - R4.3, R4.9)

    def run_daily(self) -> list[dict[str, Any]]:
        now = self.clock.now()
        evaluations = []
        for st in self.store.query(STATE):
            sku, node = st["sku"], st["node_id"]
            lots = [(lot["qty"], (now - datetime.fromisoformat(lot["received_at"])).days) for lot in st["lots"]
                    if lot["qty"] > 0]
            if not lots:
                self._reset_alerts(sku, node)
                continue
            ev = {"sku": sku, "node_id": node, "qty": sum(q for q, _ in lots), "oldest_age_days": max(a for _, a in lots),
                  "storage_cost": round(sum(q * a for q, a in lots) * self.config.storage_cost_per_unit_day, 2)}
            evaluations.append(ev)
            for threshold in self.config.aging_thresholds_days:
                aged = [(q, a) for q, a in lots if a >= threshold]
                self._aging_alert(ev, threshold, aged)
            aged = [(q, a) for q, a in lots if a >= self.config.disposition_threshold_days]
            if aged:
                self._maybe_recommend(ev, aged)
        self.audit.decision(AGENT, "daily_aging_evaluation", result={"positions": len(evaluations)},
                            owning_party=OwningParty.CORP_B)
        return evaluations

    def _aging_alert(self, ev: dict[str, Any], threshold: int, aged: list[tuple[int, int]]) -> None:
        key = f"{ev['sku']}|{ev['node_id']}|{threshold}"
        prior = self.store.get(ALERTS, key)
        if not aged:
            if prior and prior["active"]:
                self.store.put(ALERTS, key, {**prior, "active": False})  # a later crossing alerts again
            return
        if prior and prior["active"]:
            return  # one alert per crossing
        qty = sum(q for q, _ in aged)
        cost = round(sum(q * a for q, a in aged) * self.config.storage_cost_per_unit_day, 2)
        body = {"sku": ev["sku"], "node_id": ev["node_id"], "threshold_days": threshold, "quantity": qty,
                "age_days": max(a for _, a in aged), "accumulated_storage_cost": cost}
        self.notifier.send(self._contact(INVENTORY_CONTACT), f"Aging inventory: {ev['sku']} at {ev['node_id']}",
                           json.dumps(body, indent=2))
        self.store.put(ALERTS, key, {"active": True, "sent_at": self.clock.now().isoformat(), **body})
        self.audit.decision(AGENT, "aging_alert_sent", result=body, skus=[ev["sku"]], node_ids=[ev["node_id"]],
                            owning_party=OwningParty.CORP_B)

    def _reset_alerts(self, sku: str, node: str) -> None:
        for threshold in self.config.aging_thresholds_days:
            key = f"{sku}|{node}|{threshold}"
            prior = self.store.get(ALERTS, key)
            if prior and prior["active"]:
                self.store.put(ALERTS, key, {**prior, "active": False})

    # ---------------------------------------------------------------- recommendation (R4.3 - R4.5)

    def _maybe_recommend(self, ev: dict[str, Any], aged: list[tuple[int, int]]) -> None:
        now = self.clock.now()
        for r in self.store.query(RECS, sku=ev["sku"], node_id=ev["node_id"]):
            if r["status"] in OPEN_STATES:
                return
            if r["status"] == "rejected" and now - datetime.fromisoformat(r["rejected_at"]) < \
                    self.config.disposition_rerecommend_after:
                return
        qty = sum(q for q, _ in aged)
        dd = self.planning.node_daily_demand(ev["sku"], ev["node_id"])
        sell_through = None if not dd else round(ev["qty"] / dd, 1)
        options = {"return_to_rebound": round(qty * self.config.rebound_cost_per_unit, 2),
                   "removal": round(qty * self.config.removal_cost_per_unit, 2)}
        option = min(options, key=options.get)
        avoided = round(qty * self.config.storage_cost_per_unit_day * self.config.avoided_cost_horizon_days, 2)
        rec = {"rec_id": new_id("dsp"), "sku": ev["sku"], "node_id": ev["node_id"], "qty": qty, "option": option,
               "estimated_disposition_cost": options[option], "alternatives": options,
               "estimated_avoided_storage_cost": avoided,
               "demand_outlook": {"daily_demand": dd, "sell_through_days": sell_through},
               "created_at": now.isoformat(), "status": "pending_approval"}
        rationale = (f"{qty} units aged >= {self.config.disposition_threshold_days}d at {ev['node_id']}; "
                     f"demand outlook {'none' if dd is None else f'{dd:.2f}/day'}; {option} costs "
                     f"{options[option]} vs {avoided} storage avoided over {self.config.avoided_cost_horizon_days}d")
        item = self.approvals.submit(category=ApprovalCategory.DISPOSITION, owning_party=OwningParty.CORP_B,
                                     agent=AGENT, subject_id=rec["rec_id"], rationale=rationale,
                                     content={**{k: rec[k] for k in ("option", "qty", "estimated_disposition_cost",
                                                                     "estimated_avoided_storage_cost", "demand_outlook")},
                                              "skus": [ev["sku"]], "node_ids": [ev["node_id"]]})
        rec["approval_id"] = item.approval_id
        self.store.put(RECS, rec["rec_id"], rec)
        self.audit.decision(AGENT, "disposition_recommended", rationale=rationale, subject_id=rec["rec_id"],
                            skus=[ev["sku"]], node_ids=[ev["node_id"]], owning_party=OwningParty.CORP_B)

    # ---------------------------------------------------------------- approval -> initiation (R4.6, R4.9)

    def on_approval(self, event: Event) -> None:
        if event.payload.get("originating_agent") != AGENT:
            return
        rec = self.store.get(RECS, event.payload["subject_id"])
        if not rec or rec["status"] != "pending_approval":
            return
        if event.type == "approved" and self.approvals.is_executable(rec["approval_id"]):
            self._initiate(rec)
        else:
            rec["status"], rec["rejected_at"] = "rejected", self.clock.now().isoformat()
            self.store.put(RECS, rec["rec_id"], rec)
            self.audit.decision(AGENT, "disposition_rejected_aging_resumed", subject_id=rec["rec_id"],
                                rationale=event.payload.get("reason", ""), skus=[rec["sku"]], node_ids=[rec["node_id"]],
                                owning_party=OwningParty.CORP_B)

    def _initiate(self, rec: dict[str, Any]) -> None:
        h = self.handoffs.create("execute_disposition", requesting_party=OwningParty.CORP_B, requested_by=AGENT,
                                 blocked_steps=[f"{rec['rec_id']}:{rec['option']}"],
                                 context={"rec_id": rec["rec_id"], "sku": rec["sku"], "node_id": rec["node_id"],
                                          "qty": rec["qty"], "option": rec["option"]})
        rec.update(status="initiated", initiated_at=self.clock.now().isoformat(), handoff_id=h.handoff_id)
        self.store.put(RECS, rec["rec_id"], rec)
        self.bus.publish(Event(topics.TIMERS, "disposition_completion_check", {"rec_id": rec["rec_id"]},
                               self.clock.now(), correlation_id=rec["rec_id"]),
                         deliver_at=self.clock.now() + self.config.disposition_completion_window)
        self.audit.decision(AGENT, "disposition_initiated", subject_id=rec["rec_id"],
                            result={"option": rec["option"], "handoff_id": h.handoff_id},
                            skus=[rec["sku"]], node_ids=[rec["node_id"]], owning_party=OwningParty.CORP_B)

    # ---------------------------------------------------------------- tracking (R4.7, R4.8)

    def on_disposition_event(self, event: Event) -> None:
        if event.type not in TERMINAL:
            return
        rec = self.store.get(RECS, event.payload["rec_id"])
        if not rec or rec["status"] != "initiated":
            return
        rec.update(status=event.type, closed_at=self.clock.now().isoformat(), detail=event.payload.get("detail"))
        self.store.put(RECS, rec["rec_id"], rec)
        if event.type == "completed" and rec.get("handoff_id"):
            self.handoffs.complete(rec["handoff_id"], by=event.payload.get("confirmed_by", "rebound"))
        self.audit.decision(AGENT, f"disposition_{event.type}", subject_id=rec["rec_id"],
                            result={"detail": event.payload.get("detail")}, skus=[rec["sku"]],
                            node_ids=[rec["node_id"]], owning_party=OwningParty.CORP_B)

    def on_timer(self, event: Event) -> None:
        if event.type != "disposition_completion_check":
            return
        rec = self.store.get(RECS, event.payload["rec_id"])
        if not rec or rec["status"] != "initiated":
            return
        sig = UnhappyPathSignal(signal_type="disposition_overdue", source=AGENT, sku=rec["sku"], node_id=rec["node_id"],
                                details={"rec_id": rec["rec_id"], "qty": rec["qty"], "option": rec["option"],
                                         "initiated_at": rec["initiated_at"]}, received_at=self.clock.now())
        self.bus.publish(Event(topics.EXCEPTION_SIGNALS, sig.signal_type, sig.model_dump(mode="json"),
                               self.clock.now(), correlation_id=rec["rec_id"]))
        self.audit.decision(AGENT, "disposition_overdue_raised", subject_id=rec["rec_id"], skus=[rec["sku"]],
                            node_ids=[rec["node_id"]], owning_party=OwningParty.CORP_B)

    def _contact(self, name: str) -> str:
        return self.directory.contact(name) or name
