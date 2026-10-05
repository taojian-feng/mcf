"""Reconciliation_Agent (R2). Not built yet.

Planned shape: event-sourced inventory (inventory_events container, append-only) with a materialized
inventory_state view updated from the bus (R2.1), a query API by SKU / node / network incl. in-transit
(R2.2), a daily reconciliation job (R2.3, R2.4, R2.8), decant counts pushed to Corp B within 1 hour
(R2.5), point-in-time replay for history (R2.6), and rejected events audited + reported (R2.7).
"""
