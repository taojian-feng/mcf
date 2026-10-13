"""Planning judgment: which demand to plan with, and a second look at every restock, move and aged-stock call.

The planning and disposition agents propose by fixed rules from the client's forecast. Real forecasts drift:
an item takes off, another dies, a region shifts, a single bulk order makes a quiet item look busy. Three
modes (`PilotConfig.plan_review`) decide who makes those calls:

  rules    the forecast and fixed rules, as before
  sensing  a fixed rule: when recent orders drift from the plan, plan with 70% orders + 30% forecast
  claude   Claude reads the item's evidence (forecast, the client's sales feed, our daily orders with the
           largest single line, stock and inbound by node) and decides the demand to plan with, and reviews
           each restock, rebalancing plan and aged-stock recommendation before it goes out

Claude decides inside bounds the code enforces (a demand call is at most 1.5x what was ordered or planned; a restock can shrink but never grow, and never more
than the source holds; a move never grows; a disposition never exceeds the aged units), every call is
audited with its reasoning, and a failed or missing call leaves the rules' answer in place. Claude's calls
are advice to the same approval gate as before: what needed a person's approval still does.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from tower.core.ports import LLM

SENSING_WEIGHT = 0.7      # sensing mode: share of recent orders in the planned rate
# Bounds on Claude's calls. The first evaluation in Azure (2026-10-12) had Claude ship the most orders but
# carry the most excess stock, with demand calls up to 3x and restocks up to 2x the rules: now a demand call is
# at most 1.5x what was ordered or planned, and a review can shrink or hold a restock but never grow it.
DEMAND_CAP = 1.5
RESTOCK_GROWTH = 1.0

_ROLE = ("You are the demand and inventory planner in a logistics provider's control tower. The provider "
         "stores and ships retail clients' items from two regional fulfillment centres (FC-WEST, FC-EAST), "
         "refilled from bulk storage centres (ISC-X west, ISC-Y east). Stockouts lose sales; excess stock costs "
         "storage and ends up disposed of; every transfer costs freight. Excess is as real a cost as a "
         "stockout: when the evidence is mixed, plan the lower of the plausible rates. Use only the evidence given. ")

DEMAND_PROMPT = _ROLE + (
    "Recent orders for this item have drifted from the client's forecast. Decide the daily demand per region "
    "to plan the next week with. Weigh: whether the change is sustained across days and orders or comes from "
    "one or two large order lines (a one-off bulk order is not a trend); how long it has lasted; whether the "
    "client's sales feed agrees; whether low orders could be caused by our own stockout (no stock at the "
    "region's FC) rather than by demand. Basis: forecast (keep the plan), orders (follow recent orders), blend, "
    "or other. Set tell_client when the client's forecast looks wrong and their planner should know.")

RESTOCK_PROMPT = _ROLE + (
    "Review a proposed restock of a fulfillment centre from bulk storage before it goes out. The rules sized it "
    "to bring the centre to the target cover from the planned demand. Decide: approve (as proposed), adjust "
    "(give a smaller quantity; a restock is never made larger), hold (do not move stock now) or escalate "
    "(a person must look). Hold when the shortfall is not real (demand evidence says the plan is too high) or "
    "enough stock is already inbound; adjust when the evidence says less is enough; escalate only when the "
    "evidence conflicts.")

PLAN_PROMPT = _ROLE + (
    "Review a proposed rebalancing plan: moves of stock between the regional centres, drafted because one "
    "region's stock is far above and another's far below the planned demand. For each move give the quantity "
    "to move (0 drops it, never more than proposed). Drop or cut moves that chase a demand blip, would leave "
    "the sending region short, or cost more freight than the stock is worth moving.")

DISPOSITION_PROMPT = _ROLE + (
    "Review a proposed disposition of aged stock (sent to the liquidator Rebound, or removed). Decide: dispose "
    "(as proposed, or fewer units) or keep (the stock is still selling and will sell through in reasonable time). "
    "Do not keep stock that will not sell; storage keeps costing.")


class RegionRate(BaseModel):
    region: str
    daily_units: float = Field(ge=0)


class DemandCall(BaseModel):
    rates: list[RegionRate]
    basis: Literal["forecast", "orders", "blend", "other"]
    confidence: float = Field(ge=0, le=1)
    tell_client: bool = False
    rationale: str


class RestockReview(BaseModel):
    decision: Literal["approve", "adjust", "hold", "escalate"]
    qty: int | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0, le=1)
    rationale: str


class MoveQty(BaseModel):
    index: int = Field(ge=0)
    qty: int = Field(ge=0)


class PlanReview(BaseModel):
    moves: list[MoveQty]
    rationale: str


class DispositionReview(BaseModel):
    decision: Literal["dispose", "keep"]
    qty: int | None = Field(default=None, ge=0)
    rationale: str


class PlanReviewer:
    """Asks Claude for planning calls. Every method returns None when there is no Claude, the mode is not
    `claude`, or the call fails: the caller then keeps its rules' answer."""

    def __init__(self, llm: LLM | None, mode: str):
        self.llm, self.mode = llm, mode

    @property
    def active(self) -> bool:
        return self.mode == "claude" and self.llm is not None

    def _ask(self, system: str, evidence: dict[str, Any], schema: type[BaseModel]) -> Any:
        if not self.active:
            return None
        try:
            return self.llm.structured(system=system, prompt=json.dumps(evidence, default=str), schema=schema)
        except Exception:  # noqa: BLE001 - the rules' answer stands
            return None

    def demand(self, evidence: dict[str, Any]) -> DemandCall | None:
        return self._ask(DEMAND_PROMPT, evidence, DemandCall)

    def restock(self, evidence: dict[str, Any]) -> RestockReview | None:
        return self._ask(RESTOCK_PROMPT, evidence, RestockReview)

    def plan(self, evidence: dict[str, Any]) -> PlanReview | None:
        return self._ask(PLAN_PROMPT, evidence, PlanReview)

    def disposition(self, evidence: dict[str, Any]) -> DispositionReview | None:
        return self._ask(DISPOSITION_PROMPT, evidence, DispositionReview)
