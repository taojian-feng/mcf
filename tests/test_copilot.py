"""Approval briefs and the operator copilot."""
import json
import re

import pytest
from starlette.testclient import TestClient

from conftest import PILOT_UPCS
from tower.adapters.memory import ScriptedLLM, StaticDirectory
from tower.core.config import PilotConfig
from tower.core.models import ApprovalCategory, ApprovalStatus, OrderLine, OrderRecord, OwningParty
from tower.core.tenants import Tenant, default_tenants
from tower.agents.copilot import Copilot
from tower.edge.console import QUESTIONS_PER_HOUR, Console, create_app
from tower.runtime import build, memory_ports

C_UPC = "390000000001"


class Secrets:
    def get_secret(self, name):
        return {"approval-link-key": "link-key"}[name]


def make(llm=None, briefs=True):
    tenants = default_tenants()
    tenants["corp_c"] = Tenant(tenant_id="corp_c", name="Corp C", promise_days=[3])
    directory = StaticDirectory(roles={
        "alice@corpb": {"approver:replenishment:corp_b", "approver:exception_resolution:corp_b"},
        "cora@corpc": {"approver:exception_resolution:corp_c"},
        "olu@corpa": {"approver:exception_resolution:corp_a"},
        "nobody@x": set()})
    config = PilotConfig(pilot_upcs=set(PILOT_UPCS), tenants=tenants, tenant_upcs={"corp_c": {C_UPC}},
                         approval_briefs=briefs)
    ports = memory_ports(directory=directory, secrets=Secrets(), console_url="https://console.example", llm=llm)
    system = build(ports, config, sleep=lambda s: None)
    for tid, oid, upc in (("corp_b", "1001", "190000000001"), ("corp_c", "CORPC-1001", C_UPC)):
        rec = OrderRecord(order_id=oid, tenant_id=tid, client_order_id="1001", placed_at=ports.clock.now(),
                          promised_by=ports.clock.now().date(), delivery_promise_days=3, state="shipped",
                          lines=[OrderLine(line_id="1", upc=upc, quantity=2)])
        ports.store.put("orders", oid, rec.model_dump(mode="json"))
    for sku, node, qty in (("190000000001", "FC-WEST", 40), (C_UPC, "FC-EAST", 70)):
        ports.store.put("inventory_state", f"{sku}|{node}", {"sku": sku, "node_id": node, "on_hand": qty})
    system.copilot = Copilot(ports.store, ports.ledger, ports.directory, system.audit, ports.clock, config, llm)
    return system


def replenish(system):
    return system.approvals.submit(
        category=ApprovalCategory.REPLENISHMENT, owning_party=OwningParty.CORP_B, agent="planning_agent",
        subject_id="rec-1", rationale="FC-WEST cover below 2 weeks",
        content={"upc": "190000000001", "source": "ISC-WEST", "target": "FC-WEST", "qty": 120, "value": 960.0,
                 "skus": ["190000000001"], "node_ids": ["ISC-WEST", "FC-WEST"]})


# ---------------------------------------------------------------- briefs

def test_without_claude_the_brief_comes_from_the_numbers():
    s = make()
    item = replenish(s)
    b = item.brief
    assert b["source"] == "rules" and b["suggestion"] is None
    assert b["if_approved"] == "Corp A moves 120 units of 190000000001 from ISC-WEST to FC-WEST."
    assert {"label": "Units", "value": "120"} in b["key_numbers"]
    mail = s.ports.notifier.to("alice@corpb")[0]["body"]
    assert "In short:" in mail and "If you reject: No stock moves." in mail


def test_claude_writes_the_brief_from_the_request_and_the_records():
    brief = {"summary": "Move 120 units to FC-WEST, which has 40 left.", "if_approved": "Stock moves.",
             "if_rejected": "Nothing moves.", "key_numbers": [{"label": "On hand at FC-WEST", "value": "40"}],
             "check_first": ["Is a vendor receipt due Monday?"], "suggestion": "approve",
             "suggestion_reason": "Cover is low and the source has stock."}
    llm = ScriptedLLM(structured_outputs={"Brief": brief})
    s = make(llm)
    item = replenish(s)
    assert item.brief["source"] == "claude" and item.brief["suggestion"] == "approve"
    sent = json.loads(llm.seen[-1]["prompt"])
    assert sent["context"]["stock"] == [{"sku": "190000000001", "node": "FC-WEST", "on_hand": 40}]
    assert "Suggested: approve." in s.ports.notifier.to("alice@corpb")[0]["body"]
    assert item.status == ApprovalStatus.PENDING                      # advice only: a person still decides


def test_a_failed_claude_brief_falls_back_and_never_blocks_the_approval():
    class Broken(ScriptedLLM):
        def structured(self, **kw):
            raise RuntimeError("boom")
    s = make(Broken())
    item = replenish(s)
    assert item.brief["source"] == "rules" and s.approvals.get(item.approval_id).status == ApprovalStatus.PENDING


def test_briefs_can_be_switched_off():
    assert replenish(make(briefs=False)).brief is None


def test_the_approval_page_shows_the_brief():
    s = make()
    item = replenish(s)
    http = TestClient(create_app(Console(s.approvals, s.approvals.links)))
    page = http.get(f"/approvals/{item.approval_id}?t={s.approvals.links.token('alice@corpb')}")
    assert "If you approve" in page.text and "It is advice; you decide." in page.text


