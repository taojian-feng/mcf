"""Store receiving problems: what the truck really carried, the store's report, and who pays for a short delivery."""
import collections
from datetime import timedelta

import pytest

from conftest import PILOT_UPCS
from evals.world import build_world
from sim.common.freight import truck_outcome
from tower.adapters.memory import ScriptedLLM, StaticDirectory
from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.tenants import Tenant, default_tenants
from tower.edge.gateway import Gateway, GatewayError
from tower.runtime import build, memory_ports

A, B = "390000000001", "390000000002"
KEYS = {"partner-corpb-api-key": "key-b", "partner-corpc-api-key": "key-c"}
NOTICE = [{"sellerSku": A, "unitsShipped": 24, "casesShipped": 4, "casePack": 6},
          {"sellerSku": B, "unitsShipped": 12, "casesShipped": 3, "casePack": 4}]


def make(mode="rules", llm=None, **cfg):
    tenants = default_tenants()
    tenants["corp_c"] = Tenant(tenant_id="corp_c", name="Corp C", channels=["online", "store"], promise_days=[3],
                               outbound_url="https://corpc")
    directory = StaticDirectory(roles={"ann@corpa": {"approver:receiving_claim:corp_a"}},
                                pilot_list={"planning_agent"}, contacts={"corp_a_ops": "ops@corpa"})
    config = PilotConfig(pilot_upcs=set(PILOT_UPCS), tenants=tenants, tenant_upcs={"corp_c": {A, B}},
                         unit_cost={A: 10.0, B: 25.0}, receiving_review=mode, **cfg)
    s = build(memory_ports(directory=directory, llm=llm), config, sleep=lambda x: None)
    s.ports.bus.advance(timedelta(days=3, hours=1))                        # the delivery day, after unloading
    s.gateway = Gateway(s.ports.bus, KEYS.__getitem__, s.ports.clock, tenants=lambda: s.config.tenants)
    s.oms = []
    s.ports.bus.subscribe(topics.OMS_OUTBOUND, s.oms.append, subscription="test-oms")
    # what the tower already knows about the delivery: the order, its ship notice and the carrier's notes
    s.ports.store.put("orders", "CORPC-S-1", {
        "order_id": "CORPC-S-1", "tenant_id": "corp_c", "client_order_id": "S-1", "channel": "store",
        "ship_notices": [{"asnId": "ASN1", "carrier": "Estes", "proNumber": "123", "pallets": 1,
                          "shippedAt": "2026-10-05T18:00:00+00:00", "lines": NOTICE}],
        "tracking": [{"status": "DELIVERED", "events": [
            {"eventCode": "DELIVERED", "eventTime": "2026-10-08T09:00:00+00:00",
             "description": "Delivered to store receiving, POD signed clean"}]}]})
    s.ports.store.put("shipment_facts", "BOL1", {"package_id": "BOL1", "order_id": "CORPC-S-1",
                                                "shipped_on": "2026-10-05", "from_node": "ISC-X", "mode": "ltl"})
    return s


def receipt(a=24, b=12, *, damaged_a=0, receipt_id="RCV-S-1", **kw):
    return {"receipt_id": receipt_id, "order_id": "S-1", "store_id": "ST-0101", "asn_id": "ASN1",
            "received_at": "2026-10-08T10:00:00+00:00", **kw,
            "lines": [{"upc": A, "expected_units": 24, "received_units": a, "damaged_units": damaged_a},
                      {"upc": B, "expected_units": 12, "received_units": b, "damaged_units": 0}]}


def sent(s, kind):
    return [e.payload for e in s.oms if e.type == kind]


def claim(s):
    return s.ports.store.get("receiving_claims", "CORPC-RCV-S-1")


def credits(s):
    return [c for c in s.ports.store.query("charges") if c["kind"] == "receiving_credit"]


def call(cause, confidence=0.9, why="evidence"):
    return ScriptedLLM(structured_outputs={"ReceivingCall": {"cause": cause, "confidence": confidence,
                                                             "rationale": why}})


