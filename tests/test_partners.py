"""The whole loop with both partner simulators, over HTTP, in one process with virtual time.

Corp B places orders and drops feed files -> our gateway -> agents -> Corp A's MCF API (OAuth) ->
Corp A's warehouse and carriers -> signed webhooks -> our gateway -> agents -> Corp B.
"""
import json

import pytest
from starlette.testclient import TestClient

from evals.world import SECRETS, _client_sender, build_world
from sim import catalog
from sim.common.web import sign
from sim.corpa.api import Tokens, create_app as corpa_app
from sim.corpa.engine import CorpA
from sim.common.store import MemoryStore
from tower.adapters.http_mcf import HttpMCFTransport
from tower.adapters.memory import VirtualClock

SIZE = 30


@pytest.fixture
def world():
    with build_world(catalog_size=SIZE) as w:
        yield w


def test_orders_flow_end_to_end(world):
    world.run(hours=24 * 3 + 6)
    s, store = world.system, world.ports.store

    # Catalog feed -> pilot scope, listed with Corp A, opening stock reached our books
    assert len(s.config.pilot_upcs) == SIZE
    assert len(world.corpa.items) == SIZE
    assert store.query("inventory_state")

    # Forecast feed with malformed records -> valid ones planned, the rest quarantined
    assert store.query("demand_plan")
    assert any(c == "quarantine" for c, _ in world.ports.blobs.blobs)

    orders = store.query("orders")
    assert len(orders) > 300
    first_day = [o for o in orders if o["placed_at"][:10] == "2026-10-05"]
    delivered = [o for o in orders if o["state"] == "delivered"]
    assert sum(o["state"] == "delivered" for o in first_day) > 0.7 * len(first_day)  # the rest are out for delivery
    assert sum(o["on_time"] for o in delivered) / len(delivered) > 0.9
    assert all(o["transitions"][1]["state"] == "acknowledged" for o in delivered)

    # Shipment facts and picks arrived for reporting and reconciliation
    assert store.query("shipment_facts")
    assert world.ports.store.query("inventory_events", type="pick")

    # Corp B heard back: tracking updates for its orders
    tracked = [o for o in world.corpb.store.query("order") if o["status"].startswith("tracking:")]
    assert tracked

    # Nightly counts reconcile against our books
    report = s.reconciliation.run_daily()
    assert report["reconciled"] > 0
    assert len(report["unreconciled"]) < 0.05 * report["reconciled"]  # only positions opened since the count


def test_exceptions_reach_corp_a_as_cases(world):
    world.run(hours=2)
    # Force a carrier problem on the next accepted order.
    world.corpa._fate = lambda: "lost"
    world.run(hours=24 * 3)
    cases = world.ports.store.query("exception_cases")
    lost = [c for c in cases if c["signal"]["signal_type"] in ("carrier_lost", "projected_delivery_unavailable")]
    assert lost
    # A lost parcel waits for Corp B's approval; once approved, Corp A is asked to reship it.
    item = next(a for a in world.ports.store.query("approvals", status="pending")
                if a["category"] == "exception_resolution")
    world.system.approvals.decide("alice@corpb", item["approval_id"], approve=True)
    world.run(hours=24)
    handoffs = world.ports.store.query("handoffs")
    assert any(h["status"] == "completed" for h in handoffs), [(h["action"], h["status"]) for h in handoffs]
    assert world.corpa.store.query("case")


def test_gateway_rejects_bad_callers(world):
    order = {"order_id": "X1", "placed_at": "2026-10-05T12:00:00+00:00", "delivery_promise_days": 2,
             "lines": [{"line_id": "1", "upc": "640000000008", "quantity": 1}],
             "consumer": {"name": "A B", "address": {"line1": "1 Main", "city": "Reno", "state": "NV",
                                                     "postal_code": "89501"}}}
    assert world.gw.post("/v1/orders", json=order, headers={"X-Api-Key": "wrong"}).status_code == 401
    bad = {**order, "delivery_promise_days": 9}
    r = world.gw.post("/v1/orders", json=bad, headers={"X-Api-Key": SECRETS["partner-corpb-api-key"]})
    assert r.status_code == 422 and "delivery_promise_days" in json.dumps(r.json())

    body = json.dumps({"notificationId": "n1", "notificationType": "SHIPMENT_FACT", "payload": {}}).encode()
    now = int(world.clock.now().timestamp())
    stale = sign(SECRETS["partner-corpa-webhook-secret"], now - 3600, body)
    assert world.gw.post("/v1/mcf/notifications", content=body, headers={"X-MCF-Signature": stale}).status_code == 401
    forged = sign("not-the-secret", now, body)
    assert world.gw.post("/v1/mcf/notifications", content=body, headers={"X-MCF-Signature": forged}).status_code == 401


def test_corp_a_api_auth_and_faults():
    clock = VirtualClock()
    engine = CorpA(MemoryStore(), clock, webhook_url="x", webhook_secret="w", send=lambda *a: (200, b""),
                   catalog_size=5)
    engine.feed_document({"contentType": "LISTINGS",
                          "items": [{"sellerSku": it.upc} for it in catalog.items(size=5)]})
    http = TestClient(corpa_app(engine, Tokens("cid", "sec"), faults=(1.0, 0, 0), clock_s=lambda: clock.now().timestamp()))
    transport = HttpMCFTransport("https://corpa", type("S", (), {"get_secret": lambda self, n: {
        "mcf-client-id": "cid", "mcf-client-secret": "sec"}[n]})(), opener=_client_sender(http),
        clock=lambda: clock.now().timestamp())
    from tower.core.ports import MCFError
    with pytest.raises(MCFError) as e:
        transport.call("GetFulfillmentOrder", {"sellerFulfillmentOrderId": "nope"})
    assert e.value.code == "service_unavailable" and e.value.retryable
    assert http.post("/oauth2/token", data={"grant_type": "client_credentials", "client_id": "cid",
                                            "client_secret": "bad"}).status_code == 401
    assert http.get("/mcf/v1/fulfillment-orders/x").status_code == 401  # no token
