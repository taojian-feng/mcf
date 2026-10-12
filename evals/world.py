"""A whole simulated world in one process with virtual time: Corp B, our system, Corp A (and Corp C).

Corp B places orders and drops feed files -> our gateway -> agents -> Corp A's MCF API (OAuth) ->
Corp A's warehouse and carriers -> signed webhooks -> our gateway -> agents -> Corp B.

With `corpc=True` a second client joins: Corp C's website and stores order from the same network.
With `returns=True` some delivered web orders come back. The evaluations leave out Corp C and returns
so their numbers stay comparable from run to run.

Used by the end-to-end tests and by the evaluations, which run the same world for days at a time.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from starlette.testclient import TestClient

from sim.common.store import MemoryStore
from sim.corpa.api import Tokens, create_app as corpa_app
from sim.corpa.engine import CorpA
from sim.corpb.api import create_app as corpb_app
from sim.corpb.engine import CorpB
from sim.corpc.api import create_app as corpc_app
from sim.corpc.engine import CorpC
from tower.adapters.http_mcf import HttpMCFTransport
from tower.adapters.memory import StaticDirectory, VirtualClock
from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.ports import LLM, Event
from tower.core.tenants import Tenant, default_tenants
from tower.edge.connectors import PartnerConnectors
from tower.edge.gateway import Gateway, create_app as gateway_app
from tower.runtime import System, build, memory_ports

SECRETS = {"mcf-client-id": "corpb-pilot", "mcf-client-secret": "s3cret", "partner-corpa-webhook-secret": "whsec",
           "partner-corpb-api-key": "corpb-key", "corpb-inbound-api-key": "corpb-in", "pii-masking-key": "k",
           "partner-corpc-api-key": "corpc-key", "corpc-inbound-api-key": "corpc-in"}
START = datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc)  # a Monday


class _Secrets:
    def get_secret(self, name: str) -> str:
        return SECRETS[name]


def _client_sender(client: TestClient) -> Callable[..., tuple[int, bytes]]:
    def send(method, url, headers, body, timeout=30):
        r = client.request(method, urlparse(url).path, headers=headers, content=body)
        return r.status_code, r.content
    return send


@dataclass
class World:
    clock: VirtualClock
    ports: Any
    system: System
    corpa: CorpA
    corpb: CorpB
    gw: TestClient
    sleeps: list[float]
    corpc: CorpC | None = None
    _clients: list[TestClient] = field(default_factory=list)

    def run(self, hours: float, step_min: int = 10) -> None:
        for _ in range(int(hours * 60 / step_min)):
            self.ports.bus.advance(timedelta(minutes=step_min))
            self.corpb.tick()
            if self.corpc:
                self.corpc.tick()
            self.corpa.tick()

    def close(self) -> None:
        for c in self._clients:
            c.__exit__(None, None, None)

    def __enter__(self) -> "World":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def build_world(*, catalog_size: int = 30, orders_per_day: int = 200, seed_a: int = 11, seed_b: int = 5,
                llm: LLM | None = None, bad_record_rate: float = 0.05, corpc: bool = False,
                corpc_size: int = 24, corpc_web_per_day: float = 60, seed_c: int = 7,
                returns: bool = False, return_rate: float = 0.08, briefs: bool = False,
                demand_shifts: list[dict[str, Any]] | None = None, bulk_orders: list[dict[str, Any]] | None = None,
                config_overrides: dict[str, Any] | None = None) -> World:
    clock = VirtualClock(START)
    categories = ("exception_resolution", "disposition", "replenishment", "distribution_plan")
    roles = {"alice@corpb": {f"approver:{c}:corp_b" for c in categories}}
    tenants = default_tenants()
    if corpc:
        roles["cora@corpc"] = {f"approver:{c}:corp_c" for c in categories}
        tenants["corp_c"] = Tenant(tenant_id="corp_c", name="Corp C", channels=["online", "store"], promise_days=[3],
                                   outbound_url="https://corpc")
    directory = StaticDirectory(
        roles=roles,
        pilot_list={"planning_agent", "reporting_agent", "alice@corpb"},
        insight={"corp_c": {"planning_agent", "reporting_agent", "cora@corpc"}} if corpc else None,
        contacts={"exception_operator": "ops@corpb", "corp_a_ops": "ops@corpa", "fulfillment_ops": "fops@corpa",
                  "corp_b_data_contact": "data@corpb", "corp_a_data_contact": "data@corpa",
                  "corp_b_planner": "planner@corpb"})
    ports = memory_ports(clock=clock, directory=directory, llm=llm)
    ports.bus.raise_errors = True
    wall = lambda: clock.now().timestamp()  # noqa: E731

    config = PilotConfig(tenants=tenants, approval_briefs=briefs, **(config_overrides or {}))
    gateway = Gateway(ports.bus, SECRETS.__getitem__, clock, wall=wall, tenants=lambda: config.tenants)
    gw = TestClient(gateway_app(gateway)).__enter__()  # one portal per client keeps requests cheap

    corpa = CorpA(MemoryStore(), clock, webhook_url="https://gateway/v1/mcf/notifications",
                  webhook_secret=SECRETS["partner-corpa-webhook-secret"], send=_client_sender(gw),
                  rng=random.Random(seed_a), catalog_size=catalog_size, corpc_size=corpc_size)
    corpa_http = TestClient(corpa_app(corpa, Tokens(SECRETS["mcf-client-id"], SECRETS["mcf-client-secret"]),
                                      faults=(0, 0, 0), clock_s=wall)).__enter__()
    ports.mcf = HttpMCFTransport("https://corpa", _Secrets(), opener=_client_sender(corpa_http), clock=wall)

    sleeps: list[float] = []
    system = build(ports, config, sleep=sleeps.append)

    def upload(path, data):  # Corp B drops a file; Event Grid tells the bus
        ports.blobs.write("landing", path, data)
        ports.bus.publish(Event(topics.FEEDS_ARRIVED, "Microsoft.Storage.BlobCreated",
                                {"subject": f"/blobServices/default/containers/landing/blobs/{path}"}, clock.now()))

    corpb = CorpB(MemoryStore(), clock, gateway_url="https://gateway", api_key=SECRETS["partner-corpb-api-key"],
                  upload=upload, send=_client_sender(gw), rng=random.Random(seed_b), orders_per_day=orders_per_day,
                  bad_record_rate=bad_record_rate, catalog_size=catalog_size,
                  return_rate=return_rate if returns else 0.0, demand_shifts=demand_shifts, bulk_orders=bulk_orders)
    corpb_http = TestClient(corpb_app(corpb, SECRETS["corpb-inbound-api-key"])).__enter__()
    clients = {"corpb": corpb_http}
    corpc_sim = None
    if corpc:
        corpc_sim = CorpC(MemoryStore(), clock, gateway_url="https://gateway", api_key=SECRETS["partner-corpc-api-key"],
                          upload=upload, send=_client_sender(gw), rng=random.Random(seed_c),
                          web_orders_per_day=corpc_web_per_day, catalog_size=corpc_size,
                          return_rate=return_rate if returns else 0.0)
        clients["corpc"] = TestClient(corpc_app(corpc_sim, SECRETS["corpc-inbound-api-key"])).__enter__()

    def post(url, headers, body):  # each client's endpoint, by host
        u = urlparse(url)
        return clients[u.hostname].post(u.path, headers=headers, content=body).status_code
    PartnerConnectors(system, corpb_url="https://corpb", secret=SECRETS.__getitem__, post=post)

    return World(clock, ports, system, corpa, corpb, gw, sleeps, corpc_sim, [gw, corpa_http, *clients.values()])
