"""Billing_Agent (step 8): what each client owes Corp A, charged daily and invoiced monthly.

Charges come from records the tower already keeps, so every charge points at what it bills:

  pick_pack   per order Corp A shipped, plus each unit after the first        (shipment facts, orders)
  shipping    per parcel: Corp A's carrier cost plus the client's markup     (shipment facts)
  freight     per store freight shipment: carrier cost plus markup           (shipment facts, mode ltl)
  storage     per unit on hand at the end of each day, a month's rate / days  (inventory positions)
  returns     per consumer return Corp A received and graded                  (returns)

Rates are Corp A's standard card (PilotConfig.default_rates) with any the client's contract overrides.
Each charge has an id built from what it bills, so running a day twice charges nothing twice.

On the 1st the agent drafts each client's invoice for last month: totals by charge type, a top-up to the
monthly minimum, and the change against the previous invoice, with big moves flagged. Corp A's finance
approver approves it before it goes out; on approval the invoice is filed (Markdown and CSV) and emailed
to the client's billing contact. A rejected invoice is held, with the reason, for Corp A to sort out.
"""
from __future__ import annotations

import calendar
import csv
import io
import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import ApprovalCategory, ApprovalStatus, OwningParty
from tower.core.ports import BlobStore, Clock, Directory, DocumentStore, Event, MessageBus, Notifier
from tower.services.approval import ApprovalService
from tower.services.audit import AuditService

AGENT = "billing_agent"
CHARGES = "charges"
INVOICES = "invoices"
FACTS = "shipment_facts"
KINDS = ("pick_pack", "shipping", "freight", "storage", "returns")
LABELS = {"pick_pack": "Pick and pack", "shipping": "Parcel shipping", "freight": "Store freight",
          "storage": "Storage", "returns": "Returns processing", "minimum": "Monthly minimum top-up"}
log = logging.getLogger("tower.billing")


def month_of(day: date) -> str:
    return f"{day:%Y-%m}"


def previous_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


