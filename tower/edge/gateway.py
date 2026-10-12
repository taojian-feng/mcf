"""Partner gateway: the HTTPS front door for retail clients and Corp A.

  POST /v1/orders              A client's order management sends consumer orders (X-Api-Key). The key
                               says which client is calling; the order is stamped with that tenant.
  POST /v1/store-orders        A client's store system sends store replenishment orders (cases, delivery
                               window), for clients whose contract includes stores.
  POST /v1/returns             A client's order management asks for a consumer return of a delivered order.
  POST /v1/catalog             A client sends its item catalog (UPC, title, unit cost). It lands in the
                               client's feed folder like a file drop and is loaded the same way.
  POST /v1/mcf/notifications   Corp A pushes order status, tracking, inventory, shipment facts,
                               disposition results and case updates (X-MCF-Signature: HMAC-SHA256).

Requests are authenticated, validated and turned into events on the bus; the agents do the rest.
Each event's id comes from the partner's own id (order id, notification id, inventory event id),
so the bus's duplicate detection absorbs partner retries.

Run on Azure with `python -m tower.edge.gateway` (port $PORT, default 8080).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tower.core import topics
from tower.core.models import ConsumerOrder, ReturnRequest, StoreOrder, StoreReceipt, UnhappyPathSignal
from tower.core.ports import BlobStore, Clock, Event, MessageBus
from tower.core.tenants import Tenant, default_tenants
from tower.services.client_keys import MASTER_SECRET, api_key as issued_api_key

log = logging.getLogger("tower.gateway")

CORPA_WEBHOOK_SECRET = "partner-corpa-webhook-secret"
MAX_SKEW_S = 300
MAX_CATALOG_ITEMS = 20_000

# Corp A's order statuses -> our lifecycle states. RECEIVED is ours already.
STATUS_MAP = {
    "ACCEPTED": "acknowledged", "PROCESSING": "processing", "PACKED": "packed", "SHIPPED": "shipped",
    "OUT_FOR_DELIVERY": "out_for_delivery", "DELIVERED": "delivered", "CANCELLED": "cancelled",
    "UNFULFILLABLE": "cancelled",
}
# Carrier exception scans that open an exception case.
TRACKING_SIGNALS = {"LOST": "carrier_lost", "UNDELIVERABLE": "address_undeliverable", "DAMAGED": "damage_report"}


class GatewayError(Exception):
    def __init__(self, status: int, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.message, self.details = status, message, details


def verify_signature(secret: str, header: str, body: bytes, now: float) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(now - ts) > MAX_SKEW_S:
        return False
    good = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(parts.get("v1", ""), good)


class Gateway:
    def __init__(self, bus: MessageBus, secret: Callable[[str], str], clock: Clock,
                 wall: Callable[[], float] = time.time,
                 tenants: Callable[[], dict[str, Tenant]] = default_tenants, blobs: BlobStore | None = None,
                 announce_feeds: bool = False):
        self.bus, self.secret, self.clock, self.wall, self.tenants = bus, secret, clock, wall, tenants
        # On Azure, Event Grid announces every file in the landing container; elsewhere the gateway does.
        self.blobs, self.announce_feeds = blobs, announce_feeds

    # ---------------------------------------------------------------- clients: orders

    def tenant_for(self, api_key: str) -> Tenant | None:
        """The client whose key this is. Every key is compared, so timing says nothing about which matched."""
        found = None
        for t in self.tenants().values():
            try:
                expected = (issued_api_key(self.secret(MASTER_SECRET), t) if t.keys == "issued"
                            else self.secret(t.api_key_secret))
            except Exception:  # noqa: BLE001 - a client without a key yet cannot call
                continue
            if expected and hmac.compare_digest(api_key.encode(), expected.encode()):
                found = found or t
        return found

    def _caller(self, api_key: str, body: Any, channel: str | None) -> tuple[Tenant, dict[str, Any]]:
        tenant = self.tenant_for(api_key) if api_key else None
        if tenant is None:
            raise GatewayError(401, "invalid API key")
        if not tenant.active:
            raise GatewayError(403, f"client {tenant.tenant_id} is not active")
        if channel and channel not in tenant.channels:
            raise GatewayError(403, f"client {tenant.tenant_id} has no {channel} orders in its contract")
        if not isinstance(body, dict):
            raise GatewayError(422, "order failed validation", ["body: must be a JSON object"])
        return tenant, {k: v for k, v in body.items() if k not in ("tenant_id", "client_order_id", "client_return_id")}  # never from the caller

    @staticmethod
    def _parse(model, body: dict[str, Any]):
        try:
            return model.model_validate(body)
        except ValidationError as ve:
            raise GatewayError(422, "order failed validation",
                               [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in ve.errors()]) from ve

    def receive_order(self, api_key: str, body: dict[str, Any]) -> dict[str, Any]:
        tenant, body = self._caller(api_key, body, "online")
        order = self._parse(ConsumerOrder, body)
        if order.delivery_promise_days not in tenant.promise_days:
            raise GatewayError(422, "order failed validation",
                               [f"delivery_promise_days: the contract covers {tenant.promise_days} days"])
        client_id = order.order_id
        order = order.model_copy(update={"tenant_id": tenant.tenant_id, "client_order_id": client_id,
                                         "order_id": tenant.fulfillment_id(client_id)})
        self.bus.publish(Event(topics.ORDERS_RECEIVED, "order_received", order.model_dump(mode="json"),
                               self.clock.now(), event_id=f"ord_{order.order_id}", correlation_id=order.order_id))
        return {"accepted": True, "order_id": client_id}

    def receive_store_order(self, api_key: str, body: dict[str, Any]) -> dict[str, Any]:
        tenant, body = self._caller(api_key, body, "store")
        order = self._parse(StoreOrder, body)
        problems = []
        if order.window_end < order.window_start:
            problems.append("window_end: before window_start")
        if (order.window_end - order.placed_at.date()).days < tenant.store_lead_days:
            problems.append(f"window_end: the contract needs at least {tenant.store_lead_days} days' notice")
        if len({ln.line_id for ln in order.lines}) != len(order.lines):
            problems.append("lines: line_id must be unique")
        if problems:
            raise GatewayError(422, "order failed validation", problems)
        client_id = order.order_id
        order = order.model_copy(update={"tenant_id": tenant.tenant_id, "client_order_id": client_id,
                                         "order_id": tenant.fulfillment_id(client_id)})
        self.bus.publish(Event(topics.ORDERS_RECEIVED, "store_order_received", order.model_dump(mode="json"),
                               self.clock.now(), event_id=f"ord_{order.order_id}", correlation_id=order.order_id))
        return {"accepted": True, "order_id": client_id}

    def receive_store_receipt(self, api_key: str, body: dict[str, Any]) -> dict[str, Any]:
        """A store's receiving report: what its back room counted off a delivery, against the ship notice."""
        tenant, body = self._caller(api_key, body, "store")
        body.pop("client_receipt_id", None)
        receipt = self._parse(StoreReceipt, body)
        problems = []
        if not receipt.lines:
            problems.append("lines: one or more")
        if len({ln.upc for ln in receipt.lines}) != len(receipt.lines):
            problems.append("lines: one line per item")
        problems += [f"lines: {ln.upc} has more damaged units than received" for ln in receipt.lines
                     if ln.damaged_units > ln.received_units]
        if problems:
            raise GatewayError(422, "receipt failed validation", problems)
        client_id, client_order = receipt.receipt_id, receipt.order_id
        receipt = receipt.model_copy(update={
            "tenant_id": tenant.tenant_id, "client_receipt_id": client_id, "client_order_id": client_order,
            "order_id": tenant.fulfillment_id(client_order), "receipt_id": tenant.fulfillment_id(client_id),
            "recount_of": tenant.fulfillment_id(receipt.recount_of) if receipt.recount_of else None})
        self.bus.publish(Event(topics.RECEIVING_REPORTS, "store_receipt", receipt.model_dump(mode="json"),
                               self.clock.now(), event_id=f"rcv_{receipt.receipt_id}", correlation_id=receipt.order_id))
        return {"accepted": True, "receipt_id": client_id}

    def receive_return(self, api_key: str, body: dict[str, Any]) -> dict[str, Any]:
        tenant, body = self._caller(api_key, body, "online")
        if tenant.return_window_days <= 0:
            raise GatewayError(403, f"client {tenant.tenant_id} has no returns in its contract")
        req = self._parse(ReturnRequest, body)
        if len({ln.line_id for ln in req.lines}) != len(req.lines) or not req.lines:
            raise GatewayError(422, "return failed validation", ["lines: one or more, line_id unique"])
        client_id = req.return_id
        req = req.model_copy(update={"tenant_id": tenant.tenant_id, "client_return_id": client_id,
                                     "client_order_id": req.order_id,
                                     "return_id": tenant.fulfillment_id(client_id),
                                     "order_id": tenant.fulfillment_id(req.order_id)})
        self.bus.publish(Event(topics.RETURNS_EVENTS, "return_requested", req.model_dump(mode="json"),
                               self.clock.now(), event_id=f"ret_{req.return_id}", correlation_id=req.order_id))
        return {"accepted": True, "return_id": client_id}

    def receive_catalog(self, api_key: str, body: dict[str, Any]) -> dict[str, Any]:
        tenant, body = self._caller(api_key, body, None)
        if self.blobs is None:
            raise GatewayError(503, "catalog uploads aren't available here")
        items = body.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_CATALOG_ITEMS:
            raise GatewayError(422, "catalog failed validation", [f"items: a list of 1 to {MAX_CATALOG_ITEMS} items"])
        problems = []
        for i, it in enumerate(items):
            upc = it.get("upc") if isinstance(it, dict) else None
            if not (isinstance(upc, str) and len(upc) == 12 and upc.isdigit()):
                problems.append(f"items.{i}.upc: 12 digits")
            elif not isinstance(it.get("unit_cost", 0), (int, float)) or it.get("unit_cost", 0) < 0:
                problems.append(f"items.{i}.unit_cost: a number, 0 or more")
            if len(problems) >= 20:
                break
        if problems:
            raise GatewayError(422, "catalog failed validation", problems)
        records = [{k: it.get(k) for k in ("upc", "title", "category", "unit_cost", "weight_lb", "case_pack") if k in it}
                   for it in items]
        now = self.clock.now()
        path = f"{tenant.feed_folder}/catalog/{now:%Y-%m-%d}-{hashlib.sha256(json.dumps(records).encode()).hexdigest()[:8]}.json"
        self.blobs.write("landing", path, json.dumps({"feed_type": "catalog", "generated_at": now.isoformat(),
                                                      "records": records}).encode())
        if self.announce_feeds:
            self.bus.publish(Event(topics.FEEDS_ARRIVED, "Microsoft.Storage.BlobCreated",
                                   {"subject": f"/blobServices/default/containers/landing/blobs/{path}"}, now))
        return {"accepted": True, "items": len(records), "file": path}

    # ---------------------------------------------------------------- Corp A: notifications

    def receive_notification(self, signature: str, raw: bytes) -> dict[str, Any]:
        if not verify_signature(self.secret(CORPA_WEBHOOK_SECRET), signature, raw, self.wall()):
            raise GatewayError(401, "bad or expired signature")
        try:
            n = json.loads(raw)
            kind, nid, p = n["notificationType"], n["notificationId"], n["payload"]
        except (ValueError, KeyError, TypeError) as e:
            raise GatewayError(400, f"not a notification: {e}") from e
        handler = getattr(self, f"_n_{kind.lower()}", None)
        if handler is None:
            log.warning("ignoring unknown notification type %s (%s)", kind, nid)
            return {"accepted": False, "reason": f"unknown notificationType {kind}"}
        published = handler(nid, p)
        return {"accepted": True, "events": published}

    def _n_order_status_change(self, nid: str, p: dict[str, Any]) -> int:
        state = STATUS_MAP.get(p.get("status", ""))
        if state is None:
            return 0
        order_id = p["sellerFulfillmentOrderId"]
        self._emit(topics.ORDERS_LIFECYCLE, "status", {"order_id": order_id, "state": state, "at": p.get("statusTime"),
                                                       "partner_status": p["status"], "reason": p.get("reason")},
                   nid, order_id)
        if p["status"] == "UNFULFILLABLE":
            self._signal("fulfillment_unfulfillable", nid, order_id, {"reason": p.get("reason")})
            return 2
        return 1

    def _n_tracking_update(self, nid: str, p: dict[str, Any]) -> int:
        order_id = p["sellerFulfillmentOrderId"]
        self._emit(topics.ORDERS_LIFECYCLE, "tracking", {
            "order_id": order_id, "carrier": p.get("carrier"), "tracking_number": p.get("trackingNumber"),
            "status": p.get("status"), "events": p.get("events") or [], "package_number": p.get("packageNumber"),
        }, nid, order_id)
        sig = TRACKING_SIGNALS.get(p.get("status", ""))
        if sig:
            self._signal(sig, nid, order_id, {"package": p.get("packageNumber"), "carrier": p.get("carrier"),
                                              "events": p.get("events") or []})
            return 2
        return 1

    def _n_inventory_events(self, nid: str, p: dict[str, Any]) -> int:
        events = p.get("events") or []
        for ev in events:
            # Malformed events still go through: the Reconciliation agent rejects, audits and reports them (R2.5).
            ok = isinstance(ev, dict) and isinstance(ev.get("event_id"), str)
            self._emit(topics.INVENTORY_EVENTS, "inventory_event", ev if isinstance(ev, dict) else {"raw": ev},
                       ev["event_id"] if ok else f"{nid}:{events.index(ev)}",
                       f"{ev.get('sku')}|{ev.get('node_id')}" if ok else None)
        return len(events)

    def _n_advance_ship_notice(self, nid: str, p: dict[str, Any]) -> int:
        """Freight left for a store: what is on the truck (cases, pallets), the carrier's PRO number, the
        dock appointment. It becomes the ship notice the client's store receives against."""
        order_id = p["sellerFulfillmentOrderId"]
        self._emit(topics.ORDERS_LIFECYCLE, "ship_notice", {"order_id": order_id, **{k: p.get(k) for k in (
            "asnId", "carrier", "proNumber", "billOfLading", "appointment", "pallets", "lines", "shippedAt")}},
                   nid, order_id)
        return 1

    def _n_return_status(self, nid: str, p: dict[str, Any]) -> int:
        """The return parcel moved: picked up by the carrier, or delivered to Corp A's returns bench."""
        self._emit(topics.RETURNS_EVENTS, "return_status", {
            "return_id": p["sellerReturnId"], "rma_id": p.get("rmaId"), "status": p.get("status"),
            "carrier": p.get("carrier"), "tracking_number": p.get("trackingNumber"), "at": p.get("eventTime")},
                   nid, p.get("sellerFulfillmentOrderId"))
        return 1

    def _n_return_received(self, nid: str, p: dict[str, Any]) -> int:
        """Corp A opened and graded the parcel: per item, how many units and in what condition."""
        self._emit(topics.RETURNS_EVENTS, "return_received", {
            "return_id": p["sellerReturnId"], "rma_id": p.get("rmaId"), "node_id": p.get("nodeId"),
            "items": p.get("items") or [], "at": p.get("receivedAt")}, nid, p.get("sellerFulfillmentOrderId"))
        return 1

    def _n_shipment_fact(self, nid: str, p: dict[str, Any]) -> int:
        self._emit(topics.SHIPMENT_FACTS, "shipment_fact", p, nid, p.get("order_id"))
        return 1

    def _n_disposition_status(self, nid: str, p: dict[str, Any]) -> int:
        self._emit(topics.DISPOSITION_EVENTS, p.get("status", "update"), p, nid, p.get("rec_id"))
        return 1

    def _n_handoff_update(self, nid: str, p: dict[str, Any]) -> int:
        kind = {"ACKNOWLEDGED": "acknowledged", "COMPLETED": "completed"}.get(p.get("status", ""))
        if kind is None:
            return 0
        self._emit(topics.HANDOFF_UPDATES, kind, {"handoff_id": p["caseId"], "by": p.get("by", "corp_a_ops"),
                                                  "result": p.get("result") or {}}, nid, p["caseId"])
        return 1

    # ---------------------------------------------------------------- helpers

    def _emit(self, topic: str, type_: str, payload: dict[str, Any], event_id: str, correlation: str | None) -> None:
        self.bus.publish(Event(topic, type_, payload, self.clock.now(), event_id=event_id, correlation_id=correlation))

    def _signal(self, signal_type: str, nid: str, order_id: str, details: dict[str, Any]) -> None:
        sig = UnhappyPathSignal(signal_type=signal_type, source="integration_layer", order_id=order_id,
                                details=details, received_at=self.clock.now())
        self._emit(topics.EXCEPTION_SIGNALS, signal_type, sig.model_dump(mode="json"), f"{nid}:sig", order_id)


