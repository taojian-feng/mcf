"""Corp A: the fulfillment network behind the MCF API.

Orders move through Corp A's own statuses on realistic timings (minutes to accept, hours to pick and
pack, carrier transit days by distance), inventory is reserved, picked, replenished, transferred and
counted, and partner cases (carrier traces, cancellations, transfers, removals) are worked by an ops
team that is usually, but not always, on time. Everything Corp A wants the seller to know goes out
as a signed webhook through an outbox, retried until the seller's endpoint accepts it.

All state lives in Corp A's own store; `tick()` advances whatever is due.
"""
from __future__ import annotations

import json
import logging
import math
import random
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sim import catalog, geo
from sim.common.store import SimStore
from sim.common.web import Sender, sign, urllib_sender

log = logging.getLogger("sim.corpa")

FC_NODES = ("FC-WEST", "FC-EAST")
ACTIVE_STEPS = ("accept", "process", "pack", "ship")  # before the parcel leaves the building
CUTOFF_HOUR_UTC = 23

# Share of accepted orders that meet each carrier fate. The rest are delivered as planned.
FATES = (("delay", 0.04), ("attempt", 0.015), ("lost", 0.004), ("undeliverable", 0.003))


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _err(code: str, message: str) -> dict[str, Any]:
    return {"errors": [{"code": code, "message": message}]}


