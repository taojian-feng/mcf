"""Planning_Agent (R1): demand plan, ATS cover, replenishment, imbalance and distribution planning."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import ApprovalCategory, OwningParty, new_id
from tower.core.ports import Clock, Directory, DocumentStore, Event, MessageBus, Notifier
from tower.services.approval import ApprovalService
from tower.services.audit import AuditService
from tower.services.governance import GovernanceService
from tower.services.handoffs import HandoffService
from tower.services.integration import FeedResult, IntegrationLayer

from .reconciliation import ReconciliationAgent

AGENT = "planning_agent"
DATA_CONTACT = "corp_b_data_contact"
PLANNER = "corp_b_planner"

DEMAND_PLAN = "demand_plan"
SALES = "sales_history"
COLD_START = "cold_start"
META = "planning_meta"
RECS = "replenishment_recs"
PLANS = "distribution_plans"
COVER = "ats_cover"
FLAGS = "baseline_flags"


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


class PlanningAgent:
    def __init__(self, store: DocumentStore, bus: MessageBus, integration: IntegrationLayer,
                 governance: GovernanceService, approvals: ApprovalService, handoffs: HandoffService,
                 inventory: ReconciliationAgent, audit: AuditService, notifier: Notifier, directory: Directory,
                 clock: Clock, config: PilotConfig):
        self.store, self.bus, self.integration, self.governance = store, bus, integration, governance
        self.approvals, self.handoffs, self.inventory, self.audit = approvals, handoffs, inventory, audit
        self.notifier, self.directory, self.clock, self.config = notifier, directory, clock, config
        bus.subscribe(topics.INVENTORY_STATE, self.on_inventory_change, subscription="planning-agent")
        bus.subscribe(topics.APPROVAL_DECISIONS, self.on_approval, subscription="planning-agent")
        bus.subscribe(topics.TIMERS, self.on_timer, subscription="planning-agent")

    @property
    def regions(self) -> list[str]:
        return sorted({n.region for n in self.config.nodes.values() if n.region})

    # ---------------------------------------------------------------- feeds (R1.1, R1.2, R1.7, R1.8)

    def ingest_forecast(self, feed_id: str, records: list[dict[str, Any]]) -> FeedResult:
        self.governance.check_insight_access(AGENT, f"forecast:{feed_id}")
        res = self.integration.ingest_feed(feed_id, records, ForecastRecord,
                                           originating_party_contact=self._contact(DATA_CONTACT))
        for r in res.valid:
            self.store.put(DEMAND_PLAN, f"{r.upc}|{r.region}|{r.week_start}", r.model_dump(mode="json"))
        self.store.put(META, "forecast", {"last_feed_id": feed_id, "received_at": self.clock.now().isoformat()})
        self.audit.decision(AGENT, "demand_plan_updated", input_refs=[f"feed:{feed_id}"],
                            result={"accepted": len(res.valid), "rejected": len(res.rejected)},
                            skus=sorted({r.upc for r in res.valid}), owning_party=OwningParty.CORP_B)
        self.expect_next_forecast(after_feed_id=feed_id)
        for upc in sorted({r.upc for r in res.valid}):
            for node_id in self.config.nodes:
                self.evaluate_cover(upc, node_id)
        return res

    def expect_next_forecast(self, *, after_feed_id: str | None) -> None:
        self.bus.publish(Event(topics.TIMERS, "forecast_feed_due", {"after_feed_id": after_feed_id},
                               self.clock.now()), deliver_at=self.clock.now() + self.config.feed_delivery_window)

    def ingest_sales(self, feed_id: str, records: list[dict[str, Any]]) -> FeedResult:
        self.governance.check_insight_access(AGENT, f"sales:{feed_id}")
        res = self.integration.ingest_feed(feed_id, records, SalesRecord,
                                           originating_party_contact=self._contact(DATA_CONTACT))
        for r in res.valid:
            self.store.put(SALES, f"{r.upc}|{r.region}|{r.week_start}", r.model_dump(mode="json"))
        return res

    def ingest_cold_start(self, feed_id: str, records: list[dict[str, Any]]) -> FeedResult:
        self.governance.check_insight_access(AGENT, f"cold_start:{feed_id}")
        res = self.integration.ingest_feed(feed_id, records, ColdStartRecord,
                                           originating_party_contact=self._contact(DATA_CONTACT))
        for r in res.valid:
            self.store.put(COLD_START, f"{r.upc}|{r.region}", r.model_dump(mode="json"))
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

    # ---------------------------------------------------------------- replenishment (R1.3, R1.4, R8.1)

    def on_inventory_change(self, event: Event) -> None:
        self.evaluate_cover(event.payload["sku"], event.payload["node_id"])

    def evaluate_cover(self, upc: str, node_id: str) -> dict[str, Any] | None:
        if upc not in self.config.pilot_upcs or self.config.nodes[node_id].kind == "ISC":
            return None
        cover = self.ats_cover(upc, node_id)
        self.store.put(COVER, f"{upc}|{node_id}", {"upc": upc, "node_id": node_id, "at": self.clock.now().isoformat(),
                                                   "cover_days": None if cover in (None, math.inf) else round(cover, 2)})
        if cover is None or cover >= self.config.min_cover_days:
            return None
        return self._recommend(upc, node_id, cover)

    def _recommend(self, upc: str, target: str, cover: float) -> dict[str, Any] | None:
        now = self.clock.now()
        for r in self.store.query(RECS, upc=upc, target=target):
            if r["status"] == "pending_approval" or (
                    r["status"] == "released" and now - _dt(r["created_at"]) < self.config.replenishment_cooldown):
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
            self.notifier.send(self._contact(PLANNER), f"No source inventory for {upc}",
                               f"{target} cover {cover:.1f} days, needs {need} units", severity="high")
            return None
        available, source = sources[0]
        qty = min(need, available)
        unit = self.config.unit_cost.get(upc, self.config.default_unit_cost)
        rec = {"rec_id": new_id("rep"), "upc": upc, "source": source, "target": target, "qty": qty,
               "value": round(qty * unit, 2), "cover_days": round(cover, 2), "created_at": now.isoformat(),
               "status": "new",
               "rationale": (f"projected ATS cover {cover:.1f}d at {target} is below the {self.config.min_cover_days}d "
                             f"minimum; daily demand {dd:.2f}; moving {qty} units from {source} restores "
                             f"~{self.config.target_cover_days}d")}
        self.audit.decision(AGENT, "replenishment_recommended", rationale=rec["rationale"], subject_id=rec["rec_id"],
                            result={"qty": qty, "value": rec["value"]}, skus=[upc], node_ids=[source, target],
                            owning_party=OwningParty.CORP_B)
        if rec["value"] > self.config.replenishment_value_threshold:
            item = self.approvals.submit(category=ApprovalCategory.REPLENISHMENT, owning_party=OwningParty.CORP_B,
                                         agent=AGENT, subject_id=rec["rec_id"], rationale=rec["rationale"],
                                         content={k: rec[k] for k in ("upc", "source", "target", "qty", "value")}
                                         | {"skus": [upc], "node_ids": [source, target]})
            rec["status"], rec["approval_id"] = "pending_approval", item.approval_id
            self.store.put(RECS, rec["rec_id"], rec)
        else:
            self.store.put(RECS, rec["rec_id"], rec)
            self._release_replenishment(rec)
        return self.store.get(RECS, rec["rec_id"])

    def _release_replenishment(self, rec: dict[str, Any]) -> None:
        if self.config.nodes[rec["source"]].kind == "ISC":
            res = self.integration.call("CreateInboundShipmentPlan", {
                "planId": rec["rec_id"], "shipFrom": rec["source"], "shipTo": rec["target"],
                "items": [{"sellerSku": rec["upc"], "quantity": rec["qty"]}]}, agent=AGENT)
            rec["execution"] = {"mcf_ok": res.ok, "error": res.error_code}
        else:
            h = self.handoffs.create("execute_transfer", requesting_party=OwningParty.CORP_B, requested_by=AGENT,
                                     blocked_steps=[f"{rec['rec_id']}:transfer"],
                                     context={"rec_id": rec["rec_id"], "sku": rec["upc"], "from": rec["source"],
                                              "to": rec["target"], "qty": rec["qty"]})
            rec["execution"] = {"handoff_id": h.handoff_id}
        rec["status"] = "released"
        self.store.put(RECS, rec["rec_id"], rec)
        self.audit.decision(AGENT, "replenishment_released", subject_id=rec["rec_id"], result=rec["execution"],
                            skus=[rec["upc"]], node_ids=[rec["source"], rec["target"]], owning_party=OwningParty.CORP_B)

    # ---------------------------------------------------------------- imbalance + distribution plan (R1.5, R1.6)

    def assess_imbalance(self) -> list[dict[str, Any]]:
        out = []
        horizon = self.config.imbalance_horizon_days
        for upc in sorted(self.config.pilot_upcs):
            for region in self.regions:
                dd = self.daily_demand(upc, region)
                if dd is None:
                    continue
                fcs = [n for n, i in self.config.nodes.items() if i.kind == "FC" and i.region == region]
                position = sum(self.inventory.on_hand(upc, n) + self.inventory.inbound_in_transit(upc, n) for n in fcs)
                projection = dd * horizon
                if projection == 0:
                    if position > 0:
                        out.append({"upc": upc, "region": region, "status": "oversupplied", "position": position,
                                    "projection": 0, "deviation": None, "nodes": fcs})
                    continue
                deviation = (position - projection) / projection
                if abs(deviation) > self.config.imbalance_threshold:
                    out.append({"upc": upc, "region": region, "position": position, "projection": round(projection, 1),
                                "deviation": round(deviation, 3), "nodes": fcs,
                                "status": "oversupplied" if deviation > 0 else "undersupplied"})
        return out

    def draft_distribution_plan(self, imbalances: list[dict[str, Any]]) -> dict[str, Any] | None:
        if self.store.query(PLANS, status="pending_approval"):
            return None  # one plan in review at a time
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
        plan = {"plan_id": new_id("dpl"), "created_at": self.clock.now().isoformat(), "moves": moves,
                "status": "pending_approval", "imbalances": imbalances}
        rationale = f"{len(moves)} moves rebalance regional inventory against the {self.config.imbalance_horizon_days}-day projection"
        item = self.approvals.submit(category=ApprovalCategory.DISTRIBUTION_PLAN, owning_party=OwningParty.CORP_B,
                                     agent=AGENT, subject_id=plan["plan_id"], rationale=rationale,
                                     content={"moves": moves, "skus": sorted({m["upc"] for m in moves}),
                                              "node_ids": sorted({n for m in moves for n in (m["from_node"], m["to_node"])})})
        plan["approval_id"] = item.approval_id
        self.store.put(PLANS, plan["plan_id"], plan)
        self.audit.decision(AGENT, "distribution_plan_drafted", rationale=rationale, subject_id=plan["plan_id"],
                            skus=sorted({m["upc"] for m in moves}), owning_party=OwningParty.CORP_B)
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
        recs, flagged = [], []
        for upc in sorted(self.config.pilot_upcs):
            if all(self.baseline(upc, r)[0] is None for r in self.regions) and not any(
                    self.store.get(DEMAND_PLAN, f"{upc}|{r}|{week_of(self.clock.now().date())}") for r in self.regions):
                flagged.append(upc)
            for node_id in self.config.nodes:
                rec = self.evaluate_cover(upc, node_id)
                if rec:
                    recs.append(rec["rec_id"])
        new_flags = [u for u in flagged if not self.store.get(FLAGS, u)]
        for u in new_flags:
            self.store.put(FLAGS, u, {"upc": u, "flagged_at": self.clock.now().isoformat()})
        if new_flags:
            self.notifier.send(self._contact(DATA_CONTACT), "SKUs lacking a demand baseline",
                               json.dumps({"upcs": new_flags}), severity="high")
            self.audit.decision(AGENT, "baseline_missing_flagged", skus=new_flags, owning_party=OwningParty.CORP_B)
        imbalances = self.assess_imbalance()
        plan = self.draft_distribution_plan(imbalances)
        return {"recommendations": recs, "no_baseline": flagged, "imbalances": imbalances,
                "distribution_plan": plan["plan_id"] if plan else None}

    # ---------------------------------------------------------------- timers

    def on_timer(self, event: Event) -> None:
        if event.type != "forecast_feed_due":
            return
        meta = self.store.get(META, "forecast") or {}
        if meta.get("last_feed_id") != event.payload.get("after_feed_id"):
            return  # a newer feed arrived
        self.notifier.send(self._contact(DATA_CONTACT), "Demand forecast feed missing",
                           f"No forecast feed after {meta.get('last_feed_id')}; planning continues on the last valid plan.",
                           severity="high")
        self.audit.decision(AGENT, "forecast_feed_missing", input_refs=[f"feed:{meta.get('last_feed_id')}"],
                            owning_party=OwningParty.CORP_B)
        self.expect_next_forecast(after_feed_id=meta.get("last_feed_id"))

    def _contact(self, name: str) -> str:
        return self.directory.contact(name) or name



def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)
