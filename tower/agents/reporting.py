"""Reporting_Agent (R6): daily metrics, anomaly detection, export feed, weekly and QBR summaries.

One set per client: each client's metrics come from its own orders, shipments, stock, forecasts and sales,
its anomalies are judged against its own history, and its summaries go to `<client>_review` and Corp A's
review contact. Tenant one's records keep their original ids and paths; other clients' are prefixed with
the client id."""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any

from pydantic import BaseModel

from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import TERMINAL_EXCEPTION_STATES, ExceptionStatus, InventoryEventType, OwningParty
from tower.core.ports import LLM, BlobStore, Clock, Directory, DocumentStore, Event, MessageBus, Notifier
from tower.core.tenants import TENANT_ONE
from tower.services.audit import OPS_TEAM, AuditService
from tower.services.coordination import coordination
from tower.services.coordination import render as render_coordination
from tower.services.governance import AccessDenied, GovernanceService

from .planning import DEMAND_PLAN, RECS as REPLENISHMENT_RECS, SALES, PlanningAgent, week_of
from .reconciliation import EVENTS as INVENTORY_EVENTS, STATE as INVENTORY_STATE

AGENT = "reporting_agent"
METRICS = "metrics_daily"
SUMMARIES = "summaries"
FACTS = "shipment_facts"
PROVIDER_REVIEW = "corp_a_review"


def review_recipients(tenant_id: str) -> tuple[str, str]:
    return f"{tenant_id}_review", PROVIDER_REVIEW


def metrics_key(tenant_id: str, day: str) -> str:
    return day if tenant_id == TENANT_ONE else f"{tenant_id}|{day}"


def summary_key(kind: str, tenant_id: str, period_end: str) -> str:
    return f"{kind}|{period_end}" if tenant_id == TENANT_ONE else f"{kind}|{tenant_id}|{period_end}"


def _path(tenant_id: str, path: str) -> str:
    return path if tenant_id == TENANT_ONE else f"{tenant_id}/{path}"

METRIC_NAMES = (
    "per_unit_fulfillment_cost", "inventory_turns", "days_of_cover", "on_time_delivery_rate",
    "delivery_speed_days", "line_split_fulfillment_rate", "pct_ground_transport", "avg_final_mile_miles",
    "forecast_accuracy", "replenishment_accuracy",
)
# Derived from CorpB_Insight_Data: only recipients on the pilot authorization list receive them (R9.1).
INSIGHT_METRICS = {"days_of_cover", "forecast_accuracy", "replenishment_accuracy"}


class Narrative(BaseModel):
    headline: str
    highlights: list[str]


def _m(value: float | None, *, status: str = "ok", missing: str | None = None, refs: list[str] | None = None,
       **extra: Any) -> dict[str, Any]:
    return {"value": None if value is None else round(value, 4), "status": status, "missing_source": missing,
            "refs": (refs or [])[:50], **extra}


