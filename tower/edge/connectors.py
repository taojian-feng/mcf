"""Outbound partner connectors and the feed intake, run inside the agents' consumer process.

  oms.outbound    -> the client's order management   POST {client URL}/oms/v1/events
  corpb.outbound  -> Corp B (decant counts...)        POST {MCF_CORPB_URL}/corpb/v1/events
  handoffs.events -> Corp A operations                CreateCase on the MCF API, for handoffs Corp A owns
  feeds.arrived   -> a client file landed in storage: catalog, forecast, sales or cold-start data

Each client's URL is its registry entry's outbound_url, else env MCF_<CLIENT>_URL (MCF_CORPB_URL for
Corp B), and each client has its own key for it. An event goes only to the client it names.

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
from tower.core.tenants import TENANT_ONE, Tenant

log = logging.getLogger("tower.connectors")

CATALOG_DOC = ("planning_meta", "catalog")  # tenant one's; other clients' are catalog_doc(tenant)


def catalog_doc(tenant_id: str) -> tuple[str, str]:
    return CATALOG_DOC if tenant_id == TENANT_ONE else ("planning_meta", f"catalog:{tenant_id}")


def feed_done_key(path: str) -> str:
    """Marks a feed file as processed. Cosmos DB ids can't hold '/', so the path's slashes become '|'."""
    return "feed:" + path.replace("/", "|")

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
                 mcf: MCFTransport | None = None, post: Poster = urllib_post,
                 env: Callable[[str], str | None] = lambda name: None):
        self.system, self.secret, self.post, self.env = system, secret, post, env
        self.corpb_url = corpb_url.rstrip("/") if corpb_url else None
        self.mcf = mcf if mcf is not None else system.ports.mcf
        bus = system.ports.bus
        bus.subscribe(topics.OMS_OUTBOUND, self.to_oms, subscription="oms-connector")
        if self.corpb_url:
            bus.subscribe(topics.CORPB_OUTBOUND, self.to_corpb, subscription="corpb-connector")
        bus.subscribe(topics.HANDOFF_EVENTS, self.to_corpa_ops, subscription="corpa-connector")
        bus.subscribe(topics.FEEDS_ARRIVED, self.on_feed, subscription="integration-layer")

    # ---------------------------------------------------------------- clients

    def client_url(self, tenant: Tenant) -> str | None:
        if tenant.tenant_id == TENANT_ONE and self.corpb_url:
            return self.corpb_url
        url = tenant.outbound_url or self.env(f"MCF_{tenant.feed_folder.upper()}_URL")
        return url.rstrip("/") if url else None

    def to_oms(self, event: Event) -> None:
        tenant = self.system.config.tenant(event.payload.get("tenant_id"))
        url = self.client_url(tenant) if tenant else None
        if url is None:
            log.warning("no order-management endpoint for client %s; %s %s not sent",
                        event.payload.get("tenant_id") or TENANT_ONE, event.type, event.event_id)
            return
        self._deliver(f"{url}/oms/v1/events", event, tenant)

    def to_corpb(self, event: Event) -> None:
        self._deliver(f"{self.corpb_url}/corpb/v1/events", event, self.system.config.tenant(TENANT_ONE))

    def _deliver(self, url: str, event: Event, tenant: Tenant) -> None:
        body = json.dumps({"event_id": event.event_id, "type": event.type, "payload": event.payload,
                           "occurred_at": event.occurred_at.isoformat()}, default=str).encode()
        headers = {"Content-Type": "application/json", "X-Api-Key": self.secret(tenant.outbound_key_secret)}
        status = self.post(url, headers, body)
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
                                         "clientId": p.get("tenant_id") or TENANT_ONE,
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
            log.warning("feed %s: expected <client>/<feed type>/<file>", path)
            return
        kind = parts[1]
        tenant = self._tenant_for_folder(parts[0])
        if tenant is None:
            log.warning("feed %s: folder %s belongs to no client", path, parts[0])
            return
        store = self.system.ports.store
        done_key = feed_done_key(path)
        if store.get("planning_meta", done_key):
            return  # Event Grid delivers at least once
        records = self._records(path)
        feed_id = f"{kind}:{parts[-1].rsplit('.', 1)[0]}"
        planning = self.system.planning
        if kind == "catalog":
            self.apply_catalog(records, feed_id, tenant.tenant_id)
        elif tenant.tenant_id != TENANT_ONE:
            # Clients own separate stock now, but planning still runs for tenant one only (per-client planning is next).
            log.warning("feed %s: %s feeds are planned for %s only so far", path, kind, TENANT_ONE)
            return
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

    def _tenant_for_folder(self, folder: str) -> Tenant | None:
        """A new client's first files can land before this process has reread the registry: reread it then."""
        config = self.system.config
        tenant = next((t for t in config.tenants.values() if t.feed_folder == folder), None)
        load = getattr(self.system.ports.directory, "tenants", None)
        if tenant is None and load is not None:
            try:
                config.tenants = load()
            except Exception:  # noqa: BLE001 - keep the copy we have
                log.exception("could not reread the client registry")
            tenant = next((t for t in config.tenants.values() if t.feed_folder == folder), None)
        return tenant

    def _records(self, path: str) -> list[dict[str, Any]]:
        doc = json.loads(self.system.ports.blobs.read("landing", path))
        return doc.get("records", doc) if isinstance(doc, dict) else doc

    def catch_up_catalogs(self) -> list[str]:
        """Apply the newest catalog file of any client that has none loaded (one set aside before the client
        was registered, say). Without a catalog every order from that client is turned away."""
        listing = getattr(self.system.ports.blobs, "list", None)
        store, applied = self.system.ports.store, []
        if listing is None:
            return applied
        for tenant in list(self.system.config.tenants.values()):
            if store.get(*catalog_doc(tenant.tenant_id)):
                continue
            paths = sorted(listing("landing", f"{tenant.feed_folder}/catalog/"))
            if not paths:
                log.warning("no catalog for client %s yet; its orders will be turned away until one arrives",
                            tenant.tenant_id)
                continue
            path = paths[-1]
            records = self._records(path)
            self.apply_catalog(records, f"catalog:{path.rsplit('/', 1)[-1].rsplit('.', 1)[0]}", tenant.tenant_id)
            store.put("planning_meta", feed_done_key(path), {"path": path, "kind": "catalog", "records": len(records),
                                                             "processed_at": self.system.ports.clock.now().isoformat()})
            log.warning("client %s had no catalog; loaded %s (%d records)", tenant.tenant_id, path, len(records))
            applied.append(path)
        return applied

    def apply_catalog(self, records: list[dict[str, Any]], feed_id: str, tenant_id: str = TENANT_ONE) -> None:
        """A client's item master: the UPCs in scope and their unit costs (used for replenishment value)."""
        upcs = sorted({r["upc"] for r in records if isinstance(r.get("upc"), str) and len(r["upc"]) == 12})
        costs = {r["upc"]: float(r["unit_cost"]) for r in records
                 if r.get("upc") in upcs and isinstance(r.get("unit_cost"), (int, float))}
        self.system.ports.store.put(*catalog_doc(tenant_id), {
            "feed_id": feed_id, "tenant_id": tenant_id, "upcs": upcs, "unit_cost": costs,
            "received_at": self.system.ports.clock.now().isoformat()})
        apply_catalog_doc(self.system.config, {"upcs": upcs, "unit_cost": costs}, tenant_id)
        self.system.audit.decision("integration_layer", "pilot_catalog_loaded", input_refs=[f"feed:{feed_id}"],
                                   result={"upcs": len(upcs)}, owning_party=OwningParty.CORP_B, tenant_id=tenant_id)
        # List the items with Corp A so it accepts orders and stock for them.
        titles = {r["upc"]: r.get("title") for r in records if r.get("upc") in costs or r.get("upc") in upcs}
        res = self.system.integration.call("CreateFeedDocument", {
            "contentType": "LISTINGS", "feedId": feed_id,
            "items": [{"sellerSku": u, "title": titles.get(u)} for u in upcs]}, agent="integration_layer")
        if not res.ok and res.retryable:
            raise DeliveryFailed(f"listing feed {feed_id} not accepted by Corp A: {res.error_code}")


