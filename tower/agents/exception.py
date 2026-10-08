"""Exception_Agent (R3): classify, resolve via versioned playbooks, escalate, track to terminal state."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from tower.core import topics
from tower.core.config import PilotConfig
from tower.core.models import (
    ApprovalCategory, Classification, ExceptionCase, ExceptionCategory, ExceptionStatus, OrderState,
    OwningParty, TERMINAL_EXCEPTION_STATES, UnhappyPathSignal,
)
from tower.core.ports import LLM, Clock, Directory, DocumentStore, Event, MessageBus, Notifier
from tower.services.approval import ApprovalService
from tower.services.audit import AuditService
from tower.services.handoffs import HandoffService

AGENT = "exception_agent"
CASES = "exception_cases"
OPERATOR = "exception_operator"

C = ExceptionCategory


# ---------------------------------------------------------------- classification (R3.1, R3.4)

# Deterministic signal-type priors. Signals that do not map cleanly onto one of the five
# categories get a low confidence on purpose, so they go to a human (R3.4).
RULES: dict[str, dict[ExceptionCategory, float]] = {
    "projected_promise_breach": {C.DELIVERY_DELAY: 0.95},
    "carrier_delay": {C.DELIVERY_DELAY: 0.9},
    "carrier_lost": {C.LOST_SHIPMENT: 0.95},
    "tracking_stalled": {C.LOST_SHIPMENT: 0.55, C.DELIVERY_DELAY: 0.45},
    "damage_report": {C.DAMAGED_PRODUCT: 0.95},
    "delivery_failed": {C.FAILED_DELIVERY: 0.9},
    "address_undeliverable": {C.FAILED_DELIVERY: 0.95},
    "late_cancellation_request": {C.LATE_CANCELLATION: 0.99},
    "projected_delivery_unavailable": {C.DELIVERY_DELAY: 0.6, C.LOST_SHIPMENT: 0.3},
    "fulfillment_submission_failed": {C.DELIVERY_DELAY: 0.4, C.FAILED_DELIVERY: 0.3},
    "disposition_overdue": {C.LOST_SHIPMENT: 0.4},
}

SYSTEM_PROMPT = (
    "You classify fulfillment exceptions for a multi-channel fulfillment pilot into exactly one of: "
    + ", ".join(c.value for c in ExceptionCategory)
    + ". Return a calibrated confidence in [0,1], a probability per candidate category, and a one-sentence "
    "rationale. If the evidence does not support any category, return category null with low confidence."
)


# What an investigator reads about the order: its own history, no consumer data.
ORDER_EVIDENCE = ("order_id", "placed_at", "promised_by", "delivery_promise_days", "state", "transitions",
                  "projected_delivery", "missing_projection_checks", "tracking", "delivered_on")

INVESTIGATE_PROMPT = SYSTEM_PROMPT + (
    " You also get the order's record as our system saw it when the signal arrived: its lifecycle states with "
    "times, the promised delivery date, the latest projected delivery date, and carrier tracking events. Use "
    "that evidence. If the order looks healthy and the signal is a false alarm, return category null."
)


def order_evidence(order: dict | None) -> dict | None:
    return {k: order.get(k) for k in ORDER_EVIDENCE} if order else None


class Classifier:
    """Rules first; with Claude, investigate. Given the order's record, Claude weighs the signal against the
    order's history (a failed delivery attempt and a late parcel both raise a promise breach, and only the
    tracking tells them apart). Without the record, Claude only sees signals the rules find ambiguous."""

    def __init__(self, llm: LLM | None = None):
        self.llm = llm

    def classify(self, sig: UnhappyPathSignal, order: dict | None = None) -> Classification:
        prior = RULES.get(sig.signal_type, {})
        if self.llm is not None and order is not None:
            prompt = {"signal": sig.model_dump(mode="json"), "order_record": order}
            out = self.llm.structured(system=INVESTIGATE_PROMPT, prompt=json.dumps(prompt, default=str),
                                      schema=Classification)
            if out is not None:
                return out
        if prior and max(prior.values()) >= 0.9:
            cat, conf = max(prior.items(), key=lambda kv: kv[1])
            return Classification(category=cat, confidence=conf,
                                  candidates={k.value: v for k, v in prior.items()},
                                  rationale=f"rule: signal type '{sig.signal_type}'")
        if self.llm is not None and order is None:
            out = self.llm.structured(system=SYSTEM_PROMPT, prompt=json.dumps(sig.model_dump(mode="json")),
                                      schema=Classification)
            if out is not None:
                return out
        if not prior:
            return Classification(category=None, confidence=0.0, rationale=f"no rule for '{sig.signal_type}'")
        cat, conf = max(prior.items(), key=lambda kv: kv[1])
        return Classification(category=cat, confidence=conf, candidates={k.value: v for k, v in prior.items()},
                              rationale=f"rule (ambiguous): signal type '{sig.signal_type}'")


# ---------------------------------------------------------------- playbooks (R3.2, R3.3, R3.5)

@dataclass(frozen=True)
class Playbook:
    playbook_id: str
    version: str
    category: ExceptionCategory
    autonomous: bool
    steps: tuple[str, ...]
    lifecycle_states: frozenset[OrderState] | None = None


PRE_SHIP = frozenset({OrderState.RECEIVED, OrderState.ACKNOWLEDGED, OrderState.PROCESSING, OrderState.PACKED})
POST_SHIP = frozenset({OrderState.SHIPPED, OrderState.OUT_FOR_DELIVERY})

DEFAULT_PLAYBOOKS = (
    Playbook("PB-DELAY", "1.0", C.DELIVERY_DELAY, True, ("notify_oms_delay", "request_carrier_trace")),
    Playbook("PB-FAILED", "1.0", C.FAILED_DELIVERY, True, ("request_carrier_trace", "notify_oms_delivery_failed")),
    Playbook("PB-LOST", "1.0", C.LOST_SHIPMENT, False, ("request_reship", "notify_oms_reship")),
    Playbook("PB-DAMAGED", "1.0", C.DAMAGED_PRODUCT, False, ("request_return_intercept", "request_reship")),
    Playbook("PB-CANCEL-PRESHIP", "1.0", C.LATE_CANCELLATION, True,
             ("request_mcf_cancellation", "notify_oms_cancellation"), PRE_SHIP),
    Playbook("PB-CANCEL-POSTSHIP", "1.0", C.LATE_CANCELLATION, False,
             ("request_return_intercept", "notify_oms_cancellation"), POST_SHIP),
)

# Step name -> cross-boundary action it requests from Corp A (R11.2).
HANDOFF_STEPS = {
    "request_carrier_trace": "carrier_trace",
    "request_reship": "reship_order",
    "request_return_intercept": "return_intercept",
    "request_mcf_cancellation": "cancel_fulfillment_order",
}


class StepFailed(Exception):
    pass


@dataclass
class ExceptionAgent:
    store: DocumentStore
    bus: MessageBus
    approvals: ApprovalService
    handoffs: HandoffService
    audit: AuditService
    notifier: Notifier
    directory: Directory
    clock: Clock
    config: PilotConfig
    classifier: Classifier = field(default_factory=Classifier)
    playbooks: tuple[Playbook, ...] = DEFAULT_PLAYBOOKS
    step_overrides: dict[str, Callable[[ExceptionCase], None]] = field(default_factory=dict)  # tests/extensions

    def __post_init__(self) -> None:
        self.bus.subscribe(topics.EXCEPTION_SIGNALS, self.on_signal, subscription="exception-agent")
        self.bus.subscribe(topics.APPROVAL_DECISIONS, self.on_approval, subscription="exception-agent")
        self.bus.subscribe(topics.HANDOFF_EVENTS, self.on_handoff, subscription="exception-agent")
        self.bus.subscribe(topics.TIMERS, self.on_timer, subscription="exception-agent")

    # ---------------------------------------------------------------- intake

    def on_signal(self, event: Event) -> None:
        sig = UnhappyPathSignal.model_validate(event.payload)
        if self.store.query(CASES, signal_id=sig.signal_id):
            return  # duplicate delivery
        case = ExceptionCase(signal=sig, opened_at=self.clock.now())
        order = order_evidence(self.store.get("orders", sig.order_id)) if sig.order_id else None
        if order is not None:
            case.history.append({"at": self.clock.now().isoformat(), "event": "order_record_read",
                                 "state": order.get("state"), "tracking_events": len(order.get("tracking") or [])})
        case.classification = self.classifier.classify(sig, order)
        cls = case.classification
        self._log(case, "classified", category=cls.category.value if cls.category else None,
                  confidence=cls.confidence)
        self._audit(case, "classify_exception", rationale=cls.rationale,
                    result={"category": cls.category.value if cls.category else None,
                            "confidence": cls.confidence, "candidates": cls.candidates})

        limit = (self.config.resolution_time_limits.get(cls.category) if cls.category
                 else min(self.config.resolution_time_limits.values()))
        self.bus.publish(Event(topics.TIMERS, "exception_age_check", {"case_id": case.case_id},
                               self.clock.now(), correlation_id=case.case_id),
                         deliver_at=self.clock.now() + limit)

        if cls.category is None or cls.confidence < self.config.classification_confidence_threshold:
            self._save(case)
            self._escalate(case, "classification confidence below threshold",
                           {"candidates": cls.candidates, "confidence": cls.confidence})
            return

        self._notify_orchestration(case)
        playbook = self._match(case)
        if playbook is None:
            self._save(case)
            self._escalate(case, "no resolution playbook matches", {"category": cls.category.value})
            return
        case.playbook_id, case.playbook_version = playbook.playbook_id, playbook.version
        if playbook.autonomous:
            self._run(case, playbook)
        else:
            self._request_approval(case, playbook)

    def _match(self, case: ExceptionCase) -> Playbook | None:
        cat = case.classification.category
        state = None
        if cat == C.LATE_CANCELLATION and case.signal.order_id:
            order = self.store.get("orders", case.signal.order_id)
            state = OrderState(order["state"]) if order else None
            case.history.append({"at": self.clock.now().isoformat(), "event": "lifecycle_state_read",
                                 "state": state.value if state else None})
        for pb in self.playbooks:
            if pb.category != cat:
                continue
            if pb.lifecycle_states is not None and state not in pb.lifecycle_states:
                continue
            return pb
        return None

    # ---------------------------------------------------------------- execution (R3.2, R3.8)

    def _run(self, case: ExceptionCase, pb: Playbook) -> None:
        for step in pb.steps:
            if step in case.completed_steps:
                continue
            try:
                self._execute(case, step)
            except Exception as exc:
                case.failed_step = step
                self._audit(case, "playbook_step_failed", rationale=str(exc),
                            result={"step": step, "playbook": pb.playbook_id, "version": pb.version})
                self._save(case)
                self._escalate(case, f"playbook step '{step}' failed: {exc}",
                               {"completed_steps": case.completed_steps, "failed_step": step})
                return
            case.completed_steps.append(step)
            self._audit(case, "playbook_step_executed",
                        result={"step": step, "playbook": pb.playbook_id, "version": pb.version})
        case.status = ExceptionStatus.RESOLVED if not case.pending_handoffs else ExceptionStatus.OPEN
        self._save(case)
        self._notify_orchestration(case)

    def _execute(self, case: ExceptionCase, step: str) -> None:
        if step in self.step_overrides:
            self.step_overrides[step](case)
            return
        sig = case.signal
        if step in HANDOFF_STEPS:
            h = self.handoffs.create(
                HANDOFF_STEPS[step], requesting_party=OwningParty.CORP_B, requested_by=AGENT,
                blocked_steps=[f"{case.case_id}:{step}"],
                context={"case_id": case.case_id, "order_id": sig.order_id, "step": step, "details": sig.details},
            )
            case.pending_handoffs.append(h.handoff_id)
        elif step.startswith("notify_oms_"):
            self.bus.publish(Event(topics.OMS_OUTBOUND, step.removeprefix("notify_oms_"),
                                   {"order_id": sig.order_id, "case_id": case.case_id,
                                    "category": case.classification.category.value},
                                   self.clock.now(), correlation_id=sig.order_id))
        else:
            raise StepFailed(f"no executor for step {step}")

    # ---------------------------------------------------------------- approval path (R3.3)

    def _request_approval(self, case: ExceptionCase, pb: Playbook) -> None:
        party = self.handoffs.owning_party("approve_exception_resolution", actor=AGENT) or OwningParty.CORP_B
        item = self.approvals.submit(
            category=ApprovalCategory.EXCEPTION_RESOLUTION, owning_party=party, agent=AGENT,
            subject_id=case.case_id,
            content={"order_id": case.signal.order_id, "playbook": pb.playbook_id, "version": pb.version,
                     "steps": list(pb.steps), "signal": case.signal.model_dump(mode="json")},
            rationale=case.classification.rationale,
        )
        case.approval_id, case.status = item.approval_id, ExceptionStatus.AWAITING_APPROVAL
        self._save(case)

    def on_approval(self, event: Event) -> None:
        if event.payload.get("originating_agent") != AGENT:
            return
        case = self._load(event.payload["subject_id"])
        if case is None or case.status != ExceptionStatus.AWAITING_APPROVAL:
            return
        if event.type == "approved" and self.approvals.is_executable(case.approval_id):
            pb = next(p for p in self.playbooks
                      if p.playbook_id == case.playbook_id and p.version == case.playbook_version)
            case.status = ExceptionStatus.OPEN
            self._run(case, pb)
        else:
            case.status = ExceptionStatus.ESCALATED
            self._save(case)
            self._escalate(case, f"resolution rejected: {event.payload.get('reason')}", {})

    # ---------------------------------------------------------------- handoffs, closure, ageing (R3.7, R3.10)

    def on_handoff(self, event: Event) -> None:
        if event.type != "completed":
            return
        case_id = (event.payload.get("context") or {}).get("case_id")
        case = self._load(case_id) if case_id else None
        if case is None or event.payload["handoff_id"] not in case.pending_handoffs:
            return
        case.pending_handoffs.remove(event.payload["handoff_id"])
        self._log(case, "handoff_completed", handoff_id=event.payload["handoff_id"])
        pb = next((p for p in self.playbooks if p.playbook_id == case.playbook_id), None)
        if not case.pending_handoffs and pb and set(pb.steps) <= set(case.completed_steps):
            case.status = ExceptionStatus.RESOLVED
            self._audit(case, "exception_resolved")
            self._notify_orchestration(case)
        self._save(case)

    def close(self, case_id: str, *, by: str, resolution: str, cancelled: bool = False) -> ExceptionCase:
        """Human operator closes an escalated case."""
        case = self._load(case_id)
        case.status = ExceptionStatus.CANCELLED if cancelled else ExceptionStatus.ESCALATED_AND_CLOSED
        self._save(case)
        self.audit.human(by, "exception_closed", subject_id=case_id, reason=resolution,
                         order_ids=[case.signal.order_id] if case.signal.order_id else [])
        self._notify_orchestration(case)
        return case

    def on_timer(self, event: Event) -> None:
        if event.type != "exception_age_check":
            return
        case = self._load(event.payload["case_id"])
        if case is None or case.status in TERMINAL_EXCEPTION_STATES:
            return
        age_h = (self.clock.now() - case.opened_at).total_seconds() / 3600
        self._escalate(case, "resolution time limit exceeded",
                       {"age_hours": round(age_h, 1), "status": case.status.value})

    # ---------------------------------------------------------------- helpers

    def _escalate(self, case: ExceptionCase, reason: str, extra: dict) -> None:
        if case.status not in TERMINAL_EXCEPTION_STATES:
            case.status = ExceptionStatus.ESCALATED
        self._save(case)
        body = json.dumps({"case_id": case.case_id, "reason": reason, "signal": case.signal.model_dump(mode="json"),
                           **extra}, indent=2, default=str)
        self.notifier.send(self.directory.contact(OPERATOR) or OPERATOR, f"Exception escalated: {reason}", body,
                           severity="high")
        self._audit(case, "escalate_to_human", rationale=reason, result=extra)
        self._notify_orchestration(case)

    def _notify_orchestration(self, case: ExceptionCase) -> None:
        if not case.signal.order_id:
            return
        cat = case.classification.category if case.classification else None
        self.bus.publish(Event(topics.EXCEPTION_UPDATES, "case_update",
                               {"order_id": case.signal.order_id, "case_id": case.case_id,
                                "category": cat.value if cat else None, "status": case.status.value},
                               self.clock.now(), correlation_id=case.signal.order_id))

    def _audit(self, case: ExceptionCase, action: str, *, rationale: str = "", result: dict | None = None) -> None:
        sig = case.signal
        self.audit.decision(AGENT, action, rationale=rationale, result=result or {}, subject_id=case.case_id,
                            input_refs=[f"signal:{sig.signal_id}"],
                            order_ids=[sig.order_id] if sig.order_id else [],
                            skus=[sig.sku] if sig.sku else [], node_ids=[sig.node_id] if sig.node_id else [],
                            owning_party=OwningParty.CORP_B)

    def _log(self, case: ExceptionCase, what: str, **kw) -> None:
        case.history.append({"at": self.clock.now().isoformat(), "event": what, **kw})

    def _load(self, case_id: str) -> ExceptionCase | None:
        doc = self.store.get(CASES, case_id)
        return ExceptionCase.model_validate(doc) if doc else None

    def _save(self, case: ExceptionCase) -> None:
        doc = case.model_dump(mode="json")
        doc["signal_id"] = case.signal.signal_id  # flattened for idempotency lookups
        self.store.put(CASES, case.case_id, doc)
