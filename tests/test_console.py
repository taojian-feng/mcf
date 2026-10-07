"""Approval console (R8): approvers act from the signed link in their email."""
import re
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from starlette.testclient import TestClient

from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import ApprovalCategory, ApprovalStatus, OwningParty
from tower.edge.console import Console, create_app
from tower.runtime import build, memory_ports


class Secrets:
    def get_secret(self, name):
        return {"approval-link-key": "link-key"}[name]


@pytest.fixture
def world(directory):
    ports = memory_ports(directory=directory, secrets=Secrets(), console_url="https://console.example")
    system = build(ports, PilotConfig())
    decisions = []
    ports.bus.subscribe(topics.APPROVAL_DECISIONS, decisions.append, subscription="test")
    item = system.approvals.submit(category=ApprovalCategory.DISPOSITION, owning_party=OwningParty.CORP_B,
                                   agent="disposition_agent", subject_id="sku-42",
                                   content={"skus": ["sku-42"], "units": 120, "action": "liquidate"},
                                   rationale="Aged 210 days; storage costs exceed recovery value.")
    http = TestClient(create_app(Console(system.approvals, system.approvals.links)), follow_redirects=False)
    system.http, system.item, system.decisions = http, item, decisions
    return system


def _link(system, to, kind="item"):
    [mail] = [m for m in system.ports.notifier.to(to) if m["subject"].startswith("Approval needed")]
    urls = re.findall(r"https://console\.example\S+", mail["body"])
    url = urls[0] if kind == "item" else urls[1]
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}", parts.query.split("t=", 1)[1]


def test_email_link_opens_the_request_and_approves_it(world):
    path, token = _link(world, "alice@corpb")
    page = world.http.get(path)
    assert page.status_code == 200 and "Disposition for sku-42" in page.text and "liquidate" in page.text
    assert page.headers["referrer-policy"] == "no-referrer"

    done = world.http.post(path.split("?")[0], data={"t": token, "decision": "approve"})
    assert done.status_code == 303 and "done=approved" in done.headers["location"]
    assert world.approvals.get(world.item.approval_id).status == ApprovalStatus.APPROVED
    assert world.approvals.get(world.item.approval_id).decided_by == "alice@corpb"
    assert [e.type for e in world.decisions] == ["approved"]
    assert any(r.action == "approve" and r.actor == "alice@corpb" for r in world.ports.ledger.query())

    again = world.http.post(path.split("?")[0], data={"t": token, "decision": "reject", "reason": "late"})
    assert again.status_code == 409 and "already approved" in again.text


def test_inbox_lists_pending_requests(world):
    path, _ = _link(world, "alice@corpb", kind="inbox")
    page = world.http.get(path)
    assert page.status_code == 200 and "Waiting for you (1)" in page.text and "Disposition for sku-42" in page.text


def test_reject_needs_a_reason(world):
    path, token = _link(world, "alice@corpb")
    r = world.http.post(path.split("?")[0], data={"t": token, "decision": "reject"})
    assert r.status_code == 409 and "rejection reason is required" in r.text
    assert world.approvals.get(world.item.approval_id).status == ApprovalStatus.PENDING


def test_forged_expired_and_unauthorised_links_are_refused(world):
    path, token = _link(world, "alice@corpb")
    base = path.split("?")[0]
    assert world.http.get(f"{base}?t={token[:-2]}xx").status_code == 401
    assert world.http.get(base).status_code == 401

    world.ports.bus.advance(timedelta(days=15))
    assert world.http.get(path).status_code == 401

    mallory = world.approvals.links.token("mallory@corpa-retail")
    page = world.http.get(f"{base}?t={mallory}")
    assert "needs another approver" in page.text
    r = world.http.post(base, data={"t": mallory, "decision": "approve"})
    assert r.status_code == 403
    assert world.approvals.get(world.item.approval_id).status == ApprovalStatus.PENDING