# ---------------------------------------------------------------- the truck

def test_both_ends_of_the_truck_agree_on_what_happened():
    seen = collections.Counter()
    for i in range(400):
        out = truck_outcome(f"ASN{i}", NOTICE)
        assert out == truck_outcome(f"ASN{i}", [dict(ln) for ln in reversed(NOTICE)])   # Corp A and the store
        seen[out["fate"]] += 1
        if out["fate"] == "mis_pick":
            assert sum(out["loaded"].values()) != 36 and out["other_sku"]   # a 6-pack swapped for a 4-pack
        if out["fate"] == "carrier_damage":
            assert out["damaged"] and "damaged" in out["pod_note"]
    assert seen["normal"] > 300 and len(seen) == 5


# ---------------------------------------------------------------- the store's report reaches the tower

def test_receipts_need_a_store_contract_and_sound_lines():
    s = make()
    with pytest.raises(GatewayError) as e:
        s.gateway.receive_store_receipt("key-b", receipt())                   # Corp B has no stores
    assert e.value.status == 403
    bad = receipt(a=3, damaged_a=5)
    with pytest.raises(GatewayError) as e:
        s.gateway.receive_store_receipt("key-c", bad)
    assert e.value.status == 422
    assert s.gateway.receive_store_receipt("key-c", receipt()) == {"accepted": True, "receipt_id": "RCV-S-1"}
    doc = s.ports.store.get("store_receipts", "CORPC-RCV-S-1")
    assert doc["order_id"] == "CORPC-S-1" and doc["problem_lines"] == 0
    assert claim(s) is None and not s.oms                                    # a clean delivery: nothing to do


# ---------------------------------------------------------------- rules

def test_rules_blame_the_warehouse_for_a_short_case_and_credit_the_client():
    s = make()
    s.gateway.receive_store_receipt("key-c", receipt(a=18))
    c = claim(s)
    assert c["call"]["cause"] == "pick_error" and c["status"] == "closed" and c["credit_usd"] == 60.0
    (credit,) = credits(s)
    assert credit["amount"] == -60.0 and credit["tenant_id"] == "corp_c" and credit["ref"] == "RCV-S-1"
    (msg,) = sent(s, "receipt_resolved")
    assert msg["outcome"] == "credited" and msg["receipt_id"] == "RCV-S-1" and msg["order_id"] == "S-1"


def test_damage_is_the_carriers_so_corp_a_files_a_freight_claim():
    s = make()
    s.gateway.receive_store_receipt("key-c", receipt(damaged_a=6))
    c = claim(s)
    assert c["call"]["cause"] == "carrier_damage" and credits(s)[0]["amount"] == -60.0
    (h,) = s.ports.store.query("handoffs", action="freight_claim")
    assert h["responsible_party"] == "corp_a" and h["context"]["pro_number"] == "123"


def test_a_big_credit_waits_for_corp_a():
    s = make(receiving_credit_approval_usd=50)
    s.gateway.receive_store_receipt("key-c", receipt(a=18))
    c = claim(s)
    assert c["status"] == "with_person" and not credits(s)
    (item,) = s.ports.store.query("approvals")
    assert item["category"] == "receiving_claim" and item["owning_party"] == "corp_a"
    assert item["routed_to"] == ["ann@corpa"]
    s.approvals.decide("ann@corpa", item["approval_id"], approve=True)
    assert claim(s)["status"] == "closed" and credits(s)[0]["amount"] == -60.0


# ---------------------------------------------------------------- Claude

def test_claude_sees_the_carrier_note_and_the_warehouse_count():
    llm = call("pick_error", why="a 6-unit overage of the item at ISC-X right after shipping")
    s = make("claude", llm)
    s.ports.store.put("reconciliation_reports", "2026-10-06", {"date": "2026-10-06", "discrepancies": [
        {"sku": A, "node_id": "ISC-X", "expected": 100, "observed": 106}]})
    s.gateway.receive_store_receipt("key-c", receipt(a=18))
    prompt = llm.seen[0]["prompt"]
    assert "POD signed clean" in prompt and '"gap_units": 6' in prompt and "ISC-X" in prompt
    c = claim(s)
    assert c["call"]["by"] == "claude" and c["status"] == "closed" and credits(s)


