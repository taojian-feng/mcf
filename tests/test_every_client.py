"""Planning, aged stock and reports run for every client, each inside its own fence."""
import json
from datetime import timedelta

import pytest

from conftest import PILOT_UPCS, advance, inv
from tower.adapters.memory import StaticDirectory
from tower.agents.planning import week_of
from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.ports import Event
from tower.core.tenants import Tenant, default_tenants
from tower.edge.connectors import PartnerConnectors
from tower.runtime import build, memory_ports
from tower.services.governance import AccessDenied

B_UPC, C_UPC = "190000000001", "290000000001"


@pytest.fixture
def world():
    directory = StaticDirectory(
        roles={"alice@corpb": {f"approver:{c}:corp_b" for c in ("replenishment", "disposition", "distribution_plan")},
               "cora@corpc": {f"approver:{c}:corp_c" for c in ("replenishment", "disposition", "distribution_plan")}},
        pilot_list={"planning_agent", "reporting_agent", "alice@corpb", "review@corpb"},
        insight={"corp_c": {"planning_agent", "reporting_agent", "cora@corpc", "review@corpc"}},
        contacts={f"corp_b_{r}": f"{r}@corpb" for r in ("data_contact", "planner", "inventory", "review")}
        | {f"corp_c_{r}": f"{r}@corpc" for r in ("data_contact", "planner", "inventory", "review")}
        | {"corp_a_review": "review@corpa", "ops_team": "ops-team"})
    tenants = default_tenants()
    tenants["corp_c"] = Tenant(tenant_id="corp_c", name="Corp C", channels=["online", "store"], promise_days=[3])
    config = PilotConfig(pilot_upcs=set(PILOT_UPCS), tenants=tenants, tenant_upcs={"corp_c": {C_UPC}})
    return build(memory_ports(directory=directory), config, sleep=lambda s: None)


def week(system):
    return week_of(system.ports.clock.now().date()).isoformat()


def forecast(system, upc, tenant_id, west=70, feed_id="fc-1"):
    return system.planning.ingest_forecast(feed_id, [{"upc": upc, "region": "west", "week_start": week(system),
                                                      "units": west}], tenant_id)


# ---------------------------------------------------------------- planning

def test_a_second_clients_low_cover_is_restocked_for_that_client(world):
    world.config.unit_cost[C_UPC] = 200.0                       # big enough to need approval
    inv(world, "receipt", sku=C_UPC, node="IXD-1", qty=500)
    inv(world, "receipt", sku=C_UPC, node="FC-WEST", qty=50)
    forecast(world, C_UPC, "corp_c")
    [rec] = world.ports.store.query("replenishment_recs")
    assert rec["tenant_id"] == "corp_c" and rec["status"] == "pending_approval"
    item = world.approvals.get(rec["approval_id"])
    assert item.tenant_id == "corp_c"
    assert not world.approvals.can_decide("alice@corpb", item)  # Corp B can't move Corp C's stock
    world.approvals.decide("cora@corpc", rec["approval_id"], approve=True)
    rec = world.ports.store.get("replenishment_recs", rec["rec_id"])
    assert rec["status"] == "released"
    [h] = world.handoffs.open_view()
    assert h["tenant_id"] == "corp_c"


def test_a_client_cannot_forecast_another_clients_items(world):
    world.planning.ingest_forecast("fc-x", [{"upc": B_UPC, "region": "west", "week_start": week(world), "units": 999},
                                            {"upc": C_UPC, "region": "west", "week_start": week(world), "units": 7}],
                                   "corp_c")
    assert world.planning.daily_demand(B_UPC, "west") is None
    assert world.planning.daily_demand(C_UPC, "west") == 1.0
    [audit] = [r for r in world.audit.query(actor="planning_agent") if r.action == "demand_plan_updated"]
    assert audit.result["other_clients_items"] == 1 and audit.tenant_id == "corp_c"


def test_insight_fence_applies_to_each_clients_feeds(world):
    world.ports.directory.insight["corp_c"] = {"reporting_agent"}   # planning not on Corp C's list
    world.governance._auth_cache.clear()
    with pytest.raises(AccessDenied):
        forecast(world, C_UPC, "corp_c")


def test_missing_baselines_and_forecasts_go_to_each_clients_data_contact(world):
    world.planning.run_daily()
    assert any(C_UPC in m["body"] for m in world.ports.notifier.to("data_contact@corpc"))
    assert not any(C_UPC in m["body"] for m in world.ports.notifier.to("data_contact@corpb"))
    forecast(world, C_UPC, "corp_c")
    advance(world, days=7, hours=12)
    assert [m for m in world.ports.notifier.to("data_contact@corpc") if "missing" in m["subject"]]
    assert not [m for m in world.ports.notifier.to("data_contact@corpb") if "missing" in m["subject"]]


def test_one_distribution_plan_per_client(world):
    world.config.min_cover_days = 0
    for upc in (B_UPC, C_UPC):
        inv(world, "receipt", sku=upc, node="FC-EAST", qty=600)
        inv(world, "receipt", sku=upc, node="FC-WEST", qty=100)
    for upc, tid in ((B_UPC, "corp_b"), (C_UPC, "corp_c")):
        world.planning.ingest_forecast(f"fc-{tid}", [{"upc": upc, "region": r, "week_start": week(world), "units": 70}
                                                     for r in ("west", "east")], tid)
    out = world.planning.run_daily()
    plans = {tid: world.ports.store.get("distribution_plans", c["distribution_plan"])
             for tid, c in out["clients"].items()}
    assert plans["corp_b"]["moves"][0]["upc"] == B_UPC and plans["corp_c"]["moves"][0]["upc"] == C_UPC
    assert out["distribution_plan"] == plans["corp_b"]["plan_id"]        # tenant one's, as before
    assert world.approvals.get(plans["corp_c"]["approval_id"]).tenant_id == "corp_c"
    assert world.planning.run_daily()["clients"]["corp_c"]["distribution_plan"] is None


