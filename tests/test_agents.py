"""Planning, Reconciliation, Disposition and Reporting agents, in memory with virtual time."""
from datetime import date, datetime, timedelta

import pytest

from conftest import advance, inv
from tower.agents.planning import week_of
from tower.core import topics
from tower.core.models import ApprovalCategory, AuditKind, OwningParty
from tower.core.ports import Event
from tower.services.governance import AccessDenied

UPC = "190000000001"
UPC2 = "190000000002"


def now(system):
    return system.ports.clock.now()


def this_week(system):
    return week_of(now(system).date()).isoformat()


def forecast(system, feed_id="fc-1", west=70, east=None, upc=UPC, week=None):
    week = week or this_week(system)
    recs = [{"upc": upc, "region": "west", "week_start": week, "units": west}]
    if east is not None:
        recs.append({"upc": upc, "region": "east", "week_start": week, "units": east})
    return system.planning.ingest_forecast(feed_id, recs)


# ================================================================ Reconciliation (R2)

def test_inventory_events_update_state_and_in_transit(system):
    inv(system, "receipt", node="IXD-1", qty=100)
    inv(system, "transfer", node="IXD-1", qty=40, to_node="FC-WEST", transfer_id="t1")
    q = system.reconciliation.query(sku=UPC)                                   # R2.2
    assert {p["node_id"]: p["on_hand"] for p in q["positions"]} == {"IXD-1": 60}
    assert q["in_transit"] == [{"transfer_id": "t1", "sku": UPC, "from_node": "IXD-1", "to_node": "FC-WEST", "qty": 40}]
    assert q["network_total"] == {UPC: 100}

    inv(system, "receipt", node="FC-WEST", qty=40, transfer_id="t1")
    inv(system, "pick", node="FC-WEST", qty=3)
    q = system.reconciliation.query(node_id="FC-WEST")
    assert q["positions"][0]["on_hand"] == 37 and q["in_transit"] == []
    assert system.reconciliation.query()["network_total"] == {UPC: 97}


def test_invalid_and_out_of_scope_events_rejected(system):
    inv(system, "receipt", sku="999999999999", qty=5)
    inv(system, "receipt", node="FC-MARS", qty=5)
    system.ports.bus.publish(Event(topics.INVENTORY_EVENTS, "receipt", {"event_id": "bad", "type": "teleport"},
                                   now(system)))
    assert system.reconciliation.query()["positions"] == []                    # R2.7 excluded
    rejects = [r for r in system.audit.query(actor="reconciliation_agent") if r.kind == AuditKind.VALIDATION_REJECT]
    assert len(rejects) == 3
    assert len(system.ports.notifier.to("data@corpa")) == 3


def test_decant_transmits_counts_to_corp_b(system):
    inv(system, "receipt", node="IXD-1", qty=120)
    inv(system, "decant", node="IXD-1", qty=0)
    [msg] = system.ports.bus.events(topics.CORPB_OUTBOUND, "decant_counts")   # R2.5
    assert msg.payload["counts"] == [{"sku": UPC, "on_hand": 120}]


def test_point_in_time_history(system):
    t0 = now(system)
    inv(system, "receipt", qty=10, at=t0 - timedelta(days=2))
    inv(system, "pick", qty=3, at=t0 - timedelta(days=1))
    rec = system.reconciliation
    assert rec.position_at(UPC, "FC-WEST", t0 - timedelta(days=1, hours=12)) == 10   # R2.6
    assert rec.position_at(UPC, "FC-WEST", t0) == 7


def test_daily_reconciliation(system):
    inv(system, "receipt", node="FC-WEST", qty=50)
    inv(system, "physical_count", node="FC-WEST", qty=50)
    inv(system, "receipt", node="FC-EAST", qty=30)
    inv(system, "physical_count", node="FC-EAST", qty=24)
    inv(system, "receipt", node="IXD-1", qty=10)                              # no count
    report = system.reconciliation.run_daily()
    assert report["reconciled"] == 2
    assert report["discrepancies"] == [{"sku": UPC, "node_id": "FC-EAST", "expected": 30, "observed": 24}]  # R2.4
    assert report["unreconciled"] == [{"sku": UPC, "node_id": "IXD-1"}]       # R2.8
    subjects = [m["subject"] for m in system.ports.notifier.to("fops@corpa")]
    assert any("discrepancy" in s for s in subjects) and any("Daily reconciliation" in s for s in subjects)


# ================================================================ Planning (R1)

