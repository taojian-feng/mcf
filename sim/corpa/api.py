"""Corp A's MCF API over HTTPS: OAuth2 client credentials, then the fulfillment operations.

Like any busy partner API it sometimes throttles (429 with Retry-After), sometimes has a bad moment
(503), and very occasionally answers too slowly; the rates are configurable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import time
import urllib.parse
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .engine import CorpA

TOKEN_TTL_S = 3600


class Tokens:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id, self.client_secret = client_id, client_secret
        self._key = hashlib.sha256(f"corpa-token|{client_secret}".encode()).digest()

    def issue(self, now: float) -> str:
        claims = base64.urlsafe_b64encode(json.dumps({"cid": self.client_id, "exp": int(now) + TOKEN_TTL_S}).encode())
        mac = base64.urlsafe_b64encode(hmac.new(self._key, claims, hashlib.sha256).digest())
        return f"{claims.decode()}.{mac.decode()}"

    def valid(self, token: str, now: float) -> bool:
        try:
            claims, mac = token.split(".")
            good = base64.urlsafe_b64encode(hmac.new(self._key, claims.encode(), hashlib.sha256).digest()).decode()
            return hmac.compare_digest(mac, good) and json.loads(base64.urlsafe_b64decode(claims))["exp"] > now
        except Exception:  # noqa: BLE001 - any malformed token is simply invalid
            return False

    def check_client(self, cid: str | None, secret: str | None) -> bool:
        return bool(cid and secret) and hmac.compare_digest(cid, self.client_id) and \
            hmac.compare_digest(secret, self.client_secret)


def create_app(engine: CorpA, tokens: Tokens, *, faults: tuple[float, float, float] = (0.015, 0.01, 0.003),
               rng: random.Random | None = None, clock_s=time.time, sleep=time.sleep,
               on_startup: list | None = None) -> Starlette:
    """on_startup: callables run once when the server starts (the engine loop)."""
    rng = rng or random.Random()
    p503, p429, pslow = faults

    async def token(request: Request) -> JSONResponse:
        raw = (await request.body()).decode()
        if request.headers.get("content-type", "").startswith("application/json"):
            form: dict[str, Any] = json.loads(raw or "{}")
        else:
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        cid, secret = form.get("client_id"), form.get("client_secret")
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            cid, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
        if form.get("grant_type") != "client_credentials":
            return JSONResponse({"error": "unsupported_grant_type"}, 400)
        if not tokens.check_client(cid, secret):
            return JSONResponse({"error": "invalid_client"}, 401)
        return JSONResponse({"access_token": tokens.issue(clock_s()), "token_type": "Bearer",
                             "expires_in": TOKEN_TTL_S})

    def guarded(handler):
        def run(request: Request, body: dict[str, Any]) -> JSONResponse:
            auth = request.headers.get("authorization", "")
            if not (auth.startswith("Bearer ") and tokens.valid(auth[7:], clock_s())):
                return JSONResponse({"errors": [{"code": "Unauthorized", "message": "missing or expired token"}]}, 401)
            r = rng.random()
            if r < p503:
                return JSONResponse({"errors": [{"code": "ServiceUnavailable", "message": "try again"}]}, 503)
            if r < p503 + p429:
                return JSONResponse({"errors": [{"code": "QuotaExceeded", "message": "rate limit"}]}, 429,
                                    headers={"Retry-After": "2"})
            if r < p503 + p429 + pslow:
                sleep(35)
            code, out = handler(request.path_params, body)
            return JSONResponse(out, code)

        async def endpoint(request: Request) -> JSONResponse:
            raw = await request.body() if request.method in ("POST", "PUT") else b""
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                return JSONResponse({"errors": [{"code": "InvalidInput", "message": "body is not JSON"}]}, 400)
            return await run_in_threadpool(run, request, body)
        return endpoint

    routes = [
        Route("/healthz", lambda r: JSONResponse({"ok": True, "service": "corp-a-mcf"})),
        Route("/oauth2/token", token, methods=["POST"]),
        Route("/mcf/v1/fulfillment-orders", guarded(lambda p, b: engine.create_order(b)), methods=["POST"]),
        Route("/mcf/v1/fulfillment-orders/preview", guarded(lambda p, b: engine.preview(b)), methods=["POST"]),
        Route("/mcf/v1/fulfillment-orders/{sfo}", guarded(lambda p, b: engine.get_order(p["sfo"])), methods=["GET"]),
        Route("/mcf/v1/fulfillment-orders/{sfo}/status", guarded(lambda p, b: engine.order_status(p["sfo"])),
              methods=["GET"]),
        Route("/mcf/v1/tracking/{pkg}", guarded(lambda p, b: engine.tracking(p["pkg"])), methods=["GET"]),
        Route("/mcf/v1/inbound/plans", guarded(lambda p, b: engine.inbound_plan(b)), methods=["POST"]),
        Route("/mcf/v1/inbound/shipments/{sid}", guarded(lambda p, b: engine.update_inbound(p["sid"], b)),
              methods=["PUT"]),
        Route("/mcf/v1/feeds/documents", guarded(lambda p, b: engine.feed_document(b)), methods=["POST"]),
        Route("/mcf/v1/distribution-plans/{pid}/approve", guarded(lambda p, b: engine.approve_plan(p["pid"], b)),
              methods=["POST"]),
        Route("/mcf/v1/cases", guarded(lambda p, b: engine.create_case(b)), methods=["POST"]),
    ]
    return Starlette(routes=routes, lifespan=_lifespan(on_startup or []))


def _lifespan(hooks: list):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        for h in hooks:
            h()
        yield
    return lifespan
