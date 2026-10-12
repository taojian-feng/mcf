import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tower.adapters.memory import InMemoryStore, StaticDirectory  # noqa: E402
from tower.core import topics  # noqa: E402
from tower.core.config import PilotConfig  # noqa: E402
from tower.core.ports import Event  # noqa: E402
from tower.runtime import build, memory_ports  # noqa: E402

PILOT_UPCS = {"190000000001", "190000000002"}


def declared_containers() -> frozenset[str]:
    """The Cosmos DB containers the deployment creates (infra/azure/main.bicep)."""
    bicep = (Path(__file__).resolve().parents[1] / "infra" / "azure" / "main.bicep").read_text()
    block = bicep.split("var containers = [", 1)[1].split("\n]", 1)[0]
    return frozenset(re.findall(r"name: '([a-z_]+)'", block))


InMemoryStore.declared = declared_containers()


@pytest.fixture
def directory():
    return StaticDirectory(
        roles={
            "alice@corpb": {"approver:exception_resolution:corp_b", "approver:disposition:corp_b",
                            "approver:replenishment:corp_b", "approver:distribution_plan:corp_b"},
            "bob@corpb": {"approver_backup:exception_resolution:corp_b"},
            "mallory@corpa-retail": set(),
        },
        pilot_list={"planning_agent", "reporting_agent", "alice@corpb", "review@corpb"},
        contacts={"exception_operator": "ops-oncall@corpb", "corp_a_ops": "mcf-ops@corpa",
                  "corp_a_escalation": "mcf-escalation@corpa", "pilot_governance_owner": "gov@corpb",
                  "pilot_program_lead": "lead@corpb", "corp_b_data_contact": "data@corpb",
                  "corp_a_data_contact": "data@corpa", "fulfillment_ops": "fops@corpa",
                  "corp_b_inventory": "inventory@corpb", "corp_b_planner": "planner@corpb",
                  "corp_b_review": "review@corpb", "corp_a_review": "review@corpa"},
    )


@pytest.fixture
def system(directory):
    ports = memory_ports(directory=directory)
    sleeps: list[float] = []
    s = build(ports, PilotConfig(pilot_upcs=set(PILOT_UPCS)), sleep=sleeps.append)
    s.sleeps = sleeps
    return s


def order_payload(order_id="ord-1", *, upc="190000000001", state="WA", promise=2, placed_at=None):
    from tower.adapters.memory import VirtualClock
    placed_at = placed_at or VirtualClock().now()
    return {
        "order_id": order_id, "placed_at": placed_at.isoformat(), "delivery_promise_days": promise,
        "lines": [{"line_id": "1", "upc": upc, "quantity": 1}],
        "consumer": {"name": "Jane Consumer", "email": "jane@example.com", "phone": "+1-206-555-0100",
                     "address": {"line1": "1 Pine St", "city": "Seattle", "state": state, "postal_code": "98101"}},
    }


def send_order(system, payload):
    system.ports.bus.publish(Event(topics.ORDERS_RECEIVED, "order_received", payload, system.ports.clock.now(),
                                   correlation_id=payload["order_id"]))


def status(system, order_id, state, at=None):
    system.ports.bus.publish(Event(topics.ORDERS_LIFECYCLE, "status",
                                   {"order_id": order_id, "state": state,
                                    "at": (at or system.ports.clock.now()).isoformat()},
                                   system.ports.clock.now(), correlation_id=order_id))


def advance(system, **kw):
    system.ports.bus.advance(timedelta(**kw))


def inv(system, type_, sku="190000000001", node="FC-WEST", qty=0, *, at=None, event_id=None, **kw):
    import uuid
    payload = {"event_id": event_id or uuid.uuid4().hex, "type": type_, "sku": sku, "node_id": node, "quantity": qty,
               "occurred_at": (at or system.ports.clock.now()).isoformat(), "source_party": "corp_a", **kw}
    system.ports.bus.publish(Event(topics.INVENTORY_EVENTS, type_, payload, system.ports.clock.now()))
    return payload
