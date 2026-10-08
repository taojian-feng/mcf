"""Channel allocation: a client's web and store orders draw on the same stock.

A store order is large and lumpy (a few cases of each item, every week), web orders are small and steady.
Left alone, one store order can empty a node and leave the web short until the next vendor receipt. So
before a store order goes to Corp A, each line is cut to what the client can spare: stock on hand across
the network, minus what open orders already hold, minus the web reserve (the client's recent web demand
for `online_protect_days`). Stores get whole cases only. The cut is a deterministic rule, written to the
audit ledger with its numbers, and the store is told what it will receive before anything ships.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tower.core.config import PilotConfig
from tower.core.models import ACTIVE_STATES, OrderState, StoreOrder
from tower.core.ports import Clock, DocumentStore

ORDERS = "orders"
STATE = "inventory_state"
DEMAND_WINDOW_DAYS = 14
# Picks reach our books when Corp A packs an order; until then an open order still holds its stock.
HOLDING = {s.value for s in ACTIVE_STATES} - {OrderState.PACKED.value, OrderState.SHIPPED.value,
                                               OrderState.OUT_FOR_DELIVERY.value}


@dataclass
class LineAllocation:
    line_id: str
    upc: str
    case_pack: int
    cases_ordered: int
    cases_allocated: int
    available: int
    web_reserve: int

    @property
    def short_cases(self) -> int:
        return self.cases_ordered - self.cases_allocated

    def as_dict(self) -> dict[str, Any]:
        return {"line_id": self.line_id, "upc": self.upc, "case_pack": self.case_pack,
                "cases_ordered": self.cases_ordered, "cases_allocated": self.cases_allocated,
                "units_allocated": self.cases_allocated * self.case_pack, "short_cases": self.short_cases,
                "available_units": self.available, "web_reserve_units": self.web_reserve}


class AllocationService:
    def __init__(self, store: DocumentStore, clock: Clock, config: PilotConfig):
        self.store, self.clock, self.config = store, clock, config

    def on_hand(self, sku: str) -> int:
        return sum(max(0, s["on_hand"]) for s in self.store.query(STATE, sku=sku))

    def _client_orders(self, tenant_id: str) -> list[dict[str, Any]]:
        return self.store.query(ORDERS, tenant_id=tenant_id)

    def held(self, orders: list[dict[str, Any]], sku: str) -> int:
        return sum(ln["quantity"] for o in orders if o["state"] in HOLDING and not o.get("submission_failed")
                   for ln in o["lines"] if ln["upc"] == sku)

    def web_daily_rate(self, orders: list[dict[str, Any]], sku: str, now: datetime) -> float:
        since = now - timedelta(days=DEMAND_WINDOW_DAYS)
        units = sum(ln["quantity"] for o in orders if o.get("channel", "online") == "online"
                    and datetime.fromisoformat(o["placed_at"]) >= since
                    for ln in o["lines"] if ln["upc"] == sku)
        return units / DEMAND_WINDOW_DAYS

    def allocate(self, order: StoreOrder) -> list[LineAllocation]:
        tenant = self.config.tenant(order.tenant_id)
        protect_days = tenant.online_protect_days if tenant else 7.0
        orders = self._client_orders(order.tenant_id)
        now = self.clock.now()
        out, taken = [], {}
        for ln in order.lines:
            available = self.on_hand(ln.upc) - self.held(orders, ln.upc) - taken.get(ln.upc, 0)
            reserve = math.ceil(self.web_daily_rate(orders, ln.upc, now) * protect_days)
            spare = max(0, available - reserve)
            cases = min(ln.cases, spare // ln.case_pack)
            taken[ln.upc] = taken.get(ln.upc, 0) + cases * ln.case_pack
            out.append(LineAllocation(ln.line_id, ln.upc, ln.case_pack, ln.cases, cases, max(0, available), reserve))
        return out
