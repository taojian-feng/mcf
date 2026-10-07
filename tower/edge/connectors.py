"""Outbound partner connectors and the feed intake, run inside the agents' consumer process.

  oms.outbound    -> Corp B order management   POST {MCF_CORPB_URL}/oms/v1/events
  corpb.outbound  -> Corp B (decant counts...)  POST {MCF_CORPB_URL}/corpb/v1/events
  handoffs.events -> Corp A operations          CreateCase on the MCF API, for handoffs Corp A owns
  feeds.arrived   -> a partner file landed in storage: catalog, forecast, sales or cold-start data

A failed delivery raises, so the bus redelivers the message and dead-letters it after repeated failures.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from tower.core import topics
from tower.core.models import OwningParty
from tower.core.ports import Event, MCFTransport

log = logging.getLogger("tower.connectors")

CATALOG_DOC = ("planning_meta", "catalog")
CORPB_INBOUND_KEY = "corpb-inbound-api-key"

Poster = Callable[[str, dict[str, str], bytes], int]


def urllib_post(url: str, headers: dict[str, str], body: bytes) -> int:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


class DeliveryFailed(Exception):
    pass


class PartnerConnectors:
    def __init__(self, system, *, corpb_url: str | None, secret: Callable[[str], str],
                 mcf: MCFTransport | None = None, post: Poster = urllib_post):
        self.system, self.secret, self.post = system, secret, post
        self.corpb_url = corpb_url.rstrip("/") if corpb_url else None
        self.mcf = mcf if mcf is not None else system.ports.mcf
        bus = system.ports.bus
        if self.corpb_url:
            bus.subscribe(topics.OMS_OUTBOUND, self.to_oms, subscription="oms-connector")
            bus.subscribe(topics.CORPB_OUTBOUND, self.to_corpb, subscription="corpb-connector")
        bus.subscribe(topics.HANDOFF_EVENTS, self.to_corpa_ops, subscription="corpa-connector")
        bus.subscribe(topics.FEEDS_ARRIVED, self.on_feed, subscription="integration-layer")

    # ---------------------------------------------------------------- Corp B

    def to_oms(self, event: Event) -> None:
        self._deliver(f"{self.corpb_url}/oms/v1/events", event)

    def to_corpb(self, event: Event) -> None:
        self._deliver(f"{self.corpb_url}/corpb/v1/events", event)

    def _deliver(self, url: str, event: Event) -> None:
        body = json.dumps({"event_id": event.event_id, "type": event.type, "payload": event.payload,
                           "occurred_at": event.occurred_at.isoformat()}, default=str).encode()
        status = self.post(url, {"Content-Type": "application/json", "X-Api-Key": self.secret(CORPB_INBOUND_KEY)}, body)
        if not 200 <= status < 300:
            raise DeliveryFailed(f"{url} answered {status} for {event.type} {event.event_id}")

    # ---------------------------------------------------------------- Corp A operations (handoffs)

    def to_corpa_ops(self, event: Event) -> None:
        p = event.payload
        if event.type != "created" or p.get("responsible_party") != OwningParty.CORP_A.value:
            return
        # Retryable failures raise MCFError, so the bus redelivers; a refusal is final and the
        # handoff's own ack timer escalates it to Corp A's escalation contact.
        from tower.core.ports import MCFError
        try:
            self.mcf.call("CreateCase", {"caseId": p["handoff_id"], "action": p["action"], "dueAt": p.get("due_at"),
                                         "context": p.get("context") or {}})
        except MCFError as e:
            if e.retryable:
                raise
            log.error("Corp A refused case %s: %s", p["handoff_id"], e)

    # ---------------------------------------------------------------- feed files (R7.2, R7.3)

    def on_feed(self, event: Event) -> None:
        if event.type != "Microsoft.Storage.BlobCreated":
            return
        subject = event.payload.get("subject", "")
        marker = "/containers/landing/blobs/"
        if marker not in subject:
            return
        path = subject.split(marker, 1)[1]
        parts = path.split("/")
        if len(parts) < 3:
            log.warning("feed %s: expected <party>/<feed type>/<file>", path)
            return
        kind = parts[1]
        store = self.system.ports.store
        done_key = f"feed:{path}"
        if store.get("planning_meta", done_key):
            return  # Event Grid delivers at least once
        doc = json.loads(self.system.ports.blobs.read("landing", path))
        records = doc.get("records", doc) if isinstance(doc, dict) else doc
        feed_id = f"{kind}:{parts[-1].rsplit('.', 1)[0]}"
        planning = self.system.planning
        if kind == "catalog":
            self.apply_catalog(records, feed_id)
        elif kind == "forecast":
            planning.ingest_forecast(feed_id, records)
        elif kind == "sales":
            planning.ingest_sales(feed_id, records)
        elif kind == "cold_start":
            planning.ingest_cold_start(feed_id, records)
        else:
            log.warning("feed %s: unknown feed type %s", path, kind)
            return
        store.put("planning_meta", done_key, {"path": path, "kind": kind, "records": len(records),
                                              "processed_at": self.system.ports.clock.now().isoformat()})
        log.info("feed %s: %d records processed", path, len(records))

    def apply_catalog(self, records: list[dict[str, Any]], feed_id: str) -> None:
        """Pilot item master: the UPCs in scope and their unit costs (used for replenishment value)."""
        upcs = sorted({r["upc"] for r in records if isinstance(r.get("upc"), str) and len(r["upc"]) == 12})
        costs = {r["upc"]: float(r["unit_cost"]) for r in records
                 if r.get("upc") in upcs and isinstance(r.get("unit_cost"), (int, float))}
        self.system.ports.store.put(*CATALOG_DOC, {"feed_id": feed_id, "upcs": upcs, "unit_cost": costs,
                                                   "received_at": self.system.ports.clock.now().isoformat()})
        apply_catalog_doc(self.system.config, {"upcs": upcs, "unit_cost": costs})
        self.system.audit.decision("integration_layer", "pilot_catalog_loaded", input_refs=[f"feed:{feed_id}"],
                                   result={"upcs": len(upcs)}, owning_party=OwningParty.CORP_B)
        # List the items with Corp A so it accepts orders and stock for them.
        titles = {r["upc"]: r.get("title") for r in records if r.get("upc") in costs or r.get("upc") in upcs}
        res = self.system.integration.call("CreateFeedDocument", {
            "contentType": "LISTINGS", "feedId": feed_id,
            "items": [{"sellerSku": u, "title": titles.get(u)} for u in upcs]}, agent="integration_layer")
        if not res.ok and res.retryable:
            raise DeliveryFailed(f"listing feed {feed_id} not accepted by Corp A: {res.error_code}")


def apply_catalog_doc(config, doc: dict[str, Any] | None) -> bool:
    """Swap the in-process pilot catalog (attribute assignment, so readers never see a half-built set)."""
    if not doc or not doc.get("upcs"):
        return False
    upcs = set(doc["upcs"])
    if upcs != config.pilot_upcs:
        config.pilot_upcs = upcs
        config.unit_cost = {**config.unit_cost, **(doc.get("unit_cost") or {})}
        return True
    return False


def keep_catalog_fresh(system, every_s: float = 300) -> None:
    """Other replicas and processes pick up a new catalog within a few minutes."""
    def loop() -> None:
        while True:
            try:
                if apply_catalog_doc(system.config, system.ports.store.get(*CATALOG_DOC)):
                    log.info("pilot catalog refreshed: %d UPCs", len(system.config.pilot_upcs))
            except Exception:  # noqa: BLE001 - try again next round
                log.exception("catalog refresh failed")
            time.sleep(every_s)
    threading.Thread(target=loop, daemon=True, name="catalog-refresh").start()
