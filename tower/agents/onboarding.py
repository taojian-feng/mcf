"""Onboarding_Agent (step 8): takes on a new retail client, from application to first order.

  apply      Corp A's onboarding person fills in the client's application (console /clients/new). The
             agent checks it against Corp A's network: promises the network can keep, delivery area,
             channels, capacity left after today's real volume, rates against the floor, and contacts.
             A blocker stops it there; otherwise Corp A's onboarding approver gets an approval request
             with the checks, the network load before and after, and a brief.
  activate   On approval the client's contract and people go into the client registry, its keys are
             derived (services/client_keys.py), and its ops contact gets a welcome email saying how to
             send its catalog and orders. Every agent, the console and the gateway pick it up from the
             registry within minutes, with no deploy.
  go-live    A daily check marks when the catalog arrives and when the first order is accepted, says
             so when the client is live, and reminds the client when its catalog is overdue.

The agent never activates a client on its own: activation runs only on an approved request.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from tower.core import topics
from tower.core.config import US48, PilotConfig
from tower.core.models import ApprovalCategory, ApprovalStatus, OwningParty
from tower.core.ports import Clock, Directory, DocumentStore, Event, MessageBus, Notifier
from tower.core.tenants import Tenant
from tower.services.approval import ApprovalService
from tower.services.audit import AuditService
from tower.services.clients import COLLECTION, ClientApplication, ClientRecord

AGENT = "onboarding_agent"
CHANNELS = {"online", "store"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
log = logging.getLogger("tower.onboarding")


def tenant_id_for(name: str) -> str:
    tid = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not tid or not tid[0].isalpha():
        tid = f"client_{tid}".rstrip("_")
    return tid[:31]


class OnboardingAgent:
    def __init__(self, store: DocumentStore, bus: MessageBus, approvals: ApprovalService, audit: AuditService,
                 notifier: Notifier, directory: Directory, clock: Clock, config: PilotConfig,
                 gateway_url: str | None = None):
        self.store, self.bus, self.approvals, self.audit = store, bus, approvals, audit
        self.notifier, self.directory, self.clock, self.config = notifier, directory, clock, config
        self.gateway_url = gateway_url
        bus.subscribe(topics.APPROVAL_DECISIONS, self.on_decision, subscription="onboarding-agent")

    # ---------------------------------------------------------------- records

    def get(self, tenant_id: str) -> ClientRecord | None:
        doc = self.store.get(COLLECTION, tenant_id)
        return ClientRecord.model_validate(doc) if doc else None

    def all(self) -> list[ClientRecord]:
        recs = [ClientRecord.model_validate(d) for d in self.store.query(COLLECTION)]
        return sorted(recs, key=lambda r: r.submitted_at, reverse=True)

    def _save(self, rec: ClientRecord) -> None:
        self.store.put(COLLECTION, rec.tenant_id, rec.model_dump(mode="json"))
        invalidate = getattr(self.directory, "invalidate", None)
        if invalidate:
            invalidate()

    # ---------------------------------------------------------------- apply

    def apply(self, app: ClientApplication, submitted_by: str) -> ClientRecord:
        tid = app.tenant_id or tenant_id_for(app.name)
        app = app.model_copy(update={
            "tenant_id": tid,
            "channels": sorted(set(app.channels)),
            "promise_days": sorted(set(app.promise_days)),
            "delivery_states": sorted({s.strip().upper() for s in app.delivery_states}) if app.delivery_states else None})
        blockers, warnings, load = self.check(app)
        rec = ClientRecord(tenant_id=tid, name=app.name, status="blocked" if blockers else "pending_approval",
                           application=app, blockers=blockers, warnings=warnings, load=load,
                           submitted_by=submitted_by, submitted_at=self.clock.now())
        if blockers:
            if not self._taken(tid):        # never overwrite another client's record with a blocked one
                self._save(rec)
            self.audit.decision(AGENT, "client_application_blocked", rationale="; ".join(blockers),
                                subject_id=tid, owning_party=OwningParty.CORP_A, tenant_id=tid,
                                result={"blockers": blockers, "warnings": warnings})
            return rec
        rates = self.config.default_rates | app.rates
        item = self.approvals.submit(
            category=ApprovalCategory.CLIENT_ONBOARDING, owning_party=OwningParty.CORP_A, agent=AGENT,
            subject_id=tid, tenant_id=tid,
            content={"client": app.name, "tenant_id": tid, "channels": app.channels, "promise_days": app.promise_days,
                     "delivery_states": app.delivery_states or "US 48", "return_window_days": app.return_window_days,
                     "expected_orders_per_day": app.expected_orders_per_day, "expected_skus": app.expected_skus,
                     "network_orders_per_day_now": load["current"],
                     "network_orders_per_day_after": load["after"],
                     "network_capacity_orders_per_day": load["capacity"],
                     "network_load_after": f"{load['after_share']:.0%}",
                     "rates": rates, "negotiated_rates": app.rates, "warnings": warnings,
                     "client_approver": app.approver_email, "client_ops": app.ops_email,
                     "updates_to": app.outbound_url or "none", "submitted_by": submitted_by, "notes": app.notes},
            rationale=(f"Take on {app.name} as a new client: about {app.expected_orders_per_day} orders a day "
                       f"would bring the network to {load['after_share']:.0%} of capacity"
                       + (f"; {len(warnings)} warning(s) to weigh" if warnings else "") + "."))
        rec.approval_id = item.approval_id
        self._save(rec)
        return rec

    def _taken(self, tid: str) -> bool:
        if tid in self.config.tenants:
            return True
        existing = self.get(tid)
        return existing is not None and existing.status not in ("blocked", "rejected")

    def check(self, app: ClientApplication) -> tuple[list[str], list[str], dict[str, Any]]:
        """Blockers stop an application; warnings go to the approver."""
        c, tid = self.config, app.tenant_id or ""
        blockers: list[str] = []
        warnings: list[str] = []
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", tid):
            blockers.append(f"client id {tid!r} must be 2-31 lowercase letters, digits or underscores")
        elif self._taken(tid):
            blockers.append(f"client id {tid} is already taken")
        prefix = f"{tid.replace('_', '').upper()}-"
        if any(t.order_prefix == prefix for t in c.tenants.values()):
            blockers.append(f"order prefix {prefix} is already used by another client")
        if not app.channels or set(app.channels) - CHANNELS:
            blockers.append(f"channels must be online and/or store, not {', '.join(app.channels) or 'none'}")
        bad_days = sorted(set(app.promise_days) - set(c.provider_promise_days))
        if not app.promise_days or bad_days:
            blockers.append(f"Corp A's network can promise {', '.join(map(str, c.provider_promise_days))} days"
                            + (f", not {', '.join(map(str, bad_days))}" if bad_days else "; pick at least one"))
        if app.delivery_states is not None:
            outside = sorted(set(app.delivery_states) - US48)
            if outside:
                blockers.append(f"Corp A delivers to the US 48 states and DC only, not {', '.join(outside)}")
            elif not app.delivery_states:
                blockers.append("the delivery area is empty")
        for label, address in (("approver", app.approver_email), ("ops", app.ops_email),
                               ("billing", app.billing_email or app.ops_email)):
            if not EMAIL.match(address or ""):
                blockers.append(f"the {label} email {address!r} isn't an email address")
        if app.outbound_url and not app.outbound_url.startswith("https://"):
            blockers.append("the client's update endpoint must use https")
        if not app.outbound_url:
            warnings.append("no update endpoint: the client won't hear about rejections, tracking or ship notices")
        unknown = sorted(set(app.rates) - set(c.default_rates))
        if unknown:
            blockers.append(f"unknown rates: {', '.join(unknown)}")
        for name, value in app.rates.items():
            std = c.default_rates.get(name)
            if std is None:
                continue
            if value < 0 or value < std * c.rate_floor_share:
                blockers.append(f"{name} {value:g} is below the floor of {std * c.rate_floor_share:g}")
            elif value < std:
                warnings.append(f"{name} {value:g} is {1 - value / std:.0%} below the standard {std:g}")
        if app.return_window_days > 60:
            warnings.append(f"a {app.return_window_days}-day return window is longer than usual (30)")

        current = self.network_volume()
        after = current + app.expected_orders_per_day
        cap = c.network_capacity_orders_per_day
        load = {"current": round(current, 1), "after": round(after, 1), "capacity": cap,
                "after_share": after / cap if cap else 1.0}
        if after > cap:
            blockers.append(f"the network would need {after:.0f} orders a day, over its capacity of {cap}")
        elif after > cap * c.capacity_warning_share:
            warnings.append(f"the network would run at {after / cap:.0%} of capacity")
        return blockers, warnings, load

    def network_volume(self, days: int = 7) -> float:
        """Orders a day across every client, from the last week's real orders."""
        since = (self.clock.now() - timedelta(days=days)).isoformat()
        placed = [o for o in self.store.query("orders") if (o.get("placed_at") or "") >= since]
        return len(placed) / days

    # ---------------------------------------------------------------- the approver's decision

    def on_decision(self, event: Event) -> None:
        p = event.payload
        if p.get("originating_agent") != AGENT:
            return
        rec = self.get(p["subject_id"])
        if rec is None or rec.approval_id != p["approval_id"] or rec.status != "pending_approval":
            return  # an old request, or already handled
        item = self.approvals.get(p["approval_id"])
        rec.decided_by, rec.decided_at, rec.decision_reason = item.decided_by, item.decided_at, item.reason
        if item.status == ApprovalStatus.APPROVED:
            self.activate(rec)
        elif item.status == ApprovalStatus.REJECTED:
            rec.status = "rejected"
            self._save(rec)
            self.audit.decision(AGENT, "client_application_rejected", rationale=item.reason or "",
                                subject_id=rec.tenant_id, owning_party=OwningParty.CORP_A, tenant_id=rec.tenant_id)
            self.notifier.send(rec.submitted_by, f"{rec.name} was not taken on",
                               f"{item.decided_by} rejected the application for {rec.name}: {item.reason}")

    def activate(self, rec: ClientRecord) -> Tenant:
        app = rec.application
        tenant = Tenant(tenant_id=rec.tenant_id, name=app.name, channels=app.channels, promise_days=app.promise_days,
                        eligible_states=set(app.delivery_states) if app.delivery_states else None,
                        return_window_days=app.return_window_days, outbound_url=app.outbound_url,
                        keys="issued", rates=app.rates)
        rec.contract = tenant.model_dump(mode="json")
        rec.status, rec.activated_at = "active", self.clock.now()
        self._save(rec)
        # This process serves the client at once; the others pick it up from the registry within minutes.
        self.config.tenants = {**self.config.tenants, rec.tenant_id: tenant}
        self.audit.decision(AGENT, "client_activated", subject_id=rec.tenant_id, input_refs=[f"approval:{rec.approval_id}"],
                            rationale=f"approved by {rec.decided_by}", owning_party=OwningParty.CORP_A,
                            tenant_id=rec.tenant_id,
                            result={"order_prefix": tenant.order_prefix, "feed_folder": tenant.feed_folder,
                                    "channels": tenant.channels})
        log.info("client %s activated", rec.tenant_id)
        self.notifier.send(app.ops_email, f"Welcome to Corp A, {app.name}", self.welcome(rec, tenant))
        self.notifier.send(rec.submitted_by, f"{app.name} is set up",
                           f"{rec.decided_by} approved {app.name}. Its API key and webhook key are on the client's "
                           "page in the approval console; share them with the client over a secure channel. "
                           "The tower will tell you when its catalog arrives and its first order goes through.")
        return tenant

    def welcome(self, rec: ClientRecord, tenant: Tenant) -> str:
        base = self.gateway_url or "<the gateway address Corp A gives you>"
        lines = [f"Hello {tenant.name} team,", "",
                 "Corp A has set up your fulfillment account. To start:", "",
                 f"1. Send your item catalog: POST {base}/v1/catalog with your items (UPC, title, unit cost).",
                 f"2. Send orders: POST {base}/v1/orders"
                 + (f" and store orders to {base}/v1/store-orders" if "store" in tenant.channels else "") + ".",
                 f"3. Send consumer returns: POST {base}/v1/returns." if tenant.return_window_days else None,
                 "", "Put your API key in the X-Api-Key header of every call. Corp A shares it with you "
                 "separately; it never comes by email.",
                 (f"Order updates go to {tenant.outbound_url}/oms/v1/events with your webhook key in X-Api-Key."
                  if tenant.outbound_url else "You have no update endpoint yet; tell Corp A when you have one."),
                 "", f"Your contract: promises of {', '.join(map(str, tenant.promise_days))} days, "
                 f"{', '.join(tenant.channels)} orders, returns within {tenant.return_window_days} days of delivery.",
                 f"Your order numbers appear at Corp A as {tenant.order_prefix}<your order number>."]
        return "\n".join(x for x in lines if x is not None)

    # ---------------------------------------------------------------- go-live (daily)

    def run_daily(self) -> list[str]:
        """Mark catalog and first order for clients set up recently; remind those whose catalog is overdue."""
        now, changed = self.clock.now(), []
        for rec in self.all():
            if rec.status != "active":
                continue
            tid = rec.tenant_id
            if rec.catalog_at is None and self.config.catalog(tid):
                rec.catalog_at = now
            if rec.first_order_at is None and self.store.query("orders", tenant_id=tid):
                rec.first_order_at = now
            if rec.catalog_at and rec.first_order_at:
                rec.status = "live"
                self.audit.decision(AGENT, "client_live", subject_id=tid, owning_party=OwningParty.CORP_A,
                                    tenant_id=tid)
                for to in (rec.submitted_by, rec.application.ops_email):
                    self.notifier.send(to, f"{rec.name} is live",
                                       f"{rec.name}'s catalog is loaded and its first order went through.")
            elif rec.catalog_at is None and now - (rec.nudged_at or rec.activated_at or now) >= self.config.onboarding_nudge_after:
                rec.nudged_at = now
                self.notifier.send(rec.application.ops_email, f"{rec.name}: we're waiting for your catalog",
                                   "Corp A can't accept your orders until your item catalog arrives. "
                                   + (f"Send it to {self.gateway_url}/v1/catalog. " if self.gateway_url else "")
                                   + "Reply to your Corp A contact if something is in the way.")
                self.notifier.send(rec.submitted_by, f"{rec.name} hasn't sent its catalog",
                                   f"{rec.name} was set up on {rec.activated_at:%Y-%m-%d} and its catalog hasn't arrived. "
                                   "The client has been reminded.")
            else:
                continue
            self._save(rec)
            changed.append(tid)
        return changed


def is_onboarding_approver(directory: Directory, user: str) -> bool:
    from tower.services.approval import approver_role
    return directory.has_role(user, approver_role(ApprovalCategory.CLIENT_ONBOARDING, OwningParty.CORP_A))