def create_app(gateway: Gateway) -> Starlette:
    async def orders(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "body is not JSON"}, 400)
        return await _run(gateway.receive_order, request.headers.get("x-api-key", ""), body)

    async def store_orders(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "body is not JSON"}, 400)
        return await _run(gateway.receive_store_order, request.headers.get("x-api-key", ""), body)

    async def store_receipts(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "body is not JSON"}, 400)
        return await _run(gateway.receive_store_receipt, request.headers.get("x-api-key", ""), body)

    async def returns(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "body is not JSON"}, 400)
        return await _run(gateway.receive_return, request.headers.get("x-api-key", ""), body)

    async def catalog(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "body is not JSON"}, 400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, 422)
        return await _run(gateway.receive_catalog, request.headers.get("x-api-key", ""), body)

    async def notifications(request: Request) -> JSONResponse:
        return await _run(gateway.receive_notification, request.headers.get("x-mcf-signature", ""),
                          await request.body())

    async def _run(fn, *args) -> JSONResponse:
        try:
            return JSONResponse(await run_in_threadpool(fn, *args), 202)
        except GatewayError as e:
            if e.status >= 401:
                log.warning("%s: %s %s", fn.__name__, e.status, e.message)
            return JSONResponse({"error": e.message, **({"details": e.details} if e.details else {})}, e.status)

    return Starlette(routes=[
        Route("/healthz", lambda r: JSONResponse({"ok": True, "service": "mcf-gateway",
                                                  "time": datetime.now(timezone.utc).isoformat()})),
        Route("/v1/orders", orders, methods=["POST"]),
        Route("/v1/store-orders", store_orders, methods=["POST"]),
        Route("/v1/store-receipts", store_receipts, methods=["POST"]),
        Route("/v1/returns", returns, methods=["POST"]),
        Route("/v1/catalog", catalog, methods=["POST"]),
        Route("/v1/mcf/notifications", notifications, methods=["POST"]),
    ])


