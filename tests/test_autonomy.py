"""Earned autonomy: a kind of approval people keep agreeing with Claude on is approved on its own, audited."""
import itertools
import re
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from starlette.testclient import TestClient

from tower.adapters.memory import ScriptedLLM
from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import ApprovalCategory, ApprovalStatus, OwningParty
from tower.edge.console import Console, create_app
from tower.runtime import build, memory_ports
from tower.services.autonomy import AUTONOMY

SEQ = itertools.count(1)
BRIEF = {"summary": "Restock", "if_approved": "Stock moves", "if_rejected": "Nothing moves", "key_numbers": [],
         "check_first": [], "suggestion": "approve", "suggestion_reason": "Stock is low and the source has plenty."}


class Secrets:
    def get_secret(self, name):
        return {"approval-link-key": "link-key"}[name]


@pytest.fixture
def s(directory):
    llm = ScriptedLLM(structured_outputs={"Brief": dict(BRIEF)})
    ports = memory_ports(directory=directory, llm=llm, secrets=Secrets(), console_url="https://console.example")
    system = build(ports, PilotConfig())
    system.llm, system.decisions = llm, []
    ports.bus.subscribe(topics.APPROVAL_DECISIONS, system.decisions.append, subscription="test")
    return system


def ask(s, suggest="approve", value=120.0, category=ApprovalCategory.REPLENISHMENT):
    if "Brief" in s.llm.structured_outputs:
        s.llm.structured_outputs["Brief"]["suggestion"] = suggest
    return s.approvals.submit(category=category, owning_party=OwningParty.CORP_B, agent="planning_agent",
                              subject_id=f"rec-{next(SEQ)}", rationale="FC-WEST runs out in 3 days",
                              content={"upc": "190000000001", "qty": 12, "value": value,
                                       "skus": ["190000000001"], "node_ids": ["FC-EAST", "FC-WEST"]})


def audits(s, action):
    return [r for r in s.audit.query() if r.action == action]


def decide(s, item, approve=True):
    s.ports.bus.advance(timedelta(minutes=5))
    s.approvals.decide("alice@corpb", item.approval_id, approve=approve, reason="" if approve else "not now")


def history(s, n=20, agree=True):
    """People decide n requests; they agree with Claude's suggestion when `agree`."""
    for i in range(n):
        suggest = "approve" if i % 4 else "reject"
        decide(s, ask(s, suggest), approve=(suggest == "approve") == agree)


def test_an_earned_kind_is_approved_on_its_own_and_audited(s):
    history(s, 19)
    first = ask(s)
    assert first.status == ApprovalStatus.PENDING                          # 19 decisions: not yet
    decide(s, first)
    mails = len(s.ports.notifier.sent)
    item = ask(s)
    assert item.status == ApprovalStatus.APPROVED and item.decided_by == AUTONOMY
    assert "100% of the last 20" in item.reason and item.auto["lane"] == "replenishment:corp_b"
    assert len(s.ports.notifier.sent) == mails                             # nobody is emailed
    assert not [e for e in s.decisions if e.payload["approval_id"] == item.approval_id]
    s.ports.bus.advance(timedelta(minutes=2))                              # after the agent filed the request
    (ev,) = [e for e in s.decisions if e.payload["approval_id"] == item.approval_id]
    assert ev.type == "approved" and ev.payload["originating_agent"] == "planning_agent"
    (rec,) = audits(s, "auto_approved")
    assert rec.actor == AUTONOMY and rec.subject_id == item.approval_id and rec.result["agreement"] == 1.0


def test_it_only_approves_small_requests_claude_suggests_approving(s):
    history(s)
    assert ask(s, "reject").status == ApprovalStatus.PENDING
    assert ask(s, "look_closer").status == ApprovalStatus.PENDING
    assert ask(s, value=900).status == ApprovalStatus.PENDING              # above autonomy_max_usd
    assert ask(s, category=ApprovalCategory.DISPOSITION).status == ApprovalStatus.PENDING   # its own record


def test_one_rejected_approval_suggestion_keeps_autonomy_away(s):
    history(s, 19)
    decide(s, ask(s, "approve"), approve=False)                            # 19 of 20 agreed (95%), but this one counts
    first = ask(s)
    assert first.status == ApprovalStatus.PENDING
    history(s, 19)
    second = ask(s)
    assert second.status == ApprovalStatus.PENDING                         # still among the latest 20
    decide(s, first)
    decide(s, second)
    assert ask(s).status == ApprovalStatus.APPROVED                        # 21 clean decisions since


def test_rules_briefs_do_not_count(s):
    s.llm.structured_outputs.pop("Brief")                                  # Claude did not answer: rules brief
    for _ in range(25):
        decide(s, ask(s))
    s.llm.structured_outputs["Brief"] = dict(BRIEF)
    assert ask(s).status == ApprovalStatus.PENDING


def test_a_spot_check_goes_to_a_person_and_rejecting_it_hands_the_kind_back(s):
    history(s)
    auto = [ask(s) for _ in range(8)]
    assert all(i.status == ApprovalStatus.APPROVED for i in auto)
    nine = ask(s)                                                          # 9 approved on its own, then a check
    assert nine.status == ApprovalStatus.APPROVED
    check = ask(s)
    assert check.status == ApprovalStatus.PENDING and check.auto["spot_check"]
    assert s.ports.notifier.to("alice@corpb")[-1]["subject"].startswith("Approval needed")
    decide(s, check, approve=False)
    (lost,) = audits(s, "autonomy_lost")
    assert lost.result["lane"] == "replenishment:corp_b"
    assert ask(s).status == ApprovalStatus.PENDING


