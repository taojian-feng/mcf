"""Corp C's endpoints for updates from the control tower: allocations, ship notices, tracking (API key in X-Api-Key)."""
from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .engine import CorpC


def create_app(engine: CorpC, inbound_key: str, *, on_startup: list | None = None) -> Starlette:
    def receiver(channel: str):
        async def endpoint(request: Request) -> JSONResponse:
            if not hmac.compare_digest(request.headers.get("x-api-key", ""), inbound_key):
                return JSONResponse({"error": "unauthorized"}, 401)
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"error": "body is not JSON"}, 400)
            await run_in_threadpool(engine.receive, channel, body)
            return JSONResponse({"received": True})
        return endpoint

    @asynccontextmanager
    async def lifespan(app):
        for hook in on_startup or []:
            hook()
        yield

    return Starlette(routes=[
        Route("/healthz", lambda r: JSONResponse({"ok": True, "service": "corp-c-oms"})),
        Route("/oms/v1/events", receiver("oms"), methods=["POST"]),
        Route("/corpc/v1/events", receiver("corpc"), methods=["POST"]),
    ], lifespan=lifespan)
