"""Receiving_Agent: store deliveries that came up short, damaged or with the wrong item.

A client's store counts each delivery off the truck against the ship notice and sends its receiving report
(POST /v1/store-receipts). When the count matches, the delivery is closed. When it does not, someone has to
say why, because the answer decides who pays:

  pick_error         Corp A's warehouse loaded a wrong case or left one behind: credit the client
  carrier_damage     the carrier damaged a case: credit the client; Corp A files a freight claim
  carrier_loss       the carrier lost a case: credit the client; Corp A files a freight claim
  store_count_error  everything arrived and the store miscounted: no credit; ask the store to count again
  unclear            a person at Corp A decides

The evidence is what each party would really say: the store's report, the ship notice, the carrier's
delivery notes, Corp A's physical counts since the truck left (a case left on the shelf shows up as an
overage at the shipping node; one grabbed by mistake as a shortage of the other item), and the store's
earlier reports. `PilotConfig.receiving_review` decides who weighs it: fixed rules on the report alone, or
Claude with all of it. Calls below `receiving_confidence`, and credits above `receiving_credit_approval_usd`,
go to Corp A's approvers. A recount that matches the notice closes the claim with no credit; one that is
still off goes to a person. Every call is audited with its evidence and reasoning.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import ApprovalCategory, OwningParty, ReceivingCause as C, StoreReceipt
from tower.core.ports import LLM, Clock, Directory, DocumentStore, Event, MessageBus, Notifier
from tower.services.approval import ApprovalService
from tower.services.audit import AuditService
from tower.services.handoffs import HandoffService

AGENT = "receiving_agent"
RECEIPTS = "store_receipts"
CLAIMS = "receiving_claims"
ORDERS = "orders"
FACTS = "shipment_facts"
COUNTS = "reconciliation_reports"
CHARGES = "charges"                 # billing's: a credit is a negative charge on the client's next invoice
CREDIT_KIND = "receiving_credit"
CORP_A_OPS = "corp_a_ops"
HISTORY = 20                        # the store's latest reports shown as history
CREDITED = (C.PICK_ERROR, C.CARRIER_DAMAGE, C.CARRIER_LOSS)
CARRIER = (C.CARRIER_DAMAGE, C.CARRIER_LOSS)
log = logging.getLogger("tower.receiving")

PROMPT = (
    "You investigate store deliveries for a logistics provider (Corp A) that ships retail clients' stock from "
    "its warehouses to their stores by LTL freight. A store's receiving count did not match the ship notice. "
    "Decide the most likely cause, because it decides who pays:\n"
    "  pick_error: Corp A's warehouse loaded a wrong case or left a case on the shelf (Corp A credits the client)\n"
    "  carrier_damage / carrier_loss: the carrier damaged or lost cases (Corp A credits the client and files a "
    "freight claim with the carrier)\n"
    "  store_count_error: everything arrived and the store miscounted (no credit; the store is asked to recount)\n"
    "  unclear: the evidence does not support one cause\n"
    "Weigh all the evidence. The carrier's delivery note (proof of delivery) records damage or missing cases the "
    "driver saw. Corp A counts every position nightly: a case left on the shelf shows up as an overage of about "
    "that many units at the shipping node in the first count after the ship date, and a case grabbed by mistake "
    "as a shortage of the other item; small gaps of a few units are routine shrink. One item short and another "
    "over on the same delivery is a pick error. A clean delivery note and no matching overage point to a "
    "miscount, more so at a store with a history of count errors. Give a calibrated confidence in [0,1] and a "
    "one-sentence rationale citing the evidence.")


class ReceivingCall(BaseModel):
    cause: Literal["pick_error", "carrier_damage", "carrier_loss", "store_count_error", "unclear"]
    confidence: float = Field(ge=0, le=1)
    rationale: str


def rules_call(problems: list[dict[str, Any]]) -> dict[str, Any]:
    """The report alone: damage is the carrier's, anything else the warehouse's."""
    if any(p["damaged_units"] for p in problems):
        return {"cause": C.CARRIER_DAMAGE.value, "confidence": 0.9, "rationale": "the store reported damaged units"}
    if any(p["received_units"] > p["expected_units"] for p in problems):
        return {"cause": C.PICK_ERROR.value, "confidence": 0.85,
                "rationale": "the store received more of an item than the notice shows"}
    return {"cause": C.PICK_ERROR.value, "confidence": 0.75,
            "rationale": "the store received less than the notice shows"}


class ReceivingAgent:
    def __init__(self, store: DocumentStore, bus: MessageBus, approvals: ApprovalService, handoffs: HandoffService,
                 audit: AuditService, notifier: Notifier, directory: Directory, clock: Clock, config: PilotConfig,
                 llm: LLM | None = None):
        self.store, self.bus, self.approvals, self.handoffs = store, bus, approvals, handoffs
        self.audit, self.notifier, self.directory, self.clock, self.config = audit, notifier, directory, clock, config
        self.llm = llm
        bus.subscribe(topics.RECEIVING_REPORTS, self.on_receipt, subscription="receiving-agent")
        bus.subscribe(topics.APPROVAL_DECISIONS, self.on_decision, subscription="receiving-agent")

    @property
    def uses_claude(self) -> bool:
        return self.config.receiving_review == "claude" and self.llm is not None

    # ---------------------------------------------------------------- the store's report

    def on_receipt(self, event: Event) -> None:
        if event.type != "store_receipt":
            return
        r = StoreReceipt.model_validate(event.payload)
        if self.store.get(RECEIPTS, r.receipt_id):
            return                                          # at-least-once delivery
        problems = [ln.model_dump() for ln in r.problems]
        self.store.put(RECEIPTS, r.receipt_id, {
            **r.model_dump(mode="json"), "problem_lines": len(problems), "recorded_at": self.clock.now().isoformat()})
        if r.recount_of:
            self._on_recount(r, problems)
        elif not problems:
            self._audit("receipt_clean", r.receipt_id, r, rationale="the store's count matches the ship notice")
        else:
            self.investigate(r, problems)

    def investigate(self, r: StoreReceipt, problems: list[dict[str, Any]]) -> dict[str, Any]:
        evidence = self.evidence(r, problems)
        call, by = None, "rules"
        if self.uses_claude:
            call = self.ask_claude(evidence)
            by = "claude" if call else "rules (Claude unavailable)"
        call = call or rules_call(problems)
        claim = {"claim_id": r.receipt_id, "receipt_id": r.receipt_id, "client_receipt_id": r.client_receipt_id,
                 "tenant_id": r.tenant_id, "order_id": r.order_id, "client_order_id": r.client_order_id,
                 "store_id": r.store_id, "asn_id": r.asn_id, "received_at": r.received_at.isoformat(),
                 "problems": problems, "evidence": evidence, "call": {**call, "by": by},
                 "credit_usd": self.credit_value(problems), "status": "open", "actions": [],
                 "opened_at": self.clock.now().isoformat()}
        self._audit("investigate_receipt", r.receipt_id, r, rationale=call["rationale"],
                    result={"cause": call["cause"], "confidence": call["confidence"], "by": by,
                            "credit_usd": claim["credit_usd"]})
        self.act(claim)
        return claim

    def ask_claude(self, evidence: dict[str, Any]) -> dict[str, Any] | None:
        try:
            out = self.llm.structured(system=PROMPT, prompt=json.dumps(evidence, default=str), schema=ReceivingCall)
        except Exception:  # noqa: BLE001 - the rules' call stands
            log.warning("receiving call failed; using the rules", exc_info=True)
            return None
        if out is None:
            return None
        return out.model_dump() if isinstance(out, BaseModel) else ReceivingCall.model_validate(out).model_dump()

    # ---------------------------------------------------------------- evidence

    def evidence(self, r: StoreReceipt, problems: list[dict[str, Any]]) -> dict[str, Any]:
        order = self.store.get(ORDERS, r.order_id) or {}
        notices = order.get("ship_notices") or []
        notice = next((n for n in notices if n.get("asnId") == r.asn_id), notices[-1] if notices else {})
        facts = self.store.query(FACTS, order_id=r.order_id)
        shipped_on = min((f["shipped_on"] for f in facts if f.get("shipped_on")), default=None) \
            or (notice.get("shippedAt") or "")[:10] or None
        skus = {p["upc"] for p in problems}
        return {
            "store_report": {"store_id": r.store_id, "received_at": r.received_at.isoformat(), "lines": problems,
                             "lines_matching_the_notice": len(r.lines) - len(problems), "note": r.note},
            "ship_notice": {k: notice.get(k) for k in ("asnId", "carrier", "proNumber", "pallets", "shippedAt")}
            | {"lines": [{k: ln.get(k) for k in ("sellerSku", "unitsShipped", "casesShipped", "casePack")}
                         for ln in notice.get("lines") or []]},
            "shipped_from": sorted({f.get("from_node") for f in facts if f.get("from_node")}),
            "carrier_delivery_notes": self._delivery_notes(order),
            "warehouse_counts_since_ship": self._count_gaps(skus, shipped_on),
            "store_history": self._history(r),
        }

    @staticmethod
    def _delivery_notes(order: dict[str, Any]) -> list[dict[str, Any]]:
        out, seen = [], set()
        for t in order.get("tracking") or []:
            for e in t.get("events") or []:
                key = (e.get("eventCode"), e.get("eventTime"))
                if key not in seen:
                    seen.add(key)
                    out.append({"event": e.get("eventCode"), "at": e.get("eventTime"), "note": e.get("description")})
        return out[-8:]

    def _count_gaps(self, skus: set[str], shipped_on: str | None) -> dict[str, Any]:
        """Each nightly count since the ship date: where Corp A's count of these items differed from its records."""
        if not shipped_on:
            return {"days_counted": 0, "gaps": []}
        start = date.fromisoformat(shipped_on)
        days, gaps = 0, []
        for d in range(0, 8):
            day = start + timedelta(days=d)
            if day > self.clock.now().date():
                break
            rep = self.store.get(COUNTS, day.isoformat())
            if rep is None:
                continue
            days += 1
            gaps += [{"date": day.isoformat(), "sku": g["sku"], "node": g["node_id"],
                      "gap_units": g["observed"] - g["expected"]}
                     for g in rep.get("discrepancies") or [] if g["sku"] in skus]
        return {"days_counted": days, "gaps": gaps}

    def _history(self, r: StoreReceipt) -> dict[str, Any]:
        mine = sorted((x for x in self.store.query(RECEIPTS, tenant_id=r.tenant_id, store_id=r.store_id)
                       if x["receipt_id"] != r.receipt_id and not x.get("recount_of")),
                      key=lambda x: x["received_at"])[-HISTORY:]
        claims = [self.store.get(CLAIMS, x["receipt_id"]) for x in mine if x.get("problem_lines")]
        outcomes = [c.get("outcome") for c in claims if c and c.get("outcome")]
        return {"deliveries": len(mine), "with_problems": len(claims),
                "recounts_that_matched": outcomes.count("recount_matched"),
                "settled_causes": [c["call"]["cause"] for c in claims
                                   if c and c.get("status") == "closed" and c.get("outcome") != "recount_matched"]}

    def credit_value(self, problems: list[dict[str, Any]]) -> float:
        """What the client lost: units short of the notice plus damaged units, at the item's cost."""
        total = 0.0
        for p in problems:
            units = max(0, p["expected_units"] - p["received_units"]) + p["damaged_units"]
            total += units * self.config.unit_cost.get(p["upc"], self.config.default_unit_cost)
        return round(total, 2)

    # ---------------------------------------------------------------- acting on the call

    def act(self, claim: dict[str, Any]) -> None:
        call = claim["call"]
        cause = C(call["cause"])
        if cause == C.UNCLEAR or call["confidence"] < self.config.receiving_confidence:
            self._ask_a_person(claim, "the cause is unclear" if cause == C.UNCLEAR
                               else f"{cause.value} at confidence {call['confidence']:.2f}")
        elif cause == C.STORE_COUNT_ERROR:
            claim["status"] = "recount_requested"
            claim["actions"].append("recount_requested")
            self._save(claim)
            self._tell(claim, "receipt_recount_requested", {"reason": call["rationale"]})
        elif claim["credit_usd"] > self.config.receiving_credit_approval_usd:
            self._ask_a_person(claim, f"a ${claim['credit_usd']:,.2f} credit is above the "
                                      f"${self.config.receiving_credit_approval_usd:,.0f} approval limit")
        else:
            self._settle(claim, cause)

    def _settle(self, claim: dict[str, Any], cause: C, *, by: str = AGENT) -> None:
        """Credit the client and, for the carrier's fault, hand Corp A the freight claim."""
        if cause in CREDITED and claim["credit_usd"] > 0:
            self._credit(claim, cause)
        if cause in CARRIER:
            notice = claim["evidence"]["ship_notice"]
            h = self.handoffs.create("freight_claim", requesting_party=OwningParty.CORP_A, requested_by=AGENT,
                                     blocked_steps=[], tenant_id=claim["tenant_id"],
                                     context={"order_id": claim["order_id"], "claim_id": claim["claim_id"],
                                              "cause": cause.value, "carrier": notice.get("carrier"),
                                              "pro_number": notice.get("proNumber"), "value_usd": claim["credit_usd"],
                                              "lines": claim["problems"]})
            claim["actions"].append(f"freight_claim:{h.handoff_id}")
        claim["status"], claim["outcome"], claim["settled_by"] = "closed", cause.value, by
        claim["closed_at"] = self.clock.now().isoformat()
        self._save(claim)
        credited = claim["credit_usd"] if cause in CREDITED else 0.0
        self._tell(claim, "receipt_resolved", {"outcome": "credited" if credited else "no_credit",
                                               "cause": cause.value, "credit_usd": credited})
        self.audit.decision(AGENT, "settle_receiving_claim", subject_id=claim["claim_id"], tenant_id=claim["tenant_id"],
                            rationale=claim["call"]["rationale"], order_ids=[claim["order_id"]],
                            skus=[p["upc"] for p in claim["problems"]], owning_party=OwningParty.CORP_A,
                            result={"cause": cause.value, "credit_usd": credited, "actions": claim["actions"],
                                    "decided_by": by})

    def _credit(self, claim: dict[str, Any], cause: C) -> None:
        today = self.clock.now().date()
        units = sum(max(0, p["expected_units"] - p["received_units"]) + p["damaged_units"] for p in claim["problems"])
        charge_id = f"{claim['tenant_id']}|{CREDIT_KIND}|{claim['claim_id']}"
        if self.store.get(CHARGES, charge_id) is None:
            self.store.put(CHARGES, charge_id, {
                "charge_id": charge_id, "tenant_id": claim["tenant_id"], "month": f"{today:%Y-%m}",
                "day": today.isoformat(), "kind": CREDIT_KIND, "ref": claim["client_receipt_id"] or claim["claim_id"],
                "quantity": units, "rate": 0, "amount": -claim["credit_usd"],
                "basis": f"{cause.value.replace('_', ' ')}: {units} units short or damaged, store "
                         f"{claim['store_id']} order {claim['client_order_id']}"})
        claim["actions"].append(f"credit:{claim['credit_usd']:.2f}")

    def _ask_a_person(self, claim: dict[str, Any], why: str) -> None:
        call = claim["call"]
        item = self.approvals.submit(
            category=ApprovalCategory.RECEIVING_CLAIM, owning_party=OwningParty.CORP_A, agent=AGENT,
            subject_id=claim["claim_id"], tenant_id=claim["tenant_id"],
            content={"store": claim["store_id"], "order": claim["client_order_id"],
                     "credit": f"${claim['credit_usd']:,.2f}", "likely_cause": call["cause"],
                     "confidence": round(call["confidence"], 2), "lines": claim["problems"],
                     "carrier_notes": [n["note"] for n in claim["evidence"]["carrier_delivery_notes"]
                                       if n.get("event") == "DELIVERED"],
                     "warehouse_count_gaps": claim["evidence"]["warehouse_counts_since_ship"]["gaps"]},
            rationale=f"Approve to credit the client ${claim['credit_usd']:,.2f} for this delivery"
                      + (" and file a freight claim" if call["cause"] in {c.value for c in CARRIER} else "")
                      + f"; reject for no credit. Asking because {why}. {call['rationale']}")
        claim["status"], claim["approval_id"] = "with_person", item.approval_id
        claim["actions"].append("asked_a_person")
        self._save(claim)
        self._tell(claim, "receipt_under_review", {"reason": why})

    def on_decision(self, event: Event) -> None:
        p = event.payload
        if p.get("originating_agent") != AGENT:
            return
        claim = self.store.get(CLAIMS, p["subject_id"])
        if claim is None or claim.get("approval_id") != p.get("approval_id") or claim["status"] != "with_person":
            return
        if event.type == "approved" and self.approvals.is_executable(claim["approval_id"]):
            cause = C(claim["call"]["cause"])
            self._settle(claim, cause if cause in CREDITED else C.PICK_ERROR, by="approver")
            return
        claim["status"], claim["outcome"] = "closed", "rejected"
        claim["closed_at"] = self.clock.now().isoformat()
        self._save(claim)
        self._tell(claim, "receipt_resolved", {"outcome": "no_credit", "reason": p.get("reason") or "not approved"})
        self.audit.decision(AGENT, "receiving_claim_rejected", subject_id=claim["claim_id"],
                            tenant_id=claim["tenant_id"], rationale=p.get("reason") or "",
                            order_ids=[claim["order_id"]], owning_party=OwningParty.CORP_A)

    # ---------------------------------------------------------------- recount

    def _on_recount(self, r: StoreReceipt, problems: list[dict[str, Any]]) -> None:
        claim = self.store.get(CLAIMS, r.recount_of)
        if claim is None or claim["status"] != "recount_requested":
            self._audit("recount_ignored", r.receipt_id, r, rationale="no claim waiting for this recount")
            return
        if not problems:
            claim["status"], claim["outcome"] = "closed", "recount_matched"
            claim["closed_at"] = self.clock.now().isoformat()
            self._save(claim)
            self._tell(claim, "receipt_resolved", {"outcome": "no_credit", "cause": C.STORE_COUNT_ERROR.value,
                                                   "reason": "the recount matches the ship notice"})
            self._audit("recount_matched", claim["claim_id"], r, rationale="the recount matches the ship notice")
            return
        claim["problems"], claim["credit_usd"] = problems, self.credit_value(problems)
        claim["evidence"]["recount"] = {"received_at": r.received_at.isoformat(), "lines": problems}
        claim["call"] = {**claim["call"], "confidence": 0.0,
                         "rationale": "The store counted again and the delivery is still off: "
                                      + claim["call"]["rationale"]}
        self._ask_a_person(claim, "the store's recount still does not match the ship notice")

    # ---------------------------------------------------------------- helpers

    def _tell(self, claim: dict[str, Any], kind: str, extra: dict[str, Any]) -> None:
        self.bus.publish(Event(topics.OMS_OUTBOUND, kind, {
            "receipt_id": claim["client_receipt_id"], "order_id": claim["client_order_id"],
            "tenant_id": claim["tenant_id"], "store_id": claim["store_id"], **extra},
            self.clock.now(), correlation_id=claim["order_id"]))

    def _audit(self, action: str, subject: str, r: StoreReceipt, *, rationale: str = "",
               result: dict[str, Any] | None = None) -> None:
        self.audit.decision(AGENT, action, subject_id=subject, rationale=rationale, result=result or {},
                            order_ids=[r.order_id], skus=[ln.upc for ln in r.lines], tenant_id=r.tenant_id,
                            owning_party=OwningParty.CORP_A)

    def _save(self, claim: dict[str, Any]) -> None:
        self.store.put(CLAIMS, claim["claim_id"], claim)

    def open_claims(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        out = [c for c in self.store.query(CLAIMS) if c["status"] != "closed"]
        return [c for c in out if tenant_id is None or c["tenant_id"] == tenant_id]