def test_forecast_feed_validation(system):
    res = system.planning.ingest_forecast("fc-1", [
        {"upc": UPC, "region": "west", "week_start": this_week(system), "units": 70},
        {"upc": "nope", "region": "west", "week_start": this_week(system), "units": -1},
    ])
    assert len(res.valid) == 1 and len(res.rejected) == 1                     # R1.1, R1.2
    assert system.planning.daily_demand(UPC, "west") == 10
    assert system.ports.notifier.to("data@corpb")


def test_forecast_requires_pilot_authorization(system, directory):
    directory.pilot_list.discard("planning_agent")
    with pytest.raises(AccessDenied):
        forecast(system)


def test_low_cover_auto_released_from_ixd(system):
    inv(system, "receipt", node="IXD-1", qty=500)
    inv(system, "receipt", node="FC-WEST", qty=50)
    forecast(system)                                                          # 10/day west -> cover 5d
    [rec] = system.ports.store.query("replenishment_recs")
    assert (rec["source"], rec["target"], rec["qty"], rec["status"]) == ("IXD-1", "FC-WEST", 230, "released")
    [h] = system.handoffs.open_view()
    assert h["action"] == "execute_transfer" and h["responsible_party"] == "corp_a"
    assert system.ports.store.get("ats_cover", f"{UPC}|FC-WEST")["cover_days"] == 5.0   # R1.3


def test_high_value_replenishment_needs_approval(system):
    system.config.unit_cost[UPC] = 200.0
    inv(system, "receipt", node="IXD-1", qty=500)
    inv(system, "receipt", node="FC-WEST", qty=50)
    forecast(system)
    [rec] = system.ports.store.query("replenishment_recs")
    assert rec["status"] == "pending_approval" and system.handoffs.open_view() == []   # R8.1, R8.2
    system.approvals.decide("alice@corpb", rec["approval_id"], approve=True)
    assert system.ports.store.get("replenishment_recs", rec["rec_id"])["status"] == "released"


def test_isc_source_creates_inbound_shipment_plan(system):
    inv(system, "receipt", node="ISC-X", qty=1000)
    forecast(system)
    plans = system.ports.mcf.ops("CreateInboundShipmentPlan")
    assert plans and {p["shipFrom"] for p in plans} == {"ISC-X"}


def test_cover_breach_from_inventory_event_is_immediate(system):
    inv(system, "receipt", node="IXD-1", qty=500)
    inv(system, "receipt", node="FC-WEST", qty=300)
    forecast(system)                                                          # cover 30d: no rec
    assert system.ports.store.query("replenishment_recs") == []
    inv(system, "pick", node="FC-WEST", qty=200)                              # cover 10d
    [rec] = system.ports.store.query("replenishment_recs")                    # R1.4 on the event, not next day
    assert rec["created_at"] == now(system).isoformat()


def test_cold_start_replaced_after_four_weeks(system):
    p = system.planning
    p.ingest_cold_start("cs-1", [{"upc": UPC, "region": "west", "weekly_units": 35}])
    assert p.baseline(UPC, "west") == (35, "cold_start")                     # R1.7
    weeks = [(week_of(now(system).date()) - timedelta(days=7 * i)).isoformat() for i in range(1, 5)]
    p.ingest_sales("s-1", [{"upc": UPC, "region": "west", "week_start": w, "units": 14} for w in weeks[:3]])
    assert p.baseline(UPC, "west")[1] == "cold_start"
    p.ingest_sales("s-2", [{"upc": UPC, "region": "west", "week_start": weeks[3], "units": 14}])
    assert p.baseline(UPC, "west") == (14, "actual_sales")


def test_sku_without_baseline_flagged_once(system):
    forecast(system, upc=UPC)
    system.planning.run_daily()
    system.planning.run_daily()
    msgs = [m for m in system.ports.notifier.to("data@corpb") if "demand baseline" in m["subject"]]
    assert len(msgs) == 1 and UPC2 in msgs[0]["body"] and UPC not in msgs[0]["body"]   # R1.9


def test_missing_forecast_feed_alert(system):
    forecast(system, feed_id="fc-1")
    advance(system, days=7, hours=12)
    assert [m for m in system.ports.notifier.to("data@corpb") if "missing" in m["subject"]]   # R1.8
    assert system.planning.daily_demand(UPC, "west") is not None              # keeps planning on last plan


def test_no_missing_feed_alert_when_next_feed_arrives(system):
    forecast(system, feed_id="fc-1")
    advance(system, days=7)
    forecast(system, feed_id="fc-2")
    advance(system, hours=13)
    assert not [m for m in system.ports.notifier.to("data@corpb") if "missing" in m["subject"]]


