"""Approval_Workflow (R8): human-in-the-loop gate for financially impactful actions."""
from __future__ import annotations

import json
from typing import Any

from mcf.core import topics
from mcf.core.config import PilotConfig
from mcf.core.models import ApprovalCategory, ApprovalItem, ApprovalStatus, OwningParty
from mcf.core.ports import Clock, Directory, DocumentStore, Event, MessageBus, Notifier

from .audit import AuditService

COLLECTION = "approvals"
GOVERNANCE_OWNER = "pilot_governance_owner"


def approver_role(category: ApprovalCategory, party: OwningParty) -> str:
    return f"approver:{category.value}:{party.value}"


def backup_role(category: ApprovalCategory, party: OwningParty) -> str:
    return f"approver_backup:{category.value}:{party.value}"


class ApprovalError(Exception):
    pass


class ApprovalService:
    def __init__(self, store: DocumentStore, bus: MessageBus, directory: Directory, notifier: Notifier,
                 audit: AuditService, clock: Clock, config: PilotConfig):
        self.store, self.bus, self.directory = store, bus, directory
        self.notifier, self.audit, self.clock, self.config = notifier, audit, clock, config
        bus.subscribe(topics.TIMERS, self._on_timer, subscription="approval-workflow")

    def get(self, approval_id: str) -> ApprovalItem:
        doc = self.store.get(COLLECTION, approval_id)
        if doc is None:
            raise ApprovalError(f"unknown approval {approval_id}")
        return ApprovalItem.model_validate(doc)

    def _save(self, item: ApprovalItem) -> None:
        self.store.put(COLLECTION, item.approval_id, item.model_dump(mode="json"))

    # ---------------------------------------------------------------- submit and route (R8.1, R8.2, R8.8)

    def submit(self, *, category: ApprovalCategory, owning_party: OwningParty, agent: str, subject_id: str,
               content: dict[str, Any], rationale: str) -> ApprovalItem:
        item = ApprovalItem(category=category, owning_party=owning_party, originating_agent=agent,
                            subject_id=subject_id, content=content, rationale=rationale,
                            created_at=self.clock.now())
        item.routed_to = self.directory.principals_with_role(approver_role(category, owning_party))
        self._save(item)
        self.audit.decision(agent, "approval_requested", rationale=rationale, subject_id=item.approval_id,
                            result={"category": category.value, "routed_to": item.routed_to},
                            owning_party=owning_party, **_ids(content))
        if item.routed_to:
            for approver in item.routed_to:
                self.notifier.send(approver, f"Approval needed: {category.value}",
                                   _summary(item))
        else:
            owner = self.directory.contact(GOVERNANCE_OWNER) or GOVERNANCE_OWNER
            self.notifier.send(owner, "Approval routing failed",
                               f"No approver configured for {category.value}/{owning_party.value}; "
                               f"item {item.approval_id} is held pending.", severity="high")
        self.bus.publish(
            Event(topics.TIMERS, "approval_reminder", {"approval_id": item.approval_id}, self.clock.now(),
                  correlation_id=item.approval_id),
            deliver_at=self.clock.now() + self.config.approval_first_reminder,
        )
        return item

    def is_executable(self, approval_id: str) -> bool:
        return self.get(approval_id).status == ApprovalStatus.APPROVED

    # ---------------------------------------------------------------- decide (R8.3, R8.4, R8.6, R8.7)

    def decide(self, user: str, approval_id: str, *, approve: bool, reason: str = "") -> ApprovalItem:
        item = self.get(approval_id)
        decision = "approve" if approve else "reject"
        if not self.directory.has_role(user, approver_role(item.category, item.owning_party)):
            self.audit.denied(user, f"approval_{decision}_denied", subject_id=approval_id,
                              rationale="user lacks approver role for category and owning party")
            raise PermissionError(f"{user} may not {decision} {approval_id}")
        if item.status != ApprovalStatus.PENDING:
            raise ApprovalError(f"{approval_id} is already {item.status.value}")
        if not approve and not reason:
            raise ApprovalError("a rejection reason is required")
        item.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        item.decided_by, item.decided_at, item.reason = user, self.clock.now(), reason
        self._save(item)
        self.audit.human(user, decision, subject_id=approval_id, reason=reason,
                         owning_party=item.owning_party, **_ids(item.content))
        self.bus.publish(Event(topics.APPROVAL_DECISIONS, item.status.value,
                               {"approval_id": approval_id, "originating_agent": item.originating_agent,
                                "subject_id": item.subject_id, "reason": reason},
                               self.clock.now(), correlation_id=item.subject_id))
        return item

    # ---------------------------------------------------------------- reminders (R8.5)

    def _on_timer(self, event: Event) -> None:
        if event.type != "approval_reminder":
            return
        item = self.get(event.payload["approval_id"])
        if item.status != ApprovalStatus.PENDING:
            return
        recipients = set(item.routed_to) | set(
            self.directory.principals_with_role(backup_role(item.category, item.owning_party)))
        for r in sorted(recipients):
            self.notifier.send(r, f"Reminder: approval pending {item.approval_id}", _summary(item))
        item.reminders_sent += 1
        self._save(item)
        self.bus.publish(
            Event(topics.TIMERS, "approval_reminder", {"approval_id": item.approval_id}, self.clock.now(),
                  correlation_id=item.approval_id),
            deliver_at=self.clock.now() + self.config.approval_reminder_interval,
        )


def _ids(content: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "order_ids": [content["order_id"]] if content.get("order_id") else [],
        "skus": list(content.get("skus", [])),
        "node_ids": list(content.get("node_ids", [])),
    }


def _summary(item: ApprovalItem) -> str:
    return json.dumps({"approval_id": item.approval_id, "category": item.category.value,
                       "subject": item.subject_id, "rationale": item.rationale, "content": item.content},
                      indent=2, default=str)
