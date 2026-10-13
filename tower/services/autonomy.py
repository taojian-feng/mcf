"""Earned autonomy: approvals of a kind that people keep agreeing with Claude on are approved automatically.

A lane is one kind of approval for one owner (e.g. restocks for Corp C, store-delivery credits for Corp A).
Its track record is the last `autonomy_min_decisions` decisions people made in that lane on requests whose
brief Claude wrote with a clear suggestion (approve or reject). A lane earns autonomy when that record is
full, agreement is at least `autonomy_agreement`, and people never rejected a request Claude suggested
approving. Then a request Claude suggests approving, worth no more than `autonomy_max_usd`, is approved at
once and audited; one in `autonomy_spot_check_every` still goes to a person, so the record stays fresh.

Autonomy only ever approves: a suggested rejection or a closer look always goes to a person. A rejected spot
check takes the lane's autonomy away until the record is clean again, and any approver for the lane can stop
it (and start it again) from the console. Onboarding a client and sending an invoice never run on their own.
"""
from __future__ import annotations

from typing import Any

from tower.core.config import PilotConfig
from tower.core.models import ApprovalCategory, ApprovalItem, OwningParty
from tower.core.ports import Clock, DocumentStore
from tower.core.tenants import party_key

LANES = "autonomy_lanes"
APPROVALS = "approvals"
AUTONOMY = "autonomy"            # decided_by on approvals it made, and the actor in the audit log
NEVER = frozenset({ApprovalCategory.CLIENT_ONBOARDING, ApprovalCategory.INVOICE})


def lane_of(item: ApprovalItem) -> str:
    return f"{item.category.value}:{party_key(item.owning_party, item.tenant_id)}"


def value_usd(item: ApprovalItem) -> float | None:
    """What the request is worth, where the request says; None when it carries no money figure."""
    c = item.content
    for key in ("credit_usd", "value", "estimated_disposition_cost"):
        if isinstance(c.get(key), (int, float)):
            return float(c[key])
    return None


def _clear(doc: dict[str, Any]) -> str | None:
    b = doc.get("brief") or {}
    return b.get("suggestion") if b.get("source") == "claude" and b.get("suggestion") in ("approve", "reject") else None


class Autonomy:
    def __init__(self, store: DocumentStore, clock: Clock, config: PilotConfig):
        self.store, self.clock, self.config = store, clock, config

    # ---------------------------------------------------------------- the record

    def record(self, lane: str) -> dict[str, Any]:
        """People's recent decisions in the lane against Claude's suggestion, and whether autonomy is earned."""
        category, owner = lane.split(":", 1)
        docs = [d for d in self.store.query(APPROVALS, category=category)
                if d["status"] != "pending" and d.get("decided_by") != AUTONOMY and _clear(d)
                and party_key_of(d) == owner]
        docs = sorted(docs, key=lambda d: d.get("decided_at") or "", reverse=True)[:self.config.autonomy_min_decisions]
        agreed = sum((_clear(d) == "approve") == (d["status"] == "approved") for d in docs)
        wrong_approve = sum(_clear(d) == "approve" and d["status"] == "rejected" for d in docs)
        lane_docs = [d for d in self.store.query(APPROVALS, category=category) if party_key_of(d) == owner]
        auto = [d for d in lane_docs if d.get("decided_by") == AUTONOMY]
        checks = [d for d in lane_docs if (d.get("auto") or {}).get("spot_check")]
        state = self.store.get(LANES, lane) or {}
        rec = {"lane": lane, "decisions": len(docs), "agreed": agreed, "rejected_when_claude_said_approve": wrong_approve,
               "agreement": round(agreed / len(docs), 3) if docs else None, "auto_approved": len(auto),
               "spot_checks": len(checks),
               "stopped_by": state.get("stopped_by"), "stopped_at": state.get("stopped_at"),
               "stop_reason": state.get("reason")}
        rec["earned"] = (len(docs) >= self.config.autonomy_min_decisions and not wrong_approve
                         and agreed / len(docs) >= self.config.autonomy_agreement)
        rec["on"] = rec["earned"] and not rec["stopped_by"] and self.eligible(category)
        return rec

    def eligible(self, category: str) -> bool:
        return self.config.autonomy and category in self.config.autonomy_categories \
            and ApprovalCategory(category) not in NEVER

    # ---------------------------------------------------------------- a new request

    def check(self, item: ApprovalItem) -> dict[str, Any] | None:
        """{'approve': True, ...} to approve now; {'spot_check': True, ...} to send to a person on purpose;
        None when the lane has not earned autonomy or the request is not one it may approve."""
        if not self.eligible(item.category.value) or (item.brief or {}).get("source") != "claude" \
                or (item.brief or {}).get("suggestion") != "approve":
            return None
        value = value_usd(item)
        if value is not None and value > self.config.autonomy_max_usd:
            return None
        rec = self.record(lane_of(item))
        if not rec["on"]:
            return None
        info = {"lane": rec["lane"], "decisions": rec["decisions"], "agreement": rec["agreement"]}
        if (rec["auto_approved"] + rec["spot_checks"] + 1) % self.config.autonomy_spot_check_every == 0:
            return {**info, "spot_check": True}
        return {**info, "approve": True}

    # ---------------------------------------------------------------- people's switch

    def stop(self, user: str, lane: str, reason: str) -> None:
        self.store.put(LANES, lane, {"lane": lane, "stopped_by": user, "stopped_at": self.clock.now().isoformat(),
                                     "reason": reason})

    def start(self, lane: str) -> None:
        self.store.put(LANES, lane, {"lane": lane, "stopped_by": None})


def party_key_of(doc: dict[str, Any]) -> str:
    return party_key(OwningParty(doc["owning_party"]), doc.get("tenant_id"))
