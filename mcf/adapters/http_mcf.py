"""HTTP transport to the Corp A MCF API. Cloud-neutral: only needs a SecretStore for credentials.

The paths below are placeholders until the pilot's MCF API contract is confirmed; the error
mapping is what the Integration_Layer's retry policy (R7.4/R7.5) depends on.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from mcf.core.ports import MCFError, SecretStore

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
}

STATUS_CODES = {
    400: ("validation", False), 401: ("unauthorized", False), 403: ("forbidden", False),
    422: ("malformed_request", False), 408: ("timeout", True), 429: ("throttled", True),
    503: ("service_unavailable", True), 504: ("timeout", True),
}


class HttpMCFTransport:
    def __init__(self, base_url: str, secrets: SecretStore, *, timeout_s: float = 30):
        self.base_url = base_url.rstrip("/")
        self.secrets = secrets
        self.timeout_s = timeout_s

    def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        method, path = PATHS[operation]
        url = self.base_url + path.format(**payload)
        body = json.dumps(payload).encode() if method in ("POST", "PUT") else None
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.secrets.get_secret('mcf-access-token')}",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            code, retryable = STATUS_CODES.get(e.code, ("service_unavailable", True) if e.code >= 500
                                               else ("validation", False))
            raise MCFError(operation, code, f"HTTP {e.code}", retryable=retryable) from e
        except (TimeoutError, urllib.error.URLError) as e:
            raise MCFError(operation, "timeout", str(e), retryable=True) from e
