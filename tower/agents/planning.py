"""Planning_Agent (R1): demand plan, ATS cover, replenishment, imbalance and distribution planning.

Plans every client's items. Each client's forecasts, sales and cold-start baselines come from its own
feeds and are fenced by its insight access list; recommendations, plans, approvals and alerts carry the
client's tenant_id and go to that client's approvers and contacts (`<client>_planner`, `<client>_data_contact`).
A client's items are its catalog (`config.catalog`); stock belongs to whoever owns the item.

Demand: the client's forecast, unless recent orders have drifted from it and the plan-review mode
(`plan_review.py`) set a demand call for the item; restocks and rebalancing plans get a second look there too.
Recent orders are counted here from every order the tower receives (item, units, the ship-to state's region)."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import ApprovalCategory, OwningParty, new_id
from tower.core.tenants import TENANT_ONE
from tower.core.ports import LLM, Clock, Directory, DocumentStore, Event, MessageBus, Notifier
from tower.services.approval import ApprovalService
from tower.services.audit import AuditService
from tower.services.governance import GovernanceService
from tower.services.handoffs import HandoffService
from tower.services.integration import FeedResult, IntegrationLayer

from .plan_review import SENSING_WEIGHT, PlanReviewer
from .reconciliation import ReconciliationAgent

AGENT = "planning_agent"
DATA_CONTACT = "data_contact"      # <client>_data_contact: bad or missing feeds
PLANNER = "planner"                # <client>_planner: planning issues

DEMAND_PLAN = "demand_plan"
SALES = "sales_history"
COLD_START = "cold_start"
META = "planning_meta"
RECS = "replenishment_recs"
PLANS = "distribution_plans"
COVER = "ats_cover"
FLAGS = "baseline_flags"
ACTUALS = "demand_actuals"         # per item: units ordered per day and region, from the orders the tower receives
OVERRIDES = "demand_overrides"     # per item and region: a demand call that replaces the forecast for a while
ACTUALS_KEEP_DAYS = 42


# ---------------------------------------------------------------- feed schemas (master data template)

class ForecastRecord(BaseModel):
    upc: str = Field(pattern=r"^\d{12}$")
    region: str
    week_start: date
    units: float = Field(ge=0)


class SalesRecord(BaseModel):
    upc: str = Field(pattern=r"^\d{12}$")
    region: str
    week_start: date
    units: float = Field(ge=0)


class ColdStartRecord(BaseModel):
    upc: str = Field(pattern=r"^\d{12}$")
    region: str
    weekly_units: float = Field(ge=0)


def week_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def forecast_meta_key(tenant_id: str) -> str:
    return "forecast" if tenant_id == TENANT_ONE else f"forecast|{tenant_id}"


class PlanningAgent:
    def __init__(self, store: DocumentStore, bus: MessageBus, integration: IntegrationLayer,
                 governance: GovernanceService, approvals: ApprovalService, handoffs: HandoffService,
                 inventory: ReconciliationAgent, audit: AuditService, notifier: Notifier, directory: Directory,
                 clock: Clock, config: PilotConfig, llm: LLM | None = None):
        self.store, self.bus, self.integration, self.governance = store, bus, integration, governance
        self.approvals, self.handoffs, self.inventory, self.audit = approvals, handoffs, inventory, audit
        self.notifier, self.directory, self.clock, self.config = notifier, directory, clock, config
        self.reviewer = PlanReviewer(llm, config.plan_review)
        bus.subscribe(topics.ORDERS_RECEIVED, self.on_order, subscription="planning-agent")
        bus.subscribe(topics.INVENTORY_STATE, self.on_inventory_change, subscription="planning-agent")
        bus.subscribe(topics.APPROVAL_DECISIONS, self.on_approval, subscription="planning-agent")
        bus.subscribe(topics.TIMERS, self.on_timer, subscription="planning-agent")

    @property
    def regions(self) -> list[str]:
        return sorted({n.region for n in self.config.nodes.values() if n.region})

    def clients(self) -> list[str]:
        """Clients with a catalog to plan."""
        return [t for t in sorted(self.config.tenants) if self.config.catalog(t)]

    def owner(self, upc: str) -> str | None:
        return self.config.tenant_of_sku(upc)

    def _fenced(self, records: list[Any], tenant_id: str) -> list[Any]:
        """A client's feed may only speak for its own items (or items no client has listed yet)."""
        return [r for r in records if self.owner(r.upc) in (None, tenant_id)]

    # ---------------------------------------------------------------- feeds (R1.1, R1.2, R1.7, R1.8)

    def ingest_forecast(self, feed_id: str, records: list[dict[str, Any]], tenant_id: str = TENANT_ONE) -> FeedResult:
        self.governance.check_insight_access(AGENT, f"forecast:{feed_id}", tenant_id=tenant_id)
        res = self.integration.ingest_feed(feed_id, records, ForecastRecord,
                                           originating_party_contact=self._contact(tenant_id, DATA_CONTACT))
        valid = self._fenced(res.valid, tenant_id)
        for r in valid:
            self.store.put(DEMAND_PLAN, f"{r.upc}|{r.region}|{r.week_start}",
                           r.model_dump(mode="json") | {"tenant_id": tenant_id})
        self.store.put(META, forecast_meta_key(tenant_id), {"last_feed_id": feed_id, "tenant_id": tenant_id,
                                                            "received_at": self.clock.now().isoformat()})
        self.audit.decision(AGENT, "demand_plan_updated", input_refs=[f"feed:{feed_id}"],
                            result={"accepted": len(valid), "rejected": len(res.rejected),
                                    "other_clients_items": len(res.valid) - len(valid)},
                            skus=sorted({r.upc for r in valid}), owning_party=OwningParty.CORP_B, tenant_id=tenant_id)
        self.expect_next_forecast(after_feed_id=feed_id, tenant_id=tenant_id)
        for upc in sorted({r.upc for r in valid}):
            for node_id in self.config.nodes:
                self.evaluate_cover(upc, node_id)
        return res

    def expect_next_forecast(self, *, after_feed_id: str | None, tenant_id: str = TENANT_ONE) -> None:
        self.bus.publish(Event(topics.TIMERS, "forecast_feed_due", {"after_feed_id": after_feed_id, "tenant_id": tenant_id},
                               self.clock.now()), deliver_at=self.clock.now() + self.config.feed_delivery_window)

    def ingest_sales(self, feed_id: str, records: list[dict[str, Any]], tenant_id: str = TENANT_ONE) -> FeedResult:
        self.governance.check_insight_access(AGENT, f"sales:{feed_id}", tenant_id=tenant_id)
        res = self.integration.ingest_feed(feed_id, records, SalesRecord,
                                           originating_party_contact=self._contact(tenant_id, DATA_CONTACT))
        for r in self._fenced(res.valid, tenant_id):
            self.store.put(SALES, f"{r.upc}|{r.region}|{r.week_start}", r.model_dump(mode="json") | {"tenant_id": tenant_id})
        return res

    def ingest_cold_start(self, feed_id: str, records: list[dict[str, Any]], tenant_id: str = TENANT_ONE) -> FeedResult:
        self.governance.check_insight_access(AGENT, f"cold_start:{feed_id}", tenant_id=tenant_id)
        res = self.integration.ingest_feed(feed_id, records, ColdStartRecord,
                                           originating_party_contact=self._contact(tenant_id, DATA_CONTACT))
        for r in self._fenced(res.valid, tenant_id):
            self.store.put(COLD_START, f"{r.upc}|{r.region}", r.model_dump(mode="json") | {"tenant_id": tenant_id})
        return res

    # ---------------------------------------------------------------- demand model

    def baseline(self, upc: str, region: str) -> tuple[float | None, str]:
        """Weekly baseline: 4+ weeks of actual sales replace cold-start data (R1.7)."""
        weeks = sorted(self.store.query(SALES, upc=upc, region=region), key=lambda r: r["week_start"])
        if len(weeks) >= self.config.cold_start_replace_weeks:
            recent = weeks[-self.config.cold_start_replace_weeks:]
            return sum(w["units"] for w in recent) / len(recent), "actual_sales"
        cold = self.store.get(COLD_START, f"{upc}|{region}")
        if cold:
            return cold["weekly_units"], "cold_start"
        if weeks:
            return sum(w["units"] for w in weeks) / len(weeks), "partial_sales"
        return None, "none"

    def daily_demand(self, upc: str, region: str, on: date | None = None) -> float | None:
        """The daily rate planning uses: an active demand call, else the forecast (or baseline)."""
        if on is None:
            call = self.store.get(OVERRIDES, f"{upc}|{region}")
            if call and call["until"] > self.clock.now().isoformat():
                return call["daily_units"]
        return self.planned_daily(upc, region, on)

    def planned_daily(self, upc: str, region: str, on: date | None = None) -> float | None:
        """The client's plan: this week's forecast, the latest earlier one, or the baseline."""
        on = on or self.clock.now().date()
        week = week_of(on).isoformat()
        fc = self.store.get(DEMAND_PLAN, f"{upc}|{region}|{week}")
        if fc is None:
            # R1.8: a late or missing feed keeps the most recent valid plan in force
            past = [r for r in self.store.query(DEMAND_PLAN, upc=upc, region=region) if r["week_start"] <= week]
            fc = max(past, key=lambda r: r["week_start"]) if past else None
        if fc:
            return fc["units"] / 7
        weekly, _ = self.baseline(upc, region)
        return None if weekly is None else weekly / 7

    def node_daily_demand(self, upc: str, node_id: str) -> float | None:
        node = self.config.nodes[node_id]
        regions = [node.region] if node.region else self.regions
        rates = [self.daily_demand(upc, r) for r in regions]
        known = [r for r in rates if r is not None]
        return sum(known) if known else None

    def ats_cover(self, upc: str, node_id: str) -> float | None:
        dd = self.node_daily_demand(upc, node_id)
        if dd is None:
            return None
        position = self.inventory.on_hand(upc, node_id) + self.inventory.inbound_in_transit(upc, node_id)
        return math.inf if dd == 0 else position / dd

    # ---------------------------------------------------------------- recent orders and demand calls

    def on_order(self, event: Event) -> None:
        """Count each order's units per item, day and region (only the ship-to state is read)."""
        p = event.payload
        if event.type == "order_received":
            state = ((p.get("consumer") or {}).get("address") or {}).get("state")
            lines = [(ln["upc"], int(ln["quantity"])) for ln in p.get("lines", [])]
        elif event.type == "store_order_received":
            state = (p.get("ship_to") or {}).get("state")
            lines = [(ln["upc"], int(ln["cases"]) * int(ln["case_pack"])) for ln in p.get("lines", [])]
        else:
            return
        region, day, order_id = self.config.region_of_state(state), str(p.get("placed_at", ""))[:10], p.get("order_id")
        if not day:
            return
        for upc, qty in lines:
            tenant_id = self.owner(upc)
            if tenant_id is None:
                continue
            doc = self.store.get(ACTUALS, upc) or {"upc": upc, "tenant_id": tenant_id, "first_day": day, "days": {}}
            d = doc["days"].setdefault(day, {"ids": []})
            if order_id in d["ids"]:
                continue  # delivered twice
            d["ids"].append(order_id)
            d[region] = d.get(region, 0) + qty
            d["max_line"] = max(d.get("max_line", 0), qty)
            cutoff = (date.fromisoformat(day) - timedelta(days=ACTUALS_KEEP_DAYS)).isoformat()
            doc["days"] = {k: v for k, v in doc["days"].items() if k >= cutoff}
            doc["first_day"] = min(doc["first_day"], day)
            self.store.put(ACTUALS, upc, doc)

    def recent_orders(self, upc: str, days: int, doc: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Units ordered over the last `days` full days per region, or None when we have not seen that long."""
        doc = doc if doc is not None else self.store.get(ACTUALS, upc)
        today = self.clock.now().date()
        start = today - timedelta(days=days)
        if not doc or doc["first_day"] > start.isoformat():
            return None
        window = [v for k, v in doc["days"].items() if start.isoformat() <= k < today.isoformat()]
        return {r: sum(v.get(r, 0) for v in window) for r in self.regions}

    def drift(self, upc: str) -> dict[str, Any] | None:
        """Regions where recent orders and the client's plan disagree by more than the drift ratio."""
        n = self.config.drift_window_days
        actual = self.recent_orders(upc, n)
        if actual is None:
            return None
        out = {}
        for region in self.regions:
            plan = (self.planned_daily(upc, region) or 0) * n
            got = actual[region]
            if max(plan, got) < self.config.drift_min_units:
                continue
            ratio = got / plan if plan else float("inf")
            if ratio >= self.config.drift_ratio or ratio <= 1 / self.config.drift_ratio:
                out[region] = {"orders": got, "plan": round(plan, 1), "ratio": None if plan == 0 else round(ratio, 2)}
        return out or None

    def evidence(self, upc: str) -> dict[str, Any]:
        """What a planner looks at for one item: the plan, the client's sales feed, our recent orders day by
        day with the largest single line, and stock by node."""
        today = self.clock.now().date()
        week = week_of(today)
        doc = self.store.get(ACTUALS, upc) or {"days": {}}
        days = []
        for i in range(28, 0, -1):
            d = (today - timedelta(days=i)).isoformat()
            v = doc["days"].get(d)
            if v is not None or doc.get("first_day", "9") <= d:
                v = v or {}
                days.append({"day": d, **{r: v.get(r, 0) for r in self.regions}, "largest_line": v.get("max_line", 0)})
        forecast = {r: [{"week_start": (week + timedelta(weeks=w)).isoformat(),
                         "units": round((self.planned_daily(upc, r, week + timedelta(weeks=w)) or 0) * 7, 1)}
                        for w in range(4)] for r in self.regions}
        sales = sorted(({"week_start": s["week_start"], "region": s["region"], "units": s["units"]}
                        for s in self.store.query(SALES, upc=upc)), key=lambda s: s["week_start"])[-8:]
        stock = {n: {"kind": info.kind, "region": info.region, "on_hand": self.inventory.on_hand(upc, n),
                     "inbound": self.inventory.inbound_in_transit(upc, n)} for n, info in self.config.nodes.items()}
        calls = [c for c in (self.store.get(OVERRIDES, f"{upc}|{r}") for r in self.regions) if c]
        return {"upc": upc, "today": today.isoformat(), "unit_cost": self.config.unit_cost.get(upc, self.config.default_unit_cost),
                "client_forecast_weekly": forecast, "client_sales_feed_weekly": sales,
                "our_orders_daily": days, "stock_by_node": stock,
                "current_demand_calls": [{k: c[k] for k in ("region", "daily_units", "basis", "until", "by")} for c in calls]}

    def sense_demand(self, tenant_id: str) -> list[dict[str, Any]]:
        """For each drifting item, decide the demand to plan with: a fixed blend (sensing) or Claude's call."""
        mode = self.config.plan_review
        if mode == "rules":
            return []
        now, calls = self.clock.now(), []
        until = (now + timedelta(days=self.config.demand_override_days)).isoformat()
        for upc in sorted(self.config.catalog(tenant_id)):
            if any((c := self.store.get(OVERRIDES, f"{upc}|{r}")) and c["until"] > now.isoformat() for r in self.regions):
                continue  # a call is in force; it is looked at again when it ends
            drift = self.drift(upc)
            if not drift:
                continue
            n = self.config.drift_window_days
            if mode == "claude" and self.reviewer.active:
                call = self.reviewer.demand(self.evidence(upc) | {"drift": drift})
                if call is None:
                    continue  # the forecast stands
                rates = {r.region: r.daily_units for r in call.rates if r.region in self.regions}
                cap = {r: 3 * max(drift.get(r, {}).get("orders", 0) / n, self.planned_daily(upc, r) or 0, 1 / n)
                       for r in self.regions}
                rates = {r: round(min(v, cap[r]), 3) for r, v in rates.items()}
                basis, why, by, tell = call.basis, call.rationale, "claude", call.tell_client
            else:
                rates = {r: round(SENSING_WEIGHT * d["orders"] / n + (1 - SENSING_WEIGHT) * d["plan"] / n, 3)
                         for r, d in drift.items()}
                basis, by, tell = "blend", "sensing", False
                why = f"recent orders drift from the forecast; planning with {SENSING_WEIGHT:.0%} orders"
            for region, rate in rates.items():
                self.store.put(OVERRIDES, f"{upc}|{region}", {"upc": upc, "region": region, "tenant_id": tenant_id,
                                                              "daily_units": rate, "basis": basis, "by": by,
                                                              "rationale": why, "set_at": now.isoformat(), "until": until})
            self.audit.decision(AGENT, "demand_call", rationale=why, result={"rates": rates, "basis": basis, "by": by,
                                                                            "drift": drift},
                                skus=[upc], owning_party=OwningParty.CORP_B, tenant_id=tenant_id)
            if tell:
                self.notifier.send(self._contact(tenant_id, PLANNER), f"Forecast for {upc} looks off",
                                   f"{why}\n\nPlanning with {json.dumps(rates)} units a day per region until {until[:10]}.")
            calls.append({"upc": upc, "rates": rates, "basis": basis, "by": by})
        return calls

    # ---------------------------------------------------------------- replenishment (R1.3, R1.4, R8.1)

    def on_inventory_change(self, event: Event) -> None:
        self.evaluate_cover(event.payload["sku"], event.payload["node_id"])

    def evaluate_cover(self, upc: str, node_id: str) -> dict[str, Any] | None:
        tenant_id = self.owner(upc)
        if tenant_id is None or node_id not in self.config.nodes or self.config.nodes[node_id].kind != "FC":
            return None  # only fulfillment centres serve demand; storage and the cross-dock feed them
        cover = self.ats_cover(upc, node_id)
        self.store.put(COVER, f"{upc}|{node_id}", {"upc": upc, "node_id": node_id, "tenant_id": tenant_id,
                                                   "at": self.clock.now().isoformat(),
                                                   "cover_days": None if cover in (None, math.inf) else round(cover, 2)})
        if cover is None or cover >= self.config.min_cover_days:
            return None
        return self._recommend(upc, node_id, cover, tenant_id)

    def _recommend(self, upc: str, target: str, cover: float, tenant_id: str) -> dict[str, Any] | None:
        now = self.clock.now()
        for r in self.store.query(RECS, upc=upc, target=target):
            if r["status"] == "pending_approval" or (
                    r["status"] in ("released", "held") and now - _dt(r["created_at"]) < self.config.replenishment_cooldown):
                return None
        dd = self.node_daily_demand(upc, target) or 0
        position = self.inventory.on_hand(upc, target) + self.inventory.inbound_in_transit(upc, target)
        need = math.ceil(self.config.target_cover_days * dd - position)
        if need <= 0:
            return None
        kinds = ("IXD", "ISC") if self.config.nodes[target].kind == "FC" else ("ISC",)
        sources = [(self.inventory.on_hand(upc, n), n) for n, info in self.config.nodes.items()
                   if info.kind in kinds and n != target]
        sources = sorted([s for s in sources if s[0] > 0], key=lambda s: (kinds.index(self.config.nodes[s[1]].kind), -s[0]))
        if not sources:
            self.notifier.send(self._contact(tenant_id, PLANNER), f"No source inventory for {upc}",
                               f"{target} cover {cover:.1f} days, needs {need} units", severity="high")
            return None
        available, source = sources[0]
        qty = min(need, available)
        unit = self.config.unit_cost.get(upc, self.config.default_unit_cost)
        rec = {"rec_id": new_id("rep"), "tenant_id": tenant_id, "upc": upc, "source": source, "target": target, "qty": qty,
               "value": round(qty * unit, 2), "cover_days": round(cover, 2), "created_at": now.isoformat(),
               "status": "new",
               "rationale": (f"projected ATS cover {cover:.1f}d at {target} is below the {self.config.min_cover_days}d "
                             f"minimum; daily demand {dd:.2f}; moving {qty} units from {source} restores "
                             f"~{self.config.target_cover_days}d")}
        self.audit.decision(AGENT, "replenishment_recommended", rationale=rec["rationale"], subject_id=rec["rec_id"],
                            result={"qty": qty, "value": rec["value"]}, skus=[upc], node_ids=[source, target],
                            owning_party=OwningParty.CORP_B, tenant_id=tenant_id)
        escalate = False
        review = self.reviewer.restock(self.evidence(upc) | {"proposal": {k: rec[k] for k in (
            "source", "target", "qty", "value", "cover_days", "rationale")}, "source_available": available})
        if review is not None:
            rec["review"] = review.model_dump()
            self.audit.decision(AGENT, "replenishment_reviewed", rationale=review.rationale, subject_id=rec["rec_id"],
                                result={"decision": review.decision, "qty": review.qty}, skus=[upc],
                                node_ids=[source, target], owning_party=OwningParty.CORP_B, tenant_id=tenant_id)
            if review.decision == "hold":
                rec["status"] = "held"
                self.store.put(RECS, rec["rec_id"], rec)
                return rec
            if review.decision == "adjust" and review.qty is not None:
                qty = min(review.qty, 2 * rec["qty"], available)
                if qty <= 0:
                    rec["status"] = "held"
                    self.store.put(RECS, rec["rec_id"], rec)
                    return rec
                rec["qty"], rec["value"] = qty, round(qty * unit, 2)
            escalate = review.decision == "escalate"
            rec["rationale"] += f"; reviewed: {review.rationale}"
        if escalate or rec["value"] > self.config.replenishment_value_threshold:
            item = self.approvals.submit(category=ApprovalCategory.REPLENISHMENT, owning_party=OwningParty.CORP_B,
                                         agent=AGENT, subject_id=rec["rec_id"], rationale=rec["rationale"],
                                         tenant_id=tenant_id,
                                         content={k: rec[k] for k in ("upc", "source", "target", "qty", "value")}
                                         | {"skus": [upc], "node_ids": [source, target]})
            rec["status"], rec["approval_id"] = "pending_approval", item.approval_id
            self.store.put(RECS, rec["rec_id"], rec)
        else:
            self.store.put(RECS, rec["rec_id"], rec)
            self._release_replenishment(rec)
        return self.store.get(RECS, rec["rec_id"])

    def _release_replenishment(self, rec: dict[str, Any]) -> None:
        tenant_id = rec.get("tenant_id") or TENANT_ONE
        if self.config.nodes[rec["source"]].kind == "ISC":
            res = self.integration.call("CreateInboundShipmentPlan", {
                "planId": rec["rec_id"], "shipFrom": rec["source"], "shipTo": rec["target"],
                "items": [{"sellerSku": rec["upc"], "quantity": rec["qty"]}]}, agent=AGENT)
            rec["execution"] = {"mcf_ok": res.ok, "error": res.error_code}
        else:
            h = self.handoffs.create("execute_transfer", requesting_party=OwningParty.CORP_B, requested_by=AGENT,
                                     blocked_steps=[f"{rec['rec_id']}:transfer"],
                                     context={"rec_id": rec["rec_id"], "sku": rec["upc"], "from": rec["source"],
                                              "to": rec["target"], "qty": rec["qty"]}, tenant_id=tenant_id)
            rec["execution"] = {"handoff_id": h.handoff_id}
        rec["status"] = "released"
        self.store.put(RECS, rec["rec_id"], rec)
        self.audit.decision(AGENT, "replenishment_released", subject_id=rec["rec_id"], result=rec["execution"],
                            skus=[rec["upc"]], node_ids=[rec["source"], rec["target"]], owning_party=OwningParty.CORP_B,
                            tenant_id=tenant_id)

    # ---------------------------------------------------------------- imbalance + distribution plan (R1.5, R1.6)

    def assess_imbalance(self, tenant_id: str = TENANT_ONE) -> list[dict[str, Any]]:
        out = []
        horizon = self.config.imbalance_horizon_days
        for upc in sorted(self.config.catalog(tenant_id)):
            for region in self.regions:
                dd = self.daily_demand(upc, region)
                if dd is None:
                    continue
                fcs = [n for n, i in self.config.nodes.items() if i.kind == "FC" and i.region == region]
                position = sum(self.inventory.on_hand(upc, n) + self.inventory.inbound_in_transit(upc, n) for n in fcs)
                projection = dd * horizon
                if projection == 0:
                    if position > 0:
                        out.append({"upc": upc, "tenant_id": tenant_id, "region": region, "status": "oversupplied",
                                    "position": position,
                                    "projection": 0, "deviation": None, "nodes": fcs})
                    continue
                deviation = (position - projection) / projection
                if abs(deviation) > self.config.imbalance_threshold:
                    out.append({"upc": upc, "tenant_id": tenant_id, "region": region, "position": position,
                                "projection": round(projection, 1),
                                "deviation": round(deviation, 3), "nodes": fcs,
                                "status": "oversupplied" if deviation > 0 else "undersupplied"})
        return out

    def draft_distribution_plan(self, imbalances: list[dict[str, Any]], tenant_id: str = TENANT_ONE) -> dict[str, Any] | None:
        if any((p.get("tenant_id") or TENANT_ONE) == tenant_id for p in self.store.query(PLANS, status="pending_approval")):
            return None  # one plan in review at a time for each client
        imbalances = [i for i in imbalances if (i.get("tenant_id") or TENANT_ONE) == tenant_id]
        moves = []
        for upc in sorted({i["upc"] for i in imbalances}):
            over = [i for i in imbalances if i["upc"] == upc and i["status"] == "oversupplied"]
            under = [i for i in imbalances if i["upc"] == upc and i["status"] == "undersupplied"]
            for u in under:
                shortfall = u["projection"] - u["position"]
                for o in over:
                    excess = o["position"] - o["projection"]
                    qty = int(min(shortfall, excess))
                    if qty <= 0:
                        continue
                    moves.append({"upc": upc, "from_node": o["nodes"][0], "to_node": u["nodes"][0], "qty": qty})
                    o["position"] -= qty
                    shortfall -= qty
        if not moves:
            return None
        rationale = f"{len(moves)} moves rebalance regional inventory against the {self.config.imbalance_horizon_days}-day projection"
        review = self.reviewer.plan({"moves": [{"index": i, **m} for i, m in enumerate(moves)],
                                     "imbalances": imbalances,
                                     "items": {u: self.evidence(u) for u in sorted({m["upc"] for m in moves})}})
        if review is not None:
            keep = {m.index: m.qty for m in review.moves}
            reviewed = [{**m, "qty": min(m["qty"], keep.get(i, m["qty"]))} for i, m in enumerate(moves)]
            self.audit.decision(AGENT, "distribution_plan_reviewed", rationale=review.rationale,
                                result={"proposed": moves, "kept": [m for m in reviewed if m["qty"] > 0]},
                                skus=sorted({m["upc"] for m in moves}), owning_party=OwningParty.CORP_B,
                                tenant_id=tenant_id)
            moves = [m for m in reviewed if m["qty"] > 0]
            if not moves:
                return None
            rationale += f"; reviewed: {review.rationale}"
        plan = {"plan_id": new_id("dpl"), "tenant_id": tenant_id, "created_at": self.clock.now().isoformat(), "moves": moves,
                "status": "pending_approval", "imbalances": imbalances}
        item = self.approvals.submit(category=ApprovalCategory.DISTRIBUTION_PLAN, owning_party=OwningParty.CORP_B,
                                     agent=AGENT, subject_id=plan["plan_id"], rationale=rationale, tenant_id=tenant_id,
                                     content={"moves": moves, "skus": sorted({m["upc"] for m in moves}),
                                              "node_ids": sorted({n for m in moves for n in (m["from_node"], m["to_node"])})})
        plan["approval_id"] = item.approval_id
        self.store.put(PLANS, plan["plan_id"], plan)
        self.audit.decision(AGENT, "distribution_plan_drafted", rationale=rationale, subject_id=plan["plan_id"],
                            skus=sorted({m["upc"] for m in moves}), owning_party=OwningParty.CORP_B, tenant_id=tenant_id)
        return plan

    def on_approval(self, event: Event) -> None:
        if event.payload.get("originating_agent") != AGENT:
            return
        subject = event.payload["subject_id"]
        approved = event.type == "approved"
        if subject.startswith("rep_"):
            rec = self.store.get(RECS, subject)
            if rec and rec["status"] == "pending_approval":
                if approved and self.approvals.is_executable(rec["approval_id"]):
                    self._release_replenishment(rec)
                else:
                    rec["status"] = "rejected"
                    self.store.put(RECS, subject, rec)
        elif subject.startswith("dpl_"):
            plan = self.store.get(PLANS, subject)
            if plan and plan["status"] == "pending_approval":
                if approved and self.approvals.is_executable(plan["approval_id"]):
                    res = self.integration.call("ApproveDistributionPlan",
                                                {"planId": subject, "moves": plan["moves"]}, agent=AGENT)
                    plan["status"] = "approved" if res.ok else "approval_submission_failed"
                else:
                    plan["status"] = "rejected"
                self.store.put(PLANS, subject, plan)

    # ---------------------------------------------------------------- daily run (R1.3, R1.5, R1.6, R1.9)

    def run_daily(self) -> dict[str, Any]:
        """Every client: cover and restock, missing baselines, regional imbalance and a distribution plan.
        The top-level keys are tenant one's (as before); `clients` has every client's."""
        clients = {tid: self.run_daily_for(tid) for tid in self.clients()}
        return {**(clients.get(TENANT_ONE) or {"recommendations": [], "no_baseline": [], "imbalances": [],
                                               "distribution_plan": None, "demand_calls": []}), "clients": clients}

    def run_daily_for(self, tenant_id: str) -> dict[str, Any]:
        calls = self.sense_demand(tenant_id)
        recs, flagged = [], []
        week = week_of(self.clock.now().date())
        for upc in sorted(self.config.catalog(tenant_id)):
            if all(self.baseline(upc, r)[0] is None for r in self.regions) and not any(
                    self.store.get(DEMAND_PLAN, f"{upc}|{r}|{week}") for r in self.regions):
                flagged.append(upc)
            for node_id in self.config.nodes:
                rec = self.evaluate_cover(upc, node_id)
                if rec:
                    recs.append(rec["rec_id"])
        new_flags = [u for u in flagged if not self.store.get(FLAGS, u)]
        for u in new_flags:
            self.store.put(FLAGS, u, {"upc": u, "tenant_id": tenant_id, "flagged_at": self.clock.now().isoformat()})
        if new_flags:
            self.notifier.send(self._contact(tenant_id, DATA_CONTACT), "SKUs lacking a demand baseline",
                               json.dumps({"upcs": new_flags}), severity="high")
            self.audit.decision(AGENT, "baseline_missing_flagged", skus=new_flags, owning_party=OwningParty.CORP_B,
                                tenant_id=tenant_id)
        imbalances = self.assess_imbalance(tenant_id)
        plan = self.draft_distribution_plan(imbalances, tenant_id)
        return {"recommendations": recs, "no_baseline": flagged, "imbalances": imbalances,
                "distribution_plan": plan["plan_id"] if plan else None, "demand_calls": calls}

    # ---------------------------------------------------------------- timers

    def on_timer(self, event: Event) -> None:
        if event.type != "forecast_feed_due":
            return
        tenant_id = event.payload.get("tenant_id") or TENANT_ONE
        if tenant_id not in self.config.tenants:
            return  # no longer a client
        meta = self.store.get(META, forecast_meta_key(tenant_id)) or {}
        if meta.get("last_feed_id") != event.payload.get("after_feed_id"):
            return  # a newer feed arrived
        self.notifier.send(self._contact(tenant_id, DATA_CONTACT), "Demand forecast feed missing",
                           f"No forecast feed after {meta.get('last_feed_id')}; planning continues on the last valid plan.",
                           severity="high")
        self.audit.decision(AGENT, "forecast_feed_missing", input_refs=[f"feed:{meta.get('last_feed_id')}"],
                            owning_party=OwningParty.CORP_B, tenant_id=tenant_id)
        self.expect_next_forecast(after_feed_id=meta.get("last_feed_id"), tenant_id=tenant_id)

    def _contact(self, tenant_id: str, role: str) -> str:
        name = f"{tenant_id}_{role}"
        return self.directory.contact(name) or name


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)
