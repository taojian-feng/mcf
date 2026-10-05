"""Cloud-neutral topic names (Service Bus topics / Pub/Sub topics / SNS topics)."""
ORDERS_RECEIVED = "orders.received"
ORDERS_LIFECYCLE = "orders.lifecycle"
OMS_OUTBOUND = "oms.outbound"            # messages back to Corp B order management
EXCEPTION_SIGNALS = "exceptions.signals"
EXCEPTION_UPDATES = "exceptions.updates"
APPROVAL_DECISIONS = "approvals.decisions"
HANDOFF_EVENTS = "handoffs.events"
TIMERS = "timers"                         # scheduled messages; event.type names the timer
