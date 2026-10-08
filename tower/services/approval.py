"""Approval_Workflow (R8): human-in-the-loop gate for financially impactful actions."""
from __future__ import annotations

import json
from typing import Any

from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import ApprovalCategory, ApprovalItem, ApprovalStatus, OwningParty
from tower.core.ports import Clock, Directory, DocumentStore, Event, MessageBus, Notifier
from tower.core.tenants import TENANT_ONE, party_key

from .approval_links import ApprovalLinks
from .audit import AuditService
from .briefs import BriefWriter

COLLECTION = "approvals"
GOVERNANCE_OWNER = "pilot_governance_owner"


def approver_role(category: ApprovalCategory, party: OwningParty, tenant_id: str | None = None) -> str:
    """approver:<category>:corp_a for Corp A's decisions, approver:<category>:<tenant> for a client's."""
    return f"approver:{category.value}:{party_key(party, tenant_id)}"


def backup_role(category: ApprovalCategory, party: OwningParty, tenant_id: str | None = None) -> str:
    return f"approver_backup:{category.value}:{party_key(party, tenant_id)}"


class ApprovalError(Exception):
    pass


class ApprovalService:
    def __init__(self, store: DocumentStore, bus: MessageBus, directory: Directory, notifier: Notifier,
                 audit: AuditService, clock: Clock, config: PilotConfig, links: ApprovalLinks | None = None,
                 briefs: BriefWriter | None = None):
        self.store, self.bus, self.directory = store, bus, directory
        self.notifier, self.audit, self.clock, self.config = notifier, audit, clock, config
        self.links, self.briefs = links, briefs
        bus.subscribe(topics.TIMERS, self._on_timer, subscription="approval-workflow")

    def get(self, approval_id: str) -> ApprovalItem:
        doc = self.store.get(COLLECTION, approval_id)
        if doc is None:
            raise ApprovalError(f"unknown approval {approval_id}")
        return ApprovalItem.model_validate(doc)

    def can_decide(self, user: str, item: ApprovalItem) -> bool:
        return self.directory.has_role(user, approver_role(item.category, item.owning_party, item.tenant_id))

    def visible_to(self, user: str) -> list[ApprovalItem]:
        """Items this user may decide, newest first: everything still pending plus recent decisions."""
        items = [ApprovalItem.model_validate(d) for d in self.store.query(COLLECTION)]
        mine = [i for i in items if self.can_decide(user, i)]
        return sorted(mine, key=lambda i: i.created_at, reverse=True)

    def _save(self, item: ApprovalItem) -> None:
        self.store.put(COLLECTION, item.approval_id, item.model_dump(mode="json"))

    # ---------------------------------------------------------------- submit and route (R8.1, R8.2, R8.8)

    def submit(self, *, category: ApprovalCategory, owning_party: OwningParty, agent: str, subject_id: str,
               content: dict[str, Any], rationale: str, tenant_id: str = TENANT_ONE) -> ApprovalItem:
        item = ApprovalItem(category=category, owning_party=owning_party, tenant_id=tenant_id,
                            originating_agent=agent, subject_id=subject_id, content=content, rationale=rationale,
                            created_at=self.clock.now())
        item.routed_to = self.directory.principals_with_role(approver_role(category, owning_party, tenant_id))
        if self.briefs is not None:
            item.brief = self.briefs.write(item)
        self._save(item)
        self.audit.decision(agent, "approval_requested", rationale=rationale, subject_id=item.approval_id,
                            result={"category": category.value, "routed_to": item.routed_to,
                                    "brief": (item.brief or {}).get("source")},
                            owning_party=owning_party, tenant_id=tenant_id, **_ids(content))
        if item.routed_to:
            for approver in item.routed_to:
                self.notifier.send(approver, f"Approval needed: {_title(item)}", self._email(approver, item))
        else:
            owner = self.directory.contact(GOVERNANCE_OWNER) or GOVERNANCE_OWNER
            self.notifier.send(owner, "Approval routing failed",
                               f"No approver configured for {category.value}/{party_key(owning_party, tenant_id)}; "
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
        if not self.can_decide(user, item):
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
                         owning_party=item.owning_party, tenant_id=item.tenant_id, **_ids(item.content))
        self.bus.publish(Event(topics.APPROVAL_DECISIONS, item.status.value,
                               {"approval_id": approval_id, "originating_agent": item.originating_agent,
                                "subject_id": item.subject_id, "reason": reason, "tenant_id": item.tenant_id},
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
            self.directory.principals_with_role(backup_role(item.category, item.owning_party, item.tenant_id)))
        for r in sorted(recipients):
            self.notifier.send(r, f"Reminder: approval pending: {_title(item)}", self._email(r, item))
        item.reminders_sent += 1
        self._save(item)
        self.bus.publish(
            Event(topics.TIMERS, "approval_reminder", {"approval_id": item.approval_id}, self.clock.now(),
                  correlation_id=item.approval_id),
            deliver_at=self.clock.now() + self.config.approval_reminder_interval,
        )


    def _email(self, recipient: str, item: ApprovalItem) -> str:
        lines = [f"The {item.originating_agent.replace('_', ' ')} asks for your approval.", "",
                 f"What: {_title(item)}", f"Why: {item.rationale}",
                 f"Owner: {owner_label(item)}", f"Requested: {item.created_at:%Y-%m-%d %H:%M} UTC", ""]
        b = item.brief
        if b:
            lines += ["In short: " + b["summary"], "If you approve: " + b["if_approved"],
                      "If you reject: " + b["if_rejected"]]
            if b.get("suggestion"):
                lines.append(f"Suggested: {b['suggestion'].replace('_', ' ')}. {b.get('suggestion_reason', '')}".rstrip())
            lines.append("")
        if self.links and "@" in recipient:
            lines += ["Approve or reject it here:", self.links.item_url(recipient, item.approval_id), "",
                      "All approvals waiting for you:", self.links.inbox_url(recipient), "",
                      "These links sign you in as you. Don't forward this email."]
        else:
            lines += ["Details:", _summary(item)]
        return "\n".join(lines)


def owner_label(item: ApprovalItem) -> str:
    """'Corp A' for the provider's decisions, else the client's id as a label, e.g. 'Corp B'."""
    return party_key(item.owning_party, item.tenant_id).replace("_", " ").title()


def _title(item: ApprovalItem) -> str:
    return f"{item.category.value.replace('_', ' ').capitalize()} for {item.subject_id}"


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
