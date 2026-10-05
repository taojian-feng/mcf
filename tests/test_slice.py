"""Vertical slice: order intake -> MCF submission -> lifecycle -> exception handling, all in memory."""
from datetime import date, timedelta

import pytest
from pydantic import BaseModel, Field

from conftest import advance, order_payload, send_order, status
from mcf.core import topics
from mcf.core.models import (
    ApprovalCategory, AuditKind, ConsumerOrder, ExceptionCategory, ExceptionStatus, OwningParty,
    UnhappyPathSignal,
)
from mcf.core.ports import Event
from mcf.services.governance import AccessDenied, Purpose


def order_record(system, order_id="ord-1"):
    return system.ports.store.get("orders", order_id)


def cases(system):
    return system.ports.store.query("exception_cases")


def signal(system, signal_type, order_id="ord-1", **details):
    sig = UnhappyPathSignal(signal_type=signal_type, source="test", order_id=order_id, details=details)
    system.ports.bus.publish(Event(topics.EXCEPTION_SIGNALS, signal_type, sig.model_dump(mode="json"),
                                   system.ports.clock.now()))


# ---------------------------------------------------------------- R5 + R7 + R9 happy path

def test_happy_path_order_to_delivered_on_time(system):
    send_order(system, order_payload())
    mcf = system.ports.mcf
    [req] = mcf.ops("CreateFulfillmentOrder")
    assert req["packagingInstruction"] == "BLANK_BOX_OVERPACK"            # R7.7
    assert req["destinationAddress"]["addressLine1"] == "1 Pine St"        # R9.6 address unmasked
    assert "Jane" not in str(req)                                          # R9.6 other PII masked

    for s in ("acknowledged", "processing", "packed", "shipped", "out_for_delivery"):
        status(system, "ord-1", s)
    system.ports.bus.publish(Event(topics.ORDERS_LIFECYCLE, "tracking",
                                   {"order_id": "ord-1", "carrier": "UPS", "tracking_number": "1Z9"},
                                   system.ports.clock.now()))
    advance(system, days=1)
    status(system, "ord-1", "delivered")

    rec = order_record(system)
    assert [t["state"] for t in rec["transitions"]] == [
        "received", "acknowledged", "processing", "packed", "shipped", "out_for_delivery", "delivered"]
    assert rec["on_time"] is True and rec["delivered_on"] == "2026-10-06"     # R5.7
    assert system.ports.bus.events(topics.OMS_OUTBOUND, "tracking_update")     # R5.6
    assert len(mcf.ops("CreateFulfillmentOrder")) == 1                         # ack cancelled the retry timer
    audit = system.audit.query(order_id="ord-1")
    assert audit and all(r.owning_party is not None for r in audit)            # R10.1, R11.1


def test_late_delivery_marked_late(system):
    send_order(system, order_payload(promise=2))
    status(system, "ord-1", "acknowledged")
    advance(system, days=3)
    status(system, "ord-1", "delivered")
    assert order_record(system)["on_time"] is False


@pytest.mark.parametrize("kw,reason", [({"upc": "999"}, "not in the pilot catalog"),
                                       ({"state": "AK"}, "outside the US 48")])
def test_ineligible_orders_rejected_to_oms(system, kw, reason):
    send_order(system, order_payload(**kw))
    [rej] = system.ports.bus.events(topics.OMS_OUTBOUND, "order_rejected")
    assert reason in rej.payload["reasons"][0]                                 # R5.9
    assert system.ports.mcf.ops("CreateFulfillmentOrder") == []
    assert order_record(system) is None


def test_invalid_backward_transition_ignored(system):
    send_order(system, order_payload())
    status(system, "ord-1", "shipped")
    status(system, "ord-1", "processing")
    assert order_record(system)["state"] == "shipped"


# ---------------------------------------------------------------- R5.2 / R5.8 ack timeout -> exception

def test_ack_timeout_retries_once_then_raises_exception(system):
    send_order(system, order_payload())
    advance(system, minutes=30)
    assert len(system.ports.mcf.ops("CreateFulfillmentOrder")) == 2           # R5.2 retry
    advance(system, minutes=30)
    rec = order_record(system)
    assert rec["submission_failed"] is True                                    # R5.8
    [case] = cases(system)
    assert case["signal"]["signal_type"] == "fulfillment_submission_failed"
    assert case["status"] == "escalated"                                       # R3.4 low confidence -> human
    assert system.ports.notifier.to("ops-oncall@corpb")
    assert rec["exceptions"][-1]["case_id"] == case["case_id"]                 # R3.6


# ---------------------------------------------------------------- R7.4 - R7.6 retry policy

def test_retryable_errors_back_off_exponentially(system):
    system.ports.mcf.script["CreateFulfillmentOrder"] = ["throttled", "timeout", "service_unavailable"]
    send_order(system, order_payload())
    assert system.sleeps == [1, 2, 4]
    assert len(system.ports.mcf.ops("CreateFulfillmentOrder")) == 4


