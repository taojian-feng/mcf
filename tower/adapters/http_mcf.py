"""HTTP transport to the Corp A MCF API. Cloud-neutral: only needs a SecretStore for credentials.

Auth is OAuth2 client credentials (secrets `mcf-client-id` / `mcf-client-secret`); the access token
is cached until shortly before it expires and refreshed once on a 401. The error mapping is what
the Integration_Layer's retry policy (R7.4/R7.5) depends on. Contract: docs/PARTNER_APIS.md.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from tower.core.ports import MCFError, SecretStore

PATHS: dict[str, tuple[str, str]] = {
    "CreateFulfillmentOrder": ("POST", "/mcf/v1/fulfillment-orders"),
    "GetFulfillmentOrder": ("GET", "/mcf/v1/fulfillment-orders/{sellerFulfillmentOrderId}"),
    "FulfillmentOrderStatus": ("GET", "/mcf/v1/fulfillment-orders/{sellerFulfillmentOrderId}/status"),
    "GetFulfillmentPreview": ("POST", "/mcf/v1/fulfillment-orders/preview"),
    "GetPackageTrackingDetail": ("GET", "/mcf/v1/tracking/{packageNumber}"),
    "CreateInboundShipmentPlan": ("POST", "/mcf/v1/inbound/plans"),
    "UpdateInboundShipment": ("PUT", "/mcf/v1/inbound/shipments/{shipmentId}"),
    "CreateFeedDocument": ("POST", "/mcf/v1/feeds/documents"),
    "ApproveDistributionPlan": ("POST", "/mcf/v1/distribution-plans/{planId}/approve"),
    "CreateCase": ("POST", "/mcf/v1/cases"),  # cross-boundary handoffs to Corp A operations (R11)
}

STATUS_CODES = {
    400: ("validation", False), 401: ("unauthorized", False), 403: ("forbidden", False),
    404: ("not_found", False), 409: ("conflict", False), 422: ("malformed_request", False),
    408: ("timeout", True), 429: ("throttled", True), 503: ("service_unavailable", True), 504: ("timeout", True),
}

# (method, url, headers, body, timeout) -> (status, body); raises TimeoutError / OSError when unreachable.
Opener = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


def urllib_opener(method: str, url: str, headers: dict[str, str], body: bytes | None,
                  timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class HttpMCFTransport:
    def __init__(self, base_url: str, secrets: SecretStore, *, token_url: str | None = None,
                 timeout_s: float = 30, opener: Opener = urllib_opener, clock: Callable[[], float] = time.time):
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url or f"{self.base_url}/oauth2/token"
        self.secrets, self.timeout_s, self.opener, self.clock = secrets, timeout_s, opener, clock
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        method, path = PATHS[operation]
        url = self.base_url + path.format(**{k: urllib.parse.quote(str(v), safe="") for k, v in payload.items()
                                             if isinstance(v, (str, int))})
        body = json.dumps(payload).encode() if method in ("POST", "PUT") else None
        for attempt in (1, 2):
            status, raw = self._send(operation, method, url, body, self.access_token())
            if status == 401 and attempt == 1:
                self._token = None  # expired or rotated: fetch a new token once
                continue
            break
        if 200 <= status < 300:
            return json.loads(raw) if raw else {}
        code, retryable = STATUS_CODES.get(status, ("service_unavailable", True) if status >= 500
                                           else ("validation", False))
        raise MCFError(operation, code, f"HTTP {status}: {_detail(raw)}", retryable=retryable)

    def access_token(self) -> str:
        with self._lock:
            if self._token and self._token[1] > self.clock() + 60:
                return self._token[0]
            form = urllib.parse.urlencode({
                "grant_type": "client_credentials", "client_id": self.secrets.get_secret("mcf-client-id"),
                "client_secret": self.secrets.get_secret("mcf-client-secret")}).encode()
            status, raw = self._send("token", "POST", self.token_url, form, None,
                                     content_type="application/x-www-form-urlencoded")
            if status != 200:
                code, retryable = ("unauthorized", False) if status in (400, 401) else ("service_unavailable", True)
                raise MCFError("token", code, f"HTTP {status}: {_detail(raw)}", retryable=retryable)
            tok = json.loads(raw)
            self._token = (tok["access_token"], self.clock() + float(tok.get("expires_in", 3600)))
            return self._token[0]

    def _send(self, operation: str, method: str, url: str, body: bytes | None, token: str | None, *,
              content_type: str = "application/json") -> tuple[int, bytes]:
        headers = {"Content-Type": content_type, "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            return self.opener(method, url, headers, body, self.timeout_s)
        except (TimeoutError, OSError) as e:
            raise MCFError(operation, "timeout", str(e), retryable=True) from e


def _detail(raw: bytes) -> str:
    try:
        errs = json.loads(raw).get("errors") or []
        return "; ".join(e.get("message", "") for e in errs)[:300]
    except Exception:  # noqa: BLE001 - not JSON: show the start of it
        return raw[:300].decode(errors="replace")