def test_imbalance_and_distribution_plan_gated_by_approval(system):
    system.config.min_cover_days = 0  # isolate the distribution plan from replenishment
    inv(system, "receipt", node="FC-EAST", qty=600)
    inv(system, "receipt", node="FC-WEST", qty=100)
    forecast(system, west=70, east=70)
    out = system.planning.run_daily()
    status = {i["region"]: i["status"] for i in out["imbalances"]}
    assert status == {"east": "oversupplied", "west": "undersupplied"}       # R1.5
    plan = system.ports.store.get("distribution_plans", out["distribution_plan"])
    assert plan["moves"] == [{"upc": UPC, "from_node": "FC-EAST", "to_node": "FC-WEST", "qty": 180}]
    assert system.ports.mcf.ops("ApproveDistributionPlan") == []              # R1.6 nothing moves before approval
    assert system.planning.run_daily()["distribution_plan"] is None           # one plan in review at a time
    system.approvals.decide("alice@corpb", plan["approval_id"], approve=True)
    assert system.ports.mcf.ops("ApproveDistributionPlan")[0]["planId"] == plan["plan_id"]


# ================================================================ Disposition (R4)

def aged_stock(system, days, qty=20, node="FC-WEST"):
    inv(system, "receipt", node=node, qty=qty, at=now(system) - timedelta(days=days))


def test_aging_alerts_once_per_threshold_crossing(system):
    aged_stock(system, 61)
    system.disposition.run_daily()
    system.disposition.run_daily()
    alerts = system.ports.notifier.to("inventory@corpb")
    assert len(alerts) == 1 and '"threshold_days": 60' in alerts[0]["body"]   # R4.2
    assert '"accumulated_storage_cost": 61.0' in alerts[0]["body"]
    advance(system, days=30)
    system.disposition.run_daily()
    assert len(system.ports.notifier.to("inventory@corpb")) == 2              # crossed 90


def test_disposition_recommendation_approval_and_completion(system):
    aged_stock(system, 125)
    system.disposition.run_daily()
    [rec] = system.ports.store.query("disposition_recs")
    assert rec["option"] == "removal" and rec["qty"] == 20                    # R4.3, R4.4
    assert rec["estimated_disposition_cost"] == 35.0 and rec["estimated_avoided_storage_cost"] == 90.0
    assert "demand_outlook" in rec and rec["status"] == "pending_approval"    # R4.5
    assert system.handoffs.open_view() == []

    system.approvals.decide("alice@corpb", rec["approval_id"], approve=True)
    rec = system.ports.store.get("disposition_recs", rec["rec_id"])
    assert rec["status"] == "initiated"                                       # R4.6
    assert system.handoffs.open_view()[0]["action"] == "execute_disposition"
    system.ports.bus.publish(Event(topics.DISPOSITION_EVENTS, "completed",
                                   {"rec_id": rec["rec_id"], "detail": "removal confirmed"}, now(system)))
    assert system.ports.store.get("disposition_recs", rec["rec_id"])["status"] == "completed"   # R4.7
    assert [r for r in system.audit.query(actor="disposition_agent") if r.action == "disposition_completed"]


def test_disposition_overdue_raises_exception(system):
    aged_stock(system, 125)
    system.disposition.run_daily()
    [rec] = system.ports.store.query("disposition_recs")
    system.approvals.decide("alice@corpb", rec["approval_id"], approve=True)
    advance(system, days=14)
    [sig] = system.ports.bus.events(topics.EXCEPTION_SIGNALS, "disposition_overdue")   # R4.8
    assert sig.payload["sku"] == UPC and sig.payload["node_id"] == "FC-WEST" and sig.payload["details"]["qty"] == 20
    assert any(c["signal"]["signal_type"] == "disposition_overdue" for c in system.ports.store.query("exception_cases"))


def test_rejected_disposition_is_not_initiated(system):
    aged_stock(system, 125)
    system.disposition.run_daily()
    [rec] = system.ports.store.query("disposition_recs")
    system.approvals.decide("alice@corpb", rec["approval_id"], approve=False, reason="sell through promo")
    assert system.ports.store.get("disposition_recs", rec["rec_id"])["status"] == "rejected"   # R4.9
    system.disposition.run_daily()
    assert len(system.ports.store.query("disposition_recs")) == 1 and system.handoffs.open_view() == []


# ================================================================ Reporting (R6)