def test_retries_exhausted_alerts_and_audits(system):
    system.ports.mcf.script["CreateFulfillmentOrder"] = ["timeout"] * 6
    send_order(system, order_payload())
    assert system.sleeps == [1, 2, 4, 8, 16]                                    # 5 retries
    assert any(m["subject"].startswith("MCF CreateFulfillmentOrder") for m in system.ports.notifier.to("ops-team"))
    assert [r for r in system.audit.query(order_id="ord-1") if r.kind == AuditKind.INTEGRATION_FAILURE]


def test_non_retryable_error_fails_fast(system):
    system.ports.mcf.script["CreateFulfillmentOrder"] = ["validation"]
    send_order(system, order_payload())
    assert system.sleeps == []
    assert len(system.ports.mcf.ops("CreateFulfillmentOrder")) == 1
    assert not system.ports.notifier.to("ops-team")


# ---------------------------------------------------------------- R5.4 / R5.5 / R5.10 promise monitoring -> R3 playbook

def test_projected_breach_raises_one_alert_and_runs_delay_playbook(system):
    late = (date(2026, 10, 5) + timedelta(days=4)).isoformat()
    system.ports.mcf.responder["GetFulfillmentOrder"] = lambda p: {"estimatedArrivalDate": late}
    send_order(system, order_payload())
    status(system, "ord-1", "acknowledged")
    advance(system, hours=4)
    advance(system, hours=4)  # second check, same projection: no duplicate alert

    assert len(system.ports.bus.events(topics.EXCEPTION_SIGNALS, "projected_promise_breach")) == 1
    [case] = cases(system)
    assert case["classification"]["category"] == ExceptionCategory.DELIVERY_DELAY.value
    assert case["playbook_id"] == "PB-DELAY" and case["completed_steps"] == ["notify_oms_delay",
                                                                             "request_carrier_trace"]
    assert system.ports.bus.events(topics.OMS_OUTBOUND, "delay")
    [view] = system.handoffs.open_view()                                       # R11.2, R11.5
    assert view["action"] == "carrier_trace" and view["responsible_party"] == "corp_a"
    assert case["status"] == "open"

    system.handoffs.complete(view["handoff_id"], by="mcf-ops@corpa")           # R11.3
    assert system.ports.store.get("exception_cases", case["case_id"])["status"] == "resolved"
    steps = [r for r in system.audit.query(actor="exception_agent") if r.action == "playbook_step_executed"]
    assert {r.result["version"] for r in steps} == {"1.0"}                     # R3.2


def test_two_missing_projections_raise_exception(system):
    send_order(system, order_payload())
    status(system, "ord-1", "acknowledged")
    advance(system, hours=8)
    [sig] = system.ports.bus.events(topics.EXCEPTION_SIGNALS, "projected_delivery_unavailable")
    assert sig.payload["details"]["last_state"] == "acknowledged"              # R5.10


# ---------------------------------------------------------------- R3.5 + R8 late cancellation via approval

def test_post_ship_cancellation_needs_approval_and_role(system):
    send_order(system, order_payload())
    status(system, "ord-1", "shipped")
    signal(system, "late_cancellation_request")
    [case] = cases(system)
    assert case["playbook_id"] == "PB-CANCEL-POSTSHIP" and case["status"] == "awaiting_approval"
    assert system.handoffs.open_view() == []                                   # R8.2 pending blocks execution

    with pytest.raises(PermissionError):
        system.approvals.decide("mallory@corpa-retail", case["approval_id"], approve=True)
    assert [r for r in system.audit.query(actor="mallory@corpa-retail") if r.kind == AuditKind.DENIED_ATTEMPT]

    system.approvals.decide("alice@corpb", case["approval_id"], approve=True)  # R8.3
    case = system.ports.store.get("exception_cases", case["case_id"])
    assert case["completed_steps"] == ["request_return_intercept", "notify_oms_cancellation"]
    assert [r for r in system.audit.query(actor="alice@corpb") if r.action == "approve"]


def test_pre_ship_cancellation_is_autonomous(system):
    send_order(system, order_payload())
    status(system, "ord-1", "processing")
    signal(system, "late_cancellation_request")
    [case] = cases(system)
    assert case["playbook_id"] == "PB-CANCEL-PRESHIP"
    assert system.handoffs.open_view()[0]["action"] == "cancel_fulfillment_order"


def test_rejection_blocks_execution_permanently(system):
    send_order(system, order_payload())
    signal(system, "carrier_lost")
    [case] = cases(system)
    system.approvals.decide("alice@corpb", case["approval_id"], approve=False, reason="reship too costly")
    case = system.ports.store.get("exception_cases", case["case_id"])
    assert case["status"] == "escalated" and case["completed_steps"] == []
    with pytest.raises(Exception):
        system.approvals.decide("alice@corpb", case["approval_id"], approve=True)
    assert not system.approvals.is_executable(case["approval_id"])


