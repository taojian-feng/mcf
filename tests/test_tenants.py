"""Two retail clients on one control tower: each sees, approves and receives only its own work."""
import json

import pytest

from conftest import PILOT_UPCS, order_payload, status
from tower.adapters.memory import StaticDirectory
from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import ApprovalCategory, OwningParty, UnhappyPathSignal
from tower.core.ports import Event
from tower.core.tenants import Tenant, default_tenants, party_key
from tower.edge.connectors import PartnerConnectors, catalog_doc
from tower.edge.gateway import Gateway, GatewayError
from tower.runtime import build, memory_ports
from tower.services.approval import approver_role
from tower.services.governance import AccessDenied
from tower.tools.people import build as build_people

TWO_UPC = "290000000001"
KEYS = {"partner-corpb-api-key": "key-b", "partner-retailertwo-api-key": "key-two",
        "corpb-inbound-api-key": "out-b", "retailertwo-inbound-api-key": "out-two"}


def tenants():
    t = default_tenants()
    t["retailer_two"] = Tenant(tenant_id="retailer_two", name="Retailer Two", promise_days=[3],
                               outbound_url="https://two.example/")
    return t


@pytest.fixture
def two():
    directory = StaticDirectory(
        roles={"alice@corpb": {"approver:exception_resolution:corp_b"},
               "tara@two": {"approver:exception_resolution:retailer_two"}},
        pilot_list={"planning_agent", "alice@corpb"},
        insight={"retailer_two": {"planning_agent", "tara@two"}},
        contacts={"exception_operator": "ops@corpa", "corp_a_ops": "mcf-ops@corpa"})
    config = PilotConfig(pilot_upcs=set(PILOT_UPCS), tenants=tenants(), tenant_upcs={"retailer_two": {TWO_UPC}})
    system = build(memory_ports(directory=directory), config, sleep=lambda s: None)
    system.gateway = Gateway(system.ports.bus, KEYS.__getitem__, system.ports.clock,
                             tenants=lambda: system.config.tenants)
    return system


def two_order(order_id="1001", **kw):
    return order_payload(order_id, **{"upc": TWO_UPC, "promise": 3, **kw})


# ---------------------------------------------------------------- gateway: the key decides the client

def test_the_key_names_the_client_and_ids_are_namespaced(two):
    assert two.gateway.receive_order("key-b", order_payload("1001"))["order_id"] == "1001"
    spoof = {**two_order("1001"), "tenant_id": "corp_b"}          # a body can't claim another client
    assert two.gateway.receive_order("key-two", spoof)["order_id"] == "1001"

    b, t = two.ports.store.get("orders", "1001"), two.ports.store.get("orders", "RETAILERTWO-1001")
    assert b["tenant_id"] == "corp_b" and b["client_order_id"] == "1001"
    assert t["tenant_id"] == "retailer_two" and t["client_order_id"] == "1001"
    sent = {r["sellerFulfillmentOrderId"] for r in two.ports.mcf.ops("CreateFulfillmentOrder")}
    assert sent == {"1001", "RETAILERTWO-1001"}                   # two orders, no collision at Corp A


def test_gateway_enforces_each_contract(two):
    with pytest.raises(GatewayError) as e:
        two.gateway.receive_order("key-x", two_order())
    assert e.value.status == 401
    with pytest.raises(GatewayError) as e:
        two.gateway.receive_order("key-two", order_payload("1002", upc=TWO_UPC, promise=2))
    assert e.value.status == 422 and "[3]" in json.dumps(e.value.details)
    two.config.tenants["retailer_two"].active = False
    with pytest.raises(GatewayError) as e:
        two.gateway.receive_order("key-two", two_order())
    assert e.value.status == 403


def test_each_client_orders_only_from_its_own_catalog(two):
    two.gateway.receive_order("key-b", order_payload("b-1", upc=TWO_UPC))
    two.gateway.receive_order("key-two", two_order("t-1", state="AK"))
    rejected = {e.payload["tenant_id"]: e.payload for e in two.ports.bus.events(topics.OMS_OUTBOUND, "order_rejected")}
    assert "not in the client's catalog" in rejected["corp_b"]["reasons"][0]
    assert rejected["retailer_two"]["order_id"] == "t-1"          # the client's own id, not ours
    assert "delivery area" in rejected["retailer_two"]["reasons"][0]


