"""Corp B: the retailer's order management system and planning data.

Consumers place orders around the clock on a US evening-heavy curve; each order is sent to the MCF
system's gateway and retried until accepted. Updates coming back (rejections, tracking, delay
notices, decant counts) are recorded against the order. Planning data goes out as files dropped in
the MCF landing storage: the item catalog and cold-start baselines once, then forecast and last
week's actual sales every Monday. A small share of forecast records is malformed, as real feeds are.
"""
from __future__ import annotations

import json
import logging
import math
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from sim import catalog, geo
from sim.common.store import SimStore
from sim.common.web import Sender, post_json, urllib_sender

log = logging.getLogger("sim.corpb")

FIRST = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda", "David", "Elizabeth",
         "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Carlos", "Karen",
         "Daniel", "Lisa", "Matthew", "Nancy", "Anthony", "Betty", "Mark", "Sandra", "Wei", "Ashley", "Priya",
         "Kimberly", "Andrew", "Emily", "Joshua", "Donna", "Kenji", "Michelle", "Luis", "Amanda", "Aisha", "Omar"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
        "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
        "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
        "Walker", "Young", "Allen", "King", "Wright", "Scott", "Nguyen", "Hill", "Flores", "Green", "Patel", "Kim"]
STREETS = ["Main St", "Oak Ave", "Maple Dr", "Cedar Ln", "Pine St", "Elm St", "Washington Ave", "Lake Rd",
           "Hill St", "Park Ave", "Sunset Blvd", "River Rd", "Church St", "Highland Ave", "Broadway", "2nd St"]
# Relative order volume by US Eastern hour (0-23): quiet overnight, busy in the evening.
HOURLY = [0.35, 0.22, 0.15, 0.12, 0.12, 0.18, 0.35, 0.6, 0.85, 1.0, 1.1, 1.2, 1.3, 1.25, 1.2, 1.2, 1.25, 1.35,
          1.5, 1.7, 1.8, 1.65, 1.2, 0.7]
_HOURLY_MEAN = sum(HOURLY) / 24


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def week_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