def test_claude_suspects_a_miscount_and_the_recount_settles_it():
    s = make("claude", call("store_count_error", why="clean POD, no overage at the warehouse"))
    s.gateway.receive_store_receipt("key-c", receipt(b=8))
    assert claim(s)["status"] == "recount_requested" and not credits(s)
    assert sent(s, "receipt_recount_requested")[0]["receipt_id"] == "RCV-S-1"
    s.gateway.receive_store_receipt("key-c", receipt(receipt_id="RCV-S-1-R", recount_of="RCV-S-1"))
    c = claim(s)
    assert c["status"] == "closed" and c["outcome"] == "recount_matched" and not credits(s)
    assert sent(s, "receipt_resolved")[-1]["outcome"] == "no_credit"


def test_a_recount_still_short_goes_to_a_person():
    s = make("claude", call("store_count_error"))
    s.gateway.receive_store_receipt("key-c", receipt(b=8))
    s.gateway.receive_store_receipt("key-c", receipt(b=8, receipt_id="RCV-S-1-R", recount_of="RCV-S-1"))
    c = claim(s)
    assert c["status"] == "with_person" and "recount" in c["evidence"]
    (item,) = s.ports.store.query("approvals")
    s.approvals.decide("ann@corpa", item["approval_id"], approve=False, reason="store keeps the loss")
    assert claim(s)["outcome"] == "rejected" and not credits(s)


def test_an_unsure_call_goes_to_a_person_and_without_claude_the_rules_decide():
    s = make("claude", call("carrier_loss", confidence=0.4))
    s.gateway.receive_store_receipt("key-c", receipt(a=18))
    assert claim(s)["status"] == "with_person" and sent(s, "receipt_under_review")
    s2 = make("claude", ScriptedLLM())                                       # Claude did not answer
    s2.gateway.receive_store_receipt("key-c", receipt(a=18))
    assert claim(s2)["call"]["by"] == "rules (Claude unavailable)" and credits(s2)


def test_the_credit_lands_on_the_invoice():
    s = make()
    s.gateway.receive_store_receipt("key-c", receipt(a=18))
    month = s.ports.clock.now().date().strftime("%Y-%m")
    inv = s.billing.draft("corp_c", month)
    (line,) = [ln for ln in inv["lines"] if ln["kind"] == "receiving_credit"]
    assert line["amount"] == -60.0


# ---------------------------------------------------------------- the whole chain

def test_short_deliveries_are_reported_and_investigated_end_to_end():
    hot = (("mis_pick", 0.15), ("short_pick", 0.15), ("carrier_damage", 0.15), ("carrier_loss", 0.15))
    with build_world(catalog_size=12, orders_per_day=40, corpc=True, corpc_size=12, truck_fates=hot,
                     miscount_scale=3, config_overrides={"receiving_review": "rules"}) as w:
        for _ in range(10):
            w.run(hours=24, step_min=20)
            w.system.run_job("reconciliation_daily")
        receipts = w.corpc.store.query("receipt")
        assert len(receipts) >= 5 and {r["status"] for r in receipts} <= {"sent", "pending_send"}
        claims = w.system.ports.store.query("receiving_claims")
        assert claims, [r["truth"] for r in receipts]
        truth = {f"CORPC-{r['receipt_id']}": r["truth"] for r in receipts}
        assert all(truth[c["claim_id"]] != "normal" for c in claims)          # only real problems are claims
        # a pick error at Corp A leaves the case in the building: the next count finds it
        picked = [o for o in w.corpa.store.query("order") if (o.get("truck") or {}).get("fate") == "short_pick"]
        if picked:
            gaps = [g for rep in w.system.ports.store.query("reconciliation_reports") for g in rep["discrepancies"]]
            assert any(g["sku"] == picked[0]["truck"]["sku"] and g["observed"] > g["expected"] for g in gaps)
