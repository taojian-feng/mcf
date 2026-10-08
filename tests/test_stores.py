"""A retailer with stores: store orders share the client's stock with its web orders."""
import collections
from datetime import timedelta

import pytest

from conftest import PILOT_UPCS, inv, order_payload
from evals.world import build_world
from tower.adapters.memory import StaticDirectory
from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.ports import Event
from tower.core.tenants import Tenant, default_tenants
from tower.edge.gateway import Gateway, GatewayError
from tower.runtime import build, memory_ports

C_UPC = "390000000001"
KEYS = {"partner-corpb-api-key": "key-b", "partner-corpc-api-key": "key-c"}


@pytest.fixture
def chain():
    tenants = default_tenants()
    tenants["corp_c"] = Tenant(tenant_id="corp_c", name="Corp C", channels=["online", "store"], promise_days=[3],
                               online_protect_days=7, outbound_url="https://corpc")
    directory = StaticDirectory(roles={"cora@corpc": {"approver:exception_resolution:corp_c"}},
                                pilot_list={"planning_agent"}, contacts={"corp_a_ops": "ops@corpa"})
    config = PilotConfig(pilot_upcs=set(PILOT_UPCS), tenants=tenants, tenant_upcs={"corp_c": {C_UPC}})
    system = build(memory_ports(directory=directory), config, sleep=lambda s: None)
    system.gateway = Gateway(system.ports.bus, KEYS.__getitem__, system.ports.clock,
                             tenants=lambda: system.config.tenants)
    system.oms = []
    system.ports.bus.subscribe(topics.OMS_OUTBOUND, system.oms.append, subscription="test-oms")
    return system


def store_order(order_id="S-1", *, cases=4, pack=6, days=(3, 6), placed_at=None, **kw):
    from tower.adapters.memory import VirtualClock
    placed = placed_at or VirtualClock().now()
    return {"order_id": order_id, "placed_at": placed.isoformat(), "store_id": "ST-0101",
            "store_name": "Corp C Seattle", "ship_to": {"line1": "401 NE Northgate Way", "city": "Seattle",
                                                        "state": "WA", "postal_code": "98101"},
            "window_start": (placed.date() + timedelta(days=days[0])).isoformat(),
            "window_end": (placed.date() + timedelta(days=days[1])).isoformat(),
            "lines": [{"line_id": "1", "upc": C_UPC, "cases": cases, "case_pack": pack}], **kw}


def web_order(system, order_id, qty, *, days_ago=1):
    p = order_payload(order_id, upc=C_UPC, promise=3, placed_at=system.ports.clock.now() - timedelta(days=days_ago))
    p["lines"][0]["quantity"] = qty
    system.gateway.receive_order("key-c", p)


def sent(system, kind):
    return [e.payload for e in system.oms if e.type == kind]


# ---------------------------------------------------------------- gateway

def test_store_orders_need_a_store_contract(chain):
    with pytest.raises(GatewayError) as e:
        chain.gateway.receive_store_order("key-b", store_order())       # Corp B sells online only
    assert e.value.status == 403
    with pytest.raises(GatewayError) as e:
        chain.gateway.receive_store_order("key-c", store_order(days=(0, 1)))  # less than two days' notice
    assert e.value.status == 422 and "notice" in str(e.value.details)
    with pytest.raises(GatewayError) as e:
        chain.gateway.receive_store_order("key-c", store_order(days=(5, 3)))
    assert e.value.status == 422
    bad = store_order()
    bad["lines"][0]["cases"] = 0
    with pytest.raises(GatewayError) as e:
        chain.gateway.receive_store_order("key-c", bad)
    assert e.value.status == 422
    chain.config.tenants["corp_c"].channels = ["online"]
    with pytest.raises(GatewayError) as e:
        chain.gateway.receive_store_order("key-c", store_order())
    assert e.value.status == 403


# ---------------------------------------------------------------- allocation

def test_a_store_order_in_full_goes_to_corp_a_as_freight(chain):
    inv(chain, "receipt", C_UPC, "FC-WEST", 200)
    assert chain.gateway.receive_store_order("key-c", store_order())["order_id"] == "S-1"
    rec = chain.ports.store.get("orders", "CORPC-S-1")
    assert rec["channel"] == "store" and rec["lines"][0]["quantity"] == 24 and rec["tenant_id"] == "corp_c"
    (req,) = chain.ports.mcf.ops("CreateFulfillmentOrder")
    assert req["orderType"] == "B2B" and req["shippingSpeedCategory"] == "Freight"
    assert req["items"][0]["quantity"] == 24 and req["items"][0]["casePack"] == 6
    assert req["destinationAddress"]["storeId"] == "ST-0101"
    (alloc,) = sent(chain, "store_order_allocated")
    assert alloc["order_id"] == "S-1" and alloc["lines"][0]["short_cases"] == 0


