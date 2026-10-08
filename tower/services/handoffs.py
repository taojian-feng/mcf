"""Owning_Party attribution and Cross_Boundary_Handoff tracking (R11)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import Handoff, HandoffStatus, OwningParty
from tower.core.ports import Clock, Directory, DocumentStore, Event, MessageBus, Notifier
from tower.core.tenants import TENANT_ONE, party_key

from .audit import AuditService

COLLECTION = "handoffs"
PROGRAM_LEAD = "pilot_program_lead"


def ops_contact(party: OwningParty, tenant_id: str | None = None) -> str:
    """corp_a_ops for Corp A's work, <tenant>_ops for a client's."""
    return f"{party_key(party, tenant_id)}_ops"


def escalation_contact(party: OwningParty, tenant_id: str | None = None) -> str:
    return f"{party_key(party, tenant_id)}_escalation"


class HandoffService:
    def __init__(self, store: DocumentStore, bus: MessageBus, directory: Directory, notifier: Notifier,
                 audit: AuditService, clock: Clock, config: PilotConfig):
        self.store, self.bus, self.directory = store, bus, directory
        self.notifier, self.audit, self.clock, self.config = notifier, audit, clock, config
        bus.subscribe(topics.TIMERS, self._on_timer, subscription="handoff-tracker")
        bus.subscribe(topics.HANDOFF_UPDATES, self._on_update, subscription="handoff-tracker")

    # ---------------------------------------------------------------- attribution (R11.1, R11.6)

    def owning_party(self, action: str, *, actor: str) -> OwningParty | None:
        party = self.config.responsibility_matrix.get(action)
        if party is None:
            lead = self.directory.contact(PROGRAM_LEAD) or PROGRAM_LEAD
            self.notifier.send(lead, "Unmapped process action",
                               f"'{action}' by {actor} has no Owning_Party in the responsibility matrix",
                               severity="high")
        return party

    # ---------------------------------------------------------------- lifecycle (R11.2-R11.4, R11.7, R11.8)

    def create(self, action: str, *, requesting_party: OwningParty, requested_by: str,
               blocked_steps: list[str], context: dict[str, Any], due_at: datetime | None = None,
               tenant_id: str = TENANT_ONE) -> Handoff:
        responsible = self.owning_party(action, actor=requested_by)
        if responsible is None:
            raise ValueError(f"no owning party for {action}")
        now = self.clock.now()
        h = Handoff(action=action, requesting_party=requesting_party, responsible_party=responsible,
                    tenant_id=tenant_id, created_at=now, due_at=due_at or now + self.config.handoff_default_due,
                    blocked_steps=blocked_steps, context=context)
        self._save(h)
        self.audit.decision(requested_by, "handoff_created", subject_id=h.handoff_id,
                            result={"action": action, "responsible": responsible.value,
                                    "due_at": h.due_at.isoformat()},
                            owning_party=requesting_party, tenant_id=tenant_id, order_ids=_order(context))
        ops = ops_contact(responsible, tenant_id)
        self.notifier.send(self.directory.contact(ops) or ops,
                           f"Action requested: {action}", f"handoff {h.handoff_id} due {h.due_at.isoformat()}")
        self._timer("handoff_ack_check", h, now + self.config.handoff_ack_window)
        self._timer("handoff_due_check", h, h.due_at)
        self.bus.publish(Event(topics.HANDOFF_EVENTS, "created",
                               {"handoff_id": h.handoff_id, "action": action, "responsible_party": responsible.value,
                                "tenant_id": tenant_id, "due_at": h.due_at.isoformat(), "context": context}, now,
                               correlation_id=context.get("case_id") or context.get("order_id")))
        return h

    def _on_update(self, event: Event) -> None:
        """The responsible party reports progress (for Corp A: through the partner gateway)."""
        if not self.store.get(COLLECTION, event.payload.get("handoff_id") or ""):
            return
        by = event.payload.get("by") or "partner"
        if event.type == "acknowledged":
            self.acknowledge(event.payload["handoff_id"], by=by)
        elif event.type == "completed":
            self.acknowledge(event.payload["handoff_id"], by=by)
            self.complete(event.payload["handoff_id"], by=by, result=event.payload.get("result"))

    def acknowledge(self, handoff_id: str, *, by: str) -> Handoff:
        h = self.get(handoff_id)
        if h.status == HandoffStatus.OPEN:
            h.status, h.acknowledged_at = HandoffStatus.ACKNOWLEDGED, self.clock.now()
            self._save(h)
            self.audit.human(by, "handoff_acknowledged", subject_id=handoff_id, reason="",
                             owning_party=h.responsible_party, tenant_id=h.tenant_id)
        return h

    def complete(self, handoff_id: str, *, by: str, result: dict[str, Any] | None = None) -> Handoff:
        h = self.get(handoff_id)
        if h.status in (HandoffStatus.COMPLETED, HandoffStatus.CANCELLED):
            return h
        h.status, h.completed_at = HandoffStatus.COMPLETED, self.clock.now()
        self._save(h)
        self.audit.human(by, "handoff_completed", subject_id=handoff_id, reason="",
                         result=result or {}, owning_party=h.responsible_party, tenant_id=h.tenant_id,
                         order_ids=_order(h.context))
        self.bus.publish(Event(topics.HANDOFF_EVENTS, "completed",
                               {"handoff_id": handoff_id, "action": h.action, "context": h.context,
                                "tenant_id": h.tenant_id, "result": result or {}}, self.clock.now(),
                               correlation_id=h.context.get("case_id") or h.context.get("order_id")))
        return h

    def cancel(self, handoff_id: str, *, by: str, reason: str) -> Handoff:
        h = self.get(handoff_id)
        if h.status not in (HandoffStatus.COMPLETED, HandoffStatus.CANCELLED):
            h.status = HandoffStatus.CANCELLED
            self._save(h)
            self.audit.decision(by, "handoff_cancelled", subject_id=handoff_id, rationale=reason, tenant_id=h.tenant_id)
        return h

    # ---------------------------------------------------------------- view (R11.5)

    def open_view(self) -> list[dict[str, Any]]:
        now = self.clock.now()
        out = []
        for status in (HandoffStatus.OPEN, HandoffStatus.ACKNOWLEDGED):
            for doc in self.store.query(COLLECTION, status=status.value):
                h = Handoff.model_validate(doc)
                out.append({"handoff_id": h.handoff_id, "action": h.action, "status": h.status.value,
                            "responsible_party": h.responsible_party.value, "tenant_id": h.tenant_id,
                            "age_hours": round((now - h.created_at).total_seconds() / 3600, 2),
                            "due_at": h.due_at.isoformat(), "blocked_steps": h.blocked_steps})
        return sorted(out, key=lambda r: -r["age_hours"])

    # ---------------------------------------------------------------- internals

    def get(self, handoff_id: str) -> Handoff:
        return Handoff.model_validate(self.store.get(COLLECTION, handoff_id))

    def _save(self, h: Handoff) -> None:
        self.store.put(COLLECTION, h.handoff_id, h.model_dump(mode="json"))

    def _timer(self, kind: str, h: Handoff, at: datetime) -> None:
        self.bus.publish(Event(topics.TIMERS, kind, {"handoff_id": h.handoff_id}, self.clock.now(),
                               correlation_id=h.handoff_id), deliver_at=at)

    def _on_timer(self, event: Event) -> None:
        if event.type not in ("handoff_ack_check", "handoff_due_check"):
            return
        h = self.get(event.payload["handoff_id"])
        if event.type == "handoff_ack_check" and h.status != HandoffStatus.OPEN:
            return
        if event.type == "handoff_due_check" and h.status in (HandoffStatus.COMPLETED, HandoffStatus.CANCELLED):
            return
        why = "not acknowledged" if event.type == "handoff_ack_check" else "overdue"
        contact = escalation_contact(h.responsible_party, h.tenant_id)
        self.notifier.send(self.directory.contact(contact) or contact, f"Handoff {why}: {h.action}",
                           f"handoff {h.handoff_id} created {h.created_at.isoformat()}, due {h.due_at.isoformat()}",
                           severity="high")
        h.escalations.append(f"{why}@{self.clock.now().isoformat()}")
        self._save(h)
        self.audit.decision("handoff_tracker", f"handoff_escalated_{why.replace(' ', '_')}",
                            subject_id=h.handoff_id, owning_party=h.responsible_party, tenant_id=h.tenant_id,
                            order_ids=_order(h.context))


def _order(context: dict[str, Any]) -> list[str]:
    return [context["order_id"]] if context.get("order_id") else []