def test_onboarding_and_invoices_never_run_on_their_own(s):
    s.config.autonomy_categories |= {"invoice", "client_onboarding"}
    assert not s.approvals.autonomy.eligible("invoice")
    assert not s.approvals.autonomy.eligible("client_onboarding")
    s.config.autonomy = False
    assert not s.approvals.autonomy.eligible("replenishment")


# ---------------------------------------------------------------- the console switch

def _token(s):
    mail = next(m for m in s.ports.notifier.to("alice@corpb") if m["subject"].startswith("Approval needed"))
    url = re.findall(r"https://console\.example\S+", mail["body"])[0]
    return urlsplit(url).query.split("t=", 1)[1]


def test_an_approver_can_stop_and_start_a_kind_from_the_console(s):
    history(s)
    http = TestClient(create_app(Console(s.approvals, s.approvals.links)), follow_redirects=False)
    token = _token(s)
    page = http.get(f"/autonomy?t={token}").text
    assert "Replenishment for Corp B" in page and "Approving on its own" in page and "20 of 20" in page
    r = http.post("/autonomy", data={"t": token, "lane": "replenishment:corp_b", "action": "stop",
                                     "reason": "peak season"})
    assert r.status_code == 200 and "Stopped by alice@corpb" in r.text
    assert ask(s).status == ApprovalStatus.PENDING
    (rec,) = audits(s, "autonomy_stop")
    assert rec.actor == "alice@corpb" and rec.rationale == "peak season"
    http.post("/autonomy", data={"t": token, "lane": "replenishment:corp_b", "action": "start"})
    assert ask(s).status == ApprovalStatus.APPROVED
    r = http.post("/autonomy", data={"t": token, "lane": "invoice:corp_a", "action": "start"})
    assert r.status_code == 403


def test_the_console_says_who_approved_and_marks_spot_checks(s):
    history(s)
    http = TestClient(create_app(Console(s.approvals, s.approvals.links)), follow_redirects=False)
    token = _token(s)
    item = ask(s)
    assert "Approved by the tower on its own" in http.get(f"/approvals/{item.approval_id}?t={token}").text
    for _ in range(8):
        ask(s)
    check = ask(s)
    assert "Spot check" in http.get(f"/approvals/{check.approval_id}?t={token}").text


# ---------------------------------------------------------------- people's guardrails

def test_approvers_set_the_guardrails_and_the_tower_keeps_inside_them(s):
    history(s)
    a = s.approvals.autonomy
    a.set_guardrails("alice@corpb", "replenishment:corp_b", max_usd=100, daily_limit=2, spot_check_every=10)
    assert ask(s, value=150).status == ApprovalStatus.PENDING              # above the dollar limit people set
    first, second = ask(s, value=80), ask(s, value=80)
    assert first.status == second.status == ApprovalStatus.APPROVED
    assert "up to $100, 2 a day" in first.reason and first.auto["guardrails"]["daily_limit"] == 2
    assert ask(s, value=80).status == ApprovalStatus.PENDING               # daily limit reached
    s.ports.bus.advance(timedelta(days=1))
    assert ask(s, value=80).status == ApprovalStatus.APPROVED              # a new day
    (rec,) = audits(s, "auto_approved")[-1:]
    assert rec.result["guardrails"] == {"max_usd": 100.0, "daily_limit": 2, "spot_check_every": 10}


def test_guardrails_stay_within_bounds_and_survive_stop_and_start(s):
    a = s.approvals.autonomy
    with pytest.raises(ValueError):
        a.set_guardrails("alice@corpb", "replenishment:corp_b", max_usd=10_000, daily_limit=5, spot_check_every=10)
    with pytest.raises(ValueError):
        a.set_guardrails("alice@corpb", "replenishment:corp_b", max_usd=100, daily_limit=5, spot_check_every=1)
    a.set_guardrails("alice@corpb", "replenishment:corp_b", max_usd=100, daily_limit=5, spot_check_every=4)
    a.stop("alice@corpb", "replenishment:corp_b", "peak")
    a.start("replenishment:corp_b")
    g = a.guardrails("replenishment:corp_b")
    assert (g["max_usd"], g["daily_limit"], g["spot_check_every"], g["set_by"]) == (100, 5, 4, "alice@corpb")
    assert a.guardrails("disposition:corp_b")["max_usd"] == s.config.autonomy_max_usd   # untouched lanes: defaults


def test_an_approver_sets_guardrails_from_the_console(s):
    history(s)
    http = TestClient(create_app(Console(s.approvals, s.approvals.links)), follow_redirects=False)
    token = _token(s)
    assert "Guardrails: up to $" in http.get(f"/autonomy?t={token}").text
    form = {"t": token, "lane": "replenishment:corp_b", "action": "guardrails", "max_usd": "50",
            "daily_limit": "3", "spot_check_every": "5", "reason": "new client"}
    r = http.post("/autonomy", data=form)
    assert r.status_code == 200 and "up to $50 and 3 a day" in r.text and "Set by alice@corpb" in r.text
    (rec,) = audits(s, "autonomy_guardrails")
    assert rec.actor == "alice@corpb" and rec.result["after"]["max_usd"] == 50 and rec.rationale == "new client"
    assert ask(s, value=120).status == ApprovalStatus.PENDING
    assert "between $0 and" in http.post("/autonomy", data={**form, "max_usd": "99999"}).text
    assert "enter a number" in http.post("/autonomy", data={**form, "daily_limit": "lots"}).text
    r = http.post("/autonomy", data={**form, "lane": "invoice:corp_a"})
    assert r.status_code == 403