class ReportingAgent:
    def __init__(self, store: DocumentStore, bus: MessageBus, blobs: BlobStore, planning: PlanningAgent,
                 governance: GovernanceService, audit: AuditService, notifier: Notifier, directory: Directory,
                 clock: Clock, config: PilotConfig, llm: LLM | None = None):
        self.store, self.bus, self.blobs, self.planning = store, bus, blobs, planning
        self.governance, self.audit, self.notifier, self.directory = governance, audit, notifier, directory
        self.clock, self.config, self.llm = clock, config, llm
        bus.subscribe(topics.SHIPMENT_FACTS, self.on_shipment_fact, subscription="reporting-agent")

    def on_shipment_fact(self, event: Event) -> None:
        self.store.put(FACTS, event.payload["package_id"], event.payload)

    # ---------------------------------------------------------------- daily metrics (R6.1, R6.2, R6.6, R6.7)

    def clients(self) -> list[str]:
        """Clients to report on: tenant one always, others once they have a catalog."""
        return [t for t in sorted(self.config.tenants) if t == TENANT_ONE or self.config.catalog(t)]

    def run_daily(self, day: date | None = None) -> dict[str, dict[str, Any]]:
        return {tid: self.compute_daily(day, tid) for tid in self.clients()}

    def compute_daily(self, day: date | None = None, tenant_id: str = TENANT_ONE) -> dict[str, Any]:
        day = day or (self.clock.now().date() - timedelta(days=1))
        self.governance.check_insight_access(AGENT, f"metrics:{tenant_id}:{day}", tenant_id=tenant_id)
        items = self.config.catalog(tenant_id)
        orders = [o for o in self.store.query("orders") if _of(o, tenant_id)]
        mine = {o["order_id"] for o in orders}
        delivered = [o for o in orders if o.get("delivered_on") == day.isoformat()]
        shipped_ids = {o["order_id"] for o in orders
                       if any(t["state"] == "shipped" and t["at"][:10] == day.isoformat() for t in o["transitions"])}
        facts = [f for f in self.store.query(FACTS) if f["shipped_on"] == day.isoformat() and f.get("order_id") in mine]
        on_hand = sum(s["on_hand"] for s in self.store.query(INVENTORY_STATE) if s["sku"] in items)

        metrics: dict[str, dict[str, Any]] = {}
        metrics.update(self._delivery_metrics(delivered))
        metrics.update(self._shipment_metrics(facts, shipped_ids))
        metrics["inventory_turns"] = self._turns(day, on_hand, items)
        metrics["days_of_cover"] = self._days_of_cover(on_hand, items)
        metrics["forecast_accuracy"] = self._forecast_accuracy(day, tenant_id)
        metrics["replenishment_accuracy"] = self._replenishment_accuracy(day, tenant_id)

        anomalies, no_baseline = self._anomalies(day, metrics, tenant_id)
        doc = {"date": day.isoformat(), "tenant_id": tenant_id, "computed_at": self.clock.now().isoformat(),
               "metrics": metrics, "anomalies": anomalies, "baseline_not_established": no_baseline}
        self.store.put(METRICS, metrics_key(tenant_id, day.isoformat()), doc)

        incomplete = {k: v["missing_source"] for k, v in metrics.items() if v["status"] == "incomplete"}
        if incomplete:
            self.notifier.send(self._contact(OPS_TEAM), f"Incomplete metrics for {self._name(tenant_id)}, {day}",
                               json.dumps(incomplete, indent=2), severity="high")
        self.audit.decision(AGENT, "daily_metrics_computed", input_refs=[f"metrics:{metrics_key(tenant_id, day.isoformat())}"],
                            result={"incomplete": sorted(incomplete), "anomalies": [a["metric"] for a in anomalies]},
                            owning_party=OwningParty.CORP_B, tenant_id=tenant_id)
        self.export_feed(doc)
        return doc

    def _name(self, tenant_id: str) -> str:
        t = self.config.tenant(tenant_id)
        return t.name if t else tenant_id

    def _delivery_metrics(self, delivered: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not delivered:
            return {"on_time_delivery_rate": _m(None, status="no_activity"),
                    "delivery_speed_days": _m(None, status="no_activity", distribution={})}
        late = [o["order_id"] for o in delivered if not o["on_time"]]
        speeds = [(date.fromisoformat(o["delivered_on"]) - datetime.fromisoformat(o["placed_at"]).date()).days
                  for o in delivered]
        dist: dict[str, float] = {}
        for s in speeds:
            k = str(s) if s <= 3 else "4+"
            dist[k] = dist.get(k, 0) + 1 / len(speeds)
        return {"on_time_delivery_rate": _m(1 - len(late) / len(delivered), refs=late),
                "delivery_speed_days": _m(mean(speeds), distribution={k: round(v, 4) for k, v in sorted(dist.items())},
                                          refs=[o["order_id"] for o in delivered])}

    def _shipment_metrics(self, facts: list[dict[str, Any]], shipped_ids: set[str]) -> dict[str, dict[str, Any]]:
        names = ("per_unit_fulfillment_cost", "pct_ground_transport", "avg_final_mile_miles",
                 "line_split_fulfillment_rate")
        missing = shipped_ids - {f["order_id"] for f in facts}
        if missing:
            return {n: _m(None, status="incomplete", missing_source="MCF shipment facts (GetPackageTrackingDetail)",
                          refs=sorted(missing)) for n in names}
        if not facts:
            return {n: _m(None, status="no_activity") for n in names}
        refs = sorted({f["order_id"] for f in facts})
        packages: dict[str, int] = {}
        for f in facts:
            packages[f["order_id"]] = packages.get(f["order_id"], 0) + 1
        units = sum(f["units"] for f in facts)
        return {
            "per_unit_fulfillment_cost": _m(sum(f["fulfillment_cost"] for f in facts) / units if units else None, refs=refs),
            "pct_ground_transport": _m(sum(f["mode"] == "ground" for f in facts) / len(facts), refs=refs),
            "avg_final_mile_miles": _m(mean(f["final_mile_miles"] for f in facts), refs=refs),
            "line_split_fulfillment_rate": _m(sum(n > 1 for n in packages.values()) / len(packages),
                                              refs=[o for o, n in packages.items() if n > 1]),
        }

    def _turns(self, day: date, on_hand: int, items: set[str]) -> dict[str, Any]:
        picks = [e for e in self.store.query(INVENTORY_EVENTS, type=InventoryEventType.PICK.value)
                 if e["occurred_at"][:10] == day.isoformat() and e["sku"] in items]
        if on_hand <= 0:
            return _m(None, status="incomplete", missing_source="inventory state (Reconciliation_Agent)")
        return _m(sum(e["quantity"] for e in picks) * 365 / on_hand)

    def _days_of_cover(self, on_hand: int, items: set[str]) -> dict[str, Any]:
        rates = [self.planning.daily_demand(u, r) for u in items for r in self.planning.regions]
        known = [x for x in rates if x is not None]
        if not known or sum(known) == 0:
            return _m(None, status="incomplete", missing_source="demand plan (forecast feed or cold-start data)")
        return _m(on_hand / sum(known))

    def _forecast_accuracy(self, day: date, tenant_id: str) -> dict[str, Any]:
        week = week_of(day) - timedelta(days=7)  # last complete week
        items = self.config.catalog(tenant_id)
        sales = [s for s in self.store.query(SALES, week_start=week.isoformat()) if s["upc"] in items]
        if not sales:
            return _m(None, status="incomplete", missing_source=f"{self._name(tenant_id)} sales feed for week {week}")
        err = actual = 0.0
        refs = []
        for s in sales:
            fc = self.store.get(DEMAND_PLAN, f"{s['upc']}|{s['region']}|{week}")
            if fc is None:
                continue
            err += abs(fc["units"] - s["units"])
            actual += s["units"]
            refs.append(s["upc"])
        if actual == 0:
            return _m(None, status="incomplete", missing_source=f"forecast feed for week {week}")
        return _m(max(0.0, 1 - err / actual), refs=sorted(set(refs)))  # 1 - WAPE

    def _replenishment_accuracy(self, day: date, tenant_id: str) -> dict[str, Any]:
        week = week_of(day) - timedelta(days=7)
        scored, refs = [], []
        for rec in self.store.query(REPLENISHMENT_RECS, status="released"):
            if not _of(rec, tenant_id):
                continue
            created = datetime.fromisoformat(rec["created_at"]).date()
            if week_of(created) != week - timedelta(days=7):
                continue  # score recommendations once the following week's sales exist
            region = self.config.nodes[rec["target"]].region
            sale = self.store.get(SALES, f"{rec['upc']}|{region}|{week}") if region else None
            if sale is None:
                continue
            realized = sale["units"] / 7 * self.config.target_cover_days
            scored.append(abs(rec["qty"] - realized) / max(rec["qty"], 1))
            refs.append(rec["rec_id"])
        if not scored:
            return _m(None, status="no_activity")
        return _m(max(0.0, 1 - mean(scored)), refs=refs)

    def _anomalies(self, day: date, metrics: dict[str, dict[str, Any]],
                   tenant_id: str = TENANT_ONE) -> tuple[list[dict[str, Any]], list[str]]:
        history = sorted((d for d in self.store.query(METRICS) if d["date"] < day.isoformat() and _of(d, tenant_id)),
                         key=lambda d: d["date"])
        anomalies, no_baseline = [], []
        for name, m in metrics.items():
            if m["status"] != "ok" or m["value"] is None:
                continue
            past = [d["metrics"][name]["value"] for d in history
                    if d["metrics"].get(name, {}).get("status") == "ok" and d["metrics"][name]["value"] is not None]
            if len(past) < self.config.baseline_days:
                no_baseline.append(name)
                continue
            baseline = mean(past[-self.config.baseline_days:])
            if baseline == 0:
                continue
            deviation = (m["value"] - baseline) / baseline
            if abs(deviation) > self.config.anomaly_threshold:
                anomalies.append({"metric": name, "date": day.isoformat(), "observed": m["value"],
                                  "baseline": round(baseline, 4), "deviation": round(deviation, 4), "refs": m["refs"]})
        return anomalies, no_baseline

    # ---------------------------------------------------------------- export feed (R6.4)

    def export_feed(self, doc: dict[str, Any]) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["metric_date", "metric", "value", "status", "missing_source", "anomaly", "baseline"])
        flagged = {a["metric"]: a for a in doc["anomalies"]}
        for name in METRIC_NAMES:
            m = doc["metrics"][name]
            w.writerow([doc["date"], name, "" if m["value"] is None else m["value"], m["status"],
                        m["missing_source"] or "", name in flagged, flagged.get(name, {}).get("baseline", "")])
        tenant_id = doc.get("tenant_id") or TENANT_ONE
        url = self.blobs.write("exports", _path(tenant_id, f"metrics/{doc['date']}.csv"), buf.getvalue().encode(),
                               content_type="text/csv")
        if tenant_id == TENANT_ONE:
            self.bus.publish(Event(topics.CORPB_OUTBOUND, "metrics_feed", {"date": doc["date"], "url": url},
                                   self.clock.now()))
        else:   # other clients get it with their order updates
            self.bus.publish(Event(topics.OMS_OUTBOUND, "metrics_feed",
                                   {"tenant_id": tenant_id, "date": doc["date"], "url": url}, self.clock.now()))
        return url

    # ---------------------------------------------------------------- summaries (R6.3, R6.5)

    def run_weekly_all(self, period_end: date) -> dict[str, dict[str, Any]]:
        return {tid: self.run_weekly(period_end, tid) for tid in self.clients()}

    def run_quarterly_all(self, quarter_end: date) -> dict[str, dict[str, Any]]:
        return {tid: self.run_quarterly(quarter_end, tid) for tid in self.clients()}

    def run_weekly(self, period_end: date, tenant_id: str = TENANT_ONE) -> dict[str, Any]:
        days = [(period_end - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        docs = [d for d in (self.store.get(METRICS, metrics_key(tenant_id, x)) for x in days) if d]
        metrics = {}
        for name in METRIC_NAMES:
            vals = [d["metrics"][name]["value"] for d in docs
                    if d["metrics"][name]["status"] == "ok" and d["metrics"][name]["value"] is not None]
            metrics[name] = round(mean(vals), 4) if vals else None
        summary = {
            "kind": "weekly", "tenant_id": tenant_id, "client": self._name(tenant_id),
            "period_end": period_end.isoformat(), "metrics": metrics,
            "anomalies": [a for d in docs for a in d["anomalies"]],
            "baseline_not_established": sorted({n for d in docs for n in d["baseline_not_established"]}),
            "open_exceptions": [{"case_id": c["case_id"], "status": c["status"],
                                 "category": (c.get("classification") or {}).get("category")}
                                for c in self.store.query("exception_cases")
                                if _of(c, tenant_id) and ExceptionStatus(c["status"]) not in TERMINAL_EXCEPTION_STATES],
            "pending_approvals": [{"approval_id": a["approval_id"], "category": a["category"],
                                   "created_at": a["created_at"]} for a in self.store.query("approvals", status="pending")
                                  if _of(a, tenant_id)],
            "coordination": coordination(self.store, tenant_id, period_end - timedelta(days=6), period_end,
                                         self.clock.now()),
        }
        summary["narrative"] = self._narrative(summary)
        self.store.put(SUMMARIES, summary_key("weekly", tenant_id, period_end.isoformat()), summary)
        self._deliver(summary, f"{self._name(tenant_id)} weekly review, week ending {period_end}")
        return summary

    def run_quarterly(self, quarter_end: date, tenant_id: str = TENANT_ONE) -> dict[str, Any]:
        q_start = date(quarter_end.year, 3 * ((quarter_end.month - 1) // 3) + 1, 1)
        prev_end = q_start - timedelta(days=1)
        prev_start = date(prev_end.year, 3 * ((prev_end.month - 1) // 3) + 1, 1)
        weekly = [s for s in self.store.query(SUMMARIES, kind="weekly") if _of(s, tenant_id)]
        cur = [s for s in weekly if q_start.isoformat() <= s["period_end"] <= quarter_end.isoformat()]
        prev = [s for s in weekly if prev_start.isoformat() <= s["period_end"] <= prev_end.isoformat()]

        def avg(ss: list[dict[str, Any]], name: str) -> float | None:
            vals = [s["metrics"][name] for s in ss if s["metrics"].get(name) is not None]
            return round(mean(vals), 4) if vals else None

        trends = {}
        for name in METRIC_NAMES:
            c, p = avg(cur, name), avg(prev, name)
            trends[name] = {"quarter": c, "previous_quarter": p,
                            "change": None if c is None or p in (None, 0) else round((c - p) / p, 4)}
        summary = {"kind": "qbr", "tenant_id": tenant_id, "client": self._name(tenant_id),
                   "period_end": quarter_end.isoformat(), "weeks": len(cur), "metrics":
                   {k: v["quarter"] for k, v in trends.items()}, "trends": trends,
                   "anomalies": [a for s in cur for a in s["anomalies"]],
                   "baseline_not_established": [], "open_exceptions": [], "pending_approvals": []}
        summary["narrative"] = self._narrative(summary)
        self.store.put(SUMMARIES, summary_key("qbr", tenant_id, quarter_end.isoformat()), summary)
        self._deliver(summary, f"{self._name(tenant_id)} QBR summary, quarter ending {quarter_end}")
        return summary

    def _narrative(self, summary: dict[str, Any]) -> dict[str, Any]:
        if self.llm is not None:
            out = self.llm.structured(
                system="You write concise, factual executive summaries of fulfillment pilot metrics for a business "
                       "review. Use only the numbers provided. Name anomalies and open items plainly.",
                prompt=json.dumps(summary, default=str), schema=Narrative)
            if out is not None:
                return out.model_dump()
        n_anom, n_exc, n_appr = len(summary["anomalies"]), len(summary["open_exceptions"]), len(summary["pending_approvals"])
        otd = summary["metrics"].get("on_time_delivery_rate")
        return {"headline": f"On-time delivery {'n/a' if otd is None else f'{otd:.1%}'}; {n_anom} anomalies, "
                            f"{n_exc} open exceptions, {n_appr} pending approvals",
                "highlights": [f"{a['metric']} {a['observed']} vs baseline {a['baseline']} ({a['deviation']:+.0%})"
                               for a in summary["anomalies"]]}

    def _deliver(self, summary: dict[str, Any], subject: str) -> None:
        tenant_id = summary.get("tenant_id") or TENANT_ONE
        recipients = review_recipients(tenant_id)
        for name in recipients:
            recipient = self._contact(name)
            view = summary
            try:
                self.governance.check_insight_access(recipient, f"summary:{summary['kind']}:{summary['period_end']}",
                                                     tenant_id=tenant_id)
            except AccessDenied:
                view = _strip_insight(summary)
            body = render_markdown(view)
            self.blobs.write("reports", _path(tenant_id, f"{summary['kind']}/{summary['period_end']}/{name}.md"),
                             body.encode(), content_type="text/markdown")
            self.notifier.send(recipient, subject, body)
        self.audit.decision(AGENT, f"{summary['kind']}_summary_delivered",
                            input_refs=[f"summary:{summary_key(summary['kind'], tenant_id, summary['period_end'])}"],
                            result={"recipients": list(recipients)}, owning_party=OwningParty.CORP_B, tenant_id=tenant_id)

    def _contact(self, name: str) -> str:
        return self.directory.contact(name) or name


def _of(doc: dict[str, Any], tenant_id: str) -> bool:
    """Whether a record is this client's; records from before tenants existed are tenant one's."""
    return (doc.get("tenant_id") or TENANT_ONE) == tenant_id


def _strip_insight(summary: dict[str, Any]) -> dict[str, Any]:
    s = json.loads(json.dumps(summary, default=str))
    s["metrics"] = {k: ("withheld" if k in INSIGHT_METRICS else v) for k, v in s["metrics"].items()}
    s["anomalies"] = [a for a in s["anomalies"] if a["metric"] not in INSIGHT_METRICS]
    if "trends" in s:
        s["trends"] = {k: v for k, v in s["trends"].items() if k not in INSIGHT_METRICS}
    return s


def render_markdown(s: dict[str, Any]) -> str:
    lines = [f"# {s['narrative']['headline']}", "", f"{s.get('client') or 'Corp B'}, period ending {s['period_end']}", "",
             "## Metrics", "",
             "| Metric | Value |", "|---|---|"]
    for k, v in s["metrics"].items():
        flag = " (baseline not established)" if k in s.get("baseline_not_established", []) else ""
        lines.append(f"| {k} | {'n/a' if v is None else v}{flag} |")
    for title, key in (("Anomalies", "anomalies"), ("Open exceptions", "open_exceptions"),
                       ("Pending approvals", "pending_approvals")):
        lines += ["", f"## {title}", ""]
        lines += [f"- {json.dumps(x, default=str)}" for x in s[key]] or ["- none"]
    if s.get("coordination"):
        lines += ["", "## Coordination tax", ""] + render_coordination(s["coordination"])
    if s["narrative"]["highlights"]:
        lines += ["", "## Highlights", ""] + [f"- {h}" for h in s["narrative"]["highlights"]]
    return "\n".join(lines) + "\n"
