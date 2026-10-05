import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcf.adapters.memory import StaticDirectory  # noqa: E402
from mcf.core import topics  # noqa: E402
from mcf.core.config import PilotConfig  # noqa: E402
from mcf.core.ports import Event  # noqa: E402
from mcf.runtime import build, memory_ports  # noqa: E402

PILOT_UPCS = {"190000000001", "190000000002"}


@pytest.fixture
def directory():
    return StaticDirectory(
        roles={
            "alice@corpb": {"approver:exception_resolution:corp_b", "approver:disposition:corp_b"},
            "bob@corpb": {"approver_backup:exception_resolution:corp_b"},
            "mallory@corpa-retail": set(),
        },
        pilot_list={"planning_agent", "reporting_agent", "alice@corpb"},
        contacts={"exception_operator": "ops-oncall@corpb", "corp_a_ops": "mcf-ops@corpa",
                  "corp_a_escalation": "mcf-escalation@corpa", "pilot_governance_owner": "gov@corpb",
                  "pilot_program_lead": "lead@corpb"},
    )


@pytest.fixture
def system(directory):
    ports = memory_ports(directory=directory)
    sleeps: list[float] = []
    s = build(ports, PilotConfig(pilot_upcs=set(PILOT_UPCS)), sleep=sleeps.append)
    s.sleeps = sleeps
    return s


def order_payload(order_id="ord-1", *, upc="190000000001", state="WA", promise=2, placed_at=None):
    from mcf.adapters.memory import VirtualClock
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
