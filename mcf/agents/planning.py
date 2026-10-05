"""Planning_Agent (R1). Not built yet: next iteration after the order-to-exception slice.

Planned shape: daily Container Apps Job + feed-arrival subscriber.
- R1.1/R1.2: validate forecast feeds through IntegrationLayer.ingest_feed; error report to Corp B data contact.
- R1.3/R1.4: daily ATS_Cover per SKU x node; Replenishment_Recommendation on threshold breach.
- R1.5: daily imbalance assessment vs regionalized demand.
- R1.6: Distribution_Plan draft -> ApprovalService (category distribution_plan, Corp B).
- R1.7/R1.9: cold-start baseline until 4 weeks of sales; flag SKUs with no baseline.
- R1.8: missing-feed timer on the bus at the end of the delivery window.
CorpB_Insight_Data reads go through GovernanceService.check_insight_access.
"""
