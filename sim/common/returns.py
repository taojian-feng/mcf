"""A retailer's returns desk: some delivered web orders come back.

A few days after delivery a consumer asks to return one item (a share of the order's units, with a
reason). The retailer's order management sends the request to the control tower, gets back a prepaid
label, and later hears what arrived and how much it may refund. Now and then a consumer asks too late,
and the control tower says no.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from sim.common.store import SimStore
from sim.common.web import Sender, post_json

log = logging.getLogger("sim.returns")

REASONS = (("no_longer_needed", 0.55), ("not_as_described", 0.16), ("damaged_in_transit", 0.13),
           ("defective", 0.16))
LATE_SHARE = 0.03                    # asks after the return window
STATUS = {"return_authorized": "authorized", "return_rejected": "rejected", "return_expired": "expired"}


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


class ReturnDesk:
    def __init__(self, store: SimStore, rng: random.Random, *, gateway_url: str, api_key: str, send: Sender,
                 rate: float, prefix: str):
        self.store, self.rng, self.send = store, rng, send
        self.gateway_url, self.api_key = gateway_url.rstrip("/"), api_key
        self.rate, self.prefix = rate, prefix

    def on_delivered(self, order: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        """Maybe the consumer sends something back. Called once, when the order is delivered."""
        if self.rate <= 0 or self.store.get(f"return:{self.prefix}{order['order_id']}") \
                or self.rng.random() >= self.rate:
            return None
        line = self.rng.choice(order["payload"]["lines"])
        r = self.rng.random()
        reason = REASONS[-1][0]
        for name, p in REASONS:
            if r < p:
                reason = name
                break
            r -= p
        days = self.rng.uniform(33, 45) if self.rng.random() < LATE_SHARE else self.rng.uniform(0.5, 12)
        at = now + timedelta(days=days)
        rid = f"{self.prefix}{order['order_id']}"
        payload = {"return_id": rid, "order_id": order["order_id"], "requested_at": _iso(at),
                   "lines": [{"line_id": "1", "upc": line["upc"], "quantity": self.rng.randint(1, line["quantity"]),
                              "reason": reason}]}
        doc = {"id": f"return:{rid}", "doc_type": "return", "return_id": rid, "order_id": order["order_id"],
               "status": "pending_send", "attempts": 0, "next_at": _iso(at), "payload": payload, "updates": []}
        self.store.put(doc)
        return doc

    def flush(self, now: datetime, limit: int = 100) -> int:
        sent = 0
        for doc in self.store.query("return", due_by=_iso(now), status="pending_send")[:limit]:
            doc["payload"]["requested_at"] = _iso(now)
            status, body = post_json(self.send, f"{self.gateway_url}/v1/returns", doc["payload"],
                                     {"X-Api-Key": self.api_key})
            doc = self.store.get(doc["id"]) or doc   # the answer may already be in
            doc["attempts"] += 1
            if status in (200, 201, 202):
                if doc["status"] == "pending_send":
                    doc["status"] = "sent"
                doc["next_at"] = None
                sent += 1
            elif 400 <= status < 500 and status not in (401, 403, 408, 429):
                doc["status"], doc["next_at"], doc["error"] = "refused", None, body[:300].decode(errors="replace")
            else:
                doc["next_at"] = _iso(now + timedelta(seconds=min(1800, 10 * 2 ** min(doc["attempts"], 7))))
            self.store.put(doc)
            if status >= 500:
                break
        return sent

    def receive(self, kind: str, p: dict[str, Any], now: datetime) -> bool:
        """Updates about a return. True when the event was about a return."""
        if not kind or not kind.startswith("return_"):
            return False
        doc = self.store.get(f"return:{p.get('return_id')}")
        if not doc:
            return True
        doc["updates"] = doc["updates"][-10:] + [{"type": kind, "at": _iso(now)}]
        if kind in STATUS:
            doc["status"] = STATUS[kind]
            if kind == "return_authorized":
                doc["label"], doc["rma_id"] = p.get("label"), p.get("rma_id")
            if kind == "return_rejected":
                doc["reasons"] = p.get("reasons")
        elif kind == "return_update":
            doc["status"] = p.get("status") or doc["status"]
        elif kind == "return_received":
            doc["status"] = "refunded" if not p.get("held_units") else "partly_refunded"
            doc["refund_units"], doc["held_units"] = p.get("refund_units"), p.get("held_units")
        self.store.put(doc)
        return True