def test_a_clients_planning_feeds_are_routed_to_it(world):
    conn = PartnerConnectors(world, corpb_url=None, secret=lambda name: "x")
    records = [{"upc": C_UPC, "region": "west", "week_start": week(world), "units": 14}]
    world.ports.blobs.write("landing", "corpc/forecast/f1.json", json.dumps({"records": records}).encode())
    conn.on_feed(Event(topics.FEEDS_ARRIVED, "Microsoft.Storage.BlobCreated",
                       {"subject": "/blobServices/default/containers/landing/blobs/corpc/forecast/f1.json"},
                       world.ports.clock.now()))
    assert world.planning.daily_demand(C_UPC, "west") == 2.0
    assert world.ports.store.get("planning_meta", "forecast|corp_c")["last_feed_id"] == "forecast:f1"


# ---------------------------------------------------------------- aged stock

def test_aged_stock_is_each_clients_own(world):
    for upc in (B_UPC, C_UPC):
        inv(world, "receipt", sku=upc, node="FC-WEST", qty=20, at=world.ports.clock.now() - timedelta(days=125))
    world.disposition.run_daily()
    assert all(B_UPC in m["body"] for m in world.ports.notifier.to("inventory@corpb"))
    assert world.ports.notifier.to("inventory@corpc") and all(C_UPC in m["body"]
                                                              for m in world.ports.notifier.to("inventory@corpc"))
    recs = {r["sku"]: r for r in world.ports.store.query("disposition_recs")}
    assert recs[C_UPC]["tenant_id"] == "corp_c" and recs[B_UPC]["tenant_id"] == "corp_b"
    world.approvals.decide("cora@corpc", recs[C_UPC]["approval_id"], approve=True)
    [h] = world.handoffs.open_view()
    assert h["tenant_id"] == "corp_c" and h["action"] == "execute_disposition"
    advance(world, days=14)
    [sig] = world.ports.bus.events(topics.EXCEPTION_SIGNALS, "disposition_overdue")
    assert sig.payload["tenant_id"] == "corp_c"


# ---------------------------------------------------------------- reports

def test_each_client_gets_its_own_metrics_and_summaries(world):
    day = world.ports.clock.now().date() - timedelta(days=1)
    inv(world, "receipt", sku=C_UPC, node="FC-WEST", qty=140)
    forecast(world, C_UPC, "corp_c")
    docs = world.reporting.run_daily(day)
    assert set(docs) == {"corp_b", "corp_c"}
    assert docs["corp_c"]["metrics"]["days_of_cover"]["value"] == 14.0
    assert world.ports.store.get("metrics_daily", f"corp_c|{day}")["tenant_id"] == "corp_c"
    assert world.ports.store.get("metrics_daily", day.isoformat())["tenant_id"] == "corp_b"
    assert world.ports.blobs.read("exports", f"corp_c/metrics/{day}.csv")
    [feed] = [e for e in world.ports.bus.events(topics.OMS_OUTBOUND) if e.type == "metrics_feed"]
    assert feed.payload["tenant_id"] == "corp_c"

    summaries = world.reporting.run_weekly_all(day)
    assert summaries["corp_c"]["client"] == "Corp C"
    [mail] = world.ports.notifier.to("review@corpc")
    assert mail["subject"].startswith("Corp C weekly review") and "days_of_cover" in mail["body"]
    assert "14.0" in mail["body"]                                    # Corp C's reviewer may see its insight data
    corp_a = [m for m in world.ports.notifier.to("review@corpa") if m["subject"].startswith("Corp C")]
    assert corp_a and "withheld" in corp_a[0]["body"]                # Corp A's reviewer is not on Corp C's list
    assert not any("Corp C" in m["subject"] for m in world.ports.notifier.to("review@corpb"))
    assert world.ports.blobs.read("reports", f"corp_c/weekly/{day}/corp_c_review.md")


def test_the_corp_c_sim_sends_forecasts_and_sales():
    from sim.common.store import MemoryStore
    from sim.corpc.engine import CorpC
    from tower.adapters.memory import VirtualClock
    clock, drops = VirtualClock(), {}
    sim = CorpC(MemoryStore(), clock, gateway_url="https://gw", api_key="k", catalog_size=5,
                upload=lambda path, body: drops.setdefault(path.split("/")[1], []).append(json.loads(body)),
                send=lambda *a, **k: (202, b"{}"))
    for _ in range(3):
        sim.tick()
        clock.set(clock.now() + timedelta(minutes=40))
    assert {"catalog", "cold_start", "forecast"} <= set(drops)
    monday = week_of(clock.now().date()) + timedelta(days=7)
    clock.set(clock.now().replace(year=monday.year, month=monday.month, day=monday.day, hour=3))
    sim.tick()
    assert len(drops["forecast"]) == 2 and "sales" in drops
    assert all(r["upc"] in sim.by_upc for r in drops["forecast"][0]["records"])


def test_a_set_aside_forecast_is_caught_up_once(world):
    conn = PartnerConnectors(world, corpb_url=None, secret=lambda name: "x")
    for name, units in (("f0", 700), ("f1", 14)):                  # f0 is older and must not win
        rows = [{"upc": C_UPC, "region": "west", "week_start": week(world), "units": units}]
        world.ports.blobs.write("landing", f"corpc/forecast/{name}.json", json.dumps({"records": rows}).encode())
    assert conn.catch_up_planning_feeds() == ["corpc/forecast/f1.json"]
    assert world.planning.daily_demand(C_UPC, "west") == 2.0
    assert conn.catch_up_planning_feeds() == []