# ---------------------------------------------------------------- exceptions, approvals and audit stay with the client

def test_a_clients_exception_goes_to_its_own_approvers(two):
    two.gateway.receive_order("key-two", two_order())
    status(two, "RETAILERTWO-1001", "shipped")
    sig = UnhappyPathSignal(signal_type="carrier_lost", source="integration_layer", order_id="RETAILERTWO-1001")
    two.ports.bus.publish(Event(topics.EXCEPTION_SIGNALS, sig.signal_type, sig.model_dump(mode="json"),
                                two.ports.clock.now()))
    [case] = two.ports.store.query("exception_cases")
    assert case["tenant_id"] == "retailer_two"
    item = two.approvals.get(case["approval_id"])
    assert item.tenant_id == "retailer_two" and item.routed_to == ["tara@two"]

    assert two.approvals.visible_to("alice@corpb") == []           # Corp B's approver sees nothing of it
    with pytest.raises(PermissionError):
        two.approvals.decide("alice@corpb", item.approval_id, approve=True)
    two.approvals.decide("tara@two", item.approval_id, approve=True)

    hand = two.handoffs.open_view()
    assert hand and {h["tenant_id"] for h in hand} == {"retailer_two"}
    oms = [e.payload for e in two.ports.bus.events(topics.OMS_OUTBOUND) if e.payload.get("case_id")]
    assert oms and all(p["tenant_id"] == "retailer_two" and p["order_id"] == "1001" for p in oms)

    mine = two.audit.query(tenant="retailer_two")
    assert {"classify_exception", "approval_requested", "approve"} <= {r.action for r in mine}
    assert not [r for r in two.audit.query(tenant="corp_b") if "RETAILERTWO-1001" in r.order_ids]


def test_insight_data_is_fenced_per_client(two):
    two.governance.check_insight_access("alice@corpb", "sales:corp_b")
    two.governance.check_insight_access("tara@two", "sales:retailer_two", tenant_id="retailer_two")
    with pytest.raises(AccessDenied):
        two.governance.check_insight_access("alice@corpb", "sales:retailer_two", tenant_id="retailer_two")
    with pytest.raises(AccessDenied):
        two.governance.check_insight_access("tara@two", "sales:corp_b")


# ---------------------------------------------------------------- connectors and feeds

def test_updates_go_to_the_right_client_with_its_key(two):
    posts = []
    PartnerConnectors(two, corpb_url="https://corpb/", secret=KEYS.__getitem__,
                      post=lambda url, headers, body: posts.append((url, headers["X-Api-Key"])) or 202)
    two.gateway.receive_order("key-b", order_payload("b-1", upc="999"))
    two.gateway.receive_order("key-two", two_order("t-1", upc="999"))
    assert sorted(posts) == [("https://corpb/oms/v1/events", "out-b"), ("https://two.example/oms/v1/events", "out-two")]


def test_a_catalog_feed_lands_in_its_clients_catalog(two):
    conn = PartnerConnectors(two, corpb_url=None, secret=KEYS.__getitem__)
    records = [{"upc": "290000000009", "title": "Lamp", "unit_cost": 20}]
    two.ports.blobs.write("landing", "retailertwo/catalog/c1.json", json.dumps({"records": records}).encode())
    conn.on_feed(Event(topics.FEEDS_ARRIVED, "Microsoft.Storage.BlobCreated",
                       {"subject": "/blobServices/default/containers/landing/blobs/retailertwo/catalog/c1.json"},
                       two.ports.clock.now()))
    assert two.config.catalog("retailer_two") == {"290000000009"}
    assert two.config.pilot_upcs == set(PILOT_UPCS)                # Corp B's catalog is untouched
    assert two.ports.store.get(*catalog_doc("retailer_two"))["tenant_id"] == "retailer_two"