def tenant_source(appconfig_url: str | None, cred, every_s: float = 300, store=None) -> Callable[[], dict[str, Tenant]]:
    """The client registry (App Configuration, plus clients onboarded through the tower when `store` is
    given), re-read every few minutes so a new client needs no restart."""
    if not appconfig_url:
        return default_tenants
    from tower.adapters.azure import AppConfigDirectory
    directory = AppConfigDirectory(appconfig_url, cred)
    if store is not None:
        from tower.services.clients import ClientDirectory
        directory = ClientDirectory(directory, store, ttl_s=0)
    cache: dict[str, Any] = {"at": 0.0, "tenants": default_tenants()}

    def get() -> dict[str, Tenant]:
        if time.time() - cache["at"] >= every_s:
            try:
                cache["tenants"] = directory.tenants()
                cache["at"] = time.time()
            except Exception:  # noqa: BLE001 - keep serving with the last good registry
                log.exception("could not read the client registry")
        return cache["tenants"]
    return get


def main() -> None:
    import os

    import uvicorn

    from tower.adapters.azure import AzureBlobStore, CosmosStore, KeyVaultSecrets, ServiceBusBus, SystemClock, _credential

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    env = os.environ
    cred = _credential()
    secrets = KeyVaultSecrets(env["MCF_KEYVAULT_URL"], cred)
    store = CosmosStore(env["MCF_COSMOS_URL"], env.get("MCF_COSMOS_DB", "mcf"), cred) if env.get("MCF_COSMOS_URL") else None
    tenants = tenant_source(env.get("MCF_APPCONFIG_URL"), cred, store=store)
    blobs = AzureBlobStore(env["MCF_BLOB_URL"], cred) if env.get("MCF_BLOB_URL") else None
    gateway = Gateway(ServiceBusBus(env["MCF_SERVICEBUS_NAMESPACE"], cred), secrets.get_secret, SystemClock(),
                      tenants=tenants, blobs=blobs)
    uvicorn.run(create_app(gateway), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="warning")


if __name__ == "__main__":
    main()
