"""Step 8: a new client from application to first order, and what Corp A bills its clients."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from conftest import PILOT_UPCS
from tower.adapters.memory import StaticDirectory
from tower.core.config import PilotConfig
from tower.core.models import ApprovalCategory, OrderLine, OrderRecord, OrderState
from tower.edge.connectors import PartnerConnectors
from tower.edge.console import Console, create_app
from tower.edge.gateway import Gateway, GatewayError
from tower.runtime import build, memory_ports
from tower.services import client_keys
from tower.services.clients import ClientApplication, ClientDirectory

KEYS = {"partner-corpb-api-key": "key-b", "partner-corpc-api-key": "key-c", "client-key-master": "master",
        "approval-link-key": "link-key"}
ONBOARDER, FINANCE, CLIENT_APPROVER = "olga@corpa.example", "fin@corpa.example", "pat@lumen.example"


class Secrets:
    def get_secret(self, name):
        return KEYS[name]


@pytest.fixture
def world():
    directory = StaticDirectory(roles={ONBOARDER: {"approver:client_onboarding:corp_a"},
                                       FINANCE: {"approver:invoice:corp_a"},
                                       "alice@corpb": {"approver:exception_resolution:corp_b"}},
                                contacts={"corp_b_billing": "ap@corpb.example"})
    ports = memory_ports(directory=directory, secrets=Secrets(), console_url="https://console.example",
                         gateway_url="https://gateway.example")
    system = build(ports, PilotConfig(pilot_upcs=set(PILOT_UPCS)), sleep=lambda s: None)
    system.gateway = Gateway(ports.bus, KEYS.__getitem__, ports.clock, tenants=ports.directory.tenants,
                             blobs=ports.blobs, announce_feeds=True)
    system.connectors = PartnerConnectors(system, corpb_url=None, secret=KEYS.__getitem__)
    return system


def application(**over):
    fields = dict(name="Lumen Home", channels=["online"], promise_days=[3], expected_orders_per_day=40,
                  approver_email=CLIENT_APPROVER, ops_email="ops@lumen.example",
                  billing_email="ap@lumen.example", outbound_url="https://lumen.example")
    return ClientApplication(**(fields | over))


def onboard(world, **over):
    rec = world.onboarding.apply(application(**over), ONBOARDER)
    assert rec.status == "pending_approval", rec.blockers
    world.approvals.decide(ONBOARDER, rec.approval_id, approve=True)
    return world.onboarding.get(rec.tenant_id)


# ---------------------------------------------------------------- the checks

@pytest.mark.parametrize("over, blocker", [
    (dict(promise_days=[1]), "can promise"),
    (dict(delivery_states=["WA", "HI"]), "not HI"),
    (dict(channels=["marketplace"]), "channels"),
    (dict(approver_email="pat"), "isn't an email"),
    (dict(outbound_url="http://lumen.example"), "https"),
    (dict(rates={"pick_pack_order": 1.0}), "below the floor"),
    (dict(rates={"free_lunch": 1.0}), "unknown rates"),
    (dict(expected_orders_per_day=5000), "over its capacity"),
    (dict(name="Corp B"), "already taken"),
])
def test_an_application_the_network_cant_take_is_blocked(world, over, blocker):
    rec = world.onboarding.apply(application(**over), ONBOARDER)
    assert rec.status == "blocked" and any(blocker in b for b in rec.blockers), rec.blockers
    assert world.approvals.store.query("approvals") == []
    assert any(r.action == "client_application_blocked" for r in world.ports.ledger.query())


def test_warnings_go_to_the_approver(world):
    rec = world.onboarding.apply(application(outbound_url=None, rates={"pick_pack_order": 2.5},
                                             expected_orders_per_day=1300), ONBOARDER)
    assert rec.status == "pending_approval"
    assert len(rec.warnings) == 3        # no endpoint, a discount, the network near capacity
    item = world.approvals.get(rec.approval_id)
    assert item.category == ApprovalCategory.CLIENT_ONBOARDING and item.tenant_id == "lumen_home"
    assert item.content["warnings"] == rec.warnings and item.content["rates"]["pick_pack_order"] == 2.5
    assert world.ports.notifier.to(ONBOARDER)          # Corp A's onboarding approver is asked
    assert not world.approvals.can_decide(CLIENT_APPROVER, item)   # the client can't approve itself


# ---------------------------------------------------------------- approval to go-live

def test_an_approved_client_is_served_end_to_end(world):
    rec = onboard(world)
    assert rec.status == "active" and rec.decided_by == ONBOARDER
    tenant = world.config.tenant("lumen_home")
    assert tenant.keys == "issued" and tenant.order_prefix == "LUMENHOME-"

    # Every directory view knows the client and its people, without a deploy.
    fresh = ClientDirectory(StaticDirectory(), world.ports.store)
    assert "lumen_home" in fresh.tenants()
    assert fresh.has_role(CLIENT_APPROVER, "approver:exception_resolution:lumen_home")
    assert not fresh.has_role(CLIENT_APPROVER, "approver:client_onboarding:corp_a")
    assert fresh.contact("lumen_home_billing") == "ap@lumen.example"
    welcome = world.ports.notifier.to("ops@lumen.example")[0]
    assert "https://gateway.example/v1/catalog" in welcome["body"] and "master" not in welcome["body"]

    # The client calls the gateway with its derived key: catalog first, then an order.
    key = client_keys.api_key("master", tenant)
    with pytest.raises(GatewayError) as e:
        world.gateway.receive_catalog("wrong", {"items": [{"upc": "590000000001"}]})
    assert e.value.status == 401
    with pytest.raises(GatewayError) as e:
        world.gateway.receive_catalog(key, {"items": [{"upc": "59"}]})
    assert e.value.status == 422
    out = world.gateway.receive_catalog(key, {"items": [{"upc": "590000000001", "title": "Lamp", "unit_cost": 21}]})
    assert out["file"].startswith("lumenhome/catalog/")
    assert world.config.catalog("lumen_home") == {"590000000001"}
    assert world.onboarding.run_daily() == []           # catalog is in, no order yet: nothing to say

    world.gateway.receive_order(key, {
        "order_id": "L-1", "placed_at": world.ports.clock.now().isoformat(), "delivery_promise_days": 3,
        "lines": [{"line_id": "1", "upc": "590000000001", "quantity": 1}],
        "consumer": {"name": "Ann", "email": "ann@example.com", "phone": "+1-206-555-0100",
                     "address": {"line1": "1 Pine St", "city": "Seattle", "state": "WA", "postal_code": "98101"}}})
    [order] = world.ports.store.query("orders", tenant_id="lumen_home")
    assert order["state"] != "rejected"
    assert world.onboarding.run_daily() == ["lumen_home"]
    assert world.onboarding.get("lumen_home").status == "live"
    assert any("is live" in m["subject"] for m in world.ports.notifier.to("ops@lumen.example"))


def test_a_rejected_application_is_not_set_up(world):
    rec = world.onboarding.apply(application(), ONBOARDER)
    world.approvals.decide(ONBOARDER, rec.approval_id, approve=False, reason="credit check pending")
    assert world.onboarding.get("lumen_home").status == "rejected"
    assert "lumen_home" not in world.ports.directory.tenants()
    assert any("not taken on" in m["subject"] for m in world.ports.notifier.to(ONBOARDER))
    assert world.onboarding.apply(application(), ONBOARDER).status == "pending_approval"   # may apply again


def test_a_client_without_a_catalog_is_reminded(world):
    onboard(world)
    world.ports.bus.advance(timedelta(days=8))
    assert world.onboarding.run_daily() == ["lumen_home"]
    assert any("waiting for your catalog" in m["subject"] for m in world.ports.notifier.to("ops@lumen.example"))
    assert world.onboarding.run_daily() == []           # once a week, not every day


def test_the_corp_a_sim_lists_items_it_has_never_seen():
    from sim.common.store import MemoryStore
    from sim.corpa.engine import CorpA
    from tower.adapters.memory import VirtualClock
    store = MemoryStore()
    sim = CorpA(store, VirtualClock(), webhook_url="x", webhook_secret="w", send=lambda *a: (200, b""), catalog_size=5)
    status, out = sim.feed_document({"contentType": "LISTINGS", "items": [
        {"sellerSku": "590000000001", "title": "Lamp", "unitCost": 21, "weightLb": 4},
        {"sellerSku": "nope"}]})
    assert status == 200 and out["accepted"] == 1 and out["rejected"] == ["nope"]
    assert sim.items["590000000001"].weight_lb == 4
    again = CorpA(store, VirtualClock(), webhook_url="x", webhook_secret="w", send=lambda *a: (200, b""), catalog_size=5)
    assert again.items["590000000001"].title == "Lamp"          # kept across restarts


# ---------------------------------------------------------------- billing

def shipped(world, order_id, tenant_id, units, *, day, cost=8.0, mode="ground", package=None):
    rec = OrderRecord(order_id=order_id, tenant_id=tenant_id, client_order_id=order_id,
                      placed_at=datetime.combine(day, datetime.min.time(), timezone.utc), promised_by=day,
                      delivery_promise_days=3, state=OrderState.SHIPPED,
                      lines=[OrderLine(line_id="1", upc="190000000001", quantity=units)])
    world.ports.store.put("orders", order_id, rec.model_dump(mode="json"))
    world.ports.store.put("shipment_facts", package or f"P-{order_id}", {
        "package_id": package or f"P-{order_id}", "order_id": order_id, "shipped_on": day.isoformat(),
        "units": units, "fulfillment_cost": cost, "mode": mode})


def test_daily_charges_follow_the_rate_card_and_never_double(world):
    day = date(2026, 9, 10)
    shipped(world, "B-1", "corp_b", 3, day=day)
    shipped(world, "B-1", "corp_b", 3, day=day, package="P-B-1b")            # second parcel, one pick
    shipped(world, "C-1", "corp_c", 40, day=day, cost=200.0, mode="ltl")
    world.ports.store.put("inventory_state", "s1", {"sku": "190000000001", "node_id": "FC-WEST", "on_hand": 300})
    world.ports.store.put("returns", "R1", {"return_id": "R1", "tenant_id": "corp_b", "received_at": f"{day}T15:00:00"})

    totals = world.billing.run_daily(day)
    charges = {c["charge_id"]: c for c in world.ports.store.query("charges")}
    assert charges["corp_b|pick_pack|B-1"]["amount"] == round(3.10 + 2 * 0.65, 2)
    assert charges["corp_b|shipping|P-B-1"]["amount"] == round(8.0 * 1.08, 2)
    assert charges["corp_c|freight|P-C-1"]["amount"] == round(200 * 1.05, 2)
    assert charges["corp_b|storage|2026-09-10"]["amount"] == round(300 * 0.40 / 30, 2)
    assert charges["corp_b|returns|R1"]["amount"] == 3.50
    assert len([c for c in charges if c.startswith("corp_b|pick_pack")]) == 1
    assert totals["corp_b"] > 0 and world.billing.run_daily(day) == {}        # running twice charges nothing


def test_negotiated_rates_apply(world):
    onboard(world, rates={"pick_pack_order": 2.5})
    assert world.config.rates_for("lumen_home")["pick_pack_order"] == 2.5
    assert world.config.rates_for("lumen_home")["shipping_markup"] == 0.08
    assert world.config.rates_for("corp_b")["pick_pack_order"] == 3.10


def test_an_invoice_is_approved_filed_and_emailed(world):
    day = date(2026, 9, 10)
    shipped(world, "B-1", "corp_b", 1, day=day)
    world.billing.run_daily(day)
    [inv] = world.billing.run_monthly("2026-09")
    assert inv["invoice_id"] == "INV-CORPB-202609"
    kinds = [ln["kind"] for ln in inv["lines"]]
    assert kinds == ["pick_pack", "shipping", "minimum"]                     # topped up to the monthly minimum
    assert inv["total"] == 750.0
    assert world.billing.run_monthly("2026-09") == []                        # drafted once
    assert not any(m["subject"].startswith("Corp A invoice") for m in world.ports.notifier.sent)

    item = world.approvals.get(inv["approval_id"])
    assert item.category == ApprovalCategory.INVOICE and not world.approvals.can_decide("alice@corpb", item)
    world.approvals.decide(FINANCE, inv["approval_id"], approve=True)
    issued = world.ports.store.get("invoices", "INV-CORPB-202609")
    assert issued["status"] == "issued"
    assert b"$750.00" in world.ports.blobs.read("reports", "invoices/corp_b/INV-CORPB-202609.md")
    assert b"pick_pack" in world.ports.blobs.read("reports", "invoices/corp_b/INV-CORPB-202609.csv")
    [mail] = world.ports.notifier.to("ap@corpb.example")
    assert mail["subject"] == "Corp A invoice INV-CORPB-202609 for 2026-09: $750.00"


def test_a_rejected_invoice_is_held_and_can_be_redrafted(world):
    shipped(world, "B-1", "corp_b", 1, day=date(2026, 9, 10))
    world.billing.run_daily(date(2026, 9, 10))
    [inv] = world.billing.run_monthly("2026-09")
    world.approvals.decide(FINANCE, inv["approval_id"], approve=False, reason="check the freight")
    held = world.ports.store.get("invoices", inv["invoice_id"])
    assert held["status"] == "held" and held["held_reason"] == "check the freight"
    assert world.ports.notifier.to("ap@corpb.example") == []
    assert len(world.billing.run_monthly("2026-09")) == 1


def test_no_minimum_in_a_clients_first_month_and_big_moves_are_flagged(world):
    rec = onboard(world)
    assert rec.activated_at.strftime("%Y-%m") == world.ports.clock.now().strftime("%Y-%m")
    month = rec.activated_at.strftime("%Y-%m")
    day = rec.activated_at.date()
    shipped(world, "L-1", "lumen_home", 1, day=day)
    world.billing.run_daily(day)
    [inv] = world.billing.run_monthly(month)
    assert "minimum" not in [ln["kind"] for ln in inv["lines"]]

    world.ports.store.put("invoices", "INV-CORPB-202608", {
        "invoice_id": "INV-CORPB-202608", "tenant_id": "corp_b", "month": "2026-08", "total": 2000.0,
        "status": "issued", "lines": [{"kind": "shipping", "label": "Parcel shipping", "amount": 2000.0}]})
    shipped(world, "B-1", "corp_b", 1, day=date(2026, 9, 3))
    world.billing.run_daily(date(2026, 9, 3))
    [b] = world.billing.run_monthly("2026-09")
    assert any("Parcel shipping went from $2,000.00" in f for f in b["flags"])


# ---------------------------------------------------------------- console and copilot

def test_only_corp_a_onboarders_see_the_client_pages(world):
    console = Console(world.approvals, world.approvals.links, onboarding=world.onboarding,
                      key_master=lambda: "master")
    http = TestClient(create_app(console), follow_redirects=False)
    token = world.approvals.links.token(ONBOARDER)
    assert http.get(f"/clients?t={token}").status_code == 200
    other = world.approvals.links.token("alice@corpb")
    assert http.get(f"/clients?t={other}").status_code == 403

    form = {"t": token, "name": "Lumen Home", "channels": "online", "promise_days": "3",
            "expected_orders_per_day": "40", "approver_email": CLIENT_APPROVER, "ops_email": "ops@lumen.example",
            "outbound_url": "https://lumen.example"}
    done = http.post("/clients", data=form)
    assert done.status_code == 303 and "/clients/lumen_home" in done.headers["location"]
    assert world.onboarding.get("lumen_home").status == "pending_approval"
    bad = http.post("/clients", data=form | {"name": "Lumen Two", "promise_days": "1"})
    assert bad.status_code == 422 and "can promise" in bad.text

    rec = world.onboarding.get("lumen_home")
    world.approvals.decide(ONBOARDER, rec.approval_id, approve=True)
    page = http.get(f"/clients/lumen_home?t={token}")
    assert client_keys.api_key("master", world.config.tenant("lumen_home")) in page.text


def test_the_copilot_answers_billing_within_scope(world):
    shipped(world, "B-1", "corp_b", 1, day=world.ports.clock.now().date())
    world.billing.run_daily(world.ports.clock.now().date())
    from tower.agents.copilot import Copilot
    shipped(world, "C-1", "corp_c", 2, day=world.ports.clock.now().date())
    world.billing.run_daily(world.ports.clock.now().date())
    copilot = Copilot(world.ports.store, world.ports.ledger, world.ports.directory, world.audit, world.ports.clock,
                      world.config, None)
    out = json.loads(copilot.run_tool("billing", {}, {"corp_b"}))
    assert [c["client"] for c in out["clients"]] == ["corp_b"] and "corp_c" not in json.dumps(out)