# ---------------------------------------------------------------- copilot: who sees what

def test_scope_follows_approver_roles():
    s = make()
    assert s.copilot.scope("alice@corpb") == {"corp_b"}
    assert s.copilot.scope("cora@corpc") == {"corp_c"}
    assert s.copilot.scope("olu@corpa") == {"corp_b", "corp_c"}       # Corp A's people see every client
    assert s.copilot.scope("nobody@x") == set()


def test_tools_never_cross_clients():
    s = make()
    run = s.copilot.run_tool
    mine = json.loads(run("get_order", {"order_id": "1001"}, {"corp_b"}))
    assert mine["tenant_id"] == "corp_b"
    theirs = json.loads(run("get_order", {"order_id": "CORPC-1001"}, {"corp_b"}))
    assert "error" in theirs
    assert "error" in json.loads(run("stock", {"sku": C_UPC}, {"corp_b"}))
    assert json.loads(run("stock", {"sku": C_UPC}, {"corp_c"}))["total_on_hand"] == 70
    over = json.loads(run("overview", {}, {"corp_b"}))
    assert over["orders_by_state"] == {"shipped": 1} and over["clients"] == ["Corp B (corp_b)"]
    assert "unknown tool" in run("approve", {}, {"corp_b"})            # nothing that writes exists


def test_ask_uses_tools_and_is_audited():
    llm = ScriptedLLM(tool_calls=[("get_order", {"order_id": "1001"})],
                      answer_text=lambda results: f"Order 1001 is {json.loads(results[0])['state']}.")
    s = make(llm)
    out = s.copilot.ask("alice@corpb", "Where is order 1001?")
    assert out["answer"] == "Order 1001 is shipped." and out["sources"][0]["tool"] == "get_order"
    assert "Corp B (corp_b)" in llm.seen[-1]["system"] and "Corp C" not in llm.seen[-1]["system"]
    (rec,) = [r for r in s.ports.ledger.query() if r.action == "copilot_question"]
    assert rec.subject_id == "alice@corpb" and rec.rationale == "Where is order 1001?"


def test_no_scope_or_no_claude_gives_a_plain_answer():
    llm = ScriptedLLM(answer_text="should not be called")
    s = make(llm)
    assert "don't have access" in s.copilot.ask("nobody@x", "anything?")["answer"]
    assert not llm.seen
    assert "can't answer right now" in make(None).copilot.ask("alice@corpb", "hi")["answer"]


# ---------------------------------------------------------------- the page

def test_ask_page_and_rate_limit():
    s = make(ScriptedLLM(answer_text="All quiet."))
    http = TestClient(create_app(Console(s.approvals, s.approvals.links, s.copilot)))
    token = s.approvals.links.token("alice@corpb")
    assert "Ask the tower" in http.get(f"/ask?t={token}").text
    assert http.get("/ask?t=bad").status_code == 401
    page = http.post("/ask", data={"t": token, "q": "Anything late?"})
    assert page.status_code == 200 and "All quiet." in page.text
    for _ in range(QUESTIONS_PER_HOUR - 1):
        http.post("/ask", data={"t": token, "q": "again?"})
    assert http.post("/ask", data={"t": token, "q": "one more?"}).status_code == 429
    inbox = http.get(f"/approvals?t={token}")
    assert re.search(r"href='/ask\?t=", inbox.text)


# ---------------------------------------------------------------- the evaluation

def test_eval_grading():
    from evals.copilot import declines, says_number, says_word
    assert says_number(1010)("You have 1,010 delivered orders.") and says_number(9)("9 are waiting.")
    assert not says_number(9)("19 are waiting.") and not says_number(10)("10.5 days")
    assert says_number(0)("There are no held returns.")
    assert says_word("out_for_delivery")("It is out for delivery today.")
    assert declines("shipped")("I can't find that order for your clients.")
    assert not declines("shipped")("I can't say much, but it has shipped.")


def test_eval_questions_come_from_the_world():
    from evals.copilot import ask_all, build, cases
    with build(days=3, orders_per_day=60, seed=31) as world:
        items = cases(world)
        assert {c.user for c in items} == {"alice@corpb", "cora@corpc", "olu@corpa"}
        results = ask_all(world, ScriptedLLM(answer_text="I can't do that."), items)
        refusals = [r for r in results if r.case.expected.startswith("says")]
        assert refusals and all(r.ok for r in refusals)


# ---------------------------------------------------------------- console link on demand

def test_console_link_goes_only_to_approvers_and_opens_the_console():
    from tower.tools.console_link import approvers, message
    s = make()
    people = {"mcf:roles": {"alice@corpb": ["approver:replenishment:corp_b"], "ops@x": ["viewer"],
                            "planning_agent": ["approver:replenishment:corp_b"]}}
    assert approvers(people) == ["alice@corpb"]
    subject, body = message(s.approvals.links, "alice@corpb")
    assert "console link" in subject
    http = TestClient(create_app(Console(s.approvals, s.approvals.links, s.copilot)))
    for url in re.findall(r"https://console\.example(\S+)", body):
        assert http.get(url).status_code == 200
