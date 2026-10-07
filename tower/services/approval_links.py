"""Signed links that let an approver open the approval console from an email.

A link carries the approver's identity (who the email was sent to) and an expiry, signed with
HMAC-SHA256. Holding a valid link is treated as signing in as that person, the same way a
password-reset or actionable-email link works. Every decision is still checked against the
approver roles and written to the audit ledger by ApprovalService.decide.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from urllib.parse import quote

from tower.core.ports import Clock

LINK_KEY_SECRET = "approval-link-key"
DEFAULT_TTL = timedelta(days=14)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class ApprovalLinks:
    def __init__(self, base_url: str, key: bytes, clock: Clock, ttl: timedelta = DEFAULT_TTL):
        self.base_url, self.key, self.clock, self.ttl = base_url.rstrip("/"), key, clock, ttl

    def token(self, user: str) -> str:
        exp = int((self.clock.now() + self.ttl).timestamp())
        body = _b64(json.dumps({"u": user, "exp": exp}, separators=(",", ":")).encode())
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str) -> str | None:
        """Return the user the token was issued to, or None if it is forged, malformed or expired."""
        try:
            body, sig = token.split(".", 1)
            if not hmac.compare_digest(sig, self._sign(body)):
                return None
            claims = json.loads(_unb64(body))
            if claims["exp"] < self.clock.now().timestamp():
                return None
            return str(claims["u"])
        except (ValueError, KeyError, TypeError):
            return None

    def inbox_url(self, user: str) -> str:
        return f"{self.base_url}/approvals?t={quote(self.token(user))}"

    def item_url(self, user: str, approval_id: str) -> str:
        return f"{self.base_url}/approvals/{quote(approval_id)}?t={quote(self.token(user))}"

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())
