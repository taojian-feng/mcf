"""Consumer returns: policy at the gateway and the tower, label from Corp A, grading, refund decision."""
import collections
from datetime import timedelta

import pytest

from conftest import PILOT_UPCS
from evals.world import build_world
from tower.adapters.memory import StaticDirectory
from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import OrderLine, OrderRecord, OrderState, ReturnLine, ReturnReason
from tower.core.ports import Event
from tower.core.tenants import Tenant, default_tenants
from tower.edge.gateway import Gateway, GatewayError
from tower.runtime import build, memory_ports
from tower.agents.returns import ReturnsAgent

UPC = "190000000001"
KEYS = {"partner-corpb-api-key": "key-b", "partner-corpc-api-key": "key-c"}


@pytest.fixture
def sys_():
    tenants = default_tenants()
    tenants["corp_c"] = Tenant(tenant_id="corp_c", name="Corp C", promise_days=[3], return_window_days=0)
    config = PilotConfig(pilot_upcs=set(PILOT_UPCS), tenants=tenants)
    system = build(memory_ports(directory=StaticDirectory()), config, sleep=lambda s: None)
    system.gateway = Gateway(system.ports.bus, KEYS.__getitem__, system.ports.clock,
                             tenants=lambda: system.config.tenants)
    system.oms = []
    system.ports.bus.subscribe(topics.OMS_OUTBOUND, system.oms.append, subscription="test-oms")
    system.ports.mcf.responder["CreateFulfillmentReturn"] = lambda p: {
        "rmaId": f"RMA-{p['sellerReturnId']}", "returnToNode": "FC-WEST",
        "label": {"carrier": "UPS", "trackingNumber": "RT1"}, "shipBy": "2026-10-20"}
    return system


def delivered(system, order_id="1001", *, qty=2, days_ago=3, state=OrderState.DELIVERED):
    now = system.ports.clock.now()
    rec = OrderRecord(order_id=order_id, client_order_id=order_id, placed_at=now - timedelta(days=days_ago + 3),
                      promised_by=now.date(), delivery_promise_days=3, state=state,
                      delivered_on=(now - timedelta(days=days_ago)).date() if state == OrderState.DELIVERED else None,
                      lines=[OrderLine(line_id="1", upc=UPC, quantity=qty)])
    system.ports.store.put("orders", order_id, rec.model_dump(mode="json"))


def ask(system, return_id="R1", order_id="1001", qty=1, reason="no_longer_needed", key="key-b"):
    body = {"return_id": return_id, "order_id": order_id, "requested_at": system.ports.clock.now().isoformat(),
            "lines": [{"line_id": "1", "upc": UPC, "quantity": qty, "reason": reason}]}
    return system.gateway.receive_return(key, body)


def sent(system, kind):
    return [e.payload for e in system.oms if e.type == kind]


def corpa(system, kind, **payload):
    system.ports.bus.publish(Event(topics.RETURNS_EVENTS, kind, payload, system.ports.clock.now()))


# ---------------------------------------------------------------- authorization

def test_a_return_gets_an_rma_and_a_label(sys_):
    delivered(sys_)
    assert ask(sys_)["return_id"] == "R1"
    (req,) = sys_.ports.mcf.ops("CreateFulfillmentReturn")
    assert req["sellerFulfillmentOrderId"] == "1001" and req["items"][0]["sellerFulfillmentOrderItemId"] == "1"
    (auth,) = sent(sys_, "return_authorized")
    assert auth["rma_id"] == "RMA-R1" and auth["label"]["trackingNumber"] == "RT1" and auth["ship_by"] == "2026-10-20"
    assert sys_.ports.store.get("returns", "R1")["state"] == "authorized"


@pytest.mark.parametrize("setup,qty,why", [
    (dict(days_ago=31), 1, "return window is 30 days"),
    (dict(state=OrderState.SHIPPED), 1, "has not been delivered"),
    (dict(), 3, "2 left to return"),
])
def test_the_return_policy(sys_, setup, qty, why):
    delivered(sys_, **setup)
    ask(sys_, qty=qty)
    (rej,) = sent(sys_, "return_rejected")
    assert why in rej["reasons"][0] and rej["return_id"] == "R1" and rej["order_id"] == "1001"
    assert not sys_.ports.mcf.ops("CreateFulfillmentReturn")


def test_units_can_only_come_back_once(sys_):
    delivered(sys_, qty=2)
    ask(sys_, "R1", qty=1)
    ask(sys_, "R2", qty=1)
    ask(sys_, "R3", qty=1)
    assert len(sent(sys_, "return_authorized")) == 2
    assert "0 left to return" in sent(sys_, "return_rejected")[0]["reasons"][0]


def test_gateway_checks(sys_):
    with pytest.raises(GatewayError) as e:
        ask(sys_, key="key-x")
    assert e.value.status == 401
    with pytest.raises(GatewayError) as e:
        ask(sys_, key="key-c")                   # Corp C's contract takes no returns
    assert e.value.status == 403
    body = {"return_id": "R9", "order_id": "1001", "requested_at": sys_.ports.clock.now().isoformat(),
            "lines": [{"line_id": "1", "upc": UPC, "quantity": 1, "reason": "because"}]}
    with pytest.raises(GatewayError) as e:
        sys_.gateway.receive_return("key-b", body)
    assert e.value.status == 422


