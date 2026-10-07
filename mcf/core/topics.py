"""Cloud-neutral topic names (Service Bus topics / Pub/Sub topics / SNS topics)."""
ORDERS_RECEIVED = "orders.received"
ORDERS_LIFECYCLE = "orders.lifecycle"
OMS_OUTBOUND = "oms.outbound"            # messages back to Corp B order management
EXCEPTION_SIGNALS = "exceptions.signals"
EXCEPTION_UPDATES = "exceptions.updates"
APPROVAL_DECISIONS = "approvals.decisions"
HANDOFF_EVENTS = "handoffs.events"         # created / completed, for the requesting agent and the partner connector
HANDOFF_UPDATES = "handoffs.updates"       # acknowledgements and completions reported by the responsible party
TIMERS = "timers"                         # scheduled messages; event.type names the timer
INVENTORY_EVENTS = "inventory.events"     # node inventory events, validated by the Integration_Layer
INVENTORY_STATE = "inventory.state"       # Reconciliation -> Planning: a SKU x node position changed
DISPOSITION_EVENTS = "disposition.events" # Rebound receipt / removal confirmations
SHIPMENT_FACTS = "shipments.facts"        # per-package facts from MCF tracking (mode, distance, cost)
CORPB_OUTBOUND = "corpb.outbound"         # messages to Corp B systems other than OMS (decant counts, alerts feeds)
FEEDS_ARRIVED = "feeds.arrived"           # a partner file landed in storage (Event Grid BlobCreated)
