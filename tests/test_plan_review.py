"""Planning judgment: demand drift, demand calls (sensing and Claude), and Claude's review of restocks, rebalancing
plans and aged-stock dispositions, each inside the bounds the code enforces."""
from datetime import timedelta

import pytest

from conftest import PILOT_UPCS, advance, inv, order_payload, send_order
from tower.adapters.memory import ScriptedLLM
from tower.agents.planning import week_of
from tower.core.config import PilotConfig
from tower.runtime import build, memory_ports

UPC = "190000000001"


def make(directory, mode="rules", llm=None, **cfg):
    ports = memory_ports(directory=directory, llm=llm)
    return build(ports, PilotConfig(pilot_upcs=set(PILOT_UPCS), plan_review=mode, **cfg), sleep=lambda s: None)


def forecast(system, west=7.0, east=7.0):
    week = week_of(system.ports.clock.now().date())
    system.planning.ingest_forecast("fc-1", [
        {"upc": UPC, "region": r, "week_start": (week + timedelta(weeks=w)).isoformat(), "units": u}
        for r, u in (("west", west), ("east", east)) for w in range(-3, 4)])


def orders(system, per_day_west=0, per_day_east=0, days=15, qty=1, upto=1):
    now, n = system.ports.clock.now(), 0
    for d in range(days, upto - 1, -1):
        for state, count in (("WA", per_day_west), ("NY", per_day_east)):
            for _ in range(count):
                n += 1
                p = order_payload(f"o-{d}-{state}-{n}", upc=UPC, state=state, placed_at=now - timedelta(days=d))
                p["lines"][0]["quantity"] = qty
                send_order(system, p)


def test_orders_are_counted_by_item_day_and_region_once(directory):
    s = make(directory)
    p = order_payload("o-1", upc=UPC, state="CA", placed_at=s.ports.clock.now() - timedelta(days=1))
    send_order(s, p)
    send_order(s, p)                                            # delivered twice
    send_order(s, order_payload("o-2", upc=UPC, state="NY", placed_at=s.ports.clock.now() - timedelta(days=1)))
    doc = s.ports.store.get("demand_actuals", UPC)
    [day] = doc["days"].values()
    assert (day["west"], day["east"], day["max_line"]) == (1, 1, 1)


def test_drift_needs_enough_history_and_a_real_gap(directory):
    s = make(directory)
    forecast(s, west=7, east=7)                                 # 1 a day each
    orders(s, per_day_west=3, per_day_east=1, days=10)
    assert s.planning.drift(UPC) is None                        # 10 days seen, the window is 14
    orders(s, per_day_west=3, per_day_east=1, days=15, upto=11)
    drift = s.planning.drift(UPC)
    assert set(drift) == {"west"} and drift["west"]["ratio"] >= 1.5


def test_rules_mode_keeps_the_forecast(directory):
    s = make(directory)
    forecast(s)
    orders(s, per_day_west=4, days=15)
    assert s.planning.run_daily()["demand_calls"] == []
    assert s.planning.daily_demand(UPC, "west") == 1.0


def test_sensing_blends_recent_orders_into_the_plan_and_restocks(directory):
    s = make(directory, "sensing")
    forecast(s)
    inv(s, "receipt", sku=UPC, node="FC-WEST", qty=20)          # 20 days of cover on the forecast
    inv(s, "receipt", sku=UPC, node="ISC-X", qty=500)
    assert s.ports.store.query("replenishment_recs") == []
    orders(s, per_day_west=4, days=15)
    out = s.planning.run_daily()
    [call] = out["demand_calls"]
    assert call["by"] == "sensing" and call["rates"]["west"] == pytest.approx(0.7 * 4 + 0.3 * 1)
    assert s.planning.daily_demand(UPC, "west") == pytest.approx(3.1)
    assert s.ports.store.query("replenishment_recs", target="FC-WEST")   # 20 units is now ~6 days of cover


