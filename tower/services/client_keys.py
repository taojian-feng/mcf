"""Keys for clients onboarded through the tower.

Clients set up by hand (Corp B, Corp C) have their own Key Vault secrets. A client the onboarding agent
takes on gets two keys derived from one master secret instead, so activating a client needs no write
access to Key Vault:

  API key       the client sends it in X-Api-Key when it calls our gateway
  webhook key   we send it in X-Api-Key when we call the client's endpoint, so the client knows it's us

Each key is an HMAC of the client id and a version under the master secret. Raising the client's
key_version rotates both. Corp A shares the keys with the client over a secure channel; the console
shows them only to Corp A's onboarding approvers.
"""
from __future__ import annotations

import hashlib
import hmac

from tower.core.tenants import Tenant

MASTER_SECRET = "client-key-master"


def _derive(master: str, purpose: str, tenant_id: str, version: int) -> str:
    mac = hmac.new(master.encode(), f"{purpose}:{tenant_id}:{version}".encode(), hashlib.sha256).hexdigest()
    return f"ct{purpose[0]}_{mac[:40]}"


def api_key(master: str, tenant: Tenant) -> str:
    return _derive(master, "api", tenant.tenant_id, tenant.key_version)


def webhook_key(master: str, tenant: Tenant) -> str:
    return _derive(master, "webhook", tenant.tenant_id, tenant.key_version)
