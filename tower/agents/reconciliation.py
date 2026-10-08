"""Reconciliation_Agent (R2): event-sourced network inventory, queries, daily physical-vs-ATS reconciliation."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import AuditKind, InventoryEvent, InventoryEventType as T, OwningParty
from tower.core.ports import Clock, Directory, DocumentStore, Event, MessageBus, Notifier
from tower.services.audit import AuditService

AGENT = "reconciliation_agent"
EVENTS = "inventory_events"      # append-only event log: the source of truth and the history (R2.6)
STATE = "inventory_state"        # materialized current position per SKU x node
IN_TRANSIT = "inventory_in_transit"
REPORTS = "reconciliation_reports"
FULFILLMENT_OPS = "fulfillment_ops"

OUTBOUND = {T.TRANSFER, T.PICK, T.REMOVAL}


def state_key(sku: str, node_id: str) -> str:
    return f"{sku}|{node_id}"


def data_contact(party: OwningParty | str) -> str:
    return f"{OwningParty(party).value}_data_contact"


class ReconciliationAgent:
    def __init__(self, store: DocumentStore, bus: MessageBus, audit: AuditService, notifier: Notifier,
                 directory: Directory, clock: Clock, config: PilotConfig):
        self.store, self.bus, self.audit, self.notifier = store, bus, audit, notifier
        self.directory, self.clock, self.config = directory, clock, config
        bus.subscribe(topics.INVENTORY_EVENTS, self.on_inventory_event, subscription="reconciliation-agent")

    # ---------------------------------------------------------------- ingest (R2.1, R2.5, R2.7)

    def on_inventory_event(self, event: Event) -> None:
        raw = event.payload
        try:
            ev = InventoryEvent.model_validate(raw)
        except ValidationError as ve:
            self._reject(raw, [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in ve.errors()])
            return
        problems = self._scope_problems(ev)
        if problems:
            self._reject(raw, problems)
            return
        if self.store.get(EVENTS, ev.event_id):
            return  # duplicate delivery
        self.store.put(EVENTS, ev.event_id, ev.model_dump(mode="json"))
        changed = self._apply(ev)
        self.audit.decision(AGENT, f"inventory_{ev.type.value}_applied", input_refs=[f"event:{ev.event_id}"],
                            result={"quantity": ev.quantity}, skus=[ev.sku], node_ids=[ev.node_id],
                            owning_party=OwningParty.CORP_A)
        for sku, node in changed:
            self.bus.publish(Event(topics.INVENTORY_STATE, "changed", {"sku": sku, "node_id": node},
                                   self.clock.now(), correlation_id=state_key(sku, node)))
        if ev.type == T.DECANT:
            self._transmit_decant_counts(ev.node_id)

    def _scope_problems(self, ev: InventoryEvent) -> list[str]:
        out = []
        if self.config.tenant_of_sku(ev.sku) is None:
            out.append(f"sku {ev.sku} is in no client's catalog")
        for field in ("node_id", "to_node"):
            node = getattr(ev, field)
            if node is not None and node not in self.config.nodes:
                out.append(f"{field} {node} is not a pilot Fulfillment_Node")
        if ev.type == T.TRANSFER and not (ev.to_node and ev.transfer_id):
            out.append("transfer requires to_node and transfer_id")
        if ev.type != T.ADJUSTMENT and ev.quantity < 0:
            out.append("quantity must be >= 0")
        return out

    def _reject(self, raw: dict[str, Any], problems: list[str]) -> None:
        self.audit.record(AuditKind.VALIDATION_REJECT, AGENT, "inventory_event_rejected", rationale="; ".join(problems),
                          input_refs=[f"event:{raw.get('event_id')}"],
                          skus=[raw["sku"]] if isinstance(raw.get("sku"), str) else [],
                          node_ids=[raw["node_id"]] if isinstance(raw.get("node_id"), str) else [])
        party = raw.get("source_party") if raw.get("source_party") in {p.value for p in OwningParty} else "corp_a"
        contact = data_contact(party)
        self.notifier.send(self.directory.contact(contact) or contact, "Inventory event rejected",
                           json.dumps({"event": raw, "violations": problems}, default=str, indent=2))

    def _apply(self, ev: InventoryEvent) -> list[tuple[str, str]]:
        st = self._state(ev.sku, ev.node_id)
        changed = [(ev.sku, ev.node_id)]
        if ev.type == T.RECEIPT:
            lots = [{"qty": ev.quantity, "received_at": ev.occurred_at.isoformat()}]
            if ev.transfer_id:
                tr = self.store.get(IN_TRANSIT, ev.transfer_id)
                if tr and tr["status"] == "in_transit":
                    tr["status"], tr["received_at"] = "received", ev.occurred_at.isoformat()
                    self.store.put(IN_TRANSIT, ev.transfer_id, tr)
                    lots = _take_lots(tr["lots"], ev.quantity) or lots  # keep original age through transfers
                    changed.append((ev.sku, tr["from_node"]))
            st["on_hand"] += ev.quantity
            st["lots"] = sorted(st["lots"] + lots, key=lambda lot: lot["received_at"])
        elif ev.type in OUTBOUND or (ev.type == T.ADJUSTMENT and ev.quantity < 0):
            qty = abs(ev.quantity)
            st["on_hand"] -= qty
            taken = _take_lots(st["lots"], qty)
            if ev.type == T.TRANSFER:
                self.store.put(IN_TRANSIT, ev.transfer_id, {
                    "transfer_id": ev.transfer_id, "sku": ev.sku, "from_node": ev.node_id, "to_node": ev.to_node,
                    "qty": qty, "shipped_at": ev.occurred_at.isoformat(), "status": "in_transit", "lots": taken})
                changed.append((ev.sku, ev.to_node))
        elif ev.type == T.ADJUSTMENT:
            st["on_hand"] += ev.quantity
            st["lots"].append({"qty": ev.quantity, "received_at": ev.occurred_at.isoformat()})
        elif ev.type == T.PHYSICAL_COUNT:
            st["physical"] = {"qty": ev.quantity, "counted_at": ev.occurred_at.isoformat()}
        st["updated_at"] = self.clock.now().isoformat()
        st["tenant_id"] = self.config.tenant_of_sku(ev.sku)
        self.store.put(STATE, state_key(ev.sku, ev.node_id), st)
        return changed

    def _transmit_decant_counts(self, node_id: str) -> None:
        # Decant counts go to Corp B, so they carry Corp B's items only.
        counts = [{"sku": s["sku"], "on_hand": s["on_hand"]} for s in self.store.query(STATE, node_id=node_id)
                  if s["sku"] in self.config.pilot_upcs]
        self.bus.publish(Event(topics.CORPB_OUTBOUND, "decant_counts",
                               {"node_id": node_id, "as_of": self.clock.now().isoformat(), "counts": counts},
                               self.clock.now(), correlation_id=node_id))
        self.audit.decision(AGENT, "transmit_decant_counts", node_ids=[node_id], result={"skus": len(counts)},
                            owning_party=OwningParty.CORP_A)

    # ---------------------------------------------------------------- query (R2.2, R2.6)

    def query(self, *, sku: str | None = None, node_id: str | None = None) -> dict[str, Any]:
        filt = {k: v for k, v in (("sku", sku), ("node_id", node_id)) if v}
        positions = [{"sku": s["sku"], "node_id": s["node_id"], "on_hand": s["on_hand"]}
                     for s in self.store.query(STATE, **filt)]
        tfilt = {"status": "in_transit", **({"sku": sku} if sku else {})}
        transit = [t for t in self.store.query(IN_TRANSIT, **tfilt)
                   if not node_id or node_id in (t["from_node"], t["to_node"])]
        totals: dict[str, int] = {}
        for p in positions:
            totals[p["sku"]] = totals.get(p["sku"], 0) + p["on_hand"]
        for t in transit:
            totals[t["sku"]] = totals.get(t["sku"], 0) + t["qty"]
        return {"positions": positions,
                "in_transit": [{k: t[k] for k in ("transfer_id", "sku", "from_node", "to_node", "qty")} for t in transit],
                "network_total": totals}

    def on_hand(self, sku: str, node_id: str) -> int:
        doc = self.store.get(STATE, state_key(sku, node_id))
        return doc["on_hand"] if doc else 0

    def inbound_in_transit(self, sku: str, node_id: str) -> int:
        return sum(t["qty"] for t in self.store.query(IN_TRANSIT, status="in_transit", sku=sku, to_node=node_id))

    def lots(self, sku: str, node_id: str) -> list[dict[str, Any]]:
        doc = self.store.get(STATE, state_key(sku, node_id))
        return doc["lots"] if doc else []

    def position_at(self, sku: str, node_id: str, at: datetime) -> int:
        """Replay the event log: on-hand for any SKU x node at any point in time."""
        qty = 0
        for ev in sorted(self.store.query(EVENTS, sku=sku, node_id=node_id), key=lambda e: e["occurred_at"]):
            if datetime.fromisoformat(ev["occurred_at"]) > at:
                break
            t, q = T(ev["type"]), ev["quantity"]
            if t in (T.RECEIPT, T.ADJUSTMENT):
                qty += q
            elif t in OUTBOUND:
                qty -= q
        return qty

    # ---------------------------------------------------------------- daily reconciliation (R2.3, R2.4, R2.8)

    def run_daily(self) -> dict[str, Any]:
        now = self.clock.now()
        reconciled, discrepancies, unreconciled = 0, [], []
        every_client = set(self.config.pilot_upcs).union(*self.config.tenant_upcs.values())
        for sku in sorted(every_client):
            for node_id in self.config.nodes:
                st = self.store.get(STATE, state_key(sku, node_id))
                phys = (st or {}).get("physical")
                if not phys or now - datetime.fromisoformat(phys["counted_at"]) > timedelta(hours=24):
                    if st:  # only combinations that hold or held inventory need a count
                        unreconciled.append({"sku": sku, "node_id": node_id})
                    continue
                expected = self.position_at(sku, node_id, datetime.fromisoformat(phys["counted_at"]))
                reconciled += 1
                if abs(phys["qty"] - expected) > self.config.discrepancy_threshold_units:
                    d = {"sku": sku, "node_id": node_id, "expected": expected, "observed": phys["qty"]}
                    discrepancies.append(d)
                    self._discrepancy_alert(d)
        report = {"date": now.date().isoformat(), "reconciled": reconciled, "discrepancies": discrepancies,
                  "unreconciled": unreconciled}
        self.store.put(REPORTS, report["date"], report)
        contact = self.directory.contact(FULFILLMENT_OPS) or FULFILLMENT_OPS
        self.notifier.send(contact, f"Daily reconciliation {report['date']}", json.dumps(report, indent=2))
        self.audit.decision(AGENT, "daily_reconciliation", result={k: len(v) if isinstance(v, list) else v
                                                                    for k, v in report.items()},
                            owning_party=OwningParty.CORP_A)
        return report

    def _discrepancy_alert(self, d: dict[str, Any]) -> None:
        contact = self.directory.contact(FULFILLMENT_OPS) or FULFILLMENT_OPS
        self.notifier.send(contact, f"Inventory discrepancy {d['sku']} at {d['node_id']}", json.dumps(d),
                           severity="high")
        self.audit.decision(AGENT, "discrepancy_alert", result=d, skus=[d["sku"]], node_ids=[d["node_id"]],
                            owning_party=OwningParty.CORP_A, tenant_id=self.config.tenant_of_sku(d["sku"]))

    # ---------------------------------------------------------------- helpers

    def _state(self, sku: str, node_id: str) -> dict[str, Any]:
        return self.store.get(STATE, state_key(sku, node_id)) or {
            "sku": sku, "node_id": node_id, "on_hand": 0, "lots": [], "physical": None}


def _take_lots(lots: list[dict[str, Any]], qty: int) -> list[dict[str, Any]]:
    """Consume qty FIFO from lots (mutates lots). Returns the consumed slices with their receipt dates."""
    taken = []
    while qty > 0 and lots:
        lot = lots[0]
        n = min(qty, lot["qty"])
        taken.append({"qty": n, "received_at": lot["received_at"]})
        lot["qty"] -= n
        qty -= n
        if lot["qty"] <= 0:
            lots.pop(0)
    return taken