class CorpB:
    def __init__(self, store: SimStore, clock, *, gateway_url: str, api_key: str,
                 upload: Callable[[str, bytes], None], send: Sender = urllib_sender,
                 rng: random.Random | None = None, orders_per_day: float = 140, speed: float = 1.0,
                 bad_record_rate: float = 0.02, catalog_seed: int = 2026, catalog_size: int = catalog.PILOT_SIZE,
                 go_live_delay: timedelta = timedelta(minutes=15), forecast_delay: timedelta = timedelta(hours=1)):
        self.store, self.clock, self.gateway_url, self.api_key = store, clock, gateway_url.rstrip("/"), api_key
        self.upload, self.send, self.rng = upload, send, rng or random.Random()
        self.orders_per_day, self.speed, self.bad_record_rate = orders_per_day, speed, bad_record_rate
        self.items = catalog.items(catalog_seed, catalog_size)
        self.go_live_delay, self.forecast_delay = go_live_delay, forecast_delay
        self.non_pilot = catalog.non_pilot_upcs()
        self._city_weights = [c[5] for c in geo.CITIES]
        self._item_weights = {r: [it.weekly_units[r] for it in self.items] for r in catalog.REGIONS}

    # ---------------------------------------------------------------- the loop

    def tick(self) -> None:
        now = self.clock.now()
        feeds = self._feeds(now)
        meta = self.store.get("meta:orders") or {"id": "meta:orders", "doc_type": "meta", "last_at": _iso(now)}
        if now < datetime.fromisoformat(feeds["initial"]) + self.go_live_delay:
            return  # the catalog goes first; consumers can order once the items are live
        elapsed = min(3600.0, (now - datetime.fromisoformat(meta["last_at"])).total_seconds())
        expected = self.orders_per_day / 86400 * elapsed * self.speed * HOURLY[(now.hour - 4) % 24] / _HOURLY_MEAN
        for _ in range(_poisson(self.rng, expected)):
            self.place_order(now)
        meta["last_at"] = _iso(now)
        self.store.put(meta)
        self.flush_orders()

    def place_order(self, now: datetime) -> dict[str, Any]:
        city, state, zipc, *_ = self.rng.choices(geo.CITIES, weights=self._city_weights)[0]
        region = geo.region_of(state)
        n_lines = self.rng.choices([1, 2, 3], weights=[72, 21, 7])[0]
        picks = {it.upc for it in self.rng.choices(self.items, weights=self._item_weights[region], k=n_lines)}
        if self.rng.random() < 0.008:
            picks.add(self.rng.choice(self.non_pilot))
        first, last = self.rng.choice(FIRST), self.rng.choice(LAST)
        order_id = f"CB{now:%y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        payload = {
            "order_id": order_id, "placed_at": _iso(now),
            "delivery_promise_days": 2 if self.rng.random() < 0.3 else 3,
            "lines": [{"line_id": str(i + 1), "upc": upc, "quantity": self.rng.choices([1, 2, 3], weights=[85, 12, 3])[0]}
                      for i, upc in enumerate(sorted(picks))],
            "consumer": {
                "name": f"{first} {last}",
                "email": f"{first.lower()}.{last.lower()}{self.rng.randint(1, 999)}@example.com",
                "phone": f"+1-555-01{self.rng.randint(0, 99):02d}",
                "address": {"line1": f"{self.rng.randint(10, 9899)} {self.rng.choice(STREETS)}",
                            "line2": f"Apt {self.rng.randint(1, 40)}" if self.rng.random() < 0.25 else None,
                            "city": city, "state": state, "postal_code": zipc, "country": "US"},
            },
        }
        doc = {"id": f"order:{order_id}", "doc_type": "order", "order_id": order_id, "placed_at": _iso(now),
               "region": region, "status": "pending_send", "attempts": 0, "next_at": _iso(now), "payload": payload,
               "updates": []}
        self.store.put(doc)
        return doc

    def flush_orders(self, limit: int = 200) -> int:
        now = self.clock.now()
        sent = 0
        for doc in self.store.query("order", due_by=_iso(now), status="pending_send")[:limit]:
            status, body = post_json(self.send, f"{self.gateway_url}/v1/orders", doc["payload"],
                                     {"X-Api-Key": self.api_key})
            doc["attempts"] += 1
            if status in (200, 201, 202):
                doc["status"], doc["next_at"], doc["sent_at"] = "sent", None, _iso(now)
                sent += 1
            elif 400 <= status < 500 and status not in (401, 403, 408, 429):
                doc["status"], doc["next_at"] = "refused", None
                doc["error"] = body[:500].decode(errors="replace")
                log.warning("gateway refused %s: %s %s", doc["order_id"], status, doc["error"])
            else:
                doc["next_at"] = _iso(now + timedelta(seconds=min(1800, 10 * 2 ** min(doc["attempts"], 7))))
                doc["error"] = f"{status}"
            self.store.put(doc)
            if status >= 500:
                break
        return sent

    # ---------------------------------------------------------------- updates from the MCF system

    def receive(self, channel: str, body: dict[str, Any]) -> None:
        now = self.clock.now()
        event_id = body.get("event_id") or uuid.uuid4().hex
        if self.store.get(f"evt:{event_id}"):
            return
        self.store.put({"id": f"evt:{event_id}", "doc_type": "inbound_event", "channel": channel,
                        "type": body.get("type"), "received_at": _iso(now), "body": body})
        order_id = (body.get("payload") or {}).get("order_id")
        doc = self.store.get(f"order:{order_id}") if order_id else None
        if doc:
            doc["updates"] = (doc.get("updates") or [])[-20:] + [{"type": body.get("type"), "at": _iso(now)}]
            if body.get("type") == "order_rejected":
                doc["status"] = "rejected"
            elif body.get("type") == "tracking_update":
                doc["status"] = f"tracking:{(body['payload'].get('status') or '').lower()}"
            self.store.put(doc)

    # ---------------------------------------------------------------- planning files

    def _feeds(self, now: datetime) -> dict[str, Any]:
        """Go-live: the item catalog and cold-start baselines first, the first forecast an hour later
        (once Corp A has received opening stock), then forecast and last week's sales every Monday."""
        meta = self.store.get("meta:feeds") or {"id": "meta:feeds", "doc_type": "meta"}
        if not meta.get("initial"):
            self._drop("catalog", now, [{"upc": it.upc, "title": it.title, "category": it.category,
                                         "unit_cost": it.unit_cost} for it in self.items])
            self._drop("cold_start", now, [
                {"upc": it.upc, "region": r, "weekly_units": round(it.weekly_units[r] * self.rng.uniform(0.7, 1.3), 3)}
                for it in self.items for r in catalog.REGIONS])
            meta["initial"] = _iso(now)
        monday = week_of(now.date()).isoformat()
        if not meta.get("weekly") and now >= datetime.fromisoformat(meta["initial"]) + self.forecast_delay:
            self._drop("forecast", now, self._forecast(now.date()))
            meta["weekly"] = monday
        elif now.weekday() == 0 and now.hour >= 2 and meta.get("weekly") not in (None, monday):
            self._drop("forecast", now, self._forecast(now.date()))
            self._drop("sales", now, self._sales(week_of(now.date()) - timedelta(days=7)))
            meta["weekly"] = monday
        self.store.put(meta)
        return meta

    def _forecast(self, today: date) -> list[dict[str, Any]]:
        start = week_of(today)
        out = []
        for it in self.items:
            for r in catalog.REGIONS:
                for w in range(8):
                    rec: dict[str, Any] = {"upc": it.upc, "region": r,
                                           "week_start": (start + timedelta(weeks=w)).isoformat(),
                                           "units": round(it.weekly_units[r] * self.rng.uniform(0.85, 1.15), 3)}
                    if self.rng.random() < self.bad_record_rate:
                        rec = self._corrupt(rec)
                    out.append(rec)
        return out

    def _corrupt(self, rec: dict[str, Any]) -> dict[str, Any]:
        kind = self.rng.choice(["short_upc", "negative", "bad_date", "missing_region"])
        if kind == "short_upc":
            rec["upc"] = rec["upc"][:11]
        elif kind == "negative":
            rec["units"] = -abs(rec["units"]) - 1
        elif kind == "bad_date":
            rec["week_start"] = rec["week_start"].replace("-", "/")
        else:
            rec.pop("region")
        return rec

    def _sales(self, week_start: date) -> list[dict[str, Any]]:
        end = week_start + timedelta(days=7)
        pilot = {it.upc for it in self.items}
        units: dict[tuple[str, str], int] = {}
        for o in self.store.query("order"):
            placed = datetime.fromisoformat(o["placed_at"]).date()
            if week_start <= placed < end:
                for ln in o["payload"]["lines"]:
                    if ln["upc"] in pilot:
                        key = (ln["upc"], o["region"])
                        units[key] = units.get(key, 0) + ln["quantity"]
        return [{"upc": u, "region": r, "week_start": week_start.isoformat(), "units": float(n)}
                for (u, r), n in sorted(units.items())]

    def _drop(self, kind: str, now: datetime, records: list[dict[str, Any]]) -> str:
        path = f"corpb/{kind}/{now:%Y-%m-%d}-{uuid.uuid4().hex[:6]}.json"
        self.upload(path, json.dumps({"feed_type": kind, "generated_at": _iso(now), "records": records}).encode())
        log.info("dropped %s (%d records)", path, len(records))
        return path


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1