def apply_catalog_doc(config, doc: dict[str, Any] | None, tenant_id: str = TENANT_ONE) -> bool:
    """Swap a client's in-process catalog (attribute assignment, so readers never see a half-built set)."""
    if not doc or not doc.get("upcs"):
        return False
    upcs = set(doc["upcs"])
    if tenant_id != TENANT_ONE:
        if upcs == config.tenant_upcs.get(tenant_id):
            return False
        config.tenant_upcs = {**config.tenant_upcs, tenant_id: upcs}
        return True
    if upcs != config.pilot_upcs:
        config.pilot_upcs = upcs
        config.unit_cost = {**config.unit_cost, **(doc.get("unit_cost") or {})}
        return True
    return False


def load_catalogs(system) -> bool:
    """Every client's catalog from the store; True if any changed."""
    changed = False
    for tid in list(system.config.tenants):
        changed |= apply_catalog_doc(system.config, system.ports.store.get(*catalog_doc(tid)), tid)
    return changed


def keep_catalog_fresh(system, every_s: float = 300) -> None:
    """Other replicas and processes pick up new catalogs, and new clients, within a few minutes."""
    load = getattr(system.ports.directory, "tenants", None)

    def loop() -> None:
        while True:
            try:
                if load:
                    system.config.tenants = load()
                if load_catalogs(system):
                    log.info("catalogs refreshed: %s", {t: len(system.config.catalog(t)) for t in system.config.tenants})
            except Exception:  # noqa: BLE001 - try again next round
                log.exception("catalog refresh failed")
            time.sleep(every_s)
    threading.Thread(target=loop, daemon=True, name="catalog-refresh").start()