def test_corp_a_refusing_the_return_is_reported(sys_):
    delivered(sys_)
    sys_.ports.mcf.script["CreateFulfillmentReturn"] = ["validation"]
    ask(sys_)
    (rej,) = sent(sys_, "return_rejected")
    assert "Corp A refused" in rej["reasons"][0]
    assert sys_.ports.store.get("returns", "R1") is None


# ---------------------------------------------------------------- the parcel and the bench

def test_grading_decides_refund_and_disposition(sys_):
    delivered(sys_, qty=3)
    body = {"return_id": "R1", "order_id": "1001", "requested_at": sys_.ports.clock.now().isoformat(),
            "lines": [{"line_id": "1", "upc": UPC, "quantity": 3, "reason": "no_longer_needed"}]}
    sys_.gateway.receive_return("key-b", body)
    corpa(sys_, "return_status", return_id="R1", status="IN_TRANSIT", carrier="UPS", tracking_number="RT1")
    corpa(sys_, "return_status", return_id="R1", status="DELIVERED", carrier="UPS", tracking_number="RT1")
    corpa(sys_, "return_status", return_id="R1", status="IN_TRANSIT")        # late webhook: ignored
    assert [u["status"] for u in sent(sys_, "return_update")] == ["in_transit", "arrived"]
    corpa(sys_, "return_received", return_id="R1", items=[
        {"sellerSku": UPC, "grade": "SELLABLE", "quantity": 1}, {"sellerSku": UPC, "grade": "DAMAGED", "quantity": 1}])
    (got,) = sent(sys_, "return_received")
    assert got["refund_units"] == 1 and got["held_units"] == 2
    whys = {h["why"].split(",")[0] for h in got["lines"][0]["holds"]}
    assert whys == {"arrived damaged", "not in the parcel"}
    (disp,) = sys_.ports.mcf.ops("SetReturnDisposition")
    assert disp["rmaId"] == "RMA-R1" and disp["items"] == [
        {"sellerSku": UPC, "grade": "DAMAGED", "quantity": 1, "disposition": "liquidate"}]
    assert sys_.ports.store.get("returns", "R1")["state"] == "closed"
    actions = {r.action for r in sys_.ports.ledger.query() if r.actor == "returns_agent"}
    assert {"authorize_return", "decide_refund", "dispose_return"} <= actions


def test_refund_rules():
    line = lambda reason: [ReturnLine(line_id="1", upc=UPC, quantity=2, reason=reason)]  # noqa: E731
    decide = ReturnsAgent.refund_decision
    damaged = [{"sku": UPC, "grade": "DAMAGED", "quantity": 2}]
    assert decide(line(ReturnReason.DAMAGED_IN_TRANSIT), damaged)[0]["refund_units"] == 2   # damage was reported
    assert decide(line(ReturnReason.NO_LONGER_NEEDED), damaged)[0]["refund_units"] == 0     # it wasn't
    defective = [{"sku": UPC, "grade": "DEFECTIVE", "quantity": 2}]
    assert decide(line(ReturnReason.NO_LONGER_NEEDED), defective)[0]["refund_units"] == 2
    wrong = [{"sku": UPC, "grade": "WRONG_ITEM", "quantity": 1}, {"sku": UPC, "grade": "SELLABLE", "quantity": 1}]
    out = decide(line(ReturnReason.DEFECTIVE), wrong)[0]
    assert out["refund_units"] == 1 and out["holds"][0]["why"] == "the parcel held a different item"


def test_an_unused_label_expires(sys_):
    delivered(sys_, days_ago=0)
    ask(sys_)
    sys_.ports.bus.advance(timedelta(days=25))                   # label good to Oct 20, plus 10 days grace
    (exp,) = sent(sys_, "return_expired")
    assert exp["return_id"] == "R1" and sys_.ports.store.get("returns", "R1")["state"] == "expired"
    ask(sys_, "R2")                              # the units can be returned again
    assert len(sent(sys_, "return_authorized")) == 2


# ---------------------------------------------------------------- the whole loop

def test_returns_end_to_end():
    with build_world(catalog_size=12, orders_per_day=120, returns=True, return_rate=0.3) as w:
        w.run(hours=24 * 12, step_min=20)
        mine = w.corpb.store.query("return")
        status = collections.Counter(r["status"] for r in mine)
        assert status["refunded"] + status["partly_refunded"] >= 5, status
        assert all(r.get("label") for r in mine if r["status"] in ("refunded", "partly_refunded"))
        tower = w.system.ports.store.query("returns")
        closed = [r for r in tower if r["state"] == "closed"]
        assert closed and all(r["graded"] or r["outcome"] for r in closed)
        # sellable units went back on the books at the returns node
        restocked = sum(g["quantity"] for r in closed for g in r["graded"] if g["grade"] == "SELLABLE")
        assert restocked > 0


def test_world_without_returns_sends_none():
    with build_world(catalog_size=8, orders_per_day=80) as w:
        w.run(hours=24 * 5, step_min=30)
        assert not w.corpb.store.query("return") and not w.system.ports.store.query("returns")
