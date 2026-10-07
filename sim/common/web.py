"""Outbound HTTPS, webhook signing and secrets, shared by both simulated companies."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

log = logging.getLogger("sim")

# (method, url, headers, body) -> (status, response body). Swapped for an in-process client in tests.
Sender = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


def urllib_sender(method: str, url: str, headers: dict[str, str], body: bytes | None,
                  timeout: float = 30) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return 599, str(e).encode()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """Webhook signature header value: t=<unix seconds>,v1=<hex HMAC-SHA256 of '<t>.<body>'>."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def post_json(send: Sender, url: str, payload: Any, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    body = json.dumps(payload, default=str).encode()
    return send("POST", url, {"Content-Type": "application/json", **(headers or {})}, body)


def load_secret(name: str, *, env: str | None = None, wait_s: float = 1200) -> str:
    """A secret from the environment, else from Key Vault (SIM_KEYVAULT_URL) with the app's managed
    identity. The deploy seeds the secrets after the first infrastructure pass, so wait for them."""
    env = env or name.upper().replace("-", "_")
    if os.environ.get(env):
        return os.environ[env]
    vault = os.environ.get("SIM_KEYVAULT_URL")
    if not vault:
        raise RuntimeError(f"secret {name}: set {env} or SIM_KEYVAULT_URL")
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient
    client = SecretClient(vault, DefaultAzureCredential())
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return client.get_secret(name).value
        except Exception as e:  # noqa: BLE001 - not seeded yet, or role assignment still propagating
            if time.monotonic() > deadline:
                raise
            log.warning("waiting for secret %s: %s", name, type(e).__name__)
            time.sleep(15)