class CorpA:
    def __init__(self, store: SimStore, clock, *, webhook_url: str, webhook_secret: str,
                 send: Sender = urllib_sender, rng: random.Random | None = None, speed: float = 1.0,
                 shrink_rate: float = 0.0005, catalog_seed: int = 2026, catalog_size: int = catalog.PILOT_SIZE):
        self.store, self.clock = store, clock
        self.webhook_url, self.webhook_secret, self.send = webhook_url, webhook_secret, send
        self.rng = rng or random.Random()
        self.speed = speed
        self.shrink_rate = shrink_rate
        self.catalog = catalog.by_upc(catalog_seed, catalog_size)  # what the seller could list
        self.items: dict[str, catalog.Item] = {}                    # what the seller has listed with Corp A
        self.lock = threading.RLock()
        self._load_listings()

    def _load_listings(self) -> None:
        doc = self.store.get("meta:listings")
        self.items = {u: self.catalog[u] for u in (doc or {}).get("upcs", []) if u in self.catalog}

    # ================================================================ API operations

    def create_order(self, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        problems = self._validate_order(req)
        if problems:
            return 400, {"errors": [{"code": "InvalidInput", "message": p} for p in problems]}
        sfo = req["sellerFulfillmentOrderId"]
        with self.lock:
            existing = self.store.get(f"order:{sfo}")
            if existing:
                if existing["request"]["items"] != req["items"]:
                    return 409, _err("Conflict", f"order {sfo} already exists with different items")
                return 200, {"sellerFulfillmentOrderId": sfo, "status": existing["status"]}
            now = self.clock.now()
            addr = req["destinationAddress"]
            self.store.put({
                "id": f"order:{sfo}", "doc_type": "order", "sfo_id": sfo, "request": req, "status": "RECEIVED",
                "received_at": _iso(now), "status_at": _iso(now), "step": "accept",
                "next_at": _iso(now + self._wait(minutes=(1, 8))),
                "region": geo.region_of(addr["stateOrRegion"]),
                "dest": geo.zip_location(addr["postalCode"], addr["stateOrRegion"]),
                "speed": req.get("shippingSpeedCategory", "Standard"), "lines": [], "packages": [],
                "eta": None, "fate": "normal", "history": [{"status": "RECEIVED", "at": _iso(now)}],
            })
        return 202, {"sellerFulfillmentOrderId": sfo, "status": "RECEIVED"}

    def _validate_order(self, req: dict[str, Any]) -> list[str]:
        out = []
        for f in ("sellerFulfillmentOrderId", "destinationAddress", "items", "shippingSpeedCategory"):
            if not req.get(f):
                out.append(f"{f} is required")
        if out:
            return out
        a = req["destinationAddress"]
        for f in ("addressLine1", "city", "stateOrRegion", "postalCode", "countryCode"):
            if not a.get(f):
                out.append(f"destinationAddress.{f} is required")
        if a.get("countryCode") not in (None, "US"):
            out.append("only US destinations are served")
        if a.get("postalCode") and not (len(a["postalCode"]) == 5 and a["postalCode"].isdigit()):
            out.append("destinationAddress.postalCode must be 5 digits")
        if req["shippingSpeedCategory"] not in ("Standard", "Expedited", "Priority"):
            out.append("shippingSpeedCategory must be Standard, Expedited or Priority")
        for it in req["items"]:
            if it.get("sellerSku") not in self.items:
                out.append(f"sellerSku {it.get('sellerSku')} is not an active listing")
            if not isinstance(it.get("quantity"), int) or it["quantity"] < 1:
                out.append(f"quantity for {it.get('sellerSku')} must be a positive integer")
        return out

    def get_order(self, sfo: str) -> tuple[int, dict[str, Any]]:
        o = self.store.get(f"order:{sfo}")
        if not o:
            return 404, _err("NotFound", f"order {sfo} not found")
        return 200, {
            "fulfillmentOrder": {"sellerFulfillmentOrderId": sfo, "status": o["status"],
                                 "statusUpdatedDate": o["status_at"], "receivedDate": o["received_at"],
                                 "shippingSpeedCategory": o["speed"]},
            "fulfillmentShipments": [{k: p[k] for k in ("packageNumber", "carrier", "trackingNumber", "fromNode",
                                                        "shippedAt", "items")} for p in o["packages"]],
            "estimatedArrivalDate": o["eta"],
        }

    def order_status(self, sfo: str) -> tuple[int, dict[str, Any]]:
        o = self.store.get(f"order:{sfo}")
        if not o:
            return 404, _err("NotFound", f"order {sfo} not found")
        return 200, {"sellerFulfillmentOrderId": sfo, "status": o["status"], "history": o["history"]}

    def preview(self, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        addr = req.get("address") or req.get("destinationAddress") or {}
        if not addr.get("stateOrRegion"):
            return 400, _err("InvalidInput", "address.stateOrRegion is required")
        region = geo.region_of(addr["stateOrRegion"])
        dest = geo.zip_location(addr.get("postalCode", ""), addr["stateOrRegion"])
        now = self.clock.now()
        fulfillable = all(self._available(it["sellerSku"], geo.REGION_FC[region]) >= it["quantity"]
                          or self._available(it["sellerSku"], _other_fc(geo.REGION_FC[region])) >= it["quantity"]
                          for it in req.get("items", []) if it.get("sellerSku") in self.items)
        out = []
        for speed, promise in (("Standard", 3), ("Expedited", 2), ("Priority", 1)):
            ship = _ship_date(now)
            days, mode = _carrier_plan(geo.miles(geo.NODES[geo.REGION_FC[region]], dest), promise - (ship - now.date()).days)
            out.append({"shippingSpeedCategory": speed, "isFulfillable": fulfillable,
                        "estimatedArrivalDate": (ship + timedelta(days=days)).isoformat(), "mode": mode,
                        "estimatedFees": round(3.25 + (4.5 if mode == "air" else 0.8), 2)})
        return 200, {"previews": out}

    def tracking(self, package_number: str) -> tuple[int, dict[str, Any]]:
        pkg = self.store.get(f"pkg:{package_number}")
        if not pkg:
            return 404, _err("NotFound", f"package {package_number} not found")
        o = self.store.get(f"order:{pkg['sfo_id']}")
        p = next(p for p in o["packages"] if p["packageNumber"] == package_number)
        return 200, {"packageNumber": package_number, "carrier": p["carrier"], "trackingNumber": p["trackingNumber"],
                     "currentStatus": p["status"], "estimatedArrivalDate": o["eta"], "events": p["events"],
                     "shipMode": p["mode"], "finalMileMiles": p["finalMileMiles"]}

    def inbound_plan(self, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        src, dst = req.get("shipFrom"), req.get("shipTo")
        if src not in geo.NODES or dst not in geo.NODES or src == dst:
            return 400, _err("InvalidInput", "shipFrom and shipTo must be two different pilot nodes")
        bad = [it.get("sellerSku") for it in req.get("items", []) if it.get("sellerSku") not in self.items]
        if bad or not req.get("items"):
            return 400, _err("InvalidInput", f"unknown or missing items: {bad}")
        ids = [self._new_transfer(it["sellerSku"], src, dst, int(it["quantity"]), ref=req.get("planId"))
               for it in req["items"]]
        return 200, {"planId": req.get("planId"), "shipmentIds": ids}

    def update_inbound(self, shipment_id: str, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self.lock:
            t = self.store.get(f"xfer:{shipment_id}")
            if not t:
                return 404, _err("NotFound", f"shipment {shipment_id} not found")
            if t["status"] != "planned":
                return 409, _err("Conflict", f"shipment {shipment_id} is already {t['status']}")
            if "quantity" in req:
                t["qty"] = max(0, int(req["quantity"]))
            if req.get("cancel"):
                t["status"], t["next_at"] = "cancelled", None
            self.store.put(t)
        return 200, {"shipmentId": shipment_id, "status": t["status"], "quantity": t["qty"]}

    def approve_plan(self, plan_id: str, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        moves = req.get("moves") or []
        bad = [m for m in moves if m.get("from_node") not in geo.NODES or m.get("to_node") not in geo.NODES
               or m.get("upc") not in self.items]
        if bad or not moves:
            return 400, _err("InvalidInput", f"invalid moves: {bad or 'none given'}")
        ids = [self._new_transfer(m["upc"], m["from_node"], m["to_node"], int(m["qty"]), ref=plan_id) for m in moves]
        return 200, {"planId": plan_id, "status": "ACCEPTED", "shipmentIds": ids}

    def feed_document(self, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Seller feeds. LISTINGS activates items for fulfillment; vendors then ship them in (next tick)."""
        doc_id = f"feed-{uuid.uuid4().hex[:12]}"
        accepted, rejected = [], []
        if req.get("contentType") == "LISTINGS":
            for it in req.get("items") or []:
                (accepted if it.get("sellerSku") in self.catalog else rejected).append(it.get("sellerSku"))
            with self.lock:
                doc = self.store.get("meta:listings") or {"id": "meta:listings", "doc_type": "meta", "upcs": []}
                doc["upcs"] = sorted(set(doc["upcs"]) | set(accepted))
                self.store.put(doc)
                self._load_listings()
        self.store.put({"id": f"feed:{doc_id}", "doc_type": "feed", "content_type": req.get("contentType"),
                        "accepted": len(accepted), "rejected": rejected[:50], "created_at": _iso(self.clock.now())})
        return 200, {"feedDocumentId": doc_id, "accepted": len(accepted), "rejected": rejected[:50]}

    def create_case(self, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        case_id, action = req.get("caseId"), req.get("action")
        if not case_id or not action:
            return 400, _err("InvalidInput", "caseId and action are required")
        with self.lock:
            if self.store.get(f"case:{case_id}"):
                return 200, {"caseId": case_id, "status": "DUPLICATE"}
            now = self.clock.now()
            self.store.put({"id": f"case:{case_id}", "doc_type": "case", "case_id": case_id, "action": action,
                            "context": req.get("context") or {}, "due_at": req.get("dueAt"), "status": "OPEN",
                            "step": "ack", "next_at": _iso(now + self._wait(minutes=(5, 45))),
                            "created_at": _iso(now)})
        return 202, {"caseId": case_id, "status": "OPEN"}

    # ================================================================ the engine

    def tick(self, limit: int = 500) -> None:
        with self.lock:
            now = self.clock.now()
            self._load_listings()
            self._seed_once(now)
            self._daily(now)
            due = _iso(now)
            for o in self.store.query("order", due_by=due)[:limit]:
                self._advance_order(o, now)
            for t in self.store.query("transfer", due_by=due)[:limit]:
                self._advance_transfer(t, now)
            for c in self.store.query("case", due_by=due)[:limit]:
                self._advance_case(c, now)
        self.flush_outbox()

    # ---------------------------------------------------------------- orders

    def _advance_order(self, o: dict[str, Any], now: datetime) -> None:
        step = o["step"]
        if step == "accept":
            if not self._allocate(o):
                self._set_status(o, "UNFULFILLABLE", now, reason="insufficient inventory in the pilot network")
                o["step"], o["next_at"] = None, None
                self.store.put(o)
                return
            ship = _ship_date(now)
            promise = 2 if o["speed"] in ("Expedited", "Priority") else 3
            placed = _dt(o["request"].get("displayableOrderDate") or o["received_at"]).date()
            budget = max(1, promise - (ship - placed).days)
            dist = max(geo.miles(geo.NODES[ln["node"]], tuple(o["dest"])) for ln in o["lines"])
            days, mode = _carrier_plan(dist, budget)
            o["plan"] = {"ship_date": ship.isoformat(), "transit_days": days, "mode": mode, "miles": round(dist, 1)}
            o["eta"] = (ship + timedelta(days=days)).isoformat()
            o["fate"] = o["assigned_fate"] = self._fate()  # assigned_fate: kept for evaluations
            self._set_status(o, "ACCEPTED", now)
            ship_at = datetime.combine(ship, time(self.rng.randint(17, 22), self.rng.randint(0, 59)), timezone.utc)
            o["ship_at"] = _iso(max(ship_at, now + self._wait(hours=(2, 4))))
            o["step"], o["next_at"] = "process", _iso(now + self._wait(minutes=(5, 45)))
        elif step == "process":
            self._set_status(o, "PROCESSING", now)
            pack_at = _dt(o["ship_at"]) - self._wait(hours=(1, 2))
            o["step"], o["next_at"] = "pack", _iso(max(pack_at, now + timedelta(minutes=10)))
        elif step == "pack":
            self._pick(o, now)
            self._set_status(o, "PACKED", now)
            o["step"], o["next_at"] = "ship", _iso(max(_dt(o["ship_at"]), now + timedelta(minutes=5)))
        elif step == "ship":
            self._ship(o, now)
        elif step == "out_for_delivery":
            self._set_status(o, "OUT_FOR_DELIVERY", now)
            self._track(o, "OUT_FOR_DELIVERY", now, "Out for delivery")
            o["step"] = "attempt_failed" if o["fate"] in ("attempt", "undeliverable") else "deliver"
            o["next_at"] = _iso(now + self._wait(hours=(3, 7)))
        elif step == "attempt_failed":
            if o["fate"] == "undeliverable":
                self._track(o, "UNDELIVERABLE", now, "Address could not be located; returning to sender")
                o["eta"] = None
                o["step"], o["next_at"] = "return_to_sender", _iso(now + self._wait(days=(2, 4)))
            else:
                self._track(o, "DELIVERY_ATTEMPTED", now, "Delivery attempted, no safe location; will retry")
                o["eta"] = (now.date() + timedelta(days=1)).isoformat()
                o["fate"] = "normal"
                o["step"], o["next_at"] = "deliver", _iso(now + self._wait(hours=(18, 26)))
                self._status_note(o, now)
        elif step == "deliver":
            self._set_status(o, "DELIVERED", now)
            self._track(o, "DELIVERED", now, "Delivered, front door")
            o["step"], o["next_at"] = None, None
        elif step == "declare_lost":
            self._track(o, "LOST", now, "Carrier has no scan for 48 hours; package presumed lost")
            o["eta"] = None
            o["step"], o["next_at"] = None, None
            self._status_note(o, now)
        elif step == "return_to_sender":
            self._set_status(o, "CANCELLED", now, reason="returned to sender: undeliverable address")
            for ln in o["lines"]:
                self._adjust(ln["sku"], ln["node"], ln["qty"])
                self._inventory_events([self._inv_event("receipt", ln["sku"], ln["node"], ln["qty"], now)])
            o["step"], o["next_at"] = None, None
        self.store.put(o)

    def _fate(self) -> str:
        r = self.rng.random()
        for fate, p in FATES:
            if r < p:
                return fate
            r -= p
        return "normal"

    def _allocate(self, o: dict[str, Any]) -> bool:
        home = geo.REGION_FC[o["region"]]
        lines = []
        for it in o["request"]["items"]:
            sku, qty = it["sellerSku"], it["quantity"]
            node = next((n for n in (home, _other_fc(home)) if self._available(sku, n) >= qty), None)
            if node is None:
                for ln in lines:
                    self._reserve(ln["sku"], ln["node"], -ln["qty"])
                return False
            self._reserve(sku, node, qty)
            lines.append({"line_id": it.get("sellerFulfillmentOrderItemId"), "sku": sku, "qty": qty, "node": node})
        o["lines"] = lines
        return True

    def _pick(self, o: dict[str, Any], now: datetime) -> None:
        events = []
        for ln in o["lines"]:
            self._reserve(ln["sku"], ln["node"], -ln["qty"])
            self._adjust(ln["sku"], ln["node"], -ln["qty"])
            events.append(self._inv_event("pick", ln["sku"], ln["node"], ln["qty"], now))
        self._inventory_events(events)

    def _ship(self, o: dict[str, Any], now: datetime) -> None:
        plan = o["plan"]
        by_node: dict[str, list[dict[str, Any]]] = {}
        for ln in o["lines"]:
            by_node.setdefault(ln["node"], []).append(ln)
        carrier = "UPS" if plan["mode"] == "air" else self.rng.choice(["UPS", "FedEx", "USPS", "OnTrac"])
        for node, lines in by_node.items():
            pkg_no = f"PKG{uuid.uuid4().hex[:10].upper()}"
            units = sum(ln["qty"] for ln in lines)
            weight = sum(self.catalog[ln["sku"]].weight_lb * ln["qty"] for ln in lines)
            final_mile = round(self.rng.uniform(2.5, 28.0), 1)
            cost = round(3.25 + 0.45 * units + 0.32 * weight
                         + (0.006 if plan["mode"] == "air" else 0.0018) * plan["miles"], 2)
            self.store.put({"id": f"pkg:{pkg_no}", "doc_type": "package", "sfo_id": o["sfo_id"]})
            o["packages"].append({
                "packageNumber": pkg_no, "carrier": carrier, "trackingNumber": f"1Z{uuid.uuid4().hex[:16].upper()}",
                "fromNode": node, "shippedAt": _iso(now), "mode": plan["mode"], "finalMileMiles": final_mile,
                "items": [{"sellerSku": ln["sku"], "quantity": ln["qty"]} for ln in lines], "status": "SHIPPED",
                "events": [{"eventCode": "SHIPPED", "eventTime": _iso(now), "description": f"Departed {node}"}],
            })
            self._notify("SHIPMENT_FACT", {
                "package_id": pkg_no, "order_id": o["sfo_id"], "shipped_on": now.date().isoformat(), "units": units,
                "fulfillment_cost": cost, "mode": "ground" if plan["mode"] == "ground" else "air",
                "final_mile_miles": final_mile, "from_node": node, "carrier": carrier,
            })
        if o["fate"] == "delay":
            late = self.rng.choice([1, 1, 2, 3])
            o["eta"] = (date.fromisoformat(o["eta"]) + timedelta(days=late)).isoformat()
        self._set_status(o, "SHIPPED", now)
        for p in o["packages"]:
            self._send_tracking(o, p, "SHIPPED", now)
        if o["fate"] == "lost":
            o["step"], o["next_at"] = "declare_lost", _iso(now + self._wait(hours=(44, 52)))
        else:
            ofd = datetime.combine(date.fromisoformat(o["eta"]), time(self.rng.randint(12, 15), self.rng.randint(0, 59)),
                                   timezone.utc)
            o["step"], o["next_at"] = "out_for_delivery", _iso(max(ofd, now + timedelta(hours=6)))

    def _set_status(self, o: dict[str, Any], status: str, now: datetime, *, reason: str | None = None) -> None:
        o["status"], o["status_at"] = status, _iso(now)
        o["history"].append({"status": status, "at": _iso(now), **({"reason": reason} if reason else {})})
        self._notify("ORDER_STATUS_CHANGE", {"sellerFulfillmentOrderId": o["sfo_id"], "status": status,
                                             "statusTime": _iso(now), "estimatedArrivalDate": o["eta"],
                                             **({"reason": reason} if reason else {})})

    def _status_note(self, o: dict[str, Any], now: datetime) -> None:
        """Same status, new estimate (sent when the arrival estimate changes)."""
        self._notify("ORDER_STATUS_CHANGE", {"sellerFulfillmentOrderId": o["sfo_id"], "status": o["status"],
                                             "statusTime": _iso(now), "estimatedArrivalDate": o["eta"]})

    def _track(self, o: dict[str, Any], code: str, now: datetime, description: str) -> None:
        for p in o["packages"]:
            p["status"] = code
            p["events"].append({"eventCode": code, "eventTime": _iso(now), "description": description})
            self._send_tracking(o, p, code, now)

    def _send_tracking(self, o: dict[str, Any], p: dict[str, Any], code: str, now: datetime) -> None:
        self._notify("TRACKING_UPDATE", {
            "sellerFulfillmentOrderId": o["sfo_id"], "packageNumber": p["packageNumber"], "carrier": p["carrier"],
            "trackingNumber": p["trackingNumber"], "status": code, "eventTime": _iso(now),
            "estimatedArrivalDate": o["eta"], "events": p["events"][-3:],
        })

    # ---------------------------------------------------------------- inventory

    def _inv(self, sku: str, node: str) -> dict[str, Any]:
        return self.store.get(f"inv:{sku}|{node}") or {"id": f"inv:{sku}|{node}", "doc_type": "inventory",
                                                       "sku": sku, "node": node, "on_hand": 0, "reserved": 0}

    def _available(self, sku: str, node: str) -> int:
        d = self._inv(sku, node)
        return d["on_hand"] - d["reserved"]

    def _reserve(self, sku: str, node: str, qty: int) -> None:
        d = self._inv(sku, node)
        d["reserved"] = max(0, d["reserved"] + qty)
        self.store.put(d)

    def _adjust(self, sku: str, node: str, qty: int) -> None:
        d = self._inv(sku, node)
        d["on_hand"] += qty
        self.store.put(d)

    def _inv_event(self, kind: str, sku: str, node: str, qty: int, at: datetime, **extra: Any) -> dict[str, Any]:
        return {"event_id": f"ca-{uuid.uuid4().hex[:20]}", "type": kind, "sku": sku, "node_id": node, "quantity": qty,
                "occurred_at": _iso(at), "source_party": "corp_a", **extra}

    def _inventory_events(self, events: list[dict[str, Any]], *, not_before: datetime | None = None) -> None:
        for i in range(0, len(events), 250):
            self._notify("INVENTORY_EVENTS", {"events": events[i:i + 250]}, not_before=not_before)

    def _seed_once(self, now: datetime) -> None:
        """Opening stock for newly listed items: about six weeks at each FC, ten at each ISC."""
        seeded = self.store.get("meta:seeded") or {"id": "meta:seeded", "doc_type": "meta", "upcs": []}
        new = [self.items[u] for u in sorted(set(self.items) - set(seeded["upcs"]))]
        if not new:
            return
        events = []
        for it in new:
            for region in catalog.REGIONS:
                weekly = it.weekly_units[region]
                for node, weeks, floor in ((geo.REGION_FC[region], 6, 2), (geo.REGION_ISC[region], 10, 4)):
                    qty = math.ceil(weekly * weeks) + floor
                    self._adjust(it.upc, node, qty)
                    events.append(self._inv_event("receipt", it.upc, node, qty, now))
        self._inventory_events(events)
        seeded["upcs"] = sorted(set(seeded["upcs"]) | {it.upc for it in new})
        seeded["at"] = _iso(now)
        self.store.put(seeded)
        log.info("seeded %d inventory positions for %d newly listed items", len(events), len(new))

    def _daily(self, now: datetime) -> None:
        meta = self.store.get("meta:daily") or {"id": "meta:daily", "doc_type": "meta"}
        today = now.date().isoformat()
        if now.hour >= 5 and meta.get("snapshot") != today:
            self._snapshot(now)
            meta["snapshot"] = today
        if now.weekday() == 0 and now.hour >= 10 and meta.get("vendor_receipts") != today:
            self._vendor_receipts(now)
            meta["vendor_receipts"] = today
        self.store.put(meta)

    def _snapshot(self, now: datetime) -> None:
        """Nightly physical count of every position. Now and then a count finds shrink; Corp A books it
        as an adjustment later that morning, after the seller's reconciliation has seen the gap."""
        counts, adjustments = [], []
        for d in self.store.query("inventory"):
            qty = d["on_hand"]
            if qty > 0 and self.rng.random() < self.shrink_rate:
                lost = min(self.rng.randint(3, 8), qty - d["reserved"])
                if lost > 0:
                    qty -= lost
                    d["on_hand"] = qty
                    self.store.put(d)
                    adjustments.append(self._inv_event("adjustment", d["sku"], d["node"], -lost, now + timedelta(hours=6)))
            counts.append(self._inv_event("physical_count", d["sku"], d["node"], qty, now))
        self._inventory_events(counts)
        if adjustments:
            self._inventory_events(adjustments, not_before=now + timedelta(hours=6))
        log.info("snapshot: %d counts, %d shrink adjustments", len(counts), len(adjustments))

    def _vendor_receipts(self, now: datetime) -> None:
        """Corp B's vendors deliver to the inbound storage centres weekly."""
        events = []
        for it in self.items.values():
            for region in catalog.REGIONS:
                node, weekly = geo.REGION_ISC[region], it.weekly_units[region]
                have = self._inv(it.upc, node)["on_hand"]
                if have < weekly * 6:
                    qty = math.ceil(weekly * 10) - have + 2
                    self._adjust(it.upc, node, qty)
                    events.append(self._inv_event("receipt", it.upc, node, qty, now))
        self._inventory_events(events)

    # ---------------------------------------------------------------- transfers

    def _new_transfer(self, sku: str, src: str, dst: str, qty: int, *, ref: str | None) -> str:
        tid = f"SH{uuid.uuid4().hex[:10].upper()}"
        now = self.clock.now()
        self.store.put({"id": f"xfer:{tid}", "doc_type": "transfer", "transfer_id": tid, "sku": sku, "from": src,
                        "to": dst, "qty": qty, "ref": ref, "status": "planned", "created_at": _iso(now),
                        "next_at": _iso(now + self._wait(hours=(2, 8)))})
        return tid

    def _advance_transfer(self, t: dict[str, Any], now: datetime) -> None:
        if t["status"] == "planned":
            qty = min(t["qty"], max(0, self._available(t["sku"], t["from"])))
            if qty <= 0:
                t["status"], t["next_at"] = "cancelled", None
                t["reason"] = f"no available stock at {t['from']}"
            else:
                t["qty"] = qty
                self._adjust(t["sku"], t["from"], -qty)
                self._inventory_events([self._inv_event("transfer", t["sku"], t["from"], qty, now, to_node=t["to"],
                                                        transfer_id=t["transfer_id"])])
                t["status"], t["shipped_at"] = "in_transit", _iso(now)
                t["next_at"] = _iso(now + self._wait(days=(geo.node_transfer_days(t["from"], t["to"]),) * 2)
                                    + self._wait(hours=(0, 6)))
        elif t["status"] == "in_transit":
            self._adjust(t["sku"], t["to"], t["qty"])
            events = [self._inv_event("receipt", t["sku"], t["to"], t["qty"], now, transfer_id=t["transfer_id"])]
            if t["to"] in FC_NODES:
                events.append(self._inv_event("decant", t["sku"], t["to"], t["qty"], now + timedelta(minutes=30)))
            self._inventory_events(events)
            t["status"], t["received_at"], t["next_at"] = "received", _iso(now), None
        self.store.put(t)

    # ---------------------------------------------------------------- partner cases (the seller's handoffs)

    def _advance_case(self, c: dict[str, Any], now: datetime) -> None:
        if c["step"] == "ack":
            c["status"] = "ACKNOWLEDGED"
            self._notify("HANDOFF_UPDATE", {"caseId": c["case_id"], "status": "ACKNOWLEDGED", "by": "corp_a_ops",
                                            "at": _iso(now)})
            late = self.rng.random() < 0.06
            c["step"], c["next_at"] = "complete", _iso(now + self._wait(hours=(26, 40) if late else (1, 18)))
        elif c["step"] == "complete":
            result = self._work_case(c, now)
            c["status"], c["result"], c["step"], c["next_at"] = "COMPLETED", result, None, None
            self._notify("HANDOFF_UPDATE", {"caseId": c["case_id"], "status": "COMPLETED", "by": "corp_a_ops",
                                            "at": _iso(now), "result": result})
        self.store.put(c)

    def _work_case(self, c: dict[str, Any], now: datetime) -> dict[str, Any]:
        ctx, action = c["context"], c["action"]
        order = self.store.get(f"order:{ctx['order_id']}") if ctx.get("order_id") else None
        if action == "carrier_trace":
            if not order:
                return {"finding": "order_not_found"}
            finding = {"delay": "delayed_in_carrier_network", "lost": "lost_in_transit",
                       "undeliverable": "address_undeliverable"}.get(order["fate"], "in_transit_on_schedule")
            if order["status"] == "DELIVERED":
                finding = "delivered"
            return {"finding": finding, "estimatedArrivalDate": order["eta"], "status": order["status"]}
        if action == "cancel_fulfillment_order":
            if not order:
                return {"cancelled": False, "reason": "order_not_found"}
            if order["step"] in ("accept", "process"):
                for ln in order["lines"]:
                    self._reserve(ln["sku"], ln["node"], -ln["qty"])
                self._set_status(order, "CANCELLED", now, reason="cancelled at seller request")
                order["step"], order["next_at"] = None, None
                self.store.put(order)
                return {"cancelled": True}
            return {"cancelled": False, "reason": f"order is {order['status']}"}
        if action == "reship_order" and order:
            new_id = f"{order['sfo_id']}-R{sum(1 for h in order['history'] if h.get('reship')) + 1}"
            req = {**order["request"], "sellerFulfillmentOrderId": new_id, "shippingSpeedCategory": "Expedited",
                   "displayableOrderDate": _iso(now)}
            code, _ = self.create_order(req)
            order["history"].append({"reship": new_id, "at": _iso(now)})
            self.store.put(order)
            return {"reshipped": code in (200, 202), "reshipmentOrderId": new_id}
        if action == "return_intercept":
            return {"intercept_requested": True, "carrier_reference": f"RI{uuid.uuid4().hex[:8].upper()}"}
        if action == "execute_transfer" and {"sku", "from", "to", "qty"} <= ctx.keys():
            return {"transfer_id": self._new_transfer(ctx["sku"], ctx["from"], ctx["to"], int(ctx["qty"]),
                                                      ref=ctx.get("rec_id"))}
        if action == "execute_disposition" and {"sku", "node_id", "qty"} <= ctx.keys():
            qty = min(int(ctx["qty"]), max(0, self._available(ctx["sku"], ctx["node_id"])))
            if qty:
                self._adjust(ctx["sku"], ctx["node_id"], -qty)
                self._inventory_events([self._inv_event("removal", ctx["sku"], ctx["node_id"], qty, now)])
            status = "completed" if qty else "failed"
            self._notify("DISPOSITION_STATUS", {"rec_id": ctx.get("rec_id"), "status": status,
                                                "detail": f"{ctx.get('option', 'removal')}: {qty} units removed"})
            return {"removed_units": qty}
        return {"done": True}

    # ---------------------------------------------------------------- webhooks (outbox)

    def _notify(self, kind: str, payload: dict[str, Any], *, not_before: datetime | None = None) -> None:
        now = self.clock.now()
        nid = f"ntf-{now:%Y%m%d%H%M%S%f}-{uuid.uuid4().hex[:12]}"  # sorts in creation order within a due time
        self.store.put({"id": f"out:{nid}", "doc_type": "outbox", "attempts": 0, "created_at": _iso(now),
                        "next_at": _iso(max(now, not_before) if not_before else now),
                        "body": {"notificationId": nid, "notificationType": kind, "eventTime": _iso(now),
                                 "payload": payload}})

    def flush_outbox(self, limit: int = 400) -> int:
        now = self.clock.now()
        sent = 0
        for item in self.store.query("outbox", due_by=_iso(now))[:limit]:
            body = json.dumps(item["body"], default=str).encode()
            status, resp = self.send("POST", self.webhook_url, {
                "Content-Type": "application/json",
                "X-MCF-Signature": sign(self.webhook_secret, int(now.timestamp()), body),
            }, body)
            if 200 <= status < 300:
                self.store.delete(item["id"])
                sent += 1
                continue
            item["attempts"] += 1
            if now - _dt(item["created_at"]) > timedelta(hours=72):
                log.error("dropping webhook %s after 72h: %s %s", item["id"], status, resp[:200])
                self.store.delete(item["id"])
                continue
            item["next_at"] = _iso(now + timedelta(seconds=min(3600, 15 * 2 ** min(item["attempts"], 8))))
            item["last_error"] = f"{status} {resp[:200]!r}"
            self.store.put(item)
            if status >= 500 or status == 599:
                break  # the endpoint is down; stop hammering it this round
        return sent

    # ---------------------------------------------------------------- helpers

    def _wait(self, *, minutes=None, hours=None, days=None) -> timedelta:
        lo, hi = minutes or hours or days
        unit = 60 if minutes else 3600 if hours else 86400
        return timedelta(seconds=self.rng.uniform(lo, hi) * unit / self.speed)


def _other_fc(node: str) -> str:
    return "FC-EAST" if node == "FC-WEST" else "FC-WEST"


def _ship_date(now: datetime) -> date:
    return now.date() if now.hour < CUTOFF_HOUR_UTC - 4 else now.date() + timedelta(days=1)


def _carrier_plan(distance: float, days_budget: int) -> tuple[int, str]:
    """Ground when ground makes the promise, else air (next or second day)."""
    ground = geo.ground_days(distance)
    if ground <= max(1, days_budget):
        return ground, "ground"
    return max(1, min(2, days_budget)), "air"