class BillingAgent:
    def __init__(self, store: DocumentStore, bus: MessageBus, blobs: BlobStore, approvals: ApprovalService,
                 audit: AuditService, notifier: Notifier, directory: Directory, clock: Clock, config: PilotConfig):
        self.store, self.bus, self.blobs, self.approvals = store, bus, blobs, approvals
        self.audit, self.notifier, self.directory, self.clock, self.config = audit, notifier, directory, clock, config
        bus.subscribe(topics.APPROVAL_DECISIONS, self.on_decision, subscription="billing-agent")

    # ---------------------------------------------------------------- daily charges

    def run_daily(self, day: date | None = None) -> dict[str, float]:
        """Charge yesterday's work (or `day`'s). Returns the amount charged per client."""
        day = day or (self.clock.now().date() - timedelta(days=1))
        charges = self._shipments(day) + self._returns(day) + self._storage(day)
        new = [c for c in charges if self.store.get(CHARGES, c["charge_id"]) is None]
        for c in new:
            self.store.put(CHARGES, c["charge_id"], c)
        totals: dict[str, float] = defaultdict(float)
        for c in new:
            totals[c["tenant_id"]] += c["amount"]
        for tid, amount in sorted(totals.items()):
            self.audit.decision(AGENT, "charges_accrued", subject_id=f"{tid}:{day}", tenant_id=tid,
                                owning_party=OwningParty.CORP_A, input_refs=[f"charges:{tid}:{day}"],
                                result={"day": day.isoformat(), "charges": sum(c["tenant_id"] == tid for c in new),
                                        "amount": round(amount, 2)})
        return {t: round(a, 2) for t, a in totals.items()}

    def _charge(self, tid: str, day: date, kind: str, ref: str, quantity: float, rate: float, amount: float,
                basis: str) -> dict[str, Any]:
        return {"charge_id": f"{tid}|{kind}|{ref}", "tenant_id": tid, "month": month_of(day), "day": day.isoformat(),
                "kind": kind, "ref": ref, "quantity": round(quantity, 4), "rate": rate, "amount": round(amount, 2),
                "basis": basis}

    def _shipments(self, day: date) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        orders: dict[str, dict[str, Any] | None] = {}
        for f in self.store.query(FACTS, shipped_on=day.isoformat()):
            oid = f.get("order_id")
            if oid not in orders:
                orders[oid] = self.store.get("orders", oid) if oid else None
            order = orders[oid]
            if order is None:
                continue          # not an order the tower placed (or not yet known): nothing to bill
            tid = order.get("tenant_id") or "corp_b"
            rates = self.config.rates_for(tid)
            cost = float(f.get("fulfillment_cost") or 0)
            kind, markup = ("freight", rates["freight_markup"]) if f.get("mode") == "ltl" else \
                ("shipping", rates["shipping_markup"])
            out.append(self._charge(tid, day, kind, f["package_id"], 1, markup, cost * (1 + markup),
                                    f"carrier cost {cost:.2f} + {markup:.0%}, order {order.get('client_order_id') or oid}"))
            units = sum(int(ln.get("quantity") or 0) for ln in order.get("lines") or [])
            extra = max(0, units - 1)
            amount = rates["pick_pack_order"] + extra * rates["pick_pack_extra_unit"]
            out.append(self._charge(tid, day, "pick_pack", oid, units, rates["pick_pack_order"], amount,
                                    f"1 order, {units} unit(s): {rates['pick_pack_order']:.2f} + {extra} x "
                                    f"{rates['pick_pack_extra_unit']:.2f}"))
        # An order shipped in several packages is picked once: keep its first pick_pack charge only.
        seen, unique = set(), []
        for c in out:
            if c["kind"] == "pick_pack":
                if c["charge_id"] in seen:
                    continue
                seen.add(c["charge_id"])
            unique.append(c)
        return unique

    def _returns(self, day: date) -> list[dict[str, Any]]:
        out = []
        for r in self.store.query("returns"):
            if (r.get("received_at") or "")[:10] != day.isoformat():
                continue
            tid = r.get("tenant_id") or "corp_b"
            rate = self.config.rates_for(tid)["return_processing"]
            out.append(self._charge(tid, day, "returns", r["return_id"], 1, rate, rate,
                                    f"return {r.get('client_return_id') or r['return_id']} received and graded"))
        return out

    def _storage(self, day: date) -> list[dict[str, Any]]:
        units: dict[str, int] = defaultdict(int)
        for s in self.store.query("inventory_state"):
            tid = self.config.tenant_of_sku(s["sku"])
            if tid:
                units[tid] += max(0, int(s.get("on_hand") or 0))
        days = calendar.monthrange(day.year, day.month)[1]
        out = []
        for tid, n in sorted(units.items()):
            rate = self.config.rates_for(tid)["storage_unit_month"]
            out.append(self._charge(tid, day, "storage", day.isoformat(), n, rate, n * rate / days,
                                    f"{n} units on hand x {rate:.2f} a month / {days} days"))
        return out

    # ---------------------------------------------------------------- monthly invoice

    def run_monthly(self, month: str | None = None) -> list[dict[str, Any]]:
        """Draft last month's invoice (or `month`'s, YYYY-MM) for every client with charges, for approval."""
        month = month or previous_month(month_of(self.clock.now().date()))
        drafted = []
        for tid in sorted({c["tenant_id"] for c in self.store.query(CHARGES, month=month)}):
            inv = self.draft(tid, month)
            if inv is not None:
                drafted.append(inv)
        return drafted

    def draft(self, tid: str, month: str) -> dict[str, Any] | None:
        invoice_id = f"INV-{tid.replace('_', '').upper()}-{month.replace('-', '')}"
        existing = self.store.get(INVOICES, invoice_id)
        if existing and existing["status"] in ("pending_approval", "issued"):
            return None                    # already drafted; a held invoice can be drafted again
        charges = self.store.query(CHARGES, tenant_id=tid, month=month)
        if not charges:
            return None
        rates = self.config.rates_for(tid)
        lines = []
        for kind in KINDS:
            mine = [c for c in charges if c["kind"] == kind]
            if mine:
                lines.append({"kind": kind, "label": LABELS[kind], "count": len(mine),
                              "quantity": round(sum(c["quantity"] for c in mine), 2),
                              "amount": round(sum(c["amount"] for c in mine), 2)})
        subtotal = round(sum(ln["amount"] for ln in lines), 2)
        if subtotal < rates["monthly_minimum"] and self._full_month(tid, month):
            lines.append({"kind": "minimum", "label": LABELS["minimum"], "count": 1, "quantity": 1,
                          "amount": round(rates["monthly_minimum"] - subtotal, 2)})
        total = round(sum(ln["amount"] for ln in lines), 2)
        prev = self.store.get(INVOICES, f"INV-{tid.replace('_', '').upper()}-{previous_month(month).replace('-', '')}")
        flags = self._flags(lines, prev)
        name = self.config.tenant(tid).name if self.config.tenant(tid) else tid
        inv = {"invoice_id": invoice_id, "tenant_id": tid, "client": name, "month": month, "lines": lines,
               "total": total, "currency": "USD", "previous_total": prev["total"] if prev else None,
               "flags": flags, "rates": rates, "status": "pending_approval", "drafted_at": self.clock.now().isoformat()}
        change = f"{(total - prev['total']) / prev['total']:+.0%} on last month" if prev and prev["total"] else "first invoice"
        item = self.approvals.submit(
            category=ApprovalCategory.INVOICE, owning_party=OwningParty.CORP_A, agent=AGENT, subject_id=invoice_id,
            tenant_id=tid,
            content={"invoice_id": invoice_id, "client": name, "month": month, "total": f"${total:,.2f}",
                     "previous_total": f"${prev['total']:,.2f}" if prev else "none",
                     "lines": [{"charge": ln["label"], "count": ln["count"], "amount": f"${ln['amount']:,.2f}"}
                               for ln in lines], "flags": flags},
            rationale=f"{name}'s invoice for {month}: ${total:,.2f} ({change})"
                      + (f"; {len(flags)} item(s) flagged" if flags else "") + ".")
        inv["approval_id"] = item.approval_id
        self.store.put(INVOICES, invoice_id, inv)
        return inv

    def _full_month(self, tid: str, month: str) -> bool:
        """The minimum applies from a client's first full month."""
        rec = self.store.get("clients", tid)
        activated = (rec or {}).get("activated_at")
        return not activated or activated[:7] < month

    def _flags(self, lines: list[dict[str, Any]], prev: dict[str, Any] | None) -> list[str]:
        if not prev:
            return []
        before = {ln["kind"]: ln["amount"] for ln in prev["lines"]}
        flags = []
        for ln in lines:
            old = before.get(ln["kind"])
            if old and abs(ln["amount"] - old) / old >= self.config.invoice_change_flag and abs(ln["amount"] - old) >= 100:
                flags.append(f"{ln['label']} went from ${old:,.2f} to ${ln['amount']:,.2f}")
        for kind, old in before.items():
            if kind != "minimum" and kind not in {ln["kind"] for ln in lines} and old >= 100:
                flags.append(f"no {LABELS[kind].lower()} charges this month (${old:,.2f} last month)")
        return flags

    # ---------------------------------------------------------------- the approver's decision

    def on_decision(self, event: Event) -> None:
        p = event.payload
        if p.get("originating_agent") != AGENT:
            return
        inv = self.store.get(INVOICES, p["subject_id"])
        if inv is None or inv.get("approval_id") != p["approval_id"] or inv["status"] != "pending_approval":
            return
        item = self.approvals.get(p["approval_id"])
        inv["decided_by"], inv["decided_at"] = item.decided_by, item.decided_at.isoformat() if item.decided_at else None
        if item.status == ApprovalStatus.APPROVED:
            self.issue(inv)
        elif item.status == ApprovalStatus.REJECTED:
            inv["status"], inv["held_reason"] = "held", item.reason
            self.store.put(INVOICES, inv["invoice_id"], inv)
            self.audit.decision(AGENT, "invoice_held", subject_id=inv["invoice_id"], rationale=item.reason or "",
                                owning_party=OwningParty.CORP_A, tenant_id=inv["tenant_id"])

    def issue(self, inv: dict[str, Any]) -> None:
        tid, iid = inv["tenant_id"], inv["invoice_id"]
        text = self.render(inv)
        md = self.blobs.write("reports", f"invoices/{tid}/{iid}.md", text.encode(), content_type="text/markdown")
        self.blobs.write("reports", f"invoices/{tid}/{iid}.csv", self.charges_csv(tid, inv["month"]).encode(),
                         content_type="text/csv")
        inv["status"], inv["issued_at"], inv["document"] = "issued", self.clock.now().isoformat(), md
        self.store.put(INVOICES, iid, inv)
        self.audit.decision(AGENT, "invoice_issued", subject_id=iid, owning_party=OwningParty.CORP_A, tenant_id=tid,
                            input_refs=[f"approval:{inv.get('approval_id')}"], result={"total": inv["total"]})
        to = self.directory.contact(f"{tid}_billing") or self.directory.contact(f"{tid}_ops") or f"{tid}_billing"
        self.notifier.send(to, f"Corp A invoice {iid} for {inv['month']}: ${inv['total']:,.2f}", text)

    def render(self, inv: dict[str, Any]) -> str:
        y, m = map(int, inv["month"].split("-"))
        lines = [f"Invoice {inv['invoice_id']}", "From: Corp A fulfillment services", f"To: {inv['client']}",
                 f"Period: {calendar.month_name[m]} {y}", "",
                 "| Charge | Count | Amount |", "|---|---:|---:|"]
        lines += [f"| {ln['label']} | {ln['count']:,} | ${ln['amount']:,.2f} |" for ln in inv["lines"]]
        lines += [f"| **Total** | | **${inv['total']:,.2f}** |", "",
                  "Every charge is listed in the CSV that comes with this invoice: the order, parcel, return or day "
                  "it bills, and how it was worked out.", "", "Rates:"]
        lines += [f"- {k.replace('_', ' ')}: {v:g}" for k, v in inv["rates"].items()]
        return "\n".join(lines) + "\n"

    def charges_csv(self, tid: str, month: str) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["day", "charge", "reference", "quantity", "rate", "amount_usd", "how"])
        for c in sorted(self.store.query(CHARGES, tenant_id=tid, month=month), key=lambda c: (c["day"], c["kind"], c["ref"])):
            w.writerow([c["day"], c["kind"], c["ref"], c["quantity"], c["rate"], f"{c['amount']:.2f}", c["basis"]])
        return buf.getvalue()

    # ---------------------------------------------------------------- for the copilot and the console

    def summary(self, tid: str, month: str | None = None) -> dict[str, Any]:
        month = month or month_of(self.clock.now().date())
        charges = self.store.query(CHARGES, tenant_id=tid, month=month)
        by_kind: dict[str, float] = defaultdict(float)
        for c in charges:
            by_kind[c["kind"]] += c["amount"]
        invoices = sorted((i for i in self.store.query(INVOICES, tenant_id=tid)), key=lambda i: i["month"], reverse=True)
        return {"month": month, "charged_so_far": round(sum(by_kind.values()), 2),
                "by_charge": {LABELS[k]: round(v, 2) for k, v in sorted(by_kind.items())},
                "invoices": [{"invoice_id": i["invoice_id"], "month": i["month"], "total": i["total"],
                              "status": i["status"]} for i in invoices[:3]]}