def test_approval_reminders_and_missing_approver(system):
    signal(system, "carrier_lost", order_id=None)
    [case] = cases(system)
    advance(system, hours=48)
    assert {m["to"] for m in system.ports.notifier.sent if m["subject"].startswith("Reminder")} == {
        "alice@corpb", "bob@corpb"}                                            # R8.5 approver + backup
    advance(system, hours=24)
    assert system.approvals.get(case["approval_id"]).reminders_sent == 2
    assert system.approvals.get(case["approval_id"]).status.value == "pending"

    item = system.approvals.submit(category=ApprovalCategory.DISTRIBUTION_PLAN, owning_party=OwningParty.CORP_B,
                                   agent="planning_agent", subject_id="dp-1", content={}, rationale="draft")
    assert item.routed_to == [] and system.ports.notifier.to("gov@corpb")      # R8.8


def test_playbook_step_failure_halts_and_escalates(system):
    def boom(case):
        raise RuntimeError("carrier API down")
    system.exceptions.step_overrides["request_carrier_trace"] = boom
    signal(system, "projected_promise_breach")
    [case] = cases(system)
    assert case["completed_steps"] == ["notify_oms_delay"] and case["failed_step"] == "request_carrier_trace"
    assert case["status"] == "escalated"                                       # R3.8


def test_unknown_signal_escalates_and_ages_out(system):
    signal(system, "something_weird")
    [case] = cases(system)
    assert case["status"] == "escalated"
    advance(system, days=2)
    assert len([m for m in system.ports.notifier.to("ops-oncall@corpb")
                if "time limit" in m["subject"]]) == 1                         # R3.10
    system.exceptions.close(case["case_id"], by="ops@corpb", resolution="duplicate")
    assert system.ports.store.get("exception_cases", case["case_id"])["status"] == "escalated_and_closed"


# ---------------------------------------------------------------- R9 governance

def test_insight_isolation_and_revocation(system, directory):
    system.governance.check_insight_access("planning_agent", "forecast/2026-10-05")
    with pytest.raises(AccessDenied):
        system.governance.check_insight_access("corpa-retail-recs", "forecast/2026-10-05")
    [denied] = [r for r in system.audit.query(actor="corpa-retail-recs")]
    assert denied.kind == AuditKind.DENIED_ATTEMPT and denied.input_refs == ["forecast/2026-10-05"]

    directory.pilot_list.discard("planning_agent")
    advance(system, minutes=5)                                                  # cache TTL < 15 min (R9.8)
    with pytest.raises(AccessDenied):
        system.governance.check_insight_access("planning_agent", "forecast/2026-10-05")


def test_decision_view_masks_all_pii(system):
    order = ConsumerOrder.model_validate(order_payload())
    view = system.governance.view_order(order, Purpose.DECISION, requester="reporting_agent")
    text = str(view)
    for secret in ("Jane Consumer", "jane@example.com", "555-0100", "1 Pine St", "98101"):
        assert secret not in text
    assert view["consumer"]["address"]["state"] == "WA"


# ---------------------------------------------------------------- R10 audit

def test_audit_failure_preserved_and_alerted(system):
    system.ports.ledger.fail_next_appends = 1
    system.audit.decision("planning_agent", "x")
    assert len(system.audit.pending) == 1
    assert system.ports.notifier.to("ops-team")[0]["severity"] == "critical"
    assert system.audit.retry_pending() == 1 and system.audit.pending == []


def test_audit_is_append_only(system):
    rec = system.audit.decision("planning_agent", "x")
    with pytest.raises(PermissionError):
        system.audit.attempt_modify("mallory", rec.record_id, "delete")
    with pytest.raises(PermissionError):
        system.ports.ledger.delete(rec.record_id)
    assert [r.action for r in system.audit.query(actor="mallory")] == ["audit_delete_denied"]


# ---------------------------------------------------------------- R11 handoffs

def test_handoff_ack_and_overdue_escalations(system):
    h = system.handoffs.create("reship_order", requesting_party=OwningParty.CORP_B, requested_by="test",
                               blocked_steps=["x"], context={"order_id": "ord-9"})
    advance(system, hours=4)
    advance(system, hours=20)
    subjects = [m["subject"] for m in system.ports.notifier.to("mcf-escalation@corpa")]
    assert subjects == ["Handoff not acknowledged: reship_order", "Handoff overdue: reship_order"]
    assert len(system.handoffs.get(h.handoff_id).escalations) == 2


def test_unmapped_action_alerts_program_lead(system):
    assert system.handoffs.owning_party("teleport_inventory", actor="test") is None
    assert system.ports.notifier.to("lead@corpb")                              # R11.6


# ---------------------------------------------------------------- R7.2 / R7.3 feeds

class ForecastRecord(BaseModel):
    upc: str = Field(pattern=r"^\d{12}$")
    week: date
    region: str
    units: int = Field(ge=0)


def test_feed_validation_quarantines_bad_records(system):
    res = system.integration.ingest_feed("fc-2026-10-05", [
        {"upc": "190000000001", "week": "2026-10-12", "region": "west", "units": 40},
        {"upc": "bad", "week": "2026-10-12", "region": "west", "units": -1},
    ], ForecastRecord, originating_party_contact="data@corpb")
    assert len(res.valid) == 1 and res.rejected[0]["index"] == 1
    assert ("quarantine", "fc-2026-10-05.json") in system.ports.blobs.blobs
    [report] = system.ports.notifier.to("data@corpb")
    assert "upc" in report["body"] and "units" in report["body"]