def test_claude_decides_the_demand_within_bounds_and_tells_the_client(directory):
    llm = ScriptedLLM(structured_outputs={"DemandCall": {
        "rates": [{"region": "west", "daily_units": 500}, {"region": "east", "daily_units": 1}],
        "basis": "orders", "confidence": 0.8, "tell_client": True, "rationale": "sustained over two weeks"}})
    s = make(directory, "claude", llm)
    forecast(s)
    orders(s, per_day_west=4, days=15)
    [call] = s.planning.run_daily()["demand_calls"]
    assert call["by"] == "claude" and call["rates"]["west"] == pytest.approx(3 * 4)   # capped at 3x what was seen
    assert "our_orders_daily" in llm.seen[0]["prompt"] and "drift" in llm.seen[0]["prompt"]
    assert [m for m in s.ports.notifier.to("planner@corpb") if m["subject"] == f"Forecast for {UPC} looks off"]
    assert s.planning.run_daily()["demand_calls"] == []        # the call holds for a week
    advance(s, days=8)
    assert s.planning.daily_demand(UPC, "west") == 1.0          # then the forecast again until looked at


def test_without_an_answer_from_claude_the_forecast_stands(directory):
    s = make(directory, "claude", ScriptedLLM())
    forecast(s)
    orders(s, per_day_west=4, days=15)
    assert s.planning.run_daily()["demand_calls"] == []
    assert s.planning.daily_demand(UPC, "west") == 1.0


def _restock(directory, review):
    s = make(directory, "claude", ScriptedLLM(structured_outputs={"RestockReview": review}))
    forecast(s)
    inv(s, "receipt", sku=UPC, node="ISC-X", qty=40)
    inv(s, "receipt", sku=UPC, node="FC-WEST", qty=5)           # 5 days of cover: rules want 23 more
    [rec] = s.ports.store.query("replenishment_recs")
    return s, rec


def test_claude_can_hold_a_restock(directory):
    s, rec = _restock(directory, {"decision": "hold", "confidence": 0.9, "rationale": "orders stopped"})
    assert rec["status"] == "held" and not s.handoffs.open_view()
    inv(s, "receipt", sku=UPC, node="FC-WEST", qty=0)
    assert len(s.ports.store.query("replenishment_recs")) == 1  # not asked again inside the cool-down


def test_claude_adjusts_a_restock_no_further_than_twice_and_what_the_source_holds(directory):
    s, rec = _restock(directory, {"decision": "adjust", "qty": 500, "confidence": 0.7, "rationale": "surge"})
    assert rec["qty"] == 40 and rec["status"] == "released"


def test_claude_escalates_a_small_restock_to_a_person(directory):
    s, rec = _restock(directory, {"decision": "escalate", "confidence": 0.4, "rationale": "evidence conflicts"})
    assert rec["status"] == "pending_approval" and "reviewed: evidence conflicts" in rec["rationale"]


def test_claude_trims_a_rebalancing_plan(directory):
    llm = ScriptedLLM(structured_outputs={"PlanReview": {"moves": [{"index": 0, "qty": 999}],
                                                         "rationale": "keep, cannot grow"}})
    s = make(directory, "claude", llm, min_cover_days=0)
    forecast(s, west=70, east=70)
    inv(s, "receipt", sku=UPC, node="FC-EAST", qty=600)
    inv(s, "receipt", sku=UPC, node="FC-WEST", qty=100)
    plan = s.ports.store.get("distribution_plans", s.planning.run_daily()["distribution_plan"])
    assert plan["moves"][0]["qty"] == 180                       # never more than the rules proposed

    llm.structured_outputs["PlanReview"] = {"moves": [{"index": 0, "qty": 0}], "rationale": "a blip"}
    s2 = make(directory, "claude", llm, min_cover_days=0)
    forecast(s2, west=70, east=70)
    inv(s2, "receipt", sku=UPC, node="FC-EAST", qty=600)
    inv(s2, "receipt", sku=UPC, node="FC-WEST", qty=100)
    assert s2.planning.run_daily()["distribution_plan"] is None


def test_claude_can_keep_aged_stock_that_still_sells(directory):
    s = make(directory, "claude", ScriptedLLM(structured_outputs={"DispositionReview": {
        "decision": "keep", "rationale": "selling 3 a day again"}}))
    inv(s, "receipt", sku=UPC, node="FC-WEST", qty=20, at=s.ports.clock.now() - timedelta(days=125))
    s.disposition.run_daily()
    [rec] = s.ports.store.query("disposition_recs")
    assert rec["status"] == "kept" and not s.ports.store.query("approvals")
    s.disposition.run_daily()
    assert len(s.ports.store.query("disposition_recs")) == 1    # not asked again for a while