def test_a_catalog_set_aside_before_its_client_was_registered_is_caught_up(two):
    late = Tenant(tenant_id="retailer_three", name="Retailer Three", promise_days=[3])
    conn = PartnerConnectors(two, corpb_url=None, secret=KEYS.__getitem__)
    records = [{"upc": "390000000002", "title": "Rug", "unit_cost": 40}]
    two.ports.blobs.write("landing", "retailerthree/catalog/2026-10-08-a.json", json.dumps({"records": records}).encode())
    event = Event(topics.FEEDS_ARRIVED, "Microsoft.Storage.BlobCreated",
                  {"subject": "/blobServices/default/containers/landing/blobs/retailerthree/catalog/2026-10-08-a.json"},
                  two.ports.clock.now())
    conn.on_feed(event)                                                          # no such client: set aside
    assert two.config.catalog("retailer_three") == set()

    two.config.tenants = {**two.config.tenants, "retailer_three": late}          # registry reread later
    assert conn.catch_up_catalogs() == ["retailerthree/catalog/2026-10-08-a.json"]
    assert two.config.catalog("retailer_three") == {"390000000002"}
    assert conn.catch_up_catalogs() == []                                        # only once
    assert two.config.pilot_upcs == set(PILOT_UPCS)


def test_a_feed_from_a_new_client_rereads_the_registry(two):
    registry = {**tenants(), "retailer_three": Tenant(tenant_id="retailer_three", name="Retailer Three",
                                                      promise_days=[3])}
    two.ports.directory.tenants = lambda: registry
    conn = PartnerConnectors(two, corpb_url=None, secret=KEYS.__getitem__)
    records = [{"upc": "390000000002", "title": "Rug", "unit_cost": 40}]
    two.ports.blobs.write("landing", "retailerthree/catalog/c1.json", json.dumps({"records": records}).encode())
    conn.on_feed(Event(topics.FEEDS_ARRIVED, "Microsoft.Storage.BlobCreated",
                       {"subject": "/blobServices/default/containers/landing/blobs/retailerthree/catalog/c1.json"},
                       two.ports.clock.now()))
    assert two.config.catalog("retailer_three") == {"390000000002"}


# ---------------------------------------------------------------- the people file

def test_people_file_with_two_clients():
    out = build_people({
        "default_email": "me@x.com",
        "clients": {"corp_b": {"approvers": {"disposition": ["ann@b.com"]}},
                    "retailer_two": {"name": "Retailer Two", "order_prefix": "RT2-", "promise_days": [3],
                                     "delivery_states": ["ca", "NV"],
                                     "approvers": {"exception_resolution": ["tara@two.com"]},
                                     "contacts": {"ops": "ops@two.com"}}}})
    roles = out["mcf:roles"]
    assert approver_role(ApprovalCategory.EXCEPTION_RESOLUTION, OwningParty.CORP_B, "retailer_two") in roles["tara@two.com"]
    assert not any(r.endswith(":retailer_two") for r in roles["ann@b.com"])
    assert out["mcf:contacts"]["retailer_two_ops"] == "ops@two.com"
    assert out["mcf:contacts"]["corp_b_ops"] == "me@x.com"
    two = Tenant.model_validate({**out["mcf:tenants"]["retailer_two"], "tenant_id": "retailer_two"})
    assert two.order_prefix == "RT2-" and two.eligible_states == {"CA", "NV"} and two.promise_days == [3]
    assert out["mcf:tenants"]["corp_b"]["api_key_secret"] == "partner-corpb-api-key"
    assert "tara@two.com" in out["mcf:insight_access"]["retailer_two"]
    assert "tara@two.com" not in out["mcf:insight_access"]["corp_b"]
    assert "ann@b.com" not in out["mcf:insight_access"]["retailer_two"]


def test_old_people_file_still_means_tenant_one():
    old = build_people({"default_email": "me@x.com", "corp_b": {"contacts": {"inventory": "inv@b.com"}},
                        "can_see_insight_data": ["analyst@b.com"]})
    assert old["mcf:contacts"]["corp_b_inventory"] == "inv@b.com"
    assert "analyst@b.com" in old["mcf:pilot_authorization_list"]
    assert list(old["mcf:tenants"]) == ["corp_b"]


def test_party_key():
    assert party_key(OwningParty.CORP_A, "retailer_two") == "corp_a"
    assert party_key(OwningParty.CORP_B, "retailer_two") == "retailer_two"
    assert party_key(OwningParty.CORP_B, None) == "corp_b"
