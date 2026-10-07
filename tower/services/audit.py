"""Audit_Service (R10): one append path for every agent decision and human action."""
from __future__ import annotations

import json
from typing import Any, Iterable

from tower.core.models import AuditKind, AuditRecord, OwningParty
from tower.core.ports import AuditLedger, BlobStore, Clock, Notifier

OPS_TEAM = "ops-team"


class AuditService:
    def __init__(self, ledger: AuditLedger, blobs: BlobStore, notifier: Notifier, clock: Clock):
        self.ledger = ledger
        self.blobs = blobs
        self.notifier = notifier
        self.clock = clock
        self.pending: list[AuditRecord] = []  # R10.6: preserved for recovery

    def record(
        self,
        kind: AuditKind,
        actor: str,
        action: str,
        *,
        rationale: str = "",
        input_refs: Iterable[str] = (),
        result: dict[str, Any] | None = None,
        order_ids: Iterable[str] = (),
        skus: Iterable[str] = (),
        node_ids: Iterable[str] = (),
        owning_party: OwningParty | None = None,
        subject_id: str | None = None,
    ) -> AuditRecord:
        rec = AuditRecord(
            kind=kind, at=self.clock.now(), actor=actor, action=action, rationale=rationale,
            input_refs=list(input_refs), result=result or {}, order_ids=list(order_ids),
            skus=list(skus), node_ids=list(node_ids), owning_party=owning_party, subject_id=subject_id,
        )
        try:
            self.ledger.append(rec)
        except Exception as exc:
            self._preserve(rec, exc)
        return rec

    def decision(self, agent: str, action: str, **kw: Any) -> AuditRecord:
        return self.record(AuditKind.AGENT_DECISION, agent, action, **kw)

    def human(self, user: str, decision: str, *, subject_id: str, reason: str, **kw: Any) -> AuditRecord:
        return self.record(AuditKind.HUMAN_ACTION, user, decision, rationale=reason, subject_id=subject_id, **kw)

    def denied(self, requester: str, action: str, **kw: Any) -> AuditRecord:
        return self.record(AuditKind.DENIED_ATTEMPT, requester, action, **kw)

    # R10.7: there is deliberately no update/delete API. Attempts are recorded and refused.
    def attempt_modify(self, requester: str, record_id: str, operation: str) -> None:
        self.denied(requester, f"audit_{operation}_denied", subject_id=record_id,
                    rationale="audit records are append-only")
        raise PermissionError("audit records are append-only")

    def query(self, **criteria: Any) -> list[AuditRecord]:
        return self.ledger.query(**criteria)

    def retry_pending(self) -> int:
        """Flush preserved entries once the ledger is back. Returns how many were written."""
        written, still = 0, []
        for rec in self.pending:
            try:
                self.ledger.append(rec)
                written += 1
            except Exception:
                still.append(rec)
        self.pending = still
        return written

    def _preserve(self, rec: AuditRecord, exc: Exception) -> None:
        self.pending.append(rec)
        try:
            self.blobs.write("audit-pending", f"{rec.record_id}.json", rec.model_dump_json().encode())
        except Exception:
            pass  # in-memory copy still held; the alert below is what matters
        self.notifier.send(
            OPS_TEAM, "Audit write failed",
            json.dumps({"record_id": rec.record_id, "action": rec.action, "error": str(exc)}),
            severity="critical",
        )
