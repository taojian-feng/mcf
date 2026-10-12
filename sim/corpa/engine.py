"""Corp A: the fulfillment network behind the MCF API.

Orders move through Corp A's own statuses on realistic timings (minutes to accept, hours to pick and
pack, carrier transit days by distance). Parcel orders go to consumers from the FCs. B2B orders (store
replenishment) go as whole cases on pallets by LTL freight, mostly from the bulk storage centres, ship
what is in stock (short lines are cut, not held), send an advance ship notice and arrive on a dock
appointment inside the store's delivery window. Inventory is reserved, picked, replenished, transferred and
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
from sim.common.freight import CARRIER_FATES, TRUCK_FATES, truck_outcome
from sim.common.store import SimStore
from sim.common.web import Sender, sign, urllib_sender

log = logging.getLogger("sim.corpa")

FC_NODES = ("FC-WEST", "FC-EAST")
ACTIVE_STEPS = ("accept", "process", "pack", "ship")  # before the parcel leaves the building
CUTOFF_HOUR_UTC = 23

# Share of accepted orders that meet each carrier fate. The rest are delivered as planned.
FATES = (("delay", 0.04), ("attempt", 0.015), ("lost", 0.004), ("undeliverable", 0.003))
FREIGHT_FATES = (("delay", 0.06),)            # LTL: a missed dock appointment is the common failure
LTL_CARRIERS = ("Estes", "XPO", "Old Dominion", "Saia")
CASES_PER_PALLET = 30
RETURN_SHIP_BY_DAYS = 14
RETURN_NO_SHOW = 0.12                          # consumers who never send the parcel back
# What the returns bench finds, per unit, by the reason the consumer gave.
RETURN_GRADES = {
    "damaged_in_transit": (("DAMAGED", 0.78), ("SELLABLE", 0.18), ("WRONG_ITEM", 0.01), ("MISSING", 0.03)),
    "defective": (("DEFECTIVE", 0.84), ("SELLABLE", 0.13), ("WRONG_ITEM", 0.01), ("MISSING", 0.02)),
}
RETURN_GRADES_DEFAULT = (("SELLABLE", 0.9), ("DAMAGED", 0.06), ("WRONG_ITEM", 0.025), ("MISSING", 0.015))


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _err(code: str, message: str) -> dict[str, Any]:
    return {"errors": [{"code": code, "message": message}]}


class CorpA:
    def __init__(self, store: SimStore, clock, *, webhook_url: str, webhook_secret: str,
                 send: Sender = urllib_sender, rng: random.Random | None = None, speed: float = 1.0,
                 shrink_rate: float = 0.0005, catalog_seed: int = 2026, catalog_size: int = catalog.PILOT_SIZE,
                 corpc_size: int = catalog.CORPC_SIZE, truck_fates=TRUCK_FATES):
        self.store, self.clock = store, clock
        self.truck_fates = truck_fates
        self.webhook_url, self.webhook_secret, self.send = webhook_url, webhook_secret, send
        self.rng = rng or random.Random()
        self.speed = speed
        self.shrink_rate = shrink_rate
        self.catalog = catalog.every_client(catalog_seed, catalog_size, corpc_size)  # what sellers could list
        self.items: dict[str, catalog.Item] = {}                    # what the seller has listed with Corp A
        self.lock = threading.RLock()
        self._load_listings()

    def _load_listings(self) -> None:
        doc = self.store.get("meta:listings") or {}
        for upc, d in (doc.get("generic") or {}).items():     # items of clients the sim has no catalog for
            if upc not in self.catalog:
                self.catalog[upc] = _generic_item(upc, d)
        self.items = {u: self.catalog[u] for u in doc.get("upcs", []) if u in self.catalog}

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
                **({"b2b": True, "window": req["deliveryWindow"]} if req.get("orderType") == "B2B" else {}),
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
        if req.get("orderType") == "B2B":
            w = req.get("deliveryWindow") or {}
            if not (w.get("start") and w.get("end")):
                out.append("deliveryWindow.start and deliveryWindow.end are required for B2B orders")
            if req["shippingSpeedCategory"] != "Freight":
                out.append("B2B orders ship Freight")
            for it in req["items"]:
                pack, qty = it.get("casePack"), it.get("quantity")
                if not isinstance(pack, int) or pack < 1 or (isinstance(qty, int) and qty % pack):
                    out.append(f"quantity for {it.get('sellerSku')} must be whole cases of casePack")
        elif req["shippingSpeedCategory"] not in ("Standard", "Expedited", "Priority"):
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
            generic = {}
            for it in req.get("items") or []:
                upc = it.get("sellerSku")
                if upc in self.catalog:
                    accepted.append(upc)
                elif isinstance(upc, str) and len(upc) == 12 and upc.isdigit():
                    # A client new to the sim (onboarded through the tower): list it as a generic item.
                    generic[upc] = {"title": str(it.get("title") or f"Item {upc}")[:80],
                                    "unit_cost": _num(it.get("unitCost"), 15.0),
                                    "weight_lb": _num(it.get("weightLb"), 2.0)}
                    accepted.append(upc)
                else:
                    rejected.append(upc)
            with self.lock:
                doc = self.store.get("meta:listings") or {"id": "meta:listings", "doc_type": "meta", "upcs": []}
                doc["upcs"] = sorted(set(doc["upcs"]) | set(accepted))
                if generic:
                    doc["generic"] = {**(doc.get("generic") or {}), **generic}
                self.store.put(doc)
                self._load_listings()
        self.store.put({"id": f"feed:{doc_id}", "doc_type": "feed", "content_type": req.get("contentType"),
                        "accepted": len(accepted), "rejected": rejected[:50], "created_at": _iso(self.clock.now())})
        return 200, {"feedDocumentId": doc_id, "accepted": len(accepted), "rejected": rejected[:50]}

    def create_return(self, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """A return authorization (RMA) for units of a delivered order, with a prepaid label to the region's FC."""
        sfo, rid = req.get("sellerFulfillmentOrderId"), req.get("sellerReturnId")
        items = req.get("items") or []
        if not sfo or not rid or not items:
            return 400, _err("InvalidInput", "sellerFulfillmentOrderId, sellerReturnId and items are required")
        with self.lock:
            existing = self.store.get(f"return:{rid}")
            if existing:
                if existing["request"]["items"] != items:
                    return 409, _err("Conflict", f"return {rid} already exists with different items")
                return 200, existing["response"]
            o = self.store.get(f"order:{sfo}")
            if not o:
                return 404, _err("NotFound", f"order {sfo} not found")
            if o["status"] != "DELIVERED" or o.get("b2b"):
                return 400, _err("InvalidInput", f"order {sfo} is {o['status']}; only delivered parcels are returned")
            shipped = {}
            for ln in o["lines"]:
                shipped[ln["sku"]] = shipped.get(ln["sku"], 0) + ln["qty"]
            for it in items:
                left = shipped.get(it.get("sellerSku"), 0) - o.get("returned", {}).get(it.get("sellerSku"), 0)
                if not isinstance(it.get("quantity"), int) or not 0 < it["quantity"] <= left:
                    return 400, _err("InvalidInput", f"{it.get('sellerSku')}: {left} units left to return")
            now = self.clock.now()
            o["returned"] = {**o.get("returned", {})}
            for it in items:
                o["returned"][it["sellerSku"]] = o["returned"].get(it["sellerSku"], 0) + it["quantity"]
            self.store.put(o)
            node = geo.REGION_FC[o["region"]]
            carrier = self.rng.choice(["UPS", "USPS"])
            rma = f"RMA{uuid.uuid4().hex[:10].upper()}"
            response = {"rmaId": rma, "sellerReturnId": rid, "returnToNode": node,
                        "label": {"carrier": carrier, "trackingNumber": f"RT{uuid.uuid4().hex[:14].upper()}"},
                        "shipBy": (now.date() + timedelta(days=RETURN_SHIP_BY_DAYS)).isoformat()}
            ships = self.rng.random() >= RETURN_NO_SHOW
            self.store.put({
                "id": f"return:{rid}", "doc_type": "return", "rma_id": rma, "return_id": rid, "sfo_id": sfo,
                "request": req, "response": response, "node": node, "status": "AUTHORIZED", "created_at": _iso(now),
                "step": "pickup" if ships else None,
                "next_at": _iso(now + self._wait(days=(0.3, 6))) if ships else None,
                # the consumer's parcel: each unit is what it is before anyone opens the box
                "contents": [{"sellerSku": it["sellerSku"], "grade": self._fate(
                    RETURN_GRADES.get(it.get("returnReasonCode"), RETURN_GRADES_DEFAULT), default="SELLABLE")}
                    for it in items for _ in range(it["quantity"])],
            })
        return 202, response

    def set_return_disposition(self, rma: str, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """What to do with graded units that cannot go back to stock: liquidate, back to the vendor, or hold."""
        r = next(iter(self.store.query("return", rma_id=rma)), None)
        if not r or r["status"] != "RECEIVED":
            return 404, _err("NotFound", f"no received return {rma}")
        allowed = {"liquidate", "return_to_vendor", "hold_for_client"}
        bad = [it for it in req.get("items") or [] if it.get("disposition") not in allowed]
        if bad or not req.get("items"):
            return 400, _err("InvalidInput", f"disposition must be one of {sorted(allowed)}")
        r["dispositions"] = req["items"]
        self.store.put(r)
        return 200, {"rmaId": rma, "status": "ACCEPTED", "items": len(req["items"])}

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
            for r in self.store.query("return", due_by=due)[:limit]:
                self._advance_return(r, now)
        self.flush_outbox()

    # ---------------------------------------------------------------- orders

    def _advance_order(self, o: dict[str, Any], now: datetime) -> None:
        step = o["step"]
        if o.get("b2b") and step in ("accept", "ship"):
            (self._accept_b2b if step == "accept" else self._ship_b2b)(o, now)
        elif step == "accept":
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
            self._track(o, "DELIVERED", now, ((o.get("truck") or {}).get("pod_note")
                                              or "Delivered to store receiving, POD signed clean") if o.get("b2b")
                        else "Delivered, front door")
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

    # ---------------------------------------------------------------- B2B (store replenishment by freight)

    def _accept_b2b(self, o: dict[str, Any], now: datetime) -> None:
        """Ship what is in stock in whole cases, from the region's bulk centre first; cut the rest."""
        isc, fc = geo.REGION_ISC[o["region"]], geo.REGION_FC[o["region"]]
        lines, cut = [], []
        for it in o["request"]["items"]:
            sku, left, pack = it["sellerSku"], it["quantity"], it["casePack"]
            for node in (isc, fc, _other_fc(fc)):
                take = min(left, max(0, self._available(sku, node))) // pack * pack
                if take:
                    self._reserve(sku, node, take)
                    lines.append({"line_id": it.get("sellerFulfillmentOrderItemId"), "sku": sku, "qty": take,
                                  "node": node, "case_pack": pack})
                    left -= take
                if not left:
                    break
            if left:
                cut.append({"sellerSku": sku, "unitsCut": left})
        if not lines:
            self._set_status(o, "UNFULFILLABLE", now, reason="no stock for any line")
            o["step"], o["next_at"] = None, None
            return
        o["lines"], o["cut"] = lines, cut
        ship = _ship_date(now) + timedelta(days=1)                       # freight is booked a day ahead
        dist = max(geo.miles(geo.NODES[ln["node"]], tuple(o["dest"])) for ln in lines)
        start = date.fromisoformat(o["window"]["start"])
        arrive = max(start, ship + timedelta(days=geo.ground_days(dist) + 1))  # LTL runs a day slower than parcel
        o["fate"] = o["assigned_fate"] = self._fate(FREIGHT_FATES)
        o["plan"] = {"ship_date": ship.isoformat(), "mode": "ltl", "miles": round(dist, 1),
                     "appointment": arrive.isoformat()}
        o["eta"] = arrive.isoformat()
        self._set_status(o, "ACCEPTED", now, reason=f"{len(cut)} line(s) cut for stock" if cut else None)
        o["ship_at"] = _iso(datetime.combine(ship, time(self.rng.randint(14, 20)), timezone.utc))
        o["step"], o["next_at"] = "process", _iso(now + self._wait(minutes=(20, 90)))

    def _ship_b2b(self, o: dict[str, Any], now: datetime) -> None:
        """One truckload: pallets of whole cases, an LTL PRO number, a dock appointment, and the ship notice."""
        plan = o["plan"]
        cases = sum(ln["qty"] // ln["case_pack"] for ln in o["lines"])
        pallets = max(1, math.ceil(cases / CASES_PER_PALLET))
        carrier, pro = self.rng.choice(LTL_CARRIERS), f"{self.rng.randint(10**9, 10**10 - 1)}"
        bol = f"BOL{uuid.uuid4().hex[:10].upper()}"
        appt = datetime.combine(date.fromisoformat(plan["appointment"]), time(self.rng.choice([7, 8, 9, 10, 13])),
                                timezone.utc)
        weight = sum(self.catalog[ln["sku"]].weight_lb * ln["qty"] for ln in o["lines"]) + 45 * pallets
        o["packages"].append({
            "packageNumber": bol, "carrier": carrier, "trackingNumber": pro, "fromNode": o["lines"][0]["node"],
            "shippedAt": _iso(now), "mode": "ltl", "finalMileMiles": 0.0, "status": "SHIPPED",
            "items": [{"sellerSku": ln["sku"], "quantity": ln["qty"]} for ln in o["lines"]],
            "events": [{"eventCode": "SHIPPED", "eventTime": _iso(now), "description": f"Picked up by {carrier}"}],
        })
        self.store.put({"id": f"pkg:{bol}", "doc_type": "package", "sfo_id": o["sfo_id"]})
        by_sku: dict[str, dict[str, Any]] = {}
        for ln in o["lines"]:
            d = by_sku.setdefault(ln["sku"], {"sellerSku": ln["sku"], "lineId": ln["line_id"],
                                              "casePack": ln["case_pack"], "unitsShipped": 0, "casesShipped": 0})
            d["unitsShipped"] += ln["qty"]
            d["casesShipped"] += ln["qty"] // ln["case_pack"]
        asn_id = f"ASN{uuid.uuid4().hex[:10].upper()}"
        self._load_truck(o, asn_id, list(by_sku.values()))
        self._notify("ADVANCE_SHIP_NOTICE", {
            "sellerFulfillmentOrderId": o["sfo_id"], "asnId": asn_id,
            "carrier": carrier, "proNumber": pro, "billOfLading": bol, "appointment": _iso(appt),
            "pallets": pallets, "lines": list(by_sku.values()), "shippedAt": _iso(now),
        })
        self._notify("SHIPMENT_FACT", {
            "package_id": bol, "order_id": o["sfo_id"], "shipped_on": now.date().isoformat(),
            "units": sum(ln["qty"] for ln in o["lines"]), "pallets": pallets, "mode": "ltl", "carrier": carrier,
            "fulfillment_cost": round(95 + 0.00009 * weight * plan["miles"], 2), "final_mile_miles": 0.0,
            "from_node": o["lines"][0]["node"],
        })
        if o["fate"] == "delay":
            appt += timedelta(days=self.rng.choice([1, 1, 2]))
            o["eta"] = appt.date().isoformat()
        self._set_status(o, "SHIPPED", now)
        for p in o["packages"]:
            self._send_tracking(o, p, "SHIPPED", now)
        o["step"], o["next_at"] = "deliver", _iso(max(appt, now + timedelta(hours=12)))

    def _load_truck(self, o: dict[str, Any], asn_id: str, asn_lines: list[dict[str, Any]]) -> None:
        """What really went on the truck (sim.common.freight). A case left on the shelf, or grabbed by
        mistake, is still in (or missing from) the building: the next physical count finds it, and inventory
        control books the variance afterwards, as it does for shrink."""
        o["truck"] = truck = truck_outcome(asn_id, asn_lines, self.truck_fates)
        node_of = {}
        for ln in o["lines"]:
            node_of.setdefault(ln["sku"], ln["node"])
        meta = self.store.get("meta:variances") or {"id": "meta:variances", "doc_type": "meta", "pending": []}
        for sku, units in truck["loaded"].items():
            left_behind = int(sum(ln["unitsShipped"] for ln in asn_lines if ln["sellerSku"] == sku)) - units
            if left_behind and sku in node_of:
                self._adjust(sku, node_of[sku], left_behind)
                meta["pending"].append({"sku": sku, "node": node_of[sku], "qty": left_behind})
        if meta["pending"]:
            self.store.put(meta)

    # ---------------------------------------------------------------- consumer returns

    def _advance_return(self, r: dict[str, Any], now: datetime) -> None:
        o = self.store.get(f"order:{r['sfo_id']}")
        if r["step"] == "pickup":
            r["status"] = "IN_TRANSIT"
            self._return_status(r, "IN_TRANSIT", now, "Dropped off with the carrier")
            miles = geo.miles(geo.NODES[r["node"]], tuple(o["dest"])) if o else 800
            r["step"], r["next_at"] = "arrive", _iso(now + timedelta(days=geo.ground_days(miles))
                                                     + self._wait(hours=(2, 20)))
        elif r["step"] == "arrive":
            r["status"] = "DELIVERED"
            self._return_status(r, "DELIVERED", now, f"Delivered to {r['node']} returns")
            r["step"], r["next_at"] = "inspect", _iso(now + self._wait(hours=(6, 40)))
        elif r["step"] == "inspect":
            graded: dict[tuple[str, str], int] = {}
            for unit in r["contents"]:
                if unit["grade"] != "MISSING":
                    key = (unit["sellerSku"], unit["grade"])
                    graded[key] = graded.get(key, 0) + 1
            sellable = [(sku, n) for (sku, g), n in graded.items() if g == "SELLABLE"]
            for sku, n in sellable:
                self._adjust(sku, r["node"], n)
            if sellable:
                self._inventory_events([self._inv_event("receipt", sku, r["node"], n, now, reference=r["rma_id"])
                                        for sku, n in sellable])
            r["status"], r["received_at"] = "RECEIVED", _iso(now)
            self._notify("RETURN_RECEIVED", {
                "rmaId": r["rma_id"], "sellerReturnId": r["return_id"], "sellerFulfillmentOrderId": r["sfo_id"],
                "nodeId": r["node"], "receivedAt": _iso(now),
                "items": [{"sellerSku": sku, "grade": g, "quantity": n} for (sku, g), n in sorted(graded.items())]})
            r["step"], r["next_at"] = None, None
        self.store.put(r)

    def _return_status(self, r: dict[str, Any], status: str, now: datetime, description: str) -> None:
        label = r["response"]["label"]
        self._notify("RETURN_STATUS", {"rmaId": r["rma_id"], "sellerReturnId": r["return_id"],
                                       "sellerFulfillmentOrderId": r["sfo_id"], "status": status,
                                       "carrier": label["carrier"], "trackingNumber": label["trackingNumber"],
                                       "eventTime": _iso(now), "description": description})

    def _fate(self, fates=FATES, default: str = "normal") -> str:
        r = self.rng.random()
        for fate, p in fates:
            if r < p:
                return fate
            r -= p
        return default

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
        variances = self.store.get("meta:variances")
        for v in (variances or {}).get("pending") or []:
            adjustments.append(self._inv_event("adjustment", v["sku"], v["node"], v["qty"], now + timedelta(hours=6)))
        if variances and variances["pending"]:
            variances["pending"] = []
            self.store.put(variances)
        if adjustments:
            self._inventory_events(adjustments, not_before=now + timedelta(hours=6))
        log.info("snapshot: %d counts, %d shrink adjustments", len(counts), len(adjustments))

    def _vendor_receipts(self, now: datetime) -> None:
        """The clients' vendors deliver to the inbound storage centres weekly."""
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
        if action == "freight_claim":
            if not order:
                return {"filed": False, "reason": "order_not_found"}
            pkg = (order.get("packages") or [{}])[0]
            carriers = (order.get("truck") or {}).get("fate") in CARRIER_FATES
            return {"filed": True, "claim_number": f"FCL{uuid.uuid4().hex[:8].upper()}", "carrier": pkg.get("carrier"),
                    "carrier_response": "accepted" if carriers else
                    "declined: the carrier's records show every case on the notice delivered"}
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


def _num(v: Any, default: float) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else default


def _generic_item(upc: str, d: dict[str, Any]) -> catalog.Item:
    """An item the sim knows only from its listing: slow and steady demand, so it gets a little stock."""
    return catalog.Item(upc=upc, title=d.get("title") or f"Item {upc}", category="General",
                        unit_cost=float(d.get("unit_cost") or 15.0), weight_lb=float(d.get("weight_lb") or 2.0),
                        weekly_units={r: 2.0 for r in catalog.REGIONS})


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
