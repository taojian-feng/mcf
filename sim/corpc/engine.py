"""Corp C: a home goods chain with a website and twelve stores, the control tower's second client.

The website sends consumer orders like Corp B's does. The stores are the offline channel: each store
sells from its shelves every day, and once a week, on its order day, its replenishment system orders
whole cases of anything that has fallen below its reorder point, for delivery three to six days out.
Updates coming back tell the store what was allocated (store orders can be cut to protect web stock),
what is on the truck (the advance ship notice) and when it arrived; arrived cases go onto the shelves.
Web and store orders draw on the same Corp C stock at Corp A, which is the point of the exercise.
Like Corp B it sends planning files: cold-start baselines at go-live, then a forecast and last week's
actual demand on Corp A's network (web plus store orders) every Monday.
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
from sim.common.returns import ReturnDesk
from sim.common.store import SimStore
from sim.common.web import Sender, post_json, urllib_sender
from sim.corpb.engine import FIRST, HOURLY, LAST, STREETS, _HOURLY_MEAN, _poisson, week_of

log = logging.getLogger("sim.corpc")

WEB_SHARE = 1 / 3                 # of each item's demand; the stores sell the rest
REORDER_WEEKS, ORDER_UP_TO_WEEKS = 1.5, 3.0
WINDOW_START_DAYS, WINDOW_END_DAYS = 3, 6
SALES_POSTING_HOUR = 6            # UTC: last night's store sales are booked
# store id, name, city, state, zip, street, weight (relative store size)
STORES = [
    ("ST-0101", "Corp C Seattle Northgate", "Seattle", "WA", "98101", "401 NE Northgate Way", 1.2),
    ("ST-0102", "Corp C Portland Lloyd", "Portland", "OR", "97204", "1200 NE Broadway", 0.9),
    ("ST-0103", "Corp C San Jose Westgate", "San Jose", "CA", "95113", "1600 Saratoga Ave", 1.1),
    ("ST-0104", "Corp C Los Angeles Fairfax", "Los Angeles", "CA", "90012", "6333 W 3rd St", 1.4),
    ("ST-0105", "Corp C Phoenix Camelback", "Phoenix", "AZ", "85004", "2502 E Camelback Rd", 1.0),
    ("ST-0106", "Corp C Denver Cherry Creek", "Denver", "CO", "80202", "3000 E 1st Ave", 1.0),
    ("ST-0201", "Corp C Dallas Preston", "Dallas", "TX", "75201", "8687 N Central Expy", 1.3),
    ("ST-0202", "Corp C Chicago Lincoln Park", "Chicago", "IL", "60601", "1000 W North Ave", 1.3),
    ("ST-0203", "Corp C Atlanta Midtown", "Atlanta", "GA", "30303", "1275 Caroline St", 1.1),
    ("ST-0204", "Corp C Nashville West End", "Nashville", "TN", "37203", "2126 Abbott Martin Rd", 0.8),
    ("ST-0205", "Corp C Philadelphia Rittenhouse", "Philadelphia", "PA", "19107", "1625 Chestnut St", 1.0),
    ("ST-0206", "Corp C Boston Fenway", "Boston", "MA", "02108", "1341 Boylston St", 1.1),
]


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


class CorpC:
    def __init__(self, store: SimStore, clock, *, gateway_url: str, api_key: str,
                 upload: Callable[[str, bytes], None], send: Sender = urllib_sender,
                 rng: random.Random | None = None, web_orders_per_day: float = 60, speed: float = 1.0,
                 catalog_size: int = catalog.CORPC_SIZE, go_live_delay: timedelta = timedelta(minutes=15),
                 return_rate: float = 0.06):
        self.store, self.clock, self.gateway_url, self.api_key = store, clock, gateway_url.rstrip("/"), api_key
        self.upload, self.send, self.rng = upload, send, rng or random.Random()
        self.web_orders_per_day, self.speed, self.go_live_delay = web_orders_per_day, speed, go_live_delay
        self.items = catalog.corpc_items(catalog_size)
        self.returns = ReturnDesk(store, self.rng, gateway_url=gateway_url, api_key=api_key, send=send,
                                  rate=return_rate, prefix="R")
        self.by_upc = {it.upc: it for it in self.items}
        self._web_weights = {r: [it.weekly_units[r] for it in self.items] for r in catalog.REGIONS}
        web_cities = [c for c in geo.CITIES if c[1] in {"WA", "OR", "CA", "AZ", "CO", "TX", "IL", "GA", "TN", "PA",
                                                         "MA", "NY", "FL", "NC", "OH", "MN", "VA", "NJ"}]
        self._cities, self._city_weights = web_cities, [c[5] for c in web_cities]
        region_weight = {r: sum(s[6] for s in STORES if geo.region_of(s[3]) == r) for r in catalog.REGIONS}
        # weekly store sales per store and item: the stores' share of regional demand, split by store size
        self.store_rate = {s[0]: {it.upc: it.weekly_units[geo.region_of(s[3])] * (1 - WEB_SHARE) * s[6]
                                  / region_weight[geo.region_of(s[3])] for it in self.items} for s in STORES}

    # ---------------------------------------------------------------- the loop

    def tick(self) -> None:
        now = self.clock.now()
        live = self._go_live(now)
        if now < live + self.go_live_delay:
            return
        self._web_orders(now)
        self._store_day(now)
        self._planning_feeds(now, live)
        self.flush()
        self.returns.flush(now)

    def _go_live(self, now: datetime) -> datetime:
        meta = self.store.get("meta:catalog")
        if not meta:
            self._drop("catalog", now, [{"upc": it.upc, "title": it.title, "category": it.category,
                                         "unit_cost": it.unit_cost, "case_pack": it.case_pack} for it in self.items])
            meta = {"id": "meta:catalog", "doc_type": "meta", "at": _iso(now)}
            self.store.put(meta)
            self._open_stores(now)
        return datetime.fromisoformat(meta["at"])

    def _open_stores(self, now: datetime) -> None:
        for i, (sid, name, city, state, zipc, street, _w) in enumerate(STORES):
            stock = {u: math.ceil(r * self.rng.uniform(0.9, 2.6)) for u, r in self.store_rate[sid].items()}
            self.store.put({"id": f"store:{sid}", "doc_type": "store", "store_id": sid, "name": name,
                            "address": {"line1": street, "city": city, "state": state, "postal_code": zipc,
                                        "country": "US"},
                            "order_weekday": i % 5, "stock": stock, "on_order": {}, "lost_sales": 0,
                            "sales_day": now.date().isoformat()})

    # ---------------------------------------------------------------- website

    def _web_orders(self, now: datetime) -> None:
        meta = self.store.get("meta:web") or {"id": "meta:web", "doc_type": "meta", "last_at": _iso(now)}
        elapsed = min(3600.0, (now - datetime.fromisoformat(meta["last_at"])).total_seconds())
        expected = self.web_orders_per_day / 86400 * elapsed * self.speed * HOURLY[(now.hour - 4) % 24] / _HOURLY_MEAN
        for _ in range(_poisson(self.rng, expected)):
            self.place_web_order(now)
        meta["last_at"] = _iso(now)
        self.store.put(meta)

    def place_web_order(self, now: datetime) -> dict[str, Any]:
        city, state, zipc, *_ = self.rng.choices(self._cities, weights=self._city_weights)[0]
        region = geo.region_of(state)
        picks = {it.upc for it in self.rng.choices(self.items, weights=self._web_weights[region],
                                                    k=self.rng.choices([1, 2], weights=[80, 20])[0])}
        first, last = self.rng.choice(FIRST), self.rng.choice(LAST)
        order_id = f"CCW{now:%y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        payload = {
            "order_id": order_id, "placed_at": _iso(now), "delivery_promise_days": 3,
            "lines": [{"line_id": str(i + 1), "upc": u, "quantity": 1} for i, u in enumerate(sorted(picks))],
            "consumer": {"name": f"{first} {last}", "email": f"{first.lower()}.{last.lower()}@example.com",
                         "phone": f"+1-555-02{self.rng.randint(0, 99):02d}",
                         "address": {"line1": f"{self.rng.randint(10, 9899)} {self.rng.choice(STREETS)}",
                                     "city": city, "state": state, "postal_code": zipc, "country": "US"}},
        }
        doc = {"id": f"order:{order_id}", "doc_type": "order", "channel": "web", "order_id": order_id,
               "placed_at": _iso(now), "status": "pending_send", "attempts": 0, "next_at": _iso(now),
               "path": "/v1/orders", "payload": payload, "updates": []}
        self.store.put(doc)
        return doc

    # ---------------------------------------------------------------- stores

    def _store_day(self, now: datetime) -> None:
        """Book each store's sales for the days since the last posting, then order on its order day."""
        if now.hour < SALES_POSTING_HOUR:
            return
        today = now.date()
        for st in self.store.query("store"):
            last = datetime.fromisoformat(st["sales_day"]).date()
            if last >= today:
                continue
            for _ in range((today - last).days):
                for upc, weekly in self.store_rate[st["store_id"]].items():
                    sold = _poisson(self.rng, weekly / 7)
                    have = st["stock"].get(upc, 0)
                    st["lost_sales"] += max(0, sold - have)
                    st["stock"][upc] = max(0, have - sold)
            st["sales_day"] = today.isoformat()
            if today.weekday() == st["order_weekday"]:
                self._reorder(st, now)
            self.store.put(st)

    def _reorder(self, st: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        lines = []
        for upc, weekly in sorted(self.store_rate[st["store_id"]].items()):
            pack = self.by_upc[upc].case_pack
            position = st["stock"].get(upc, 0) + st["on_order"].get(upc, 0)
            if position > weekly * REORDER_WEEKS:
                continue
            cases = max(1, math.ceil((weekly * ORDER_UP_TO_WEEKS - position) / pack))
            lines.append({"line_id": str(len(lines) + 1), "upc": upc, "cases": cases, "case_pack": pack})
            st["on_order"][upc] = st["on_order"].get(upc, 0) + cases * pack
        if not lines:
            return None
        today = now.date()
        order_id = f"CCS-{st['store_id']}-{today:%y%m%d}"
        payload = {"order_id": order_id, "placed_at": _iso(now), "store_id": st["store_id"], "store_name": st["name"],
                   "ship_to": st["address"], "window_start": (today + timedelta(days=WINDOW_START_DAYS)).isoformat(),
                   "window_end": (today + timedelta(days=WINDOW_END_DAYS)).isoformat(), "lines": lines}
        doc = {"id": f"order:{order_id}", "doc_type": "order", "channel": "store", "store_id": st["store_id"],
               "order_id": order_id, "placed_at": _iso(now), "status": "pending_send", "attempts": 0,
               "next_at": _iso(now), "path": "/v1/store-orders", "payload": payload, "updates": [],
               "allocated": None, "ship_notice": None}
        self.store.put(doc)
        return doc

    # ---------------------------------------------------------------- sending (web and store orders alike)

    def flush(self, limit: int = 200) -> int:
        now = self.clock.now()
        sent = 0
        for doc in self.store.query("order", due_by=_iso(now), status="pending_send")[:limit]:
            status, body = post_json(self.send, f"{self.gateway_url}{doc['path']}", doc["payload"],
                                     {"X-Api-Key": self.api_key})
            # The tower may answer (allocation, rejection) before the post returns; keep what it said.
            doc = self.store.get(doc["id"]) or doc
            doc["attempts"] += 1
            if status in (200, 201, 202):
                if doc["status"] == "pending_send":
                    doc["status"] = "sent"
                doc["next_at"], doc["sent_at"] = None, _iso(now)
                sent += 1
            elif 400 <= status < 500 and status not in (401, 403, 408, 429):
                doc["status"], doc["next_at"] = "refused", None
                doc["error"] = body[:500].decode(errors="replace")
                self._release(doc)
                log.warning("gateway refused %s: %s %s", doc["order_id"], status, doc["error"])
            else:
                doc["next_at"] = _iso(now + timedelta(seconds=min(1800, 10 * 2 ** min(doc["attempts"], 7))))
                doc["error"] = f"{status}"
            self.store.put(doc)
            if status >= 500:
                break
        return sent

    # ---------------------------------------------------------------- updates from the control tower

    def receive(self, channel: str, body: dict[str, Any]) -> None:
        now = self.clock.now()
        event_id = body.get("event_id") or uuid.uuid4().hex
        if self.store.get(f"evt:{event_id}"):
            return
        self.store.put({"id": f"evt:{event_id}", "doc_type": "inbound_event", "channel": channel,
                        "type": body.get("type"), "received_at": _iso(now), "body": body})
        p, kind = body.get("payload") or {}, body.get("type")
        if self.returns.receive(kind, p, now):
            return
        doc = self.store.get(f"order:{p.get('order_id')}") if p.get("order_id") else None
        if not doc:
            return
        doc["updates"] = (doc.get("updates") or [])[-20:] + [{"type": kind, "at": _iso(now)}]
        if kind == "order_rejected":
            doc["status"] = "rejected"
            log.warning("order %s turned away: %s", p.get("order_id"), "; ".join(p.get("reasons") or []))
            self._release(doc)
        elif kind == "store_order_allocated":
            doc["allocated"] = {ln["line_id"]: ln["cases_allocated"] for ln in p.get("lines") or []}
            doc["status"] = "allocated"
            self._release(doc, keep=doc["allocated"])
        elif kind == "advance_ship_notice":
            doc["ship_notice"] = {k: p.get(k) for k in ("asnId", "carrier", "proNumber", "appointment", "pallets", "lines")}
            doc["status"] = "in_transit"
        elif kind == "tracking_update":
            status = (p.get("status") or "").lower()
            was, doc["status"] = doc["status"], f"tracking:{status}"
            if doc["channel"] == "web" and status == "delivered" and was != doc["status"]:
                self.returns.on_delivered(doc, now)
            if doc["channel"] == "store" and status == "delivered":
                self._receive_at_store(doc)
                doc["status"] = "received"
        self.store.put(doc)

    def _receive_at_store(self, doc: dict[str, Any]) -> None:
        """Cases on the ship notice go onto the shelves; whatever was ordered and not shipped is off order."""
        st = self.store.get(f"store:{doc['store_id']}")
        shipped = {ln["sellerSku"]: ln["unitsShipped"] for ln in (doc.get("ship_notice") or {}).get("lines") or []}
        for ln in doc["payload"]["lines"]:
            upc, ordered = ln["upc"], ln["cases"] * ln["case_pack"]
            got = shipped.get(upc, 0)
            st["stock"][upc] = st["stock"].get(upc, 0) + got
            st["on_order"][upc] = max(0, st["on_order"].get(upc, 0) - ordered)
        self.store.put(st)

    def _release(self, doc: dict[str, Any], keep: dict[str, int] | None = None) -> None:
        """Take cases that will not come off the store's order book (rejected, or cut by allocation)."""
        if doc.get("channel") != "store":
            return
        st = self.store.get(f"store:{doc['store_id']}")
        for ln in doc["payload"]["lines"]:
            dropped = ln["cases"] - (keep or {}).get(ln["line_id"], 0)
            if dropped > 0:
                st["on_order"][ln["upc"]] = max(0, st["on_order"].get(ln["upc"], 0) - dropped * ln["case_pack"])
                if keep is not None:
                    ln["cases"] -= dropped        # the store now expects only what was allocated
        self.store.put(st)

    # ---------------------------------------------------------------- files

    # ---------------------------------------------------------------- planning files

    def _planning_feeds(self, now: datetime, live: datetime) -> dict[str, Any]:
        """Like Corp B: cold-start baselines at go-live, the first forecast an hour later, then every Monday
        a forecast and last week's actual demand on Corp A's network (web orders plus store orders, in units)."""
        meta = self.store.get("meta:feeds") or {"id": "meta:feeds", "doc_type": "meta"}
        if not meta.get("initial"):
            self._drop("cold_start", now, [
                {"upc": it.upc, "region": r, "weekly_units": round(it.weekly_units[r] * self.rng.uniform(0.7, 1.3), 3)}
                for it in self.items for r in catalog.REGIONS])
            meta["initial"] = _iso(now)
        monday = week_of(now.date()).isoformat()
        if not meta.get("weekly") and now >= live + timedelta(hours=1):
            self._drop("forecast", now, self._forecast(now.date()))
            meta["weekly"] = monday
        elif meta.get("weekly") and now.weekday() == 0 and now.hour >= 2 and meta["weekly"] != monday:
            self._drop("forecast", now, self._forecast(now.date()))
            self._drop("sales", now, self._sales(week_of(now.date()) - timedelta(days=7)))
            meta["weekly"] = monday
        self.store.put(meta)
        return meta

    def _forecast(self, today: date) -> list[dict[str, Any]]:
        start = week_of(today)
        return [{"upc": it.upc, "region": r, "week_start": (start + timedelta(weeks=w)).isoformat(),
                 "units": round(it.weekly_units[r] * self.rng.uniform(0.85, 1.15), 3)}
                for it in self.items for r in catalog.REGIONS for w in range(8)]

    def _sales(self, week_start: date) -> list[dict[str, Any]]:
        end = week_start + timedelta(days=7)
        stores = {sid: state for sid, _n, _c, state, *_ in STORES}
        units: dict[tuple[str, str], int] = {}
        for o in self.store.query("order"):
            if not week_start <= datetime.fromisoformat(o["placed_at"]).date() < end:
                continue
            if o.get("channel") == "store":
                region = geo.region_of(stores.get(o.get("store_id"), "WA"))
                lines = [(ln["upc"], ln["cases"] * ln["case_pack"]) for ln in o["payload"]["lines"]]
            else:
                region = geo.region_of(o["payload"]["consumer"]["address"]["state"])
                lines = [(ln["upc"], ln["quantity"]) for ln in o["payload"]["lines"]]
            for upc, qty in lines:
                if upc in self.by_upc:
                    units[(upc, region)] = units.get((upc, region), 0) + qty
        return [{"upc": u, "region": r, "week_start": week_start.isoformat(), "units": float(n)}
                for (u, r), n in sorted(units.items())]

    def _drop(self, kind: str, now: datetime, records: list[dict[str, Any]]) -> str:
        path = f"corpc/{kind}/{now:%Y-%m-%d}-{uuid.uuid4().hex[:6]}.json"
        self.upload(path, json.dumps({"feed_type": kind, "generated_at": _iso(now), "records": records}).encode())
        log.info("dropped %s (%d records)", path, len(records))
        return path
