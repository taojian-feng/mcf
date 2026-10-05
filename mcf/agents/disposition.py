"""Disposition_Agent (R4). Not built yet.

Planned shape: daily aging + storage-cost job; one alert per (SKU, node, threshold) via an idempotency
key (R4.2); Disposition_Recommendation -> ApprovalService (R4.3-R4.5); on approval initiate via
IntegrationLayer within 24h (R4.6); completion-window timer raises an UnhappyPathSignal
'disposition_overdue' to the Exception agent (R4.8); rejection resumes daily evaluation (R4.9).
"""