def test_store_orders_are_cut_to_protect_web_stock(chain):
    inv(chain, "receipt", C_UPC, "FC-WEST", 100)
    web_order(chain, "W-1", 28, days_ago=3)          # 28 units in 14 days: 2 a day, 14 held back for the web
    chain.ports.store.put("orders", "CORPC-W-1", {**chain.ports.store.get("orders", "CORPC-W-1"), "state": "delivered"})
    chain.gateway.receive_store_order("key-c", store_order(cases=20, pack=6))   # asks for 120
    (alloc,) = sent(chain, "store_order_allocated")
    line = alloc["lines"][0]
    assert line["web_reserve_units"] == 14 and line["cases_allocated"] == 14     # (100 - 14) // 6
    assert line["short_cases"] == 6
    assert chain.ports.mcf.ops("CreateFulfillmentOrder")[-1]["items"][0]["quantity"] == 84
    (entry,) = [r for r in chain.ports.ledger.query(tenant="corp_c") if r.action == "allocate_store_order"]
    assert "cut" in entry.rationale


def test_open_orders_hold_their_stock(chain):
    inv(chain, "receipt", C_UPC, "FC-WEST", 30)
    chain.gateway.receive_store_order("key-c", store_order("S-1", cases=4, pack=6))   # takes 24
    chain.gateway.receive_store_order("key-c", store_order("S-2", cases=4, pack=6))   # 6 left: 1 case
    assert sent(chain, "store_order_allocated")[-1]["lines"][0]["cases_allocated"] == 1
    chain.gateway.receive_store_order("key-c", store_order("S-3", cases=2, pack=6))   # nothing left
    (rej,) = sent(chain, "order_rejected")
    assert rej["order_id"] == "S-3" and "web reserve" in rej["reasons"][0]
    assert not chain.ports.store.get("orders", "CORPC-S-3")


# ---------------------------------------------------------------- ship notice

def test_the_ship_notice_reaches_the_store(chain):
    inv(chain, "receipt", C_UPC, "FC-WEST", 200)
    chain.gateway.receive_store_order("key-c", store_order())
    notice = {"order_id": "CORPC-S-1", "asnId": "ASN1", "carrier": "Estes", "proNumber": "123", "pallets": 1,
              "appointment": "2026-10-09T08:00:00+00:00", "lines": [{"sellerSku": C_UPC, "unitsShipped": 24}]}
    for _ in range(2):                                                    # Corp A may send it twice
        chain.ports.bus.publish(Event(topics.ORDERS_LIFECYCLE, "ship_notice", notice, chain.ports.clock.now()))
    (asn,) = sent(chain, "advance_ship_notice")
    assert asn["order_id"] == "S-1" and asn["store_id"] == "ST-0101" and asn["proNumber"] == "123"
    assert len(chain.ports.store.get("orders", "CORPC-S-1")["ship_notices"]) == 1


# ---------------------------------------------------------------- per-client inventory

def test_inventory_belongs_to_its_client(chain):
    inv(chain, "receipt", C_UPC, "FC-WEST", 50)
    inv(chain, "receipt", "190000000001", "FC-WEST", 50)
    states = {s["sku"]: s for s in chain.ports.store.query("inventory_state", node_id="FC-WEST")}
    assert states[C_UPC]["tenant_id"] == "corp_c" and states["190000000001"]["tenant_id"] == "corp_b"
    inv(chain, "receipt", "999999999999", "FC-WEST", 5)                    # nobody's item
    assert "999999999999" not in {s["sku"] for s in chain.ports.store.query("inventory_state")}
    decant = []
    chain.ports.bus.subscribe(topics.CORPB_OUTBOUND, decant.append, subscription="test-decant")
    chain.reconciliation._transmit_decant_counts("FC-WEST")
    assert {c["sku"] for c in decant[-1].payload["counts"]} == {"190000000001"}   # Corp B sees only its own


# ---------------------------------------------------------------- the whole chain

def test_stores_are_replenished_end_to_end():
    with build_world(catalog_size=12, orders_per_day=60, corpc=True, corpc_size=12) as w:
        w.run(hours=24 * 8, step_min=20)
        orders = w.corpc.store.query("order")
        stores = collections.Counter(o["status"] for o in orders if o["channel"] == "store")
        assert stores["received"] >= 5, stores
        got = [o for o in orders if o["status"] == "received"]
        assert all(o["ship_notice"] and o["ship_notice"]["proNumber"] for o in got)
        assert all(o["allocated"] for o in got)
        web = collections.Counter(o["status"] for o in orders if o["channel"] == "web")
        assert web["tracking:delivered"] > 50, web
        tower = w.system.ports.store.query("orders", tenant_id="corp_c")
        assert {o["channel"] for o in tower} == {"online", "store"}
        assert all(o["order_id"].startswith("CORPC-") for o in tower)
        # Corp B is untouched by the second client: its orders still flow and its catalog is its own.
        assert sum(o["state"] == "delivered" for o in w.system.ports.store.query("orders", tenant_id="corp_b")) > 50
        assert not w.system.config.pilot_upcs & w.system.config.tenant_upcs["corp_c"]