def seed_day(system, day: date, *, late=False, facts=True):
    placed = datetime(day.year, day.month, day.day, tzinfo=now(system).tzinfo) - timedelta(days=2)
    oid = f"ord-{day}-{'late' if late else 'ok'}"
    system.ports.store.put("orders", oid, {
        "order_id": oid, "placed_at": placed.isoformat(), "delivered_on": day.isoformat(), "on_time": not late,
        "transitions": [{"state": "shipped", "at": f"{day}T08:00:00+00:00"}]})
    if facts:
        system.ports.store.put("shipment_facts", f"pkg-{oid}", {
            "package_id": f"pkg-{oid}", "order_id": oid, "shipped_on": day.isoformat(), "mode": "ground",
            "final_mile_miles": 12.0, "fulfillment_cost": 9.0, "units": 1})
    return oid


def test_daily_metrics_and_export(system):
    inv(system, "receipt", qty=100)
    forecast(system)
    day = now(system).date() - timedelta(days=1)
    seed_day(system, day)
    late = seed_day(system, day, late=True)
    doc = system.reporting.compute_daily(day)                                 # R6.1
    m = doc["metrics"]
    assert m["on_time_delivery_rate"]["value"] == 0.5 and m["on_time_delivery_rate"]["refs"] == [late]
    assert m["per_unit_fulfillment_cost"]["value"] == 9.0 and m["pct_ground_transport"]["value"] == 1.0
    assert m["days_of_cover"]["value"] == 10.0
    assert m["forecast_accuracy"]["status"] == "incomplete"                   # R6.6 no sales feed yet
    assert any("Incomplete metrics for Corp B" in x["subject"] for x in system.ports.notifier.to("ops-team"))
    assert doc["baseline_not_established"]                                    # R6.7
    csv_body = system.ports.blobs.read("exports", f"metrics/{day}.csv").decode()   # R6.4
    assert "on_time_delivery_rate,0.5,ok" in csv_body


def test_missing_shipment_facts_marks_metrics_incomplete(system):
    day = now(system).date() - timedelta(days=1)
    seed_day(system, day, facts=False)
    m = system.reporting.compute_daily(day)["metrics"]
    assert m["per_unit_fulfillment_cost"]["status"] == "incomplete"
    assert "shipment facts" in m["per_unit_fulfillment_cost"]["missing_source"]


def test_anomaly_flagged_against_four_week_baseline(system):
    day = now(system).date() - timedelta(days=1)
    for i in range(28, 0, -1):
        d = day - timedelta(days=i)
        system.ports.store.put("metrics_daily", d.isoformat(), {
            "date": d.isoformat(), "anomalies": [], "baseline_not_established": [],
            "metrics": {"on_time_delivery_rate": {"value": 0.95, "status": "ok", "refs": []}}})
    seed_day(system, day)
    late = seed_day(system, day, late=True)
    doc = system.reporting.compute_daily(day)
    [a] = [a for a in doc["anomalies"] if a["metric"] == "on_time_delivery_rate"]   # R6.2
    assert (a["observed"], a["baseline"]) == (0.5, 0.95) and late in a["refs"]
    assert "on_time_delivery_rate" not in doc["baseline_not_established"]


def test_weekly_summary_delivery_and_insight_isolation(system):
    inv(system, "receipt", qty=100)
    forecast(system)
    end = now(system).date() - timedelta(days=1)
    for i in range(7):
        seed_day(system, end - timedelta(days=i))
        system.reporting.compute_daily(end - timedelta(days=i))
    system.approvals.submit(category=ApprovalCategory.DISPOSITION, owning_party=OwningParty.CORP_B,
                            agent="disposition_agent", subject_id="dsp_x", content={}, rationale="r")
    s = system.reporting.run_weekly(end)                                      # R6.3
    assert s["metrics"]["on_time_delivery_rate"] == 1.0 and len(s["pending_approvals"]) == 1
    corp_b = system.ports.notifier.to("review@corpb")[-1]["body"]
    corp_a = system.ports.notifier.to("review@corpa")[-1]["body"]
    assert "| days_of_cover | 10.0" in corp_b
    assert "| days_of_cover | withheld" in corp_a                             # R9.1 insight data not shared
    assert [r for r in system.audit.query(actor="review@corpa") if r.kind == AuditKind.DENIED_ATTEMPT]


def test_qbr_trends(system):
    for end, otd in ((date(2026, 8, 30), 0.9), (date(2026, 9, 27), 1.0), (date(2026, 6, 28), 0.8)):
        system.ports.store.put("summaries", f"weekly|{end}", {
            "kind": "weekly", "period_end": end.isoformat(), "anomalies": [],
            "metrics": {"on_time_delivery_rate": otd}})
    q = system.reporting.run_quarterly(date(2026, 9, 30))                     # R6.5
    t = q["trends"]["on_time_delivery_rate"]
    assert (t["quarter"], t["previous_quarter"], t["change"]) == (0.95, 0.8, 0.1875)
    assert system.ports.notifier.to("review@corpb")
